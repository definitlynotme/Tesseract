from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config_utils import coerce_bool, ensure_mapping_section
from killswitch import WHITELIST_ENTRY_SEPARATORS, firewall_ip_token, firewall_remote_token


@dataclass(frozen=True)
class KillswitchConfigFormState:
    enabled: bool
    allow_lan: bool
    allow_dns: bool
    additional_whitelist: list[str]
    dns_whitelist: list[str]


@dataclass(frozen=True)
class KillswitchWhitelistFormState:
    allow_lan: bool
    allow_dns: bool
    additional_whitelist: list[str]
    dns_whitelist: list[str]


def list_entries(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.splitlines()
    return [str(item).strip() for item in value or [] if item is not None and str(item).strip()]


def parse_whitelist_text(value: str, *, dns: bool = False) -> tuple[list[str], list[str]]:
    """Split a whitelist editor's text into (entries, invalid entries).

    Entries may be separated by newlines, whitespace, "," ";" (half- or
    full-width) or the ideographic comma. The additional whitelist accepts
    what the kill switch can pass to netsh remoteip=; the DNS whitelist
    accepts only IP addresses/subnets, the same check the kill switch applies
    at runtime, so nothing that is saved is silently dropped later.
    """
    accept = firewall_ip_token if dns else firewall_remote_token
    entries: list[str] = []
    invalid: list[str] = []
    for item in WHITELIST_ENTRY_SEPARATORS.split(str(value or "")):
        if not item:
            continue
        target = entries if accept(item) is not None else invalid
        if item not in target:
            target.append(item)
    return entries, invalid


def effective_killswitch_config(runtime_config: dict[str, Any], local_config: dict[str, Any]) -> dict[str, Any]:
    runtime_kill = runtime_config.get("killswitch", {}) if isinstance(runtime_config.get("killswitch"), dict) else {}
    local_kill = local_config.get("killswitch", {}) if isinstance(local_config.get("killswitch"), dict) else {}
    return {**runtime_kill, **local_kill}


def killswitch_form_state(runtime_config: dict[str, Any], local_config: dict[str, Any]) -> KillswitchConfigFormState:
    kill = effective_killswitch_config(runtime_config, local_config)
    return KillswitchConfigFormState(
        enabled=coerce_bool(kill.get("enabled", False), False),
        allow_lan=coerce_bool(kill.get("allow_lan", True), True),
        allow_dns=coerce_bool(kill.get("allow_dns", False), False),
        additional_whitelist=list_entries(kill.get("additional_whitelist") or []),
        dns_whitelist=list_entries(kill.get("dns_whitelist") or []),
    )


def apply_killswitch_config_payload(
    payload: dict[str, Any],
    state: KillswitchConfigFormState,
) -> dict[str, Any]:
    apply_killswitch_enabled_payload(payload, state.enabled)
    apply_killswitch_whitelist_payload(
        payload,
        KillswitchWhitelistFormState(
            allow_lan=state.allow_lan,
            allow_dns=state.allow_dns,
            additional_whitelist=state.additional_whitelist,
            dns_whitelist=state.dns_whitelist,
        ),
    )
    return payload


def apply_killswitch_enabled_payload(
    payload: dict[str, Any],
    enabled: bool,
) -> dict[str, Any]:
    kill = ensure_mapping_section(payload, "killswitch")
    kill["enabled"] = bool(enabled)
    return payload


def apply_killswitch_whitelist_payload(
    payload: dict[str, Any],
    state: KillswitchWhitelistFormState,
) -> dict[str, Any]:
    kill = ensure_mapping_section(payload, "killswitch")
    kill["allow_lan"] = state.allow_lan
    kill["allow_dns"] = state.allow_dns
    kill["additional_whitelist"] = list(state.additional_whitelist)
    kill["dns_whitelist"] = list(state.dns_whitelist)
    return payload
