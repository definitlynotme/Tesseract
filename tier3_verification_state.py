from __future__ import annotations

from typing import Any


def node_extra(node: Any) -> dict[str, Any]:
    extra = getattr(node, "extra", {})
    return extra if isinstance(extra, dict) else {}


def node_has_startup_core_verification(node: Any) -> bool:
    extra = node_extra(node)
    return bool(extra.get("startup_core_probe_url")) or extra.get("startup_core_delay_ms") is not None


def node_has_tcp_prelaunch_marker(node: Any) -> bool:
    return node_extra(node).get("startup_prelaunch_validation") == "tcp"


def node_is_unverified_tcp_startup_candidate(node: Any) -> bool:
    return node_has_tcp_prelaunch_marker(node) and not node_has_startup_core_verification(node)


def tier3_startup_core_verified_nodes(nodes: list[Any]) -> list[Any]:
    return [node for node in nodes if node_has_startup_core_verification(node)]
