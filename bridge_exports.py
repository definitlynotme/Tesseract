"""P2-19: shell save grants, the diagnostic bundle and filtered log export.

Contract: PROTOCOL §10 (``shell.saveGrant``, ``logs.exportBundle``,
``logs.exportFiltered``), §13 (save grants) and §7 (privacy).

* The page only ever holds an opaque ``save_ref``.  The shell registers it with
  ``shell.saveGrant`` together with a duplicated, already approved file handle.
  The sidecar resolves the ref through that grant table and writes only through
  the granted handle; it never accepts or invents a path, never falls back to a
  default location.  A grant is bound to (epoch, owner, purpose, format),
  expires after at most five minutes and is consumed once.
* Every text inside an export passes the existing privacy processing
  (``diagnostic_bundle``'s redactor for the bundle, the producer-side log
  redaction for log records) and then the known-secret registry, including
  common single-layer encodings.  The finished bytes are re-checked against the
  registry before anything reaches the granted handle; a hit fails the job.
* Runtime access goes through the ports below.  Facts the runtime does not
  expose yet are declared on the ports; their adapters report "unknown" and the
  exports fail closed instead of guessing.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol
from urllib.parse import quote, quote_plus

import yaml

from bridge_jobs import JobFailure
from bridge_redact import MAX_TEXT, REDACTED, KnownSecrets, redact_text
from diagnostic_bundle import (
    DiagnosticAttachment,
    DiagnosticBundleInputs,
    default_bundle_name,
    state_file_candidates,
    write_diagnostic_bundle,
)
from log_event_reports import build_log_event_report


GRANT_TTL_MAX_MS = 5 * 60 * 1000
GRANT_CAPACITY = 32
GRANT_OWNER_CAPACITY = 8
USED_REF_MEMORY = 256
# PROTOCOL §16: every wire array holds at most 4000 items (bridge_protocol
# _schema rejects longer ``[uint]`` values, and adapt_result would turn an
# already written export into failed/internal).  Both bounds follow it.
MISSING_SEQS_MAX = 4000
SELECTED_SEQS_MAX = 4000
EXTRA_LOG_TAIL_BYTES = 2 * 1024 * 1024
WRITE_CHUNK = 64 * 1024
# Known-secret scans run in windows of this many characters so a single
# regex call never holds the GIL for long (the 15 s pong watchdog, §2).
SCAN_CHUNK = 16 * 1024
# Bundle drafts live in private temporary directories with this prefix; a
# crash between write and removal leaves one behind, swept at registration.
DRAFT_PREFIX = "tesseract-export-"
DRAFT_STALE_SECONDS = 600
EXPORT_COMMANDS = frozenset({"logs.exportBundle", "logs.exportFiltered"})
# Integer document fields that are structure, never content: the registry
# pass leaves them as numbers even when a low-entropy registered value has
# the same spelling (text/csv output still masks such spellings as text).
STRUCTURAL_INT_KEYS = frozenset({
    "seq", "ts_ms", "rx_ms", "watermark", "exported_at_ms", "snapshot_at_ms",
    "count", "missing_seqs", "produced_at_ms", "rev", "bytes", "source_bytes",
    "min_seq", "latest_seq", "retained", "from_seq", "to_seq", "view_clear_seq",
    "evicted_through_seq",
})
PURPOSE_FORMATS: Mapping[str, frozenset[str]] = {
    "bundle": frozenset({"zip"}),
    "filtered": frozenset({"json", "text", "csv"}),
}
# Unkeyed state topics whose latest published snapshot describes the runtime.
STATE_TOPICS = (
    "service.state", "route.published", "tiers.summary", "core.info",
    "service.collect", "network.mode", "killswitch.state", "bastion.pool",
    "gateway.current", "protocols.mix", "logs.feed", "geo.status",
    "update.state", "exit.identity", "ui.language",
)
# Written by the shell (``data/logs/shell.log``) and the sidecar
# (``data/logs/sidecar-stderr.log``); both roll over to ``<name>.1``.
EXTRA_LOG_FAMILIES = ("shell.log", "sidecar-stderr.log")
_LOG_FIELDS = ("seq", "ts_ms", "rx_ms", "source", "level", "category", "key",
               "title", "detail", "raw")
_CSV_COLUMNS = ("seq", "time", "source", "level", "category", "key", "title",
                "detail", "raw")
_CATEGORY = re.compile(r"[a-z][a-z0-9_]{0,31}")
_MARKER = re.compile(r"\[redacted:[A-Za-z0-9_]{1,40}\]|\[REDACTED\]")
# Local diagnostics only: fixed codes and exception class names, never text.
_LOG = logging.getLogger("tesseract.bridge.exports")


def _wall_ms() -> int:
    return time.time_ns() // 1_000_000


# --------------------------------------------------------------------- ports
class JobIdentityPort(Protocol):
    """(epoch, owner) of the executor request ``op``; None when unknown.

    Public getter ``JobExecutor.job_identity(op)`` (bridge_jobs, T-P2-31a).
    """

    def job_identity(self, op: str) -> tuple[str, str] | None: ...


@dataclass(frozen=True)
class TerminalJob:
    """One job that reached a terminal state (from the executor listener)."""

    name: str
    args: Mapping[str, Any]
    epoch: str | None
    owner: str | None
    state: str


class JobOutcomePort(Protocol):
    """Terminal-state notifications, so a queued-cancelled export frees its grant.

    The executor finishes a queued ``logs.export*`` job as ``cancelled``
    without ever calling the handler.  Public API:
    ``JobExecutor.add_terminal_listener(listener)`` (bridge_jobs, T-P2-31a).
    ``subscribe`` returns False for an executor that does not offer it.
    """

    def subscribe(self, callback: Callable[[TerminalJob], None]) -> bool: ...


class SaveTarget(Protocol):
    """One granted, already opened target (P3-06 supplies the real handle).

    Write-target contract (决定_P3-06裁定 S-R1, PROTOCOL §13): writes start at
    offset 0 of the target; ``commit`` sets the end of file at the written
    length (SetEndOfFile) so an overwritten longer file keeps no stale tail;
    ``discard`` only closes and never truncates an existing file (the shell
    deletes a file its own grant created).
    """

    def write(self, data: bytes) -> None: ...

    def commit(self) -> int | None:
        """Flush, set the end of file, persist and close; return the size (or None)."""

    def discard(self) -> None:
        """Close without truncating; never raise on reuse."""


class SaveTargetPort(Protocol):
    """Resolve a shell-registered ``handle_id``; never a path from the page."""

    def verify(self, handle_id: int, *, purpose: str, format: str) -> bool:
        """Process/epoch ownership, file type, write access, not consumed."""

    def open(self, handle_id: int) -> SaveTarget:
        """Consume the handle for writing."""

    def release(self, handle_id: int) -> None:
        """Close an unused handle; a no-op for unknown or consumed ids."""


@dataclass(frozen=True)
class LogSnapshot:
    """One atomic, owned copy of the bridge log model."""

    rows: tuple  # (row, report, run_id) as LogView.snapshot() yields them
    min_seq: int
    latest_seq: int
    clear_seq: int | None  # None: the view-clear watermark is unknown
    taken_at_ms: int


class LogModelPort(Protocol):
    def export_snapshot(self) -> LogSnapshot | None: ...


@dataclass(frozen=True)
class BundlePaths:
    config_files: Mapping[str, Path]
    state_files: Mapping[str, Path]
    log_file: Path | None
    events_file: Path | None
    extra_log_dir: Path | None
    home: str | None


class ExportSourcesPort(Protocol):
    def known_secret_values(self) -> Mapping[str, str]: ...

    def bundle_paths(self) -> BundlePaths: ...

    def app_facts(self) -> Mapping[str, Any]: ...

    def published_facts(self) -> Mapping[str, Mapping[str, Any] | None]:
        """Latest published snapshot event per topic; None when unavailable."""


# ------------------------------------------------------------------ adapters
class ExecutorJobIdentity:
    """Calls the public ``JobExecutor.job_identity`` getter (T-P2-31a).

    An executor without it leaves the identity unknown and every grant/export
    fails closed with ``not_allowed``; executor internals are never read here.
    """

    def __init__(self, executor_getter: Callable[[], Any]) -> None:
        self._executor = executor_getter

    def job_identity(self, op: str) -> tuple[str, str] | None:
        executor = self._executor()
        getter = getattr(type(executor), "job_identity", None)
        if getter is None:
            return None
        try:
            value = getter(executor, op)
        except Exception:
            return None
        if (isinstance(value, tuple) and len(value) == 2
                and all(isinstance(item, str) and item for item in value)):
            return value
        return None


class ExecutorJobOutcomes:
    """Subscribes through ``JobExecutor.add_terminal_listener`` when it exists.

    Expected listener argument: a mapping with ``op``, ``name``, ``args`` (an
    owned copy of the accepted args), ``epoch``, ``owner`` and ``state``,
    delivered after the executor lock is released (bridge_jobs supervisor
    thread, T-P2-31a).  For an executor without the API this is a no-op and a
    queued-cancelled export's grant is released by owner revocation,
    ``close()`` or the expiry timer (at most 5 minutes).

    ``names`` (31a review): subscribe to these command names only; the
    executor then hands over ``args`` for them alone (other jobs' args may
    hold secrets).  ``register`` passes ``EXPORT_COMMANDS``.
    """

    def __init__(self, executor_getter: Callable[[], Any],
                 names: frozenset[str] | None = None) -> None:
        self._executor = executor_getter
        self._names = names

    def subscribe(self, callback: Callable[[TerminalJob], None]) -> bool:
        executor = self._executor()
        method = getattr(type(executor), "add_terminal_listener", None)
        if method is None:
            return False

        def listener(event: Mapping[str, Any]) -> None:
            try:
                args = event.get("args")
                callback(TerminalJob(
                    str(event.get("name") or ""),
                    dict(args) if isinstance(args, Mapping) else {},
                    event.get("epoch"), event.get("owner"), str(event.get("state") or "")))
            except Exception:
                _LOG.warning("export terminal listener failed: %s", "listener_error")

        if self._names is None:
            method(executor, listener)
        else:
            method(executor, listener, names=self._names)
        return True


class UnavailableSaveTargets:
    """Default until the shell's handle registry (P3-06) is wired."""

    def verify(self, handle_id: int, *, purpose: str, format: str) -> bool:
        raise NotImplementedError("shell save handles arrive with P3-06")

    def open(self, handle_id: int) -> SaveTarget:
        raise NotImplementedError("shell save handles arrive with P3-06")

    def release(self, handle_id: int) -> None:
        return None


