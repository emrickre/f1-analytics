"""Прогноз результата гонки по тому, что известно до старта.

Единица — пилот × гонка (data/results_2026.parquet). Признаки:
* grid — официальная стартовая позиция (со штрафами);
* quali_gap — отставание лучшего круга квалификации от поула, %;
* form_team / form_driver — средний финиш команды / пилота в FORM_RACES прошлых гонках;
* pace — темп длинных отрезков практик относительно поля, % (analysis/practice.py).

Проверка — rolling origin: гонку k прогнозирует модель, обученная только на гонках
до k, поэтому в признаках и в обучении нет ничего из будущего.

Вероятности (победа, подиум, очки) — Монте-Карло: сход с вероятностью по надёжности
команды (`reliability`), порядок финишировавших — прогноз модели плюс нормальный
шум с разбросом её остатков на прошлых гонках.
"""

import numpy as np
import pandas as pd
import statsmodels.formula.api as smf
from scipy.stats import spearmanr

FORM_RACES = 3
RELIABILITY_PRIOR = 10      # сила априори для надёжности команды, «гонок» средней по сезону
EVENTS = {'win': 1, 'podium': 3, 'points': 10}


# --- признаки --------------------------------------------------------------------

def practice_pace(runs, deg, offsets):
    """Темп длинных отрезков практик относительно поля, % (> 0 — медленнее).

    runs — analysis.practice.long_runs; deg — {(race, compound): с/круг} износ по
    практике уикенда; offsets — {compound: с} разница темпа составов. Круг отрезка
    приводится к новым шинам MEDIUM, среднее по отрезку сравнивается с медианой
    отрезков той же сессии (топливо и трасса у сессий разные), по пилоту — медиана.
    → race, driver_number, pace, runs.
    """
    r = runs.copy()
    r['adj'] = (r['lap_time_fc'] - [deg[(a, c)] * t for a, c, t
                                    in zip(r['race'], r['compound'], r['tyre_age'])]
                - r['compound'].map(offsets))
    m = r.groupby(['race', 'session', 'run', 'driver_number'], as_index=False)['adj'].mean()
    m['rel'] = (m['adj'] / m.groupby(['race', 'session'])['adj'].transform('median') - 1) * 100
    return m.groupby(['race', 'driver_number'], as_index=False).agg(
        pace=('rel', 'median'), runs=('rel', 'size'))


def add_form(results, k=FORM_RACES):
    """form_driver, form_team — средний финиш в k прошлых гонках (только классифицированные)."""
    d = results.copy()
    rounds = sorted(d['round'].unique())
    for col, key in (('form_driver', 'driver_number'), ('form_team', 'team')):
        d[col] = np.nan
        for i, rd in enumerate(rounds):
            past = d[d['round'].isin(rounds[max(0, i - k):i])]
            mean = past.groupby(key)['finish'].mean()
            idx = d['round'] == rd
            d.loc[idx, col] = d.loc[idx, key].map(mean)
    return d


def reliability(past, teams, a=RELIABILITY_PRIOR):
    """Вероятность не доехать (сход или не классифицирован) по прошлым гонкам команды,
    сжатая к средней сезона: (сходы + a·p0) / (старты + a)."""
    out = past['status'].isin(['dnf', 'nc'])
    p0 = out.mean()
    g = out.groupby(past['team']).agg(['sum', 'size'])
    return teams.map((g['sum'] + a * p0) / (g['size'] + a)).fillna(p0).to_numpy()


def _prepare(test):
    """Пропуски в тесте: нет прошлых гонок — форма по стартовой позиции; нет круга
    в квалификации — как у последнего."""
    test = test.copy()
    if 'form_team' in test:
        test['form_team'] = test['form_team'].fillna(test['grid'])
    if 'form_driver' in test:
        test['form_driver'] = test['form_driver'].fillna(test['grid'])
    if 'quali_gap' in test:
        test['quali_gap'] = test['quali_gap'].fillna(test['quali_gap'].max())
    if 'pace' in test:
        test['pace'] = test['pace'].fillna(0.0)
    return test


def fit(train, formula):
    """OLS места на финише (классифицированные пилоты прошлых гонок)."""
    data = train[train['status'] == 'finished'].dropna(
        subset=[c for c in ('form_team', 'form_driver', 'quali_gap') if c in formula])
    return smf.ols('finish ~ ' + formula, _prepare(data)).fit()


