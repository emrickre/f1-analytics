"""Тесты analysis/ — без сети, на синтетике с известным ответом.

python -m unittest tests.test_analysis
"""

import unittest

import numpy as np
import pandas as pd

from analysis.clean import clean_laps
from analysis.dataset import build_session_laps, neutralisations
from analysis import model as M
from analysis import strategy as S
from analysis import undercut as U
from analysis import simulate as X
from analysis import practice as F
from analysis import predict as P

T0 = pd.Timestamp('2026-07-05T14:00:00Z')


def iso(sec):
    return (T0 + pd.Timedelta(seconds=sec)).isoformat()


def raw_race(n_laps=12, lap=90.0):
    """Мини-гонка: пилот 1 едет на 1 остановку (MEDIUM → HARD на 6-м круге),
    SC с 8-го по 9-й круг, жёлтый флаг на 3-м круге."""
    laps = [{'session_key': 1, 'driver_number': 1, 'lap_number': i,
             'date_start': iso((i - 1) * lap), 'lap_duration': lap,
             'duration_sector_1': 30, 'duration_sector_2': 30, 'duration_sector_3': 30,
             'is_pit_out_lap': i == 7, 'st_speed': 300} for i in range(1, n_laps + 1)]
    return {
        'laps': laps,
        'drivers': [{'driver_number': 1, 'name_acronym': 'NOR', 'team_name': 'McLaren'}],
        'stints': [
            {'driver_number': 1, 'stint_number': 1, 'lap_start': 1, 'lap_end': 6,
             'compound': 'MEDIUM', 'tyre_age_at_start': 0},
            {'driver_number': 1, 'stint_number': 2, 'lap_start': 7, 'lap_end': n_laps,
             'compound': 'HARD', 'tyre_age_at_start': 2}],
        'pit': [{'driver_number': 1, 'lap_number': 6, 'lane_duration': 21.0}],
        'race_control': [
            {'date': iso(2 * lap + 10), 'message': 'YELLOW IN TRACK SECTOR 4',
             'flag': 'YELLOW', 'scope': 'Sector'},
            {'date': iso(7 * lap + 5), 'message': 'SAFETY CAR DEPLOYED',
             'flag': None, 'scope': None},
            {'date': iso(8 * lap + 80), 'message': 'TRACK CLEAR', 'flag': 'CLEAR',
             'scope': 'Track'}],
        'weather': [{'date': iso(-60), 'track_temperature': 40.0,
                     'air_temperature': 25.0, 'rainfall': 0}],
    }


class DatasetTest(unittest.TestCase):
    def setUp(self):
        self.df = build_session_laps(raw_race()).set_index('lap_number')

    def test_tyres(self):
        self.assertEqual(self.df.loc[6, 'compound'], 'MEDIUM')
        self.assertEqual(self.df.loc[6, 'tyre_age'], 5)
        self.assertEqual(self.df.loc[7, 'compound'], 'HARD')
        self.assertEqual(self.df.loc[7, 'tyre_age'], 2)        # б/у комплект
        self.assertEqual(self.df.loc[12, 'tyre_age'], 7)

    def test_flags(self):
        self.assertTrue(self.df.loc[6, 'is_in_lap'])
        self.assertEqual(self.df.loc[6, 'pit_lane_time'], 21.0)
        self.assertTrue(self.df.loc[7, 'is_pit_out_lap'])
        self.assertTrue(self.df.loc[3, 'yellow'])
        self.assertFalse(self.df.loc[4, 'yellow'])
        self.assertEqual(list(self.df['neutralised'].notna().index[self.df['neutralised'].notna()]),
                         [8, 9])
        self.assertEqual(self.df.loc[12, 'laps_remaining'], 0)
        self.assertEqual(self.df.loc[5, 'track_temperature'], 40.0)

    def test_neutralisations_switch(self):
        rc = [{'date': iso(0), 'message': 'VSC DEPLOYED'},
              {'date': iso(30), 'message': 'SAFETY CAR DEPLOYED'},
              {'date': iso(90), 'message': 'TRACK CLEAR', 'flag': 'CLEAR', 'scope': 'Track'}]
        kinds = [k for _, _, k in neutralisations(rc)]
        self.assertEqual(kinds, ['VSC', 'SC'])