class WindowsHandleSaveTargets:
    """P2-19 / P3-06 adapter: file handles the shell duplicated into this process.

    ``handle_id`` is the sidecar-side value of a handle the shell opened on an
    approved target (OPEN_ALWAYS, no truncation) and duplicated with
    ``DuplicateHandle``; no path ever reaches this process. ``verify`` accepts
    only an unconsumed disk-file handle. Writes follow the SaveTarget
    contract: from offset 0, SetEndOfFile at commit, discard only closes.
    Every handle is closed exactly once (consumed by ``open`` or ``release``).

    T-P2-31b fix round 1 (M1): Windows recycles a closed handle's value at
    once, so the table only remembers values *currently* held by an open
    target (``_open``); the target forgets its value under the same lock in
    which it calls CloseHandle, so a recycled value verifies again.
    ``release`` closes only a disk-file value that no open target holds: the
    shell is trusted to send handles it duplicated, and the disk check keeps
    a stale or garbage value from closing one of the sidecar's own pipes or
    events (a stale value that happens to name another disk file of this
    process cannot be told apart; that is the documented trust boundary).
    """

    FILE_TYPE_DISK = 1

    def __init__(self, kernel32: Any = None) -> None:
        self._kernel32 = kernel32
        self._lock = threading.Lock()
        self._open: set[int] = set()

    def _api(self) -> Any:
        if self._kernel32 is None:
            import ctypes
            from ctypes import wintypes
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.GetFileType.argtypes = [wintypes.HANDLE]
            api.GetFileType.restype = wintypes.DWORD
            api.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
                                             ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
            api.SetFilePointerEx.restype = wintypes.BOOL
            api.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
            api.WriteFile.restype = wintypes.BOOL
            api.FlushFileBuffers.argtypes = [wintypes.HANDLE]
            api.FlushFileBuffers.restype = wintypes.BOOL
            api.SetEndOfFile.argtypes = [wintypes.HANDLE]
            api.SetEndOfFile.restype = wintypes.BOOL
            api.CloseHandle.argtypes = [wintypes.HANDLE]
            api.CloseHandle.restype = wintypes.BOOL
            self._kernel32 = api
        return self._kernel32

    @staticmethod
    def _valid(handle_id: Any) -> bool:
        return type(handle_id) is int and 0 < handle_id < 2 ** 63

    def verify(self, handle_id: int, *, purpose: str, format: str) -> bool:
        if not self._valid(handle_id):
            return False
        with self._lock:
            if handle_id in self._open:
                return False
            return int(self._api().GetFileType(handle_id)) == self.FILE_TYPE_DISK

    def open(self, handle_id: int) -> SaveTarget:
        if not self._valid(handle_id):
            raise ValueError("invalid handle")
        api = self._api()
        with self._lock:
            if handle_id in self._open:
                raise ValueError("handle already open")
            self._open.add(handle_id)
        return _HandleTarget(api, handle_id, self._lock, self._open)

    def release(self, handle_id: int) -> None:
        if not self._valid(handle_id):
            return
        api = self._api()
        with self._lock:
            if handle_id in self._open:
                return  # The open target closes it exactly once.
            try:
                if int(api.GetFileType(handle_id)) == self.FILE_TYPE_DISK:
                    api.CloseHandle(handle_id)
            except Exception:
                pass

    def open_count(self) -> int:
        with self._lock:
            return len(self._open)


