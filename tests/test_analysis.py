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


def synthetic_race(deg=None, fuel=0.04, n_laps=50, drivers=12, noise=0.15, seed=0):
    """Круги с известной деградацией и эффектом топлива."""
    deg = deg or {'MEDIUM': 0.08, 'HARD': 0.04}
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(drivers):
        pace = 90 + rng.normal(0, 0.5)
        stop = int(rng.integers(18, 30))
        first, second = ('MEDIUM', 'HARD') if d % 2 else ('HARD', 'MEDIUM')
        for lap in range(2, n_laps + 1):
            comp, age = (first, lap - 1) if lap <= stop else (second, lap - stop)
            t = pace + deg[comp] * age + fuel * (n_laps - lap) + rng.normal(0, noise)
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


if __name__ == '__main__':
    unittest.main()
