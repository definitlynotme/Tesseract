from __future__ import annotations

import asyncio
import html
import re
import time
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from collections.abc import AsyncIterator
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import quote, urlparse

import aiohttp

from config_utils import coerce_bool
DEFAULT_GITHUB_CANDIDATE_PATHS = [
    "Config/vless.txt",
    "Config/vmess.txt",
    "Config/trojan.txt",
    "Config/shadowsocks.txt",
    "Config/hysteria2.txt",
    "Splitted-By-Protocol/vless.txt",
    "Splitted-By-Protocol/vmess.txt",
    "Splitted-By-Protocol/trojan.txt",
    "Splitted-By-Protocol/ss.txt",
    "Splitted-By-Protocol/hy2.txt",
    "Splitted-By-Protocol/tuic.txt",
    "subscriptions/filtered/subs/vless.txt",
    "subscriptions/filtered/subs/vmess.txt",
    "subscriptions/filtered/subs/trojan.txt",
    "subscriptions/filtered/subs/ss.txt",
    "All_Configs_Sub.txt",
    "sub",
    "sub.txt",
    "v2ray.txt",
    "clash.yaml",
    "nodes.txt",
    "Sub1.txt",
    "Sub2.txt",
    "Sub3.txt",
    "Sub4.txt",
    "Sub5.txt",
    "Sub6.txt",
    "Sub7.txt",
    "Sub8.txt",
    "All_Configs_base64_Sub.txt",
    "Base64/Sub1_base64.txt",
    "Base64/Sub2_base64.txt",
    "Base64/Sub3_base64.txt",
    "Base64/Sub4_base64.txt",
    "Base64/Sub5_base64.txt",
    "Base64/Sub6_base64.txt",
    "Base64/Sub7_base64.txt",
    "Base64/Sub8_base64.txt",
]
DEFAULT_SOURCE_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_GITHUB_CANDIDATE_CONCURRENCY = 6

# Delivery endpoints are supplied exclusively by editable source configuration.
GITHUB_API_RAW_ACCEPT = "application/vnd.github.raw"
# Compatibility delay for explicitly configured legacy delivery roads.
GITHUB_DELIVERY_HEDGE_SECONDS = 0.75
_GITHUB_FILE_MISSING_STATUSES = frozenset({404, 410})


@dataclass(frozen=True)
class GitHubDeliveryPath:
    """One road to a GitHub-hosted file."""

    label: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    start_delay_seconds: float = 0.0
    # Whether a missing-file status on this road means the file does not
    # exist. Only the configured original is authoritative: a derived road may
    # resolve an ambiguous branch/path differently, even on GitHub's API.
    authoritative: bool = False


def _github_file_coordinates(url: str) -> tuple[str, str, str, str] | None:
    """Return ``(owner, repo, branch, path)`` for a GitHub file URL."""

    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return None
    host = parsed.netloc.lower()
    parts = [part for part in parsed.path.split("/") if part]
    if host == "raw.githubusercontent.com":
        if len(parts) < 4:
            return None
        owner, repo = parts[0], parts[1]
        if (
            parts[2] == "refs"
            and len(parts) >= 6
            and parts[3] in {"heads", "tags"}
        ):
            branch, path_parts = parts[4], parts[5:]
        else:
            branch, path_parts = parts[2], parts[3:]
    elif host in {"github.com", "www.github.com"}:
        if len(parts) < 5 or parts[2] not in {"raw", "blob"}:
            return None
        owner, repo = parts[0], parts[1]
        if parts[3] == "refs" and len(parts) >= 7 and parts[4] in {"heads", "tags"}:
            branch, path_parts = parts[5], parts[6:]
        else:
            branch, path_parts = parts[3], parts[4:]
    else:
        return None
    if not path_parts or not owner or not repo or not branch:
        return None
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo, branch, "/".join(path_parts)


def github_raw_mirror_urls(
    url: str,
    *,
    mirror_hosts: Iterable[str] = (),
) -> list[str]:
    """Return CDN mirror URLs for a GitHub raw file URL (empty when not applicable)."""

    coordinates = _github_file_coordinates(url)
    if coordinates is None:
        return []
    owner, repo, branch, file_path = coordinates
    return [
        f"{_mirror_base_url(str(mirror))}/gh/{owner}/{repo}@{branch}/{file_path}"
        for mirror in mirror_hosts
        if str(mirror).strip()
    ]


def _mirror_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    return value if "://" in value else f"https://{value}"


def _delivery_endpoint(value: Any, default_delay: float) -> tuple[str, float] | None:
    if isinstance(value, dict):
        if not coerce_bool(value.get("enabled", True), True):
            return None
        return str(value.get("url") or "").strip(), float(
            value.get("delay_seconds", default_delay)
        )
    return str(value).strip(), default_delay


def github_delivery_paths(
    url: str,
    *,
    proxy_prefixes: Iterable[Any] = (),
    mirror_hosts: Iterable[Any] = (),
    hedge_seconds: float = GITHUB_DELIVERY_HEDGE_SECONDS,
    use_api: bool = False,
) -> list[GitHubDeliveryPath]:
    """Every road to a GitHub file, canonical first; empty for other URLs."""

    coordinates = _github_file_coordinates(url)
    if coordinates is None:
        return []
    owner, repo, branch, file_path = coordinates
    raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"
    hedge = max(0.0, float(hedge_seconds))
    paths = [GitHubDeliveryPath("canonical", str(url).strip(), authoritative=True)]
    if use_api:
        # The contents API returns the raw bytes of the current commit from a
        # different host than raw.githubusercontent.com.  Its unauthenticated
        # quota is small, and an exhausted quota answers 403, which is not a
        # verdict on the file.
        paths.append(
            GitHubDeliveryPath(
                "github_api",
                f"https://api.github.com/repos/{owner}/{repo}/contents/"
                f"{quote(file_path)}?ref={quote(branch)}",
                headers=(("Accept", GITHUB_API_RAW_ACCEPT),),
                authoritative=False,
            )
        )
    for index, item in enumerate(proxy_prefixes):
        endpoint = _delivery_endpoint(item, 0.0 if index == 0 else hedge)
        if endpoint is None or not endpoint[0]:
            continue
        prefix, delay = endpoint
        paths.append(
            GitHubDeliveryPath(
                f"proxy:{urlparse(str(prefix).strip()).netloc}",
                f"{str(prefix).strip()}{raw}",
                start_delay_seconds=delay,
            )
        )
    for item in mirror_hosts:
        endpoint = _delivery_endpoint(item, hedge)
        if endpoint is None or not endpoint[0]:
            continue
        base, delay = endpoint
        host = urlparse(_mirror_base_url(base)).netloc
        mirror_url = github_raw_mirror_urls(url, mirror_hosts=(base,))[0]
        paths.append(
            GitHubDeliveryPath(
                f"mirror:{host}",
                mirror_url,
                start_delay_seconds=delay,
            )
        )
    return paths


def normalize_github_delivery(value: Any) -> dict[str, Any]:
    """Resolve explicit delivery settings without injecting remote endpoints.

    Configured under ``github_delivery`` in sources.yaml.  An explicit empty
    list removes that kind of road; ``api: false`` drops the contents API.
    """

    config = value if isinstance(value, dict) else {}

    def entries(key: str) -> tuple[Any, ...]:
        raw = config.get(key)
        if not isinstance(raw, (list, tuple)):
            return ()
        return tuple(dict(item) if isinstance(item, dict) else str(item).strip()
                     for item in raw if isinstance(item, dict) or str(item).strip())

    try:
        hedge = float(config.get("hedge_seconds", GITHUB_DELIVERY_HEDGE_SECONDS))
    except (TypeError, ValueError):
        hedge = GITHUB_DELIVERY_HEDGE_SECONDS
    return {
        "api": coerce_bool(config.get("api", False), False),
        "proxies": entries("proxies"),
        "mirrors": entries("mirrors"),
        "hedge_seconds": max(0.0, min(5.0, hedge)),
    }