class _HandleTarget:
    """One opened handle; see WindowsHandleSaveTargets."""

    def __init__(self, api: Any, handle: int, lock: Any = None,
                 open_set: set[int] | None = None) -> None:
        self._api, self._handle = api, handle
        self._lock = lock if lock is not None else threading.Lock()
        self._open = open_set if open_set is not None else set()
        self._size = 0
        self._started = False
        self._closed = False

    def write(self, data: bytes) -> None:
        import ctypes
        from ctypes import wintypes
        if self._closed:
            raise OSError("target closed")
        if not self._started:
            # Offset 0 regardless of the shared file pointer of the duplicate.
            if not self._api.SetFilePointerEx(self._handle, 0, None, 0):
                raise OSError("seek failed")
            self._started = True
        view = memoryview(bytes(data))
        while view:
            written = wintypes.DWORD(0)
            chunk = bytes(view[:1 << 20])
            if not self._api.WriteFile(self._handle, chunk, len(chunk),
                                       ctypes.byref(written), None) or written.value == 0:
                raise OSError("write failed")
            self._size += written.value
            view = view[written.value:]

    def commit(self) -> int | None:
        if self._closed:
            raise OSError("target closed")
        try:
            if not self._started and not self._api.SetFilePointerEx(self._handle, 0, None, 0):
                raise OSError("seek failed")
            if not self._api.SetEndOfFile(self._handle):
                raise OSError("set end of file failed")
            if not self._api.FlushFileBuffers(self._handle):
                raise OSError("flush failed")
        finally:
            self._close()
        return self._size

    def discard(self) -> None:
        self._close()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Close and forget under the table lock: the value may be recycled
        # by the very next DuplicateHandle and must verify again then.
        with self._lock:
            try:
                self._api.CloseHandle(self._handle)
            except Exception:
                pass
            self._open.discard(self._handle)


class LogViewModel:
    """Adapter over bridge_log_view.LogView's public surface.

    ``snapshot()`` is atomic.  The view-clear watermark is the public
    ``LogView.clear_seq`` property (T-P2-31b); filtered exports start after
    it.  A view without it reports the watermark unknown.
    """

    def __init__(self, view_getter: Callable[[], Any], *,
                 wall_ms: Callable[[], int] = _wall_ms) -> None:
        self._view = view_getter
        self._wall_ms = wall_ms

    def export_snapshot(self) -> LogSnapshot | None:
        view = self._view()
        if view is None:
            return None
        before = view.latest_seq
        rows = tuple(view.snapshot())
        taken = self._wall_ms()
        if rows:
            # The newest record is never evicted by its own append.
            minimum, latest = rows[0][0]["seq"], rows[-1][0]["seq"]
        else:
            minimum, latest = before + 1, before
        clear = None
        if isinstance(getattr(type(view), "clear_seq", None), property):
            value = view.clear_seq
            clear = value if type(value) is int and value >= 0 else None
        return LogSnapshot(rows, minimum, latest, clear, taken)


class SessionExportSources:
    """Runtime facts through public RuntimeAccess / runtime / state surfaces."""

    def __init__(self, session: Any) -> None:
        self._session = session

    def known_secret_values(self) -> Mapping[str, str]:
        return dict(self._session.access.known_secret_values())

    def bundle_paths(self) -> BundlePaths:
        from log_capture import structured_event_log_path

        runtime = self._session.runtime
        paths = runtime.app_paths
        try:  # literal attribute: the bridge gate forbids getattr on the runtime
            alerts = runtime.alerts_config
        except AttributeError:
            alerts = None
        configured = (str(alerts.get("log_file") or "").strip()
                      if isinstance(alerts, Mapping) else "")
        log_file = Path(configured) if configured else Path(paths.log_file)
        if not log_file.is_absolute():
            log_file = Path.cwd() / log_file
        return BundlePaths(
            config_files={
                "config.yaml": Path(paths.config_path),
                "config.local.yaml": Path(paths.local_config_path),
                "sources.yaml": Path(paths.sources_path),
            },
            state_files=state_file_candidates(Path(paths.data_dir)),
            log_file=log_file,
            events_file=structured_event_log_path(log_file),
            extra_log_dir=Path(paths.data_dir) / "logs",
            home=str(Path.home()),
        )

    def app_facts(self) -> Mapping[str, Any]:
        try:
            info = self._session.access.app_info()
        except Exception:
            return {}
        return {key: info.get(key) for key in ("version", "language")}

    def published_facts(self) -> Mapping[str, Mapping[str, Any] | None]:
        # PublicationState.snapshot() serves subscribed topics only; an
        # unsubscribed topic is reported unavailable (needed: a latest-value
        # getter that does not require a subscription).
        state = self._session.state
        facts: dict[str, Mapping[str, Any] | None] = {}
        for topic in STATE_TOPICS:
            try:
                facts[topic] = state.snapshot(topic)
            except Exception:
                facts[topic] = None
        return facts


# ------------------------------------------------------- known-secret pass
def _secret_forms(value: str) -> set[str]:
    forms = {value, value.strip()}
    for item in list(forms):
        if not item:
            continue
        ascii_json = json.dumps(item)[1:-1]
        encoded = {
            quote(item, safe=""), quote(item, safe="/:@"), quote_plus(item),
            # URL encoders that keep RFC 3986 reserved characters literal.
            quote(item, safe=":/?#[]@!$&'()*+,;="),
            json.dumps(item, ensure_ascii=False)[1:-1], ascii_json,
            # \uXXXX with upper-case hex is equally valid JSON.
            re.sub(r"\\u([0-9a-f]{4})", lambda m: "\\u" + m[1].upper(), ascii_json),
        }
        forms |= encoded
        forms |= {re.sub(r"%[0-9A-F]{2}", lambda m: m[0].lower(), form) for form in encoded}
    return {form for form in forms if form}


def _trie_regex(node: dict) -> str:
    """Prefix-factored alternation; a greedy optional tail keeps longest-wins."""

    end = "" in node
    branches = []
    for char in sorted(key for key in node if key):
        literal, child = char, node[char]
        while len(child) == 1 and "" not in child:  # Collapse single chains.
            (following, child), = child.items()
            literal += following
        branches.append(re.escape(literal) + _trie_regex(child))
    if not branches:
        return ""
    body = branches[0] if len(branches) == 1 and not end else "(?:" + "|".join(branches) + ")"
    return body + "?" if end else body


def _literal_pattern(words: Iterable[str]) -> re.Pattern[str]:
    """One matcher for many literals: at each position it costs about one
    branch per distinct next character instead of one per registered form,
    and it picks the longest literal starting there (as a longest-first flat
    alternation does)."""

    words = sorted(set(words), key=lambda word: (-len(word), word))
    root: dict = {}
    for word in words:
        node = root
        for char in word:
            node = node.setdefault(char, {})
        node[""] = {}
    try:
        return re.compile(_trie_regex(root))
    except (RecursionError, re.error):
        return re.compile("|".join(re.escape(word) for word in words))


def _registry_values(values: Any) -> dict[str, str]:
    pairs = values.items() if isinstance(values, Mapping) else ((v, "secret") for v in values or ())
    owned: dict[str, str] = {}
    for value, category in pairs:
        if isinstance(value, str) and value:
            owned[value] = (category if isinstance(category, str) and _CATEGORY.fullmatch(category)
                            else "secret")
    return owned


class RegistryKnownSecrets(KnownSecrets):
    """``bridge_redact.KnownSecrets`` with the prefix-factored matcher.

    Same values, same longest-wins replacement, only the matcher differs:
    ``redact_text`` calls ``replace`` per line, and a flat alternation of
    thousands of values costs one branch per value at every position.
    """

    def __init__(self, values: Mapping[str, str] | Iterable[str] = ()) -> None:
        super().__init__(())
        owned = {value: f"[redacted:{category}]"
                 for value, category in _registry_values(values).items()}
        self._registry_replacements = owned
        self._registry_pattern = _literal_pattern(owned) if owned else None

    def replace(self, text: str) -> str:
        if self._registry_pattern is None:
            return text
        return self._registry_pattern.sub(lambda m: self._registry_replacements[m[0]], text)


