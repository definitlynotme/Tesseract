from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


MANUAL_PROTOCOL_OPTIONS: tuple[tuple[str, str], ...] = (
    ("VLESS Reality", "vless_reality"),
    ("VMess WS TLS", "vmess"),
    ("Trojan TLS", "trojan"),
    ("Shadowsocks", "ss"),
    ("AnyTLS", "anytls"),
    ("Naive HTTPS", "naive"),
    ("SOCKS5", "socks5"),
    ("Hysteria2", "hysteria2"),
    ("TUIC v5", "tuic"),
    ("WireGuard", "wireguard"),
    ("Argo XHTTP", "argo_xhttp"),
)

MANUAL_IMPORT_LAYERS = frozenset({"tier1", "tier2", "tier3", "exit"})
MANUAL_NODE_KINDS = frozenset({"single", "multi"})

@dataclass(frozen=True)
class ImportWorkspaceMode:
    key: str
    label: str
    method: str
    kind: str


@dataclass(frozen=True)
class ImportEditorModeState:
    placeholder: str
    editable: bool
    apply_text: str
    multiprotocol: bool


@dataclass(frozen=True)
class ImportPasteCopy:
    placeholder: str
    hint: str
    help_text: str


def import_workspace_modes(layer: str) -> tuple[ImportWorkspaceMode, ...]:
    """Return the task pages exposed by the import workspace for one layer.

    The manual page keeps ``kind`` empty because the current protocol resolves
    the existing backend kind. Source management stays in Settings.
    """
    modes = [
        ImportWorkspaceMode("paste", "粘贴", "paste", "nodes"),
        ImportWorkspaceMode("manual", "手动", "manual", ""),
    ]
    if layer in {"tier2", "tier3", "exit"}:
        modes.append(ImportWorkspaceMode("subscription", "订阅", "subscription", "subscription"))
    return tuple(modes)


def import_method_options(layer: str) -> list[tuple[str, str]]:
    options = [
        ("粘贴", "paste"),
        ("手动", "manual"),
    ]
    if layer in {"tier2", "tier3", "exit"}:
        options.append(("URL", "subscription"))
    return options


def manual_protocol_options(
    layer: str,
    node_kind: str = "single",
) -> list[tuple[str, str]]:
    """Return protocols supported by one explicit manual editor kind.

    The default preserves the legacy one-argument call as the single-node
    editor.  Multi-protocol nodes belong to Tier1 only.  Argo XHTTP is a
    single-protocol choice because its service path is special and must not be
    mixed into a fallback chain.  Unsupported layer/kind combinations expose
    no choices instead of silently falling back to a different editor.
    """
    if layer not in MANUAL_IMPORT_LAYERS or node_kind not in MANUAL_NODE_KINDS:
        return []
    if node_kind == "multi" and layer != "tier1":
        return []
    if layer == "tier1":
        return [
            option
            for option in MANUAL_PROTOCOL_OPTIONS
            if node_kind == "single" or option[1] != "argo_xhttp"
        ]
    return [option for option in MANUAL_PROTOCOL_OPTIONS if option[1] != "argo_xhttp"]


def import_kind_from_selection(method: str, protocol: str) -> str:
    if method == "subscription":
        return "subscription"
    if method == "manual":
        # The unified manual editor starts without a protocol.  A concrete
        # protocol still maps to its legacy backend kind for callers that use
        # the compatibility path.
        return protocol or "manual"
    return "nodes"


def import_method_for_kind(kind: str) -> str:
    if kind == "subscription":
        return "subscription"
    if kind == "nodes":
        return "paste"
    return "manual"


def import_paste_copy(layer: str) -> ImportPasteCopy:
    del layer
    return ImportPasteCopy(
        placeholder="粘贴",
        hint="",
        help_text="",
    )


def structured_shared_copy(layer: str, *, multiprotocol: bool) -> tuple[str, str]:
    del layer, multiprotocol
    return ("", "")


def structured_shared_field_labels(*, multiprotocol: bool) -> dict[str, str]:
    if multiprotocol:
        return {
            "name": "名称",
            "server": "地址",
            "port": "端口",
            "username": "用户名",
            "sni": "SNI",
            "fingerprint": "fp",
        }
    return {
        "name": "名称",
        "server": "地址",
        "port": "端口",
        "username": "用户名",
        "sni": "SNI",
        "fingerprint": "fp",
    }


