"""python -m unittest discover tests"""

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone

from live.decode import decode_z, encode_z
from live.openf1 import CarData, OpenF1Source, compact_locations
from live.player import Player, Timeline
from live.server import LiveServer
from live.sim import SimSource
from live.sources import parse_json_stream
from live.state import SessionState, merge
from live import strategy as LS
from live.history import lap_seconds, parse_gap


class MergeTest(unittest.TestCase):
    def test_nested_dict(self):
        base = {'Lines': {'1': {'Position': '1', 'GapToLeader': ''}}}
        merge(base, {'Lines': {'1': {'GapToLeader': '+1.2'}, '44': {'Position': '2'}}})
        self.assertEqual(base['Lines']['1'], {'Position': '1', 'GapToLeader': '+1.2'})
        self.assertEqual(base['Lines']['44'], {'Position': '2'})

    def test_list_patched_by_index_dict(self):
        base = {'Sectors': [{'Value': '28.1'}, {'Value': ''}, {'Value': ''}]}
        merge(base, {'Sectors': {'1': {'Value': '30.5', 'PersonalFastest': True}}})
        self.assertEqual(base['Sectors'][0], {'Value': '28.1'})
        self.assertEqual(base['Sectors'][1], {'Value': '30.5', 'PersonalFastest': True})

    def test_list_grows(self):
        base = {'Messages': [{'Message': 'a'}]}
        merge(base, {'Messages': {'2': {'Message': 'c'}}})
        self.assertEqual(len(base['Messages']), 3)
        self.assertEqual(base['Messages'][2]['Message'], 'c')

    def test_scalar_and_list_replace(self):
        self.assertEqual(merge({'a': 1}, {'a': [1, 2]}), {'a': [1, 2]})
        self.assertEqual(merge('x', 5), 5)

    def test_deleted(self):
        base = {'a': {'1': 1, '2': 2}}
        merge(base, {'a': {'_deleted': ['1']}})
        self.assertEqual(base, {'a': {'2': 2}})


class DecodeTest(unittest.TestCase):
    def test_roundtrip(self):
        obj = {'Position': [{'Timestamp': 'x', 'Entries': {'1': {'X': 1, 'Y': 2}}}]}
        self.assertEqual(decode_z(encode_z(obj)), obj)

    def test_json_stream(self):
        raw = '﻿00:00:01.500{"Status":"Started"}\r\n00:01:00.000"abc"\r\n'.encode()
        self.assertEqual(parse_json_stream(raw), [(1.5, {'Status': 'Started'}), (60.0, 'abc')])


class StateTest(unittest.TestCase):
    def test_view_and_position(self):
        st = SessionState()
        st.apply_snapshot({
            'DriverList': {'1': {'Tla': 'VER', 'TeamColour': '3671C6'}},
            'TimingData': {'Lines': {'1': {'Position': '1', 'Sectors': [{}, {}, {}]}}},
            'TimingAppData': {'Lines': {'1': {'Stints': [{'Compound': 'SOFT', 'TotalLaps': 3}]}}},
        })
        st.apply('TimingData', {'Lines': {'1': {'Sectors': {'2': {'Value': '25.0',
                                                                  'OverallFastest': True}}}}})
        st.apply('Position.z', encode_z({'Position': [{
            'Timestamp': '2024-09-01T13:00:00.1234567Z',
            'Entries': {'1': {'Status': 'OnTrack', 'X': 10, 'Y': 20}}}]}))
        v = st.view()
        row = v['rows'][0]
        self.assertEqual((row['tla'], row['tyre'], row['tyreAge']), ('VER', 'SOFT', 3))
        self.assertEqual(row['sectors'][2], {'v': '25.0', 'ob': True, 'pb': False})
        self.assertEqual((v['cars']['1']['x'], v['cars']['1']['y']), (10, 20))
        self.assertEqual(v['feedTime'][:19], '2024-09-01T13:00:00')


class SafetyCarTest(unittest.TestCase):
    def test_sc_in_this_lap(self):
        st = SessionState()
        st.apply('TrackStatus', {'Status': '4', 'Message': 'SCDeployed'})
        msgs = ['SAFETY CAR DEPLOYED', 'LAPPED CARS MAY NOW OVERTAKE THE SAFETY CAR: 81',
                'SAFETY CAR IN THIS LAP']
        for i, text in enumerate(msgs):
            st.apply('RaceControlMessages', {'Messages': {str(i): {'Message': text}}})
            self.assertEqual(st.view()['track']['flag'], 'sc-end' if i == 2 else 'sc')
        st.apply('RaceControlMessages', {'Messages': {'3': {'Message': 'SAFETY CAR DEPLOYED'}}})
        self.assertEqual(st.view()['track']['flag'], 'sc')       # повторный выезд
        st.apply('TrackStatus', {'Status': '1'})
        self.assertEqual(st.view()['track']['flag'], 'green')


