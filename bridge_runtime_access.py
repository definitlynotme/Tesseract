"""Explicit P1 runtime capabilities; job scheduling belongs to the executor."""

from __future__ import annotations

import json
import re
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol
from functools import lru_cache
from dataclasses import dataclass

from bridge_vocab import (SECRET_FIELDS, secret_category,
                          secret_projection_path, alert_secret_forms)
from bridge_vocab import RUNTIME_TIERS as RUNTIME_TIER_NAMES

_SUMMARY_TIERS = frozenset(RUNTIME_TIER_NAMES.values())
# T-P4-C2 #7: tiers.summary drops a pending count older than this (the node
# projection resamples every 10 s while nodes.list/subscriptions.list is
# subscribed, so a live count is never this old) or while the runtime is
# stopped/stopping (PROTOCOL §5.3).
PENDING_MAX_AGE_S = 30.0
_PENDING_OFFLINE_REASONS = frozenset({"stopped", "stopping"})


CAPABILITIES = frozenset({
    "start", "request_stop", "stop", "restart_core", "lifecycle_status",
    "core_fact", "connection_info", "capabilities", "published_route_status", "tiers_summary_status",
})

RUNTIME_FAILURE_CODES = frozenset({
    "admin_required", "lifecycle_busy", "validation", "conflict",
    "not_running", "needs_route", "timeout", "superseded", "cancelled",
    "io", "runtime_listener_conflict", "runtime_listener_unavailable",
    "source_supply_no_egress", "no_sources", "internal",
})


def failure_code(value: Any) -> str | None:
    """Project only registered machine codes; never put raw errors on the wire."""
    if not value:
        return None
    from bridge_messages import classify_start_failure
    if isinstance(value, BaseException):
        return classify_start_failure(value)["code"]
    if isinstance(value, Mapping):
        return classify_start_failure(diagnostic=value)["code"]
    return classify_start_failure(diagnostic={"text": str(value).strip()})["code"]


def frozen_record(**fields: Any) -> Mapping[str, Any]:
    """Own the supplied scalar values and prevent mutation of a sampled domain."""
    return MappingProxyType(dict(fields))


@lru_cache(maxsize=1)
def cached_process_admin(checker: Any) -> bool:
    return bool(checker())