def _structural(key: Any, value: Any) -> bool:
    if key not in STRUCTURAL_INT_KEYS:
        return False
    if type(value) is int:
        return True
    return isinstance(value, list) and all(type(item) is int for item in value)


class SecretPass:
    """Exact known-secret replacement over text and parsed documents.

    Strings and keys are replaced as substrings outside existing markers;
    numbers only when their whole spelling is a registered value (as the log
    redactor's JSON projection does), except structural integer fields
    (``STRUCTURAL_INT_KEYS``).  ``leaks*`` re-check finished output.

    Long text is scanned in ``SCAN_CHUNK`` windows overlapping by the longest
    form, with results identical to one whole-text scan; between windows the
    interpreter regains control, so other threads keep running.
    """

    def __init__(self, values: Mapping[str, str] | Iterable[str]) -> None:
        replacements: dict[str, str] = {}
        for value, category in _registry_values(values).items():
            for form in _secret_forms(value):
                replacements.setdefault(form, f"[redacted:{category}]")
        self._replacements = replacements
        self._pattern = _literal_pattern(replacements) if replacements else None
        self._longest = max(map(len, replacements), default=0)
        self.replacements = 0

    def _windows(self, text: str):
        """Yield matches of a whole-text ``finditer`` window by window."""

        length, position = len(text), 0
        while position < length:
            window_end = min(length, position + SCAN_CHUNK)
            limit = min(length, window_end + self._longest - 1)
            last_end = position
            for match in self._pattern.finditer(text, position, limit):
                if match.start() >= window_end:
                    break
                last_end = match.end()
                yield match
            position = max(window_end, last_end)

    def _sub(self, segment: str) -> str:
        parts, emitted = [], 0
        for match in self._windows(segment):
            parts.append(segment[emitted:match.start()])
            parts.append(self._replacements[match[0]])
            self.replacements += 1
            emitted = match.end()
        if not parts:
            return segment
        parts.append(segment[emitted:])
        return "".join(parts)

    def text(self, text: str) -> str:
        if self._pattern is None or not text:
            return text
        parts, position = [], 0
        for marker in _MARKER.finditer(text):
            parts.append(self._sub(text[position:marker.start()]))
            parts.append(marker[0])
            position = marker.end()
        parts.append(self._sub(text[position:]))
        return "".join(parts)

    def _scalar_spelling(self, value: Any) -> str | None:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return json.dumps(value) if math.isfinite(value) else None
        return str(value)

    def value(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {self._key(key): child if _structural(key, child) else self.value(child)
                    for key, child in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(child) for child in value]
        if isinstance(value, str):
            return self.text(value)
        spelling = self._scalar_spelling(value)
        if spelling is None or self._pattern is None:
            return value
        if isinstance(value, (int, float)):
            if spelling in self._replacements:
                self.replacements += 1
                return self._replacements[spelling]
            return value
        # Dates and other scalars: replaced whole when any registered value
        # appears in their spelling.
        return self.text(spelling) if self._pattern.search(spelling) else value

    def _key(self, key: Any) -> Any:
        return self.text(key) if isinstance(key, str) else self.value(key)

    def leaks(self, text: str) -> bool:
        if self._pattern is None or not text:
            return False
        masked = _MARKER.sub("\x00", text)
        return next(self._windows(masked), None) is not None

    def leaks_value(self, value: Any) -> bool:
        if isinstance(value, Mapping):
            return any(self.leaks_value(key)
                       or (not _structural(key, child) and self.leaks_value(child))
                       for key, child in value.items())
        if isinstance(value, (list, tuple)):
            return any(self.leaks_value(child) for child in value)
        if isinstance(value, str):
            return self.leaks(value)
        spelling = self._scalar_spelling(value)
        if spelling is None or self._pattern is None:
            return False
        if isinstance(value, (int, float)):
            return spelling in self._replacements
        return self.leaks(spelling)


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"


def _dump_yaml(value: Any) -> str:
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=1 << 30)


def _document_kind(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return {".json": "json", ".jsonl": "jsonl", ".yaml": "yaml", ".yml": "yaml"}.get(suffix, "text")


def _parse(kind: str, text: str) -> Any:
    try:
        parsed = json.loads(text) if kind == "json" else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError, RecursionError):
        return None
    return parsed if isinstance(parsed, (Mapping, list)) else None


def _pass_document(name: str, text: str, secrets: SecretPass) -> str:
    kind = _document_kind(name)
    if kind == "jsonl":
        lines = []
        for line in text.splitlines():
            parsed = _parse("json", line) if line.strip() else None
            lines.append(json.dumps(secrets.value(parsed), ensure_ascii=False, default=str)
                         if parsed is not None else secrets.text(line))
        return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    if kind in {"json", "yaml"}:
        parsed = _parse(kind, text)
        if parsed is not None:
            cleaned = secrets.value(parsed)
            return _dump_json(cleaned) if kind == "json" else _dump_yaml(cleaned)
    return secrets.text(text)


def _document_leaks(name: str, text: str, secrets: SecretPass) -> bool:
    kind = _document_kind(name)
    if kind == "jsonl":
        for line in text.splitlines():
            parsed = _parse("json", line) if line.strip() else None
            if secrets.leaks_value(parsed) if parsed is not None else secrets.leaks(line):
                return True
        return False
    if kind in {"json", "yaml"}:
        parsed = _parse(kind, text)
        if parsed is not None:
            return secrets.leaks_value(parsed)
    return secrets.leaks(text)


class SecretLeak(RuntimeError):
    """The finished export still holds a registered value; nothing is written."""


def registry_pass_bundle(data: bytes, secrets: SecretPass) -> bytes:
    """Apply the known-secret registry to every entry of a written bundle."""

    with zipfile.ZipFile(io.BytesIO(data)) as source:
        contents = [(info.filename, source.read(info)) for info in source.infolist()]
    cleaned: list[tuple[str, bytes | None]] = []
    sizes: dict[str, int] = {}
    manifest_arc = None
    for arcname, raw in contents:
        name = arcname.partition("/")[2] or arcname
        if name == "manifest.json":
            manifest_arc = arcname
            cleaned.append((arcname, None))
            continue
        text = _pass_document(name, raw.decode("utf-8", errors="replace"), secrets)
        encoded = text.encode("utf-8")
        sizes[name] = len(encoded)
        cleaned.append((arcname, encoded))
    if manifest_arc is not None:
        raw = dict(contents)[manifest_arc]
        manifest = _parse("json", raw.decode("utf-8", errors="replace")) or {}
        for entry in manifest.get("files", []) if isinstance(manifest, dict) else []:
            if isinstance(entry, dict) and entry.get("name") in sizes:
                entry["bytes"] = sizes[entry["name"]]
        if isinstance(manifest, dict):
            manifest["registry_pass"] = {"replacements": secrets.replacements}
        encoded = _dump_json(secrets.value(manifest)).encode("utf-8")
        cleaned = [(arc, encoded if arc == manifest_arc else data_) for arc, data_ in cleaned]
    for arcname, encoded in cleaned:
        name = arcname.partition("/")[2] or arcname
        if _document_leaks(name, encoded.decode("utf-8", errors="replace"), secrets):
            raise SecretLeak("bundle entry failed the registry check")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for arcname, encoded in cleaned:
            archive.writestr(arcname, encoded)
    return output.getvalue()