class CarDataTest(unittest.TestCase):
    def test_window(self):
        rows = [{'date': f'2026-10-03T08:00:0{i}+00:00', 'speed': 100 + i, 'rpm': 10000,
                 'n_gear': 5, 'throttle': 100, 'brake': 0, 'drs': None} for i in range(5)]
        cd = CarData(rows)
        t0 = cd.t[0]
        w = cd.window(t0, t0 + 2000)                   # (t0, t0+2с] → 2 сэмпла
        self.assertEqual([f[1] for f in w], [101, 102])
        self.assertEqual(w[0][6], -1)                  # DRS нет в данных
        self.assertEqual(cd.window(t0 + 9000, t0 + 9999), [])


def feed_lap(st, num, lap, t, pos, gap, comp='MEDIUM', age=None, pit_out=None):
    """Патчи, которые приходят при завершении круга (как в OpenF1-ленте)."""
    st.apply('TimingAppData', {'Lines': {num: {'Stints': {'0': {
        'Compound': comp, 'TotalLaps': age if age is not None else lap}}}}})
    line = {'NumberOfLaps': lap, 'Position': str(pos), 'GapToLeader': gap,
            'LastLapTime': {'Value': f'{int(t // 60)}:{t % 60:06.3f}'}}
    st.apply('TimingData', {'Lines': {num: line}})
    if pit_out is not None:                     # начало следующего круга
        st.apply('TimingData', {'Lines': {num: {'PitOut': pit_out}}})


class HistoryTest(unittest.TestCase):
    def test_parsers(self):
        self.assertAlmostEqual(lap_seconds('1:35.130'), 95.13)
        self.assertIsNone(lap_seconds(''))
        self.assertEqual(parse_gap('+12.345', 3), (12.345, 0))
        self.assertEqual(parse_gap('1 L', 15), (None, 1))
        self.assertEqual(parse_gap('LAP 5', 1), (0.0, 0))

    def test_laps_pits_and_neutralisation(self):
        st = SessionState()
        st.apply('TrackStatus', {'Status': '1'})
        feed_lap(st, '1', 1, 95.0, 1, '')
        feed_lap(st, '1', 2, 92.0, 1, '', pit_out=False)
        feed_lap(st, '1', 3, 112.0, 2, '+3.0', pit_out=True)      # 4-й круг — выезд
        feed_lap(st, '1', 4, 110.0, 4, '+15.1', comp='HARD', age=1, pit_out=False)
        st.apply('TrackStatus', {'Status': '4'})
        feed_lap(st, '1', 5, 130.0, 4, '+14.0', comp='HARD', age=2)
        st.apply('TrackStatus', {'Status': '1'})
        feed_lap(st, '1', 5, 130.0, 4, '+14.0')                   # повтор — не записывается
        rows = {r['lap']: r for r in st.history.laps['1']}
        self.assertEqual(sorted(rows), [1, 2, 3, 4, 5])
        self.assertAlmostEqual(rows[2]['t'], 92.0)
        self.assertEqual((rows[3]['in'], rows[4]['out']), (True, True))
        self.assertFalse(rows[2]['in'] or rows[2]['out'])
        self.assertEqual((rows[4]['comp'], rows[4]['age'], rows[4]['gap']), ('HARD', 1, 15.1))
        self.assertTrue(rows[5]['dirty'])
        self.assertFalse(rows[4]['dirty'])


