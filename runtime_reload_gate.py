from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


class RuntimeReloadReason(str, Enum):
    """Closed set of reasons presented to :class:`RuntimeReloadGate`.

    Runtime health events deliberately have their own values instead of being
    folded into a generic ``runtime`` reason.  That makes an integration error
    fail closed: a failover callback cannot accidentally acquire permission to
    reload the complete Core graph.
    """

    USER_GRAPH_CONFIG = "user_graph_config"
    CAPTURE_LIFECYCLE = "capture_lifecycle"
    TUN_LIFECYCLE = "tun_lifecycle"
    DNS_LIFECYCLE = "dns_lifecycle"
    CORE_RECOVERY = "core_recovery"
    MEMBERSHIP_CHANGE = "membership_change"
    MEMBERSHIP_UNREPRESENTABLE = "membership_unrepresentable"

    RUNTIME_HEALTH = "runtime_health"
    RUNTIME_FAILOVER = "runtime_failover"
    RUNTIME_RECHECK = "runtime_recheck"
    RUNTIME_FAILBACK = "runtime_failback"
    RUNTIME_FACILITY = "runtime_facility"
    COLD_SAME_DIGEST = "cold_same_digest"


class RuntimeReloadDisposition(str, Enum):
    ALLOW = "allow"
    WAIT = "wait"
    REJECT = "reject"
    DEDUPLICATED = "deduplicated"


_HARD_REJECT_REASONS = frozenset(
    {
        RuntimeReloadReason.RUNTIME_HEALTH,
        RuntimeReloadReason.RUNTIME_FAILOVER,
        RuntimeReloadReason.RUNTIME_RECHECK,
        RuntimeReloadReason.RUNTIME_FAILBACK,
        RuntimeReloadReason.RUNTIME_FACILITY,
        RuntimeReloadReason.COLD_SAME_DIGEST,
    }
)

_UNCONDITIONAL_RELOAD_REASONS = frozenset(
    {
        RuntimeReloadReason.USER_GRAPH_CONFIG,
        RuntimeReloadReason.CAPTURE_LIFECYCLE,
        RuntimeReloadReason.TUN_LIFECYCLE,
        RuntimeReloadReason.DNS_LIFECYCLE,
        RuntimeReloadReason.CORE_RECOVERY,
        RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
    }
)

_REASON_ALIASES: dict[str, RuntimeReloadReason] = {
    "user": RuntimeReloadReason.USER_GRAPH_CONFIG,
    "user_config": RuntimeReloadReason.USER_GRAPH_CONFIG,
    "user_graph": RuntimeReloadReason.USER_GRAPH_CONFIG,
    "graph_config": RuntimeReloadReason.USER_GRAPH_CONFIG,
    "capture": RuntimeReloadReason.CAPTURE_LIFECYCLE,
    "tun": RuntimeReloadReason.TUN_LIFECYCLE,
    "dns": RuntimeReloadReason.DNS_LIFECYCLE,
    "core_restart": RuntimeReloadReason.CORE_RECOVERY,
    "restart": RuntimeReloadReason.CORE_RECOVERY,
    "membership": RuntimeReloadReason.MEMBERSHIP_CHANGE,
    "membership_unexpressible": RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
    "membership_not_dynamic": RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
    "health": RuntimeReloadReason.RUNTIME_HEALTH,
    "failover": RuntimeReloadReason.RUNTIME_FAILOVER,
    "recheck": RuntimeReloadReason.RUNTIME_RECHECK,
    "failback": RuntimeReloadReason.RUNTIME_FAILBACK,
    "facility": RuntimeReloadReason.RUNTIME_FACILITY,
    "cold": RuntimeReloadReason.COLD_SAME_DIGEST,
}


def _normalise_digest(value: Any) -> str:
    return str(value or "").strip()


def _normalise_reason(value: RuntimeReloadReason | str) -> RuntimeReloadReason | None:
    if isinstance(value, RuntimeReloadReason):
        return value
    normalised = str(value or "").strip().casefold().replace("-", "_")
    if not normalised:
        return None
    try:
        return RuntimeReloadReason(normalised)
    except ValueError:
        return _REASON_ALIASES.get(normalised)