class CleanTest(unittest.TestCase):
    def test_reasons(self):
        df = build_session_laps(raw_race(n_laps=20))
        df.loc[df['lap_number'] == 11, 'lap_time'] = 200.0     # выброс
        clean, report = clean_laps(df)
        # 1-й стинт: остались 2, 4, 5 — меньше 4 кругов, стинт отбрасывается целиком
        self.assertEqual(sorted(clean['lap_number']), [10] + list(range(12, 21)))
        self.assertEqual(report['race start lap'], 1)
        self.assertEqual(report['pit in / out lap'], 2)
        self.assertEqual(report['SC / VSC / red flag'], 2)
        self.assertEqual(report['yellow flag in sector'], 1)
        self.assertEqual(report['slower than 107% of driver median'], 1)
        self.assertEqual(report['stint shorter than 4 clean laps'], 3)


def synthetic_race(deg=None, fuel=0.04, n_laps=50, drivers=12, noise=0.15, seed=0,
                   evolution=0.0, quad=None):
    """Круги с известной деградацией, эффектом топлива, эволюцией трассы
    (с/круг гонки) и квадратичным износом (с/круг²) по составам."""
    deg = deg or {'MEDIUM': 0.08, 'HARD': 0.04}
    quad = quad or {}
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(drivers):
        pace = 90 + rng.normal(0, 0.5)
        stop = int(rng.integers(18, 30))
        first, second = ('MEDIUM', 'HARD') if d % 2 else ('HARD', 'MEDIUM')
        for lap in range(2, n_laps + 1):
            comp, age = (first, lap - 1) if lap <= stop else (second, lap - stop)
            t = (pace + deg[comp] * age + quad.get(comp, 0.0) * age ** 2
                 + fuel * (n_laps - lap) + evolution * lap + rng.normal(0, noise))
            rows.append({'race': 'Test GP', 'session_key': 1, 'driver_number': d,
                         'driver': f'D{d}', 'lap_number': lap, 'lap_time': t,
                         'compound': comp, 'tyre_age': age, 'stint': 1 + (lap > stop),
                         'laps_remaining': n_laps - lap, 'track_temperature': 40.0,
                         'group': f'1_{d}'})
    return pd.DataFrame(rows)


class ModelTest(unittest.TestCase):
    def test_race_model_recovers_degradation(self):
        df = synthetic_race()
        p = M.race_params(M.fit_race(df, fuel=0.04))
        self.assertAlmostEqual(p['deg']['MEDIUM'], 0.08, delta=0.01)
        self.assertAlmostEqual(p['deg']['HARD'], 0.04, delta=0.01)

    def test_season_model_recovers_fuel(self):
        df = pd.concat([synthetic_race(seed=s).assign(race=f'R{s}', session_key=s,
                                                      group=lambda x, s=s: f'{s}_' + x['driver'])
                        for s in range(3)])
        res = M.fit_season(df)
        self.assertAlmostEqual(M.fuel_effect(res)[0], 0.04, delta=0.01)
        deg = M.degradation(res)['deg_s_per_lap']
        self.assertAlmostEqual(deg['MEDIUM'], 0.08, delta=0.01)

    def test_track_evolution_separated_from_wear(self):
        # Трасса ускоряется на 0.03 с/круг; пилоты меняют шины на разных кругах
        df = synthetic_race(evolution=-0.03)
        p = M.race_params(M.fit_race(df, fuel=0.04, evolution=True))
        self.assertAlmostEqual(p['evolution'], -0.03, delta=0.005)
        self.assertAlmostEqual(p['deg']['MEDIUM'], 0.08, delta=0.01)
        self.assertAlmostEqual(p['deg']['HARD'], 0.04, delta=0.01)
        # Без члена эволюции ускорение трассы «съедает» часть износа
        naive = M.race_params(M.fit_race(df, fuel=0.04))
        self.assertLess(naive['deg']['MEDIUM'], 0.08 - 0.015)
        self.assertEqual(naive['evolution'], 0.0)

    def test_quadratic_wear(self):
        df = synthetic_race(quad={'MEDIUM': 0.002}, seed=1)
        p = M.race_params(M.fit_race(df, fuel=0.04, curve=True))
        self.assertAlmostEqual(p['deg2']['MEDIUM'], 0.002, delta=0.0005)
        self.assertAlmostEqual(p['deg2']['HARD'], 0.0, delta=0.0005)
        # Прогноз внутри стинта с кривой точнее линейного
        lin = M.leave_one_driver_out(df.assign(race='R'), 0.04)
        cur = M.leave_one_driver_out(df.assign(race='R'), 0.04, curve=True)
        self.assertLess(cur['mae_model'].iloc[0], lin['mae_model'].iloc[0])

    def test_forecast_beats_flat_baseline(self):
        df = synthetic_race()
        e = M.stint_forecast_errors(df, {'MEDIUM': 0.08, 'HARD': 0.04}, fuel=0.04)
        self.assertLess(e['model'].mean(), e['baseline'].mean())


