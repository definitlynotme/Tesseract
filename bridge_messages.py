"""Pure startup failure projection, governed solely by PROTOCOL section 14.

Raw exceptions stay local. Every string sent to the UI comes from these tables.
"""
from __future__ import annotations

from collections.abc import Mapping
import ipaddress
import re


_REASONS = {
    "runtime_listener_conflict": ("runtime_listener_conflict", "listener_conflict"),
    "runtime_listener_unavailable": ("runtime_listener_unavailable", "listener_unavailable"),
    "source_supply_no_egress": ("source_supply_no_egress", "source_supply_no_egress"),
    "no_sources": ("no_sources", "no_sources"),
    "no_validated_nodes": ("needs_route", "no_validated_nodes"),
    "insufficient_validated_nodes": ("needs_route", "insufficient_validated_nodes"),
    "core_missing": ("io", "core_missing"),
    "core_start_timeout": ("timeout", "core_start_timeout"),
    "core_not_running": ("not_running", "core_not_running"),
    "route_verification_failed": ("needs_route", "route_verification_failed"),
    "fallback_warmup_failed": ("needs_route", "fallback_warmup_failed"),
    "geodata_missing": ("validation", "geodata_missing"),
    "geodata_needs_route": ("needs_route", "geodata_needs_route"),
    "geodata_download_failed": ("io", "geodata_download_failed"),
    "admin_required": ("admin_required", "admin_required"),
    "cleanup_pending": ("lifecycle_busy", "cleanup_pending"),
    "superseded": ("superseded", "superseded"),
    "source_supply_terminal.rule_blocked": ("validation", "source_supply_terminal"),
    "source_supply_terminal.no_proxy": ("needs_route", "source_supply_terminal"),
    "source_supply_terminal.transport_failed": ("io", "source_supply_terminal"),
}
_TYPES = {
    "RuntimeListenerPreflightError": "runtime_listener_conflict",
    "RuntimeListenerSetupError": "runtime_listener_unavailable",
    "RuntimeStartCancelled": "superseded",
    "TimeoutError": "core_start_timeout",
}
_KINDS = {"mixed", "http", "socks", "controller", "subscription", "dns"}
START_FAILURE_CODES = frozenset(code for code, _key in _REASONS.values()) | {"internal"}
_LEGACY_CODES = START_FAILURE_CODES | {"conflict", "cancelled"}


def _mapping(value):
    return value if isinstance(value, Mapping) else {}


def _reason(facts):
    reason = facts.get("reason_code") or facts.get("reason") or facts.get("code")
    if reason == "source_supply_terminal":
        supply = _mapping(facts.get("source_supply"))
        child = (facts.get("subreason") or facts.get("source_supply_terminal_reason")
                 or supply.get("reason_code") or supply.get("reason"))
        if child not in ("rule_blocked", "no_proxy", "transport_failed", "no_egress"):
            # These are the runtime terminal recorder's authoritative fallbacks.
            blocked = facts.get("source_retry_blocked_reason")
            if blocked in ("prerequisite_missing", "rule_blocked", "runtime_source_retry_disabled"):
                child = "rule_blocked"
            elif blocked in ("blocked_no_route", "runtime_proxy_unavailable_after_recovery"):
                child = "no_proxy"
            elif any(type(facts.get(key)) is int and facts[key] > 0 for key in
                     ("source_supply_blocked_count", "failed_sources", "source_fetch_failed")):
                child = "transport_failed"
        reason = "source_supply_no_egress" if child == "no_egress" else (
            "source_supply_terminal." + child if isinstance(child, str) else "")
    if reason == "shutdown_cleanup_pending":
        reason = "cleanup_pending"
    return reason if isinstance(reason, str) and reason in _REASONS else None


def _compatibility(text):
    # Narrow legacy producers, never generic 'timeout', 'permission' or nodes=0.
    if text == "no_sources":
        return "no_sources", {}
    for reason in ("runtime_listener_conflict", "runtime_listener_unavailable", "source_supply_no_egress"):
        if text.startswith(reason + ":"):
            return reason, {}
    if text.startswith("Fallback has no validated proxy nodes; startup remains fail-closed"):
        return "no_validated_nodes", {}
    match = re.match(r"^Fallback has only (\d{1,9})/(\d{1,9}) validated proxy nodes; startup remains fail-closed", text)
    if match:
        return "insufficient_validated_nodes", dict(zip(("healthy", "required"), map(int, match.groups())))
    prefixes = {
        "mihomo binary not found: ": "core_missing",
        "mihomo core is not running": "core_not_running",
        "Mihomo bootstrap core is not available for promotion": "core_not_running",
        "Could not verify fallback after core launch": "route_verification_failed",
        "Could not verify Bastion Layer after core launch": "route_verification_failed",
        "Could not pre-warm fallback": "fallback_warmup_failed",
        "Could not pre-warm Bastion Layer": "fallback_warmup_failed",
        "Required GeoData is missing and the GeoData updater is disabled: ": "geodata_missing",
        "Required GeoData is missing before Mihomo startup; preflight download requires an explicit egress proxy: ": "geodata_needs_route",
        "GeoData preflight through the explicit egress proxy failed: ": "geodata_download_failed",
        "Required GeoData is still missing after explicit-proxy preflight: ": "geodata_download_failed",
    }
    for prefix, reason in prefixes.items():
        if text.startswith(prefix):
            return reason, {}
    if text == "TUN mode requires running Tesseract as Administrator (WinTun driver)":
        return "admin_required", {}
    return None, {}


