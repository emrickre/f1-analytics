"""Износ шин по свободным практикам: что стратег знает до гонки.

Длинный отрезок (long run) — стинт практики или спринта, где пилот едет ровным
гоночным темпом: круги в пределах `RUN_TOL` от медианы стинта (так отпадают
быстрые круги-попытки и круги охлаждения), не меньше `MIN_RUN` таких кругов.

Топливо в практике неизвестно, но внутри отрезка оно убывает на круг за круг —
поправка та же, что в гонке (`fuel` из сезонной модели, с/круг). Уровень топлива,
режим мотора и состояние трассы у разных отрезков разные, поэтому у каждого
отрезка свой свободный член, а износ — общий наклон по возрасту шин внутри
отрезков. По той же причине разницу темпа составов практика не даёт: отрезки на
разных составах едут с разным топливом.

Оценка по практике шумная и смещённая (в гонке шины берегут), поэтому прогноз
гоночного износа — взвешенное среднее с медианой сезона по другим трассам
(эмпирический Байес, `combine`).
"""

import re
import warnings

import numpy as np
import pandas as pd
import statsmodels.formula.api as smf

from .clean import DRY

RUN_TOL = 0.02      # круг отрезка — в пределах 2 % от медианы стинта
MIN_RUN = 5         # кругов в отрезке
MIN_LAPS = 15       # кругов состава на уикенде, чтобы оценивать его износ


def long_runs(laps, fuel, tol=RUN_TOL, min_laps=MIN_RUN):
    """Круги длинных отрезков с поправкой на топливо (`lap_time_fc`) и номером отрезка `run`."""
    d = laps
    ok = (d['lap_time'].notna() & d['compound'].isin(DRY) & ~d['is_in_lap']
          & ~d['is_pit_out_lap'] & d['neutralised'].isna() & ~d['yellow']
          & (d['rainfall'].fillna(0) == 0)
          & ~((d['session'] == 'Sprint') & (d['lap_number'] == 1)))
    d = d[ok].sort_values(['session_key', 'driver_number', 'lap_number'])
    key = ['session_key', 'driver_number', 'stint']
    med = d.groupby(key)['lap_time'].transform('median')
    d = d[(d['lap_time'] - med).abs() <= tol * med].copy()
    d = d[d.groupby(key)['lap_time'].transform('size') >= min_laps].copy()
    d['run'] = d.groupby(key).ngroup()
    d['run_lap'] = d.groupby('run').cumcount()
    d['lap_time_fc'] = d['lap_time'] + fuel * d['run_lap']    # топливо сгорает по ходу отрезка
    return d


def practice_deg(runs, min_laps=MIN_LAPS):
    """Износ составов по длинным отрезкам каждого уикенда, с/круг.

    Ошибка — кластеризованная по отрезкам: круги одного отрезка зависимы, и три
    отрезка — это три наблюдения, а не тридцать кругов.
    → race, compound, deg, se, laps, runs.
    """
    rows = []
    for race, g in runs.groupby('race', sort=False):
        comps = [c for c in DRY if (g['compound'] == c).sum() >= min_laps]
        g = g[g['compound'].isin(comps)]
        if not comps or g['run'].nunique() < 2:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            res = smf.ols('lap_time_fc ~ C(run) + C(compound):tyre_age', g).fit(
                cov_type='cluster', cov_kwds={'groups': g['run']})
        for name, value in res.params.items():
            m = re.search(r'\[(\w+)\]:tyre_age$', name)
            if m:
                c = g[g['compound'] == m.group(1)]
                rows.append({'race': race, 'compound': m.group(1), 'deg': value,
                             'se': res.bse[name], 'laps': len(c), 'runs': c['run'].nunique()})
    return pd.DataFrame(rows)


def _merge(practice, race_deg):
    y = race_deg[['race', 'compound', 'deg_s_per_lap']].rename(columns={'deg_s_per_lap': 'race_deg'})
    return y.merge(practice[['race', 'compound', 'deg', 'se']].rename(columns={'deg': 'practice'}),
                   on=['race', 'compound'], how='left')


def shrinkage(m):
    """Параметры сжатия по таблице гонок (race, compound, race_deg, practice, se):
    prior — медиана износа по составам; tau2 — разброс износа между трассами;
    bias и delta2 — сдвиг практики относительно гонки и её шум сверх `se`."""
    o = m.dropna(subset=['practice'])
    bias = float((o['practice'] - o['race_deg']).median())
    return {'prior': m.groupby('compound')['race_deg'].median().to_dict(),
            'tau2': float(m.groupby('compound')['race_deg'].var().mean()),
            'bias': bias,
            'delta2': float(max(((o['practice'] - bias - o['race_deg']) ** 2 - o['se'] ** 2).mean(), 1e-6))}


def shrink(practice, se, prior, k):
    """(прогноз, вес практики) — среднее prior и (практика − bias) с весами 1/tau² и 1/(se² + delta²)."""
    if practice is None or practice != practice:          # нет практики (None / NaN)
        return prior, 0.0
    s2 = se ** 2 + k['delta2']
    w = (1 / s2) / (1 / k['tau2'] + 1 / s2)
    return prior + w * (practice - k['bias'] - prior), w


def combine(practice, race_deg):
    """Прогноз износа в гонке до гонки: медиана сезона, уточнённая практикой.

    Для каждой гонки параметры сжатия (`shrinkage`) считаются только по **другим**
    гонкам (leave-one-race-out).
    → race, compound, race_deg (факт), practice, se, prior, post, weight.
    """
    m = _merge(practice, race_deg)
    out = []
    for row in m.itertuples():
        k = shrinkage(m[m['race'] != row.race])
        prior = k['prior'][row.compound]
        post, w = shrink(row.practice, row.se, prior, k)
        out.append({'prior': prior, 'post': post, 'weight': w})
    return pd.concat([m, pd.DataFrame(out, index=m.index)], axis=1)


def season_shrinkage(practice, race_deg):
    """Параметры сжатия по всему сезону — для live-приложения (live/practice.py)."""
    return shrinkage(_merge(practice, race_deg))


def prior_offsets(params, race):
    """Разница темпа составов по медиане остальных гонок (относительно первого состава гонки)."""
    comps = list(params[race]['offset'])
    base = comps[0]

    def rel(p, c):
        return p['offset'][c] - p['offset'][base] if c in p['offset'] and base in p['offset'] else np.nan

    return {c: 0.0 if c == base else
            float(np.nanmedian([rel(p, c) for r, p in params.items() if r != race]))
            for c in comps}


def pre_race_params(params, combined, race, use_practice=True):
    """Параметры стратегии, известные до гонки: износ — prior или prior+практика,
    темп составов — медиана сезона. Составы — те, на которых ехали в гонке."""
    c = combined[combined['race'] == race].set_index('compound')
    col = 'post' if use_practice else 'prior'
    return {'deg': {k: float(c.loc[k, col]) for k in params[race]['deg']},
            'offset': prior_offsets(params, race)}
