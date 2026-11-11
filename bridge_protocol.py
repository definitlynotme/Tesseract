"""P1 sidecar/shell wire contract: bounded NDJSON and explicit DTO validation.

This module has no runtime, Qt, transport, or third-party dependencies.  It
validates a wire shape; ownership and deadline decisions belong to the runtime.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from collections import deque
from pathlib import PureWindowsPath
from typing import Any, Callable
from urllib.parse import unquote
from urllib.parse import urlsplit


PROTOCOL_VERSION = 1
HARD_FRAME_LIMIT = 4 * 1024 * 1024
SEND_BUDGET = 256 * 1024

READINESS_STATES = frozenset(
    {
        "serving",
        "ready",
        "activation_hold",
        "data_plane_failed",
        "controller_hold",
        "facility_hold",
        "fail_closed",
    }
)
RECOVERY_STATES = frozenset(
    {"not_needed", "restored", "not_owned", "permission_required", "failed", "unknown"}
)
JOB_STATES = frozenset(
    {"queued", "running", "done", "failed", "cancelled", "superseded"}
)
P1_COMMANDS = frozenset(
    {"service.start", "service.stop", "service.restart", "app.quit"}
)
P1_TOPICS = frozenset(
    {
        "service.state",
        "route.published",
        "tiers.summary",
        "traffic.tick",
        "traffic.history",
        "logs.batch",
        "core.info",
        "jobs",
    }
)
P1_SNAPSHOT_TOPICS = P1_TOPICS - {"logs.batch", "jobs"}
TOPIC_KINDS = {
    "service.state": frozenset({"snapshot", "edge", "gap"}),
    "route.published": frozenset({"snapshot", "edge", "gap"}),
    "tiers.summary": frozenset({"snapshot", "edge", "gap"}),
    "traffic.tick": frozenset({"snapshot"}),
    "traffic.history": frozenset({"snapshot"}),
    "logs.batch": frozenset({"edge", "gap"}),
    "core.info": frozenset({"snapshot", "edge", "gap"}),
    "jobs": frozenset({"edge"}),
}
ERROR_CODES = frozenset(
    {
        "invalid_utf8", "invalid_json", "not_object", "frame_too_large",
        "missing_field", "invalid_type", "invalid_enum", "proto_mismatch",
        "epoch_mismatch", "snapshot_required", "sequence_gap", "not_allowed",
        "runtime_unavailable", "runtime_gone", "job_capacity", "internal",
        "admin_required", "lifecycle_busy", "validation", "conflict",
        "not_running", "needs_route", "timeout", "superseded", "cancelled",
        "io", "runtime_listener_conflict", "runtime_listener_unavailable",
        "source_supply_no_egress", "no_sources",
    }
)
_TIERS = frozenset({"vanguard", "sentinel", "bastion", "gateway"})
_CREDENTIAL_KEY_TERMS = frozenset(
    {
        "password", "passwd", "pwd", "secret", "token", "apikey", "key",
        "authorization", "cookie", "session", "credential", "credentials",
    }
)
_NON_CREDENTIAL_KEY_SUFFIXES = frozenset(
    {"count", "len", "length", "total", "size", "ms", "s", "id"}
)
_FORBIDDEN_WIRE_KEY_SEGMENTS = frozenset(
    {
        ("monotonic",), ("deadline",), ("subscription", "url"),
        ("subscriptionurl",), ("uri",), ("uuid",), ("url",),
    }
)
_JSON_BACKSLASH_ESCAPE = re.compile(
    r'\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])'
)
_JSON_SIMPLE_ESCAPES = {
    '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
    "n": "\n", "r": "\r", "t": "\t",
}
_TEXT_KEY_ASSIGNMENT = re.compile(
    r'"(?P<double_key>[^"\r\n]*)"\s*[:=]|'
    r"'(?P<single_key>[^'\r\n]*)'\s*[:=]|"
    r'(?<![a-z0-9])(?P<bare_key>[a-z0-9]+'
    r'(?:[^a-z0-9"\'=:\r\n]+[a-z0-9]+)*'
    r'[^a-z0-9"\'=:\r\n]*)'
    r'(?:\s*["\'])?\s*[:=]'
)
_JSON_TEXT_KEY = re.compile(
    r'"(?P<key>(?:\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])|[^"\\\r\n])*)"\s*[:=]'
)
_QUERY_TEXT_KEY = re.compile(r"[?&](?P<key>[^=&\s#]+)=")
_BARE_EQUALS_KEY = re.compile(
    r"(?<![a-z0-9])(?P<key>[a-z0-9][^=\r\n]*?)\s*="
)
_SENSITIVE_TEXT_VALUE = re.compile(
    r"(?<![a-z0-9])[a-z][a-z0-9+.-]{0,63}://\S+(?![a-z0-9])|"
    r"(?<![a-z0-9])bearer\s+[a-z0-9._~+/=-]+(?![a-z0-9])|"
    r"(?<![a-z0-9])-----begin\s+"
    r"(?:(?:rsa|ec|dsa|openssh|encrypted)\s+)?private key-----(?![a-z0-9])"
)
_MESSAGE_KEY = re.compile(r"^errors(?:\.[a-z][a-z0-9_]*)+$")
_FEATURE_NAME = re.compile(r"B-[0-9]{1,3}[a-z]?")  # = shell hgate.rs feature_name
# Bounded in-memory diagnostics contain fixed codes/surface labels only.
# P2/P3 own persistence and diagnostic export; never retain text or frames here.
privacy_diagnostics = deque(maxlen=64)
_TEXT_SCAN_HINT = re.compile(
    r"[\\%]|://|password|passwd|pwd|secret|token|apikey|key|authorization|"
    r"cookie|session|credential|monotonic|deadline|subscription|uri|uuid|bearer|-----begin")


# Exact, immutable schema names and enums need no repeated text regex work.
_SAFE_WIRE_NAMES = frozenset(("t", "epoch", "produced_at_ms", "sent_at_ms", "topic", "rev",
    "kind", "data", "records", "seq", "ts_ms", "rx_ms", "source", "level", "category",
    "title", "detail", "raw", "from_seq", "dropped", "dropped_total", "gap", "tick_seq"))
_SAFE_WIRE_TEXT = frozenset(("", "evt", "batch", "edge", "gap", "snapshot", "logs.batch",
    "MIHOMO", "TESSERACT", "ALERT", "UNKNOWN", "debug", "info", "warn", "error", "critical",
    "runtime", "routing", "health", "circuit_breaker", "update", "configuration",
    "connection", "alert", "other"))


def _decode_json_backslash_escape(match: re.Match[str]) -> str:
    escape = match.group(0)[1:]
    if escape.startswith("u"):
        return chr(int(escape[1:], 16))
    return _JSON_SIMPLE_ESCAPES[escape]


def _fully_decode_json_backslash_escapes(value: str) -> tuple[str, bool]:
    current = value
    for _ in range(32):
        decoded = _JSON_BACKSLASH_ESCAPE.sub(
            _decode_json_backslash_escape, current
        )
        if decoded == current:
            return current, False
        current = decoded
    return current, bool(_JSON_BACKSLASH_ESCAPE.search(current))


def _is_sensitive_key(value: str) -> bool:
    if value in _SAFE_WIRE_NAMES:
        return False
    current, decoding_incomplete = _fully_decode_json_backslash_escapes(value)
    if decoding_incomplete:
        return True
    normalized = current.lower()
    if _SENSITIVE_TEXT_VALUE.search(normalized):
        return True
    segments = tuple(re.findall(r"[a-z0-9]+", normalized))
    return bool(
        segments
        and (any(part in {"monotonic", "deadline"} for part in segments)
             or segments[-1] not in _NON_CREDENTIAL_KEY_SUFFIXES
             and (segments[-1] in _CREDENTIAL_KEY_TERMS
                  or segments in _FORBIDDEN_WIRE_KEY_SEGMENTS))
    )


def _json_value_contains_sensitive(value: Any, depth: int) -> bool:
    if depth > 64:
        return True
    if isinstance(value, dict):
        return any(
            not isinstance(key, str)
            or _is_sensitive_key(key)
            or _json_value_contains_sensitive(child, depth + 1)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(
            _json_value_contains_sensitive(child, depth + 1)
            for child in value
        )
    if isinstance(value, str):
        return _contains_sensitive_text(value, _depth=depth + 1)
    return False


def _contains_sensitive_text(value: str, *, _depth: int = 0) -> bool:
    if _depth > 64:
        return True
    if value in _SAFE_WIRE_TEXT or not _TEXT_SCAN_HINT.search(value.lower()):
        return False
    current, decoding_incomplete = _fully_decode_json_backslash_escapes(value)
    if decoding_incomplete:
        return True
    try:
        parsed = json.loads(value) if value.lstrip().startswith(("{", "[")) or '\":' in value else None
    except (json.JSONDecodeError, ValueError, RecursionError):
        pass
    else:
        if parsed is not None:
            return _json_value_contains_sensitive(parsed, _depth + 1)
    try:
        parsed = json.loads(current) if current.lstrip().startswith(("{", "[")) or '\":' in current else None
    except (json.JSONDecodeError, ValueError, RecursionError):
        pass
    else:
        if parsed is not None:
            return _json_value_contains_sensitive(parsed, _depth + 1)

    # The whole string has already been decoded above.  Use the original text
    # only to retain unambiguous quoted-key boundaries that decoding an escaped
    # quote or control character would otherwise destroy.  Each extracted key
    # still goes through the same bounded, multilayer key classifier before a
    # known-safe quoted assignment is excluded from the remaining text scan.
    masked_original = list(value)
    quoted_assignment_found = False
    for match in _JSON_TEXT_KEY.finditer(value):
        try:
            key = json.loads(f'"{match.group("key")}"')
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(key, str):
            if _is_sensitive_key(key):
                return True
            quoted_assignment_found = True
            masked_original[match.start():match.end()] = " " * (
                match.end() - match.start()
            )
    if quoted_assignment_found:
        current, decoding_incomplete = _fully_decode_json_backslash_escapes(
            "".join(masked_original)
        )
        if decoding_incomplete:
            return True
    if _SENSITIVE_TEXT_VALUE.search(current.lower()):
        return True

    masked = list(current)
    for match in _JSON_TEXT_KEY.finditer(current):
        try:
            key = json.loads(f'"{match.group("key")}"')
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(key, str):
            if _is_sensitive_key(key):
                return True
            masked[match.start():match.end()] = " " * (match.end() - match.start())
    scan_text = "".join(masked)

    masked = list(scan_text)
    for match in _QUERY_TEXT_KEY.finditer(scan_text):
        key = unquote(match.group("key"), encoding="utf-8", errors="replace")
        if _is_sensitive_key(key):
            return True
        start = match.start("key")
        masked[start:match.end()] = " " * (match.end() - start)
    scan_text = "".join(masked)

    normalized = scan_text.lower()
    for match in _TEXT_KEY_ASSIGNMENT.finditer(normalized):
        key = next(
            candidate for candidate in match.group(
                "double_key", "single_key", "bare_key"
            ) if candidate is not None
        )
        if _is_sensitive_key(key):
            return True

    masked = list(scan_text)
    for match in _BARE_EQUALS_KEY.finditer(scan_text.lower()):
        if _is_sensitive_key(match.group("key")):
            return True
        masked[match.start():match.end()] = " " * (match.end() - match.start())
    return False


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _positive(value: Any) -> bool:
    return _integer(value) and value > 0


def _number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value >= 0 and value != float("inf"))


def _string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _enum(value: Any, options: frozenset[str] | set[str]) -> bool:
    return isinstance(value, str) and value in options


def _nullable_string(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _nullable_integer(value: Any) -> bool:
    return value is None or _integer(value)


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(_string(item) for item in value)


def _request_id(value: Any) -> bool:
    return _positive(value)


# v1 is a contract registry, NOT a runtime capability/dispatch registry. P1
# constants remain unchanged until the separately ticketed adapters are wired.
# Schema notation: trailing ? on a field is optional, on a type is nullable;
# [T] is an array, a|b is an enum, and named DTOs are closed objects.
V1_DTOS = {
    "Error": {"code": "code", "message_key": "message", "params": "object",
              "field?": "str", "hold?": "bool", "detail?": "str", "reason_code?": "str"},
    "CollectTier": {"tier": "tier", "generation": "uint", "first_check": "unknown|running|complete",
                    "source_count": "uint?", "healthy": "uint?", "required": "uint?", "observed_at_ms": "uint?"},
    "Listener": {"kind": "mixed|http|socks|controller|subscription", "host": "str", "port": "port", "available": "bool?"},
    "Diagnostic": {"request_id": "str", "mode": "direct|chain", "state": "running|ok|failed|cancelled|superseded",
                   "latency_ms": "number?", "reason_code": "str?", "status_applied": "bool", "cleanup_pending": "bool"},
    "DropCounts": {"debug": "uint", "info": "uint", "warn": "uint", "error": "uint", "critical": "uint", "total": "uint"},
    "ConfigValues": {"network?": "object", "dns?": "object", "routing?": "object", "latency?": "object",
                     "sources?": "[Source]", "alerts?": "object", "ui?": "object", "autostart?": "object",
                     "traffic?": "object", "killswitch?": "object", "bastion?": "object"},
    "SecretEdit": {"field": "str", "target_id?": "str",
                   "action": "set|clear|keep", "value?": "text"},
    "ConfigChange": {"section": "network|dns|routing|latency|sources|alerts|ui|autostart|traffic|killswitch|bastion",
                     "values": "object", "secrets?": "[SecretEdit]"},
    "SectionResult": {"section": "str", "saved": "bool", "applied": "bool",
                      "effect": "none|ui|autostart|recheck|reload|restart|deferred",
                      "error": "Error?"},
    "PendingApply": {"intent_id": "str", "config_rev": "uint", "generation": "uint",
                     "state": "pending|applying|applied|failed|superseded|cancelled",
                     "effect": "recheck|reload|restart", "error": "Error?"},
    "SaveResult": {"rev": "uint", "sections": "[SectionResult]", "pending": "PendingApply?"},
    "Applied": {"saved": "bool", "applied": "bool", "apply_error": "Error?",
                "pending": "PendingApply?"},
    "Node": {"id": "str", "name": "str", "tier": "tier", "kind": "manual|subscription|automatic",
             "enabled": "bool", "status": "unknown|pending|healthy|failed|disabled",
             "reason_code": "str?", "protocol": "str", "latency_ms": "uint?",
             "subscription_id?": "str?",
             # T-P4-C2 #4 (K-5) / #3 (K-2).
             "slow?": "bool", "last_checked_at_ms?": "uint?"},
    "Subscription": {"id": "str", "name": "str", "tier": "tier", "enabled": "bool",
                     "display": "str", "total": "uint", "healthy": "uint",
                     "visibility": "unknown|pending|current|previous", "refreshing": "bool",
                     # T-P4-C2 #6 (K-6).
                     "recheck?": "Recheck", "last_refresh_at_ms?": "uint?"},
    "Recheck": {"done": "uint", "total": "uint"},
    # T-P4-C2 #3/#5 (K-1, K-3): nodes.detail only, never a credential.
    "Endpoint": {"host_masked": "str", "port": "port"},
    "NodeProtocol": {"name": "str", "port": "port", "node_id?": "str"},
    "ConfigItem": {"id": "str", "name": "str", "kind": "str", "section": "str",
                   "enabled": "bool", "secret_set": "bool"},
    "Source": {"id": "str", "kind": "str", "display": "str", "enabled": "bool", "hint?": "str"},
    "IdentityLeg": {"status": "unknown|running|ok|failed|cancelled", "address": "str?",
                    "country": "str?", "reason_code": "str?", "observed_at_ms": "uint?",
                    # T-P4-C2 #1 (G07): source-reported context of an ok leg;
                    # org is untrusted external text.
                    "city?": "str", "timezone?": "str", "asn?": "str", "org?": "str",
                    "type?": "backbone|business|hosting|mobile|residential|isp",
                    "single_source?": "bool"},
    "IdentityResult": {"request_id": "str", "generation": "uint", "binding_id": "str?",
                       "status": "idle|running|ok|partial|failed|cancelled|superseded",
                       "reason_code": "str?", "v4": "IdentityLeg", "v6": "IdentityLeg",
                       "fresh": "bool", "observed_at_ms": "uint?",
                       "native_fallback_verified": "bool",
                       "native_fallback_verified_commit_rejected": "bool",
                       "native_fallback_unverified_budget": "bool",
                       "manual_execution_binding_stale": "bool",
                       # T-P4-C2 #2 (overview R3).
                       "trigger?": "auto|manual|recheck"},
    "RefreshResult": {"total": "uint", "healthy": "uint", "stored": "bool", "applied": "bool",
                      "retained_previous": "bool", "apply_error": "Error?", "error": "Error?"},
    "Alert": {"id": "str", "seq": "uint", "severity": "info|warn|error|critical",
              "event_id": "str", "title": "text", "detail": "text", "ts_ms": "uint"},
    # 提醒 redesign 10-30 (.build/提醒模块重设计_20261030.md §10.3): what a card
    # or an in-app notice renders. The ids are UI message ids (notice.*,
    # errors.start.*); one incident keeps its id through update/resolved/closed.
    "Notice": {"kind": "outage|capture_lost|start_failed|fallback|unstable|update|preview",
               "incident": "str", "state": "open|update|resolved|closed",
               "group": "connection|tier_fallback|update|preview",
               "title_id": "str", "detail_id?": "str", "reason_id?": "str", "fix_id?": "str",
               "params": "object", "action?": "view", "sound": "bool", "sticky": "bool", "ttl_ms": "uint"},
    "Log": {"seq": "uint", "ts_ms": "uint", "rx_ms": "uint", "key": "str",
            "source": "TESSERACT|MIHOMO|ALERT|UNKNOWN", "level": "debug|info|warn|error|critical",
            "category": "str", "title": "text", "detail": "text", "raw": "text",
            "is_key_event?": "bool", "run_id?": "str"},
    "LogFilter": {"levels?": "[str]", "sources?": "[str]", "categories?": "[str]", "text?": "text"},
    "LogPage": {"records": "[Log]", "watermark": "uint", "next_cursor": "str?",
                "gap": "bool", "dropped": "uint"},
    "Geo": {"running": "bool", "updated_at_ms": "uint?", "errors": "[Error]",
            "items?": "[GeoItem]"},  # T-P4-C2 #11 (settings R-11)
    "GeoItem": {"kind": "geoip|geosite|mmdb|asn", "updated_at_ms?": "uint?"},
    "Update": {"state": "idle|checking|available|downloading|verified|handoff|failed",
               "version": "str?", "update_available": "bool", "installable": "bool", "error": "Error?",
               "current_version?": "str?", "previous_version?": "str?",
               # T-P4-C2 #10 (settings R-09): notes is plain, untrusted text.
               "notes?": "text?", "enabled?": "bool", "last_check_at_ms?": "uint?"},
    # T-P4-C2 #13 (G38): one physical node; indexes into the accepted nodes.
    "ImportGroup": {"key": "str", "label?": "str", "row_indexes": "[uint]"},
    "Recovery": {"proxy": "not_needed|restored|not_owned|permission_required|failed|unknown",
                 "firewall": "not_needed|restored|not_owned|permission_required|failed|unknown"},
    "FieldSpec": {"id": "str", "label_id": "str", "type": "string|integer|number|boolean|choice|multichoice|secret|textarea", "required": "bool",
                  "choices": "[str]", "conditions": "object"},
    "ProtocolSpec": {"id": "str", "label_id": "str", "fields": "[FieldSpec]"},
}

# name: (domain, origin, reply mode, cancellation, args, result, business errors)
# All commands additionally admit the common bridge errors in PROTOCOL.md §10.
V1_COMMAND_SPECS = {
    "service.start": ("mutation", "page", "job", "supersede", {},
                      {"startup_phase": "str", "failure_code": "str?"}, "admin_required lifecycle_busy needs_route io timeout"),
    "service.stop": ("control", "page", "job", "none", {"expected_generation?": "uint"},
                     {"startup_phase": "str", "shutdown_exit_code": "uint?", "cleanup_pending": "bool"}, "lifecycle_busy io"),
    "service.restart": ("mutation", "page", "job", "supersede", {},
                        {"applied": "bool", "core_running": "bool", "core_cleanup_pending": "bool", "core_generation": "uint"}, "lifecycle_busy needs_route io timeout"),
    "mode.set": ("mutation", "page", "job", "queued", {"mode": "system|tun|mixed"},
                 {"mode": "system|tun|mixed", "changed": "bool", "saved": "bool", "restarted": "bool", "applied": "bool", "apply_error": "Error?"}, "admin_required conflict io"),
    "exit.verify": ("normal", "page", "job", "owner", {"trigger": "=manual"}, "IdentityResult", "not_running needs_route timeout"),
    "settings.apply": ("mutation", "page", "job", "queued", {"base_rev": "uint", "changes": "[ConfigChange]"}, "SaveResult", "validation conflict io lifecycle_busy"),
    "settings.validate": ("normal", "page", "res", "owner", {"section": "str", "field": "str", "value": "json"},
                          {"valid": "bool", "errors": "[Error]"}, "validation"),
    "nodes.diagnose": ("normal", "page", "job", "diagnostic", {"node_id": "str", "probe_mode": "direct|chain", "request_id": "str"},
                       {"request_id": "str", "status_applied": "bool", "reason_code": "str?", "cleanup_pending": "bool"}, "not_allowed not_running timeout"),
    "nodes.cancelDiagnose": ("control", "page", "res", "none", {"request_id": "str"},
                             {"requested": "bool", "cleanup_pending": "bool"}, "conflict"),
    "nodes.update": ("mutation", "page", "job", "queued", {"item_id": "str", "updates": "object", "secrets?": "[SecretEdit]"}, "Applied", "validation conflict io"),
    "nodes.delete": ("mutation", "page", "job", "queued", {"item_id": "str"}, "Applied", "conflict io"),
    "nodes.reveal": ("normal", "page", "private", "owner", {"item_id": "str", "fields?": "[str]"},
                     {"item_id": "str", "fields": {"password?": "text", "uuid?": "text", "private_key?": "text", "uri?": "text", "subscription_url?": "text"}}, "not_allowed conflict"),
    "subscriptions.refresh": ("mutation", "page", "job", "queued", {"item_id": "str"}, "RefreshResult", "io timeout conflict"),
    "import.preview": ("normal", "page", "res", "owner", {"tier": "tier", "text": "text"},
                       {"plan_id": "str", "expires_at_ms": "uint", "accepted": "uint", "rejected": "[Error]",
                        "groups?": "[ImportGroup]"}, "validation"),
    "import.apply": ("mutation", "page", "job", "queued", {"tier": "tier", "mode": "paste|manual|chain|subscriptions", "plan_id?": "str", "manual?": "object", "chain?": "[object]", "urls?": "[text]"},
                     {"saved": "bool", "applied": "bool", "items": "[str]", "subscriptions": "[RefreshResult]", "apply_error": "Error?"}, "validation conflict io"),
    # T-P4-C2 #9 (K-10): proxy_name "" deactivates the Gateway (selected "").
    # T-P2-H5: node_id (P2-04 Node.id of a Gateway row) is preferred; exactly
    # one of node_id / proxy_name (compatibility) is given (_command_invariants).
    "gateway.select": ("mutation", "page", "job", "queued", {"proxy_name?": "text", "node_id?": "str"},
                       {"selected": "text", "applied": "bool", "apply_error": "Error?", "selection": "str?", "pending": "PendingApply?"}, "validation conflict io needs_route"),
    "geo.update": ("normal", "page", "job", "queued", {"force": "true"},
                   {"triggered": "bool", "completed": "bool", "status": "Geo"}, "needs_route io timeout"),
    "app.update.check": ("normal", "page", "job", "queued", {}, "Update", "io timeout"),
    "app.update.install": ("normal", "page", "job", "queued", {},
                           {"verified": "bool", "handoff_pending": "bool", "receipt_ref": "str"}, "not_allowed io conflict"),
    "logs.setMihomoLevel": ("normal", "page", "res", "none", {"level": "info|debug"}, {"level": "info|debug", "applied": "bool"}, "not_running io"),
    "logs.exportBundle": ("normal", "page", "job", "queued", {"save_ref": "str"}, {"ref": "str", "size": "uint", "files": "[str]"}, "not_allowed io"),
    "logs.exportFiltered": ("normal", "page", "job", "queued", {"save_ref": "str", "format": "json|text|csv", "seqs?": "[uint]", "filter?": "LogFilter"},
                            {"ref": "str", "count": "uint", "missing_seqs": "[uint]"}, "not_allowed io snapshot_required"),
    "alerts.test": ("normal", "page", "job", "queued", {}, {"ok": "bool", "error": "Error?", "delivery_id": "str?"}, "io timeout"),
    "alerts.desktopDelivered": ("control", "shell", "res", "none", {"delivery_id": "str", "outcome": "delivered|failed|dropped", "surface": "windows|in_app|none"}, {"recorded": "bool", "duplicate": "bool"}, "conflict timeout"),
    "ui.presence": ("sampling", "shell", "res", "none", {"visible": "bool", "focused": "bool", "page": "overview|nodes|settings|logs|alerts|about|none"}, {"recorded": "bool"}, "validation"),
    "app.quit": ("control", "shell", "job", "none", {"reason?": "user|update|shell_lost"},
                 {"startup_phase": "str", "shutdown_exit_code": "uint?", "cleanup_pending": "bool"}, "lifecycle_busy io"),
    "shell.saveGrant": ("control", "shell", "res", "none", {"ref": "str", "purpose": "bundle|filtered", "expires_at_ms": "uint", "format": "zip|json|text|csv", "handle_id": "uint", "for_owner?": "str"}, {"registered": "bool"}, "not_allowed conflict"),
    "update.exitResponse": ("control", "shell", "res", "none", {"handoff_id": "str", "accepted": "bool", "reason_code": "str?"}, {"recorded": "bool"}, "conflict"),
    "update.installResult": ("control", "shell", "res", "none", {"receipt_ref": "str", "launched": "bool", "reason_code": "str?"}, {"recorded": "bool"}, "conflict io"),
    "app.sessionEnd": ("control", "shell", "res", "none", {"reason": "logoff|shutdown", "budget_ms": "uint"},
                       {"writes_revoked": "bool", "recovery": "Recovery", "workers_pending": "bool"}, "io timeout"),
    "host.saveFile": ("shell", "page", "res", "dialog", {"purpose": "bundle|filtered", "format": "zip|json|text|csv"}, {"cancelled": "bool", "ref": "str?"}, "io"),
    "host.openFolder": ("shell", "page", "res", "none", {"kind": "config|data|logs|exports"}, {"opened": "bool"}, "io not_allowed"),
    "host.openUrl": ("shell", "page", "res", "none", {"url": "text"}, {"opened": "bool"}, "not_allowed io"),
    "host.reveal": ("shell", "page", "res", "none", {"ref": "str"}, {"revealed": "bool"}, "not_allowed io"),
}

V1_QUERY_SPECS = {
    "app.info": ({}, {"version": "str", "language": "str", "caps": "[str]", "listeners": "[Listener]"}),
    "config.snapshot": ({}, {"rev": "uint", "values": "ConfigValues", "secret_fields_set": "[str]", "read_errors": "[Error]", "pending": "PendingApply?"}),
    "nodes.detail": ({"node_id": "str"}, {"node": "Node", "config_item": "ConfigItem?", "last_diagnostic": "Diagnostic?", "check_interval_s": "uint",
                                          "endpoint?": "Endpoint?", "protocols?": "[NodeProtocol]"}),
    "import.specs": ({}, {"protocols": "[ProtocolSpec]"}),
    "logs.tail": ({"since_seq?": "uint", "n": "limit4000", "cursor?": "str"}, "LogPage"),
    "logs.query": ({"before_seq": "uint", "filter": "LogFilter", "n": "limit4000", "cursor?": "str"}, "LogPage"),
    "autostart.state": ({}, {"supported": "bool", "enabled": "bool", "target": "shell|legacy_qt|other|none", "mismatch": "bool", "error": "Error?"}),
    "geo.status": ({}, "Geo"),
}

# New topics, with source domain, kind, payload, frequency and stale rules.
V1_TOPIC_SPECS = {
    "service.collect": ("sampling", "state", {"generation": "uint", "tiers": "[CollectTier]", "observed_at_ms": "uint"}),
    "tier.transition": ("sampling", "event", {"from": "tier", "to": "tier", "observed_at_ms": "uint", "authority": "=observed"}),
    "exit.identity": ("exit_owner", "state", {"current": "IdentityResult", "previous": "IdentityResult?"}),
    "protocols.mix": ("sampling", "state", {"source": "loaded|configured", "counts": "counts", "observed_at_ms": "uint"}),
    "network.mode": ("sampling", "state", {"mode": "system|tun|mixed", "admin": "bool", "switching": "bool", "proxy_state": "owned|other|disabled|unknown", "drift": "bool"}),
    "killswitch.state": ("rt5", "state", {"enabled": "bool", "active": "bool", "admin": "bool", "error": "Error?", "triggered?": "bool"}),
    "bastion.pool": ("sampling", "state", {"runtime_alive": "uint", "user_alive": "uint", "capacity": "uint", "min_alive_nodes": "uint", "target_alive": "uint", "collecting": "bool", "observed_at_ms": "uint",
                                          # §8u: totals of the auto pool and of the user's own Bastion nodes
                                          "runtime_total?": "uint", "user_total?": "uint"}),
    "bastion.sources": ("config_owner", "list", {"items": "[Source]"}),
    "nodes.list": ("node_owner", "keyed", {"items": "[Node]"}),
    "subscriptions.list": ("node_owner", "keyed", {"items": "[Subscription]"}),
    "gateway.current": ("sampling", "state", {"selected": "str?", "effective": "str?", "missing": "bool", "applied": "bool", "pending": "PendingApply?",
                                              "selected_node_id?": "str"}),
    "config.changed": ("config_owner", "event", {"rev": "uint", "files": "[str]", "sections": "[SectionResult]", "pending": "PendingApply?", "external_detection": "=bridge_writes_only"}),
    "config.items": ("config_owner", "list", {"items": "[ConfigItem]"}),
    "logs.feed": ("log_owner", "state", {"state": "connected|reconnecting|stdout|stopped", "level": "info|debug", "updated_at_ms": "uint", "dropped": "DropCounts", "run_id": "str?",
                                         "reason?": "controller_disabled|core_not_running|stream_error?"}),
    "alerts.history": ("rt6", "state", {"items": "[Alert]", "last_seq": "uint"}),
    "alerts.desktop": ("alert_owner", "shell_event", {"delivery_id": "str", "event_ids": "[str]", "severity": "info|warn|error|critical", "title": "text", "detail": "text", "members": "[str]", "target": "overview|alerts|update", "version": "str?", "force?": "bool", "notice?": "Notice"}),
    "update.state": ("update_owner", "state", "Update"),
    "geo.status": ("geo_owner", "state", "Geo"),
    "shutdown": ("shutdown_owner", "shell_state", {"phase": "draining|stopping|recovering|waiting_workers|finished", "recovery": "Recovery", "workers_pending": "bool"}),
    "update.prepareExit": ("update_owner", "shell_event", {"handoff_id": "str", "receipt_ref": "str", "version": "str", "verified": "true", "expires_at_ms": "uint",
                                                           "installer_path": "str", "sha256": "str", "args": "[str]"}),
    "ui.language": ("config_owner", "state", {"language": "zh_CN|en_US|ja_JP|ko_KR|fr_FR|de_DE|es_ES|pt_BR|ru_RU", "config_rev": "uint"}),
}
V1_COMMANDS = frozenset(V1_COMMAND_SPECS)
V1_QUERIES = frozenset(V1_QUERY_SPECS)
V1_TOPICS = P1_TOPICS | frozenset(V1_TOPIC_SPECS)
PAGE_COMMANDS = frozenset(name for name, spec in V1_COMMAND_SPECS.items() if spec[1] == "page")
SHELL_COMMANDS = V1_COMMANDS - PAGE_COMMANDS
# hello.caps.commands: every command the sidecar executes plus the queries
# (host.* are shell-domain and never announced by Python).
HELLO_CAP_COMMANDS = frozenset(name for name, spec in V1_COMMAND_SPECS.items()
                               if spec[0] != "shell") | V1_QUERIES
SHELL_TOPICS = frozenset(name for name, spec in V1_TOPIC_SPECS.items() if spec[1].startswith("shell_"))
KEYED_TOPICS = frozenset({"nodes.list", "subscriptions.list"})
V1_SNAPSHOT_TOPICS = P1_SNAPSHOT_TOPICS | frozenset(
    name for name, spec in V1_TOPIC_SPECS.items() if spec[1] not in {"event", "shell_event"})
for _topic, _spec in V1_TOPIC_SPECS.items():
    TOPIC_KINDS[_topic] = (frozenset({"edge", "gap"}) if _spec[1] in {"event", "shell_event"}
                         else frozenset({"snapshot", "patch", "gap"}) if _spec[1] in {"list", "keyed"}
                         else frozenset({"snapshot", "edge", "gap"}))


def _schema(value: Any, spec: Any, path: str, errors: list[str]) -> None:
    """Validate bounded, closed DTOs without resolving any runtime object."""
    if isinstance(spec, str):
        if spec.endswith("?"):
            if value is not None:
                _schema(value, spec[:-1], path, errors)
            return
        if spec.startswith("["):
            if not isinstance(value, list) or len(value) > 4000:
                errors.append(f"type:{path}")
            else:
                for index, item in enumerate(value):
                    _schema(item, spec[1:-1], f"{path}[{index}]", errors)
            return
        if spec in V1_DTOS:
            _schema(value, V1_DTOS[spec], path, errors)
            if spec == "IdentityResult" and isinstance(value, dict):
                legs = [value.get("v4"), value.get("v6")]
                states = [leg.get("status") if isinstance(leg, dict) else None for leg in legs]
                if ((value.get("status") == "ok" and states != ["ok", "ok"]) or
                        (value.get("status") == "partial" and sorted(state for state in states if isinstance(state, str)) != ["failed", "ok"])):
                    errors.append(f"invariant:{path}.status")
                if value.get("fresh") is True and (not _string(value.get("binding_id")) or not _integer(value.get("observed_at_ms"))):
                    errors.append(f"invariant:{path}.fresh")
            if spec == "IdentityLeg" and isinstance(value, dict):
                if value.get("status") == "ok" and (not _string(value.get("address")) or not _integer(value.get("observed_at_ms")) or value.get("reason_code") is not None):
                    errors.append(f"invariant:{path}.address")
            if spec == "Update" and isinstance(value, dict):
                if ((value.get("update_available") is True and not _string(value.get("version"))) or
                        (value.get("installable") is True and value.get("update_available") is not True) or
                        (value.get("state") == "available" and value.get("update_available") is not True)):
                    errors.append(f"invariant:{path}.version")
                # T-P4-C1 #7: "update completed" needs the running version too.
                if value.get("previous_version") is not None and not _string(value.get("current_version")):
                    errors.append(f"invariant:{path}.previous_version")
            if spec == "Source" and isinstance(value, dict) and isinstance(value.get("hint"), str):
                # T-P4-C1 (U4) review fix: only the sidecar's mask shape goes on
                # the wire, so a generator bug can never send a source value.
                if not _SOURCE_HINT.fullmatch(value["hint"]):
                    errors.append(f"invariant:{path}.hint")
            if isinstance(value, dict):
                _c2_invariants(spec, value, path, errors)
            return
        predicates = {"uint": _integer, "str": _string, "text": lambda v: isinstance(v, str),
                      "number": _number, "port": lambda v: _positive(v) and v <= 65535,
                      "counts": lambda v: isinstance(v, dict) and all(_string(k) and _integer(n) for k, n in v.items()),
                      "bool": lambda v: isinstance(v, bool), "true": lambda v: v is True,
                      "false": lambda v: v is False, "object": lambda v: isinstance(v, dict),
                      "json": lambda v: v is None or isinstance(v, (str, bool, int, float, list, dict)),
                      "tier": lambda v: _enum(v, _TIERS), "code": lambda v: _enum(v, ERROR_CODES),
                      "message": lambda v: isinstance(v, str) and bool(_MESSAGE_KEY.fullmatch(v)),
                      "limit200": lambda v: _positive(v) and v <= 200,
                      "limit4000": lambda v: _positive(v) and v <= 4000}
        predicate = predicates.get(spec)
        if predicate is not None:
            good = predicate(value)
        elif spec.startswith("="):
            good = value == spec[1:]
        else:
            good = _enum(value, set(spec.split("|")))
        if not good:
            errors.append(f"type:{path}")
        return
    if not isinstance(value, dict):
        errors.append(f"type:{path}")
        return
    if set(value) - {key.rstrip("?") for key in spec}:
        errors.append(f"unknown:{path}")
    for field, field_spec in spec.items():
        name = field.rstrip("?")
        if name in value:
            _schema(value[name], field_spec, f"{path}.{name}", errors)
        elif not field.endswith("?"):
            errors.append(f"missing:{path}.{name}")


# Source.hint (U4): "<o>•••/<r>•••", "@<c>•••" or "<h>•••"; one ASCII
# letter/digit (or "•") per segment, nothing else of the value.
_SOURCE_HINT = re.compile("@?[A-Za-z0-9•]•{3}(?:/[A-Za-z0-9•]•{3})?")

# T-P4-C2 #3 (K-1) Endpoint.host_masked: "<c>•••.<tld>" / "<c>•••" for a name,
# "<first octet>.•••" for IPv4, "<first group>:•••" for IPv6. Nothing more
# of the host can reach the wire even if the generator is wrong.
_HOST_MASK = re.compile("[A-Za-z0-9•]•{3}(?:\\.[A-Za-z][A-Za-z0-9-]{0,62})?"
                        "|[0-9]{1,3}\\.•{3}|[0-9a-fA-F]{1,4}:•{3}")
# overview_ip_check: IANA zone (or UTC), decimal ASN without the "AS" prefix.
_TIMEZONE = re.compile(r"(?:UTC|[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+){1,2})")
_ASN = re.compile(r"[1-9][0-9]{0,9}")
_LEG_CONTEXT = ("city", "timezone", "asn", "org", "type", "single_source")
# ImportGroup.key is a metadata path (no sensitive scan), so its shape is fixed:
# the group position "g<n>", never anything derived from the input text.
_IMPORT_GROUP_KEY = re.compile(r"g[0-9]{1,6}")


def _c2_invariants(spec: str, value: dict, path: str, errors: list[str]) -> None:
    """T-P4-C2 field invariants (v1 additions; every field optional)."""
    if spec == "IdentityLeg":
        # Context belongs to an observed address only, never to a failed leg.
        if value.get("status") != "ok" and any(name in value for name in _LEG_CONTEXT):
            errors.append(f"invariant:{path}.context")
        if isinstance(value.get("timezone"), str) and (
                len(value["timezone"]) > 64 or not _TIMEZONE.fullmatch(value["timezone"])):
            errors.append(f"invariant:{path}.timezone")
        if isinstance(value.get("asn"), str) and (
                not _ASN.fullmatch(value["asn"]) or int(value["asn"]) >= 2 ** 32):
            errors.append(f"invariant:{path}.asn")
        for name in ("city", "org"):
            if isinstance(value.get(name), str) and len(value[name]) > 256:
                errors.append(f"invariant:{path}.{name}")
    elif spec == "Node":
        if value.get("slow") is True and value.get("status") != "healthy":
            errors.append(f"invariant:{path}.slow")
    elif spec == "Recheck":
        if _integer(value.get("done")) and _integer(value.get("total")) and value["done"] > value["total"]:
            errors.append(f"invariant:{path}.done")
    elif spec == "Endpoint":
        if isinstance(value.get("host_masked"), str) and not _HOST_MASK.fullmatch(value["host_masked"]):
            errors.append(f"invariant:{path}.host_masked")
    elif spec == "ImportGroup":
        rows = value.get("row_indexes")
        if isinstance(rows, list) and (not rows or len(rows) != len(set(
                row for row in rows if _integer(row)))):
            errors.append(f"invariant:{path}.row_indexes")
        if isinstance(value.get("label"), str) and len(value["label"]) > 128:
            errors.append(f"invariant:{path}.label")
        if isinstance(value.get("key"), str) and not _IMPORT_GROUP_KEY.fullmatch(value["key"]):
            errors.append(f"invariant:{path}.key")


def _import_groups_invariant(data: dict, errors: list[str]) -> None:
    """T-P4-C2 #13: every row index is an accepted node, in one group only."""
    groups, accepted = data.get("groups"), data.get("accepted")
    if not isinstance(groups, list) or not _integer(accepted):
        return
    seen: set[int] = set()
    for group in groups:
        rows = group.get("row_indexes") if isinstance(group, dict) else None
        for row in rows if isinstance(rows, list) else ():
            if not _integer(row) or row >= accepted or row in seen:
                errors.append("invariant:data.groups")
                return
            seen.add(row)