@dataclass(frozen=True, slots=True)
class RuntimeReloadRequest:
    semantic_digest: str
    reason: RuntimeReloadReason | str
    request_id: str = ""
    user_commit_id: str = ""
    recovery_token: str = ""
    wait_if_busy: bool = True
    dynamic_update_supported: bool = True

    @property
    def normalised_digest(self) -> str:
        return _normalise_digest(self.semantic_digest)

    @property
    def normalised_reason(self) -> RuntimeReloadReason | None:
        return _normalise_reason(self.reason)

    @property
    def user_commit_key(self) -> str:
        if self.normalised_reason is not RuntimeReloadReason.USER_GRAPH_CONFIG:
            return ""
        explicit = str(self.user_commit_id or self.request_id or "").strip()
        return explicit or f"digest:{self.normalised_digest}"


@dataclass(frozen=True, slots=True)
class RuntimeRecoveryLease:
    token: str
    transaction_id: str
    revision: int


@dataclass(frozen=True, slots=True)
class RuntimeReloadLease:
    token: str
    request: RuntimeReloadRequest
    reason: RuntimeReloadReason
    revision: int


@dataclass(frozen=True, slots=True)
class RuntimeReloadDecision:
    disposition: RuntimeReloadDisposition
    code: str
    lease: RuntimeReloadLease | None = None
    deferred_token: str = ""

    @property
    def allowed(self) -> bool:
        return self.disposition is RuntimeReloadDisposition.ALLOW

    @property
    def should_wait(self) -> bool:
        return self.disposition is RuntimeReloadDisposition.WAIT

    @property
    def rejected(self) -> bool:
        return self.disposition is RuntimeReloadDisposition.REJECT

    @property
    def deduplicated(self) -> bool:
        return self.disposition is RuntimeReloadDisposition.DEDUPLICATED


@dataclass(frozen=True, slots=True)
class RuntimeReloadGateSnapshot:
    revision: int
    loaded_semantic_digest: str
    active_recovery_transaction_id: str
    active_reload_reason: str
    pending_user_commit_id: str
    completed_user_commit_count: int


@dataclass(frozen=True, slots=True)
class _DeferredUserCommit:
    token: str
    key: str
    request: RuntimeReloadRequest


