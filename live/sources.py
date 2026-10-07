"""Источники сообщений. События везде одинаковые:

    {'kind': 'snapshot', 'data': {topic: state}}            — полный снимок
    {'kind': 'msg', 'topic': str, 'data': ..., 'ts': str}   — патч

Потоковые источники (live, sim) отдают их через async events().
Повторяемые (archive, file, openf1) — целиком через load_timeline(),
а воспроизведением/перемоткой управляет live/player.py.

- LiveSource    — живой SignalR Core фид livetiming.formula1.com;
- ArchiveSource — прошедшая сессия из статического архива F1 (.jsonStream);
- FileSource    — повтор нашей собственной записи (.jsonl);
- SimSource     — синтетическая гонка (live/sim.py), работает без сети.
"""

import asyncio
import json
import logging
import ssl
import threading
import time
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path

from .player import Timeline
from .state import parse_ts

log = logging.getLogger('live.sources')

STATIC_URL = 'https://livetiming.formula1.com/static/'
UA = {'User-Agent': 'Mozilla/5.0 (f1-analytics live MVP)'}

TOPICS = ['Heartbeat', 'DriverList', 'ExtrapolatedClock', 'RaceControlMessages',
          'SessionInfo', 'SessionStatus', 'TimingAppData', 'TimingStats',
          'TrackStatus', 'WeatherData', 'Position.z', 'CarData.z',
          'SessionData', 'TimingData', 'TopThree', 'LapCount', 'TeamRadio']

# В архиве CarData огромный и для MVP не нужен.
ARCHIVE_TOPICS = [t for t in TOPICS if t not in ('CarData.z', 'TeamRadio')]


def _ssl_context():
    # Python с python.org на macOS не видит системные сертификаты.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_CTX = _ssl_context()


def http_get(url, timeout=30):
    """Тело ответа. timeout — предел на весь запрос: таймаут сокета ограничивает
    только паузу между пакетами, а сервер под нагрузкой может отдавать ответ по
    капле минутами (OpenF1 так делает)."""
    req = urllib.request.Request(url, headers=UA)
    deadline = time.monotonic() + timeout
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        parts = []
        while chunk := r.read(1 << 16):
            parts.append(chunk)
            if time.monotonic() > deadline:
                raise TimeoutError(f'{url[:80]}…: no full answer in {timeout} s')
        return b''.join(parts)


# --- live ------------------------------------------------------------------

class LiveSource:
    URL = 'wss://livetiming.formula1.com/signalrcore'
    NEGOTIATE = 'https://livetiming.formula1.com/signalrcore/negotiate'

    def __init__(self, no_auth=False):
        self.no_auth = no_auth

    async def events(self):
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        put = lambda ev: loop.call_soon_threadsafe(queue.put_nowait, ev)  # noqa: E731
        conn = await asyncio.to_thread(self._connect, put)
        try:
            while True:
                yield await queue.get()
        finally:
            await asyncio.to_thread(conn.stop)

    def _connect(self, put):
        import requests
        from signalrcore.hub_connection_builder import HubConnectionBuilder
        from signalrcore.messages.completion_message import CompletionMessage

        token_factory = None
        if not self.no_auth:
            # Логин в аккаунт F1/F1TV через FastF1 (откроет ссылку для входа,
            # токен кешируется). С 2025 г. live-фид требует авторизацию.
            from fastf1.internals.f1auth import get_auth_token
            token_factory = get_auth_token

        r = requests.options(self.NEGOTIATE, headers=UA, timeout=15)
        headers = dict(UA)
        if 'AWSALBCORS' in r.cookies:
            headers['Cookie'] = f"AWSALBCORS={r.cookies['AWSALBCORS']}"

        conn = HubConnectionBuilder() \
            .with_url(self.URL, options={'verify_ssl': True, 'headers': headers,
                                         'access_token_factory': token_factory}) \
            .with_automatic_reconnect({'type': 'interval', 'keep_alive_interval': 10,
                                       'intervals': [1, 2, 5, 10, 30, 60]}) \
            .build()

        def on_result(msg):
            for m in (msg if isinstance(msg, list) else [msg]):
                if isinstance(m, CompletionMessage) and isinstance(m.result, dict):
                    put({'kind': 'snapshot', 'data': m.result})

        def on_feed(args):
            # args = [topic, data, utc]
            if isinstance(args, list) and len(args) >= 2:
                put({'kind': 'msg', 'topic': args[0], 'data': args[1],
                     'ts': args[2] if len(args) > 2 else None})

        def subscribe():
            time.sleep(0.2)
            log.info('subscribing to %d topics', len(TOPICS))
            conn.invoke('Subscribe', [TOPICS], on_invocation=on_result)

        conn.on_open(lambda: (log.info('connected'),
                              threading.Thread(target=subscribe, daemon=True).start()))
        conn.on_close(lambda: log.warning('connection closed'))
        conn.on_error(lambda e: log.error('signalr error: %s', e))
        conn.on('feed', on_feed)
        conn.start()
        return conn


