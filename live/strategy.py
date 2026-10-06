"""Стратегия в реальном времени: деградация шин и окно пит-стопа.

Модель учится только на кругах, проехавших к текущему моменту (LapHistory),
по всем пилотам гонки — так деградацию оценивают стратеги, и именно такой
вариант дал −20 % ошибки прогноза в notebooks/tyre_degradation.ipynb.

Чистый Python: логика повторяет analysis/clean.py, analysis/model.py:fit_race
и analysis/strategy.py:one_stop_curve, но серверу не нужны pandas/statsmodels.
"""

import math
from statistics import median

from .history import DRY, WET, lap_seconds, parse_gap

FUEL = 0.044            # с за каждый оставшийся круг топлива — оценка по сезону 2026 (ноутбук)
DEFAULT_PIT_LOSS = 22.0
OUTLIER = 1.07
MIN_STINT = 4
MIN_LAPS_FOR_ESTIMATE = 30
WINDOW_TOLERANCE = 1.0  # с — «окно» кругов, где проигрыш оптимуму меньше этого
FL_SEASONS = range(2019, 2025)  # очко за быстрейший круг (при финише в топ-10)
FL_LAPS = 10            # стоп «на быстрейший круг» предлагаем в последних кругах
FL_MARGIN = 1.0         # с — запас отрыва от машины сзади сверх потери на пит-стоп
FL_TIGHT = 3.0          # с — меньше отрыв ещё возможен: на свежем SOFT круг выезда
                        # быстрее среднего стопа (Лас-Вегас 2024, NOR: 21.2 с против 23.6)

# Нейтрализации сезона 2026 (notebooks/strategy_sim.ipynb, analysis/simulate.py):
# вероятность начала на «зелёном» круге, доля полного SC, средняя длина в кругах
# и цена пит-стопа под ней в долях от обычной.
SC_RATE = 0.040
SC_SHARE = 0.31
NEUTRAL = {'SC': {'ratio': 0.50, 'laps': 7}, 'VSC': {'ratio': 0.84, 'laps': 4}}
TRACK_NEUTRAL = {'4': 'SC', '6': 'VSC'}     # TrackStatus.Status


def clean_laps(history):
    """Чистые круги темпа: [(num, row)] — те же правила, что analysis/clean.py."""
    rows = [(num, r) for num, laps in history.laps.items() for r in laps
            if r['t'] and r['lap'] > 1 and not (r['in'] or r['out'] or r['dirty'])
            and r['comp'] in DRY + WET and r['age'] is not None]
    by_driver = {}
    for num, r in rows:
        by_driver.setdefault(num, []).append(r['t'])
    med = {num: median(ts) for num, ts in by_driver.items()}
    rows = [(num, r) for num, r in rows if r['t'] <= OUTLIER * med[num]]
    count = {}
    for num, r in rows:
        count[(num, r['stint'])] = count.get((num, r['stint']), 0) + 1
    return [(num, r) for num, r in rows if count[(num, r['stint'])] >= MIN_STINT]


def degradation(clean, fuel=FUEL):
    """С/круг по составу: внутристинтовая OLS (средние по стинту вычитаются —
    это фиксированный эффект пилота×стинта) на время с поправкой на топливо.

    Поправка: t + fuel·lap (то же, что t − fuel·laps_remaining с точностью до
    константы стинта), поэтому общее число кругов не нужно.
    → {состав: {'deg', 'se', 'n'}}
    """
    groups = {}
    for num, r in clean:
        groups.setdefault((r['comp'], num, r['stint']), []).append(
            (r['age'], r['t'] + fuel * r['lap']))
    acc = {}
    for (comp, _, _), pts in groups.items():
        if len(pts) < 2:
            continue
        ma = sum(a for a, _ in pts) / len(pts)
        my = sum(y for _, y in pts) / len(pts)
        a = acc.setdefault(comp, {'sxx': 0.0, 'sxy': 0.0, 'pts': [], 'groups': 0})
        a['groups'] += 1
        for x, y in pts:
            a['sxx'] += (x - ma) ** 2
            a['sxy'] += (x - ma) * (y - my)
            a['pts'].append((x - ma, y - my))
    out = {}
    for comp, a in acc.items():
        if a['sxx'] <= 0:
            continue
        b = a['sxy'] / a['sxx']
        n = len(a['pts'])
        dof = n - a['groups'] - 1
        sse = sum((y - b * x) ** 2 for x, y in a['pts'])
        se = math.sqrt(sse / dof / a['sxx']) if dof > 0 else None
        out[comp] = {'deg': b, 'se': se, 'n': n}
    return out