def contains_sensitive_text(value: str) -> bool:
    """Public form of the frame text scan (producers drop a field it rejects)."""
    return isinstance(value, str) and _contains_sensitive_text(value)


def _command_invariants(command: str, args: dict, errors: list[str]) -> None:
    if command == "nodes.reveal" and "fields" in args:
        fields = args["fields"]
        if (not isinstance(fields, list) or not fields or
                any(not _enum(field, {"password", "uuid", "private_key", "uri", "subscription_url"}) for field in fields) or
                len(fields) != len(set(field for field in fields if isinstance(field, str)))):
            errors.append("not_allowed:args.fields")
    if command == "import.apply":
        mode = args.get("mode")
        field = {"paste": "plan_id", "manual": "manual", "chain": "chain", "subscriptions": "urls"}.get(mode) if isinstance(mode, str) else None
        if field and (field not in args or not args[field]):
            errors.append(f"missing:args.{field}")
        if set(args) & {"plan_id", "manual", "chain", "urls"} != ({field} if field else set()):
            errors.append("invariant:args.import_mode")
    if command == "logs.exportFiltered" and (("seqs" in args) == ("filter" in args)):
        errors.append("invariant:args.seqs|filter")
    if command == "gateway.select":
        # T-P2-H5: node_id and proxy_name together are invalid_type; neither
        # is the old missing proxy_name (missing_field).
        if "node_id" in args and "proxy_name" in args:
            errors.append("type:args.node_id|proxy_name")
        elif "node_id" not in args and "proxy_name" not in args:
            errors.append("missing:args.proxy_name")
    if command == "host.openUrl" and isinstance(args.get("url"), str):
        try:
            link = urlsplit(args["url"])
            valid = (link.scheme == "https" and link.hostname in {"github.com", "docs.github.com", "t.me", "ntfy.sh", "pushover.net"}
                     and link.username is None and link.password is None and link.port in (None, 443)
                     and not any(char.isspace() or ord(char) < 32 for char in args["url"]))
        except ValueError:
            valid = False
        if not valid:
            errors.append("not_allowed:args.url")
    if command == "alerts.desktopDelivered":
        if args.get("outcome") == "delivered" and args.get("surface") == "none":
            errors.append("invariant:args.surface")
    if command in {"host.saveFile", "shell.saveGrant"}:
        if ((args.get("purpose") == "bundle" and args.get("format") != "zip") or
                (args.get("purpose") == "filtered" and args.get("format") not in ("json", "text", "csv"))):
            errors.append("invariant:args.format")
    if command in {"settings.apply", "nodes.update"}:
        containers = args.get("changes", []) if command == "settings.apply" else [args]
        if isinstance(containers, list):
            if command == "settings.apply" and not containers:
                errors.append("missing:args.changes")
            sections = [item.get("section") for item in containers if isinstance(item, dict) and isinstance(item.get("section"), str)]
            if command == "settings.apply" and len(sections) != len(set(sections)):
                errors.append("invariant:args.changes")
            for item in containers:
                if not isinstance(item, dict):
                    continue
                for secret in item.get("secrets", []) if isinstance(item.get("secrets", []), list) else []:
                    if isinstance(secret, dict) and ((secret.get("action") == "set") != ("value" in secret)):
                        errors.append("invariant:args.secrets.value")
                values = item.get("values", item.get("updates", {}))
                if isinstance(values, dict) and _contains_hidden_setting(values):
                    errors.append("not_allowed:args.node_judgement")
    if command == "settings.validate" and (args.get("field") == "node_judgement" or
                                            "node_judgement" in str(args.get("field", "")).split(".")):
        errors.append("not_allowed:args.node_judgement")


