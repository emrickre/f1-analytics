"""Модель деградации шин.

Сезонная модель — линейная смешанная (statsmodels MixedLM):

    lap_time ~ C(compound) + C(compound):tyre_age + laps_remaining + track_temperature
    случайный эффект: гонка × пилот (базовый темп машины на трассе)

Коэффициент при `compound:tyre_age` — деградация, с/круг; при
`laps_remaining` — эффект топлива (каждый оставшийся круг ≈ лишний вес).
"""

import re
import warnings

import numpy as np
import pandas as pd
import statsmodels.formula.api as smf

COMPOUNDS = ('SOFT', 'MEDIUM', 'HARD')
FORMULA = ('lap_time ~ C(compound, Treatment("MEDIUM")) '
           '+ C(compound, Treatment("MEDIUM")):tyre_age + laps_remaining')


def _formula(df):
    temp = 'track_temperature' in df and df['track_temperature'].notna().all() \
        and df['track_temperature'].nunique() > 1
    return FORMULA + (' + track_temperature' if temp else '')


def fit_season(df):
    """Смешанная модель по всем гонкам сезона (df — чистые круги из clean_laps)."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')          # сходимость MixedLM многословна
        model = smf.mixedlm(_formula(df), df, groups=df['group'])
        return model.fit(method='lbfgs', reml=True)


def degradation(result, level=0.95):
    """Деградация по составам (с/круг) с доверительными интервалами."""
    ci = result.conf_int(alpha=1 - level)
    rows = []
    for name, value in result.params.items():
        m = re.search(r'\[T?\.?(\w+)\]:tyre_age$', name)
        if m:
            rows.append({'compound': m.group(1), 'deg_s_per_lap': value,
                         'ci_low': ci.loc[name, 0], 'ci_high': ci.loc[name, 1],
                         'p_value': result.pvalues[name]})
    out = pd.DataFrame(rows).set_index('compound')
    return out.reindex([c for c in COMPOUNDS if c in out.index])


def fuel_effect(result):
    """С/круг за каждый оставшийся круг топлива (+ ДИ)."""
    ci = result.conf_int().loc['laps_remaining']
    return result.params['laps_remaining'], ci[0], ci[1]


def compound_offsets(result):
    """Разница темпа новых шин относительно MEDIUM, с."""
    out = {'MEDIUM': 0.0}
    for name, value in result.params.items():
        m = re.fullmatch(r'C\(compound.*\)\[T\.(\w+)\]', name)
        if m:
            out[m.group(1)] = value
    return pd.Series(out).reindex([c for c in COMPOUNDS if c in out])


# --- по отдельной гонке -----------------------------------------------------------
#
# Деградация сильно зависит от трассы, поэтому для стратегии модель строится
# по каждой гонке. Топливо берём общим по сезону (fuel из fit_season): внутри
# одной гонки возраст шин и запас топлива слишком коррелируют.

def fit_race(df_race, fuel):
    """OLS внутри гонки по времени с поправкой на топливо; темп пилота — фикс. эффект."""
    present = [c for c in COMPOUNDS if (df_race['compound'] == c).sum() >= 30]
    if not present:
        raise ValueError('мало кругов сухих составов')
    d = df_race[df_race['compound'].isin(present)].assign(
        lap_time_fc=lambda x: x['lap_time'] - fuel * x['laps_remaining'])
    base = 'MEDIUM' if 'MEDIUM' in present else present[0]
    drivers = ' + C(driver)' if d['driver'].nunique() > 1 else ''
    comp = f'C(compound, Treatment("{base}"))'
    f = (f'lap_time_fc ~ {comp} + {comp}:tyre_age{drivers}' if len(present) > 1
         else f'lap_time_fc ~ tyre_age{drivers}')
    res = smf.ols(f, d).fit()
    res.base_compound, res.compounds, res.fuel = base, present, fuel
    return res


def race_params(result):
    """Из модели гонки: смещения составов (с) и деградация (с/круг) — для стратегии."""
    deg, off = {}, {c: 0.0 for c in result.compounds}
    for name, value in result.params.items():
        if name == 'tyre_age':
            deg[result.compounds[0]] = value
            continue
        m = re.search(r'\[T?\.?(\w+)\]:tyre_age$', name)
        if m:
            deg[m.group(1)] = value
            continue
        m = re.fullmatch(r'C\(compound, Treatment\("\w+"\)\)\[T\.(\w+)\]', name)
        if m:
            off[m.group(1)] = value
    return {'deg': deg, 'offset': off, 'fuel': result.fuel}


def deg_by_race(df, fuel):
    """Деградация каждого состава в каждой гонке (с ДИ)."""
    rows = []
    for race, d in df.groupby('race', sort=False):
        try:
            r = fit_race(d, fuel)
        except (ValueError, np.linalg.LinAlgError):
            continue
        ci = r.conf_int()
        for name, value in r.params.items():
            m = re.search(r'\[T?\.?(\w+)\]:tyre_age$', name)
            comp = m.group(1) if m else (r.compounds[0] if name == 'tyre_age' else None)
            if comp:
                rows.append({'race': race, 'compound': comp, 'deg_s_per_lap': value,
                             'ci_low': ci.loc[name, 0], 'ci_high': ci.loc[name, 1],
                             'laps': int((d['compound'] == comp).sum()),
                             'track_temp': d['track_temperature'].mean()})
    return pd.DataFrame(rows)


def paired_compound_diff(by_race, compound, base='MEDIUM'):
    """Разница деградации compound − base внутри одних и тех же гонок."""
    p = by_race.pivot(index='race', columns='compound', values='deg_s_per_lap')
    if compound not in p or base not in p:
        return pd.Series(dtype=float)
    return (p[compound] - p[base]).dropna()


# --- валидация -------------------------------------------------------------------

def stint_forecast_errors(test, deg, fuel, ref_laps=3, min_ahead=3):
    """Ошибки прогноза внутри стинта по первым ref_laps чистым кругам.

    deg — {состав: с/круг} или функция (session_key, driver, состав) → с/круг.
    Модель: t = t_ref + deg·(age − age_ref) − fuel·(laps_rem_ref − laps_rem).
    Базовая линия: «темп не меняется» (t = t_ref).
    """
    get = deg if callable(deg) else (lambda key, drv, c: deg.get(c))
    errs = []
    for (key, drv, _), s in test.groupby(['session_key', 'driver_number', 'stint']):
        s = s.sort_values('lap_number')
        d = get(key, drv, s['compound'].iloc[0])
        if len(s) < ref_laps + min_ahead or d is None:
            continue
        ref, ahead = s.iloc[:ref_laps], s.iloc[ref_laps:]
        t0 = ref['lap_time'].mean()
        a0, r0 = ref['tyre_age'].mean(), ref['laps_remaining'].mean()
        pred = t0 + d * (ahead['tyre_age'] - a0) + fuel * (ahead['laps_remaining'] - r0)
        errs.append(pd.DataFrame({'model': (ahead['lap_time'] - pred).abs(),
                                  'baseline': (ahead['lap_time'] - t0).abs(),
                                  'laps_ahead': np.arange(1, len(ahead) + 1)}))
    return pd.concat(errs, ignore_index=True) if errs else pd.DataFrame()


def leave_one_race_out(df):
    """Средняя по сезону деградация → прогноз на трассе, которой не было в обучении."""
    rows = []
    for race in df['race'].unique():
        train, test = df[df['race'] != race], df[df['race'] == race]
        res = fit_season(train)
        e = stint_forecast_errors(test, degradation(res)['deg_s_per_lap'].to_dict(),
                                  fuel_effect(res)[0])
        if not e.empty:
            rows.append({'race': race, 'mae_model': e['model'].mean(),
                         'mae_baseline': e['baseline'].mean(), 'laps': len(e)})
    return pd.DataFrame(rows)


def leave_one_driver_out(df, fuel):
    """Деградация трассы по ДРУГИМ пилотам той же гонки → прогноз для пилота.

    Так работают стратеги: оценивают износ по соперникам, уже проехавшим стинт.
    """
    rows = []
    for race, d in df.groupby('race', sort=False):
        cache = {}

        def deg(key, drv, comp):
            if drv not in cache:
                try:
                    cache[drv] = race_params(fit_race(d[d['driver_number'] != drv], fuel))['deg']
                except (ValueError, np.linalg.LinAlgError):
                    cache[drv] = {}
            return cache[drv].get(comp)

        e = stint_forecast_errors(d, deg, fuel)
        if not e.empty:
            rows.append({'race': race, 'mae_model': e['model'].mean(),
                         'mae_baseline': e['baseline'].mean(), 'laps': len(e)})
    return pd.DataFrame(rows)
