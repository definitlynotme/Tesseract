from __future__ import annotations

import logging
from pathlib import Path
from collections.abc import Sequence
from typing import Any

from clash_formatter import (
    node_to_clash_proxy,
)
from dns_resolver_policy import helper_dns_block
from models import Node


def build_validator_prewarm_config(
    mixed_port: int, controller_port: int, *, ipv6: bool = False,
) -> dict[str, Any]:
    return {
        "mixed-port": int(mixed_port),
        "external-controller": f"127.0.0.1:{int(controller_port)}",
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "ipv6": bool(ipv6),
        "dns": helper_dns_block(ipv6=bool(ipv6)),
        "proxies": [],
        "proxy-groups": [],
        "rules": ["MATCH,REJECT"],
    }


def build_single_proxy_validation_config(
    node: Node,
    mixed_port: int,
    *,
    controller_port: int | None = None,
    ipv6: bool = False,
    proxy_name: str = "TEST-PROXY",
    group_name: str = "TEST-DIRECT",
    logger: logging.Logger | None = None,
) -> dict[str, Any] | None:
    """Build the canonical one-candidate Mihomo validation config."""

    proxy = node_to_clash_proxy(node, logger=logger, seen_names=set())
    if not proxy:
        return None
    normalized_proxy_name = str(proxy_name or "TEST-PROXY").strip() or "TEST-PROXY"
    normalized_group_name = str(group_name or "TEST-DIRECT").strip() or "TEST-DIRECT"
    normalized_proxy = dict(proxy)
    normalized_proxy["name"] = normalized_proxy_name
    config: dict[str, Any] = {
        "mixed-port": int(mixed_port),
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "ipv6": bool(ipv6),
        "dns": helper_dns_block(ipv6=bool(ipv6)),
        "proxies": [normalized_proxy],
        "proxy-groups": [
            {
                "name": normalized_group_name,
                "type": "select",
                "empty-fallback": "REJECT",
                "proxies": [normalized_proxy_name],
            }
        ],
        "rules": [f"MATCH,{normalized_group_name}"],
    }
    if controller_port is not None:
        config["external-controller"] = f"127.0.0.1:{int(controller_port)}"
    return config


