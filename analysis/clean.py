"""Очистка кругов: оставляем только «чистые» круги темпа.

Каждый отброшенный круг получает одну причину (первую сработавшую), чтобы в
ноутбуке можно было показать воронку очистки.
"""

import pandas as pd

DRY = ('SOFT', 'MEDIUM', 'HARD')
OUTLIER_FACTOR = 1.07     # «правило 107%» от медианы пилота в гонке
MIN_STINT_LAPS = 4        # короче — не по чему оценивать деградацию

REASONS = {
    'no_time': 'no lap time',
    'wet': 'rain / intermediate tyres',
    'lap1': 'race start lap',
    'pit': 'pit in / out lap',
    'neutralised': 'SC / VSC / red flag',
    'yellow': 'yellow flag in sector',
    'outlier': f'slower than {OUTLIER_FACTOR:.0%} of driver median',
    'short_stint': f'stint shorter than {MIN_STINT_LAPS} clean laps',
}


def drop_reason(df):
    """Причина отбросить круг (или None) — без учёта выбросов и коротких стинтов."""
    r = pd.Series(None, index=df.index, dtype=object)

    def mark(mask, reason):
        r[mask & r.isna()] = reason

    mark(df['lap_time'].isna(), 'no_time')
    rain = df['rainfall'].fillna(0) > 0 if 'rainfall' in df else False
    mark(~df['compound'].isin(DRY) | rain, 'wet')
    mark(df['lap_number'] == 1, 'lap1')
    mark(df['is_in_lap'] | df['is_pit_out_lap'], 'pit')
    mark(df['neutralised'].notna(), 'neutralised')
    mark(df['yellow'], 'yellow')
    return r


def clean_laps(df):
    """→ (чистые круги, отчёт: сколько кругов отброшено по каждой причине)."""
    df = df.copy()
    df['drop'] = drop_reason(df)

    kept = df[df['drop'].isna()]
    med = kept.groupby(['session_key', 'driver_number'])['lap_time'].transform('median')
    slow = kept['lap_time'] > OUTLIER_FACTOR * med
    df.loc[slow[slow].index, 'drop'] = 'outlier'

    kept = df[df['drop'].isna()]
    n = kept.groupby(['session_key', 'driver_number', 'stint'])['lap_time'].transform('size')
    short = n < MIN_STINT_LAPS
    df.loc[short[short].index, 'drop'] = 'short_stint'

    report = (df['drop'].value_counts()
              .reindex(list(REASONS)).fillna(0).astype(int)
              .rename(index=REASONS).rename('laps'))
    clean = df[df['drop'].isna()].drop(columns='drop')
    clean['group'] = clean['session_key'].astype(str) + '_' + clean['driver_number'].astype(str)
    return clean.reset_index(drop=True), report
