"""Запуск: python -m live <источник> [опции]

    python -m live sim                         # синтетическая гонка, без сети
    python -m live live                        # живой фид (нужен вход в F1TV)
    python -m live archive 2024 Monza Race     # повтор прошедшей сессии
    python -m live replay recordings/x.jsonl   # повтор своей записи
    python -m live openf1                      # последняя сессия из OpenF1 (без VPN)
    python -m live openf1 2026 "Kuala Lumpur" Qualifying
"""

import argparse
import asyncio
import errno
import logging
import os
from datetime import datetime

from .openf1 import OpenF1Source
from .server import LiveServer
from .sim import SimSource
from .sources import ArchiveSource, FileSource, LiveSource, resolve_archive_path


def main():
    p = argparse.ArgumentParser(prog='python -m live', description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--host', default='127.0.0.1',
                   help='0.0.0.0 — слушать снаружи (тогда задайте --token)')
    p.add_argument('--token', default=os.environ.get('F1_LIVE_TOKEN'),
                   help='доступ только по ссылке /?token=… (или env F1_LIVE_TOKEN)')
    p.add_argument('--speed', type=float, default=1.0, help='ускорение повтора/симуляции')
    p.add_argument('--record', metavar='FILE.jsonl',
                   help='писать сырой поток в файл (для live по умолчанию включено)')
    sub = p.add_subparsers(dest='source', required=True)

    sub.add_parser('sim', help='синтетическая гонка')

    lv = sub.add_parser('live', help='живой фид livetiming.formula1.com')
    lv.add_argument('--no-auth', action='store_true',
                    help='без входа в F1TV (может отдавать пустые/частичные данные)')

    ar = sub.add_parser('archive', help='прошедшая сессия из архива F1')
    ar.add_argument('year', help='год, либо полный путь сессии из Index.json')
    ar.add_argument('gp', nargs='?', help='часть названия ГП/города: Monza, Italian, Baku…')
    ar.add_argument('session', nargs='?', default='Race',
                    help='Race, Qualifying, Sprint, "Practice 1"… (по умолчанию Race)')
    ar.add_argument('--skip', type=float, default=0,
                    help='начать через N секунд после старта сессии')

    rp = sub.add_parser('replay', help='повтор своей записи .jsonl')
    rp.add_argument('file')

    of = sub.add_parser('openf1', help='сессия из api.openf1.org (работает без VPN)')
    of.add_argument('year', nargs='?', default='latest',
                    help='год, session_key или latest (по умолчанию)')
    of.add_argument('location', nargs='?', help='город трассы: Monza, Baku, "Kuala Lumpur"…')
    of.add_argument('session', nargs='?', default='Race',
                    help='Race, Qualifying, Sprint, "Practice 1"… (по умолчанию Race)')
    of.add_argument('--skip', type=float, default=0,
                    help='начать через N секунд после старта сессии')

    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s: %(message)s',
                        datefmt='%H:%M:%S')
    # websockets пишет «connection rejected (200 OK)» на каждый обычный HTTP-запрос.
    logging.getLogger('websockets.server').setLevel(logging.WARNING)

    record = a.record
    if a.source == 'sim':
        source = SimSource(speed=a.speed)
    elif a.source == 'live':
        source = LiveSource(no_auth=a.no_auth)
        record = record or f'recordings/live_{datetime.now():%Y%m%d_%H%M%S}.jsonl'
    elif a.source == 'archive':
        path = a.year if '/' in a.year else resolve_archive_path(a.year, a.gp or '', a.session)
        logging.info('archive path: %s', path)
        source = ArchiveSource(path)
    elif a.source == 'openf1':
        if a.location:
            session = {'year': a.year, 'location': a.location,
                       'session_name': a.session}
        else:
            session = a.year            # latest или session_key
        source = OpenF1Source(session)
    else:
        source = FileSource(a.file)

    if record:
        logging.info('raw recording → %s', record)
    try:
        if a.host not in ('127.0.0.1', 'localhost') and not a.token:
            logging.warning('сервер открыт наружу без --token — доступ есть у любого')
        server = LiveServer(source, record, speed=a.speed, skip=getattr(a, 'skip', 0.0),
                            token=a.token)
        asyncio.run(server.run(a.host, a.port))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        raise SystemExit(
            f'Порт {a.port} уже занят — скорее всего, сервер уже запущен в другом окне.\n'
            f'Откройте http://{a.host}:{a.port} или остановите его (Ctrl+C в том окне),\n'
            f'либо запустите на другом порту: --port {a.port + 1}')


if __name__ == '__main__':
    main()
