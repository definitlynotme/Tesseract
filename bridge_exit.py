"""Exit identity request owner: ``exit.verify`` and the ``exit.identity`` topic.

T-P2-16 (ASK-02 M-03; PROTOCOL §10 exit.verify, §11 exit.identity, §15, §16).

One ``ExitIdentityOwner`` holds, for the whole sidecar, everything the Qt
window used to keep on itself: the request owner (the job context), the
original deadline, the lease, the current and previous IdentityResult and the
progress phase.  Rules:

* ``exit.verify`` is single-flight in the normal domain, for ownership AND for
  the runtime probe.  A second request while one is owned, or while a revoked
  request's probe is still running in the runtime (an orphan, at most ~8.25 s),
  is refused with ``conflict`` / ``errors.exit.in_progress``; it is never
  queued behind the first on another normal worker and never starts a second
  concurrent manual check (those carry native commit / isolation side
  effects).  ``max_orphans`` > 0 is a test-only relaxation.
* Cancellation is ``owner``: the executor revokes the job, this owner closes
  the request as ``cancelled`` and ignores the late result.  The probe itself
  keeps running until its own deadline; nothing here claims it stopped (both
  stacks of a cancelled result stay ``unknown``).
* Every probe attempt receives an absolute deadline that is never extended.
  Like Qt, a 429 is not retried by default (``rate_limit_retries=0``); the
  pause is recorded for automatic lookups.  An opt-in Retry-After retry runs
  inside the SAME attempt deadline (no new budget).
* A request made while the service is still starting is not executed: it is
  recorded as deferred (``running`` with ``reason_code='deferred'``) and runs
  once the lifecycle reports ready, within the 120 s deferral window.
* T-P2-31a: the registered handler answers through deferred completion; the
  request's waits run on its own driver thread and automatic lookups on
  theirs, so neither holds one of the two normal workers.
* On completion the owner re-checks request ownership, the lifecycle /
  network-context / Core generation captured when the attempt started and,
  when the runtime offers it, the current route binding.  A mismatch discards
  the result (``superseded``); a revoked owner discards it silently.
* ``previous`` is always "the last result": the latest usable result that
  preceded ``current``, demoted to ``fresh=false``.  It is never promoted back
  to ``current``.
* Fallback results are kept verbatim as the four IdentityResult booleans and
  mapped to ``errors.exit.*`` message keys.  On a failed result the Fallback
  flag is the primary ``reason_code`` (fixtures ``exit_*.json``); the runtime
  failure reason stays on the stack legs.  ``native_fallback_verified`` means
  Mihomo moved by itself and the application verified it; nothing here
  describes it as the application switching nodes.
* A usable result that carries ``diagnostics.overview_warning_reason`` (e.g.
  the system proxy does not point at this app) keeps its addresses, reports
  the warning as ``reason_code`` and is never ``fresh``.

Note: ``DesktopRuntime.lifecycle_status()`` bumps the runtime's
``_bridge_lifecycle_snapshot_revision`` on every call; bridge_publish strips
that field before comparing.  Never use it here as a change detector.

Automatic lookups (T-P2-17, ASK-02 M-03) are scheduled by the same owner.
It consumes three observations: ``ui.presence`` (shell only), lifecycle
samples and route publications.  The old Qt rule is kept unchanged: only a
VISIBLE overview, a READY service and a settled binding may start one;
``focused`` is never required (PROTOCOL §13).  The decision itself is the
shared ``OverviewAutoIdentityScheduler`` (spacing, hourly cap, back-off,
429 / Retry-After pause).  Scheduling never waits for the network: a start
is handed to the normal domain (``auto_submit``) and the probe stays
single-flight with manual requests.  A manual request arriving during an
automatic lookup takes the request over (the automatic one yields and its
result is ignored) and runs as a formal request once the automatic probe has
returned; the same bounded wait applies when only an orphaned automatic probe
(timed out, still running in the runtime) is left.  An automatic start the
normal domain accepted but never ran (dropped by ``begin_shutdown`` /
``prepare_quit`` / ``advance_epoch``) is reclaimed after its start deadline,
so scheduling cannot stay ``busy`` forever.  An automatic lookup publishes nothing while it runs and replaces
``current`` only when Qt would have shown its result.  It is an identity read
through the route the runtime already holds READY: never a route-refresh
timer and never a substitute for the Gateway exit recheck.

Pure helpers marked "Qt port" are copies of frozen desktop_app.py logic.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
import ipaddress
import logging
import math
import re
import threading
import time
from typing import Any, Callable, Mapping, Protocol
import uuid

from bridge_jobs import JobFailure, Pending
from bridge_vocab import error_message_key
from desktop_runtime_preferences import overview_auto_exit_lookup_enabled
from overview_auto_identity import (
    AUTO_REFRESH_AFTER_SECONDS,
    AUTO_SKIPPED_REASONS,
    AutoLookupDecision,
    AutoLookupInputs,
    OverviewAutoIdentityScheduler,
)
from overview_validation_view import (
    overview_result_awaits_connection,
    overview_service_availability,
    overview_visible_identity,
)


TOPIC = "exit.identity"
COMMAND = "exit.verify"
PRESENCE_COMMAND = "ui.presence"

# Qt port: desktop_app.py:650-661 and startup_phase_view.py:31 (frozen values).
ATTEMPT_SECONDS = 8.0                 # OVERVIEW_VALIDATION_FALLBACK_RUNTIME_SECONDS
DEFER_MAX_WAIT_SECONDS = 120.0        # OVERVIEW_DEFERRED_CHECK_MAX_WAIT_SECONDS
DEFER_RETRY_SECONDS = 30.0            # OVERVIEW_DEFERRED_CHECK_RETRY_SECONDS
DEFER_MAX_ATTEMPTS = 12               # OVERVIEW_DEFERRED_CHECK_MAX_ATTEMPTS
WATCHDOG_GRACE_SECONDS = 0.25         # VALIDATION_WATCHDOG_GRACE_SECONDS
# Bridge-only bounds.
RATE_LIMIT_RETRIES = 0                # Qt never retries a manual 429 (opt-in only)
RATE_LIMIT_MIN_ATTEMPT_SECONDS = 1.0
# Orphan probes allowed to keep running beside a NEW owned probe.  0 keeps
# runtime manual checks strictly serial; a larger value is test-only.
MAX_ORPHAN_PROBES = 0
# Automatic lookups (T-P2-17).
OVERVIEW_RESULT_FRESH_SECONDS = 300.0  # Qt port: desktop_app.py:660 (a clicked result)
# A manual request that takes over an automatic lookup waits for the
# automatic probe (bounded by its own deadline) plus this margin.
UPGRADE_WAIT_MARGIN_SECONDS = 1.0
# Slack added to the deferred-completion cap of one manual request (T-P2-31a).
VERIFY_BUDGET_MARGIN_SECONDS = 30.0
# Reason reported by an owner built without an automatic runner
# (``auto_submit``): it has no automatic lookup wired and never starts one.
# The value is kept from the P2-16 stub contract.
AUTO_NOT_WIRED = "not_implemented"
PRESENCE_PAGES = frozenset({"overview", "nodes", "settings", "logs", "alerts", "about", "none"})

_LOG = logging.getLogger(__name__)

FALLBACK_FLAGS = (
    "native_fallback_verified",
    "native_fallback_verified_commit_rejected",
    "native_fallback_unverified_budget",
    "manual_execution_binding_stale",
)
# Primary reason_code priority on a failed result (most binding first).
FALLBACK_FAILURE_PRIORITY = (
    "manual_execution_binding_stale",
    "native_fallback_verified_commit_rejected",
    "native_fallback_unverified_budget",
    "native_fallback_verified",
)
# Qt overview_validation_view.OVERVIEW_WARNING_TEXT keys: the address stays
# visible, but a secondary proof failed.
WARNING_REASONS = frozenset({
    "partial_validation", "system_proxy_capture_inactive", "system_proxy_host_path_unverified",
})

# reason_code -> exit.verify failure code.  Every key also owns the message key
# ``errors.exit.<reason>``; codes are limited to the exit.verify contract
# (common codes plus not_running/needs_route/timeout).
EXIT_REASON_CODES = {
    # PROTOCOL §15 Fallback results (fixtures exit_*.json: all needs_route).
    "route_contract_failed": "needs_route",
    "native_fallback_verified": "needs_route",
    "native_fallback_verified_commit_rejected": "needs_route",
    "native_fallback_unverified_budget": "needs_route",
    "manual_execution_binding_stale": "needs_route",
    # Runtime reasons (desktop_runtime.check_current_exit_identity).
    "validation_timeout": "timeout",
    "identity_timeout": "timeout",
    "identity_rate_limited": "timeout",
    "timeout": "timeout",
    "identity_unavailable": "needs_route",
    "identity_conflict": "conflict",
    "identity_rotating": "conflict",
    "identity_stale": "conflict",
    "waiting_connection": "needs_route",
    "connection_not_ready": "needs_route",
    "route_unconfirmed": "needs_route",
    "exit_unreachable": "needs_route",
    "data_plane_unavailable": "needs_route",
    "partial_validation": "needs_route",
    "probe_target_error": "needs_route",
    "system_proxy_capture_inactive": "needs_route",
    "system_proxy_host_path_unverified": "needs_route",
    "route_changed": "conflict",
    "manual_validation_execution_changed": "conflict",
    "validation_cancelled": "conflict",
    "probe_busy": "runtime_unavailable",
    "probe_unavailable": "runtime_unavailable",
    "lookup_unavailable": "runtime_unavailable",
    "core_not_running": "not_running",
    "runtime_stop_requested": "not_running",
    # Bridge owner reasons.
    "service_stopped": "not_running",
    "in_progress": "conflict",
    "owner_revoked": "conflict",
    "lifecycle_changed": "conflict",
    "network_changed": "conflict",
    "core_generation_changed": "conflict",
}
DEFAULT_FAILURE_CODE = "runtime_unavailable"
DEFAULT_MESSAGE_KEY = "errors.exit.unavailable"

_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MARKER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_LEG_FAMILY_REASONS = {"conflict", "rotating", "stale", "unavailable"}
_QUIET_REASONS = {"manual_lookup", "auto_lookup", "none", ""}


def _ms() -> int:
    return time.time_ns() // 1_000_000


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _token(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if _TOKEN.fullmatch(text) else None


def _marker(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if _MARKER.fullmatch(text) else None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _iso_ms(value: Any) -> int | None:
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        stamp = int(parsed.timestamp() * 1000)
        return stamp if stamp >= 0 else None
    return None


def _address(value: Any) -> str | None:
    try:
        return str(ipaddress.ip_address(str(value or "").strip()))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Message keys and errors (PROTOCOL §15)
# --------------------------------------------------------------------------

def exit_message_key(reason_code: Any) -> str:
    reason = _token(reason_code)
    if reason in EXIT_REASON_CODES:
        # One shared rule for ``errors.<domain>.<code>`` (bridge_vocab).
        return error_message_key(reason, domain="exit")
    return DEFAULT_MESSAGE_KEY


def exit_failure_code(reason_code: Any) -> str:
    return EXIT_REASON_CODES.get(_token(reason_code) or "", DEFAULT_FAILURE_CODE)


def identity_message_keys(result: Mapping[str, Any]) -> list[str]:
    """Ordered message keys for one IdentityResult: reason first, then flags.

    ``idle`` and ``running`` have none: a running ``reason_code`` is only the
    progress phase (``deferred``, ``querying`` ...), not an error.
    """
    if result.get("status") in {"idle", "running"}:
        return []
    keys: list[str] = []
    reason = _token(result.get("reason_code"))
    if reason:
        keys.append(exit_message_key(reason))
    for flag in FALLBACK_FLAGS:
        key = f"errors.exit.{flag}"
        if result.get(flag) is True and key not in keys:
            keys.append(key)
    return keys


def exit_error(result: Mapping[str, Any]) -> dict[str, Any]:
    """Error DTO for a failed exit.verify job (PROTOCOL §7/§15)."""
    reason = _token(result.get("reason_code")) or "lookup_unavailable"
    error: dict[str, Any] = {
        "code": exit_failure_code(reason),
        "message_key": exit_message_key(reason),
        "params": {},
        "reason_code": reason,
    }
    flags = [flag for flag in FALLBACK_FLAGS if result.get(flag) is True and flag != reason]
    if flags:
        error["params"] = {"fallback": flags}
    return error


# --------------------------------------------------------------------------
# IdentityResult builders (PROTOCOL §16)
# --------------------------------------------------------------------------

def identity_leg(status: str, *, address: str | None = None, country: str | None = None,
                 reason_code: str | None = None, observed_at_ms: int | None = None) -> dict[str, Any]:
    return {"status": status, "address": address, "country": country,
            "reason_code": reason_code, "observed_at_ms": observed_at_ms}


def _base_result(request_id: str, generation: int, status: str, *, reason_code: str | None,
                 leg_status: str, leg_reason: str | None = None,
                 binding_id: str | None = None, observed_at_ms: int | None = None) -> dict[str, Any]:
    result = {
        "request_id": request_id, "generation": int(generation), "binding_id": binding_id,
        "status": status, "reason_code": reason_code,
        "v4": identity_leg(leg_status, reason_code=leg_reason),
        "v6": identity_leg(leg_status, reason_code=leg_reason),
        "fresh": False, "observed_at_ms": observed_at_ms,
    }
    result.update(dict.fromkeys(FALLBACK_FLAGS, False))
    return result


def idle_result() -> dict[str, Any]:
    return _base_result("idle", 0, "idle", reason_code=None, leg_status="unknown")


def running_result(request_id: str, generation: int, phase: str | None,
                   *, deferred: bool = False) -> dict[str, Any]:
    return _base_result(request_id, generation, "running", reason_code=_token(phase),
                        leg_status="unknown" if deferred else "running")


def closed_result(request_id: str, generation: int, status: str, reason_code: str,
                  *, observed_at_ms: int | None = None) -> dict[str, Any]:
    """cancelled / superseded / failed without a usable runtime payload.

    A cancelled (owner revoked) request keeps both stacks ``unknown``: only
    the ownership ended; the probe may still be running in the runtime.
    """
    leg_status = "unknown" if status in {"cancelled", "superseded"} else "failed"
    leg_reason = reason_code if leg_status == "failed" else None
    return _base_result(request_id, generation, status, reason_code=_token(reason_code),
                        leg_status=leg_status, leg_reason=_token(leg_reason),
                        observed_at_ms=observed_at_ms)


def fallback_flags(payload: Mapping[str, Any]) -> dict[str, bool]:
    """Keep the Fallback session's switch outcome verbatim as booleans."""
    diagnostics = _mapping(payload.get("diagnostics"))
    neutral = _mapping(diagnostics.get("neutral_connectivity"))
    seen = {
        str(neutral.get("switch_reason") or ""),
        str(diagnostics.get("overview_failure_reason") or ""),
        str(diagnostics.get("data_plane_reason") or ""),
        str(payload.get("error") or ""),
    }
    return {flag: flag in seen for flag in FALLBACK_FLAGS}