def pit_loss(history, clean):
    """Медиана (круг заезда + круг выезда − 2 × медианный чистый круг) по стопам
    при зелёном флаге. → (секунды, число стопов; DEFAULT_PIT_LOSS если стопов нет)."""
    med = {}
    for num, r in clean:
        med.setdefault(num, []).append(r['t'])
    med = {num: median(ts) for num, ts in med.items()}
    losses = []
    for num, laps in history.laps.items():
        for prev, nxt in zip(laps, laps[1:]):
            if (prev['in'] and nxt['lap'] == prev['lap'] + 1 and num in med
                    and prev['t'] and nxt['t'] and not (prev['dirty'] or nxt['dirty'])):
                loss = prev['t'] + nxt['t'] - 2 * med[num]
                if 5 < loss < 60:
                    losses.append(loss)
    if not losses:
        return DEFAULT_PIT_LOSS, 0
    return median(losses), len(losses)


def usable(d):
    """Оценки износа хватает для решения: своя по гонке или прогноз по практикам."""
    return d['n'] >= MIN_LAPS_FOR_ESTIMATE or d.get('source') == 'practice'


def with_practice(deg, pre):
    """Износ для решений: пока в гонке мало кругов состава — прогноз по практикам."""
    if not pre:
        return deg
    out = dict(deg)
    for comp, p in pre['compounds'].items():
        if comp not in deg or deg[comp]['n'] < MIN_LAPS_FOR_ESTIMATE:
            out[comp] = {'deg': p['deg'], 'se': None, 'n': deg.get(comp, {}).get('n', 0),
                         'source': 'practice'}
    return out


def neutral_stop(deg, compound, age, laps_left, loss, kind):
    """Заехать сейчас, под SC/VSC, или ехать по плану (одна остановка позже или ни одной)?

    Как политика «реагировать» в analysis/simulate.py: стоп стоит долю обычной
    потери (NEUTRAL), а круги этой нейтрализации (средняя длина) у всех одинаково
    медленные и в сравнении не участвуют.
    → {'kind', 'cost', 'compound', 'gain' (против «без остановки»), 'vs_plan'} или None.
    """
    cur = deg.get(compound)
    n = NEUTRAL[kind]
    green = laps_left - n['laps']
    if cur is None or compound not in DRY or green < 3:
        return None
    d_cur = max(cur['deg'], 0.0)
    stay = _stint_cost(d_cur, age + n['laps'], green)
    cost = loss * n['ratio']
    best = None
    for comp, d in deg.items():
        if comp not in DRY or comp == compound or not usable(d):
            continue
        gain = stay - (cost + _stint_cost(max(d['deg'], 0.0), n['laps'], green))
        if best is None or gain > best['gain']:
            best = {'compound': comp, 'gain': gain}
    if best is None:
        return None
    later = pit_window(deg, compound, age + n['laps'], green, loss)
    plan = max([0.0] + [w['gain'] for w in later])
    return {'kind': kind, 'cost': cost, **best, 'vs_plan': best['gain'] - plan}


def _stint_cost(deg, start_age, laps):
    """Σ deg·(start_age + i), i = 0..laps−1 — потеря темпа на износе за стинт."""
    return deg * (laps * start_age + laps * (laps - 1) / 2) if laps > 0 else 0.0


