from __future__ import annotations

import logging
from typing import Any

from config_utils import coerce_bool
from models import Node
from parser import parse_content_blob


def nodes_from_single_protocol_entries(
    entries: list[Any],
    tier: str,
    *,
    logger: logging.Logger | None = None,
) -> list[Node]:
    nodes: list[Node] = []
    for index, entry in enumerate(entries or []):
        text = ""
        name = ""
        if isinstance(entry, str):
            text = entry
        elif isinstance(entry, dict):
            if not coerce_bool(entry.get("enabled", True), True):
                continue
            name = str(entry.get("name") or "")
            text = str(entry.get("uri") or entry.get("link") or entry.get("url") or "")
        if not text:
            continue
        parsed = parse_content_blob(text, source="config.yaml", tier=tier, logger=logger)
        if name and len(parsed) == 1:
            parsed[0].name = name
            parsed[0].remarks = name
        for node in parsed:
            node.tier = tier
            if not node.name:
                node.name = f"{tier}-{index + 1}"
        nodes.extend(parsed)
    return nodes