def lookup_outcome(payload: Mapping[str, Any]) -> tuple[bool, str, float | None]:
    """Qt port of desktop_app.py:17068 ``_overview_lookup_outcome``.

    Returns (usable, failure reason, largest Retry-After seconds).
    """
    diagnostics = _mapping(payload.get("diagnostics"))
    reason = str(diagnostics.get("overview_failure_reason") or "")
    visible = overview_visible_identity(dict(payload))
    identity = _mapping(visible.get("identity"))
    primary = ""
    for family in ("ipv4", "ipv6"):
        primary = str(_mapping(identity.get(family)).get("ip") or "").strip()
        if primary:
            break
    if not primary:
        primary = str(visible.get("ip") or "").strip()
    usable = bool(str(payload.get("state") or "") in {"complete", "partial"}
                  and not payload.get("error") and primary)
    retry_after = None
    raw_identity = _mapping(payload.get("identity"))
    for family in ("ipv4", "ipv6"):
        family_payload = _mapping(raw_identity.get(family))
        for source in family_payload.get("evidence", []) or []:
            for attempt in _mapping(source).get("attempts", []) or []:
                attempt = _mapping(attempt)
                for item in [*(attempt.get("transport_retries") or []), attempt]:
                    value = _mapping(item).get("retry_after_seconds")
                    if isinstance(value, (int, float)) and not isinstance(value, bool) \
                            and math.isfinite(value):
                        retry_after = max(float(value), retry_after or 0.0)
    return usable, reason, retry_after


def payload_failure_reason(payload: Mapping[str, Any]) -> str:
    diagnostics = _mapping(payload.get("diagnostics"))
    freshness = _mapping(payload.get("freshness"))
    for candidate in (diagnostics.get("overview_failure_reason"), payload.get("error"),
                      freshness.get("reason")):
        reason = _token(candidate)
        if reason and reason not in _QUIET_REASONS:
            return reason
    state = str(payload.get("state") or "")
    if state == "route_changed" or payload.get("route_changed"):
        return "route_changed"
    if state == "data_plane_failed":
        return "exit_unreachable"
    if state in {"data_plane_indeterminate", "controller_hold"}:
        return "probe_unavailable"
    return "lookup_unavailable"


def identity_result_from_payload(payload: Mapping[str, Any], *, request_id: str,
                                 generation: int, fallback_ms: int) -> dict[str, Any]:
    """Project one runtime result into IdentityResult; never invent success.

    * A usable result with ``diagnostics.overview_warning_reason`` keeps its
      addresses (Qt overview_validation_view.py:131-133) but reports the
      warning as ``reason_code`` and is never ``fresh``.
    * On a failed result a Fallback flag is the primary ``reason_code``
      (binding_stale > commit_rejected > unverified_budget > verified); the
      runtime failure reason stays on the two stack legs.
    """
    flags = fallback_flags(payload)
    usable, _reason, _retry = lookup_outcome(payload)
    if flags["manual_execution_binding_stale"]:
        # §15: a stale manual binding never admits its late identity.
        usable = False
    diagnostics = _mapping(payload.get("diagnostics"))
    warning = _token(diagnostics.get("overview_warning_reason"))
    visible = _mapping(overview_visible_identity(dict(payload)).get("identity"))
    raw_identity = _mapping(payload.get("identity"))
    route = _mapping(payload.get("route"))
    binding_id = _marker(route.get("binding_marker"))
    observed = _iso_ms(payload.get("checked_at")) or int(fallback_ms)
    failure = None if usable else payload_failure_reason(payload)
    legs: dict[str, dict[str, Any]] = {}
    for family, key in (("ipv4", "v4"), ("ipv6", "v6")):
        shown = _mapping(visible.get(family))
        address = _address(shown.get("ip")) if usable else None
        if address:
            location = _mapping(shown.get("location"))
            country = str(location.get("country_code") or "").strip().upper()
            if location.get("state") == "conflict" or not _COUNTRY.fullmatch(country):
                country = ""
            legs[key] = identity_leg("ok", address=address, country=country or None,
                                     observed_at_ms=observed)
            legs[key].update(leg_context(shown, _mapping(raw_identity.get(family))))
            continue
        state = str(_mapping(raw_identity.get(family)).get("state") or "").strip().lower()
        if usable:
            leg_reason = f"identity_{state}" if state in _LEG_FAMILY_REASONS else "identity_unavailable"
        else:
            leg_reason = failure
        legs[key] = identity_leg("failed", reason_code=_token(leg_reason) or "identity_unavailable")
    ok_count = sum(leg["status"] == "ok" for leg in legs.values())
    if usable and ok_count:
        status = "ok" if ok_count == 2 else "partial"
        reason_code = next((flag for flag in FALLBACK_FLAGS[:3] if flags[flag]), None) or warning
    else:
        status = "failed"
        warning = None
        reason_code = (next((flag for flag in FALLBACK_FAILURE_PRIORITY if flags[flag]), None)
                       or _token(failure) or "lookup_unavailable")
    # A missing freshness block is not evidence of freshness.
    freshness_state = str(_mapping(payload.get("freshness")).get("state") or "")
    result = {
        "request_id": request_id, "generation": int(generation), "binding_id": binding_id,
        "status": status, "reason_code": reason_code, "v4": legs["v4"], "v6": legs["v6"],
        "fresh": bool(status in {"ok", "partial"} and binding_id and not warning
                      and freshness_state == "fresh"),
        "observed_at_ms": observed,
    }
    result.update(flags)
    return result


