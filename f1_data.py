"""F1 session data from OpenF1: schedule, laps, stints, positions, car telemetry.

FastF1 downloads timing data from livetiming.formula1.com, which many networks block
(HTTP 403). OpenF1 serves the same data and is reachable everywhere, so the explorer
builds FastF1-like tables from it. Downloads are cached on disk (the same cache as
the live timing app), so a session loads from the network only once.

OpenF1 has data from the 2023 season onwards.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from live.openf1 import OpenF1Source, catalog

SESSION_TYPES = ['R', 'Q', 'S', 'SQ', 'FP1', 'FP2', 'FP3']
SESSION_NAMES = {'R': 'Race', 'Q': 'Qualifying', 'S': 'Sprint',
                 'SQ': ('Sprint Qualifying', 'Sprint Shootout'),
                 'FP1': 'Practice 1', 'FP2': 'Practice 2', 'FP3': 'Practice 3'}
RACE_SESSIONS = {'Race', 'Sprint'}   # sessions with positions by lap
FIRST_YEAR = 2023


@dataclass
class Session:
    key: int
    year: int
    event: str                      # 'Las Vegas Grand Prix'
    name: str                       # 'Race', 'Qualifying', …
    drivers: dict                   # 'VER' → {'number', 'team', 'color'}
    laps: pd.DataFrame              # one row per lap, FastF1-like columns
    stints: pd.DataFrame            # Driver, Stint, Compound, LapStart, LapEnd
    classification: dict = field(default_factory=dict)  # code → final position (with penalties)
    finished: bool = True
    _src: OpenF1Source = field(default=None, repr=False)

    @property
    def title(self):
        return f'{self.event} {self.year}'

    def color(self, code):
        return self.drivers.get(code, {}).get('color', '#888888')

    def style(self, code):
        """Line style: team colour; the second driver of a team is dashed."""
        team = self.drivers.get(code, {}).get('team')
        mates = sorted(c for c, d in self.drivers.items() if d.get('team') == team)
        dashed = bool(team) and mates.index(code) > 0 if code in mates else False
        return {'color': self.color(code), 'linestyle': '--' if dashed else '-'}

    def driver_laps(self, code):
        return self.laps[self.laps['Driver'] == code]

    def quick_laps(self, code=None, threshold=1.07):
        """Laps within 107 % of the session's fastest, without pit in/out laps
        (what FastF1 calls `pick_quicklaps`)."""
        laps = self.laps if code is None else self.driver_laps(code)
        ok = self.laps['LapTime'].notna() & ~self.laps['PitOutLap'] & ~self.laps['PitInLap']
        best = self.laps.loc[ok, 'LapTime'].min()
        return laps[ok.loc[laps.index] & (laps['LapTime'] <= threshold * best)]

    def fastest_lap(self, code):
        laps = self.driver_laps(code).dropna(subset=['LapTime'])
        return None if laps.empty else laps.loc[laps['LapTime'].idxmin()]

    def lap_telemetry(self, code, lap):
        """Car data over one lap: Time (s), Distance (m), Speed, RPM, nGear, Throttle, Brake."""
        num = self.drivers[code]['number']
        car = self._src.car_data(self.key, num, finished=self.finished)
        t0 = lap['LapStartDate'].timestamp() * 1000
        t1 = t0 + lap['LapTime'] * 1000
        i0, i1 = np.searchsorted(car.t, [t0, t1])
        if i1 - i0 < 2:
            return pd.DataFrame()
        tel = pd.DataFrame({
            'Time': (np.asarray(car.t[i0:i1]) - t0) / 1000,
            'Speed': car.speed[i0:i1], 'RPM': car.rpm[i0:i1], 'nGear': car.gear[i0:i1],
            'Throttle': car.throttle[i0:i1], 'Brake': car.brake[i0:i1],
        })
        # Distance: integrate speed (km/h → m/s) over time, as FastF1 `add_distance` does
        dt = np.diff(tel['Time'], prepend=0.0)
        tel['Distance'] = np.cumsum(tel['Speed'] / 3.6 * dt)
        return tel


# --- schedule ---------------------------------------------------------------------

def enable_cache(path=None):
    """Kept for compatibility: OpenF1 data is always cached in live_cache/."""


def get_years(start=FIRST_YEAR, end=None):
    """Seasons for the drop-down (newest first)."""
    end = end or pd.Timestamp.now().year
    return list(range(end, start - 1, -1))


def get_events(year):
    """Grand Prix names of a season that already have a session (no pre-season testing)."""
    known = {n for v in SESSION_NAMES.values() for n in ((v,) if isinstance(v, str) else v)}
    events = []
    for s in catalog(year):
        if s['available'] and s['name'] in known and s['meeting'] not in events:
            events.append(s['meeting'])
    return events


def _find_session(year, gp, session_type):
    wanted = SESSION_NAMES.get(session_type, session_type)
    wanted = (wanted,) if isinstance(wanted, str) else wanted
    gp_l = gp.lower()
    for s in catalog(year):
        if s['name'] in wanted and gp_l in (s['meeting'] or '').lower() + ' ' + (s['location'] or '').lower():
            return s
    raise ValueError(f'No {"/".join(wanted)} for "{gp}" in {year} on OpenF1')


# --- loading ----------------------------------------------------------------------

def get_session(year, gp, session_type, src=None):
    """Load a session: `gp` is a Grand Prix name or location ('Monza', 'Italian')."""
    item = _find_session(year, gp, session_type)
    if not item['available']:
        raise ValueError(f'{item["meeting"]} {item["name"]} has not finished yet')
    src = src or OpenF1Source(item['key'])
    src.session = item['key']
    sess = src.resolve()
    finished = src._finished(sess)
    cdir = src.cache_dir / str(item['key']) if finished else None
    raw = {ep: src._get(ep, cdir / f'{ep}.json' if cdir else None, session_key=item['key'])
           for ep in ('drivers', 'laps', 'stints', 'position', 'pit')}
    return build_session(raw, key=item['key'], year=year, event=item['meeting'],
                         name=item['name'], finished=finished, src=src)


def build_session(raw, key, year, event, name, finished=True, src=None):
    """OpenF1 tables → Session (pure function, no network)."""
    drivers = {}
    for d in raw['drivers']:
        code = d.get('name_acronym') or str(d['driver_number'])
        drivers[code] = {'number': d['driver_number'], 'team': d.get('team_name') or '',
                         'color': '#' + (d.get('team_colour') or '888888')}
    code_of = {v['number']: k for k, v in drivers.items()}

    laps = pd.DataFrame(raw['laps'])
    if laps.empty:
        laps = pd.DataFrame(columns=['driver_number', 'lap_number', 'date_start',
                                     'lap_duration', 'is_pit_out_lap'])
    laps = pd.DataFrame({
        'Driver': laps['driver_number'].map(code_of),
        'DriverNumber': laps['driver_number'],
        'LapNumber': laps['lap_number'].astype(int),
        'LapStartDate': pd.to_datetime(laps['date_start'], utc=True, format='ISO8601'),
        'LapTime': pd.to_numeric(laps['lap_duration'], errors='coerce'),
        'PitOutLap': laps['is_pit_out_lap'].fillna(False).astype(bool),
    }).sort_values(['Driver', 'LapNumber'], ignore_index=True)
    pit_in = {(p['driver_number'], p['lap_number']) for p in raw.get('pit', [])}
    laps['PitInLap'] = [(n, l) in pit_in for n, l in zip(laps['DriverNumber'], laps['LapNumber'])]

    # Tyres on each lap
    stints = pd.DataFrame([{
        'Driver': code_of.get(s['driver_number']), 'Stint': s['stint_number'],
        'Compound': (s.get('compound') or 'UNKNOWN').upper(),
        'LapStart': s['lap_start'], 'LapEnd': s.get('lap_end') or s['lap_start'],
        'TyreAgeAtStart': s.get('tyre_age_at_start') or 0,
    } for s in raw['stints'] if s.get('lap_start')],
        columns=['Driver', 'Stint', 'Compound', 'LapStart', 'LapEnd', 'TyreAgeAtStart'])
    laps['Stint'], laps['Compound'], laps['TyreLife'] = np.nan, None, np.nan
    for st in stints.itertuples():
        m = (laps['Driver'] == st.Driver) & laps['LapNumber'].between(st.LapStart, st.LapEnd)
        laps.loc[m, 'Stint'] = st.Stint
        laps.loc[m, 'Compound'] = st.Compound
        laps.loc[m, 'TyreLife'] = st.TyreAgeAtStart + laps.loc[m, 'LapNumber'] - st.LapStart + 1

    # Position at the end of each lap (on the road: last update before the lap ends)
    laps['Position'] = np.nan
    pos = pd.DataFrame(raw.get('position') or [], columns=['date', 'driver_number', 'position'])
    # Final classification: the last update per driver, which comes after the flag and
    # includes time penalties
    classification = {}
    if name in RACE_SESSIONS and not pos.empty:
        last = pos.sort_values('date').groupby('driver_number').tail(1)
        classification = {code_of[n]: int(p) for n, p in zip(last['driver_number'], last['position'])
                          if n in code_of}
    if name in RACE_SESSIONS and not pos.empty and laps['LapStartDate'].notna().any():
        # Same resolution on both keys: pandas 3 parses to µs, Timedelta math gives ns
        pos['date'] = pd.to_datetime(pos['date'], utc=True, format='ISO8601').astype('datetime64[ns, UTC]')
        end = (laps['LapStartDate'] + pd.to_timedelta(laps['LapTime'], unit='s')).astype('datetime64[ns, UTC]')
        q = laps.assign(_end=end).dropna(subset=['_end'])
        merged = pd.merge_asof(
            q.reset_index().sort_values('_end'), pos.sort_values('date'),
            left_on='_end', right_on='date', left_by='DriverNumber', right_by='driver_number',
            direction='backward')
        laps.loc[merged['index'], 'Position'] = merged['position'].values

    return Session(key=key, year=year, event=event, name=name, drivers=drivers,
                   laps=laps, stints=stints, classification=classification,
                   finished=finished, _src=src)


def get_drivers(session):
    """Driver codes of a session: race classification, otherwise best-lap order."""
    laps = session.laps
    if session.classification:
        order = sorted(session.classification, key=session.classification.get)
    elif laps.empty:
        return sorted(session.drivers)
    else:
        order = laps.groupby('Driver')['LapTime'].min().sort_values().index
    rest = sorted(set(session.drivers) - set(order))
    return list(order) + rest
