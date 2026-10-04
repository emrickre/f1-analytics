"""Plots for the session explorer. Each returns a matplotlib Figure, so it can be
embedded in Tkinter or saved to a file. `session` is an f1_data.Session."""

from matplotlib.figure import Figure
from matplotlib.patches import Patch
from matplotlib.ticker import MaxNLocator

COMPOUND_COLORS = {'SOFT': '#da291c', 'MEDIUM': '#ffd12e', 'HARD': '#f0f0ec',
                   'INTERMEDIATE': '#43b02a', 'WET': '#0067ad'}


def _placeholder(message):
    """Empty figure with a note, instead of failing when there is no data."""
    fig = Figure(figsize=(9, 6))
    ax = fig.add_subplot(111)
    ax.text(0.5, 0.5, message, ha='center', va='center', wrap=True, fontsize=12)
    ax.axis('off')
    return fig


def _title(session, suffix):
    return f'{session.title} {session.name} — {suffix}'


def plot_lap_times(session, driver_codes):
    """Lap time (s) by lap number for the selected drivers."""
    if not driver_codes:
        return _placeholder('Select at least one driver')

    fig = Figure(figsize=(9, 6))
    ax = fig.add_subplot(111)
    plotted = False
    for code in driver_codes:
        laps = session.quick_laps(code)
        if laps.empty:
            continue
        ax.plot(laps['LapNumber'], laps['LapTime'], marker='o', markersize=3,
                label=code, **session.style(code))
        plotted = True

    if not plotted:
        return _placeholder('No lap data for the selected drivers')

    ax.set_xlabel('Lap')
    ax.set_ylabel('Lap time (s)')
    ax.set_title(_title(session, 'lap times'))
    ax.legend()
    fig.tight_layout()
    return fig


def plot_telemetry(session, driver_codes):
    """Speed / throttle / brake / gear over distance on each driver's fastest lap."""
    if not driver_codes:
        return _placeholder('Select at least one driver')

    channels = [('Speed', 'Speed (km/h)'),
                ('Throttle', 'Throttle (%)'),
                ('Brake', 'Brake'),
                ('nGear', 'Gear')]

    fig = Figure(figsize=(9, 8))
    axes = fig.subplots(len(channels), 1, sharex=True)
    plotted = False
    for code in driver_codes:
        lap = session.fastest_lap(code)
        if lap is None:
            continue
        tel = session.lap_telemetry(code, lap)
        if tel.empty:
            continue
        label = f'{code} {int(lap["LapTime"] // 60)}:{lap["LapTime"] % 60:06.3f}'
        for ax, (col, _) in zip(axes, channels):
            ax.plot(tel['Distance'], tel[col], label=label, **session.style(code))
        plotted = True

    if not plotted:
        return _placeholder('No telemetry for the selected drivers')

    for ax, (_, label) in zip(axes, channels):
        ax.set_ylabel(label)
    axes[-1].set_xlabel('Distance (m)')
    axes[0].set_title(_title(session, 'fastest lap telemetry'))
    axes[0].legend(loc='lower right')
    fig.tight_layout()
    return fig


def plot_positions(session, driver_codes):
    """Position through the race (P1 on top). Race and sprint only."""
    if not driver_codes:
        return _placeholder('Select at least one driver')
    if session.name not in ('Race', 'Sprint'):
        return _placeholder('Positions are available for a race or sprint only')

    fig = Figure(figsize=(9, 6))
    ax = fig.add_subplot(111)
    plotted = False
    for code in driver_codes:
        laps = session.driver_laps(code)
        if laps.empty or laps['Position'].isna().all():
            continue
        ax.plot(laps['LapNumber'], laps['Position'], label=code, **session.style(code))
        plotted = True

    if not plotted:
        return _placeholder('No position data for the selected drivers')

    ax.invert_yaxis()
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_xlabel('Lap')
    ax.set_ylabel('Position')
    ax.set_title(_title(session, 'positions'))
    ax.legend(bbox_to_anchor=(1.02, 1), loc='upper left')
    fig.tight_layout()
    return fig


def plot_tyre_strategy(session, driver_codes):
    """Tyre stints by compound: one horizontal bar per driver."""
    if not driver_codes:
        return _placeholder('Select at least one driver')

    fig = Figure(figsize=(9, 6))
    ax = fig.add_subplot(111)
    seen, rows = {}, []
    for code in driver_codes:
        stints = session.stints[session.stints['Driver'] == code].sort_values('Stint')
        if stints.empty:
            continue
        for st in stints.itertuples():
            color = COMPOUND_COLORS.get(st.Compound, '#888888')
            seen[st.Compound] = color
            ax.barh(code, st.LapEnd - st.LapStart + 1, left=st.LapStart - 1,
                    color=color, edgecolor='black')
        rows.append(code)

    if not rows:
        return _placeholder('No tyre data for the selected drivers')

    ax.invert_yaxis()
    ax.set_xlabel('Lap')
    ax.set_title(_title(session, 'tyre strategy'))
    ax.legend(handles=[Patch(facecolor=c, edgecolor='black', label=k) for k, c in seen.items()],
              bbox_to_anchor=(1.02, 1), loc='upper left')
    fig.tight_layout()
    return fig


def plot_lap_distribution(session, driver_codes):
    """Box plot of lap times per driver."""
    if not driver_codes:
        return _placeholder('Select at least one driver')

    data, labels, colors = [], [], []
    for code in driver_codes:
        times = session.quick_laps(code)['LapTime'].dropna()
        if times.empty:
            continue
        data.append(times.values)
        labels.append(code)
        colors.append(session.color(code))

    if not data:
        return _placeholder('No lap data for the selected drivers')

    fig = Figure(figsize=(9, 6))
    ax = fig.add_subplot(111)
    bp = ax.boxplot(data, tick_labels=labels, patch_artist=True)
    for patch, color in zip(bp['boxes'], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.7)
    ax.set_ylabel('Lap time (s)')
    ax.set_xlabel('Driver')
    ax.set_title(_title(session, 'lap time distribution'))
    fig.tight_layout()
    return fig


# Registry for the GUI and CLI: label → function
PLOTS = {
    'Lap times': plot_lap_times,
    'Fastest lap telemetry': plot_telemetry,
    'Positions': plot_positions,
    'Tyre strategy': plot_tyre_strategy,
    'Lap time distribution': plot_lap_distribution,
}