def pit_window(deg, compound, age, laps_left, loss, min_new_stint=3):
    """Лучший момент для одной остановки на каждый состав-кандидат.

    → [{'compound', 'in_laps', 'window': (от, до), 'gain'}], где in_laps — через
    сколько кругов заезжать (0 — на этом круге), gain — выигрыш против «доехать
    без остановки», с. Пусто, если для текущих шин нет оценки.
    """
    cur = deg.get(compound)
    if cur is None or laps_left is None or laps_left < min_new_stint + 1:
        return []
    # Отрицательная «деградация» — это трасса, становящаяся быстрее, а не шины,
    # которые молодеют: для решения о пит-стопе считаем её нулём.
    d_cur = max(cur['deg'], 0.0)
    stay = _stint_cost(d_cur, age, laps_left)
    same_class = WET if compound in WET else DRY
    out = []
    for comp, d in deg.items():
        if (comp not in same_class or not usable(d)
                or (comp == compound and compound in DRY)):
            continue
        d_new = max(d['deg'], 0.0)
        costs = [(k, _stint_cost(d_cur, age, k) + loss
                  + _stint_cost(d_new, 0, laps_left - k))
                 for k in range(0, laps_left - min_new_stint + 1)]
        best_k, best = min(costs, key=lambda kc: kc[1])
        ok = [k for k, c in costs if c - best <= WINDOW_TOLERANCE]
        out.append({'compound': comp, 'in_laps': best_k, 'window': (min(ok), max(ok)),
                    'gain': stay - best})
    return sorted(out, key=lambda w: -w['gain'])


def fastest_lap_stop(state, num, laps, total, deg, loss):
    """Поздний стоп на свежий SOFT ради очка за быстрейший круг (сезоны 2019–2024).

    Предлагается пилоту из топ-10 за ≤ FL_LAPS кругов до финиша. status: 'free' —
    отрыв от машины сзади больше потери на пит-стоп + FL_MARGIN (или она отстаёт
    на круг), 'tight' — не меньше потери − FL_TIGHT (риск потерять место), иначе 'no'. Круг заезда — не позже чем за 2 круга до финиша: круг выезда
    и хотя бы один быстрый. → dict или None, если вариант неприменим.
    """
    d = state.data
    info = d.get('SessionInfo') or {}
    year = str(info.get('StartDate') or '')[:4]
    if (not year.isdigit() or int(year) not in FL_SEASONS
            or info.get('Type') != 'Race' or 'sprint' in str(info.get('Name', '')).lower()):
        return None
    last = laps[-1]
    left = total - last['lap']
    # Заезд не позже круга total−2 → нужно ≥ 3 круга до финиша
    if last['comp'] not in DRY or not 3 <= left <= FL_LAPS:
        return None
    lines = (d.get('TimingData') or {}).get('Lines') or {}
    pos = {}
    for n, line in lines.items():
        try:
            pos[int(line.get('Position') or 0)] = n
        except (TypeError, ValueError):
            pass
    me = next((p for p, n in pos.items() if n == num), None)
    if not me or me > 10:
        return None

    # Отрыв от машины сзади: её интервал до позиции впереди (то есть до нас)
    behind = pos.get(me + 1)
    gap, lapped = None, behind is None
    if behind:
        line = lines[behind]
        if line.get('Retired') or line.get('Stopped'):
            lapped = True
        else:
            gap, down = parse_gap((line.get('IntervalToPositionAhead') or {}).get('Value'), None)
            lapped = down > 0
    status = ('free' if lapped or (gap is not None and gap > loss + FL_MARGIN)
              else 'tight' if gap is not None and gap > loss - FL_TIGHT else 'no')

    # Быстрейший круг гонки сейчас
    best = [(t, n) for n, line in lines.items()
            if (t := lap_seconds((line.get('BestLapTime') or {}).get('Value')))]
    record, holder = min(best) if best else (None, None)

    # Темп: медиана последних трёх нормальных кругов; свежие шины вернут износ
    recent = [r['t'] for r in laps[-6:] if r['t'] and not (r['in'] or r['out'] or r['dirty'])][-3:]
    pace = median(recent) if recent else None
    cur = deg.get(last['comp'])
    recovered = max(cur['deg'], 0.0) * last['age'] if cur else 0.0
    drivers = d.get('DriverList') or {}
    tla = lambda n: (drivers.get(n) or {}).get('Tla') or n
    return {
        'status': status, 'gapBehind': gap, 'lapped': lapped,
        'behind': tla(behind) if behind else None,
        'loss': loss,
        'window': [max(last['lap'] + 1, total - 4), total - 2],
        'record': record, 'holder': tla(holder) if holder else None, 'mine': holder == num,
        'pace': pace, 'recovered': recovered,
        # > 0 — свежие шины по оценке износа не хватает до рекорда (сцепление
        # SOFT сверх этого модель не оценивает)
        'need': (pace - recovered - FUEL * 2 - record) if pace and record else None,
    }


