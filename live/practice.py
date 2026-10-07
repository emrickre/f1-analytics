"""Износ шин до старта: длинные отрезки практик того же уикенда.

Чистый Python (серверу не нужен pandas): та же логика, что analysis/practice.py,
см. «Planning from practice» в notebooks/strategy_sim.ipynb.

* длинный отрезок — стинт, где не меньше MIN_RUN кругов в пределах RUN_TOL от
  медианы стинта (без кругов заезда/выезда, флагов, дождя и старта спринта);
* износ — наклон по возрасту шин внутри отрезков с поправкой на сгорающее
  топливо, ошибка кластеризована по отрезкам;
* прогноз на гонку — взвешенное среднее медианы сезона и практики (минус её
  систематический сдвиг) с весами 1/tau² и 1/(se² + delta²).
"""

import logging
import math
from bisect import bisect_right
from datetime import timedelta
from statistics import median

from . import openf1
from .history import DRY

log = logging.getLogger('live.practice')

PRACTICE = ('Practice 1', 'Practice 2', 'Practice 3', 'Sprint')
RUN_TOL = 0.02
MIN_RUN = 5
MIN_LAPS = 15
ENDPOINTS = ('laps', 'stints', 'pit', 'race_control', 'weather')

# Параметры сжатия по сезону 2026: analysis.practice.season_shrinkage
# (износ по гонкам — analysis.model.deg_by_race, практика — practice_deg).
SHRINK = {'prior': {'SOFT': 0.0504, 'MEDIUM': 0.0512, 'HARD': 0.0460},
          'tau2': 0.00278, 'bias': 0.0171, 'delta2': 0.00349}


# --- круги практики -----------------------------------------------------------

def _neutral_intervals(race_control):
    """[(start, end)] — SC, VSC и красный флаг (как analysis.dataset.neutralisations)."""
    out, cur = [], None
    for m in sorted(race_control, key=lambda m: m['date']):
        msg = (m.get('message') or '').upper()
        t = openf1.ts_of(m['date'])
        kind = ('VSC' if 'VSC DEPLOYED' in msg or 'VIRTUAL SAFETY CAR DEPLOYED' in msg
                else 'SC' if 'SAFETY CAR DEPLOYED' in msg
                else 'RED' if m.get('flag') == 'RED' else None)
        ends = ('TRACK CLEAR' in msg
                or (m.get('flag') in ('GREEN', 'CLEAR') and m.get('scope') == 'Track'))
        if kind:
            if cur and cur[1] != kind:
                out.append((cur[0], t))
            if not cur or cur[1] != kind:
                cur = (t, kind)
        elif ends and cur:
            out.append((cur[0], t))
            cur = None
    if cur:
        out.append((cur[0], cur[0] + timedelta(days=1)))
    return out


def session_laps(raw, sprint=False):
    """Круги одной сессии, пригодные для длинных отрезков:
    [{'num', 'stint', 'lap', 't', 'comp', 'age'}]."""
    pit = openf1.normalize_pits(raw['pit'], raw['laps'])
    stints = {}
    for s in openf1.reconcile_stints(raw['stints'], pit, raw['laps']):
        stints.setdefault(s['driver_number'], []).append(s)
    in_laps = {(p['driver_number'], p['lap_number']) for p in pit}
    neutral = _neutral_intervals(raw['race_control'])
    yellow = sorted(openf1.ts_of(m['date']) for m in raw['race_control']
                    if m.get('flag') in ('YELLOW', 'DOUBLE YELLOW'))
    rain = sorted((openf1.ts_of(w['date']), bool(w.get('rainfall'))) for w in raw['weather'])
    rain_t = [t for t, _ in rain]

    out = []
    for r in raw['laps']:
        num, lap, t = r['driver_number'], r['lap_number'], r.get('lap_duration')
        if (not t or not r.get('date_start') or r.get('is_pit_out_lap')
                or (num, lap) in in_laps or (sprint and lap == 1)):
            continue
        a = openf1.ts_of(r['date_start'])
        b = a + timedelta(seconds=t)
        if any(s < b and e > a for s, e in neutral):
            continue
        i = bisect_right(yellow, a)
        if i < len(yellow) and yellow[i] <= b:
            continue
        j = bisect_right(rain_t, a) - 1
        if j >= 0 and rain[j][1]:
            continue
        s = next((s for s in stints.get(num, ())
                  if s['lap_start'] <= lap <= (s.get('lap_end') or 10_000)), None)
        if not s or s.get('compound') not in DRY:
            continue
        out.append({'num': num, 'stint': s['stint_number'], 'lap': lap, 't': t,
                    'comp': s['compound'],
                    'age': (s.get('tyre_age_at_start') or 0) + lap - s['lap_start']})
    return out


