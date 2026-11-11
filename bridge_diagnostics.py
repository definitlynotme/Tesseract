"""Node diagnostics: ``nodes.diagnose`` and ``nodes.cancelDiagnose``.

T-P2-17 (ASK-02 §2.1; PROTOCOL §10 command rows, §15 diagnose semantics,
§16 Diagnostic).  One ``DiagnosticsOwner`` per sidecar holds the request
owner of the single node diagnostic the runtime can run at a time.

* ``nodes.diagnose{node_id, probe_mode, request_id}`` runs in the normal
  domain, single-flight: a second request while one is in flight (including
  a cancelled one whose temporary Core is still being cleaned up) is refused
  with ``conflict`` / ``diagnostic_in_progress``.  ``request_id`` is the
  resource owner: it is the runtime request id passed to
  ``DesktopRuntime.diagnose_node`` and the key of every cancellation.
* The public ``Node.id`` is resolved to the runtime's internal diagnostic
  identity with the P2-04 / P2-14 ID rule (``bridge_node_commands``).  When
  the resolved target is (or shares its identity with) an automatic-pool
  node, the command is refused with ``not_allowed`` /
  ``tier3_auto_pool_self_check``; no manager private method is called and no
  new diagnostic semantics are added (ASK-02 "自动池诊断必须单独纠正").
* The job keeps ``status_applied``, the failure reason and ``cleanup_pending``.
  ``done`` only for a successful probe with nothing left to clean
  (``reason_code=null``, ``cleanup_pending=false``, §15/§16).
* Cancellation is ``diagnostic``: the real ``cancel_node_diagnostic`` is
  called (retried until it lands or the runtime call returns, because the
  runtime registers the request only after resolving its inventory) and the
  job ends ``cancelled`` only after the runtime call returned, i.e. after the
  diagnostic service ran its resource cleanup.
* ``nodes.cancelDiagnose{request_id}`` (control domain, ``res``) requests the
  same cancellation and waits a bounded time for that cleanup.  It answers
  ``{requested, cleanup_pending}``: "requested" is never "cleaned".
* A diagnosis does not need a running service: with the service stopped the
  runtime diagnoses from its persisted inventory with a temporary Core
  (existing semantics); this module never checks the lifecycle.
* Owner revoke cancels that owner's diagnostic through the same real
  cancellation; its late result never becomes the job result (the job ends
  ``cancelled``) and its latency is not kept.  The revoked owner is
  remembered: a diagnose of that owner that was still queued when the owner
  was revoked ends ``cancelled`` at its first gate, before resolution, and
  never starts the runtime (the executor's revoke does not end queued
  diagnostic jobs).
* A ``cancel{op}`` that lands after the job became running but before the
  handler registered its flight is remembered by op and honoured at that
  registration gate (nothing has started, so "cleaned" is true).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import logging
import re
import threading
import time
from typing import Any, Callable, Mapping, Protocol

from bridge_jobs import CancellationReport, JobFailure
from bridge_vocab import error_message_key
from tier3_origin import is_tier3_auto_pool_node


COMMAND = "nodes.diagnose"
CANCEL_COMMAND = "nodes.cancelDiagnose"
PROBE_MODES = frozenset({"direct", "chain"})
AUTO_POOL_REASON = "tier3_auto_pool_self_check"

# Bounded wait of nodes.cancelDiagnose on the single control worker.  The
# runtime cancel terminates the temporary Core synchronously; the remaining
# wait is the diagnose call unwinding (service close uses 3 s, node_diagnostic.py).
CANCEL_WAIT_SECONDS = 2.0
# Retry spacing while the runtime has not registered the request yet.
CANCEL_RETRY_SECONDS = 0.05
RECORD_LIMIT = 256          # last Diagnostic per node (nodes.detail)
TOMBSTONE_LIMIT = 64        # finished request ids (cancelDiagnose answers)
JOB_MEMORY_LIMIT = 64       # recent flights by op / early cancels by op
REVOKED_OWNER_LIMIT = 1024  # revoked wire owners remembered for queued jobs

_LOG = logging.getLogger(__name__)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_NODE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# reason_code -> nodes.diagnose failure code (closed set: common codes plus
# not_allowed / not_running / timeout).  Every key owns ``errors.diagnose.<reason>``.
DIAGNOSE_REASON_CODES = {
    # Bridge-side refusals.
    AUTO_POOL_REASON: "not_allowed",
    "diagnostic_in_progress": "conflict",
    "request_id_reused": "conflict",
    "node_not_found": "conflict",
    "node_index_unavailable": "runtime_unavailable",
    "chain_requires_gateway": "validation",
    "diagnostic_unavailable": "runtime_unavailable",
    # Runtime reasons (node_diagnostic.EphemeralNodeDiagnosticService /
    # DesktopRuntime.diagnose_node).
    "timeout": "timeout",
    "probe_failed": "timeout",
    "startup_failed": "runtime_unavailable",
    "setup_failed": "runtime_unavailable",
    "component_missing": "runtime_unavailable",
    "cleanup_failed": "runtime_unavailable",
    "invalid_request": "validation",
    "unsupported_protocol": "validation",
    "upstream_missing": "conflict",
    "node_changed": "conflict",
    "service_closed": "not_running",
    # A cancellation this module did not request (runtime close / cancel_all).
    "cancelled": "not_running",
}
DEFAULT_FAILURE_CODE = "runtime_unavailable"


def _token(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if _TOKEN.fullmatch(text) else None


def diagnose_failure_code(reason: Any) -> str:
    return DIAGNOSE_REASON_CODES.get(_token(reason) or "", DEFAULT_FAILURE_CODE)


def diagnose_message_key(reason: Any) -> str:
    token = _token(reason)
    return error_message_key(token if token in DIAGNOSE_REASON_CODES else "unavailable",
                             domain="diagnose")


def diagnose_error(reason: Any) -> dict[str, Any]:
    """Error DTO (PROTOCOL §7) for a failed diagnose job, for executor adoption."""
    token = _token(reason) or "diagnostic_unavailable"
    return {"code": diagnose_failure_code(token), "message_key": diagnose_message_key(token),
            "params": {}, "reason_code": token}


def diagnose_result(request_id: str, *, status_applied: bool, reason_code: str | None,
                    cleanup_pending: bool) -> dict[str, Any]:
    """The closed ``nodes.diagnose`` result object."""
    return {"request_id": request_id, "status_applied": bool(status_applied),
            "reason_code": reason_code, "cleanup_pending": bool(cleanup_pending)}


def diagnostic_dto(request_id: str, mode: str, state: str, *, latency_ms: float | None = None,
                   reason_code: str | None = None, status_applied: bool = False,
                   cleanup_pending: bool = False) -> dict[str, Any]:
    """PROTOCOL §16 ``Diagnostic`` (``nodes.detail.last_diagnostic``)."""
    return {"request_id": request_id, "mode": mode, "state": state, "latency_ms": latency_ms,
            "reason_code": reason_code, "status_applied": bool(status_applied),
            "cleanup_pending": bool(cleanup_pending)}


def node_pool_kind(tier: str, extra: Mapping[str, Any] | None) -> str:
    """P2-04 rule, exactly as ``DesktopRuntime.bridge_node_records`` projects
    ``pool_kind``: Tier3 nodes not of tier3a origin are the automatic pool."""
    extra = extra if isinstance(extra, Mapping) else {}
    origin = extra.get("tier3_origin") or extra.get("source_origin")
    return "automatic" if tier == "tier3" and origin != "tier3a" else "user"


def is_automatic_pool(tier: str, node: Any) -> bool:
    """Refusal predicate: the authoritative ``tier3_origin.is_tier3_auto_pool_node``
    (what the manager itself uses) OR the public projection's ``pool_kind``
    (keyed by the runtime tier, origin unstripped).  The union is never
    looser than either rule, so a node the page shows as automatic, or the
    manager treats as automatic, is always refused."""
    return bool(is_tier3_auto_pool_node(node)
                or node_pool_kind(str(tier), getattr(node, "extra", None)) == "automatic")


@dataclass(frozen=True)
class Outcome:
    state: str                    # ok | failed | cancelled
    reason: str | None
    latency_ms: float | None
    status_applied: bool
    cleanup_pending: bool


def runtime_outcome(raw: Any) -> Outcome:
    """Project one ``diagnose_node`` result; never invent success or cleanup."""
    if not isinstance(raw, Mapping):
        return Outcome("failed", "diagnostic_unavailable", None, False, False)
    state = str(raw.get("state") or "").strip().lower()
    reason = _token(raw.get("reason")) or ("ok" if state == "success" else "diagnostic_unavailable")
    applied = raw.get("status_applied") is True
    cleanup_pending = reason == "cleanup_failed"
    if state == "success":
        latency = raw.get("latency_ms")
        if isinstance(latency, bool) or not isinstance(latency, (int, float)) or latency < 0:
            latency = None
        return Outcome("ok", None, latency, applied, False)
    if state == "cancelled":
        return Outcome("cancelled", "cancelled", None, applied, cleanup_pending)
    return Outcome("failed", reason, None, applied, cleanup_pending)


# --------------------------------------------------------------------------
# Runtime port
# --------------------------------------------------------------------------

class TargetUnavailable(RuntimeError):
    """The node index cannot be read now (stale owner or missing getter)."""


@dataclass(frozen=True)
class NodeTarget:
    node_id: str
    node_identity: str        # node_diagnostic.diagnostic_node_identity
    tier: str                 # runtime tier id: tier1 | tier2 | tier3 | exit
    auto_pool: bool           # target or a node sharing its identity is automatic
    gateway: bool             # exit tier: the only one with a chain probe (Qt)


class DiagnosticRuntimePort(Protocol):
    """Runtime surface used by the two commands; every call may block."""

    def resolve_node(self, node_id: str) -> NodeTarget | None:
        """Public Node.id -> internal target; None = no such node in this
        generation; raises ``TargetUnavailable`` when it cannot be known."""

    def diagnose_node(self, node_identity: str, *, probe_mode: str,
                      request_id: str) -> Mapping[str, Any]:
        """``DesktopRuntime.diagnose_node`` (public; temporary Core)."""

    def cancel_node_diagnostic(self, request_id: str) -> bool:
        """``DesktopRuntime.cancel_node_diagnostic``: True when the runtime's
        active request matched and its cleanup ran."""


class RuntimeDiagnosticPort:
    """Adapter over DesktopRuntime public methods and attributes only.

    ``resolve_node`` prefers the runtime getter ``bridge_node_diagnostic_target``
    (see the report).  Until it exists it reads the loaded nodes the way the
    accepted P2-14 adapter does (``runtime.manager.lock`` / ``runtime_nodes``,
    tuples only, generation re-checked) and applies the P2-04 ID and pool
    rules; with the service stopped the persisted inventory has no public
    reader, so resolution reports ``TargetUnavailable``.
    """

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def _generation(self) -> int:
        return int(self.runtime.lifecycle_status()["intent"]["runtime_generation"])

    def resolve_node(self, node_id: str) -> NodeTarget | None:
        getter = getattr(type(self.runtime), "bridge_node_diagnostic_target", None)
        if callable(getter):
            value = getter(self.runtime, node_id)
            if value is None:
                return None
            if not isinstance(value, Mapping) or value.get("stale"):
                raise TargetUnavailable("stale node index")
            tier = str(value.get("tier") or "")
            return NodeTarget(node_id, str(value.get("node_identity") or ""), tier,
                              bool(value.get("auto_pool")), tier == "exit")
        manager = self.runtime.manager
        if manager is None:
            raise TargetUnavailable("stopped inventory has no public reader")
        from bridge_node_commands import identity_item_id, node_identity
        from node_diagnostic import diagnostic_node_identity
        generation = self._generation()
        with manager.lock:
            if getattr(manager, "runtime_generation", generation) != generation:
                raise TargetUnavailable("manager generation changed")
            rows = [(str(tier), node_identity(node), diagnostic_node_identity(node),
                     is_automatic_pool(str(tier), node))
                    for tier, nodes in manager.runtime_nodes.items() for node in nodes]
        if self._generation() != generation or self.runtime.manager is not manager:
            raise TargetUnavailable("runtime generation changed")
        for tier, identity, diagnostic, _auto in rows:
            if identity_item_id(generation, tier, identity) == node_id:
                auto = any(automatic for _t, _i, other, automatic in rows
                           if other == diagnostic)
                return NodeTarget(node_id, diagnostic, tier, auto, tier == "exit")
        return None

    def diagnose_node(self, node_identity: str, *, probe_mode: str,
                      request_id: str) -> Mapping[str, Any]:
        return self.runtime.diagnose_node(node_identity, probe_mode=probe_mode,
                                          request_id=request_id)

    def cancel_node_diagnostic(self, request_id: str) -> bool:
        return bool(self.runtime.cancel_node_diagnostic(request_id))


def executor_owner_resolver(get_executor: Callable[[], Any]) -> Callable[[Any], str | None]:
    """JobContext carries no owner: the current executor's public
    ``owner_of(op)`` (T-P2-31a); executor internals are never read."""

    def resolve(context: Any) -> str | None:
        public = getattr(get_executor(), "owner_of", None)
        return public(getattr(context, "op", None)) if callable(public) else None
    return resolve


# --------------------------------------------------------------------------
# Owner
# --------------------------------------------------------------------------

class _Flight:
    def __init__(self, request_id: str, node_id: str, mode: str, owner: str | None,
                 context: Any) -> None:
        self.request_id = request_id
        self.node_id = node_id
        self.mode = mode
        self.owner = owner
        self.context = context
        self.state = "resolving"          # resolving | running | finished
        self.cancel_requested = False
        self.revoked = False
        self.call_done = threading.Event()
        self.raw: Any = None
        self.canceller = False
        self.tombstoned = False
        # Shared cleanup-wait deadline of every nodes.cancelDiagnose on this
        # flight: only the first one waits, later ones answer the facts.
        self.cancel_deadline: float | None = None


def _context_key(context: Any) -> Any:
    op = getattr(context, "op", None)
    return op if op is not None else id(context)


def _event_set(context: Any, name: str) -> bool:
    event = getattr(context, name, None)
    return bool(event is not None and event.is_set())


@dataclass(frozen=True)
class _Tombstone:
    owner: str | None
    cleanup_pending: bool


class DiagnosticsOwner:
    """Single owner of the node diagnostic request (normal domain)."""

    def __init__(self, port: DiagnosticRuntimePort, *,
                 owner_of: Callable[[Any], str | None] | None = None,
                 cancel_wait_seconds: float = CANCEL_WAIT_SECONDS,
                 cancel_retry_seconds: float = CANCEL_RETRY_SECONDS,
                 thread_factory: Callable[..., Any] = threading.Thread,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.port = port
        self._owner_of = owner_of or (lambda _context: None)
        self.cancel_wait_seconds = float(cancel_wait_seconds)
        self.cancel_retry_seconds = float(cancel_retry_seconds)
        self._thread_factory = thread_factory
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._flight: _Flight | None = None
        self._records: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._tombstones: OrderedDict[str, _Tombstone] = OrderedDict()
        # Wire owners revoked so far (M1: a queued diagnose of a revoked owner
        # must never start); recent flights by op; early cancels by op (S2).
        self._revoked_owners: OrderedDict[str, None] = OrderedDict()
        self._flights_by_op: OrderedDict[Any, _Flight] = OrderedDict()
        self._early_cancels: OrderedDict[Any, None] = OrderedDict()

    # -- read side -------------------------------------------------------------

    def last_diagnostic(self, node_id: str) -> dict[str, Any] | None:
        """``nodes.detail.last_diagnostic`` for a node of this generation."""
        with self._lock:
            record = self._records.get(node_id)
            return dict(record) if record is not None else None

    def active_request(self) -> str | None:
        with self._lock:
            return self._flight.request_id if self._flight is not None else None

    def _record_locked(self, node_id: str, dto: dict[str, Any]) -> None:
        self._records[node_id] = dto
        self._records.move_to_end(node_id)
        while len(self._records) > RECORD_LIMIT:
            self._records.popitem(last=False)

    def _tombstone_locked(self, flight: _Flight, cleanup_pending: bool) -> None:
        self._tombstones[flight.request_id] = _Tombstone(flight.owner, cleanup_pending)
        self._tombstones.move_to_end(flight.request_id)
        while len(self._tombstones) > TOMBSTONE_LIMIT:
            self._tombstones.popitem(last=False)
        flight.tombstoned = True

    @staticmethod
    def _bounded_put(table: OrderedDict, key: Any, value: Any, limit: int) -> None:
        table[key] = value
        table.move_to_end(key)
        while len(table) > limit:
            table.popitem(last=False)

    # -- nodes.diagnose ----------------------------------------------------------

    def diagnose(self, args: Mapping[str, Any], context: Any) -> dict[str, Any]:
        """``nodes.diagnose`` job handler (normal domain, cancellation=diagnostic)."""
        request_id = args.get("request_id") if isinstance(args, Mapping) else None
        node_id = args.get("node_id") if isinstance(args, Mapping) else None
        mode = args.get("probe_mode") if isinstance(args, Mapping) else None
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id) \
                or not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id) \
                or mode not in PROBE_MODES:
            raise JobFailure("validation")
        owner = self._owner_of(context)
        flight = _Flight(request_id, node_id, mode, owner, context)
        key = _context_key(context)
        with self._lock:
            # Registration gate (M1 / S2), atomic with the revoke hook and with
            # cancel_job: an owner revoked while this job was still queued, or
            # a cancel{op} that landed before this point, ends the job here,
            # before resolution; nothing was started, nothing needs cleanup.
            revoked = (owner is not None and owner in self._revoked_owners) \
                or _event_set(context, "owner_revoked")
            early_cancel = key in self._early_cancels
            self._early_cancels.pop(key, None)
            if revoked or early_cancel:
                flight.state = "finished"
                flight.cancel_requested = True
                flight.revoked = revoked
                self._bounded_put(self._flights_by_op, key, flight, JOB_MEMORY_LIMIT)
                active = self._flight is not None and self._flight.request_id == request_id
                if not active and request_id not in self._tombstones:
                    self._tombstone_locked(flight, False)
            else:
                if self._flight is not None:
                    self._refuse(request_id, "diagnostic_in_progress")
                if request_id in self._tombstones:
                    self._refuse(request_id, "request_id_reused")
                self._flight = flight
                self._bounded_put(self._flights_by_op, key, flight, JOB_MEMORY_LIMIT)
        if revoked or early_cancel:
            event = getattr(context, "cancelled", None)
            if event is not None:
                event.set()
            return diagnose_result(request_id, status_applied=False, reason_code="cancelled",
                                   cleanup_pending=False)
        cleanup_pending = False
        try:
            outcome = self._run(flight)
            cleanup_pending = outcome.cleanup_pending
            return self._finish(flight, outcome)
        finally:
            with self._lock:
                flight.state = "finished"
                if self._flight is flight:
                    self._flight = None
                if not flight.tombstoned:
                    self._tombstone_locked(flight, cleanup_pending)

    @staticmethod
    def _refuse(request_id: str, reason: str, *, cleanup_pending: bool = False) -> None:
        raise JobFailure(diagnose_failure_code(reason),
                         partial_result=diagnose_result(request_id, status_applied=False,
                                                        reason_code=reason,
                                                        cleanup_pending=cleanup_pending),
                         error=diagnose_error(reason))

    def _run(self, flight: _Flight) -> Outcome:
        try:
            target = self.port.resolve_node(flight.node_id)
        except TargetUnavailable:
            target = "unavailable"
        except Exception as exc:
            _LOG.warning("node diagnostic target lookup raised %s (request %s)",
                         type(exc).__name__, flight.request_id)
            target = "unavailable"
        with self._lock:
            # MUST-A (round 2): a cancel{op} or owner revoke that landed while
            # the target was being resolved wins over any refusal below, so no
            # late failure (with its partial_result) reaches the job.
            if flight.cancel_requested or flight.revoked:
                return Outcome("cancelled", "cancelled", None, False, False)
        if target == "unavailable":
            self._refuse(flight.request_id, "node_index_unavailable")
        if target is None or not target.node_identity:
            self._refuse(flight.request_id, "node_not_found")
        if target.auto_pool:
            # ASK-02: refuse automatic-pool targets after resolution; never
            # call a manager private method.
            self._refuse(flight.request_id, AUTO_POOL_REASON)
        if flight.mode == "chain" and not target.gateway:
            # Qt (desktop_app.py:21509) offers the chain probe only on Gateway rows.
            self._refuse(flight.request_id, "chain_requires_gateway")
        with self._lock:
            if flight.cancel_requested or flight.revoked:
                # Cancelled before the runtime started anything: no resource.
                return Outcome("cancelled", "cancelled", None, False, False)
            flight.state = "running"
            self._record_locked(flight.node_id, diagnostic_dto(flight.request_id, flight.mode,
                                                               "running"))
        raw = None
        try:
            raw = self.port.diagnose_node(target.node_identity, probe_mode=flight.mode,
                                          request_id=flight.request_id)
        except Exception as exc:
            _LOG.warning("node diagnostic raised %s (request %s)", type(exc).__name__,
                         flight.request_id)
        finally:
            flight.raw = raw
            flight.call_done.set()
        return runtime_outcome(raw)

    def _finish(self, flight: _Flight, outcome: Outcome) -> dict[str, Any]:
        with self._lock:
            # Decided under the lock: a cancel that lands after this point
            # finds the flight finished and reports requested=false.
            flight.state = "finished"
            cancelled = flight.cancel_requested or flight.revoked
            # S3: the tombstone is written in the same critical section that
            # marks the flight finished, so a cancelDiagnose that sees
            # ``finished`` always finds the true cleanup_pending.
            self._tombstone_locked(flight, outcome.cleanup_pending)
            # Only a diagnostic the runtime actually ran leaves a record; a
            # request cancelled before it started has nothing to report.
            if flight.call_done.is_set():
                self._record_locked(flight.node_id, diagnostic_dto(
                    flight.request_id, flight.mode,
                    "cancelled" if cancelled else outcome.state,
                    # A late result after cancel/revoke is not kept.
                    latency_ms=None if cancelled else outcome.latency_ms,
                    reason_code=("cancelled" if cancelled
                                 and not outcome.cleanup_pending else outcome.reason),
                    status_applied=outcome.status_applied,
                    cleanup_pending=outcome.cleanup_pending))
        result = diagnose_result(flight.request_id, status_applied=outcome.status_applied,
                                 reason_code=outcome.reason,
                                 cleanup_pending=outcome.cleanup_pending)
        if cancelled and outcome.cleanup_pending and not flight.revoked:
            # T-P2-31b (决定_第三波裁定 T-P2-31a #2): a user cancel —
            # nodes.cancelDiagnose like cancel{op} — whose cleanup failed ends
            # failed/cleanup_failed with cleanup_pending=true. Only an owner
            # revocation (no observer left) keeps ending cancelled.
            raise JobFailure(diagnose_failure_code("cleanup_failed"),
                             partial_result=diagnose_result(
                                 flight.request_id, status_applied=outcome.status_applied,
                                 reason_code="cleanup_failed", cleanup_pending=True),
                             error=diagnose_error("cleanup_failed"))
        if cancelled:
            # The runtime call has returned (its cleanup ran): the executor
            # ends the job cancelled; the late result never becomes a result.
            event = getattr(flight.context, "cancelled", None)
            if event is not None:
                event.set()
            result["reason_code"] = "cancelled"
            return result
        if outcome.state == "ok" and not outcome.cleanup_pending:
            return result
        reason = outcome.reason or "diagnostic_unavailable"
        raise JobFailure(diagnose_failure_code(reason), partial_result=result,
                         error=diagnose_error(reason))

    # -- cancellation ----------------------------------------------------------------

    def _request_cancel(self, flight: _Flight, *, revoke: bool = False) -> str:
        """Mark and request the real cancellation; never blocks the caller.

        Returns the flight state seen: ``resolving`` (nothing started, nothing
        to clean), ``running`` (runtime cancel requested, retried in the
        background until it lands or the call returns) or ``finished``.
        """
        with self._lock:
            state = flight.state
            if state == "finished":
                return state
            flight.cancel_requested = True
            flight.revoked = flight.revoked or revoke
            start = state == "running" and not flight.canceller
            flight.canceller = flight.canceller or start
        if start:
            self._thread_factory(target=self._cancel_loop, args=(flight,),
                                 name="bridge-diagnostic-cancel", daemon=True).start()
        return state

    def _cancel_loop(self, flight: _Flight) -> None:
        # The runtime registers its request only after resolving inventory:
        # an early cancel returns False.  Retry until it lands or the call
        # returned (bounded by the runtime's own startup/probe timeouts).
        while not flight.call_done.is_set():
            try:
                if self.port.cancel_node_diagnostic(flight.request_id):
                    return
            except Exception as exc:
                _LOG.warning("node diagnostic cancel raised %s (request %s)",
                             type(exc).__name__, flight.request_id)
            flight.call_done.wait(self.cancel_retry_seconds)

    def cancel_job(self, args: Mapping[str, Any], context: Any) -> CancellationReport:
        """Executor ``cancel`` callable for ``cancel{op}`` on nodes.diagnose.

        The executor calls it only for a running job.  When this job's handler
        has not registered its flight yet (S2: the job is running but the
        handler is still validating / resolving its owner), the cancel is
        remembered by op and honoured at the registration gate; nothing has
        started, so reporting cleanup complete is true.
        """
        key = _context_key(context)
        with self._lock:
            flight = self._flights_by_op.get(key)
            if flight is None:
                self._bounded_put(self._early_cancels, key, None, JOB_MEMORY_LIMIT)
                return CancellationReport(True, True)
        if flight.context is not context:
            return CancellationReport(False)
        state = self._request_cancel(flight)
        if state == "finished":
            return CancellationReport(False)
        if state == "resolving":
            return CancellationReport(True, True)
        # Released only after the runtime call returned (resource cleanup ran).
        # T-P2-31a (P2-17 SHOULD-7): the cleanup-failed receipt makes the job
        # end ``failed`` (cleanup_failed, cleanup_pending=true), not cancelled.
        return CancellationReport(True, flight.call_done,
                                  cleanup_failure=lambda: self._cleanup_failure(flight))

    @staticmethod
    def _cleanup_failure(flight: _Flight) -> JobFailure | None:
        """Read once the runtime call returned; never blocks."""
        if not flight.call_done.is_set():
            return None
        outcome = runtime_outcome(flight.raw)
        if not outcome.cleanup_pending:
            return None
        return JobFailure(diagnose_failure_code("cleanup_failed"),
                          partial_result=diagnose_result(flight.request_id,
                                                         status_applied=outcome.status_applied,
                                                         reason_code="cleanup_failed",
                                                         cleanup_pending=True),
                          error=diagnose_error("cleanup_failed"))

    def cancel_request(self, args: Mapping[str, Any], context: Any) -> dict[str, bool]:
        """``nodes.cancelDiagnose`` (control domain, res): bounded cleanup wait."""
        request_id = args.get("request_id") if isinstance(args, Mapping) else None
        if not isinstance(request_id, str) or not request_id:
            raise JobFailure("validation")
        owner = self._owner_of(context)
        with self._lock:
            flight = self._flight if (self._flight is not None
                                      and self._flight.request_id == request_id) else None
            tomb = self._tombstones.get(request_id)
        if flight is None:
            if tomb is None:
                raise JobFailure("conflict")
            if tomb.owner is not None and tomb.owner != owner:
                raise JobFailure("not_allowed")
            return {"requested": False, "cleanup_pending": tomb.cleanup_pending}
        if flight.owner is not None and flight.owner != owner:
            raise JobFailure("not_allowed")
        state = self._request_cancel(flight)
        if state == "finished":
            with self._lock:
                tomb = self._tombstones.get(request_id)
            return {"requested": False,
                    "cleanup_pending": bool(tomb.cleanup_pending) if tomb else False}
        if state == "resolving":
            return {"requested": True, "cleanup_pending": False}
        # SHOULD-3 / S5: the single control worker is shared with stop/quit.
        # Only the first cancelDiagnose of a flight waits; every later one
        # shares that deadline and answers the facts at once after it.
        with self._lock:
            if flight.cancel_deadline is None:
                flight.cancel_deadline = self._monotonic() + self.cancel_wait_seconds
            remaining = max(0.0, flight.cancel_deadline - self._monotonic())
        cleaned = flight.call_done.wait(remaining)
        return {"requested": True,
                "cleanup_pending": not cleaned or runtime_outcome(flight.raw).cleanup_pending}

    def revoke_owner(self, owner: str) -> None:
        """Owner-revoke hook: cancel that owner's diagnostic; never blocks.

        The owner is remembered first, under the same lock as the
        registration gate in ``diagnose``: a job of that owner still queued in
        the executor (whose revoke does not end diagnostic jobs) is cancelled
        when it starts, before any resolution or runtime call (M1).
        """
        with self._lock:
            if owner is not None:
                self._bounded_put(self._revoked_owners, owner, None, REVOKED_OWNER_LIMIT)
            flight = self._flight
        if flight is not None and flight.owner is not None and flight.owner == owner:
            self._request_cancel(flight, revoke=True)


# --------------------------------------------------------------------------
# Wiring (called by the integration ticket, not by bridge_main here)
# --------------------------------------------------------------------------

def register(session: Any, *, port: DiagnosticRuntimePort | None = None,
             owner: DiagnosticsOwner | None = None) -> DiagnosticsOwner:
    """Register ``nodes.diagnose`` / ``nodes.cancelDiagnose`` and the revoke hook.

    ``session`` needs ``executor`` and, without an explicit port, ``runtime``.
    The executor is looked up at call time (a new epoch has a new one).
    ``nodes.detail`` (another ticket) reads ``owner.last_diagnostic(node_id)``.
    """
    if owner is None:
        owner = DiagnosticsOwner(
            port if port is not None else RuntimeDiagnosticPort(session.runtime),
            owner_of=executor_owner_resolver(lambda: session.executor))
    session.executor.register(COMMAND, owner.diagnose, cancel=owner.cancel_job)
    session.executor.register(CANCEL_COMMAND, owner.cancel_request)
    hook = getattr(session, "register_owner_revoke_hook", None)
    if callable(hook):
        hook(owner.revoke_owner)
    return owner