class LiveStrategyTest(unittest.TestCase):
    def synthetic(self, deg=0.08, drivers=6, laps=30):
        st = SessionState()
        st.apply('TrackStatus', {'Status': '1'})
        st.apply('LapCount', {'CurrentLap': laps, 'TotalLaps': 50})
        import random
        rnd = random.Random(1)
        for d in range(drivers):
            for lap in range(1, laps + 1):
                t = 90 + d * 0.3 + deg * lap - LS.FUEL * lap + rnd.gauss(0, 0.05)
                feed_lap(st, str(d), lap, t, d + 1, f'+{d * 2.0}', comp='MEDIUM', age=lap)
        # Второй состав — у пары пилотов, чтобы было из чего выбирать
        for d in range(2):
            for lap in range(laps + 1, laps + 21):
                t = 90 + 0.04 * (lap - laps) - LS.FUEL * lap + rnd.gauss(0, 0.05)
                st.apply('TimingAppData', {'Lines': {str(d): {'Stints': {'1': {
                    'Compound': 'HARD', 'TotalLaps': lap - laps}}}}})
                st.apply('TimingData', {'Lines': {str(d): {
                    'NumberOfLaps': lap, 'Position': str(d + 1), 'GapToLeader': '',
                    'LastLapTime': {'Value': f'1:{t - 60:06.3f}'}}}})
        return st

    def test_degradation_recovered(self):
        st = self.synthetic()
        deg = LS.degradation(LS.clean_laps(st.history))
        self.assertAlmostEqual(deg['MEDIUM']['deg'], 0.08, delta=0.005)
        self.assertAlmostEqual(deg['HARD']['deg'], 0.04, delta=0.01)

    def test_pit_window_analytic(self):
        deg = {'MEDIUM': {'deg': 0.10, 'n': 100}, 'HARD': {'deg': 0.05, 'n': 100}}
        # Новые MEDIUM, 50 кругов до финиша, потеря 20 с → оптимум через 17 кругов
        w = LS.pit_window(deg, 'MEDIUM', 0, 50, 20.0)
        self.assertEqual(w[0]['compound'], 'HARD')
        self.assertEqual(w[0]['in_laps'], 17)
        self.assertGreater(w[0]['gain'], 0)
        # Мало кругов до финиша — останавливаться невыгодно
        self.assertLess(LS.pit_window(deg, 'MEDIUM', 5, 8, 20.0)[0]['gain'], 0)

    def test_pit_window_wet(self):
        deg = {'INTERMEDIATE': {'deg': 0.15, 'n': 100}, 'WET': {'deg': 0.05, 'n': 100},
               'HARD': {'deg': 0.01, 'n': 100}}
        w = LS.pit_window(deg, 'INTERMEDIATE', 10, 40, 20.0)
        # Дождевые → дождевые, включая свежий комплект; на сухие не советуем
        self.assertEqual({o['compound'] for o in w}, {'INTERMEDIATE', 'WET'})
        # Сухие → только другой сухой состав
        self.assertEqual([o['compound'] for o in LS.pit_window(deg, 'HARD', 10, 40, 20.0)], [])

    def test_wet_laps_modelled(self):
        st = SessionState()
        st.apply('TrackStatus', {'Status': '1'})
        st.apply('LapCount', {'CurrentLap': 20, 'TotalLaps': 50})
        for d in range(4):
            for lap in range(1, 21):
                # трасса подсыхает: круги быстреют на 0.1 с
                feed_lap(st, str(d), lap, 100 + d * 0.3 - 0.1 * lap, d + 1, f'+{d * 2.0}',
                         comp='INTERMEDIATE', age=lap)
        v = LS.strategy_view(st)
        self.assertTrue(v['deg']['INTERMEDIATE']['improving'])
        self.assertTrue(v['windows']['0']['wet'])

    def fl_state(self, year=2024, name='Race', gap_behind='+30.000', pos=1, at=45):
        st = SessionState()
        st.apply('SessionInfo', {'Type': 'Race', 'Name': name, 'StartDate': f'{year}-11-03T17:00:00'})
        st.apply('DriverList', {'1': {'Tla': 'NOR'}, '2': {'Tla': 'PIA'}})
        st.apply('TrackStatus', {'Status': '1'})
        st.apply('LapCount', {'CurrentLap': at, 'TotalLaps': 50})
        other = 2 if pos == 1 else pos - 1
        for lap in range(1, at + 1):
            feed_lap(st, '1', lap, 90.0, pos, '', comp='HARD', age=lap)
            feed_lap(st, '2', lap, 91.0, pos + 1 if pos == 1 else other, '', comp='HARD', age=lap)
        st.apply('TimingData', {'Lines': {
            '1': {'BestLapTime': {'Value': '1:29.500'}},
            '2': {'BestLapTime': {'Value': '1:29.000'},
                  'IntervalToPositionAhead': {'Value': gap_behind if pos == 1 else '+1.0'}}}})
        return st

    def test_fastest_lap_stop(self):
        fl = LS.strategy_view(self.fl_state())['windows']['1']['fastestLap']
        self.assertEqual(fl['status'], 'free')
        self.assertEqual(fl['window'], [46, 48])                  # заезд не позже total−2
        self.assertEqual((fl['holder'], fl['record']), ('PIA', 89.0))
        self.assertAlmostEqual(fl['pace'], 90.0)
        view = lambda **kw: LS.strategy_view(self.fl_state(**kw))['windows']['1']['fastestLap']
        loss = fl['loss']
        self.assertEqual(view(gap_behind=f'+{loss - 1:.3f}')['status'], 'tight')
        self.assertEqual(view(gap_behind=f'+{loss - 5:.3f}')['status'], 'no')
        self.assertEqual(view(gap_behind='1 L')['status'], 'free')
        self.assertIsNone(view(year=2025))                         # очко отменили
        self.assertIsNone(view(name='Sprint'))
        self.assertIsNone(view(at=30))                             # рано
        self.assertIsNone(view(at=48))                             # поздно: не успеть
        self.assertIsNone(view(pos=11))                            # вне очков

    def test_view(self):
        v = LS.strategy_view(self.synthetic())
        self.assertEqual(v['totalLaps'], 50)
        self.assertEqual(len(v['laps']), 6)
        self.assertTrue(v['deg']['MEDIUM']['significant'])
        self.assertIn('0', v['windows'])
        json.dumps(v)                                   # сериализуется