# --------------------------------------------------------------- save grants
@dataclass(frozen=True)
class SaveGrant:
    ref: str
    purpose: str
    format: str
    handle_id: int
    epoch: str
    owner: str
    deadline: float  # monotonic


class SaveGrantTable:
    """Shell-registered save grants: bound, expiring, consumed once.

    ``expires_at_ms`` is wall clock; it is converted once to a monotonic
    deadline and capped at five minutes.  Expired, revoked or rejected grants
    release their handle through the target port, outside the table lock.
    The port's ``verify`` also runs outside the lock against a reservation, so
    a slow handle check never blocks consume/sweep/revoke.  A timer (when a
    factory is given) sweeps at the earliest deadline, so an expired handle is
    closed even when no further traffic arrives.
    """

    def __init__(self, targets: SaveTargetPort, *,
                 monotonic: Callable[[], float] = time.monotonic,
                 wall_ms: Callable[[], int] = _wall_ms,
                 ttl_max_ms: int = GRANT_TTL_MAX_MS, capacity: int = GRANT_CAPACITY,
                 owner_capacity: int = GRANT_OWNER_CAPACITY,
                 timer_factory: Callable[[float, Callable[[], None]], Any] | None = threading.Timer,
                 ) -> None:
        self._targets = targets
        self._monotonic = monotonic
        self._wall_ms = wall_ms
        self._ttl_max_ms = ttl_max_ms
        self._capacity = capacity
        self._owner_capacity = owner_capacity
        self._timer_factory = timer_factory
        self._lock = threading.Lock()
        self._grants: dict[str, SaveGrant] = {}
        # ref -> (handle_id, owner) while the port verifies outside the lock.
        self._pending: dict[str, tuple[int, str]] = {}
        self._withdrawn: set[str] = set()  # pending refs revoked meanwhile
        # T-P2-31b fix round 1: owners revoked in this session.  saveGrant
        # rides the shell envelope, so the executor no longer refuses a grant
        # for a page owner whose owner_revoke arrived first; the table does.
        self._revoked_owners: set[str] = set()
        self._used: deque[str] = deque(maxlen=USED_REF_MEMORY)
        self._timer: Any = None
        self._timer_deadline: float | None = None
        self._timer_token: object | None = None
        self._closed = False

    def __len__(self) -> int:
        with self._lock:
            return len(self._grants)

    def _forget_locked(self, ref: str) -> None:
        self._used.append(ref)

    def _sweep_locked(self) -> list[int]:
        now = self._monotonic()
        expired = [grant for grant in self._grants.values() if now >= grant.deadline]
        for grant in expired:
            del self._grants[grant.ref]
            self._forget_locked(grant.ref)
        return [grant.handle_id for grant in expired]

    # ------------------------------------------------------------ expiry timer
    def _cancel_timer_locked(self) -> None:
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:
                pass
        self._timer = self._timer_deadline = self._timer_token = None

    def _arm_locked(self) -> None:
        if self._timer_factory is None or self._closed or not self._grants:
            return
        earliest = min(grant.deadline for grant in self._grants.values())
        if self._timer is not None and self._timer_deadline is not None \
                and self._timer_deadline <= earliest:
            return
        self._cancel_timer_locked()
        token = object()
        delay = max(0.0, earliest - self._monotonic()) + 0.01
        timer = self._timer_factory(delay, lambda: self._on_timer(token))
        if hasattr(timer, "daemon"):
            timer.daemon = True
        self._timer, self._timer_deadline, self._timer_token = timer, earliest, token
        timer.start()

    def _on_timer(self, token: object) -> None:
        with self._lock:
            if token is not self._timer_token:
                return
            self._timer = self._timer_deadline = self._timer_token = None
            releases = self._sweep_locked()
            self._arm_locked()
        self.release(releases)

    def release(self, handles: Iterable[int]) -> None:
        for handle in handles:
            try:
                self._targets.release(handle)
            except Exception:
                pass  # The handle owner's own cleanup is the last word.

    def register(self, args: Mapping[str, Any], identity: tuple[str, str] | None) -> bool:
        ref, purpose, fmt = args["ref"], args["purpose"], args["format"]
        expires_at_ms, handle = args["expires_at_ms"], args["handle_id"]
        releases: list[int] = []
        release_new = True
        reserved = False
        try:
            with self._lock:
                releases.extend(self._sweep_locked())
                if self._closed:
                    raise JobFailure("not_allowed")
                if (any(grant.handle_id == handle for grant in self._grants.values())
                        or any(item[0] == handle for item in self._pending.values())):
                    release_new = False  # Never close a handle another grant owns.
                    raise JobFailure("conflict")
                if ref in self._grants or ref in self._pending or ref in self._used:
                    raise JobFailure("conflict")
                if identity is None or identity[1] in self._revoked_owners:
                    raise JobFailure("not_allowed")
                if fmt not in PURPOSE_FORMATS.get(purpose, ()):
                    raise JobFailure("validation")
                owned = (sum(grant.owner == identity[1] for grant in self._grants.values())
                         + sum(item[1] == identity[1] for item in self._pending.values()))
                if (len(self._grants) + len(self._pending) >= self._capacity
                        or owned >= self._owner_capacity):
                    raise JobFailure("job_capacity")
                if expires_at_ms - self._wall_ms() <= 0:
                    raise JobFailure("validation")
                self._pending[ref] = (handle, identity[1])
                reserved = True
            try:
                verified = self._targets.verify(handle, purpose=purpose, format=fmt) is True
            except Exception:
                verified = False
            with self._lock:
                self._pending.pop(ref, None)
                reserved = False
                withdrawn = ref in self._withdrawn
                self._withdrawn.discard(ref)
                if withdrawn:
                    self._forget_locked(ref)
                if not verified or withdrawn or self._closed:
                    raise JobFailure("not_allowed")
                remaining = expires_at_ms - self._wall_ms()
                if remaining <= 0:
                    raise JobFailure("validation")
                deadline = self._monotonic() + min(remaining, self._ttl_max_ms) / 1000
                self._grants[ref] = SaveGrant(ref, purpose, fmt, handle, identity[0],
                                              identity[1], deadline)
                release_new = False
                self._arm_locked()
                return True
        except BaseException:
            if reserved:
                with self._lock:
                    self._pending.pop(ref, None)
                    self._withdrawn.discard(ref)
            if release_new:
                releases.append(handle)
            raise
        finally:
            self.release(releases)

    def consume(self, ref: Any, purpose: str, fmt: str,
                identity: tuple[str, str] | None) -> SaveGrant:
        with self._lock:
            releases = self._sweep_locked()
            grant = self._grants.get(ref) if isinstance(ref, str) else None
            usable = (grant is not None and identity is not None
                      and (grant.epoch, grant.owner) == identity
                      and grant.purpose == purpose and grant.format == fmt)
            if usable:
                del self._grants[ref]
                self._forget_locked(ref)
        self.release(releases)
        if not usable:
            # A mismatched attempt leaves the grant to its rightful use or expiry.
            raise JobFailure("not_allowed")
        return grant

    def sweep(self) -> int:
        with self._lock:
            releases = self._sweep_locked()
        self.release(releases)
        return len(releases)

    def revoke_ref(self, ref: Any, identity: tuple[Any, Any]) -> bool:
        """Withdraw one unconsumed grant of ``identity`` (queued-cancelled export)."""

        with self._lock:
            grant = self._grants.get(ref) if isinstance(ref, str) else None
            if grant is None or (grant.epoch, grant.owner) != tuple(identity):
                return False
            del self._grants[ref]
            self._forget_locked(ref)
        self.release([grant.handle_id])
        return True

    def revoke_owner(self, owner: str) -> int:
        with self._lock:
            self._revoked_owners.add(owner)
            revoked = [grant for grant in self._grants.values() if grant.owner == owner]
            for grant in revoked:
                del self._grants[grant.ref]
                self._forget_locked(grant.ref)
            self._withdrawn.update(ref for ref, item in self._pending.items() if item[1] == owner)
        self.release(grant.handle_id for grant in revoked)
        return len(revoked)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._cancel_timer_locked()
            grants = list(self._grants.values())
            self._grants.clear()
            self._withdrawn.update(self._pending)
        self.release(grant.handle_id for grant in grants)


