from __future__ import annotations

from typing import Any, Callable, Iterable

from ui_format import fmt


TIER1_ROW_TEXTS = ("配置", "是", "正常")
STATUS_RANK = {"alive": 0, "slow": 1, "running": 0, "active": 0, "unknown": 2, "suspicious": 3, "dead": 4}


def _identity(text: str) -> str:
    return text


def node_row_meta(row: list[Any]) -> dict[str, Any]:
    meta = row[10] if len(row) > 10 else {}
    return meta if isinstance(meta, dict) else {}


def tier1_variants_from_status_payload(protocols: Any) -> list[dict[str, Any]]:
    if not isinstance(protocols, list):
        return []
    variants: list[dict[str, Any]] = []
    for item in protocols:
        if not isinstance(item, dict):
            continue
        address = fmt(item.get("address"))
        port = fmt(item.get("port"))
        variants.append(
            {
                "name": fmt(item.get("name")),
                "protocol": fmt(item.get("protocol_label") or item.get("protocol")),
                "protocol_key": fmt(item.get("protocol_key"), ""),
                "status": fmt(item.get("status")),
                "latency": fmt(item.get("latency_ms")),
                "endpoint": f"{address}:{port}",
                "identity": fmt(item.get("identity"), ""),
                "has_uri": bool(item.get("has_uri")),
                "fallback_order": list(item.get("fallback_order", []) or []),
                "connection": (
                    dict(item.get("connection"))
                    if isinstance(item.get("connection"), dict)
                    else {}
                ),
            }
        )
    return variants


def tier1_variant_payload(row: list[Any]) -> dict[str, Any]:
    meta = node_row_meta(row)
    return {
        "name": fmt(row[1] if len(row) > 1 else ""),
        "protocol": fmt(row[2] if len(row) > 2 else meta.get("protocol_label") or meta.get("protocol_key")),
        "protocol_key": fmt(meta.get("protocol_key"), ""),
        "status": fmt(row[4] if len(row) > 4 else ""),
        "latency": fmt(row[5] if len(row) > 5 else ""),
        "endpoint": fmt(row[3] if len(row) > 3 else ""),
        "identity": fmt(meta.get("identity"), ""),
        "has_uri": bool(meta.get("has_uri")),
        "fallback_order": list(meta.get("fallback_order", []) or []),
        "connection": (
            dict(meta.get("connection"))
            if isinstance(meta.get("connection"), dict)
            else {}
        ),
    }


def latency_value(value: Any) -> float:
    try:
        return float(str(value).replace("ms", "").strip())
    except (TypeError, ValueError):
        return 999999.0


def best_tier1_variant_row(rows: list[list[Any]]) -> list[Any]:
    def key(row: list[Any]) -> tuple[int, float]:
        status = fmt(row[4] if len(row) > 4 else "").lower()
        return STATUS_RANK.get(status, 2), latency_value(row[5] if len(row) > 5 else None)

    return min(rows, key=key) if rows else []


def best_latency(rows: list[list[Any]]) -> str:
    candidates = [row for row in rows if latency_value(row[5] if len(row) > 5 else None) < 999999.0]
    if not candidates:
        return "--"
    best = min(candidates, key=lambda row: latency_value(row[5] if len(row) > 5 else None))
    return fmt(best[5] if len(best) > 5 else "--")


def latest_text(values: Iterable[Any]) -> str:
    items = [fmt(value, "") for value in values if fmt(value, "")]
    return max(items) if items else "--"


def tier1_aggregate_risk(rows: list[list[Any]], *, translate: Callable[[str], str] = _identity) -> str:
    risks = [fmt(row[9] if len(row) > 9 else "", "") for row in rows]
    abnormal = [risk for risk in risks if risk and risk not in {translate("正常"), "Normal"}]
    if abnormal:
        return abnormal[0]
    return translate("正常")


def tier1_aggregate_row(
    physical_name: str,
    rows: list[list[Any]],
    *,
    translate: Callable[[str], str] = _identity,
) -> list[Any]:
    first = rows[0]
    variants = [tier1_variant_payload(row) for row in rows]
    fallback_order = variants[0].get("fallback_order", []) if variants else []
    protocol_labels = [str(item.get("protocol") or "") for item in variants if item.get("protocol")]
    protocol_summary = " -> ".join(protocol_labels) if protocol_labels else fmt(first[2] if len(first) > 2 else "")
    best_row = best_tier1_variant_row(rows)
    status = fmt(best_row[4] if len(best_row) > 4 else "")
    latency = best_latency(rows)
    last_checked = latest_text(row[6] for row in rows if len(row) > 6)
    meta = dict(node_row_meta(best_row))
    meta.update(
        {
            "aggregate": True,
            "physical_node": physical_name,
            "variants": variants,
            "fallback_order": fallback_order,
            "identity": node_row_meta(best_row).get("identity"),
        }
    )
    return [
        "tier1",
        physical_name,
        protocol_summary,
        fmt(best_row[3] if len(best_row) > 3 else ""),
        status,
        latency,
        last_checked,
        translate("配置"),
        translate("是") if any(item.get("has_uri") for item in variants) else "--",
        tier1_aggregate_risk(rows, translate=translate),
        meta,
    ]


def tier1_display_rows(runtime_rows: list[list[Any]], *, translate: Callable[[str], str] = _identity) -> list[list[Any]]:
    if any(node_row_meta(row).get("aggregate") for row in runtime_rows):
        return runtime_rows
    grouped: dict[str, list[list[Any]]] = {}
    order: list[str] = []
    for row in runtime_rows:
        meta = node_row_meta(row)
        physical = fmt(meta.get("physical_node") or row[1] if len(row) > 1 else "")
        if physical not in grouped:
            grouped[physical] = []
            order.append(physical)
        grouped[physical].append(row)
    return [tier1_aggregate_row(name, grouped[name], translate=translate) for name in order]