class RuntimeReloadGate:
    """Serialize and classify Core reload authority.

    The gate performs no I/O and never sleeps.  A caller either receives a
    reload lease, a retryable ``WAIT`` decision, or a terminal rejection.
    Network probes and Core mutations therefore remain outside this lock.

    A new Core process should call :meth:`reset_for_new_core` (or clear the
    loaded digest) before asking for ``CORE_RECOVERY``.  This preserves the
    invariant that a loaded process is never reloaded with its current semantic
    digest, regardless of the stated reason.
    """

    def __init__(
        self,
        loaded_semantic_digest: str = "",
        *,
        completed_user_commit_limit: int = 64,
        token_factory: Callable[[str, int], str] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._loaded_semantic_digest = _normalise_digest(
            loaded_semantic_digest
        )
        self._revision = 0
        self._active_recovery: RuntimeRecoveryLease | None = None
        self._active_reload: RuntimeReloadLease | None = None
        self._pending_user_commit: _DeferredUserCommit | None = None
        self._completed_user_commits: OrderedDict[str, str] = OrderedDict()
        self._completed_user_commit_limit = max(
            1, int(completed_user_commit_limit)
        )
        self._token_factory = token_factory or (
            lambda kind, revision: f"{kind}-{revision}"
        )

    def _next_token_locked(self, kind: str) -> tuple[int, str]:
        self._revision += 1
        return self._revision, str(self._token_factory(kind, self._revision))

    @property
    def loaded_semantic_digest(self) -> str:
        with self._lock:
            return self._loaded_semantic_digest

    def snapshot(self) -> RuntimeReloadGateSnapshot:
        with self._lock:
            active_reload = self._active_reload
            pending = self._pending_user_commit
            return RuntimeReloadGateSnapshot(
                revision=self._revision,
                loaded_semantic_digest=self._loaded_semantic_digest,
                active_recovery_transaction_id=(
                    self._active_recovery.transaction_id
                    if self._active_recovery is not None
                    else ""
                ),
                active_reload_reason=(
                    active_reload.reason.value if active_reload else ""
                ),
                pending_user_commit_id=(pending.key if pending else ""),
                completed_user_commit_count=len(self._completed_user_commits),
            )

    def begin_recovery(
        self,
        transaction_id: str = "",
    ) -> RuntimeRecoveryLease | None:
        """Acquire the sole recovery transaction lease without blocking."""

        with self._lock:
            requested_id = str(transaction_id or "").strip()
            current = self._active_recovery
            if current is not None:
                if requested_id and current.transaction_id == requested_id:
                    return current
                return None
            if self._active_reload is not None:
                return None
            revision, token = self._next_token_locked("recovery")
            lease = RuntimeRecoveryLease(
                token=token,
                transaction_id=requested_id or token,
                revision=revision,
            )
            self._active_recovery = lease
            return lease

    # Explicit alias for integrations that prefer non-throwing try semantics.
    try_begin_recovery = begin_recovery

    def end_recovery(self, lease: RuntimeRecoveryLease | str) -> bool:
        with self._lock:
            expected = lease.token if isinstance(lease, RuntimeRecoveryLease) else str(lease)
            current = self._active_recovery
            if current is None or current.token != expected:
                return False
            if (
                self._active_reload is not None
                and self._active_reload.request.recovery_token == current.token
            ):
                # The owner must settle its reload before publishing recovery
                # completion; otherwise another writer could enter mid-apply.
                return False
            self._active_recovery = None
            self._revision += 1
            return True

    def end_recovery_with_loaded_semantic_digest(
        self,
        lease: RuntimeRecoveryLease | str,
        semantic_digest: str,
    ) -> bool:
        """Atomically publish a dynamic Core mutation and release its owner."""

        result_digest = _normalise_digest(semantic_digest)
        if not result_digest:
            raise ValueError("a committed recovery mutation requires a semantic digest")
        with self._lock:
            token = lease.token if isinstance(lease, RuntimeRecoveryLease) else str(lease)
            current = self._active_recovery
            if (
                current is None
                or current.token != token
                or self._active_reload is not None
            ):
                return False
            self._loaded_semantic_digest = result_digest
            self._active_recovery = None
            self._revision += 1
            return True

    @staticmethod
    def _decision(
        disposition: RuntimeReloadDisposition,
        code: str,
        *,
        lease: RuntimeReloadLease | None = None,
        deferred_token: str = "",
    ) -> RuntimeReloadDecision:
        return RuntimeReloadDecision(
            disposition=disposition,
            code=code,
            lease=lease,
            deferred_token=deferred_token,
        )

    def _classify_locked(
        self,
        request: RuntimeReloadRequest,
    ) -> tuple[RuntimeReloadReason | None, RuntimeReloadDecision | None]:
        digest = request.normalised_digest
        reason = request.normalised_reason
        if not digest:
            return reason, self._decision(
                RuntimeReloadDisposition.REJECT,
                "missing_semantic_digest",
            )
        if digest == self._loaded_semantic_digest:
            return reason, self._decision(
                RuntimeReloadDisposition.REJECT,
                "same_semantic_digest",
            )
        if reason is None:
            return None, self._decision(
                RuntimeReloadDisposition.REJECT,
                "unknown_reload_reason",
            )
        if reason in _HARD_REJECT_REASONS:
            return reason, self._decision(
                RuntimeReloadDisposition.REJECT,
                "runtime_hot_path_reload_forbidden",
            )
        if reason is RuntimeReloadReason.MEMBERSHIP_CHANGE:
            if request.dynamic_update_supported:
                return reason, self._decision(
                    RuntimeReloadDisposition.REJECT,
                    "membership_is_dynamically_expressible",
                )
        elif reason not in _UNCONDITIONAL_RELOAD_REASONS:
            return reason, self._decision(
                RuntimeReloadDisposition.REJECT,
                "reload_reason_not_allowed",
            )
        return reason, None

    def _issue_reload_locked(
        self,
        request: RuntimeReloadRequest,
        reason: RuntimeReloadReason,
    ) -> RuntimeReloadDecision:
        revision, token = self._next_token_locked("reload")
        lease = RuntimeReloadLease(
            token=token,
            request=request,
            reason=reason,
            revision=revision,
        )
        self._active_reload = lease
        return self._decision(
            RuntimeReloadDisposition.ALLOW,
            "reload_lease_acquired",
            lease=lease,
        )

    def _duplicate_user_commit_locked(
        self,
        request: RuntimeReloadRequest,
    ) -> RuntimeReloadDecision | None:
        key = request.user_commit_key
        if not key:
            return None
        if key in self._completed_user_commits:
            return self._decision(
                RuntimeReloadDisposition.DEDUPLICATED,
                "user_commit_already_completed",
            )
        active = self._active_reload
        if active is not None and active.request.user_commit_key == key:
            return self._decision(
                RuntimeReloadDisposition.DEDUPLICATED,
                "user_commit_already_active",
                lease=active,
            )
        pending = self._pending_user_commit
        if pending is not None and pending.key == key:
            return self._decision(
                RuntimeReloadDisposition.DEDUPLICATED,
                "user_commit_already_waiting",
                deferred_token=pending.token,
            )
        return None

    def _defer_user_commit_locked(
        self,
        request: RuntimeReloadRequest,
    ) -> RuntimeReloadDecision:
        duplicate = self._duplicate_user_commit_locked(request)
        if duplicate is not None:
            return duplicate
        if self._pending_user_commit is not None:
            return self._decision(
                RuntimeReloadDisposition.REJECT,
                "another_user_commit_is_already_waiting",
                deferred_token=self._pending_user_commit.token,
            )
        revision, token = self._next_token_locked("deferred-user")
        self._pending_user_commit = _DeferredUserCommit(
            token=token,
            key=request.user_commit_key,
            request=request,
        )
        return self._decision(
            RuntimeReloadDisposition.WAIT,
            "user_commit_waiting_for_recovery",
            deferred_token=token,
        )

    def request_reload(
        self,
        request: RuntimeReloadRequest | None = None,
        **request_kwargs: Any,
    ) -> RuntimeReloadDecision:
        """Request exclusive permission for one semantic Core reload.

        ``request_kwargs`` is a convenience integration surface; it is used
        only when ``request`` is omitted.
        """

        if request is None:
            request = RuntimeReloadRequest(**request_kwargs)
        elif request_kwargs:
            raise TypeError("request_kwargs cannot be combined with request")
        if not isinstance(request, RuntimeReloadRequest):
            raise TypeError("request must be a RuntimeReloadRequest")

        with self._lock:
            reason, rejection = self._classify_locked(request)
            if rejection is not None:
                return rejection
            assert reason is not None

            duplicate = self._duplicate_user_commit_locked(request)
            if duplicate is not None:
                return duplicate

            recovery = self._active_recovery
            supplied_recovery_token = str(request.recovery_token or "").strip()
            if supplied_recovery_token and (
                recovery is None
                or supplied_recovery_token != recovery.token
            ):
                # An old recovery callback must not become an ordinary reload
                # merely because its owning transaction has already ended.
                return self._decision(
                    RuntimeReloadDisposition.REJECT,
                    "stale_recovery_token",
                )
            owns_recovery = bool(
                recovery is not None
                and supplied_recovery_token
                and supplied_recovery_token == recovery.token
            )
            if recovery is not None and not owns_recovery:
                if not request.wait_if_busy:
                    return self._decision(
                        RuntimeReloadDisposition.REJECT,
                        "recovery_transaction_active",
                    )
                if reason is RuntimeReloadReason.USER_GRAPH_CONFIG:
                    return self._defer_user_commit_locked(request)
                return self._decision(
                    RuntimeReloadDisposition.WAIT,
                    "recovery_transaction_active",
                )

            if self._active_reload is not None:
                if not request.wait_if_busy:
                    return self._decision(
                        RuntimeReloadDisposition.REJECT,
                        "reload_transaction_active",
                    )
                if reason is RuntimeReloadReason.USER_GRAPH_CONFIG:
                    return self._defer_user_commit_locked(request)
                return self._decision(
                    RuntimeReloadDisposition.WAIT,
                    "reload_transaction_active",
                )

            return self._issue_reload_locked(request, reason)

    # Short aliases keep call sites readable without weakening the API.
    request = request_reload
    evaluate = request_reload

    def claim_deferred_user_commit(
        self,
        deferred_token: str,
    ) -> RuntimeReloadDecision:
        """Turn the single deferred user commit into the active reload lease."""

        with self._lock:
            pending = self._pending_user_commit
            if pending is None or pending.token != str(deferred_token or ""):
                return self._decision(
                    RuntimeReloadDisposition.REJECT,
                    "deferred_user_commit_not_found",
                )
            if self._active_recovery is not None or self._active_reload is not None:
                return self._decision(
                    RuntimeReloadDisposition.WAIT,
                    "reload_gate_still_busy",
                    deferred_token=pending.token,
                )
            reason, rejection = self._classify_locked(pending.request)
            if rejection is not None:
                self._pending_user_commit = None
                return rejection
            assert reason is RuntimeReloadReason.USER_GRAPH_CONFIG
            self._pending_user_commit = None
            return self._issue_reload_locked(pending.request, reason)

    def cancel_deferred_user_commit(self, deferred_token: str) -> bool:
        with self._lock:
            pending = self._pending_user_commit
            if pending is None or pending.token != str(deferred_token or ""):
                return False
            self._pending_user_commit = None
            self._revision += 1
            return True

    def complete_reload(
        self,
        lease: RuntimeReloadLease | str,
        *,
        applied: bool,
        loaded_semantic_digest: str | None = None,
    ) -> bool:
        """Settle a reload lease and publish its digest only after success."""

        with self._lock:
            token = lease.token if isinstance(lease, RuntimeReloadLease) else str(lease)
            current = self._active_reload
            if current is None or current.token != token:
                return False
            if applied:
                result_digest = _normalise_digest(
                    loaded_semantic_digest
                    if loaded_semantic_digest is not None
                    else current.request.semantic_digest
                )
                if not result_digest:
                    raise ValueError("a successful reload requires a semantic digest")
                if result_digest != current.request.normalised_digest:
                    raise ValueError(
                        "loaded semantic digest does not match the reload lease"
                    )
                self._loaded_semantic_digest = result_digest
                key = current.request.user_commit_key
                if key:
                    self._completed_user_commits[key] = result_digest
                    self._completed_user_commits.move_to_end(key)
                    while (
                        len(self._completed_user_commits)
                        > self._completed_user_commit_limit
                    ):
                        self._completed_user_commits.popitem(last=False)
            self._active_reload = None
            self._revision += 1
            return True

    finish_reload = complete_reload

    def cancel_reload(self, lease: RuntimeReloadLease | str) -> bool:
        return self.complete_reload(lease, applied=False)

    def set_loaded_semantic_digest(self, semantic_digest: str) -> None:
        """Synchronize a known loaded digest while no transaction is active."""

        with self._lock:
            if self._active_reload is not None or self._active_recovery is not None:
                raise RuntimeError("cannot replace loaded digest while gate is busy")
            self._loaded_semantic_digest = _normalise_digest(semantic_digest)
            self._revision += 1

    def set_loaded_semantic_digest_if_idle(self, semantic_digest: str) -> bool:
        """Synchronize an externally loaded digest without racing gate owners.

        A successful Core apply publishes its exact runtime contract before the
        caller settles the matching reload lease.  In that narrow interval the
        active lease is the sole authority for committing the gate digest, so a
        secondary publication must be a no-op instead of turning an already
        applied reload into an exception.  Startup/external Core observations
        still synchronize normally when the gate is idle.
        """

        with self._lock:
            if self._active_reload is not None or self._active_recovery is not None:
                return False
            self._loaded_semantic_digest = _normalise_digest(semantic_digest)
            self._revision += 1
            return True

    def reset_for_new_core(self, loaded_semantic_digest: str = "") -> None:
        """Invalidate process-scoped leases when Core generation changes."""

        with self._lock:
            self._loaded_semantic_digest = _normalise_digest(
                loaded_semantic_digest
            )
            self._active_recovery = None
            self._active_reload = None
            self._pending_user_commit = None
            self._revision += 1


__all__ = [
    "RuntimeRecoveryLease",
    "RuntimeReloadDecision",
    "RuntimeReloadDisposition",
    "RuntimeReloadGate",
    "RuntimeReloadGateSnapshot",
    "RuntimeReloadLease",
    "RuntimeReloadReason",
    "RuntimeReloadRequest",
]
