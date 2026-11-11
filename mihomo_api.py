"""Rich client for the Mihomo external controller RESTful API.

Consolidates every Mihomo controller call the desktop app needs so that
callers never have to hand-build urllib requests. Keeps all endpoints in one
place, uses a single secret/timeout source, and exposes both one-shot JSON
calls (/proxies, /rules, /providers, /version, /memory, /configs, /group/*)
and streaming calls (/traffic, /logs, /connections).  The post-dial observer
polls /connections through ``MihomoControllerKeepAlive``, which keeps one
persistent controller socket.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import socket
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Literal
from urllib.parse import quote, urlencode, urlsplit
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from config_utils import coerce_bool, coerce_float, coerce_port

UrlOpen = Callable[..., Any]
ConnectionAbortSink = Callable[[Callable[[], None]], None]
STREAM_READ_TIMEOUT_SECONDS = 3600.0
# Post-dial observer controller budget (design v3 §18.2 CTRL_TIMEOUT_S and
# MAX_BODY_BYTES): each blocking phase of a keep-alive read is bounded by
# 0.35 s, and a /connections body above ~2000 entries is not parsed.
KEEPALIVE_PHASE_TIMEOUT_SECONDS = 0.35
KEEPALIVE_MAX_BODY_BYTES = 1_500_000
_KEEPALIVE_READ_CHUNK_BYTES = 65_536
# The raw transport's bound on a response head or a chunk-size line.
_KEEPALIVE_HEAD_LIMIT_BYTES = 65_536
# Initial size of the raw transport's receive buffer (it grows to the largest body).
_KEEPALIVE_RAW_BUFFER_BYTES = 262_144
# Signatures of a reused keep-alive socket that the controller had already
# closed.  Only these, and only on a reused socket, earn one immediate retry
# on a fresh connection; timeouts and refused dials never retry.
_KEEPALIVE_STALE_SOCKET_ERRORS: tuple[type[BaseException], ...] = (
    http.client.RemoteDisconnected,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
)

# Route lease (lease-v1): wall-clock windows of this process's own delay
# requests, so a lease can tell Mihomo's own health check from the app's.
# Names: a proxy name (``/proxies/<name>/delay``), ``group:<g>``
# (``/group/<g>/delay``) and ``provider:<p>`` (provider ``/healthcheck``).
DELAY_WINDOW_MAXLEN = 8
DELAY_WINDOW_MAX_NAMES = 512
_DELAY_WINDOW_LOCK = threading.Lock()
_DELAY_WINDOWS: dict[tuple[str, str], deque[list[Any]]] = {}


@contextmanager
def _delay_window(base_url: str, name: str) -> Iterator[None]:
    """Record [issue wall, finish wall] around one delay request (no I/O)."""

    entry: list[Any] = [time.time(), None]
    key = (str(base_url or ""), str(name or ""))
    with _DELAY_WINDOW_LOCK:
        windows = _DELAY_WINDOWS.pop(key, None)
        if windows is None:
            windows = deque(maxlen=DELAY_WINDOW_MAXLEN)
        # Re-insert last: dict order is the least-recently-used order.
        _DELAY_WINDOWS[key] = windows
        while len(_DELAY_WINDOWS) > DELAY_WINDOW_MAX_NAMES:
            _DELAY_WINDOWS.pop(next(iter(_DELAY_WINDOWS)))
        windows.append(entry)
    try:
        yield
    finally:
        with _DELAY_WINDOW_LOCK:
            entry[1] = time.time()


def recent_delay_windows(base_url: str) -> dict[str, list[tuple[float, float | None]]]:
    """This process's recent delay windows on one controller, by name.

    A window still in flight has ``None`` as its finish time.
    """

    base = str(base_url or "").rstrip("/")
    with _DELAY_WINDOW_LOCK:
        return {
            name: [(float(item[0]), None if item[1] is None else float(item[1])) for item in windows]
            for (url, name), windows in _DELAY_WINDOWS.items()
            if url.rstrip("/") == base
        }


def clear_delay_windows() -> None:
    """Forget every recorded window (tests)."""

    with _DELAY_WINDOW_LOCK:
        _DELAY_WINDOWS.clear()


class MihomoAPIError(RuntimeError):
    """Raised when a Mihomo controller call fails."""


class MihomoDelayTestFailed(MihomoAPIError):
    """The controller completed a proxy URL test with a negative verdict.

    Every native verdict raises one of the subclasses below, so existing
    ``except MihomoDelayTestFailed`` handlers keep catching all of them.
    """


class MihomoDelayTransportFailed(MihomoDelayTestFailed):
    """HTTP 503 from ``/proxies/{name}/delay``: the test failed in transport.

    Mihomo answers 503 when the dial, the tunnel or the HTTP exchange broke
    before any status line arrived (zombie, refused or down node).
    """


class MihomoDelayTimeout(MihomoDelayTestFailed):
    """HTTP 504 ``{"message": "Timeout"}``: the test reached its native timeout."""


class MihomoDelayStatusMismatch(MihomoDelayTestFailed):
    """The test returned a delay, but ``extra[url].alive`` read back false.

    The node answered over HTTP with a status outside the expected range;
    Mihomo records that as a failed sample for the URL, as its own tick would.
    """


class MihomoProviderUpdateRejected(MihomoAPIError):
    """HTTP 503 from ``PUT /providers/proxies/{name}`` carrying a parser error.

    Mihomo answers 503 for any ``provider.Update()`` error.  Only its
    parser's messages mean the file was refused and the loaded members
    kept; a read error (another process holding the file) stays a plain
    ``MihomoAPIError``, which callers retry like a transport failure.  The
    controller's message is kept as ``controller_message`` on both, never
    in the text (a read error's message names the local file path).
    """


# Mihomo 1.19.27's proxy-provider parser errors (adapter/provider/provider.go
# NewProxiesParser, adapter.ParseProxy; every yaml.v3 error starts "yaml: ").
# Fixed English text, unlike a read error's OS text, which Windows localises.
_PROVIDER_PARSE_ERROR = re.compile(
    r"proxy \d+ (?:override )?error: |yaml: |decrypt config error: "
    r"|file must have a `proxies` field|file doesn't have any proxy|doesn't match any proxy"
)


def _http_failure(method: str, path: str, status: int, reason: str, raw: bytes) -> MihomoAPIError:
    error_type: type[MihomoAPIError] = MihomoAPIError
    member_delay = (path.startswith("/proxies/") and path.endswith("/delay")) or (
        # GET /providers/proxies/{provider}/{member}/healthcheck shares the
        # /proxies/{name}/delay handler (names are quoted, so no extra '/').
        path.startswith("/providers/proxies/")
        and path.endswith("/healthcheck")
        and path.count("/") == 5
    )
    message: str | None = None
    if method.upper() == "GET" and member_delay:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            payload = None
        # Mihomo uses 504/Timeout and 503/a failed delay test (or a Go error
        # marshalled as {}) as data-plane results. HTML upstream 503/504 and
        # arbitrary controller errors remain attribution-neutral API failures.
        if status == 504 and payload == {"message": "Timeout"}:
            error_type = MihomoDelayTimeout
        elif status == 503 and payload in ({}, {"message": "An error occurred in the delay test"}):
            error_type = MihomoDelayTransportFailed
    elif (
        method.upper() == "PUT"
        and status == 503
        and path.startswith("/providers/proxies/")
        and path.count("/") == 3
    ):
        # PUT /providers/proxies/{provider}: any provider.Update() error.
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("message"), str):
            message = payload["message"]
            if _PROVIDER_PARSE_ERROR.match(message):
                error_type = MihomoProviderUpdateRejected
    error = error_type(f"{method.upper()} {path} failed: HTTP {status} {reason}".rstrip())
    if message is not None:
        # Kept off the text, which stays as it was for every lane.
        error.controller_message = message
    return error


MihomoSelectionMode = Literal["selected", "fixed"]
MihomoSelectionCommitState = Literal["committed", "not_committed", "unknown"]
MihomoSelectionRequestOutcome = Literal["succeeded", "failed"]


@dataclass(frozen=True, slots=True)
class MihomoSelectionCommit:
    """Observed result of one selector PUT followed by one authoritative GET.

    Mihomo may apply a fallback selection before the PUT response is returned,
    so a client-side timeout does not prove that the mutation failed.  The
    commit state is therefore derived only from the subsequent group readback.
    """

    group: str
    target: str
    mode: MihomoSelectionMode
    commit_state: MihomoSelectionCommitState
    request_outcome: MihomoSelectionRequestOutcome
    now: str | None
    fixed: str | None
    fixed_observed: bool
    request_error: str | None = None
    readback_error: str | None = None


class MihomoAPIClient:
    """Thin synchronous client for the Mihomo external controller.

    All one-shot methods return parsed JSON (dict/list). Streaming methods
    yield decoded JSON lines until the caller stops iterating or calls
    ``stop_streams()``.
    """

    def __init__(
        self,
        *,
        base_url: str,
        secret: str = "",
        timeout: float = 3.0,
        opener: UrlOpen = urlopen,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.secret = secret or ""
        self.timeout = float(timeout)
        self.opener = opener
        self._lock = threading.RLock()
        self._active_streams: list[tuple[Any, object | None]] = []

    # ---------------------------------------------------------------- core

    def _headers(self, *, accept_json: bool = True) -> dict[str, str]:
        headers: dict[str, str] = {}
        if accept_json:
            headers["Accept"] = "application/json"
            headers["Content-Type"] = "application/json"
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        timeout: float | None = None,
        connection_abort_sink: ConnectionAbortSink | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode({k: v for k, v in query.items() if v is not None})}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(url, data=body, headers=self._headers(), method=method.upper())
        effective_timeout = self.timeout if timeout is None else float(timeout)
        if connection_abort_sink is not None:
            return self._request_with_closable_connection(
                method,
                path,
                url=url,
                body=body,
                effective_timeout=effective_timeout,
                connection_abort_sink=connection_abort_sink,
            )
        try:
            with self.opener(request, timeout=effective_timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            try:
                detail = exc.read(4096)
            except Exception:
                detail = b""
            finally:
                exc.close()
            raise _http_failure(method, path, exc.code, str(exc.reason), detail) from exc
        except Exception as exc:  # urllib.error.URLError, socket.timeout, ...
            raise MihomoAPIError(f"{method.upper()} {path} failed: {exc}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return raw.decode("utf-8", errors="replace")

    def _request_with_closable_connection(
        self,
        method: str,
        path: str,
        *,
        url: str,
        body: bytes | None,
        effective_timeout: float,
        connection_abort_sink: ConnectionAbortSink,
    ) -> Any:
        """Run one controller request through an explicitly closable transport.

        ``urllib`` does not expose its connection before the blocking request
        begins.  Runtime probes with an absolute execution deadline therefore
        opt into this path: the newly-created connection's ``close`` callback
        is published to ``connection_abort_sink`` *before* ``request()`` can
        dial.  The sink may register that callback with a probe execution
        context and mark the real socket attempt as started in the same call.

        Calls that do not provide a sink retain the historical ``opener``
        behavior, including injected/custom openers used by diagnostics and
        tests.
        """

        parsed = urlsplit(url)
        hostname = parsed.hostname
        if not hostname or parsed.scheme not in {"http", "https"}:
            raise MihomoAPIError(
                f"{method.upper()} {path} failed: unsupported controller URL"
            )
        connection_type: type[http.client.HTTPConnection]
        if parsed.scheme == "https":
            connection_type = http.client.HTTPSConnection
        else:
            connection_type = http.client.HTTPConnection
        connection = connection_type(
            hostname,
            port=parsed.port,
            timeout=effective_timeout,
        )
        try:
            # This callback is intentionally outside the transport exception
            # wrapper.  Cancellation/admission exceptions raised by a runtime
            # probe sink are control-plane outcomes, not Mihomo failures.
            connection_abort_sink(connection.close)
            target = parsed.path or "/"
            if parsed.query:
                target = f"{target}?{parsed.query}"
            try:
                connection.request(
                    method.upper(),
                    target,
                    body=body,
                    headers=self._headers(),
                )
                response = connection.getresponse()
                raw = response.read()
                status = int(getattr(response, "status", 200) or 200)
                reason = str(getattr(response, "reason", "") or "").strip()
                if not 200 <= status < 300:
                    raise _http_failure(method, path, status, reason, raw)
            except MihomoAPIError:
                raise
            except Exception as exc:  # socket.timeout, OSError, HTTPException, ...
                raise MihomoAPIError(
                    f"{method.upper()} {path} failed: {exc}"
                ) from exc
        finally:
            connection.close()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return raw.decode("utf-8", errors="replace")

    # ---------------------------------------------------------- one-shot

    def version(self) -> dict[str, Any]:
        return self._request("GET", "/version")

    def memory(self) -> dict[str, Any]:
        return self._request("GET", "/memory")

    def configs(self) -> dict[str, Any]:
        return self._request("GET", "/configs")

    def reload_config(self, config_path: str, *, force: bool = True) -> dict[str, Any]:
        """Hot-reload a new runtime config file without restarting the core."""
        query = {"force": "true" if force else "false"}
        return self._request("PUT", "/configs", payload={"path": config_path, "payload": ""}, query=query) or {"ok": True}

    def patch_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Partial-update the running config (e.g. {'mode': 'rule'})."""
        return self._request("PATCH", "/configs", payload=patch) or {"ok": True}

    def proxies(self, *, timeout: float | None = None) -> dict[str, Any]:
        return self._request("GET", "/proxies", timeout=timeout)

    def proxy(
        self,
        name: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/proxies/{quote(name, safe='')}",
            timeout=timeout,
        )

    def select_proxy(
        self,
        group_name: str,
        proxy_name: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self._request(
            "PUT",
            f"/proxies/{quote(group_name, safe='')}",
            payload={"name": proxy_name},
            timeout=timeout,
        )
        return {"group": group_name, "selected": proxy_name}

    def select_proxy_with_readback(
        self,
        group_name: str,
        proxy_name: str,
        *,
        mode: MihomoSelectionMode = "selected",
        timeout: float | None = None,
        readback_timeout: float | None = None,
        deadline_monotonic: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> MihomoSelectionCommit:
        """Issue exactly one selection PUT and always read the group back once.

        No retry is performed.  In ``fixed`` mode both ``now`` and ``fixed``
        must name the target.  A successful readback that contradicts the
        target is ``not_committed``; an unavailable or incomplete readback is
        ``unknown`` even when the PUT itself returned successfully.
        """
        if mode not in {"selected", "fixed"}:
            raise ValueError(f"unsupported Mihomo selection mode: {mode!r}")

        monotonic = clock if callable(clock) else time.monotonic

        def remaining_timeout(requested: float | None) -> float | None:
            if deadline_monotonic is None:
                return requested
            remaining = max(0.0, float(deadline_monotonic) - monotonic())
            return remaining if requested is None else min(float(requested), remaining)

        request_outcome: MihomoSelectionRequestOutcome = "succeeded"
        request_error: str | None = None
        put_timeout = remaining_timeout(timeout)
        if put_timeout is not None and put_timeout <= 0.0:
            request_outcome = "failed"
            request_error = "selection deadline expired before PUT"
        else:
            try:
                self.select_proxy(
                    group_name,
                    proxy_name,
                    timeout=put_timeout,
                )
            except MihomoAPIError as exc:
                # An interrupted PUT is an ambiguous mutation, not proof that
                # the controller rejected it. Continue to one bounded readback.
                request_outcome = "failed"
                request_error = str(exc)

        readback_timeout = timeout if readback_timeout is None else readback_timeout
        readback_timeout = remaining_timeout(readback_timeout)
        if readback_timeout is not None and readback_timeout <= 0.0:
            return MihomoSelectionCommit(
                group=group_name,
                target=proxy_name,
                mode=mode,
                commit_state="unknown",
                request_outcome=request_outcome,
                now=None,
                fixed=None,
                fixed_observed=False,
                request_error=request_error,
                readback_error="selection deadline expired before readback",
            )
        try:
            snapshot = self.proxy(group_name, timeout=readback_timeout)
        except MihomoAPIError as exc:
            return MihomoSelectionCommit(
                group=group_name,
                target=proxy_name,
                mode=mode,
                commit_state="unknown",
                request_outcome=request_outcome,
                now=None,
                fixed=None,
                fixed_observed=False,
                request_error=request_error,
                readback_error=str(exc),
            )

        if not isinstance(snapshot, dict):
            return MihomoSelectionCommit(
                group=group_name,
                target=proxy_name,
                mode=mode,
                commit_state="unknown",
                request_outcome=request_outcome,
                now=None,
                fixed=None,
                fixed_observed=False,
                request_error=request_error,
                readback_error="group readback was not a JSON object",
            )

        raw_now = snapshot.get("now")
        now = raw_now if isinstance(raw_now, str) and raw_now else None
        fixed_observed = "fixed" in snapshot
        raw_fixed = snapshot.get("fixed")
        fixed = raw_fixed if isinstance(raw_fixed, str) and raw_fixed else None

        if now is None:
            commit_state: MihomoSelectionCommitState = "unknown"
        elif mode == "selected":
            commit_state = "committed" if now == proxy_name else "not_committed"
        elif now != proxy_name:
            commit_state = "not_committed"
        elif not fixed_observed:
            # Older/incompatible controller schemas cannot prove that the
            # fallback was fixed rather than merely selecting the same member.
            commit_state = "unknown"
        else:
            commit_state = "committed" if fixed == proxy_name else "not_committed"

        return MihomoSelectionCommit(
            group=group_name,
            target=proxy_name,
            mode=mode,
            commit_state=commit_state,
            request_outcome=request_outcome,
            now=now,
            fixed=fixed,
            fixed_observed=fixed_observed,
            request_error=request_error,
            readback_error=None,
        )

    def unfix_proxy(
        self,
        group_name: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Release a non-selector group's temporary fixed member."""
        self._request(
            "DELETE",
            f"/proxies/{quote(group_name, safe='')}",
            timeout=timeout,
        )
        return {"group": group_name, "fixed": False}

    def proxy_delay(
        self,
        name: str,
        *,
        url: str,
        timeout_ms: int = 5000,
        expected_status: str | int | None = None,
        request_timeout: float | None = None,
        connection_abort_sink: ConnectionAbortSink | None = None,
        verify_expected_status: bool = False,
    ) -> dict[str, Any]:
        with _delay_window(getattr(self, "base_url", ""), name):
            return self._proxy_delay_unrecorded(
                name,
                url=url,
                timeout_ms=timeout_ms,
                expected_status=expected_status,
                request_timeout=request_timeout,
                connection_abort_sink=connection_abort_sink,
                verify_expected_status=verify_expected_status,
            )

    def _proxy_delay_unrecorded(
        self,
        name: str,
        *,
        url: str,
        timeout_ms: int,
        expected_status: str | int | None,
        request_timeout: float | None,
        connection_abort_sink: ConnectionAbortSink | None,
        verify_expected_status: bool,
    ) -> dict[str, Any]:
        effective_timeout = (
            max(self.timeout, (timeout_ms / 1000.0) + 2.0)
            if request_timeout is None
            else max(0.1, float(request_timeout))
        )
        deadline = time.monotonic() + effective_timeout
        result = self._request(
            "GET",
            f"/proxies/{quote(name, safe='')}/delay",
            query={
                "url": url,
                "timeout": int(timeout_ms),
                # Mihomo's controller calls this query parameter `expected`,
                # while generated proxy-group YAML uses `expected-status`.
                "expected": expected_status,
            },
            timeout=effective_timeout,
            connection_abort_sink=connection_abort_sink,
        )
        if verify_expected_status and expected_status is not None:
            # URLTest can return a positive delay for an unexpected HTTP status.
            # Only its per-URL alive state contains the expected-status verdict.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MihomoAPIError("proxy delay status readback deadline expired")
            state = self._request(
                "GET", f"/proxies/{quote(name, safe='')}",
                timeout=remaining, connection_abort_sink=connection_abort_sink,
            )
            extra = state.get("extra") if isinstance(state, dict) else None
            verdict = extra.get(url) if isinstance(extra, dict) else None
            alive = verdict.get("alive") if isinstance(verdict, dict) else None
            if alive is False:
                raise MihomoDelayStatusMismatch("proxy URL test did not satisfy its expected status")
            if alive is not True:
                raise MihomoAPIError("proxy delay expected-status readback is unavailable")
        return result

    def group_delay(
        self,
        group: str,
        *,
        url: str,
        timeout_ms: int = 5000,
        request_timeout: float | None = None,
        connection_abort_sink: ConnectionAbortSink | None = None,
        expected_status: str | int | None = None,
    ) -> dict[str, Any]:
        """Run one native round over every member of ``group``.

        ``expected_status`` is sent as Mihomo's ``expected`` query parameter
        only when given, so a round can use the owning group's exact
        contract.  Unlike ``/proxies/{name}/delay``, a group round runs on
        the request's own context (design v3 Conf-6): a ``timeout_ms``
        shorter than the round trip writes every healthy member dead, and a
        client disconnect mid-round writes every member dead.  Callers should
        pass the group's native timeout and a ``request_timeout`` above it.
        """
        with _delay_window(getattr(self, "base_url", ""), f"group:{group}"):
            return self._request(
                "GET",
                f"/group/{quote(group, safe='')}/delay",
                query={
                    "url": url,
                    "timeout": int(timeout_ms),
                    "expected": expected_status,
                },
                timeout=(
                    max(self.timeout, (timeout_ms / 1000.0) + 2.0)
                    if request_timeout is None
                    else max(0.1, float(request_timeout))
                ),
                connection_abort_sink=connection_abort_sink,
            )

    def rules(self) -> dict[str, Any]:
        return self._request("GET", "/rules")

    def providers_proxies(self, *, timeout: float | None = None) -> dict[str, Any]:
        """List every proxy provider (``GET /providers/proxies``).

        This read is not free of side effects.  The listing includes each
        group's compatible provider, and marshalling a group resolves its
        current pick, which clears a dead ``fixed`` member of a fallback
        group (design v3 Conf-1, 5 of 5).  Runtime judgement reads use
        :meth:`provider_proxies_one` instead.
        """
        return self._request("GET", "/providers/proxies", timeout=timeout)

    def provider_proxies_one(
        self,
        name: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Read one proxy provider (``GET /providers/proxies/{name}``).

        For a provider whose members are leaves (``TIER3B-RUNTIME``) this is
        the side-effect-free provider read (design v3 Conf-1, 5 of 5): no
        group is marshalled, so no group's ``fixed`` pick is cleared.  Its
        ``testUrl`` is ``""`` and ``expectedStatus`` is ``"*"``, so a probe
        contract must come from the owning group, not from this payload.
        """
        return self._request(
            "GET",
            f"/providers/proxies/{quote(name, safe='')}",
            timeout=timeout,
        )

    def proxy_provider(
        self,
        provider_name: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Read one proxy-provider's current controller representation."""
        return self._request(
            "GET",
            f"/providers/proxies/{quote(provider_name, safe='')}",
            timeout=timeout,
        )

    def update_proxy_provider(
        self,
        provider_name: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Ask Mihomo to refresh an already provider-backed proxy inventory."""
        self._request(
            "PUT",
            f"/providers/proxies/{quote(provider_name, safe='')}",
            timeout=timeout,
        )
        return {"provider": provider_name, "updated": True}

    def provider_member_healthcheck(
        self,
        provider_name: str,
        member_name: str,
        *,
        url: str,
        timeout_ms: int = 5000,
        expected_status: str | int | None = None,
        request_timeout: float | None = None,
    ) -> dict[str, Any]:
        """Test one provider member (``GET /providers/proxies/{p}/{m}/healthcheck``).

        Mihomo resolves the member inside that provider (deterministic even
        when another provider or the main config uses the same name) and runs
        URLTest directly on its current object, outside the provider's 1 s
        singledo, so it also tests right after a rebuild whose own check was
        skipped (measured on 1.19.27).  The verdict lands in the member's
        ``extra[url]``; the HTTP answer is not the verdict (200 with a delay
        when the expected status failed).  503/504 raise the same
        ``MihomoDelayTestFailed`` subclasses as ``/proxies/{name}/delay``.
        """
        with _delay_window(getattr(self, "base_url", ""), member_name):
            return self._request(
                "GET",
                f"/providers/proxies/{quote(provider_name, safe='')}"
                f"/{quote(member_name, safe='')}/healthcheck",
                query={
                    "url": url,
                    "timeout": int(timeout_ms),
                    "expected": expected_status,
                },
                timeout=(
                    max(self.timeout, (timeout_ms / 1000.0) + 2.0)
                    if request_timeout is None
                    else max(0.1, float(request_timeout))
                ),
            )

    def providers_rules(self) -> dict[str, Any]:
        return self._request("GET", "/providers/rules")

    def health_check_provider(
        self,
        provider_name: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        with _delay_window(getattr(self, "base_url", ""), f"provider:{provider_name}"):
            self._request(
                "GET",
                f"/providers/proxies/{quote(provider_name, safe='')}/healthcheck",
                timeout=max(self.timeout, 10.0) if timeout is None else max(0.1, float(timeout)),
            )
        return {"provider": provider_name, "triggered": True}

    def connections(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Snapshot of current connections (non-streaming)."""
        return self._request("GET", "/connections", timeout=timeout)

    def close_connection(
        self,
        connection_id: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self._request(
            "DELETE",
            f"/connections/{quote(connection_id, safe='')}",
            timeout=timeout,
        )
        return {"closed": connection_id}

    def close_all_connections(self) -> dict[str, Any]:
        self._request("DELETE", "/connections")
        return {"closed": "all"}

    def dns_query(self, name: str, dns_type: str = "A") -> dict[str, Any]:
        return self._request("GET", "/dns/query", query={"name": name, "type": dns_type})

    # ---------------------------------------------------------- streaming

    @contextmanager
    def _open_stream(
        self,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        timeout: float | None = None,
        owner: object | None = None,
    ) -> Iterator[Any]:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode({k: v for k, v in query.items() if v is not None})}"
        request = Request(url, headers=self._headers(accept_json=True), method="GET")
        effective_timeout = self.timeout if timeout is None else max(0.1, float(timeout))
        response = self.opener(request, timeout=effective_timeout)
        with self._lock:
            self._active_streams.append((response, owner))
        try:
            yield response
        finally:
            with self._lock:
                self._active_streams = [
                    (active_response, active_owner)
                    for active_response, active_owner in self._active_streams
                    if active_response is not response
                ]
            try:
                response.close()
            except Exception:
                pass

    def stream_traffic(self, *, owner: object | None = None) -> Iterator[dict[str, Any]]:
        """Yield {'up': bytes/s, 'down': bytes/s} samples until the caller breaks."""
        with self._open_stream("/traffic", timeout=STREAM_READ_TIMEOUT_SECONDS, owner=owner) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip() if isinstance(raw_line, (bytes, bytearray)) else str(raw_line).strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def stream_logs(
        self,
        level: str = "info",
        *,
        owner: object | None = None,
        on_open: Callable[[], None] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield log records and optionally report when the HTTP stream opens."""
        with self._open_stream("/logs", query={"level": level}, timeout=STREAM_READ_TIMEOUT_SECONDS, owner=owner) as response:
            if on_open is not None:
                on_open()
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip() if isinstance(raw_line, (bytes, bytearray)) else str(raw_line).strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    yield {"type": level, "payload": line}

    def stream_connections(self, *, owner: object | None = None) -> Iterator[dict[str, Any]]:
        """Yield periodic connection snapshots with per-connection up/down bytes."""
        with self._open_stream("/connections", timeout=STREAM_READ_TIMEOUT_SECONDS, owner=owner) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip() if isinstance(raw_line, (bytes, bytearray)) else str(raw_line).strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def stop_streams(self, *, owner: object | None = None) -> None:
        with self._lock:
            if owner is None:
                streams = [response for response, _owner in self._active_streams]
                self._active_streams.clear()
            else:
                streams = [
                    response
                    for response, active_owner in self._active_streams
                    if active_owner is owner
                ]
                self._active_streams = [
                    (response, active_owner)
                    for response, active_owner in self._active_streams
                    if active_owner is not owner
                ]
        for response in streams:
            try:
                response.close()
            except Exception:
                pass


class MihomoControllerKeepAlive:
    """One persistent controller connection for the post-dial observer.

    The observer polls ``/connections`` up to every 100 ms while user
    connections open (design v3 §18.4), so it keeps a single HTTP/1.1
    socket instead of dialling per request (risk #4).  The client starts no
    thread and no timer.  Each blocking phase (connect, send, each receive)
    is bounded by the call's ``timeout_seconds``; this is not a total
    deadline.

    A failed call discards the socket and the next call dials a fresh one.
    When a reused socket turns out to have been closed by the controller,
    the call is retried once on a fresh connection; a timeout or a refused
    dial is never retried.  A body larger than ``max_body_bytes`` is not
    parsed: the call returns ``None`` and closes the socket instead of
    draining it.

    Calls from several threads are serialised.  ``close()`` may be called
    from any thread; an in-flight call then raises instead of retrying.  On
    Windows, closing may not interrupt a receive that is already blocked in
    ``http.client``, so that call still ends within its phase timeout; the
    raw socket of ``connections_raw()`` is shut down and closed, which ends
    a blocked receive at once.

    ``connections_raw()`` returns the body undecoded, over its own plain
    socket with the same rules (one socket, one retry of a stale reused
    socket, per-phase timeouts, the body cap); it skips ``http.client`` and
    ``json.loads``, which cost the observer more than the rest of a poll.
    Its receive buffer is kept between calls; after a failed, oversized or
    much smaller response a grown buffer is given back.
    """

    MAX_BODY_BYTES = KEEPALIVE_MAX_BODY_BYTES
    PHASE_TIMEOUT_SECONDS = KEEPALIVE_PHASE_TIMEOUT_SECONDS

    def __init__(
        self,
        *,
        base_url: str,
        secret: str = "",
        max_body_bytes: int = KEEPALIVE_MAX_BODY_BYTES,
    ) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if not parsed.hostname or parsed.scheme not in {"http", "https"}:
            raise ValueError("unsupported controller URL")
        self._https = parsed.scheme == "https"
        self._host = parsed.hostname
        self._port = parsed.port
        self._path_prefix = parsed.path.rstrip("/")
        self._headers = {"Accept": "application/json"}
        if secret:
            self._headers["Authorization"] = f"Bearer {secret}"
        self._max_body_bytes = max(1, int(max_body_bytes))
        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._connection: http.client.HTTPConnection | None = None
        self._raw_socket: socket.socket | None = None
        self._raw_socket_timeout: float | None = None
        self._raw_buffer: bytearray | None = None
        self._raw_request = self._build_raw_request("/connections")
        self._generation = 0
        self._connect_count = 0
        self._request_count = 0
        self._last_body_bytes = 0

    @classmethod
    def from_client(
        cls,
        client: MihomoAPIClient,
        *,
        max_body_bytes: int = KEEPALIVE_MAX_BODY_BYTES,
    ) -> MihomoControllerKeepAlive:
        """Build a keep-alive client for the same controller and secret."""
        return cls(
            base_url=client.base_url,
            secret=client.secret,
            max_body_bytes=max_body_bytes,
        )

    @property
    def connect_count(self) -> int:
        """Sockets dialled so far (1 while one connection is reused)."""
        return self._connect_count

    @property
    def request_count(self) -> int:
        """Requests sent so far, retries included."""
        return self._request_count

    @property
    def last_body_bytes(self) -> int:
        """Body bytes of the last 2xx response.

        For an oversized body this is its declared length, or
        ``max_body_bytes + 1`` when a chunked body was cut off.
        """
        return self._last_body_bytes

    def connections(
        self,
        *,
        timeout_seconds: float = KEEPALIVE_PHASE_TIMEOUT_SECONDS,
    ) -> dict[str, Any] | None:
        """Return the parsed ``GET /connections`` snapshot.

        Returns ``None`` when the body exceeds ``max_body_bytes``.  Raises
        :class:`MihomoAPIError` on a transport failure, a non-2xx status or
        a body that is not a JSON object; the message never quotes the body.
        """
        return self._get_json_object(
            "/connections",
            label="/connections",
            timeout_seconds=timeout_seconds,
        )

    def connections_raw(
        self,
        *,
        timeout_seconds: float = KEEPALIVE_PHASE_TIMEOUT_SECONDS,
    ) -> bytes | None:
        """Return the ``GET /connections`` body as raw bytes, not decoded.

        Returns ``None`` when the body exceeds ``max_body_bytes``.  Raises
        :class:`MihomoAPIError` on a transport failure or a non-2xx status;
        the body itself is not checked (the caller decodes it), and no
        message quotes it.
        """
        if self._raw_request is None:
            # HTTPS, or a request line http.client must validate and reject.
            return self._get_body(
                "/connections",
                label="/connections",
                timeout_seconds=timeout_seconds,
            )
        return self._raw_get(label="/connections", timeout_seconds=timeout_seconds)

    def close(self) -> None:
        """Drop the socket.  Idempotent; the next call dials a fresh one."""
        with self._state_lock:
            connection = self._connection
            self._connection = None
            raw_socket = self._raw_socket
            self._raw_socket = None
            self._generation += 1
        if connection is not None:
            _abort_http_connection(connection)
        if raw_socket is not None:
            _abort_socket(raw_socket)

    def __enter__(self) -> MihomoControllerKeepAlive:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _get_json_object(
        self,
        path: str,
        *,
        label: str,
        timeout_seconds: float,
    ) -> dict[str, Any] | None:
        raw = self._get_body(path, label=label, timeout_seconds=timeout_seconds)
        if raw is None:
            return None
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            raise MihomoAPIError(f"GET {label} failed: body is not JSON") from None
        if not isinstance(payload, dict):
            raise MihomoAPIError(f"GET {label} failed: body is not a JSON object")
        return payload

    def _get_body(
        self,
        path: str,
        *,
        label: str,
        timeout_seconds: float,
    ) -> bytes | None:
        # ``label`` is the only request text an error message may carry.
        timeout = max(0.001, float(timeout_seconds))
        target = f"{self._path_prefix}{path}"
        with self._request_lock:
            retried = False
            while True:
                connection, generation, reused = self._checkout(timeout)
                self._request_count += 1
                try:
                    status, reason, raw = self._exchange(connection, target)
                except _KEEPALIVE_STALE_SOCKET_ERRORS as exc:
                    self._discard(connection)
                    if reused and not retried and generation == self._generation:
                        retried = True
                        continue
                    raise MihomoAPIError(f"GET {label} failed: {exc}") from exc
                except Exception as exc:  # socket.timeout, OSError, HTTPException, ...
                    self._discard(connection)
                    raise MihomoAPIError(f"GET {label} failed: {exc}") from exc
                break
            if generation != self._generation:
                self._discard(connection)
                raise MihomoAPIError(f"GET {label} failed: controller connection closed")
            if raw is None:
                # Error status or oversized body: the rest of the response
                # was not read, so the socket cannot carry another request.
                self._discard(connection)
            if not 200 <= status < 300:
                raise _http_failure("GET", label, status, reason, b"")
        return raw

    # -- raw socket transport (``connections_raw``) --------------------------

    def _build_raw_request(self, path: str) -> bytes | None:
        """The request ``http.client`` would send, or None to leave it to it."""
        if self._https:
            return None
        target = f"{self._path_prefix}{path}"
        if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in target):
            return None
        try:
            host = self._host.encode("ascii").decode("ascii")
        except UnicodeEncodeError:
            try:
                host = self._host.encode("idna").decode("ascii")
            except UnicodeError:
                return None
        if ":" in host:
            host = f"[{host}]"
        if self._port is not None and self._port != http.client.HTTP_PORT:
            host = f"{host}:{self._port}"
        lines = [f"GET {target} HTTP/1.1", f"Host: {host}", "Accept-Encoding: identity"]
        for name, value in self._headers.items():
            if any(char in value for char in "\r\n\x00"):
                return None
            lines.append(f"{name}: {value}")
        try:
            return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        except UnicodeEncodeError:
            return None

    def _raw_get(self, *, label: str, timeout_seconds: float) -> bytes | None:
        timeout = max(0.001, float(timeout_seconds))
        with self._request_lock:
            retried = False
            while True:
                with self._state_lock:
                    sock = self._raw_socket
                    generation = self._generation
                reused = sock is not None
                self._request_count += 1
                try:
                    if sock is None:
                        self._connect_count += 1
                        sock = self._raw_dial(timeout, generation)
                    if self._raw_socket_timeout != timeout:
                        _set_phase_timeout(sock, timeout)
                        self._raw_socket_timeout = timeout
                    status, reason, body, reusable = self._raw_exchange(sock)
                except _KEEPALIVE_STALE_SOCKET_ERRORS as exc:
                    if sock is not None:
                        self._raw_discard(sock)
                    if reused and not retried and generation == self._generation:
                        retried = True
                        continue
                    raise MihomoAPIError(f"GET {label} failed: {exc}") from exc
                except Exception as exc:  # socket.timeout, OSError, HTTPException, ...
                    if sock is not None:
                        self._raw_discard(sock)
                    raise MihomoAPIError(f"GET {label} failed: {exc}") from exc
                break
            if generation != self._generation:
                self._raw_discard(sock)
                raise MihomoAPIError(f"GET {label} failed: controller connection closed")
            if not reusable:
                # Error status, oversized or unframed body, or ``Connection:
                # close``: the socket cannot carry another request.
                self._raw_discard(sock)
            if not 200 <= status < 300:
                raise _http_failure("GET", label, status, reason, b"")
        return body

    def _raw_dial(self, timeout: float, generation: int) -> socket.socket:
        sock = socket.create_connection((self._host, self._port or http.client.HTTP_PORT), timeout)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        with self._state_lock:
            if generation == self._generation and self._raw_socket is None:
                self._raw_socket = sock
                self._raw_socket_timeout = None
                return sock
        _abort_socket(sock)
        raise OSError("controller connection closed")

    def _raw_discard(self, sock: socket.socket) -> None:
        with self._state_lock:
            if self._raw_socket is sock:
                self._raw_socket = None
        _abort_socket(sock)

    def _raw_exchange(self, sock: socket.socket) -> tuple[int, str, bytes | None, bool]:
        """Send the GET; return (status, reason, body or None, socket reusable)."""
        request = self._raw_request
        assert request is not None
        sock.sendall(request)
        if self._raw_buffer is None:
            self._raw_buffer = bytearray(_KEEPALIVE_RAW_BUFFER_BYTES)
        reader = _RawReader(sock, self._raw_buffer)
        result: tuple[int, str, bytes | None, bool] | None = None
        try:
            result = self._raw_response(reader)
            return result
        finally:
            buffer = reader.buffer
            reader.release()
            body = None if result is None else result[2]
            if len(buffer) > _KEEPALIVE_RAW_BUFFER_BYTES and (body is None or 4 * len(body) < len(buffer)):
                # Grown for a body this call did not return (an error, an
                # oversized or cut-off body) or for a much larger one than
                # now: the next call starts from the initial size again.
                self._raw_buffer = None
            else:
                self._raw_buffer = buffer

    def _raw_response(self, reader: _RawReader) -> tuple[int, str, bytes | None, bool]:
        start = 0
        while True:
            head_end = reader.buffer.find(b"\r\n\r\n", start, reader.end)
            while head_end < 0:
                if reader.end - start > _KEEPALIVE_HEAD_LIMIT_BYTES:
                    raise http.client.HTTPException("response head too long")
                search_from = max(start, reader.end - 3)
                if not reader.more():
                    if reader.end == 0:
                        raise http.client.RemoteDisconnected(
                            "Remote end closed connection without response"
                        )
                    raise http.client.HTTPException("incomplete response head")
                head_end = reader.buffer.find(b"\r\n\r\n", search_from, reader.end)
            status, reason, version, headers = _parse_response_head(bytes(reader.view[start:head_end]))
            start = head_end + 4
            if 100 <= status < 200:
                continue  # interim response; the real one follows
            break
        if not 200 <= status < 300:
            return status, reason, None, False
        connection = headers.get(b"connection", b"").lower()
        if version == b"HTTP/1.1":
            will_close = b"close" in connection
        else:
            will_close = not (b"keep-alive" in connection or headers.get(b"keep-alive"))
        if status in (204, 304):
            self._last_body_bytes = 0
            return status, reason, b"", not will_close and reader.end == start
        body: bytes | None
        if headers.get(b"transfer-encoding", b"").lower() == b"chunked":
            body, end = self._raw_read_chunked(reader, start)
        else:
            length: int | None = None
            declared = headers.get(b"content-length")
            if declared is not None:
                try:
                    length = int(declared)
                except ValueError:
                    length = None
                if length is not None and length < 0:
                    length = None
            if length is None:
                body = self._raw_read_to_close(reader, start)
                end = reader.end
                will_close = True
            elif length > self._max_body_bytes:
                self._last_body_bytes = length
                return status, reason, None, False
            else:
                if not reader.fill(start + length):
                    raise http.client.HTTPException("response body did not complete")
                end = start + length
                body = bytes(reader.view[start:end])
        if body is None:
            return status, reason, None, False
        self._last_body_bytes = len(body)
        return status, reason, body, not will_close and reader.end == end

    def _raw_read_to_close(self, reader: _RawReader, start: int) -> bytes | None:
        limit = self._max_body_bytes
        while reader.end - start <= limit:
            if not reader.more():
                return bytes(reader.view[start : reader.end])
        self._last_body_bytes = limit + 1
        return None

    def _raw_read_chunked(self, reader: _RawReader, pos: int) -> tuple[bytes | None, int]:
        limit = self._max_body_bytes
        parts: list[bytes] = []
        total = 0
        while True:
            line = _raw_line_end(reader, pos)
            size_text = bytes(reader.view[pos:line]).split(b";", 1)[0].strip()
            try:
                size = int(size_text, 16)
            except ValueError:
                raise http.client.HTTPException("bad chunk size") from None
            if size < 0:
                raise http.client.HTTPException("bad chunk size")
            pos = line + 2
            if size == 0:
                while True:  # trailers, up to the empty line
                    line = _raw_line_end(reader, pos)
                    if line == pos:
                        return b"".join(parts), pos + 2
                    pos = line + 2
            if total + size > limit:
                self._last_body_bytes = limit + 1
                return None, pos
            if not reader.fill(pos + size + 2):
                raise http.client.HTTPException("response body did not complete")
            if reader.view[pos + size : pos + size + 2] != b"\r\n":
                raise http.client.HTTPException("bad chunk terminator")
            parts.append(bytes(reader.view[pos : pos + size]))
            pos += size + 2
            total += size

    def _checkout(
        self,
        timeout: float,
    ) -> tuple[http.client.HTTPConnection, int, bool]:
        with self._state_lock:
            connection = self._connection
            if connection is None:
                connection_type: type[http.client.HTTPConnection] = (
                    http.client.HTTPSConnection if self._https else http.client.HTTPConnection
                )
                connection = connection_type(self._host, port=self._port, timeout=timeout)
                self._connection = connection
            generation = self._generation
        # http.client dials on the next request whenever ``sock`` is None,
        # including after a ``Connection: close`` response.
        sock = connection.sock
        connection.timeout = timeout
        if sock is not None:
            try:
                sock.settimeout(timeout)
            except OSError:
                pass
        else:
            self._connect_count += 1
        return connection, generation, sock is not None

    def _exchange(
        self,
        connection: http.client.HTTPConnection,
        target: str,
    ) -> tuple[int, str, bytes | None]:
        """Send one GET; return (status, reason, body or None if unread)."""
        connection.request("GET", target, headers=self._headers)
        response = connection.getresponse()
        status = int(response.status)
        reason = str(response.reason or "").strip()
        if not 200 <= status < 300:
            return status, reason, None
        declared = response.length
        if declared is not None and declared > self._max_body_bytes:
            self._last_body_bytes = declared
            return status, reason, None
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(
                min(_KEEPALIVE_READ_CHUNK_BYTES, self._max_body_bytes + 1 - total)
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > self._max_body_bytes:
                self._last_body_bytes = total
                return status, reason, None
        self._last_body_bytes = total
        if not response.isclosed():
            raise http.client.HTTPException("response body did not complete")
        return status, reason, b"".join(chunks)

    def _discard(self, connection: http.client.HTTPConnection) -> None:
        with self._state_lock:
            if self._connection is connection:
                self._connection = None
        _abort_http_connection(connection)


def _abort_http_connection(connection: http.client.HTTPConnection) -> None:
    sock = connection.sock
    if sock is not None:
        try:
            # Wakes a blocked receive where the platform supports it; the
            # response's file object otherwise defers the real close.
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    try:
        connection.close()
    except Exception:
        pass


def _set_phase_timeout(sock: socket.socket, timeout: float) -> None:
    """Bound every send and receive on ``sock`` by ``timeout`` seconds.

    On Windows the socket goes to blocking mode with kernel SO_RCVTIMEO and
    SO_SNDTIMEO: each receive is then one system call, instead of the
    wait-then-receive pair a Python-level socket timeout costs per read.
    Elsewhere the Python-level timeout is kept.
    """
    if os.name != "nt":
        sock.settimeout(timeout)
        return
    milliseconds = max(1, int(math.ceil(float(timeout) * 1000.0)))
    sock.settimeout(None)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVTIMEO, milliseconds)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, milliseconds)


def _abort_socket(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


class _RawReader:
    """The receive buffer of one raw exchange: ``buffer[:end]`` is received.

    One persistent buffer per client, so a poll costs no allocation or
    zero-fill; each receive takes whatever the kernel holds (up to the free
    space), so a body that arrived whole is read with one system call.
    """

    __slots__ = ("sock", "buffer", "view", "end")

    def __init__(self, sock: socket.socket, buffer: bytearray) -> None:
        self.sock = sock
        self.buffer = buffer
        self.view = memoryview(buffer)
        self.end = 0

    def release(self) -> None:
        self.view.release()

    def fill(self, need: int) -> bool:
        """Receive until at least ``need`` bytes are buffered; False at EOF."""
        while self.end < need:
            if need > len(self.buffer):
                self._grow(need)
            received = self.sock.recv_into(self.view[self.end :], len(self.buffer) - self.end)
            if not received:
                return False
            self.end += received
        return True

    def more(self) -> bool:
        """Receive at least one more byte; False at EOF."""
        if self.end >= len(self.buffer):
            self._grow(self.end + 1)
        return self.fill(self.end + 1)

    def _grow(self, need: int) -> None:
        grown = bytearray(max(need, 2 * len(self.buffer)))
        grown[: self.end] = self.view[: self.end]
        self.view.release()
        self.buffer = grown
        self.view = memoryview(grown)


def _raw_line_end(reader: _RawReader, start: int) -> int:
    """Offset of the CRLF ending the line at ``start``, receiving as needed."""
    end = reader.buffer.find(b"\r\n", start, reader.end)
    while end < 0:
        if reader.end - start > _KEEPALIVE_HEAD_LIMIT_BYTES:
            raise http.client.HTTPException("chunk header too long")
        search_from = max(start, reader.end - 1)
        if not reader.more():
            raise http.client.HTTPException("response body did not complete")
        end = reader.buffer.find(b"\r\n", search_from, reader.end)
    return end


def _parse_response_head(head: bytes) -> tuple[int, str, bytes, dict[bytes, bytes]]:
    """(status, reason, version, lower-cased headers) of one response head."""
    lines = head.split(b"\r\n")
    parts = lines[0].split(None, 2)
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/"):
        raise http.client.HTTPException("bad status line")
    try:
        status = int(parts[1])
    except ValueError:
        raise http.client.HTTPException("bad status line") from None
    if not 100 <= status <= 999:
        raise http.client.HTTPException("bad status line")
    reason = parts[2].decode("latin-1").strip() if len(parts) > 2 else ""
    headers: dict[bytes, bytes] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(b":")
        if not sep:
            continue
        name = name.strip().lower()
        value = value.strip()
        headers[name] = headers[name] + b", " + value if name in headers else value
    return status, reason, parts[0], headers


def mihomo_controller_enabled(controller: dict[str, Any], *, default: bool = True) -> bool:
    return coerce_bool(controller.get("enabled", default), default)


def _mihomo_controller_host(controller: dict[str, Any]) -> str:
    return str(controller.get("host", "127.0.0.1"))


def _mihomo_controller_port(controller: dict[str, Any]) -> int:
    return coerce_port(controller.get("port", 9090), 9090)


def mihomo_controller_address(controller: dict[str, Any]) -> str:
    return f"{_mihomo_controller_host(controller)}:{_mihomo_controller_port(controller)}"


def build_mihomo_controller_base_url(controller: dict[str, Any]) -> str:
    return f"http://{mihomo_controller_address(controller)}"


def mihomo_controller_secret(controller: dict[str, Any]) -> str:
    return str(controller.get("secret", ""))


def mihomo_controller_timeout(controller: dict[str, Any]) -> float:
    return coerce_float(controller.get("timeout", 3), 3.0, minimum=0.1)


def build_client_from_config(client_config: dict[str, Any]) -> MihomoAPIClient | None:
    """Construct a MihomoAPIClient from the config's client.mihomo_controller block.

    Returns None if the controller is disabled.
    """
    controller = client_config.get("mihomo_controller", {}) if isinstance(client_config, dict) else {}
    if not isinstance(controller, dict):
        return None
    if not mihomo_controller_enabled(controller):
        return None
    return MihomoAPIClient(
        base_url=build_mihomo_controller_base_url(controller),
        secret=mihomo_controller_secret(controller),
        timeout=mihomo_controller_timeout(controller),
    )


def hot_reload_kwargs_from_controller(controller: dict[str, Any]) -> dict[str, Any]:
    return {
        "controller_host": _mihomo_controller_host(controller),
        "controller_port": _mihomo_controller_port(controller),
        "controller_secret": mihomo_controller_secret(controller),
        "timeout": mihomo_controller_timeout(controller),
    }