def build_batch_proxy_validation_config(
    assignments: Sequence[tuple[Node, int]],
    mixed_port: int,
    *,
    controller_port: int | None = None,
    ipv6: bool = False,
    slot_keys: Sequence[int] | None = None,
    logger: logging.Logger | None = None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Build one config that validates many candidates at once.

    Each candidate gets its own listener bound to its own proxy, which is the
    same binding the runtime already uses for its private source lanes, so a
    single Mihomo can carry the whole batch: measured 2026-09-20, 298
    candidates in one process, ready in 1.3 s, 30 validations/second and 73 MB,
    against ~5/second across sixteen single-candidate processes.

    Returns the config and, positionally per assignment, the proxy name that
    carries it - empty where the candidate could not be rendered, so the caller
    can fail exactly those without losing the rest of the batch.  ``MATCH`` stays
    REJECT: a listener's binding decides its own egress, so nothing that misses
    a binding may fall through to a direct path.

    ``slot_keys`` names each proxy/listener by a caller-owned stable key instead
    of its list position.  The per-slot hot-install backend passes each slot's
    worker id, so that a reload changing only one slot leaves every other slot's
    proxy name, definition and listener port byte-identical - the property that
    lets a sibling's in-flight probe survive the reload.
    """

    proxies: list[dict[str, Any]] = []
    listeners: list[dict[str, Any]] = []
    names: list[str] = []
    seen: set[str] = set()
    for index, (node, listener_port) in enumerate(assignments):
        proxy = node_to_clash_proxy(node, logger=logger, seen_names=seen)
        if not proxy:
            names.append("")
            continue
        key = index if slot_keys is None else int(slot_keys[index])
        name = f"TEST-PROXY-{key}"
        seen.add(name)
        rendered = dict(proxy)
        rendered["name"] = name
        proxies.append(rendered)
        listeners.append(
            {
                "name": f"test-listener-{key}",
                "type": "mixed",
                "listen": "127.0.0.1",
                "port": int(listener_port),
                "proxy": name,
            }
        )
        names.append(name)
    config: dict[str, Any] = {
        "mixed-port": int(mixed_port),
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "ipv6": bool(ipv6),
        "dns": helper_dns_block(ipv6=bool(ipv6)),
        "proxies": proxies,
        "proxy-groups": [],
        "listeners": listeners,
        "rules": ["MATCH,REJECT"],
    }
    if controller_port is not None:
        config["external-controller"] = f"127.0.0.1:{int(controller_port)}"
    return config, tuple(names)


BATCH_PROVIDER_FILE_PREFIX = "provider-"


def batch_provider_proxy_name(key: int) -> str:
    return f"TEST-PROXY-{int(key)}"


def batch_provider_name(key: int) -> str:
    return f"TEST-PROVIDER-{int(key)}"


def batch_provider_placeholder(key: int) -> dict[str, Any]:
    """What a slot's provider carries before any candidate: a REJECT outbound.

    A listener bound to it answers every request with a refusal and never
    dials anything, so an idle slot has no egress at all.
    """

    return {"name": batch_provider_proxy_name(key), "type": "reject"}


def render_batch_provider_proxy(
    node: Node, key: int, *, logger: logging.Logger | None = None,
) -> dict[str, Any] | None:
    """One candidate as the sole proxy of slot ``key``'s provider, or None."""

    proxy = node_to_clash_proxy(node, logger=logger, seen_names=set())
    if not proxy:
        return None
    rendered = dict(proxy)
    rendered["name"] = batch_provider_proxy_name(key)
    return rendered


def build_batch_provider_validation_config(
    slot_listeners: Sequence[tuple[int, int]],
    mixed_port: int,
    *,
    provider_dir: str,
    controller_port: int | None = None,
    ipv6: bool = False,
) -> dict[str, Any]:
    """The fixed layout of a shared validation Mihomo: one provider per slot.

    Each ``(key, listener_port)`` becomes listener ``test-listener-<key>`` ->
    select group ``TEST-GROUP-<key>`` -> file provider ``TEST-PROVIDER-<key>``
    (``<provider_dir>/provider-<key>.yaml``) whose single proxy is the slot's
    current candidate.  Installing a candidate is then a provider update
    (``PUT /providers/proxies/TEST-PROVIDER-<key>``): Mihomo parses one proxy,
    touches no listener and no sibling, and answers a bad candidate with a
    non-2xx status while keeping the previous proxy - so a poison pill fails
    alone without any whole-config reload.  Measured with the bundled binary
    (2026-09-22): 16 concurrent installs in 156 ms wall, 20-65 ms each,
    against 271-664 ms per whole-config reload at K=16.

    ``provider_dir`` must lie under Mihomo's home directory (``-d``); the
    binary refuses provider paths outside it.  ``MATCH`` stays REJECT.
    """

    providers: dict[str, Any] = {}
    groups: list[dict[str, Any]] = []
    listeners: list[dict[str, Any]] = []
    for key, listener_port in slot_listeners:
        key = int(key)
        provider = batch_provider_name(key)
        group = f"TEST-GROUP-{key}"
        providers[provider] = {
            "type": "file",
            "path": str(Path(provider_dir) / f"{BATCH_PROVIDER_FILE_PREFIX}{key}.yaml"),
            "health-check": {
                "enable": False,
                "url": "http://cp.cloudflare.com/generate_204",
                "interval": 36000,
            },
        }
        groups.append({"name": group, "type": "select", "use": [provider]})
        listeners.append(
            {
                "name": f"test-listener-{key}",
                "type": "mixed",
                "listen": "127.0.0.1",
                "port": int(listener_port),
                "proxy": group,
            }
        )
    config: dict[str, Any] = {
        "mixed-port": int(mixed_port),
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "ipv6": bool(ipv6),
        "dns": helper_dns_block(ipv6=bool(ipv6)),
        "proxies": [],
        "proxy-providers": providers,
        "proxy-groups": groups,
        "listeners": listeners,
        "rules": ["MATCH,REJECT"],
    }
    if controller_port is not None:
        config["external-controller"] = f"127.0.0.1:{int(controller_port)}"
    return config