# ----------------------------------------------------------------- rendering
def _rendered(row: Mapping[str, Any], report: Any) -> dict[str, Any]:
    rendered = {field: row.get(field) for field in _LOG_FIELDS}
    if report:
        built = build_log_event_report(report)
        rendered["title"], rendered["detail"] = built.headline, built.detail
    return rendered


def _normalize_filter(value: Any) -> dict[str, Any]:
    """The LogView query filter semantics (bridge_log_view.LogView._filter)."""

    if not isinstance(value, Mapping) or set(value) - {"levels", "sources", "categories", "text"}:
        raise ValueError("invalid log filter")
    result: dict[str, Any] = {}
    for key in ("levels", "sources", "categories"):
        items = value.get(key, [])
        if (not isinstance(items, list) or len(items) > 4000
                or any(not isinstance(item, str) or len(item) > 128 for item in items)):
            raise ValueError("invalid log filter")
        result[key] = sorted(set(items))
    text = value.get("text", "")
    if not isinstance(text, str) or len(text) > 16_384:
        raise ValueError("invalid log text")
    result["text"] = text
    return result


def _matches(row: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    if row.get("key") == "source_boundary":
        return True  # Boundaries are never hidden by a filter (LogPage contract).
    for key, field_name in (("levels", "level"), ("sources", "source"), ("categories", "category")):
        if filters[key] and row.get(field_name) not in filters[key]:
            return False
    needle = filters["text"].casefold()
    return not needle or needle in " ".join(
        str(row.get(key) or "") for key in ("key", "title", "detail", "raw")).casefold()


def _iso(ts_ms: Any) -> str:
    if type(ts_ms) is not int:
        return str(ts_ms)
    try:
        moment = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone()
    except (OverflowError, OSError, ValueError):
        return str(ts_ms)
    return moment.isoformat(timespec="milliseconds")


def _ranges(seqs: list[int]) -> str:
    parts: list[str] = []
    for seq in seqs:
        if parts and isinstance(parts[-1], list) and parts[-1][1] + 1 == seq:
            parts[-1][1] = seq
        else:
            parts.append([seq, seq])
    return ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in parts)


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    # Log text is untrusted: never let a spreadsheet evaluate it.
    return "'" + text if text.startswith(_FORMULA_START) else text


def _scope_notes(header: Mapping[str, Any]) -> list[str]:
    """Header lines that state what the export could not include."""

    notes = []
    if header["missing_seqs"]:
        notes.append(f"# missing_seqs (evicted or unknown): {_ranges(header['missing_seqs'])}")
    evicted = header.get("evicted_before_export")
    if evicted:
        notes.append(f"# evicted before export, not checked against the filter: "
                     f"{evicted['from_seq']}-{evicted['to_seq']} ({evicted['count']} records)")
    return notes


def format_records(fmt: str, records: list[dict[str, Any]], header: Mapping[str, Any]) -> str:
    if fmt == "json":
        return _dump_json({**header, "records": records})
    if fmt == "text":
        lines = [f"# Tesseract log export (format {header['version']})",
                 f"# exported_at: {_iso(header['exported_at_ms'])}",
                 f"# records: {header['count']}; watermark: {header['watermark']}"]
        lines += _scope_notes(header)
        for record in records:
            title, raw = str(record.get("title") or ""), str(record.get("raw") or "")
            if title and title != raw:
                lines.append(f"[REPORT] {title}")
                detail = str(record.get("detail") or "")
                if detail:
                    lines.append(detail)
            lines.append(f"{_iso(record.get('ts_ms'))} #{record.get('seq')} [{record.get('source')}] "
                         f"[{record.get('level')}] [{record.get('category')}] {raw}")
        return "\n".join(lines) + "\n"
    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        # Gaps are stated, never silently dropped; '#' never starts a formula.
        for note in _scope_notes(header):
            buffer.write(note + "\r\n")
        writer.writerow(_CSV_COLUMNS)
        for record in records:
            writer.writerow([_csv_cell(record.get("seq")), _csv_cell(_iso(record.get("ts_ms")))]
                            + [_csv_cell(record.get(name)) for name in _CSV_COLUMNS[2:]])
        return "﻿" + buffer.getvalue()
    raise ValueError("unsupported export format")


def _read_tail(path: Path, budget: int) -> tuple[str, int, bool] | None:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            truncated = size > budget
            if truncated:
                handle.seek(size - budget)
            data = handle.read(budget)
    except OSError:
        return None
    if truncated:
        newline = data.find(b"\n")
        data = data[newline + 1:] if newline >= 0 else b""
    return data.decode("utf-8", errors="replace"), size, truncated


def redact_lines(text: str, known: KnownSecrets, secrets: SecretPass | None = None) -> str:
    """Producer-side log redaction, one line at a time, never the raw file.

    With ``secrets`` the registry first runs over the whole tail, so a
    registered value that spans lines (a PEM key, for example) is removed
    before the per-line pass can split it into unmatchable pieces.
    """

    text = text.replace("\r\n", "\n")
    if secrets is not None:
        text = secrets.text(text)
    lines = []
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if len(line) > MAX_TEXT:
            lines.append(REDACTED)
            continue
        try:
            lines.append(redact_text(line, known_secrets=known))
        except Exception:
            lines.append(REDACTED)
    return "\n".join(lines)


# ------------------------------------------------------------------- service
def sweep_stale_drafts(root: Path | str | None = None, *,
                       max_age: float = DRAFT_STALE_SECONDS,
                       now: Callable[[], float] = time.time) -> int:
    """Remove bundle drafts a crashed sidecar left behind (older than max_age)."""

    base = Path(root) if root is not None else Path(tempfile.gettempdir())
    removed = 0
    try:
        entries = list(os.scandir(base))
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(DRAFT_PREFIX):
            continue
        try:
            if (entry.is_symlink() or entry.is_junction()
                    or not entry.is_dir(follow_symlinks=False)):
                continue
            if now() - entry.stat(follow_symlinks=False).st_mtime < max_age:
                continue
        except OSError:
            continue
        shutil.rmtree(entry.path, ignore_errors=True)
        removed += 1
    return removed


