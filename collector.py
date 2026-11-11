from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import queue
import random
import re
import tempfile
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable

import aiohttp
import yaml
from urllib.parse import quote

from collection_settings import (
    collection_max_bytes,
    collection_retries,
    collection_timeout_seconds,
    collection_user_agents,
)
from atomic_file import replace_with_retry
from config_utils import coerce_bool
from log_event_reports import log_event
from models import Node, PoolSnapshot, deduplicate_nodes, utc_now_iso
from parser import parse_content_blob
from runtime_source_transport import (
    RuntimeSourceFetchEndpoint,
    RuntimeSourceFetchRaceResult,
    RuntimeSourceRouteKind,
    race_runtime_source_fetch,
)
from tier3_sources import (
    CollectionReport,
    SourceDispatchDeferred,
    SourceFetchResult,
    SourceUnitResult,
    build_source,
    canonical_source_identity,
    source_entries_from_sources_config,
)


DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/123.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15",
]


SourceBatchObserver = Callable[[list[Node], CollectionReport], Any]
SourceRootTerminalObserver = Callable[[dict[str, Any]], Any]


@dataclass(slots=True)
class _BufferedRuntimeSourceFetch:
    endpoint: RuntimeSourceFetchEndpoint
    iterator: Any | None
    buffered: list[tuple[SourceUnitResult, list[Node], int]]
    exhausted: bool
    lease_valid: bool = True