# --- для фронтенда --------------------------------------------------------------

def sc_risk(state, total):
    """Риск нейтрализации до финиша и цена стопа под ней (только гонка с дистанцией)."""
    d = state.data
    if not total or (d.get('SessionInfo') or {}).get('Type') != 'Race':
        return None
    left = max(int(total) - int((d.get('LapCount') or {}).get('CurrentLap') or 0), 0)
    now = TRACK_NEUTRAL.get(str((d.get('TrackStatus') or {}).get('Status')))
    return {'perLap': SC_RATE, 'shareSC': SC_SHARE, 'lapsLeft': left,
            'rest': 1 - (1 - SC_RATE) ** left, 'now': now,
            'ratio': {k: v['ratio'] for k, v in NEUTRAL.items()}}


def strategy_view(state, pre=None):
    """Компактный JSON для вкладки Strategy; pre — износ по практикам (live/practice.py)."""
    h, d = state.history, state.data
    lap_info = d.get('LapCount') or {}
    total = lap_info.get('TotalLaps')
    clean = clean_laps(h)
    deg = degradation(clean)
    loss, n_stops = pit_loss(h, clean)
    risk = sc_risk(state, total)
    pre = pre if risk else None              # прогноз по практикам — только для гонки
    use = with_practice(deg, pre)
    drivers = d.get('DriverList') or {}

    out_laps, windows = {}, {}
    for num, laps in h.laps.items():
        # [круг, время, позиция, отрыв, кругов отставания, состав, возраст, стинт, флаги]
        out_laps[num] = [[r['lap'], r['t'], r['pos'], r['gap'], r['down'], r['comp'], r['age'],
                          r['stint'], int(r['in']) | int(r['out']) << 1 | int(r['dirty']) << 2]
                         for r in laps]
        last = laps[-1] if laps else None
        if last and total and last['comp'] in DRY + WET and last['age'] is not None:
            left = int(total) - last['lap']
            windows[num] = {
                'compound': last['comp'], 'age': last['age'], 'lap': last['lap'],
                'laps_left': left, 'wet': last['comp'] in WET,
                # Круг заезда: после k кругов на старых шинах (k=0 — в конце текущего).
                'options': [dict(w, stop_lap=last['lap'] + max(1, w['in_laps']),
                                 window=[last['lap'] + max(1, k) for k in w['window']])
                            for w in pit_window(use, last['comp'], last['age'], left, loss)],
                'fastestLap': fastest_lap_stop(state, num, laps, int(total), use, loss),
                'neutral': (neutral_stop(use, last['comp'], last['age'], left, loss, risk['now'])
                            if risk and risk['now'] else None),
                'practice': [c for c, v in use.items() if v.get('source') == 'practice'],
            }
    return {
        'type': 'strategy',
        'sessionType': (d.get('SessionInfo') or {}).get('Type') or '',
        'totalLaps': total,
        'currentLap': lap_info.get('CurrentLap'),
        'drivers': {num: {'tla': v.get('Tla') or num, 'color': '#' + (v.get('TeamColour') or '888888')}
                    for num, v in drivers.items()},
        'laps': out_laps,
        'deg': {c: {**v, 'enough': v['n'] >= MIN_LAPS_FOR_ESTIMATE,
                    # 95% ДИ не захватывает ноль — износ статистически заметен
                    'significant': bool(v['se']) and v['deg'] - 1.96 * v['se'] > 0,
                    # Круги заметно быстреют: трасса подсыхает / набирает резину
                    # быстрее, чем изнашиваются шины (типично для дождевой гонки)
                    'improving': bool(v['se']) and v['deg'] + 1.96 * v['se'] < 0}
                for c, v in deg.items()},
        'fuel': FUEL,
        'pitLoss': {'value': loss, 'stops': n_stops},
        'preRace': pre,
        'scRisk': risk,
        'windows': windows,
        'cleanLaps': len(clean),
    }