_NETWORK_TYPES = frozenset({"backbone", "business", "hosting", "mobile", "residential", "isp"})
_TIMEZONE_TEXT = re.compile(r"(?:UTC|[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+){1,2})")
_ASN_TEXT = re.compile(r"[1-9][0-9]{0,9}")


def leg_context(shown: Mapping[str, Any], raw_family: Mapping[str, Any]) -> dict[str, Any]:
    """T-P4-C2 #1 (G07, overview R1): source-reported context of an ok leg.

    Only what ``overview_ip_check`` attached to this exact observed address
    (ident.me profile): ``city``/``timezone`` when the location report is
    consistent (``state=reported``, the same rule the country follows),
    ``asn``/``org``/``type`` for the fields the sources agreed on
    (conflicting fields are already absent), and ``single_source`` = only
    one stable source confirmed the address (family state ``unconfirmed``,
    Qt's 「单一来源」 badge). ``org`` and ``city`` are untrusted external
    text: a value the frame scanner would reject is dropped, never sent.
    Absent fields mean "not reported"; nothing is invented.
    """
    from bridge_protocol import contains_sensitive_text
    context: dict[str, Any] = {}

    def text(value: Any, limit: int = 256) -> str:
        value = str(value or "").strip()
        if (not value or len(value) > limit or value.lower() in {"none", "null", "unknown"}
                or contains_sensitive_text(value)
                or any(ord(ch) < 32 or ch in "\u2028\u2029" for ch in value)):
            return ""
        return value

    location = _mapping(shown.get("location"))
    if location.get("state") == "reported":
        city = text(location.get("city"))
        zone = text(location.get("timezone"), 64)
        if city:
            context["city"] = city
        if zone and _TIMEZONE_TEXT.fullmatch(zone):
            context["timezone"] = zone
    network = _mapping(shown.get("network"))
    asn = str(network.get("asn") or "").strip().upper().removeprefix("AS").strip()
    if _ASN_TEXT.fullmatch(asn) and int(asn) < 2 ** 32:
        context["asn"] = asn
    org = text(network.get("organisation") or network.get("organization"))
    if org:
        context["org"] = org
    kind = str(network.get("type") or "").strip().casefold()
    if kind in _NETWORK_TYPES:
        context["type"] = kind
    state = str(raw_family.get("state") or shown.get("state") or "").strip().lower()
    if state in {"confirmed", "unconfirmed"}:
        context["single_source"] = state == "unconfirmed"
    return context


def _result_meta(payload: Mapping[str, Any], trigger: str) -> dict[str, Any]:
    """Binding/trigger facts of a committed result (not part of the DTO)."""
    route = _mapping(payload.get("route"))
    return {"binding_marker": str(route.get("binding_marker") or "").strip(),
            "core_instance": str(route.get("core_instance") or "").strip(),
            "trigger": trigger}


def result_is_done(result: Mapping[str, Any]) -> bool:
    """PROTOCOL §16: only ok/partial with a live binding may finish ``done``."""
    return (result.get("status") in {"ok", "partial"}
            and result.get("manual_execution_binding_stale") is False)


# --------------------------------------------------------------------------
# Lease, owner context and deferral (pure Qt ports)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ExitLease:
    """Qt port of desktop_app.py:672 ``OverviewValidationLeaseState``.

    Core generation is added: the bridge owner also binds the Core it started
    against (T-P2-16).  ``deadline_monotonic`` never leaves the process.
    """

    request_id: str
    deadline_monotonic: float
    trigger: str = "manual"
    lifecycle_revision: int | None = None
    network_context_epoch: int | None = None
    core_generation: int | None = None
    active: bool = True
    terminal_state: str = ""


def lease_terminal(lease: ExitLease, state: str) -> ExitLease:
    """Qt port of desktop_app.py:16424 ``_overview_lease_terminal``."""
    return replace(lease, active=False, terminal_state=str(state or "unavailable"))


def lease_is_active(lease: ExitLease, now: float, grace: float = WATCHDOG_GRACE_SECONDS) -> bool:
    """Qt port of desktop_app.py:16443 ``_overview_validation_is_active``."""
    return bool(lease.active and now <= lease.deadline_monotonic + grace)


@dataclass(frozen=True)
class OwnerContext:
    """The lifecycle facts one attempt is bound to (sampled, not authoritative)."""

    availability: str
    startup_phase: str
    lifecycle_revision: int | None
    network_context_epoch: int | None
    core_generation: int | None
    signature: tuple


def owner_context(lifecycle: Mapping[str, Any]) -> OwnerContext:
    intent = _mapping(lifecycle.get("intent"))
    runtime = _mapping(lifecycle.get("runtime"))
    core = _mapping(lifecycle.get("core"))
    readiness = _mapping(lifecycle.get("readiness"))
    phase = str(runtime.get("startup_phase") or "").strip().lower()
    running = bool(core.get("running"))
    availability = overview_service_availability(
        {"startup_phase": phase, "core_running": running})
    revision = _nonnegative_int(intent.get("lifecycle_revision"))
    network = _nonnegative_int(readiness.get("network_context_epoch"))
    generation = _nonnegative_int(core.get("core_generation"))
    # Qt port of desktop_app.py:16846 ``_overview_exit_status_signature``.
    # core_runtime_phase / initial_admission_active have no public getter yet;
    # readiness validation_active is the nearest public substitute.
    signature = (phase, str(readiness.get("state") or ""), running, revision, generation,
                 bool(readiness.get("validation_active")))
    return OwnerContext(availability, phase, revision, network, generation, signature)


def lease_owner_mismatch(lease: ExitLease, context: OwnerContext) -> str | None:
    """Qt port of desktop_app.py:16566 plus the Core generation; reason or None."""
    if (lease.lifecycle_revision is not None and context.lifecycle_revision
            and context.lifecycle_revision != lease.lifecycle_revision):
        return "lifecycle_changed"
    if (lease.network_context_epoch is not None and context.network_context_epoch is not None
            and context.network_context_epoch != lease.network_context_epoch):
        return "network_changed"
    if (lease.core_generation is not None and context.core_generation is not None
            and context.core_generation != lease.core_generation):
        return "core_generation_changed"
    return None


def completed_within_deadline(lease: ExitLease, payload: Mapping[str, Any]) -> bool:
    """Qt port of desktop_app.py:16600: execution owns the deadline."""
    completed = payload.get("completed_monotonic")
    if isinstance(completed, (int, float)) and not isinstance(completed, bool):
        return 0.0 <= float(completed) <= lease.deadline_monotonic
    return True


@dataclass(frozen=True)
class Deferral:
    """Qt port of the desktop_app.py:16866-16960 deferred-click fields."""

    deadline_monotonic: float
    attempts: int
    periodic: bool
    signature: tuple
    attempt_at: float


def arm_deferral(current: Deferral | None, now: float, signature: tuple, *, periodic: bool,
                 max_wait: float = DEFER_MAX_WAIT_SECONDS) -> Deferral:
    """Qt port of ``_arm_deferred_overview_exit_check``: the window never grows."""
    if current is None:
        return Deferral(now + max_wait, 0, bool(periodic), signature, now)
    return replace(current, periodic=bool(periodic), signature=signature, attempt_at=now)


def deferral_step(deferral: Deferral, now: float, context: OwnerContext, *,
                  retry_seconds: float = DEFER_RETRY_SECONDS,
                  max_attempts: int = DEFER_MAX_ATTEMPTS) -> tuple[str, Deferral]:
    """Qt port of ``_resume_deferred_overview_exit_check``.

    Returns (action, deferral): offline | expired | wait | resume.
    """
    if context.availability == "offline":
        return "offline", deferral
    if now >= deferral.deadline_monotonic or (
            context.availability == "ready" and deferral.attempts >= max_attempts):
        return "expired", deferral
    if context.availability != "ready":
        return "wait", deferral
    changed = context.signature != deferral.signature
    retry_due = deferral.periodic and now - deferral.attempt_at >= retry_seconds
    if not (changed or retry_due):
        return "wait", deferral
    return "resume", replace(deferral, signature=context.signature, attempt_at=now,
                             attempts=deferral.attempts + 1)