class _Tier3SourceUnitTransportBroker:
    """Bound source transports while rotating roots before route lanes."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self._active = 0
        self._lock = asyncio.Lock()
        self._root_order: deque[str] = deque()
        self._root_members: set[str] = set()
        self._route_order: dict[str, deque[str]] = {}
        self._route_members: dict[str, set[str]] = {}
        self._waiters: dict[
            tuple[str, str],
            deque[asyncio.Future[None]],
        ] = {}

    def _queue_has_waiters_unlocked(self, root_key: str, route_key: str) -> bool:
        return any(
            not waiter.done()
            for waiter in self._waiters.get((root_key, route_key), ())
        )

    def _root_has_waiters_unlocked(self, root_key: str) -> bool:
        return any(
            self._queue_has_waiters_unlocked(root_key, route_key)
            for route_key in self._route_order.get(root_key, ())
        )

    def _cleanup_route_unlocked(self, root_key: str, route_key: str) -> None:
        queue_for_route = self._waiters.get((root_key, route_key))
        if queue_for_route is not None:
            while queue_for_route and queue_for_route[0].done():
                queue_for_route.popleft()
            if not queue_for_route:
                self._waiters.pop((root_key, route_key), None)
        if self._queue_has_waiters_unlocked(root_key, route_key):
            return
        route_members = self._route_members.get(root_key)
        route_order = self._route_order.get(root_key)
        if route_members is not None:
            route_members.discard(route_key)
        if route_order is not None:
            try:
                route_order.remove(route_key)
            except ValueError:
                pass
            if not route_order:
                self._route_order.pop(root_key, None)
                self._route_members.pop(root_key, None)

    def _grant_unlocked(self) -> None:
        while self._active < self.limit and self._root_order:
            granted = False
            root_attempts = len(self._root_order)
            for _index in range(root_attempts):
                root_key = self._root_order.popleft()
                self._root_members.discard(root_key)
                route_order = self._route_order.get(root_key)
                if not route_order:
                    continue
                route_attempts = len(route_order)
                for _route_index in range(route_attempts):
                    route_key = route_order.popleft()
                    route_members = self._route_members.setdefault(root_key, set())
                    route_members.discard(route_key)
                    queue_for_route = self._waiters.get((root_key, route_key))
                    while queue_for_route and queue_for_route[0].done():
                        queue_for_route.popleft()
                    if queue_for_route:
                        waiter = queue_for_route.popleft()
                        if queue_for_route:
                            route_order.append(route_key)
                            route_members.add(route_key)
                        else:
                            self._waiters.pop((root_key, route_key), None)
                        self._active += 1
                        waiter.set_result(None)
                        granted = True
                        break
                    self._waiters.pop((root_key, route_key), None)
                if route_order:
                    self._root_order.append(root_key)
                    self._root_members.add(root_key)
                else:
                    self._route_order.pop(root_key, None)
                    self._route_members.pop(root_key, None)
                if granted:
                    break
            if not granted:
                break

    async def _acquire(self, root_key: str, route_key: str) -> None:
        root = str(root_key or "unknown")
        route = str(route_key or "default")
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()
        async with self._lock:
            self._waiters.setdefault((root, route), deque()).append(waiter)
            if root not in self._root_members:
                self._root_order.append(root)
                self._root_members.add(root)
            route_members = self._route_members.setdefault(root, set())
            route_order = self._route_order.setdefault(root, deque())
            if route not in route_members:
                route_order.append(route)
                route_members.add(route)
            self._grant_unlocked()
        try:
            await waiter
        except BaseException:
            async with self._lock:
                if waiter.done() and not waiter.cancelled():
                    self._active = max(0, self._active - 1)
                else:
                    queue_for_route = self._waiters.get((root, route))
                    if queue_for_route is not None:
                        try:
                            queue_for_route.remove(waiter)
                        except ValueError:
                            pass
                self._cleanup_route_unlocked(root, route)
                self._grant_unlocked()
            raise

    async def _release(self) -> None:
        async with self._lock:
            self._active = max(0, self._active - 1)
            self._grant_unlocked()

    @asynccontextmanager
    async def lease(self, root_key: str, route_key: str):
        await self._acquire(root_key, route_key)
        try:
            yield
        finally:
            await self._release()


class BlockingWorkCancellationTimeout(RuntimeError):
    """A local worker did not acknowledge cooperative cancellation in time."""


RAW_ONLY_LOG_EXTRA = {
    "category": "update",
    "is_key_event": False,
    "event_match_kind": "raw_only",
}


SOURCE_FETCH_FALLBACK_ERROR_MARKERS = (
    "aggregate_deferred_after_protocol_supply",
    "blocked_no_route",
    "cannot connect",
    "connection",
    "refused",
    "timed out",
    "timeout",
    "dns",
    "getaddrinfo",
    "network is unreachable",
    "remote host",
)
# The detail half of a runtime route race error: an exception class name or a
# fixed outcome token.  Anything else is dropped rather than persisted.
_RUNTIME_ROUTE_ERROR_DETAIL = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def _source_failure_kind(error: str) -> str:
    """Classify diagnostics for logging only; never drive retry or routing."""
    message = str(error or "").lower()
    http_status = re.search(r"(?:^|\bhttp(?:\s+error)?\s+)([45]\d{2})\b", message)
    if http_status is not None:
        return f"http_{http_status.group(1)}"
    for kind, markers in (
        ("blocked_no_route", ("blocked_no_route",)),
        ("dns", ("dns", "getaddrinfo", "name or service not known", "name resolution", "resolve host")),
        ("timeout", ("timeout", "timed out")),
        # aiohttp's "ssl:default" describes connection options, not a TLS failure.
        ("tls", ("ssl handshake", "tls handshake", "sslerror", "ssl error", "certificate verify", "certificate has expired", "certificate is not valid")),
        ("refused", ("refused",)),
        ("connection", ("connection", "cannot connect", "network is unreachable", "remote host")),
        ("unsupported_content", ("no supported node uris",)),
        ("empty", ("empty",)),
    ):
        if any(marker in message for marker in markers):
            return kind
    return "other"


def _source_failure_kind_counts(report: CollectionReport) -> dict[str, int]:
    """Failed units per failure class, so the all-failed event can say why."""

    counts: dict[str, int] = {}
    for source in report.sources:
        for unit in source.get("units") or []:
            if unit.get("ok") or unit.get("pending") or unit.get("status") == "deferred":
                continue
            kind = _source_failure_kind(unit.get("fallback_error") or unit.get("error") or "")
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def _log_source_failure_summaries(logger: logging.Logger, report: CollectionReport) -> None:
    # Use final per-unit facts, so a successful fallback does not leave a stale
    # diagnostic. This state is local to one collection and cannot grow across retries.
    # Individual sources may fail during useful refreshes; the collection-wide
    # sources_failed event remains the error signal when every source fails.
    for source in report.sources:
        failures: dict[str, int] = {}
        units = source.get("units") or []
        for unit in units:
            if unit.get("ok") or unit.get("pending") or unit.get("status") == "deferred":
                continue
            kind = _source_failure_kind(unit.get("fallback_error") or unit.get("error") or "")
            failures[kind] = failures.get(kind, 0) + 1
        for kind, count in sorted(failures.items()):
            # A repository portfolio intentionally probes paths that may not exist.
            # A wholly unavailable repository still has a summary and the all-failed event.
            if kind == "http_404" and source.get("type") == "github_repo" and source.get("ok"):
                continue
            logger.info(
                "Collection source failures for %s: reason=%s failed_units=%s "
                "total_units=%s source_status=%s",
                source.get("value") or source.get("id"),
                kind,
                count,
                len(units),
                source.get("status"),
                extra=RAW_ONLY_LOG_EXTRA,
            )


def _parse_content_blob_bounded(
    text: str,
    *,
    source: str,
    tier: str,
    logger: logging.Logger,
    cancel_event: threading.Event | None = None,
    node_template_cache: dict[str, Node] | None = None,
    node_template_cache_lock: threading.Lock | None = None,
    node_template_cache_limit: int = 0,
) -> list[Node]:
    return parse_content_blob(
        text,
        source=source,
        tier=tier,
        logger=logger,
        cancel_event=cancel_event,
        node_template_cache=node_template_cache,
        node_template_cache_lock=node_template_cache_lock,
        node_template_cache_limit=node_template_cache_limit,
    )


def source_observer_requests_stop(result: Any) -> bool:
    if isinstance(result, bool):
        return result
    if isinstance(result, dict):
        return bool(result.get("stop") or result.get("cancel"))
    if isinstance(result, str):
        return result.strip().lower() in {"stop", "cancel", "close", "closed"}
    return False


async def _run_blocking_cancellable(
    func: Callable[..., Any],
    *args: Any,
    cancel_event: threading.Event | None = None,
    cooperative_cancel: bool = False,
    poll_seconds: float = 0.01,
    drain_seconds: float = 1.0,
    **kwargs: Any,
) -> Any:
    """Run local CPU work and give cooperative cancellation a bounded drain."""
    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError

    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
    worker_cancel_event = threading.Event()
    worker_name = getattr(func, "__name__", "work")

    def worker() -> None:
        try:
            call_kwargs = dict(kwargs)
            if cooperative_cancel:
                call_kwargs["cancel_event"] = worker_cancel_event
            result_queue.put((True, func(*args, **call_kwargs)))
        except BaseException as exc:
            result_queue.put((False, exc))

    thread = threading.Thread(
        target=worker,
        name=f"collector-local-{worker_name}",
        daemon=True,
    )
    thread.start()
    interval = max(0.001, float(poll_seconds))

    async def cancel_and_drain() -> None:
        worker_cancel_event.set()
        deadline = time.monotonic() + max(0.0, float(drain_seconds))
        while thread.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(interval)
        if thread.is_alive():
            raise BlockingWorkCancellationTimeout(
                f"collector local worker did not stop after cancellation: {worker_name}"
            )

    async def drain_resisting_repeated_cancel() -> None:
        drain_task = asyncio.create_task(cancel_and_drain())
        while not drain_task.done():
            try:
                await asyncio.shield(drain_task)
            except asyncio.CancelledError:
                # A second Task.cancel() must not interrupt the bounded worker
                # drain started by the first cancellation.
                continue
        drain_task.result()

    try:
        while thread.is_alive():
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError
            await asyncio.sleep(interval)
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError
    except asyncio.CancelledError:
        await drain_resisting_repeated_cancel()
        raise
    ok, payload = result_queue.get_nowait()
    if ok:
        return payload
    raise payload


def source_fetch_error_should_try_runtime_proxy(error: str) -> bool:
    text = str(error or "").strip().lower()
    if not text:
        return False
    return any(marker in text for marker in SOURCE_FETCH_FALLBACK_ERROR_MARKERS)


def load_yaml(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_snapshot(path: str | Path) -> PoolSnapshot:
    snapshot_path = Path(path)
    if not snapshot_path.exists():
        return PoolSnapshot(generated_at=utc_now_iso(), nodes=[], metadata={})
    for attempt in range(3):
        try:
            with snapshot_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            return PoolSnapshot.from_dict(payload)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            # Windows readers can briefly race a replace performed by another
            # process. Do not turn that transient into an empty Tier3 pool.
            if attempt < 2:
                time.sleep(0.01 * (attempt + 1))
    return PoolSnapshot(
        generated_at=utc_now_iso(),
        nodes=[],
        metadata={"load_error": "invalid_snapshot"},
    )


def save_snapshot(
    snapshot: PoolSnapshot,
    path: str | Path,
    *,
    cancel_event: threading.Event | None = None,
) -> bool:
    if cancel_event is not None and cancel_event.is_set():
        return False
    snapshot_path = Path(path)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    if cancel_event is not None and cancel_event.is_set():
        return False
    payload = json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2)
    if cancel_event is not None and cancel_event.is_set():
        return False
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=snapshot_path.parent,
            delete=False,
            prefix=f".{snapshot_path.name}.",
            suffix=".tmp",
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if cancel_event is not None and cancel_event.is_set():
            return False
        replace_with_retry(
            temp_path,
            snapshot_path,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            return False
        return True
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _normalize_entries(entries: Iterable[Any], default_type: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for entry in entries or []:
        if isinstance(entry, str):
            normalized.append({"type": default_type, "value": entry, "enabled": True})
        elif isinstance(entry, dict):
            value = entry.get("value") or entry.get("url") or entry.get("path")
            if not value:
                continue
            normalized.append(
                {
                    "type": entry.get("type", default_type),
                    "value": value,
                    "enabled": coerce_bool(entry.get("enabled", True), True),
                    "headers": entry.get("headers", {}),
                    "tags": entry.get("tags", []),
                }
            )
    return [entry for entry in normalized if coerce_bool(entry.get("enabled", True), True)]


def source_entries_from_config(sources_config: dict[str, Any]) -> list[dict[str, Any]]:
    return source_entries_from_sources_config(sources_config)


def proxy_url_from_config(proxy_config: dict[str, Any] | None, purpose: str = "collection") -> str | None:
    if not proxy_config or not coerce_bool(proxy_config.get("enabled", False), False):
        return None
    use_for = proxy_config.get("use_for")
    if isinstance(use_for, dict) and purpose in use_for and not coerce_bool(use_for.get(purpose), True):
        return None

    server = str(proxy_config.get("server") or proxy_config.get("host") or "").strip()
    port = proxy_config.get("port")
    if not server or not port:
        return None

    scheme = str(proxy_config.get("scheme", "http")).strip() or "http"
    username = str(proxy_config.get("username", "") or "")
    password = str(proxy_config.get("password", "") or "")
    auth = ""
    if username or password:
        auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return f"{scheme}://{auth}{server}:{int(port)}"


async def collect_nodes_from_sources(
    source_entries: Iterable[dict[str, Any]],
    tier: str,
    logger: logging.Logger,
    settings: dict[str, Any] | None = None,
    cancel_event: threading.Event | None = None,
) -> list[Node]:
    nodes, _report = await collect_nodes_from_sources_with_report(
        source_entries=source_entries,
        tier=tier,
        logger=logger,
        settings=settings,
        cancel_event=cancel_event,
    )
    return nodes


async def collect_nodes_from_sources_with_report(
    source_entries: Iterable[dict[str, Any]],
    tier: str,
    logger: logging.Logger,
    settings: dict[str, Any] | None = None,
    source_observer: SourceBatchObserver | None = None,
    cancel_event: threading.Event | None = None,
    source_stop_event: threading.Event | None = None,
) -> tuple[list[Node], CollectionReport]:
    settings = settings or {}
    timeout_seconds = collection_timeout_seconds(settings)
    retries = collection_retries(settings)
    max_bytes = collection_max_bytes(settings)
    user_agents = collection_user_agents(settings, DEFAULT_USER_AGENTS)
    entries = list(source_entries)
    nodes: list[Node] = []
    configured_node_template_cache = settings.get("_source_node_template_cache")
    source_node_template_cache = (
        configured_node_template_cache
        if isinstance(configured_node_template_cache, dict)
        else {}
    )
    configured_node_template_cache_lock = settings.get(
        "_source_node_template_cache_lock"
    )
    source_node_template_cache_lock = (
        configured_node_template_cache_lock
        if configured_node_template_cache_lock is not None
        else threading.Lock()
    )
    source_node_template_cache_limit = max(
        1024,
        min(
            262_144,
            int(settings.get("source_node_template_cache_limit") or 65_536),
        ),
    )
    proxy_url = proxy_url_from_config(settings.get("egress_proxy"), purpose="collection")
    primary_egress_mode = "configured_proxy" if proxy_url else "direct"
    runtime_proxy_url = str(settings.get("runtime_source_fetch_proxy_url") or "").strip() or None
    runtime_proxy_first = coerce_bool(settings.get("runtime_source_fetch_proxy_first"), False)
    configured_runtime_endpoint_map = settings.get(
        "runtime_source_fetch_endpoints_by_authority"
    )
    runtime_endpoint_map = (
        dict(configured_runtime_endpoint_map)
        if isinstance(configured_runtime_endpoint_map, dict)
        else {}
    )
    runtime_route_success_observer = settings.get(
        "runtime_source_fetch_route_success_observer"
    )
    # Called with (authority, failed lane endpoints) when a route race ends
    # without a winner although at least one lane dispatched, so the owner can
    # stop re-leading the next retry with a lane that just failed.
    runtime_route_failure_observer = settings.get(
        "runtime_source_fetch_route_failure_observer"
    )
    runtime_endpoint_lease_validator = settings.get(
        "runtime_source_fetch_endpoint_lease_validator"
    )
    source_root_terminal_observer = settings.get(
        "source_root_terminal_observer"
    )
    source_unit_transport_started_observer = settings.get(
        "source_unit_transport_started_observer"
    )
    configured_content_facts = settings.get(
        "source_content_facts_by_authority"
    )
    source_content_facts_by_authority = (
        dict(configured_content_facts)
        if isinstance(configured_content_facts, dict)
        else {}
    )
    source_supply_phase = str(settings.get("source_supply_phase") or "").strip()
    require_remote_source_route = coerce_bool(
        settings.get("require_remote_source_route"),
        False,
    )
    source_first_result_deadline_seconds = max(
        0.1,
        float(settings.get("source_first_result_deadline_seconds") or 8.0),
    )
    source_unit_deadline_seconds = max(
        0.1,
        float(
            settings.get("source_unit_deadline_seconds")
            or source_first_result_deadline_seconds
        ),
    )
    source_cancel_slack_seconds = min(
        0.25,
        max(0.0, float(settings.get("source_cancel_slack_seconds") or 0.25)),
    )
    source_unit_delivery_slack_seconds = min(
        1.0,
        max(
            0.0,
            float(
                settings.get("source_unit_delivery_slack_seconds")
                if settings.get("source_unit_delivery_slack_seconds") is not None
                else source_cancel_slack_seconds
            ),
        ),
    )
    source_cancel_drain_seconds = min(
        0.9,
        max(
            source_cancel_slack_seconds,
            float(settings.get("source_cancel_drain_seconds") or 0.9),
        ),
    )
    source_root_concurrency = max(
        1,
        min(64, int(settings.get("source_root_concurrency") or max(1, len(entries)))),
    )
    if runtime_proxy_first and runtime_proxy_url:
        proxy_url = runtime_proxy_url
        primary_egress_mode = "runtime_proxy"
    elif runtime_proxy_url == proxy_url:
        runtime_proxy_url = None
    candidate_paths = list(settings.get("github_candidate_paths") or [])
    github_delivery = (
        settings.get("github_delivery")
        if isinstance(settings.get("github_delivery"), dict)
        else None
    )
    sources = []
    seen_source_ids: set[str] = set()
    for entry in entries:
        if (
            (cancel_event is not None and cancel_event.is_set())
            or (source_stop_event is not None and source_stop_event.is_set())
        ):
            break
        source = build_source(
            entry,
            candidate_paths=candidate_paths or None,
            fetch_profiles=settings.get('fetch_profiles'),
        )
        if source is None or source.source_id in seen_source_ids:
            continue
        seen_source_ids.add(source.source_id)
        sources.append(source)
    source_unit_budget = max(1, min(64, int(settings.get("source_unit_budget") or 6)))
    source_unit_concurrency = max(1, min(16, int(settings.get("source_unit_concurrency") or 6)))
    source_unit_transport_limit = max(
        1,
        min(
            min(48, 4 * source_unit_budget),
            int(
                settings.get("source_unit_transport_limit")
                or min(48, 4 * source_unit_budget)
            ),
        ),
    )
    source_unit_hedge_delay_seconds = max(
        0.0,
        min(
            1.0,
            float(settings.get("source_unit_hedge_delay_seconds") or 0.25),
        ),
    )
    source_unit_transport_broker = _Tier3SourceUnitTransportBroker(
        source_unit_transport_limit
    )
    # Parsing node blobs is Python-heavy.  Letting every root/unit race start a
    # parser thread at once increases wall time through GIL contention and can
    # starve the validator supervisor.  Transport concurrency remains
    # unchanged; only completed bodies enter this small CPU bulkhead.
    source_parse_concurrency = max(
        1,
        min(4, int(settings.get("source_parse_concurrency") or 3)),
    )
    source_bulk_parse_concurrency = max(
        1,
        min(
            source_parse_concurrency,
            int(settings.get("source_bulk_parse_concurrency") or 1),
        ),
    )
    source_large_parse_concurrency = max(
        1,
        min(
            source_parse_concurrency,
            int(settings.get("source_large_parse_concurrency") or 1),
        ),
    )
    source_large_parse_threshold_bytes = max(
        64 * 1024,
        int(settings.get("source_large_parse_threshold_bytes") or 512 * 1024),
    )
    source_priority_parse_threshold_bytes = max(
        16 * 1024,
        min(
            source_large_parse_threshold_bytes,
            int(
                settings.get("source_priority_parse_threshold_bytes")
                or 256 * 1024
            ),
        ),
    )
    source_priority_parse_candidate_limit = max(
        16,
        int(settings.get("source_priority_parse_candidate_limit") or 384),
    )
    source_parse_semaphore = asyncio.Semaphore(source_parse_concurrency)
    # Keep aggregate bodies on one CPU lane by default. They are Python-heavy
    # and otherwise contend with frozen helper startup while also delaying the
    # compact protocol units that supply the first validation wave. Operators
    # may raise the bounded bulk share explicitly; compact units still consume
    # only the total semaphore.
    source_bulk_parse_semaphore = (
        asyncio.Semaphore(source_bulk_parse_concurrency)
        if source_parse_concurrency > 1
        else None
    )
    source_large_parse_semaphore = asyncio.Semaphore(source_large_parse_concurrency)
    source_states: dict[str, dict[str, Any]] = {}
    source_units: dict[str, dict[str, dict[str, Any]]] = {}
    source_collection_started_at: dict[str, float] = {}
    runtime_route_context: dict[
        str,
        tuple[RuntimeSourceFetchEndpoint, tuple[RuntimeSourceRouteKind, ...]],
    ] = {}
    # Roots whose route race ended with no winning lane: their unit facts are
    # labelled with the attempted lanes instead of being credited to one lane.
    runtime_route_unattributed: dict[
        str,
        tuple[RuntimeSourceRouteKind, ...],
    ] = {}
    for source in sources:
        metadata = source.metadata()
        metadata.update(
            {
                "ok": False,
                "pending": True,
                "status": "pending",
                "error": "",
                "node_count": 0,
                "egress_mode": primary_egress_mode,
                "fallback_attempted": bool(
                    runtime_proxy_first and primary_egress_mode == "runtime_proxy"
                ),
                "retryable": False,
                "first_result_elapsed_ms": 0,
                "first_body_elapsed_ms": 0,
                "units": [],
            }
        )
        source_states[source.source_id] = metadata
        unit_descriptors = getattr(source, "unit_descriptors", None)
        configured_units = (
            unit_descriptors(unit_budget=source_unit_budget)
            if callable(unit_descriptors)
            else [{"id": "default", "value": source.value, "family": "default"}]
        )
        source_units[source.source_id] = {
            str(unit.get("id") or "default"): {
                "id": str(unit.get("id") or "default"),
                "value": str(unit.get("value") or source.value),
                "family": str(unit.get("family") or "default"),
                "ok": False,
                "pending": True,
                "status": "pending",
                "error": "",
                "retryable": False,
                "node_count": 0,
                "egress_mode": primary_egress_mode,
                "fallback_attempted": bool(
                    runtime_proxy_first and primary_egress_mode == "runtime_proxy"
                ),
                "fallback_error": "",
                "deadline_actual_ms": 0,
                "transport_started": False,
                "launch_tranche": "queued",
                "parse_valid": False,
                "observer_handoff": False,
            }
            for unit in configured_units
        }

    def source_unit_content_digest(result: SourceUnitResult) -> str:
        if not result.content:
            return ""
        content = (
            result.content
            if isinstance(result.content, bytes)
            else str(result.content).encode("utf-8", errors="surrogatepass")
        )
        return hashlib.sha256(content).hexdigest()

    def source_unit_previous_fact(
        source: Any,
        result: SourceUnitResult,
    ) -> dict[str, Any]:
        keys = (
            canonical_source_identity(source.source_type, source.value),
            str(source.source_id),
            str(result.root_id),
        )
        for key in keys:
            units = source_content_facts_by_authority.get(key)
            if not isinstance(units, dict):
                continue
            fact = units.get(str(result.unit_id))
            if isinstance(fact, dict):
                return dict(fact)
            if isinstance(fact, str):
                return {"content_digest": fact}
        return {}

    def source_unit_content_unchanged(
        source: Any,
        result: SourceUnitResult,
    ) -> bool:
        current_digest = source_unit_content_digest(result)
        if not current_digest:
            return False
        previous = source_unit_previous_fact(source, result)
        return bool(
            current_digest
            == str(previous.get("content_digest") or "").strip()
        )

    def runtime_endpoints_for_source(
        source: Any,
    ) -> tuple[RuntimeSourceFetchEndpoint, ...]:
        authority = canonical_source_identity(source.source_type, source.value)
        supplied = runtime_endpoint_map.get(authority)
        if supplied is None:
            supplied = runtime_endpoint_map.get(source.source_id)
        endpoints: list[RuntimeSourceFetchEndpoint] = []
        seen_routes: set[RuntimeSourceRouteKind] = set()
        for endpoint in tuple(supplied or ()):
            if not isinstance(endpoint, RuntimeSourceFetchEndpoint):
                continue
            if not endpoint.ready or endpoint.route_kind in seen_routes:
                continue
            seen_routes.add(endpoint.route_kind)
            endpoints.append(endpoint)
        return tuple(
            sorted(
                endpoints,
                key=lambda endpoint: (endpoint.priority, endpoint.stagger_ms),
            )
        )

    def runtime_endpoint_lease_is_current(
        source: Any,
        endpoint: RuntimeSourceFetchEndpoint,
    ) -> bool:
        if not callable(runtime_endpoint_lease_validator):
            return True
        authority = canonical_source_identity(source.source_type, source.value)
        try:
            return bool(
                runtime_endpoint_lease_validator(
                    source_supply_phase,
                    authority,
                    endpoint,
                )
            )
        except Exception:
            logger.warning(
                "Tier3 runtime source endpoint lease validation failed "
                "(phase=%s route=%s)",
                source_supply_phase,
                endpoint.route_kind.value,
                extra=RAW_ONLY_LOG_EXTRA,
            )
            return False

    def mark_runtime_source_route_pending(
        source: Any,
        *,
        attempted_routes: tuple[RuntimeSourceRouteKind, ...] = (),
    ) -> None:
        state = source_states[source.source_id]
        state["ok"] = False
        state["pending"] = True
        state["status"] = "pending"
        state["retryable"] = True
        state["error"] = "runtime_source_route_revalidation_pending"
        state["egress_mode"] = "runtime_proxy"
        state["runtime_source_route_race"] = True
        state["runtime_source_route_winner"] = ""
        state["runtime_source_route_dispatch_deferred"] = True
        state["runtime_source_routes_attempted"] = [
            route.value for route in attempted_routes
        ]
        for unit in source_units[source.source_id].values():
            if unit.get("ok"):
                continue
            unit["pending"] = True
            unit["status"] = "pending"
            unit["retryable"] = True
            unit["error"] = "runtime_source_route_revalidation_pending"
            unit["egress_mode"] = "runtime_proxy"

    observer_queue: queue.Queue[
        tuple[list[Node], CollectionReport, threading.Event, dict[str, Any]] | None
    ] | None = (
        queue.Queue() if source_observer is not None else None
    )
    observer_stop_requested = threading.Event()
    observer_dispatcher: threading.Thread | None = None
    if observer_queue is not None:
        def dispatch_source_observer() -> None:
            while True:
                item = observer_queue.get()
                try:
                    if item is None:
                        return
                    observed_nodes, observed_report, result_ready, result_holder = item
                    try:
                        if (
                            external_cancel_requested()
                            or observer_stop_requested.is_set()
                        ):
                            result_holder["stop"] = True
                            observer_stop_requested.set()
                        else:
                            observer_result = source_observer(
                                observed_nodes,
                                observed_report,
                            )
                            stop_requested = source_observer_requests_stop(observer_result)
                            result_holder["stop"] = stop_requested
                            if stop_requested:
                                observer_stop_requested.set()
                    except Exception as exc:
                        logger.warning(
                            "Tier3 source observer failed: %s",
                            exc,
                            extra=RAW_ONLY_LOG_EXTRA,
                        )
                    finally:
                        result_ready.set()
                finally:
                    observer_queue.task_done()

        observer_dispatcher = threading.Thread(
            target=dispatch_source_observer,
            name="tier3-source-observer-dispatcher",
            daemon=True,
        )
        observer_dispatcher.start()

    def external_cancel_requested() -> bool:
        return bool(cancel_event is not None and cancel_event.is_set())

    def source_phase_stop_requested() -> bool:
        return bool(source_stop_event is not None and source_stop_event.is_set())

    def collection_stop_requested() -> bool:
        return bool(
            external_cancel_requested()
            or source_phase_stop_requested()
            or observer_stop_requested.is_set()
        )

    async def cancel_and_wait_tasks(
        pending_tasks: set[asyncio.Task[Any]],
        *,
        timeout: float,
    ) -> tuple[set[asyncio.Task[Any]], set[asyncio.Task[Any]], bool]:
        if not pending_tasks:
            return set(), set(), False
        for pending_task in pending_tasks:
            pending_task.cancel()
        waiter = asyncio.create_task(
            asyncio.wait(pending_tasks, timeout=max(0.0, float(timeout)))
        )
        interrupted = False
        while not waiter.done():
            try:
                await asyncio.shield(waiter)
            except asyncio.CancelledError:
                interrupted = True
                for pending_task in pending_tasks:
                    if not pending_task.done():
                        pending_task.cancel()
        done, still_pending = waiter.result()
        return done, still_pending, interrupted

    def report_sources() -> list[dict[str, Any]]:
        rendered: list[dict[str, Any]] = []
        for source in sources:
            state = dict(source_states[source.source_id])
            state["units"] = [dict(unit) for unit in source_units[source.source_id].values()]
            rendered.append(state)
        return rendered

    def build_report(*, node_count_override: int | None = None) -> CollectionReport:
        rendered = report_sources()
        units = [unit for source in rendered for unit in source.get("units", [])]
        return CollectionReport(
            source_count=len(sources),
            configured_source_count=len(sources),
            fetched_sources=sum(1 for source in rendered if source.get("ok")),
            failed_sources=sum(
                1
                for source in rendered
                if not source.get("ok") and not source.get("pending")
            ),
            node_count=(
                len(deduplicate_nodes(nodes, cancel_event=cancel_event))
                if node_count_override is None
                else max(0, int(node_count_override))
            ),
            sources=rendered,
            discovered_source_units=len(units),
            fetched_source_units=sum(1 for unit in units if unit.get("ok")),
            failed_source_units=sum(
                1 for unit in units if not unit.get("ok") and not unit.get("pending")
            ),
            pending_source_units=sum(1 for unit in units if unit.get("pending")),
            retry_due_source_units=sum(
                1 for unit in units if not unit.get("ok") and unit.get("retryable")
            ),
        )

    def coerce_unit_result(source: Any, result: Any, *, egress_mode: str) -> SourceUnitResult:
        if isinstance(result, SourceUnitResult):
            return replace(result, egress_mode=result.egress_mode or egress_mode)
        if isinstance(result, SourceFetchResult):
            return SourceUnitResult(
                root_id=source.source_id,
                root_type=source.source_type,
                root_value=source.value,
                origin=source.origin,
                unit_id="default",
                unit_value=source.value,
                unit_family="default",
                ok=result.ok,
                content=result.content,
                error=result.error,
                egress_mode=result.egress_mode or egress_mode,
                fallback_attempted=result.fallback_attempted,
                fallback_error=result.fallback_error,
                deadline_actual_ms=result.deadline_actual_ms,
                transport_elapsed_ms=(
                    result.transport_elapsed_ms or result.deadline_actual_ms
                ),
                first_byte_elapsed_ms=result.first_byte_elapsed_ms,
                transport_started=True,
                delivery_path=result.delivery_path,
                delivery_outcomes=result.delivery_outcomes,
            )
        raise TypeError(f"unsupported source unit result: {type(result).__name__}")

    async def notify_source_observer(parsed: list[Node]) -> bool:
        if collection_stop_requested():
            return True
        if observer_queue is None:
            return False
        # Progress snapshots do not need an exact cross-unit deduplication.
        # Re-running it for every streamed unit made the collector event loop
        # increasingly expensive as large roots accumulated thousands of
        # candidates. The final report still performs the exact deduplication.
        report_snapshot = build_report(node_count_override=len(nodes))
        if collection_stop_requested():
            return True
        started = time.perf_counter()
        result_ready = threading.Event()
        result_holder: dict[str, Any] = {}
        observer_queue.put_nowait(
            (list(parsed), report_snapshot, result_ready, result_holder)
        )
        elapsed_ms = int(round((time.perf_counter() - started) * 1000))
        # Stop acknowledgements are best-effort and bounded. Normal manager
        # observers return immediately; a slow observer must not stretch root
        # deadlines or source-to-TCP handoff.
        wait_deadline = asyncio.get_running_loop().time() + 0.005
        while not result_ready.is_set() and asyncio.get_running_loop().time() < wait_deadline:
            await asyncio.sleep(0.001)
        if elapsed_ms > 50:
            logger.warning(
                "Tier3 source observer handoff exceeded 50ms (elapsed_ms=%s)",
                elapsed_ms,
                extra=RAW_ONLY_LOG_EXTRA,
            )
        return bool(result_holder.get("stop") or observer_stop_requested.is_set())

    payload_parse_tasks: dict[tuple[str, str], asyncio.Task[list[Node]]] = {}
    payload_parse_elapsed_ms: dict[tuple[str, str], int] = {}

    def payload_parse_key(content: bytes | str, source_value: str) -> tuple[str, str]:
        raw = content if isinstance(content, bytes) else content.encode("utf-8", errors="surrogatepass")
        return source_value, hashlib.sha256(raw).hexdigest()

    async def parse_source_content(
        content: bytes | str, *, source_value: str,
    ) -> list[Node]:
        key = payload_parse_key(content, source_value)
        task = payload_parse_tasks.get(key)
        if task is None:
            async def parse() -> list[Node]:
                started = time.perf_counter()
                try:
                    return await parse_source_content_uncached(content, source_value=source_value)
                finally:
                    payload_parse_elapsed_ms[key] = int(round((time.perf_counter() - started) * 1000))
            task = asyncio.create_task(parse())
            payload_parse_tasks[key] = task
        return await asyncio.shield(task)

    async def release_payload_parsers() -> None:
        pending_parsers = {task for task in payload_parse_tasks.values() if not task.done()}
        if pending_parsers:
            _done, remaining, interrupted = await cancel_and_wait_tasks(
                pending_parsers, timeout=min(1.0, source_cancel_drain_seconds + 0.1),
            )
            for task in _done:
                if not task.cancelled():
                    error = task.exception()
                    if isinstance(error, BlockingWorkCancellationTimeout):
                        raise error
            if remaining:
                raise BlockingWorkCancellationTimeout("Tier3 source parsers did not stop")
            if interrupted:
                raise asyncio.CancelledError
        payload_parse_tasks.clear()

    async def parse_source_content_uncached(
        content: bytes | str,
        *,
        source_value: str,
    ) -> list[Node]:
        raw_content = (
            content
            if isinstance(content, bytes)
            else str(content).encode("utf-8", errors="surrogatepass")
        )
        content_size = len(raw_content)
        candidate_markers = raw_content.count(b"://")
        priority_unit = bool(
            content_size <= source_priority_parse_threshold_bytes
            and candidate_markers <= source_priority_parse_candidate_limit
        )

        async def run_parse() -> list[Node]:
            async with source_parse_semaphore:
                return await _run_blocking_cancellable(
                    _parse_content_blob_bounded,
                    content,
                    source=source_value,
                    tier=tier,
                    logger=logger,
                    cancel_event=cancel_event,
                    node_template_cache=source_node_template_cache,
                    node_template_cache_lock=source_node_template_cache_lock,
                    node_template_cache_limit=source_node_template_cache_limit,
                    cooperative_cancel=True,
                )

        async def run_bulk_parse() -> list[Node]:
            if source_bulk_parse_semaphore is None:
                return await run_parse()
            async with source_bulk_parse_semaphore:
                return await run_parse()

        if priority_unit:
            return await run_parse()
        if content_size >= source_large_parse_threshold_bytes:
            # Keep very large aggregate subscriptions serialized inside the
            # bulk share as an additional memory/GIL guardrail.
            async with source_large_parse_semaphore:
                return await run_bulk_parse()
        return await run_bulk_parse()

    async def ingest_source_unit(
        source: Any,
        result: SourceUnitResult,
        *,
        parsed_override: list[Node] | None = None,
        parse_elapsed_ms: int | None = None,
    ) -> tuple[list[Node], bool]:
        if collection_stop_requested():
            return [], True
        root_state = source_states[source.source_id]
        collection_started_at = source_collection_started_at.get(source.source_id)
        root_elapsed_ms = 0
        if collection_started_at is not None:
            root_elapsed_ms = int(
                round((time.perf_counter() - collection_started_at) * 1000)
            )
            if not int(root_state.get("first_result_elapsed_ms") or 0):
                root_state["first_result_elapsed_ms"] = root_elapsed_ms
            if (
                result.content
                and not int(root_state.get("first_body_elapsed_ms") or 0)
            ):
                root_state["first_body_elapsed_ms"] = root_elapsed_ms
        retryable = bool(
            not result.ok and source_fetch_error_should_try_runtime_proxy(result.error)
        )
        content_digest = source_unit_content_digest(result)
        previous_fact = source_unit_previous_fact(source, result)
        content_unchanged = bool(
            content_digest
            and content_digest
            == str(previous_fact.get("content_digest") or "").strip()
        )
        unit_metadata = {
            "id": result.unit_id,
            "value": result.unit_value,
            "family": result.unit_family,
            "ok": bool(result.ok and result.content),
            "pending": False,
            "status": "ok" if result.ok and result.content else "failed",
            "error": result.error,
            "retryable": retryable,
            "node_count": (
                max(0, int(previous_fact.get("node_count") or 0))
                if content_unchanged
                else 0
            ),
            "egress_mode": result.egress_mode or primary_egress_mode,
            "fallback_attempted": bool(result.fallback_attempted),
            "fallback_error": result.fallback_error,
            "deadline_actual_ms": int(result.deadline_actual_ms or 0),
            "transport_elapsed_ms": int(
                result.transport_elapsed_ms or result.deadline_actual_ms or 0
            ),
            "queued_elapsed_ms": max(0, int(result.queued_elapsed_ms or 0)),
            "transport_started_elapsed_ms": max(
                0,
                int(result.transport_started_elapsed_ms or 0),
            ),
            "first_byte_elapsed_ms": max(
                0,
                int(result.first_byte_elapsed_ms or 0),
            ),
            "launch_tranche": str(result.launch_tranche or "primary"),
            "delivery_path": str(result.delivery_path or ""),
            "delivery_outcomes": str(result.delivery_outcomes or ""),
            "transport_started": bool(result.transport_started),
            "parse_elapsed_ms": max(0, int(parse_elapsed_ms or 0)),
            "parse_valid_elapsed_ms": 0,
            "observer_handoff_elapsed_ms": 0,
            "parse_valid": False,
            "observer_handoff": False,
            "content_digest": content_digest,
            "content_unchanged": content_unchanged,
        }
        route_context = runtime_route_context.get(source.source_id)
        if route_context is not None:
            route_endpoint, attempted_routes = route_context
            unit_metadata["runtime_source_route_kind"] = (
                route_endpoint.route_kind.value
            )
            unit_metadata["runtime_source_routes_attempted"] = [
                route.value for route in attempted_routes
            ]
        elif source.source_id in runtime_route_unattributed:
            # No lane won: the file failed on every attempted lane, so no
            # single lane is credited with it.
            unit_metadata["runtime_source_route_kind"] = ""
            unit_metadata["runtime_source_routes_attempted"] = [
                route.value
                for route in runtime_route_unattributed[source.source_id]
            ]
        parsed: list[Node] = []
        if result.ok and result.content:
            if content_unchanged:
                parsed = []
                unit_metadata["parse_elapsed_ms"] = 0
            elif parsed_override is None:
                parse_started = time.perf_counter()
                parsed = await parse_source_content(
                    result.content,
                    source_value=result.root_value,
                )
                unit_metadata["parse_elapsed_ms"] = int(
                    round((time.perf_counter() - parse_started) * 1000)
                )
                unit_metadata["parse_elapsed_ms"] = payload_parse_elapsed_ms.get(
                    payload_parse_key(result.content, result.root_value),
                    unit_metadata["parse_elapsed_ms"],
                )
            else:
                parsed = list(parsed_override)
            if collection_stop_requested():
                return [], True
            if parsed and collection_started_at is not None:
                unit_metadata["parse_valid_elapsed_ms"] = int(
                    round((time.perf_counter() - collection_started_at) * 1000)
                )
                unit_metadata["parse_valid"] = True
            if not parsed and not content_unchanged:
                unit_metadata["ok"] = False
                unit_metadata["status"] = "failed"
                unit_metadata["error"] = (
                    result.error or f"{result.unit_value} contained no supported node URIs"
                )
            for node in parsed:
                node.tier = tier
                node.extra["source_parsed_from_content"] = True
                node.extra["source_origin"] = result.origin
                node.extra["source_type"] = result.root_type
                node.extra["source_root_id"] = result.root_id
                node.extra["source_unit_id"] = result.unit_id
                node.extra["source_path"] = result.unit_id
                node.extra["source_unit_family"] = result.unit_family
                if tier == "tier3":
                    node.extra["tier3_origin"] = result.origin
                    node.extra["tier3_source_parsed_from_content"] = True
                    node.extra["tier3_source_root_id"] = result.root_id
                    node.extra["tier3_source_unit_id"] = result.unit_id
                    node.extra["tier3_source_path"] = result.unit_id
                    node.extra["tier3_source_unit_family"] = result.unit_family
                    if route_context is not None:
                        route_endpoint, _attempted_routes = route_context
                        node.extra["tier3_source_fetch_route_kind"] = (
                            route_endpoint.route_kind.value
                        )
                        node.extra["tier3_source_fetch_runtime_generation"] = (
                            route_endpoint.runtime_generation
                        )
                        node.extra["tier3_source_fetch_config_generation"] = (
                            route_endpoint.config_generation
                        )
                else:
                    node.extra.pop("tier3_origin", None)
            nodes.extend(parsed)
            if not content_unchanged:
                unit_metadata["node_count"] = len(parsed)
        elif result.error:
            logger.debug(
                "Collection failed for %s unit %s: %s",
                source.value,
                result.unit_id,
                result.error,
                extra=RAW_ONLY_LOG_EXTRA,
            )
        result.acknowledge_parse_valid(bool(unit_metadata["ok"]))
        source_units[source.source_id][result.unit_id] = unit_metadata
        root_state["ok"] = bool(
            root_state.get("ok") or parsed or unit_metadata.get("ok")
        )
        if root_state["ok"]:
            root_state["pending"] = False
            root_state["status"] = (
                "partial"
                if any(unit.get("pending") for unit in source_units[source.source_id].values())
                else "ok"
            )
        root_state["node_count"] = sum(
            int(unit.get("node_count") or 0)
            for unit in source_units[source.source_id].values()
        )
        root_state["egress_mode"] = unit_metadata["egress_mode"]
        root_state["fallback_attempted"] = bool(
            root_state.get("fallback_attempted") or unit_metadata["fallback_attempted"]
        )
        root_state["retryable"] = bool(root_state.get("retryable") or retryable)
        root_state["deadline_actual_ms"] = max(
            int(root_state.get("deadline_actual_ms") or 0),
            int(unit_metadata["deadline_actual_ms"] or 0),
        )
        if route_context is not None:
            route_endpoint, attempted_routes = route_context
            root_state["runtime_source_route_race"] = True
            root_state["runtime_source_route_winner"] = (
                route_endpoint.route_kind.value
            )
            root_state["runtime_source_routes_attempted"] = [
                route.value for route in attempted_routes
            ]
        if result.fallback_error:
            root_state["fallback_error"] = result.fallback_error
        if unit_metadata.get("error") and not root_state.get("error"):
            root_state["error"] = str(unit_metadata["error"])
        if parsed and collection_started_at is not None:
            unit_metadata["observer_handoff_elapsed_ms"] = int(
                round((time.perf_counter() - collection_started_at) * 1000)
            )
            unit_metadata["observer_handoff"] = True
        return parsed, await notify_source_observer(parsed)

    async def iter_source_units(
        source: Any,
        *,
        active_proxy_url: str | None,
        egress_mode: str,
        fallback_attempted: bool,
        route_key: str = "default",
        route_endpoint: RuntimeSourceFetchEndpoint | None = None,
    ):
        if collection_stop_requested():
            return
        unit_deadline_seconds = source_unit_deadline_seconds
        @asynccontextmanager
        async def acquire_transport(root: str, route: str):
            async with source_unit_transport_broker.lease(root, route):
                if route_endpoint is not None and not runtime_endpoint_lease_is_current(
                    source, route_endpoint,
                ):
                    raise SourceDispatchDeferred('runtime_source_route_revalidation_pending')
                yield

        async def validate_delivery_body(unit: SourceUnitResult) -> bool:
            if collection_stop_requested():
                raise asyncio.CancelledError
            if source_unit_content_unchanged(source, unit):
                return True
            return bool(await parse_source_content(unit.content, source_value=unit.root_value))

        fetch_units = getattr(source, "fetch_units", None)
        if callable(fetch_units):
            fetch_kwargs: dict[str, Any] = {
                "timeout_seconds": timeout_seconds,
                "retries": retries,
                "user_agent": random.choice(user_agents),
                "proxy_url": active_proxy_url,
                "max_bytes": max_bytes,
                "unit_budget": source_unit_budget,
                "unit_concurrency": source_unit_concurrency,
                "unit_deadline_seconds": unit_deadline_seconds,
                "cancel_slack_seconds": source_cancel_slack_seconds,
                "delivery_slack_seconds": source_unit_delivery_slack_seconds,
            }
            try:
                fetch_signature = inspect.signature(fetch_units)
                accepts_kwargs = any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in fetch_signature.parameters.values()
                )
            except (TypeError, ValueError):
                fetch_signature = None
                accepts_kwargs = False
            optional_fetch_kwargs = {
                "unit_transport_acquire": acquire_transport,
                "unit_transport_started_observer": (
                    source_unit_transport_started_observer
                ),
                "unit_route_key": str(route_key or egress_mode or "default"),
                "unit_hedge_delay_seconds": source_unit_hedge_delay_seconds,
                "unit_content_validator": validate_delivery_body,
            }
            for key, value in optional_fetch_kwargs.items():
                if (
                    accepts_kwargs
                    or fetch_signature is not None
                    and key in fetch_signature.parameters
                ):
                    fetch_kwargs[key] = value
            async for result in fetch_units(session, **fetch_kwargs):
                if collection_stop_requested():
                    return
                unit = coerce_unit_result(source, result, egress_mode=egress_mode)
                if fallback_attempted:
                    unit = replace(unit, fallback_attempted=True)
                yield unit
            return
        try:
            result = await asyncio.wait_for(
                source.fetch(
                    session,
                    timeout_seconds=timeout_seconds,
                    retries=retries,
                    user_agent=random.choice(user_agents),
                    proxy_url=active_proxy_url,
                    max_bytes=max_bytes,
                ),
                timeout=(
                    unit_deadline_seconds + source_unit_delivery_slack_seconds
                ),
            )
        except asyncio.TimeoutError:
            result = SourceFetchResult(
                source.source_id,
                source.source_type,
                source.value,
                source.origin,
                ok=False,
                error=(
                    "source first-result timeout after "
                    f"{unit_deadline_seconds:.2f}s"
                ),
                deadline_actual_ms=int(round(unit_deadline_seconds * 1000)),
            )
        if collection_stop_requested():
            return
        unit = coerce_unit_result(source, result, egress_mode=egress_mode)
        if fallback_attempted:
            unit = replace(unit, fallback_attempted=True)
        yield unit

    async def parse_runtime_route_unit(
        result: SourceUnitResult,
    ) -> tuple[list[Node], int]:
        if not result.ok or not result.content:
            return [], 0
        started = time.perf_counter()
        parsed = await parse_source_content(
            result.content,
            source_value=result.root_value,
        )
        return parsed, payload_parse_elapsed_ms.get(
            payload_parse_key(result.content, result.root_value),
            int(round((time.perf_counter() - started) * 1000)),
        )

    async def collect_source_via_runtime_endpoints(
        source: Any,
        endpoints: tuple[RuntimeSourceFetchEndpoint, ...],
    ) -> tuple[bool, bool]:
        route_buffers: dict[
            RuntimeSourceRouteKind,
            _BufferedRuntimeSourceFetch,
        ] = {}
        lease_valid_routes: set[RuntimeSourceRouteKind] = set()
        route_parse_tasks: dict[
            str,
            asyncio.Task[tuple[list[Node], int]],
        ] = {}

        async def parse_runtime_route_unit_once(
            result: SourceUnitResult,
        ) -> tuple[list[Node], int]:
            content = result.content
            if not result.ok or not content:
                return [], 0
            if source_unit_content_unchanged(source, result):
                return [], 0
            raw_content = (
                content
                if isinstance(content, bytes)
                else str(content).encode("utf-8", errors="surrogatepass")
            )
            content_digest = hashlib.sha256(raw_content).hexdigest()
            parse_task = route_parse_tasks.get(content_digest)
            if parse_task is None:
                parse_task = asyncio.create_task(parse_runtime_route_unit(result))
                route_parse_tasks[content_digest] = parse_task
            try:
                # A losing route may be cancelled as soon as another route
                # wins. Shield the shared parse so its peer can still consume
                # the same immutable result instead of parsing a large body
                # again on another executor thread.
                return await asyncio.shield(parse_task)
            except BaseException:
                if parse_task.done():
                    route_parse_tasks.pop(content_digest, None)
                raise

        async def close_route_buffer(result: _BufferedRuntimeSourceFetch) -> None:
            iterator = result.iterator
            if iterator is None:
                return
            close = getattr(iterator, "aclose", None)
            if callable(close):
                try:
                    await close()
                except BaseException:
                    pass

        async def fetch_route(
            endpoint: RuntimeSourceFetchEndpoint,
        ) -> _BufferedRuntimeSourceFetch:
            if not runtime_endpoint_lease_is_current(source, endpoint):
                result = _BufferedRuntimeSourceFetch(
                    endpoint=endpoint,
                    iterator=None,
                    buffered=[],
                    exhausted=False,
                    lease_valid=False,
                )
                route_buffers[endpoint.route_kind] = result
                return result
            lease_valid_routes.add(endpoint.route_kind)
            iterator = iter_source_units(
                source,
                active_proxy_url=endpoint.proxy_url,
                egress_mode="runtime_proxy",
                fallback_attempted=True,
                route_key=endpoint.route_kind.value,
                route_endpoint=endpoint,
            ).__aiter__()
            buffered: list[tuple[SourceUnitResult, list[Node], int]] = []
            try:
                while not collection_stop_requested():
                    try:
                        unit = await iterator.__anext__()
                    except StopAsyncIteration:
                        result = _BufferedRuntimeSourceFetch(
                            endpoint=endpoint,
                            iterator=iterator,
                            buffered=buffered,
                            exhausted=True,
                        )
                        route_buffers[endpoint.route_kind] = result
                        return result
                    parsed, parse_elapsed_ms = await parse_runtime_route_unit_once(unit)
                    buffered.append((unit, parsed, parse_elapsed_ms))
                    if parsed:
                        result = _BufferedRuntimeSourceFetch(
                            endpoint=endpoint,
                            iterator=iterator,
                            buffered=buffered,
                            exhausted=False,
                        )
                        route_buffers[endpoint.route_kind] = result
                        return result
                raise asyncio.CancelledError
            except BaseException as exc:
                close = getattr(iterator, "aclose", None)
                if callable(close):
                    try:
                        await close()
                    except BaseException:
                        pass
                if isinstance(exc, SourceDispatchDeferred):
                    lease_valid_routes.discard(endpoint.route_kind)
                    result = _BufferedRuntimeSourceFetch(
                        endpoint=endpoint, iterator=None, buffered=buffered,
                        exhausted=False, lease_valid=False,
                    )
                    route_buffers[endpoint.route_kind] = result
                    return result
                raise

        async def validate_route(
            _endpoint: RuntimeSourceFetchEndpoint,
            result: _BufferedRuntimeSourceFetch,
        ) -> bool:
            return bool(
                result.lease_valid
                and any(
                    bool(parsed) or source_unit_content_unchanged(source, unit)
                    for unit, parsed, _elapsed in result.buffered
                )
            )

        async def record_winner(
            endpoint: RuntimeSourceFetchEndpoint,
            _result: _BufferedRuntimeSourceFetch,
        ) -> None:
            if not callable(runtime_route_success_observer):
                return
            authority = canonical_source_identity(source.source_type, source.value)
            try:
                runtime_route_success_observer(authority, endpoint)
            except Exception:
                logger.warning(
                    "Tier3 runtime source route success observer failed "
                    "(route=%s)",
                    endpoint.route_kind.value,
                    extra=RAW_ONLY_LOG_EXTRA,
                )

        def record_route_failure() -> None:
            # Only lanes that really dispatched failed; a deferred lease is a
            # route revalidation, not evidence against the lane.
            if not callable(runtime_route_failure_observer):
                return
            failed_endpoints = tuple(
                endpoint
                for endpoint in endpoints
                if endpoint.route_kind in lease_valid_routes
            )
            if not failed_endpoints:
                return
            authority = canonical_source_identity(source.source_type, source.value)
            try:
                runtime_route_failure_observer(authority, failed_endpoints)
            except Exception:
                logger.warning(
                    "Tier3 runtime source route failure observer failed "
                    "(routes=%s)",
                    ",".join(
                        endpoint.route_kind.value for endpoint in failed_endpoints
                    ),
                    extra=RAW_ONLY_LOG_EXTRA,
                )

        def lane_failure_kind(lane: _BufferedRuntimeSourceFetch) -> str:
            for unit, parsed, _elapsed in lane.buffered:
                if parsed:
                    continue
                if unit.ok and unit.content:
                    return "unsupported_content"
                return _source_failure_kind(unit.error)
            return ""

        def route_error_summary(errors: tuple[str, ...]) -> list[str]:
            """Per-lane race outcomes that are safe to persist and display.

            Entries are ``route:ExceptionType`` from the race, or
            ``route:rejected[:failure_kind]`` / ``route:deferred`` for lanes
            that completed without a parsed result.  Raw error text (which can
            carry hosts or URLs) never enters this list.
            """

            summary: list[str] = []
            for raw_error in errors:
                route_value, separator, detail = str(raw_error or "").partition(":")
                if not separator or not _RUNTIME_ROUTE_ERROR_DETAIL.fullmatch(detail):
                    continue
                try:
                    route_kind = RuntimeSourceRouteKind(route_value)
                except ValueError:
                    continue
                lane = route_buffers.get(route_kind)
                if detail == "rejected" and lane is not None:
                    if not lane.lease_valid:
                        detail = "deferred"
                    else:
                        kind = lane_failure_kind(lane)
                        if kind:
                            detail = f"rejected:{kind}"
                summary.append(f"{route_kind.value}:{detail}")
            return summary

        def unattributed_route_unit_facts(
            lanes: list[_BufferedRuntimeSourceFetch],
        ) -> list[tuple[SourceUnitResult, list[Node], int]]:
            """Per-file facts of a race that no lane won.

            Each file keeps the first dispatched lane's answer, as before,
            unless a later lane learned a fact about the file itself: a body
            arrived (it held no supported nodes), the body was empty, or the
            file is gone (HTTP 404/410).  Any other failure -- a timeout, a
            dropped connection, 5xx, 429, 403, or a proxy's own 502 when its
            exit cannot reach the source -- can be lane-local, so it never
            replaces another lane's answer; otherwise one lane's transient
            answer could turn a still-retryable source terminal and shrink
            supply.  Adopting every file of the first lane let a lane-local
            fault (for example an unusable FINAL-EXIT node) hide a real 404.
            """

            def file_fact(unit: SourceUnitResult) -> bool:
                if unit.ok:
                    return True
                error = str(unit.error or "").strip().lower()
                return bool(
                    error == "empty"
                    or _source_failure_kind(error) in {"http_404", "http_410"}
                )

            chosen: dict[str, tuple[SourceUnitResult, list[Node], int]] = {}
            for lane in lanes:
                for fact in lane.buffered:
                    current = chosen.get(fact[0].unit_id)
                    if current is None or (
                        not file_fact(current[0]) and file_fact(fact[0])
                    ):
                        chosen[fact[0].unit_id] = fact
            return list(chosen.values())

        race_result: RuntimeSourceFetchRaceResult[_BufferedRuntimeSourceFetch]
        try:
            race_result = await race_runtime_source_fetch(
                endpoints,
                fetch_route,
                validate_route,
                on_winner=record_winner,
            )
        except BaseException:
            for result in tuple(route_buffers.values()):
                await close_route_buffer(result)
            raise
        attempted_routes = tuple(race_result.attempted_routes)
        winner = race_result.winner
        selected = race_result.value if winner is not None else None
        state = source_states[source.source_id]
        if lease_valid_routes:
            state["fallback_attempted"] = True
        # Written for every race, so a merged root history never keeps the
        # lane errors of an older attempt after a later success.
        state["runtime_source_route_errors"] = route_error_summary(
            tuple(race_result.errors)
        )
        if winner is not None and selected is not None:
            runtime_route_context[source.source_id] = (
                winner,
                attempted_routes,
            )
        elif lease_valid_routes and not collection_stop_requested():
            record_route_failure()

        try:
            if not lease_valid_routes:
                mark_runtime_source_route_pending(
                    source,
                    attempted_routes=attempted_routes,
                )
                return False, True
            if selected is None:
                root_state = source_states[source.source_id]
                root_state["runtime_source_route_race"] = True
                root_state["runtime_source_route_winner"] = ""
                root_state["runtime_source_routes_attempted"] = [
                    route.value for route in attempted_routes
                ]
                runtime_route_unattributed[source.source_id] = attempted_routes
                dispatched_lanes = [
                    route_buffers[endpoint.route_kind]
                    for endpoint in endpoints
                    if endpoint.route_kind in route_buffers
                    and route_buffers[endpoint.route_kind].lease_valid
                ]
                if dispatched_lanes:
                    for unit, parsed, parse_elapsed_ms in (
                        unattributed_route_unit_facts(dispatched_lanes)
                    ):
                        _parsed, should_stop = await ingest_source_unit(
                            source,
                            unit,
                            parsed_override=parsed,
                            parse_elapsed_ms=parse_elapsed_ms,
                        )
                        if should_stop:
                            return True, False
                    return False, False
                for unit in list(source_units[source.source_id].values()):
                    _parsed, should_stop = await ingest_source_unit(
                        source,
                        SourceUnitResult(
                            root_id=source.source_id,
                            root_type=source.source_type,
                            root_value=source.value,
                            origin=source.origin,
                            unit_id=str(unit.get("id") or "default"),
                            unit_value=str(unit.get("value") or source.value),
                            unit_family=str(unit.get("family") or "default"),
                            ok=False,
                            error="connection failed through runtime source routes",
                            egress_mode="runtime_proxy",
                            fallback_attempted=True,
                        ),
                    )
                    if should_stop:
                        return True, False
                return False, False
            for unit, parsed, parse_elapsed_ms in selected.buffered:
                _parsed, should_stop = await ingest_source_unit(
                    source,
                    unit,
                    parsed_override=parsed,
                    parse_elapsed_ms=parse_elapsed_ms,
                )
                if should_stop:
                    return True, False
            while not collection_stop_requested():
                try:
                    if selected.iterator is None:
                        break
                    unit = await selected.iterator.__anext__()
                except StopAsyncIteration:
                    break
                except SourceDispatchDeferred:
                    mark_runtime_source_route_pending(source, attempted_routes=attempted_routes)
                    return False, True
                parsed, parse_elapsed_ms = await parse_runtime_route_unit(unit)
                _parsed, should_stop = await ingest_source_unit(
                    source,
                    unit,
                    parsed_override=parsed,
                    parse_elapsed_ms=parse_elapsed_ms,
                )
                if should_stop:
                    return True, False
            return False, False
        finally:
            for result in tuple(route_buffers.values()):
                await close_route_buffer(result)
            pending_parse_tasks = [
                task for task in route_parse_tasks.values() if not task.done()
            ]
            for task in pending_parse_tasks:
                task.cancel()
            if pending_parse_tasks:
                await asyncio.gather(*pending_parse_tasks, return_exceptions=True)

    async def finalize_source_state(source: Any) -> None:
        state = source_states[source.source_id]
        state["pending"] = False
        resolved_units = list(source_units[source.source_id].values())
        if state.get("ok"):
            state["status"] = (
                "partial"
                if any(not unit.get("ok") for unit in resolved_units)
                else "ok"
            )
        else:
            state["status"] = "failed"
        state["retryable"] = bool(
            not state.get("ok")
            and (
                state.get("retryable")
                or source_fetch_error_should_try_runtime_proxy(
                    str(state.get("error") or "")
                )
            )
        )
        for unit in source_units[source.source_id].values():
            if not unit.get("pending"):
                continue
            unit["pending"] = False
            unit["status"] = "failed"
            unit["error"] = str(
                state.get("error") or "source unit did not complete"
            )
            unit["retryable"] = bool(state.get("retryable"))
        if callable(source_root_terminal_observer):
            terminal_payload = dict(state)
            terminal_payload["units"] = [
                dict(unit) for unit in source_units[source.source_id].values()
            ]
            terminal_started_at = time.perf_counter()
            try:
                source_root_terminal_observer(terminal_payload)
            except Exception:
                logger.warning(
                    "Tier3 source root terminal observer failed for %s",
                    source.value,
                    exc_info=True,
                    extra=RAW_ONLY_LOG_EXTRA,
                )
            terminal_elapsed_ms = int(
                round((time.perf_counter() - terminal_started_at) * 1000)
            )
            if terminal_elapsed_ms > 50:
                logger.warning(
                    "Tier3 source root terminal handoff exceeded 50ms "
                    "(source=%s elapsed_ms=%s)",
                    source.value,
                    terminal_elapsed_ms,
                    extra=RAW_ONLY_LOG_EXTRA,
                )
        if not state.get("ok"):
            await notify_source_observer([])

    async def collect_source(source: Any) -> bool:
        if collection_stop_requested():
            return True
        source_collection_started_at[source.source_id] = time.perf_counter()
        state = source_states[source.source_id]
        runtime_endpoints = runtime_endpoints_for_source(source)
        if (
            require_remote_source_route
            and source.source_type != "local_file"
            and not proxy_url
            and not runtime_endpoints
        ):
            if runtime_endpoint_map:
                mark_runtime_source_route_pending(source)
                return False
            blocked_error = (
                "blocked_no_route: remote source requires a configured egress proxy "
                "or verified Mihomo FINAL-EXIT"
            )
            for unit in list(source_units[source.source_id].values()):
                _parsed, should_stop = await ingest_source_unit(
                    source,
                    SourceUnitResult(
                        root_id=source.source_id,
                        root_type=source.source_type,
                        root_value=source.value,
                        origin=source.origin,
                        unit_id=str(unit.get("id") or "default"),
                        unit_value=str(unit.get("value") or source.value),
                        unit_family=str(unit.get("family") or "default"),
                        ok=False,
                        error=blocked_error,
                        egress_mode="blocked_no_route",
                        transport_started=False,
                    ),
                )
                if should_stop:
                    return True
            state["pending"] = False
            state["status"] = "failed"
            state["retryable"] = False
            state["error"] = blocked_error
            state["egress_mode"] = "blocked_no_route"
            return False
        if runtime_endpoints and source.source_type != "local_file":
            state["egress_mode"] = "runtime_proxy"
            route_dispatch_deferred = False
            try:
                should_stop, route_dispatch_deferred = (
                    await collect_source_via_runtime_endpoints(
                        source,
                        runtime_endpoints,
                    )
                )
                if should_stop:
                    return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state["error"] = (
                    "runtime source route race failed: "
                    f"{type(exc).__name__}"
                )
                state["retryable"] = True
                logger.warning(
                    "Tier3 runtime source route race failed for %s (%s)",
                    source.value,
                    type(exc).__name__,
                    extra=RAW_ONLY_LOG_EXTRA,
                )
            if route_dispatch_deferred:
                return False
            await finalize_source_state(source)
            return False
        primary_errors: list[str] = []
        primary_success = False
        try:
            async for unit in iter_source_units(
                source,
                active_proxy_url=proxy_url,
                egress_mode=primary_egress_mode,
                fallback_attempted=bool(
                    runtime_proxy_first and primary_egress_mode == "runtime_proxy"
                ),
                route_key=primary_egress_mode,
            ):
                if collection_stop_requested():
                    return True
                _parsed, should_stop = await ingest_source_unit(source, unit)
                primary_success = primary_success or bool(unit.ok and unit.content)
                if unit.error:
                    primary_errors.append(unit.error)
                if should_stop:
                    return True
        except Exception as exc:
            primary_errors.append(str(exc))
            logger.warning(
                "Collection raised for %s: %s",
                source.value,
                exc,
                extra=RAW_ONLY_LOG_EXTRA,
            )
        if (
            not primary_success
            and runtime_proxy_url
            and runtime_proxy_url != proxy_url
            and source_fetch_error_should_try_runtime_proxy("; ".join(primary_errors))
        ):
            try:
                async for unit in iter_source_units(
                    source,
                    active_proxy_url=runtime_proxy_url,
                    egress_mode="runtime_proxy",
                    fallback_attempted=True,
                    route_key="runtime_proxy_fallback",
                ):
                    if collection_stop_requested():
                        return True
                    if unit.error and primary_errors:
                        unit = replace(
                            unit,
                            error=(
                                unit.error
                                if unit.ok
                                else f"{'; '.join(primary_errors)}; runtime proxy fallback: {unit.error}"
                            ),
                            fallback_error=unit.error,
                        )
                    _parsed, should_stop = await ingest_source_unit(source, unit)
                    if should_stop:
                        return True
            except Exception as exc:
                state["fallback_attempted"] = True
                state["fallback_error"] = str(exc)
                state["error"] = f"{'; '.join(primary_errors)}; runtime proxy fallback: {exc}"
        await finalize_source_state(source)
        return False

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    # Paths/sources share a connection pool, never server-issued credentials.
    async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar()) as session:
        tasks: list[asyncio.Task[bool]] = []
        pending: set[asyncio.Task[bool]] = set()
        next_source_index = 0

        def schedule_source_roots() -> None:
            nonlocal next_source_index
            while (
                next_source_index < len(sources)
                and len(pending) < source_root_concurrency
                and not collection_stop_requested()
            ):
                task = asyncio.create_task(
                    collect_source(sources[next_source_index])
                )
                next_source_index += 1
                tasks.append(task)
                pending.add(task)

        schedule_source_roots()
        try:
            while pending and not collection_stop_requested():
                done, still_pending = await asyncio.wait(
                    pending,
                    timeout=0.025,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                pending = set(still_pending)
                for completed in done:
                    try:
                        should_stop = completed.result()
                    except asyncio.CancelledError:
                        should_stop = True
                    if should_stop:
                        observer_stop_requested.set()
                        break
                schedule_source_roots()
        finally:
            pending = {task for task in tasks if not task.done()}
            if pending:
                done, still_pending, cleanup_interrupted = await cancel_and_wait_tasks(
                    pending,
                    timeout=min(1.0, source_cancel_drain_seconds + 0.1),
                )
                cleanup_failure: BlockingWorkCancellationTimeout | None = None
                for task in done:
                    try:
                        task.result()
                    except BlockingWorkCancellationTimeout as exc:
                        cleanup_failure = cleanup_failure or exc
                    except BaseException:
                        pass
                for task in still_pending:
                    task.add_done_callback(
                        lambda late: None if late.cancelled() else late.exception()
                    )
                if still_pending and cleanup_failure is None:
                    cleanup_failure = BlockingWorkCancellationTimeout(
                        "Tier3 source wrappers did not stop within the cancellation budget"
                    )
                await release_payload_parsers()
                if cleanup_failure is not None:
                    raise cleanup_failure
                if cleanup_interrupted:
                    raise asyncio.CancelledError
            else:
                await release_payload_parsers()
    if source_phase_stop_requested() and not external_cancel_requested():
        # The startup floor is a successful supply stop, not a transport
        # failure. Resolve roots that were never scheduled (or were cancelled
        # mid-fetch) without making them eligible for an in-session retry loop.
        for source in sources:
            state = source_states[source.source_id]
            if state.get("ok"):
                continue
            state.update(
                {
                    "pending": False,
                    "status": "deferred",
                    "retryable": False,
                    "error": "deferred_startup_floor_reached",
                }
            )
            for unit in source_units[source.source_id].values():
                if unit.get("ok"):
                    continue
                unit.update(
                    {
                        "pending": False,
                        "status": "deferred",
                        "retryable": False,
                        "error": "deferred_startup_floor_reached",
                    }
                )
    if observer_queue is not None:
        observer_queue.put_nowait(None)
        if observer_dispatcher is not None:
            observer_dispatcher.join(timeout=source_cancel_slack_seconds)
    externally_cancelled = external_cancel_requested()
    if externally_cancelled:
        deduped = []
    else:
        try:
            deduped = await _run_blocking_cancellable(
                deduplicate_nodes,
                nodes,
                cancel_event=cancel_event,
                cooperative_cancel=True,
            )
        except asyncio.CancelledError:
            if not external_cancel_requested():
                raise
            externally_cancelled = True
            deduped = []
    report = build_report(node_count_override=len(deduped))
    report = replace(report, node_count=len(deduped))
    source_total = max(
        int(getattr(report, "configured_source_count", 0) or 0),
        int(report.source_count or 0),
    )
    failed_count = max(0, int(report.failed_sources or 0))
    if source_total > 0 and not externally_cancelled:
        _log_source_failure_summaries(logger, report)
        all_failed = failed_count >= source_total and not deduped
        params: dict[str, Any] = {
            "source_total": source_total,
            "failed_count": failed_count,
            "usable_count": len(deduped),
        }
        if all_failed:
            params["failure_kinds"] = _source_failure_kind_counts(report)
        log_event(
            logger,
            "ERROR" if all_failed else "INFO",
            "update.sources_failed" if all_failed else "update.sources_completed",
            "Node source refresh failed for all sources"
            if all_failed
            else (
                "Node source refresh completed: "
                f"sources={source_total} failed={failed_count} nodes={len(deduped)}"
            ),
            params=params,
        )
    return deduped, report


async def collect_from_sources_file(
    sources_path: str | Path,
    output_path: str | Path,
    logger: logging.Logger,
    settings: dict[str, Any] | None = None,
    tier: str = "tier3",
    cancel_event: threading.Event | None = None,
) -> PoolSnapshot:
    sources_config = load_yaml(sources_path)
    entries = source_entries_from_config(sources_config)
    nodes, report = await collect_nodes_from_sources_with_report(
        entries,
        tier=tier,
        logger=logger,
        settings=settings,
        cancel_event=cancel_event,
    )
    snapshot = PoolSnapshot(
        generated_at=utc_now_iso(),
        nodes=nodes,
        metadata={
            "source_count": report.source_count,
            "tier": tier,
            "collection": report.to_dict(),
            "cancelled": bool(cancel_event is not None and cancel_event.is_set()),
        },
    )
    save_snapshot(snapshot, output_path, cancel_event=cancel_event)
    return snapshot
