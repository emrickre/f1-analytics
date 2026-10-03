"""State reducer: собирает текущее состояние сессии из снапшотов и патчей.

Особенности протокола F1:
- патчи приходят как частичные dict, их надо deep-merge'ить в состояние;
- массивы в патчах приходят словарём с индексами-строками
  (`"Sectors": {"1": {...}}`), их нужно мёржить в элемент списка;
- `Position` — не патч, а пачка сэмплов; храним только последний.
"""

import copy
import math
from datetime import datetime, timezone

from .decode import normalize
from .history import LapHistory
from .util import at, items

# Топики, которые редьюсер игнорирует (пишутся только в сырую запись).
IGNORED = {'CarData', 'Heartbeat', 'AudioStreams', 'ContentStreams',
           'TeamRadio', 'TimingStats', 'TopThree', 'RcmSeries'}

TRACK_STATUS = {
    '1': ('green', 'Green flag'),
    '2': ('yellow', 'Yellow flag'),
    '4': ('sc', 'Safety Car'),
    '5': ('red', 'Red flag'),
    '6': ('vsc', 'Virtual Safety Car'),
    '7': ('vsc-end', 'VSC ending'),
}
SC_ENDING = ('sc-end', 'Safety Car in this lap')


def merge(base, patch):
    """Deep-merge патча F1 в базовое состояние. Возвращает новое значение."""
    if isinstance(patch, dict):
        if isinstance(base, list):
            for k, v in patch.items():
                if k == '_deleted':
                    continue
                i = int(k)
                while len(base) <= i:
                    base.append(None)
                base[i] = merge(base[i], v)
            return base
        if not isinstance(base, dict):
            base = {}
        for k, v in patch.items():
            if k == '_deleted':
                continue
            base[k] = merge(base.get(k), v)
        for k in patch.get('_deleted') or ():
            base.pop(str(k), None)
        return base
    return copy.deepcopy(patch) if isinstance(patch, list) else patch


def parse_ts(ts):
    """'2024-09-01T13:03:12.3456789Z' → aware datetime (или None)."""
    if not ts or not isinstance(ts, str):
        return None
    s = ts.rstrip('Z')
    if '.' in s:
        head, frac = s.split('.', 1)
        s = f'{head}.{frac[:6]}'
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def overtake_mode(rcm):
    """Режим обгона в сессии по Race Control: Overtake (2026+) или DRS.

    → {'name': 'Overtake' | 'DRS', 'on': bool} или None, если объявлений не было.
    """
    for m in reversed(rcm):
        msg = (m.get('Message') or '').upper().strip()
        for name in ('OVERTAKE', 'DRS'):
            if msg in (f'{name} ENABLED', f'{name} DISABLED'):
                return {'name': 'Overtake' if name == 'OVERTAKE' else 'DRS',
                        'on': msg.endswith('ENABLED')}
    return None


def sc_in_this_lap(rcm):
    """Последнее сообщение о машине безопасности — «SAFETY CAR IN THIS LAP»?"""
    for m in reversed(rcm):
        msg = (m.get('Message') or '').upper()
        if 'VSC' in msg or 'VIRTUAL' in msg or 'SAFETY CAR' not in msg:
            continue
        if 'IN THIS LAP' in msg:
            return True
        if 'DEPLOYED' in msg:
            return False
    return False


class TrackOutline:
    """Строит контур трассы по координатам одной машины за круг.

    Используется как запасной вариант, если готовый контур
    (api.multiviewer.app) недоступен.
    """

    MIN_STEP = 30        # 3 м — точки ближе не пишем (единицы — дм)
    CLOSE_DIST = 300     # 30 м до первой точки — круг замкнулся
    MIN_LENGTH = 20000   # 2 км — меньше не считается кругом

    def __init__(self):
        self.points = []
        self.rotation = 0
        self.corners = []
        self.sectors = []
        self.source = None
        self.done = False
        self._ref = None
        self._length = 0.0
        self.version = 0

    def set_external(self, points, rotation=0, corners=(), source='external',
                     sectors=None):
        self.points = points
        self.rotation = rotation
        self.corners = list(corners)
        self.source = source
        self.sectors = sectors or []      # индексы точек — границы S1/S2 и S2/S3
        self.done = True
        self.version += 1

    def feed(self, entries):
        if self.done:
            return
        if self._ref is None or self._ref not in entries:
            # Выбираем машину на трассе; при смене опорной машины начинаем заново.
            for num, e in entries.items():
                if e.get('Status') == 'OnTrack' and (e.get('X') or e.get('Y')):
                    self._ref = num
                    self.points, self._length = [], 0.0
                    break
            else:
                return
        e = entries[self._ref]
        if e.get('Status') != 'OnTrack':
            return
        p = (e.get('X', 0), e.get('Y', 0))
        if self.points:
            d = math.dist(p, self.points[-1])
            if d < self.MIN_STEP:
                return
            if d > 2000:  # телепорт/выброс — игнорируем
                return
            self._length += d
        self.points.append(p)
        self.version += 1
        if (self._length > self.MIN_LENGTH
                and math.dist(p, self.points[0]) < self.CLOSE_DIST):
            self.done = True
            self.source = 'recorded'

    def to_json(self):
        return {
            'points': self.points,
            'rotation': self.rotation,
            'corners': self.corners,
            'sectors': self.sectors,
            'source': self.source or 'recording',
            'done': self.done,
        }