def service_state_data(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Make a fresh wire DTO from the five immutable lifecycle domains."""
    from bridge_messages import classify_start_failure
    data = {name: dict(snapshot[name]) for name in
            ("intent", "runtime", "core", "readiness", "freshness")}
    runtime = data["runtime"]
    diagnostic = runtime.pop("failure_diagnostic", None) or runtime.get("failure")
    if diagnostic or runtime.get("failure_code") or runtime.get("startup_phase") == "failed":
        if diagnostic and data["intent"].get("stop_requested") is True:
            diagnostic = {"reason_code": "superseded"}
        failure = classify_start_failure(diagnostic=diagnostic, lifecycle=data)
        runtime.update(failure=failure, failure_code=failure["code"])
        if (diagnostic and data["intent"].get("desired_running") is True
                and runtime.get("startup_phase") == "stopped"):
            runtime["startup_phase"] = "failed"
    else:
        runtime.update(failure=None, failure_code=None)
    return data


@dataclass(frozen=True)
class StopRequest:
    """Keep the accepted intent for stop while exposing only scalar wire facts."""

    intent: Any
    wire: Mapping[str, Any]


P2_READ_CAPABILITIES = frozenset({
    "network_mode", "bastion_pool", "protocol_mix", "collect_status",
    "gateway_status", "config_values", "app_info", "node_records", "subscription_records",
})


class RuntimeReadAccess:
    """Read-only P2 surface; not an extension of the P1 command allowlist.

    Resolve the runtime getter on every call. No rebuilt manager, monitor or
    configuration object is retained here. Domain records carry stale/owner
    facts; business adapters remove those internal facts when forming wire DTOs.
    """

    def network_mode(self) -> Mapping[str, Any]:
        return self.runtime.bridge_network_mode()

    def bastion_pool(self) -> Mapping[str, Any]:
        return self.runtime.bridge_bastion_pool()

    def protocol_mix(self, *, source: str = "loaded") -> Mapping[str, Any]:
        if source not in {"loaded", "configured"}:
            raise ValueError("invalid protocol source")
        return self.runtime.bridge_protocol_mix(source=source)

    def collect_status(self) -> Mapping[str, Any]:
        return self.runtime.bridge_collect_status()

    def gateway_status(self) -> Mapping[str, Any]:
        return self.runtime.bridge_gateway_status()

    def config_values(self) -> Mapping[str, Any]:
        return self.runtime.bridge_config_snapshot()

    def app_info(self) -> Mapping[str, Any]:
        return self.runtime.bridge_app_info()

    def node_records(self) -> Mapping[str, Any]:
        return self.runtime.bridge_node_records()

    def subscription_records(self) -> Mapping[str, Any]:
        return self.runtime.bridge_subscription_records()


class RuntimeAccess(RuntimeReadAccess):
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        # T-P4-C2 #7 (G33): (runtime generation, per-tier pending node count,
        # monotonic time noted) handed over by the node projection
        # (note_node_pending); one small tuple, replaced whole. Not a
        # capability: no runtime call behind it.
        self._node_pending: tuple[int, dict[str, int], float] | None = None

    def known_secret_values(self) -> dict[str, str]:
        """Local redactor input only, outside CAPABILITIES and all wire DTOs.

        Read owned config and loaded-node facts; the P2 public getter also
        reads sources configuration outside locks. Run on a normal worker,
        never on the pipe reader or publication tick. No controller calls,
        config mutation or logging of secrets. P1-16 owns refresh wiring.
        """
        from copy import deepcopy
        getter = getattr(type(self.runtime), "bridge_secret_facts", None)
        config = (getter(self.runtime) if getter is not None
                  else deepcopy(self.runtime.config))
        return _config_secret_values(config)

    def lifecycle_status(self) -> Mapping[str, Any]:
        sample = self.runtime.lifecycle_status()
        # P1 permits omitting the extension; keep legacy no-failure samples exact.
        facts = sample["runtime"]
        if ("failure_diagnostic" not in facts and "failure" not in facts
                and not facts.get("failure_code") and facts.get("startup_phase") != "failed"):
            return sample
        return immutable_record(service_state_data(sample))

    def core_fact(self) -> Mapping[str, Any]:
        return self.runtime.core_fact()

    def published_route_status(self) -> Mapping[str, Any]:
        return self.runtime.published_route_status()

    def tiers_summary_status(self) -> tuple[int, dict[str, Any]] | None:
        raw = self.runtime.tiers_summary_status()
        if raw is None:
            return None
        revision, summary = raw
        return revision, self._summary_additions(summary)

    def _summary_additions(self, summary: Any) -> Any:
        """T-P4-C2 #3 (K-2) / #7 (G33): per-tier ``check_interval_s`` and ``pending``.

        ``check_interval_s`` is the Manager's own schedule (``_tier_interval``:
        ``tiers.<tier>.check_interval``, positive int, default 60). ``pending``
        is the node projection's pending-row count of the same runtime
        generation; it is omitted while the runtime is stopped or stopping
        (the generation only advances on start, so a stopped summary would
        otherwise keep the pre-stop count), before any projection, and once
        the count is older than PENDING_MAX_AGE_S (the node projection only
        runs while nodes.list or subscriptions.list has a subscriber).
        A copy is returned; the runtime's cached summary is never modified.
        """
        if not isinstance(summary, Mapping) or not isinstance(summary.get("tiers"), Mapping):
            return summary
        from config_utils import coerce_positive_int
        from runtime_settings import tier_block
        result = dict(summary)
        tiers = {name: dict(row) if isinstance(row, Mapping) else row
                 for name, row in summary["tiers"].items()}
        try:
            config = self.runtime.config
        except AttributeError:
            config = None
        pending = self._node_pending
        if pending is not None and (
                summary.get("offline_reason") in _PENDING_OFFLINE_REASONS
                or not 0 <= time.monotonic() - pending[2] <= PENDING_MAX_AGE_S):
            pending = None
        generation = None
        if pending is not None:
            try:
                generation = self.runtime.lifecycle_status()["intent"]["runtime_generation"]
            except Exception:
                generation = None
        for runtime_tier, name in RUNTIME_TIER_NAMES.items():
            row = tiers.get(name)
            if not isinstance(row, dict):
                continue
            if isinstance(config, Mapping):
                try:
                    row["check_interval_s"] = coerce_positive_int(
                        tier_block(config, runtime_tier).get("check_interval", 60), 60)
                except Exception:
                    pass
            if pending is not None and generation == pending[0] and name in pending[1]:
                row["pending"] = pending[1][name]
        result["tiers"] = tiers
        return result

    def connection_info(self) -> Mapping[str, Any]:
        return self.runtime.connection_info()

    def capabilities(self) -> Mapping[str, Any]:
        return self.runtime.capabilities()

    def start(self) -> dict[str, Any]:
        raw = self.runtime.start()
        state = service_state_data(self.runtime.lifecycle_status())
        result = start_result(raw, state)
        if raw.get("start_blocked_by_cleanup"):
            from bridge_messages import classify_start_failure
            result["failure_code"] = classify_start_failure(
                diagnostic={"reason_code": "cleanup_pending"})["code"]
        return result

    def request_stop(self, *, expected_generation: int | None = None) -> StopRequest:
        intent = self.runtime.request_stop(expected_generation=expected_generation)
        return StopRequest(intent=intent, wire=frozen_record(
            accepted=bool(intent.accepted),
            generation=int(intent.generation),
            lifecycle_revision=int(intent.lifecycle_revision),
            skipped_reason=str(intent.skipped_reason or ""),
        ))

    def stop(self, *, stop_request: StopRequest) -> dict[str, Any]:
        raw = self.runtime.stop(stop_intent=stop_request.intent)
        state = service_state_data(self.runtime.lifecycle_status())
        return stop_result(raw, state)

    def restart_core(self) -> dict[str, Any]:
        before = self.runtime.core_fact()
        raw = self.runtime.restart_core()
        after = self.runtime.core_fact()
        return restart_result(raw, before, after)


def start_result(raw: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve a blocked or failed start even if a later sample has moved on."""
    sampled = state["runtime"]
    phase = str(raw.get("startup_phase") or sampled["startup_phase"])
    if raw.get("start_blocked_by_cleanup"):
        phase = "failed"
    elif phase not in {"failed", "stopping"} and sampled["startup_phase"] in {"failed", "stopping"}:
        phase = sampled["startup_phase"]
    code = failure_code(raw.get("failure_code") or raw.get("start_blocked_reason")
                        or raw.get("startup_failure_reason") or raw.get("last_error")
                        or sampled["failure_code"])
    if phase == "failed" and code is None:
        code = "internal"
    return {"startup_phase": phase, "failure_code": code if phase == "failed" else None}


