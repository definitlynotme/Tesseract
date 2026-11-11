from __future__ import annotations

import logging
from typing import Any

from config_utils import coerce_bool
from models import Node
from parser import parse_content_blob
from tier1_protocols import expand_tier1_entry


def mark_exit_node(node: Node, *, index: int) -> Node:
    node.tier = "exit"
    node.source = node.source or "config.yaml"
    node.extra["exit_node"] = True
    if not node.name:
        node.name = f"exit-{index + 1}"
    return node


def exit_nodes_from_entry(
    entry: Any,
    index: int,
    *,
    logger: logging.Logger | None = None,
) -> list[Node]:
    nodes: list[Node] = []
    if isinstance(entry, str):
        nodes.extend(parse_content_blob(entry, source="config.yaml", tier="exit", logger=logger))
    elif isinstance(entry, dict):
        if not coerce_bool(entry.get("enabled", True), True):
            return []
        text_parts: list[str] = []
        for key in ["uri", "link", "url"]:
            value = entry.get(key)
            if value:
                text_parts.append(str(value))
        links = entry.get("links")
        if isinstance(links, list):
            text_parts.extend(str(link) for link in links if link)
        if text_parts:
            parsed = parse_content_blob("\n".join(text_parts), source="config.yaml", tier="exit", logger=logger)
            if entry.get("name") and len(parsed) == 1:
                parsed[0].name = str(entry["name"])
                parsed[0].remarks = str(entry["name"])
            nodes.extend(parsed)
        if any(entry.get(key) for key in ["vless_link", "hysteria2_link", "naive_link", "imported_link"]) or isinstance(entry.get("protocols"), dict):
            nodes.extend(expand_tier1_entry(entry, logger=logger))
        elif not nodes and entry.get("address") and entry.get("port"):
            nodes.extend(expand_tier1_entry(entry, logger=logger))

    return [mark_exit_node(node, index=index) for node in nodes]