class SimStrategyTest(unittest.TestCase):
    """Сим-гонка: деградация 0.06 с/круг, топлива нет → модель должна найти ~0.06."""

    def test_sim(self):
        async def run():
            st = SessionState()
            async for ev in SimSource(speed=400, seed=3).events():
                if ev['kind'] == 'snapshot':
                    st.apply_snapshot(ev['data'])
                else:
                    st.apply(ev['topic'], ev['data'], ev['ts'])
                if (st.data.get('LapCount') or {}).get('CurrentLap', 0) >= 12:
                    return st
        st = asyncio.run(asyncio.wait_for(run(), 120))
        deg = LS.degradation(LS.clean_laps(st.history), fuel=0.0)
        best = max(deg.values(), key=lambda d: d['n'])
        self.assertAlmostEqual(best['deg'], 0.06, delta=0.02)


class TokenTest(unittest.TestCase):
    def test_access(self):
        from websockets.datastructures import Headers
        from websockets.http11 import Request
        srv = LiveServer(source=None, token='s3cret')
        req = lambda path, cookie=None: Request(
            path, Headers({'Cookie': cookie} if cookie else {}))
        self.assertEqual(srv.http(None, req('/')).status_code, 401)
        self.assertEqual(srv.http(None, req('/ws')).status_code, 401)
        self.assertEqual(srv.http(None, req('/?token=wrong')).status_code, 401)
        r = srv.http(None, req('/?token=s3cret'))
        self.assertEqual(r.status_code, 200)
        self.assertIn('f1token=s3cret', r.headers['Set-Cookie'])
        self.assertIsNone(srv.http(None, req('/ws', 'f1token=s3cret')))   # → handshake
        self.assertEqual(srv.http(None, req('/app.js', 'f1token=s3cret')).status_code, 200)
        self.assertIsNone(LiveServer(source=None).http(None, req('/ws')))  # без токена — открыто


class OvertakeTest(unittest.TestCase):
    def test_modes(self):
        st = SessionState()
        self.assertIsNone(st.view()['overtake'])
        for i, (text, want) in enumerate([
                ('OVERTAKE ENABLED', {'name': 'Overtake', 'on': True}),
                ('LAPPED CARS MAY NOW OVERTAKE THE SAFETY CAR: 77', {'name': 'Overtake', 'on': True}),
                ('OVERTAKE DISABLED', {'name': 'Overtake', 'on': False}),
                ('DRS ENABLED', {'name': 'DRS', 'on': True})]):
            st.apply('RaceControlMessages', {'Messages': {str(i): {'Message': text}}})
            self.assertEqual(st.view()['overtake'], want)


