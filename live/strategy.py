"""Стратегия в реальном времени: деградация шин и окно пит-стопа.

Модель учится только на кругах, проехавших к текущему моменту (LapHistory),
по всем пилотам гонки — так деградацию оценивают стратеги, и именно такой
вариант дал −20 % ошибки прогноза в notebooks/tyre_degradation.ipynb.

Чистый Python: логика повторяет analysis/clean.py, analysis/model.py:fit_race
и analysis/strategy.py:one_stop_curve, но серверу не нужны pandas/statsmodels.
"""

import math
from statistics import median

from .history import DRY

FUEL = 0.044            # с за каждый оставшийся круг топлива — оценка по сезону 2026 (ноутбук)
DEFAULT_PIT_LOSS = 22.0
OUTLIER = 1.07
MIN_STINT = 4
MIN_LAPS_FOR_ESTIMATE = 30
WINDOW_TOLERANCE = 1.0  # с — «окно» кругов, где проигрыш оптимуму меньше этого


def clean_laps(history):
    """Чистые круги темпа: [(num, row)] — те же правила, что analysis/clean.py."""
    rows = [(num, r) for num, laps in history.laps.items() for r in laps
            if r['t'] and r['lap'] > 1 and not (r['in'] or r['out'] or r['dirty'])
            and r['comp'] in DRY and r['age'] is not None]
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
    out = []
    for comp, d in deg.items():
        if comp == compound or d['n'] < MIN_LAPS_FOR_ESTIMATE:
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


# --- для фронтенда --------------------------------------------------------------

def strategy_view(state):
    """Компактный JSON для вкладки Strategy."""
    h, d = state.history, state.data
    lap_info = d.get('LapCount') or {}
    total = lap_info.get('TotalLaps')
    clean = clean_laps(h)
    deg = degradation(clean)
    loss, n_stops = pit_loss(h, clean)
    drivers = d.get('DriverList') or {}

    out_laps, windows = {}, {}
    for num, laps in h.laps.items():
        # [круг, время, позиция, отрыв, кругов отставания, состав, возраст, стинт, флаги]
        out_laps[num] = [[r['lap'], r['t'], r['pos'], r['gap'], r['down'], r['comp'], r['age'],
                          r['stint'], int(r['in']) | int(r['out']) << 1 | int(r['dirty']) << 2]
                         for r in laps]
        last = laps[-1] if laps else None
        if last and total and last['comp'] in DRY and last['age'] is not None:
            left = int(total) - last['lap']
            windows[num] = {
                'compound': last['comp'], 'age': last['age'], 'lap': last['lap'],
                'laps_left': left,
                # Круг заезда: после k кругов на старых шинах (k=0 — в конце текущего).
                'options': [dict(w, stop_lap=last['lap'] + max(1, w['in_laps']),
                                 window=[last['lap'] + max(1, k) for k in w['window']])
                            for w in pit_window(deg, last['comp'], last['age'], left, loss)],
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
                    'significant': bool(v['se']) and v['deg'] - 1.96 * v['se'] > 0}
                for c, v in deg.items()},
        'fuel': FUEL,
        'pitLoss': {'value': loss, 'stops': n_stops},
        'windows': windows,
        'cleanLaps': len(clean),
    }