class PosFrame:
    """Компактный кадр координат: nums[i] → vals[3i:3i+3] = (x, y, t_ms)."""
    __slots__ = ('t', 'nums', 'vals')

    def __init__(self, t, nums, vals):
        self.t, self.nums, self.vals = t, nums, vals


class SessionState:
    def __init__(self):
        self.data = {}           # topic → состояние
        self.positions = {}      # num → {X, Y, Status}
        self.positions_ts = None
        # Сэмплы координат с метками времени — для плавной интерполяции
        # на клиенте. Сервер забирает их и очищает (take_frames).
        self.pos_frames = []     # [(epoch_ms, {num: [x, y, on]})]
        self.clock = None        # время фида последнего сообщения
        self.outline = TrackOutline()
        self.history = LapHistory()   # строка на каждый завершённый круг
        self.version = 0
        self.messages = 0

    # --- reducer -------------------------------------------------------

    def apply(self, topic, data, ts=None):
        topic, data = normalize(topic, data)
        self.messages += 1
        t = parse_ts(ts)
        if t:
            self.clock = t
        if topic in IGNORED:
            return
        if topic == 'Position':
            self._apply_position(data)
        else:
            self.data[topic] = merge(self.data.get(topic), data)
            self.history.observe(topic, data, self.data)
        self.version += 1

    def apply_snapshot(self, snapshot):
        """Ответ на Subscribe: {topic: полное состояние}."""
        for topic, data in snapshot.items():
            if topic.endswith('.z') or topic == 'Position':
                self.apply(topic, data)
            else:
                self.data.pop(topic, None)
                self.apply(topic, data)

    def _apply_position(self, data):
        if isinstance(data, PosFrame):
            self._apply_pos_frame(data)
            return
        for sample in data.get('Position', []):
            for num, e in (sample.get('Entries') or {}).items():
                self.positions[num] = e
            self.outline.feed(sample.get('Entries') or {})
            self.positions_ts = sample.get('Timestamp')
            t = parse_ts(self.positions_ts)
            if t and (self.clock is None or t > self.clock):
                self.clock = t
            if not t:
                continue
            frame_ms = int(t.timestamp() * 1000)
            exact = {}                           # сэмплы с собственным временем (OpenF1)
            common = {}
            for num, e in (sample.get('Entries') or {}).items():
                v = [e.get('X', 0), e.get('Y', 0), e.get('Status') == 'OnTrack']
                if e.get('T'):
                    exact.setdefault(e['T'], {})[num] = v
                else:
                    common[num] = v
            if common:
                self.pos_frames.append((frame_ms, common))
            self.pos_frames.extend(sorted(exact.items()))
        if len(self.pos_frames) > 6000:         # перемотка — нужен только хвост
            del self.pos_frames[:-6000]

    def _apply_pos_frame(self, f):
        v = f.vals
        exact = {}
        for i, num in enumerate(f.nums):
            x, y, t = v[3 * i], v[3 * i + 1], v[3 * i + 2]
            self.positions[num] = {'Status': 'OnTrack', 'X': x, 'Y': y}
            exact.setdefault(t, {})[num] = [x, y, True]
        if not self.outline.done:
            self.outline.feed(self.positions)
        clock = datetime.fromtimestamp(f.t / 1000, timezone.utc)
        if self.clock is None or clock > self.clock:
            self.clock = clock
        self.pos_frames.extend(sorted(exact.items()))
        if len(self.pos_frames) > 6000:
            del self.pos_frames[:-6000]

    def take_frames(self):
        frames, self.pos_frames = self.pos_frames, []
        return frames

    # --- view для фронтенда -------------------------------------------

    def view(self):
        d = self.data
        info = d.get('SessionInfo') or {}
        meeting = info.get('Meeting') or {}
        drivers = d.get('DriverList') or {}
        lines = (d.get('TimingData') or {}).get('Lines') or {}
        app_lines = (d.get('TimingAppData') or {}).get('Lines') or {}

        rows = []
        for num, line in items(lines):
            if not isinstance(line, dict):
                continue
            drv = drivers.get(num) or {}
            stints = [s for _, s in items((app_lines.get(num) or {}).get('Stints'))
                      if isinstance(s, dict)]
            stint = stints[-1] if stints else {}
            sectors = []
            for i in range(3):
                s = at(line.get('Sectors'), i) or {}
                sectors.append({
                    'v': s.get('Value') or '',
                    'ob': bool(s.get('OverallFastest')),
                    'pb': bool(s.get('PersonalFastest')),
                })
            last = line.get('LastLapTime') or {}
            best = line.get('BestLapTime') or {}
            gap = line.get('GapToLeader')
            if gap is None:
                gap = line.get('TimeDiffToFastest')
            interval = (line.get('IntervalToPositionAhead') or {}).get('Value')
            if interval is None:
                interval = line.get('TimeDiffToPositionAhead')
            try:
                pos = int(line.get('Position') or line.get('Line') or 99)
            except (TypeError, ValueError):
                pos = 99
            rows.append({
                'num': num,
                'pos': pos,
                'tla': drv.get('Tla') or num,
                'name': drv.get('FullName') or drv.get('BroadcastName') or '',
                'team': drv.get('TeamName') or '',
                'color': '#' + (drv.get('TeamColour') or '888888'),
                'gap': gap or '',
                'interval': interval or '',
                'last': {'v': last.get('Value') or '',
                         'ob': bool(last.get('OverallFastest')),
                         'pb': bool(last.get('PersonalFastest'))},
                'best': best.get('Value') or '',
                'sectors': sectors,
                'laps': line.get('NumberOfLaps'),
                'pits': line.get('NumberOfPitStops'),
                'tyre': stint.get('Compound') or '',
                'tyreAge': stint.get('TotalLaps'),
                'tyreNew': stint.get('New') == 'true' or stint.get('New') is True,
                'inPit': bool(line.get('InPit')),
                'pitOut': bool(line.get('PitOut')),
                'out': bool(line.get('Retired') or line.get('Stopped')
                            or line.get('KnockedOut')),
            })
        rows.sort(key=lambda r: r['pos'])

        cars = {}
        for num, e in self.positions.items():
            drv = drivers.get(num) or {}
            cars[num] = {
                'x': e.get('X', 0), 'y': e.get('Y', 0),
                'on': e.get('Status') == 'OnTrack',
                'tla': drv.get('Tla') or num,
                'color': '#' + (drv.get('TeamColour') or '888888'),
            }

        ts = d.get('TrackStatus') or {}
        code = str(ts.get('Status') or '1')
        flag, flag_text = TRACK_STATUS.get(code, ('green', ts.get('Message') or ''))

        lap = d.get('LapCount') or {}
        weather = d.get('WeatherData') or {}
        rcm = [m for _, m in items((d.get('RaceControlMessages') or {})
                                   .get('Messages')) if isinstance(m, dict)]
        # Код статуса трассы при заезде SC не меняется (остаётся 4) — смотрим,
        # что последним объявлял Race Control: DEPLOYED или IN THIS LAP.
        if code == '4' and sc_in_this_lap(rcm):
            flag, flag_text = SC_ENDING

        return {
            'session': {
                'meeting': meeting.get('Name') or '',
                'circuit': (meeting.get('Circuit') or {}).get('ShortName') or '',
                'name': info.get('Name') or '',
                'type': info.get('Type') or '',
                'status': (d.get('SessionStatus') or {}).get('Status') or '',
            },
            'lap': {'current': lap.get('CurrentLap'), 'total': lap.get('TotalLaps')},
            'clock': self._remaining(),
            'feedTime': self.clock.isoformat() if self.clock else None,
            'track': {'code': code, 'flag': flag, 'text': flag_text},
            'overtake': overtake_mode(rcm),
            'weather': {
                'air': weather.get('AirTemp'), 'track': weather.get('TrackTemp'),
                'rain': weather.get('Rainfall') in ('1', 1, True),
            },
            'rows': rows,
            'cars': cars,
            'rcm': [{'utc': m.get('Utc'), 'cat': m.get('Category'),
                     'flag': m.get('Flag'), 'msg': m.get('Message')}
                    for m in rcm[-20:]][::-1],
            'outlineVersion': self.outline.version,
        }

    def _remaining(self):
        """Оставшееся время сессии с экстраполяцией по времени фида."""
        c = self.data.get('ExtrapolatedClock') or {}
        rem = c.get('Remaining')
        if not rem:
            return None
        try:
            h, m, s = rem.split(':')
            secs = int(h) * 3600 + int(m) * 60 + float(s)
        except ValueError:
            return rem
        if c.get('Extrapolating') and self.clock:
            t0 = parse_ts(c.get('Utc'))
            if t0:
                secs = max(0.0, secs - (self.clock - t0).total_seconds())
        secs = int(secs)
        return f'{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}'
