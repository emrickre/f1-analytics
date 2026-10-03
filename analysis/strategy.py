"""Стратегия пит-стопов на параметрах модели гонки (analysis.model.race_params).

Время круга на свежих шинах состава c через a кругов: offset[c] + deg[c]·a
(константы темпа пилота и топлива одинаковы для всех вариантов стратегии и
при сравнении сокращаются).
"""

import numpy as np
import pandas as pd


def pit_loss(raw_race, clean_race):
    """Сколько секунд стоит пит-стоп в этой гонке (медиана по реальным стопам).

    Потеря = (круг заезда + круг выезда) − 2 × медианный чистый круг пилота.
    Стопы под SC/VSC/красным не учитываем — они «дешевле» обычных.
    """
    med = clean_race.groupby('driver_number')['lap_time'].median()
    laps = raw_race.set_index(['driver_number', 'lap_number'])
    losses = []
    for (drv, lap), row in laps[laps['is_in_lap']].iterrows():
        nxt = laps.loc[(drv, lap + 1)] if (drv, lap + 1) in laps.index else None
        # pd.notna, а не truthiness: в pandas 3 пустое значение строки — NaN.
        if nxt is None or drv not in med or pd.notna(row['neutralised']) \
                or pd.notna(nxt['neutralised']):
            continue
        t = row['lap_time'] + nxt['lap_time'] - 2 * med[drv]
        if np.isfinite(t) and 5 < t < 60:
            losses.append(t)
    return float(np.median(losses)) if losses else float('nan'), len(losses)


def stint_time(params, compound, laps, start_age=0):
    """Сумма (относительного) времени стинта из `laps` кругов."""
    a = np.arange(start_age, start_age + laps)
    return float(np.sum(params['offset'][compound] + params['deg'][compound] * a))


def one_stop_curve(params, total_laps, first, second, loss, min_stint=6):
    """Время гонки на одну остановку first→second для каждого круга остановки.

    → DataFrame: stop_lap, delta (с от оптимума).
    """
    rows = []
    for n in range(min_stint, total_laps - min_stint + 1):
        t = stint_time(params, first, n) + stint_time(params, second, total_laps - n) + loss
        rows.append((n, t))
    out = pd.DataFrame(rows, columns=['stop_lap', 'total'])
    out['delta'] = out['total'] - out['total'].min()
    return out


def optimal_window(curve, tolerance=1.0):
    """Оптимальный круг и «окно» кругов, где потеря < tolerance секунд."""
    best = int(curve.loc[curve['delta'].idxmin(), 'stop_lap'])
    win = curve.loc[curve['delta'] <= tolerance, 'stop_lap']
    return best, int(win.min()), int(win.max())


def warmup_penalty(clean_race, model_result):
    """Насколько первый чистый круг на свежих шинах медленнее прогноза модели, с."""
    d = clean_race.loc[model_result.model.data.row_labels].copy()
    d['resid'] = model_result.resid
    d = d.sort_values('lap_number')
    first = d[d['stint'] > 1].groupby(['driver_number', 'stint'])['resid'].first()
    return float(first.mean()) if len(first) else 0.0


def undercut_gain(params, old, old_age, new, laps=3, warmup=0.0):
    """Накопленный выигрыш свежих шин `new` против старых `old` (возраст old_age).

    Положительное значение — андеркат сработал бы на этом круге (без учёта
    потери на самом пит-стопе: её оба пилота платят по разу).
    """
    gains = []
    for i in range(laps):
        g = (params['offset'][old] + params['deg'][old] * (old_age + i)) \
            - (params['offset'][new] + params['deg'][new] * i)
        gains.append(g - (warmup if i == 0 else 0.0))
    return np.cumsum(gains)


def actual_one_stops(raw_race):
    """Пилоты, проехавшие гонку на одну остановку: составы и круг остановки."""
    rows = []
    for drv, d in raw_race.groupby('driver'):
        st = d.dropna(subset=['stint']).groupby('stint').agg(
            compound=('compound', 'first'), last=('lap_number', 'max'))
        if len(st) == 2:
            rows.append({'driver': drv, 'first': st['compound'].iloc[0],
                         'second': st['compound'].iloc[1], 'stop_lap': int(st['last'].iloc[0]),
                         'under_neutralisation': bool(
                             d.loc[d['lap_number'] == st['last'].iloc[0], 'neutralised']
                             .notna().any())})
    out = pd.DataFrame(rows, columns=['driver', 'first', 'second', 'stop_lap',
                                      'under_neutralisation'])
    return out.astype({'stop_lap': int, 'under_neutralisation': bool})
