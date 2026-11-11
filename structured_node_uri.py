from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from import_protocol_view import PROTOCOL_SPECS, protocol_visible_field_keys
from shadowsocks_capabilities import DEFAULT_SHADOWSOCKS_CIPHER, normalize_shadowsocks_cipher


FieldReader = Callable[[str, str], str]
TRUE_TEXT_VALUES = {"1", "true", "yes", "on"}
_PROTOCOL_FIELD_KEYS = {
    spec.key: frozenset(field.key for field in spec.fields) for spec in PROTOCOL_SPECS
}


@dataclass(frozen=True)
class StructuredNodeFields:
    name: str
    server: str
    port: str
    uuid_value: str
    username: str
    password: str
    method: str
    encryption: str
    security: str
    network: str
    flow: str
    fingerprint: str
    alpn: str
    alter_id: str
    scy: str
    sni: str
    host: str
    path: str
    grpc_service_name: str
    public_key: str
    short_id: str
    private_key: str
    local_address: str
    allowed_ips: str
    reserved: str
    mtu: str
    obfs: str
    obfs_password: str
    udp_relay_mode: str
    congestion_control: str
    skip_cert_verify: bool


def _resolve_structured_node_fields(field: FieldReader, *, protocol: str = "") -> StructuredNodeFields:
    server = field("server", "example.com")
    sni = field("sni", server)
    method = field("method", DEFAULT_SHADOWSOCKS_CIPHER)
    username_default = "" if protocol == "socks5" else "user"
    password_default = (
        ""
        if protocol == "socks5"
        or (protocol == "ss" and normalize_shadowsocks_cipher(method) == "none")
        else "PASSWORD"
    )
    return StructuredNodeFields(
        name=field("name", "My-Node"),
        server=server,
        port=field("port", "443"),
        uuid_value=field("uuid", "11111111-1111-1111-1111-111111111111"),
        username=field("username", username_default),
        password=field("password", password_default),
        method=method,
        encryption=field("encryption", "none"),
        security=field("security", "tls"),
        network=field("network", "tcp"),
        flow=field("flow", ""),
        fingerprint=field("fingerprint", "chrome"),
        alpn=field("alpn", ""),
        alter_id=field("alter_id", "0"),
        scy=field("scy", "auto"),
        sni=sni,
        host=field("host", sni),
        path=field("path", "/ws"),
        grpc_service_name=field("grpc_service_name", ""),
        public_key=field("public_key", "PUBLIC_KEY"),
        short_id=field("short_id", ""),
        private_key=field("private_key", "PRIVATE_KEY"),
        local_address=field("local_address", "10.0.0.2/32"),
        allowed_ips=field("allowed_ips", "0.0.0.0/0,::/0"),
        reserved=field("reserved", ""),
        mtu=field("mtu", ""),
        obfs=field("obfs", ""),
        obfs_password=field("obfs_password", ""),
        udp_relay_mode=field("udp_relay_mode", ""),
        congestion_control=field("congestion_control", ""),
        skip_cert_verify=str(field("skip_cert_verify", "")).strip().lower() in TRUE_TEXT_VALUES,
    )


def _fragment(fields: StructuredNodeFields) -> str:
    return quote(fields.name, safe="")


def _build_query(items: list[tuple[str, str]]) -> str:
    return urlencode([(key, value) for key, value in items if str(value) != ""], doseq=True)


def build_structured_node_uri(protocol: str, field: FieldReader) -> str:
    visible_fields = set(
        protocol_visible_field_keys(
            protocol,
            lambda _protocol, key: field(key, ""),
        )
    )
    known_fields = _PROTOCOL_FIELD_KEYS.get(protocol, frozenset())

    def active_field(key: str, default: str) -> str:
        if key in known_fields and key not in visible_fields:
            return ""
        if protocol == "naive" and key == "sni":
            return field(key, "")
        return field(key, default)

    fields = _resolve_structured_node_fields(active_field, protocol=protocol)
    if protocol == "vless_reality":
        return _build_vless_reality_uri(fields)
    if protocol == "vmess":
        return _build_vmess_uri(fields)
    if protocol == "trojan":
        return _build_trojan_uri(fields)
    if protocol == "ss":
        return f"ss://{fields.method}:{fields.password}@{fields.server}:{fields.port}#{_fragment(fields)}"
    if protocol == "anytls":
        return _build_anytls_uri(fields)
    if protocol == "naive":
        security = fields.security or "tls"
        query = _build_query(
            [
                ("security", security),
                ("sni", fields.sni if security == "tls" else ""),
            ]
        )
        return (
            f"naive://{quote(fields.username, safe='')}:{quote(fields.password, safe='')}"
            f"@{fields.server}:{fields.port}?{query}#{_fragment(fields)}"
        )
    if protocol == "socks5":
        credentials = ""
        if fields.username or fields.password:
            credentials = f"{quote(fields.username, safe='')}:{quote(fields.password, safe='')}@"
        return f"socks5://{credentials}{fields.server}:{fields.port}#{_fragment(fields)}"
    if protocol == "hysteria2":
        return _build_hysteria2_uri(fields)
    if protocol == "tuic":
        return _build_tuic_uri(fields)
    if protocol == "wireguard":
        return _build_wireguard_uri(fields)
    raise ValueError(f"unsupported structured protocol: {protocol}")


