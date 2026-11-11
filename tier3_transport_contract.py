from __future__ import annotations

from models import Node


def tcp_reachability_is_required(node: Node) -> bool:
    """Whether a TCP refusal can rule out this proxy's upstream transport.

    This describes wire semantics, not protocol quality or scheduler priority.
    A datagram upstream still needs the ordinary mixed-port admission probe.
    """
    protocol = str(node.protocol or "").strip().lower()
    if protocol in {"hysteria", "hysteria2", "hy2", "tuic", "wireguard"}:
        return False
    return not (
        protocol in {"vmess", "vless"}
        and str(node.network or "").strip().lower() in {"kcp", "quic"}
    )
