from __future__ import annotations

import base64

import asyncio
import inspect
import secrets
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Awaitable, Callable, Generic, Iterable, Mapping, Sequence, TypeVar
from urllib.parse import quote


RUNTIME_SOURCE_LISTENER_PREFIX = "source-"
RUNTIME_SOURCE_EGRESS_PROXY_NAME = "RUNTIME-SOURCE-EGRESS-PROXY"
RUNTIME_SOURCE_BOOTSTRAP_POOL_PROXY = "PROXY-FALLBACK"
RUNTIME_SOURCE_MAX_ROUTE_ATTEMPTS = 3
RUNTIME_SOURCE_DEFAULT_STAGGER_MS = 250


class RuntimeSourceRouteKind(str, Enum):
    FINAL_EXIT = "final_exit"
    EGRESS = "egress"
    DIRECT = "direct"


class RuntimeSourcePhase(str, Enum):
    BOOTSTRAP = "bootstrap"
    FORMAL = "formal"


_LISTENER_NAME_BY_ROUTE = {
    RuntimeSourceRouteKind.FINAL_EXIT: "source-final-exit",
    RuntimeSourceRouteKind.EGRESS: "source-egress",
    RuntimeSourceRouteKind.DIRECT: "source-direct",
}
RUNTIME_SOURCE_LISTENER_NAMES = frozenset(_LISTENER_NAME_BY_ROUTE.values())
_PROXY_NAME_BY_ROUTE = {
    RuntimeSourceRouteKind.FINAL_EXIT: "FINAL-EXIT",
    RuntimeSourceRouteKind.EGRESS: RUNTIME_SOURCE_EGRESS_PROXY_NAME,
    RuntimeSourceRouteKind.DIRECT: "DIRECT",
}
_FORMAL_ROUTE_ORDER = (
    RuntimeSourceRouteKind.FINAL_EXIT,
    RuntimeSourceRouteKind.EGRESS,
    RuntimeSourceRouteKind.DIRECT,
)
_BOOTSTRAP_ROUTE_ORDER = (
    RuntimeSourceRouteKind.EGRESS,
    RuntimeSourceRouteKind.DIRECT,
    RuntimeSourceRouteKind.FINAL_EXIT,
)


def _coerce_route_kind(value: RuntimeSourceRouteKind | str) -> RuntimeSourceRouteKind:
    if isinstance(value, RuntimeSourceRouteKind):
        return value
    normalized = str(value or "").strip().lower().replace("-", "_")
    try:
        return RuntimeSourceRouteKind(normalized)
    except ValueError as exc:
        raise ValueError(f"unsupported runtime source route kind: {value!r}") from exc


def _coerce_phase(value: RuntimeSourcePhase | str) -> RuntimeSourcePhase:
    if isinstance(value, RuntimeSourcePhase):
        return value
    normalized = str(value or "").strip().lower().replace("-", "_")
    try:
        return RuntimeSourcePhase(normalized)
    except ValueError as exc:
        raise ValueError(f"unsupported runtime source phase: {value!r}") from exc


def _validate_generation(value: str | int, label: str) -> str | int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label} is required")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"{label} must be non-negative")
        return value
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{label} is required")
    return normalized


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeSourceListenerCredentials:
    username: str
    password: str

    def __post_init__(self) -> None:
        if not self.username or not self.password:
            raise ValueError("runtime source listener credentials must be non-empty")
        if any(character.isspace() for character in self.username):
            raise ValueError("runtime source listener username cannot contain whitespace")

    @classmethod
    def generate(cls) -> RuntimeSourceListenerCredentials:
        return cls(
            username=f"tesseract-{secrets.token_urlsafe(12)}",
            password=secrets.token_urlsafe(32),
        )

    def __repr__(self) -> str:
        return "RuntimeSourceListenerCredentials(<redacted>)"


