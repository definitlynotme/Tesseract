from __future__ import annotations

import logging
from typing import Any


def try_log_nowait(
    logger: Any,
    level: str,
    message: str,
    *args: Any,
    **kwargs: Any,
) -> bool:
    """Emit only when every effective handler lock is immediately available.

    ``nowait`` applies only to acquiring the effective handler locks.  Once
    those locks are held, normal logging still runs synchronously and may do
    formatter, filter, stream, or filesystem work before returning.
    """

    if not isinstance(logger, logging.Logger):
        return False
    handlers: list[logging.Handler] = []
    current: logging.Logger | None = logger
    while current is not None:
        handlers.extend(current.handlers)
        if not current.propagate:
            break
        current = current.parent

    acquired: list[object] = []
    for handler in handlers:
        lock = getattr(handler, "lock", None)
        if lock is None:
            continue
        if not lock.acquire(blocking=False):
            for held in reversed(acquired):
                held.release()
            return False
        acquired.append(lock)
    try:
        target = getattr(logger, str(level), None)
        if not callable(target):
            return False
        target(message, *args, **kwargs)
        return True
    finally:
        for held in reversed(acquired):
            held.release()