def _contains_hidden_setting(value: Any) -> bool:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if any("node_judgement" in str(key).split(".") for key in item):
                return True
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def _topic_key(obj: dict, topic: Any, errors: list[str]) -> None:
    if _enum(topic, KEYED_TOPICS):
        _need(obj, "key", "", lambda v: _enum(v, _TIERS), errors, enum=True)
    elif "key" in obj:
        errors.append("not_allowed:key")


def _schema_metadata_paths(value: Any, spec: Any, prefix=()) -> set[tuple]:
    paths: set[tuple] = set()
    if isinstance(spec, str):
        if spec.endswith("?"):
            return _schema_metadata_paths(value, spec[:-1], prefix) if value is not None else paths
        if spec.startswith("[") and isinstance(value, list):
            for i, item in enumerate(value):
                paths.update(_schema_metadata_paths(item, spec[1:-1], prefix + (i,)))
        elif spec in V1_DTOS:
            if spec == "Error":
                paths.add(prefix + ("message_key",))
            elif spec == "Notice" and isinstance(value, dict):
                paths.update(prefix + (name,) for name in ("title_id", "detail_id", "reason_id", "fix_id")
                             if name in value)
            elif spec in {"Log", "ImportGroup"}:  # ImportGroup.key: T-P4-C2 #13
                paths.add(prefix + ("key",))
            paths.update(_schema_metadata_paths(value, V1_DTOS[spec], prefix))
    elif isinstance(spec, dict) and isinstance(value, dict):
        for name, child in spec.items():
            key = name.rstrip("?")
            if key in value:
                paths.update(_schema_metadata_paths(value[key], child, prefix + (key,)))
    return paths


