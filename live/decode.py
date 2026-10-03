"""Декодирование сжатых топиков F1 (`Position.z`, `CarData.z`).

Данные приходят строкой base64 от raw deflate (без zlib-заголовка).
"""

import base64
import json
import zlib


def decode_z(data):
    """base64 + raw deflate → объект JSON."""
    raw = zlib.decompress(base64.b64decode(data), -zlib.MAX_WBITS)
    return json.loads(raw)


def encode_z(obj):
    """Обратная операция (нужна симулятору и тестам)."""
    co = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw = co.compress(json.dumps(obj).encode()) + co.flush()
    return base64.b64encode(raw).decode()


def normalize(topic, data):
    """Привести сообщение к виду (topic_без_.z, dict)."""
    if topic.endswith('.z'):
        return topic[:-2], decode_z(data) if isinstance(data, str) else data
    return topic, data