def should_defer_result(payload: Mapping[str, Any], *, trigger: str, context: OwnerContext,
                        deferred: bool) -> tuple[bool, bool]:
    """Qt port of ``_defer_overview_exit_result_while_connecting`` (16893).

    Returns (defer, periodic).
    """
    if trigger == "auto" or not overview_result_awaits_connection(payload):
        return False, True
    if context.availability == "offline":
        return False, True
    if not deferred and context.startup_phase not in {"collecting", "core_standby"}:
        return False, True
    reason = str(_mapping(payload.get("diagnostics")).get("data_plane_reason") or "")
    return True, reason != "initial_egress_admission_pending"


# --------------------------------------------------------------------------
# Runtime port
# --------------------------------------------------------------------------

class ExitRuntimePort(Protocol):
    """Runtime facts the owner needs.  All calls run on a worker or on the
    owner's driver threads, never on the pipe reader or a publication tick."""

    def lifecycle_status(self) -> Mapping[str, Any]:
        """RT-3 five-domain sample (intent/runtime/core/readiness/freshness)."""

    def proxy_url(self) -> str | None:
        """Local mixed-port URL to carry the lookup, or None without a Core.

        Runtime getter ``bridge_exit_proxy_url`` should honour
        ``system_proxy.host`` (wildcards -> 127.0.0.1) like Qt
        desktop_app.py:14953-14962; the fallback only knows the mixed port."""

    def exit_binding(self) -> Mapping[str, Any] | None:
        """Current logical route binding without a Mihomo read, or None (unknown).

        ``{binding_marker, core_instance, confirmed}``.  Needs runtime getter
        ``bridge_exit_binding`` (see report); HARD dependency before P2-17
        wiring.  ``confirmed`` is REQUIRED (Qt reads ``route.get('confirmed')
        is True``): a binding without ``confirmed: true`` never starts an
        automatic lookup.  Two uses: (1) the completion binding check (skipped while
        None) and (2) the click lifting the per-(binding, core_instance)
        back-off (Qt desktop_app.py:17017; a no-op while None)."""

    def check_current_exit_identity(self, **kwargs: Any) -> Mapping[str, Any]:
        """``DesktopRuntime.check_current_exit_identity`` (public entry)."""

    def auto_lookup_enabled(self) -> bool:
        """``ui.overview_auto_exit_lookup`` of the effective config (default on).

        Optional on test ports; a port without it counts as enabled, like
        the config default (desktop_runtime_preferences.py:11)."""


class RuntimeExitPort:
    """Adapter over DesktopRuntime public methods only.

    No ``current_route_context``: that path reads ``GET /proxies`` (§5.2 bans
    bridge-originated Mihomo reads).  Missing getters report unknown (None).
    """

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def lifecycle_status(self) -> Mapping[str, Any]:
        return self.runtime.lifecycle_status()

    def proxy_url(self) -> str | None:
        getter = getattr(type(self.runtime), "bridge_exit_proxy_url", None)
        if getter is not None:
            return getter(self.runtime)
        core = _mapping(_mapping(self.runtime.lifecycle_status()).get("core"))
        if not core.get("running"):
            return None
        port = _mapping(self.runtime.connection_info()).get("mixed_port")
        # Qt port of desktop_app.py:14949: the system proxy host defaults to
        # 127.0.0.1 and wildcard listeners are reached on loopback.
        return f"http://127.0.0.1:{int(port)}" if port else None

    def exit_binding(self) -> Mapping[str, Any] | None:
        getter = getattr(type(self.runtime), "bridge_exit_binding", None)
        return getter(self.runtime) if getter is not None else None

    def check_current_exit_identity(self, **kwargs: Any) -> Mapping[str, Any]:
        if kwargs.get("trigger") == "auto":
            # T-P2-RT2 (Fallback 10-06 ruling): the bridge's automatic lookup
            # reads only the published route snapshot (no GET /proxies); the
            # runtime default "live" stays Qt's behavior.
            kwargs = {**kwargs, "route_source": "published"}
        return self.runtime.check_current_exit_identity(**kwargs)

    def auto_lookup_enabled(self) -> bool:
        # Same pure rule as Qt (desktop_app.py:16996) over the public config
        # attribute; a read-only dict lookup, no I/O. Literal attribute access:
        # the bridge private-access gate forbids getattr on the runtime.
        try:
            config = self.runtime.config
        except AttributeError:
            config = {}
        return overview_auto_exit_lookup_enabled(config)


# --------------------------------------------------------------------------
# Owner
# --------------------------------------------------------------------------

def _revoked(flight: "_Flight") -> bool:
    """The executor's owner revoke / cancel flag (``JobContext.owner_revoked``)."""
    revoked = getattr(flight.context, "owner_revoked", None)
    return bool(revoked is not None and revoked.is_set())


class _Probe:
    __slots__ = ("done", "payload", "failed", "orphaned")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.payload: Mapping[str, Any] | None = None
        self.failed = False
        self.orphaned = False


class _AutoContext:
    """Ownership of an automatic lookup: no job, no wire owner.

    ``owner_revoked`` is set when a manual request takes the lookup over (the
    automatic request yields) or when an accepted start never ran before its
    start deadline and is reclaimed; the late result is then ignored exactly
    like a revoked manual request's.  (The owner has no ``close``: a session
    that drops it simply stops feeding observations.)
    """

    def __init__(self) -> None:
        self.op = None
        self.owner_revoked = threading.Event()
        self.superseded = threading.Event()


class _Flight:
    def __init__(self, request_id: str, context: Any, alive: Callable[[], bool] | None,
                 trigger: str, *, binding_marker: str = "", core_instance: str = "") -> None:
        self.request_id = request_id
        self.context = context
        self.alive = alive
        self.trigger = trigger
        self.generation = 0
        self.phase = ""
        self.attempt = 0
        self.deferred = False
        # The binding an automatic lookup was decided for (expected marker).
        self.binding_marker = binding_marker
        self.core_instance = core_instance
        self.pending_meta: Mapping[str, Any] | None = None
        # Automatic flights only (MUST-1): a start handed to the normal
        # domain that has not begun running by ``start_deadline`` is
        # reclaimed; ``started`` is set by ``_run_auto`` under the owner lock.
        self.start_deadline: float | None = None
        self.started = False


