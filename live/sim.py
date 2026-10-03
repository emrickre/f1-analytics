"""Синтетическая гонка в формате фида F1 — чтобы гонять весь конвейер без сети.

Отдаёт те же топики и патчи, что и настоящий SignalR (включая сжатый
Position.z), поэтому через неё проходят decoder, reducer, сервер и фронт.
"""

import asyncio
import math
import random
from datetime import datetime, timedelta, timezone

from .decode import encode_z

GRID = [  # num, tla, имя, команда, цвет
    ('1', 'VER', 'Max Verstappen', 'Red Bull Racing', '3671C6'),
    ('11', 'PER', 'Sergio Perez', 'Red Bull Racing', '3671C6'),
    ('16', 'LEC', 'Charles Leclerc', 'Ferrari', 'E8002D'),
    ('55', 'SAI', 'Carlos Sainz', 'Ferrari', 'E8002D'),
    ('4', 'NOR', 'Lando Norris', 'McLaren', 'FF8000'),
    ('81', 'PIA', 'Oscar Piastri', 'McLaren', 'FF8000'),
    ('44', 'HAM', 'Lewis Hamilton', 'Mercedes', '27F4D2'),
    ('63', 'RUS', 'George Russell', 'Mercedes', '27F4D2'),
    ('14', 'ALO', 'Fernando Alonso', 'Aston Martin', '229971'),
    ('18', 'STR', 'Lance Stroll', 'Aston Martin', '229971'),
    ('10', 'GAS', 'Pierre Gasly', 'Alpine', 'FF87BC'),
    ('31', 'OCO', 'Esteban Ocon', 'Alpine', 'FF87BC'),
    ('23', 'ALB', 'Alexander Albon', 'Williams', '64C4FF'),
    ('2', 'SAR', 'Logan Sargeant', 'Williams', '64C4FF'),
    ('22', 'TSU', 'Yuki Tsunoda', 'RB', '6692FF'),
    ('3', 'RIC', 'Daniel Ricciardo', 'RB', '6692FF'),
    ('77', 'BOT', 'Valtteri Bottas', 'Kick Sauber', '52E252'),
    ('24', 'ZHO', 'Zhou Guanyu', 'Kick Sauber', '52E252'),
    ('20', 'MAG', 'Kevin Magnussen', 'Haas F1 Team', 'B6BABD'),
    ('27', 'HUL', 'Nico Hulkenberg', 'Haas F1 Team', 'B6BABD'),
]

TICK = 0.25          # с времени фида на шаг симуляции
TOTAL_LAPS = 20
SECTOR_SPEED = (1.12, 0.92, 0.99)   # медленный/быстрый сектор


def fmt_lap(t):
    m, s = divmod(t, 60)
    return f'{int(m)}:{s:06.3f}'


def fmt_gap(t):
    return f'+{t:.3f}'


class Track:
    """Замкнутая кривая ~5 км в дециметрах (как координаты F1)."""

    def __init__(self, seed):
        rnd = random.Random(seed)
        harm = [(k, rnd.uniform(0.04, 0.16) / k ** 0.6, rnd.uniform(0, 6.28))
                for k in (2, 3, 4, 5)]
        pts = []
        for i in range(1200):
            th = 2 * math.pi * i / 1200
            r = 8000 * (1 + sum(a * math.cos(k * th + p) for k, a, p in harm))
            pts.append((r * math.cos(th) * 1.3, r * math.sin(th)))
        self.pts = pts
        self.cum = [0.0]
        for a, b in zip(pts, pts[1:] + pts[:1]):
            self.cum.append(self.cum[-1] + math.dist(a, b))
        self.length = self.cum[-1]

    def at(self, s):
        s %= self.length
        lo, hi = 0, len(self.pts)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.cum[mid] <= s:
                lo = mid
            else:
                hi = mid
        a = self.pts[lo]
        b = self.pts[(lo + 1) % len(self.pts)]
        seg = self.cum[lo + 1] - self.cum[lo]
        f = (s - self.cum[lo]) / seg if seg else 0
        return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f


class Car:
    def __init__(self, idx, row, rnd):
        self.num, self.tla, self.name, self.team, self.color = row
        self.pace = 90.0 + idx * 0.12 + rnd.uniform(-0.3, 0.3)
        self.dist = -idx * 80.0          # сетка: по 8 м
        self.lap = 0
        self.sector = 0
        self.t_sector = 0.0
        self.t_lap = 0.0
        self.sector_times = [None, None, None]
        self.best = None
        self.best_sectors = [None, None, None]
        self.lap_time = self.pace
        self.pit_lap = rnd.randint(6, 14)
        self.pit_left = 0.0
        self.pits = 0
        self.tyre_age = 0
        self.compound = rnd.choice(['MEDIUM', 'MEDIUM', 'SOFT'])
        self.done = False


