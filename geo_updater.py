"""Background updater for Mihomo geoip / geosite / mmdb assets.

Clash Verge ships with a scheduled downloader for GeoData; this module
offers the same capability without touching existing manager/scheduler
logic. A single ``GeoUpdater`` instance owns:

- the configuration block (enabled flag, interval, URLs)
- the target download directory (Mihomo ``work_dir``)
- a small JSON metadata file that records the last successful update per
  asset so restarts don't trigger a re-download storm

All writes are atomic (temp file + ``os.replace``). Failures are logged
and recorded in the metadata file but never raised to callers, so a
broken mirror can never crash the host runtime.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from clash_formatter import DEFAULT_GEOX_URL
from config_utils import coerce_bool, coerce_float, coerce_port
from models import utc_now_iso
from log_event_reports import log_event, log_raw_diagnostic


DEFAULT_INTERVAL_HOURS = 24
MIN_INTERVAL_HOURS = 1
MAX_ASSET_BYTES = 200 * 1024 * 1024  # Safety cap; real geoip.dat < 50 MB.
ASSET_KEYS = ("geoip", "geosite", "mmdb", "asn")
ASSET_FILENAMES = {
    "geoip": "geoip.dat",
    "geosite": "geosite.dat",
    "mmdb": "country.mmdb",
    "asn": "asn.mmdb",
}
_RAW_ONLY_LOG_EXTRA = {
    "category": "update",
    "is_key_event": False,
    "event_match_kind": "raw_only",
}


class GeoDownloadCancelled(RuntimeError):
    """The caller withdrew the download (the runtime is stopping)."""


@dataclass(frozen=True, slots=True)
class GeoDownloadPolicy:
    """How to download over a route that leaves through another proxy per connection.

    Without a policy a download is one connection, and whichever proxy it got
    holds it for as long as that proxy likes (11-06: one pool member held the
    first-run download for 92 s while fifteen others were idle).  Public
    proxies differ by more than tenfold in what they carry and many answer
    nothing at all, so a connection that delivers is never given up for one
    that only might.  With a policy the download is a race:

    * the *lead* is the connection the download relies on.  It is left alone
      while it carries ``comfort_rate`` or more;
    * while it carries less (measured over ``window`` seconds), or has not
      answered within ``stagger`` seconds, one *rival* connection is opened
      beside it, through the next proxy.  Whichever finishes first is the
      download;
    * a rival that answered but is not ``rival_margin`` times faster than the
      lead is closed after its own ``window``; one that has caught up with the
      lead's bytes replaces it.  After ``max_plain_rivals`` rivals that were no
      faster (a slow link, not a slow proxy) no more are opened;
    * at most ``max_attempts`` connections are opened per asset; after one
      that never answered the next waits ``retry_pause`` seconds (longer after
      several in a row).
    """

    comfort_rate: float = 1024 * 1024.0
    window: float = 4.0
    stagger: float = 2.5
    rival_margin: float = 1.25
    max_plain_rivals: int = 3
    max_attempts: int = 16
    operation_timeout: float = 12.0
    retry_pause: float = 0.3
    poll: float = 0.2


class _Transfer:
    """One connection's download into its own temporary file."""

    def __init__(self, run: Callable[["_Transfer"], tuple[str, int]], name: str, started_at: float) -> None:
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.error: BaseException | None = None
        self.path = ""
        self.size = 0
        self.started_at = started_at
        self.answered_at: float | None = None
        self.received = 0
        self._samples: deque[tuple[float, int]] = deque()
        self._run = run
        self._thread = threading.Thread(target=self._work, name=name, daemon=True)
        self._thread.start()

    def _work(self) -> None:
        try:
            self.path, self.size = self._run(self)
            if self.cancel.is_set():
                # Finished after the race was decided: nobody will take it.
                self.discard()
                self.error = GeoDownloadCancelled("GeoData download cancelled")
        except BaseException as exc:  # noqa: BLE001 - reported to the race
            self.error = exc
        finally:
            self.done.set()

    def discard(self) -> None:
        """Stop the connection and remove whatever it has written."""
        self.cancel.set()
        path, self.path = self.path, ""
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

    def sample(self, now: float, window: float) -> None:
        samples = self._samples
        samples.append((now, self.received))
        # Keep the newest sample that is at least one window old in front.
        while len(samples) > 1 and now - samples[1][0] >= window:
            samples.popleft()

    def rate(self, now: float) -> float:
        """Bytes per second over the trailing window (0 until it has data)."""
        if self.answered_at is None or not self._samples:
            return 0.0
        since, counted = self._samples[0]
        since = max(since, self.answered_at)
        span = now - since
        if span <= 0.2:
            return 0.0
        return max(0, self.received - counted) / span


