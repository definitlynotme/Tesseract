from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from shadowsocks_capabilities import (
    DEFAULT_SHADOWSOCKS_CIPHER,
    SHADOWSOCKS_CIPHER_CHOICES,
    SHADOWSOCKS_NONE_NOTICE_CODE,
)


@dataclass(frozen=True)
class ProtocolFieldSpec:
    """Describe one protocol field independently from its Qt widget.

    ``key``, ``label`` and ``placeholder`` intentionally remain the first
    three parameters so existing callers can keep constructing text fields
    positionally.  The semantic metadata lets the unified editor select the
    right native control without encoding protocol-specific UI rules.
    """

    key: str
    label: str
    placeholder: str
    input_type: str = "text"
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    advanced: bool = False


@dataclass(frozen=True)
class ProtocolSectionSpec:
    key: str
    label: str
    fields: tuple[ProtocolFieldSpec, ...]


@dataclass(frozen=True)
class FieldCondition:
    """A small, declarative condition shared by layout and validation.

    ``values`` and ``excluded_values`` perform case-insensitive membership
    checks. ``present`` can instead require an empty or non-empty value.
    Conditions in one placement are combined with AND semantics.
    """

    field_key: str
    values: tuple[str, ...] = ()
    present: bool | None = None
    excluded_values: tuple[str, ...] = ()

    def matches(self, value: object) -> bool:
        normalized = str(value or "").strip()
        if self.values:
            accepted = {str(item).strip().casefold() for item in self.values}
            if normalized.casefold() not in accepted:
                return False
        if self.excluded_values:
            excluded = {str(item).strip().casefold() for item in self.excluded_values}
            if normalized.casefold() in excluded:
                return False
        if self.present is not None and bool(normalized) is not self.present:
            return False
        return True


@dataclass(frozen=True)
class FieldPlacement:
    """Place one existing :class:`ProtocolFieldSpec` in a template grid."""

    field_key: str
    row: int
    column: int = 0
    column_span: int = 1
    required: bool = False
    visible_when: tuple[FieldCondition, ...] = ()
    required_when: tuple[FieldCondition, ...] = ()


@dataclass(frozen=True)
class ProtocolGroupSpec:
    key: str
    label: str
    fields: tuple[FieldPlacement, ...]
    advanced: bool = False


@dataclass(frozen=True)
class ProtocolNoticeSpec:
    code: str
    visible_when: tuple[FieldCondition, ...] = ()


@dataclass(frozen=True)
class ProtocolTemplateSpec:
    protocol_key: str
    groups: tuple[ProtocolGroupSpec, ...]
    columns: int = 2
    notices: tuple[ProtocolNoticeSpec, ...] = ()


ProtocolFieldValue = Callable[[str, str], object]


class SingleProtocolDraft:
    """Session state for one explicitly selected protocol.

    Single- and multi-protocol editors keep separate draft instances.  This
    small model owns only the single editor's selection so switching editor
    kind cannot accidentally reuse or rewrite ``ProtocolOrderDraft`` state.
    """

    def __init__(
        self,
        supported_keys: Iterable[str],
        selected_key: str = "",
    ) -> None:
        self._supported_keys = tuple(dict.fromkeys(str(key) for key in supported_keys if str(key)))
        self._selected_key = ""
        self.select(selected_key)

    @property
    def supported_keys(self) -> tuple[str, ...]:
        return self._supported_keys

    @property
    def selected_key(self) -> str:
        return self._selected_key

    def select(self, protocol_key: str) -> bool:
        if protocol_key not in self._supported_keys:
            return False
        self._selected_key = protocol_key
        return True

    def clear(self) -> None:
        self._selected_key = ""