def stop_result(raw: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    """Keep stopping/failed cleanup distinct from a completed stop."""
    phase = state["runtime"]["startup_phase"]
    pending = bool(state["runtime"]["shutdown_cleanup_pending"] or
                   state["core"]["cleanup_pending"] or raw.get("cleanup_pending")
                   or raw.get("shutdown_cleanup_pending"))
    if raw.get("startup_phase") == "failed" or (pending and phase != "stopping"):
        phase = "failed"
    code = raw.get("shutdown_exit_code")
    if code is not None and (type(code) is not int or code < 0):
        raise ValueError("invalid shutdown exit code")
    return {"startup_phase": phase, "shutdown_exit_code": code,
            "cleanup_pending": pending,
            "stop_skipped": bool(raw.get("stop_skipped")),
            "skipped_reason": str(raw.get("stop_skipped_reason") or "")}


def restart_result(raw: Mapping[str, Any], before: Mapping[str, Any],
                   after: Mapping[str, Any]) -> dict[str, Any]:
    """A return without an exception does not prove that restart applied."""
    running = bool(after["running"])
    pending = bool(after["cleanup_pending"])
    generation = int(after["core_generation"])
    applied = (running and not pending and
               generation > int(before["core_generation"]) and
               not bool(raw.get("cleanup_pending")) and
               not bool(raw.get("still_running")) and
               raw.get("applied") is not False)
    return {"applied": applied, "core_running": running,
            "core_cleanup_pending": pending, "core_generation": generation}


def traffic_monitor(access: RuntimeAccess) -> Any:
    """Resolve the runtime-owned monitor on every call, including after reload.

    Local read-only object access, outside command and wire-DTO capabilities.
    No controller calls, raw history copies or cached monitor reference here.
    """
    return access.runtime.traffic_monitor


def _config_secret_values(config: Any) -> dict[str, str]:
    """Extract nested legacy/channels configs, subscriptions and node material."""
    import re
    from urllib.parse import unquote, urlsplit
    values: dict[str, str] = {}
    def add(value, category):
        # Known configured values, including short effective strip aliases.
        if isinstance(value, str) and 0 < len(value) < 6:
            values[value] = category
        if isinstance(value, str) and len(value) >= 6:
            values[value] = category
    def walk(item, path=(), inherited=None):
        if isinstance(item, dict):
            # Channel arrays otherwise lose the type in their traversal path.
            if "alerts" in path and isinstance(item.get("type"), str):
                path = path + (item["type"].strip().lower(),)
            if "alerts" in path:
                channel = item.get("channel") or item.get("kind")
                if isinstance(channel, str):
                    path = path + (channel.strip().lower(),)
            for key, child in item.items():
                name = re.sub(r"[^a-z0-9]", "", str(key).lower())
                category = secret_category(name, path, inherited)
                walk(child, path + (name,), category)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child, path, inherited)
        elif type(item) in (int, float) and inherited:
            # Senders and _headers stringify configured numeric credentials.
            add(str(item), inherited)
        elif isinstance(item, str):
            if "://" in item and not (inherited is None and _dns_resolver_path(path)
                                      and _public_dns_resolver(item)):
                add(item, inherited or "secret")
            if inherited:
                add(item, inherited)
                for alias in _alert_used_secret_forms(item, path):
                    add(alias, inherited)
            if "://" in item and ("nodes" in path or inherited == "node"):
                add(item, "node")
                # Public parser handles base64 SS/VMess and URI auth forms. A
                # private local logger prevents malformed input reaching a sink.
                import logging
                from parser import parse_node_uri
                logger = logging.Logger("bridge-secret-parser")
                logger.addHandler(logging.NullHandler())
                node = parse_node_uri(item, logger=logger)
                if node is not None:
                    add(node.password, "password")
                    add(node.uuid, "uuid")
                    walk(node.extra, path)
                try:
                    add(unquote(urlsplit(item).password or ""), "password")
                except ValueError:
                    pass
    walk(config)
    return values


def _dns_resolver_path(path: tuple[str, ...]) -> bool:
    """client.dns resolver lists (T-P4-C1 #4); keys are normalized like walk()."""
    from bridge_protocol import DNS_RESOLVER_FIELDS
    # The config may be nested (bridge_secret_facts: {"config": {...}}).
    return (len(path) >= 3 and path[-3:-1] == ("client", "dns")
            and path[-1] in {name.replace("_", "") for name in DNS_RESOLVER_FIELDS})


def _public_dns_resolver(value: str) -> bool:
    from bridge_protocol import public_dns_resolver
    return public_dns_resolver(value)


# Audited against alerts.py senders; aliases change display filtering only.
_ALERT_STRIPPED_FIELDS = SECRET_FIELDS["alert_stripped"]


def _alert_used_secret_forms(value: str, path: tuple[str, ...]) -> tuple[str, ...]:
    return alert_secret_forms(value, path)