def _params(reason, facts):
    source = {**facts, **_mapping(facts.get("params"))}
    params = {}
    if reason == "insufficient_validated_nodes":
        for name in ("healthy", "required"):
            value = source.get(name)
            if type(value) is int and value >= 0:
                params[name] = value
    elif reason in {"runtime_listener_conflict", "runtime_listener_unavailable"}:
        # No params.field (决定_第一波裁定2 T-P2-12 item 2): the focusable
        # form field is Error.field, set by listener_error from page IDs.
        kind, port = source.get("kind"), source.get("port")
        if isinstance(kind, str) and kind in _KINDS:
            params["kind"] = kind
        if type(port) is int and 1 <= port <= 65535:
            params["port"] = port
    elif reason == "cleanup_pending":
        params["cleanup_pending"] = True
    return params


def classify_start_failure(exception=None, diagnostic=None, lifecycle=None):
    """Return a fresh safe Error DTO; no Qt, transport, I/O or runtime reads."""
    facts = _mapping(diagnostic)
    exc_facts = _mapping(getattr(exception, "diagnostic", None))
    # A selected source owns the entire reason and its parameters. In particular,
    # an old runtime reason_code must never override an exception's code/type.
    typed = dict(exc_facts)
    conflicts = getattr(exception, "conflicts", ())
    if conflicts:
        kind, _transport, _host, port = conflicts[0]
        typed["params"] = {"kind": kind, "port": port}
    result = getattr(exception, "result", None)
    failures = getattr(result, "failures", ())
    if failures and not conflicts:
        spec = failures[0].spec
        typed["params"] = {"kind": spec.kind, "port": spec.port}
    exception_type = type(exception).__name__ if exception is not None else None
    reason = _reason(typed) or (_TYPES.get(exception_type) if isinstance(exception_type, str) else None)
    chosen = typed
    exception_evidence = reason is not None
    local = _mapping(lifecycle)
    runtime = _mapping(local.get("runtime", local))
    if reason is None:
        for source in (facts, runtime):
            source_type = source.get("exception_type")
            reason = _reason(source) or (_TYPES.get(source_type) if isinstance(source_type, str) else None)
            if reason is not None:
                chosen = source
                break
    if reason is None and (runtime.get("shutdown_cleanup_pending") is True
                           or runtime.get("start_blocked_by_cleanup") is True):
        reason = "cleanup_pending"
        chosen = runtime
    if reason is None and _mapping(local.get("intent")).get("stop_requested") is True:
        reason = "superseded"
    if reason is None:
        authority = runtime.get("failure_code")
        reason = authority if isinstance(authority, str) and authority in _REASONS else None
        if reason is not None:
            chosen = runtime
        if reason is None:
            authority = facts.get("code") or authority
            if isinstance(authority, str) and authority in _LEGACY_CODES - {"internal"}:
                return dict(code=authority, message_key="errors.start.unexpected", params={}, reason_code="unexpected")
    if reason is None:
        text = str(exception) if exception is not None else (
            facts.get("text") or runtime.get("startup_failure_reason") or runtime.get("last_error") or "")
        reason, chosen = _compatibility(text if isinstance(text, str) else "")
        exception_evidence = exception is not None
        if reason is None and isinstance(text, str) and text in _REASONS:
            reason = text
        if reason is None and isinstance(text, str) and text in _LEGACY_CODES:
            return dict(code=text, message_key="errors.start.unexpected", params={}, reason_code="unexpected")
    if reason is None and isinstance(getattr(exception, "__cause__", None), TimeoutError):
        reason = "core_start_timeout"
    if reason is None:
        return dict(code="internal", message_key="errors.start.unexpected", params={}, reason_code="unexpected")
    code, key = _REASONS[reason]
    # Timeout applies only to explicit timeout evidence within this failure stage.
    if reason in {"geodata_download_failed", "source_supply_terminal.transport_failed"}:
        cause = getattr(exception, "__cause__", None)
        if (chosen.get("timed_out") is True or chosen.get("cause_type") == "TimeoutError"
                or chosen.get("code") == "timeout"
                or (exception_evidence and isinstance(cause, TimeoutError))):
            code = "timeout"
    return dict(code=code, message_key="errors.start." + key,
                params=_params(reason, chosen), reason_code=reason)


