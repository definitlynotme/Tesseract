from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from import_protocol_view import protocol_required_field_keys
from shadowsocks_capabilities import (
    DEFAULT_SHADOWSOCKS_CIPHER,
    validate_shadowsocks_credential,
)


def _identity(text: str) -> str:
    return text


def _format_text(text: str, **values: str) -> str:
    return text.format(**values)


@dataclass(frozen=True)
class StructuredImportUiState:
    layer: str
    kind: str
    protocol: str
    structured: bool
    subscription: bool
    paste: bool
    multiprotocol: bool
    help_text: str


def structured_protocol_key(kind: str, supported_protocols: Iterable[str]) -> str:
    return kind if kind in set(supported_protocols) else ""


def selected_structured_protocols(
    mode: str,
    supported_protocols: Iterable[str],
    *,
    selected_protocols: Iterable[str] = (),
    priority_value: Callable[[str], int] | None = None,
    protocol_order: Iterable[str] = (),
) -> list[str]:
    _layer, _separator, kind = mode.partition("_")
    if kind in {"manual", "multiprotocol"}:
        supported = set(supported_protocols)
        selected = list(
            dict.fromkeys(
                str(protocol)
                for protocol in selected_protocols
                if str(protocol) in supported
            )
        )
        order = {key: index for index, key in enumerate(protocol_order)}
        if priority_value is not None:
            return sorted(selected, key=lambda key: (priority_value(key), order.get(key, 999)))
        if order:
            return sorted(selected, key=lambda key: order.get(key, 999))
        return selected
    protocol = structured_protocol_key(kind, supported_protocols)
    return [protocol] if protocol else []


def structured_import_ui_state(mode: str, supported_protocols: Iterable[str]) -> StructuredImportUiState:
    layer, _, kind = mode.partition("_")
    protocol = structured_protocol_key(kind, supported_protocols)
    structured = bool(protocol or kind in {"manual", "multiprotocol"})
    subscription = kind == "subscription"
    multiprotocol = layer == "tier1" and kind == "multiprotocol"
    help_text = (
        "公共信息只填一次；每个协议在下面的协议页维护自己的端口、密钥与传输细节。"
        if multiprotocol
        else (
            "先添加协议，再在下方维护该协议的端口、密钥与传输细节。"
            if kind == "manual"
            else "普通单协议节点只有一张表单；名称、地址、端口和协议参数都在同一张卡片里填写。"
        )
    )
    return StructuredImportUiState(
        layer=layer,
        kind=kind,
        protocol=protocol,
        structured=structured,
        subscription=subscription,
        paste=not structured and not subscription,
        multiprotocol=multiprotocol,
        help_text=help_text,
    )


def structured_required_fields(
    protocols: Iterable[str],
    *,
    field_value: Callable[[str, str], str] | None = None,
) -> set[str]:
    """Return fields required by the selected protocol shapes.

    When ``field_value`` is supplied, transport and TLS requirements follow
    the live form values.  Without it, protocol defaults are used so callers
    that build the initial form keep a deterministic required/advanced split.
    URI/config parsers remain the final authority after this fast UI check.
    """

    fields = {"name", "server", "port"}
    for protocol in protocols:
        fields.update(protocol_required_field_keys(protocol, field_value))
    return fields


def missing_structured_field_labels(
    protocols: list[str],
    *,
    multiprotocol: bool,
    field_value: Callable[[str, str], str],
    field_label: Callable[[str], str],
    protocol_label: Callable[[str], str],
) -> list[str]:
    missing: list[str] = []
    seen_missing: set[str] = set()
    for protocol in protocols:
        for key in sorted(structured_required_fields([protocol], field_value=field_value)):
            if field_value(protocol, key):
                continue
            label = field_label(key)
            if multiprotocol and len(protocols) > 1 and key not in {"name", "server"}:
                label = f"{protocol_label(protocol)} {label}"
            if label not in seen_missing:
                seen_missing.add(label)
                missing.append(label)
    return missing


def structured_port_values(
    protocols: list[str],
    *,
    multiprotocol: bool,
    shared_port: Callable[[], str],
    protocol_port: Callable[[str], str],
) -> list[tuple[str, str]]:
    # Port now belongs to each protocol, even when only one protocol is
    # enabled.  Keep the legacy arguments so existing callers do not need an
    # interface migration while the unified editor is rolled out.
    return [(f"{protocol}:port", protocol_port(protocol)) for protocol in protocols]


def first_invalid_port_value(port_values: Iterable[tuple[str, str]]) -> tuple[str, str] | None:
    for key, value in port_values:
        if not value:
            continue
        try:
            port = int(value)
        except ValueError:
            return key, value
        if not 1 <= port <= 65535:
            return key, value
    return None


def structured_import_validation_message(
    protocols: list[str],
    *,
    multiprotocol: bool,
    priority_value: Callable[[str], int],
    field_value: Callable[[str, str], str],
    field_label: Callable[[str], str],
    protocol_label: Callable[[str], str],
    shared_port: Callable[[], str],
    protocol_port: Callable[[str], str],
    translate: Callable[[str], str] = _identity,
    format_text: Callable[..., str] = _format_text,
) -> str | None:
    if not protocols:
        return translate("至少选择一个协议。")
    missing = missing_structured_field_labels(
        protocols,
        multiprotocol=multiprotocol,
        field_value=field_value,
        field_label=field_label,
        protocol_label=protocol_label,
    )
    if missing:
        return format_text(translate("请填写必填字段：{fields}"), fields=", ".join(missing))
    port_values = structured_port_values(
        protocols,
        multiprotocol=multiprotocol,
        shared_port=shared_port,
        protocol_port=protocol_port,
    )
    if first_invalid_port_value(port_values) is not None:
        return translate("端口必须是 1-65535 的整数。")
    if "ss" in protocols:
        cipher = field_value("ss", "method") or DEFAULT_SHADOWSOCKS_CIPHER
        credential_issue = validate_shadowsocks_credential(
            cipher,
            field_value("ss", "password"),
        )
        if credential_issue is not None:
            params = credential_issue.params
            if credential_issue.code == "missing_ss_password":
                return translate("Shadowsocks 节点缺少密码。")
            if credential_issue.code == "missing_ss_2022_key":
                return translate("Shadowsocks 2022 节点缺少密钥。")
            if credential_issue.code == "unsupported_cipher":
                return format_text(
                    translate("Shadowsocks cipher“{cipher}”不受当前内置 Mihomo 支持。"),
                    cipher=str(params.get("cipher", cipher)),
                )
            if credential_issue.code == "invalid_ss_2022_key_base64":
                return translate("Shadowsocks 2022 密钥必须是有效 Base64。")
            if credential_issue.code == "invalid_ss_2022_key_length":
                return translate("Shadowsocks 2022 密钥长度不正确。")
    return None