class StrategyTest(unittest.TestCase):
    params = {'offset': {'MEDIUM': 0.0, 'HARD': 0.0},
              'deg': {'MEDIUM': 0.10, 'HARD': 0.05}, 'fuel': 0.0}

    def test_one_stop_optimum(self):
        # Σ 0.1·a (n кругов) + Σ 0.05·a (50−n кругов) → минимум при n ≈ 16.8
        curve = S.one_stop_curve(self.params, 50, 'MEDIUM', 'HARD', loss=20)
        best, lo, hi = S.optimal_window(curve)
        self.assertEqual(best, 17)
        self.assertLessEqual(lo, 17)
        self.assertGreaterEqual(hi, 17)

    def test_undercut(self):
        g = S.undercut_gain(self.params, 'MEDIUM', 20, 'HARD', laps=3, warmup=1.0)
        self.assertAlmostEqual(g[0], 0.10 * 20 - 1.0)
        self.assertTrue(np.all(np.diff(g) > 0))           # каждый круг добавляет выигрыш

    def test_pit_loss(self):
        raw = build_session_laps(raw_race())
        raw.loc[raw['lap_number'] == 6, 'lap_time'] = 100.0   # круг заезда
        raw.loc[raw['lap_number'] == 7, 'lap_time'] = 110.0   # круг выезда
        clean = raw[~raw['lap_number'].isin([6, 7])]
        loss, n = S.pit_loss(raw, clean)
        self.assertEqual(n, 1)
        self.assertAlmostEqual(loss, 100 + 110 - 2 * 90)


def duel_race(sc_lap=None):
    """Две машины 10 кругов по 90 с: B впереди на 2 с, A заезжает на 4-м круге,
    B — на 5-м. На свежих шинах A на 1.5 с быстрее; оба стопа по 20 с."""
    rows = []
    for num, name, offset, stop in ((1, 'AAA', 2.0, 4), (2, 'BBB', 0.0, 5)):
        t = offset
        for lap in range(1, 11):
            lt = 90.0
            if lap == stop:
                lt += 10                      # круг заезда
            if lap == stop + 1:
                lt += 10                      # круг выезда
            if num == 1 and lap > stop:
                lt -= 1.5                     # свежие шины против старых у B
            rows.append({'session_key': 1, 'race': 'Test GP', 'driver_number': num, 'driver': name,
                         'lap_number': lap, 'date_start': T0 + pd.Timedelta(seconds=t), 'lap_time': lt,
                         'is_in_lap': lap == stop, 'pit_lane_time': 20.0 if lap == stop else np.nan,
                         'neutralised': 'SC' if lap == sc_lap else None,
                         'tyre_age': lap if lap <= stop else lap - stop,
                         'compound': 'MEDIUM' if lap <= stop else 'HARD'})
            t += lt
    return pd.DataFrame(rows)


class UndercutTest(unittest.TestCase):
    def test_pair(self):
        p = U.pit_pairs(duel_race())
        self.assertEqual(len(p), 1)
        r = p.iloc[0]
        self.assertEqual((r['first'], r['second'], r['stop_lap'], r['response_laps']), (1, 2, 4, 1))
        self.assertAlmostEqual(r['gap_before'], 2.0)
        # A на свежих шинах на кругах 5 и 6: 2 × 1.5 с
        self.assertAlmostEqual(r['gain'], 3.0)
        self.assertTrue(r['overtook'])
        self.assertEqual((r['second_old'], r['first_new']), ('MEDIUM', 'HARD'))
        self.assertAlmostEqual(r['pit_lane_diff'], 0.0)

    def test_neutralised_pair_dropped(self):
        self.assertTrue(U.pit_pairs(duel_race(sc_lap=5)).empty)

    def test_gap_limit(self):
        self.assertTrue(U.pit_pairs(duel_race(), max_gap=1.0).empty)