def _v1_frame_metadata_paths(value: dict) -> set[tuple]:
    paths: set[tuple] = set()
    frame_type, cmd = value.get("t"), value.get("cmd")
    if frame_type == "res" and _enum(cmd, V1_COMMANDS | V1_QUERIES):
        spec = V1_QUERY_SPECS[cmd][1] if cmd in V1_QUERIES else V1_COMMAND_SPECS[cmd][5]
        paths.update(_schema_metadata_paths(value.get("data"), spec, ("data",)))
    elif frame_type == "evt":
        topic = value.get("topic")
        if _enum(topic, frozenset(V1_TOPIC_SPECS)):
            paths.update(_schema_metadata_paths(value.get("data"), V1_TOPIC_SPECS[topic][2], ("data",)))
            if topic in KEYED_TOPICS:
                paths.add(("key",))
        elif topic == "jobs" and isinstance(value.get("data"), dict):
            name = value["data"].get("name")
            if _enum(name, V1_COMMANDS):
                paths.update(_schema_metadata_paths(value["data"].get("result"), V1_COMMAND_SPECS[name][5], ("data", "result")))
    elif frame_type == "batch" and isinstance(value.get("items"), list):
        for i, item in enumerate(value["items"]):
            if isinstance(item, dict):
                paths.update(("items", i) + path for path in _v1_frame_metadata_paths(item))
    elif frame_type in ("snapshot_req", "snapshot_res"):
        if _enum(value.get("topic"), KEYED_TOPICS):
            paths.add(("key",))
        if isinstance(value.get("snapshot"), dict):
            paths.update(("snapshot",) + path for path in _v1_frame_metadata_paths(value["snapshot"]))
    return paths


# These are the ONLY command-specific credential exceptions. A slot does not
# exempt extensions, logs, job results, topics, or internal deadline values.
PRIVATE_INPUT_PATHS = {
    "settings.apply": (("changes", "*", "secrets", "*", "value"),),
    "settings.validate": (("value",),),
    "nodes.update": (("secrets", "*", "value"),),
    "import.preview": (("text",),),
    "import.apply": (("manual",), ("chain",), ("urls",)),
    "host.openUrl": (("url",),),
}
PRIVATE_OUTPUT_PATHS = {
    "nodes.reveal": (("fields",),),
}


def _private_frame_paths(value: dict) -> tuple[tuple, ...]:
    cmd = value.get("cmd")
    if not isinstance(cmd, str) or not _string(value.get("owner")) or not _string(value.get("req_id")):
        return ()
    if value.get("t") == "req":
        return tuple(("args",) + path for path in PRIVATE_INPUT_PATHS.get(cmd, ()))
    if value.get("t") == "res" and value.get("ok") is True:
        return tuple(("data",) + path for path in PRIVATE_OUTPUT_PATHS.get(cmd, ()))
    return ()


# T-P4-C1 #4 (R-04): DNS resolver addresses (``https://``, ``tls://``, ``quic://``
# and plain IPs) are not credentials. They are exempt from the "contains ://"
# rule ONLY in these DNS list slots, and only without userinfo and without any
# other sensitive text (a query key such as ``token=`` still rejects the frame).
DNS_RESOLVER_FIELDS = frozenset({"nameserver", "default_nameserver", "proxy_server_nameserver",
                                 "direct_nameserver", "fallback"})
_RESOLVER_SCHEME = re.compile(r"[a-z][a-z0-9+.-]{0,15}")


# T-P2-H2 (决定_第五波裁定 C1-#2): private resolvers (NextDNS, AdGuard, ControlD, ...)
# carry an account ID in the path or in a host label (any label left of the
# registrable domain, or anything below a private-resolver zone), which is an
# access credential. Public only when every condition holds; anything in doubt
# is a secret (fail closed) and goes back to the redaction table.
_RESOLVER_PUBLIC_PATHS = frozenset({"", "/", "/dns-query", "/resolve", "/query", "/dns"})
_RESOLVER_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_RESOLVER_HEX_ID = re.compile(r"[0-9a-f]{6,}")
# T-P2-H2 fix round 1: zones whose every sub-name is a per-account private
# resolver, whatever its spelling (AdGuard DNS private servers, NextDNS,
# ControlD). Any label left of the zone is an ID, even an all-letter one.
_PRIVATE_RESOLVER_ZONES = (("d", "adguard-dns", "com"), ("dns", "nextdns", "io"),
                           ("dns", "controld", "com"))


def _resolver_label_id_like(label: str) -> bool:
    """A host label that may be a private resolver ID: all-hex of six or more
    characters (NextDNS ``45f2a1``, AdGuard ``d1a2b3c4``), six or more
    characters with any digit, or unusually long."""
    return (bool(_RESOLVER_HEX_ID.fullmatch(label))
            or (len(label) >= 6 and any(ch.isdigit() for ch in label))
            or len(label) > 24)


def _resolver_hostname_public(labels: list[str]) -> bool:
    """Every label except the registrable pair (the last two) must not look
    like an ID, and nothing may sit below a known private-resolver zone."""
    for zone in _PRIVATE_RESOLVER_ZONES:
        if len(labels) > len(zone) and tuple(labels[-len(zone):]) == zone:
            return False
    return not any(_resolver_label_id_like(label) for label in labels[:-2])


def _public_resolver_host(authority: str) -> bool:
    import ipaddress
    if authority.startswith("["):
        end = authority.find("]")
        port = authority[end + 1:] if end > 0 else ""
        if end <= 1 or (port and not re.fullmatch(r":[0-9]{1,5}", port)):
            return False
        try:
            ipaddress.IPv6Address(authority[1:end])
        except ValueError:
            return False
        return True
    try:
        ipaddress.ip_address(authority)        # IPv4, or a bare IPv6 literal
        return True
    except ValueError:
        pass
    host, separator, port = authority.rpartition(":")
    if not separator:
        host = authority
    elif not re.fullmatch(r"[0-9]{1,5}", port):
        return False
    try:
        ipaddress.IPv4Address(host)
        return True
    except ValueError:
        pass
    labels = host.lower().split(".")
    if not all(_RESOLVER_HOST_LABEL.fullmatch(label) for label in labels):
        return False
    return _resolver_hostname_public(labels)


def public_dns_resolver(value: Any) -> bool:
    """One resolver address that may be shown as plain configuration.

    T-P2-H2 (C1-#2), all of: no userinfo; no query string and no fragment;
    a path that is empty or exactly ``/dns-query``, ``/resolve``, ``/query``
    or ``/dns``; a host that is an IP literal, or a host name none of whose
    labels left of the registrable pair looks like a random ID and that is not
    below a private-resolver zone (``d.adguard-dns.com``, ``dns.nextdns.io``,
    ``dns.controld.com``); and no other sensitive text. Otherwise False.
    """
    if not isinstance(value, str) or not value or len(value) > 1024 or value != value.strip():
        return False
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        return False
    scheme, separator, rest = value.partition("://")
    if not separator:
        rest = value
    elif not _RESOLVER_SCHEME.fullmatch(scheme.lower()):
        return False
    if "://" in rest or "?" in rest or "#" in rest or "@" in rest:
        return False
    authority, slash, path = rest.partition("/")
    if not authority or (slash + path) not in _RESOLVER_PUBLIC_PATHS:
        return False
    if not _public_resolver_host(authority):
        return False
    return not _contains_sensitive_text(rest)


def public_dns_resolver_text(value: Any) -> bool:
    """A resolver value as submitted: one address or newline-separated lines."""
    if not isinstance(value, str):
        return False
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return bool(lines) and all(public_dns_resolver(line) for line in lines)


def _dns_resolver_frame_paths(value: dict) -> frozenset[tuple[str | int, ...]]:
    """Exact DNS list slots of config.snapshot (res) and settings.apply (req)."""
    paths: set[tuple[str | int, ...]] = set()

    def add(prefix: tuple, block: Any) -> None:
        if not isinstance(block, dict):
            return
        for field in DNS_RESOLVER_FIELDS:
            item = block.get(field)
            if isinstance(item, str):
                paths.add(prefix + (field,))
            elif isinstance(item, list):
                paths.update(prefix + (field, index) for index in range(len(item)))

    if value.get("t") == "res" and value.get("cmd") == "config.snapshot" and value.get("ok") is True:
        data = value.get("data")
        values = data.get("values") if isinstance(data, dict) else None
        if isinstance(values, dict):
            add(("data", "values", "dns"), values.get("dns"))
    if value.get("t") == "req" and value.get("cmd") == "settings.apply" and isinstance(value.get("args"), dict):
        changes = value["args"].get("changes")
        for index, change in enumerate(changes if isinstance(changes, list) else ()):
            if isinstance(change, dict) and change.get("section") == "dns":
                add(("args", "changes", index, "values"), change.get("values"))
    return frozenset(paths)


def _inside_private(path: tuple, slots: tuple[tuple, ...]) -> bool:
    return any(len(path) >= len(slot) and all(
        expected == actual or expected == "*" and isinstance(actual, int)
        for expected, actual in zip(slot, path)) for slot in slots)


def _internal_clock_key(key: str) -> bool:
    decoded, incomplete = _fully_decode_json_backslash_escapes(key)
    return incomplete or any(part in {"deadline", "monotonic"} for part in re.findall(r"[a-z0-9]+", decoded.lower()))


def _need(
    obj: Any, key: str, path: str, predicate: Callable[[Any], bool],
    errors: list[str], *, enum: bool = False,
) -> Any:
    name = f"{path}.{key}" if path else key
    if not isinstance(obj, dict):
        errors.append(f"type:{path or 'object'}")
        return None
    if key not in obj:
        errors.append(f"missing:{name}")
        return None
    value = obj[key]
    if not predicate(value):
        errors.append(f"{'enum' if enum else 'type'}:{name}")
        return None
    return value


def _optional(
    obj: dict[str, Any], key: str, path: str,
    predicate: Callable[[Any], bool], errors: list[str], *, enum: bool = False,
) -> None:
    if key in obj:
        _need(obj, key, path, predicate, errors, enum=enum)


def _nested(obj: Any, key: str, path: str, errors: list[str]) -> dict[str, Any] | None:
    value = _need(obj, key, path, lambda v: isinstance(v, dict), errors)
    return value if isinstance(value, dict) else None