def legacy_fetch_paths(delivery: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert explicit legacy settings, without supplying any remote default."""
    normalized = normalize_github_delivery(delivery)
    paths = []
    if normalized['api']:
        paths.append({'kind': 'github_api', 'enabled': True, 'delay_seconds': 0.0})
    for key, kind in (('proxies', 'github_proxy'), ('mirrors', 'github_cdn')):
        for index, value in enumerate(normalized[key]):
            default_delay = 0.0 if key == 'proxies' and index == 0 else normalized['hedge_seconds']
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault('delay_seconds', default_delay)
            else:
                item = {'url': value, 'enabled': True, 'delay_seconds': default_delay}
            if kind == 'github_cdn':
                item['url'] = _mirror_base_url(str(item.get('url') or ''))
            paths.append({'kind': kind, **item})
    return paths


def source_fetch_paths(entry: dict[str, Any], profiles: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if 'fetch_paths' in entry:
        paths = entry['fetch_paths']
    elif 'fetch_profile' in entry:
        profile = str(entry['fetch_profile'] or '')
        if not profile:
            return []
        if profile not in (profiles or {}):
            raise ValueError('source references an undefined fetch profile')
        paths = profiles[profile]
    elif isinstance(entry.get('github_delivery'), dict):
        return legacy_fetch_paths(entry['github_delivery'])
    else:
        return []
    if not isinstance(paths, list) or any(not isinstance(item, dict) for item in paths):
        raise ValueError('source fetch paths must be a list of mappings')
    return [dict(item) for item in paths]


def configured_delivery_paths(url: str, paths: Iterable[dict[str, Any]]) -> list[GitHubDeliveryPath]:
    result = [GitHubDeliveryPath('canonical', url, authoritative=True)]
    for index, item in enumerate(paths):
        if not coerce_bool(item.get('enabled', True), True):
            continue
        unit = str(item.get('unit') or '')
        execution = _SOURCE_UNIT_EXECUTION.get()
        if unit and (execution is None or execution.unit_id != unit):
            continue
        kind = str(item.get('kind') or '')
        delay = float(item.get('delay_seconds', 0.0))
        if kind == 'url':
            result.append(GitHubDeliveryPath(f'configured:{index}', str(item['url']),
                                             start_delay_seconds=delay))
            continue
        derived = github_delivery_paths(
            url, use_api=kind == 'github_api',
            proxy_prefixes=[item] if kind == 'github_proxy' else (),
            mirror_hosts=[item] if kind == 'github_cdn' else (),
        )
        for path in derived[1:]:
            label = path.label
            if any(existing.label == label for existing in result):
                label = f'{label}:{index}'
            result.append(replace(path, label=label, start_delay_seconds=delay))
    return result


def _unit_delivery_timeout_seconds(
    unit_deadline_seconds: float,
    delivery_slack_seconds: float,
) -> float:
    """Return the transport entitlement plus its bounded delivery grace."""

    deadline = max(0.001, float(unit_deadline_seconds))
    delivery_slack = max(0.0, min(1.0, float(delivery_slack_seconds)))
    return deadline + delivery_slack

def infer_source_type(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("@"):
        return "telegram_channel"
    parsed = urlparse(text)
    netloc = parsed.netloc.lower()
    if netloc in {"t.me", "telegram.me", "www.t.me"}:
        return "telegram_channel"
    if netloc in {"github.com", "www.github.com"}:
        if _github_file_coordinates(text) is not None:
            return "subscription_url"
        return "github_repo"
    return "subscription_url"


@dataclass(frozen=True)
class SourceFetchResult:
    source_id: str
    source_type: str
    value: str
    origin: str
    ok: bool
    content: str = ""
    error: str = ""
    egress_mode: str = ""
    fallback_attempted: bool = False
    fallback_error: str = ""
    deadline_actual_ms: int = 0
    first_byte_elapsed_ms: int = 0
    transport_elapsed_ms: int = 0
    # Which road delivered the body (see ``github_delivery_paths``); empty
    # for sources with a single request path.
    delivery_path: str = ""
    # What every road did, e.g. ``canonical=ConnectionError github_api=ok``;
    # kept out of ``error`` because failures are classified from that text.
    delivery_outcomes: str = ""


@dataclass(frozen=True)
class SourceUnitResult:
    root_id: str
    root_type: str
    root_value: str
    origin: str
    unit_id: str
    unit_value: str
    unit_family: str
    ok: bool
    content: str = ""
    error: str = ""
    egress_mode: str = ""
    fallback_attempted: bool = False
    fallback_error: str = ""
    deadline_actual_ms: int = 0
    transport_elapsed_ms: int = 0
    queued_elapsed_ms: int = 0
    transport_started_elapsed_ms: int = 0
    first_byte_elapsed_ms: int = 0
    launch_tranche: str = "primary"
    transport_started: bool = True
    delivery_path: str = ""
    delivery_outcomes: str = ""
    _parse_valid_observer: Callable[[bool], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def acknowledge_parse_valid(self, parse_valid: bool) -> None:
        observer = self._parse_valid_observer
        if observer is not None:
            observer(bool(parse_valid))


@dataclass
class _SourceUnitExecution:
    root: Any
    unit_id: str
    unit_value: str
    unit_family: str
    route: str
    acquire: Callable[[str, str], Any] | None
    observer: Callable[[str, str, str, float], Any] | None
    validate: Callable[[SourceUnitResult], Awaitable[bool]] | None
    timer: asyncio.Timeout
    budget: float
    started_at: float | None = None
    transport_requested: bool = False
    parse_seconds: float = 0.0
    delivery_path: str = ""
    outcomes: dict[str, str] = field(default_factory=dict)
    first_byte_elapsed_ms: int = 0

    def transport_started(self) -> None:
        if self.started_at is None:
            self.started_at = asyncio.get_running_loop().time()
            self.timer.reschedule(self.started_at + self.budget)
            if self.observer is not None:
                self.observer(self.root.source_id, self.unit_id, self.route, time.monotonic())

    async def parse_valid(self, content: str) -> bool:
        if self.validate is None:
            return bool(content)
        loop = asyncio.get_running_loop()
        deadline = self.timer.when()
        self.timer.reschedule(None)
        started = loop.time()
        try:
            return await self.validate(SourceUnitResult(
                self.root.source_id, self.root.source_type, self.root.value,
                self.root.origin, self.unit_id, self.unit_value, self.unit_family,
                True, content=content,
            ))
        finally:
            elapsed = loop.time() - started
            self.parse_seconds += elapsed
            if not self.timer.expired():
                self.timer.reschedule(None if deadline is None else deadline + elapsed)


# Source instances may be shared by concurrent listener lanes. Execution
# ownership must follow the coroutine, not mutable attributes on the source.
_SOURCE_UNIT_EXECUTION: ContextVar[_SourceUnitExecution | None] = ContextVar(
    "tier3_source_unit_execution", default=None,
)


@asynccontextmanager
async def _source_unit_execution(root, unit_id, unit_value, family, route,
                                 acquire, observer, validate, budget):
    execution = None
    try:
        async with asyncio.timeout(budget) as timer:
            execution = _SourceUnitExecution(root, unit_id, unit_value, family,
                                             route, acquire, observer, validate, timer, budget)
            token = _SOURCE_UNIT_EXECUTION.set(execution)
            try:
                yield execution
            finally:
                _SOURCE_UNIT_EXECUTION.reset(token)
    except TimeoutError:
        if (execution is not None and execution.timer.expired()
                and execution.transport_requested and execution.started_at is None):
            raise SourceDispatchDeferred('source transport capacity pending') from None
        raise


@dataclass
class _DeliveryResponse:
    response: Any
    lease: Any
    closed: bool = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.response, name)


async def _release_delivery_response(response: Any) -> None:
    if not isinstance(response, _DeliveryResponse):
        response.release()
        return
    if response.closed:
        return
    response.closed = True
    try:
        response.response.release()
    finally:
        if response.lease is not None:
            await response.lease.__aexit__(None, None, None)


@dataclass(frozen=True)
class CollectionReport:
    source_count: int = 0
    configured_source_count: int = 0
    fetched_sources: int = 0
    failed_sources: int = 0
    node_count: int = 0
    sources: list[dict[str, Any]] = field(default_factory=list)
    discovered_source_units: int = 0
    fetched_source_units: int = 0
    failed_source_units: int = 0
    pending_source_units: int = 0
    retry_due_source_units: int = 0

    @property
    def any_failure(self) -> bool:
        return self.failed_sources > 0

    @property
    def all_failed(self) -> bool:
        return self.source_count > 0 and self.fetched_sources == 0 and self.failed_sources >= self.source_count

    def to_dict(self) -> dict[str, Any]:
        effective_source_count = max(int(self.source_count or 0), int(self.configured_source_count or 0))
        if self.sources:
            source_fetch_ok = sum(1 for source in self.sources if source.get("ok"))
            source_fetch_pending_reported = sum(
                1
                for source in self.sources
                if not source.get("ok")
                and (
                    source.get("pending")
                    or str(source.get("status") or "").strip().lower() == "pending"
                )
            )
            source_fetch_failed = sum(
                1
                for source in self.sources
                if not source.get("ok")
                and not source.get("pending")
                and str(source.get("status") or "").strip().lower() != "pending"
            )
        else:
            source_fetch_ok = self.fetched_sources
            source_fetch_pending_reported = 0
            source_fetch_failed = self.failed_sources
        source_fetch_pending = max(
            source_fetch_pending_reported,
            max(0, effective_source_count - source_fetch_ok - source_fetch_failed),
        )
        runtime_proxy_retry_count = sum(1 for source in self.sources if source.get("fallback_attempted"))
        runtime_proxy_retry_success_count = sum(
            1
            for source in self.sources
            if source.get("fallback_attempted") and source.get("ok")
        )
        retryable_failed_sources = sum(
            1
            for source in self.sources
            if not source.get("ok")
            and not source.get("pending")
            and str(source.get("status") or "").strip().lower() != "pending"
            and source.get("retryable")
        )
        retryable_pending_sources = sum(
            1
            for source in self.sources
            if not source.get("ok")
            and (
                source.get("pending")
                or str(source.get("status") or "").strip().lower() == "pending"
            )
            and source.get("retryable")
        )
        source_fetch_egress_mode = "direct"
        source_modes = {
            str(source.get("egress_mode") or "").strip()
            for source in self.sources
            if str(source.get("egress_mode") or "").strip()
        }
        if "runtime_proxy" in source_modes:
            source_fetch_egress_mode = "runtime_proxy"
        elif "configured_proxy" in source_modes:
            source_fetch_egress_mode = "configured_proxy"
        elif "blocked_no_route" in source_modes:
            source_fetch_egress_mode = "blocked_no_route"
        units = [
            unit
            for source in self.sources
            for unit in (source.get("units") or ())
            if isinstance(unit, dict)
        ]
        unit_transport_started = sum(
            1 for unit in units if bool(unit.get("transport_started"))
        )
        unit_parse_valid = sum(
            1 for unit in units if bool(unit.get("parse_valid"))
        )
        unit_observer_handoff = sum(
            1 for unit in units if bool(unit.get("observer_handoff"))
        )
        unit_launch_debt = sum(
            1
            for unit in units
            if bool(unit.get("pending"))
            and not bool(unit.get("transport_started"))
        )
        return {
            "source_count": effective_source_count,
            "configured_source_count": effective_source_count,
            "collection_source_count": self.source_count,
            "fetched_sources": self.fetched_sources,
            "failed_sources": self.failed_sources,
            "node_count": self.node_count,
            "sources": list(self.sources),
            "source_fetch_ok": source_fetch_ok,
            "source_fetch_failed": source_fetch_failed,
            "source_fetch_pending": source_fetch_pending,
            "source_fetch_egress_mode": source_fetch_egress_mode,
            "runtime_proxy_retry_count": runtime_proxy_retry_count,
            "runtime_proxy_retry_success_count": runtime_proxy_retry_success_count,
            "retryable_failed_sources": retryable_failed_sources,
            "retryable_pending_sources": retryable_pending_sources,
            "source_retry_due_count": retryable_failed_sources + retryable_pending_sources,
            "discovered_source_units": int(self.discovered_source_units or 0),
            "fetched_source_units": int(self.fetched_source_units or 0),
            "failed_source_units": int(self.failed_source_units or 0),
            "pending_source_units": int(self.pending_source_units or 0),
            "retry_due_source_units": int(self.retry_due_source_units or 0),
            "source_unit_queued_count": unit_launch_debt,
            "source_unit_transport_started_count": unit_transport_started,
            "source_unit_parse_valid_count": unit_parse_valid,
            "source_unit_observer_handoff_count": unit_observer_handoff,
            "source_unit_launch_debt": unit_launch_debt,
            "source_unit_primary_started_count": sum(
                1
                for unit in units
                if bool(unit.get("transport_started"))
                and str(unit.get("launch_tranche") or "") == "primary"
            ),
            "source_unit_hedge_started_count": sum(
                1
                for unit in units
                if bool(unit.get("transport_started"))
                and str(unit.get("launch_tranche") or "") == "hedge"
            ),
        }


class SourceDispatchDeferred(Exception):
    """A source route lease expired before transport, not a network failure."""


class Tier3Source(ABC):
    source_type = "source"

    def __init__(
        self,
        value: str,
        *,
        enabled: bool = True,
        headers: dict[str, str] | None = None,
        tags: list[str] | None = None,
        origin: str = "tier3b",
        candidate_paths: list[str] | None = None,
        mirror_fallback: bool = True,
        github_delivery: dict[str, Any] | None = None,
        fetch_paths: list[dict[str, Any]] | None = None,
    ) -> None:
        self.value = str(value).strip()
        self.enabled = coerce_bool(enabled, True)
        self.headers = dict(headers or {})
        self.tags = list(tags or [])
        self.origin = str(origin or "tier3b")
        self.candidate_paths = list(DEFAULT_GITHUB_CANDIDATE_PATHS if candidate_paths is None else candidate_paths)
        self.mirror_fallback = coerce_bool(mirror_fallback, True)
        self.github_delivery = normalize_github_delivery(github_delivery)
        self.fetch_paths = [dict(item) for item in fetch_paths] if fetch_paths is not None else None
        self._last_delivery_path = ""
        self._last_road_outcomes: dict[str, str] = {}

    def delivery_outcomes(self) -> str:
        """What every road of the last fetch did, in start order."""
        execution = _SOURCE_UNIT_EXECUTION.get()
        outcomes = execution.outcomes if execution else getattr(self, "_last_road_outcomes", {})
        return " ".join(
            f"{label}={outcome}"
            for label, outcome in outcomes.items()
        )

    @property
    def source_id(self) -> str:
        return f"{self.source_type}:{self.value}"

    def metadata(self) -> dict[str, Any]:
        return {
            "id": self.source_id,
            "type": self.source_type,
            "value": self.value,
            "origin": self.origin,
            "tags": list(self.tags),
        }

    async def fetch(
        self,
        session: aiohttp.ClientSession,
        *,
        timeout_seconds: int,
        retries: int,
        user_agent: str,
        proxy_url: str | None,
        max_bytes: int = DEFAULT_SOURCE_MAX_BYTES,
    ) -> SourceFetchResult:
        started = asyncio.get_running_loop().time()
        execution = _SOURCE_UNIT_EXECUTION.get()
        self._last_fetch_first_byte_elapsed_ms = 0
        self._last_delivery_path = ""
        self._last_road_outcomes = {}
        if not self.enabled:
            return SourceFetchResult(self.source_id, self.source_type, self.value, self.origin, ok=False, error="disabled")
        try:
            content = await self._fetch(
                session,
                timeout_seconds=timeout_seconds,
                retries=retries,
                user_agent=user_agent,
                proxy_url=proxy_url,
                max_bytes=max_bytes,
            )
        except SourceDispatchDeferred:
            raise
        except Exception as exc:
            elapsed_ms = int(
                round((asyncio.get_running_loop().time() - started) * 1000)
            )
            return SourceFetchResult(
                self.source_id,
                self.source_type,
                self.value,
                self.origin,
                ok=False,
                error=str(exc),
                first_byte_elapsed_ms=max(
                    0,
                    int(execution.first_byte_elapsed_ms if execution else getattr(self, "_last_fetch_first_byte_elapsed_ms", 0)),
                ),
                transport_elapsed_ms=max(0, elapsed_ms - int(1000 * (execution.parse_seconds if execution else 0))),
                delivery_outcomes=self.delivery_outcomes(),
            )
        if not content:
            return SourceFetchResult(self.source_id, self.source_type, self.value, self.origin, ok=False, error="empty")
        elapsed_ms = int(
            round((asyncio.get_running_loop().time() - started) * 1000)
        )
        return SourceFetchResult(
            self.source_id,
            self.source_type,
            self.value,
            self.origin,
            ok=True,
            content=content,
            first_byte_elapsed_ms=max(
                0,
                int(execution.first_byte_elapsed_ms if execution else getattr(self, "_last_fetch_first_byte_elapsed_ms", 0)),
            ),
            transport_elapsed_ms=max(0, elapsed_ms - int(1000 * (execution.parse_seconds if execution else 0))),
            delivery_path=execution.delivery_path if execution else str(getattr(self, "_last_delivery_path", "") or ""),
            delivery_outcomes=self.delivery_outcomes(),
        )

    async def fetch_units(
        self,
        session: aiohttp.ClientSession,
        *,
        timeout_seconds: int,
        retries: int,
        user_agent: str,
        proxy_url: str | None,
        max_bytes: int = DEFAULT_SOURCE_MAX_BYTES,
        unit_budget: int = 1,
        unit_concurrency: int = 1,
        unit_deadline_seconds: float = 8.0,
        cancel_slack_seconds: float = 0.25,
        delivery_slack_seconds: float = 0.25,
        unit_transport_acquire: Callable[[str, str], Any] | None = None,
        unit_transport_started_observer: (
            Callable[[str, str, str, float], Any] | None
        ) = None,
        unit_route_key: str = "default",
        unit_hedge_delay_seconds: float = 0.25,
        unit_content_validator: Callable[[SourceUnitResult], Awaitable[bool]] | None = None,
    ) -> AsyncIterator[SourceUnitResult]:
        del (
            unit_budget,
            unit_concurrency,
            cancel_slack_seconds,
            unit_hedge_delay_seconds,
        )
        delivery_timeout = _unit_delivery_timeout_seconds(
            unit_deadline_seconds,
            delivery_slack_seconds,
        )
        started = asyncio.get_running_loop().time()
        execution = None
        try:
            async with _source_unit_execution(
                self, "default", self.value, "default", unit_route_key,
                unit_transport_acquire, unit_transport_started_observer,
                unit_content_validator, delivery_timeout,
            ) as execution:
                result = await self.fetch(
                    session,
                    timeout_seconds=timeout_seconds,
                    retries=retries,
                    user_agent=user_agent,
                    proxy_url=proxy_url,
                    max_bytes=max_bytes,
                )
        except asyncio.TimeoutError:
            result = SourceFetchResult(
                self.source_id,
                self.source_type,
                self.value,
                self.origin,
                ok=False,
                error=f"source unit timeout after {float(unit_deadline_seconds):.2f}s",
                delivery_outcomes=self.delivery_outcomes(),
            )
        completed_at = asyncio.get_running_loop().time()
        elapsed_ms = int(round((completed_at - started) * 1000))
        transport_started_at = execution.started_at if execution else None
        transport_ms = (
            max(0, int(round((completed_at - transport_started_at - execution.parse_seconds) * 1000)))
            if transport_started_at is not None else 0
        )
        queued_ms = max(0, int(round(((transport_started_at or completed_at) - started) * 1000)))
        yield SourceUnitResult(
            root_id=self.source_id,
            root_type=self.source_type,
            root_value=self.value,
            origin=self.origin,
            unit_id="default",
            unit_value=self.value,
            unit_family="default",
            ok=result.ok,
            content=result.content,
            error=result.error,
            egress_mode=result.egress_mode,
            fallback_attempted=result.fallback_attempted,
            fallback_error=result.fallback_error,
            deadline_actual_ms=elapsed_ms,
            transport_elapsed_ms=transport_ms,
            queued_elapsed_ms=queued_ms,
            transport_started_elapsed_ms=queued_ms if transport_started_at is not None else 0,
            first_byte_elapsed_ms=result.first_byte_elapsed_ms,
            transport_started=bool(execution and execution.started_at is not None),
            delivery_path=result.delivery_path,
            delivery_outcomes=result.delivery_outcomes,
        )

    @abstractmethod
    async def _fetch(
        self,
        session: aiohttp.ClientSession,
        *,
        timeout_seconds: int,
        retries: int,
        user_agent: str,
        proxy_url: str | None,
        max_bytes: int,
    ) -> str | None:
        raise NotImplementedError

    def discover(self) -> list["Tier3Source"]:
        return []

    def unit_descriptors(self, *, unit_budget: int = 1) -> list[dict[str, str]]:
        del unit_budget
        return [
            {
                "id": "default",
                "value": self.value,
                "family": "default",
            }
        ]


class RemoteTextSource(Tier3Source):
    source_type = "subscription_url"

    def delivery_paths(self) -> list[GitHubDeliveryPath]:
        """Every road to this source's file; empty when it has only one."""

        parsed = urlparse(self.value)
        if (not self.mirror_fallback or self.headers or parsed.query
                or parsed.username or parsed.password):
            return []
        if self.fetch_paths is not None:
            return configured_delivery_paths(self.value, self.fetch_paths)
        delivery = self.github_delivery
        return github_delivery_paths(
            self.value,
            proxy_prefixes=delivery["proxies"],
            mirror_hosts=delivery["mirrors"],
            hedge_seconds=delivery["hedge_seconds"],
            use_api=delivery["api"],
        )

    def mirror_urls(self) -> list[str]:
        """URLs of every road besides the canonical one."""

        return [path.url for path in self.delivery_paths()[1:]]

    async def _fetch(
        self,
        session: aiohttp.ClientSession,
        *,
        timeout_seconds: int,
        retries: int,
        user_agent: str,
        proxy_url: str | None,
        max_bytes: int,
    ) -> str | None:
        # Only encodings every build can decode: a server may otherwise answer
        # with brotli whenever the client merely has the module installed.
        headers = {
            "User-Agent": user_agent,
            "Accept-Encoding": "gzip, deflate",
            **self.headers,
        }
        paths = self.delivery_paths()
        if len(paths) <= 1:
            return await self._fetch_url(
                self.value,
                session,
                headers=headers,
                timeout_seconds=timeout_seconds,
                retries=retries,
                proxy_url=proxy_url,
                max_bytes=max_bytes,
            )
        return await self._fetch_delivery_race(
            paths,
            session,
            headers=headers,
            timeout_seconds=timeout_seconds,
            retries=retries,
            proxy_url=proxy_url,
            max_bytes=max_bytes,
        )

    async def _fetch_delivery_race(
        self,
        paths: list[GitHubDeliveryPath],
        session: aiohttp.ClientSession,
        *,
        headers: dict[str, str],
        timeout_seconds: int,
        retries: int,
        proxy_url: str | None,
        max_bytes: int,
    ) -> str | None:
        """Race every road for the first answer, then read the file from it alone.

        Roads race only until one answers successfully.  Downloading a list
        over every road at once splits the link between copies of the same
        bytes, and on a slow network none of them finishes (measured on a
        campus WLAN: 26 parallel copies, none complete within the unit
        deadline).  The winner streams the body with the whole link; if that
        body fails, the race resumes over the roads that have not failed.
        """

        outcomes = self._last_road_outcomes = {}
        execution = _SOURCE_UNIT_EXECUTION.get()
        if execution is not None:
            execution.outcomes = outcomes
        failed: set[str] = set()
        last_error: BaseException | None = None
        while True:
            remaining = [path for path in paths if path.label not in failed]
            if not remaining:
                raise last_error or RuntimeError("no road delivered the file")
            path, response = await self._first_road_response(
                remaining,
                session,
                outcomes=outcomes,
                failed=failed,
                headers=headers,
                timeout_seconds=timeout_seconds,
                retries=retries,
                proxy_url=proxy_url,
            )
            try:
                content = await self._read_body(response, max_bytes)
            except asyncio.CancelledError:
                outcomes[path.label] = "body_cancelled"
                raise
            except Exception as exc:
                outcomes[path.label] = f"body:{type(exc).__name__}"
                failed.add(path.label)
                last_error = exc
                continue
            execution = _SOURCE_UNIT_EXECUTION.get()
            if content and (execution is None or await execution.parse_valid(content)):
                outcomes[path.label] = "ok"
                self._last_delivery_path = path.label
                if execution is not None:
                    execution.delivery_path = path.label
                return content
            outcomes[path.label] = "parse_invalid" if content else "empty"
            failed.add(path.label)
            last_error = RuntimeError("source body contains no valid candidates" if content else "empty")

    async def _first_road_response(
        self,
        paths: list[GitHubDeliveryPath],
        session: aiohttp.ClientSession,
        *,
        outcomes: dict[str, str],
        failed: set[str],
        headers: dict[str, str],
        timeout_seconds: int,
        retries: int,
        proxy_url: str | None,
    ) -> tuple[GitHubDeliveryPath, Any]:
        """Return the first road that answers successfully, body unread.

        Roads start at their own head start, except that when every started
        road has already failed the next ones start at once instead of
        sitting out the remainder.  A missing-file answer from GitHub itself
        ends the race, because no mirror can serve a file that is not there.
        Mirror status is never an authoritative verdict. The canonical road keeps
        the configured retry policy; every other road is itself the retry.
        Losers are cancelled or released.
        """

        loop = asyncio.get_running_loop()
        started = loop.time()
        waiting = list(paths)
        running: dict[asyncio.Task[Any], GitHubDeliveryPath] = {}
        canonical_error: BaseException | None = None
        other_error: BaseException | None = None
        winner: tuple[GitHubDeliveryPath, Any] | None = None

        def launch(path: GitHubDeliveryPath) -> None:
            outcomes[path.label] = "started"
            task = loop.create_task(
                self._open_url(
                    path.url,
                    session,
                    headers={
                        **(headers if path.label == "canonical" else {
                            key: value for key, value in headers.items()
                            if key.lower() in {"user-agent", "accept-encoding"}
                        }),
                        **dict(path.headers),
                    },
                    timeout_seconds=timeout_seconds,
                    retries=retries if path.label == "canonical" else 0,
                    proxy_url=proxy_url,
                    # A proxy or CDN that cannot serve the file may answer
                    # 200 with its own error page; GitHub never does.
                    reject_html=not path.authoritative,
                )
            )
            running[task] = path

        try:
            while waiting or running:
                elapsed = loop.time() - started
                due = [path for path in waiting if path.start_delay_seconds <= elapsed]
                if not due and not running and waiting:
                    soonest = min(path.start_delay_seconds for path in waiting)
                    due = [path for path in waiting if path.start_delay_seconds == soonest]
                for path in due:
                    waiting.remove(path)
                    launch(path)
                next_start = min(
                    (path.start_delay_seconds for path in waiting),
                    default=None,
                )
                wait_timeout = (
                    None
                    if next_start is None
                    else max(0.0, next_start - (loop.time() - started))
                )
                done, _pending = await asyncio.wait(
                    set(running),
                    timeout=wait_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                missing: BaseException | None = None
                # Simultaneous headers preserve the configured road order.
                for task in sorted(done, key=lambda item: paths.index(running[item])):
                    path = running.pop(task)
                    error = task.exception()
                    if error is None:
                        response = task.result()
                        if winner is None:
                            winner = (path, response)
                            outcomes[path.label] = "answered"
                            self._last_fetch_first_byte_elapsed_ms = int(
                                round((loop.time() - started) * 1000)
                            )
                            execution = _SOURCE_UNIT_EXECUTION.get()
                            if execution is not None:
                                execution.first_byte_elapsed_ms = max(0, int(round((
                                    loop.time() - (execution.started_at or started) - execution.parse_seconds
                                ) * 1000)))
                        else:
                            await _release_delivery_response(response)
                        continue
                    failed.add(path.label)
                    status = getattr(error, "status", None)
                    outcomes[path.label] = (
                        f"http:{status}"
                        if isinstance(status, int) and not isinstance(status, bool)
                        else type(error).__name__
                    )
                    if path.label == "canonical":
                        canonical_error = error
                    elif other_error is None:
                        other_error = error
                    if _github_file_missing(error):
                        if path.authoritative:
                            missing = error
                if missing is not None:
                    if winner is not None:
                        await _release_delivery_response(winner[1])
                        winner = None
                    raise missing
                if winner is not None:
                    return winner
            raise canonical_error or other_error or RuntimeError("no road answered")
        finally:
            for task in running:
                if not task.done():
                    task.cancel()
            try:
                await asyncio.gather(*running, return_exceptions=True)
            except BaseException:
                # A cancellation during loser cleanup prevents the caller from
                # receiving the selected response and its transport lease.
                if winner is not None:
                    await _release_delivery_response(winner[1])
                raise
            finally:
                for task in running:
                    if task.done() and not task.cancelled() and task.exception() is None:
                        response = task.result()
                        if winner is None or response is not winner[1]:
                            await _release_delivery_response(response)

    async def _open_url(
        self,
        url: str,
        session: aiohttp.ClientSession,
        *,
        headers: dict[str, str],
        timeout_seconds: int,
        retries: int,
        proxy_url: str | None,
        reject_html: bool = False,
    ) -> Any:
        """Send the request; return the successful response with its body unread."""

        last_error: Exception | None = None
        for attempt in range(1, retries + 2):
            lease = None
            try:
                execution = _SOURCE_UNIT_EXECUTION.get()
                if execution is not None:
                    execution.transport_requested = True
                    if execution.acquire is not None:
                        pending_lease = execution.acquire(execution.root.source_id, execution.route)
                        await pending_lease.__aenter__()
                        lease = pending_lease
                    execution.transport_started()
                response = await session.get(
                    url,
                    headers=headers,
                    timeout=timeout_seconds,
                    proxy=proxy_url,
                    **({"allow_redirects": False} if (
                        self.headers or urlparse(self.value).query
                        or urlparse(self.value).username
                    ) else {}),
                )
                try:
                    response.raise_for_status()
                    if 300 <= response.status < 400:
                        raise ValueError("source redirect requires an explicit destination")
                    content_type = str(response.headers.get("Content-Type") or "").lower()
                    if reject_html and "html" in content_type:
                        raise ValueError("road answered with an HTML page, not a list")
                except BaseException:
                    response.release()
                    raise
                wrapped = _DeliveryResponse(response, lease)
                lease = None
                return wrapped
            except asyncio.CancelledError:
                raise
            except SourceDispatchDeferred:
                raise
            except Exception as exc:
                last_error = exc
                if lease is not None:
                    await lease.__aexit__(None, None, None)
                    lease = None
                if attempt > retries or _github_file_missing(exc):
                    raise
                await asyncio.sleep(min(attempt, 3))
            finally:
                if lease is not None:
                    await lease.__aexit__(None, None, None)
        raise last_error or RuntimeError("no response")

    @staticmethod
    async def _read_body(response: Any, max_bytes: int) -> str:
        """Read a response body under the byte cap, then release it."""

        try:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    expected_bytes = int(content_length)
                except ValueError:
                    expected_bytes = 0
                if expected_bytes > max_bytes:
                    raise ValueError(f"source exceeds {max_bytes} byte read cap")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"source exceeds {max_bytes} byte read cap")
                chunks.append(chunk)
            charset = response.charset or "utf-8"
            return b"".join(chunks).decode(charset, errors="replace")
        finally:
            await _release_delivery_response(response)

    async def _fetch_url(
        self,
        url: str,
        session: aiohttp.ClientSession,
        *,
        headers: dict[str, str],
        timeout_seconds: int,
        retries: int,
        proxy_url: str | None,
        max_bytes: int,
    ) -> str | None:
        started = asyncio.get_running_loop().time()
        response = await self._open_url(
            url, session, headers=headers, timeout_seconds=timeout_seconds,
            retries=retries, proxy_url=proxy_url,
        )
        self._last_fetch_first_byte_elapsed_ms = int(
            round((asyncio.get_running_loop().time() - started) * 1000)
        )
        execution = _SOURCE_UNIT_EXECUTION.get()
        if execution is not None:
            execution.first_byte_elapsed_ms = self._last_fetch_first_byte_elapsed_ms
        return await self._read_body(response, max_bytes)


def _github_file_missing(error: BaseException | None) -> bool:
    """Whether an HTTP error says the file itself does not exist."""

    status = getattr(error, "status", None)
    if isinstance(status, bool) or not isinstance(status, int):
        return False
    return status in _GITHUB_FILE_MISSING_STATUSES


class GitHubRepositorySource(RemoteTextSource):
    source_type = "github_repo"

    def _repo_parts(self) -> tuple[str, str | None] | None:
        parsed = urlparse(self.value)
        if parsed.netloc in {"raw.githubusercontent.com", "gist.githubusercontent.com"}:
            return None
        if parsed.netloc not in {"github.com", "www.github.com"}:
            return None
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) < 2:
            return None
        repo = f"{parts[0]}/{parts[1]}"
        branch = None
        if len(parts) >= 5 and parts[2] in {"blob", "raw"}:
            branch = parts[3]
        return repo, branch

    def discover(self) -> list[Tier3Source]:
        repo_parts = self._repo_parts()
        if not repo_parts:
            return []
        repo, branch = repo_parts
        branch = branch or "main"
        return [
            RemoteTextSource(
                raw_github_url(repo, branch, path),
                enabled=self.enabled,
                headers=self.headers,
                tags=[*self.tags, "github-candidate"],
                origin=self.origin,
                mirror_fallback=self.mirror_fallback,
                github_delivery=self.github_delivery,
                fetch_paths=self.fetch_paths,
            )
            for path in self.candidate_paths
        ]

    @staticmethod
    def _candidate_unit_family(path: str) -> str:
        lowered = str(path or "").replace("\\", "/").lower()
        name = lowered.rsplit("/", 1)[-1]
        if "vless" in name:
            return "vless"
        if "vmess" in name:
            return "vmess"
        if "trojan" in name:
            return "trojan"
        if name in {"ss.txt", "shadowsocks.txt"} or "shadowsocks" in name:
            return "ss"
        if any(marker in name for marker in ("hysteria", "hy2", "tuic")):
            return "modern"
        if any(marker in lowered for marker in ("all_config", "base64", "sub", "nodes", "clash")):
            return "aggregate"
        return "unknown"

    def _candidate_unit_portfolio(self, unit_budget: int) -> list[tuple[str, Tier3Source, str]]:
        candidates = self.discover()
        path_candidates = list(zip(self.candidate_paths, candidates))
        family_order: list[str] = []
        buckets: dict[str, list[tuple[str, Tier3Source, str]]] = {}
        for path, candidate in path_candidates:
            family = self._candidate_unit_family(path)
            if family not in buckets:
                buckets[family] = []
                family_order.append(family)
            buckets[family].append((path, candidate, family))
        budget = max(1, min(len(path_candidates), int(unit_budget or 1)))
        selected: list[tuple[str, Tier3Source, str]] = []
        while len(selected) < budget:
            progressed = False
            for family in family_order:
                bucket = buckets.get(family, [])
                if not bucket:
                    continue
                selected.append(bucket.pop(0))
                progressed = True
                if len(selected) >= budget:
                    break
            if not progressed:
                break
        return selected

    def unit_descriptors(self, *, unit_budget: int = 6) -> list[dict[str, str]]:
        return [
            {
                "id": path,
                "value": candidate.value,
                "family": family,
            }
            for path, candidate, family in self._candidate_unit_portfolio(unit_budget)
        ]

    async def fetch_units(
        self,
        session: aiohttp.ClientSession,
        *,
        timeout_seconds: int,
        retries: int,
        user_agent: str,
        proxy_url: str | None,
        max_bytes: int = DEFAULT_SOURCE_MAX_BYTES,
        unit_budget: int = 6,
        unit_concurrency: int = DEFAULT_GITHUB_CANDIDATE_CONCURRENCY,
        unit_deadline_seconds: float = 8.0,
        cancel_slack_seconds: float = 0.25,
        delivery_slack_seconds: float = 0.25,
        unit_transport_acquire: Callable[[str, str], Any] | None = None,
        unit_transport_started_observer: (
            Callable[[str, str, str, float], Any] | None
        ) = None,
        unit_route_key: str = "default",
        unit_hedge_delay_seconds: float = 0.25,
        unit_content_validator: Callable[[SourceUnitResult], Awaitable[bool]] | None = None,
    ) -> AsyncIterator[SourceUnitResult]:
        if not self.enabled:
            yield SourceUnitResult(
                self.source_id,
                self.source_type,
                self.value,
                self.origin,
                "default",
                self.value,
                "default",
                False,
                error="disabled",
                transport_started=False,
            )
            return
        portfolio = self._candidate_unit_portfolio(unit_budget)
        if not portfolio:
            yield SourceUnitResult(
                self.source_id, self.source_type, self.value, self.origin,
                "default", self.value, "default", False,
                error="no candidate paths configured", transport_started=False,
            )
            return
        semaphore = asyncio.Semaphore(max(1, min(int(unit_concurrency or 1), len(portfolio))))
        delivery_timeout = _unit_delivery_timeout_seconds(
            unit_deadline_seconds,
            delivery_slack_seconds,
        )

        async def fetch_candidate_unit(
            path: str,
            candidate: Tier3Source,
            family: str,
            *,
            portfolio_started: float,
            launch_tranche: str,
            parse_valid_observer: Callable[[bool], None] | None,
        ) -> SourceUnitResult:
            loop = asyncio.get_running_loop()
            queued_at = loop.time()
            transport_started = queued_at
            execution = None
            try:
                async with semaphore:
                    async with _source_unit_execution(
                        self, path, candidate.value, family, unit_route_key,
                        unit_transport_acquire, unit_transport_started_observer,
                        unit_content_validator, delivery_timeout,
                    ) as execution:
                        transport_started = loop.time()
                        result = await candidate.fetch(
                                session,
                                timeout_seconds=timeout_seconds,
                                retries=0,
                                user_agent=user_agent,
                                proxy_url=proxy_url,
                                max_bytes=max_bytes,
                        )
            except asyncio.TimeoutError:
                result = SourceFetchResult(
                    candidate.source_id,
                    candidate.source_type,
                    candidate.value,
                    candidate.origin,
                    ok=False,
                    error=f"source unit timeout after {float(unit_deadline_seconds):.2f}s",
                    delivery_outcomes=candidate.delivery_outcomes(),
                )
            except SourceDispatchDeferred:
                raise
            except Exception as exc:
                result = SourceFetchResult(
                    candidate.source_id,
                    candidate.source_type,
                    candidate.value,
                    candidate.origin,
                    ok=False,
                    error=str(exc),
                )
            # Parsing belongs to the collector's winner-selection boundary.
            # Doing it here made the unit deadline cover CPU work and caused
            # runtime route racing to parse the same payload twice.
            content = result.content if result.ok else ""
            completed_at = loop.time()
            if execution is not None and execution.started_at is not None:
                transport_started = execution.started_at
            parse_seconds = execution.parse_seconds if execution else 0.0
            elapsed_ms = max(0, int(round((completed_at - transport_started - parse_seconds) * 1000)))
            return SourceUnitResult(
                root_id=self.source_id,
                root_type=self.source_type,
                root_value=self.value,
                origin=self.origin,
                unit_id=path,
                unit_value=candidate.value,
                unit_family=family,
                ok=bool(result.ok and content),
                content=content,
                error=result.error,
                egress_mode=result.egress_mode,
                fallback_attempted=result.fallback_attempted,
                fallback_error=result.fallback_error,
                deadline_actual_ms=elapsed_ms,
                transport_elapsed_ms=elapsed_ms,
                queued_elapsed_ms=max(
                    0,
                    int(round((transport_started - queued_at) * 1000)),
                ),
                transport_started_elapsed_ms=max(
                    0,
                    int(round((transport_started - portfolio_started) * 1000)),
                ),
                first_byte_elapsed_ms=max(
                    0,
                    int(result.first_byte_elapsed_ms or 0),
                ),
                launch_tranche=launch_tranche,
                transport_started=bool(execution and execution.started_at is not None),
                delivery_path=result.delivery_path,
                delivery_outcomes=result.delivery_outcomes,
                _parse_valid_observer=parse_valid_observer,
            )

        async def iter_portfolio(
            items: list[tuple[str, Tier3Source, str]],
        ) -> AsyncIterator[SourceUnitResult]:
            if not items:
                return
            loop = asyncio.get_running_loop()
            portfolio_started = loop.time()
            parse_valid_event = asyncio.Event()
            primary: list[tuple[str, Tier3Source, str]] = []
            hedge: list[tuple[str, Tier3Source, str]] = []
            primary_families: set[str] = set()
            primary_limit = min(6, max(1, int(unit_concurrency or 1)))
            for item in items:
                family = item[2]
                # Aggregate files are fallback supply.  Keep them out of the
                # first tranche, but let them start with the 250 ms hedge when
                # the protocol units have not produced parse-valid content.
                # Waiting for every slow/404 protocol unit to finish before
                # starting an aggregate serializes the cold-start path behind
                # the full unit deadline.
                if (
                    family != "aggregate"
                    and family not in primary_families
                    and len(primary) < primary_limit
                ):
                    primary.append(item)
                    primary_families.add(family)
                else:
                    hedge.append(item)

            def create_tasks(
                tranche_items: list[tuple[str, Tier3Source, str]],
                tranche: str,
            ) -> list[asyncio.Task[SourceUnitResult]]:
                return [
                    asyncio.create_task(
                        fetch_candidate_unit(
                            path,
                            candidate,
                            family,
                            portfolio_started=portfolio_started,
                            launch_tranche=tranche,
                            parse_valid_observer=(
                                lambda valid: parse_valid_event.set()
                                if valid
                                else None
                            ),
                        )
                    )
                    for path, candidate, family in tranche_items
                ]

            tasks = create_tasks(primary, "primary")
            hedge_task: asyncio.Task[None] | None = None
            if hedge:
                async def wait_for_hedge() -> None:
                    await asyncio.sleep(
                        max(0.0, min(1.0, float(unit_hedge_delay_seconds)))
                    )

                hedge_task = asyncio.create_task(wait_for_hedge())
            try:
                active: set[asyncio.Task[Any]] = set(tasks)
                if hedge_task is not None:
                    active.add(hedge_task)
                while active:
                    done, active = await asyncio.wait(
                        active,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    hedge_due = False
                    if hedge_task is not None and hedge_task in done:
                        done.remove(hedge_task)
                        hedge_task = None
                        hedge_due = True
                    for completed in done:
                        yield await completed
                    if hedge_due and not parse_valid_event.is_set():
                        active.update(create_tasks(hedge, "hedge"))
            finally:
                pending = [task for task in active if not task.done()]
                for task in pending:
                    task.cancel()
                if pending and cancel_slack_seconds > 0:
                    done, still_pending = await asyncio.wait(
                        pending,
                        timeout=min(
                            0.25,
                            max(0.0, float(cancel_slack_seconds)),
                        ),
                    )
                    for task in done:
                        try:
                            task.result()
                        except BaseException:
                            pass
                    for task in still_pending:
                        task.add_done_callback(
                            lambda late: (
                                None if late.cancelled() else late.exception()
                            )
                        )

        async for result in iter_portfolio(portfolio):
            yield result

    async def _fetch(
        self,
        session: aiohttp.ClientSession,
        *,
        timeout_seconds: int,
        retries: int,
        user_agent: str,
        proxy_url: str | None,
        max_bytes: int,
    ) -> str | None:
        contents: list[str] = []
        failures: list[str] = []
        async for result in self.fetch_units(
            session,
            timeout_seconds=timeout_seconds,
            retries=retries,
            user_agent=user_agent,
            proxy_url=proxy_url,
            max_bytes=max_bytes,
            unit_budget=6,
            unit_concurrency=DEFAULT_GITHUB_CANDIDATE_CONCURRENCY,
            unit_deadline_seconds=float(timeout_seconds),
        ):
            if result.ok and result.content:
                contents.append(result.content)
            elif result.error:
                failures.append(result.error)
        if contents:
            return "\n".join(contents)
        raise RuntimeError("; ".join(failures) or "no candidate paths produced content")


class TelegramPublicChannelSource(RemoteTextSource):
    source_type = "telegram_channel"

    def __init__(self, value: str, **kwargs: Any) -> None:
        super().__init__(telegram_web_url(value), **kwargs)

    async def _fetch(
        self,
        session: aiohttp.ClientSession,
        *,
        timeout_seconds: int,
        retries: int,
        user_agent: str,
        proxy_url: str | None,
        max_bytes: int,
    ) -> str | None:
        text = await super()._fetch(
            session,
            timeout_seconds=timeout_seconds,
            retries=retries,
            user_agent=user_agent,
            proxy_url=proxy_url,
            max_bytes=max_bytes,
        )
        content = extract_telegram_static_content(text or "")
        if not content:
            raise RuntimeError(
                "Telegram public preview returned no static message content; use subscription_urls or local_files for this channel"
            )
        return content


class LocalFileSource(Tier3Source):
    source_type = "local_file"

    async def _fetch(
        self,
        session: aiohttp.ClientSession,
        *,
        timeout_seconds: int,
        retries: int,
        user_agent: str,
        proxy_url: str | None,
        max_bytes: int,
    ) -> str | None:
        path = Path(self.value)
        size = await asyncio.to_thread(lambda: path.stat().st_size)
        if size > max_bytes:
            raise ValueError(f"source exceeds {max_bytes} byte read cap")

        def read_limited() -> str:
            with path.open("rb") as handle:
                data = handle.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError(f"source exceeds {max_bytes} byte read cap")
            return data.decode("utf-8", errors="replace")

        return await asyncio.to_thread(read_limited)


def raw_github_url(repo_full_name: str, branch: str, path: str) -> str:
    encoded_path = "/".join(quote(part, safe="") for part in path.split("/"))
    return f"https://raw.githubusercontent.com/{repo_full_name}/{quote(branch, safe='')}/{encoded_path}"


def telegram_web_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("@"):
        return f"https://t.me/s/{text[1:]}"
    parsed = urlparse(text)
    if parsed.netloc in {"t.me", "telegram.me", "www.t.me"}:
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if parts and parts[0] == "s":
            return f"https://t.me/s/{'/'.join(parts[1:])}"
        if parts:
            return f"https://t.me/s/{parts[0]}"
    if "://" not in text and "/" not in text:
        return f"https://t.me/s/{text}"
    return text


def canonical_source_identity(source_type: str, value: str) -> str:
    """Return the stable root identity used by config, collector and reports."""

    normalized_type = str(source_type or "subscription_url").strip() or "subscription_url"
    normalized_value = str(value or "").strip()
    if normalized_type == "telegram_channel":
        normalized_value = telegram_web_url(normalized_value)
    elif normalized_type == "github_repo":
        normalized_value = normalized_value.rstrip("/")
    return f"{normalized_type}:{normalized_value}"


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "")


