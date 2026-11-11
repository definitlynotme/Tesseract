from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

from config_utils import coerce_bool

TRAFFIC_HISTORY_SECONDS = 24 * 60 * 60
TRAFFIC_HISTORY_SAVE_INTERVAL_SECONDS = 30.0
TRAFFIC_SAMPLE_MAX_ELAPSED_SECONDS = 5.0
TRAFFIC_RAW_HISTORY_LIMIT = TRAFFIC_HISTORY_SECONDS + 1
TRAFFIC_HISTORY_PRUNE_BUDGET = 32
_BUCKET_METADATA_KEY = "_traffic_buckets_v1"


UrlOpen = Callable[..., Any]
NowFn = Callable[[], float]


def parse_traffic_sample(raw: bytes | str) -> dict[str, float] | None:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    text = text.strip()
    if not text:
        return None
    payload = json.loads(text)
    if not isinstance(payload, dict):
        return None
    up = payload.get("up")
    down = payload.get("down")
    if up is None or down is None:
        return None
    return {"up": float(up), "down": float(down)}


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch_from_iso(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except (OSError, OverflowError, ValueError):
        return None


class _RateBuckets:
    """Fixed slots keyed by wall-clock bucket number; gaps have no samples."""

    def __init__(self, seconds: int, limit: int) -> None:
        self.seconds = seconds
        self.limit = limit
        self.slots: list[dict[str, Any] | None] = [None] * limit
        self.first_seq: int | None = None
        self.last_seq: int | None = None

    def add(self, epoch: float, up: float, down: float) -> None:
        seq = math.floor(epoch / self.seconds)
        self.last_seq = seq if self.last_seq is None else max(self.last_seq, seq)
        if seq < self.last_seq - self.limit + 1:
            return  # A backward jump outside the retained window cannot evict it.
        self.first_seq = seq if self.first_seq is None else min(self.first_seq, seq)
        index = seq % self.limit
        bucket = self.slots[index]
        if bucket is None or bucket["seq"] != seq:
            bucket = {"seq": seq, "up_sum": 0.0, "down_sum": 0.0, "n": 0}
            self.slots[index] = bucket
        bucket["up_sum"] += up
        bucket["down_sum"] += down
        bucket["n"] += 1

    def rows(self) -> list[dict[str, Any]]:
        if self.last_seq is None or self.first_seq is None:
            return []
        first = max(self.first_seq, self.last_seq - self.limit + 1)
        result = []
        for seq in range(first, self.last_seq + 1):
            bucket = self.slots[seq % self.limit]
            n = bucket["n"] if bucket is not None and bucket["seq"] == seq else 0
            result.append({
                "seq": seq, "t_ms": seq * self.seconds * 1000, "n": n,
                "up_bps": bucket["up_sum"] / n if n else None,
                "down_bps": bucket["down_sum"] / n if n else None,
            })
        return result

    def dump(self) -> dict[str, Any]:
        return {"first_seq": self.first_seq, "last_seq": self.last_seq,
                "buckets": [dict(bucket) for bucket in self.slots if bucket is not None
                            and self.last_seq - self.limit < bucket["seq"] <= self.last_seq]}

    @classmethod
    def restore(cls, payload: Any, seconds: int, limit: int) -> _RateBuckets:
        result = cls(seconds, limit)
        first, last = payload["first_seq"], payload["last_seq"]
        buckets = payload["buckets"]
        if first is None and last is None and buckets == []:
            return result
        if (type(first) is not int or type(last) is not int or first < 0 or first > last
                or not isinstance(buckets, list) or len(buckets) > limit):
            raise ValueError("invalid traffic bucket metadata")
        result.first_seq, result.last_seq = first, last
        for bucket in buckets:
            seq, n = bucket["seq"], bucket["n"]
            up, down = float(bucket["up_sum"]), float(bucket["down_sum"])
            if (type(seq) is not int or type(n) is not int or n <= 0
                    or not max(first, last - limit + 1) <= seq <= last
                    or result.slots[seq % limit] is not None
                    or not all(math.isfinite(rate) and rate >= 0 for rate in (up, down))):
                raise ValueError("invalid traffic bucket metadata")
            result.slots[seq % limit] = {"seq": seq, "n": n, "up_sum": up, "down_sum": down}
        return result


class MihomoTrafficMonitor:
    def __init__(
        self,
        *,
        base_url: str,
        secret: str = "",
        timeout: float = 3.0,
        history_path: str | Path,
        enabled: bool = True,
        opener: UrlOpen = urlopen,
        now_fn: NowFn = time.time,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.timeout = float(timeout)
        self.history_path = Path(history_path)
        self.enabled = coerce_bool(enabled, True) and bool(self.base_url)
        self.opener = opener
        self.now_fn = now_fn
        self._lock = threading.RLock()
        self._save_lock = threading.RLock()
        self._snapshot_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._close_thread: threading.Thread | None = None
        self._finalize_thread: threading.Thread | None = None
        self._response: Any = None
        self._running = False
        self._error: str | None = None
        self._history: deque[dict[str, Any]] = deque(maxlen=TRAFFIC_RAW_HISTORY_LIMIT)
        self._history_epochs: deque[float] = deque(maxlen=TRAFFIC_RAW_HISTORY_LIMIT)
        self._live = _RateBuckets(1, 300)
        self._h24 = _RateBuckets(300, 288)
        self._latest: dict[str, Any] | None = None
        self._history_anchor_epoch: float | None = None
        self._history_version = 0
        self._session = {"upload_bytes": 0, "download_bytes": 0}
        self._last_sample_epoch: float | None = None
        self._last_history_save_epoch: float | None = None
        self._history_dirty = False
        self._load_history()

    def start(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._running = True
            self._error = None
            self._thread = threading.Thread(target=self._run, name="mihomo-traffic-monitor", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + max(0.0, float(timeout))

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        self._stop_event.set()
        response = self._response
        close = getattr(response, "close", None)
        if callable(close):
            close_thread = self._close_thread
            if close_thread is None or not close_thread.is_alive():
                def close_response() -> None:
                    try:
                        close()
                    except Exception:
                        pass

                close_thread = threading.Thread(
                    target=close_response,
                    name="mihomo-traffic-response-close",
                    daemon=True,
                )
                self._close_thread = close_thread
                close_thread.start()
            close_thread.join(timeout=min(0.1, remaining()))
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=remaining())

        def finalize_stop() -> None:
            with self._snapshot_lock, self._save_lock:
                with self._lock:
                    self._running = False
                    self._response = None
                    if self._thread is thread and (thread is None or not thread.is_alive()):
                        self._thread = None
                    self._last_sample_epoch = None
                self._flush_history_locked()

        finalize_thread = self._finalize_thread
        if finalize_thread is None or not finalize_thread.is_alive():
            finalize_thread = threading.Thread(
                target=finalize_stop,
                name="mihomo-traffic-stop-finalize",
                daemon=True,
            )
            self._finalize_thread = finalize_thread
            finalize_thread.start()
        finalize_thread.join(timeout=remaining())

    def record_sample(self, sample: dict[str, Any], *, timestamp_epoch: float | None = None) -> None:
        parsed = parse_traffic_sample(json.dumps(sample)) if not {"up", "down"} <= set(sample) else {
            "up": float(sample["up"]),
            "down": float(sample["down"]),
        }
        if not parsed:
            return
        self._record_rates(parsed["up"], parsed["down"], timestamp_epoch=timestamp_epoch)

    def snapshot(self) -> dict[str, Any]:
        # Legacy Qt callers historically observed finalized disk state together
        # with running=False. New lightweight readers never take the disk lock.
        with self._snapshot_lock, self._lock:
            now_epoch = self.now_fn()
            self._prune_history_locked(now_epoch=now_epoch)
            cutoff = now_epoch - TRAFFIC_HISTORY_SECONDS
            history = [dict(item) for epoch, item in zip(self._history_epochs, self._history)
                       if epoch >= cutoff]
            session = dict(self._session)
            latest = dict(history[-1]) if history else None
            return {
                "supported": self.enabled,
                "running": self._running,
                "latest": latest,
                "history": history,
                "session": session,
                "session_totals": dict(session),
                "error": self._error,
            }

    def _latest_locked(self) -> dict[str, Any]:
        return {"supported": self.enabled, "running": self._running, "error": self._error,
                "latest": dict(self._latest) if self._latest is not None else None}

    def latest(self) -> dict[str, Any]:
        """Return the last real sample and current status without copying history."""
        with self._lock:
            return self._latest_locked()

    def session(self) -> dict[str, int]:
        with self._lock:
            return dict(self._session)

    def tick_snapshot(self) -> dict[str, Any]:
        """Atomically read status, sample, monitor totals and five-minute watermark."""
        with self._lock:
            return {**self._latest_locked(), "session": dict(self._session),
                    "history_seq": self._h24.last_seq}

    def history_buckets(self) -> dict[str, Any]:
        """Return at most 300/288 rows, including gaps (n=0, rates=None).

        Windows end at the greatest observed sample time, never at read time.
        n counts samples; rates are arithmetic means, not byte totals.
        This monitor DTO needs a protocol adapter for empty rows in P2-25.
        """
        with self._lock:
            return {"source": "bucketed", "history_seq": self._h24.last_seq,
                    "live": self._live.rows(), "h24": self._h24.rows()}

    def _run(self) -> None:
        try:
            headers = {"Accept": "application/json"}
            if self.secret:
                headers["Authorization"] = f"Bearer {self.secret}"
            request = Request(f"{self.base_url}/traffic", headers=headers, method="GET")
            with self.opener(request, timeout=self.timeout) as response:
                with self._lock:
                    self._response = response
                for raw_line in response:
                    if self._stop_event.is_set():
                        break
                    try:
                        sample = parse_traffic_sample(raw_line)
                    except Exception as exc:
                        with self._lock:
                            self._error = str(exc)
                        continue
                    if sample:
                        self._record_rates(sample["up"], sample["down"])
        except Exception as exc:
            with self._lock:
                if not self._stop_event.is_set():
                    self._error = str(exc)
        finally:
            with self._snapshot_lock, self._save_lock:
                with self._lock:
                    self._running = False
                    self._response = None
                    self._last_sample_epoch = None
                self._flush_history_locked()

    def _record_rates(self, up: float, down: float, *, timestamp_epoch: float | None = None) -> None:
        # Keep the Qt snapshot's existing sample/save completion boundary without
        # blocking tick_snapshot/latest/session/history_buckets on disk I/O.
        with self._snapshot_lock:
            self._record_rates_serialized(up, down, timestamp_epoch=timestamp_epoch)

    def _record_rates_serialized(self, up: float, down: float, *, timestamp_epoch: float | None) -> None:
        timestamp_epoch = self.now_fn() if timestamp_epoch is None else float(timestamp_epoch)
        if timestamp_epoch < 0 or not all(math.isfinite(value) for value in (timestamp_epoch, up, down)):
            return
        row = {"timestamp": _iso_from_epoch(timestamp_epoch),
               "up": round(float(up), 2), "down": round(float(down), 2)}
        with self._lock:
            if self._last_sample_epoch is not None:
                elapsed = min(max(0.0, timestamp_epoch - self._last_sample_epoch), TRAFFIC_SAMPLE_MAX_ELAPSED_SECONDS)
                self._session["upload_bytes"] += int(max(0.0, up) * elapsed)
                self._session["download_bytes"] += int(max(0.0, down) * elapsed)
            self._last_sample_epoch = timestamp_epoch
            self._latest = row
            self._history.append(row)
            self._history_epochs.append(timestamp_epoch)
            self._live.add(timestamp_epoch, max(0.0, row["up"]), max(0.0, row["down"]))
            self._h24.add(timestamp_epoch, max(0.0, row["up"]), max(0.0, row["down"]))
            self._history_anchor_epoch = (timestamp_epoch if self._history_anchor_epoch is None
                                          else max(self._history_anchor_epoch, timestamp_epoch))
            self._error = None
            self._prune_history_locked(now_epoch=timestamp_epoch)
            self._history_dirty = True
            self._history_version += 1
            should_save = self._should_save_history_locked(timestamp_epoch)
        if should_save:
            self._save_history_locked(saved_at_epoch=timestamp_epoch)

    def _load_history(self) -> None:
        try:
            if not self.history_path.exists():
                return
            payload = json.loads(self.history_path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                cutoff = self.now_fn() - TRAFFIC_HISTORY_SECONDS
                metadata = payload[-1].get(_BUCKET_METADATA_KEY) if payload and isinstance(payload[-1], dict) else None
                with self._lock:
                    # Legacy files are scanned once, including rows beyond the raw cap.
                    for item in payload:
                        if not isinstance(item, dict):
                            continue
                        epoch = _epoch_from_iso(item.get("timestamp"))
                        try:
                            up, down = float(item["up"]), float(item["down"])
                        except (KeyError, TypeError, ValueError, OverflowError):
                            continue
                        if (epoch is None or epoch < max(0, cutoff)
                                or not all(math.isfinite(value) for value in (epoch, up, down))):
                            continue
                        row = {"timestamp": item["timestamp"], "up": up, "down": down}
                        self._history.append(row)
                        self._history_epochs.append(epoch)
                        self._latest = row
                        self._live.add(epoch, max(0.0, row["up"]), max(0.0, row["down"]))
                        self._h24.add(epoch, max(0.0, row["up"]), max(0.0, row["down"]))
                        self._history_anchor_epoch = (epoch if self._history_anchor_epoch is None
                                                      else max(self._history_anchor_epoch, epoch))
                    # Additive metadata keeps full aggregate counts even if raw rows
                    # were capped. Older readers still see a list of ordinary rows.
                    if isinstance(metadata, dict):
                        try:
                            anchor = float(metadata["anchor_epoch"])
                            live = _RateBuckets.restore(metadata["live"], 1, 300)
                            h24 = _RateBuckets.restore(metadata["h24"], 300, 288)
                            latest = metadata["latest"]
                            latest_epoch = _epoch_from_iso(latest["timestamp"])
                            if (not math.isfinite(anchor) or anchor < max(0, cutoff)
                                    or latest_epoch is None or not 0 <= latest_epoch <= anchor
                                    or live.last_seq != math.floor(anchor)
                                    or h24.last_seq != math.floor(anchor / 300)
                                    or not all(math.isfinite(float(latest[key]))
                                               for key in ("up", "down"))):
                                raise ValueError("invalid traffic bucket metadata")
                            self._live, self._h24 = live, h24
                            self._history_anchor_epoch = anchor
                            self._latest = {key: latest[key] for key in ("timestamp", "up", "down")}
                        except (KeyError, TypeError, ValueError, OverflowError):
                            pass  # Invalid/expired extension falls back to legacy rows.
        except Exception as exc:
            with self._lock:
                self._error = str(exc)

    def _should_save_history_locked(self, timestamp_epoch: float) -> bool:
        return (
            self._history_dirty
            and (
                self._last_history_save_epoch is None
                or timestamp_epoch < self._last_history_save_epoch
                or timestamp_epoch - self._last_history_save_epoch >= TRAFFIC_HISTORY_SAVE_INTERVAL_SECONDS
            )
        )

    def _flush_history_locked(self) -> None:
        # Historical method name retained; callers must not hold the monitor lock.
        with self._lock:
            dirty = self._history_dirty
        if dirty:
            self._save_history_locked(saved_at_epoch=self.now_fn())

    def _save_history_locked(self, *, saved_at_epoch: float | None = None) -> None:
        # Serialize writers before taking the snapshot so an older writer cannot
        # replace a newer file. Disk I/O never holds the monitor's sample lock.
        with self._save_lock:
            with self._lock:
                if not self._history_dirty:
                    return
                history = [dict(item) for item in self._history]
                if history:
                    history[-1][_BUCKET_METADATA_KEY] = {
                        "anchor_epoch": self._history_anchor_epoch, "latest": dict(self._latest),
                        "live": self._live.dump(), "h24": self._h24.dump(),
                    }
                version = self._history_version
            self._write_history(history, version, saved_at_epoch=saved_at_epoch)

    def _write_history(self, history: list[dict[str, Any]], version: int,
                       *, saved_at_epoch: float | None) -> None:
        temp_path: Path | None = None
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.history_path.parent,
                delete=False,
                prefix=f".{self.history_path.name}.",
                suffix=".tmp",
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(json.dumps(history, ensure_ascii=False, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.history_path)
            with self._lock:
                self._history_dirty = self._history_version != version
                self._last_history_save_epoch = self.now_fn() if saved_at_epoch is None else float(saved_at_epoch)
        except Exception as exc:
            with self._lock:
                self._error = str(exc)
        finally:
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def _prune_history_locked(self, *, now_epoch: float | None = None) -> None:
        now_epoch = self.now_fn() if now_epoch is None else now_epoch
        cutoff = max(now_epoch, self._history_anchor_epoch or 0) - TRAFFIC_HISTORY_SECONDS
        # Fixed budget even for a day-long forward jump. The legacy snapshot
        # filters any remaining expired rows; maxlen bounds memory and disk copies.
        for _ in range(TRAFFIC_HISTORY_PRUNE_BUDGET):
            if not self._history_epochs or self._history_epochs[0] >= cutoff:
                break
            self._history_epochs.popleft()
            self._history.popleft()