def startup_diagnostic(exception=None, *, diagnostic=None, lifecycle=None):
    """Own bounded diagnostic facts at a runtime failure recording point.

    It retains type/known fields locally, but no path, traceback or arbitrary text.
    """
    dto = classify_start_failure(exception, diagnostic, lifecycle)
    record = {**dto, "exception_type": type(exception).__name__ if exception else None}
    # B-26(3): keep every whitelisted endpoint locally so a later Error built
    # from this record still has all conflicts. Never read by the classifier.
    if dto["reason_code"] in _LISTENER_REASONS:
        key = "conflicts" if dto["reason_code"] == _LISTENER_REASONS[0] else "unavailable"
        entries = listener_endpoints(*_listener_owner(exception, diagnostic, lifecycle,
                                                      dto["reason_code"]), key)
        if entries:
            record["listener_" + key] = entries
    return record


# ---------------------------------------------------------------------------
# B-26(3) listener field errors and save-failure semantics (T-P2-12).
#
# Complete kind set actually produced by the runtime listener preflight
# (DesktopRuntime._runtime_listener_preflight_specs): mixed, controller, dns.
# Field ids are focusable page form fields, the same "<section>.<field>" ids
# bridge_validation reports. Controller port is read-only on the page and DNS
# listen is not page-editable, so they have no field. A kind missing from this
# table is unknown and never gets a guessed field.
LISTENER_KIND_FIELDS = {
    "mixed": "network.mihomo_mixed_port",
    "controller": None,
    "dns": None,
}
_LISTENER_REASONS = ("runtime_listener_conflict", "runtime_listener_unavailable")
_LISTENER_MAX_ENTRIES = 16


def _listener_entry(item):
    """Whitelist one endpoint to {kind, transport?, host?, port}.

    Same rule as the runtime's _diagnostic_endpoints: a present transport other
    than tcp/udp drops the whole entry, a non-IP host drops only the host key.
    transport may be absent only in an exception-free P2-05 legacy record,
    which carries just the first {kind, port}.
    """
    if isinstance(item, Mapping):
        kind, transport, host, port = (item.get(k) for k in ("kind", "transport", "host", "port"))
    elif isinstance(item, (tuple, list)) and len(item) == 4:
        kind, transport, host, port = item
    else:
        return None
    if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z_]{0,31}", kind):
        return None
    if type(port) is not int or not 1 <= port <= 65535:
        return None
    if transport is not None and transport not in ("tcp", "udp"):
        return None
    entry = {"kind": kind}
    if transport is not None:
        entry["transport"] = transport
    if isinstance(host, str):
        text = host.strip().strip("[]").split("%", 1)[0]
        if text.casefold() == "localhost":
            entry["host"] = "localhost"
        else:
            try:
                entry["host"] = str(ipaddress.ip_address(text))
            except ValueError:
                pass
    entry["port"] = port
    return entry


def _listener_entries(items):
    entries = []
    if isinstance(items, (str, bytes, Mapping)) or not isinstance(items, (tuple, list)):
        return entries
    for item in items[:64]:
        entry = _listener_entry(item)
        if entry is not None and entry not in entries:
            entries.append(entry)
        if len(entries) >= _LISTENER_MAX_ENTRIES:
            break
    return entries


def _typed_exception_reason(exception):
    """The reason an exception proves by type or typed diagnostic, never text."""
    exception_type = type(exception).__name__
    return (_reason(_mapping(getattr(exception, "diagnostic", None)))
            or _TYPES.get(exception_type))


def _listener_owner(exception, diagnostic, lifecycle, reason):
    """The one source that owns ``reason``; others are never mixed in.

    Mirrors classify_start_failure's precedence: an exception with typed
    evidence (its type or typed diagnostic), then the supplied diagnostic, then
    the lifecycle runtime record: its raw ``failure_diagnostic`` (all
    conflicts), else the ``failure`` DTO that service_state_data leaves on the
    bridge side (first kind/port only), else the record itself. An exception
    known only by its compatibility text prefix ranks last, as in the
    classifier, so it never hides a typed diagnostic.
    """
    if exception is not None and _typed_exception_reason(exception) == reason:
        return exception, None
    if diagnostic is not None and classify_start_failure(diagnostic=diagnostic)["reason_code"] == reason:
        return None, diagnostic
    local = _mapping(lifecycle)
    runtime = _mapping(local.get("runtime", local))
    for name in ("failure_diagnostic", "failure"):
        nested = _mapping(runtime.get(name))
        if nested and classify_start_failure(diagnostic=nested)["reason_code"] == reason:
            return None, nested
    runtime_type = runtime.get("exception_type")
    runtime_reason = (_reason(runtime) or (_TYPES.get(runtime_type) if isinstance(runtime_type, str) else None)
                      or runtime.get("failure_code"))
    if exception is not None and runtime_reason != reason:
        return exception, None
    return None, runtime