class ExitIdentityOwner:
    """Single owner of exit identity requests, results and progress."""

    def __init__(
        self, port: ExitRuntimePort, *, publish: Callable[[dict[str, Any]], Any] | None = None,
        invalidate: Callable[[], Any] | None = None,
        monotonic:Callable[[], float] = time.monotonic, wall_ms: Callable[[], int] = _ms,
        attempt_seconds: float = ATTEMPT_SECONDS, defer_max_wait: float = DEFER_MAX_WAIT_SECONDS,
        defer_retry_seconds: float = DEFER_RETRY_SECONDS,
        defer_max_attempts: int = DEFER_MAX_ATTEMPTS, grace_seconds: float = WATCHDOG_GRACE_SECONDS,
        poll_seconds: float = 0.1, lifecycle_poll_seconds: float = 1.0,
        max_orphans: int = MAX_ORPHAN_PROBES, rate_limit_retries: int = RATE_LIMIT_RETRIES,
        rate_limit_min_attempt: float = RATE_LIMIT_MIN_ATTEMPT_SECONDS,
        scheduler: OverviewAutoIdentityScheduler | None = None,
        thread_factory: Callable[..., Any] = threading.Thread,
        auto_submit: Callable[[Callable[[], Any]], bool] | None = None,
    ) -> None:
        self.port = port
        self._publish_sink = publish
        self._invalidate_sink = invalidate
        self._monotonic = monotonic
        self._wall_ms = wall_ms
        self.attempt_seconds = float(attempt_seconds)
        self.defer_max_wait = float(defer_max_wait)
        self.defer_retry_seconds = float(defer_retry_seconds)
        self.defer_max_attempts = int(defer_max_attempts)
        self.grace_seconds = float(grace_seconds)
        self.poll_seconds = float(poll_seconds)
        self.lifecycle_poll_seconds = float(lifecycle_poll_seconds)
        self.max_orphans = int(max_orphans)
        self.rate_limit_retries = int(rate_limit_retries)
        self.rate_limit_min_attempt = float(rate_limit_min_attempt)
        self.scheduler = scheduler if scheduler is not None else OverviewAutoIdentityScheduler()
        self._thread_factory = thread_factory
        self._lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self._wake = threading.Event()
        self._current: dict[str, Any] = idle_result()
        self._previous: dict[str, Any] | None = None
        self._flight: _Flight | None = None
        self._orphans = 0
        self._live_probes = 0        # runtime checks not yet returned (owned or orphaned)
        self._auto_live_probes = 0   # of which started by an automatic lookup
        self._published: dict[str, Any] | None = None
        self.publish_errors = 0
        # Automatic scheduling inputs (T-P2-17).  Nothing is visible before
        # the shell reports presence; nothing is ready before a sample.
        self._auto_submit = auto_submit
        self._presence: dict[str, Any] = {"visible": False, "focused": False, "page": "none"}
        self._lifecycle: Mapping[str, Any] | None = None
        self._route: Mapping[str, Any] | None = None
        # Facts about ``current`` that the DTO does not carry: the binding it
        # was observed on, when it was committed and who asked for it.
        self._current_meta: dict[str, Any] = {}
        self.last_auto_decision: AutoLookupDecision | None = None

    # -- read side -----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Complete ``exit.identity`` DTO (subscription seed / new epoch)."""
        with self._lock:
            return {"current": deepcopy(self._current), "previous": deepcopy(self._previous)}

    def progress(self) -> dict[str, Any] | None:
        with self._lock:
            flight = self._flight
            if flight is None:
                return None
            return {"request_id": flight.request_id, "phase": flight.phase,
                    "attempt": flight.attempt, "deferred": flight.deferred,
                    "trigger": flight.trigger}

    def presence(self) -> dict[str, Any]:
        """The last ``ui.presence`` value recorded (a copy)."""
        with self._lock:
            return dict(self._presence)

    @property
    def orphan_probes(self) -> int:
        with self._lock:
            return self._orphans

    @property
    def live_probes(self) -> int:
        """Runtime checks still running, owned or orphaned."""
        with self._lock:
            return self._live_probes

    def wake(self, *_args: Any) -> None:
        """Lifecycle observation / owner revoke hint; never required for correctness."""
        self._wake.set()

    def notify_lifecycle(self, sample: Mapping[str, Any] | None = None) -> None:
        """Sampler hook: wake a deferred request and expire stale freshness.

        With a lifecycle sample, a ``fresh`` current/previous whose Core
        generation no longer matches (or whose Core is gone) is republished
        with ``fresh=false``; the addresses stay, as "last observed".
        """
        self._wake.set()
        if not isinstance(sample, Mapping):
            return
        try:
            context = owner_context(sample)
        except Exception:
            return
        changed = False
        with self._lock:
            self._lifecycle = sample
            for result in (self._current, self._previous):
                if not result or result.get("fresh") is not True:
                    continue
                if context.availability == "offline" or (
                        context.core_generation is not None
                        and result.get("generation") != context.core_generation):
                    result["fresh"] = False
                    changed = True
        if changed:
            self._publish()
        self._schedule()

    # -- automatic scheduling (T-P2-17) ------------------------------------------

    def observe_presence(self, presence: Mapping[str, Any]) -> dict[str, bool]:
        """``ui.presence`` handler body (shell only; the executor checks shape).

        The shell sends the complete value on every change and reconnect.
        ``focused`` is recorded but never consulted (PROTOCOL §13).
        """
        if not isinstance(presence, Mapping) or not isinstance(presence.get("visible"), bool) \
                or not isinstance(presence.get("focused"), bool) \
                or presence.get("page") not in PRESENCE_PAGES:
            raise JobFailure("validation")
        with self._lock:
            self._presence = {"visible": presence["visible"], "focused": presence["focused"],
                              "page": presence["page"]}
        self._schedule()
        return {"recorded": True}

    def observe_route(self, route: Mapping[str, Any] | None) -> None:
        """A ``route.published`` DTO (P2-08 publication revision changed)."""
        with self._lock:
            self._route = dict(route) if isinstance(route, Mapping) else None
        self._schedule()

    def schedule_auto(self, presence: Mapping[str, Any] | None = None,
                      lifecycle: Mapping[str, Any] | None = None,
                      route: Mapping[str, Any] | None = None) -> AutoLookupDecision:
        """Record any given observation, decide once and maybe start a lookup."""
        with self._lock:
            if isinstance(presence, Mapping):
                self._presence = {"visible": presence.get("visible") is True,
                                  "focused": presence.get("focused") is True,
                                  "page": presence.get("page") if presence.get("page")
                                  in PRESENCE_PAGES else "none"}
            if isinstance(lifecycle, Mapping):
                self._lifecycle = lifecycle
            if isinstance(route, Mapping):
                self._route = dict(route)
        return self._schedule()

    def _auto_enabled(self) -> bool:
        getter = getattr(self.port, "auto_lookup_enabled", None)
        if not callable(getter):
            return True
        try:
            return bool(getter())
        except Exception:
            return False

    def _current_result_age_locked(self, binding_marker: str, core_instance: str,
                                   now: float) -> tuple[float | None, float]:
        """Qt port of desktop_app.py:17000 ``_overview_current_result_age``.

        Age of the displayed usable result for this exact binding (None when
        the card shows nothing current for it) and its refresh interval.
        """
        meta = self._current_meta
        refresh_after = (AUTO_REFRESH_AFTER_SECONDS if meta.get("trigger") == "auto"
                         else OVERVIEW_RESULT_FRESH_SECONDS)
        if self._current.get("status") not in {"ok", "partial"} or not binding_marker:
            return None, refresh_after
        if str(meta.get("binding_marker") or "") != binding_marker:
            return None, refresh_after
        saved_instance = str(meta.get("core_instance") or "")
        if saved_instance and core_instance and saved_instance != core_instance:
            return None, refresh_after
        at = meta.get("at")
        if not isinstance(at, (int, float)):
            return None, refresh_after
        return max(0.0, now - float(at)), refresh_after

    def _schedule(self) -> AutoLookupDecision:
        """Decide from the latest observations; never waits for the network.

        Inputs (Qt port of desktop_app.py:17024 ``_maybe_start_overview_auto_lookup``):
        visible = presence.visible and page == 'overview' (no ``focused``);
        ready = lifecycle availability ready + readiness state/validation;
        settled binding = the runtime's confirmed binding marker on a valid
        published route; busy = any request or runtime probe in flight.
        """
        if self._auto_submit is None:
            decision = AutoLookupDecision(False, AUTO_NOT_WIRED)
            self.last_auto_decision = decision
            return decision
        enabled = self._auto_enabled()
        binding = self._binding_facts()
        with self._lock:
            sample = self._lifecycle
            route = self._route
            presence = dict(self._presence)
        try:
            context = owner_context(_mapping(sample)) if sample is not None else None
        except Exception:
            context = None
        readiness = _mapping(_mapping(sample).get("readiness"))
        route_valid = isinstance(route, Mapping) and route.get("valid") is True
        marker = binding["binding_marker"]
        instance = binding["core_instance"]
        flight = None
        with self._lock:
            now = self._monotonic()
            self._reclaim_unstarted_auto_locked(now)
            age, refresh_after = self._current_result_age_locked(marker, instance, now)
            busy = bool(self._flight is not None or self._live_probes > 0
                        or context is None or context.availability != "ready")
            decision = self.scheduler.decide(AutoLookupInputs(
                now=now, enabled=enabled,
                visible=presence.get("visible") is True and presence.get("page") == "overview",
                readiness_state=str(readiness.get("state") or "").strip().lower(),
                validation_active=bool(readiness.get("validation_active")),
                route_confirmed=bool(route_valid and marker and binding["confirmed"]),
                binding_marker=marker, core_instance=instance, busy=busy,
                current_result_age=age, current_result_refresh_after=refresh_after))
            self.last_auto_decision = decision
            if decision.start:
                self.scheduler.record_start(now)
                flight = _Flight(uuid.uuid4().hex, _AutoContext(), None, "auto",
                                 binding_marker=marker, core_instance=instance)
                # A start the executor accepts but drops (begin_shutdown /
                # prepare_quit / advance_epoch discard queued callables)
                # must not hold the flight forever (MUST-1).
                flight.start_deadline = now + self.attempt_seconds + self.grace_seconds
                self._flight = flight
        if flight is not None:
            try:
                submitted = bool(self._auto_submit(lambda: self._run_auto(flight)))
            except Exception:
                submitted = False
            if not submitted:
                # Normal domain full or closing: nothing ran; the spacing
                # already recorded keeps this from retrying every sample.
                with self._lock:
                    if self._flight is flight:
                        self._flight = None
                return AutoLookupDecision(False, "busy")
        return decision

    def _reclaim_unstarted_auto_locked(self, now: float) -> bool:
        """MUST-1: release an automatic flight whose submitted work never began.

        ``_run_auto`` marks ``started`` under this lock; a flight still
        unstarted after ``start_deadline`` was dropped by the executor (or is
        so late that its decision is stale).  Revoke it so the work, should
        it ever run, returns at its start gate, and free the request slot.
        """
        flight = self._flight
        if (flight is None or flight.trigger != "auto" or flight.started
                or flight.start_deadline is None or now <= flight.start_deadline):
            return False
        flight.context.owner_revoked.set()
        self._flight = None
        _LOG.info("automatic exit identity lookup never started; reclaimed (request %s)",
                  flight.request_id)
        return True

    # -- publication -----------------------------------------------------------

    def _publish(self) -> None:
        with self._publish_lock:
            dto = self.snapshot()
            if dto == self._published or self._publish_sink is None:
                self._published = dto
                return
            try:
                self._publish_sink(dto)
            except Exception as exc:
                self.publish_errors += 1
                self._published = None
                _LOG.warning("exit.identity publish failed: %s (request %s)",
                             type(exc).__name__, _marker(dto["current"].get("request_id")))
                # Withdraw the stale baseline so subscribers see a gap instead
                # of an old value that silently stays current.
                if self._invalidate_sink is not None:
                    try:
                        self._invalidate_sink()
                    except Exception as inner:
                        _LOG.warning("exit.identity invalidate failed: %s", type(inner).__name__)
                return
            self._published = dto

    def _install_locked(self, result: dict[str, Any],
                        meta: Mapping[str, Any] | None = None) -> None:
        """The only writer of current: a usable current of another request
        becomes ``previous`` ("last result"), demoted to ``fresh=false``.

        ``meta`` carries what the DTO does not: the binding the result was
        observed on, its trigger and the commit time (automatic scheduling
        reads them as Qt's ``_overview_current_result_age``).
        """
        current = self._current
        if current.get("status") in {"ok", "partial"} and \
                current.get("request_id") != result.get("request_id"):
            self._previous = {**current, "fresh": False}
        self._current = deepcopy(result)
        # T-P4-C2 #2 (overview R3): who asked for this result, kept on the
        # DTO so a reopened page still tells 「已验证」 from 「已确认」. Every
        # writer holds the owning flight; idle has none and omits the field.
        flight = self._flight
        if (flight is not None and flight.request_id == result.get("request_id")
                and flight.trigger in {"auto", "manual"}):
            self._current["trigger"] = flight.trigger
        if meta is not None:
            self._current_meta = {**dict(meta), "at": self._monotonic()}
        elif current.get("request_id") != result.get("request_id"):
            self._current_meta = {}

    def _commit(self, flight: _Flight, result: dict[str, Any],
                probe: _Probe | None = None, *, meta: Mapping[str, Any] | None = None) -> bool:
        """Install ``result`` as current if ``flight`` still owns the request.

        Ownership (flight identity and the executor's ``owner_revoked``) is
        re-checked under the same lock as the write, so a result revoked
        between ``_owned`` and here never reaches the public topic.  A progress
        update from a probe that was orphaned or already finished can never
        overwrite the terminal result written after it.
        """
        with self._lock:
            if self._flight is not flight or _revoked(flight):
                return False
            if probe is not None and (probe.orphaned or probe.done.is_set()):
                return False
            if meta is None:
                meta, flight.pending_meta = flight.pending_meta, None
            self._install_locked(result, meta)
        self._publish()
        return True

    # -- ownership -------------------------------------------------------------

    def _owned(self, flight: _Flight) -> bool:
        if self._ownership_lost(flight):
            return False
        with self._lock:
            return self._flight is flight

    def _sample(self) -> OwnerContext | None:
        try:
            return owner_context(_mapping(self.port.lifecycle_status()))
        except Exception:
            return None

    def _binding(self) -> Mapping[str, str]:
        """``{binding_marker, core_instance}`` from the port; empty strings = unknown."""
        facts = self._binding_facts()
        return {"binding_marker": facts["binding_marker"], "core_instance": facts["core_instance"]}

    def _binding_facts(self) -> dict[str, Any]:
        """Binding plus ``confirmed``.  ``confirmed`` is a required getter field
        read exactly like Qt (``route.get('confirmed') is True``): absent or
        anything but ``True`` means no confirmed route (SHOULD-5)."""
        try:
            binding = _mapping(self.port.exit_binding())
        except Exception:
            binding = {}
        return {"binding_marker": str(binding.get("binding_marker") or "").strip(),
                "core_instance": str(binding.get("core_instance") or "").strip(),
                "confirmed": binding.get("confirmed") is True}

    # -- job handler -------------------------------------------------------------

    def verify(self, args: Mapping[str, Any], context: Any, *,
               alive: Callable[[], bool] | None = None) -> dict[str, Any]:
        """``exit.verify`` job handler (normal domain, cancellation=owner).

        Blocking form: the whole request runs on the calling thread. The
        registered handler uses ``verify_deferred`` instead.
        """
        flight, wait_for_auto = self._admit(args, context, alive)
        return self._drive(flight, wait_for_auto)

    def verify_budget_seconds(self) -> float:
        """Upper bound of one manual request (deferred-completion timeout):
        an upgrade wait, the 120 s deferral window and probe attempts."""
        attempt = self.attempt_seconds + self.grace_seconds
        return (self.defer_max_wait + 3 * attempt + UPGRADE_WAIT_MARGIN_SECONDS
                + VERIFY_BUDGET_MARGIN_SECONDS)

    def verify_deferred(self, args: Mapping[str, Any], context: Any, *,
                        alive: Callable[[], bool] | None = None,
                        thread_factory: Callable[..., Any] = threading.Thread) -> Pending:
        """``exit.verify`` through deferred completion (T-P2-31a).

        Admission (validation, single flight, take-over of an automatic
        lookup) runs on the normal worker and may raise at once. The waits
        that follow (P2-17 upgrade wait, P2-16 deferral of a starting
        service, probes and rate-limit pauses) run on one dedicated driver
        thread per request, so the job stays ``running`` without holding a
        normal worker. The driver settles the returned Pending; when the job
        ends otherwise (owner cancel, epoch, timeout) ``alive()`` turns false
        and the driver stops at its next ownership check. Ownership also
        requires the handle itself to be live (``pending.abandoned`` unset):
        op ids restart per executor, so after an epoch change a lookup by op
        alone could name another job (31a review).
        """
        pending = Pending(timeout=self.verify_budget_seconds())

        def owned() -> bool:
            if pending.abandoned.is_set():
                return False
            return alive is None or bool(alive())

        flight, wait_for_auto = self._admit(args, context, owned)

        def drive() -> None:
            try:
                pending.complete(self._drive(flight, wait_for_auto))
            except JobFailure as exc:
                pending.fail(exc)
            except Exception as exc:
                _LOG.warning("exit.verify driver raised %s (request %s)",
                             type(exc).__name__, flight.request_id)
                pending.fail(JobFailure("internal"))

        try:
            thread_factory(target=drive, name="bridge-exit-verify", daemon=True).start()
        except BaseException:
            with self._lock:
                if self._flight is flight:
                    self._flight = None
            raise
        return pending

    def _admit(self, args: Mapping[str, Any], context: Any,
               alive: Callable[[], bool] | None) -> tuple[_Flight, bool]:
        if not isinstance(args, Mapping) or args.get("trigger") != "manual":
            raise JobFailure("validation")
        flight = _Flight(uuid.uuid4().hex, context, alive, "manual")
        with self._lock:
            self._reclaim_unstarted_auto_locked(self._monotonic())
            old = self._flight
        # Outside the owner lock: ``alive()`` takes the executor lock.  A
        # flight whose owner is already revoked (cancel just landed, its worker
        # has not polled yet) is taken over instead of answering conflict.
        old_released = old is not None and self._ownership_lost(old)
        with self._lock:
            self._reclaim_unstarted_auto_locked(self._monotonic())
            taken = self._flight
            # T-P2-17: a manual request during an automatic lookup takes the
            # request over; the automatic one yields (its late result is
            # ignored) and the manual one runs as a formal request.  Decided
            # on the flight held NOW (SHOULD-2): an automatic lookup that
            # started between the two reads is upgraded, not a conflict.
            upgrade = taken is not None and taken.trigger == "auto"
            held = taken is not None and not upgrade and not (
                taken is old and old_released)
            # SHOULD-1: when every live probe is an orphaned AUTOMATIC one
            # (it timed out, the runtime has not returned yet), the click
            # waits for it like an upgrade instead of answering conflict;
            # nothing the user could see was running.
            over = self._live_probes > self.max_orphans
            auto_orphans_only = (not upgrade and over
                                 and self._live_probes == self._auto_live_probes)
            # Probe-level single flight: a revoked request's probe still runs
            # in the runtime until its own deadline; never start a second one.
            if held or (over and not upgrade and not auto_orphans_only):
                busy = closed_result(flight.request_id, 0, "failed", "in_progress")
                raise JobFailure("conflict", error=exit_error(busy))
            if upgrade:
                taken.context.owner_revoked.set()
            self._flight = flight
        wait_for_auto = upgrade or auto_orphans_only
        binding = self._binding()
        with self._lock:
            # A click lifts the per-(binding, core instance) back-off
            # (Qt desktop_app.py:17017); a no-op until bridge_exit_binding exists.
            self.scheduler.record_user_request(**binding)
        return flight, wait_for_auto

    def _drive(self, flight: _Flight, wait_for_auto: bool) -> dict[str, Any]:
        try:
            if wait_for_auto and not self._wait_probes_idle(flight):
                if not self._owned(flight):
                    return self._cancelled(flight)
                return self._finish(flight, closed_result(
                    flight.request_id, 0, "failed", "in_progress",
                    observed_at_ms=self._wall_ms()))
            return self._run(flight)
        finally:
            stranded = None
            with self._lock:
                if self._flight is flight and self._current.get("status") == "running":
                    # An unexpected exception must not leave progress running.
                    stranded = closed_result(flight.request_id, flight.generation, "failed",
                                             "lookup_unavailable", observed_at_ms=self._wall_ms())
                    self._install_locked(stranded)
                if self._flight is flight:
                    self._flight = None
            if stranded is not None:
                self._publish()

    def _wait_probes_idle(self, flight: _Flight) -> bool:
        """After taking over an automatic lookup, wait for its probe to return.

        Bounded by the automatic attempt's own deadline plus a margin; the
        runtime never runs two identity checks at once (M1).  Publishes the
        manual request as running (``upgrading``) meanwhile.
        """
        # SHOULD-8: the running result carries the Core generation, not 0.
        sample = self._sample()
        if sample is not None:
            flight.generation = sample.core_generation or 0
        self._set_running(flight, "upgrading")
        end = (self._monotonic() + self.attempt_seconds + self.grace_seconds
               + UPGRADE_WAIT_MARGIN_SECONDS)
        while True:
            if not self._owned(flight):
                return False
            with self._lock:
                if self._live_probes <= self.max_orphans:
                    return True
            if self._monotonic() >= end:
                return False
            time.sleep(self.poll_seconds)

    def _ownership_lost(self, flight: _Flight) -> bool:
        if _revoked(flight):
            return True
        if flight.alive is None:
            return False
        try:
            return not flight.alive()
        except Exception:
            return True

    def _finish(self, flight: _Flight, result: dict[str, Any],
                meta: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not self._owned(flight):
            # Owner revoked meanwhile: the late result is discarded; the job is
            # already terminal on the executor side.
            return self._cancelled(flight)
        # Passed on the flight: ``_commit`` keeps its (flight, result, probe)
        # call shape.
        flight.pending_meta = meta
        if not self._commit(flight, result):
            # Revoked between ``_owned`` and the write: still a late result.
            return self._cancelled(flight)
        if result_is_done(result):
            return result
        raise JobFailure(exit_failure_code(result.get("reason_code")), partial_result=result,
                         error=exit_error(result))

    def _cancelled(self, flight: _Flight) -> dict[str, Any]:
        result = closed_result(flight.request_id, flight.generation, "cancelled", "owner_revoked")
        with self._lock:
            owned = self._flight is flight
            if owned:
                self._install_locked(result)
        if owned:
            self._publish()
        return result

    def _superseded(self, flight: _Flight, reason: str) -> dict[str, Any]:
        if not self._owned(flight):
            return self._cancelled(flight)
        result = closed_result(flight.request_id, flight.generation, "superseded", reason)
        if not self._commit(flight, result):
            return self._cancelled(flight)
        superseded = getattr(flight.context, "superseded", None)
        if superseded is not None:
            superseded.set()
        raise JobFailure("conflict")

    def _set_running(self, flight: _Flight, phase: str, *, deferred: bool = False,
                     probe: _Probe | None = None) -> None:
        if self._commit(flight, running_result(flight.request_id, flight.generation, phase,
                                               deferred=deferred), probe):
            flight.phase = phase
            flight.deferred = deferred

    def _record_outcome(self, payload: Mapping[str, Any], *, auto: bool = False,
                        flight: _Flight | None = None) -> None:
        """Qt port of desktop_app.py:17130 ``_overview_record_user_outcome``
        (``auto=True``: 17105 ``_overview_record_auto_outcome``, which does not
        count as a user lookup).  Qt keys the outcome by the current status
        route; an automatic flight's decided binding stands in for it when the
        payload carries none (timeout / cancellation)."""
        usable, reason, retry_after = lookup_outcome(payload)
        route = _mapping(payload.get("route"))
        marker = str(route.get("binding_marker") or "")
        instance = str(route.get("core_instance") or "")
        if flight is not None and not marker:
            marker, instance = flight.binding_marker, flight.core_instance
        now = self._monotonic()
        with self._lock:
            if not auto:
                self.scheduler.record_user_lookup(now)
            self.scheduler.record_outcome(
                now=now, binding_marker=marker,
                core_instance=instance, success=usable,
                skipped_reason=reason, rate_limited=reason == "identity_rate_limited",
                retry_after=retry_after)

    def _run(self, flight: _Flight) -> dict[str, Any]:
        context = self._sample()
        if context is None:
            return self._finish(flight, closed_result(
                flight.request_id, 0, "failed", "lookup_unavailable",
                observed_at_ms=self._wall_ms()))
        flight.generation = context.core_generation or 0
        if context.availability == "offline":
            return self._finish(flight, closed_result(
                flight.request_id, flight.generation, "failed", "service_stopped",
                observed_at_ms=self._wall_ms()))
        deferral: Deferral | None = None
        trigger = "manual"
        if context.availability == "starting":
            # Not ready: do not execute, record as deferred (Qt 17168).
            deferral = arm_deferral(None, self._monotonic(), context.signature, periodic=True,
                                    max_wait=self.defer_max_wait)
            outcome = self._wait_deferral(flight, deferral)
            if outcome[0] != "resume":
                return self._deferral_end(flight, outcome[0])
            context, deferral = outcome[1], outcome[2]
            trigger = "deferred"
        rate_retries = 0
        lease: ExitLease | None = None
        while True:
            now = self._monotonic()
            if lease is None:
                deadline = now + self.attempt_seconds
                if deferral is not None:
                    deadline = min(deadline, deferral.deadline_monotonic)
                flight.generation = context.core_generation or 0
                lease = ExitLease(flight.request_id, deadline, trigger,
                                  context.lifecycle_revision, context.network_context_epoch,
                                  context.core_generation)
            flight.attempt += 1
            self._set_running(flight, "querying")
            outcome = self._probe(flight, lease, trigger)
            kind = outcome[0]
            if kind == "revoked":
                return self._cancelled(flight)
            if kind == "superseded":
                return self._superseded(flight, outcome[1])
            if kind == "timeout":
                timeout = {"request_id": flight.request_id, "state": "unavailable",
                           "error": "timeout",
                           "diagnostics": {"overview_failure_reason": "validation_timeout"}}
                self._record_outcome(timeout)
                return self._finish(flight, closed_result(
                    flight.request_id, flight.generation, "failed", "validation_timeout",
                    observed_at_ms=self._wall_ms()))
            payload = outcome[1]
            if payload is None or str(payload.get("request_id") or "") != flight.request_id:
                # A framework failure has no trustworthy request ID (Qt 33815).
                return self._finish(flight, closed_result(
                    flight.request_id, flight.generation, "failed", "lookup_unavailable",
                    observed_at_ms=self._wall_ms()))
            if not self._owned(flight):
                return self._cancelled(flight)
            current = self._sample()
            mismatch = lease_owner_mismatch(lease, current) if current is not None else None
            if mismatch:
                return self._superseded(flight, mismatch)
            if not completed_within_deadline(lease, payload):
                diagnostics = {key: value for key, value in _mapping(payload.get("diagnostics")).items()
                               if key != "overview_warning_reason"}
                payload = {**payload, "state": "unavailable", "identity": {},
                           "error": "validation_timeout",
                           "diagnostics": {**diagnostics,
                                           "overview_failure_reason": "validation_timeout"}}
            marker = str(_mapping(payload.get("route")).get("binding_marker") or "").strip()
            binding = self._binding()["binding_marker"]
            if marker and binding and binding != marker:
                return self._superseded(flight, "route_changed")
            if current is not None:
                defer, periodic = should_defer_result(
                    payload, trigger=flight.trigger, context=current,
                    deferred=deferral is not None)
                if defer:
                    deferral = arm_deferral(deferral, self._monotonic(), current.signature,
                                            periodic=periodic, max_wait=self.defer_max_wait)
                    outcome = self._wait_deferral(flight, deferral)
                    if outcome[0] != "resume":
                        return self._deferral_end(flight, outcome[0])
                    context, deferral = outcome[1], outcome[2]
                    trigger, lease = "deferred", None
                    continue
            usable, reason, retry_after = lookup_outcome(payload)
            if (not usable and reason == "identity_rate_limited"
                    and rate_retries < self.rate_limit_retries and retry_after is not None
                    and self._monotonic() + retry_after + self.rate_limit_min_attempt
                    <= lease.deadline_monotonic):
                # Opt-in only (Qt never retries).  Retry-After inside the SAME
                # original deadline; no new budget.  The 429 pause is recorded
                # first, so a successful retry never erases it.
                self._record_outcome(payload)
                rate_retries += 1
                self._set_running(flight, "rate_limited")
                if not self._sleep_owned(flight, retry_after):
                    return self._cancelled(flight)
                continue
            self._record_outcome(payload)
            return self._finish(flight, identity_result_from_payload(
                payload, request_id=flight.request_id, generation=flight.generation,
                fallback_ms=self._wall_ms()), _result_meta(payload, "manual"))

    def _deferral_end(self, flight: _Flight, action: str) -> dict[str, Any]:
        if action == "revoked":
            return self._cancelled(flight)
        reason = "service_stopped" if action == "offline" else "connection_not_ready"
        return self._finish(flight, closed_result(
            flight.request_id, flight.generation, "failed", reason,
            observed_at_ms=self._wall_ms()))

    # -- automatic lookup (normal domain, single flight with manual) ------------

    def _run_auto(self, flight: _Flight) -> None:
        """One automatic identity lookup on its driver thread (T-P2-31a).

        Qt port of desktop_app.py:17178 (trigger ``auto``) with the completion
        rules of 34473 / 16491 / 16530 / 33821: the expected binding marker is
        the one the decision was made for; nothing is published while it
        runs; a timeout, cancellation or framework failure keeps the card as
        it was; skipped outcomes never back off.
        """
        try:
            with self._lock:
                if self._flight is not flight or _revoked(flight):
                    return  # a manual request took over, or the start was reclaimed
                if flight.start_deadline is not None and \
                        self._monotonic() > flight.start_deadline:
                    # MUST-1: began after its start deadline (the decision is
                    # stale); give the slot back, the next sample decides anew.
                    flight.context.owner_revoked.set()
                    self._flight = None
                    return
                flight.started = True
            context = self._sample()
            if context is None or context.availability != "ready":
                self._record_outcome({"state": "unavailable", "error": "auto_not_ready",
                                      "diagnostics": {"overview_failure_reason": "auto_not_ready"}},
                                     auto=True, flight=flight)
                return
            flight.generation = context.core_generation or 0
            lease = ExitLease(flight.request_id, self._monotonic() + self.attempt_seconds, "auto",
                              context.lifecycle_revision, context.network_context_epoch,
                              context.core_generation)
            flight.attempt += 1
            outcome = self._probe(flight, lease, "auto", flight.binding_marker)
            kind = outcome[0]
            if kind == "revoked":
                return  # yielded to a manual request; its result is ignored
            if kind == "superseded":
                self._record_outcome({"state": "unavailable", "error": "validation_cancelled",
                                      "diagnostics": {"overview_failure_reason":
                                                      "validation_cancelled"}},
                                     auto=True, flight=flight)
                return
            if kind == "timeout":
                # Nobody asked: count the hang, keep the card (Qt 16491).
                self._record_outcome({"state": "unavailable", "error": "timeout",
                                      "diagnostics": {"overview_failure_reason":
                                                      "validation_timeout"}},
                                     auto=True, flight=flight)
                return
            payload = outcome[1]
            if payload is None or str(payload.get("request_id") or "") != flight.request_id:
                # Framework failure: no trustworthy request id (Qt 33821).
                _LOG.info("automatic exit identity lookup failed (request %s)",
                          flight.request_id)
                return
            if not self._owned(flight):
                return
            current = self._sample()
            if current is not None and lease_owner_mismatch(lease, current):
                self._record_outcome({"state": "unavailable", "error": "validation_cancelled",
                                      "diagnostics": {"overview_failure_reason":
                                                      "validation_cancelled"}},
                                     auto=True, flight=flight)
                return
            if not completed_within_deadline(lease, payload):
                payload = {"request_id": flight.request_id, "state": "unavailable",
                           "error": "timeout",
                           "diagnostics": {"overview_failure_reason": "validation_timeout"}}
            self._commit_auto(flight, payload)
        except Exception as exc:
            _LOG.warning("automatic exit identity lookup raised %s (request %s)",
                         type(exc).__name__, flight.request_id)
        finally:
            with self._lock:
                if self._flight is flight:
                    self._flight = None

    def _commit_auto(self, flight: _Flight, payload: Mapping[str, Any]) -> bool:
        """Qt port of desktop_app.py:17105 ``_overview_record_auto_outcome``.

        Records the outcome once; installs the result only when Qt would
        have shown it: a usable result, or a failure while the card holds no
        usable result for this binding.  Skipped reasons say nothing about
        the exit and keep the card.  Returns whether ``current`` changed.
        """
        usable, reason, _retry = lookup_outcome(payload)
        self._record_outcome(payload, auto=True, flight=flight)
        result = identity_result_from_payload(
            payload, request_id=flight.request_id, generation=flight.generation,
            fallback_ms=self._wall_ms())
        if not result_is_done(result):
            usable = False
        if not usable:
            if reason in AUTO_SKIPPED_REASONS:
                return False
            with self._lock:
                age, _refresh = self._current_result_age_locked(
                    flight.binding_marker, flight.core_instance, self._monotonic())
            if age is not None:
                return False  # a failed refresh never replaces a usable result
        meta = _result_meta(payload, "auto")
        if not meta["binding_marker"]:
            meta.update(binding_marker=flight.binding_marker, core_instance=flight.core_instance)
        return self._commit(flight, result, meta=meta)

    def _sleep_owned(self, flight: _Flight, seconds: float) -> bool:
        end = self._monotonic() + max(0.0, seconds)
        while True:
            if not self._owned(flight):
                return False
            remaining = end - self._monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(self.poll_seconds, remaining))

    def _wait_deferral(self, flight: _Flight, deferral: Deferral) -> tuple:
        """Wait for a starting service without holding any lock or the network."""
        self._set_running(flight, "deferred", deferred=True)
        last_sample = None
        while True:
            if not self._owned(flight):
                return ("revoked",)
            now = self._monotonic()
            if last_sample is None or self._wake.is_set() or \
                    now - last_sample >= self.lifecycle_poll_seconds:
                self._wake.clear()
                last_sample = now
                context = self._sample()
                if context is not None:
                    action, deferral = deferral_step(
                        deferral, now, context, retry_seconds=self.defer_retry_seconds,
                        max_attempts=self.defer_max_attempts)
                    if action == "resume":
                        return ("resume", context, deferral)
                    if action in {"offline", "expired"}:
                        return (action,)
                elif now >= deferral.deadline_monotonic:
                    return ("expired",)
            self._wake.wait(self.poll_seconds)

    def _probe(self, flight: _Flight, lease: ExitLease, trigger: str,
               expected_binding_marker: str = "") -> tuple:
        probe = _Probe()
        is_auto = flight.trigger == "auto"
        if not self._owned(flight):
            # Revoked before the runtime check started: never start one.
            return ("revoked",)
        try:
            proxy_url = self.port.proxy_url()
        except Exception:
            proxy_url = None

        def progress(update: Any) -> None:
            phase = _token(_mapping(update).get("phase"))
            if flight.trigger == "auto":
                # An automatic lookup publishes nothing while it runs.
                return
            if phase and phase != flight.phase and self._owned(flight):
                self._set_running(flight, phase, probe=probe)

        def call() -> None:
            payload = None
            failed = False
            try:
                payload = self.port.check_current_exit_identity(
                    proxy_url=proxy_url, request_id=flight.request_id,
                    timeout_seconds=max(0.0, lease.deadline_monotonic - self._monotonic()),
                    deadline_monotonic=lease.deadline_monotonic,
                    progress_callback=progress, trigger=trigger,
                    expected_binding_marker=expected_binding_marker)
            except Exception as exc:
                failed = True
                # Sanitized: type name and request id only, never the message.
                _LOG.warning("exit.verify probe raised %s (request %s)",
                             type(exc).__name__, flight.request_id)
            with self._lock:
                probe.payload = dict(payload) if isinstance(payload, Mapping) else None
                probe.failed = failed or probe.payload is None
                probe.done.set()
                self._live_probes -= 1
                if is_auto:
                    self._auto_live_probes -= 1
                if probe.orphaned:
                    self._orphans -= 1

        def orphan() -> None:
            with self._lock:
                if not probe.done.is_set() and not probe.orphaned:
                    probe.orphaned = True
                    self._orphans += 1

        with self._lock:
            # Atomic with ``verify``'s take-over check: a request that took
            # this flight over saw ``_live_probes`` before this increment.
            if self._flight is not flight or _revoked(flight):
                return ("revoked",)
            self._live_probes += 1
            if is_auto:
                self._auto_live_probes += 1
        try:
            self._thread_factory(target=call, name="bridge-exit-probe", daemon=True).start()
        except BaseException:
            with self._lock:
                self._live_probes -= 1
                if is_auto:
                    self._auto_live_probes -= 1
            raise
        last_sample = self._monotonic()
        while not probe.done.wait(self.poll_seconds):
            if not self._owned(flight):
                orphan()
                return ("revoked",)
            now = self._monotonic()
            if not lease_is_active(lease, now, self.grace_seconds):
                orphan()
                return ("timeout",)
            if now - last_sample >= self.lifecycle_poll_seconds:
                last_sample = now
                context = self._sample()
                mismatch = lease_owner_mismatch(lease, context) if context is not None else None
                if mismatch:
                    orphan()
                    return ("superseded", mismatch)
        return ("done", None if probe.failed else probe.payload)