def _traffic_session_counters(key: Any, value: Any) -> bool:
    # traffic.tick.session is a defined pair of byte counters, not a credential.
    return (key == "session" and isinstance(value, dict)
            and set(value) == {"up_bytes", "down_bytes"}
            and all(_integer(count) for count in value.values()))


def _error_schema_key_paths(
    value: Any, prefix: tuple[str | int, ...] = (),
) -> set[tuple[str | int, ...]]:
    if not isinstance(value, dict):
        return set()
    return {prefix + ("message_key",)}


def _log_schema_key_paths(
    value: Any, prefix: tuple[str | int, ...] = (),
) -> set[tuple[str | int, ...]]:
    if not isinstance(value, dict) or not isinstance(value.get("records"), list):
        return set()
    return {
        prefix + ("records", index, "key")
        for index, record in enumerate(value["records"])
        if isinstance(record, dict)
    }


def _topic_schema_key_paths(
    topic: Any, value: Any, prefix: tuple[str | int, ...] = (),
) -> set[tuple[str | int, ...]]:
    if topic == "logs.batch":
        return _log_schema_key_paths(value, prefix)
    if topic == "service.state" and isinstance(value, dict) and isinstance(value.get("runtime"), dict):
        return _error_schema_key_paths(value["runtime"].get("failure"), prefix + ("runtime", "failure"))
    if topic == "jobs" and isinstance(value, dict):
        paths = _error_schema_key_paths(value.get("error"), prefix + ("error",))
        name = value.get("name")
        if _enum(name, V1_COMMANDS):
            paths.update(_schema_metadata_paths(value.get("result"), V1_COMMAND_SPECS[name][5], prefix + ("result",)))
        return paths
    return set()


def _event_schema_key_paths(
    value: Any, prefix: tuple[str | int, ...] = (),
) -> set[tuple[str | int, ...]]:
    if not isinstance(value, dict):
        return set()
    return _topic_schema_key_paths(
        value.get("topic"), value.get("data"), prefix + ("data",)
    )


def _frame_schema_key_paths(value: Any) -> frozenset[tuple[str | int, ...]]:
    if not isinstance(value, dict):
        return frozenset()
    paths: set[tuple[str | int, ...]] = set()
    frame_type = value.get("t")
    if frame_type == "hello":
        paths.update(_error_schema_key_paths(value.get("init_error"), ("init_error",)))
    elif isinstance(frame_type, str) and frame_type in {
        "hello_ack", "ack", "res", "error", "fatal", "snapshot_res",
    }:
        paths.update(_error_schema_key_paths(value.get("error"), ("error",)))
    elif frame_type == "evt":
        paths.update(_event_schema_key_paths(value))
    elif frame_type == "batch" and isinstance(value.get("items"), list):
        for index, item in enumerate(value["items"]):
            paths.update(_event_schema_key_paths(item, ("items", index)))
    paths.update(_v1_frame_metadata_paths(value))
    return frozenset(paths)


def _validated_log_payload_paths(value: Any, prefix=()) -> set[tuple]:
    if not isinstance(value, dict):
        return set()
    if value.get("t") == "evt" and value.get("topic") == "logs.batch" and value.get("kind") == "edge":
        return {prefix + ("data",)}
    if value.get("t") == "batch" and isinstance(value.get("items"), list):
        return set().union(*(_validated_log_payload_paths(item, prefix + ("items", i))
                             for i, item in enumerate(value["items"])))
    return set()


def _sensitive(
    value: Any, path: str, errors: list[str],
    *, traffic_session_paths: frozenset[tuple[str | int, ...]] = frozenset(),
    schema_key_paths: frozenset[tuple[str | int, ...]] = frozenset(),
    scanned_log_paths: frozenset[tuple[str | int, ...]] = frozenset(),
    private_paths: tuple[tuple, ...] = (),
    resolver_paths: frozenset[tuple[str | int, ...]] = frozenset(),
) -> None:
    error_prefixes = tuple(slot[:-1] for slot in schema_key_paths if slot[-1] == "message_key")
    key_cache, text_cache = {}, {}
    stack = [(value, path, (), 0)]
    while stack:
        item, item_path, item_key_path, depth = stack.pop()
        if item_key_path in scanned_log_paths:
            continue
        if depth > 64:
            errors.append(f"depth:{item_path}")
            continue
        if isinstance(item, dict):
            for key, child in item.items():
                child_path = f"{item_path}.{key}" if item_path else str(key)
                child_key_path = item_key_path + (key,)
                if isinstance(key, str) and key not in key_cache:
                    key_cache[key] = _is_sensitive_key(key)
                if (not isinstance(key, str)
                        or (key_cache[key]
                            and child_key_path not in schema_key_paths
                            and not (_inside_private(child_key_path, private_paths)
                                     and not _internal_clock_key(key))
                            and not (child_key_path in traffic_session_paths
                                     and _traffic_session_counters(key, child)))):
                    errors.append(f"sensitive:{child_path}")
                stack.append((child, child_path, child_key_path, depth + 1))
        elif isinstance(item, list):
            for index, child in enumerate(item):
                stack.append((
                    child, f"{item_path}[{index}]", item_key_path + (index,), depth + 1,
                ))
        elif isinstance(item, str):
            if _inside_private(item_key_path, private_paths):
                continue
            if item_key_path in resolver_paths and public_dns_resolver_text(item):
                continue  # T-P4-C1 #4: a DNS resolver address is not a credential.
            if item not in text_cache:
                text_cache[item] = _contains_sensitive_text(item)
            if text_cache[item]:
                surface = None
                if (item_key_path[-1:] in (("key",), ("title",), ("detail",), ("raw",))
                        and item_key_path[:-1] + ("key",) in schema_key_paths):
                    surface = "logs"
                elif any(item_key_path == prefix + ("detail",) or
                         item_key_path[:len(prefix) + 1] == prefix + ("params",)
                         for prefix in error_prefixes):
                    surface = "error"
                if surface:
                    privacy_diagnostics.append({"code": "free_text_sensitive", "surface": surface})
                else:
                    errors.append(f"sensitive:{item_path}")


def _common(obj: Any, expected: str, errors: list[str]) -> bool:
    if not isinstance(obj, dict):
        errors.append("type:frame")
        return False
    _need(obj, "t", "", lambda v: v == expected, errors, enum=True)
    _need(obj, "epoch", "", _string, errors)
    _need(obj, "produced_at_ms", "", _integer, errors)
    _need(obj, "sent_at_ms", "", _integer, errors)
    return True


def _error_payload(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"type:{path}")
        return
    if set(value) - {"code", "message_key", "params", "field", "hold", "detail", "reason_code"}:
        errors.append(f"unknown:{path}")
    _optional(value, "reason_code", path, _string, errors)
    _need(value, "code", path, lambda v: _enum(v, ERROR_CODES), errors, enum=True)
    _need(value, "message_key", path,
          lambda v: isinstance(v, str) and bool(_MESSAGE_KEY.fullmatch(v)), errors)
    _need(value, "params", path, lambda v: isinstance(v, dict), errors)
    _optional(value, "field", path, _string, errors)
    _optional(value, "hold", path, lambda v: isinstance(v, bool), errors)
    _optional(value, "detail", path, _string, errors)
    _sensitive(
        value, path, errors,
        schema_key_paths=frozenset(_error_schema_key_paths(value)),
    )


def _recovery(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"type:{path}")
        return
    for domain in ("proxy", "firewall"):
        item = _nested(value, domain, path, errors)
        if item is None:
            continue
        prefix = f"{path}.{domain}"
        _need(item, "state", prefix, lambda v: _enum(v, RECOVERY_STATES),
              errors, enum=True)
        _need(item, "error_code", prefix, _nullable_string, errors)
        _need(item, "remaining_count", prefix, _nullable_integer, errors)
        if item.get("state") == "unknown" and item.get("remaining_count") is not None:
            errors.append(f"invariant:{prefix}.remaining_count")


def validate_hello(obj: Any) -> list[str]:
    errors: list[str] = []
    if not _common(obj, "hello", errors):
        return errors
    _need(obj, "proto", "", lambda v: _integer(v) and v == PROTOCOL_VERSION,
          errors, enum=True)
    _need(obj, "app_version", "", _string, errors)
    _need(obj, "runtime_pid", "", _positive, errors)
    _need(obj, "lang", "", _string, errors)
    caps = _nested(obj, "caps", "", errors)
    if caps is not None:
        for name in ("commands", "topics", "features"):
            _need(caps, name, "caps", _string_list, errors)
        # T-P2-31b: v1 capability names. Queries share req.cmd, so they are
        # announced in caps.commands; host.* never runs in Python.
        if isinstance(caps.get("commands"), list) and any(
            not _enum(command, HELLO_CAP_COMMANDS) for command in caps["commands"]
        ):
            errors.append("enum:caps.commands")
        if isinstance(caps.get("topics"), list) and any(
            not _enum(topic, V1_TOPICS) for topic in caps["topics"]
        ):
            errors.append("enum:caps.topics")
        # T-P4-C1 (决定_第四波裁定 02a-4): feature names are B-item IDs such as
        # ``B-31`` or the sub-item form ``B-30a`` (same as the front-end list).
        if isinstance(caps.get("features"), list) and any(
            not isinstance(feature, str) or not _FEATURE_NAME.fullmatch(feature)
            for feature in caps["features"]
        ):
            errors.append("enum:caps.features")
        for name in ("commands", "topics", "features"):
            if isinstance(caps.get(name), list) and len(caps[name]) != len(set(
                    item for item in caps[name] if isinstance(item, str))):
                errors.append(f"invariant:caps.{name}")
    scope = _nested(obj, "scope", "", errors)
    if scope is not None:
        _need(scope, "kind", "scope", lambda v: _enum(v, {"default", "audit", "custom"}),
              errors, enum=True)
        _need(scope, "id", "scope", _string, errors)
    recovery = _need(obj, "network_recovery", "", lambda v: isinstance(v, dict), errors)
    if recovery is not None:
        _recovery(recovery, "network_recovery", errors)
    _optional(obj, "init_error", "", lambda v: v is None or isinstance(v, dict), errors)
    if isinstance(obj.get("init_error"), dict):
        _error_payload(obj["init_error"], "init_error", errors)
    return errors


def validate_hello_ack(obj: Any) -> list[str]:
    errors: list[str] = []
    if not _common(obj, "hello_ack", errors):
        return errors
    _need(obj, "proto", "", _integer, errors)
    _need(obj, "accepted", "", lambda v: isinstance(v, bool), errors)
    if obj.get("accepted") is True and obj.get("proto") != PROTOCOL_VERSION:
        errors.append("enum:proto")
    if obj.get("accepted") is False:
        error = _need(obj, "error", "", lambda v: isinstance(v, dict), errors)
        if error is not None:
            _error_payload(error, "error", errors)
    elif "error" in obj:
        errors.append("invariant:error")
    return errors


def validate_req(obj: Any) -> list[str]:
    errors: list[str] = []
    if not _common(obj, "req", errors):
        return errors
    _need(obj, "id", "", _request_id, errors)
    _optional(obj, "req_id", "", _string, errors)
    command = _need(obj, "cmd", "", lambda v: _enum(v, V1_COMMANDS | V1_QUERIES), errors, enum=True)
    args = _need(obj, "args", "", lambda v: isinstance(v, dict), errors)
    _need(obj, "from", "", lambda v: _enum(v, {"page", "shell"}), errors, enum=True)
    _optional(obj, "owner", "", _string, errors)
    if _enum(command, SHELL_COMMANDS) and obj.get("from") == "page":
        errors.append("not_allowed:cmd")
    if isinstance(args, dict):
        if command in V1_COMMANDS:
            _schema(args, V1_COMMAND_SPECS[command][4], "args", errors)
            _command_invariants(command, args, errors)
        elif command in V1_QUERIES:
            _schema(args, V1_QUERY_SPECS[command][0], "args", errors)
    if command is not None and command not in P1_COMMANDS:
        _need(obj, "req_id", "", _string, errors)
        _need(obj, "owner", "", _string, errors)
    return errors


def validate_ack(obj: Any) -> list[str]:
    errors: list[str] = []
    if not _common(obj, "ack", errors):
        return errors
    _need(obj, "id", "", _request_id, errors)
    _optional(obj, "req_id", "", _string, errors)
    _need(obj, "accepted", "", lambda v: isinstance(v, bool), errors)
    if obj.get("accepted") is True:
        # owner_revoke confirms control work without allocating a job op.
        _optional(obj, "op", "", _string, errors)
    elif obj.get("accepted") is False:
        if "op" in obj:
            errors.append("invariant:op")
        if "skipped_reason" in obj:
            _need(obj, "skipped_reason", "", _string, errors)
        elif "error" in obj:
            error = _need(obj, "error", "", lambda v: isinstance(v, dict), errors)
            if error is not None:
                _error_payload(error, "error", errors)
        else:
            errors.append("missing:skipped_reason|error")
    _optional(obj, "owner", "", _string, errors)
    return errors


def validate_res(obj: Any) -> list[str]:
    errors: list[str] = []
    if not _common(obj, "res", errors):
        return errors
    _need(obj, "id", "", _request_id, errors)
    _optional(obj, "req_id", "", _string, errors)
    _optional(obj, "owner", "", _string, errors)
    _need(obj, "ok", "", lambda v: isinstance(v, bool), errors)
    if "cmd" in obj:
        command = _need(obj, "cmd", "", lambda v: _enum(v, V1_COMMANDS | V1_QUERIES), errors, enum=True)
        _need(obj, "owner", "", _string, errors)
        _need(obj, "req_id", "", _string, errors)
        if obj.get("ok") is True and command is not None:
            spec = (V1_QUERY_SPECS[command][1] if command in V1_QUERIES
                    else V1_COMMAND_SPECS[command][5])
            _schema(obj.get("data"), spec, "data", errors)
            if command == "config.snapshot" and isinstance(obj.get("data"), dict) and _contains_hidden_setting(obj["data"].get("values")):
                errors.append("not_allowed:data.node_judgement")
            data = obj.get("data")
            if isinstance(data, dict):
                if command == "settings.validate" and isinstance(data.get("errors"), list) and data.get("valid") is not (not data["errors"]):
                    errors.append("invariant:data.valid")
                if command == "host.saveFile" and (data.get("cancelled") is True) != (data.get("ref") is None):
                    errors.append("invariant:data.ref")
    if obj.get("ok") is True:
        _need(obj, "data", "", lambda v: isinstance(v, dict), errors)
        if "error" in obj:
            errors.append("invariant:error")
    elif obj.get("ok") is False:
        error = _need(obj, "error", "", lambda v: isinstance(v, dict), errors)
        if error is not None:
            _error_payload(error, "error", errors)
        if "data" in obj:
            errors.append("invariant:data")
    return errors