# --- точечный прогноз ------------------------------------------------------------

def rolling_ranks(d, formulas, start=3):
    """Прогноз каждой гонки начиная с номера start+1 по моделям formulas {имя: правая часть}.

    Сравнение среди классифицированных: ранговая корреляция с фактом, средняя ошибка
    места, угадан ли победитель, сколько из подиума угадано.
    → round, race, model, rho, mae, winner, podium.
    """
    rounds = sorted(d['round'].unique())
    rows = []
    for rd in rounds[start:]:
        test = _prepare(d[d['round'] == rd])
        fin = test[test['status'] == 'finished']
        for name, f in formulas.items():
            score = fit(d[d['round'] < rd], f).predict(test)
            rank = score[fin.index].rank()
            rows.append({'round': rd, 'race': test['race'].iloc[0], 'model': name,
                         'rho': spearmanr(rank, fin['finish']).statistic,
                         'mae': (rank - fin['finish'].rank()).abs().mean(),
                         'winner': bool(test.loc[score.idxmin(), 'finish'] == 1),
                         'podium': len(set(score.nsmallest(3).index)
                                       & set(fin.index[fin['finish'] <= 3]))})
    return pd.DataFrame(rows)


# --- вероятности -----------------------------------------------------------------

def simulate(score, sigma, p_out, n=5000, seed=0):
    """P(место ≤ k) для каждого пилота: сход с вероятностью p_out, порядок остальных —
    score + N(0, sigma). → {событие: массив вероятностей}."""
    rng = np.random.default_rng(seed)
    k = len(score)
    noisy = np.asarray(score, float)[None, :] + rng.normal(0, sigma, (n, k))
    out = rng.random((n, k)) < np.asarray(p_out)[None, :]
    noisy[out] = np.inf
    order = np.argsort(noisy, axis=1)
    pos = np.empty_like(order)
    pos[np.arange(n)[:, None], order] = np.arange(1, k + 1)
    pos[out] = k + 1
    return {ev: (pos <= top).mean(0) for ev, top in EVENTS.items()}


def rolling_probabilities(d, configs, start=3, n=5000):
    """Вероятности для каждой гонки начиная с номера start+1.

    configs — {имя: (формула, 'team' | 'season')}: модель порядка и риск схода (по
    команде или одинаковый для всех). → по пилоту: round, race, driver, model,
    событие, p, y (произошло ли).
    """
    rounds = sorted(d['round'].unique())
    rows = []
    for rd in rounds[start:]:
        past, test = d[d['round'] < rd], _prepare(d[d['round'] == rd])
        for name, (f, risk) in configs.items():
            m = fit(past, f)
            p_out = (reliability(past, test['team']) if risk == 'team'
                     else np.full(len(test), past['status'].isin(['dnf', 'nc']).mean()))
            sim = simulate(m.predict(test).to_numpy(), np.sqrt(m.scale), p_out, n=n, seed=rd)
            for ev, top in EVENTS.items():
                rows.append(pd.DataFrame({
                    'round': rd, 'race': test['race'].to_numpy(), 'driver': test['driver'].to_numpy(),
                    'model': name, 'event': ev, 'p': sim[ev],
                    'y': (test['finish'] <= top).to_numpy()}))
    return pd.concat(rows, ignore_index=True)


def log_loss(p, y, eps=1e-3):
    p = np.clip(p, eps, 1 - eps)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def dnf_log_loss(d, start=3, a=RELIABILITY_PRIOR):
    """Прогноз схода по прошлым гонкам: одинаковый для всех против надёжности команды.
    → round, season, team (log loss по пилотам гонки)."""
    rounds = sorted(d['round'].unique())
    rows = []
    for rd in rounds[start:]:
        past, test = d[d['round'] < rd], d[d['round'] == rd]
        y = test['status'].isin(['dnf', 'nc']).to_numpy()
        p0 = np.full(len(test), past['status'].isin(['dnf', 'nc']).mean())
        rows.append({'round': rd, 'season': log_loss(p0, y),
                     'team': log_loss(reliability(past, test['team'], a), y)})
    return pd.DataFrame(rows)