# --------------------------------------------------------------------------
# Wiring (called by the integration ticket, not by bridge_main here)
# --------------------------------------------------------------------------

def register(session: Any, *, port: ExitRuntimePort | None = None,
             owner: ExitIdentityOwner | None = None,
             presence: bool = True,
             owner_options: Mapping[str, Any] | None = None) -> ExitIdentityOwner:
    """Register ``exit.verify`` (+ ``ui.presence``) and seed ``exit.identity``.

    ``session`` needs ``executor`` (JobExecutor), ``state`` (PublicationState)
    and, without an explicit port, ``runtime``.  Topic edges go through the
    existing ``state.record_edge``; the state assigns rev/seq and turns ring
    overflow into ``gap``.  On a new epoch the integration re-seeds with
    ``session.state.record_state(TOPIC, owner.snapshot())``.  A failed edge
    withdraws the baseline (``invalidate_state(notify=True)`` -> gap); the
    next change publishes a complete DTO again.  The sampler should call
    ``owner.notify_lifecycle(sample)`` with every lifecycle sample so a fresh
    result whose Core generation changed is republished as not fresh.

    T-P2-17 automatic lookups: the owner hands a start to a dedicated
    daemon driver thread (T-P2-31a; single flight, so at most one).
    Inputs: ``ui.presence`` is registered here (sampling domain, shell
    only) unless ``presence=False``; the sampler's
    ``notify_lifecycle(sample)`` (about 1 Hz while running) re-evaluates the
    settle/spacing windows; the integration calls
    ``owner.observe_route(route_published_dto)`` whenever the
    ``route.published`` publication revision changes.  ``owner_options`` are
    extra ``ExitIdentityOwner`` keyword arguments (e.g. an injected clock)
    used only when this function builds the owner.
    """
    if owner is None:
        def publish(dto: dict[str, Any]) -> None:
            session.state.record_edge(TOPIC, dto)

        def invalidate() -> None:
            session.state.invalidate_state(TOPIC, notify=True)

        def auto_submit(work: Callable[[], Any]) -> bool:
            # T-P2-31a: an automatic lookup (single flight) runs on its own
            # driver thread, never on one of the two normal workers. Like the
            # former submit_normal it is refused once the executor stops
            # admitting (shutdown, epoch end, app.quit): an automatic lookup
            # issues GET /proxies (route-side effects, P2-17 decision 5).
            executor = getattr(session, "executor", None)
            if not bool(getattr(executor, "accepting", False)):
                return False
            threading.Thread(target=work, name="bridge-exit-auto", daemon=True).start()
            return True
        owner = ExitIdentityOwner(port if port is not None else RuntimeExitPort(session.runtime),
                                  publish=publish, invalidate=invalidate,
                                  auto_submit=auto_submit, **dict(owner_options or {}))

    def handler(args: dict[str, Any], context: Any) -> dict[str, Any]:
        executor = session.executor

        def alive() -> bool:
            job = executor.get_job(context.op)
            return bool(job and job.get("name") == COMMAND and job.get("state") == "running")

        # T-P2-31a: deferred completion; no normal worker is held while the
        # request waits (deferral, upgrade wait, probes).
        return owner.verify_deferred(args, context, alive=alive)

    session.executor.register(COMMAND, handler)
    if presence:
        session.executor.register(
            PRESENCE_COMMAND, lambda args, _context: owner.observe_presence(args))
    session.state.record_state(TOPIC, owner.snapshot())
    hook = getattr(session, "register_owner_revoke_hook", None)
    if callable(hook):
        hook(owner.wake)
    return owner