def _owner_params(exception, diagnostic):
    """The owner's own params mapping (P2-05 legacy {kind, port} shape)."""
    facts = _mapping(getattr(exception, "diagnostic", None)) if exception is not None else _mapping(diagnostic)
    return _mapping(facts.get("params"))


def listener_endpoints(exception=None, diagnostic=None, key="conflicts"):
    """Whitelisted endpoints for ``key`` ("conflicts" or "unavailable").

    With an exception only its own evidence is read: typed data first, then
    its diagnostic params. Without one, the supplied exception-free record.
    Free text is never read.
    """
    if exception is not None:
        if key == "conflicts":
            typed = getattr(exception, "conflicts", None)
        else:
            failures = getattr(getattr(exception, "result", None), "failures", None) or ()
            typed = [tuple(getattr(getattr(f, "spec", None), n, None)
                           for n in ("kind", "transport", "host", "port")) for f in failures]
        entries = _listener_entries(list(typed) if isinstance(typed, (tuple, list)) else [])
        if entries:
            return entries
        facts = _mapping(getattr(exception, "diagnostic", None))
    else:
        facts = _mapping(diagnostic)
    return (_listener_entries(_mapping(facts.get("params")).get(key))
            or _listener_entries(facts.get("listener_" + key)))


def _wire_entry(entry):
    """Wire copy: keep host only for loopback, unspecified or localhost.

    Local diagnostics keep the full whitelisted IP; a LAN or public address
    never reaches Error.params (PROTOCOL section 7: redacted values only).
    """
    host = entry.get("host")
    if host is None or host == "localhost":
        return dict(entry)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (address.is_loopback or address.is_unspecified):
        return dict(entry)
    return {k: v for k, v in entry.items() if k != "host"}


def listener_field(entries):
    """First registered page field among the endpoints' kinds, else None."""
    for entry in entries:
        field = LISTENER_KIND_FIELDS.get(entry.get("kind"))
        if field:
            return field
    return None


def listener_error(exception=None, diagnostic=None, lifecycle=None, *, written):
    """Error DTO for a listener failure during save/apply, or None if not one.

    code/message_key/reason_code are exactly classify_start_failure's, so job,
    service.state and settings agree. params is the B-26(3) whitelist: a
    conflict carries {conflicts:[...]}; a setup failure (not proven occupancy)
    carries no conflicts. ``written`` is a required bool: False (validation or
    preflight failed before any write) adds hold=true; True means the config
    was written and is kept as written-not-applied, never rolled back here.

    Pass the exception whenever it is at hand: only the exception or the raw
    runtime record carries every conflict. The bridge-side lifecycle (after
    service_state_data) keeps just the P2-05 DTO, i.e. the first kind/port.
    """
    if type(written) is not bool:
        raise TypeError("written must be a bool")
    base = classify_start_failure(exception, diagnostic, lifecycle)
    reason = base["reason_code"]
    if reason not in _LISTENER_REASONS:
        return None
    key = "conflicts" if reason == "runtime_listener_conflict" else "unavailable"
    owner = _listener_owner(exception, diagnostic, lifecycle, reason)
    entries = listener_endpoints(*owner, key)
    if not entries:
        # P2-05 legacy shape (e.g. the bridge-side runtime["failure"] DTO):
        # only the owner's own first kind/port survive.
        legacy = _listener_entry(_owner_params(*owner))
        entries = [legacy] if legacy is not None else []
    error = dict(code=base["code"], message_key=base["message_key"],
                 params={"conflicts": [_wire_entry(e) for e in entries]}
                 if key == "conflicts" and entries else {},
                 reason_code=reason)
    field = listener_field(entries)
    if field is not None:
        error["field"] = field
    if not written:
        error["hold"] = True
    return error


def listener_section_result(section, effect, exception=None, diagnostic=None, lifecycle=None, *,
                            written):
    """SectionResult for a section whose apply failed on a listener.

    Not written: saved=false and error.hold=true. Written: saved=true,
    applied=false, the saved file is kept (no automatic multi-file rollback).
    Returns None when the failure is not a listener failure.
    """
    error = listener_error(exception, diagnostic, lifecycle, written=written)
    if error is None:
        return None
    return {"section": section, "saved": bool(written), "applied": False,
            "effect": effect, "error": error}