@dataclass(frozen=True, slots=True)
class RuntimeSourceListenerPorts:
    final_exit: int
    direct: int
    egress: int | None = None

    def __post_init__(self) -> None:
        values = [self.final_exit, self.direct]
        if self.egress is not None:
            values.append(self.egress)
        if any(isinstance(port, bool) or not 1 <= int(port) <= 65535 for port in values):
            raise ValueError("runtime source listener ports must be between 1 and 65535")
        if len({int(port) for port in values}) != len(values):
            raise ValueError("runtime source listener ports must be unique")

    def for_route(self, route_kind: RuntimeSourceRouteKind) -> int | None:
        if route_kind is RuntimeSourceRouteKind.FINAL_EXIT:
            return int(self.final_exit)
        if route_kind is RuntimeSourceRouteKind.EGRESS:
            return int(self.egress) if self.egress is not None else None
        return int(self.direct)

    def as_tuple(self) -> tuple[int, ...]:
        if self.egress is None:
            return (int(self.final_exit), int(self.direct))
        return (int(self.final_exit), int(self.egress), int(self.direct))


def choose_runtime_source_listener_ports(
    candidate_attempts: Iterable[Sequence[int]],
    *,
    excluded_ports: Iterable[int] = (),
    include_egress: bool = True,
    max_attempts: int = RUNTIME_SOURCE_MAX_ROUTE_ATTEMPTS,
) -> RuntimeSourceListenerPorts:
    """Choose the first conflict-free candidate set, examining at most 3 attempts.

    Port discovery/binding stays with the runtime owner.  Supplying candidate
    sets keeps this policy deterministic and makes the three-attempt contract
    testable without opening sockets in the formatter.
    """

    attempt_limit = int(max_attempts)
    if not 1 <= attempt_limit <= RUNTIME_SOURCE_MAX_ROUTE_ATTEMPTS:
        raise ValueError("runtime source listener allocation allows 1 to 3 attempts")
    excluded: set[int] = set()
    for port in excluded_ports:
        if isinstance(port, bool):
            continue
        try:
            normalized_port = int(port)
        except (TypeError, ValueError):
            continue
        if 1 <= normalized_port <= 65535:
            excluded.add(normalized_port)
    required_count = 3 if include_egress else 2
    examined = 0
    candidate_iterator = iter(candidate_attempts)
    while examined < attempt_limit:
        try:
            candidate_set = next(candidate_iterator)
        except StopIteration:
            break
        examined += 1
        try:
            candidates = tuple(int(port) for port in candidate_set)
        except (TypeError, ValueError):
            continue
        if len(candidates) != required_count:
            continue
        if any(not 1 <= port <= 65535 for port in candidates):
            continue
        if len(set(candidates)) != len(candidates) or any(
            port in excluded for port in candidates
        ):
            continue
        if include_egress:
            return RuntimeSourceListenerPorts(
                final_exit=candidates[0],
                egress=candidates[1],
                direct=candidates[2],
            )
        return RuntimeSourceListenerPorts(
            final_exit=candidates[0],
            direct=candidates[1],
        )
    raise RuntimeError(
        f"no conflict-free runtime source listener ports after {examined} attempt(s)"
    )


@dataclass(frozen=True, slots=True)
class RuntimeSourceListenerSpec:
    route_kind: RuntimeSourceRouteKind
    port: int
    proxy: str
    runtime_generation: str | int
    config_generation: str | int
    credentials: RuntimeSourceListenerCredentials = field(repr=False)
    name: str = ""
    listen: str = "127.0.0.1"

    def __post_init__(self) -> None:
        route_kind = _coerce_route_kind(self.route_kind)
        object.__setattr__(self, "route_kind", route_kind)
        object.__setattr__(
            self,
            "runtime_generation",
            _validate_generation(self.runtime_generation, "runtime_generation"),
        )
        object.__setattr__(
            self,
            "config_generation",
            _validate_generation(self.config_generation, "config_generation"),
        )
        if self.listen != "127.0.0.1":
            raise ValueError("runtime source listeners must bind only to 127.0.0.1")
        if isinstance(self.port, bool) or not 1 <= int(self.port) <= 65535:
            raise ValueError("runtime source listener port must be between 1 and 65535")
        object.__setattr__(self, "port", int(self.port))
        expected_name = _LISTENER_NAME_BY_ROUTE[route_kind]
        normalized_name = str(self.name or expected_name).strip()
        if normalized_name != expected_name:
            raise ValueError(
                f"runtime source {route_kind.value} listener name must be {expected_name}"
            )
        object.__setattr__(self, "name", normalized_name)
        normalized_proxy = str(self.proxy or "").strip()
        if not normalized_proxy:
            raise ValueError("runtime source listener proxy target is required")
        object.__setattr__(self, "proxy", normalized_proxy)

    def to_mihomo_listener(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": "http",
            "listen": self.listen,
            "port": self.port,
            "proxy": self.proxy,
            "users": [
                {
                    "username": self.credentials.username,
                    "password": self.credentials.password,
                }
            ],
        }

    def endpoint(
        self,
        *,
        ready: bool,
        priority: int,
        stagger_ms: int,
    ) -> RuntimeSourceFetchEndpoint:
        return RuntimeSourceFetchEndpoint(
            route_kind=self.route_kind,
            listener_name=self.name,
            host=self.listen,
            port=self.port,
            runtime_generation=self.runtime_generation,
            config_generation=self.config_generation,
            ready=bool(ready),
            priority=int(priority),
            stagger_ms=int(stagger_ms),
            _credentials=self.credentials,
        )