# --- архив ----------------------------------------------------------------

def resolve_archive_path(year, gp, session):
    """Найти путь сессии в {year}/Index.json по подстроке названия ГП."""
    idx = json.loads(http_get(f'{STATIC_URL}{year}/Index.json').decode('utf-8-sig'))
    gp_l, sess_l = gp.lower(), session.lower()
    for m in idx.get('Meetings', []):
        names = ' '.join(str(m.get(k, '')) for k in ('Name', 'Location', 'OfficialName'))
        names += ' ' + str((m.get('Country') or {}).get('Name', ''))
        if gp_l not in names.lower():
            continue
        for s in m.get('Sessions', []):
            if s.get('Path') and sess_l == str(s.get('Name', '')).lower():
                return s['Path']
        avail = [s.get('Name') for s in m.get('Sessions', [])]
        raise SystemExit(f'Сессия "{session}" не найдена в {m["Name"]}; есть: {avail}')
    raise SystemExit(f'Гран-при "{gp}" не найден в {year}/Index.json')


def parse_json_stream(raw):
    """Строки вида 'HH:MM:SS.mmm{json}' → [(offset_sec, data)]."""
    out = []
    for line in raw.decode('utf-8-sig').splitlines():
        line = line.strip()
        if len(line) < 13:
            continue
        h, m, s = line[:12].split(':')
        off = int(h) * 3600 + int(m) * 60 + float(s)
        try:
            out.append((off, json.loads(line[12:])))
        except json.JSONDecodeError:
            continue
    return out


class ArchiveSource:
    """Прошедшая сессия из livetiming.formula1.com/static (для плеера)."""

    def __init__(self, path, cache_dir='live_cache'):
        self.path = path.strip('/') + '/'
        self.cache = Path(cache_dir) / self.path

    def _load_topic(self, topic):
        f = self.cache / f'{topic}.jsonStream'
        if not f.exists():
            url = STATIC_URL + urllib.parse.quote(self.path) + f'{topic}.jsonStream'
            try:
                raw = http_get(url, timeout=120)
            except Exception as e:  # у части сессий нет некоторых топиков
                log.warning('%s: %s', topic, e)
                return []
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(raw)
        return parse_json_stream(f.read_bytes())

    def _load_all(self):
        msgs = []
        for topic in ARCHIVE_TOPICS:
            entries = self._load_topic(topic)
            log.info('archive %-22s %6d msgs', topic, len(entries))
            msgs += [(off, topic, data) for off, data in entries]
        msgs.sort(key=lambda m: m[0])
        return msgs

    def load_timeline(self):
        msgs = self._load_all()
        if not msgs:
            raise RuntimeError('Archive is empty or unavailable (check VPN / path)')

        # Абсолютное время: Heartbeat.Utc - смещение.
        base = None
        for off, topic, data in msgs:
            if topic == 'Heartbeat' and isinstance(data, dict) and parse_ts(data.get('Utc')):
                base = parse_ts(data['Utc']) - timedelta(seconds=off)
                break

        start = 0.0
        for off, topic, data in msgs:
            if topic == 'SessionStatus' and isinstance(data, dict) \
                    and data.get('Status') == 'Started':
                start = off
                break
        return Timeline(
            msgs=[(off, {'kind': 'msg', 'topic': topic, 'data': data})
                  for off, topic, data in msgs],
            base=base, start=start, title=self.path.strip('/'))


# --- наша запись -----------------------------------------------------------

class FileSource:
    def __init__(self, path):
        self.path = path

    def load_timeline(self):
        msgs, t_first = [], None
        with open(self.path) as f:
            for line in f:
                rec = json.loads(line)
                t = rec.pop('t', None) or t_first or 0.0
                if t_first is None:
                    t_first = t
                msgs.append((t - t_first, rec))
        return Timeline(msgs=msgs, title=Path(self.path).name)


# --- запись ---------------------------------------------------------------

async def recorded(events, path):
    """Tee: пишет каждое событие (сырым, до декодирования) в .jsonl."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as f:
        async for ev in events:
            f.write(json.dumps({'t': time.time(), **ev}, separators=(',', ':')) + '\n')
            f.flush()
            yield ev
