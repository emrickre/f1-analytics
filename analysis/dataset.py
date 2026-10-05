"""Датасет кругов сезона из OpenF1 → pandas / Parquet.

Одна строка = один круг одного пилота в гонке: время круга и секторов,
состав и возраст шин, пит-стопы, нейтрализации (SC / VSC / красный флаг),
жёлтые флаги, температура трассы, сколько кругов осталось (прокси топлива).

Загрузка и дисковый кеш — те же, что у live-таймингов (live/openf1.py),
поэтому повторная сборка идёт без сети.

    python -m analysis.dataset 2026            # → data/laps_2026.parquet
"""

import argparse
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from live.openf1 import OpenF1Source, catalog, normalize_pits, reconcile_stints

log = logging.getLogger('analysis.dataset')

DATA_DIR = Path(__file__).resolve().parent.parent / 'data'
ENDPOINTS = ('drivers', 'laps', 'stints', 'pit', 'race_control', 'weather')


# --- загрузка ---------------------------------------------------------------

def fetch_session(key, src=None):
    """Сырые таблицы одной сессии (с кешем на диске, как у live-таймингов)."""
    src = src or OpenF1Source()
    cdir = src.cache_dir / str(key)
    return {ep: src._get(ep, cdir / f'{ep}.json', session_key=key) for ep in ENDPOINTS}


# --- признаки ---------------------------------------------------------------

def neutralisations(race_control):
    """Интервалы нейтрализаций [(start, end, kind)] по сообщениям Race Control.

    kind: 'SC', 'VSC' или 'RED'. Окончание — «TRACK CLEAR», зелёный флаг
    на всю трассу или начало другой нейтрализации.
    """
    out, cur = [], None
    for m in sorted(race_control, key=lambda m: m['date']):
        msg = (m.get('message') or '').upper()
        t = pd.Timestamp(m['date'])
        kind = None
        if 'VSC DEPLOYED' in msg or 'VIRTUAL SAFETY CAR DEPLOYED' in msg:
            kind = 'VSC'
        elif 'SAFETY CAR DEPLOYED' in msg:
            kind = 'SC'
        elif m.get('flag') == 'RED':
            kind = 'RED'
        ends = ('TRACK CLEAR' in msg
                or (m.get('flag') in ('GREEN', 'CLEAR') and m.get('scope') == 'Track'))
        if kind:
            if cur and cur[1] != kind:
                out.append((cur[0], t, cur[1]))
            if not cur or cur[1] != kind:
                cur = (t, kind)
        elif ends and cur:
            out.append((cur[0], t, cur[1]))
            cur = None
    if cur:
        out.append((cur[0], pd.Timestamp.max.tz_localize('UTC'), cur[1]))
    return out


def yellow_times(race_control):
    """Моменты жёлтых флагов в секторах — круг с ними тоже нерепрезентативен."""
    return sorted(pd.Timestamp(m['date']) for m in race_control
                  if m.get('flag') in ('YELLOW', 'DOUBLE YELLOW'))