class ExportService:
    def __init__(self, *, targets: SaveTargetPort, identity: JobIdentityPort,
                 sources: ExportSourcesPort, log_model: LogModelPort,
                 monotonic: Callable[[], float] = time.monotonic,
                 wall_ms: Callable[[], int] = _wall_ms,
                 timer_factory: Callable[[float, Callable[[], None]], Any] | None = threading.Timer,
                 draft_root: Path | str | None = None) -> None:
        self.targets = targets
        self.identity = identity
        self.sources = sources
        self.log_model = log_model
        self._wall_ms = wall_ms
        self.draft_root = draft_root
        self.grants = SaveGrantTable(targets, monotonic=monotonic, wall_ms=wall_ms,
                                     timer_factory=timer_factory)

    # shell.saveGrant: control domain, res.  No disk work beyond the port's
    # handle verification (which runs outside the grant-table lock).
    def save_grant(self, args: dict[str, Any], context: Any) -> dict[str, Any]:
        # T-P2-31b (决定_第一波裁定2 T-P2-19 #2): the grant binds to the page
        # owner that asked for host.saveFile (``for_owner``); a shell without
        # it keeps the P2-19 interim rule (the request envelope's owner).
        identity = self.identity.job_identity(context.op)
        for_owner = args.get("for_owner")
        if identity is not None and isinstance(for_owner, str) and for_owner:
            identity = (identity[0], for_owner)
        return {"registered": self.grants.register(args, identity)}

    def revoke_owner(self, owner: str) -> None:
        self.grants.revoke_owner(owner)

    def on_job_terminal(self, job: TerminalJob) -> None:
        """A queued export that ends cancelled never ran: free its grant now."""

        if job.name in EXPORT_COMMANDS and job.state == "cancelled":
            self.grants.revoke_ref(job.args.get("save_ref"), (job.epoch, job.owner))

    def close(self) -> None:
        self.grants.close()

    def _write(self, grant: SaveGrant, payload: bytes) -> int:
        try:
            target = self.targets.open(grant.handle_id)
        except Exception as exc:
            self.grants.release([grant.handle_id])
            _LOG.warning("export target open failed: code=io cause=%s", type(exc).__name__)
            raise JobFailure("io") from None
        try:
            view = memoryview(payload)
            for offset in range(0, len(payload), WRITE_CHUNK):
                target.write(bytes(view[offset:offset + WRITE_CHUNK]))
            size = target.commit()
            if size is not None and size != len(payload):
                raise OSError("short write")
        except Exception as exc:
            try:
                target.discard()
            except Exception:
                pass
            _LOG.warning("export write failed: code=io cause=%s", type(exc).__name__)
            raise JobFailure("io") from None
        return len(payload)

    def _guarded(self, grant: SaveGrant, build: Callable[[], Any]) -> Any:
        try:
            return build()
        except JobFailure:
            self.grants.release([grant.handle_id])
            raise
        except OSError as exc:
            self.grants.release([grant.handle_id])
            _LOG.warning("export build failed: code=io cause=%s", type(exc).__name__)
            raise JobFailure("io") from None
        except Exception as exc:
            self.grants.release([grant.handle_id])
            # PROTOCOL §7: unknown failures map to internal with a local note.
            _LOG.warning("export build failed: code=internal cause=%s", type(exc).__name__)
            raise JobFailure("internal") from None

    # logs.exportBundle: normal domain, job, cancellable only while queued.
    def export_bundle(self, args: dict[str, Any], context: Any) -> dict[str, Any]:
        grant = self.grants.consume(args.get("save_ref"), "bundle", "zip",
                                    self.identity.job_identity(context.op))
        payload, files = self._guarded(grant, self.build_bundle)
        size = self._write(grant, payload)
        return {"ref": grant.ref, "size": size, "files": files}

    # logs.exportFiltered: normal domain, job, cancellable only while queued.
    def export_filtered(self, args: dict[str, Any], context: Any) -> dict[str, Any]:
        fmt = args["format"]
        if "seqs" in args:
            if len(args["seqs"]) > SELECTED_SEQS_MAX:
                raise JobFailure("validation")
            filters = None
        else:
            try:
                filters = _normalize_filter(args.get("filter"))
            except ValueError:
                raise JobFailure("validation") from None
        # Reject before consuming: a malformed request keeps the grant usable.
        grant = self.grants.consume(args.get("save_ref"), "filtered", fmt,
                                    self.identity.job_identity(context.op))
        payload, count, missing = self._guarded(
            grant, lambda: self.build_filtered(fmt, args.get("seqs"), filters))
        self._write(grant, payload)
        return {"ref": grant.ref, "count": count, "missing_seqs": missing}

    # ------------------------------------------------------------ builders
    def build_filtered(self, fmt: str, seqs: list[int] | None,
                       filters: Mapping[str, Any] | None) -> tuple[bytes, int, list[int]]:
        snapshot = self.log_model.export_snapshot()
        if snapshot is None:
            raise JobFailure("runtime_unavailable")
        secrets = SecretPass(self.sources.known_secret_values())
        extra: dict[str, Any] = {}
        if seqs is not None:
            by_seq = {row["seq"]: (row, report) for row, report, _run in snapshot.rows}
            wanted = sorted(set(seqs))
            records = [_rendered(*by_seq[seq]) for seq in wanted if seq in by_seq]
            # Evicted before the atomic snapshot, or never issued: not exported.
            missing = [seq for seq in wanted if seq not in by_seq]
        else:
            # The filter scope is what the model still retains after the view
            # clear watermark.  Records evicted before the snapshot cannot be
            # matched against the filter at all; they are stated as one range
            # in the file header (never silently dropped) instead of being
            # listed seq by seq, which a long session would overflow.
            # ``missing_seqs`` lists holes inside the retained window.
            lower = snapshot.clear_seq if snapshot.clear_seq is not None else 0
            start = max(lower, snapshot.min_seq - 1)
            records, present = [], set()
            for row, report, _run in snapshot.rows:
                if row["seq"] <= start:
                    continue
                present.add(row["seq"])
                rendered = _rendered(row, report)
                if _matches(rendered, filters):
                    records.append(rendered)
            missing = []
            for seq in range(start + 1, snapshot.latest_seq + 1):
                if seq not in present:
                    missing.append(seq)
                    if len(missing) > MISSING_SEQS_MAX:
                        # Cannot be stated within one wire array: narrow scope.
                        raise JobFailure("snapshot_required")
            extra["view_clear_seq"] = snapshot.clear_seq
            if start > lower:
                extra["evicted_before_export"] = {
                    "from_seq": lower + 1, "to_seq": start, "count": start - lower}
        header = {"format": "tesseract.logs", "version": 1,
                  "exported_at_ms": self._wall_ms(), "snapshot_at_ms": snapshot.taken_at_ms,
                  "selection": "seqs" if seqs is not None else "filter",
                  "watermark": snapshot.latest_seq, "count": len(records),
                  "missing_seqs": missing, **extra}
        if filters is not None:
            header["filter"] = dict(filters)
        if fmt == "json":
            document = secrets.value({**header, "records": records})
            if secrets.leaks_value(document):
                raise SecretLeak("filtered export failed the registry check")
            text = _dump_json(document)
        else:
            text = secrets.text(format_records(fmt, [secrets.value(r) for r in records], header))
            if secrets.leaks(text):
                raise SecretLeak("filtered export failed the registry check")
        return text.encode("utf-8"), len(records), missing

    def _extra_logs(self, directory: Path | None, known: KnownSecrets, secrets: SecretPass,
                    times: dict[str, int]) -> list[DiagnosticAttachment]:
        attachments: list[DiagnosticAttachment] = []
        if directory is None:
            return attachments
        for family in EXTRA_LOG_FAMILIES:
            budget = EXTRA_LOG_TAIL_BYTES
            for path in (directory / family, directory / f"{family}.1"):
                if budget <= 0:
                    break
                tail = _read_tail(path, budget)
                if tail is None:
                    continue
                text, size, truncated = tail
                budget -= min(size, budget)
                times[path.name] = self._wall_ms()
                attachments.append(DiagnosticAttachment(
                    path.name, redact_lines(text, known, secrets), source_bytes=size,
                    truncated=truncated))
        return attachments

    def _log_model_attachment(self, times: dict[str, int]
                              ) -> tuple[list[DiagnosticAttachment], str, dict[str, Any] | None]:
        snapshot = self.log_model.export_snapshot()
        if snapshot is None:
            return [], "", None
        times["log_model"] = snapshot.taken_at_ms
        lines, events = [], []
        for row, report, run_id in snapshot.rows:
            rendered = _rendered(row, report)
            rendered["run_id"] = run_id
            lines.append(json.dumps(rendered, ensure_ascii=False, default=str))
            if report:
                events.append(f"{_iso(rendered['ts_ms'])} [{rendered['level']}] {rendered['title']}")
                if rendered["detail"]:
                    events.append(f"  {rendered['detail']}")
        model = DiagnosticAttachment("bridge-log.jsonl", "\n".join(lines) + ("\n" if lines else ""),
                                     jsonl=True)
        # Retention, so a reader can tell early records were evicted.
        retention = {"min_seq": snapshot.min_seq, "latest_seq": snapshot.latest_seq,
                     "retained": len(snapshot.rows),
                     "evicted_through_seq": snapshot.min_seq - 1,
                     "view_clear_seq": snapshot.clear_seq,
                     "snapshot_at_ms": snapshot.taken_at_ms}
        return [model], "\n".join(events), retention

    def _app(self) -> dict[str, Any]:
        from app_info import APP_NAME, APP_VERSION

        facts = self.sources.app_facts()
        version = facts.get("version")
        language = facts.get("language")
        # No Qt fields: the bundle describes the sidecar and its shell.
        return {"name": APP_NAME,
                "version": version if isinstance(version, str) and version else APP_VERSION,
                "frozen": bool(getattr(sys, "frozen", False)),
                "language": language if isinstance(language, str) else "",
                "surface": "sidecar"}

    def build_bundle(self) -> tuple[bytes, list[str]]:
        values = _registry_values(self.sources.known_secret_values())
        secrets, known = SecretPass(values), RegistryKnownSecrets(values)
        paths = self.sources.bundle_paths()
        times: dict[str, int] = {}
        attachments, key_events, retention = self._log_model_attachment(times)
        attachments += self._extra_logs(paths.extra_log_dir, known, secrets, times)
        published, unavailable = {}, []
        for topic, event in self.sources.published_facts().items():
            if not isinstance(event, Mapping):
                unavailable.append(topic)
                continue
            produced = event.get("produced_at_ms")
            published[topic] = {"produced_at_ms": produced, "rev": event.get("rev"),
                                "data": event.get("data")}
            if type(produced) is int and produced >= 0:
                times[f"published:{topic}"] = produced
        status: dict[str, Any] = {"published": published, "unavailable": unavailable}
        if retention is not None:
            status["log_model"] = retention
        now = datetime.now().astimezone()
        times["bundle"] = self._wall_ms()
        inputs = DiagnosticBundleInputs(
            log_file=paths.log_file, events_file=paths.events_file,
            config_files=dict(paths.config_files), state_files=dict(paths.state_files),
            runtime_status=status,
            key_events_text=key_events, app=self._app(), home=paths.home,
            extra_logs=tuple(attachments), snapshot_times=times)
        # The writer needs a real file for its atomic replace.  The draft has
        # passed the bundle redactor and lives only in a private temporary
        # directory until the registry pass has read it back; a crash in
        # between leaves a draft that ``sweep_stale_drafts`` removes later.
        workdir = Path(tempfile.mkdtemp(prefix=DRAFT_PREFIX, dir=self.draft_root))
        try:
            result = write_diagnostic_bundle(workdir / default_bundle_name(now), inputs, now=now)
            draft = result.path.read_bytes()
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return registry_pass_bundle(draft, secrets), list(result.files)


