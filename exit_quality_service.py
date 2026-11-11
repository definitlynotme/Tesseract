from __future__ import annotations

import copy
import hashlib
import html
import ipaddress
import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from exit_quality_endpoints import (
    AWS_PREFIX_URL,
    AZURE_DOWNLOAD_HOST,
    AZURE_SERVICE_TAGS_PAGE_URL,
    FEODO_ACTIVE_C2_URL,
    GCP_PREFIX_URL,
    OCI_PREFIX_URL,
    ONIONOO_DETAILS_URL,
    PROXYCHECK_URL_TEMPLATE,
    SPAMHAUS_ASNDROP_URL,
    SPAMHAUS_DROP_V4_URL,
    SPAMHAUS_DROP_V6_URL,
    TEAM_CYMRU_DOH_URL,
    SameHostHttpsRedirectHandler,
)
from log_event_reports import log_raw_diagnostic
from overview_ip_check import collect_exit_identity as _collect_exit_identity_v2


RESULT_SCHEMA_VERSION = 2
STORE_SCHEMA_VERSION = 1
MAX_RESULT_RECORDS = 8
DEFAULT_TIMEOUT_SECONDS = 8.0
COOPERATIVE_CANCEL_REQUEST_TIMEOUT_SECONDS = 0.5
MAX_JSON_RESPONSE_BYTES = 64 * 1024 * 1024

TEAM_CYMRU_TTL_SECONDS = 4 * 60 * 60
ONIONOO_TTL_SECONDS = 60 * 60
PROXYCHECK_TTL_SECONDS = 24 * 60 * 60
CLOUD_DATASET_TTL_SECONDS = 24 * 60 * 60
SPAMHAUS_TTL_SECONDS = 24 * 60 * 60
FEODO_TTL_SECONDS = 15 * 60
IDENTITY_TTL_SECONDS = 15 * 60

SPAMHAUS_TERMS_URL = "https://www.spamhaus.org/blocklists/drop-fair-use-policy/"
SPAMHAUS_ATTRIBUTION = "The Spamhaus Project SLU"

AZURE_DOWNLOAD_RE = re.compile(
    rf"https://{re.escape(AZURE_DOWNLOAD_HOST)}/[^\"'<> ]+/ServiceTags_Public_\d{{8}}\.json",
    re.IGNORECASE,
)


class DatasetRefreshCancelled(RuntimeError):
    """The runtime cancelled a static dataset refresh before it could commit."""


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise DatasetRefreshCancelled("exit-quality dataset refresh cancelled")


