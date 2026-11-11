from __future__ import annotations

from typing import Any


def _configured_port(value: Any, fallback: Any) -> tuple[int, str | None]:
    if value is None or value == "":
        value = fallback
    try:
        return int(value or 0), None
    except (TypeError, ValueError):
        return 0, f"invalid configured port ({value})"


def tier1_link_integrity_issues(node: Any, entry: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    expected_address = str(entry.get("address", "")).strip()
    expected_port, port_issue = _configured_port(entry.get("port"), getattr(node, "port", 0) or 0)
    if port_issue:
        issues.append(port_issue)

    if str(getattr(node, "protocol", "") or "").lower() != "vless":
        issues.append(f"expected VLESS but parsed {getattr(node, 'protocol', '') or 'unknown'}")
    if not getattr(node, "uuid", None):
        issues.append("missing UUID")
    if not getattr(node, "address", None):
        issues.append("missing server address")
    if not getattr(node, "port", None):
        issues.append("missing server port")
    if expected_address and getattr(node, "address", None) and node.address != expected_address:
        issues.append(f"address mismatch ({node.address} != {expected_address})")
    if expected_port and getattr(node, "port", None) and int(node.port) != expected_port:
        issues.append(f"port mismatch ({node.port} != {expected_port})")

    security = str(getattr(node, "security", "") or "").lower()
    extra = getattr(node, "extra", {})
    extra = extra if isinstance(extra, dict) else {}
    if security == "reality":
        if not extra.get("pbk"):
            issues.append("missing reality public key")
        if extra.get("sid") in {None, ""}:
            issues.append("missing reality short id")
    return issues


def mark_tier1_link_integrity(node: Any, entry: dict[str, Any]) -> list[str]:
    issues = tier1_link_integrity_issues(node, entry)
    extra = getattr(node, "extra", None)
    if not isinstance(extra, dict):
        extra = {}
        setattr(node, "extra", extra)
    extra["config_integrity_ok"] = not issues
    if issues:
        extra["config_integrity_errors"] = list(issues)
    return issues