def immutable_record(value: Any) -> Any:
    """Recursively own JSON facts; never freeze live runtime objects in place."""
    if isinstance(value, Mapping):
        return MappingProxyType({k: immutable_record(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(immutable_record(v) for v in value)
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise TypeError("non-scalar runtime fact")


def observation_ms(value: Any) -> int | None:
    from datetime import datetime
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        return max(0, int(stamp.timestamp() * 1000))
    except (ValueError, OverflowError):
        return None


def opaque_id(*parts: Any) -> str:
    import hashlib
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()[:24]


def display_filter(secret_facts: Any):
    from bridge_redact import KnownSecrets, redact_text
    secrets = KnownSecrets(_config_secret_values(secret_facts))
    return lambda text: redact_text(str(text or ""), known_secrets=secrets)


def source_identity(kind: str, entry: Any) -> str:
    """Stable opaque identity, independent of position and mutable settings.

    Prefer a persisted ID; legacy string/mapping entries use the same stripped
    source value as the source loader. Credentials and paths never leave here.
    """
    if isinstance(entry, Mapping):
        if entry.get("id"):
            return opaque_id("source", kind, "id", str(entry["id"]).strip())
        value = next((entry[key] for key in SECRET_FIELDS["source_identity"]
                      if entry.get(key)), "")
    else:
        value = entry
    return opaque_id("source", kind, "value", str(value or "").strip())


def config_item_id(item: Mapping[str, Any]) -> str:
    """Stable opaque ConfigItem ID (决定_第一波裁定2, T-P2-14 #3; T-P2-RG).

    Same rule as ``source_identity``: a persisted ``id`` wins; otherwise the
    stripped item key (Tier1 name, node URI or subscription URL, as
    ``config_items_view.configured_config_items`` emits it), scoped by
    runtime tier and kind. Independent of list position, enablement and
    runtime generation, so a disabled item keeps its ID. Pure; raw keys
    (credentials) never leave here. P2-14/P2-15 switch to it via integration.

    NOT unique for duplicates: two entries of one tier and kind with the same
    stripped key (or the same persisted id) share one ID. Consumers resolving
    an ID must treat more than one match as ambiguous (conflict), never pick
    the first; the integration ticket adds that check (or an occurrence
    ordinal) and its test when it switches the ID space.
    """
    tier = str(item.get("tier") or "").strip().lower()
    kind = str(item.get("kind") or "").strip().lower()
    if item.get("id"):
        return opaque_id("config_item", tier, kind, "id", str(item["id"]).strip())
    value = item.get("key") or item.get("uri") or item.get("url") or item.get("name") or ""
    return opaque_id("config_item", tier, kind, "value", str(value).strip())


# Explicit field schemas, not a recursive forwarding of whole config blocks.
_TUN_FIELDS = dict.fromkeys(("device", "stack", "auto_route", "auto_detect_interface",
                           "mtu", "strict_route", "dns_hijack"), True)
_PROXY_FIELDS = dict.fromkeys(("enabled", "host", "port", "startup_timeout",
                             "health_failures_before_restore", "bypass"), True)
_DNS_FIELDS = dict.fromkeys(("mode", "enable", "respect_rules", "ipv6", "listen",
                           "enhanced_mode", "fake_ip_range", "fake_ip_filter", "nameserver",
                           "default_nameserver", "proxy_server_nameserver", "direct_nameserver",
                           "fallback"), True)
# T-P4-C1 #4 (R-03/R-06): user-editable kill switch and egress recovery facts.
_KILLSWITCH_FIELDS = dict.fromkeys(("enabled", "allow_lan", "allow_dns", "dns_whitelist",
                                    "additional_whitelist"), True)
_EGRESS_RECOVERY_FIELDS = dict.fromkeys(("enabled", "retry_source_refresh"), True)
_RULE_FIELDS = dict.fromkeys(("direct", "proxy", "reject", "raw", "rule_sets"), True)
_UI_FIELDS = dict.fromkeys(("language", "theme", "overview_auto_exit_lookup"), True)
_TRAFFIC_FIELDS = dict.fromkeys(("enabled", "sample_interval", "history_hours",
                               "retention_hours", "max_points"), True)
_DNS_RESOLVER_FIELDS = frozenset({"nameserver", "default_nameserver", "proxy_server_nameserver",
                                  "direct_nameserver", "fallback"})


_HEADER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
_HEADER_HEX_PART = re.compile(r"[0-9A-Fa-f]{6,}")


def _header_name_public(name: Any, slot: str, clean: Callable[[Any], str]) -> bool:
    """A credential header NAME that may leave as a field ID (T-P2-H11).

    Fail closed: a plain token (letter first, letters/digits/``-``/``_``, at
    most 64), no ``-``/``_`` part that looks like a random ID (six or more
    hex characters, six or more with a digit, or over 24 characters), no
    known secret inside (``clean``) and nothing the frame scan rejects.
    Anything else is secret-shaped: only the whole ``headers`` field is listed.
    """
    from bridge_protocol import contains_sensitive_text
    if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name):
        return False
    for part in re.split(r"[-_]", name):
        if (_HEADER_HEX_PART.fullmatch(part) or len(part) > 24
                or (len(part) >= 6 and any(ch.isdigit() for ch in part))):
            return False
    field_id = f"{slot}.{name}"
    return clean(name) == name and not contains_sensitive_text(field_id)


def _credential_header_ids(slot: str, headers: Mapping[str, Any], kind: Any,
                           clean: Callable[[Any], str]) -> set[str]:
    """``alerts.<id>.headers.<name>`` for each saved, non-empty credential
    header (``bridge_vocab.alert_secret_path``); ordinary headers and
    secret-shaped names add nothing (the whole-field ID is always listed)."""
    from bridge_vocab import alert_secret_path
    kind = kind.strip().lower() if isinstance(kind, str) else ""
    ids = set()
    for name, value in headers.items():
        if value is None or value == "" or not isinstance(name, str):
            continue
        if alert_secret_path(kind, f"headers.{name}") and _header_name_public(name, slot, clean):
            ids.add(f"{slot}.{name}")
    return ids


def config_projection(config: Mapping[str, Any], sources: Mapping[str, Any], *, clean):
    """Section 16 safe fields only; omitted secrets remain in backend config."""
    from bridge_vocab import protocol_mode
    from config_utils import coerce_bool, normalize_routing_rule_order
    from routing_rules import effective_rule_list, provider_rows
    secret_fields: set[str] = set()

    def resolver(path, value):
        # T-P4-C1 #4: DNS resolver lists only; userinfo stays a secret.
        parts = path.split(".")
        return (len(parts) >= 2 and parts[0] == "dns" and parts[1] in _DNS_RESOLVER_FIELDS
                and isinstance(value, str) and _public_dns_resolver(value))

    def project(value, path):
        """(projected, status): status is "ok", "secret" or "opaque"."""
        if isinstance(value, str):
            # The display filter withholds every URL; a resolver address is
            # shown when its host/path part carries no known secret.
            if resolver(path, value) and clean(value.partition("://")[2]) == value.partition("://")[2]:
                return value, "ok"
            if "://" in value or clean(value) != value:
                return None, "secret" if value else "opaque"
            return value, "ok"
        if value is None or type(value) in (bool, int, float):
            return value, "ok"
        if isinstance(value, (list, tuple)):
            # T-P4-C1 fix M1: a list is shown whole or withheld whole. Dropping
            # one withheld entry (a URL, a registered secret such as the SMTP
            # username in to_addrs) would make an unchanged re-save delete it.
            result, status = [], "ok"
            for index, child in enumerate(value):
                projected, child_status = project(child, f"{path}.{index}")
                if child_status == "secret":
                    return None, "secret"
                if child_status != "ok":
                    status = "opaque"
                result.append(projected)
            return (result, "ok") if status == "ok" else (None, "opaque")
        return None, "opaque"

    def safe(value, path):
        projected, status = project(value, path)
        if status == "secret":
            secret_fields.add(path)  # Field-level ID, also for a whole list.
        return projected, status == "ok"

    def fields(block, schema, path):
        block = block if isinstance(block, Mapping) else {}
        result = {}
        for key in schema:
            if key in block:
                projected, keep = safe(block[key], f"{path}.{key}")
                if keep:
                    result[key] = projected
        return result

    def register(block, path, kind=None, by_name=True):
        # Stable field IDs only, including secrets excluded by the allowlist.
        import re
        for key, value in (block.items() if isinstance(block, Mapping) else ()):
            name = re.sub(r"[^a-z0-9]", "", str(key).lower())
            slot = f"{path}.{clean(key)}"
            if name == "nodejudgement":
                continue
            if name == "headers" and value:
                secret_fields.add(slot)
                if by_name and path.startswith("alerts.") and isinstance(value, Mapping):
                    # T-P2-H11: each saved credential header by name (never
                    # its value), so the page can clear or re-enter it.
                    secret_fields.update(_credential_header_ids(slot, value, kind, clean))
            elif isinstance(value, Mapping):
                register(value, slot, kind, by_name)
            elif isinstance(value, (list, tuple)):
                for index, child in enumerate(value):
                    if isinstance(child, Mapping):
                        register(child, f"{slot}.{index}", kind, by_name)
                    elif child and (name in {"subscriptionurls", "headers"}
                                    or isinstance(child, str) and "://" in child
                                    and not resolver(slot, child)):
                        secret_fields.add(slot)  # Whole list withheld (fix M1).
            elif value and (secret_projection_path(kind, name)
                or isinstance(value, str) and "://" in value and not resolver(slot, value)):
                secret_fields.add(slot)

    client = config.get("client", {})
    client = client if isinstance(client, Mapping) else {}
    network = fields(client, {"allow_lan": True, "mihomo_mixed_port": True, "ipv6": True}, "network")
    mode = client.get("network_mode", "system_proxy")
    network["mode"] = protocol_mode(mode) if mode in {"system_proxy", "tun", "mixed"} else "system"
    network["tun"] = fields(client.get("tun"), _TUN_FIELDS, "network.tun")
    network["system_proxy"] = fields(client.get("system_proxy"), _PROXY_FIELDS, "network.system_proxy")
    network["controller"] = fields(client.get("mihomo_controller"), {"enabled": True, "host": True, "port": True}, "network.controller")
    network["subscription"] = fields(config.get("subscription_server"), {"host": True, "port": True}, "network.subscription")
    # T-P4-C1 #4 (R-06): the two user switches only; node_judgement stays hidden.
    network["egress_recovery"] = fields(client.get("egress_recovery"), _EGRESS_RECOVERY_FIELDS,
                                        "network.egress_recovery")
    register(client.get("mihomo_controller"), "network.controller")
    register(config.get("subscription_server"), "network.subscription")
    dns = fields(client.get("dns"), _DNS_FIELDS, "dns")
    register(client.get("dns"), "dns")
    # A resolver list holding any secret entry (userinfo, token) is withheld
    # whole by safe(), like every list (fix M1), as ``dns.<field>``.
    routing_config = config.get("routing", {})
    routing_config = routing_config if isinstance(routing_config, Mapping) else {}
    routing = {"rules": fields(routing_config.get("rules"), _RULE_FIELDS, "routing.rules"),
               # the match order of the three target lists, always complete
               "rule_order": normalize_routing_rule_order(routing_config.get("rule_order"))}
    rules = routing_config.get("rules")
    if isinstance(rules, Mapping) and "compatibility_defaults" in rules:
        compatibility = []
        for index, entry in enumerate(rules["compatibility_defaults"] or ()):
            row = fields(entry, {"rule_type": True, "value": True},
                         f"routing.rules.compatibility_defaults.{index}")
            if (row.get("rule_type") in {"direct", "proxy", "reject"}
                    and isinstance(row.get("value"), str) and row["value"]):
                compatibility.append(row)
        routing["rules"]["compatibility_defaults"] = compatibility
    providers = {}
    for name, block in (routing_config.get("rule_providers", {}) or {}).items():
        safe_name = clean(name)
        providers[safe_name] = fields(block, dict.fromkeys(("type", "behavior", "interval", "format", "proxy")), f"routing.rule_providers.{safe_name}")
        register(block, f"routing.rule_providers.{safe_name}")
    routing["rule_providers"] = providers
    # 配置整理 P2: the rules as one ordered list (the legacy layout read as one while the
    # file holds no routing.rules.list) and the rule sets as rows; a rule set's address is
    # shown only when it is public (no credentials, no query), else url_hidden. A list
    # holding an address at all is withheld whole, as safe() does with any list.
    rule_list = effective_rule_list(routing_config)
    if "://" in json.dumps(rule_list, ensure_ascii=False, default=str):
        secret_fields.add("routing.rule_list")
        rule_list = []
    routing["rule_list"] = rule_list
    routing["providers"] = provider_rows(routing_config.get("rule_providers"))
    source_rows = []
    for kind in ("github_repos", "telegram_channels", "subscription_urls", "local_files"):
        for entry in sources.get(kind, ()) or ():
            source_rows.append({"id": source_identity(kind, entry), "kind": kind,
                                "display": kind, "enabled": coerce_bool(entry.get("enabled", True), True) if isinstance(entry, Mapping) else True})
            hint = source_hint(kind, entry)  # T-P4-C1 #9 (U4)
            if hint is not None:
                source_rows[-1]["hint"] = hint
            raw = next((entry[key] for key in SECRET_FIELDS["source_identity"]
                        if entry.get(key)), "") if isinstance(entry, Mapping) else entry
            if raw:
                secret_fields.add(f"sources.{source_rows[-1]['id']}.value")
    # 提醒 redesign 10-30: local notices only, three switches (alert_policy
    # holds the defaults); the retired channel blocks are never projected.
    from alert_policy import notify_settings
    values = {"network": network, "dns": dns, "routing": routing,
              "latency": fields(config.get("testing"), {"primary_gateway_max_latency_ms": True}, "latency"),
              "sources": source_rows, "alerts": {"notify": notify_settings(config.get("alerts"))},
              "ui": fields(config.get("ui"), _UI_FIELDS, "ui"),
              "autostart": {},  # RuntimeReadAccess adds the authoritative registry fact.
              "traffic": fields(config.get("traffic"), _TRAFFIC_FIELDS, "traffic"),
              "killswitch": fields(config.get("killswitch"), _KILLSWITCH_FIELDS, "killswitch"),
              "bastion": bastion_values(config)}
    return values, tuple(sorted(secret_fields))


def bastion_values(config: Mapping[str, Any]) -> dict[str, Any]:
    """T-P4-C1 #1 (K-7): ``{enabled, min_alive_nodes}`` of tiers.tier3.

    Same normalization as the Qt Bastion card (``tier3_config_view``) and the
    runtime thresholds: ``min_alive_nodes`` is clamped to the pool capacity
    (``max_pool_size``, the ``bastion.pool.capacity`` fact). Pure.
    """
    from tier3_config_view import tier3_config_form_state
    state = tier3_config_form_state(dict(config) if isinstance(config, Mapping) else {}, {})
    return {"enabled": state.enabled, "min_alive_nodes": state.min_alive_nodes}


def bastion_capacity(config: Mapping[str, Any]) -> int:
    """Upper bound of ``bastion.min_alive_nodes`` (D9): the pool capacity."""
    from tier3_config_view import tier3_config_form_state
    return tier3_config_form_state(dict(config) if isinstance(config, Mapping) else {}, {}).max_pool_size


_HINT_DOT = "\u2022"
_HINT_MASK = _HINT_DOT * 3


def _hint_char(value: str) -> str | None:
    if not value:
        return None
    first = value[0]
    return first if first.isascii() and first.isalnum() else _HINT_DOT


def _source_raw_value(entry: Any) -> str:
    if isinstance(entry, Mapping):
        raw = next((entry[key] for key in SECRET_FIELDS["source_identity"] if entry.get(key)), "")
    else:
        raw = entry
    return raw.strip() if isinstance(raw, str) else ""


def source_hint(kind: str, entry: Any) -> str | None:
    """Masked display hint of a source value (U4, 决定_P4_用户答复 C1 #9).

    Only the first character of each segment ever leaves here: GitHub
    ``<o>•••/<r>•••``, Telegram ``@<c>•••``, subscription host ``<h>•••``,
    local file name ``<f>•••``. A first character that is not an ASCII letter
    or digit becomes ``•``. Empty or unparsable values give None (no hint).
    Not a secret field ID; the source value itself stays a secret.
    """
    import re
    from urllib.parse import urlsplit
    raw = _source_raw_value(entry)
    if not raw:
        return None
    try:
        if kind == "github_repos":
            text = raw
            if "://" in text:
                parsed = urlsplit(text)
                if (parsed.hostname or "").lower() not in {"github.com", "www.github.com"}:
                    return None
                text = parsed.path
            elif text.lower().startswith(("github.com/", "www.github.com/")):
                text = text.split("/", 1)[1]
            parts = [part for part in text.strip("/").split("/") if part]
            if len(parts) < 2:
                return None
            owner, repo = parts[0], parts[1].removesuffix(".git")
            first, second = _hint_char(owner), _hint_char(repo)
            if first is None or second is None:
                return None
            return f"{first}{_HINT_MASK}/{second}{_HINT_MASK}"
        if kind == "telegram_channels":
            text = raw
            if "://" in text or re.match(r"(?i)(www\.)?(t|telegram)\.me/", text):
                parsed = urlsplit(text if "://" in text else "https://" + text)
                segments = [part for part in parsed.path.split("/") if part]
                if segments and segments[0] == "s":
                    segments = segments[1:]
                text = segments[0] if segments else ""
            first = _hint_char(text.lstrip("@"))
            return None if first is None else f"@{first}{_HINT_MASK}"
        if kind == "subscription_urls":
            first = _hint_char(urlsplit(raw).hostname or "")
            return None if first is None else f"{first}{_HINT_MASK}"
        if kind == "local_files":
            name = re.split(r"[\\/]", raw.rstrip("\\/"))[-1]
            first = _hint_char(name)
            return None if first is None else f"{first}{_HINT_MASK}"
    except ValueError:
        return None
    return None


def config_query_owners(access: RuntimeAccess) -> tuple:
    """Resolve rebuilt controlled-write owners on each ordinary-domain query."""
    return (lambda: getattr(access.runtime, "bridge_config_revision", lambda: 0)(),
            lambda: getattr(access.runtime, "bridge_config_pending", lambda: None)())


def node_secret_fingerprint(nodes: Any) -> tuple:
    """Equality key of what one loaded tier list adds to the redaction set.

    T-P2-H6 修复第 1 轮: the manager swaps a tier's whole list on every
    health result, targeted check and Tier3 admission with the credentials
    unchanged, so the node counter compares this key, never the list object.
    Covers the facts ``bridge_secret_facts`` exports per node: the raw
    ``uri/password/uuid/source/sources`` (a superset of what the redaction
    derives from them; order-free, so a re-sort is no change) and the values
    ``_config_secret_values`` harvests from ``extra`` (secret-named keys and
    URL strings, the same walk ``display_filter`` runs). Tier3 bookkeeping in
    ``extra`` (counters, flags, reasons) is not harvested, so it is no change.
    No lock: ``list()`` and ``dict()`` are atomic copies; a walk broken by a
    concurrent in-place write returns a key equal to nothing (a conservative
    change, never a missed one). About 1.4 ms per 300 nodes.
    """
    try:
        rows = list(nodes)
        idents = frozenset((node.uri, node.password, node.uuid, node.source, tuple(node.sources))
                           for node in rows)
        extras = [{"extra": dict(node.extra)} for node in rows if node.extra]
        harvested = (frozenset(_config_secret_values({"loaded": {"nodes": extras}}).items())
                     if extras else frozenset())
    except Exception:
        return (object(),)
    return (idents, harvested)


def sampling_fact_versions(access: RuntimeAccess) -> tuple[int, int, int] | None:
    """Read the config/source/node-secret owners' lightweight version receipt.

    T-P2-H6: DesktopRuntime.bridge_sampling_fact_versions observes the three
    owners (no commit hook exists outside the protected regions): each counter
    advances when the held config object, bridge config revision or saved exit
    selection; the store object or sources file stat; or the manager, its
    runtime_nodes mapping or a tier's ``node_secret_fingerprint`` changes (only
    recomputed when that tier's list object or length changed). A write is
    seen once it becomes observable; an in-place credential edit on a loaded
    list stays the 30 s gateway recheck's job.
    Integration may expose this getter on runtime without changing capability
    registries. Missing owners explicitly withhold the running gateway
    baseline; malformed receipts count as missing.
    """
    getter = getattr(access.runtime, "bridge_sampling_fact_versions", None)
    if getter is None:
        return None
    versions = getter()
    if (not isinstance(versions, tuple) or len(versions) != 3
            or any(type(value) is not int or value < 0 for value in versions)):
        return None
    return versions


def sampling_config_identity(access: RuntimeAccess) -> tuple:
    """Constant-size owner token, including in-place source/secret revisions."""
    runtime = access.runtime
    return (id(runtime.config), id(runtime.config_store), id(runtime.manager),
            sampling_fact_versions(access))


class GatewaySelectedNodePort(Protocol):
    """T-P2-H8 runtime port: DesktopRuntime.bridge_gateway_selected_node_id.

    Internal fact {"node_id": str|None, "reason": str|None, "generation": int}:
    the saved Gateway selection reversed through the latest strict reload's
    render receipt, None (with a reason) whenever that is not certain.
    """

    def bridge_gateway_selected_node_id(self) -> Mapping[str, Any]: ...

    def bridge_loaded_export_revision(self) -> int | None: ...


def loaded_export_revision(access: RuntimeAccess) -> int | None:
    """Revision of the Core-loaded export contract (the render receipt), or None.

    T-P2-H8: part of the gateway.current sampling token, so a reload that
    installs a new receipt without republishing the route (no-capture standby)
    still re-reads ``selected_node_id``. A runtime without the getter, or a
    malformed answer, is None: the token then just does not observe it.
    """
    getter = getattr(access.runtime, "bridge_loaded_export_revision", None)
    if getter is None:
        return None
    value = getter()
    return value if type(value) is int and value >= 0 else None


_NODE_ID_DIGITS = frozenset("0123456789abcdef")  # opaque_id(): sha256 hex, first 24


def gateway_selected_node_id(access: RuntimeAccess, *, generation: Any) -> str | None:
    """``gateway.current.selected_node_id`` or None (omit the field).

    Adapter for GatewaySelectedNodePort: resolves only the public runtime
    getter, like sampling_fact_versions, so capability registries stay as
    they are. A runtime without the getter, an answer for another runtime
    generation than the gateway read (``generation``), or anything that is
    not an opaque Node.id shape is no answer: the field is omitted, never
    guessed and never filled with a name.
    """
    getter = getattr(access.runtime, "bridge_gateway_selected_node_id", None)
    if getter is None:
        return None
    raw = getter()
    if not isinstance(raw, Mapping) or type(generation) is not int:
        return None
    node_id = raw.get("node_id")
    if (type(raw.get("generation")) is not int or raw.get("generation") != generation
            or not isinstance(node_id, str) or len(node_id) != 24
            or not set(node_id) <= _NODE_ID_DIGITS):
        return None
    return node_id


def geo_status_facts(access: RuntimeAccess) -> Mapping[str, Any]:
    """Resolve the existing public geo getter; no update or reconfigure call."""
    runtime = access.runtime
    try:
        getter, updater = runtime.geo_update_status, runtime.geo_updater
    except AttributeError:
        return immutable_record({"unknown": True, "stale": False})
    config, manager = runtime.config, runtime.manager
    before = access.lifecycle_status()
    raw = getter()
    after = access.lifecycle_status()
    stale = (updater is not runtime.geo_updater or config is not runtime.config
             or manager is not runtime.manager or before["intent"] != after["intent"]
             or before["core"] != after["core"] or after["freshness"]["stale"])
    return immutable_record({"running": raw["running"],
        "last_update": raw.get("last_update", {}), "errors": raw.get("errors", {}),
        "stale": stale})


GEO_KINDS = ("geoip", "geosite", "mmdb", "asn")  # geo_updater.ASSET_KEYS order


def geo_items(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    """T-P4-C2 #11 (settings R-11): ``Geo.items`` from ``geo_status_facts``.

    One row per data kind the updater has a fact about (a completed download
    time, or a failed attempt), in the updater's fixed order; the time of a
    kind that only failed is null. Error text never leaves (it may carry
    download URLs or paths). The integration adds ``"items": geo_items(raw)``
    to ``bridge_queries.ReadQueries.geo_status`` (not an allowed file here).
    """
    last = raw.get("last_update") if isinstance(raw, Mapping) else None
    errors = raw.get("errors") if isinstance(raw, Mapping) else None
    last = last if isinstance(last, Mapping) else {}
    errors = errors if isinstance(errors, Mapping) else {}
    return [{"kind": kind, "updated_at_ms": observation_ms(last.get(kind))}
            for kind in GEO_KINDS if kind in last or kind in errors]


def mask_host(host: Any) -> str | None:
    """T-P4-C2 #3 (K-1): ``Endpoint.host_masked`` of a node address.

    A name keeps its first character and its top-level label
    (``gw-tyo01.example.org`` -> ``g•••.org``); an IPv4 address keeps the
    first octet (``203.•••``), an IPv6 address its first group (``2001:•••``).
    A first character that is not an ASCII letter or digit becomes ``•``.
    Empty or unparsable -> None. The full host stays local (nodes.reveal is
    the only path to it, and it does not carry ``host`` today).
    """
    import ipaddress
    import re
    if not isinstance(host, str):
        return None
    text = host.strip().strip("[]").rstrip(".")
    if not text or len(text) > 253 or any(ch.isspace() for ch in text):
        return None
    try:
        address = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        address = None
    dot = "•"
    if address is not None:
        if address.version == 4:
            return f"{str(address).split('.', 1)[0]}.{dot * 3}"
        return f"{address.exploded.split(':', 1)[0].lstrip('0') or '0'}:{dot * 3}"
    labels = text.split(".")
    first = text[0] if text[0].isascii() and text[0].isalnum() else dot
    tld = labels[-1] if len(labels) > 1 else ""
    if tld and re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,62}", tld):
        return f"{first}{dot * 3}.{tld}"
    return f"{first}{dot * 3}"


# --- T-P4-C2 ports ------------------------------------------------------------
# Module functions (like geo_status_facts), not RuntimeAccess capabilities:
# each resolves one public ``bridge_*`` runtime getter per call. A runtime
# without it (frozen sidecar, fakes) raises AttributeError; callers treat any
# failure as "unknown" and omit the v1 addition.

def subscription_refresh_times(access: RuntimeAccess) -> Mapping[str, Any]:
    """K-6: ``{"generation", "stale", "tiers": {sentinel|gateway: ms|None}}``."""
    return access.runtime.bridge_subscription_refresh_times()


def node_endpoint(access: RuntimeAccess, node_id: str) -> Mapping[str, Any] | None:
    """K-1/K-3: raw ``{"stale", "host", "port", "protocols"}`` of one Node.id.

    Internal facts only; bridge_nodes.node_detail_extras masks the host.
    """
    return access.runtime.bridge_node_endpoint(node_id)


def note_node_pending(access: Any, generation: int, counts: Mapping[str, int]) -> None:
    """G33: node owner -> tiers.summary, the latest pending counts per tier."""
    if not isinstance(access, RuntimeAccess) or type(generation) is not int or generation < 0:
        return
    clean = {tier: count for tier, count in dict(counts).items()
             if tier in _SUMMARY_TIERS and type(count) is int and count >= 0}
    access._node_pending = (generation, clean, time.monotonic())
