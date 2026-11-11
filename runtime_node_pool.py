from __future__ import annotations

from typing import Any, Callable, Iterable


def runtime_node_dedupe_key(node: Any) -> tuple[str, int, str, str]:
    return (
        node.address,
        int(node.port),
        node.protocol,
        str(node.uuid or node.password or node.uri or node.name),
    )


def dedupe_runtime_nodes(nodes: Iterable[Any]) -> list[Any]:
    deduped: dict[tuple[str, int, str, str], Any] = {}
    for node in nodes:
        key = runtime_node_dedupe_key(node)
        if key not in deduped:
            deduped[key] = node
    return list(deduped.values())


def runtime_node_index(
    nodes: Iterable[Any],
    node_identity: str,
    *,
    identity_fn: Callable[[Any], str],
) -> int | None:
    return next(
        (index for index, node in enumerate(nodes) if identity_fn(node) == node_identity),
        None,
    )
