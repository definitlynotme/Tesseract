"""Pure section validation and private write-value preparation for P2-10.

``current`` is the latest BACKEND section snapshot, not ConfigValues from a
page: it contains retained hidden keys and real secrets. ``values`` in a
successful ValidationResult is private write material and must never be sent
over IPC. Error, SecretEdit and validate_field's response are wire DTOs.
Sources use ConfigChange.values.items because ConfigChange.values is an object;
their public projection remains ConfigValues.sources = [Source].
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field as data_field
import re
from typing import Any
from urllib.parse import urlsplit

from alert_policy import NOTICE_GROUPS
from bridge_protocol import _schema, _contains_hidden_setting
from bridge_vocab import SECRET_FIELDS
from config_utils import ROUTING_TARGET_RULE_TYPES
from config_validation import (
    validate_dns_local, validate_killswitch_local,
    validate_sources_config, validate_testing_local, validate_url,
)
from dns_resolver_policy import DNS_MODES
from dns_ui import validate_plain_dns_ips
from killswitch_config_view import parse_whitelist_text
from latency_policy_view import validate_latency_policy_max_latency_ms
from system_proxy_runtime import normalize_system_proxy_bypass_entry


# Explicit field registry, never arbitrary dotted writes. Existing unlisted
# keys may be retained, but cannot be introduced/edited by a page.
FIELDS = {
    "network": {"network_mode", "ipv6", "allow_lan", "mihomo_mixed_port",
                "subscription_port", "tun", "system_proxy", "egress_recovery"},
    "dns": {"mode", "respect_rules", "nameserver", "default_nameserver",
            "proxy_server_nameserver", "direct_nameserver", "fallback"},
    # rule_order: the match order of the direct / proxy / reject lists.
    # rule_list / providers (配置整理 P2): the rules as one ordered list
    # (routing.rules.list) and the rule sets it may name (routing.rule_providers).
    "routing": {"rules", "rule_order", "rule_list", "providers"},
    "latency": {"primary_gateway_max_latency_ms"},
    # 提醒 redesign 10-30: three switches under alerts.notify (alert_policy).
    "sources": {"items"}, "alerts": {"notify"},
    # D8 ``start_in_tray_on_autostart`` (T-P4-C1 #4): shell autostart preference.
    "ui": {"language", "overview_auto_exit_lookup", "start_in_tray_on_autostart"},
    "autostart": {"enabled"},
    "traffic": {"enabled", "history_seconds", "sample_interval_seconds"},
    "killswitch": {"enabled", "allow_lan", "allow_dns", "additional_whitelist", "dns_whitelist"},
    # T-P4-C1 #1 (K-7): the Qt Bastion card's two fields (tiers.tier3).
    "bastion": {"enabled", "min_alive_nodes"},
}
# Bastion bound when no backend capacity is known (settings.validate preflight).
BASTION_MIN_ALIVE_CEILING = 100_000
NESTED = {
    "tun": {"enabled": "bool", "stack": "stack", "auto_route": "bool",
            "strict_route": "bool", "auto_detect_interface": "bool"},
    "system_proxy": {"enabled": "bool", "bypass": "bypass"},
    "egress_recovery": {"enabled": "bool", "retry_source_refresh": "bool"},
}
LANGUAGES = {"zh_CN", "en_US", "de_DE", "es_ES", "fr_FR", "ja_JP", "ko_KR", "pt_BR", "ru_RU"}
SOURCE_KINDS = {"github_repos", "subscription_urls", "telegram_channels", "local_files"}


@dataclass(frozen=True)
class SecretPlacement:
    edit: dict[str, Any]  # Exact SecretEdit DTO (private input).
    path: tuple[str | int, ...]  # Relative to normalized section value.


@dataclass
class ValidationResult:
    values: Any
    errors: list[dict[str, Any]] = data_field(default_factory=list)
    secret_edits: list[SecretPlacement] = data_field(default_factory=list)
    retained_fields: list[str] = data_field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors


class _Invalid(ValueError):
    def __init__(self, field: str):
        self.field = field


# 配置整理 P2: why a routing row or rule set is refused (routing_rules.RuleError.reason);
# any other reason is the generic invalid field.
_RULE_ERROR_KEYS = {
    "local_only": "errors.settings.rule_local_only",
    "reserved_host": "errors.settings.rule_reserved_host",
    "own_program": "errors.settings.rule_own_program",
    "unknown_set": "errors.settings.rule_unknown_set",
    "too_many": "errors.settings.rule_too_many",
    "duplicate": "errors.settings.rule_set_duplicate",
    "url": "errors.settings.rule_set_url",
    "in_use": "errors.settings.rule_set_in_use",
}


def _error(field: str, reason: str | None = None) -> dict:
    # Do not reflect unknown keys containing credentials, URLs or control chars.
    if not re.fullmatch(r"[A-Za-z0-9_.\[\]-]+", field):
        field = "values"
    return {"code": "validation", "message_key": _RULE_ERROR_KEYS.get(reason or "", "errors.settings.invalid_field"),
            "params": {"field": field}}


def _dto_errors(name: str, value: Any) -> list[str]:
    # P2-01 has no public standalone DTO validator. Keep the single schema
    # adapter here; do not duplicate protocol shapes or edit bridge_protocol.
    errors: list[str] = []
    _schema(value, name, name, errors)
    return errors


def _bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise _Invalid(path)
    return value


def _int(value: Any, path: str, lo: int, hi: int) -> int:
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise _Invalid(path)
    return value


def _strings(value: Any, path: str) -> list[str]:
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise _Invalid(path)
    return list(dict.fromkeys(v.strip() for v in value if v.strip()))


def _masked(value: Any) -> bool:
    return isinstance(value, str) and (value.strip().casefold() in {
        "已设置", "已配置", "set", "configured", "[redacted]", "<redacted>", "••••••",
    } or bool(re.fullmatch(r"[*•●]+", value.strip())))


def _normal(section: str, field: str, value: Any) -> Any:
    path = f"{section}.{field}"
    if section not in FIELDS or field not in FIELDS[section]:
        raise _Invalid(path)
    if field in {"enabled", "ipv6", "allow_lan", "allow_dns", "respect_rules", "overview_auto_exit_lookup",
                 "start_in_tray_on_autostart"}:
        return _bool(value, path)
    if section == "bastion" and field == "min_alive_nodes":
        return _int(value, path, 1, BASTION_MIN_ALIVE_CEILING)
    if field in {"mihomo_mixed_port", "subscription_port"}:
        return _int(value, path, 1, 65535)
    if field == "network_mode":
        if value not in ("system_proxy", "tun", "mixed"):
            raise _Invalid(path)
        return value
    if section == "network" and field in NESTED:
        if not isinstance(value, dict):
            raise _Invalid(path)
        result = {}
        for key, item in value.items():
            child = f"{path}.{key}"
            kind = NESTED[field].get(key)
            if kind == "bool":
                result[key] = _bool(item, child)
            elif kind == "stack" and item in ("system", "gvisor", "mixed"):
                result[key] = item
            elif kind == "bypass":
                entries = _strings(item, child)
                normalized = [normalize_system_proxy_bypass_entry(v) for v in entries]
                if not all(normalized):
                    raise _Invalid(child)
                result[key] = list(dict.fromkeys(normalized))
            else:
                raise _Invalid(child)
        return result
    if section == "dns":
        if field == "mode":
            value = value.strip().lower().replace("-", "_") if isinstance(value, str) else value
            if value not in DNS_MODES:
                raise _Invalid(path)
            return value
        entries = _strings(value, path)
        if field == "default_nameserver" and entries and not validate_plain_dns_ips(entries):
            raise _Invalid(path)
        validate_dns_local({"mode": "custom", field: entries})
        return entries
    if section == "latency":
        normalized = validate_latency_policy_max_latency_ms(value)
        validate_testing_local({field: normalized})
        return normalized
    if section == "ui" and field == "language":
        if value not in LANGUAGES:
            raise _Invalid(path)
        return value
    if section == "traffic":
        return _int(value, path, 1, 86400 if field == "history_seconds" else 60)
    if section == "killswitch":
        entries = _strings(value, path)
        normalized, invalid = parse_whitelist_text("\n".join(entries), dns=field == "dns_whitelist")
        if invalid:
            raise _Invalid(path)
        return normalized
    if section == "routing" and field == "rule_order":
        # A full permutation of the three target lists, nothing else.
        if (not isinstance(value, list) or len(value) != len(ROUTING_TARGET_RULE_TYPES)
                or sorted(value) != sorted(ROUTING_TARGET_RULE_TYPES)):
            raise _Invalid(path)
        return list(value)
    if section == "routing":
        if not isinstance(value, dict):
            raise _Invalid(path)
        result = {}
        for key, item in value.items():
            child = f"{path}.{key}"
            if key in {"direct", "proxy", "reject"}:
                result[key] = _strings(item, child)
            elif key == "compatibility_defaults" and isinstance(item, list):
                rows = []
                for i, row in enumerate(item):
                    if (not isinstance(row, dict) or set(row) != {"rule_type", "value"}
                            or row["rule_type"] not in {"direct", "proxy", "reject"}
                            or not isinstance(row["value"], str) or not row["value"].strip()):
                        raise _Invalid(f"{child}[{i}]")
                    rows.append({"rule_type": row["rule_type"], "value": row["value"].strip()})
                result[key] = rows
            else:
                raise _Invalid(child)
        return result
    if section == "alerts":
        return _notify(value, path)
    raise _Invalid(path)


def _set_names(rows: Any) -> set[str]:
    """The rule sets the compiled rows name (off rows are not compiled)."""
    names: set[str] = set()
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or row.get("off"):
            continue
        for item in row.get("of") if isinstance(row.get("of"), list) else [row]:
            if isinstance(item, dict) and item.get("kind") == "rule-set":
                names.add(str(item.get("value")))
    return names


def _routing_lists(submitted: dict, original: dict, result: ValidationResult, retained: set[str]) -> None:
    """``routing.rule_list`` and ``routing.providers`` (配置整理 P2): whole lists, replaced
    (never merged), checked together: every rule set a compiled row names must stay a
    provider, whichever of the two is submitted."""
    from routing_rules import RuleError, normalize_providers, normalize_rule_list

    try:
        if "providers" in submitted:
            result.values["providers"] = normalize_providers(submitted["providers"], original.get("providers"))
            retained -= _leaves(result.values["providers"], "routing.providers")
        names = set(result.values.get("providers") or {})
        if "rule_list" in submitted:
            result.values["rule_list"] = normalize_rule_list(submitted["rule_list"], providers=names)
            retained -= _leaves(result.values["rule_list"], "routing.rule_list")
        elif _set_names(result.values.get("rule_list")) - names:
            raise RuleError("routing.providers", "in_use")
    except RuleError as exc:
        result.errors.append(_error(exc.path, exc.reason))


def _notify(value: Any, path: str) -> dict:
    """``alerts.notify`` (提醒 redesign 10-30): enabled / events / sound.

    A partial object is merged over the stored block (``_merge``); ``events``
    is a list of NOTICE_GROUPS names (empty: no group shows a notice) and is
    stored in NOTICE_GROUPS order.
    """
    if not isinstance(value, dict):
        raise _Invalid(path)
    result: dict[str, Any] = {}
    for key, item in value.items():
        child = f"{path}.{key}"
        if key in ("enabled", "sound"):
            result[key] = _bool(item, child)
        elif key == "events":
            if (not isinstance(item, list) or any(entry not in NOTICE_GROUPS for entry in item)
                    or len(set(item)) != len(item)):
                raise _Invalid(child)
            result[key] = [group for group in NOTICE_GROUPS if group in item]
        else:
            raise _Invalid(child)
    return result


def _merge(old: dict, new: dict) -> dict:
    result = deepcopy(old)
    for key, value in new.items():
        result[key] = _merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else deepcopy(value)
    return result


def _leaves(value: Any, prefix: str) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_leaves(v, f"{prefix}.{k}") for k, v in value.items()))
    return {prefix}


def _row_id(row: Any, path: str) -> str:
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
        raise _Invalid(f"{path}.id")
    return row["id"]


def _rows(section: str, rows: Any, old: Any, retained: set[str]) -> list[dict]:
    path = f"{section}.items"
    if not isinstance(rows, list) or len(rows) > 4000:
        raise _Invalid(path)
    previous = {r["id"]: r for r in old if isinstance(r, dict) and isinstance(r.get("id"), str)}
    result, seen = [], set()
    for index, row in enumerate(rows):
        p = f"{path}[{index}]"
        identity = _row_id(row, p)
        if identity in seen:
            raise _Invalid(f"{p}.id")
        seen.add(identity)
        base = deepcopy(previous.get(identity, {}))
        retained.update(_leaves(base, p))
        kind = row.get("kind", base.get("kind"))
        allowed = {"id", "kind", "display", "hint", "enabled", "owner", "repo", "url", "value", "secret_fields_set"}
        for key, value in row.items():
            if key not in allowed:
                raise _Invalid(f"{p}.{key}")
            if key == "secret_fields_set":
                continue  # Read-only state, never written.
            if key == "hint":
                # T-P4-C1 #9: sidecar-generated mask; validated and ignored.
                if not isinstance(value, str) or not value.strip():
                    raise _Invalid(f"{p}.{key}")
                continue
            if section == "sources" and key in SECRET_FIELDS["sources"]:
                raise _Invalid(f"{p}.{key}")  # Secrets require an explicit edit.
            if section == "sources" and key in {"owner", "repo"}:
                # These are aliases for the secret source value, not metadata.
                # Submit owner/repo via SecretEdit.value ("owner/repo").
                raise _Invalid(f"{p}.{key}")
            if key == "enabled":
                value = _bool(value, f"{p}.{key}")
            elif not isinstance(value, str) or not value.strip():
                raise _Invalid(f"{p}.{key}")
            base[key] = deepcopy(value)
            retained.discard(f"{p}.{key}")
        if kind not in SOURCE_KINDS:
            raise _Invalid(f"{p}.kind")
        result.append(base)
    return result


def _secret_edit_value(edit: dict, path: str) -> None:
    action = edit.get("action")
    if (_dto_errors("SecretEdit", edit)
            or action == "set" and (not isinstance(edit.get("value"), str)
                                    or not edit["value"].strip() or _masked(edit["value"]))
            or action != "set" and "value" in edit):
        raise _Invalid(f"{path}.value")


def _apply_secrets(section: str, edits: list, result: ValidationResult, retained: set[str]) -> None:
    seen = set()
    for i, edit in enumerate(edits):
        p = f"{section}.secrets[{i}]"
        if _dto_errors("SecretEdit", edit):
            raise _Invalid(p)
        field, target = edit["field"], edit.get("target_id")
        identity = (target, field)
        if identity in seen:
            raise _Invalid(f"{p}.field")
        seen.add(identity)
        rows = result.values if section == "sources" else []
        match = [(index, row) for index, row in enumerate(rows) if row.get("id") == target]
        if len(match) != 1:
            raise _Invalid(f"{p}.target_id")
        index, row = match[0]
        if section == "sources" and field in SECRET_FIELDS["sources"]:
            field = "value"
            slot = row
            path = (index, field)
            retained_path = f"sources.items[{index}].value"
        else:
            raise _Invalid(f"{p}.field")
        identity = (target, tuple(str(part).casefold() for part in path))
        if identity in seen:
            raise _Invalid(f"{p}.field")
        seen.add(identity)
        action = edit["action"]
        _secret_edit_value(edit, p)
        if action != "keep":
            value = edit.get("value", "")
            if section == "sources" and action == "set":
                value = _source_value(row["kind"], value, f"{p}.value")
            slot[field] = value
            if section == "sources":
                row.pop("url", None)
            retained.discard(retained_path)
        elif field in slot:
            retained.add(retained_path)
        result.secret_edits.append(SecretPlacement(deepcopy(edit), path))


def _source_value(kind: str, value: Any, path: str) -> str:
    if kind not in SOURCE_KINDS or not isinstance(value, str) or not value.strip() or _masked(value):
        raise _Invalid(path)
    value = value.strip()
    if kind == "github_repos":
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
            value = f"https://github.com/{value}"
        parsed = urlsplit(value)
        if parsed.hostname == "github.com":
            if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise _Invalid(path)
            parts = parsed.path.strip("/").split("/")
            if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
                raise _Invalid(path)
            value = f"https://github.com/{parts[0]}/{parts[1].removesuffix('.git')}"
    if kind in {"github_repos", "subscription_urls"}:
        try:
            validate_url(value)
        except ValueError as exc:
            raise _Invalid(path) from exc
    return value


def validate_change(change: Any, current: dict[str, Any]) -> ValidationResult:
    """Normalize one partial ConfigChange without mutating either input.

    Invalid sections return values=None: partial normalization is never writable.
    Hidden submissions are errors; omitted/current values are deep-copy retained.
    """
    result = ValidationResult(None)
    shape_errors = _dto_errors("ConfigChange", change)
    if shape_errors:
        result.errors.extend(_error(item.split(":", 1)[1].replace("ConfigChange", "change", 1)) for item in shape_errors)
        return result
    section, submitted = change["section"], change["values"]
    if _contains_hidden_setting(submitted):
        result.errors.append(_error(f"{section}.node_judgement"))
        return result
    original = deepcopy(current.get(section, [] if section == "sources" else {}))
    retained = _leaves(original, section) if original else set()
    result.values = original
    try:
        if section == "sources":
            if set(submitted) - {"items"}:
                raise _Invalid(f"sources.{next(iter(set(submitted) - {'items'}))}")
            if "items" in submitted:
                retained.clear()
                result.values = _rows(section, submitted["items"], original, retained)
        else:
            lists = {}
            for field, value in submitted.items():
                if section == "routing" and field in ("rule_list", "providers"):
                    lists[field] = value  # checked together below
                    continue
                try:
                    normalized = _normal(section, field, value)
                    result.values = _merge(result.values, {field: normalized})
                    retained -= _leaves(normalized, f"{section}.{field}")
                except (ValueError, TypeError, OverflowError) as exc:
                    result.errors.append(_error(exc.field if isinstance(exc, _Invalid) else f"{section}.{field}"))
            if lists:
                _routing_lists(lists, original, result, retained)
        _apply_secrets(section, change.get("secrets", []), result, retained)
        if section == "dns":
            validate_dns_local(result.values)
            if result.values.get("mode") == "custom":
                for field in ("nameserver", "proxy_server_nameserver"):
                    if not result.values.get(field):
                        result.errors.append(_error(f"dns.{field}"))
        elif section == "killswitch":
            validate_killswitch_local(result.values)
        elif section == "bastion":
            # D9: the upper bound is the runtime pool capacity (max_pool_size),
            # a retained backend key the page cannot submit.
            capacity = result.values.get("max_pool_size")
            minimum = result.values.get("min_alive_nodes")
            if (type(capacity) is int and type(minimum) is int and minimum > capacity
                    and "min_alive_nodes" in submitted):
                raise _Invalid("bastion.min_alive_nodes")
        elif section == "sources":
            # Preserve delivery/fetch-profile/extension keys, validating only
            # the editable identity here with the common sources validator.
            previous_rows = {row["id"]: row for row in original}
            for index, row in enumerate(result.values):
                p = f"sources.items[{index}].value"
                cleared = any(placement.path == (index, "value")
                              and placement.edit["action"] == "clear"
                              for placement in result.secret_edits)
                previous = previous_rows.get(row["id"], {})
                retained_clear = previous.get("enabled") is False and previous.get("value") == ""
                if (cleared or retained_clear) and row.get("enabled", True) is False and row.get("value") == "":
                    continue  # Explicit or retained clear of a disabled source.
                _source_value(row["kind"], row.get("value", row.get("url", row.get("path"))), p)
                # Additional keys are not page-editable. Profile definitions
                # live in the source document and are resolved by P2-13; don't
                # reject a retained profile by validating it out of context.
                payload = {row["kind"]: [{"value": row.get("value", row.get("url", row.get("path")))}]}
                try:
                    validate_sources_config(payload)
                except ValueError as exc:
                    raise _Invalid(p) from exc
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        if isinstance(exc, _Invalid):
            field = exc.field
        else:
            # Public validators name fields in errors; retain just that path,
            # never their message (which may contain actual input material).
            match = re.match(r"(?:client\.)?((?:dns|alerts|killswitch)(?:\.[A-Za-z0-9_]+|\[\d+\])+)", str(exc))
            field = match.group(1) if match else f"{section}.values"
        result.errors.append(_error(field))
    result.retained_fields = sorted(retained)
    if result.errors:
        result.values = None
        result.secret_edits.clear()
    return result


def validate_field(section: str, field: str, value: Any, *,
                   bastion_capacity: int | None = None) -> dict[str, Any]:
    """settings.validate response, with no normalized secret in the response.

    Source values require items.<kind>.value (or .url); GitHub owner/repo
    shorthand belongs in SecretEdit.value. Row metadata uses items.<field>. Secret values
    preflight a prospective SecretEdit.set; keep/clear use a SecretEdit object.
    Alert switches use notify.<enabled|events|sound>.
    No sibling completeness is required here.
    """
    try:
        if section == "sources" and field in {"items.id", "items.kind", "items.display", "items.enabled"}:
            if field == "items.enabled":
                _bool(value, f"{section}.{field}")
            elif field == "items.kind":
                if value not in SOURCE_KINDS:
                    raise _Invalid(f"{section}.{field}")
            elif not isinstance(value, str) or not value.strip():
                raise _Invalid(f"{section}.{field}")
        elif section == "sources" and field.startswith("items."):
            parts = field.split(".")
            if len(parts) != 3 or parts[1] not in SOURCE_KINDS:
                raise _Invalid(f"{section}.{field}")
            if parts[2] in SECRET_FIELDS["sources"]:
                edit = value if isinstance(value, dict) else {"field": parts[2], "action": "set", "value": value}
                if edit.get("field") != parts[2]:
                    raise _Invalid(f"{section}.{field}")
                _secret_edit_value(edit, f"{section}.{field}")
                if edit["action"] == "set":
                    _source_value(parts[1], edit["value"], f"{section}.{field}")
            else:
                raise _Invalid(f"{section}.{field}")
        elif section == "alerts" and field.startswith("notify."):
            _notify({field[len("notify."):]: value}, "alerts.notify")
        else:
            if section == "network" and "." in field:
                parent, child = field.split(".", 1)
                _normal(section, parent, {child: value})
            elif section == "routing" and field.startswith("rules."):
                _normal(section, "rules", {field.split(".", 1)[1]: value})
            else:
                normalized = _normal(section, field, value)
                if (section == "bastion" and field == "min_alive_nodes"
                        and type(bastion_capacity) is int and normalized > bastion_capacity):
                    raise _Invalid(f"{section}.{field}")
        return {"valid": True, "errors": []}
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        # Nested headers locate their leaf; other preflight errors keep the
        # requested field path (including SecretEdit.value failures).
        path = exc.field if isinstance(exc, _Invalid) and ".headers." in exc.field and not exc.field.endswith(".value") else f"{section}.{field}"
        return {"valid": False, "errors": [_error(path)]}
