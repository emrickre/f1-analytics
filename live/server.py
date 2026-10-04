"""HTTP + WebSocket сервер: раздаёт фронтенд и транслирует состояние.

Без внешних зависимостей — только `websockets` (уже стоит вместе с FastF1).

Сервер → браузер (JSON):
    {'type': 'state',    ...SessionState.view()}    — не чаще 4 раз в секунду
    {'type': 'outline',  ...TrackOutline.to_json()}  — контур трассы
    {'type': 'playback', ...Player.status()}         — положение/скорость плеера
    {'type': 'pos', 'now': ms, 'frames': [[ms, {num: [x, y, on]}]]}
                                                     — сэмплы координат, 10 Гц
    {'type': 'sessions', 'year': int, 'items': [...]} — каталог OpenF1
    {'type': 'strategy', ...strategy_view()}          — история кругов и модель шин
    {'type': 'hello', 'build': str}                    — версия фронтенда (при подключении)

Браузер → сервер:
    {'cmd': 'play' | 'pause' | 'toggle'}
    {'cmd': 'speed', 'value': 8}
    {'cmd': 'seek', 't': сек от старта сессии} / {'cmd': 'step', 'dt': ±сек}
    {'cmd': 'sessions', 'year': 2026}
    {'cmd': 'load', 'session_key': 11730}
"""

import asyncio
import hmac
import http.cookies
import json
import logging
import mimetypes
import urllib.parse
from datetime import datetime
from http import HTTPStatus
from pathlib import Path

from websockets.asyncio.server import broadcast, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

from . import openf1
from .player import Player
from .sources import http_get, recorded
from .state import SessionState
from .strategy import strategy_view

log = logging.getLogger('live.server')
STATIC = Path(__file__).parent / 'static'
TICK = 0.25
POS_TICK = 0.1


def frontend_build():
    """Версия фронтенда — по времени изменения файлов static/. Вкладка, открытая
    до перезапуска сервера, сравнивает её при переподключении и перезагружается."""
    return str(max(int(f.stat().st_mtime) for f in STATIC.iterdir() if f.is_file()))


def dumps(obj):
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=False)