class SimPipelineTest(unittest.TestCase):
    """Симуляция ×200: проходят круги, есть порядок, отрывы и контур трассы."""

    def test_sim_race(self):
        async def run():
            st = SessionState()
            n = 0
            async for ev in SimSource(speed=200, seed=1).events():
                if ev['kind'] == 'snapshot':
                    st.apply_snapshot(ev['data'])
                else:
                    st.apply(ev['topic'], ev['data'], ev['ts'])
                n += 1
                if (st.data.get('LapCount') or {}).get('CurrentLap', 0) >= 4:
                    return st, n
        st, n = asyncio.run(asyncio.wait_for(run(), 60))
        v = st.view()
        self.assertEqual(len(v['rows']), 20)
        self.assertEqual([r['pos'] for r in v['rows']], list(range(1, 21)))
        self.assertTrue(all(r['last']['v'] for r in v['rows']))
        self.assertTrue(v['rows'][1]['gap'].startswith('+'))
        self.assertEqual(len(v['cars']), 20)
        self.assertTrue(st.outline.done, 'контур должен замкнуться за круг')
        self.assertGreater(len(st.outline.points), 100)
        self.assertTrue(v['rcm'])
        self.assertTrue(v['clock'])


class OpenF1Test(unittest.TestCase):
    """Таблицы OpenF1 → лента событий F1 → reducer."""

    def test_timeline(self):
        T = '2026-10-03T08:00:{:02d}+00:00'.format
        d = {
            'session': {'session_key': 1, 'session_type': 'Qualifying',
                        'session_name': 'Qualifying', 'date_start': T(0),
                        'date_end': '2026-10-03T09:00:00+00:00', 'circuit_key': 12,
                        'circuit_short_name': 'KL', 'location': 'KL', 'year': 2026},
            'meeting': {'meeting_name': 'Test GP'},
            'drivers': [{'driver_number': 1, 'name_acronym': 'NOR', 'team_colour': 'F47600'},
                        {'driver_number': 63, 'name_acronym': 'RUS', 'team_colour': '27F4D2'}],
            'laps': [{'driver_number': 1, 'lap_number': 1, 'date_start': T(1),
                      'duration_sector_1': 10.0, 'duration_sector_2': 20.0,
                      'duration_sector_3': 15.5, 'lap_duration': 45.5},
                     {'driver_number': 63, 'lap_number': 1, 'date_start': T(2),
                      'duration_sector_1': 10.2, 'duration_sector_2': 19.0,
                      'duration_sector_3': 16.0, 'lap_duration': 45.2}],
            'stints': [{'driver_number': 1, 'stint_number': 1, 'lap_start': 1,
                        'lap_end': 3, 'compound': 'SOFT', 'tyre_age_at_start': 0}],
            'pit': [], 'intervals': [], 'weather': [],
            'position': [{'date': T(50), 'driver_number': 63, 'position': 1},
                         {'date': T(50), 'driver_number': 1, 'position': 2}],
            'race_control': [{'date': '2026-10-03T08:00:00+00:00', 'category': 'Flag',
                              'flag': 'RED', 'scope': 'Track', 'message': 'RED FLAG'}],
            'location': {'1': compact_locations(
                [{'date': T(5), 'driver_number': 1, 'x': 100, 'y': 200, 'z': 0}])},
        }
        st = SessionState()
        for t, topic, data in OpenF1Source.timeline(d):
            st.apply(topic, data, t.isoformat())
        v = st.view()
        self.assertEqual([r['tla'] for r in v['rows']], ['RUS', 'NOR'])
        rus, nor = v['rows']
        self.assertEqual((rus['best'], rus['gap']), ('0:45.200', ''))
        self.assertEqual((nor['gap'], nor['interval']), ('+0.300', '+0.300'))
        self.assertTrue(rus['last']['ob'])
        self.assertTrue(rus['sectors'][1]['ob'] and not rus['sectors'][0]['ob'])
        self.assertEqual((nor['tyre'], nor['tyreAge']), ('SOFT', 1))
        self.assertEqual(v['track']['flag'], 'red')
        self.assertTrue(v['rcm'][0]['utc'].endswith('Z'))
        self.assertEqual(v['cars']['1']['x'], 100)
        self.assertEqual(v['session']['meeting'], 'Test GP')

    def test_pit_exit_at_date(self):
        # Стоп под красным флагом: `date` — выезд из пит-лейна, lane_duration
        # уже включён; круг после рестарта не должен считаться кругом выезда
        T = lambda s: (datetime(2026, 10, 3, 8, tzinfo=timezone.utc)
                       + timedelta(seconds=s)).isoformat()
        laps = [{'driver_number': 1, 'lap_number': n, 'date_start': T(s), 'lap_duration': dur}
                for n, s, dur in ((1, 0, 90), (2, 90, 1300), (3, 1390, 90), (4, 1480, 90))]
        d = {'session': {'session_key': 1, 'session_type': 'Race', 'session_name': 'Race',
                         'date_start': T(0), 'date_end': T(3600), 'circuit_key': 1,
                         'circuit_short_name': 'X', 'location': 'X', 'year': 2026},
             'meeting': {'meeting_name': 'Test GP'},
             'drivers': [{'driver_number': 1, 'name_acronym': 'NOR', 'team_colour': 'F47600'}],
             'laps': laps, 'stints': [], 'intervals': [], 'weather': [], 'position': [],
             'race_control': [], 'location': {},
             'pit': [{'driver_number': 1, 'lap_number': 1, 'date': T(1290),
                      'lane_duration': 1200.0}]}
        inpit = [(t, data['Lines']['1']['InPit']) for t, topic, data in OpenF1Source.timeline(d)
                 if topic == 'TimingData' and 'InPit' in data.get('Lines', {}).get('1', {})]
        self.assertEqual([(int((t - datetime.fromisoformat(T(0))).total_seconds()), v)
                          for t, v in inpit], [(90, True), (1290, False)])


