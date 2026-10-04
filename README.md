# F1 Tyre Degradation & Pit-Stop Strategy

[![tests](https://github.com/emrickre/f1-analytics/actions/workflows/tests.yml/badge.svg)](https://github.com/emrickre/f1-analytics/actions/workflows/tests.yml)

How fast do Formula 1 tyres degrade in the 2026 season, does it depend on the compound
or on the track, and when is the optimal moment to pit?

📓 **[Read the analysis → notebooks/tyre_degradation.ipynb](notebooks/tyre_degradation.ipynb)**

## Key findings

- **Degradation is a property of the track, not of the compound.** Fitted race by race,
  degradation ranges from −0.013 to 0.115 s/lap between tracks. Within the same race the median
  SOFT − MEDIUM difference is only −0.008 s/lap, and the soft wears faster in just 5 of 13 races.
- **Simpson's paradox in the naive model.** A season-wide mixed model says SOFT wears slower
  than MEDIUM (0.013 vs 0.032 s/lap). The reason is that teams run softs mostly on low-wear tracks.
- **What a strategist can predict.** After 3 laps of a stint, estimating degradation from the
  *other drivers in the same race* cuts the lap-time forecast error by **20 %** (MAE 0.78 s vs
  0.97 s for a flat-pace baseline). A season-average model gains only 2 %. The track model wins
  in 12 of 16 races.
- **Belgian GP case.** The model's optimal one-stop lap (16, window 12–21) matches the laps the
  teams actually chose (14–17).

![Degradation by race and compound](docs/degradation_by_track.png)

![Lap-time forecast error by race](docs/forecast_error.png)

## Data

Every race lap of the 2026 season from the [OpenF1](https://openf1.org) API: lap and sector
times, tyre compound and age, pit stops, Safety Car / VSC / flags, track temperature.
18,405 laps from 16 races, 14,263 after cleaning. The dataset is included in
[`data/laps_2026.parquet`](data/laps_2026.parquet).

## Method

1. **Load**: `analysis/dataset.py` builds one row per lap per driver from the OpenF1 tables.
2. **Clean**: `analysis/clean.py` drops start laps, pit in/out laps, neutralised laps, yellow
   flags, wet running, outliers (>107 % of the driver's median) and short stints, and records
   the reason for each.
3. **Model**: `analysis/model.py` fits a linear mixed model
   `lap_time ~ compound + compound:tyre_age + fuel + track_temperature` over the season, and
   per-race models with a fixed season fuel effect (0.045 s per lap of fuel).
4. **Validate**: lap-time forecasts with leave-one-race-out and leave-one-driver-out splits.
5. **Strategy**: `analysis/strategy.py` computes measured pit loss, the one-stop race-time
   curve, the optimal window and the undercut gain.

Limitations (linear wear, track evolution vs. wear, no traffic model) are discussed at the end
of the notebook.

## How to run

Requires Python 3.10 or newer.

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements-analysis.txt
jupyter notebook notebooks/tyre_degradation.ipynb
```

The notebook runs offline on the included dataset (about 15 s). To rebuild the dataset from
the API (about 3 min; OpenF1 is closed to free users while a session is live):

```bash
python -m analysis.dataset 2026
```

Tests: `python -m unittest discover tests`

## Also in this repo

- **Live timing web app** ([`live/`](live/README.md)): race replay with timing tower, track
  map, car telemetry and an in-race strategy tab that learns tyre degradation from the laps
  completed so far. `pip install -r requirements-live.txt && python -m live openf1`
- **Session explorer** (`app.py`, `main.py`, `f1_data.py`, `plots.py`): a Tkinter app and CLI
  with lap times, fastest-lap telemetry, positions, tyre strategy and lap-time distribution for
  any session since 2023. Data comes from OpenF1 as FastF1-like tables, because FastF1's source
  (livetiming.formula1.com) is blocked on many networks.
  ```bash
  pip install -r requirements.txt
  python app.py                                   # GUI
  python main.py 2024 "Las Vegas" R NOR PIA      # save all plots to figures/
  ```

## License

[MIT](LICENSE)