@dataclass(frozen=True, slots=True)
class RuntimeSourceFetchEndpoint:
    route_kind: RuntimeSourceRouteKind
    listener_name: str
    host: str
    port: int
    runtime_generation: str | int
    config_generation: str | int
    ready: bool
    priority: int
    stagger_ms: int
    _credentials: RuntimeSourceListenerCredentials = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "route_kind", _coerce_route_kind(self.route_kind))
        if self.host != "127.0.0.1":
            raise ValueError("runtime source endpoint must use 127.0.0.1")
        if isinstance(self.port, bool) or not 1 <= int(self.port) <= 65535:
            raise ValueError("runtime source endpoint port must be between 1 and 65535")
        if int(self.priority) < 0 or int(self.stagger_ms) < 0:
            raise ValueError("runtime source priority and stagger must be non-negative")
        object.__setattr__(self, "port", int(self.port))
        object.__setattr__(self, "priority", int(self.priority))
        object.__setattr__(self, "stagger_ms", int(self.stagger_ms))
        object.__setattr__(
            self,
            "runtime_generation",
            _validate_generation(self.runtime_generation, "runtime_generation"),
        )
        object.__setattr__(
            self,
            "config_generation",
            _validate_generation(self.config_generation, "config_generation"),
        )

    @property
    def proxy_url(self) -> str:
        username = quote(self._credentials.username, safe="")
        password = quote(self._credentials.password, safe="")
        return f"http://{username}:{password}@{self.host}:{self.port}"

    @property
    def proxy_authorization(self) -> str:
        raw = f"{self._credentials.username}:{self._credentials.password}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    @property
    def redacted_proxy_url(self) -> str:
        return f"http://<redacted>@{self.host}:{self.port}"

    def safe_summary(self) -> dict[str, Any]:
        return {
            "route_kind": self.route_kind.value,
            "proxy_url": self.redacted_proxy_url,
            "runtime_generation": self.runtime_generation,
            "config_generation": self.config_generation,
            "ready": self.ready,
            "priority": self.priority,
            "stagger_ms": self.stagger_ms,
        }