class LiveServer:
    def __init__(self, source, record_path=None, speed=1.0, skip=0.0, token=None):
        self.source = source
        self.token = token             # если задан — доступ только с токеном
        self.record_path = record_path
        self.speed = speed
        self.skip = skip
        self.state = SessionState()
        self.player = Player(self)
        self.streaming = hasattr(source, 'events')   # live / sim
        self.clients = set()
        self.ext_outline = None        # kwargs для TrackOutline.set_external
        self._circuit_tried = set()
        self._stream_task = None
        self._load_task = None
        self.focus = {}                # ws → номер пилота для телеметрии
        self.tel = {}                  # номер → CarData (None — грузится/нет данных)
        self._tel_sent = {}            # ws → (номер, до какого t_ms отправлено)

    # --- состояние -------------------------------------------------------

    def apply_event(self, ev, ts=None):
        try:
            kind = ev.get('kind')
            if kind == 'snapshot':
                if 'SessionInfo' in ev['data']:
                    self.new_session()
                self.state.apply_snapshot(ev['data'])
            elif kind == 'outline':
                self.set_outline(ev['points'], source=ev.get('source', 'external'))
            else:
                self.state.apply(ev['topic'], ev['data'], ts or ev.get('ts'))
        except Exception:
            log.exception('bad message: %.300s', ev)
        self._maybe_fetch_outline()

    def reset_state(self, outline=None):
        """Чистое состояние (перемотка назад); готовый контур сохраняется."""
        if outline:
            self.ext_outline = {'points': outline['points'], 'sectors': outline['sectors'],
                                'source': 'fastest-lap'}
        self.state = SessionState()
        if self.ext_outline:
            self.state.outline.set_external(**self.ext_outline)

    def new_session(self):
        log.info('new session — state reset')
        self.ext_outline = None
        self.tel.clear()
        self._tel_sent.clear()
        self._circuit_tried.clear()
        self.reset_state()

    def set_outline(self, pts, rotation=0, corners=(), source='external'):
        self.ext_outline = {'points': pts, 'rotation': rotation,
                            'corners': list(corners), 'source': source}
        self.state.outline.set_external(**self.ext_outline)

    def _maybe_fetch_outline(self):
        info = self.state.data.get('SessionInfo') or {}
        key = ((info.get('Meeting') or {}).get('Circuit') or {}).get('Key')
        if key is None or key in self._circuit_tried or self.state.outline.done:
            return
        self._circuit_tried.add(key)
        if key < 0:  # симулятор
            return
        year = (info.get('StartDate') or '')[:4] or str(datetime.now().year)
        asyncio.get_running_loop().create_task(self._fetch_outline(key, year))

    async def _fetch_outline(self, key, year):
        url = f'https://api.multiviewer.app/api/v1/circuits/{key}/{year}'
        try:
            data = json.loads(await asyncio.to_thread(http_get, url, 15))
            pts = list(zip(data['x'], data['y']))
            corners = [{'n': c.get('number'),
                        'x': c['trackPosition']['x'], 'y': c['trackPosition']['y']}
                       for c in data.get('corners', [])]
            if not self.state.outline.done:
                self.set_outline(pts, data.get('rotation', 0), corners, 'multiviewer')
                log.info('track outline: multiviewer, %d points', len(pts))
        except Exception as e:
            log.warning('outline from %s failed (%s) — строим по позициям машин', url, e)

    # --- источники --------------------------------------------------------

    async def stream(self):
        events = self.source.events()
        if self.record_path:
            events = recorded(events, self.record_path)
        async for ev in events:
            self.apply_event(ev)

    def load(self, source, label='loading…'):
        """Загрузить повторяемый источник в плеер (в фоне)."""
        if self._load_task and not self._load_task.done():
            return
        self._load_task = asyncio.get_running_loop().create_task(self._load(source, label))

    async def _load(self, source, label):
        p = self.player
        p.loading, p.error = label, None
        p._changed()
        loop = asyncio.get_running_loop()

        def progress(text):
            def upd():
                p.loading = text
                p._changed()
            loop.call_soon_threadsafe(upd)

        if hasattr(source, 'on_progress'):
            source.on_progress = progress
        try:
            tl = await asyncio.to_thread(source.load_timeline)
        except BaseException as e:   # SystemExit из резолверов тоже сюда
            log.exception('load failed')
            p.loading, p.error = None, str(e) or e.__class__.__name__
            p._changed()
            return
        if self._stream_task:          # переход из live/sim в повтор
            self._stream_task.cancel()
            self._stream_task = None
            self.streaming = False
        self.new_session()
        p.set_timeline(tl, speed=self.speed, skip=self.skip)
        self.skip = 0.0

    # --- команды из браузера --------------------------------------------------

    async def command(self, ws, msg):
        p, cmd = self.player, msg.get('cmd')
        if cmd == 'play':
            p.play()
        elif cmd == 'pause':
            p.pause()
        elif cmd == 'toggle':
            p.pause() if p.playing else p.play()
        elif cmd == 'speed':
            p.set_speed(msg.get('value', 1))
            self.speed = p.speed
        elif cmd == 'seek':
            p.seek_session(float(msg.get('t', 0)))
        elif cmd == 'step':
            if p.tl:
                p.seek(p.pos + float(msg.get('dt', 0)))
        elif cmd == 'sessions':
            year = int(msg.get('year') or datetime.now().year)
            error = None
            try:
                items = await asyncio.to_thread(openf1.catalog, year)
            except Exception as e:
                log.warning('catalog %s: %s', year, e)
                items, error = [], str(e)
            await ws.send(dumps({'type': 'sessions', 'year': year, 'items': items,
                                 'error': error}))
        elif cmd == 'focus':
            num = msg.get('driver')
            self.focus[ws] = str(num) if num else None
            self._tel_sent.pop(ws, None)
            if num:
                self._ensure_telemetry(str(num), ws)
        elif cmd == 'load':
            key = msg.get('session_key')
            if key:
                self.load(openf1.OpenF1Source(int(key)), 'loading session…')

    # --- телеметрия выбранного пилота (грузится по требованию) -----------------

    def _ensure_telemetry(self, num, ws):
        meta = self.player.tl.meta if (self.player.tl and not self.streaming) else {}
        key = meta.get('session_key')
        if not key:
            broadcast([ws], dumps({'type': 'tel', 'num': num, 'unavailable': True}))
            return
        if num in self.tel:
            return
        self.tel[num] = None
        asyncio.get_running_loop().create_task(
            self._load_telemetry(key, num, meta.get('finished', True)))

    async def _load_telemetry(self, key, num, finished):
        try:
            cd = await asyncio.to_thread(openf1.OpenF1Source().car_data, key, num, finished)
        except Exception as e:
            log.warning('telemetry %s/%s: %s', key, num, e)
            self.tel.pop(num, None)
            return
        cur = self.player.tl.meta.get('session_key') if self.player.tl else None
        if cur == key:
            self.tel[num] = cd
            log.info('telemetry #%s: %d samples', num, len(cd.t))

    async def push_telemetry(self):
        while True:
            await asyncio.sleep(POS_TICK)
            now = self.feed_now_ms()
            if now is None:
                continue
            for ws, num in list(self.focus.items()):
                cd = self.tel.get(num) if num else None
                if cd is None:
                    continue
                prev = self._tel_sent.get(ws)
                reset = (prev is None or prev[0] != num or now < prev[1]
                         or now - prev[1] > 10_000 * max(1.0, self.player.speed))
                t0 = now - 3000 if reset else prev[1]
                frames = cd.window(t0, now)
                self._tel_sent[ws] = (num, now)
                if frames or reset:
                    broadcast([ws], dumps({'type': 'tel', 'num': num, 'reset': reset,
                                           'frames': frames}))

    def playback(self):
        if self.streaming:
            return {'mode': 'live', 'loading': self.player.loading,
                    'error': self.player.error}
        return self.player.status()

    # --- рассылка ----------------------------------------------------------

    async def push(self):
        last_state = last_outline = last_pb = last_strategy = None
        last_outline_t = last_strategy_t = 0.0
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(TICK)
            if not self.clients:
                continue
            st, ol = self.state, self.state.outline
            now = loop.time()
            # Пока контур записывается — шлём не чаще раза в 2 с.
            key = (id(ol), ol.version)
            if key != last_outline and (ol.done or now - last_outline_t > 2):
                broadcast(self.clients, self._outline_msg())
                last_outline, last_outline_t = key, now
            key = (id(st), st.version)
            if key != last_state:
                broadcast(self.clients, self._state_msg())
                last_state = key
            # Стратегия меняется раз в круг — не чаще раза в секунду.
            key = (id(st), st.history.version)
            if key != last_strategy and now - last_strategy_t >= 1.0:
                broadcast(self.clients, dumps(strategy_view(st)))
                last_strategy, last_strategy_t = key, now
            pb = self.playback()
            if pb != last_pb:
                broadcast(self.clients, dumps({'type': 'playback', **pb}))
                last_pb = pb

    async def push_positions(self):
        """Сэмплы координат ~10 раз в секунду + текущее время фида."""
        last_state, last_sent = None, 0.0
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(POS_TICK)
            st = self.state
            reset = id(st) != last_state
            last_state = id(st)
            frames = st.take_frames()
            now = loop.time()
            if not self.clients or not (frames or reset or now - last_sent > 1):
                continue
            last_sent = now
            broadcast(self.clients, dumps({
                'type': 'pos', 'now': self.feed_now_ms(), 'reset': reset,
                'frames': frames[-1500:]}))

    def feed_now_ms(self):
        """Текущее время фида: в повторе — позиция плеера, в live — по данным."""
        p = self.player
        if not self.streaming and p.tl and p.tl.base:
            return int((p.tl.base.timestamp() + p.pos) * 1000)
        return int(self.state.clock.timestamp() * 1000) if self.state.clock else None

    def _state_msg(self):
        return dumps({'type': 'state', **self.state.view()})

    def _outline_msg(self):
        return dumps({'type': 'outline', **self.state.outline.to_json()})

    # --- web -------------------------------------------------------------

    async def ws_handler(self, ws):
        self.clients.add(ws)
        try:
            await ws.send(dumps({'type': 'hello', 'build': frontend_build()}))
            await ws.send(self._outline_msg())
            await ws.send(self._state_msg())
            await ws.send(dumps({'type': 'playback', **self.playback()}))
            await ws.send(dumps(strategy_view(self.state)))
            async for raw in ws:
                try:
                    await self.command(ws, json.loads(raw))
                except (ValueError, TypeError) as e:
                    log.warning('bad command %.200s: %s', raw, e)
        except ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)
            self.focus.pop(ws, None)
            self._tel_sent.pop(ws, None)

    def _authorized(self, request):
        """(ok, токен из ссылки?) — проверка ?token=… или cookie f1token."""
        if not self.token:
            return True, False
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.path).query)
        given = (query.get('token') or [''])[0]
        if given and hmac.compare_digest(given, self.token):
            return True, True
        cookies = http.cookies.SimpleCookie(request.headers.get('Cookie', ''))
        c = cookies.get('f1token')
        return bool(c and hmac.compare_digest(c.value, self.token)), False

    def http(self, connection, request):
        path = request.path.split('?')[0]
        ok, from_link = self._authorized(request)
        if not ok:
            return self._resp(401, 'Access denied: open the link with ?token=…'.encode(),
                              'text/plain')
        if path == '/ws':
            return None  # дальше — WebSocket handshake
        if path == '/api/state':
            return self._resp(200, dumps(self.state.data).encode(), 'application/json')
        if path == '/':
            path = '/index.html'
        f = (STATIC / path.lstrip('/')).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file():
            return self._resp(404, b'not found', 'text/plain')
        ctype = mimetypes.guess_type(f.name)[0] or 'application/octet-stream'
        extra = {}
        if from_link:   # запоминаем токен, чтобы дальше ходить без него в адресе
            extra['Set-Cookie'] = (f'f1token={self.token}; Path=/; HttpOnly; '
                                   f'SameSite=Strict; Max-Age={60 * 60 * 24 * 90}')
        return self._resp(200, f.read_bytes(), ctype, extra)

    @staticmethod
    def _resp(code, body, ctype, extra=None):
        h = Headers()
        for k, v in (extra or {}).items():
            h[k] = v
        h['Content-Type'] = ctype + ('; charset=utf-8' if ctype.startswith(('text', 'application/j')) else '')
        h['Content-Length'] = str(len(body))
        h['Cache-Control'] = 'no-cache'
        return Response(code, HTTPStatus(code).phrase, h, body)

    async def run(self, host='127.0.0.1', port=8765):
        async with serve(self.ws_handler, host, port, process_request=self.http,
                         max_size=None):
            log.info('открой http://%s:%d', host, port)
            loop = asyncio.get_running_loop()
            if self.streaming:
                self._stream_task = loop.create_task(self.stream())
            else:
                self.load(self.source)
            await asyncio.gather(self.player.run(), self.push(), self.push_positions(),
                                 self.push_telemetry())