def _build_vless_reality_uri(fields: StructuredNodeFields) -> str:
    params: list[tuple[str, str]] = [
        ("encryption", fields.encryption or "none"),
        ("security", fields.security or "reality"),
        ("type", fields.network or "tcp"),
        ("pbk", fields.public_key),
        ("sid", fields.short_id),
        ("sni", fields.sni),
        ("fp", fields.fingerprint),
        ("flow", fields.flow),
        ("alpn", fields.alpn),
        ("host", fields.host if fields.network in {"ws", "h2"} else ""),
        ("path", fields.path if fields.network in {"ws", "h2"} else ""),
        ("serviceName", fields.grpc_service_name if fields.network == "grpc" else ""),
    ]
    if fields.skip_cert_verify:
        params.append(("allowInsecure", "1"))
    return f"vless://{quote(fields.uuid_value, safe='')}@{fields.server}:{fields.port}?{_build_query(params)}#{_fragment(fields)}"


def _build_vmess_uri(fields: StructuredNodeFields) -> str:
    network = fields.network or "ws"
    payload = {
        "v": "2",
        "ps": fields.name,
        "add": fields.server,
        "port": str(fields.port),
        "id": fields.uuid_value,
        "aid": str(fields.alter_id or "0"),
        "scy": fields.scy or "auto",
        "net": network,
        "type": "none",
        "tls": "tls" if (fields.security or "tls") == "tls" else "",
    }
    if network in {"ws", "h2"}:
        payload["host"] = fields.host
        payload["path"] = fields.path
    elif network == "grpc" and fields.grpc_service_name:
        payload["path"] = fields.grpc_service_name
        payload["serviceName"] = fields.grpc_service_name
    if (fields.security or "tls") == "tls" and fields.sni:
        payload["sni"] = fields.sni
    if fields.fingerprint:
        payload["fp"] = fields.fingerprint
    if fields.alpn:
        payload["alpn"] = fields.alpn
    encoded = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return f"vmess://{encoded}"


def _build_trojan_uri(fields: StructuredNodeFields) -> str:
    params = [
        ("security", fields.security or "tls"),
        ("sni", fields.sni),
        ("type", fields.network or "tcp"),
        ("host", fields.host if fields.network == "ws" else ""),
        ("path", fields.path if fields.network == "ws" else ""),
        ("fp", fields.fingerprint),
        ("alpn", fields.alpn),
    ]
    if fields.skip_cert_verify:
        params.append(("allowInsecure", "1"))
    return f"trojan://{quote(fields.password, safe='')}@{fields.server}:{fields.port}?{_build_query(params)}#{_fragment(fields)}"


def _build_anytls_uri(fields: StructuredNodeFields) -> str:
    params = [("sni", fields.sni), ("alpn", fields.alpn), ("fp", fields.fingerprint)]
    if fields.skip_cert_verify:
        params.append(("insecure", "1"))
    return f"anytls://{quote(fields.password, safe='')}@{fields.server}:{fields.port}?{_build_query(params)}#{_fragment(fields)}"


def _build_hysteria2_uri(fields: StructuredNodeFields) -> str:
    params = [("sni", fields.sni)]
    if fields.obfs:
        params.append(("obfs", fields.obfs))
    if fields.obfs_password:
        params.append(("obfs-password", fields.obfs_password))
    if fields.alpn:
        params.append(("alpn", fields.alpn))
    params.append(("insecure", "1" if fields.skip_cert_verify else "0"))
    return f"hysteria2://{quote(fields.password, safe='')}@{fields.server}:{fields.port}?{_build_query(params)}#{_fragment(fields)}"


def _build_tuic_uri(fields: StructuredNodeFields) -> str:
    params = [
        ("version", "5"),
        ("sni", fields.sni),
        ("alpn", fields.alpn),
        ("udp_relay_mode", fields.udp_relay_mode or "native"),
        ("congestion_control", fields.congestion_control or "bbr"),
    ]
    if fields.skip_cert_verify:
        params.append(("allow_insecure", "1"))
    return f"tuic://{quote(fields.uuid_value, safe='')}:{quote(fields.password, safe='')}@{fields.server}:{fields.port}?{_build_query(params)}#{_fragment(fields)}"


def _build_wireguard_uri(fields: StructuredNodeFields) -> str:
    params: list[tuple[str, str]] = [
        ("publickey", fields.public_key),
        ("address", fields.local_address),
        ("allowed_ips", fields.allowed_ips or "0.0.0.0/0,::/0"),
    ]
    if fields.reserved:
        params.append(("reserved", fields.reserved))
    if fields.mtu:
        params.append(("mtu", fields.mtu))
    return f"wireguard://{quote(fields.private_key, safe='')}@{fields.server}:{fields.port}?{_build_query(params)}#{_fragment(fields)}"
