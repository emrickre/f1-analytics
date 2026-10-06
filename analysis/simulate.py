"""Симулятор стратегии с Safety Car (Монте-Карло).

Время гонки одной машины по плану пит-стопов складывается из:
* кругов при зелёном флаге — модель износа трассы из первого исследования
  (`model.race_params`: смещение состава + деградация × возраст шин);
* пит-стопов — потеря при зелёном флаге (`strategy.pit_loss`), под SC и VSC — её
  доля, измеренная по данным сезона (`neutral_pit_ratio`).
Круги под нейтрализацией у всех одинаково медленные, поэтому в сравнении
стратегий они не участвуют (шины на них стареют). Топливо и эволюция трассы
общие для всех стратегий и тоже сокращаются.

Нейтрализации разыгрываются по модели сезона (`sc_model`): вероятность начала на
каждом круге, доля SC среди них и длительность — из реальных эпизодов 2026 года.
Все стратегии считаются на одних и тех же сценариях (common random numbers),
чтобы разница между ними не тонула в шуме.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .strategy import stint_time

KINDS = ('GREEN', 'SC', 'VSC')


# --- нейтрализации по данным -----------------------------------------------------

def neutralisation_events(laps):
    """Эпизоды SC/VSC по гонкам: race, kind, start, end, laps (по кругам лидирующей группы)."""
    rows = []
    for race, r in laps.groupby('race', sort=False):
        kind = r.groupby('lap_number')['neutralised'].agg(
            lambda s: s.dropna().mode().iloc[0] if s.notna().any() else None)
        cur = None
        for lap in range(1, int(r['total_laps'].iloc[0]) + 1):
            k = kind.get(lap)
            if k in ('SC', 'VSC'):
                if cur and cur['kind'] == k and cur['end'] == lap - 1:
                    cur['end'] = lap
                else:
                    cur = {'race': race, 'kind': k, 'start': lap, 'end': lap}
                    rows.append(cur)
            else:
                cur = None
    ev = pd.DataFrame(rows, columns=['race', 'kind', 'start', 'end'])
    ev['laps'] = ev['end'] - ev['start'] + 1
    return ev


def sc_model(events, laps):
    """Модель нейтрализаций: вероятность начала на круге, доли SC/VSC, длительности."""
    race_laps = laps.groupby('race')['total_laps'].first()
    occupied = events['laps'].sum()
    p = len(events) / max(race_laps.sum() - occupied, 1)    # на круг «зелёной» гонки
    return {'p': p,
            'share_sc': float((events['kind'] == 'SC').mean()),
            'durations': {k: events.loc[events['kind'] == k, 'laps'].to_numpy() for k in ('SC', 'VSC')},
            'races': len(race_laps), 'events': len(events)}


def neutral_pit_ratio(laps, max_lane_excess=5.0):
    """Доля потери на пит-стопе под SC и VSC от потери при зелёном флаге.

    Потеря = (круг заезда + круг выезда пилота) − медиана тех же двух кругов у тех,
    кто на них не заезжал. Так одинаково считается и зелёный стоп, и стоп под SC,
    когда медленные все. Отношение — медиана по гонкам, где есть оба вида стопов.
    """
    rows = []
    for race, r in laps.groupby('race', sort=False):
        t = r.pivot_table(index='lap_number', columns='driver_number', values='lap_time')
        busy = (r.assign(b=(r['is_in_lap'] | r['is_pit_out_lap']).astype(int))
                 .pivot_table(index='lap_number', columns='driver_number', values='b', aggfunc='max')
                 .reindex_like(t).fillna(0).astype(bool))
        kind = r.groupby('lap_number')['neutralised'].agg(
            lambda s: s.dropna().mode().iloc[0] if s.notna().any() else 'GREEN')
        lane_med = r.loc[r['is_in_lap'] & r['neutralised'].isna(), 'pit_lane_time'].median()
        for _, s in r[r['is_in_lap']].iterrows():
            k, n = int(s['lap_number']), s['driver_number']
            if k < 2 or k + 1 not in t.index or pd.isna(t.at[k, n]) or pd.isna(t.at[k + 1, n]):
                continue
            if pd.notna(s['pit_lane_time']) and s['pit_lane_time'] - lane_med > max_lane_excess:
                continue
            free = ~(busy.loc[k] | busy.loc[k + 1])
            others = t.loc[[k, k + 1], free].sum(min_count=2).dropna()
            if len(others) >= 5:
                rows.append({'race': race, 'kind': kind.get(k, 'GREEN'),
                             'loss': t.at[k, n] + t.at[k + 1, n] - others.median()})
    d = pd.DataFrame(rows)
    green = d[d['kind'] == 'GREEN'].groupby('race')['loss'].median()
    out = {'GREEN': 1.0}
    for k in ('SC', 'VSC'):
        x = d[d['kind'] == k].groupby('race')['loss'].median()
        both = pd.concat([green, x], axis=1, keys=['g', 'k']).dropna()
        out[k] = float((both['k'] / both['g']).median()) if len(both) else 1.0
    return out, d


def sample_scenarios(model, total_laps, n, seed=0):
    """n сценариев гонки: для каждого круга код 0 — зелёный, 1 — SC, 2 — VSC."""
    rng = np.random.default_rng(seed)
    out = np.zeros((n, total_laps), dtype=np.int8)
    for i in range(n):
        lap = 1
        while lap <= total_laps:
            if rng.random() < model['p']:
                kind = 1 if rng.random() < model['share_sc'] else 2
                dur = int(rng.choice(model['durations']['SC' if kind == 1 else 'VSC']))
                out[i, lap - 1:lap - 1 + dur] = kind
                lap += dur
            else:
                lap += 1
    return out


# --- стратегии -------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    """План: стартовый состав и стопы [(круг заезда, новый состав), …]."""
    start: str
    stops: tuple

    @property
    def label(self):
        return ' → '.join([self.start] + [f'{c} (L{l})' for l, c in self.stops])


def lap_times(params, total_laps, plan, stops=None):
    """Время каждого круга при зелёном флаге (без топлива — оно общее), с."""
    stops = plan.stops if stops is None else stops
    out, comp, start = np.empty(total_laps), plan.start, 1
    for lap_end, new in list(stops) + [(total_laps, None)]:
        n = lap_end - start + 1
        ages = np.arange(n)
        out[start - 1:lap_end] = params['offset'][comp] + params['deg'][comp] * ages
        comp, start = new, lap_end + 1
    return out


def best_plans(params, total_laps, loss, min_stint=6):
    """Лучший план на 1 и на 2 остановки для каждой пары/тройки составов (без SC).

    Правило двух сухих составов: в плане минимум два разных состава.
    """
    comps = [c for c in params['deg'] if c in params['offset']]
    plans = []
    for a in comps:
        for b in comps:
            if a == b:
                continue
            k = min(range(min_stint, total_laps - min_stint + 1),
                    key=lambda k: stint_time(params, a, k) + stint_time(params, b, total_laps - k))
            plans.append(Plan(a, ((k, b),)))
    for a in comps:
        for b in comps:
            for c in comps:
                if len({a, b, c}) < 2:
                    continue
                best = None
                for k1 in range(min_stint, total_laps - 2 * min_stint + 1):
                    for k2 in range(k1 + min_stint, total_laps - min_stint + 1):
                        t = (stint_time(params, a, k1) + stint_time(params, b, k2 - k1)
                             + stint_time(params, c, total_laps - k2))
                        if best is None or t < best[0]:
                            best = (t, k1, k2)
                plans.append(Plan(a, ((best[1], b), (best[2], c))))
    det = {p: lap_times(params, total_laps, p).sum() + loss * len(p.stops) for p in plans}
    one = min((p for p in plans if len(p.stops) == 1), key=det.get)
    two = min((p for p in plans if len(p.stops) == 2), key=det.get)
    return one, two, det


def react(plan, scen, total_laps, params, cost, expected, window=15, min_stint=6):
    """Политика «реагировать», как у стратегов. Когда начинается нейтрализация,
    выбирается лучшее по модели износа из:
    * остаться на плане;
    * перенести ближайший стоп (если он не дальше `window` кругов) на этот круг;
    * сделать дополнительный стоп сейчас — на любой состав (свежие шины за полцены).

    Длительность текущей нейтрализации команде неизвестна — берётся средняя для её
    типа (`expected`); круги под ней в сравнении не учитываются (все едут одинаково
    медленно), будущие нейтрализации не предполагаются. Стинты не короче min_stint.
    """
    stops = list(plan.stops)
    comps = list(params['deg'])
    starts = [i + 1 for i in range(total_laps) if scen[i] and (i == 0 or not scen[i - 1])]

    def total(cand, green):
        return (lap_times(params, total_laps, plan, tuple(cand))[green].sum()
                + sum(cost[scen[lap - 1]] if lap == s else cost[0] for lap, _ in cand))

    for s in starts:
        kind = scen[s - 1]
        green = np.ones(total_laps, dtype=bool)
        green[s - 1:s + expected[kind] - 1] = False
        nxt = next((j for j, (lap, _) in enumerate(stops) if lap >= s), len(stops))
        prev = stops[nxt - 1][0] if nxt else 0
        after = stops[nxt][0] if nxt < len(stops) else total_laps
        if stops[nxt:nxt + 1] and stops[nxt][0] < s + expected[kind]:
            continue                               # стоп и так придётся на нейтрализацию
        options = [stops]
        if s - prev >= min_stint and nxt < len(stops) and stops[nxt][0] - s <= window:
            after2 = stops[nxt + 1][0] if nxt + 1 < len(stops) else total_laps
            if after2 - s >= min_stint:
                options.append(stops[:nxt] + [(s, stops[nxt][1])] + stops[nxt + 1:])
        if s - prev >= min_stint and after - s >= min_stint:
            options += [stops[:nxt] + [(s, c)] + stops[nxt:] for c in comps]
        stops = min(options, key=lambda cand: total(cand, green))
    return tuple(stops)


def race_times(params, total_laps, plan, scenarios, loss, ratio, policy='fixed', model=None,
               window=15):
    """Время гонки плана в каждом сценарии, с (только части, различающиеся между стратегиями).

    policy: 'fixed' — план не меняется; 'react' — см. `react` (нужна `model` из sc_model).
    """
    cost = np.array([loss * ratio['GREEN'], loss * ratio['SC'], loss * ratio['VSC']])
    expected = {1: 4, 2: 4}
    if model is not None:
        expected = {1: max(1, round(float(np.mean(model['durations']['SC'])))),
                    2: max(1, round(float(np.mean(model['durations']['VSC']))))}
    base = lap_times(params, total_laps, plan)
    out = np.empty(len(scenarios))
    for i, scen in enumerate(scenarios):
        stops = plan.stops if policy == 'fixed' else react(plan, scen, total_laps, params, cost,
                                                           expected, window)
        lt = base if stops == plan.stops else lap_times(params, total_laps, plan, stops)
        green = scen == 0
        out[i] = lt[green].sum() + sum(cost[scen[l - 1]] for l, _ in stops)
    return out