class ProtocolOrderDraft:
    """Session state for an ordered set of enabled protocols.

    The draft deliberately owns only membership, order, and the active detail
    selection.  Protocol field widgets can therefore keep their text while an
    entry is removed; adding the protocol again only restores its membership
    and never clears the external field draft.
    """

    def __init__(
        self,
        supported_keys: Iterable[str],
        ordered_keys: Iterable[str] = (),
        *,
        active_key: str = "",
    ) -> None:
        self._supported_keys = tuple(dict.fromkeys(str(key) for key in supported_keys if str(key)))
        self._ordered_keys: list[str] = []
        self._active_key = ""
        self.replace(ordered_keys, active_key=active_key)

    @property
    def supported_keys(self) -> tuple[str, ...]:
        return self._supported_keys

    @property
    def ordered_keys(self) -> tuple[str, ...]:
        return tuple(self._ordered_keys)

    @property
    def active_key(self) -> str:
        return self._active_key

    @property
    def available_keys(self) -> tuple[str, ...]:
        selected = set(self._ordered_keys)
        return tuple(key for key in self._supported_keys if key not in selected)

    def replace(self, ordered_keys: Iterable[str], *, active_key: str = "") -> None:
        supported = set(self._supported_keys)
        self._ordered_keys = list(
            dict.fromkeys(str(key) for key in ordered_keys if str(key) in supported)
        )
        self._active_key = active_key if active_key in self._ordered_keys else ""
        if not self._active_key and self._ordered_keys:
            self._active_key = self._ordered_keys[0]

    def add(self, protocol_key: str) -> bool:
        if protocol_key not in self._supported_keys:
            return False
        if protocol_key in self._ordered_keys:
            self._active_key = protocol_key
            return False
        self._ordered_keys.append(protocol_key)
        self._active_key = protocol_key
        return True

    def select(self, protocol_key: str) -> bool:
        if protocol_key not in self._ordered_keys:
            return False
        self._active_key = protocol_key
        return True

    def move(self, protocol_key: str, delta: int) -> bool:
        if protocol_key not in self._ordered_keys or delta == 0:
            return False
        current_index = self._ordered_keys.index(protocol_key)
        target_index = max(0, min(len(self._ordered_keys) - 1, current_index + delta))
        if current_index == target_index:
            return False
        self._ordered_keys.pop(current_index)
        self._ordered_keys.insert(target_index, protocol_key)
        self._active_key = protocol_key
        return True

    def move_up(self, protocol_key: str | None = None) -> bool:
        return self.move(protocol_key or self._active_key, -1)

    def move_down(self, protocol_key: str | None = None) -> bool:
        return self.move(protocol_key or self._active_key, 1)

    def remove(self, protocol_key: str) -> bool:
        if protocol_key not in self._ordered_keys:
            return False
        removed_index = self._ordered_keys.index(protocol_key)
        self._ordered_keys.pop(removed_index)
        if self._active_key == protocol_key:
            if self._ordered_keys:
                next_index = min(removed_index, len(self._ordered_keys) - 1)
                self._active_key = self._ordered_keys[next_index]
            else:
                self._active_key = ""
        return True

    def clear(self) -> None:
        self._ordered_keys.clear()
        self._active_key = ""


SINGLE_PROTOCOL_BASE_FIELDS: tuple[ProtocolFieldSpec, ...] = (
    ProtocolFieldSpec("name", "节点名称", "My-Node"),
    ProtocolFieldSpec("server", "服务器地址", "example.com"),
)