@dataclass(frozen=True, slots=True)
class RuntimeSourceTransportGeneration:
    runtime_generation: str | int
    config_generation: str | int
    listeners: tuple[RuntimeSourceListenerSpec, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "runtime_generation",
            _validate_generation(self.runtime_generation, "runtime_generation"),
        )
        object.__setattr__(
            self,
            "config_generation",
            _validate_generation(self.config_generation, "config_generation"),
        )
        routes = [listener.route_kind for listener in self.listeners]
        if len(set(routes)) != len(routes):
            raise ValueError("runtime source generation contains duplicate routes")
        if RuntimeSourceRouteKind.FINAL_EXIT not in routes:
            raise ValueError("runtime source generation requires FINAL-EXIT listener")
        if RuntimeSourceRouteKind.DIRECT not in routes:
            raise ValueError("runtime source generation requires DIRECT listener")
        if any(
            listener.runtime_generation != self.runtime_generation
            or listener.config_generation != self.config_generation
            for listener in self.listeners
        ):
            raise ValueError("runtime source listener generation does not match owner")

    def listener_payloads(self) -> list[dict[str, Any]]:
        return [listener.to_mihomo_listener() for listener in self.listeners]

    def fetch_endpoints(
        self,
        *,
        phase: RuntimeSourcePhase | str,
        readiness: bool | Mapping[RuntimeSourceRouteKind | str, bool] = False,
        last_successful_route: RuntimeSourceRouteKind | str | None = None,
        stagger_ms: int = RUNTIME_SOURCE_DEFAULT_STAGGER_MS,
        max_routes: int = RUNTIME_SOURCE_MAX_ROUTE_ATTEMPTS,
    ) -> tuple[RuntimeSourceFetchEndpoint, ...]:
        normalized_phase = _coerce_phase(phase)
        if not 0 <= int(stagger_ms):
            raise ValueError("runtime source stagger must be non-negative")
        route_limit = int(max_routes)
        if not 1 <= route_limit <= RUNTIME_SOURCE_MAX_ROUTE_ATTEMPTS:
            raise ValueError("runtime source fetch allows 1 to 3 routes")

        listeners_by_route = {
            listener.route_kind: listener for listener in self.listeners
        }
        base_order = (
            _FORMAL_ROUTE_ORDER
            if normalized_phase is RuntimeSourcePhase.FORMAL
            else _BOOTSTRAP_ROUTE_ORDER
        )
        ordered_routes: list[RuntimeSourceRouteKind] = []
        if last_successful_route is not None:
            prior = _coerce_route_kind(last_successful_route)
            if prior in listeners_by_route:
                ordered_routes.append(prior)
        ordered_routes.extend(
            route for route in base_order if route not in ordered_routes
        )
        ordered_routes = [
            route for route in ordered_routes if route in listeners_by_route
        ][:route_limit]

        normalized_readiness: dict[RuntimeSourceRouteKind, bool] = {}
        if isinstance(readiness, Mapping):
            for key, value in readiness.items():
                normalized_readiness[_coerce_route_kind(key)] = bool(value)
        else:
            normalized_readiness = {
                route: bool(readiness) for route in listeners_by_route
            }
        return tuple(
            listeners_by_route[route].endpoint(
                ready=normalized_readiness.get(route, False),
                priority=index,
                stagger_ms=index * int(stagger_ms),
            )
            for index, route in enumerate(ordered_routes)
        )


def build_runtime_source_transport_generation(
    *,
    runtime_generation: str | int,
    config_generation: str | int,
    ports: RuntimeSourceListenerPorts,
    egress_proxy_name: str | None = None,
    credentials: Mapping[
        RuntimeSourceRouteKind | str,
        RuntimeSourceListenerCredentials,
    ]
    | None = None,
) -> RuntimeSourceTransportGeneration:
    normalized_credentials = {
        _coerce_route_kind(route): value
        for route, value in (credentials or {}).items()
    }
    routes = [RuntimeSourceRouteKind.FINAL_EXIT]
    if ports.egress is not None and egress_proxy_name is not None:
        routes.append(RuntimeSourceRouteKind.EGRESS)
    elif ports.egress is not None or egress_proxy_name is not None:
        raise ValueError("egress listener requires both a port and a proxy name")
    routes.append(RuntimeSourceRouteKind.DIRECT)

    listeners: list[RuntimeSourceListenerSpec] = []
    for route in routes:
        credentials_for_route = normalized_credentials.get(route)
        if credentials_for_route is None:
            credentials_for_route = RuntimeSourceListenerCredentials.generate()
        proxy = (
            str(egress_proxy_name).strip()
            if route is RuntimeSourceRouteKind.EGRESS
            else _PROXY_NAME_BY_ROUTE[route]
        )
        listeners.append(
            RuntimeSourceListenerSpec(
                route_kind=route,
                port=int(ports.for_route(route) or 0),
                proxy=proxy,
                runtime_generation=runtime_generation,
                config_generation=config_generation,
                credentials=credentials_for_route,
            )
        )
    return RuntimeSourceTransportGeneration(
        runtime_generation=runtime_generation,
        config_generation=config_generation,
        listeners=tuple(listeners),
    )