class SimulateTest(unittest.TestCase):
    params = {'offset': {'MEDIUM': 0.0, 'HARD': 0.3}, 'deg': {'MEDIUM': 0.10, 'HARD': 0.05}}

    def test_events_from_laps(self):
        rows = [{'race': 'R', 'total_laps': 10, 'lap_number': l, 'driver_number': d,
                 'neutralised': 'SC' if 3 <= l <= 5 else ('VSC' if l == 8 else None)}
                for l in range(1, 11) for d in (1, 2)]
        ev = X.neutralisation_events(pd.DataFrame(rows))
        self.assertEqual(ev[['kind', 'start', 'end', 'laps']].values.tolist(),
                         [['SC', 3, 5, 3], ['VSC', 8, 8, 1]])

    def test_scenarios_rate(self):
        model = {'p': 0.05, 'share_sc': 0.5, 'durations': {'SC': np.array([3]), 'VSC': np.array([2])}}
        sc = X.sample_scenarios(model, 60, 3000, seed=0)
        starts = ((sc[:, 1:] != 0) & (sc[:, :-1] == 0)).sum(1) + (sc[:, 0] != 0)
        # ~ 0.05 на «зелёный» круг: при средней длине 2.5 круга ≈ 2.6 эпизода на 60 кругов
        self.assertAlmostEqual(starts.mean(), 60 / (1 / 0.05 + 2.5), delta=0.25)
        self.assertTrue(set(np.unique(sc)) <= {0, 1, 2})

    def test_no_neutralisation_equals_deterministic(self):
        one, two, det = X.best_plans(self.params, 50, loss=20.0)
        green = np.zeros((3, 50), dtype=np.int8)
        ratio = {'GREEN': 1.0, 'SC': 0.5, 'VSC': 0.8}
        for plan in (one, two):
            for pol in ('fixed', 'react'):
                t = X.race_times(self.params, 50, plan, green, 20.0, ratio, policy=pol)
                self.assertTrue(np.allclose(t, det[plan]))

    def test_react_takes_cheap_stop_under_sc(self):
        plan = X.Plan('MEDIUM', ((20, 'HARD'),))
        scen = np.zeros(50, dtype=np.int8)
        scen[17:21] = 1                       # SC с 18-го круга, стоп запланирован на 20-м
        cost = np.array([20.0, 10.0, 16.0])
        stops = X.react(plan, scen, 50, self.params, cost, {1: 4, 2: 3})
        self.assertEqual(stops, ((20, 'HARD'),))   # стоп и так под SC — ничего не меняем
        scen = np.zeros(50, dtype=np.int8)
        scen[13:17] = 1                       # SC с 14-го: перенести стоп выгоднее
        stops = X.react(plan, scen, 50, self.params, cost, {1: 4, 2: 3})
        self.assertEqual(stops[0][0], 14)


class PracticeTest(unittest.TestCase):
    FUEL = 0.05

    def practice_laps(self, deg, seed=0):
        """Уикенд «R»: 8 отрезков по 10 кругов + быстрый круг и круг охлаждения в каждом."""
        rng = np.random.default_rng(seed)
        rows = []
        for run in range(8):
            comp = 'MEDIUM' if run % 2 else 'HARD'
            base = 90 + rng.normal(0, 0.5)               # топливо, режим мотора — у каждого свой
            times = [base + deg[comp] * a - self.FUEL * a + rng.normal(0, 0.03) for a in range(10)]
            times = [base - 2.5] + times + [base * 1.3]  # попытка на круг и охлаждение
            for i, t in enumerate(times):
                rows.append({'race': 'R', 'session': 'Practice 2', 'session_key': 1,
                             'driver_number': run, 'driver': f'D{run}', 'stint': 1,
                             'lap_number': i + 1, 'lap_time': t, 'compound': comp,
                             'tyre_age': i, 'is_in_lap': False, 'is_pit_out_lap': False,
                             'neutralised': None, 'yellow': False, 'rainfall': 0})
        return pd.DataFrame(rows)

    def test_long_runs_drop_push_and_cooldown_laps(self):
        runs = F.long_runs(self.practice_laps({'HARD': 0.04, 'MEDIUM': 0.08}), self.FUEL)
        self.assertEqual(runs['run'].nunique(), 8)
        self.assertEqual(len(runs), 80)
        self.assertEqual(runs.groupby('run')['run_lap'].max().tolist(), [9] * 8)

    def test_practice_deg_recovers_wear_net_of_fuel(self):
        runs = F.long_runs(self.practice_laps({'HARD': 0.04, 'MEDIUM': 0.08}), self.FUEL)
        pr = F.practice_deg(runs).set_index('compound')
        self.assertAlmostEqual(pr.loc['HARD', 'deg'], 0.04, delta=0.01)
        self.assertAlmostEqual(pr.loc['MEDIUM', 'deg'], 0.08, delta=0.01)
        self.assertEqual(pr.loc['HARD', 'runs'], 4)

    def test_live_wear_matches_analysis(self):
        from live import practice as LP
        runs = F.long_runs(self.practice_laps({'HARD': 0.04, 'MEDIUM': 0.08}, seed=2), self.FUEL)
        pr = F.practice_deg(runs).set_index('compound')
        live = LP.wear({k: list(zip(g['compound'], g['tyre_age'], g['lap_time_fc']))
                        for k, g in runs.groupby('run')})
        for c in ('HARD', 'MEDIUM'):
            self.assertAlmostEqual(live[c]['deg'], pr.loc[c, 'deg'], places=10)
            self.assertAlmostEqual(live[c]['se'], pr.loc[c, 'se'], places=10)

    def test_combine_uses_only_other_races(self):
        races = [f'R{i}' for i in range(6)]
        true = dict(zip(races, [0.02, 0.05, 0.08, 0.11, 0.14, 0.17]))
        race_deg = pd.DataFrame({'race': races, 'compound': 'MEDIUM',
                                 'deg_s_per_lap': [true[r] for r in races]})
        # практика ровно на 0.03 выше гонки и очень точная → прогноз почти равен гонке
        practice = pd.DataFrame({'race': races, 'compound': 'MEDIUM',
                                 'deg': [true[r] + 0.03 for r in races], 'se': 0.001})
        cb = F.combine(practice, race_deg).set_index('race')
        for r in races:
            self.assertAlmostEqual(cb.loc[r, 'post'], true[r], delta=0.002)
            self.assertAlmostEqual(cb.loc[r, 'prior'],
                                   np.median([true[o] for o in races if o != r]))
        # без практики — медиана остальных гонок
        cb = F.combine(practice[practice.race != 'R0'], race_deg).set_index('race')
        self.assertEqual(cb.loc['R0', 'post'], cb.loc['R0', 'prior'])
        self.assertEqual(cb.loc['R0', 'weight'], 0.0)