PROTOCOL_SPECS: tuple[ProtocolSectionSpec, ...] = (
    ProtocolSectionSpec(
        "vless_reality",
        "VLESS Reality",
        (
            ProtocolFieldSpec("port", "端口", "443", "integer", minimum=1, maximum=65535),
            ProtocolFieldSpec("uuid", "UUID", "11111111-1111-1111-1111-111111111111"),
            ProtocolFieldSpec(
                "network",
                "传输 (tcp/ws/grpc/h2)",
                "tcp",
                "enum",
                ("tcp", "ws", "grpc", "h2"),
            ),
            ProtocolFieldSpec("encryption", "Encryption", "none"),
            ProtocolFieldSpec(
                "security",
                "Security",
                "reality",
                "enum",
                ("reality", "tls", "none"),
            ),
            ProtocolFieldSpec("flow", "Flow", "xtls-rprx-vision", advanced=True),
            ProtocolFieldSpec("public_key", "Reality Public Key (pbk)", "PUBLIC_KEY"),
            ProtocolFieldSpec("short_id", "Reality Short ID (sid)", "SHORT_ID"),
            ProtocolFieldSpec("sni", "SNI", ""),
            ProtocolFieldSpec("fingerprint", "指纹 fp", "", advanced=True),
            ProtocolFieldSpec("alpn", "ALPN", "h2,http/1.1", advanced=True),
            ProtocolFieldSpec("host", "Host (ws/h2)", ""),
            ProtocolFieldSpec("path", "Path (ws/h2)", "/ws"),
            ProtocolFieldSpec("grpc_service_name", "gRPC serviceName", ""),
        ),
    ),
    ProtocolSectionSpec(
        "vmess",
        "VMess WS TLS",
        (
            ProtocolFieldSpec("port", "端口", "443", "integer", minimum=1, maximum=65535),
            ProtocolFieldSpec("uuid", "UUID", "11111111-1111-1111-1111-111111111111"),
            ProtocolFieldSpec(
                "alter_id",
                "alterId",
                "0",
                "integer",
                minimum=0,
                maximum=2_147_483_647,
                advanced=True,
            ),
            ProtocolFieldSpec("scy", "VMess cipher (scy)", "auto", advanced=True),
            ProtocolFieldSpec(
                "network",
                "传输 (ws/grpc/h2/tcp)",
                "ws",
                "enum",
                ("ws", "grpc", "h2", "tcp"),
            ),
            ProtocolFieldSpec(
                "security",
                "Security (tls/none)",
                "tls",
                "enum",
                ("tls", "none"),
            ),
            ProtocolFieldSpec("sni", "SNI", ""),
            ProtocolFieldSpec("host", "Host", ""),
            ProtocolFieldSpec("path", "Path", "/ws"),
            ProtocolFieldSpec("fingerprint", "指纹 fp", "", advanced=True),
            ProtocolFieldSpec("alpn", "ALPN", "", advanced=True),
            ProtocolFieldSpec("grpc_service_name", "gRPC serviceName", ""),
        ),
    ),
    ProtocolSectionSpec(
        "trojan",
        "Trojan TLS",
        (
            ProtocolFieldSpec("port", "端口", "443", "integer", minimum=1, maximum=65535),
            ProtocolFieldSpec("password", "密码", "PASSWORD", "password"),
            ProtocolFieldSpec("network", "传输 (tcp/ws)", "tcp", "enum", ("tcp", "ws")),
            ProtocolFieldSpec("sni", "SNI", ""),
            ProtocolFieldSpec("host", "Host (ws)", ""),
            ProtocolFieldSpec("path", "Path (ws)", "/ws"),
            ProtocolFieldSpec("fingerprint", "指纹 fp", "", advanced=True),
            ProtocolFieldSpec("alpn", "ALPN", "", advanced=True),
            ProtocolFieldSpec(
                "skip_cert_verify",
                "skip-cert-verify",
                "false",
                "boolean",
                advanced=True,
            ),
        ),
    ),
    ProtocolSectionSpec(
        "ss",
        "Shadowsocks",
        (
            ProtocolFieldSpec("port", "端口", "443", "integer", minimum=1, maximum=65535),
            ProtocolFieldSpec("password", "密码", "PASSWORD", "password"),
            ProtocolFieldSpec(
                "method",
                "Cipher",
                DEFAULT_SHADOWSOCKS_CIPHER,
                "enum",
                SHADOWSOCKS_CIPHER_CHOICES,
            ),
        ),
    ),
    ProtocolSectionSpec(
        "anytls",
        "AnyTLS",
        (
            ProtocolFieldSpec("port", "端口", "443", "integer", minimum=1, maximum=65535),
            ProtocolFieldSpec("password", "密码", "PASSWORD", "password"),
            ProtocolFieldSpec("sni", "SNI", ""),
            ProtocolFieldSpec("alpn", "ALPN", "", advanced=True),
            ProtocolFieldSpec("fingerprint", "指纹 fp", "", advanced=True),
            ProtocolFieldSpec(
                "skip_cert_verify",
                "skip-cert-verify",
                "false",
                "boolean",
                advanced=True,
            ),
        ),
    ),
    ProtocolSectionSpec(
        "naive",
        "Naive HTTPS",
        (
            ProtocolFieldSpec("port", "端口", "443", "integer", minimum=1, maximum=65535),
            ProtocolFieldSpec("username", "用户名", ""),
            ProtocolFieldSpec("password", "密码", "PASSWORD", "password"),
            ProtocolFieldSpec(
                "security",
                "Security (tls/none)",
                "tls",
                "enum",
                ("tls", "none"),
            ),
            ProtocolFieldSpec("sni", "SNI", ""),
        ),
    ),
    ProtocolSectionSpec(
        "socks5",
        "SOCKS5",
        (
            ProtocolFieldSpec("port", "端口", "1080", "integer", minimum=1, maximum=65535),
            ProtocolFieldSpec("username", "用户名", ""),
            ProtocolFieldSpec("password", "密码", "PASSWORD", "password"),
        ),
    ),
    ProtocolSectionSpec(
        "hysteria2",
        "Hysteria2",
        (
            ProtocolFieldSpec("port", "端口", "443", "integer", minimum=1, maximum=65535),
            ProtocolFieldSpec("password", "密码", "PASSWORD", "password"),
            ProtocolFieldSpec("sni", "SNI", ""),
            ProtocolFieldSpec("obfs", "Obfs (salamander)", "salamander", advanced=True),
            ProtocolFieldSpec(
                "obfs_password",
                "Obfs password",
                "OBFS_PASSWORD",
                "password",
                advanced=True,
            ),
            ProtocolFieldSpec("alpn", "ALPN", "", advanced=True),
            ProtocolFieldSpec(
                "skip_cert_verify",
                "skip-cert-verify",
                "false",
                "boolean",
                advanced=True,
            ),
        ),
    ),
    ProtocolSectionSpec(
        "tuic",
        "TUIC v5",
        (
            ProtocolFieldSpec("port", "端口", "443", "integer", minimum=1, maximum=65535),
            ProtocolFieldSpec("uuid", "UUID", "11111111-1111-1111-1111-111111111111"),
            ProtocolFieldSpec("password", "Token / 密码", "PASSWORD", "password"),
            ProtocolFieldSpec("sni", "SNI", ""),
            ProtocolFieldSpec("alpn", "ALPN", "", advanced=True),
            ProtocolFieldSpec(
                "udp_relay_mode",
                "udp_relay_mode",
                "native",
                "enum",
                ("native", "quic"),
                advanced=True,
            ),
            ProtocolFieldSpec(
                "congestion_control",
                "congestion_control",
                "bbr",
                "enum",
                ("bbr", "cubic", "new_reno"),
                advanced=True,
            ),
            ProtocolFieldSpec(
                "skip_cert_verify",
                "skip-cert-verify",
                "false",
                "boolean",
                advanced=True,
            ),
        ),
    ),
    ProtocolSectionSpec(
        "wireguard",
        "WireGuard",
        (
            ProtocolFieldSpec("port", "端口", "51820", "integer", minimum=1, maximum=65535),
            ProtocolFieldSpec(
                "private_key",
                "Private Key (本机)",
                "PRIVATE_KEY",
                "password",
            ),
            ProtocolFieldSpec("public_key", "Peer Public Key", "PEER_PUBLIC_KEY"),
            ProtocolFieldSpec("local_address", "本机地址", "10.0.0.2/32"),
            ProtocolFieldSpec("allowed_ips", "Allowed IPs", "0.0.0.0/0,::/0", advanced=True),
            ProtocolFieldSpec("reserved", "Reserved", "0,0,0", advanced=True),
            ProtocolFieldSpec(
                "mtu",
                "MTU",
                "1408",
                "integer",
                minimum=576,
                maximum=65535,
                advanced=True,
            ),
        ),
    ),
    ProtocolSectionSpec(
        "argo_xhttp",
        "Argo XHTTP (本地)",
        (
            ProtocolFieldSpec("port", "本地监听端口", "2080", "integer", minimum=1, maximum=65535),
            ProtocolFieldSpec("uuid", "UUID", "11111111-1111-1111-1111-111111111111"),
            ProtocolFieldSpec("network", "传输", "xhttp", "enum", ("xhttp",)),
            ProtocolFieldSpec(
                "security",
                "Security (tls/none)",
                "tls",
                "enum",
                ("tls", "none"),
            ),
            ProtocolFieldSpec("sni", "SNI", ""),
            ProtocolFieldSpec("host", "Host", ""),
            ProtocolFieldSpec("path", "Path", "/argo-xhttp"),
            ProtocolFieldSpec("fingerprint", "指纹 fp", "", advanced=True),
        ),
    ),
)