def long_runs(laps, fuel, tol=RUN_TOL, min_laps=MIN_RUN):
    """{run: [(comp, age, t + fuel·круг_отрезка)]} — отрезки ровного темпа.
    laps — [(session, row)], run — (session, num, stint)."""
    groups = {}
    for ses, r in laps:
        groups.setdefault((ses, r['num'], r['stint']), []).append(r)
    runs = {}
    for key, rows in groups.items():
        med = median(r['t'] for r in rows)
        rows = sorted((r for r in rows if abs(r['t'] - med) <= tol * med), key=lambda r: r['lap'])
        if len(rows) >= min_laps:
            runs[key] = [(r['comp'], r['age'], r['t'] + fuel * i) for i, r in enumerate(rows)]
    return runs


def wear(runs, min_laps=MIN_LAPS):
    """Износ по составам: наклон внутри отрезков (у каждого свой уровень), ошибка
    кластеризована по отрезкам — как OLS с C(run) и cov_type='cluster' в statsmodels.
    → {состав: {'deg', 'se', 'laps', 'runs'}}."""
    count = {}
    for pts in runs.values():
        count[pts[0][0]] = count.get(pts[0][0], 0) + len(pts)
    runs = {k: pts for k, pts in runs.items() if count[pts[0][0]] >= min_laps}
    if len(runs) < 2:
        return {}
    demeaned = {}
    for k, pts in runs.items():
        ma = sum(a for _, a, _ in pts) / len(pts)
        my = sum(y for _, _, y in pts) / len(pts)
        demeaned[k] = (pts[0][0], [(a - ma, y - my) for _, a, y in pts])
    acc = {}
    for comp, pts in demeaned.values():
        a = acc.setdefault(comp, [0.0, 0.0])
        a[0] += sum(x * x for x, _ in pts)
        a[1] += sum(x * y for x, y in pts)
    slope = {c: sxy / sxx for c, (sxx, sxy) in acc.items() if sxx > 0}
    n = sum(len(p) for _, p in demeaned.values())
    g = len(demeaned)
    k = g + len(slope)                                  # уровни отрезков + наклоны
    if n <= k or g < 2:
        return {}
    corr = g / (g - 1) * (n - 1) / (n - k)
    meat = {}
    for comp, pts in demeaned.values():
        if comp in slope:
            s = sum(x * (y - slope[comp] * x) for x, y in pts)
            meat[comp] = meat.get(comp, 0.0) + s * s
    out = {}
    for comp, b in slope.items():
        rs = [p for c, p in demeaned.values() if c == comp]
        out[comp] = {'deg': b, 'se': math.sqrt(corr * meat[comp]) / acc[comp][0],
                     'laps': sum(len(p) for p in rs), 'runs': len(rs)}
    return out


def pre_race(practice, k=SHRINK):
    """Прогноз износа на гонку по каждому сухому составу.
    → {состав: {'deg', 'prior', 'practice', 'se', 'runs', 'weight'}}."""
    out = {}
    for comp in DRY:
        prior = k['prior'][comp]
        p = practice.get(comp)
        if not p:
            out[comp] = {'deg': prior, 'prior': prior, 'practice': None, 'se': None,
                         'runs': 0, 'weight': 0.0}
            continue
        s2 = p['se'] ** 2 + k['delta2']
        w = (1 / s2) / (1 / k['tau2'] + 1 / s2)
        out[comp] = {'deg': prior + w * (p['deg'] - k['bias'] - prior), 'prior': prior,
                     'practice': p['deg'], 'se': p['se'], 'runs': p['runs'], 'weight': w}
    return out


# --- загрузка для гонки ------------------------------------------------------------

def weekend_sessions(info):
    """Практики и спринт того же уикенда, прошедшие до старта этой гонки."""
    meeting = (info.get('Meeting') or {}).get('Name')
    start = str(info.get('StartDate') or '')
    if not meeting or not start[:4].isdigit():
        return []
    return [s for s in openf1.catalog(int(start[:4]))
            if s['meeting'] == meeting and s['name'] in PRACTICE and s['available']
            and s['start'][:19] < start[:19]]


def for_race(info, fuel, src=None):
    """Износ по практикам для гонки из SessionInfo → dict для strategy_view или None."""
    sessions = weekend_sessions(info)
    if not sessions:
        return None
    src = src or openf1.OpenF1Source()
    laps = []
    for s in sessions:
        cdir = src.cache_dir / str(s['key'])
        raw = {ep: src._get(ep, cdir / f'{ep}.json', session_key=s['key']) for ep in ENDPOINTS}
        laps += [(s['key'], r) for r in session_laps(raw, sprint=s['name'] == 'Sprint')]
    runs = long_runs(laps, fuel)
    practice = wear(runs)
    log.info('practice: %s — %d long runs, %s', ', '.join(s['name'] for s in sessions),
             len(runs), {c: round(v['deg'], 3) for c, v in practice.items()})
    return {'sessions': [s['name'] for s in sessions], 'runs': len(runs),
            'compounds': pre_race(practice)}
