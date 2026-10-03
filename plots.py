"""Функции построения графиков. Каждая возвращает matplotlib Figure
(чтобы её можно было встроить в Tkinter или сохранить в файл)."""

import matplotlib
from matplotlib.figure import Figure
from matplotlib.patches import Patch
import fastf1.plotting


# Стиль FastF1 (цветовые схемы команд/пилотов). Без mpl_timedelta_support,
# т.к. время кругов переводим в секунды вручную.
try:
    fastf1.plotting.setup_mpl(color_scheme='fastf1')
except Exception:
    pass


def _placeholder(message):
    """Пустая фигура с поясняющим текстом — вместо падения при отсутствии данных."""
    fig = Figure(figsize=(9, 6))
    ax = fig.add_subplot(111)
    ax.text(0.5, 0.5, message, ha='center', va='center', wrap=True, fontsize=12)
    ax.axis('off')
    return fig


def _driver_color(code, session):
    """Цвет пилота через FastF1; запасной серый при ошибке."""
    try:
        return fastf1.plotting.get_driver_color(code, session=session)
    except Exception:
        return '#888888'


def _title(session, suffix):
    ev = session.event
    return f'{ev["EventName"]} {ev.year} — {suffix}'


def plot_lap_times(session, driver_codes):
    """Время круга (с) по номеру круга для выбранных пилотов."""
    if not driver_codes:
        return _placeholder('Выберите хотя бы одного пилота')

    fig = Figure(figsize=(9, 6))
    ax = fig.add_subplot(111)
    plotted = False
    for code in driver_codes:
        laps = session.laps.pick_drivers(code).pick_quicklaps().reset_index()
        if laps.empty:
            continue
        ax.plot(laps['LapNumber'], laps['LapTime'].dt.total_seconds(),
                marker='o', label=code, color=_driver_color(code, session))
        plotted = True

    if not plotted:
        return _placeholder('Нет данных кругов для выбранных пилотов')

    ax.set_xlabel('Круг')
    ax.set_ylabel('Время круга (с)')
    ax.set_title(_title(session, 'темп по кругам'))
    ax.legend()
    fig.tight_layout()
    return fig


def plot_telemetry(session, driver_codes):
    """Скорость / газ / тормоз / передача по дистанции на быстрейшем круге."""
    if not driver_codes:
        return _placeholder('Выберите хотя бы одного пилота')

    channels = [('Speed', 'Скорость (км/ч)'),
                ('Throttle', 'Газ (%)'),
                ('Brake', 'Тормоз'),
                ('nGear', 'Передача')]

    fig = Figure(figsize=(9, 8))
    axes = fig.subplots(len(channels), 1, sharex=True)
    plotted = False
    for code in driver_codes:
        try:
            lap = session.laps.pick_drivers(code).pick_fastest()
            tel = lap.get_car_data().add_distance()
        except Exception:
            continue
        if tel is None or tel.empty:
            continue
        color = _driver_color(code, session)
        for ax, (col, _) in zip(axes, channels):
            if col in tel:
                ax.plot(tel['Distance'], tel[col], label=code, color=color)
        plotted = True

    if not plotted:
        return _placeholder('Нет телеметрии для выбранных пилотов')

    for ax, (_, label) in zip(axes, channels):
        ax.set_ylabel(label)
    axes[-1].set_xlabel('Дистанция (м)')
    axes[0].set_title(_title(session, 'телеметрия быстрейшего круга'))
    axes[0].legend(loc='upper right')
    fig.tight_layout()
    return fig


def plot_positions(session, driver_codes):
    """Позиция по ходу гонки (1 — сверху). Только для гонки/спринта."""
    if not driver_codes:
        return _placeholder('Выберите хотя бы одного пилота')
    if session.name not in ('Race', 'Sprint'):
        return _placeholder('График позиций доступен только для гонки или спринта')

    fig = Figure(figsize=(9, 6))
    ax = fig.add_subplot(111)
    plotted = False
    for code in driver_codes:
        laps = session.laps.pick_drivers(code)
        if laps.empty or laps['Position'].isna().all():
            continue
        ax.plot(laps['LapNumber'], laps['Position'],
                label=code, color=_driver_color(code, session))
        plotted = True

    if not plotted:
        return _placeholder('Нет данных о позициях для выбранных пилотов')

    ax.invert_yaxis()
    ax.set_xlabel('Круг')
    ax.set_ylabel('Позиция')
    ax.set_title(_title(session, 'позиции в гонке'))
    ax.legend(bbox_to_anchor=(1.02, 1), loc='upper left')
    fig.tight_layout()
    return fig


def plot_tyre_strategy(session, driver_codes):
    """Стинты по компаундам шин: горизонтальные сегментные бары на каждого пилота."""
    if not driver_codes:
        return _placeholder('Выберите хотя бы одного пилота')

    fig = Figure(figsize=(9, 6))
    ax = fig.add_subplot(111)
    compounds_seen = {}
    rows = []
    for code in driver_codes:
        laps = session.laps.pick_drivers(code)
        if laps.empty:
            continue
        stints = (laps.groupby(['Stint', 'Compound'])
                  .size().reset_index(name='Laps')
                  .sort_values('Stint'))
        if stints.empty:
            continue
        start = 0
        for _, st in stints.iterrows():
            compound = st['Compound']
            try:
                color = fastf1.plotting.get_compound_color(compound, session=session)
            except Exception:
                color = '#888888'
            compounds_seen[compound] = color
            ax.barh(code, st['Laps'], left=start, color=color, edgecolor='black')
            start += st['Laps']
        rows.append(code)

    if not rows:
        return _placeholder('Нет данных о шинах для выбранных пилотов')

    ax.set_xlabel('Число кругов')
    ax.set_title(_title(session, 'стратегия шин'))
    if compounds_seen:
        ax.legend(handles=[Patch(color=c, label=k) for k, c in compounds_seen.items()],
                  bbox_to_anchor=(1.02, 1), loc='upper left')
    fig.tight_layout()
    return fig


def plot_lap_distribution(session, driver_codes):
    """Box-plot распределения времён кругов по пилотам."""
    if not driver_codes:
        return _placeholder('Выберите хотя бы одного пилота')

    data, labels, colors = [], [], []
    for code in driver_codes:
        laps = session.laps.pick_drivers(code).pick_quicklaps()
        times = laps['LapTime'].dropna().dt.total_seconds()
        if times.empty:
            continue
        data.append(times.values)
        labels.append(code)
        colors.append(_driver_color(code, session))

    if not data:
        return _placeholder('Нет данных кругов для выбранных пилотов')

    fig = Figure(figsize=(9, 6))
    ax = fig.add_subplot(111)
    bp = ax.boxplot(data, labels=labels, patch_artist=True)
    for patch, color in zip(bp['boxes'], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.7)
    ax.set_ylabel('Время круга (с)')
    ax.set_xlabel('Пилот')
    ax.set_title(_title(session, 'разброс времён кругов'))
    fig.tight_layout()
    return fig


# Реестр для GUI: подпись -> функция
PLOTS = {
    'Темп по кругам': plot_lap_times,
    'Телеметрия быстрого круга': plot_telemetry,
    'Позиции в гонке': plot_positions,
    'Стратегия шин': plot_tyre_strategy,
    'Разброс времён кругов': plot_lap_distribution,
}