_NETWORK_WS_OR_H2 = (FieldCondition("network", ("ws", "h2")),)
_NETWORK_WS = (FieldCondition("network", ("ws",)),)
_NETWORK_GRPC = (FieldCondition("network", ("grpc",)),)
_SECURITY_TLS = (FieldCondition("security", ("tls",)),)
_SECURITY_TLS_OR_REALITY = (FieldCondition("security", ("tls", "reality")),)
_SECURITY_REALITY = (FieldCondition("security", ("reality",)),)
_OBFS_PRESENT = (FieldCondition("obfs", present=True),)
_SS_PASSWORD_USED = (FieldCondition("method", excluded_values=("none",)),)
_SS_NONE = (FieldCondition("method", ("none",)),)
_SOCKS_USERNAME_PRESENT = (FieldCondition("username", present=True),)
_SOCKS_PASSWORD_PRESENT = (FieldCondition("password", present=True),)


PROTOCOL_TEMPLATE_SPECS: tuple[ProtocolTemplateSpec, ...] = (
    ProtocolTemplateSpec(
        "vless_reality",
        (
            ProtocolGroupSpec(
                "identity",
                "身份",
                (
                    FieldPlacement("port", 0, column_span=2, required=True),
                    FieldPlacement("uuid", 1, column_span=2, required=True),
                ),
            ),
            ProtocolGroupSpec(
                "transport",
                "传输",
                (
                    FieldPlacement("network", 0, required=True),
                    FieldPlacement("encryption", 0, 1, required=True),
                    FieldPlacement(
                        "host",
                        1,
                        visible_when=_NETWORK_WS_OR_H2,
                        required_when=_NETWORK_WS_OR_H2,
                    ),
                    FieldPlacement(
                        "path",
                        1,
                        1,
                        visible_when=_NETWORK_WS_OR_H2,
                        required_when=_NETWORK_WS_OR_H2,
                    ),
                    FieldPlacement(
                        "grpc_service_name",
                        1,
                        column_span=2,
                        visible_when=_NETWORK_GRPC,
                        required_when=_NETWORK_GRPC,
                    ),
                ),
            ),
            ProtocolGroupSpec(
                "security",
                "安全",
                (
                    FieldPlacement("security", 0, column_span=2, required=True),
                    FieldPlacement(
                        "sni",
                        1,
                        column_span=2,
                        visible_when=_SECURITY_TLS_OR_REALITY,
                        required_when=_SECURITY_TLS_OR_REALITY,
                    ),
                ),
            ),
            ProtocolGroupSpec(
                "reality",
                "Reality 参数",
                (
                    FieldPlacement(
                        "public_key",
                        0,
                        column_span=2,
                        visible_when=_SECURITY_REALITY,
                        required_when=_SECURITY_REALITY,
                    ),
                    FieldPlacement(
                        "short_id",
                        1,
                        column_span=2,
                        visible_when=_SECURITY_REALITY,
                        required_when=_SECURITY_REALITY,
                    ),
                ),
            ),
            ProtocolGroupSpec(
                "tls_advanced",
                "TLS 高级",
                (
                    FieldPlacement(
                        "fingerprint",
                        0,
                        visible_when=_SECURITY_TLS_OR_REALITY,
                    ),
                    FieldPlacement(
                        "alpn",
                        0,
                        1,
                        visible_when=_SECURITY_TLS_OR_REALITY,
                    ),
                    FieldPlacement(
                        "flow",
                        1,
                        column_span=2,
                        visible_when=_SECURITY_REALITY,
                    ),
                ),
                advanced=True,
            ),
        ),
    ),
    ProtocolTemplateSpec(
        "vmess",
        (
            ProtocolGroupSpec(
                "identity",
                "身份",
                (
                    FieldPlacement("port", 0, column_span=2, required=True),
                    FieldPlacement("uuid", 1, column_span=2, required=True),
                ),
            ),
            ProtocolGroupSpec(
                "transport",
                "传输",
                (
                    FieldPlacement("network", 0, column_span=2, required=True),
                    FieldPlacement(
                        "host",
                        1,
                        visible_when=_NETWORK_WS_OR_H2,
                        required_when=_NETWORK_WS_OR_H2,
                    ),
                    FieldPlacement(
                        "path",
                        1,
                        1,
                        visible_when=_NETWORK_WS_OR_H2,
                        required_when=_NETWORK_WS_OR_H2,
                    ),
                    FieldPlacement(
                        "grpc_service_name",
                        1,
                        column_span=2,
                        visible_when=_NETWORK_GRPC,
                        required_when=_NETWORK_GRPC,
                    ),
                ),
            ),
            ProtocolGroupSpec(
                "tls",
                "TLS",
                (
                    FieldPlacement("security", 0, column_span=2, required=True),
                    FieldPlacement(
                        "sni",
                        1,
                        column_span=2,
                        visible_when=_SECURITY_TLS,
                        required_when=_SECURITY_TLS,
                    ),
                ),
            ),
            ProtocolGroupSpec(
                "vmess_advanced",
                "VMess 高级",
                (
                    FieldPlacement("alter_id", 0),
                    FieldPlacement("scy", 0, 1),
                    FieldPlacement("fingerprint", 1, visible_when=_SECURITY_TLS),
                    FieldPlacement("alpn", 1, 1, visible_when=_SECURITY_TLS),
                ),
                advanced=True,
            ),
        ),
    ),
    ProtocolTemplateSpec(
        "trojan",
        (
            ProtocolGroupSpec(
                "authentication",
                "端口与认证",
                (
                    FieldPlacement("port", 0, required=True),
                    FieldPlacement("password", 0, 1, required=True),
                ),
            ),
            ProtocolGroupSpec(
                "transport",
                "传输",
                (
                    FieldPlacement("network", 0, column_span=2, required=True),
                    FieldPlacement(
                        "host",
                        1,
                        visible_when=_NETWORK_WS,
                        required_when=_NETWORK_WS,
                    ),
                    FieldPlacement(
                        "path",
                        1,
                        1,
                        visible_when=_NETWORK_WS,
                        required_when=_NETWORK_WS,
                    ),
                ),
            ),
            ProtocolGroupSpec(
                "tls",
                "TLS",
                (FieldPlacement("sni", 0, column_span=2),),
            ),
            ProtocolGroupSpec(
                "tls_advanced",
                "TLS 高级",
                (
                    FieldPlacement("fingerprint", 0),
                    FieldPlacement("alpn", 0, 1),
                    FieldPlacement("skip_cert_verify", 1, column_span=2),
                ),
                advanced=True,
            ),
        ),
    ),
    ProtocolTemplateSpec(
        "ss",
        (
            ProtocolGroupSpec(
                "cipher",
                "加密与认证",
                (
                    FieldPlacement("port", 0, required=True),
                    FieldPlacement("method", 0, 1, required=True),
                    FieldPlacement(
                        "password",
                        1,
                        column_span=2,
                        visible_when=_SS_PASSWORD_USED,
                        required_when=_SS_PASSWORD_USED,
                    ),
                ),
            ),
        ),
        notices=(ProtocolNoticeSpec(SHADOWSOCKS_NONE_NOTICE_CODE, _SS_NONE),),
    ),
    ProtocolTemplateSpec(
        "anytls",
        (
            ProtocolGroupSpec(
                "connection",
                "连接与认证",
                (
                    FieldPlacement("port", 0, required=True),
                    FieldPlacement("sni", 0, 1),
                    FieldPlacement("password", 1, column_span=2, required=True),
                ),
            ),
            ProtocolGroupSpec(
                "tls_advanced",
                "TLS 高级",
                (
                    FieldPlacement("alpn", 0),
                    FieldPlacement("fingerprint", 0, 1),
                    FieldPlacement("skip_cert_verify", 1, column_span=2),
                ),
                advanced=True,
            ),
        ),
    ),
    ProtocolTemplateSpec(
        "naive",
        (
            ProtocolGroupSpec(
                "connection",
                "连接",
                (
                    FieldPlacement("port", 0, required=True),
                    FieldPlacement("security", 0, 1, required=True),
                    FieldPlacement(
                        "sni",
                        1,
                        column_span=2,
                        visible_when=_SECURITY_TLS,
                        required_when=_SECURITY_TLS,
                    ),
                ),
            ),
            ProtocolGroupSpec(
                "authentication",
                "身份认证",
                (
                    FieldPlacement("username", 0, required=True),
                    FieldPlacement("password", 0, 1, required=True),
                ),
            ),
        ),
    ),
    ProtocolTemplateSpec(
        "socks5",
        (
            ProtocolGroupSpec(
                "authentication",
                "连接与认证",
                (
                    FieldPlacement("port", 0, required=True),
                    FieldPlacement("username", 0, 1, required_when=_SOCKS_PASSWORD_PRESENT),
                    FieldPlacement(
                        "password",
                        1,
                        column_span=2,
                        required_when=_SOCKS_USERNAME_PRESENT,
                    ),
                ),
            ),
        ),
    ),
    ProtocolTemplateSpec(
        "hysteria2",
        (
            ProtocolGroupSpec(
                "connection",
                "连接与认证",
                (
                    FieldPlacement("port", 0, required=True),
                    FieldPlacement("sni", 0, 1),
                    FieldPlacement("password", 1, column_span=2, required=True),
                ),
            ),
            ProtocolGroupSpec(
                "obfuscation",
                "混淆",
                (
                    FieldPlacement("obfs", 0, column_span=2),
                    FieldPlacement(
                        "obfs_password",
                        1,
                        column_span=2,
                        visible_when=_OBFS_PRESENT,
                        required_when=_OBFS_PRESENT,
                    ),
                ),
            ),
            ProtocolGroupSpec(
                "tls_advanced",
                "TLS 高级",
                (
                    FieldPlacement("alpn", 0),
                    FieldPlacement("skip_cert_verify", 0, 1),
                ),
                advanced=True,
            ),
        ),
    ),
    ProtocolTemplateSpec(
        "tuic",
        (
            ProtocolGroupSpec(
                "connection",
                "连接",
                (
                    FieldPlacement("port", 0, required=True),
                    FieldPlacement("sni", 0, 1),
                ),
            ),
            ProtocolGroupSpec(
                "authentication",
                "身份认证",
                (
                    FieldPlacement("uuid", 0, column_span=2, required=True),
                    FieldPlacement("password", 1, column_span=2, required=True),
                ),
            ),
            ProtocolGroupSpec(
                "quic_advanced",
                "QUIC 调优",
                (
                    FieldPlacement("udp_relay_mode", 0),
                    FieldPlacement("congestion_control", 0, 1),
                    FieldPlacement("alpn", 1),
                    FieldPlacement("skip_cert_verify", 1, 1),
                ),
                advanced=True,
            ),
        ),
    ),
    ProtocolTemplateSpec(
        "wireguard",
        (
            ProtocolGroupSpec(
                "endpoint",
                "端点",
                (FieldPlacement("port", 0, column_span=2, required=True),),
            ),
            ProtocolGroupSpec(
                "keys",
                "密钥",
                (
                    FieldPlacement("private_key", 0, column_span=2, required=True),
                    FieldPlacement("public_key", 1, column_span=2, required=True),
                ),
            ),
            ProtocolGroupSpec(
                "tunnel",
                "隧道地址",
                (FieldPlacement("local_address", 0, column_span=2, required=True),),
            ),
            ProtocolGroupSpec(
                "routing",
                "路由",
                (FieldPlacement("allowed_ips", 0, column_span=2),),
            ),
            ProtocolGroupSpec(
                "advanced",
                "高级",
                (
                    FieldPlacement("reserved", 0),
                    FieldPlacement("mtu", 0, 1),
                ),
                advanced=True,
            ),
        ),
    ),
    ProtocolTemplateSpec(
        "argo_xhttp",
        (
            ProtocolGroupSpec(
                "local_listener",
                "本地监听",
                (FieldPlacement("port", 0, column_span=2, required=True),),
            ),
            ProtocolGroupSpec(
                "identity",
                "身份",
                (FieldPlacement("uuid", 0, column_span=2, required=True),),
            ),
            ProtocolGroupSpec(
                "upstream",
                "XHTTP 上游",
                (
                    FieldPlacement("host", 0, column_span=2, required=True),
                    FieldPlacement("path", 1, column_span=2, required=True),
                ),
            ),
            ProtocolGroupSpec(
                "tls",
                "TLS",
                (
                    FieldPlacement("security", 0, column_span=2, required=True),
                    FieldPlacement(
                        "sni",
                        1,
                        column_span=2,
                        visible_when=_SECURITY_TLS,
                        required_when=_SECURITY_TLS,
                    ),
                ),
            ),
            ProtocolGroupSpec(
                "tls_advanced",
                "TLS 高级",
                (
                    FieldPlacement(
                        "fingerprint",
                        0,
                        column_span=2,
                        visible_when=_SECURITY_TLS,
                    ),
                ),
                advanced=True,
            ),
        ),
    ),
)