class PredictTest(unittest.TestCase):
    def results(self, races=6, noise=0.0, seed=0):
        """Сезон, где финиш = стартовая позиция (+ шум); команда «X» сходит каждую гонку."""
        rng = np.random.default_rng(seed)
        rows = []
        for rd in range(1, races + 1):
            for g in range(1, 11):
                team = 'X' if g == 10 else f'T{(g + 1) // 2}'
                rows.append({'round': rd, 'race': f'R{rd}', 'driver_number': g, 'driver': f'D{g}',
                             'team': team, 'grid': g, 'quali_gap': 0.1 * g,
                             'status': 'dnf' if team == 'X' else 'finished'})
        d = pd.DataFrame(rows)
        d['finish'] = np.where(d['status'] == 'finished', d['grid'] + rng.normal(0, noise, len(d)), np.nan)
        d.loc[d['status'] == 'finished', 'finish'] = (
            d[d['status'] == 'finished'].groupby('round')['finish'].rank())
        return d

    def test_form_uses_only_past_races(self):
        d = P.add_form(self.results())
        self.assertTrue(d.loc[d['round'] == 1, 'form_team'].isna().all())
        r2 = d[(d['round'] == 2) & (d['driver_number'] == 3)].iloc[0]
        self.assertEqual(r2['form_driver'], 3.0)                 # только гонка 1
        self.assertEqual(r2['form_team'], 3.5)                   # T2 = пилоты 3 и 4

    def test_reliability_shrinks_to_season(self):
        d = self.results(races=4)
        p = P.reliability(d, pd.Series(['X', 'T1', 'NEW']), a=10)
        p0 = 0.1
        self.assertAlmostEqual(p[0], (4 + 10 * p0) / (4 + 10))   # 4 схода из 4 стартов
        self.assertAlmostEqual(p[1], (0 + 10 * p0) / (8 + 10))
        self.assertAlmostEqual(p[2], p0)                         # новой команды нет в истории

    def test_grid_model_is_perfect_when_finish_equals_grid(self):
        r = P.rolling_ranks(self.results(), {'grid': 'grid'})
        self.assertTrue((r['rho'] > 0.999).all())
        self.assertTrue(r['winner'].all())
        self.assertEqual(set(r['round']), {4, 5, 6})

    def test_simulate(self):
        sim = P.simulate([1.0, 2.0, 3.0], sigma=1e-6, p_out=[0.0, 0.0, 1.0], n=200)
        self.assertEqual(list(sim['win']), [1.0, 0.0, 0.0])
        self.assertEqual(list(sim['podium']), [1.0, 1.0, 0.0])   # сошедший не на подиуме
        sim = P.simulate([0.0] * 4, sigma=1.0, p_out=[0.0] * 4, n=20000, seed=1)
        self.assertTrue(np.allclose(sim['win'], 0.25, atol=0.02))


if __name__ == '__main__':
    unittest.main()
