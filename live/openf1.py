"""Источник на OpenF1 (api.openf1.org) — работает без VPN.

OpenF1 отдаёт таблицы (круги, позиции, шины, координаты…), а не поток
патчей. Здесь они превращаются в ту же ленту событий, что у SignalR-фида F1
(TimingData, TimingAppData, Position, RaceControlMessages…), поэтому reducer,
сервер и фронтенд работают без изменений.

Бесплатно доступны прошедшие сессии (обычно вскоре после окончания);
live-доступ у OpenF1 платный. Источник отдаёт ленту целиком (load_timeline),
воспроизведением управляет live/player.py.
"""

import bisect
import json
import logging
import math
import time
import urllib.error
import urllib.parse
from array import array
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .player import Timeline
from .state import PosFrame
from .sources import http_get

log = logging.getLogger('live.openf1')

API = 'https://api.openf1.org/v1/'
POS_BUCKET = 0.25   # с — частота кадров Position (сэмплы машин не синхронны)


def ts_of(s):
    return datetime.fromisoformat(s) if s else None


def fmt_lap(t):
    m, s = divmod(t, 60)
    return f'{int(m)}:{s:06.3f}'


def fmt_gap(v):
    if v is None:
        return ''
    if isinstance(v, str):          # "+1 LAP" и т.п.
        return v.replace(' LAP', ' L').lstrip('+') if 'LAP' in v else v
    return f'+{v:.3f}' if v else ''  # 0 — это сам лидер


class CarData:
    """Телеметрия машины в массивах: t_ms, speed, rpm, gear, throttle, brake, drs."""
    __slots__ = ('t', 'speed', 'rpm', 'gear', 'throttle', 'brake', 'drs')

    def __init__(self, rows):
        self.t = array('q', (int(ts_of(r['date']).timestamp() * 1000) for r in rows))
        self.speed = array('H', ((r.get('speed') or 0) for r in rows))
        self.rpm = array('H', ((r.get('rpm') or 0) for r in rows))
        self.gear = array('b', ((r.get('n_gear') or 0) for r in rows))
        self.throttle = array('h', ((r.get('throttle') or 0) for r in rows))
        self.brake = array('h', ((r.get('brake') or 0) for r in rows))
        # DRS: None в данных (с 2026 его нет) → -1; 10/12/14 — открыт.
        self.drs = array('b', (-1 if r.get('drs') is None else min(r['drs'], 127)
                               for r in rows))

    def window(self, t0, t1):
        """Сэмплы с t0 < t <= t1 для отправки в браузер."""
        i0 = bisect.bisect_right(self.t, t0)
        i1 = bisect.bisect_right(self.t, t1)
        return [[self.t[i], self.speed[i], self.rpm[i], self.gear[i], self.throttle[i],
                 self.brake[i], self.drs[i]] for i in range(i0, i1)]


def compact_locations(rows):
    """Строки /location одной машины → (t_ms, x, y) компактными массивами."""
    rows.sort(key=lambda r: r['date'])
    ts = array('q', (int(ts_of(r['date']).timestamp() * 1000) for r in rows))
    return ts, array('l', (r['x'] for r in rows)), array('l', (r['y'] for r in rows))