PROTOCOL_FIELD_DEFAULTS: dict[str, dict[str, str]] = {
    "vless_reality": {"network": "tcp", "security": "reality"},
    "vmess": {"network": "ws", "security": "tls"},
    "trojan": {"network": "tcp", "security": "tls"},
    "ss": {"method": DEFAULT_SHADOWSOCKS_CIPHER},
    "naive": {"security": "tls"},
    "argo_xhttp": {"network": "xhttp", "security": "tls"},
}

_PROTOCOL_TEMPLATE_BY_KEY: dict[str, ProtocolTemplateSpec] = {
    template.protocol_key: template for template in PROTOCOL_TEMPLATE_SPECS
}


def protocol_template_spec(protocol_key: str) -> ProtocolTemplateSpec | None:
    return _PROTOCOL_TEMPLATE_BY_KEY.get(protocol_key)


def protocol_effective_field_value(
    protocol_key: str,
    field_key: str,
    field_value: ProtocolFieldValue | None = None,
) -> str:
    value = ""
    if field_value is not None:
        value = str(field_value(protocol_key, field_key) or "").strip()
    if value:
        return value
    return PROTOCOL_FIELD_DEFAULTS.get(protocol_key, {}).get(field_key, "")


def _placement_conditions_match(
    protocol_key: str,
    conditions: tuple[FieldCondition, ...],
    field_value: ProtocolFieldValue | None,
) -> bool:
    return all(
        condition.matches(
            protocol_effective_field_value(protocol_key, condition.field_key, field_value)
        )
        for condition in conditions
    )