def _iso(timestamp: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat()


def _float_timestamp(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        from datetime import datetime

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _canonical_ip(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return ""


def _canonical_asn(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    match = re.match(r"^(?:AS\s*)?(\d+)\b", text)
    if not match:
        return ""
    return f"AS{int(match.group(1))}"


def _bounded_percent(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def _truth(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"yes", "true", "1", "on", "detected", "positive"}:
        return True
    if text in {"no", "false", "0", "off", "not detected", "negative"}:
        return False
    return None


def _safe_error_text(value: Any, *, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    body: bytes
    headers: dict[str, str]
    url: str


class HttpGetter(Protocol):
    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        proxy_url: str | None = None,
    ) -> HttpResponse: ...


InternalRequestObserver = Callable[[str, float], None]


def default_http_get(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    proxy_url: str | None = None,
) -> HttpResponse:
    request_headers = {
        "User-Agent": "Tesseract/1.0 ExitQuality",
        **{str(key): str(value) for key, value in dict(headers or {}).items()},
    }
    request = urllib.request.Request(str(url), headers=request_headers, method="GET")
    opener = (
        urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}),
            SameHostHttpsRedirectHandler(),
        )
        if proxy_url
        else urllib.request.build_opener(SameHostHttpsRedirectHandler())
    )
    try:
        with opener.open(request, timeout=max(0.1, float(timeout))) as response:
            body = response.read(MAX_JSON_RESPONSE_BYTES + 1)
            return HttpResponse(
                status_code=int(getattr(response, "status", 200) or 200),
                body=body,
                headers={str(key).lower(): str(value) for key, value in response.headers.items()},
                url=str(getattr(response, "url", url) or url),
            )
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(MAX_JSON_RESPONSE_BYTES + 1)
        except Exception:
            body = b""
        return HttpResponse(
            status_code=int(exc.code),
            body=body,
            headers={str(key).lower(): str(value) for key, value in (exc.headers or {}).items()},
            url=str(getattr(exc, "url", url) or url),
        )


def _require_https_response_host(response: HttpResponse, expected_host: str) -> None:
    parsed = urllib.parse.urlparse(response.url)
    if parsed.scheme.lower() != "https" or str(parsed.hostname or "").lower() != expected_host.lower():
        raise ValueError(f"provider redirected outside its HTTPS host: {expected_host}")


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    source: str
    status: str
    observed_at: str
    expires_at: str
    positive: bool | None = None
    data: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    cache_hit: bool = False
    stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.data is None:
            payload.pop("data")
        if self.error is None:
            payload.pop("error")
        return payload


@dataclass(frozen=True, slots=True)
class ExitQualityResultV2:
    schema_version: int
    request_id: str
    state: str
    freshness: dict[str, Any]
    checked_at: str
    expires_at: str
    route: dict[str, Any]
    identity: dict[str, Any]
    network: dict[str, Any]
    reputation: dict[str, Any]
    coverage: dict[str, Any]
    evidence: tuple[dict[str, Any], ...]
    diagnostics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence"] = [copy.deepcopy(item) for item in self.evidence]
        return payload

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


RouteSnapshot = Callable[[], Mapping[str, Any] | None]
IdentityCollector = Callable[..., Mapping[str, Any] | None]


def _normalize_route(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(payload or {})
    selected = raw.get("selected_chain")
    selected_chain = [str(item) for item in selected] if isinstance(selected, (list, tuple)) else []
    route = {
        "route_marker": str(raw.get("route_marker") or ""),
        "selected_chain": selected_chain,
        "leaf_proxy": str(raw.get("leaf_proxy") or ""),
        "path_kind": str(raw.get("path_kind") or raw.get("kind") or "unknown"),
        "core_instance": str(raw.get("core_instance") or raw.get("core_generation") or ""),
    }
    route["confirmed"] = bool(
        raw.get("confirmed", bool(route["route_marker"] or route["selected_chain"]))
    )
    route["facility_failure"] = _safe_error_text(raw.get("facility_failure") or "")
    route["checked_at"] = str(raw.get("checked_at") or "")
    # ``core_instance`` is execution/CAS context, not part of the logical
    # FINAL-EXIT route.  A Core restart that reloads the same route must not
    # turn an otherwise fresh identity result into ``route_changed``.
    marker_payload = json.dumps(
        {
            key: route[key]
            for key in ("route_marker", "selected_chain", "leaf_proxy", "path_kind")
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    route["binding_marker"] = hashlib.sha256(marker_payload.encode("utf-8")).hexdigest()[:24]
    execution_payload = json.dumps(
        {
            "binding_marker": route["binding_marker"],
            "core_instance": route["core_instance"],
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    route["execution_binding_marker"] = hashlib.sha256(
        execution_payload.encode("utf-8")
    ).hexdigest()[:24]
    return route


def _route_changed(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    return any(
        before.get(key) != after.get(key)
        for key in ("route_marker", "selected_chain", "leaf_proxy", "path_kind")
    )


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass
        raise


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass
        raise


def _load_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


class _SourceCache:
    def __init__(self, path: Path, *, clock: Callable[[], float]) -> None:
        self.path = Path(path)
        self.clock = clock
        payload = _load_json_dict(self.path)
        self.entries = dict(payload.get("entries") or {}) if payload.get("schema_version") == STORE_SCHEMA_VERSION else {}

    @staticmethod
    def _key(source: str, ip: str) -> str:
        return f"{str(source)}|{_canonical_ip(ip) or str(ip)}"

    def get(self, source: str, ip: str) -> dict[str, Any] | None:
        entry = self.entries.get(self._key(source, ip))
        if not isinstance(entry, dict):
            return None
        if _float_timestamp(entry.get("expires_at")) <= self.clock():
            return None
        value = entry.get("value")
        return copy.deepcopy(value) if isinstance(value, dict) else None

    def peek(self, source: str, ip: str) -> dict[str, Any] | None:
        """Return a cached value even after expiry, for conditional revalidation."""
        entry = self.entries.get(self._key(source, ip))
        if not isinstance(entry, dict):
            return None
        value = entry.get("value")
        return copy.deepcopy(value) if isinstance(value, dict) else None

    def put(self, source: str, ip: str, value: dict[str, Any], *, expires_at: float) -> None:
        self.entries[self._key(source, ip)] = {
            "expires_at": _iso(expires_at),
            "value": copy.deepcopy(value),
        }
        _atomic_write_json(
            self.path,
            {
                "schema_version": STORE_SCHEMA_VERSION,
                "updated_at": _iso(self.clock()),
                "entries": self.entries,
            },
        )


class _ResultStore:
    def __init__(self, path: Path, *, clock: Callable[[], float], limit: int = MAX_RESULT_RECORDS) -> None:
        self.path = Path(path)
        self.clock = clock
        self.limit = max(1, int(limit))

    def records(self) -> list[dict[str, Any]]:
        payload = _load_json_dict(self.path)
        if payload.get("schema_version") != RESULT_SCHEMA_VERSION:
            return []
        records = payload.get("records")
        return [copy.deepcopy(item) for item in records if isinstance(item, dict)][-self.limit :] if isinstance(records, list) else []

    def append(self, result: ExitQualityResultV2) -> None:
        records = self.records()
        records.append(result.to_dict())
        _atomic_write_json(
            self.path,
            {
                "schema_version": RESULT_SCHEMA_VERSION,
                "updated_at": _iso(self.clock()),
                "records": records[-self.limit :],
            },
        )


def team_cymru_query_name(ip: str) -> str:
    address = ipaddress.ip_address(ip)
    if address.version == 4:
        return ".".join(reversed(str(address).split("."))) + ".origin.asn.cymru.com"
    return ".".join(reversed(address.exploded.replace(":", ""))) + ".origin6.asn.cymru.com"


def parse_team_cymru_doh(payload: bytes | str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        document = dict(payload)
    else:
        text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
        document = json.loads(text)
    if not isinstance(document, dict) or int(document.get("Status", -1)) != 0:
        raise ValueError("Team Cymru DoH response was not successful")
    answers = document.get("Answer")
    if not isinstance(answers, list):
        raise ValueError("Team Cymru DoH response has no TXT answer")
    for answer in answers:
        if not isinstance(answer, dict):
            continue
        raw = str(answer.get("data") or "").strip()
        if not raw:
            continue
        raw = raw.replace('" "', "").strip('"')
        fields = [field.strip() for field in raw.split("|")]
        if len(fields) < 5:
            continue
        asns = list(
            dict.fromkeys(
                asn
                for token in re.split(r"[\s,]+", fields[0])
                if (asn := _canonical_asn(token))
            )
        )
        if not asns:
            continue
        try:
            prefix = str(ipaddress.ip_network(fields[1], strict=False))
        except ValueError:
            prefix = ""
        return {
            "asn": asns[0] if len(asns) == 1 else "",
            "asns": asns,
            "asn_conflict": len(asns) > 1,
            "prefix": prefix,
            "country": fields[2].upper(),
            "registry": fields[3].lower(),
            "allocated": fields[4],
        }
    raise ValueError("Team Cymru TXT answer did not contain an origin ASN")


def _onionoo_address(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("[") and "]" in text:
        text = text[1 : text.index("]")]
    direct = _canonical_ip(text)
    if direct:
        return direct
    if text.count(":") == 1:
        return _canonical_ip(text.rsplit(":", 1)[0])
    return ""


def parse_onionoo_exact_ip(payload: bytes | str | Mapping[str, Any], ip: str) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        document = dict(payload)
    else:
        text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
        document = json.loads(text)
    target = _canonical_ip(ip)
    if not target or not isinstance(document, dict):
        raise ValueError("invalid Onionoo response or target IP")
    # Onionoo's ``exit_addresses`` field is the exact IPv4 address observed
    # exiting during the previous 24 hours.  ``or_addresses`` and
    # ``dir_address`` are relay listeners, not evidence that the address was
    # used for exit traffic.  Onionoo does not provide an equivalent exact
    # IPv6 exit-address field, so IPv6 must remain unknown rather than becoming
    # a false negative.
    if ipaddress.ip_address(target).version != 4:
        return {
            "tor": None,
            "coverage": "ipv4_exit_addresses_only",
        }
    relays = document.get("relays", [])
    bridges = document.get("bridges", [])
    if not isinstance(relays, list) or not isinstance(bridges, list):
        raise ValueError("Onionoo response has an invalid relay schema")
    for relay in relays:
        if not isinstance(relay, dict):
            continue
        values = relay.get("exit_addresses")
        addresses = values if isinstance(values, list) else []
        if target in {_onionoo_address(value) for value in addresses}:
            return {
                "tor": True,
                "fingerprint": str(relay.get("fingerprint") or ""),
                "nickname": str(relay.get("nickname") or ""),
                "running": bool(relay.get("running")),
            }
    return {"tor": False}


def _retry_after_seconds(headers: Mapping[str, str], default: int = 300) -> int:
    value = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return max(1, min(24 * 60 * 60, int(str(value).strip())))
    except (TypeError, ValueError):
        return int(default)


def parse_proxycheck_response(response: HttpResponse, ip: str) -> dict[str, Any]:
    document: dict[str, Any] = {}
    try:
        parsed = json.loads(response.body.decode("utf-8", errors="replace"))
        document = parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        document = {}
    message = _safe_error_text(document.get("message") or document.get("error") or "")
    provider_status = str(document.get("status") or "").strip().lower()
    message_lower = message.lower()
    proxy_denied = any(
        marker in message_lower
        for marker in (
            "using a proxy",
            "proxy server",
            "proxy queries",
            "queries from proxies",
        )
    )
    if proxy_denied and (response.status_code in {401, 403, 429} or provider_status == "denied"):
        return {
            "status": "denied",
            "error": {
                "code": "proxy_denied",
                "http_status": response.status_code,
                "retry_after_seconds": _retry_after_seconds(response.headers, 3600),
                "message": message,
            },
        }
    if response.status_code == 429:
        return {
            "status": "rate_limited",
            "error": {
                "code": "rate_limited",
                "http_status": 429,
                "retry_after_seconds": _retry_after_seconds(response.headers),
                "message": message,
            },
        }
    if response.status_code in {401, 403} or provider_status == "denied":
        return {
            "status": "denied",
            "error": {
                "code": "denied",
                "http_status": response.status_code,
                "retry_after_seconds": _retry_after_seconds(response.headers, 3600),
                "message": message,
            },
        }
    if response.status_code != 200:
        return {
            "status": "error",
            "error": {
                "code": "http_error",
                "http_status": response.status_code,
                "message": message,
            },
        }
    status = provider_status
    if status not in {"ok", "warning"}:
        return {
            "status": "error",
            "error": {"code": "provider_error", "http_status": response.status_code, "message": message},
        }
    target = _canonical_ip(ip)
    record = document.get(target)
    if not isinstance(record, dict):
        record = next(
            (
                value
                for key, value in document.items()
                if _canonical_ip(key) == target and isinstance(value, dict)
            ),
            None,
        )
    if not isinstance(record, dict):
        return {"status": "error", "error": {"code": "missing_ip_record", "message": ""}}
    network = record.get("network") if isinstance(record.get("network"), dict) else {}
    detections = record.get("detections") if isinstance(record.get("detections"), dict) else {}
    risk = _bounded_percent(detections.get("risk"))
    confidence = _bounded_percent(detections.get("confidence"))
    network_type = str(network.get("type") or "").strip().lower()
    signals = {
        name: _truth(detections.get(name))
        for name in ("proxy", "vpn", "compromised", "scraper", "tor", "hosting", "anonymous")
    }
    if risk is None and not any(isinstance(value, bool) for value in signals.values()):
        return {
            "status": "unknown",
            "data": {
                "risk": None,
                "confidence": confidence,
                "type": network_type,
                "asn": _canonical_asn(network.get("asn")),
                "range": str(network.get("range") or ""),
                "provider": str(network.get("provider") or ""),
                "organisation": str(network.get("organisation") or ""),
                "signals": signals,
            },
            "error": {"code": "incomplete_detection_data", "message": ""},
        }
    positive = any(value is True for value in signals.values()) or (risk is not None and risk >= 26)
    return {
        "status": "positive" if positive else "negative",
        "data": {
            "risk": risk,
            "confidence": confidence,
            "type": network_type,
            "asn": _canonical_asn(network.get("asn")),
            "range": str(network.get("range") or ""),
            "provider": str(network.get("provider") or ""),
            "organisation": str(network.get("organisation") or ""),
            "signals": signals,
        },
    }


DatasetParser = Callable[[bytes], list[dict[str, Any]]]
DatasetUrlResolver = Callable[["DatasetSpec", HttpGetter, float, str | None], str]


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    name: str
    url: str
    ttl_seconds: int
    parser: DatasetParser
    max_bytes: int = 64 * 1024 * 1024
    url_resolver: DatasetUrlResolver | None = None
    allow_empty: bool = False
    attribution: str = ""
    terms_url: str = ""


@dataclass(frozen=True, slots=True)
class DatasetSnapshot:
    name: str
    entries: tuple[dict[str, Any], ...]
    status: str
    observed_at: str
    expires_at: str
    cache_hit: bool
    stale: bool
    error: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


def _json_document(payload: bytes) -> Any:
    return json.loads(payload.decode("utf-8", errors="strict"))


def _network_entry(value: Any, *, source: str, **metadata: Any) -> dict[str, Any] | None:
    try:
        network = ipaddress.ip_network(str(value or "").strip(), strict=False)
    except ValueError:
        return None
    return {
        "kind": "network",
        "value": str(network),
        "source": source,
        **{str(key): value for key, value in metadata.items() if value not in (None, "", [], {})},
    }


def parse_aws_prefixes(payload: bytes) -> list[dict[str, Any]]:
    document = _json_document(payload)
    if not isinstance(document, dict) or not isinstance(document.get("prefixes"), list):
        raise ValueError("AWS prefix schema is invalid")
    entries: list[dict[str, Any]] = []
    for family_key, prefix_key in (("prefixes", "ip_prefix"), ("ipv6_prefixes", "ipv6_prefix")):
        values = document.get(family_key, [])
        if not isinstance(values, list):
            raise ValueError(f"AWS {family_key} schema is invalid")
        for item in values:
            if not isinstance(item, dict):
                continue
            entry = _network_entry(
                item.get(prefix_key),
                source="cloud_aws",
                provider="AWS",
                service=item.get("service"),
                region=item.get("region"),
                network_border_group=item.get("network_border_group"),
            )
            if entry:
                entries.append(entry)
    if not entries:
        raise ValueError("AWS prefix dataset is empty")
    return entries


def parse_gcp_prefixes(payload: bytes) -> list[dict[str, Any]]:
    document = _json_document(payload)
    prefixes = document.get("prefixes") if isinstance(document, dict) else None
    if not isinstance(prefixes, list):
        raise ValueError("GCP prefix schema is invalid")
    entries: list[dict[str, Any]] = []
    for item in prefixes:
        if not isinstance(item, dict):
            continue
        value = item.get("ipv4Prefix") or item.get("ipv6Prefix")
        entry = _network_entry(
            value,
            source="cloud_gcp",
            provider="GCP",
            service=item.get("service"),
            region=item.get("scope"),
        )
        if entry:
            entries.append(entry)
    if not entries:
        raise ValueError("GCP prefix dataset is empty")
    return entries


def parse_azure_service_tags(payload: bytes) -> list[dict[str, Any]]:
    document = _json_document(payload)
    values = document.get("values") if isinstance(document, dict) else None
    if not isinstance(values, list):
        raise ValueError("Azure service-tag schema is invalid")
    entries: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        properties = item.get("properties") if isinstance(item.get("properties"), dict) else {}
        prefixes = properties.get("addressPrefixes")
        if not isinstance(prefixes, list):
            continue
        for prefix in prefixes:
            entry = _network_entry(
                prefix,
                source="cloud_azure",
                provider="Azure",
                service=properties.get("systemService") or item.get("name"),
                region=properties.get("region"),
                tag=item.get("name"),
            )
            if entry:
                entries.append(entry)
    if not entries:
        raise ValueError("Azure service-tag dataset is empty")
    return entries


def parse_oci_prefixes(payload: bytes) -> list[dict[str, Any]]:
    document = _json_document(payload)
    regions = document.get("regions") if isinstance(document, dict) else None
    if not isinstance(regions, list):
        raise ValueError("OCI prefix schema is invalid")
    entries: list[dict[str, Any]] = []
    for region in regions:
        if not isinstance(region, dict):
            continue
        cidrs = region.get("cidrs")
        if not isinstance(cidrs, list):
            continue
        for item in cidrs:
            if not isinstance(item, dict):
                continue
            tags = item.get("tags") if isinstance(item.get("tags"), list) else []
            entry = _network_entry(
                item.get("cidr"),
                source="cloud_oci",
                provider="OCI",
                service=",".join(str(tag) for tag in tags),
                region=region.get("region"),
            )
            if entry:
                entries.append(entry)
    if not entries:
        raise ValueError("OCI prefix dataset is empty")
    return entries


def _json_lines(payload: bytes) -> list[dict[str, Any]]:
    text = payload.decode("utf-8", errors="strict").strip()
    if not text:
        raise ValueError("dataset is empty")
    if text.startswith("[") or text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # Spamhaus publishes JSON Lines, whose first line also starts with
            # ``{``.  A whole-document parse therefore legitimately fails.
            parsed = None
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        if isinstance(parsed, dict):
            for key in ("data", "entries", "results"):
                value = parsed.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            return [parsed]
    items: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            item = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"dataset contains invalid JSON on line {line_number}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"dataset contains a non-object JSON value on line {line_number}")
        items.append(item)
    return items


def _spamhaus_data_rows(payload: bytes) -> list[dict[str, Any]]:
    rows = _json_lines(payload)
    if not rows:
        raise ValueError("Spamhaus dataset is empty")
    metadata = rows[-1]
    if str(metadata.get("type") or "").strip().lower() != "metadata":
        raise ValueError("Spamhaus dataset is missing its terminal metadata record")
    try:
        declared_records = int(metadata.get("records"))
        timestamp = int(metadata.get("timestamp"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Spamhaus terminal metadata is invalid") from exc
    data_rows = rows[:-1]
    if declared_records != len(data_rows) or timestamp <= 0:
        raise ValueError("Spamhaus terminal metadata does not match the downloaded records")
    if not str(metadata.get("copyright") or "").strip():
        raise ValueError("Spamhaus terminal metadata has no copyright statement")
    return data_rows


def parse_spamhaus_networks(payload: bytes) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for item in _spamhaus_data_rows(payload):
        value = item.get("cidr") or item.get("prefix") or item.get("network")
        entry = _network_entry(
            value,
            source="spamhaus",
            sblid=item.get("sblid"),
            rir=item.get("rir"),
            country=item.get("cc"),
            listed_at=item.get("date"),
            provider_expires_at=item.get("expires"),
        )
        if entry:
            entries.append(entry)
    if not entries:
        raise ValueError("Spamhaus network dataset is empty or invalid")
    return entries


def parse_spamhaus_asns(payload: bytes) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for item in _spamhaus_data_rows(payload):
        asn = _canonical_asn(item.get("asn") or item.get("as"))
        if not asn:
            continue
        entries.append(
            {
                "kind": "asn",
                "value": asn,
                "source": "spamhaus_asndrop",
                **{
                    key: item[key]
                    for key in ("sblid", "rir", "cc", "date", "expires")
                    if item.get(key) not in (None, "")
                },
            }
        )
    if not entries:
        raise ValueError("Spamhaus ASN-DROP dataset is empty or invalid")
    return entries


def parse_feodo_active_c2(payload: bytes) -> list[dict[str, Any]]:
    document = _json_document(payload)
    if isinstance(document, dict):
        values = next(
            (document[key] for key in ("data", "entries", "results") if key in document),
            None,
        )
    else:
        values = document
    if not isinstance(values, list):
        raise ValueError("Feodo dataset schema is invalid")
    entries: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict) or not str(item.get("status") or "").strip():
            raise ValueError("Feodo dataset entry schema is invalid")
        if str(item.get("status") or "").strip().lower() != "online":
            continue
        address = _canonical_ip(item.get("ip_address") or item.get("ip"))
        if not address:
            raise ValueError("Feodo online entry has no valid IP address")
        entries.append(
            {
                "kind": "ip",
                "value": address,
                "source": "feodo",
                **{
                    key: item[key]
                    for key in (
                        "port",
                        "hostname",
                        "as_number",
                        "as_name",
                        "country",
                        "first_seen",
                        "last_online",
                        "malware",
                    )
                    if item.get(key) not in (None, "")
                },
            }
        )
    # A valid empty list is meaningful: Feodo explicitly publishes no active
    # entries after a successful takedown.  Only malformed schema is unknown.
    return entries


def resolve_azure_service_tags_url(
    spec: DatasetSpec,
    http_get: HttpGetter,
    timeout: float,
    proxy_url: str | None,
) -> str:
    response = http_get(
        spec.url,
        headers={"Accept": "text/html,application/xhtml+xml"},
        timeout=timeout,
        proxy_url=proxy_url,
    )
    response_url = urllib.parse.urlparse(response.url)
    requested_url = urllib.parse.urlparse(spec.url)
    if response_url.scheme.lower() != "https":
        raise ValueError(f"refusing Azure directory redirected to non-HTTPS URL: {response.url}")
    if response_url.hostname != requested_url.hostname:
        raise ValueError(f"refusing Azure directory redirected to another host: {response.url}")
    if response.status_code != 200:
        raise RuntimeError(f"Azure service-tag directory returned HTTP {response.status_code}")
    text = html.unescape(response.body.decode("utf-8", errors="replace"))
    matches = AZURE_DOWNLOAD_RE.findall(text)
    if not matches:
        raise ValueError("Azure service-tag download link was not found")
    resolved = sorted(set(matches))[-1]
    parsed = urllib.parse.urlparse(resolved)
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname != AZURE_DOWNLOAD_HOST
        or not re.fullmatch(r"ServiceTags_Public_\d{8}\.json", Path(parsed.path).name, re.IGNORECASE)
    ):
        raise ValueError("Azure service-tag download link failed allowlist validation")
    return resolved


DEFAULT_DATASET_SPECS: tuple[DatasetSpec, ...] = (
    DatasetSpec("cloud_aws", AWS_PREFIX_URL, CLOUD_DATASET_TTL_SECONDS, parse_aws_prefixes),
    DatasetSpec("cloud_gcp", GCP_PREFIX_URL, CLOUD_DATASET_TTL_SECONDS, parse_gcp_prefixes),
    DatasetSpec(
        "cloud_azure",
        AZURE_SERVICE_TAGS_PAGE_URL,
        CLOUD_DATASET_TTL_SECONDS,
        parse_azure_service_tags,
        url_resolver=resolve_azure_service_tags_url,
    ),
    DatasetSpec("cloud_oci", OCI_PREFIX_URL, CLOUD_DATASET_TTL_SECONDS, parse_oci_prefixes),
    DatasetSpec(
        "spamhaus_drop",
        SPAMHAUS_DROP_V4_URL,
        SPAMHAUS_TTL_SECONDS,
        parse_spamhaus_networks,
        attribution=SPAMHAUS_ATTRIBUTION,
        terms_url=SPAMHAUS_TERMS_URL,
    ),
    DatasetSpec(
        "spamhaus_dropv6",
        SPAMHAUS_DROP_V6_URL,
        SPAMHAUS_TTL_SECONDS,
        parse_spamhaus_networks,
        attribution=SPAMHAUS_ATTRIBUTION,
        terms_url=SPAMHAUS_TERMS_URL,
    ),
    DatasetSpec(
        "spamhaus_asndrop",
        SPAMHAUS_ASNDROP_URL,
        SPAMHAUS_TTL_SECONDS,
        parse_spamhaus_asns,
        attribution=SPAMHAUS_ATTRIBUTION,
        terms_url=SPAMHAUS_TERMS_URL,
    ),
    DatasetSpec(
        "feodo",
        FEODO_ACTIVE_C2_URL,
        FEODO_TTL_SECONDS,
        parse_feodo_active_c2,
        allow_empty=True,
    ),
)


class DatasetUpdater:
    def __init__(
        self,
        root: str | Path,
        *,
        http_get: HttpGetter = default_http_get,
        clock: Callable[[], float] = time.time,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        self.root = Path(root)
        self.metadata_path = self.root / "metadata.json"
        self.http_get = http_get
        self.clock = clock
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.logger = logger or logging.getLogger("tesseract.exit_quality.datasets")
        payload = _load_json_dict(self.metadata_path)
        self.metadata = dict(payload.get("datasets") or {}) if payload.get("schema_version") == STORE_SCHEMA_VERSION else {}

    def _dataset_path(self, spec: DatasetSpec) -> Path:
        return self.root / f"{spec.name}.json"

    def _parse_existing(self, spec: DatasetSpec) -> tuple[bool, tuple[dict[str, Any], ...]]:
        path = self._dataset_path(spec)
        if not path.exists():
            return False, ()
        try:
            entries = tuple(spec.parser(path.read_bytes()))
            if not entries and not spec.allow_empty:
                return False, ()
            return True, entries
        except Exception:
            return False, ()

    def _save_metadata(self) -> None:
        _atomic_write_json(
            self.metadata_path,
            {
                "schema_version": STORE_SCHEMA_VERSION,
                "updated_at": _iso(self.clock()),
                "datasets": self.metadata,
            },
        )

    def _snapshot(
        self,
        spec: DatasetSpec,
        entries: tuple[dict[str, Any], ...],
        *,
        status: str,
        observed_at: float,
        expires_at: float,
        cache_hit: bool,
        stale: bool,
        error: dict[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> DatasetSnapshot:
        return DatasetSnapshot(
            name=spec.name,
            entries=entries,
            status=status,
            observed_at=_iso(observed_at),
            expires_at=_iso(expires_at),
            cache_hit=cache_hit,
            stale=stale,
            error=copy.deepcopy(error),
            metadata=copy.deepcopy(dict(metadata or {})),
        )

    @staticmethod
    def _public_metadata(spec: DatasetSpec, metadata: Mapping[str, Any], observed_at: float) -> dict[str, Any]:
        public = {
            key: copy.deepcopy(metadata[key])
            for key in ("url", "etag", "last_modified", "bytes")
            if metadata.get(key) not in (None, "")
        }
        if spec.attribution:
            year_match = re.search(r"\b(?:19|20)\d{2}\b", str(metadata.get("last_modified") or ""))
            copyright_date = year_match.group(0) if year_match else _iso(observed_at)[:4]
            public.update(
                {
                    "attribution": f"© {copyright_date} {spec.attribution}",
                    "copyright_date": copyright_date,
                    "terms_url": spec.terms_url,
                }
            )
        return public

    def load_cached(self, spec: DatasetSpec) -> DatasetSnapshot:
        """Load and validate a local snapshot without making a network request."""
        now = self.clock()
        metadata = self.metadata.get(spec.name)
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        observed_at = _float_timestamp(metadata.get("observed_at"))
        expires_at = _float_timestamp(metadata.get("expires_at"))
        valid_existing, existing = self._parse_existing(spec)
        public_metadata = self._public_metadata(spec, metadata, observed_at or now)
        if not valid_existing:
            return self._snapshot(
                spec,
                (),
                status="error",
                observed_at=observed_at or now,
                expires_at=expires_at or now,
                cache_hit=False,
                stale=False,
                error={"code": "dataset_cache_missing", "message": "No valid local snapshot"},
                metadata=public_metadata,
            )
        fresh = expires_at > now
        return self._snapshot(
            spec,
            existing,
            status="fresh" if fresh else "stale",
            observed_at=observed_at or now,
            expires_at=expires_at or now,
            cache_hit=True,
            stale=not fresh,
            error=None
            if fresh
            else {"code": "dataset_cache_expired", "message": "Local snapshot needs background refresh"},
            metadata=public_metadata,
        )

    def refresh(
        self,
        spec: DatasetSpec,
        *,
        proxy_url: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> DatasetSnapshot:
        _raise_if_cancelled(cancel_event)
        now = self.clock()
        metadata = self.metadata.get(spec.name)
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        observed_at = _float_timestamp(metadata.get("observed_at"))
        expires_at = _float_timestamp(metadata.get("expires_at"))
        valid_existing, existing = self._parse_existing(spec)
        _raise_if_cancelled(cancel_event)
        public_metadata = self._public_metadata(spec, metadata, observed_at or now)
        if valid_existing and expires_at > now:
            _raise_if_cancelled(cancel_event)
            return self._snapshot(
                spec,
                existing,
                status="fresh",
                observed_at=observed_at or now,
                expires_at=expires_at,
                cache_hit=True,
                stale=False,
                metadata=public_metadata,
            )

        target = self._dataset_path(spec)
        previous_payload = target.read_bytes() if valid_existing else None
        had_metadata = spec.name in self.metadata
        previous_metadata = copy.deepcopy(self.metadata.get(spec.name))
        payload_replaced = False
        metadata_saved = False
        request_timeout = (
            min(
                self.timeout_seconds,
                COOPERATIVE_CANCEL_REQUEST_TIMEOUT_SECONDS,
            )
            if cancel_event is not None
            else self.timeout_seconds
        )
        try:
            _raise_if_cancelled(cancel_event)
            if urllib.parse.urlparse(spec.url).scheme.lower() != "https":
                raise ValueError(f"refusing non-HTTPS dataset URL: {spec.url}")
            resolved_url = (
                spec.url_resolver(spec, self.http_get, request_timeout, proxy_url)
                if spec.url_resolver
                else spec.url
            )
            _raise_if_cancelled(cancel_event)
            parsed_url = urllib.parse.urlparse(resolved_url)
            if parsed_url.scheme.lower() != "https":
                raise ValueError(f"refusing non-HTTPS dataset URL: {resolved_url}")
            headers: dict[str, str] = {"Accept": "application/json,text/plain;q=0.8,*/*;q=0.1"}
            if metadata.get("etag"):
                headers["If-None-Match"] = str(metadata["etag"])
            if metadata.get("last_modified"):
                headers["If-Modified-Since"] = str(metadata["last_modified"])
            response = self.http_get(
                resolved_url,
                headers=headers,
                timeout=request_timeout,
                proxy_url=proxy_url,
            )
            _raise_if_cancelled(cancel_event)
            response_url = urllib.parse.urlparse(response.url)
            if response_url.scheme.lower() != "https":
                raise ValueError(f"refusing dataset redirected to non-HTTPS URL: {response.url}")
            if response_url.hostname != parsed_url.hostname:
                raise ValueError(f"refusing dataset redirected to another host: {response.url}")
            if response.status_code == 304 and valid_existing:
                observed = observed_at or now
                expiry = now + spec.ttl_seconds
                metadata["expires_at"] = _iso(expiry)
                metadata["last_checked_at"] = _iso(now)
                metadata.update(self._public_metadata(spec, metadata, observed))
                self.metadata[spec.name] = metadata
                _raise_if_cancelled(cancel_event)
                self._save_metadata()
                metadata_saved = True
                _raise_if_cancelled(cancel_event)
                return self._snapshot(
                    spec,
                    existing,
                    status="fresh",
                    observed_at=observed,
                    expires_at=expiry,
                    cache_hit=True,
                    stale=False,
                    metadata=self._public_metadata(spec, metadata, observed),
                )
            if response.status_code != 200:
                raise RuntimeError(f"dataset returned HTTP {response.status_code}")
            if not response.body or len(response.body) > max(1, int(spec.max_bytes)):
                raise ValueError("dataset response failed size validation")
            _raise_if_cancelled(cancel_event)
            parsed_entries = tuple(spec.parser(response.body))
            _raise_if_cancelled(cancel_event)
            if not parsed_entries and not spec.allow_empty:
                raise ValueError("dataset response failed schema validation")
            target.parent.mkdir(parents=True, exist_ok=True)
            _raise_if_cancelled(cancel_event)
            fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(response.body)
                    handle.flush()
                    os.fsync(handle.fileno())
                _raise_if_cancelled(cancel_event)
                os.replace(temporary, target)
                payload_replaced = True
            except Exception:
                try:
                    Path(temporary).unlink()
                except FileNotFoundError:
                    pass
                raise
            expiry = now + spec.ttl_seconds
            new_metadata = {
                "url": resolved_url,
                "observed_at": _iso(now),
                "expires_at": _iso(expiry),
                "last_checked_at": _iso(now),
                "etag": response.headers.get("etag", ""),
                "last_modified": response.headers.get("last-modified", ""),
                "bytes": len(response.body),
            }
            new_metadata.update(self._public_metadata(spec, new_metadata, now))
            self.metadata[spec.name] = new_metadata
            _raise_if_cancelled(cancel_event)
            self._save_metadata()
            metadata_saved = True
            _raise_if_cancelled(cancel_event)
            return self._snapshot(
                spec,
                parsed_entries,
                status="fresh",
                observed_at=now,
                expires_at=expiry,
                cache_hit=False,
                stale=False,
                metadata=self._public_metadata(spec, new_metadata, now),
            )
        except Exception as exc:
            if payload_replaced:
                try:
                    if previous_payload is None:
                        target.unlink(missing_ok=True)
                    else:
                        _atomic_write_bytes(target, previous_payload)
                except Exception as rollback_exc:
                    log_raw_diagnostic(
                        self.logger,
                        "error",
                        "Failed to restore last-good exit-quality dataset %s: %s",
                        spec.name,
                        rollback_exc,
                    )
            if had_metadata:
                self.metadata[spec.name] = previous_metadata
            else:
                self.metadata.pop(spec.name, None)
            if metadata_saved:
                try:
                    self._save_metadata()
                except Exception as rollback_exc:
                    log_raw_diagnostic(
                        self.logger,
                        "error",
                        "Failed to restore last-good exit-quality metadata %s: %s",
                        spec.name,
                        rollback_exc,
                    )
            if isinstance(exc, DatasetRefreshCancelled):
                raise
            error = {"code": "dataset_update_failed", "message": _safe_error_text(exc)}
            log_raw_diagnostic(
                self.logger,
                "warning",
                "Exit-quality dataset update failed for %s: %s",
                spec.name,
                exc,
            )
            if valid_existing:
                return self._snapshot(
                    spec,
                    existing,
                    status="stale",
                    observed_at=observed_at or now,
                    expires_at=expires_at or now,
                    cache_hit=True,
                    stale=True,
                    error=error,
                    metadata=public_metadata,
                )
            return self._snapshot(
                spec,
                (),
                status="error",
                observed_at=now,
                expires_at=now,
                cache_hit=False,
                stale=False,
                error=error,
                metadata=public_metadata,
            )

    def load(
        self,
        spec: DatasetSpec,
        *,
        proxy_url: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> DatasetSnapshot:
        """Backward-compatible explicit refresh entry point."""
        return self.refresh(
            spec,
            proxy_url=proxy_url,
            cancel_event=cancel_event,
        )


def _match_dataset(
    snapshot: DatasetSnapshot,
    ips: Iterable[str],
    asns: Iterable[str],
) -> list[dict[str, Any]]:
    addresses = [ipaddress.ip_address(ip) for ip in ips if _canonical_ip(ip)]
    normalized_asns = {_canonical_asn(asn) for asn in asns if _canonical_asn(asn)}
    matches: list[dict[str, Any]] = []
    for entry in snapshot.entries:
        kind = str(entry.get("kind") or "")
        value = str(entry.get("value") or "")
        try:
            if kind == "network":
                network = ipaddress.ip_network(value, strict=False)
                for address in addresses:
                    if address in network:
                        match = copy.deepcopy(entry)
                        match["target_ip"] = str(address)
                        matches.append(match)
            elif kind == "ip" and _canonical_ip(value) in {_canonical_ip(ip) for ip in ips}:
                match = copy.deepcopy(entry)
                match["target_ip"] = _canonical_ip(value)
                matches.append(match)
            elif kind == "asn" and _canonical_asn(value) in normalized_asns:
                matches.append(copy.deepcopy(entry))
        except ValueError:
            continue
    return matches


def collect_exit_identity(
    *,
    proxy_url: str | None = None,
    request_id: str = "",
) -> dict[str, Any]:
    """Call the multi-source identity collector while accepting service kwargs."""

    del request_id
    return _collect_exit_identity_v2(proxy_url=proxy_url)


def _call_identity_collector(
    collector: IdentityCollector,
    *,
    proxy_url: str | None,
    request_id: str,
) -> Mapping[str, Any] | None:
    import inspect

    kwargs = {"proxy_url": proxy_url, "request_id": request_id}
    try:
        signature = inspect.signature(collector)
    except (TypeError, ValueError):
        return collector(**kwargs)
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    selected = kwargs if accepts_kwargs else {key: value for key, value in kwargs.items() if key in signature.parameters}
    return collector(**selected)


_LOCATION_TEXT_FIELDS: tuple[tuple[str, str], ...] = (
    ("cc", "country_code"),
    ("country", "country"),
    ("city", "city"),
    ("continent", "continent"),
    ("tz", "timezone"),
)
_LOCATION_VALUE_FIELDS: tuple[str, ...] = (
    "country_code",
    "country",
    "city",
    "continent",
    "timezone",
    "latitude",
    "longitude",
)


def _reported_text(value: Any, *, uppercase: bool = False) -> str:
    text = " ".join(str(value or "").split())
    if not text or text.lower() in {"none", "null", "unknown", "unavailable"}:
        return ""
    return text.upper() if uppercase else text


def _reported_coordinate(value: Any, *, latitude: bool) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    limit = 90.0 if latitude else 180.0
    if not -limit <= number <= limit:
        return None
    return number


def _location_observation(profile: Mapping[str, Any]) -> dict[str, Any]:
    observation: dict[str, Any] = {}
    for source_key, target_key in _LOCATION_TEXT_FIELDS:
        value = _reported_text(
            profile.get(source_key),
            uppercase=target_key == "country_code",
        )
        if value:
            observation[target_key] = value
    latitude = _reported_coordinate(profile.get("latitude"), latitude=True)
    longitude = _reported_coordinate(profile.get("longitude"), latitude=False)
    if latitude is not None:
        observation["latitude"] = latitude
    if longitude is not None:
        observation["longitude"] = longitude
    return observation


def _family_location(
    family: Mapping[str, Any],
    bound_profiles: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    selected_ip = _canonical_ip(family.get("ip"))
    location: dict[str, Any] = {
        "state": "unknown",
        "ip": selected_ip or None,
        "country_code": "",
        "country": "",
        "city": "",
        "continent": "",
        "timezone": "",
        "latitude": None,
        "longitude": None,
        "source": "",
        "observations_consistent": False,
        "conflicting_fields": [],
    }
    if str(family.get("state") or "").lower() != "confirmed" or not selected_ip:
        return location

    observations: list[dict[str, Any]] = []
    for item in bound_profiles:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("source") or "").strip().lower() != "ident.me":
            continue
        if _canonical_ip(item.get("ip")) != selected_ip:
            continue
        profile = item.get("profile")
        if not isinstance(profile, Mapping):
            continue
        observation = _location_observation(profile)
        if observation and observation not in observations:
            observations.append(observation)
    if not observations:
        return location

    conflicting_fields: list[str] = []
    for field in _LOCATION_VALUE_FIELDS:
        values: list[Any] = []
        for observation in observations:
            value = observation.get(field)
            if value in (None, "") or value in values:
                continue
            values.append(value)
        if len(values) == 1:
            location[field] = values[0]
        elif len(values) > 1:
            conflicting_fields.append(field)

    location.update(
        {
            "state": "conflict" if conflicting_fields else "reported",
            "source": "ident.me",
            "observations_consistent": not bool(conflicting_fields),
            "conflicting_fields": conflicting_fields,
        }
    )
    return location


def _normalize_identity(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(payload or {})
    families_raw = raw.get("identity") if isinstance(raw.get("identity"), Mapping) else raw

    def normalize_family(name: str, family: int) -> dict[str, Any]:
        value = families_raw.get(name) if isinstance(families_raw, Mapping) else None
        if isinstance(value, Mapping):
            item = dict(value)
            selected_address = _canonical_ip(item.get("ip") or item.get("value"))
            state = str(item.get("state") or "unavailable").strip().lower()
            confidence = item.get("confidence")
            agreement = item.get("agreement") if isinstance(item.get("agreement"), Mapping) else {}
            evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
            observed_ips = [
                observed_address
                for candidate in item.get("observed_ips", [])
                if (observed_address := _canonical_ip(candidate))
            ] if isinstance(item.get("observed_ips"), list) else []
            if selected_address and selected_address not in observed_ips:
                observed_ips.insert(0, selected_address)
            return {
                "family": family,
                "state": state,
                "confidence": confidence,
                "ip": selected_address or None,
                "observed_ips": observed_ips,
                "agreement": copy.deepcopy(dict(agreement)),
                "evidence": copy.deepcopy([entry for entry in evidence if isinstance(entry, dict)]),
            }
        address = _canonical_ip(value)
        return {
            "family": family,
            "state": "unconfirmed" if address else "unavailable",
            "confidence": 0.0,
            "ip": address or None,
            "observed_ips": [address] if address else [],
            "agreement": {},
            "evidence": [],
        }

    ipv4 = normalize_family("ipv4", 4)
    ipv6 = normalize_family("ipv6", 6)
    flat = _canonical_ip(raw.get("ip"))
    if flat:
        target = ipv4 if ipaddress.ip_address(flat).version == 4 else ipv6
        if not target["ip"]:
            target.update({"state": "unconfirmed", "ip": flat, "observed_ips": [flat]})
    states = {str(ipv4["state"]), str(ipv6["state"])}
    conflict = bool(raw.get("conflict")) or bool(states.intersection({"conflict", "rotating"}))
    confirmed = (
        not conflict
        and any(item["state"] == "confirmed" for item in (ipv4, ipv6))
        and all(item["state"] in {"confirmed", "unavailable"} for item in (ipv4, ipv6))
    )

    bound_profiles: list[dict[str, Any]] = []
    for family_name, normalized_family in (("ipv4", ipv4), ("ipv6", ipv6)):
        selected_ip = _canonical_ip(normalized_family.get("ip"))
        family_raw = families_raw.get(family_name) if isinstance(families_raw, Mapping) else None
        if not selected_ip or not isinstance(family_raw, Mapping):
            continue
        family_evidence = family_raw.get("evidence") if isinstance(family_raw.get("evidence"), list) else []
        for source_evidence in family_evidence:
            if not isinstance(source_evidence, Mapping):
                continue
            source = str(source_evidence.get("source") or "identity")
            candidates: list[Mapping[str, Any]] = []
            attempts = source_evidence.get("attempts")
            if isinstance(attempts, list):
                for attempt in attempts:
                    if not isinstance(attempt, Mapping) or attempt.get("status") != "ok":
                        continue
                    if _canonical_ip(attempt.get("ip")) != selected_ip:
                        continue
                    attempt_profile = attempt.get("profile")
                    if isinstance(attempt_profile, Mapping):
                        candidates.append(attempt_profile)
            profiles = source_evidence.get("profiles")
            if isinstance(profiles, Mapping):
                for profile_ip, profile in profiles.items():
                    if _canonical_ip(profile_ip) == selected_ip and isinstance(profile, Mapping):
                        candidates.append(profile)
            direct_profile = source_evidence.get("profile")
            if isinstance(direct_profile, Mapping):
                direct_ip = _canonical_ip(direct_profile.get("ip"))
                observed = {
                    _canonical_ip(item)
                    for item in source_evidence.get("observed_ips", [])
                    if _canonical_ip(item)
                } if isinstance(source_evidence.get("observed_ips"), list) else set()
                if direct_ip == selected_ip or (not direct_ip and observed == {selected_ip}):
                    candidates.append(direct_profile)
            for profile in candidates:
                profile_copy = copy.deepcopy(dict(profile))
                profile_ip = _canonical_ip(profile_copy.get("ip"))
                if profile_ip and profile_ip != selected_ip:
                    continue
                candidate = {
                    "family": int(normalized_family["family"]),
                    "ip": selected_ip,
                    "source": source,
                    "profile": profile_copy,
                }
                if candidate not in bound_profiles:
                    bound_profiles.append(candidate)

    ipv4["location"] = _family_location(ipv4, bound_profiles)
    ipv6["location"] = _family_location(ipv6, bound_profiles)

    network = raw.get("network") if isinstance(raw.get("network"), Mapping) else {}
    if not network and bound_profiles:
        network = bound_profiles[0]["profile"]
    diagnostics = raw.get("diagnostics") if isinstance(raw.get("diagnostics"), Mapping) else {}
    return {
        "ipv4": ipv4,
        "ipv6": ipv6,
        "confirmed": confirmed,
        "conflict": conflict,
        "network": copy.deepcopy(dict(network)),
        "network_profiles": bound_profiles,
        "diagnostics": {
            **copy.deepcopy(dict(diagnostics)),
            "collector_schema_version": raw.get("schema_version"),
            "collector_checked_at": raw.get("checked_at"),
            "rounds": raw.get("rounds"),
        },
    }


_FAMILY_NETWORK_FIELDS: tuple[str, ...] = (
    "asn",
    "prefix",
    "registered_country",
    "registry",
    "organisation",
    "provider",
    "type",
)


def _network_field_value(field: str, value: Any) -> str:
    if field == "asn":
        return _canonical_asn(value)
    if field == "prefix":
        text = _reported_text(value)
        if not text:
            return ""
        try:
            return str(ipaddress.ip_network(text, strict=False))
        except ValueError:
            return ""
    if field == "registered_country":
        return _reported_text(value, uppercase=True)
    if field in {"registry", "type"}:
        return _reported_text(value).lower()
    return _reported_text(value)


def _empty_network_observations() -> dict[str, list[dict[str, Any]]]:
    return {"ipv4": [], "ipv6": []}


def _add_network_observation(
    observations: dict[str, list[dict[str, Any]]],
    identity: Mapping[str, Any],
    *,
    source: str,
    target_ip: Any,
    values: Mapping[str, Any],
) -> None:
    address = _canonical_ip(target_ip)
    if not address:
        return
    family_name = "ipv4" if ipaddress.ip_address(address).version == 4 else "ipv6"
    family = identity.get(family_name)
    if not isinstance(family, Mapping) or _canonical_ip(family.get("ip")) != address:
        return
    normalized = {
        field: value
        for field in _FAMILY_NETWORK_FIELDS
        if (value := _network_field_value(field, values.get(field)))
    }
    if not normalized:
        return
    candidate = {
        "source": str(source or "network"),
        "ip": address,
        **normalized,
    }
    if candidate not in observations[family_name]:
        observations[family_name].append(candidate)


def _finalize_network_by_family(
    identity: Mapping[str, Any],
    observations: Mapping[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    by_family: dict[str, dict[str, Any]] = {}
    for family_name, family_number in (("ipv4", 4), ("ipv6", 6)):
        family = identity.get(family_name)
        selected_ip = (
            _canonical_ip(family.get("ip"))
            if isinstance(family, Mapping)
            else ""
        )
        sources = [
            copy.deepcopy(dict(item))
            for item in observations.get(family_name, [])
            if isinstance(item, Mapping) and _canonical_ip(item.get("ip")) == selected_ip
        ]
        result: dict[str, Any] = {
            "family": family_number,
            "ip": selected_ip or None,
            **{field: "" for field in _FAMILY_NETWORK_FIELDS},
            "sources": sources,
            "conflicts": [],
            "source_conflict": False,
        }
        for field in _FAMILY_NETWORK_FIELDS:
            values = list(
                dict.fromkeys(
                    str(item.get(field) or "")
                    for item in sources
                    if str(item.get(field) or "")
                )
            )
            if len(values) == 1:
                result[field] = values[0]
            elif len(values) > 1:
                result["conflicts"].append(field)
        result["source_conflict"] = bool(result["conflicts"])
        by_family[family_name] = result
    return by_family


def _team_cymru_lookup(
    ip: str,
    *,
    http_get: HttpGetter,
    timeout: float,
    proxy_url: str | None,
) -> dict[str, Any]:
    query = urllib.parse.urlencode({"name": team_cymru_query_name(ip), "type": "TXT"})
    response = http_get(
        f"{TEAM_CYMRU_DOH_URL}?{query}",
        headers={"Accept": "application/dns-json"},
        timeout=timeout,
        proxy_url=proxy_url,
    )
    _require_https_response_host(response, "cloudflare-dns.com")
    if response.status_code != 200:
        return {
            "status": "error",
            "error": {"code": "http_error", "http_status": response.status_code, "message": ""},
        }
    try:
        data = parse_team_cymru_doh(response.body)
    except Exception as exc:
        return {"status": "error", "error": {"code": "invalid_response", "message": _safe_error_text(exc)}}
    return {"status": "confirmed", "data": data}


def _onionoo_lookup(
    ip: str,
    *,
    http_get: HttpGetter,
    timeout: float,
    proxy_url: str | None,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    target = _canonical_ip(ip)
    if target and ipaddress.ip_address(target).version != 4:
        return {
            "status": "unknown",
            "data": {
                "tor": None,
                "coverage": "ipv4_exit_addresses_only",
            },
            "error": {
                "code": "ipv6_exit_evidence_unavailable",
                "message": "Onionoo does not publish exact IPv6 exit-address evidence",
            },
        }
    # Do not pre-filter by current running/Exit-flag state.  ``exit_addresses``
    # is already the exact past-24h exit evidence; an exit that just went
    # offline or lost the flag must not become a false negative.
    query = urllib.parse.urlencode({"search": ip})
    headers = {"Accept": "application/json"}
    previous = dict(previous or {})
    validators = previous.get("validators") if isinstance(previous.get("validators"), Mapping) else {}
    if validators.get("etag"):
        headers["If-None-Match"] = str(validators["etag"])
    if validators.get("last_modified"):
        headers["If-Modified-Since"] = str(validators["last_modified"])
    response = http_get(
        f"{ONIONOO_DETAILS_URL}?{query}",
        headers=headers,
        timeout=timeout,
        proxy_url=proxy_url,
    )
    _require_https_response_host(response, "onionoo.torproject.org")
    if response.status_code == 304:
        previous_status = str(previous.get("last_good_status") or previous.get("status") or "")
        previous_data = previous.get("data") if isinstance(previous.get("data"), dict) else None
        if previous_status in {"positive", "negative"} and previous_data is not None:
            return {
                "status": previous_status,
                "data": copy.deepcopy(previous_data),
                "validators": copy.deepcopy(dict(validators)),
                "cache_hit": True,
            }
        return {
            "status": "error",
            "error": {"code": "invalid_not_modified", "http_status": 304, "message": ""},
        }
    if response.status_code != 200:
        return {
            "status": "error",
            "error": {"code": "http_error", "http_status": response.status_code, "message": ""},
        }
    try:
        data = parse_onionoo_exact_ip(response.body, ip)
    except Exception as exc:
        return {"status": "error", "error": {"code": "invalid_response", "message": _safe_error_text(exc)}}
    response_validators = {
        "etag": response.headers.get("etag") or response.headers.get("ETag") or "",
        "last_modified": response.headers.get("last-modified")
        or response.headers.get("Last-Modified")
        or "",
    }
    tor = data.get("tor")
    return {
        "status": "positive" if tor is True else "negative" if tor is False else "unknown",
        "data": data,
        "validators": response_validators,
    }


def _proxycheck_lookup(
    ip: str,
    *,
    http_get: HttpGetter,
    timeout: float,
    proxy_url: str | None,
) -> dict[str, Any]:
    url = PROXYCHECK_URL_TEMPLATE.format(ip=urllib.parse.quote(ip, safe=""))
    response = http_get(
        url,
        headers={"Accept": "application/json"},
        timeout=timeout,
        proxy_url=proxy_url,
    )
    _require_https_response_host(response, "proxycheck.io")
    return parse_proxycheck_response(response, ip)


def _cached_evidence(
    cache: _SourceCache,
    source: str,
    ip: str,
    *,
    now: float,
) -> SourceEvidence | None:
    cached = cache.get(source, ip)
    if not cached:
        return None
    return SourceEvidence(
        source=source,
        status=str(cached.get("status") or "unknown"),
        observed_at=str(cached.get("observed_at") or _iso(now)),
        expires_at=str(cached.get("expires_at") or _iso(now)),
        positive=cached.get("positive") if isinstance(cached.get("positive"), bool) else None,
        data=copy.deepcopy(cached.get("data")) if isinstance(cached.get("data"), dict) else None,
        error=copy.deepcopy(cached.get("error")) if isinstance(cached.get("error"), dict) else None,
        cache_hit=True,
        stale=bool(cached.get("stale")),
    )


def _source_evidence(
    cache: _SourceCache,
    source: str,
    ip: str,
    *,
    ttl_seconds: int,
    lookup: Callable[[], dict[str, Any]],
    now: float,
) -> SourceEvidence:
    cached = _cached_evidence(cache, source, ip, now=now)
    if cached is not None:
        return cached
    previous = cache.peek(source, ip)
    try:
        result = lookup()
    except Exception as exc:
        result = {"status": "error", "error": {"code": "request_failed", "message": _safe_error_text(exc)}}
    status = str(result.get("status") or "unknown")
    error = result.get("error") if isinstance(result.get("error"), dict) else None
    previous_status = str(
        (previous or {}).get("last_good_status")
        or (previous or {}).get("status")
        or ""
    )
    if (
        status in {"error", "unknown", "rate_limited", "denied"}
        and previous_status in {"positive", "negative", "confirmed"}
    ):
        evidence = SourceEvidence(
            source=source,
            status=status,
            observed_at=str(previous.get("observed_at") or _iso(now)),
            expires_at=str(previous.get("expires_at") or _iso(now)),
            positive=previous.get("positive") if isinstance(previous.get("positive"), bool) else None,
            data=copy.deepcopy(previous.get("data")) if isinstance(previous.get("data"), dict) else None,
            error=copy.deepcopy(error),
            cache_hit=True,
            stale=True,
        )
        if status in {"rate_limited", "denied"}:
            retry_after = max(1, int((error or {}).get("retry_after_seconds") or 300))
            cache.put(
                source,
                ip,
                {
                    "status": status,
                    "last_good_status": previous_status,
                    "observed_at": evidence.observed_at,
                    "expires_at": evidence.expires_at,
                    "positive": evidence.positive,
                    "data": evidence.data,
                    "error": evidence.error,
                    "stale": True,
                    "validators": copy.deepcopy((previous or {}).get("validators"))
                    if isinstance((previous or {}).get("validators"), Mapping)
                    else {},
                },
                expires_at=now + retry_after,
            )
        return evidence
    expiry = now + max(1, int(ttl_seconds))
    if status in {"rate_limited", "denied"} and error:
        expiry = now + max(1, int(error.get("retry_after_seconds") or 300))
    positive = True if status == "positive" else False if status == "negative" else None
    evidence = SourceEvidence(
        source=source,
        status=status,
        observed_at=_iso(now),
        expires_at=_iso(expiry),
        positive=positive,
        data=copy.deepcopy(result.get("data")) if isinstance(result.get("data"), dict) else None,
        error=copy.deepcopy(error),
        cache_hit=bool(result.get("cache_hit")),
        stale=False,
    )
    if status not in {"error", "unknown"}:
        cache.put(
            source,
            ip,
            {
                "status": status,
                "observed_at": evidence.observed_at,
                "expires_at": evidence.expires_at,
                "positive": positive,
                "data": evidence.data,
                "error": evidence.error,
                "stale": False,
                "last_good_status": status if status in {"positive", "negative", "confirmed"} else "",
                "validators": copy.deepcopy(result.get("validators"))
                if isinstance(result.get("validators"), Mapping)
                else {},
            },
            expires_at=expiry,
        )
    return evidence


def _tri_state(values: Iterable[bool | None]) -> bool | None:
    normalized = list(values)
    if any(value is True for value in normalized):
        return True
    known = [value for value in normalized if isinstance(value, bool)]
    return (
        False
        if known and len(known) == len(normalized) and all(value is False for value in known)
        else None
    )


def _fresh_negative(evidence: Iterable[SourceEvidence], source: str) -> bool:
    matching = [item for item in evidence if item.source == source]
    return bool(matching) and all(
        item.status == "negative" and not item.stale
        for item in matching
    )


def _public_signal_state(value: bool | None, evidence: Iterable[SourceEvidence]) -> str:
    items = list(evidence)
    # Current positive evidence always wins over missing or negative evidence.
    if value is True:
        return "detected"
    if any(item.status == "conflict" for item in items):
        return "conflict"
    if any(item.stale or item.status == "stale" for item in items):
        return "stale"
    if any(item.status == "rate_limited" for item in items):
        return "rate_limited"
    if any(item.status in {"error", "denied", "unknown"} for item in items):
        return "unknown"
    if value is False:
        return "not_detected"
    return "unknown"


def _public_evidence_state(item: SourceEvidence) -> str:
    if item.positive is True and not item.stale and item.status == "positive":
        return "detected"
    if item.status == "conflict":
        return "conflict"
    if item.stale or item.status == "stale":
        return "stale"
    if item.status == "rate_limited":
        return "rate_limited"
    if item.positive is False and item.status == "negative":
        return "not_detected"
    if item.status == "confirmed":
        return "detected"
    return "unknown"


def _public_evidence(item: SourceEvidence) -> dict[str, Any]:
    payload = item.to_dict()
    payload["state"] = _public_evidence_state(item)
    return payload


FREE_CHECK_GROUP_ORDER: tuple[str, ...] = (
    "identity",
    "network",
    "anonymizer",
    "cloud",
    "threat",
)
FREE_CHECK_GROUP_SOURCES: dict[str, tuple[str, ...]] = {
    "identity": ("ipify", "ident.me"),
    "network": ("ident.me", "team_cymru"),
    "anonymizer": ("onionoo",),
    "cloud": ("cloud_aws", "cloud_gcp", "cloud_azure", "cloud_oci"),
    "threat": ("spamhaus_drop", "spamhaus_dropv6", "spamhaus_asndrop", "feodo"),
}
_GROUP_RESOLVED_STATUSES = {"confirmed", "observed", "positive", "negative", "fresh"}
QUALITY_SCORE_ALGORITHM = "tesseract-free-evidence-v3"
QUALITY_SCORE_GROUP_WEIGHTS: dict[str, int] = {
    "identity": 15,
    "network": 10,
    "anonymizer": 25,
    "cloud": 10,
    "threat": 40,
}
QUALITY_SCORE_PRIOR = 0.55
QUALITY_SCORE_MIN_CONFIDENCE = 45


def _evidence_target_ip(item: SourceEvidence) -> str:
    return _canonical_ip((item.data or {}).get("target_ip"))


def _conservative_evidence_time(items: Iterable[SourceEvidence], field: str) -> str:
    candidates = [
        (timestamp, str(getattr(item, field, "") or ""))
        for item in items
        if (timestamp := _float_timestamp(getattr(item, field, ""))) > 0
    ]
    return min(candidates, key=lambda candidate: candidate[0])[1] if candidates else ""


def _evidence_warning_codes(items: Iterable[SourceEvidence]) -> list[str]:
    warnings: set[str] = set()
    for item in items:
        if item.stale or item.status == "stale":
            warnings.add("stale_data")
        if item.status == "conflict":
            warnings.add("source_conflict")
        error = item.error if isinstance(item.error, Mapping) else {}
        code = str(error.get("code") or "").strip()
        if code:
            warnings.add(code)
        elif item.status in {"rate_limited", "denied", "error", "unknown"}:
            warnings.add(item.status)
    return sorted(warnings)


def _evidence_retry_at(items: Iterable[SourceEvidence], *, started_at: float) -> str:
    retry_seconds = [
        value
        for item in items
        if isinstance(item.error, Mapping)
        and isinstance((value := item.error.get("retry_after_seconds")), int)
        and not isinstance(value, bool)
        and value > 0
    ]
    return _iso(float(started_at) + min(retry_seconds)) if retry_seconds else ""


def _group_item_is_match(group: str, item: SourceEvidence) -> bool:
    if item.stale or item.status != "positive" or item.positive is not True:
        return False
    if group == "anonymizer":
        return item.source == "onionoo"
    return group in {"cloud", "threat"}


def _group_source_state(
    *,
    group: str,
    items: list[SourceEvidence],
    complete: bool,
    matched: bool,
) -> str:
    if matched:
        return "detected"
    statuses = {str(item.status or "unknown") for item in items}
    if "conflict" in statuses:
        return "conflict"
    if "rate_limited" in statuses:
        return "rate_limited"
    if "denied" in statuses:
        return "denied"
    if "error" in statuses or any(
        item.status == "unknown" and isinstance(item.error, Mapping)
        for item in items
    ):
        return "error"
    if any(item.stale or item.status == "stale" for item in items):
        return "stale"
    # A completed provider query that contains a positive signal below the
    # product's positive-evidence threshold is neither a clean non-hit nor a
    # detected result.  Keep it visibly partial without inflating confidence.
    if any(item.status == "positive" for item in items):
        return "partial"
    if complete:
        return "not_detected"
    if items:
        return "partial"
    return "unknown"


def _free_check_groups(
    *,
    identity: Mapping[str, Any],
    network: Mapping[str, Any],
    evidence: Iterable[SourceEvidence],
    started_at: float,
) -> dict[str, dict[str, Any]]:
    """Aggregate provider evidence into five stable, UI-ready free checks.

    Source rows remain target-aware so a successful IPv4 lookup cannot hide a
    failed IPv6 lookup.  Exact/current positive evidence wins the group state,
    while ``complete`` and warning codes retain any evidence gaps.
    """

    items = list(evidence)
    evidence_by_source: dict[str, list[SourceEvidence]] = {}
    for item in items:
        evidence_by_source.setdefault(item.source, []).append(item)

    family_targets: dict[str, str] = {}
    family_states: dict[str, str] = {}
    for family_name in ("ipv4", "ipv6"):
        family = identity.get(family_name)
        if not isinstance(family, Mapping):
            continue
        address = _canonical_ip(family.get("ip"))
        if address:
            family_targets[family_name] = address
            family_states[family_name] = str(family.get("state") or "unknown").lower()
    target_ips = list(dict.fromkeys(family_targets.values()))
    ipv4_targets = [address for address in target_ips if ipaddress.ip_address(address).version == 4]
    ipv6_targets = [address for address in target_ips if ipaddress.ip_address(address).version == 6]

    asns_by_target: dict[str, set[str]] = {}
    for item in network.get("asn_sources", []):
        if not isinstance(item, Mapping):
            continue
        address = _canonical_ip(item.get("ip"))
        asn = _canonical_asn(item.get("asn"))
        if address and asn:
            asns_by_target.setdefault(address, set()).add(asn)
    unique_asn_targets = {
        address for address, values in asns_by_target.items() if len(values) == 1
    }

    expected_by_group: dict[str, list[str]] = {
        "identity": list(FREE_CHECK_GROUP_SOURCES["identity"]) if target_ips else [],
        "network": list(FREE_CHECK_GROUP_SOURCES["network"]) if target_ips else [],
        "anonymizer": ["onionoo"] if ipv4_targets else [],
        "cloud": list(FREE_CHECK_GROUP_SOURCES["cloud"]) if target_ips else [],
        "threat": (
            (["spamhaus_drop", "feodo"] if ipv4_targets else [])
            + (["spamhaus_dropv6"] if ipv6_targets else [])
            + (["spamhaus_asndrop"] if unique_asn_targets else [])
        ),
    }

    def targets_for_source(source: str) -> list[str]:
        if source in {"ipify", "ident.me", "team_cymru"}:
            return target_ips
        if source in {"onionoo", "spamhaus_drop", "feodo"}:
            return ipv4_targets
        if source == "spamhaus_dropv6":
            return ipv6_targets
        if source == "spamhaus_asndrop":
            return [address for address in target_ips if address in unique_asn_targets]
        return target_ips

    def current_row(item: SourceEvidence) -> bool:
        return not item.stale and item.status in _GROUP_RESOLVED_STATUSES

    def relevant_source_items(
        group: str,
        source: str,
        source_items: list[SourceEvidence],
    ) -> list[SourceEvidence]:
        if group in {"identity", "network"} and source == "ident.me":
            return source_items
        if source not in {"team_cymru", "onionoo"}:
            return source_items
        required = set(targets_for_source(source))
        return [item for item in source_items if _evidence_target_ip(item) in required]

    def source_complete(group: str, source: str, source_items: list[SourceEvidence]) -> bool:
        required_targets = targets_for_source(source)
        if not required_targets:
            return False
        if group == "identity":
            return bool(identity.get("confirmed")) and all(
                state == "confirmed" for state in family_states.values()
            )
        if group == "network" and source == "ident.me":
            by_family = network.get("by_family") if isinstance(network.get("by_family"), Mapping) else {}
            for family_name, address in family_targets.items():
                family_network = by_family.get(family_name)
                rows = (
                    family_network.get("sources")
                    if isinstance(family_network, Mapping)
                    and isinstance(family_network.get("sources"), list)
                    else []
                )
                if not any(
                    isinstance(row, Mapping)
                    and str(row.get("source") or "").strip().lower().rsplit(":", 1)[-1]
                    == "ident.me"
                    and _canonical_ip(row.get("ip")) == address
                    for row in rows
                ):
                    return False
            return True
        if source in {"team_cymru", "onionoo"}:
            return all(
                any(
                    _evidence_target_ip(item) == target and current_row(item)
                    for item in source_items
                )
                for target in required_targets
            )
        # Static datasets are one validated snapshot evaluated against all
        # applicable targets.  Missing specs remain an explicit evidence gap.
        if not source_items or not all(current_row(item) for item in source_items):
            return False
        evaluated_targets = {
            address
            for item in source_items
            for value in (
                (item.data or {}).get("evaluated_ips")
                if isinstance((item.data or {}).get("evaluated_ips"), list)
                else []
            )
            if (address := _canonical_ip(value))
        }
        return set(required_targets).issubset(evaluated_targets)

    groups: dict[str, dict[str, Any]] = {}
    identity_item = evidence_by_source.get("identity", [])
    for group in FREE_CHECK_GROUP_ORDER:
        expected_sources = expected_by_group[group]
        checked_sources: list[str] = []
        matched_sources: list[str] = []
        source_results: list[dict[str, Any]] = []
        group_items: list[SourceEvidence] = []
        group_warning_codes: set[str] = set()

        for source in expected_sources:
            source_items = relevant_source_items(
                group,
                source,
                identity_item
                if group == "identity"
                else identity_item
                if group == "network" and source == "ident.me"
                else evidence_by_source.get(source, []),
            )
            group_items.extend(source_items)
            complete = source_complete(group, source, source_items)
            matched = any(_group_item_is_match(group, item) for item in source_items)
            if complete:
                checked_sources.append(source)
            if matched:
                matched_sources.append(source)
            warnings = set(_evidence_warning_codes(source_items))
            if not complete:
                warnings.add("incomplete_coverage")
            fresh_outcomes = {
                "detected" if _group_item_is_match(group, item) else "not_detected"
                for item in source_items
                if current_row(item) and item.status in {"positive", "negative"}
            }
            if len(fresh_outcomes) > 1:
                warnings.add("family_results_differ")
            group_warning_codes.update(warnings)
            source_result = {
                "source": source,
                "state": _group_source_state(
                    group=group,
                    items=source_items,
                    complete=complete,
                    matched=matched,
                ),
                "complete": complete,
                "target_ips": targets_for_source(source),
                "matched": matched,
                "warning_codes": sorted(warnings),
                "observed_at": _conservative_evidence_time(source_items, "observed_at"),
                "expires_at": _conservative_evidence_time(source_items, "expires_at"),
            }
            retry_at = _evidence_retry_at(source_items, started_at=started_at)
            if retry_at:
                source_result["retry_at"] = retry_at
            source_results.append(source_result)

        if group == "identity" and identity.get("conflict"):
            group_warning_codes.add("identity_conflict")
        if group == "network" and (
            network.get("source_conflict") or network.get("asn_conflict")
        ):
            group_warning_codes.add("network_source_conflict")

        applicable = bool(expected_sources)
        complete = applicable and len(checked_sources) == len(expected_sources)
        if matched_sources:
            state = "detected"
        elif group == "identity" and identity.get("conflict"):
            state = "conflict"
        elif group == "network" and (
            network.get("source_conflict") or network.get("asn_conflict")
        ):
            state = "conflict"
        elif not applicable:
            state = "not_applicable"
        else:
            source_states = [str(item.get("state") or "unknown") for item in source_results]
            state = next(
                (
                    candidate
                    for candidate in ("conflict", "rate_limited", "denied", "error", "stale")
                    if candidate in source_states
                ),
                "not_detected"
                if complete and all(value == "not_detected" for value in source_states)
                else "partial"
                if target_ips and (checked_sources or group_items)
                else "unknown",
            )
        if applicable and not complete and target_ips:
            group_warning_codes.add("incomplete_coverage")
        group_targets = list(
            dict.fromkeys(
                target
                for source in expected_sources
                for target in targets_for_source(source)
            )
        )
        group_result = {
            "state": state,
            "applicable": applicable,
            "complete": complete,
            "checked_sources": checked_sources,
            "expected_sources": expected_sources,
            "target_ips": group_targets,
            "matched_sources": matched_sources,
            "warning_codes": sorted(group_warning_codes),
            "observed_at": _conservative_evidence_time(group_items, "observed_at"),
            "expires_at": _conservative_evidence_time(group_items, "expires_at"),
            "source_results": source_results,
        }
        retry_at = _evidence_retry_at(group_items, started_at=started_at)
        if retry_at:
            group_result["retry_at"] = retry_at
        groups[group] = group_result
    return groups


def _quality_score(
    *,
    groups: Mapping[str, Mapping[str, Any]],
    conclusion: str,
    route_confirmed: bool,
    route_changed: bool,
    identity_confirmed: bool,
    identity_conflict: bool,
) -> dict[str, Any]:
    """Build a conservative score from the free evidence groups.

    Missing evidence is held at a neutral prior instead of being treated as a
    clean result.  Definitive matches cap the score by severity, and no number
    is published until the current route and exit identity are confirmed with
    enough resolved evidence.  Supplier risk/confidence numbers are therefore
    never exposed as the product score.
    """

    unavailable = {
        "quality_score": None,
        "quality_grade": "unavailable",
        "score_confidence": 0,
        "score_algorithm": QUALITY_SCORE_ALGORITHM,
    }
    if route_changed or not route_confirmed or not identity_confirmed or identity_conflict:
        return unavailable

    weighted_score = 0.0
    applicable_weight = 0.0
    resolved_weight = 0.0
    for key, weight in QUALITY_SCORE_GROUP_WEIGHTS.items():
        payload = groups.get(key)
        group = payload if isinstance(payload, Mapping) else {}
        if "applicable" in group:
            applicable = bool(group.get("applicable"))
        elif "expected_sources" in group:
            expected_sources = group.get("expected_sources")
            applicable = bool(expected_sources) if isinstance(expected_sources, list) else True
        else:
            # Preserve deterministic behavior for schema-v2/direct-call
            # payloads that predate the explicit applicability marker.
            applicable = bool(group)
        if not applicable:
            continue
        applicable_weight += weight
        state = str(group.get("state") or "unknown").strip().lower()
        complete = bool(group.get("complete"))
        checked_sources = group.get("checked_sources")
        checked_count = len(checked_sources) if isinstance(checked_sources, list) else 0

        if state == "detected":
            # Exact cloud-prefix membership is a moderate reputation signal;
            # exact threat/anonymizer membership is a severe one.
            factor = 0.55 if key == "cloud" else 0.0
            resolved_weight += weight
        elif complete and state in {"not_detected", "confirmed", "observed"}:
            factor = 1.0
            resolved_weight += weight
        elif state == "conflict":
            factor = 0.0 if key in {"identity", "network"} else QUALITY_SCORE_PRIOR
            resolved_weight += weight * 0.5
        else:
            # Unknown, partial, limited, stale and failed checks do not earn
            # clean points.  They retain a neutral prior until verified.
            factor = QUALITY_SCORE_PRIOR
            if checked_count:
                resolved_weight += weight * 0.25
        weighted_score += weight * factor

    if applicable_weight <= 0:
        return unavailable
    score_confidence = max(
        0,
        min(100, int(round((resolved_weight / applicable_weight) * 100))),
    )
    if score_confidence < QUALITY_SCORE_MIN_CONFIDENCE:
        return {
            **unavailable,
            "score_confidence": score_confidence,
        }

    score = int(round((weighted_score / applicable_weight) * 100))
    conclusion_caps = {
        "high_risk": 20,
        "anonymizer_detected": 45,
        "hosting_detected": 82,
        "needs_attention": 68,
        "insufficient_evidence": 72,
    }
    if conclusion in conclusion_caps:
        score = min(score, conclusion_caps[conclusion])
    score = max(0, min(96, score))
    grade = (
        "excellent"
        if score >= 90
        else "good"
        if score >= 80
        else "attention"
        if score >= 70
        else "poor"
        if score >= 40
        else "high_risk"
    )
    return {
        "quality_score": score,
        "quality_grade": grade,
        "score_confidence": score_confidence,
        "score_algorithm": QUALITY_SCORE_ALGORITHM,
    }


def _conclusion(
    *,
    identity_confirmed: bool,
    identity_conflict: bool,
    evidence: list[SourceEvidence],
    reputation: dict[str, Any],
    groups: Mapping[str, Mapping[str, Any]] | None = None,
    asn_conflict: bool = False,
) -> str:
    signals = reputation["signals"]
    tor_exact = any(
        item.source == "onionoo"
        and item.positive is True
        and not item.stale
        and item.status == "positive"
        for item in evidence
    )
    if signals["threat"] is True:
        return "high_risk"
    if tor_exact:
        return "anonymizer_detected"
    if signals["cloud"] is True:
        return "hosting_detected"
    stale_positive = any(
        item.source != "proxycheck" and item.positive is True and item.stale
        for item in evidence
    )
    if identity_conflict or asn_conflict or stale_positive:
        return "needs_attention"

    applicable_groups = [
        payload
        for payload in dict(groups or {}).values()
        if isinstance(payload, Mapping) and bool(payload.get("applicable", True))
    ]
    fully_resolved = bool(applicable_groups) and all(
        bool(payload.get("complete"))
        and str(payload.get("state") or "").strip().lower() == "not_detected"
        for payload in applicable_groups
    )
    if identity_confirmed and fully_resolved:
        return "no_known_issue"
    return "insufficient_evidence"


def _conclusion_confidence(
    conclusion: str,
    evidence: Iterable[SourceEvidence],
    *,
    identity_states: Mapping[str, str] | None = None,
) -> int | None:
    """Bind confidence to the exact evidence supporting the conclusion."""

    items = list(evidence)
    fresh_positive = [
        item
        for item in items
        if item.positive is True and item.status == "positive" and not item.stale
    ]
    states_by_ip = {
        _canonical_ip(address): str(state or "").strip().lower()
        for address, state in dict(identity_states or {}).items()
        if _canonical_ip(address)
    }

    def bound_to_confirmed_identity(item: SourceEvidence, confidence: int) -> int:
        data = item.data or {}
        targets: list[str] = []
        direct = _canonical_ip(data.get("target_ip"))
        if direct:
            targets.append(direct)
        raw_targets = data.get("target_ips")
        if isinstance(raw_targets, list):
            targets.extend(
                target
                for value in raw_targets
                if (target := _canonical_ip(value))
            )
        matches = data.get("matches")
        if isinstance(matches, list):
            for match in matches:
                if not isinstance(match, Mapping):
                    continue
                target = _canonical_ip(match.get("target_ip"))
                if target:
                    targets.append(target)
                match_targets = match.get("target_ips")
                if isinstance(match_targets, list):
                    targets.extend(
                        normalized
                        for value in match_targets
                        if (normalized := _canonical_ip(value))
                    )
        targets = list(dict.fromkeys(targets))
        if targets and not any(states_by_ip.get(target) == "confirmed" for target in targets):
            return min(int(confidence), 25)
        return int(confidence)

    if conclusion == "high_risk":
        exact_values = [
            bound_to_confirmed_identity(item, 100)
            for item in fresh_positive
            if item.source in {"spamhaus_drop", "spamhaus_dropv6", "spamhaus_asndrop", "feodo"}
        ]
        return max(exact_values) if exact_values else None

    if conclusion == "anonymizer_detected":
        exact_values = [
            bound_to_confirmed_identity(item, 100)
            for item in fresh_positive
            if item.source == "onionoo"
        ]
        return max(exact_values) if exact_values else None

    if conclusion == "hosting_detected":
        exact_values = [
            bound_to_confirmed_identity(item, 100)
            for item in fresh_positive
            if item.source.startswith("cloud_")
        ]
        return max(exact_values) if exact_values else None

    if conclusion == "needs_attention":
        if any(
            item.stale
            or item.status in {"conflict", "rate_limited", "denied", "unknown"}
            for item in items
        ):
            return 25
        return 25

    if conclusion == "no_known_issue":
        # A non-hit across the applicable exact sources is still not a promise
        # of future cleanliness, so keep the conclusion confidence moderate.
        return 50

    return None


class ExitQualityService:
    def __init__(
        self,
        exit_quality_dir: str | Path,
        *,
        results_path: str | Path | None = None,
        route_snapshot: RouteSnapshot,
        identity_collector: IdentityCollector = collect_exit_identity,
        http_get: HttpGetter = default_http_get,
        internal_request_observer: InternalRequestObserver | None = None,
        dataset_specs: Iterable[DatasetSpec] | None = None,
        clock: Callable[[], float] = time.time,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        self.exit_quality_dir = Path(exit_quality_dir)
        self.results_path = Path(results_path) if results_path is not None else self.exit_quality_dir / "results-v2.json"
        self.route_snapshot = route_snapshot
        self.identity_collector = identity_collector
        self._raw_http_get = http_get
        self.internal_request_observer = internal_request_observer
        self.http_get: HttpGetter = self._observed_http_get
        self.dataset_specs = tuple(DEFAULT_DATASET_SPECS if dataset_specs is None else dataset_specs)
        self.clock = clock
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.logger = logger or logging.getLogger("tesseract.exit_quality")
        self.source_cache = _SourceCache(self.exit_quality_dir / "source-cache.json", clock=clock)
        self.result_store = _ResultStore(self.results_path, clock=clock)
        self.dataset_updater = DatasetUpdater(
            self.exit_quality_dir / "datasets",
            http_get=self.http_get,
            clock=clock,
            timeout_seconds=self.timeout_seconds,
            logger=self.logger,
        )

    def _observed_http_get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        proxy_url: str | None = None,
    ) -> HttpResponse:
        try:
            timeout_seconds = float(timeout)
        except (TypeError, ValueError):
            timeout_seconds = DEFAULT_TIMEOUT_SECONDS
        observer = self.internal_request_observer
        if observer is not None:
            try:
                # Registration must happen before the request so Core log
                # attribution cannot race ahead of the internal-flow marker.
                # Observers are intentionally required to be non-blocking.
                observer(str(url), timeout_seconds)
            except Exception:
                try:
                    self.logger.debug(
                        "Exit-quality internal request observer failed",
                        exc_info=True,
                    )
                except Exception:
                    pass
        return self._raw_http_get(
            url,
            headers=headers,
            timeout=timeout,
            proxy_url=proxy_url,
        )

    def recent_results(self) -> list[dict[str, Any]]:
        return self.result_store.records()

    def latest_result(self, *, binding_marker: str | None = None) -> dict[str, Any] | None:
        records = self.recent_results()
        if binding_marker:
            records = [
                item
                for item in records
                if isinstance(item.get("route"), dict)
                and item["route"].get("binding_marker") == binding_marker
            ]
        if not records:
            return None
        result = copy.deepcopy(records[-1])
        checked_at = _float_timestamp(result.get("checked_at"))
        age_seconds = max(0.0, self.clock() - checked_at) if checked_at > 0 else float("inf")
        freshness_state = "fresh" if age_seconds <= 5 * 60 else "stale" if age_seconds <= 30 * 60 else "expired"
        freshness = result.get("freshness") if isinstance(result.get("freshness"), dict) else {}
        result["freshness"] = {
            **freshness,
            "state": freshness_state,
            "age_seconds": None if age_seconds == float("inf") else round(age_seconds, 1),
        }
        return result

    def refresh_static_sources(
        self,
        *,
        proxy_url: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, DatasetSnapshot]:
        """Refresh large static sources outside the user-triggered check path."""
        snapshots: dict[str, DatasetSnapshot] = {}
        for spec in self.dataset_specs:
            if cancel_event is not None and cancel_event.is_set():
                break
            try:
                snapshot = self.dataset_updater.refresh(
                    spec,
                    proxy_url=proxy_url,
                    cancel_event=cancel_event,
                )
            except DatasetRefreshCancelled:
                break
            if cancel_event is not None and cancel_event.is_set():
                break
            snapshots[spec.name] = snapshot
        return snapshots

    def _route_snapshot(self) -> tuple[dict[str, Any], str]:
        try:
            raw = self.route_snapshot()
        except Exception as exc:
            return _normalize_route(None), _safe_error_text(exc)
        return _normalize_route(raw), ""

    def _identity(self, *, proxy_url: str | None, request_id: str) -> tuple[dict[str, Any], str]:
        try:
            payload = _call_identity_collector(
                self.identity_collector,
                proxy_url=proxy_url,
                request_id=request_id,
            )
            return _normalize_identity(payload), ""
        except Exception as exc:
            return _normalize_identity(None), _safe_error_text(exc)

    def check(self, *, proxy_url: str | None = None, request_id: str = "") -> ExitQualityResultV2:
        request_id = str(request_id or "")
        started_at = self.clock()
        route_before, route_before_error = self._route_snapshot()
        identity, identity_error = self._identity(proxy_url=proxy_url, request_id=request_id)
        ips = list(
            dict.fromkeys(
                address
                for family_name in ("ipv4", "ipv6")
                if isinstance((family := identity.get(family_name)), Mapping)
                and (address := _canonical_ip(family.get("ip")))
            )
        )
        evidence: list[SourceEvidence] = []
        team_request_durations_ms: list[float] = []

        identity_expiry = started_at + IDENTITY_TTL_SECONDS
        identity_status = (
            "conflict"
            if identity.get("conflict")
            else "confirmed"
            if identity.get("confirmed")
            else "observed"
            if ips
            else "error"
        )
        evidence.append(
            SourceEvidence(
                source="identity",
                status=identity_status,
                observed_at=_iso(started_at),
                expires_at=_iso(identity_expiry),
                positive=None,
                data={"ipv4": identity.get("ipv4"), "ipv6": identity.get("ipv6")},
                error={"code": "identity_unavailable", "message": identity_error} if not ips else None,
            )
        )

        for address in ips:
            team_started = time.perf_counter()
            team = _source_evidence(
                self.source_cache,
                "team_cymru",
                address,
                ttl_seconds=TEAM_CYMRU_TTL_SECONDS,
                lookup=lambda address=address: _team_cymru_lookup(
                    address,
                    http_get=self.http_get,
                    timeout=self.timeout_seconds,
                    proxy_url=proxy_url,
                ),
                now=started_at,
            )
            team_elapsed_ms = max(0.0, (time.perf_counter() - team_started) * 1000.0)
            if not team.cache_hit:
                team_request_durations_ms.append(team_elapsed_ms)
            team_data = {**(team.data or {}), "target_ip": address}
            team = SourceEvidence(**{**team.to_dict(), "data": team_data})
            evidence.append(team)
            if ipaddress.ip_address(address).version == 4:
                onionoo_previous = self.source_cache.peek("onionoo", address)
                onionoo = _source_evidence(
                    self.source_cache,
                    "onionoo",
                    address,
                    ttl_seconds=ONIONOO_TTL_SECONDS,
                    lookup=lambda address=address: _onionoo_lookup(
                        address,
                        http_get=self.http_get,
                        timeout=self.timeout_seconds,
                        proxy_url=proxy_url,
                        previous=onionoo_previous,
                    ),
                    now=started_at,
                )
                onionoo = SourceEvidence(
                    **{
                        **onionoo.to_dict(),
                        "data": {**(onionoo.data or {}), "target_ip": address},
                    }
                )
                evidence.append(onionoo)

        network = {
            "asn": "",
            "prefix": "",
            "country": "",
            "registered_country": "",
            "registry": "",
            "organisation": "",
            "provider": "",
            "type": "",
            "cloud_matches": [],
            "asn_sources": [],
            "asn_conflict": False,
            "source_conflict": False,
            "by_family": {},
        }
        identity_network = identity.get("network") if isinstance(identity.get("network"), dict) else {}
        network.update(
            {
                "asn": _canonical_asn(identity_network.get("asn")),
                "prefix": str(identity_network.get("prefix") or ""),
                "country": str(identity_network.get("country") or ""),
                "registered_country": str(identity_network.get("registered_country") or ""),
                "registry": str(identity_network.get("registry") or ""),
                "organisation": str(
                    identity_network.get("organisation")
                    or identity_network.get("organization")
                    or identity_network.get("org")
                    or identity_network.get("aso")
                    or ""
                ),
                "provider": str(identity_network.get("provider") or identity_network.get("isp") or ""),
                "type": str(identity_network.get("type") or ""),
                "cloud_matches": [],
                "asn_sources": [],
                "asn_conflict": False,
                "source_conflict": False,
                "by_family": {},
            }
        )
        asn_sources: list[dict[str, str]] = []
        network_observations = _empty_network_observations()

        def add_asn_source(source: str, target_ip: Any, value: Any) -> None:
            address = _canonical_ip(target_ip)
            asn = _canonical_asn(value)
            if not address or not asn:
                return
            candidate = {"source": str(source), "ip": address, "asn": asn}
            if candidate not in asn_sources:
                asn_sources.append(candidate)

        identity_profiles = (
            identity.get("network_profiles")
            if isinstance(identity.get("network_profiles"), list)
            else []
        )
        for item in identity_profiles:
            if not isinstance(item, Mapping) or not isinstance(item.get("profile"), Mapping):
                continue
            profile = item["profile"]
            add_asn_source(
                f"identity:{item.get('source') or 'profile'}",
                item.get("ip"),
                profile.get("asn"),
            )
            _add_network_observation(
                network_observations,
                identity,
                source=f"identity:{item.get('source') or 'profile'}",
                target_ip=item.get("ip"),
                values={
                    "asn": profile.get("asn"),
                    "prefix": profile.get("prefix") or profile.get("range"),
                    "registered_country": profile.get("registered_country"),
                    "registry": profile.get("registry"),
                    "organisation": (
                        profile.get("organisation")
                        or profile.get("organization")
                        or profile.get("org")
                        or profile.get("aso")
                    ),
                    "provider": profile.get("provider") or profile.get("isp"),
                    "type": profile.get("type"),
                },
            )
        if not identity_profiles and ips:
            add_asn_source("identity", ips[0], identity_network.get("asn"))

        for item in evidence:
            data = item.data or {}
            if item.source == "team_cymru" and item.status == "confirmed":
                team_asns = [
                    asn
                    for value in (
                        data.get("asns")
                        if isinstance(data.get("asns"), list)
                        else [data.get("asn")]
                    )
                    if (asn := _canonical_asn(value))
                ]
                for asn in dict.fromkeys(team_asns):
                    add_asn_source("team_cymru", data.get("target_ip"), asn)
                    _add_network_observation(
                        network_observations,
                        identity,
                        source="team_cymru",
                        target_ip=data.get("target_ip"),
                        values={
                            "asn": asn,
                            "prefix": data.get("prefix"),
                            "registered_country": data.get("country"),
                            "registry": data.get("registry"),
                        },
                    )
                if not team_asns:
                    _add_network_observation(
                        network_observations,
                        identity,
                        source="team_cymru",
                        target_ip=data.get("target_ip"),
                        values={
                            "prefix": data.get("prefix"),
                            "registered_country": data.get("country"),
                            "registry": data.get("registry"),
                        },
                    )
                if len(set(team_asns)) == 1:
                    network["asn"] = network["asn"] or team_asns[0]
                for key in ("prefix", "registry"):
                    network[key] = network[key] or str(data.get(key) or "")
                network["registered_country"] = (
                    network["registered_country"] or str(data.get("country") or "")
                )

        asns_by_ip: dict[str, set[str]] = {}
        for item in asn_sources:
            asns_by_ip.setdefault(item["ip"], set()).add(item["asn"])
        network["asn_sources"] = asn_sources
        network["asn_conflict"] = any(len(values) > 1 for values in asns_by_ip.values())
        network["by_family"] = _finalize_network_by_family(identity, network_observations)
        network["source_conflict"] = any(
            bool(item.get("source_conflict"))
            for item in network["by_family"].values()
            if isinstance(item, Mapping)
        )
        primary_ip = next(
            (
                _canonical_ip(identity[family_name].get("ip"))
                for family_name in ("ipv4", "ipv6")
                if isinstance(identity.get(family_name), Mapping)
                and _canonical_ip(identity[family_name].get("ip"))
            ),
            "",
        )
        primary_asns = asns_by_ip.get(primary_ip, set())
        network["asn"] = next(iter(primary_asns)) if len(primary_asns) == 1 else ""
        primary_family_name = (
            "ipv4"
            if primary_ip and ipaddress.ip_address(primary_ip).version == 4
            else "ipv6"
        )
        primary_family_network = network["by_family"].get(primary_family_name, {})
        if isinstance(primary_family_network, Mapping):
            for key in (
                "prefix",
                "registered_country",
                "registry",
                "organisation",
                "provider",
                "type",
            ):
                network[key] = network[key] or str(primary_family_network.get(key) or "")
        # Never turn a disputed ASN into an exact ASN-DROP hit.  Unique ASNs
        # from other (for example dual-stack) addresses remain usable.
        asns: set[str] = {
            next(iter(values))
            for values in asns_by_ip.values()
            if len(values) == 1
        }

        dataset_snapshots: list[DatasetSnapshot] = []
        for spec in self.dataset_specs:
            # Static lists are refreshed by ``refresh_static_sources`` on a
            # background worker.  A click only reads validated local snapshots.
            snapshot = self.dataset_updater.load_cached(spec)
            dataset_snapshots.append(snapshot)
            applicable = spec.name != "spamhaus_asndrop" or bool(asns)
            evaluated_target_ips = (
                sorted(
                    address
                    for address, values in asns_by_ip.items()
                    if len(values) == 1
                )
                if spec.name == "spamhaus_asndrop" and applicable
                else sorted(ips)
                if applicable
                else []
            )
            matches = _match_dataset(snapshot, ips, asns) if applicable else []
            if spec.name == "spamhaus_asndrop" and matches:
                for match in matches:
                    matched_asn = _canonical_asn(match.get("value"))
                    target_ips = sorted(
                        address
                        for address, values in asns_by_ip.items()
                        if len(values) == 1 and matched_asn in values
                    )
                    if target_ips:
                        match["target_ips"] = target_ips
                        if len(target_ips) == 1:
                            match["target_ip"] = target_ips[0]
            matched_target_ips = sorted(
                {
                    target
                    for match in matches
                    for value in (
                        [match.get("target_ip")]
                        + (match.get("target_ips") if isinstance(match.get("target_ips"), list) else [])
                    )
                    if (target := _canonical_ip(value))
                }
            )
            source_error = copy.deepcopy(snapshot.error)
            if not applicable:
                asn_is_conflicted = spec.name == "spamhaus_asndrop" and bool(network["asn_conflict"])
                status = "conflict" if asn_is_conflicted else "unknown"
                positive = None
                source_error = {
                    "code": "asn_conflict" if asn_is_conflicted else "asn_unavailable",
                    "message": (
                        "ASN sources conflict, so ASN-DROP was not evaluated"
                        if asn_is_conflicted
                        else "ASN evidence is unavailable, so ASN-DROP was not evaluated"
                    ),
                }
            elif matches:
                status = "positive"
                positive: bool | None = True
            elif snapshot.status == "fresh":
                status = "negative"
                positive = False
            else:
                status = snapshot.status
                positive = None
            evidence.append(
                SourceEvidence(
                    source=spec.name,
                    status=status,
                    observed_at=snapshot.observed_at,
                    expires_at=snapshot.expires_at,
                    positive=positive,
                    data={
                        "matches": matches[:20],
                        "match_count": len(matches),
                        "target_ips": matched_target_ips,
                        "evaluated_ips": evaluated_target_ips,
                        "metadata": copy.deepcopy(snapshot.metadata or {}),
                    },
                    error=source_error,
                    cache_hit=snapshot.cache_hit,
                    stale=snapshot.stale,
                )
            )
            if spec.name.startswith("cloud_") and matches and not snapshot.stale:
                network["cloud_matches"].extend(matches[:20])

        route_after, route_after_error = self._route_snapshot()
        changed = _route_changed(route_before, route_after)
        route_confirmed = bool(route_before.get("confirmed") and route_after.get("confirmed"))
        route = copy.deepcopy(route_after)
        route["stable_during_check"] = not changed
        route["confirmed_before"] = bool(route_before.get("confirmed"))
        route["confirmed_after"] = bool(route_after.get("confirmed"))

        tor_exact = _tri_state(
            item.positive for item in evidence if item.source == "onionoo" and not item.stale
        )
        cloud_signal = _tri_state(
            item.positive for item in evidence if item.source.startswith("cloud_") and not item.stale
        )
        threat_sources = {"spamhaus_drop", "spamhaus_dropv6", "spamhaus_asndrop", "feodo"}
        threat_signal = _tri_state(
            item.positive for item in evidence if item.source in threat_sources and not item.stale
        )
        signals = {
            "threat": threat_signal,
            "tor": tor_exact,
            "cloud": cloud_signal,
        }
        reputation = {
            "conclusion": "insufficient_evidence",
            "confidence": None,
            "conclusion_confidence": None,
            "signals": signals,
            "matched_sources": [
                item.source for item in evidence if item.positive is True and not item.stale
            ],
        }
        coverage_groups = _free_check_groups(
            identity=identity,
            network=network,
            evidence=evidence,
            started_at=started_at,
        )
        reputation["conclusion"] = _conclusion(
            identity_confirmed=bool(identity.get("confirmed")),
            identity_conflict=bool(identity.get("conflict")),
            evidence=evidence,
            reputation=reputation,
            groups=coverage_groups,
            asn_conflict=bool(network["asn_conflict"]),
        )
        if changed or not route_confirmed:
            reputation["conclusion"] = "insufficient_evidence"
        identity_states = {
            address: str(family.get("state") or "unavailable")
            for family_name in ("ipv4", "ipv6")
            if isinstance((family := identity.get(family_name)), Mapping)
            and (address := _canonical_ip(family.get("ip")))
        }
        reputation["conclusion_confidence"] = _conclusion_confidence(
            str(reputation["conclusion"]),
            evidence,
            identity_states=identity_states,
        )
        # ``confidence`` remains the public compatibility field, but is bound
        # only to the local conclusion and exact free-source evidence.
        reputation["confidence"] = reputation["conclusion_confidence"]
        reputation["signals"] = {
            "threat": _public_signal_state(
                signals["threat"],
                (item for item in evidence if item.source in threat_sources),
            ),
            "tor": _public_signal_state(
                signals["tor"],
                (item for item in evidence if item.source == "onionoo"),
            ),
            "cloud": _public_signal_state(
                signals["cloud"],
                (item for item in evidence if item.source.startswith("cloud_")),
            ),
        }

        source_states: dict[str, str] = {}
        incomplete_statuses = {"error", "denied", "rate_limited", "unknown", "conflict", "stale"}
        for item in evidence:
            current = source_states.get(item.source)
            if current == "positive" or (current in incomplete_statuses and item.status != "positive"):
                continue
            if item.status == "positive" or item.status in incomplete_statuses or current is None:
                source_states[item.source] = item.status
        expected_sources = [
            "identity",
            "team_cymru",
            *(["onionoo"] if any(ipaddress.ip_address(ip).version == 4 for ip in ips) else []),
            *(spec.name for spec in self.dataset_specs),
        ]
        fresh_statuses = {"confirmed", "observed", "positive", "negative", "fresh"}
        fresh_sources = sum(1 for source in expected_sources if source_states.get(source) in fresh_statuses)
        coverage = {
            "identity": "conflict" if identity.get("conflict") else "confirmed" if identity.get("confirmed") else "observed" if ips else "unknown",
            "source_states": source_states,
            "fresh_sources": fresh_sources,
            "expected_sources": len(expected_sources),
            "ratio": round(fresh_sources / len(expected_sources), 3) if expected_sources else 0.0,
        }
        checked_groups = sum(
            1
            for group in coverage_groups.values()
            if bool(group.get("applicable")) and bool(group.get("complete"))
        )
        expected_groups = sum(
            1
            for group in coverage_groups.values()
            if bool(group.get("applicable"))
        )
        coverage.update(
            {
                "groups": coverage_groups,
                "checked_groups": checked_groups,
                "expected_groups": expected_groups,
                "group_ratio": (
                    round(checked_groups / expected_groups, 3)
                    if expected_groups
                    else 0.0
                ),
            }
        )
        reputation.update(
            _quality_score(
                groups=coverage_groups,
                conclusion=str(reputation["conclusion"]),
                route_confirmed=route_confirmed,
                route_changed=changed,
                identity_confirmed=bool(identity.get("confirmed")),
                identity_conflict=bool(identity.get("conflict")),
            )
        )

        if changed:
            state = "route_changed"
            reputation["conclusion"] = "insufficient_evidence"
        elif not ips:
            state = "unavailable"
        elif identity.get("conflict"):
            state = "conflict"
        elif (
            route_confirmed
            and bool(identity.get("confirmed"))
            and coverage["group_ratio"] >= 1.0
            and reputation["conclusion"] != "insufficient_evidence"
        ):
            state = "complete"
        else:
            state = "partial"

        expiry_values = [
            _float_timestamp(item.expires_at)
            for item in evidence
            if _float_timestamp(item.expires_at) > started_at
        ]
        expires_at = min(expiry_values) if expiry_values else started_at
        stale_sources = sorted({item.source for item in evidence if item.stale})
        error_sources = sorted(
            {
                item.source
                for item in evidence
                if item.status in {"error", "denied", "rate_limited", "unknown", "conflict"}
            }
        )
        freshness = {
            # ``state`` is the age of this result, not a synonym for source
            # coverage.  Missing/stale providers remain explicit below.
            "state": "stale" if changed else "fresh",
            "status": "invalid" if changed else "partial" if stale_sources or error_sources else "fresh",
            **({"reason": "route_changed"} if changed else {}),
            "cache_hits": sorted({item.source for item in evidence if item.cache_hit}),
            "stale_sources": stale_sources,
            "source_expiries": {item.source: item.expires_at for item in evidence},
        }
        team_evidence = [item for item in evidence if item.source == "team_cymru"]
        if not ips:
            dns_reachability_state = "not_run"
        elif team_evidence and all(item.cache_hit for item in team_evidence):
            dns_reachability_state = "cached"
        elif any(item.status == "confirmed" and not item.cache_hit for item in team_evidence):
            dns_reachability_state = "reachable"
        else:
            dns_reachability_state = "unreachable"
        diagnostics = {
            "identity": copy.deepcopy(identity.get("diagnostics") or {}),
            "identity_error": identity_error,
            "all_identity_failed": not bool(ips),
            "route_before": route_before,
            "route_after": route_after,
            "route_before_error": route_before_error,
            "route_after_error": route_after_error,
            "route_unconfirmed": not route_confirmed,
            "source_errors": error_sources,
            "dns_reachability": {
                "state": dns_reachability_state,
                "source": "team_cymru_doh",
                "elapsed_ms": round(sum(team_request_durations_ms), 1)
                if team_request_durations_ms
                else None,
                "cache_hit": bool(team_evidence) and all(item.cache_hit for item in team_evidence),
            },
            "dns_leak_test": "not_run",
            "elapsed_ms": round(max(0.0, self.clock() - started_at) * 1000.0, 1),
        }
        public_identity = {
            "ipv4": identity.get("ipv4"),
            "ipv6": identity.get("ipv6"),
            "confirmed": bool(identity.get("confirmed")),
            "conflict": bool(identity.get("conflict")),
        }
        result = ExitQualityResultV2(
            schema_version=RESULT_SCHEMA_VERSION,
            request_id=request_id,
            state=state,
            freshness=freshness,
            checked_at=_iso(started_at),
            expires_at=_iso(expires_at),
            route=route,
            identity=public_identity,
            network=network,
            reputation=reputation,
            coverage=coverage,
            evidence=tuple(_public_evidence(item) for item in evidence),
            diagnostics=diagnostics,
        )
        if state != "route_changed":
            try:
                self.result_store.append(result)
            except Exception as exc:
                log_raw_diagnostic(self.logger, "warning", "Failed to persist exit-quality result: %s", exc)
        return result
