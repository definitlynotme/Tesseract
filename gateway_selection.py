"""The saved Gateway (exit) selection: which node, not which proxy name.

A Gateway's rendered Mihomo proxy name moves: same-named exits swap their
``-2`` suffixes with latency order and status, and a same-named node in an
earlier tier (or the Tier3 naming switch) pushes it to ``-2``.  The selection
is therefore saved as the node's identity, with its proxy name at pick time as
a label:

* ``tiers.exit.selected_proxy`` holds the LABEL;
* ``tiers.exit.selected_node`` holds ``{"identity": "v1:<64 hex>", "label":
  <exactly selected_proxy>}`` (bound) or ``{"unresolved": <reason>, "label":
  ...}`` (an old name-only selection that names more than one Gateway);
* ``tiers.exit.selected`` is never written by this build.  Older builds write
  it on every pick (and read ``selected or selected_proxy``), so a non-empty
  ``selected`` means a pick made by an older build or by hand, which wins.

The identity is a digest, never the raw node: it covers the credentials.
Pure module (stdlib only); the renderer-side rules live in clash_formatter.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

GATEWAY_IDENTITY_VERSION = 1
_DOMAIN = "tesseract.gateway-identity.v1"
_IDENTITY_RE = re.compile(r"v([1-9][0-9]*):([0-9a-f]{64})")
_SELECTION_KEYS = ("selected", "selected_proxy", "selected_node")


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def gateway_identity(node: Any) -> str:
    """``v1:`` + sha256 over what chooses the exit a Gateway node reaches.

    Protocol, server, port, credentials (uuid, password, method), transport
    (security, network, host, path, sni, plugin), the HTTP/SOCKS5 username
    (multiplexed residential gateways route by it) and the gRPC service name.
    Never the name, status, latency or rendered name.  Any change to this
    material bumps the version.
    """

    extra = getattr(node, "extra", None)
    extra = extra if isinstance(extra, dict) else {}
    try:
        port = str(int(getattr(node, "port", 0)))
    except (TypeError, ValueError):
        port = _text(getattr(node, "port", "")).strip()
    network = _text(getattr(node, "network", None))
    grpc_service = (
        _text(extra.get("serviceName") or extra.get("grpc-service-name") or "")
        if network.strip().lower() == "grpc"
        else ""
    )
    material = [
        _DOMAIN,
        _text(getattr(node, "protocol", "")).strip().lower(),
        _text(getattr(node, "address", "")).strip().lower(),
        port,
        _text(getattr(node, "uuid", None)),
        _text(getattr(node, "password", None)),
        _text(getattr(node, "method", None)),
        _text(getattr(node, "security", None)),
        network,
        _text(getattr(node, "host", None)),
        _text(getattr(node, "path", None)),
        _text(getattr(node, "sni", None)),
        _text(getattr(node, "plugin", None)),
        _text(extra.get("username") or ""),
        grpc_service,
    ]
    encoded = json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"v{GATEWAY_IDENTITY_VERSION}:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class GatewaySelection:
    """One parsed selection.

    ``state``: none | legacy (name only) | bound (identity) | unresolved
    (a name-only selection proven ambiguous) | invalid (a record this build
    cannot verify).  Only ``bound`` carries an identity; ``label`` is the saved
    proxy name (display and old-build compatibility only).
    """

    state: str
    label: str = ""
    identity: str = ""
    reason: str = ""

    @property
    def active(self) -> bool:
        return self.state != "none"

    @property
    def key(self) -> str:
        if self.state == "none":
            return ""
        if self.state == "bound":
            return "id:" + self.identity
        if self.state == "legacy":
            return "name:" + self.label
        return f"{self.state}:{self.label}"


NO_SELECTION = GatewaySelection("none")


def read_selection(exit_block: Any) -> GatewaySelection:
    """Parse ``tiers.exit``; one parser for every reader."""

    block = exit_block if isinstance(exit_block, dict) else {}
    old_pick = _text(block.get("selected")).strip()
    if old_pick:
        # Written by an older build (or by hand): it wins over any record.
        return GatewaySelection("legacy", old_pick)
    label = _text(block.get("selected_proxy")).strip()
    if not label:
        return NO_SELECTION
    record = block.get("selected_node")
    if not isinstance(record, dict) or _text(record.get("label")).strip() != label:
        # No record, or one left behind by a label edit: the name alone.
        return GatewaySelection("legacy", label)
    unresolved = record.get("unresolved")
    if isinstance(unresolved, str) and unresolved:
        return GatewaySelection("unresolved", label, reason=unresolved)
    identity = _text(record.get("identity")).strip()
    match = _IDENTITY_RE.fullmatch(identity)
    if match and int(match.group(1)) == GATEWAY_IDENTITY_VERSION:
        return GatewaySelection("bound", label, identity=identity)
    # A record this build cannot verify fails closed, never served by name.
    return GatewaySelection("invalid", label, reason="unsupported" if match else "malformed")


def selection_observation(exit_block: Any) -> tuple[Any, ...]:
    """The raw selection fields as plain strings (change detection only)."""

    block = exit_block if isinstance(exit_block, dict) else {}
    record = block.get("selected_node")
    return (
        _text(block.get("selected")),
        _text(block.get("selected_proxy")),
        (
            (_text(record.get("identity")), _text(record.get("unresolved")), _text(record.get("label")))
            if isinstance(record, dict)
            else None
        ),
    )


def write_bound_selection(block: dict[str, Any], label: str, identity: str) -> None:
    block.pop("selected", None)
    block["selected_proxy"] = label
    block["selected_node"] = {"identity": identity, "label": label}


def write_name_only_selection(block: dict[str, Any], label: str) -> None:
    block.pop("selected", None)
    block["selected_proxy"] = label
    block.pop("selected_node", None)


def write_unresolved_selection(block: dict[str, Any], label: str, reason: str = "ambiguous") -> None:
    block.pop("selected", None)
    block["selected_proxy"] = label
    block["selected_node"] = {"unresolved": reason, "label": label}


def clear_gateway_selection(block: dict[str, Any]) -> bool:
    """Remove every selection key; whether any was present."""

    removed = [block.pop(key, None) is not None for key in _SELECTION_KEYS]  # a list: every key is popped
    return any(removed)


def selection_from_manager(manager: Any) -> GatewaySelection:
    """The Manager's saved selection (older managers: their name only)."""

    if manager is None:
        return NO_SELECTION
    reader = getattr(manager, "active_exit_selection", None)
    if callable(reader):
        selection = reader()
        if isinstance(selection, GatewaySelection):
            return selection
    getter = getattr(manager, "active_exit_proxy_name", None)
    label = str(getter() or "").strip() if callable(getter) else ""
    return GatewaySelection("legacy", label) if label else NO_SELECTION
