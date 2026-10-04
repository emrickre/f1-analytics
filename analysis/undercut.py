"""Андеркат: сколько выигрывает тот, кто заезжает в боксы первым.

Отрывы между машинами восстанавливаются из самого датасета кругов: момент, когда
пилот пересёк линию в конце круга n, — это начало его круга n+1 (или начало круга n
плюс время круга). Разница этих моментов у двух машин на одном круге — отрыв.

Единица анализа — **пара пит-стопов**: две машины в пределах `max_gap` секунд друг от
друга, обе заехали в боксы с разницей не больше `max_response` кругов, без SC/VSC/
красного флага на этих кругах. Исход — `gain`: на сколько секунд сократил отставание
(или увеличил преимущество) тот, кто заехал первым, к моменту, когда оба цикла пит-стопов
завершены.
"""

import numpy as np
import pandas as pd

from .model import fit_race, race_params
from .strategy import undercut_gain, warmup_penalty


def crossing_times(laps):
    """Момент пересечения линии в конце каждого круга, с от начала гонки.

    → DataFrame с индексом (session_key, lap_number) и колонками — номерами пилотов.
    """
    d = laps.sort_values(['session_key', 'driver_number', 'lap_number']).copy()
    end = d['date_start'] + pd.to_timedelta(d['lap_time'], unit='s')
    nxt = d.groupby(['session_key', 'driver_number'])['date_start'].shift(-1)
    same_next = d.groupby(['session_key', 'driver_number'])['lap_number'].shift(-1) == d['lap_number'] + 1
    d['cross'] = nxt.where(same_next, end)            # начало следующего круга точнее
    t0 = d.groupby('session_key')['date_start'].transform('min')
    d['t'] = (d['cross'] - t0).dt.total_seconds()
    return d.pivot_table(index=['session_key', 'lap_number'], columns='driver_number',
                         values='t', aggfunc='first')


def pit_pairs(laps, max_gap=5.0, max_response=5, max_lane_excess=5.0):
    """Пары пит-стопов (см. описание модуля).

    Стопы, которые дольше медианы гонки в пит-лейне больше чем на max_lane_excess
    секунд (штраф, ремонт, проблема с колесом), — не андеркат; такие пары отбрасываются.

    Колонки: race, session_key, first, second (номера), first_driver, second_driver,
    stop_lap (круг заезда первого), second_stop_lap, response_laps,
    gap_before / gap_after — на сколько секунд первый **позади** второго (< 0 — впереди)
    на конец круга перед его стопом и на конец круга выезда второго;
    gain = gap_before − gap_after (> 0 — первый выиграл время);
    overtook — первый был позади и оказался впереди;
    second_age — возраст шин второго на круге стопа первого; составы old/new обоих;
    pit_lane_diff — время в пит-лейне первого минус второго (> 0 — стоп первого медленнее).
    """
    cross = crossing_times(laps)
    rows = []
    for key, r in laps.groupby('session_key', sort=False):
        if key not in cross.index.get_level_values(0):
            continue
        cr = cross.loc[key]
        lap = r.set_index(['driver_number', 'lap_number'])
        neutral_laps = set(r.loc[r['neutralised'].notna(), 'lap_number'])
        stops = {n: sorted(g.loc[g['is_in_lap'], 'lap_number']) for n, g in r.groupby('driver_number')}
        in_laps = r[r['is_in_lap']]
        lane_median = in_laps.loc[in_laps['neutralised'].isna(), 'pit_lane_time'].median()

        for a, a_stops in stops.items():
            for k in a_stops:
                if k < 2 or (k - 1) not in cr.index or pd.isna(cr.at[k - 1, a]):
                    continue
                before = cr.loc[k - 1].dropna()
                near = before[(before - before[a]).abs() <= max_gap].drop(a)
                for b in near.index:
                    later = [x for x in stops.get(b, []) if k < x <= k + max_response]
                    if not later:
                        continue
                    kb = later[0]
                    after = kb + 1                                  # круг выезда второго
                    window = range(k - 1, after + 1)
                    if (after not in cr.index or pd.isna(cr.at[after, a]) or pd.isna(cr.at[after, b])
                            or neutral_laps.intersection(window)
                            # другой стоп кого-то из пары внутри окна смешал бы циклы
                            or any(k < x <= after for x in a_stops if x != k)
                            or any(k - max_response <= x <= k for x in stops.get(b, []))):
                        continue
                    gap_before = cr.at[k - 1, a] - cr.at[k - 1, b]
                    gap_after = cr.at[after, a] - cr.at[after, b]

                    def get(n, l, col):
                        return lap[col].get((n, l), np.nan)

                    lane_a, lane_b = get(a, k, 'pit_lane_time'), get(b, kb, 'pit_lane_time')
                    if any(x - lane_median > max_lane_excess for x in (lane_a, lane_b) if pd.notna(x)):
                        continue
                    rows.append({
                        'race': r['race'].iloc[0], 'session_key': key,
                        'first': a, 'second': b,
                        'first_driver': get(a, k, 'driver'), 'second_driver': get(b, k, 'driver'),
                        'stop_lap': k, 'second_stop_lap': kb, 'response_laps': kb - k,
                        'gap_before': gap_before, 'gap_after': gap_after,
                        'gain': gap_before - gap_after,
                        'overtook': gap_before > 0 > gap_after,
                        'second_age': get(b, k, 'tyre_age'),
                        'first_old': get(a, k, 'compound'), 'first_new': get(a, k + 1, 'compound'),
                        'second_old': get(b, k, 'compound'), 'second_new': get(b, kb + 1, 'compound'),
                        'pit_lane_diff': lane_a - lane_b,
                    })
    return pd.DataFrame(rows)


def race_models(clean, fuel):
    """Параметры модели износа (первое исследование) и прогрев шин для каждой гонки."""
    out = {}
    for race, d in clean.groupby('race', sort=False):
        try:
            res = fit_race(d, fuel)
        except (ValueError, np.linalg.LinAlgError):
            continue
        out[race] = (race_params(res), warmup_penalty(d, res))
    return out


def predicted_gain(pair, models):
    """Прогноз выигрыша первого по модели износа, с (NaN — если составов нет в модели).

    Круги, пока первый на свежих шинах, а второй на старых, — `undercut_gain`
    (с прогревом на круге выезда первого); плюс круг выезда второго: свежие шины
    второго с прогревом против шин первого возрастом response_laps; минус разница
    времени в пит-лейне. Топливо и эволюция трассы у пары общие и сокращаются.
    """
    if pair['race'] not in models:
        return np.nan
    p, warm = models[pair['race']]
    old, new1, new2 = pair['second_old'], pair['first_new'], pair['second_new']
    if any(c not in p['deg'] for c in (old, new1, new2)) or pd.isna(pair['second_age']):
        return np.nan
    j = int(pair['response_laps'])
    g = undercut_gain(p, old, pair['second_age'], new1, laps=j, warmup=warm)[-1]
    last = (p['offset'][new2] + warm) - (p['offset'][new1] + p['deg'][new1] * j)
    lane = pair['pit_lane_diff'] if pd.notna(pair['pit_lane_diff']) else 0.0
    return g + last - lane