class OpenF1LockedTest(unittest.TestCase):
    """Во время live-сессии OpenF1 отвечает 401 на всё — работаем с диска."""

    def setUp(self):
        import os
        import tempfile
        import live.openf1 as O
        self.O, self.cwd = O, os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)
        O._catalog.clear()
        self.real_get, self.calls = O.http_get, []
        self.locked = False
        sess = {'session_key': 7, 'meeting_key': 1, 'session_name': 'Race',
                'date_start': '2024-11-03T15:00:00+00:00', 'date_end': '2024-11-03T17:00:00+00:00',
                'location': 'Sao Paulo'}

        def fake(url, timeout=30):
            import io
            import urllib.error
            self.calls.append(url)
            if self.locked:
                raise urllib.error.HTTPError(url, 401, 'Unauthorized', {}, io.BytesIO(b''))
            body = [sess] if '/sessions' in url else [{'meeting_key': 1, 'meeting_name': 'Brazil GP'}]
            return json.dumps(body).encode()
        O.http_get = fake
        from unittest import mock
        self.no_sleep = mock.patch.object(O.time, 'sleep', lambda s: None)
        self.no_sleep.start()

    def tearDown(self):
        import os
        self.O.http_get = self.real_get
        self.no_sleep.stop()
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_catalog_and_session_from_disk(self):
        O = self.O
        self.assertEqual(O.catalog(2024)[0]['meeting'], 'Brazil GP')     # сохраняет на диск
        O._catalog.clear()
        self.locked = True
        self.assertEqual(O.catalog(2024)[0]['key'], 7)                   # с диска
        n = len(self.calls)
        self.assertEqual(O.OpenF1Source(7).resolve()['session_name'], 'Race')
        self.assertEqual(len(self.calls), n)                             # без сети
        with self.assertRaises(O.OpenF1Locked):
            O.OpenF1Source('latest').resolve()


class PlayerTest(unittest.TestCase):
    """Перемотка назад/вперёд даёт то же состояние, что и проигрыш подряд."""

    def make(self):
        msgs = [(float(i), {'kind': 'msg', 'topic': 'TimingData', 'data': {'Lines': {
            '1': {'NumberOfLaps': i, 'Sectors': {str(i % 3): {'Value': f'{i}.0'}}}}}})
            for i in range(100)]
        server = LiveServer(source=None)
        server.player.set_timeline(Timeline(msgs=msgs, start=40.0), speed=1)
        return server

    def laps(self, server):
        return server.state.data['TimingData']['Lines']['1']['NumberOfLaps']

    def test_seek(self):
        server = self.make()
        p = server.player
        self.assertEqual(self.laps(server), 10)        # старт сессии − 30 с
        p.seek_session(30)                              # вперёд → 70
        self.assertEqual(self.laps(server), 70)
        ahead = server.state.data
        p.seek_session(-35)                             # назад → 5
        self.assertEqual(self.laps(server), 5)
        p.seek_session(30)
        self.assertEqual(server.state.data, ahead)
        p.seek(10_000)                                  # за конец — упирается
        self.assertEqual(self.laps(server), 99)
        st = p.status()
        self.assertEqual((st['t'], st['min'], st['max']), (59.0, -40.0, 59.0))


if __name__ == '__main__':
    unittest.main()