class GeoUpdater:
    """Periodically downloads GeoIP / GeoSite / MMDB files for Mihomo."""

    def __init__(
        self,
        config: dict[str, Any] | None,
        work_dir: str | Path,
        metadata_path: str | Path,
        *,
        egress_proxy: dict[str, Any] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.logger = logger or logging.getLogger("proxy_manager.geo")
        self._config_lock = threading.RLock()
        self._action_lock = threading.Lock()
        self._running = False
        self._last_attempt_monotonic = 0.0
        # Transfer timing, replaceable in tests.
        self._clock: Callable[[], float] = time.monotonic
        self._sleep: Callable[[float], None] = time.sleep
        self.work_dir = Path(work_dir)
        self.metadata_path = Path(metadata_path)
        self.egress_proxy = egress_proxy if isinstance(egress_proxy, dict) else {}
        self._apply_config(config or {})
        self._metadata: dict[str, Any] = self._load_metadata()

    def reconfigure(
        self,
        config: dict[str, Any] | None,
        *,
        egress_proxy: dict[str, Any] | None = None,
    ) -> None:
        with self._config_lock:
            self._apply_config(config or {})
            if egress_proxy is not None:
                self.egress_proxy = egress_proxy if isinstance(egress_proxy, dict) else {}

    def _apply_config(self, config: dict[str, Any]) -> None:
        enabled = config.get("enabled", True)
        interval = coerce_float(
            config.get("interval_hours", DEFAULT_INTERVAL_HOURS),
            DEFAULT_INTERVAL_HOURS,
            minimum=MIN_INTERVAL_HOURS,
        )
        request_timeout = coerce_float(config.get("request_timeout", 30), 30.0, minimum=0.1)
        urls_raw = config.get("geox_url") or config.get("geox-url") or {}
        urls: dict[str, str] = {}
        if isinstance(urls_raw, dict):
            for key in ASSET_KEYS:
                raw_value = urls_raw.get(key) or urls_raw.get(key.replace("_", "-"))
                if raw_value:
                    urls[key] = str(raw_value).strip()
        # Fall back to Mihomo defaults for geoip/geosite so the update is still
        # meaningful when the user hasn't customised the config.
        for key in ("geoip", "geosite"):
            urls.setdefault(key, DEFAULT_GEOX_URL.get(key, ""))
        if coerce_bool(config.get("include_mmdb"), False):
            urls.setdefault("mmdb", DEFAULT_GEOX_URL.get("mmdb", ""))
        self.config = {
            "enabled": coerce_bool(enabled, True),
            "interval_hours": interval,
            "request_timeout": request_timeout,
            "urls": urls,
            "user_agent": str(config.get("user_agent") or "Mozilla/5.0 (Tesseract GeoUpdater)"),
            "allow_insecure_http": coerce_bool(config.get("allow_insecure_http"), False),
        }

    # ------------------------------------------------------------------ state

    def status(self) -> dict[str, Any]:
        with self._config_lock:
            return {
                "enabled": bool(self.config["enabled"]),
                "interval_hours": float(self.config["interval_hours"]),
                "work_dir": str(self.work_dir),
                "urls": dict(self.config["urls"]),
                "running": self._running,
                "last_update": dict(self._metadata.get("last_update", {})),
                "errors": dict(self._metadata.get("errors", {})),
                "bytes": dict(self._metadata.get("bytes", {})),
            }

    def is_due(self, now_monotonic: float | None = None) -> bool:
        with self._config_lock:
            if not self.config["enabled"]:
                return False
            interval_seconds = float(self.config["interval_hours"]) * 3600.0
            last = float(self._metadata.get("last_success_monotonic", 0.0))
            wall_last = self._metadata.get("last_success_wall")
            if wall_last:
                try:
                    wall = float(wall_last)
                    if wall:
                        return (time.time() - wall) >= interval_seconds
                except (TypeError, ValueError):
                    pass
            now = now_monotonic if now_monotonic is not None else time.monotonic()
            if last and (now - last) < interval_seconds:
                return False
            return True

    # ------------------------------------------------------------------ tick

    def tick(self, now_monotonic: float | None = None) -> None:
        """Run update when due; safe to call from an always-on loop."""
        if not self.is_due(now_monotonic):
            return
        # Even if due, back off to avoid a tight retry loop on repeated failure.
        now = now_monotonic if now_monotonic is not None else time.monotonic()
        if (now - self._last_attempt_monotonic) < 60.0 and self._last_attempt_monotonic:
            return
        self._last_attempt_monotonic = now
        if not self._action_lock.acquire(blocking=False):
            return
        thread = threading.Thread(
            target=self._background_update,
            name="geo-updater-tick",
            daemon=True,
        )
        thread.start()

    def _background_update(self) -> None:
        try:
            self.update_all()
        finally:
            self._action_lock.release()

    def update_now(
        self,
        *,
        force: bool = True,
        policy: GeoDownloadPolicy | None = None,
        cancel: Callable[[], bool] | None = None,
        assets: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Synchronously update. Returns status dict.

        ``policy`` retries each asset over a route that changes per
        connection, ``cancel`` is polled between reads and ends the update at
        once, ``assets`` limits it to the named assets.
        """
        if not force and not self.is_due():
            return self.status()
        acquired = self._action_lock.acquire(blocking=True, timeout=60.0)
        if not acquired:
            return self.status()
        try:
            self.update_all(policy=policy, cancel=cancel, assets=assets)
        finally:
            self._action_lock.release()
        return self.status()

    # ----------------------------------------------------------------- download

    def update_all(
        self,
        *,
        policy: GeoDownloadPolicy | None = None,
        cancel: Callable[[], bool] | None = None,
        assets: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        wanted = None if assets is None else {str(item) for item in assets}
        cancelled = False
        with self._config_lock:
            if not self.config["enabled"]:
                return self.status()
            urls = dict(self.config["urls"])
            timeout = float(self.config["request_timeout"])
            user_agent = str(self.config["user_agent"])
        self._running = True
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log_event(
                self.logger,
                "ERROR",
                "update.geodata_failed",
                f"GeoUpdater could not create work_dir {self.work_dir}: {exc}",
                params={"reason": str(exc)},
            )
            self._running = False
            return self.status()

        last_update = dict(self._metadata.get("last_update", {}))
        byte_counts = dict(self._metadata.get("bytes", {}))
        errors = dict(self._metadata.get("errors", {}))
        any_success = False
        for key, url in urls.items():
            if not url or (wanted is not None and key not in wanted):
                continue
            filename = ASSET_FILENAMES.get(key, f"{key}.dat")
            target = self.work_dir / filename
            self.logger.info("GeoUpdater downloading %s -> %s", url, target)
            try:
                if policy is None:
                    size = self._download_file(
                        key, url, target, timeout=timeout, user_agent=user_agent, cancel=cancel,
                    )
                else:
                    size = self._download_with_policy(
                        key, url, target, policy=policy, user_agent=user_agent, cancel=cancel,
                    )
            except GeoDownloadCancelled as exc:
                # The caller is stopping: no further asset, and not a failure
                # of the mirror or the route.
                errors[key] = f"{type(exc).__name__}: {exc}"
                cancelled = True
                self.logger.info("GeoUpdater stopped before %s finished", key, extra=_RAW_ONLY_LOG_EXTRA)
                break
            except Exception as exc:
                errors[key] = f"{type(exc).__name__}: {exc}"
                self.logger.warning(
                    "GeoUpdater failed %s: %s",
                    key,
                    exc,
                    extra=_RAW_ONLY_LOG_EXTRA,
                )
                continue
            last_update[key] = utc_now_iso()
            byte_counts[key] = size
            errors.pop(key, None)
            any_success = True

        self._metadata["last_update"] = last_update
        self._metadata["bytes"] = byte_counts
        self._metadata["errors"] = errors
        if any_success:
            self._metadata["last_success_monotonic"] = time.monotonic()
            self._metadata["last_success_wall"] = time.time()
        self._save_metadata()
        self._running = False
        if cancelled:
            return self.status()
        if errors:
            log_event(
                self.logger,
                "ERROR",
                "update.geodata_failed",
                "GeoData update finished with "
                f"{len(errors)} failed asset(s) and {len(last_update)} available asset(s)",
                params={
                    "reason": "; ".join(
                        f"{key}: {error}" for key, error in sorted(errors.items())
                    ),
                },
            )
        elif any_success:
            log_event(
                self.logger,
                "INFO",
                "update.geodata_completed",
                f"GeoData update completed for {len(last_update)} asset(s)",
            )
        return self.status()

    # ---------------------------------------------------------------- internals

    def _opener(self) -> urllib.request.OpenerDirector:
        if not self.egress_proxy or not coerce_bool(self.egress_proxy.get("enabled"), False):
            raise RuntimeError(
                "GeoData refresh requires a routed proxy; host-default direct access is disabled"
            )
        scheme = str(self.egress_proxy.get("scheme", "http")).lower()
        server = str(self.egress_proxy.get("server", "")).strip()
        port = coerce_port(self.egress_proxy.get("port"), 0)
        if not server or not port:
            raise RuntimeError(
                "GeoData refresh requires a valid routed proxy; host-default direct access is disabled"
            )
        username = str(self.egress_proxy.get("username", ""))
        password = str(self.egress_proxy.get("password", ""))
        auth = ""
        if username:
            auth = f"{urllib.parse.quote(username, safe='')}:{urllib.parse.quote(password, safe='')}@"
        proxy_url = f"{scheme}://{auth}{server}:{port}"
        proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        return urllib.request.build_opener(proxy_handler)

    def _require_allowed_url(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        with self._config_lock:
            allow_insecure_http = coerce_bool(self.config.get("allow_insecure_http"), False)
        if parsed.scheme.lower() != "https" and not allow_insecure_http:
            raise ValueError(f"refusing non-HTTPS GeoData URL: {url}")

    def _download_with_policy(
        self,
        key: str,
        url: str,
        target: Path,
        *,
        policy: GeoDownloadPolicy,
        user_agent: str,
        cancel: Callable[[], bool] | None,
    ) -> int:
        """Race connections for one asset as ``policy`` says; see its docstring.

        Returns the size of the file now at ``target``; raises the last
        connection error when every allowed connection has ended without one.
        """

        # Not the route's fault and the same on every connection: say so once.
        self._require_allowed_url(url)
        self._opener()
        self._clear_stale_parts(target)
        clock = self._clock
        opened = 0
        plain_rivals = 0
        unanswered_in_a_row = 0
        next_open_at = clock()
        last_error: BaseException | None = None
        lead: _Transfer | None = None
        rival: _Transfer | None = None
        every: list[_Transfer] = []

        def open_connection() -> _Transfer:
            nonlocal opened
            opened += 1
            transfer = _Transfer(
                lambda progress: self._fetch_to_temp(
                    key,
                    url,
                    target,
                    timeout=float(policy.operation_timeout),
                    user_agent=user_agent,
                    cancel=progress.cancel.is_set,
                    progress=progress,
                ),
                f"geo-transfer-{key}-{opened}",
                clock(),
            )
            every.append(transfer)
            return transfer

        def ended(transfer: _Transfer, role: str) -> None:
            """Note a connection that ended without the file."""
            nonlocal last_error, unanswered_in_a_row, next_open_at
            last_error = transfer.error
            if transfer.answered_at is None:
                unanswered_in_a_row += 1
                next_open_at = clock() + max(0.0, float(policy.retry_pause)) * min(unanswered_in_a_row, 4)
            else:
                unanswered_in_a_row = 0
            self.logger.info(
                "GeoUpdater %s connection (%s) ended after %d KiB: %s: %s",
                key,
                role,
                transfer.received // 1024,
                type(transfer.error).__name__,
                transfer.error,
                extra=_RAW_ONLY_LOG_EXTRA,
            )

        try:
            while True:
                if cancel is not None and cancel():
                    raise GeoDownloadCancelled("GeoData download cancelled")
                now = clock()
                for transfer in (lead, rival):
                    if transfer is not None and transfer.done.is_set() and transfer.error is None:
                        os.replace(transfer.path, target)
                        transfer.path = ""
                        if transfer is rival:
                            took = max(0.001, now - (transfer.answered_at or now))
                            self.logger.info(
                                "GeoUpdater %s finished on the second connection (%d KiB/s)",
                                key,
                                int(transfer.size / took // 1024),
                                extra=_RAW_ONLY_LOG_EXTRA,
                            )
                        return transfer.size
                if rival is not None and rival.done.is_set():
                    ended(rival, "rival")
                    rival = None
                if lead is not None and lead.done.is_set():
                    ended(lead, "lead")
                    lead, rival = rival, None
                for transfer in (lead, rival):
                    if transfer is not None:
                        transfer.sample(now, float(policy.window))

                can_open = opened < int(policy.max_attempts) and now >= next_open_at
                if lead is None:
                    if opened >= int(policy.max_attempts):
                        raise last_error or RuntimeError(f"no connection for {url}")
                    if can_open:
                        lead = open_connection()
                elif rival is None:
                    if lead.answered_at is None:
                        # Dead proxies take seconds to fail: do not wait for each in turn.
                        if can_open and now - lead.started_at >= float(policy.stagger):
                            rival = open_connection()
                    elif (
                        can_open
                        and plain_rivals < int(policy.max_plain_rivals)
                        and now - lead.answered_at >= float(policy.window)
                        and lead.rate(now) < float(policy.comfort_rate)
                    ):
                        self.logger.info(
                            "GeoUpdater %s runs at %d KiB/s; opening a second connection through another proxy",
                            key,
                            int(lead.rate(now) // 1024),
                            extra=_RAW_ONLY_LOG_EXTRA,
                        )
                        rival = open_connection()
                elif rival.answered_at is not None:
                    if lead.answered_at is None:
                        # The later connection answered first: it leads now.
                        lead, rival = rival, lead
                    elif (
                        rival.received > 0
                        and rival.received >= lead.received
                        and now - rival.answered_at >= 1.0
                    ):
                        # Caught up from behind, so it is the faster one.
                        self.logger.info(
                            "GeoUpdater %s continues on the faster connection (%d KiB/s, was %d KiB/s)",
                            key,
                            int(rival.rate(now) // 1024),
                            int(lead.rate(now) // 1024),
                            extra=_RAW_ONLY_LOG_EXTRA,
                        )
                        lead.discard()
                        lead, rival = rival, None
                    elif (
                        now - rival.answered_at >= float(policy.window)
                        and rival.rate(now) < lead.rate(now) * float(policy.rival_margin)
                    ):
                        # No faster than what is already running: it only
                        # shares the line.
                        rival.discard()
                        rival = None
                        plain_rivals += 1
                self._sleep(max(0.01, float(policy.poll)))
        finally:
            for transfer in every:
                transfer.discard()
            if not (cancel is not None and cancel()):
                # A closed connection notices at its next read and removes its
                # own file; give the usual case a moment to be tidy.  One that
                # is silent ends at its socket timeout, see _clear_stale_parts.
                deadline = clock() + 0.5
                for transfer in every:
                    transfer.done.wait(max(0.0, deadline - clock()))

    @staticmethod
    def _clear_stale_parts(target: Path, *, older_than: float = 900.0) -> None:
        """Remove partial files an interrupted earlier download left behind."""
        try:
            candidates = list(target.parent.glob(f".{target.name}.*.tmp"))
        except OSError:
            return
        horizon = time.time() - older_than
        for path in candidates:
            try:
                if path.stat().st_mtime < horizon:
                    path.unlink()
            except OSError:
                continue

    def _download_file(
        self,
        key: str,
        url: str,
        target: Path,
        *,
        timeout: float,
        user_agent: str,
        cancel: Callable[[], bool] | None = None,
    ) -> int:
        temp_path, total = self._fetch_to_temp(
            key, url, target, timeout=timeout, user_agent=user_agent, cancel=cancel,
        )
        try:
            os.replace(temp_path, target)
        except BaseException:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
        return total

    def _fetch_to_temp(
        self,
        key: str,
        url: str,
        target: Path,
        *,
        timeout: float,
        user_agent: str,
        cancel: Callable[[], bool] | None = None,
        progress: _Transfer | None = None,
    ) -> tuple[str, int]:
        """Download ``url`` into a validated temporary file beside ``target``.

        Returns its path and size; the caller moves it into place.  On any
        failure the temporary file is removed.
        """
        self._require_allowed_url(url)
        opener = self._opener()
        request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "*/*"})
        temp_path = ""
        total = 0
        expected_bytes = 0
        finished = False
        try:
            with opener.open(request, timeout=timeout) as response:
                if progress is not None:
                    progress.answered_at = self._clock()
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        expected_bytes = int(content_length)
                    except ValueError:
                        expected_bytes = 0
                    if expected_bytes > MAX_ASSET_BYTES:
                        raise ValueError(f"asset from {url} declares {expected_bytes} bytes, above {MAX_ASSET_BYTES} cap")
                # A raced connection reads whatever has arrived (read1), so its
                # progress is known as the bytes come in and a close is noticed
                # at once; a full 64 KiB read would sit on a trickling proxy
                # for a minute first.
                read = (getattr(response, "read1", None) if progress is not None else None) or response.read
                with tempfile.NamedTemporaryFile(
                    "wb",
                    dir=target.parent,
                    prefix=f".{target.name}.",
                    suffix=".tmp",
                    delete=False,
                    **({"buffering": 1024 * 1024} if progress is not None else {}),
                ) as handle:
                    temp_path = handle.name
                    while True:
                        if cancel is not None and cancel():
                            raise GeoDownloadCancelled("GeoData download cancelled")
                        chunk = read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_ASSET_BYTES:
                            raise ValueError(f"asset from {url} exceeds {MAX_ASSET_BYTES} byte safety cap")
                        handle.write(chunk)
                        if progress is not None:
                            progress.received = total
                    handle.flush()
                    os.fsync(handle.fileno())
            if total == 0:
                raise ValueError(f"empty response from {url}")
            if expected_bytes and total != expected_bytes:
                # http.client ends a sized read at a closed connection without
                # an error; a cut-off file must never replace a good one.
                raise ValueError(
                    f"incomplete response from {url}: {total} of {expected_bytes} bytes"
                )
            self._validate_asset_file(key, Path(temp_path), total)
            finished = True
            return temp_path, total
        finally:
            if not finished and temp_path and Path(temp_path).exists():
                try:
                    Path(temp_path).unlink()
                except OSError:
                    pass

    def _validate_asset_file(self, key: str, path: Path, total_bytes: int) -> None:
        with path.open("rb") as handle:
            head = handle.read(512)
            stripped = head.lstrip()
            if stripped.startswith((b"<", b"{", b"[")):
                raise ValueError(f"{key} download looks like text/HTML, not GeoData")
            if key in {"mmdb", "asn"}:
                handle.seek(max(0, total_bytes - 131072))
                tail = handle.read()
                if b"\xab\xcd\xefMaxMind.com" not in tail:
                    raise ValueError(f"{key} download is missing MaxMind DB metadata marker")

    def _load_metadata(self) -> dict[str, Any]:
        try:
            if not self.metadata_path.exists():
                return {"last_update": {}, "bytes": {}, "errors": {}}
            raw = self.metadata_path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict):
                return {"last_update": {}, "bytes": {}, "errors": {}}
            data.setdefault("last_update", {})
            data.setdefault("bytes", {})
            data.setdefault("errors", {})
            return data
        except Exception as exc:
            log_raw_diagnostic(
                self.logger,
                "warning",
                "GeoUpdater failed to load metadata %s: %s",
                self.metadata_path,
                exc,
            )
            return {"last_update": {}, "bytes": {}, "errors": {}}

    def _save_metadata(self) -> None:
        try:
            self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = ""
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    dir=self.metadata_path.parent,
                    prefix=f".{self.metadata_path.name}.",
                    suffix=".tmp",
                    encoding="utf-8",
                    delete=False,
                ) as handle:
                    temp_path = handle.name
                    json.dump(self._metadata, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self.metadata_path)
                temp_path = ""
            finally:
                if temp_path and Path(temp_path).exists():
                    try:
                        Path(temp_path).unlink()
                    except OSError:
                        pass
        except Exception as exc:
            log_raw_diagnostic(
                self.logger,
                "warning",
                "GeoUpdater failed to save metadata %s: %s",
                self.metadata_path,
                exc,
            )