def extract_telegram_static_content(text: str) -> str:
    if "tgme_widget_message" not in text:
        return ""
    links = _unique(
        [
            html.unescape(match)
            for match in re.findall(
                r"""(?<![\w])((?:vmess|vless|trojan|ss|ssr|hysteria2|hy2|tuic)://[^"'<>\s]+)""",
                text or "",
                flags=re.IGNORECASE,
            )
        ]
    )
    message_blocks = re.findall(
        r"""<div[^>]+class=["'][^"']*tgme_widget_message_text[^"']*["'][^>]*>(.*?)</div>""",
        text or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    message_text = html.unescape(_strip_html("\n".join(message_blocks))).strip()
    parts = [*links]
    if message_text:
        parts.append(message_text)
    return "\n".join(part for part in parts if part).strip()


def _unique(values: Iterable[str]) -> list[str]:
    unique_values: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    return unique_values


def source_value(entry: Any) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        value = entry.get("value") or entry.get("url") or entry.get("path") or entry.get("channel")
        return str(value) if value else ""
    return ""


def normalize_source_entries(entries: Iterable[Any], source_type: str, *, origin: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for entry in entries or []:
        value = source_value(entry)
        if not value:
            continue
        if isinstance(entry, dict):
            normalized.append(
                {
                    **entry,
                    "type": entry.get("type", source_type),
                    "value": value,
                    "enabled": coerce_bool(entry.get("enabled", True), True),
                    "headers": entry.get("headers", {}),
                    "tags": entry.get("tags", []),
                    "origin": entry.get("origin", origin),
                }
            )
        else:
            normalized.append({"type": source_type, "value": value, "enabled": True, "origin": origin})
    return [entry for entry in normalized if coerce_bool(entry.get("enabled", True), True)]


def source_entries_from_sources_config(sources_config: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    entries.extend(normalize_source_entries(sources_config.get("local_files", []), "local_file", origin="tier3b"))
    entries.extend(normalize_source_entries(sources_config.get("subscription_urls", []), "subscription_url", origin="tier3b"))
    entries.extend(normalize_source_entries(sources_config.get("github_repos", []), "github_repo", origin="tier3b"))
    entries.extend(normalize_source_entries(sources_config.get("telegram_channels", []), "telegram_channel", origin="tier3b"))
    for entry in entries:
        entry['fetch_paths'] = source_fetch_paths(entry, sources_config.get('fetch_profiles'))
    return entries


def source_entries_from_tier3_config(tier3_config: dict[str, Any]) -> list[dict[str, Any]]:
    return normalize_source_entries(tier3_config.get("subscription_urls", []), "subscription_url", origin="tier3a")


def bootstrap_source_entries(entries: Iterable[Any] | None = None) -> list[dict[str, Any]]:
    if entries is None:
        return []
    normalized: list[dict[str, Any]] = []
    for entry in entries or []:
        value = source_value(entry)
        if not value:
            continue
        if isinstance(entry, dict):
            tags = list(entry.get("tags", []) or [])
            if not tags:
                tags = ["bootstrap-user"]
            normalized.append(
                {
                    **entry,
                    "type": str(entry.get("type") or infer_source_type(value)),
                    "value": value,
                    "enabled": coerce_bool(entry.get("enabled", True), True),
                    "headers": entry.get("headers", {}) if isinstance(entry.get("headers"), dict) else {},
                    "tags": tags,
                    "origin": "bootstrap",
                }
            )
        else:
            normalized.append(
                {
                    "type": infer_source_type(value),
                    "value": value,
                    "enabled": True,
                    "origin": "bootstrap",
                    "tags": ["bootstrap-user"],
                }
            )
    return [entry for entry in normalized if coerce_bool(entry.get("enabled", True), True)]


def build_source(
    entry: dict[str, Any],
    *,
    candidate_paths: list[str] | None = None,
    github_delivery: dict[str, Any] | None = None,
    fetch_profiles: dict[str, Any] | None = None,
) -> Tier3Source | None:
    source_type = str(entry.get("type") or "subscription_url")
    value = source_value(entry)
    if not value:
        return None
    entry_candidate_paths = entry.get("candidate_paths")
    effective_candidate_paths = (
        list(entry_candidate_paths)
        if isinstance(entry_candidate_paths, list)
        else candidate_paths
    )
    kwargs = {
        "enabled": coerce_bool(entry.get("enabled", True), True),
        "headers": entry.get("headers", {}) if isinstance(entry.get("headers"), dict) else {},
        "tags": list(entry.get("tags", []) or []),
        "origin": str(entry.get("origin") or "tier3b"),
        "candidate_paths": effective_candidate_paths,
        "mirror_fallback": coerce_bool(entry.get("mirror_fallback", True), True),
        "fetch_paths": source_fetch_paths(entry, fetch_profiles),
    }
    if source_type in {"github", "github_repo", "github_repos"}:
        return GitHubRepositorySource(value, **kwargs)
    if source_type in {"telegram", "telegram_channel", "telegram_public_channel"}:
        return TelegramPublicChannelSource(value, **kwargs)
    if source_type in {"file", "local_file", "local_files"}:
        return LocalFileSource(value, **kwargs)
    return RemoteTextSource(value, **kwargs)
