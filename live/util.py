"""Мелкие помощники для структур фида F1."""


def items(x):
    """Пары (ключ, значение) и для списка, и для dict-с-индексами."""
    if isinstance(x, list):
        return [(str(i), v) for i, v in enumerate(x)]
    if isinstance(x, dict):
        return list(x.items())
    return []


def at(x, i):
    """Элемент по индексу из списка или dict-с-индексами."""
    if isinstance(x, list):
        return x[i] if i < len(x) else None
    if isinstance(x, dict):
        return x.get(str(i))
    return None
