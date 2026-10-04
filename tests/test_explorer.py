"""Session explorer (f1_data.py, plots.py): OpenF1 tables → Session → figures.
No network: synthetic tables with a known answer.

python -m unittest tests.test_explorer
"""

import unittest

import matplotlib
matplotlib.use('Agg')

import f1_data
import plots

T0 = '2024-11-24T06:00:{:02d}+00:00'.format


def raw_race():
    """3 laps, two teammates: NOR leads on the road, PIA stops on lap 2 and
    finishes ahead after a penalty for NOR (final classification update)."""
    laps = []
    for num, offs in ((4, 0), (81, 1)):
        for lap in (1, 2, 3):
            laps.append({'driver_number': num, 'lap_number': lap,
                         'date_start': T0((lap - 1) * 10 + offs),
                         'lap_duration': 9.5 if num == 4 else 9.6,
                         'is_pit_out_lap': num == 81 and lap == 3})
    return {
        'drivers': [{'driver_number': 4, 'name_acronym': 'NOR', 'team_name': 'McLaren', 'team_colour': 'FF8000'},
                    {'driver_number': 81, 'name_acronym': 'PIA', 'team_name': 'McLaren', 'team_colour': 'FF8000'}],
        'laps': laps,
        'stints': [{'driver_number': 4, 'stint_number': 1, 'lap_start': 1, 'lap_end': 3,
                    'compound': 'MEDIUM', 'tyre_age_at_start': 2},
                   {'driver_number': 81, 'stint_number': 1, 'lap_start': 1, 'lap_end': 2,
                    'compound': 'MEDIUM', 'tyre_age_at_start': 0},
                   {'driver_number': 81, 'stint_number': 2, 'lap_start': 3, 'lap_end': 3,
                    'compound': 'HARD', 'tyre_age_at_start': 0}],
        'pit': [{'driver_number': 81, 'lap_number': 2}],
        'position': [{'date': T0(0), 'driver_number': 4, 'position': 1},
                     {'date': T0(0), 'driver_number': 81, 'position': 2},
                     {'date': T0(50), 'driver_number': 81, 'position': 1},   # after the flag
                     {'date': T0(50), 'driver_number': 4, 'position': 2}],
    }


class ExplorerTest(unittest.TestCase):
    def setUp(self):
        self.s = f1_data.build_session(raw_race(), key=1, year=2024,
                                       event='Las Vegas Grand Prix', name='Race')

    def test_laps(self):
        laps = self.s.laps.set_index(['Driver', 'LapNumber'])
        self.assertEqual(len(laps), 6)
        self.assertEqual(laps.loc[('NOR', 3), 'TyreLife'], 5)            # used set: 2 + 3
        self.assertEqual(laps.loc[('PIA', 3), 'Compound'], 'HARD')
        self.assertTrue(laps.loc[('PIA', 2), 'PitInLap'])
        self.assertTrue(laps.loc[('PIA', 3), 'PitOutLap'])
        self.assertEqual(laps.loc[('NOR', 3), 'Position'], 1)            # on the road

    def test_classification_and_order(self):
        self.assertEqual(self.s.classification, {'PIA': 1, 'NOR': 2})   # with the penalty
        self.assertEqual(f1_data.get_drivers(self.s), ['PIA', 'NOR'])

    def test_quick_and_fastest_laps(self):
        self.assertEqual(list(self.s.quick_laps('PIA')['LapNumber']), [1])   # no in/out laps
        self.assertEqual(self.s.fastest_lap('NOR')['LapTime'], 9.5)

    def test_teammate_style(self):
        self.assertEqual(self.s.style('NOR')['linestyle'], '-')
        self.assertEqual(self.s.style('PIA')['linestyle'], '--')
        self.assertEqual(self.s.style('PIA')['color'], '#FF8000')

    def test_plots(self):
        for label, fn in plots.PLOTS.items():
            if fn is plots.plot_telemetry:
                continue                                               # needs car data
            fig = fn(self.s, ['NOR', 'PIA'])
            self.assertTrue(fig.axes, label)
        # no drivers → a placeholder figure instead of an exception
        self.assertEqual(len(plots.plot_lap_times(self.s, []).axes), 1)


if __name__ == '__main__':
    unittest.main()