def validate_cancel(obj: Any) -> list[str]:
    errors: list[str] = []
    if not _common(obj, "cancel", errors):
        return errors
    _need(obj, "id", "", _request_id, errors)
    _need(obj, "op", "", _string, errors)
    _optional(obj, "req_id", "", _string, errors)
    _optional(obj, "owner", "", _string, errors)
    return errors


def validate_owner_revoke(obj: Any) -> list[str]:
    errors: list[str] = []
    if not _common(obj, "owner_revoke", errors):
        return errors
    _need(obj, "id", "", _request_id, errors)
    _need(obj, "owner", "", _string, errors)
    _optional(obj, "from", "", lambda v: v == "shell", errors, enum=True)
    if obj.get("from") == "page":
        errors.append("not_allowed:from")
    return errors


def validate_error(obj: Any) -> list[str]:
    errors: list[str] = []
    if not _common(obj, "error", errors):
        return errors
    _optional(obj, "id", "", _request_id, errors)
    _optional(obj, "req_id", "", _string, errors)
    _optional(obj, "owner", "", _string, errors)
    error = _need(obj, "error", "", lambda v: isinstance(v, dict), errors)
    if error is not None:
        _error_payload(error, "error", errors)
    return errors


def validate_ping(obj: Any) -> list[str]:
    errors: list[str] = []
    if _common(obj, "ping", errors):
        _need(obj, "n", "", _integer, errors)
    return errors


def validate_pong(obj: Any) -> list[str]:
    errors: list[str] = []
    if _common(obj, "pong", errors):
        _need(obj, "n", "", _integer, errors)
    return errors


def validate_fatal(obj: Any) -> list[str]:
    errors: list[str] = []
    if not _common(obj, "fatal", errors):
        return errors
    _need(obj, "where", "", _string, errors)
    error = _need(obj, "error", "", lambda v: isinstance(v, dict), errors)
    if error is not None:
        _error_payload(error, "error", errors)
    return errors


def validate_bye(obj: Any) -> list[str]:
    errors: list[str] = []
    if not _common(obj, "bye", errors):
        return errors
    _need(obj, "exit_code", "", _integer, errors)
    recovery = _need(obj, "network_recovery", "", lambda v: isinstance(v, dict), errors)
    if recovery is not None:
        _recovery(recovery, "network_recovery", errors)
    return errors


