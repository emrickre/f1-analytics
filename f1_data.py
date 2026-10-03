"""Тонкая обёртка над FastF1: кэш, расписание, загрузка сессий, список пилотов."""

import fastf1


SESSION_TYPES = ['R', 'Q', 'S', 'FP1', 'FP2', 'FP3']
RACE_SESSIONS = {'R', 'S'}  # сессии, где есть позиции по кругам


def enable_cache(path='cache'):
    """Включить кэш FastF1 (ускоряет повторные загрузки)."""
    fastf1.Cache.enable_cache(path)


def get_years(start=2018, end=2024):
    """Список годов для выпадающего списка (по убыванию)."""
    return list(range(end, start - 1, -1))


def get_events(year):
    """Названия Гран-при за сезон, без предсезонных тестов (RoundNumber == 0).

    Используем backend='ergast' — открытый источник расписания, который не
    упирается в заблокированный сервер livetiming.formula1.com.
    """
    try:
        schedule = fastf1.get_event_schedule(year, backend='ergast')
    except Exception:
        schedule = fastf1.get_event_schedule(year)
    schedule = schedule[schedule['RoundNumber'] > 0]
    return schedule['EventName'].tolist()


def get_session(year, gp, session_type):
    """Получить и загрузить сессию FastF1."""
    session = fastf1.get_session(year, gp, session_type)
    session.load()
    return session


def get_drivers(session):
    """Аббревиатуры пилотов сессии (для списка выбора)."""
    try:
        drivers = session.laps['Driver'].dropna().unique().tolist()
    except Exception:
        drivers = []
    if not drivers:
        try:
            drivers = session.results['Abbreviation'].dropna().tolist()
        except Exception:
            drivers = []
    return sorted(set(drivers))