class OpenF1Source:
    """session: session_key (число), 'latest', либо dict-фильтр для /sessions."""

    def __init__(self, session='latest', cache_dir='live_cache', on_progress=None):
        self.session = session
        self.on_progress = on_progress or (lambda text: None)
        self.cache_dir = Path(cache_dir) / 'openf1'

    # --- загрузка ------------------------------------------------------

    def _get(self, endpoint, cache=None, **params):
        if cache and cache.exists():
            return json.loads(cache.read_text())
        query = '&'.join(f'{k}={urllib.parse.quote(str(v), safe=":")}' for k, v in params.items())
        url = f'{API}{endpoint}?{query}'
        for attempt in range(8):
            try:
                data = json.loads(http_get(url, timeout=120))
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:          # «No results found»
                    data = []
                    break
                if e.code == 429 and attempt < 7:
                    # Лимит бесплатного доступа (в секунду и в минуту) — ждём.
                    wait = e.headers.get('Retry-After')
                    wait = float(wait) if wait and wait.isdigit() else min(60, 2 ** (attempt + 1))
                    self.on_progress(f'OpenF1 rate limit, waiting {wait:.0f} s…')
                    log.info('OpenF1 429, ждём %.0f с', wait)
                    time.sleep(wait)
                    continue
                raise
        time.sleep(0.35)                   # бесплатный лимит — ~3 запроса/с
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data))
        return data

    def resolve(self):
        if isinstance(self.session, dict):
            found = self._get('sessions', **self.session)
            if not found:
                raise SystemExit(f'OpenF1: сессия не найдена по {self.session}')
            return found[-1]
        found = self._get('sessions', session_key=self.session)
        if not found:
            raise SystemExit(f'OpenF1: сессия {self.session} не найдена')
        return found[0]

    def load(self):
        sess = self.resolve()
        key = sess['session_key']
        end = ts_of(sess['date_end'])
        # Кешируем только завершённые сессии — иначе данные ещё дополняются.
        finished = self._finished(sess)
        cdir = self.cache_dir / str(key) if finished else None
        c = (lambda name: cdir / f'{name}.json') if cdir else (lambda name: None)

        log.info('OpenF1: %s %s — %s (key %s)', sess['year'], sess['location'],
                 sess['session_name'], key)
        d = {'session': sess}
        d['meeting'] = (self._get('meetings', c('meeting'),
                                  meeting_key=sess['meeting_key']) or [{}])[0]
        for ep in ('drivers', 'laps', 'stints', 'pit', 'race_control',
                   'weather', 'position', 'intervals'):
            self.on_progress(f'tables: {ep}')
            d[ep] = self._get(ep, c(ep), session_key=key)
            log.info('  %-13s %6d', ep, len(d[ep]))

        start = ts_of(sess['date_start']) - timedelta(minutes=2)
        stop = end + timedelta(minutes=2)
        d['location'] = {}
        for i, drv in enumerate(d['drivers'], 1):
            num = drv['driver_number']
            self.on_progress(f"car positions {i}/{len(d['drivers'])}")
            rows = self._get('location', c(f'location_{num}'), session_key=key,
                             driver_number=num,
                             **{'date>': start.isoformat().replace('+00:00', ''),
                                'date<': stop.isoformat().replace('+00:00', '')})
            d['location'][str(num)] = compact_locations(rows)
            log.info('  location #%-3s %6d', num, len(rows))
            del rows
        return d

    # --- преобразование в ленту событий F1 --------------------------------

    @staticmethod
    def timeline(d):
        sess, meeting = d['session'], d['meeting']
        is_race = sess['session_type'] == 'Race'   # включая спринт
        t_start = ts_of(sess['date_start'])
        ev = []                                    # (datetime, topic, data)

        def add(t, topic, data):
            if t is not None:
                ev.append((t, topic, data))

        drivers = {str(x['driver_number']): x for x in d['drivers']}
        t0 = t_start - timedelta(minutes=3)
        add(t0, 'SessionInfo', {
            'Meeting': {'Name': meeting.get('meeting_name') or sess.get('location'),
                        'Circuit': {'Key': sess.get('circuit_key'),
                                    'ShortName': sess.get('circuit_short_name')}},
            'Name': sess['session_name'], 'Type': sess['session_type'],
            'StartDate': sess['date_start'][:19]})
        add(t0, 'DriverList', {n: {'RacingNumber': n, 'Tla': x.get('name_acronym'),
                                   'FullName': x.get('full_name'),
                                   'TeamName': x.get('team_name'),
                                   'TeamColour': x.get('team_colour') or '888888'}
                               for n, x in drivers.items()})
        add(t0, 'TimingData', {'Lines': {n: {
            'Position': '', 'Sectors': [{'Value': ''}, {'Value': ''}, {'Value': ''}],
            'LastLapTime': {'Value': ''}, 'BestLapTime': {'Value': ''},
            'NumberOfLaps': 0, 'NumberOfPitStops': 0} for n in drivers}})
        add(t0, 'TrackStatus', {'Status': '1', 'Message': 'AllClear'})
        add(t0, 'SessionStatus', {'Status': 'Inactive'})
        if sess['session_type'] == 'Practice':
            dur = int((ts_of(sess['date_end']) - t_start).total_seconds())
            add(t_start, 'ExtrapolatedClock', {
                'Utc': sess['date_start'], 'Extrapolating': True,
                'Remaining': f'{dur // 3600}:{dur % 3600 // 60:02d}:{dur % 60:02d}'})
            add(ts_of(sess['date_end']), 'ExtrapolatedClock',
                {'Extrapolating': False, 'Remaining': '0:00:00'})

        for p in d['position']:
            add(ts_of(p['date']), 'TimingData', {'Lines': {str(p['driver_number']): {
                'Position': str(p['position']), 'Line': p['position']}}})

        for w in d['weather']:
            add(ts_of(w['date']), 'WeatherData', {
                'AirTemp': str(w.get('air_temperature')),
                'TrackTemp': str(w.get('track_temperature')),
                'Rainfall': str(int(bool(w.get('rainfall'))))})

        # Race Control + статус трассы/сессии
        for i, m in enumerate(sorted(d['race_control'], key=lambda m: m['date'])):
            t, msg, flag = ts_of(m['date']), m.get('message') or '', m.get('flag')
            add(t, 'RaceControlMessages', {'Messages': {str(i): {
                'Utc': t.isoformat().replace('+00:00', 'Z'), 'Category': m.get('category'),
                'Flag': flag, 'Message': msg}}})
            status = None
            if 'VIRTUAL SAFETY CAR DEPLOYED' in msg or 'VSC DEPLOYED' in msg:
                status = ('6', 'VSCDeployed')
            elif 'VIRTUAL SAFETY CAR ENDING' in msg or 'VSC ENDING' in msg:
                status = ('7', 'VSCEnding')
            elif 'SAFETY CAR DEPLOYED' in msg:
                status = ('4', 'SCDeployed')
            elif flag == 'RED':
                status = ('5', 'Red')
            elif (flag in ('GREEN', 'CLEAR') and m.get('scope') == 'Track') \
                    or 'TRACK CLEAR' in msg:
                status = ('1', 'AllClear')
            if status:
                add(t, 'TrackStatus', {'Status': status[0], 'Message': status[1]})
            if m.get('category') == 'SessionStatus':
                phase = m.get('qualifying_phase')
                word = 'Started' if 'STARTED' in msg else \
                    'Finished' if 'FINISHED' in msg else msg.title()
                add(t, 'SessionStatus',
                    {'Status': f'Q{phase} {word}' if phase else word})

        # Пит-стопы (в гонке есть время в пит-лейне)
        stops = {}
        for p in sorted(d['pit'], key=lambda p: p['date']):
            lane = p.get('lane_duration') or p.get('pit_duration')
            if not lane:
                continue
            n = str(p['driver_number'])
            stops[n] = stops.get(n, 0) + 1
            t = ts_of(p['date'])
            add(t, 'TimingData', {'Lines': {n: {'InPit': True,
                                                'NumberOfPitStops': stops[n]}}})
            add(t + timedelta(seconds=lane), 'TimingData',
                {'Lines': {n: {'InPit': False, 'PitOut': True}}})

        # Отрывы в гонке
        for iv in d['intervals']:
            add(ts_of(iv['date']), 'TimingData', {'Lines': {str(iv['driver_number']): {
                'GapToLeader': fmt_gap(iv.get('gap_to_leader')),
                'IntervalToPositionAhead': {'Value': fmt_gap(iv.get('interval'))}}}})

        OpenF1Source._laps(d, ev, is_race, t_start)
        OpenF1Source._locations(d, ev)

        ev.sort(key=lambda e: e[0])
        return ev

    @staticmethod
    def _laps(d, ev, is_race, t_start):
        """Сектора/круги/шины с флагами personal/overall best по хронологии."""
        stints = {}
        for s in d['stints']:
            stints.setdefault(str(s['driver_number']), []).append(s)
        lap_start = {(str(x['driver_number']), x['lap_number']): ts_of(x['date_start'])
                     for x in d['laps'] if x.get('date_start')}

        def stint_for(n, lap):
            for s in stints.get(n, []):
                if s['lap_start'] <= lap <= (s.get('lap_end') or 999):
                    return s
            return None

        # Смена шин — в момент начала первого круга стинта.
        for n, ss in stints.items():
            for s in ss:
                t = lap_start.get((n, s['lap_start'])) or t_start
                age = s.get('tyre_age_at_start') or 0
                ev.append((t, 'TimingAppData', {'Lines': {n: {'Stints': {
                    str(s['stint_number'] - 1): {
                        'Compound': s.get('compound') or 'UNKNOWN',
                        'New': 'true' if age == 0 else 'false', 'TotalLaps': age}}}}}))

        # Сырые отметки: (t, n, kind, payload) — потом проходим по порядку.
        raw = []
        for x in d['laps']:
            t = ts_of(x.get('date_start'))
            if t is None:
                continue
            n, lap = str(x['driver_number']), x['lap_number']
            raw.append((t, n, 'start', x))
            acc = 0.0
            for i in range(3):
                v = x.get(f'duration_sector_{i + 1}')
                if v is None:
                    acc = None
                    continue
                if acc is not None:
                    acc += v
                    raw.append((t + timedelta(seconds=acc), n, 'sector', (i, v)))
            dur = x.get('lap_duration')
            if dur and x.get('is_pit_out_lap'):
                # Круг выезда включает время в гараже — как и F1, не показываем.
                raw.append((t + timedelta(seconds=dur), n, 'lap', (lap, None)))
            elif dur:
                raw.append((t + timedelta(seconds=dur), n, 'lap', (lap, dur)))
            elif acc:
                raw.append((t + timedelta(seconds=acc), n, 'lap', (lap, None)))
        raw.sort(key=lambda r: r[0])

        best_sec, best_lap = {}, {}
        ob_sec, ob_lap = [None] * 3, None
        cur_lap = 0
        for t, n, kind, x in raw:
            line = {}
            if kind == 'start':
                line['Sectors'] = {'0': {'Value': ''}, '1': {'Value': ''},
                                   '2': {'Value': ''}}
                line['PitOut'] = bool(x.get('is_pit_out_lap'))
                if is_race and x['lap_number'] > cur_lap:
                    cur_lap = x['lap_number']
                    ev.append((t, 'LapCount', {'CurrentLap': cur_lap}))
            elif kind == 'sector':
                i, v = x
                pb = v < best_sec.get((n, i), 1e9)
                ob = ob_sec[i] is None or v < ob_sec[i]
                if pb:
                    best_sec[(n, i)] = v
                if ob:
                    ob_sec[i] = v
                line['Sectors'] = {str(i): {'Value': f'{v:.3f}', 'PersonalFastest': pb,
                                            'OverallFastest': ob}}
            else:
                lap, dur = x
                line['NumberOfLaps'] = lap
                s = stint_for(n, lap)
                if s:
                    age = (s.get('tyre_age_at_start') or 0) + lap - s['lap_start'] + 1
                    ev.append((t, 'TimingAppData', {'Lines': {n: {'Stints': {
                        str(s['stint_number'] - 1): {'TotalLaps': age}}}}}))
                if dur:
                    pb = dur < best_lap.get(n, 1e9)
                    ob = ob_lap is None or dur < ob_lap
                    line['LastLapTime'] = {'Value': fmt_lap(dur), 'PersonalFastest': pb,
                                           'OverallFastest': ob}
                    if pb:
                        best_lap[n] = dur
                        line['BestLapTime'] = {'Value': fmt_lap(dur)}
                    if ob:
                        ob_lap = dur
                    if pb and not is_race:
                        # Квалификация/практика: отрыв по лучшему кругу.
                        order = sorted(best_lap.items(), key=lambda kv: kv[1])
                        lines, prev = {}, None
                        for m, b in order:
                            lines[m] = {'GapToLeader': fmt_gap(b - ob_lap) if prev else '',
                                        'IntervalToPositionAhead': {
                                            'Value': fmt_gap(b - prev) if prev else ''}}
                            prev = b
                        ev.append((t, 'TimingData', {'Lines': lines}))
                else:
                    line['LastLapTime'] = {'Value': '', 'PersonalFastest': False,
                                           'OverallFastest': False}
            ev.append((t, 'TimingData', {'Lines': {n: line}}))

    @staticmethod
    def _locations(d, ev):
        """Координаты машин → кадры Position с шагом POS_BUCKET."""
        bucket_ms = int(POS_BUCKET * 1000)
        frames = {}
        for num, (ts, xs, ys) in d['location'].items():
            for t, x, y in zip(ts, xs, ys):
                frames.setdefault(t // bucket_ms, []).append((num, x, y, t))
        for b in sorted(frames):
            items = frames.pop(b)
            vals = array('q')
            for _, x, y, t in items:
                vals.extend((x, y, t))
            # Компактный кадр вместо dict'ов: ~10 МБ на гонку вместо ~200.
            ev.append((datetime.fromtimestamp(b * bucket_ms / 1000, timezone.utc),
                       'Position', PosFrame(b * bucket_ms,
                                            tuple(i[0] for i in items), vals)))

    @staticmethod
    def fastest_lap_outline(d):
        """Контур трассы по координатам самого быстрого чистого круга."""
        clean = [x for x in d['laps']
                 if x.get('lap_duration') and x.get('date_start')
                 and not x.get('is_pit_out_lap')
                 and all(x.get(f'duration_sector_{i}') for i in (1, 2, 3))]
        if not clean:
            return None
        lap = min(clean, key=lambda x: x['lap_duration'])
        # Окно с запасом ±2 с, затем режем по самой близкой паре точек на
        # стыке: сэмплы идут раз в ~0.27 с (20+ м на прямой).
        t0 = ts_of(lap['date_start']) - timedelta(seconds=2)
        t1 = t0 + timedelta(seconds=lap['lap_duration'] + 4)
        loc = d['location'].get(str(lap['driver_number']))
        if not loc:
            return None
        ts, xs, ys = loc
        i0 = bisect.bisect_left(ts, int(t0.timestamp() * 1000))
        i1 = bisect.bisect_right(ts, int(t1.timestamp() * 1000))
        pts = list(zip(xs[i0:i1], ys[i0:i1]))
        if len(pts) < 50:
            return None
        k = min(20, len(pts) // 4)
        i, j = min(((i, j) for i in range(k) for j in range(len(pts) - k, len(pts))),
                   key=lambda ij: math.dist(pts[ij[0]], pts[ij[1]]))
        # Границы секторов: где была машина в момент S1 и S1+S2 этого круга.
        lap_t0 = ts_of(lap['date_start']).timestamp() * 1000
        sectors = []
        for dur in (lap['duration_sector_1'],
                    lap['duration_sector_1'] + lap['duration_sector_2']):
            k = bisect.bisect_left(ts, int(lap_t0 + dur * 1000), i0, i1) - i0
            if i < k < j:
                sectors.append(k - i)
        return {'points': pts[i:j], 'sectors': sectors}

    # --- лента для плеера --------------------------------------------------

    def load_timeline(self):
        d = self.load()
        ev = self.timeline(d)
        if not ev:
            raise RuntimeError('OpenF1: no data for this session')
        sess, meeting = d['session'], d['meeting']
        base = ev[0][0]
        msgs = [((t - base).total_seconds(), {'kind': 'msg', 'topic': topic, 'data': data})
                for t, topic, data in ev]
        title = f"{sess['year']} {meeting.get('meeting_name') or sess['location']} — " \
                f"{sess['session_name']}"
        log.info('OpenF1: %s, %d событий', title, len(msgs))
        return Timeline(msgs=msgs, base=base,
                        start=(ts_of(sess['date_start']) - base).total_seconds(),
                        outline=self.fastest_lap_outline(d), title=title,
                        meta={'session_key': sess['session_key'],
                              'finished': self._finished(sess)})

    @staticmethod
    def _finished(sess):
        return datetime.now(timezone.utc) > ts_of(sess['date_end']) + timedelta(minutes=30)

    def car_data(self, session_key, num, finished=True):
        """Телеметрия одной машины за сессию (скорость, обороты, газ…) — компактно."""
        cache = self.cache_dir / str(session_key) / f'car_{num}.json' if finished else None
        rows = self._get('car_data', cache, session_key=session_key, driver_number=num)
        rows.sort(key=lambda r: r['date'])
        return CarData(rows)


_catalog = {}


def catalog(year):
    """Сессии сезона для выбора в интерфейсе (кеш 10 минут)."""
    hit = _catalog.get(year)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    src = OpenF1Source()
    sessions = src._get('sessions', year=year)
    meetings = {m['meeting_key']: m for m in src._get('meetings', year=year)}
    now = datetime.now(timezone.utc)
    items = []
    for s in sorted(sessions, key=lambda s: s['date_start']):
        m = meetings.get(s['meeting_key'], {})
        items.append({
            'key': s['session_key'],
            'meeting': m.get('meeting_name') or s.get('location'),
            'location': s.get('location'),
            'name': s['session_name'],
            'start': s['date_start'],
            'available': ts_of(s['date_end']) < now,
        })
    _catalog[year] = (time.time(), items)
    return items