def _protocol_field_placement(
    protocol_key: str,
    field_key: str,
) -> FieldPlacement | None:
    template = protocol_template_spec(protocol_key)
    if template is None:
        return None
    for group in template.groups:
        for placement in group.fields:
            if placement.field_key == field_key:
                return placement
    return None


def protocol_field_is_visible(
    protocol_key: str,
    field_key: str,
    field_value: ProtocolFieldValue | None = None,
) -> bool:
    placement = _protocol_field_placement(protocol_key, field_key)
    if placement is None:
        return False
    return _placement_conditions_match(protocol_key, placement.visible_when, field_value)


def protocol_field_is_required(
    protocol_key: str,
    field_key: str,
    field_value: ProtocolFieldValue | None = None,
) -> bool:
    placement = _protocol_field_placement(protocol_key, field_key)
    if placement is None or not protocol_field_is_visible(protocol_key, field_key, field_value):
        return False
    if placement.required:
        return True
    return bool(placement.required_when) and _placement_conditions_match(
        protocol_key,
        placement.required_when,
        field_value,
    )


def protocol_visible_field_keys(
    protocol_key: str,
    field_value: ProtocolFieldValue | None = None,
) -> tuple[str, ...]:
    template = protocol_template_spec(protocol_key)
    if template is None:
        return ()
    return tuple(
        placement.field_key
        for group in template.groups
        for placement in group.fields
        if protocol_field_is_visible(protocol_key, placement.field_key, field_value)
    )