def validate_service_state(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["type:service.state"]
    intent = _nested(value, "intent", "service.state", errors)
    if intent is not None:
        for name in ("desired_running", "stop_requested"):
            _need(intent, name, "service.state.intent", lambda v: isinstance(v, bool), errors)
        for name in ("lifecycle_revision", "runtime_generation"):
            _need(intent, name, "service.state.intent", _integer, errors)
    runtime = _nested(value, "runtime", "service.state", errors)
    if runtime is not None:
        for name in ("runtime_running", "shutdown_cleanup_pending"):
            _need(runtime, name, "service.state.runtime", lambda v: isinstance(v, bool), errors)
        _need(runtime, "startup_phase", "service.state.runtime",
              lambda v: _enum(v, {"stopping", "failed", "stopped", "running", "core_standby", "collecting"}),
              errors, enum=True)
        _need(runtime, "failure_code", "service.state.runtime", _nullable_string, errors)
        if "failure" in runtime:
            _schema(runtime["failure"], "Error?", "service.state.runtime.failure", errors)
            if isinstance(runtime["failure"], dict) and runtime["failure"].get("code") != runtime.get("failure_code"):
                errors.append("invariant:service.state.runtime.failure_code")
    core = _nested(value, "core", "service.state", errors)
    if core is not None:
        for name in ("running", "cleanup_pending"):
            _need(core, name, "service.state.core", lambda v: isinstance(v, bool), errors)
        _need(core, "pid", "service.state.core", _nullable_integer, errors)
        for name in ("process_generation", "core_generation"):
            _need(core, name, "service.state.core", _integer, errors)
    readiness = _nested(value, "readiness", "service.state", errors)
    if readiness is not None:
        _need(readiness, "state", "service.state.readiness",
              lambda v: _enum(v, READINESS_STATES), errors, enum=True)
        _need(readiness, "validation_active", "service.state.readiness",
              lambda v: isinstance(v, bool), errors)
        _need(readiness, "reason", "service.state.readiness", lambda v: isinstance(v, str), errors)
        _need(readiness, "remaining_ms", "service.state.readiness", _nullable_integer, errors)
        for name in ("publication_revision", "lifecycle_revision", "route_epoch", "network_context_epoch"):
            _need(readiness, name, "service.state.readiness", _integer, errors)
    freshness = _nested(value, "freshness", "service.state", errors)
    if freshness is not None:
        for name in ("sampled_at_ms", "snapshot_revision"):
            _need(freshness, name, "service.state.freshness", _integer, errors)
        _need(freshness, "uptime_s", "service.state.freshness", _nullable_integer, errors)
        _need(freshness, "stale", "service.state.freshness",
              lambda v: isinstance(v, bool), errors)
    if isinstance(runtime, dict) and isinstance(readiness, dict) and isinstance(freshness, dict):
        if (runtime.get("startup_phase") == "failed" and freshness.get("stale") is False and
                readiness.get("state") in ("ready", "serving")):
            errors.append("invariant:service.state.failed_readiness")
        failure = runtime.get("failure")
        if (isinstance(failure, dict) and failure.get("reason_code") in ("core_missing", "core_not_running") and
                isinstance(core, dict) and core.get("running") is True and freshness.get("stale") is False):
            errors.append("invariant:service.state.failed_core")
    return errors


def validate_route_published(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["type:route.published"]
    _need(value, "publication_revision", "route.published", _integer, errors)
    owner = _need(value, "owner", "route.published",
                  lambda v: v is None or isinstance(v, dict), errors)
    if isinstance(owner, dict):
        for name in ("process_pid", "process_generation", "core_generation", "route_epoch", "network_context_epoch"):
            _need(owner, name, "route.published.owner", _integer, errors)
        _need(owner, "semantic_digest", "route.published.owner", _string, errors)
    _need(value, "selected_chain", "route.published", _string_list, errors)
    _need(value, "leaf", "route.published", _nullable_string, errors)
    _need(value, "managed_tier", "route.published",
          lambda v: v is None or _enum(v, _TIERS), errors, enum=True)
    _need(value, "observed_at_ms", "route.published", _integer, errors)
    _need(value, "valid", "route.published", lambda v: isinstance(v, bool), errors)
    _need(value, "invalid_reason", "route.published", _nullable_string, errors)
    _need(value, "fail_closed_confirmed", "route.published",
          lambda v: isinstance(v, bool), errors)
    if "owner" in value and value["owner"] is None:
        if (value.get("valid") is not False or value.get("selected_chain") != []
                or value.get("leaf") is not None or value.get("managed_tier") is not None
                or value.get("invalid_reason") != "no_published_route"):
            errors.append("invariant:route.published.owner")
    return errors


def validate_tiers_summary(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["type:tiers.summary"]
    _need(value, "carrier", "tiers.summary",
          lambda v: v is None or _enum(v, _TIERS), errors, enum=True)
    _need(value, "offline_reason", "tiers.summary", _nullable_string, errors)
    _need(value, "order", "tiers.summary",
          lambda v: isinstance(v, list) and all(_enum(vv, _TIERS) for vv in v), errors)
    for name in ("degraded", "fallback"):
        _need(value, name, "tiers.summary", lambda v: isinstance(v, bool), errors)
    tiers = _nested(value, "tiers", "tiers.summary", errors)
    if tiers is not None:
        for tier in ("vanguard", "sentinel", "bastion", "gateway"):
            item = _nested(tiers, tier, "tiers.summary.tiers", errors)
            if item is None:
                continue
            _need(item, "enabled", f"tiers.summary.tiers.{tier}",
                  lambda v: isinstance(v, bool), errors)
            for name in ("total", "usable"):
                _need(item, name, f"tiers.summary.tiers.{tier}", _integer, errors)
            if _integer(item.get("total")) and _integer(item.get("usable")) and item["usable"] > item["total"]:
                errors.append(f"invariant:tiers.summary.tiers.{tier}.usable")
            # T-P4-C2 #3 (K-2) / #7 (G33): optional v1 additions.
            if "check_interval_s" in item and not _positive(item["check_interval_s"]):
                errors.append(f"type:tiers.summary.tiers.{tier}.check_interval_s")
            if "pending" in item and not _integer(item["pending"]):
                errors.append(f"type:tiers.summary.tiers.{tier}.pending")
            if set(item) - {"enabled", "total", "usable", "check_interval_s", "pending"}:
                errors.append(f"unknown:tiers.summary.tiers.{tier}")
    pool = _nested(value, "pool", "tiers.summary", errors)
    if pool is not None:
        for name in ("total", "usable"):
            _need(pool, name, "tiers.summary.pool", _integer, errors)
        if _integer(pool.get("total")) and _integer(pool.get("usable")) and pool["usable"] > pool["total"]:
            errors.append("invariant:tiers.summary.pool.usable")
    return errors


def validate_traffic_tick(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["type:traffic.tick"]
    _need(value, "t_ms", "traffic.tick", _integer, errors)
    for name in ("up_bps", "down_bps"):
        _need(value, name, "traffic.tick", _number, errors)
    session = _nested(value, "session", "traffic.tick", errors)
    if session is not None:
        for name in ("up_bytes", "down_bytes"):
            _need(session, name, "traffic.tick.session", _integer, errors)
    _need(value, "running", "traffic.tick", lambda v: isinstance(v, bool), errors)
    _need(value, "error_code", "traffic.tick", _nullable_string, errors)
    return errors


def validate_traffic_history(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["type:traffic.history"]
    _need(value, "sampled_at_ms", "traffic.history", _integer, errors)
    _need(value, "source", "traffic.history",
          lambda v: _enum(v, {"temporary_snapshot", "bucketed"}), errors, enum=True)
    for name, fields, maximum in (
        ("h24", ("t_ms", "down_bps", "up_bps", "sample_count"), 288),
        ("live", ("t_ms", "down_bps", "up_bps"), 300),
    ):
        rows = _need(value, name, "traffic.history",
                     lambda v: isinstance(v, list) and len(v) <= maximum, errors)
        if rows is None:
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                errors.append(f"type:traffic.history.{name}[{index}]")
                continue
            for field in fields:
                check = _number if field in {"down_bps", "up_bps"} else _integer
                _need(row, field, f"traffic.history.{name}[{index}]", check, errors)
    return errors


def validate_logs_batch(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["type:logs.batch"]
    _need(value, "from_seq", "logs.batch", _integer, errors)
    records = _need(value, "records", "logs.batch",
                    lambda v: isinstance(v, list) and len(v) <= 1000, errors)
    if records is not None:
        last_seq = None
        for index, row in enumerate(records):
            path = f"logs.batch.records[{index}]"
            if not isinstance(row, dict):
                errors.append(f"type:{path}")
                continue
            seq = _need(row, "seq", path, _integer, errors)
            for field in ("ts_ms", "rx_ms"):
                _need(row, field, path, _integer, errors)
            _need(row, "source", path,
                  lambda v: _enum(v, {"TESSERACT", "MIHOMO", "ALERT", "UNKNOWN"}), errors, enum=True)
            _need(row, "level", path,
                  lambda v: _enum(v, {"debug", "info", "warn", "error", "critical"}), errors, enum=True)
            _need(row, "category", path,
                  lambda v: _enum(v, {"runtime", "routing", "health", "circuit_breaker", "update", "configuration", "connection", "alert", "other"}), errors, enum=True)
            for field in ("key", "title", "detail", "raw"):
                _need(row, field, path, lambda v: isinstance(v, str), errors)
            # T-P4-C1 #2 (v1 addition): optional Log facts, see PROTOCOL §5.5/§16.
            _optional(row, "is_key_event", path, lambda v: isinstance(v, bool), errors)
            _optional(row, "run_id", path, _string, errors)
            if _integer(seq):
                if last_seq is not None and seq <= last_seq:
                    errors.append(f"invariant:{path}.seq")
                last_seq = seq
    for name in ("dropped", "dropped_total"):
        _need(value, name, "logs.batch", _integer, errors)
    _need(value, "gap", "logs.batch", lambda v: isinstance(v, bool), errors)
    if (_integer(value.get("dropped")) and _integer(value.get("dropped_total"))
            and value["dropped_total"] < value["dropped"]):
        errors.append("invariant:logs.batch.dropped_total")
    if value.get("gap") is False and _integer(value.get("dropped")) and value["dropped"]:
        errors.append("invariant:logs.batch.gap")
    _sensitive(
        value, "logs.batch", errors,
        schema_key_paths=frozenset(_log_schema_key_paths(value)),
    )
    return errors


def validate_core_info(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["type:core.info"]
    for name in ("running", "cleanup_pending"):
        _need(value, name, "core.info", lambda v: isinstance(v, bool), errors)
    _need(value, "pid", "core.info", _nullable_integer, errors)
    _need(value, "version", "core.info", _nullable_string, errors)
    _need(value, "generation", "core.info", _integer, errors)
    return errors


def _job_result(name: Any, result: Any, state: Any, errors: list[str]) -> None:
    path = "jobs.result"
    if not isinstance(result, dict):
        errors.append(f"type:{path}")
        return
    if name == "service.start":
        phase = _need(result, "startup_phase", path,
                      lambda v: _enum(v, {"collecting", "core_standby", "running",
                                               "stopping", "failed", "stopped"}),
                      errors, enum=True)
        _need(result, "failure_code", path, _nullable_string, errors)
        if state == "done" and (not _enum(phase, {"collecting", "core_standby", "running"})
                                or result.get("failure_code") is not None):
            errors.append(f"invariant:{path}.startup_phase")
    elif _enum(name, {"service.stop", "app.quit"}):
        phase = _need(result, "startup_phase", path,
                      lambda v: _enum(v, {"collecting", "core_standby", "running",
                                               "stopping", "failed", "stopped"}),
                      errors, enum=True)
        code = _need(result, "shutdown_exit_code", path, _nullable_integer, errors)
        _need(result, "cleanup_pending", path, lambda v: isinstance(v, bool), errors)
        if state == "done" and (phase != "stopped" or result.get("cleanup_pending") is not False
                                or code not in (None, 0)):
            errors.append(f"invariant:{path}.startup_phase")
    elif name == "service.restart":
        for field in ("applied", "core_running", "core_cleanup_pending"):
            _need(result, field, path, lambda v: isinstance(v, bool), errors)
        _need(result, "core_generation", path, _integer, errors)
        if state == "done" and (result.get("applied") is not True
                                or result.get("core_running") is not True
                                or result.get("core_cleanup_pending") is not False):
            errors.append(f"invariant:{path}.applied")
    elif _enum(name, V1_COMMANDS):
        _schema(result, V1_COMMAND_SPECS[name][5], path, errors)
        if state == "done":
            if result.get("applied") is False or result.get("ok") is False:
                errors.append(f"invariant:{path}.applied")
            for field in ("error", "apply_error"):
                if result.get(field) is not None:
                    errors.append(f"invariant:{path}.{field}")
            if name == "geo.update":
                geo = result.get("status")
                if (result.get("triggered") is not True or result.get("completed") is not True or
                        not isinstance(geo, dict) or geo.get("running") is not False or geo.get("errors") != []):
                    errors.append(f"invariant:{path}.completed")
            if name == "app.update.install" and (result.get("verified") is not True or result.get("handoff_pending") is not True):
                errors.append(f"invariant:{path}.handoff_pending")
            if name == "app.update.check" and result.get("state") not in ("idle", "available", "verified"):
                errors.append(f"invariant:{path}.state")
            if name == "nodes.diagnose" and (result.get("cleanup_pending") is not False or result.get("reason_code") is not None):
                errors.append(f"invariant:{path}.cleanup_pending")
            if name == "exit.verify" and (result.get("status") not in ("ok", "partial") or result.get("manual_execution_binding_stale") is not False):
                errors.append(f"invariant:{path}.status")
            if name == "subscriptions.refresh" and result.get("stored") is not True:
                errors.append(f"invariant:{path}.stored")
            # T-P4-C2 #9: a done deactivation ("") has no selected Gateway.
            if name == "gateway.select" and not (
                    _string(result.get("selection")) if result.get("selected") != ""
                    else result.get("selection") is None):
                errors.append(f"invariant:{path}.selection")
            if name in {"nodes.update", "nodes.delete", "import.apply", "mode.set"} and result.get("saved") is not True:
                errors.append(f"invariant:{path}.saved")
            if name == "import.apply" and isinstance(result.get("subscriptions"), list):
                if any(isinstance(item, dict) and (item.get("error") is not None or item.get("apply_error") is not None or item.get("applied") is not True) for item in result["subscriptions"]):
                    errors.append(f"invariant:{path}.subscriptions")
            if name == "settings.apply":
                sections = result.get("sections", [])
                if not isinstance(sections, list):
                    return
                if not sections:
                    errors.append(f"invariant:{path}.sections")
                if any(isinstance(section, dict) and
                       (not section.get("saved") or section.get("error") is not None)
                       for section in sections):
                    errors.append(f"invariant:{path}.sections")
                if (any(isinstance(section, dict) and not section.get("applied") for section in sections)
                        and not isinstance(result.get("pending"), dict)):
                    errors.append(f"invariant:{path}.pending")
                pending = result.get("pending")
                if isinstance(pending, dict) and pending.get("state") not in ("pending", "applying", "applied"):
                    errors.append(f"invariant:{path}.pending")


def validate_jobs(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["type:jobs"]
    _need(value, "op", "jobs", _string, errors)
    _optional(value, "req_id", "jobs", _string, errors)
    _optional(value, "owner", "jobs", _string, errors)
    _need(value, "name", "jobs", lambda v: _enum(v, frozenset(
        name for name, spec in V1_COMMAND_SPECS.items() if spec[2] == "job")), errors, enum=True)
    _need(value, "state", "jobs", lambda v: _enum(v, JOB_STATES), errors, enum=True)
    _need(value, "started_at_ms", "jobs", _nullable_integer, errors)
    _optional(value, "progress", "jobs", lambda v: v is None or isinstance(v, dict), errors)
    _optional(value, "result", "jobs", lambda v: v is None or isinstance(v, dict), errors)
    _optional(value, "error", "jobs", lambda v: v is None or isinstance(v, dict), errors)
    if isinstance(value.get("error"), dict):
        _error_payload(value["error"], "jobs.error", errors)
    state = value.get("state")
    result = value.get("result")
    error = value.get("error")
    if state == "done":
        if not isinstance(result, dict):
            errors.append("missing:jobs.result")
        else:
            _job_result(value.get("name"), result, state, errors)
        if error is not None:
            errors.append("invariant:jobs.error")
    elif state == "failed":
        if not isinstance(error, dict):
            errors.append("missing:jobs.error")
        if result is not None:
            _job_result(value.get("name"), result, state, errors)
    elif _enum(state, JOB_STATES):
        if result is not None:
            errors.append("invariant:jobs.result")
        if error is not None:
            errors.append("invariant:jobs.error")
    return errors


_TOPIC_VALIDATORS: dict[str, Callable[[Any], list[str]]] = {
    "service.state": validate_service_state,
    "route.published": validate_route_published,
    "tiers.summary": validate_tiers_summary,
    "traffic.tick": validate_traffic_tick,
    "traffic.history": validate_traffic_history,
    "logs.batch": validate_logs_batch,
    "core.info": validate_core_info,
    "jobs": validate_jobs,
}


def _prepare_exit_invariants(payload: dict, errors: list[str]) -> None:
    """Shell-only installer facts (T-P2-27): drive-letter path, digest, switches."""
    path, version = payload.get("installer_path"), payload.get("version")
    if isinstance(path, str) and isinstance(version, str):
        windows = PureWindowsPath(path)
        # Drive-letter only: no UNC share, no \\?\ or \\.\ device prefix.
        if (not re.fullmatch(r"[A-Za-z]:", windows.drive) or not windows.is_absolute()
                or ".." in windows.parts
                or windows.name != f"Tesseract-Setup-{version}-win-x64.exe"):
            errors.append("invariant:update.prepareExit.installer_path")
    if isinstance(payload.get("sha256"), str) and not re.fullmatch(r"[0-9a-f]{64}", payload["sha256"]):
        errors.append("invariant:update.prepareExit.sha256")
    args = payload.get("args")
    # Installer switches only (never a path or shell text): "/UPPER_CASE".
    if isinstance(args, list) and (not 0 < len(args) <= 16 or not all(
            isinstance(item, str) and re.fullmatch(r"/[A-Z][A-Z0-9_]{0,31}", item) for item in args)):
        errors.append("invariant:update.prepareExit.args")


def validate_topic_payload(topic: str, payload: Any) -> list[str]:
    """Return stable error codes; never raise for an invalid payload."""
    validator = _TOPIC_VALIDATORS.get(topic) if isinstance(topic, str) else None
    if validator is None and _enum(topic, frozenset(V1_TOPIC_SPECS)):
        errors: list[str] = []
        _schema(payload, V1_TOPIC_SPECS[topic][2], topic, errors)
        if isinstance(payload, dict):
            if topic == "service.collect" and isinstance(payload.get("tiers"), list):
                rows = payload["tiers"]
                if (len(rows) != 4 or {row.get("tier") for row in rows if isinstance(row, dict) and isinstance(row.get("tier"), str)} != _TIERS or
                        any(isinstance(row, dict) and row.get("generation") != payload.get("generation") for row in rows)):
                    errors.append("invariant:service.collect.tiers")
            if topic == "logs.feed" and isinstance(payload.get("dropped"), dict):
                dropped = payload["dropped"]
                parts = [dropped.get(level) for level in ("debug", "info", "warn", "error", "critical")]
                if all(_integer(part) for part in parts) and dropped.get("total") != sum(parts):
                    errors.append("invariant:logs.feed.dropped")
            if topic == "gateway.current" and payload.get("applied") is True and (not _string(payload.get("effective")) or payload.get("missing") is True):
                errors.append("invariant:gateway.current.effective")
            # T-P2-H8: the Node.id of the saved selection; it exists only with a
            # loaded selection (selected set, not missing) and is an opaque id.
            if topic == "gateway.current" and "selected_node_id" in payload and (
                    not _string(payload.get("selected")) or payload.get("missing") is not False
                    or not re.fullmatch(r"[0-9a-f]{24}", str(payload.get("selected_node_id")))):
                errors.append("invariant:gateway.current.selected_node_id")
            if topic == "update.prepareExit":
                _prepare_exit_invariants(payload, errors)
        _sensitive(payload, topic, errors, schema_key_paths=frozenset(
            _schema_metadata_paths(payload, V1_TOPIC_SPECS[topic][2])))
        return errors
    if validator is None:
        return ["enum:topic"]
    errors = validator(payload)
    if topic == "logs.batch":
        return errors  # Its validator has scanned the whole payload already.
    allowed = frozenset({("session",)}) if topic == "traffic.tick" else frozenset()
    _sensitive(
        payload, topic, errors,
        traffic_session_paths=allowed,
        schema_key_paths=frozenset(_topic_schema_key_paths(topic, payload)),
    )
    return errors


def _patch_lists_valid(changed: Any, removed: Any) -> bool:
    if (not isinstance(changed, list) or not isinstance(removed, list)
            or any(not isinstance(item, dict) or not _string(item.get("id"))
                   for item in changed)
            or not _string_list(removed)):
        return False
    change_ids = [item["id"] for item in changed]
    return (len(change_ids) == len(set(change_ids))
            and len(removed) == len(set(removed))
            and not set(change_ids).intersection(removed))


def validate_evt(obj: Any) -> list[str]:
    errors: list[str] = []
    if not _common(obj, "evt", errors):
        return errors
    topic = _need(obj, "topic", "", lambda v: _enum(v, V1_TOPICS), errors, enum=True)
    _topic_key(obj, topic, errors)
    rev = _need(obj, "rev", "", _positive, errors)
    kind = _need(obj, "kind", "",
                 lambda v: _enum(v, {"snapshot", "patch", "edge", "gap"}), errors, enum=True)
    data = _need(obj, "data", "", lambda v: isinstance(v, dict), errors)
    if topic in TOPIC_KINDS and kind is not None and kind not in TOPIC_KINDS[topic]:
        errors.append("not_allowed:topic.kind")
    if kind != "patch" and "base_rev" in obj:
        errors.append("not_allowed:base_rev")
    if kind not in {"edge", "gap"} and "seq" in obj:
        errors.append("not_allowed:seq")
    if kind == "patch":
        base_rev = _need(obj, "base_rev", "", _integer, errors)
        if _integer(base_rev) and _integer(rev) and base_rev >= rev:
            errors.append("invariant:base_rev")
        if isinstance(data, dict):
            if set(data) - {"changed", "removed"}:
                errors.append("unknown:data")
            changed = _need(data, "changed", "data", lambda v: isinstance(v, list), errors)
            removed = _need(data, "removed", "data", lambda v: isinstance(v, list), errors)
            if isinstance(changed, list) and isinstance(removed, list) and not _patch_lists_valid(changed, removed):
                errors.append("invariant:data")
            if topic in V1_TOPIC_SPECS and V1_TOPIC_SPECS[topic][1] in {"list", "keyed"}:
                _schema({"items": changed}, V1_TOPIC_SPECS[topic][2], "data", errors)
    elif kind == "gap":
        seq = _need(obj, "seq", "", _positive, errors)
        if isinstance(data, dict):
            start = _need(data, "from_seq", "data", _positive, errors)
            end = _need(data, "to_seq", "data", _positive, errors)
            dropped = _need(data, "dropped", "data", _positive, errors)
            if all(_positive(item) for item in (seq, start, end, dropped)):
                if end < start or dropped != end - start + 1 or seq != end + 1:
                    errors.append("invariant:data.gap")
    elif kind == "edge":
        _need(obj, "seq", "", _positive, errors)
        if topic is not None and data is not None:
            errors.extend(validate_topic_payload(topic, data))
    elif kind == "snapshot" and topic is not None and data is not None:
        errors.extend(validate_topic_payload(topic, data))
        if topic in V1_TOPIC_SPECS and V1_TOPIC_SPECS[topic][1] in {"list", "keyed"}:
            if not _patch_lists_valid(data.get("items"), []):
                errors.append("invariant:data.items")
    if topic in KEYED_TOPICS and isinstance(data, dict) and kind in {"snapshot", "patch"}:
        rows = data.get("items" if kind == "snapshot" else "changed")
        if isinstance(rows, list) and any(isinstance(row, dict) and row.get("tier") != obj.get("key") for row in rows):
            errors.append("invariant:data.tier")
    if "page" in obj:
        _schema(obj["page"], {"snapshot_id": "str", "index": "uint", "complete": "bool", "next_cursor": "str?"}, "page", errors)
        if kind != "snapshot" or topic not in V1_TOPIC_SPECS or V1_TOPIC_SPECS[topic][1] not in {"list", "keyed"}:
            errors.append("not_allowed:page")
        page = obj["page"]
        if isinstance(page, dict) and (page.get("complete") is True) != (page.get("next_cursor") is None):
            errors.append("invariant:page.next_cursor")
    return errors


def validate_batch(obj: Any) -> list[str]:
    errors: list[str] = []
    if not _common(obj, "batch", errors):
        return errors
    _need(obj, "tick_seq", "", _positive, errors)
    items = _need(obj, "items", "", lambda v: isinstance(v, list) and bool(v), errors)
    if items is not None:
        for index, item in enumerate(items):
            for error in validate_evt(item):
                errors.append(f"items[{index}].{error}")
            if isinstance(item, dict) and item.get("epoch") != obj.get("epoch"):
                errors.append(f"epoch:items[{index}]")
    return errors


def _topics_list(value: Any) -> bool:
    return (isinstance(value, list) and bool(value)
            and all(_enum(item, V1_TOPICS) for item in value)
            and len(value) == len(set(value)))


def validate_subscribe(obj: Any) -> list[str]:
    errors: list[str] = []
    if _common(obj, "subscribe", errors):
        _need(obj, "id", "", _request_id, errors)
        _need(obj, "topics", "", _topics_list, errors)
    return errors


def validate_subscribed(obj: Any) -> list[str]:
    errors: list[str] = []
    if _common(obj, "subscribed", errors):
        _need(obj, "id", "", _request_id, errors)
        _need(obj, "topics", "", _topics_list, errors)
    return errors


def validate_snapshot_req(obj: Any) -> list[str]:
    errors: list[str] = []
    if _common(obj, "snapshot_req", errors):
        _need(obj, "id", "", _request_id, errors)
        _need(obj, "topic", "", lambda v: _enum(v, V1_SNAPSHOT_TOPICS), errors,
              enum=True)
        _topic_key(obj, obj.get("topic"), errors)
        _optional(obj, "cursor", "", _string, errors)
        if "cursor" in obj and obj.get("topic") not in KEYED_TOPICS | {"config.items", "bastion.sources"}:
            errors.append("not_allowed:cursor")
    return errors


def validate_snapshot_res(obj: Any) -> list[str]:
    errors: list[str] = []
    if not _common(obj, "snapshot_res", errors):
        return errors
    _need(obj, "id", "", _request_id, errors)
    topic = _need(obj, "topic", "", lambda v: _enum(v, V1_SNAPSHOT_TOPICS),
                  errors, enum=True)
    if "snapshot" in obj and "error" in obj:
        errors.append("invariant:snapshot|error")
    elif "snapshot" in obj:
        snapshot = _need(obj, "snapshot", "", lambda v: isinstance(v, dict), errors)
        if isinstance(snapshot, dict):
            errors.extend(f"snapshot.{error}" for error in validate_evt(snapshot))
            if (snapshot.get("topic") != topic or snapshot.get("kind") != "snapshot"
                    or snapshot.get("epoch") != obj.get("epoch") or snapshot.get("key") != obj.get("key")):
                errors.append("invariant:snapshot")
    elif "error" in obj:
        error = _need(obj, "error", "", lambda v: isinstance(v, dict), errors)
        if isinstance(error, dict):
            _error_payload(error, "error", errors)
    else:
        errors.append("missing:snapshot|error")
    _topic_key(obj, topic, errors)
    return errors


_FRAME_VALIDATORS: dict[str, Callable[[Any], list[str]]] = {
    "hello": validate_hello,
    "hello_ack": validate_hello_ack,
    "req": validate_req,
    "ack": validate_ack,
    "res": validate_res,
    "evt": validate_evt,
    "batch": validate_batch,
    "subscribe": validate_subscribe,
    "subscribed": validate_subscribed,
    "snapshot_req": validate_snapshot_req,
    "snapshot_res": validate_snapshot_res,
    "cancel": validate_cancel,
    "owner_revoke": validate_owner_revoke,
    "error": validate_error,
    "ping": validate_ping,
    "pong": validate_pong,
    "fatal": validate_fatal,
    "bye": validate_bye,
}


def validate_frame(obj: Any) -> list[str]:
    """Validate an entire v1 frame, including forbidden sensitive material."""
    if not isinstance(obj, dict):
        return ["type:frame"]
    validator = _FRAME_VALIDATORS.get(obj.get("t")) if isinstance(obj.get("t"), str) else None
    if validator is None:
        errors = ["missing:t"] if "t" not in obj else ["enum:t"]
    else:
        errors = validator(obj)
    if _integer(obj.get("produced_at_ms")):
        active_expiry = None
        if obj.get("t") == "req" and obj.get("cmd") == "shell.saveGrant" and isinstance(obj.get("args"), dict):
            active_expiry = obj["args"].get("expires_at_ms")
        elif obj.get("t") == "res" and obj.get("cmd") == "import.preview" and isinstance(obj.get("data"), dict):
            active_expiry = obj["data"].get("expires_at_ms")
        elif obj.get("t") == "evt" and obj.get("topic") == "update.prepareExit" and isinstance(obj.get("data"), dict):
            active_expiry = obj["data"].get("expires_at_ms")
        if _integer(active_expiry) and active_expiry <= obj["produced_at_ms"]:
            errors.append("invariant:expires_at_ms")
    if obj.get("t") == "res" and obj.get("cmd") == "import.preview" and isinstance(obj.get("data"), dict):
        _import_groups_invariant(obj["data"], errors)
    allowed: set[tuple[str | int, ...]] = set()
    if obj.get("t") == "evt" and obj.get("topic") == "traffic.tick":
        allowed.add(("data", "session"))
    elif obj.get("t") == "batch" and isinstance(obj.get("items"), list):
        allowed.update(
            ("items", index, "data", "session")
            for index, item in enumerate(obj["items"])
            if isinstance(item, dict) and item.get("topic") == "traffic.tick"
        )
    elif obj.get("t") == "snapshot_res" and isinstance(obj.get("snapshot"), dict):
        if obj["snapshot"].get("topic") == "traffic.tick":
            allowed.add(("snapshot", "data", "session"))
    _sensitive(
        obj, "", errors,
        traffic_session_paths=frozenset(allowed),
        schema_key_paths=_frame_schema_key_paths(obj),
        # Each logs.batch payload was validated/scanned by validate_evt above;
        # scan its enclosing frame/extensions without repeating every record.
        scanned_log_paths=frozenset(_validated_log_payload_paths(obj)),
        private_paths=_private_frame_paths(obj),
        resolver_paths=_dns_resolver_frame_paths(obj),
    )
    return errors


def validate_control_reply(request: Any, reply: Any) -> list[str]:
    """Check shape and correlation for one P1 subscription or snapshot reply."""
    errors = [f"request.{item}" for item in validate_frame(request)]
    errors.extend(f"reply.{item}" for item in validate_frame(reply))
    if not isinstance(request, dict) or not isinstance(reply, dict):
        return errors
    request_type = request.get("t")
    if request_type not in ("subscribe", "snapshot_req", "owner_revoke"):
        errors.append("enum:request.t")
        return errors
    allowed_replies = (("subscribed", "error") if request_type == "subscribe"
                       else ("ack",) if request_type == "owner_revoke"
                       else ("snapshot_res",))
    if reply.get("t") not in allowed_replies:
        errors.append("enum:reply.t")
    if reply.get("id") != request.get("id"):
        errors.append("invariant:reply.id")
    if reply.get("epoch") != request.get("epoch"):
        errors.append("invariant:reply.epoch")
    if request_type == "owner_revoke" and "op" in reply:
        errors.append("invariant:reply.op")
    if request_type == "subscribe" and reply.get("t") == "subscribed":
        if reply.get("topics") != request.get("topics"):
            errors.append("invariant:reply.topics")
    if request_type == "snapshot_req" and reply.get("t") == "snapshot_res":
        if reply.get("topic") != request.get("topic"):
            errors.append("invariant:reply.topic")
        if reply.get("key") != request.get("key"):
            errors.append("invariant:reply.key")
    return errors


def validate_response(request: Any, reply: Any = None) -> list[str]:
    """Validate private/unicast response ownership; caller also checks live owner."""
    errors = [f"request.{error}" for error in validate_frame(request)]
    errors.extend(f"reply.{error}" for error in validate_frame(reply))
    if not isinstance(request, dict) or not isinstance(reply, dict):
        return errors
    if request.get("t") != "req" or reply.get("t") not in ("res", "ack", "error"):
        errors.append("invariant:reply.t")
    for field in ("epoch", "id", "req_id", "owner"):
        if request.get(field) != reply.get(field):
            errors.append(f"invariant:reply.{field}")
    cmd = request.get("cmd")
    if reply.get("t") == "res":
        if reply.get("cmd") != cmd:
            errors.append("invariant:reply.cmd")
        if _enum(cmd, V1_COMMANDS) and V1_COMMAND_SPECS[cmd][2] == "job":
            errors.append("invariant:reply.mode")
    elif reply.get("t") == "ack" and (
            _enum(cmd, V1_QUERIES) or _enum(cmd, V1_COMMANDS) and V1_COMMAND_SPECS[cmd][2] != "job"):
        errors.append("invariant:reply.mode")
    if (reply.get("t") == "ack" and reply.get("accepted") is True
            and not _string(reply.get("op"))):
        errors.append("missing:reply.op")
    return errors


def validate_page_request(request: Any) -> list[str]:
    """Outer-shell ingress guard; never trust a page's claimed `from`."""
    errors = validate_frame(request)
    if not isinstance(request, dict):
        return errors
    if request.get("t") == "req":
        if request.get("from") != "page" or not _enum(request.get("cmd"), PAGE_COMMANDS | V1_QUERIES):
            errors.append("not_allowed:page")
    elif request.get("t") in ("subscribe", "snapshot_req"):
        topics = request.get("topics", [request.get("topic")])
        if not isinstance(topics, list) or any(_enum(topic, SHELL_TOPICS) for topic in topics):
            errors.append("not_allowed:page.topics")
    elif request.get("t") != "cancel":
        errors.append("not_allowed:page.frame")
    return errors


def apply_topic_patch(baseline: Any, patch: Any) -> tuple[dict | None, bool]:
    """P2-07 reference oracle; assembly/cache ownership remains out of scope."""
    if (validate_frame(baseline) or validate_frame(patch) or
            baseline.get("t") != "evt" or patch.get("t") != "evt" or
            baseline.get("kind") != "snapshot" or patch.get("kind") != "patch"):
        return None, True
    if any(baseline.get(field) != patch.get(field) for field in ("epoch", "topic", "key")):
        return None, True
    if isinstance(baseline.get("page"), dict) and (not baseline["page"].get("complete") or baseline["page"].get("index") != 0):
        return None, True
    merged, missing = apply_patch({"rev": baseline["rev"], **baseline["data"]},
                                 {"rev": patch["rev"], "base_rev": patch["base_rev"], **patch["data"]})
    if missing:
        return None, True
    result = deepcopy(baseline)
    result.update(rev=merged["rev"], data={"items": merged["items"]},
                  produced_at_ms=patch["produced_at_ms"], sent_at_ms=patch["sent_at_ms"])
    result.pop("page", None)
    return result, False


def validate_snapshot_pages(pages: Any) -> list[str]:
    """Reference validation of a completed frozen page group, not a cache."""
    if not isinstance(pages, list) or not pages:
        return ["type:pages"]
    errors: list[str] = []
    first = pages[0]
    ids: set[str] = set()
    if not isinstance(first, dict) or not isinstance(first.get("page"), dict):
        return ["missing:pages.page"]
    for index, frame in enumerate(pages):
        errors.extend(f"pages[{index}].{error}" for error in validate_frame(frame))
        if not isinstance(frame, dict) or not isinstance(frame.get("page"), dict):
            errors.append("missing:pages.page")
            continue
        page = frame["page"]
        if (any(frame.get(field) != first.get(field) for field in ("epoch", "topic", "key", "rev")) or
                page.get("snapshot_id") != first["page"].get("snapshot_id") or
                page.get("index") != index or page.get("complete") is not (index == len(pages) - 1)):
            errors.append("invariant:pages.baseline")
        rows = frame.get("data", {}).get("items", []) if isinstance(frame.get("data"), dict) else []
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and isinstance(row.get("id"), str):
                    if row["id"] in ids:
                        errors.append("invariant:pages.ids")
                    ids.add(row["id"])
    return errors


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_key")
        value[key] = item
    return value


def _bad_constant(value: str) -> None:
    raise ValueError(f"bad_json:{value}")


def encode_frame(obj: Any) -> bytes:
    """Encode one validated frame, including its LF delimiter."""
    errors = validate_frame(obj)
    if errors:
        raise ValueError("invalid_frame:" + ",".join(errors))
    try:
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("bad_json") from exc
    if len(data) > HARD_FRAME_LIMIT:
        raise ValueError("frame_too_large")
    return data


def decode_line(line: bytes) -> dict[str, Any]:
    """Decode one complete NDJSON line; schema validation is a separate step."""
    if not isinstance(line, bytes):
        raise ValueError("type:line")
    if len(line) > HARD_FRAME_LIMIT:
        raise ValueError("frame_too_large")
    if line.endswith(b"\n"):
        line = line[:-1]
    if not line or b"\n" in line or b"\r" in line:
        raise ValueError("bad_line")
    try:
        text = line.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid_utf8") from exc
    try:
        obj = json.loads(text, object_pairs_hook=_no_duplicate_keys,
                         parse_constant=_bad_constant)
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid_json") from exc
    if not isinstance(obj, dict):
        raise ValueError("not_object")
    return obj


def apply_patch(
    baseline: Any, patch: Any,
) -> tuple[dict[str, Any] | None, bool]:
    """Merge by id only when receiver rev equals base_rev.

    Returns ``(None, True)`` when the receiver must request a replacement
    snapshot.  Inputs are never modified.
    """
    if not isinstance(baseline, dict) or not isinstance(patch, dict):
        return None, True
    current_rev = baseline.get("rev")
    base_rev = patch.get("base_rev")
    new_rev = patch.get("rev")
    old_items = baseline.get("items")
    changed = patch.get("changed")
    removed = patch.get("removed")
    if (not _integer(current_rev) or not _integer(base_rev)
            or not _integer(new_rev) or base_rev != current_rev
            or new_rev <= base_rev or not isinstance(old_items, list)
            or not isinstance(changed, list) or not isinstance(removed, list)):
        return None, True
    if (any(not isinstance(item, dict) or not _string(item.get("id"))
            for item in old_items)
            or not _patch_lists_valid(changed, removed)):
        return None, True
    ids = [item["id"] for item in old_items]
    if len(ids) != len(set(ids)):
        return None, True
    result = {item["id"]: deepcopy(item) for item in old_items}
    for item_id in removed:
        result.pop(item_id, None)
    for item in changed:
        result[item["id"]] = deepcopy(item)
    return {"rev": new_rev, "items": list(result.values())}, False
