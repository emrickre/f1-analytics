"""Плеер записанной сессии: пауза, скорость, перемотка в любую сторону.

Вся лента событий лежит в памяти. Вперёд — просто применяем события до
нужной точки; назад — сбрасываем состояние и мгновенно применяем ленту
заново с начала (десятки тысяч событий — доли секунды).
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

TICK = 0.05


@dataclass
class Timeline:
    msgs: list                          # [(offset_sec, event)] по возрастанию
    base: datetime | None = None        # абсолютное время нулевого смещения
    start: float = 0.0                  # смещение старта сессии
    outline: list | None = None         # готовый контур трассы, если есть
    title: str = ''
    meta: dict = field(default_factory=dict)

    @property
    def end(self):
        return self.msgs[-1][0] if self.msgs else 0.0


class Player:
    SPEEDS = (0.5, 1, 2, 4, 8, 16, 32, 64)

    def __init__(self, server):
        self.server = server
        self.tl = None
        self.idx = 0
        self.pos = 0.0
        self.playing = False
        self.speed = 1.0
        self.loading = None     # текст прогресса загрузки
        self.error = None
        self.version = 0

    # --- управление ------------------------------------------------------

    def set_timeline(self, tl, speed=None, skip=0.0):
        self.tl = tl
        if speed:
            self.speed = speed
        self.server.reset_state(outline=tl.outline)
        self.idx, self.pos = 0, 0.0
        self.seek(tl.start - 30 + skip)
        self.playing = True
        self.loading = self.error = None
        self._changed()

    def play(self):
        if self.tl and self.pos >= self.tl.end:
            self.seek(self.tl.start - 30)
        self.playing = bool(self.tl)
        self._changed()

    def pause(self):
        self.playing = False
        self._changed()

    def set_speed(self, v):
        self.speed = min(max(float(v), 0.1), 256)
        self._changed()

    def seek(self, t):
        """t — смещение от начала ленты (не от старта сессии)."""
        if not self.tl:
            return
        t = min(max(t, 0.0), self.tl.end)
        if t < self.pos:
            self.server.reset_state(outline=self.tl.outline)
            self.idx = 0
        self._apply_until(t)
        self.pos = t
        self._changed()

    def seek_session(self, rel):
        """rel — секунды от старта сессии (может быть отрицательным)."""
        if self.tl:
            self.seek(self.tl.start + rel)

    # --- цикл ------------------------------------------------------------

    async def run(self):
        last = time.monotonic()
        while True:
            await asyncio.sleep(TICK)
            now = time.monotonic()
            dt, last = now - last, now
            if not (self.playing and self.tl):
                continue
            self.pos = min(self.pos + dt * self.speed, self.tl.end)
            self._apply_until(self.pos)
            if self.pos >= self.tl.end:
                self.playing = False
                self._changed()

    def _apply_until(self, t):
        msgs, base = self.tl.msgs, self.tl.base
        while self.idx < len(msgs) and msgs[self.idx][0] <= t:
            off, ev = msgs[self.idx]
            ts = (base + timedelta(seconds=off)).isoformat().replace('+00:00', 'Z') \
                if base else ev.get('ts')
            self.server.apply_event(ev, ts)
            self.idx += 1

    def _changed(self):
        self.version += 1

    # --- статус для фронтенда --------------------------------------------

    def status(self):
        tl = self.tl
        return {
            'mode': 'replay',
            'loaded': tl is not None,
            'title': tl.title if tl else '',
            'sessionKey': tl.meta.get('session_key') if tl else None,
            't': round(self.pos - tl.start, 2) if tl else 0,
            'min': round(-tl.start, 2) if tl else 0,
            'max': round(tl.end - tl.start, 2) if tl else 0,
            'playing': self.playing,
            'speed': self.speed,
            'speeds': self.SPEEDS,
            'loading': self.loading,
            'error': self.error,
        }
