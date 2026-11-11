from __future__ import annotations

import os
import time
from pathlib import Path
from threading import Event


def replace_with_retry(
    source: str | Path,
    target: str | Path,
    *,
    attempts: int = 4,
    initial_delay_seconds: float = 0.01,
    cancel_event: Event | None = None,
) -> None:
    """Atomically replace a file, tolerating brief Windows scanner locks."""

    total_attempts = max(1, int(attempts))
    delay = max(0.0, float(initial_delay_seconds))
    for attempt in range(total_attempts):
        if cancel_event is not None and cancel_event.is_set():
            return
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt + 1 >= total_attempts:
                raise
            if cancel_event is not None and cancel_event.wait(delay):
                return
            if cancel_event is None and delay > 0.0:
                time.sleep(delay)
            delay = min(0.08, max(0.01, delay * 2.0))