def build_session_laps(raw, meta=None):
    """Таблица кругов одной гонки из сырых таблиц OpenF1."""
    laps = pd.DataFrame(raw['laps'])
    if laps.empty:
        return laps
    raw = {**raw, 'pit': normalize_pits(raw['pit'], raw['laps'])}
    laps = laps[['session_key', 'driver_number', 'lap_number', 'date_start',
                 'lap_duration', 'duration_sector_1', 'duration_sector_2',
                 'duration_sector_3', 'is_pit_out_lap', 'st_speed']].copy()
    laps = laps.rename(columns={'lap_duration': 'lap_time',
                                'duration_sector_1': 's1', 'duration_sector_2': 's2',
                                'duration_sector_3': 's3'})
    laps['date_start'] = pd.to_datetime(laps['date_start'], utc=True, format='ISO8601')
    laps['date_end'] = laps['date_start'] + pd.to_timedelta(laps['lap_time'], unit='s')
    laps['is_pit_out_lap'] = laps['is_pit_out_lap'].fillna(False).astype(bool)

    # Пилоты
    drv = {d['driver_number']: d for d in raw['drivers']}
    laps['driver'] = laps['driver_number'].map(lambda n: drv.get(n, {}).get('name_acronym'))
    laps['team'] = laps['driver_number'].map(lambda n: drv.get(n, {}).get('team_name'))

    # Шины: стинт, состав, возраст на круге
    compound = np.full(len(laps), None, dtype=object)
    stint_no = np.full(len(laps), np.nan)
    age = np.full(len(laps), np.nan)
    stints = {}
    # Стинты OpenF1 сверяются с пит-стопами (см. live.openf1.reconcile_stints)
    for s in reconcile_stints(raw['stints'], raw['pit'], raw['laps']):
        stints.setdefault(s['driver_number'], []).append(s)
    for i, (n, lap) in enumerate(zip(laps['driver_number'], laps['lap_number'])):
        for s in stints.get(n, ()):
            if s['lap_start'] <= lap <= (s.get('lap_end') or 10_000):
                compound[i] = s.get('compound')
                stint_no[i] = s['stint_number']
                age[i] = (s.get('tyre_age_at_start') or 0) + lap - s['lap_start']
                break
    laps['compound'], laps['stint'], laps['tyre_age'] = compound, stint_no, age

    # Пит-стопы: круг заезда и время в пит-лейне
    pits = {(p['driver_number'], p['lap_number']): p.get('lane_duration') or p.get('pit_duration')
            for p in raw['pit']}
    keys = list(zip(laps['driver_number'], laps['lap_number']))
    laps['is_in_lap'] = [k in pits for k in keys]
    laps['pit_lane_time'] = [pits.get(k) for k in keys]

    # Нейтрализации и жёлтые флаги, пересекающие круг
    neut = neutralisations(raw['race_control'])
    yel = yellow_times(raw['race_control'])
    status = []
    yellow = []
    for a, b in zip(laps['date_start'], laps['date_end']):
        if pd.isna(b):
            b = a + pd.Timedelta(seconds=200)
        status.append(next((k for s, e, k in neut if s < b and e > a), None))
        yellow.append(any(a <= t <= b for t in yel))
    laps['neutralised'] = status
    laps['yellow'] = yellow

    # Температура трассы на начало круга
    w = pd.DataFrame(raw['weather'])
    if not w.empty:
        w['date'] = pd.to_datetime(w['date'], utc=True, format='ISO8601')
        w = w.sort_values('date')[['date', 'track_temperature', 'air_temperature', 'rainfall']]
        order = laps['date_start'].notna()
        merged = pd.merge_asof(laps[order].sort_values('date_start'), w,
                               left_on='date_start', right_on='date', direction='backward')
        merged = merged.set_index(laps[order].sort_values('date_start').index)
        for col in ('track_temperature', 'air_temperature', 'rainfall'):
            laps.loc[merged.index, col] = merged[col]

    # Прокси топлива: сколько кругов осталось до финиша лидера
    total = int(laps['lap_number'].max())
    laps['total_laps'] = total
    laps['laps_remaining'] = total - laps['lap_number']

    for k, v in (meta or {}).items():
        laps[k] = v
    return laps.drop(columns=['date_end'])


def build_laps(year, session_name='Race', src=None):
    """Все прошедшие гонки сезона → один DataFrame кругов."""
    src = src or OpenF1Source()
    season = [s for s in catalog(year) if s['name'] == session_name]
    sessions = [s for s in season if s['available']]
    # Перенесённый этап сохраняет название: в 2026 «Bahrain Grand Prix» прошёл в
    # Куала-Лумпуре. Одинаковые названия в сезоне подписываем местом проведения.
    names = [s['meeting'] for s in season]
    label = lambda s: (f"{s['meeting']} ({s['location']})"            # noqa: E731
                       if names.count(s['meeting']) > 1 else s['meeting'])
    frames = []
    for round_no, s in enumerate(sessions, 1):
        log.info('%2d/%d %s %s', round_no, len(sessions), label(s), s['name'])
        raw = fetch_session(s['key'], src)
        frames.append(build_session_laps(raw, meta={
            'year': year, 'round': round_no, 'race': label(s),
            'location': s['location'], 'session': s['name']}))
    df = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    return df


def main():
    p = argparse.ArgumentParser(description='Собрать датасет кругов сезона из OpenF1')
    p.add_argument('year', type=int)
    p.add_argument('--session', default='Race', help='Race или Sprint')
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s',
                        datefmt='%H:%M:%S')
    df = build_laps(a.year, a.session)
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f'laps_{a.year}{"" if a.session == "Race" else "_" + a.session.lower()}.parquet'
    df.to_parquet(path, index=False)
    log.info('%d кругов, %d гонок → %s', len(df), df['session_key'].nunique(), path)


if __name__ == '__main__':
    main()
