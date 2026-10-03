"""История кругов: строка на каждый завершённый круг каждого пилота.

Копится прямо из потока патчей (SessionState.apply), поэтому работает для
любого источника и «без спойлеров»: при перемотке назад состояние
пересоздаётся, и история собирается заново до текущего момента.
"""

import re

from .util import items

DRY = ('SOFT', 'MEDIUM', 'HARD')


def lap_seconds(v):
    """'1:35.130' → 95.13; '' / None → None."""
    if not v or not isinstance(v, str):
        return None
    m = re.fullmatch(r'(?:(\d+):)?(\d+(?:\.\d+)?)', v.strip())
    if not m:
        return None
    return int(m.group(1) or 0) * 60 + float(m.group(2))


def parse_gap(v, pos):
    """Отрыв от лидера → (секунды | None, кругов отставания)."""
    if pos == 1:
        return 0.0, 0
    if not v or not isinstance(v, str):
        return None, 0
    s = v.strip().upper().lstrip('+')
    m = re.fullmatch(r'(\d+)\s*L(?:APS?)?', s)
    if m:
        return None, int(m.group(1))
    try:
        return float(s), 0
    except ValueError:
        return None, 0


class LapHistory:
    def __init__(self):
        self.laps = {}           # num → [row, ...]
        self._open = {}          # num → флаги текущего (незавершённого) круга
        self._track_green = True
        self.version = 0

    def _flags(self, num):
        return self._open.setdefault(num, {'out': False, 'dirty': not self._track_green})

    # --- наблюдение за патчами --------------------------------------------

    def observe(self, topic, patch, data):
        """Вызывается после merge патча; data — всё состояние SessionState.data."""
        if topic == 'TrackStatus' and isinstance(patch, dict) and 'Status' in patch:
            self._track_green = str(patch['Status']) == '1'
            if not self._track_green:
                for f in self._open.values():
                    f['dirty'] = True
        elif topic == 'TimingData' and isinstance(patch, dict):
            for num, p in items(patch.get('Lines')):
                if isinstance(p, dict):
                    self._observe_line(num, p, data)

    def _observe_line(self, num, p, data):
        f = self._flags(num)
        if p.get('PitOut') is True:
            f['out'] = True
        if 'NumberOfLaps' not in p:
            return
        try:
            lap = int(p['NumberOfLaps'])
        except (TypeError, ValueError):
            return
        rows = self.laps.setdefault(num, [])
        if lap < 1 or (rows and rows[-1]['lap'] >= lap):
            return
        self._record(num, lap, f, data)
        # Новый круг. PitOut в состоянии может остаться с прошлого круга выезда,
        # поэтому круг выезда — только если пилот всё ещё в пит-лейне или
        # PitOut придёт в патче уже во время этого круга. Круг заезда помечается
        # задним числом: это круг перед кругом выезда (надёжно для любого фида).
        line = ((data.get('TimingData') or {}).get('Lines') or {}).get(num) or {}
        self._open[num] = {'out': bool(line.get('InPit')), 'dirty': not self._track_green}

    def _record(self, num, lap, f, data):
        line = ((data.get('TimingData') or {}).get('Lines') or {}).get(num) or {}
        try:
            pos = int(line.get('Position') or 0) or None
        except (TypeError, ValueError):
            pos = None
        gap, down = parse_gap(line.get('GapToLeader') or line.get('TimeDiffToFastest'), pos)
        app = ((data.get('TimingAppData') or {}).get('Lines') or {}).get(num) or {}
        stints = [s for _, s in items(app.get('Stints')) if isinstance(s, dict)]
        stint = stints[-1] if stints else {}
        age = stint.get('TotalLaps')
        rows = self.laps[num]
        if f['out'] and rows and rows[-1]['lap'] == lap - 1:
            rows[-1]['in'] = True          # круг перед кругом выезда — круг заезда
        rows.append({
            'lap': lap,
            't': lap_seconds((line.get('LastLapTime') or {}).get('Value')),
            'pos': pos, 'gap': gap, 'down': down,
            'comp': stint.get('Compound') or None,
            'age': int(age) if isinstance(age, (int, float)) or str(age).isdigit() else None,
            'stint': len(stints),
            'in': False, 'out': f['out'], 'dirty': f['dirty'],
        })
        self.version += 1