class SimSource:
    def __init__(self, speed=1.0, seed=None):
        self.speed = speed
        self.seed = seed if seed is not None else random.randrange(1 << 30)

    async def events(self):
        while True:
            async for ev in self._race(self.seed):
                yield ev
            self.seed += 1
            await asyncio.sleep(10 / self.speed)

    async def _race(self, seed):
        rnd = random.Random(seed)
        track = Track(seed)
        L = track.length
        cars = [Car(i, row, rnd) for i, row in enumerate(GRID)]
        clock = datetime.now(timezone.utc)
        race_secs = TOTAL_LAPS * 95

        def ts():
            return clock.isoformat().replace('+00:00', 'Z')

        def msg(topic, data):
            return {'kind': 'msg', 'topic': topic, 'data': data, 'ts': ts()}

        rcm = []

        def race_control(text, cat='Other', flag=None):
            rcm.append({'Utc': ts(), 'Category': cat, 'Message': text,
                        **({'Flag': flag} if flag else {})})
            return msg('RaceControlMessages', {'Messages': {str(len(rcm) - 1): rcm[-1]}})

        yield {'kind': 'snapshot', 'data': {
            'SessionInfo': {'Meeting': {'Name': 'Simulated Grand Prix',
                                        'Circuit': {'Key': -1, 'ShortName': 'Simulator'}},
                            'Name': 'Race', 'Type': 'Race'},
            'DriverList': {c.num: {'RacingNumber': c.num, 'Tla': c.tla, 'FullName': c.name,
                                   'TeamName': c.team, 'TeamColour': c.color}
                           for c in cars},
            'TimingData': {'Lines': {c.num: {
                'Position': str(i + 1), 'Line': i + 1, 'GapToLeader': '',
                'IntervalToPositionAhead': {'Value': ''}, 'NumberOfLaps': 0,
                'NumberOfPitStops': 0, 'InPit': False, 'PitOut': False,
                'Retired': False, 'Stopped': False,
                'Sectors': [{'Value': ''}, {'Value': ''}, {'Value': ''}],
                'LastLapTime': {'Value': ''}, 'BestLapTime': {'Value': ''}}
                for i, c in enumerate(cars)}},
            'TimingAppData': {'Lines': {c.num: {'Stints': [
                {'Compound': c.compound, 'New': 'true', 'TotalLaps': 0}]} for c in cars}},
            'TrackStatus': {'Status': '1', 'Message': 'AllClear'},
            'SessionStatus': {'Status': 'Started'},
            'LapCount': {'CurrentLap': 1, 'TotalLaps': TOTAL_LAPS},
            'ExtrapolatedClock': {'Utc': ts(), 'Remaining': f'0:{race_secs // 60:02d}:00',
                                  'Extrapolating': True},
            'WeatherData': {'AirTemp': '24.1', 'TrackTemp': '38.6', 'Rainfall': '0'},
            'RaceControlMessages': {'Messages': []},
        }}
        yield race_control('GREEN LIGHT - PIT EXIT OPEN', flag='GREEN')

        overall_best = None
        overall_sectors = [None, None, None]
        leader_lap = 1
        chequered = False
        yellow_until = None
        t_pos = t_gaps = 0.0
        sim_t = 0.0

        while not all(c.done for c in cars):
            # Шаг симуляции фиксирован, ускорение — за счёт более короткого сна.
            await asyncio.sleep(TICK / self.speed)
            dt = TICK
            sim_t += dt
            clock += timedelta(seconds=dt)
            out = []

            if yellow_until is None and leader_lap == 8:
                yellow_until = sim_t + 25
                out.append(msg('TrackStatus', {'Status': '2', 'Message': 'Yellow'}))
                out.append(race_control('YELLOW IN TRACK SECTOR 7', 'Flag', 'YELLOW'))
            elif yellow_until and sim_t > yellow_until > 0:
                yellow_until = -1
                out.append(msg('TrackStatus', {'Status': '1', 'Message': 'AllClear'}))
                out.append(race_control('CLEAR IN TRACK SECTOR 7', 'Flag', 'CLEAR'))

            for c in cars:
                if c.done:
                    continue
                if c.pit_left > 0:
                    c.pit_left -= dt
                    if c.pit_left <= 0:
                        c.compound = 'HARD'
                        c.tyre_age = 0
                        out.append(msg('TimingData', {'Lines': {c.num: {
                            'InPit': False, 'PitOut': True, 'NumberOfPitStops': c.pits}}}))
                        out.append(msg('TimingAppData', {'Lines': {c.num: {'Stints': {
                            str(c.pits): {'Compound': 'HARD', 'New': 'true',
                                          'TotalLaps': 0}}}}}))
                    c.t_lap += dt
                    c.t_sector += dt
                    continue
                v = L / c.lap_time * SECTOR_SPEED[min(c.sector, 2)]
                c.dist += v * dt
                c.t_lap += dt
                c.t_sector += dt
                if c.dist < 0:
                    continue
                pos_in_lap = c.dist - c.lap * L
                sector_now = min(2, int(pos_in_lap / (L / 3))) if pos_in_lap < L else 3
                if sector_now <= c.sector:
                    continue
                # Сектор пройден
                i = c.sector
                # Точный момент пересечения границы сектора внутри шага.
                over = (pos_in_lap - (i + 1) * L / 3) / v
                st = c.t_sector - over
                c.t_sector = over
                c.sector_times[i] = st
                pb = c.best_sectors[i] is None or st < c.best_sectors[i]
                ob = overall_sectors[i] is None or st < overall_sectors[i]
                if pb:
                    c.best_sectors[i] = st
                if ob:
                    overall_sectors[i] = st
                sectors = {str(i): {'Value': f'{st:.3f}', 'PersonalFastest': pb,
                                    'OverallFastest': ob}}
                if i == 0:
                    sectors.update({'1': {'Value': ''}, '2': {'Value': ''}})
                line = {'Sectors': sectors}
                if i == 0 and c.pits and c.tyre_age < 1:
                    line['PitOut'] = False
                c.sector = i + 1
                if c.sector == 3:  # круг завершён
                    lt = c.t_lap - over
                    c.t_lap = over
                    c.lap += 1
                    c.sector = 0
                    c.tyre_age += 1
                    c.lap_time = c.pace + c.tyre_age * 0.06 + rnd.gauss(0, 0.25) \
                        - (0.8 if c.compound == 'SOFT' else 0)
                    pb = c.best is None or lt < c.best
                    ob = c.lap > 1 and (overall_best is None or lt < overall_best)
                    line['LastLapTime'] = {'Value': fmt_lap(lt), 'PersonalFastest': pb,
                                           'OverallFastest': ob}
                    line['NumberOfLaps'] = c.lap
                    if pb:
                        c.best = lt
                        line['BestLapTime'] = {'Value': fmt_lap(lt)}
                    if ob:
                        overall_best = lt
                        out.append(race_control(
                            f'FASTEST LAP {c.tla} ({c.num}) TIME {fmt_lap(lt)}'))
                    out.append(msg('TimingAppData', {'Lines': {c.num: {'Stints': {
                        str(c.pits): {'TotalLaps': c.tyre_age}}}}}))
                    if c.lap >= TOTAL_LAPS or chequered:
                        c.done = True
                    elif c.lap == c.pit_lap:
                        c.pits += 1
                        c.pit_left = 22.0
                        line['InPit'] = True
                    if c.lap + 1 > leader_lap and not chequered and not c.done:
                        leader_lap = c.lap + 1
                        out.append(msg('LapCount', {'CurrentLap': leader_lap}))
                    if c.lap >= TOTAL_LAPS and not chequered:
                        chequered = True
                        out.append(race_control('CHEQUERED FLAG', 'Flag', 'CHEQUERED'))
                out.append(msg('TimingData', {'Lines': {c.num: line}}))

            # Позиции на карте ~ раз в 0.3 с фида
            t_pos += dt
            if t_pos >= 0.3:
                t_pos = 0.0
                entries = {}
                for c in cars:
                    x, y = track.at(max(c.dist, 0))
                    status = 'OffTrack' if c.pit_left > 0 else 'OnTrack'
                    entries[c.num] = {'Status': status, 'X': int(x), 'Y': int(y), 'Z': 0}
                out.append(msg('Position.z', encode_z(
                    {'Position': [{'Timestamp': ts(), 'Entries': entries}]})))

            # Порядок и отрывы ~ раз в секунду фида
            t_gaps += dt
            if t_gaps >= 1.0:
                t_gaps = 0.0
                order = sorted(cars, key=lambda c: (-c.lap if c.done else 0, -c.dist))
                lead = order[0]
                lines = {}
                for p, c in enumerate(order, 1):
                    v = L / c.lap_time
                    if p == 1:
                        gap = f'LAP {leader_lap}'
                        interval = gap
                    else:
                        laps_down = int((lead.dist - c.dist) // L)
                        gap = f'{laps_down} L' if laps_down else fmt_gap((lead.dist - c.dist) / v)
                        ahead = order[p - 2]
                        interval = fmt_gap((ahead.dist - c.dist) / v)
                    lines[c.num] = {'Position': str(p), 'Line': p, 'GapToLeader': gap,
                                    'IntervalToPositionAhead': {'Value': interval}}
                out.append(msg('TimingData', {'Lines': lines}))
                out.append(msg('Heartbeat', {'Utc': ts()}))

            for ev in out:
                yield ev

        yield msg('SessionStatus', {'Status': 'Finished'})
        yield msg('ExtrapolatedClock', {'Extrapolating': False, 'Remaining': '0:00:00'})