T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True, slots=True)
class RuntimeSourceFetchRaceResult(Generic[T]):
    winner: RuntimeSourceFetchEndpoint | None
    value: T | None
    attempted_routes: tuple[RuntimeSourceRouteKind, ...]
    errors: tuple[str, ...]

    @property
    def succeeded(self) -> bool:
        return self.winner is not None


async def _await_if_needed(value: R | Awaitable[R]) -> R:
    if inspect.isawaitable(value):
        return await value
    return value


async def race_runtime_source_fetch(
    endpoints: Sequence[RuntimeSourceFetchEndpoint],
    fetch: Callable[[RuntimeSourceFetchEndpoint], Awaitable[T]],
    validate: Callable[
        [RuntimeSourceFetchEndpoint, T],
        bool | Awaitable[bool],
    ],
    *,
    on_winner: Callable[
        [RuntimeSourceFetchEndpoint, T],
        Any | Awaitable[Any],
    ]
    | None = None,
    max_routes: int = RUNTIME_SOURCE_MAX_ROUTE_ATTEMPTS,
) -> RuntimeSourceFetchRaceResult[T]:
    """Race ready route lanes and publish at most one validated response."""

    route_limit = int(max_routes)
    if not 1 <= route_limit <= RUNTIME_SOURCE_MAX_ROUTE_ATTEMPTS:
        raise ValueError("runtime source fetch allows 1 to 3 routes")
    ordered_endpoints = sorted(
        endpoints,
        key=lambda endpoint: (endpoint.priority, endpoint.stagger_ms),
    )
    candidates = [
        endpoint for endpoint in ordered_endpoints if endpoint.ready
    ][:route_limit]
    # Readiness can change independently per lane.  Compress the stagger
    # after filtering so the first usable lane starts immediately and gaps
    # left by unready lanes do not add artificial delay.
    step_candidates = [
        endpoint.stagger_ms // endpoint.priority
        for endpoint in ordered_endpoints
        if endpoint.priority > 0 and endpoint.stagger_ms > 0
    ]
    stagger_step_ms = min(step_candidates) if step_candidates else 0
    candidates = [
        replace(endpoint, stagger_ms=index * stagger_step_ms)
        for index, endpoint in enumerate(candidates)
    ]
    if not candidates:
        return RuntimeSourceFetchRaceResult(None, None, (), ())

    attempted: list[RuntimeSourceRouteKind] = []
    errors: list[str] = []

    async def attempt(
        endpoint: RuntimeSourceFetchEndpoint,
    ) -> tuple[RuntimeSourceFetchEndpoint, T, bool]:
        if endpoint.stagger_ms:
            await asyncio.sleep(endpoint.stagger_ms / 1000.0)
        attempted.append(endpoint.route_kind)
        value = await fetch(endpoint)
        accepted = bool(await _await_if_needed(validate(endpoint, value)))
        return endpoint, value, accepted

    tasks = {
        asyncio.create_task(attempt(endpoint)): endpoint for endpoint in candidates
    }
    try:
        pending = set(tasks)
        while pending:
            completed, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in sorted(
                completed,
                key=lambda item: tasks[item].priority,
            ):
                endpoint = tasks[task]
                try:
                    completed_endpoint, value, accepted = task.result()
                except asyncio.CancelledError:
                    continue
                except Exception as exc:
                    errors.append(
                        f"{endpoint.route_kind.value}:{type(exc).__name__}"
                    )
                    continue
                if not accepted:
                    errors.append(f"{endpoint.route_kind.value}:rejected")
                    continue
                for other in pending:
                    other.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if on_winner is not None:
                    await _await_if_needed(on_winner(completed_endpoint, value))
                return RuntimeSourceFetchRaceResult(
                    completed_endpoint,
                    value,
                    tuple(attempted),
                    tuple(errors),
                )
        return RuntimeSourceFetchRaceResult(
            None,
            None,
            tuple(attempted),
            tuple(errors),
        )
    finally:
        unfinished = [task for task in tasks if not task.done()]
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)