def import_editor_mode_state(mode: str) -> ImportEditorModeState:
    layer, _separator, _kind = mode.partition("_")
    paste_placeholder = import_paste_copy(layer).placeholder
    layer_defaults = {
        "tier1": (paste_placeholder, True, "导入"),
        "tier2": (paste_placeholder, True, "导入"),
        "tier3": (paste_placeholder, True, "导入"),
        "exit": (paste_placeholder, True, "导入"),
    }
    settings = {
        "tier1_nodes": (paste_placeholder, True, "导入"),
        "tier2_nodes": (paste_placeholder, True, "导入"),
        "tier2_subscription": ("URL", True, "添加"),
        "tier3_subscription": ("URL", True, "添加"),
        "tier3_nodes": (paste_placeholder, True, "导入"),
        "exit_nodes": (paste_placeholder, True, "导入"),
        "exit_subscription": ("URL", True, "添加"),
    }
    placeholder, editable, apply_text = settings.get(mode, layer_defaults.get(layer, layer_defaults["tier1"]))
    multiprotocol = mode == "tier1_multiprotocol"
    return ImportEditorModeState(
        placeholder=placeholder,
        editable=editable,
        apply_text="导入" if multiprotocol else apply_text,
        multiprotocol=multiprotocol,
    )


def subscription_urls_from_inputs(
    *,
    panel_visible: bool,
    pending_url: str,
    text: str,
) -> list[str]:
    if panel_visible:
        pending = str(pending_url or "").strip()
        return [pending] if pending else []
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def subscription_tier_for_mode(mode: str) -> str | None:
    return {
        "tier2_subscription": "tier2",
        "tier3_subscription": "tier3",
        "exit_subscription": "exit",
    }.get(mode)


def subscription_import_preview_payload(tier: str, urls: list[str]) -> dict[str, Any]:
    return {"tier": tier, "subscription_count": len(urls), "subscriptions": list(urls)}


def multiprotocol_import_payload(name: str, text: str, fallback_order: str) -> dict[str, str]:
    return {"name": name, "text": text, "fallback_order": fallback_order}


def node_import_tier_for_mode(mode: str) -> str:
    return {
        "tier2_nodes": "tier2",
        "tier3_nodes": "tier3",
        "exit_nodes": "exit",
    }.get(mode, "tier1")


def node_import_preview_payload(mode: str, text: str) -> dict[str, str]:
    tier = node_import_tier_for_mode(mode)
    return {"text": text, "tier": tier, "source": f"native-app-{tier}-import"}


def node_import_apply_payload(
    mode: str,
    text: str,
    expected_plan_id: str | None = None,
) -> dict[str, str]:
    tier = node_import_tier_for_mode(mode)
    source = {
        "tier2": "native-app-tier2-import",
        "tier3": "native-app-tier3a-import",
        "exit": "native-app-exit-import",
    }.get(tier, "native-app-tier1-import")
    payload = {"text": text, "source": source}
    plan_id = str(expected_plan_id or "").strip()
    if plan_id:
        payload["expected_plan_id"] = plan_id
    return payload


def structured_multiprotocol_block(
    protocols: list[str],
    *,
    fallback_order: list[str],
    uri_factory: Callable[[str], str],
    field_value: Callable[[str, str, str], str],
) -> str:
    ordered = fallback_order or protocols
    lines = ["fallback:", f"  {' -> '.join(ordered)}", ""]
    for protocol in protocols:
        lines.append(f"{protocol}:")
        if protocol == "argo_xhttp":
            listen_port = field_value(protocol, "port", "443")
            listen_host = field_value(protocol, "server", "127.0.0.1")
            argo_uuid = field_value(protocol, "uuid", "11111111-1111-1111-1111-111111111111")
            argo_host = field_value(protocol, "host", field_value(protocol, "sni", "example.com"))
            argo_path = field_value(protocol, "path", "/argo-xhttp")
            argo_security = field_value(protocol, "security", "tls")
            argo_sni = (
                field_value(protocol, "sni", argo_host)
                if str(argo_security).strip().lower() == "tls"
                else ""
            )
            argo_fingerprint = (
                field_value(protocol, "fingerprint", "")
                if str(argo_security).strip().lower() == "tls"
                else ""
            )
            lines.extend(
                [
                    f"  listen={listen_host}:{listen_port}",
                    f"  uuid={argo_uuid}",
                    f"  security={argo_security}",
                    f"  host={argo_host}",
                    f"  path={argo_path}",
                    "  mode=packet-up",
                ]
            )
            if argo_sni:
                lines.append(f"  sni={argo_sni}")
            if argo_fingerprint:
                lines.append(f"  fp={argo_fingerprint}")
        else:
            lines.append(f"  uri={uri_factory(protocol)}")
        lines.append("")
    return "\n".join(lines).strip()