def register(session: Any, *, targets: SaveTargetPort | None = None,
             identity: JobIdentityPort | None = None,
             sources: ExportSourcesPort | None = None,
             log_model: LogModelPort | None = None,
             outcomes: JobOutcomePort | None = None,
             monotonic: Callable[[], float] = time.monotonic,
             wall_ms: Callable[[], int] = _wall_ms,
             timer_factory: Callable[[float, Callable[[], None]], Any] | None = threading.Timer,
             draft_root: Path | str | None = None) -> ExportService:
    """Register shell.saveGrant, logs.exportBundle and logs.exportFiltered.

    Call after ``BridgeSession.connect`` (the log model lives on
    ``session.logs.view``).  Owner revocation releases unconsumed grants; a
    terminal-job listener (when the executor offers one) releases the grant of
    a queued-cancelled export; stale bundle drafts are swept on a normal worker.
    """

    service = ExportService(
        targets=targets if targets is not None else UnavailableSaveTargets(),
        identity=identity if identity is not None else ExecutorJobIdentity(lambda: session.executor),
        sources=sources if sources is not None else SessionExportSources(session),
        log_model=log_model if log_model is not None else LogViewModel(
            lambda: getattr(getattr(session, "logs", None), "view", None), wall_ms=wall_ms),
        monotonic=monotonic, wall_ms=wall_ms, timer_factory=timer_factory,
        draft_root=draft_root)
    session.executor.register("shell.saveGrant", service.save_grant)
    session.executor.register("logs.exportBundle", service.export_bundle)
    session.executor.register("logs.exportFiltered", service.export_filtered)
    hook = getattr(session, "register_owner_revoke_hook", None)
    if callable(hook):
        hook(service.revoke_owner)
    (outcomes if outcomes is not None else ExecutorJobOutcomes(
        lambda: session.executor, names=EXPORT_COMMANDS)
     ).subscribe(service.on_job_terminal)
    submit = getattr(session.executor, "submit_normal", None)
    if callable(submit):
        submit(lambda: sweep_stale_drafts(draft_root))
    return service


__all__ = [
    "BundlePaths", "ExecutorJobIdentity", "ExecutorJobOutcomes", "ExportService",
    "ExportSourcesPort", "JobIdentityPort", "JobOutcomePort", "LogModelPort",
    "LogSnapshot", "LogViewModel", "RegistryKnownSecrets", "SaveGrant",
    "SaveGrantTable", "SaveTarget",
    "SaveTargetPort", "SecretLeak", "SecretPass", "SessionExportSources",
    "TerminalJob", "UnavailableSaveTargets", "WindowsHandleSaveTargets",
    "format_records", "redact_lines",
    "register", "registry_pass_bundle", "sweep_stale_drafts",
]