def protocol_required_field_keys(
    protocol_key: str,
    field_value: ProtocolFieldValue | None = None,
) -> tuple[str, ...]:
    return tuple(
        field_key
        for field_key in protocol_visible_field_keys(protocol_key, field_value)
        if protocol_field_is_required(protocol_key, field_key, field_value)
    )


def protocol_visible_groups(
    protocol_key: str,
    field_value: ProtocolFieldValue | None = None,
) -> tuple[ProtocolGroupSpec, ...]:
    template = protocol_template_spec(protocol_key)
    if template is None:
        return ()
    visible = set(protocol_visible_field_keys(protocol_key, field_value))
    return tuple(
        group for group in template.groups if any(field.field_key in visible for field in group.fields)
    )


def protocol_visible_notice_codes(
    protocol_key: str,
    field_value: ProtocolFieldValue | None = None,
) -> tuple[str, ...]:
    template = protocol_template_spec(protocol_key)
    if template is None:
        return ()
    return tuple(
        notice.code
        for notice in template.notices
        if _placement_conditions_match(protocol_key, notice.visible_when, field_value)
    )


def protocol_condition_field_keys(protocol_key: str) -> tuple[str, ...]:
    """Return the fields that can change this protocol's visible form state."""

    template = protocol_template_spec(protocol_key)
    if template is None:
        return ()
    keys = {
        condition.field_key
        for group in template.groups
        for placement in group.fields
        for condition in (*placement.visible_when, *placement.required_when)
    }
    keys.update(
        condition.field_key
        for notice in template.notices
        for condition in notice.visible_when
    )
    return tuple(sorted(keys))


PROTOCOL_DISPLAY_LABELS: dict[str, str] = {
    **{spec.key: spec.label for spec in PROTOCOL_SPECS},
    "argo_xhttp": "Argo XHTTP",
}


def protocol_display_label(protocol_key: str) -> str:
    return PROTOCOL_DISPLAY_LABELS.get(protocol_key, protocol_key)


def protocol_legacy_override_field_map(protocol_key: str, field_keys: Iterable[str]) -> dict[str, str]:
    keys = set(field_keys)
    result: dict[str, str] = {}
    if "port" in keys:
        result["port"] = "port"
    if protocol_key in {"vless_reality", "vmess", "argo_xhttp", "tuic"} and "uuid" in keys:
        result["secret"] = "uuid"
    elif protocol_key == "wireguard" and "private_key" in keys:
        result["secret"] = "private_key"
    elif "password" in keys:
        result["secret"] = "password"
    if protocol_key == "wireguard" and "public_key" in keys:
        result["path"] = "public_key"
    elif "path" in keys:
        result["path"] = "path"
    return result
