# Live timing web app

A browser timing screen for Formula 1 sessions: timing tower, track map, car telemetry and an
in-race strategy tab. It replays any past session from [OpenF1](https://openf1.org) with full
playback controls and can also follow the official F1 live feed.

The server needs only `websockets` and `certifi` (`requirements-live.txt`): no pandas, so it runs
on a small VPS.

## Run

```bash
pip install -r requirements-live.txt
python -m live openf1                           # latest OpenF1 session
python -m live --speed 8 openf1 2026 "Kuala Lumpur" Qualifying
python -m live openf1 11730 --skip 2700         # by session_key, from minute 45
python -m live sim                              # synthetic race, no network needed
```

Open http://127.0.0.1:8765. Other sources: `live` (official F1 feed, writes `recordings/*.jsonl`),
`archive 2024 Monza` (F1 static archive), `replay recordings/….jsonl`.

The first time an OpenF1 session is opened, playback starts after a few seconds, once lap
times, tyres and flags have loaded. Car positions and gaps keep loading in the background in
5-minute chunks, starting from the moment on screen (and from the new spot after a seek). A
race takes about 30 s in total. Telemetry for the selected driver first loads a window around
the current moment (~1 s), then the full session. After that everything comes from `live_cache/`. OpenF1 is free for past sessions but locks all free access while a session
is live. Sessions that are already cached keep working then.

## Features

**Playback**: play/pause (space), ±30 s (←/→ ±10 s, Shift ±1 min), 0.5×–64× speed, seek bar,
session picker by season. Sessions load without restarting the server.

**Timing**: positions with change animation, gaps and intervals, sectors (personal/overall
best), tyres and stint age, pit status, Race Control messages, track status banner
(including *Safety Car in this lap*). The track map is interpolated with a short render
delay for smooth motion. The header shows Overtake mode (2026+) or DRS enabled/disabled.

**Telemetry** (F1 TV-style): gauge with speed on the outer ring and throttle/brake on the inner
ring, RPM, gear, per-car DRS (before 2026), plus a large map with sectors, zoom, pan and
follow. Car data is fetched on demand for the selected driver only.

**Strategy**: learns from laps completed *so far*, so there are no spoilers when you seek back.
- race trace (gap to leader per lap, SC/VSC shaded) and tyre stint chart;
- tyre model: degradation per compound (s/lap ± 95 % CI) from clean laps of all drivers, pit
  loss measured from real green-flag stops. This is the same approach that cut forecast error
  by 20 % in the [notebook](../notebooks/tyre_degradation.ipynb);
- pit window for the selected driver: which lap to stop, which compound, gain vs. staying out;
- before the race has enough laps: tyre wear forecast from long runs in the same weekend's
  practice and sprint, blended with the season median (the method and its check against 16
  races are in the [strategy notebook](../notebooks/strategy_sim.ipynb), section 6);
- Safety Car risk: the chance of a Safety Car or VSC before the flag (2026 season rate). Under a
  Safety Car or VSC it also says whether to box now at the reduced cost or keep the
  green-flag plan;
- wet races: intermediate/wet tyres, with a *track improving* flag when the track gets faster
  quicker than the tyres wear;
- 2019–2024 races: a late stop for fresh softs to chase the fastest-lap point when the gap to
  the car behind covers the pit loss.

## How it works

```
source (OpenF1 / F1 feed / archive / recording / sim)
  → events in F1 feed format → reducer (deep-merge of patches)
  → lap history + strategy model → WebSocket → browser
```

| Module | Role |
|---|---|
| `openf1.py` | OpenF1 tables → F1 feed events, disk cache, rate-limit handling |
| `sources.py` | F1 SignalR Core feed, static archive, recordings |
| `decode.py`, `state.py` | `.z` decoding, patch reducer, track outline, view model |
| `history.py`, `strategy.py` | lap history from the patch stream; degradation, pit window and Safety Car calls in pure Python |
| `practice.py` | pre-race tyre wear from the weekend's practice long runs (fetched in the background) |
| `player.py` | in-memory timeline, seeking replays from the start |
| `server.py` | HTTP + WebSocket server and playback commands |
| `sim.py` | synthetic race in feed format |
| `static/` | frontend (vanilla JS, canvas/SVG) |

The track outline comes from api.multiviewer.app. If that is unavailable, it is traced from car
positions.

## Limitations

The official live feed (`live` / `archive`) is served from livetiming.formula1.com, which
returns 403 to many networks, including all cloud/hosting IPs tested. Since 2025, car positions
and telemetry in that feed require an F1 TV login (via FastF1; `--no-auth` tries without).
OpenF1 live data requires a paid OpenF1 plan.

## Deploy to a server

Debian/Ubuntu with Python 3.10+ and SSH access. About 200 MB of free RAM is enough: a race takes
~110 MB in memory, ~130 MB peak while loading, and ~7 MB of disk cache (car positions and
telemetry are stored as compressed columns, not raw JSON).

```bash
./deploy/deploy.sh root@1.2.3.4      # port 8765; run again to update
```

The script installs `requirements-live.txt`, creates the `f1live` user and the `f1-live`
systemd service (restart on failure, 300 MB memory limit), and generates an access token. It
then prints `http://IP:8765/?token=…`. After the first visit the token is kept in a cookie;
without it the server answers 401.
