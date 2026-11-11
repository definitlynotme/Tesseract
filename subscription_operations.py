from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable, Iterator, Literal, TypeVar
from urllib.parse import urlsplit, urlunsplit


SubscriptionOperationActor = Literal["manual", "system"]
SubscriptionOperationKey = tuple[str, str]
SubscriptionOperationResult = TypeVar("SubscriptionOperationResult")


def canonical_subscription_url(url: str) -> str:
    raw_url = str(url or "").strip()
    if not raw_url:
        return ""
    try:
        parsed = urlsplit(raw_url)
        scheme = str(parsed.scheme or "").lower()
        hostname = parsed.hostname
        if not scheme or not hostname:
            return raw_url
        normalized_host = hostname.lower()
        if ":" in normalized_host and not normalized_host.startswith("["):
            normalized_host = f"[{normalized_host}]"
        port = parsed.port
        default_port = {
            "http": 80,
            "https": 443,
            "ws": 80,
            "wss": 443,
        }.get(scheme)
        port_suffix = "" if port is None or port == default_port else f":{port}"
        userinfo = ""
        if "@" in parsed.netloc:
            # Preserve credentials for source identity, but Lease repr hides the
            # canonical URL so secrets cannot leak through diagnostics.
            userinfo = f"{parsed.netloc.rsplit('@', 1)[0]}@"
        netloc = f"{userinfo}{normalized_host}{port_suffix}"
        return urlunsplit((scheme, netloc, parsed.path, parsed.query, ""))
    except (TypeError, ValueError):
        return raw_url


def subscription_operation_key(tier: str, url: str) -> SubscriptionOperationKey:
    tier_id = str(tier or "").strip().lower()
    source_url = canonical_subscription_url(url)
    if not tier_id:
        raise ValueError("subscription operation tier is required")
    if not source_url:
        raise ValueError("subscription operation URL is required")
    return tier_id, source_url


@dataclass(frozen=True, slots=True)
class SubscriptionOperationLease:
    operation_id: int
    actor: SubscriptionOperationActor
    tier: str
    url: str = field(repr=False)
    cancel_event: threading.Event

    @property
    def key(self) -> SubscriptionOperationKey:
        return self.tier, self.url


class SubscriptionOperationCoordinator:
    """Coordinate subscription work without letting manual probes delay system work.

    Manual work is read-only and cancellable. A system operation atomically claims
    every requested source, removes matching manual registrations, and signals their
    cancellation events after releasing the coordinator lock. It never waits for a
    manual worker to finish.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._next_operation_id = 0
        self._manual_operations: dict[
            SubscriptionOperationKey,
            dict[int, threading.Event],
        ] = {}
        self._system_operations: dict[int, SubscriptionOperationKey] = {}
        self._system_counts: dict[SubscriptionOperationKey, int] = {}

    def _next_id_locked(self) -> int:
        self._next_operation_id += 1
        return self._next_operation_id

    def try_begin_manual(
        self,
        tier: str,
        url: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> SubscriptionOperationLease | None:
        key = subscription_operation_key(tier, url)
        event = cancel_event or threading.Event()
        with self._lock:
            if (
                event.is_set()
                or self._system_counts.get(key, 0) > 0
                or bool(self._manual_operations.get(key))
            ):
                return None
            operation_id = self._next_id_locked()
            self._manual_operations.setdefault(key, {})[operation_id] = event
            return SubscriptionOperationLease(
                operation_id=operation_id,
                actor="manual",
                tier=key[0],
                url=key[1],
                cancel_event=event,
            )

    def begin_system(
        self,
        tier: str,
        url: str,
    ) -> SubscriptionOperationLease:
        leases = self.begin_system_batch(tier, [url])
        return leases[0]

    def begin_system_batch(
        self,
        tier: str,
        urls: list[str] | tuple[str, ...],
    ) -> tuple[SubscriptionOperationLease, ...]:
        keys = tuple(
            dict.fromkeys(
                subscription_operation_key(tier, url)
                for url in urls
            )
        )
        manual_cancel_events: list[threading.Event] = []
        leases: list[SubscriptionOperationLease] = []
        with self._lock:
            for key in keys:
                operation_id = self._next_id_locked()
                event = threading.Event()
                self._system_operations[operation_id] = key
                self._system_counts[key] = self._system_counts.get(key, 0) + 1
                manual_cancel_events.extend(
                    self._manual_operations.pop(key, {}).values()
                )
                leases.append(
                    SubscriptionOperationLease(
                        operation_id=operation_id,
                        actor="system",
                        tier=key[0],
                        url=key[1],
                        cancel_event=event,
                    )
                )
        for event in manual_cancel_events:
            event.set()
        return tuple(leases)

    def finish(self, lease: SubscriptionOperationLease) -> None:
        with self._lock:
            if lease.actor == "manual":
                operations = self._manual_operations.get(lease.key)
                if operations is None:
                    return
                operations.pop(lease.operation_id, None)
                if not operations:
                    self._manual_operations.pop(lease.key, None)
                return

            key = self._system_operations.pop(lease.operation_id, None)
            if key is None:
                return
            remaining = self._system_counts.get(key, 0) - 1
            if remaining > 0:
                self._system_counts[key] = remaining
            else:
                self._system_counts.pop(key, None)

    def is_system_active(self, tier: str, url: str) -> bool:
        key = subscription_operation_key(tier, url)
        with self._lock:
            return self._system_counts.get(key, 0) > 0

    def manual_active_count(self, tier: str, url: str) -> int:
        key = subscription_operation_key(tier, url)
        with self._lock:
            return len(self._manual_operations.get(key, {}))

    @contextmanager
    def system_scope(
        self,
        tier: str,
        urls: list[str] | tuple[str, ...],
    ) -> Iterator[tuple[SubscriptionOperationLease, ...]]:
        leases = self.begin_system_batch(tier, urls)
        try:
            yield leases
        finally:
            for lease in leases:
                self.finish(lease)


def system_subscription_operation(
    tier: str,
    urls_getter: str,
) -> Callable[
    [Callable[..., SubscriptionOperationResult]],
    Callable[..., SubscriptionOperationResult],
]:
    """Wrap a Manager system operation without changing its implementation body."""

    def decorate(
        operation: Callable[..., SubscriptionOperationResult],
    ) -> Callable[..., SubscriptionOperationResult]:
        @wraps(operation)
        def wrapped(manager: Any, *args: Any, **kwargs: Any) -> SubscriptionOperationResult:
            get_urls = getattr(manager, urls_getter)
            urls = list(get_urls())
            coordinator = manager.subscription_operation_coordinator
            with coordinator.system_scope(tier, urls):
                return operation(manager, *args, **kwargs)

        return wrapped

    return decorate
