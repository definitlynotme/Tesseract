"""Tests for the Mihomo external controller client."""
from __future__ import annotations

import http.client
import inspect
import io
import json
import select
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from mihomo_api import (
    KEEPALIVE_MAX_BODY_BYTES,
    KEEPALIVE_PHASE_TIMEOUT_SECONDS,
    MihomoAPIClient,
    MihomoAPIError,
    MihomoControllerKeepAlive,
    MihomoDelayStatusMismatch,
    MihomoDelayTestFailed,
    MihomoDelayTimeout,
    MihomoDelayTransportFailed,
    MihomoSelectionCommit,
    STREAM_READ_TIMEOUT_SECONDS,
    build_mihomo_controller_base_url,
    build_client_from_config,
    hot_reload_kwargs_from_controller,
    mihomo_controller_address,
    mihomo_controller_enabled,
    mihomo_controller_secret,
    mihomo_controller_timeout,
)
from runtime_route_audit import (
    builtin_terminal_kind,
    inspect_runtime_route,
    next_viable_candidate,
)


class FakeResponse:
    def __init__(self, body: bytes = b"", lines: list[bytes] | None = None) -> None:
        self._body = body
        self._lines = lines or []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def read(self):
        return self._body

    def close(self):
        self.closed = True

    def __iter__(self):
        return iter(self._lines)


class FakeOpener:
    def __init__(self, responses: list[FakeResponse | BaseException]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, bytes | None, dict]] = []
        self.timeouts: list[float | None] = []

    def __call__(self, request, timeout=None):  # mimic urlopen signature
        headers = {}
        try:
            headers = dict(request.header_items())
        except Exception:
            pass
        self.calls.append((request.get_method(), request.full_url, request.data, headers))
        self.timeouts.append(timeout)
        if not self.responses:
            raise AssertionError("No FakeResponse queued")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeHTTPClientResponse:
    def __init__(
        self,
        body: bytes = b"",
        *,
        status: int = 200,
        reason: str = "OK",
    ) -> None:
        self._body = body
        self.status = status
        self.reason = reason

    def read(self) -> bytes:
        return self._body


class FakeHTTPConnection:
    def __init__(
        self,
        host: str,
        port: int | None = None,
        timeout: float | None = None,
        *,
        response: FakeHTTPClientResponse | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.response = response or FakeHTTPClientResponse()
        self.events = events if events is not None else []
        self.closed = False
        self.request_call: tuple[str, str, bytes | None, dict[str, str]] | None = None

    def request(
        self,
        method: str,
        target: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.events.append("request")
        self.request_call = (method, target, body, dict(headers or {}))

    def getresponse(self) -> FakeHTTPClientResponse:
        self.events.append("getresponse")
        return self.response

    def close(self) -> None:
        self.events.append("close")
        self.closed = True


CONTRACT_URL = "http://www.google.com/generate_204"

# Shape of `GET /providers/proxies/TIER3B-RUNTIME` from the Mihomo v1.19.27
# conformance run (design v3 Conf-c): the provider itself carries no contract.
CONF_C_PROVIDER_FIXTURE: dict[str, object] = {
    "name": "TIER3B-RUNTIME",
    "type": "Proxy",
    "vehicleType": "File",
    "testUrl": "",
    "expectedStatus": "*",
    "updatedAt": "2026-09-26T11:38:15.5910014-04:00",
    "proxies": [
        {
            "name": name,
            "type": proxy_type,
            "alive": alive,
            "history": [],
            "extra": {
                CONTRACT_URL: {
                    "alive": alive,
                    "history": [{"time": "2026-09-26T11:38:17.5057777-04:00", "delay": 0}],
                }
            },
        }
        for name, proxy_type, alive in (
            ("n1", "Socks5", True),
            ("n2", "Socks5", True),
            ("n3", "Socks5", True),
            ("REJECT", "Reject", False),
        )
    ],
}


def _delay_http_error(status: int, body: bytes) -> HTTPError:
    return HTTPError(
        "http://127.0.0.1:9090/proxies/n1/delay",
        status,
        "failure",
        {},  # type: ignore[arg-type]
        io.BytesIO(body),
    )


class _FakeControllerHandler(BaseHTTPRequestHandler):
    """Scripted Mihomo controller: one queued action per GET request."""

    protocol_version = "HTTP/1.1"
    timeout = 5

    server: _FakeControllerServer

    def setup(self) -> None:
        super().setup()
        with self.server.lock:
            self.server.accepted += 1

    def handle(self) -> None:
        try:
            super().handle()
        finally:
            # The handler socket times out after 5 s, so an exit well before
            # that means the client closed its end.
            with self.server.disconnect_changed:
                self.server.disconnects += 1
                self.server.disconnect_changed.notify_all()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        server = self.server
        with server.lock:
            server.requests.append((self.path, self.headers.get("Authorization")))
            action = server.actions.pop(0) if server.actions else ("json", {"connections": []})
        kind = action[0]
        if kind == "stall":
            server.release.wait(5.0)
            self.close_connection = True
            return
        if kind == "drop":
            # Close without a status line, as a controller that dies while
            # answering looks to the client.
            self.close_connection = True
            return
        if kind == "delayed":
            # Answer after action[1] seconds, or at once when the client
            # closes its end first, and record which of the two happened.
            server.reply_pending.set()
            if _peer_closed_within(self.connection, float(action[1])):
                server.eof_before_reply.set()
            try:
                self._send_body(json.dumps(action[2]).encode())
            except OSError:
                self.close_connection = True
            return
        if kind == "status":
            self._send_body(json.dumps({"message": "Unauthorized"}).encode(), status=action[1])
            return
        body = action[1] if isinstance(action[1], bytes) else json.dumps(action[1]).encode()
        if kind == "chunked_one":
            # One chunk for the whole body and a trailer, as Go's net/http
            # writes a large render.JSON body.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"%x;ext=1\r\n" % len(body) + body + b"\r\n0\r\nX-Trailer: 1\r\n\r\n")
            return
        if kind == "chunked":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for offset in range(0, len(body), 700):
                piece = body[offset:offset + 700]
                self.wfile.write(f"{len(piece):X}\r\n".encode() + piece + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        self._send_body(body, connection_close=kind == "json_close")
        if kind == "json_silent_close":
            # Close after answering without announcing it, as an idle
            # keep-alive socket dropped by the controller looks to a client.
            self.close_connection = True

    def _send_body(self, body: bytes, *, status: int = 200, connection_close: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if connection_close:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


class _FakeControllerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, actions: list[tuple[object, ...]] | None = None) -> None:
        super().__init__(("127.0.0.1", 0), _FakeControllerHandler)
        self.lock = threading.Lock()
        self.actions = list(actions or [])
        self.accepted = 0
        self.requests: list[tuple[str, str | None]] = []
        self.release = threading.Event()
        self.reply_pending = threading.Event()
        self.eof_before_reply = threading.Event()
        self.disconnects = 0
        self.disconnect_changed = threading.Condition()
        self._thread = threading.Thread(
            target=self.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def wait_disconnects(self, count: int, *, timeout: float) -> bool:
        """Wait until ``count`` handler sockets have ended."""
        with self.disconnect_changed:
            return self.disconnect_changed.wait_for(
                lambda: self.disconnects >= count,
                timeout=timeout,
            )

    def handle_error(self, request: object, client_address: object) -> None:
        # Clients in these tests abandon oversized bodies mid-write.
        return

    def __enter__(self) -> _FakeControllerServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release.set()
        self.shutdown()
        self.server_close()
        self._thread.join(2.0)


def _peer_closed_within(sock: socket.socket, seconds: float) -> bool:
    """True when the peer closes ``sock`` within ``seconds``."""
    readable, _, _ = select.select([sock], [], [], seconds)
    if not readable:
        return False
    try:
        return sock.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


def _json_body_of_size(size: int) -> bytes:
    skeleton = b'{"connections":[],"pad":""}'
    body = skeleton[:-2] + b"x" * (size - len(skeleton)) + b'"}'
    assert len(body) == size
    return body


class MihomoAPIClientTests(unittest.TestCase):
    def _client(self, opener: FakeOpener) -> MihomoAPIClient:
        return MihomoAPIClient(
            base_url="http://127.0.0.1:9090",
            secret="my-secret",
            timeout=1.0,
            opener=opener,
        )

    def test_runtime_route_inspector_returns_full_chain_and_leaf(self) -> None:
        route = inspect_runtime_route(
            {
                "proxies": {
                    "FINAL-EXIT": {"type": "Selector", "now": "PROXY-FALLBACK", "all": ["PROXY-FALLBACK", "DIRECT"]},
                    "PROXY-FALLBACK": {"type": "Fallback", "now": "TIER1-SELECT", "all": ["TIER1-SELECT", "TIER2-AUTO"]},
                    "TIER1-SELECT": {"type": "Selector", "now": "edge-select", "all": ["TIER1-AUTO", "edge-select"]},
                    "edge-select": {"type": "Selector", "now": "edge-ss", "all": ["edge-auto", "edge-ss", "edge-vmess"]},
                    "edge-ss": {"type": "Shadowsocks"},
                }
            }
        )

        self.assertEqual(
            route["selected_chain"],
            ["FINAL-EXIT", "PROXY-FALLBACK", "TIER1-SELECT", "edge-select", "edge-ss"],
        )
        self.assertEqual(route["leaf_proxy"], "edge-ss")
        self.assertEqual(route["layers"][0]["type"], "select")
        self.assertEqual(route["layers"][1]["type"], "fallback")
        self.assertTrue(route["route_marker"])

    def test_runtime_route_next_viable_candidate_is_conservative(self) -> None:
        route = inspect_runtime_route(
            {
                "proxies": {
                    "FINAL-EXIT": {"type": "Selector", "now": "PROXY-FALLBACK", "all": ["PROXY-FALLBACK", "DIRECT"]},
                    "PROXY-FALLBACK": {"type": "Fallback", "now": "TIER1-SELECT", "all": ["TIER1-SELECT"]},
                    "TIER1-SELECT": {"type": "Selector", "now": "edge-select", "all": ["TIER1-AUTO", "edge-select"]},
                    "edge-select": {"type": "Selector", "now": "edge-ss", "all": ["edge-auto", "edge-ss", "edge-vmess", "DIRECT"]},
                    "edge-ss": {"type": "Shadowsocks"},
                }
            }
        )

        action = next_viable_candidate(route, failed_proxies={"edge-ss", "edge-auto"}, active_gateway=False)

        self.assertEqual(action, {"group": "edge-select", "proxy": "edge-vmess"})

    def test_runtime_route_rotation_skips_all_builtin_terminals(self) -> None:
        route = inspect_runtime_route(
            {
                "proxies": {
                    "FINAL-EXIT": {
                        "type": "Selector",
                        "now": "bad-edge",
                        "all": [
                            "bad-edge",
                            "DIRECT",
                            "COMPATIBLE",
                            "REJECT",
                            "REJECT-DROP",
                            "good-edge",
                        ],
                    },
                    "bad-edge": {"type": "Shadowsocks"},
                }
            }
        )

        action = next_viable_candidate(
            route,
            failed_proxies={"bad-edge"},
            active_gateway=False,
        )

        self.assertEqual(action, {"group": "FINAL-EXIT", "proxy": "good-edge"})

    def test_runtime_route_classifies_builtin_terminal_semantics(self) -> None:
        for proxy_name in ("REJECT", "REJECT-DROP"):
            with self.subTest(proxy_name=proxy_name):
                self.assertEqual(builtin_terminal_kind(proxy_name), "fail_closed")
        for proxy_name in ("DIRECT", "COMPATIBLE"):
            with self.subTest(proxy_name=proxy_name):
                self.assertEqual(builtin_terminal_kind(proxy_name), "unsafe_direct")
        self.assertEqual(builtin_terminal_kind("managed-node"), "")

    def test_runtime_route_inspector_treats_direct_as_leaf(self) -> None:
        route = inspect_runtime_route(
            {"proxies": {"FINAL-EXIT": {"type": "Selector", "now": "DIRECT", "all": ["PROXY-FALLBACK", "DIRECT"]}}}
        )

        self.assertEqual(route["leaf_proxy"], "DIRECT")
        self.assertEqual(route["selected_chain"], ["FINAL-EXIT", "DIRECT"])

    def test_version_sends_bearer_token(self) -> None:
        opener = FakeOpener([FakeResponse(body=b'{"version":"1.17"}')])
        client = self._client(opener)
        self.assertEqual(client.version(), {"version": "1.17"})
        method, url, body, headers = opener.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "http://127.0.0.1:9090/version")
        self.assertIsNone(body)
        normalized = {k.lower(): v for k, v in headers.items()}
        self.assertEqual(normalized.get("authorization"), "Bearer my-secret")

    def test_proxies_accepts_incident_bounded_timeout(self) -> None:
        opener = FakeOpener([FakeResponse(body=b'{"proxies":{}}')])
        client = self._client(opener)

        self.assertEqual(client.proxies(timeout=0.35), {"proxies": {}})
        self.assertEqual(opener.timeouts, [0.35])

    def test_select_proxy_uses_put_and_url_escapes_name(self) -> None:
        opener = FakeOpener([FakeResponse()])
        client = self._client(opener)
        result = client.select_proxy("PROXY FALLBACK", "Example-VPS")
        self.assertEqual(result, {"group": "PROXY FALLBACK", "selected": "Example-VPS"})
        method, url, body, _ = opener.calls[0]
        self.assertEqual(method, "PUT")
        self.assertEqual(url, "http://127.0.0.1:9090/proxies/PROXY%20FALLBACK")
        self.assertEqual(json.loads(body.decode("utf-8")), {"name": "Example-VPS"})

    def test_select_proxy_accepts_incident_bounded_timeout(self) -> None:
        opener = FakeOpener([FakeResponse()])
        client = self._client(opener)

        client.select_proxy("FINAL-EXIT", "REJECT", timeout=0.35)

        self.assertEqual(opener.timeouts, [0.35])

    def test_selection_commit_puts_once_and_reads_fixed_group_back(self) -> None:
        opener = FakeOpener(
            [
                FakeResponse(),
                FakeResponse(body=b'{"type":"Fallback","now":"Example-VPS","fixed":"Example-VPS"}'),
            ]
        )
        client = self._client(opener)

        result = client.select_proxy_with_readback(
            "PROXY FALLBACK",
            "Example-VPS",
            mode="fixed",
            timeout=0.35,
            readback_timeout=0.2,
        )

        self.assertIsInstance(result, MihomoSelectionCommit)
        self.assertEqual(result.commit_state, "committed")
        self.assertEqual(result.request_outcome, "succeeded")
        self.assertEqual(result.now, "Example-VPS")
        self.assertEqual(result.fixed, "Example-VPS")
        self.assertEqual([call[0] for call in opener.calls], ["PUT", "GET"])
        self.assertEqual(opener.timeouts, [0.35, 0.2])

    def test_selection_put_and_readback_share_one_deadline(self) -> None:
        clock_value = [10.0]

        class AdvancingOpener(FakeOpener):
            def __call__(self, request, timeout=None):
                response = super().__call__(request, timeout=timeout)
                clock_value[0] += 0.3
                return response

        opener = AdvancingOpener(
            [
                FakeResponse(),
                FakeResponse(
                    body=b'{"now":"Example-VPS","fixed":"Example-VPS"}'
                ),
            ]
        )
        client = self._client(opener)

        result = client.select_proxy_with_readback(
            "PROXY-FALLBACK",
            "Example-VPS",
            mode="fixed",
            timeout=0.35,
            readback_timeout=0.35,
            deadline_monotonic=10.5,
            clock=lambda: clock_value[0],
        )

        self.assertEqual(result.commit_state, "committed")
        self.assertEqual(len(opener.timeouts), 2)
        self.assertAlmostEqual(opener.timeouts[0] or 0.0, 0.35)
        self.assertAlmostEqual(opener.timeouts[1] or 0.0, 0.2)

    def test_selection_commit_can_be_proven_after_put_timeout(self) -> None:
        opener = FakeOpener(
            [
                TimeoutError("PUT response timed out"),
                FakeResponse(body=b'{"now":"Example-VPS","fixed":"Example-VPS"}'),
            ]
        )
        client = self._client(opener)

        result = client.select_proxy_with_readback(
            "PROXY-FALLBACK",
            "Example-VPS",
            mode="fixed",
            timeout=0.35,
        )

        self.assertEqual(result.commit_state, "committed")
        self.assertEqual(result.request_outcome, "failed")
        self.assertIn("timed out", result.request_error or "")
        self.assertEqual([call[0] for call in opener.calls], ["PUT", "GET"])
        self.assertEqual(opener.timeouts, [0.35, 0.35])

    def test_selection_commit_reports_not_committed_after_disconnect(self) -> None:
        opener = FakeOpener(
            [
                ConnectionResetError("controller disconnected"),
                FakeResponse(body=b'{"now":"backup","fixed":"backup"}'),
            ]
        )
        client = self._client(opener)

        result = client.select_proxy_with_readback(
            "PROXY-FALLBACK",
            "Example-VPS",
            mode="fixed",
        )

        self.assertEqual(result.commit_state, "not_committed")
        self.assertEqual(result.request_outcome, "failed")
        self.assertEqual(result.now, "backup")
        self.assertEqual(result.fixed, "backup")
        self.assertEqual(len(opener.calls), 2)

    def test_selection_commit_unknown_does_not_retry_readback(self) -> None:
        opener = FakeOpener(
            [
                TimeoutError("PUT response timed out"),
                ConnectionResetError("readback disconnected"),
            ]
        )
        client = self._client(opener)

        result = client.select_proxy_with_readback(
            "PROXY-FALLBACK",
            "Example-VPS",
            mode="fixed",
        )

        self.assertEqual(result.commit_state, "unknown")
        self.assertEqual(result.request_outcome, "failed")
        self.assertIn("readback disconnected", result.readback_error or "")
        self.assertEqual([call[0] for call in opener.calls], ["PUT", "GET"])

    def test_fixed_selection_needs_fixed_field_but_selector_only_needs_now(self) -> None:
        opener = FakeOpener(
            [
                FakeResponse(),
                FakeResponse(body=b'{"now":"Example-VPS"}'),
                FakeResponse(),
                FakeResponse(body=b'{"now":"Example-VPS"}'),
            ]
        )
        client = self._client(opener)

        fixed = client.select_proxy_with_readback(
            "PROXY-FALLBACK",
            "Example-VPS",
            mode="fixed",
        )
        selected = client.select_proxy_with_readback(
            "FINAL-EXIT",
            "Example-VPS",
            mode="selected",
        )

        self.assertEqual(fixed.commit_state, "unknown")
        self.assertFalse(fixed.fixed_observed)
        self.assertEqual(selected.commit_state, "committed")

    def test_unfix_proxy_uses_delete_and_bounded_timeout(self) -> None:
        opener = FakeOpener([FakeResponse()])
        client = self._client(opener)

        self.assertEqual(
            client.unfix_proxy("PROXY FALLBACK", timeout=0.35),
            {"group": "PROXY FALLBACK", "fixed": False},
        )

        method, url, body, _ = opener.calls[0]
        self.assertEqual(method, "DELETE")
        self.assertEqual(url, "http://127.0.0.1:9090/proxies/PROXY%20FALLBACK")
        self.assertIsNone(body)
        self.assertEqual(opener.timeouts, [0.35])

    def test_connection_cleanup_calls_accept_bounded_timeouts(self) -> None:
        opener = FakeOpener(
            [
                FakeResponse(body=b'{"connections":[]}'),
                FakeResponse(),
            ]
        )
        client = self._client(opener)

        self.assertEqual(client.connections(timeout=0.35), {"connections": []})
        self.assertEqual(
            client.close_connection("connection/id", timeout=0.2),
            {"closed": "connection/id"},
        )
        self.assertEqual(opener.timeouts, [0.35, 0.2])

    def test_reload_config_uses_force_query(self) -> None:
        opener = FakeOpener([FakeResponse(body=b"{}")])
        client = self._client(opener)
        client.reload_config("C:/tmp/mihomo-runtime.yaml", force=True)
        method, url, body, _ = opener.calls[0]
        self.assertEqual(method, "PUT")
        self.assertIn("/configs?", url)
        self.assertIn("force=true", url)
        self.assertNotIn("path=", url)
        self.assertEqual(json.loads(body.decode("utf-8")), {"path": "C:/tmp/mihomo-runtime.yaml", "payload": ""})

    def test_proxy_delay_issues_timeout_and_url(self) -> None:
        opener = FakeOpener([FakeResponse(body=b'{"delay":120}')])
        client = self._client(opener)
        result = client.proxy_delay("Osaka", url="https://www.gstatic.com/generate_204", timeout_ms=3000)
        self.assertEqual(result, {"delay": 120})
        method, url, _, _ = opener.calls[0]
        self.assertEqual(method, "GET")
        self.assertIn("/proxies/Osaka/delay?", url)
        self.assertIn("url=https", url)
        self.assertIn("timeout=3000", url)

    def test_proxy_delay_can_bound_controller_wait_to_incident_budget(self) -> None:
        opener = FakeOpener([FakeResponse(body=b'{"delay":120}')])
        client = self._client(opener)

        self.assertEqual(
            client.proxy_delay(
                "Osaka",
                url="https://www.gstatic.com/generate_204",
                timeout_ms=1500,
                request_timeout=1.5,
            ),
            {"delay": 120},
        )
        self.assertEqual(opener.timeouts, [1.5])

    def test_proxy_delay_uses_mihomo_expected_query_name(self) -> None:
        opener = FakeOpener([FakeResponse(body=b'{"delay":120}')])
        client = self._client(opener)

        client.proxy_delay(
            "Osaka",
            url="https://www.gstatic.com/generate_204",
            timeout_ms=1500,
            expected_status="200-204",
            request_timeout=1.5,
        )

        _, url, _, _ = opener.calls[0]
        self.assertIn("expected=200-204", url)
        self.assertNotIn("expected-status", url)

    def test_proxy_delay_publishes_closer_before_closable_request_starts(self) -> None:
        opener = FakeOpener([AssertionError("custom opener must remain unused")])
        client = self._client(opener)
        events: list[str] = []
        connection = FakeHTTPConnection(
            "127.0.0.1",
            response=FakeHTTPClientResponse(body=b'{"delay":41}'),
            events=events,
        )
        published_closer: list[object] = []

        def connection_abort_sink(close_callback) -> None:
            events.append("sink")
            published_closer.append(close_callback)

        with patch(
            "mihomo_api.http.client.HTTPConnection",
            return_value=connection,
        ) as connection_type:
            result = client.proxy_delay(
                "Osaka",
                url="https://www.gstatic.com/generate_204",
                timeout_ms=1500,
                expected_status=204,
                request_timeout=0.75,
                connection_abort_sink=connection_abort_sink,
            )

        self.assertEqual(result, {"delay": 41})
        self.assertEqual(events, ["sink", "request", "getresponse", "close"])
        self.assertEqual(len(published_closer), 1)
        self.assertEqual(opener.calls, [])
        connection_type.assert_called_once_with(
            "127.0.0.1",
            port=9090,
            timeout=0.75,
        )
        self.assertIsNotNone(connection.request_call)
        method, target, body, headers = connection.request_call or ("", "", None, {})
        self.assertEqual(method, "GET")
        self.assertIsNone(body)
        self.assertIn("/proxies/Osaka/delay?", target)
        self.assertIn("timeout=1500", target)
        self.assertIn("expected=204", target)
        self.assertEqual(headers.get("Authorization"), "Bearer my-secret")

    def test_closable_delay_sink_exception_propagates_without_dial(self) -> None:
        client = self._client(FakeOpener([]))
        connection = FakeHTTPConnection("127.0.0.1")
        sentinel = RuntimeError("probe admission expired")

        def reject_connection(_close_callback) -> None:
            raise sentinel

        with patch(
            "mihomo_api.http.client.HTTPConnection",
            return_value=connection,
        ):
            with self.assertRaises(RuntimeError) as raised:
                client.proxy_delay(
                    "Osaka",
                    url="https://www.gstatic.com/generate_204",
                    connection_abort_sink=reject_connection,
                )

        self.assertIs(raised.exception, sentinel)
        self.assertIsNone(connection.request_call)
        self.assertTrue(connection.closed)

    def test_published_delay_closer_aborts_blocked_controller_read(self) -> None:
        client = self._client(FakeOpener([]))
        request_started = threading.Event()
        connection_closed = threading.Event()
        published_closer: list[object] = []
        outcome: list[BaseException] = []

        class BlockingHTTPConnection(FakeHTTPConnection):
            def request(self, method, target, body=None, headers=None) -> None:
                super().request(method, target, body=body, headers=headers)
                request_started.set()

            def getresponse(self):
                if not connection_closed.wait(2.0):
                    raise AssertionError("test controller read was not aborted")
                raise ConnectionAbortedError("controller connection closed")

            def close(self) -> None:
                connection_closed.set()
                super().close()

        connection = BlockingHTTPConnection("127.0.0.1")

        def run_delay() -> None:
            try:
                client.proxy_delay(
                    "Osaka",
                    url="https://www.gstatic.com/generate_204",
                    request_timeout=5.0,
                    connection_abort_sink=published_closer.append,
                )
            except BaseException as exc:  # asserted below from the worker
                outcome.append(exc)

        with patch(
            "mihomo_api.http.client.HTTPConnection",
            return_value=connection,
        ):
            worker = threading.Thread(target=run_delay)
            worker.start()
            self.assertTrue(request_started.wait(0.5))
            self.assertEqual(len(published_closer), 1)
            published_closer[0]()
            worker.join(0.5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], MihomoAPIError)
        self.assertIn("controller connection closed", str(outcome[0]))

    def test_group_delay_supports_same_abortable_controller_contract(self) -> None:
        client = self._client(FakeOpener([]))
        events: list[str] = []
        connection = FakeHTTPConnection(
            "127.0.0.1",
            response=FakeHTTPClientResponse(body=b'{"delay":17}'),
            events=events,
        )

        with patch(
            "mihomo_api.http.client.HTTPConnection",
            return_value=connection,
        ):
            result = client.group_delay(
                "PROXY FALLBACK",
                url="https://www.gstatic.com/generate_204",
                timeout_ms=900,
                request_timeout=0.4,
                connection_abort_sink=lambda _close: events.append("sink"),
            )

        self.assertEqual(result, {"delay": 17})
        self.assertEqual(events[:2], ["sink", "request"])
        self.assertIsNotNone(connection.request_call)
        _, target, _, _ = connection.request_call or ("", "", None, {})
        self.assertIn("/group/PROXY%20FALLBACK/delay?", target)
        self.assertIn("timeout=900", target)

    def test_group_delay_sends_expected_only_when_given(self) -> None:
        opener = FakeOpener(
            [
                FakeResponse(body=b'{"n1":120}'),
                FakeResponse(body=b'{"n1":121}'),
                FakeResponse(body=b'{"n1":122}'),
            ]
        )
        client = self._client(opener)

        self.assertEqual(
            client.group_delay("TIER3-AUTO", url=CONTRACT_URL, timeout_ms=5000),
            {"n1": 120},
        )
        client.group_delay(
            "TIER3-AUTO",
            url=CONTRACT_URL,
            timeout_ms=5000,
            expected_status="204",
        )
        client.group_delay(
            "TIER3-AUTO",
            url=CONTRACT_URL,
            timeout_ms=5000,
            request_timeout=5.4,
            expected_status=204,
        )

        prefix = (
            "http://127.0.0.1:9090/group/TIER3-AUTO/delay"
            "?url=http%3A%2F%2Fwww.google.com%2Fgenerate_204&timeout=5000"
        )
        self.assertEqual(
            [call[1] for call in opener.calls],
            [prefix, f"{prefix}&expected=204", f"{prefix}&expected=204"],
        )
        self.assertEqual(opener.timeouts, [7.0, 7.0, 5.4])

    def test_group_delay_expected_reaches_closable_transport(self) -> None:
        client = self._client(FakeOpener([]))
        connection = FakeHTTPConnection(
            "127.0.0.1",
            response=FakeHTTPClientResponse(body=b'{"n1":17}'),
        )

        with patch(
            "mihomo_api.http.client.HTTPConnection",
            return_value=connection,
        ):
            client.group_delay(
                "TIER3-AUTO",
                url=CONTRACT_URL,
                timeout_ms=5000,
                request_timeout=5.25,
                connection_abort_sink=lambda _close: None,
                expected_status="204",
            )

        _, target, _, _ = connection.request_call or ("", "", None, {})
        self.assertIn("timeout=5000", target)
        self.assertIn("expected=204", target)
        self.assertNotIn("expected-status", target)

    def test_group_delay_timeout_stays_attribution_neutral(self) -> None:
        error = HTTPError(
            "http://127.0.0.1:9090/group/TIER3-AUTO/delay",
            504,
            "failure",
            {},  # type: ignore[arg-type]
            io.BytesIO(b'{"message":"Timeout"}'),
        )
        client = self._client(FakeOpener([error]))

        with self.assertRaises(MihomoAPIError) as raised:
            client.group_delay("TIER3-AUTO", url=CONTRACT_URL, expected_status="204")

        self.assertIs(type(raised.exception), MihomoAPIError)

    def test_native_delay_verdicts_raise_specific_subclasses(self) -> None:
        cases = (
            (503, b"{}", MihomoDelayTransportFailed),
            (503, b'{"message":"An error occurred in the delay test"}', MihomoDelayTransportFailed),
            (504, b'{"message":"Timeout"}', MihomoDelayTimeout),
            (503, b"<html>Service Unavailable</html>", MihomoAPIError),
            (504, b'{"message":"controller busy"}', MihomoAPIError),
        )
        for closable in (False, True):
            for status, body, expected_type in cases:
                with self.subTest(closable=closable, status=status, body=body):
                    client = self._client(FakeOpener([_delay_http_error(status, body)]))
                    connection = FakeHTTPConnection(
                        "127.0.0.1",
                        response=FakeHTTPClientResponse(body, status=status, reason="failure"),
                    )
                    with patch(
                        "mihomo_api.http.client.HTTPConnection",
                        return_value=connection,
                    ):
                        with self.assertRaises(MihomoAPIError) as raised:
                            client.proxy_delay(
                                "n1",
                                url=CONTRACT_URL,
                                expected_status="204",
                                connection_abort_sink=(lambda _close: None) if closable else None,
                            )
                    self.assertIs(type(raised.exception), expected_type)
                    self.assertEqual(
                        isinstance(raised.exception, MihomoDelayTestFailed),
                        expected_type is not MihomoAPIError,
                    )
                    # tester.py still classifies the verdicts by this text.
                    self.assertIn(f"HTTP {status}", str(raised.exception))

    def test_expected_status_readback_mismatch_raises_status_mismatch(self) -> None:
        for alive, expected_type in ((False, MihomoDelayStatusMismatch), (None, MihomoAPIError)):
            with self.subTest(alive=alive):
                state = {"extra": {CONTRACT_URL: {"alive": alive}}} if alive is not None else {}
                opener = FakeOpener(
                    [
                        FakeResponse(body=b'{"delay":8}'),
                        FakeResponse(body=json.dumps(state).encode()),
                    ]
                )
                client = self._client(opener)

                with self.assertRaises(MihomoAPIError) as raised:
                    client.proxy_delay(
                        "n1",
                        url=CONTRACT_URL,
                        expected_status="204",
                        verify_expected_status=True,
                        request_timeout=5.65,
                    )

                self.assertIs(type(raised.exception), expected_type)
                self.assertEqual(
                    [call[1] for call in opener.calls][1],
                    "http://127.0.0.1:9090/proxies/n1",
                )

    def test_delay_verdict_vocabulary_keeps_legacy_handlers_working(self) -> None:
        verdicts = (MihomoDelayTransportFailed, MihomoDelayTimeout, MihomoDelayStatusMismatch)
        for verdict in verdicts:
            with self.subTest(verdict=verdict.__name__):
                self.assertTrue(issubclass(verdict, MihomoDelayTestFailed))
                self.assertTrue(issubclass(verdict, MihomoAPIError))
                for other in verdicts:
                    if other is not verdict:
                        self.assertFalse(issubclass(verdict, other))
                try:
                    raise verdict("probe")
                except MihomoDelayTestFailed as caught:
                    self.assertIsInstance(caught, verdict)

    def test_provider_proxies_one_reads_single_provider_endpoint(self) -> None:
        opener = FakeOpener(
            [
                FakeResponse(body=json.dumps(CONF_C_PROVIDER_FIXTURE).encode()),
                FakeResponse(body=b'{"name":"odd"}'),
                FakeResponse(body=b'{"providers":{}}'),
            ]
        )
        client = self._client(opener)

        provider = client.provider_proxies_one("TIER3B-RUNTIME", timeout=0.35)
        client.provider_proxies_one("odd name/#?")
        self.assertEqual(client.providers_proxies(timeout=0.2), {"providers": {}})

        self.assertEqual(provider, CONF_C_PROVIDER_FIXTURE)
        self.assertEqual(provider["testUrl"], "")
        self.assertEqual(provider["expectedStatus"], "*")
        self.assertEqual([call[0] for call in opener.calls], ["GET", "GET", "GET"])
        self.assertEqual(
            [call[1] for call in opener.calls],
            [
                "http://127.0.0.1:9090/providers/proxies/TIER3B-RUNTIME",
                "http://127.0.0.1:9090/providers/proxies/odd%20name%2F%23%3F",
                "http://127.0.0.1:9090/providers/proxies",
            ],
        )
        self.assertEqual(opener.timeouts, [0.35, 1.0, 0.2])

    def test_provider_proxies_one_errors_are_attribution_neutral(self) -> None:
        missing = HTTPError(
            "http://127.0.0.1:9090/providers/proxies/TIER3B-RUNTIME",
            404,
            "Not Found",
            {},  # type: ignore[arg-type]
            io.BytesIO(b'{"message":"resource not found"}'),
        )
        client = self._client(FakeOpener([missing, ConnectionRefusedError("refused")]))

        for expected_text in ("HTTP 404", "refused"):
            with self.subTest(expected_text=expected_text):
                with self.assertRaises(MihomoAPIError) as raised:
                    client.provider_proxies_one("TIER3B-RUNTIME", timeout=0.35)
                self.assertIs(type(raised.exception), MihomoAPIError)
                self.assertIn(expected_text, str(raised.exception))

    def test_proxy_provider_update_healthcheck_and_readback_endpoints(self) -> None:
        opener = FakeOpener(
            [
                FakeResponse(),
                FakeResponse(),
                FakeResponse(body=b'{"name":"remote provider"}'),
            ]
        )
        client = self._client(opener)

        self.assertEqual(
            client.update_proxy_provider("remote provider", timeout=0.4),
            {"provider": "remote provider", "updated": True},
        )
        self.assertEqual(
            client.health_check_provider("remote provider", timeout=0.5),
            {"provider": "remote provider", "triggered": True},
        )
        self.assertEqual(
            client.proxy_provider("remote provider", timeout=0.25),
            {"name": "remote provider"},
        )

        self.assertEqual([call[0] for call in opener.calls], ["PUT", "GET", "GET"])
        self.assertEqual(
            [call[1] for call in opener.calls],
            [
                "http://127.0.0.1:9090/providers/proxies/remote%20provider",
                "http://127.0.0.1:9090/providers/proxies/remote%20provider/healthcheck",
                "http://127.0.0.1:9090/providers/proxies/remote%20provider",
            ],
        )
        self.assertEqual(opener.timeouts, [0.4, 0.5, 0.25])

    def test_close_all_connections(self) -> None:
        opener = FakeOpener([FakeResponse()])
        client = self._client(opener)
        result = client.close_all_connections()
        self.assertEqual(result, {"closed": "all"})
        method, url, _, _ = opener.calls[0]
        self.assertEqual(method, "DELETE")
        self.assertEqual(url, "http://127.0.0.1:9090/connections")

    def test_network_error_becomes_mihomo_api_error(self) -> None:
        class ExplodingOpener:
            def __call__(self, request, timeout=None):
                raise ConnectionRefusedError("refused")

        client = MihomoAPIClient(
            base_url="http://127.0.0.1:9090",
            opener=ExplodingOpener(),
            timeout=0.5,
        )
        with self.assertRaises(MihomoAPIError) as ctx:
            client.version()
        self.assertIn("refused", str(ctx.exception))

    def test_stream_traffic_yields_parsed_samples(self) -> None:
        lines = [b'{"up":10,"down":20}\n', b"\n", b'{"up":30,"down":40}\n']
        opener = FakeOpener([FakeResponse(lines=lines)])
        client = self._client(opener)
        samples = list(client.stream_traffic())
        self.assertEqual(samples, [{"up": 10, "down": 20}, {"up": 30, "down": 40}])

    def test_stream_logs_falls_back_to_payload_for_non_json(self) -> None:
        lines = [b'{"type":"info","payload":"hello"}\n', b"raw-non-json\n"]
        opener = FakeOpener([FakeResponse(lines=lines)])
        client = self._client(opener)
        opened: list[bool] = []
        records = list(client.stream_logs(on_open=lambda: opened.append(True)))
        self.assertEqual(opened, [True])
        self.assertEqual(records[0], {"type": "info", "payload": "hello"})
        self.assertEqual(records[1], {"type": "info", "payload": "raw-non-json"})

    def test_streams_use_long_read_timeout(self) -> None:
        opener = FakeOpener([FakeResponse(lines=[b'{"type":"info","payload":"hello"}\n'])])
        client = self._client(opener)

        list(client.stream_logs())

        self.assertEqual(opener.timeouts[0], STREAM_READ_TIMEOUT_SECONDS)

    def test_stop_streams_can_close_only_matching_owner(self) -> None:
        first = FakeResponse()
        second = FakeResponse()
        opener = FakeOpener([first, second])
        client = self._client(opener)
        first_owner = object()
        second_owner = object()

        with client._open_stream("/logs", owner=first_owner):
            with client._open_stream("/connections", owner=second_owner):
                client.stop_streams(owner=first_owner)

                self.assertTrue(first.closed)
                self.assertFalse(second.closed)

    def test_build_client_from_config_disabled(self) -> None:
        self.assertIsNone(build_client_from_config({"mihomo_controller": {"enabled": False}}))

    def test_build_client_from_config_string_false_disabled(self) -> None:
        self.assertIsNone(build_client_from_config({"mihomo_controller": {"enabled": "false"}}))

    def test_build_client_from_config_populates(self) -> None:
        client = build_client_from_config(
            {
                "mihomo_controller": {
                    "enabled": True,
                    "host": "10.0.0.1",
                    "port": 19090,
                    "secret": "s",
                    "timeout": 5,
                }
            }
        )
        self.assertIsNotNone(client)
        assert client is not None
        self.assertEqual(client.base_url, "http://10.0.0.1:19090")
        self.assertEqual(client.secret, "s")
        self.assertEqual(client.timeout, 5.0)

    def test_controller_config_helpers_preserve_casts_and_defaults(self) -> None:
        controller = {
            "enabled": "false",
            "host": "10.0.0.2",
            "port": "19090",
            "secret": 1234,
            "timeout": "7.5",
        }

        self.assertFalse(mihomo_controller_enabled(controller))
        self.assertFalse(mihomo_controller_enabled({}, default=False))
        self.assertTrue(mihomo_controller_enabled({}))
        self.assertEqual(mihomo_controller_address(controller), "10.0.0.2:19090")
        self.assertEqual(build_mihomo_controller_base_url(controller), "http://10.0.0.2:19090")
        self.assertEqual(mihomo_controller_secret(controller), "1234")
        self.assertEqual(mihomo_controller_timeout(controller), 7.5)
        self.assertEqual(mihomo_controller_address({}), "127.0.0.1:9090")
        self.assertEqual(build_mihomo_controller_base_url({}), "http://127.0.0.1:9090")
        self.assertEqual(mihomo_controller_secret({}), "")
        self.assertEqual(mihomo_controller_timeout({}), 3.0)

    def test_controller_config_helpers_fall_back_for_invalid_runtime_values(self) -> None:
        controller = {"port": "bad", "timeout": "bad"}

        self.assertEqual(mihomo_controller_address(controller), "127.0.0.1:9090")
        self.assertEqual(build_mihomo_controller_base_url(controller), "http://127.0.0.1:9090")
        self.assertEqual(mihomo_controller_timeout(controller), 3.0)
        self.assertEqual(
            hot_reload_kwargs_from_controller({"port": 70000, "timeout": 0}),
            {
                "controller_host": "127.0.0.1",
                "controller_port": 9090,
                "controller_secret": "",
                "timeout": 0.1,
            },
        )

    def test_hot_reload_kwargs_from_controller_matches_runner_contract(self) -> None:
        self.assertEqual(
            hot_reload_kwargs_from_controller(
                {
                    "host": "10.0.0.2",
                    "port": "19090",
                    "secret": 1234,
                    "timeout": "7.5",
                }
            ),
            {
                "controller_host": "10.0.0.2",
                "controller_port": 19090,
                "controller_secret": "1234",
                "timeout": 7.5,
            },
        )
        self.assertEqual(
            hot_reload_kwargs_from_controller({}),
            {
                "controller_host": "127.0.0.1",
                "controller_port": 9090,
                "controller_secret": "",
                "timeout": 3.0,
            },
        )


class MihomoControllerKeepAliveTests(unittest.TestCase):
    """The post-dial observer's one-socket client against a local server."""

    def _client(
        self,
        server: _FakeControllerServer,
        **kwargs: object,
    ) -> MihomoControllerKeepAlive:
        client = MihomoControllerKeepAlive(
            base_url=server.base_url,
            secret="s3cret",
            **kwargs,  # type: ignore[arg-type]
        )
        self.addCleanup(client.close)
        return client

    @staticmethod
    def _start_poll(
        client: MihomoControllerKeepAlive,
        *,
        timeout_seconds: float,
    ) -> tuple[threading.Thread, list[object]]:
        """Run one ``connections()`` call on a worker; collect its outcome."""
        outcome: list[object] = []

        def poll() -> None:
            try:
                outcome.append(client.connections(timeout_seconds=timeout_seconds))
            except BaseException as exc:  # asserted by the caller
                outcome.append(exc)

        worker = threading.Thread(target=poll, daemon=True)
        worker.start()
        return worker, outcome

    def test_defaults_match_design_budget(self) -> None:
        self.assertEqual(KEEPALIVE_MAX_BODY_BYTES, 1_500_000)
        self.assertEqual(KEEPALIVE_PHASE_TIMEOUT_SECONDS, 0.35)
        self.assertEqual(MihomoControllerKeepAlive.MAX_BODY_BYTES, 1_500_000)
        parameter = inspect.signature(MihomoControllerKeepAlive.connections).parameters[
            "timeout_seconds"
        ]
        self.assertEqual(parameter.default, 0.35)
        self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)

    def test_connections_reuse_one_socket_across_calls(self) -> None:
        snapshots = [
            {"downloadTotal": index, "connections": [{"id": f"c{index}"}]}
            for index in range(3)
        ]
        with _FakeControllerServer([("json", item) for item in snapshots]) as server:
            client = self._client(server)

            results = [client.connections() for _ in range(3)]

            self.assertEqual(results, snapshots)
            self.assertEqual(server.accepted, 1)
            self.assertEqual(client.connect_count, 1)
            self.assertEqual(client.request_count, 3)
            self.assertEqual(
                server.requests,
                [("/connections", "Bearer s3cret")] * 3,
            )

    def test_chunked_snapshot_is_parsed_on_the_same_socket(self) -> None:
        large = {"connections": [{"id": f"c{index}", "upload": index} for index in range(200)]}
        with _FakeControllerServer(
            [("chunked", large), ("json", {"connections": []})]
        ) as server:
            client = self._client(server)

            self.assertEqual(client.connections(), large)
            self.assertEqual(client.connections(), {"connections": []})
            self.assertEqual(server.accepted, 1)

    def test_body_at_the_cap_is_parsed(self) -> None:
        body = _json_body_of_size(4096)
        with _FakeControllerServer([("raw", body), ("chunked", body)]) as server:
            client = self._client(server, max_body_bytes=4096)

            self.assertEqual(client.connections(), json.loads(body))
            self.assertEqual(client.connections(), json.loads(body))
            self.assertEqual(client.last_body_bytes, 4096)
            self.assertEqual(server.accepted, 1)

    def test_oversized_body_returns_none_and_next_call_redials(self) -> None:
        for kind in ("raw", "chunked"):
            with self.subTest(kind=kind):
                oversized = _json_body_of_size(8192)
                with _FakeControllerServer(
                    [(kind, oversized), ("json", {"connections": []})]
                ) as server:
                    client = self._client(server, max_body_bytes=4096)

                    self.assertIsNone(client.connections())
                    self.assertGreater(client.last_body_bytes, 4096)
                    self.assertEqual(client.connections(), {"connections": []})
                    self.assertEqual(server.accepted, 2)
                    self.assertEqual(client.connect_count, 2)
                    self.assertEqual(client.request_count, 2)

    def test_reconnects_after_server_closes_the_idle_socket(self) -> None:
        with _FakeControllerServer(
            [
                ("json_silent_close", {"connections": [{"id": "a"}]}),
                ("json", {"connections": [{"id": "b"}]}),
                ("json", {"connections": [{"id": "c"}]}),
            ]
        ) as server:
            client = self._client(server)

            self.assertEqual(client.connections(), {"connections": [{"id": "a"}]})
            self.assertEqual(client.connections(), {"connections": [{"id": "b"}]})
            self.assertEqual(client.connections(), {"connections": [{"id": "c"}]})

            self.assertEqual(server.accepted, 2)
            self.assertEqual(client.connect_count, 2)
            # The stale socket costs exactly one retried send.
            self.assertEqual(client.request_count, 4)
            self.assertEqual(len(server.requests), 3)

    def test_connection_close_response_redials_without_retry(self) -> None:
        with _FakeControllerServer(
            [("json_close", {"connections": []}), ("json", {"connections": []})]
        ) as server:
            client = self._client(server)

            client.connections()
            client.connections()

            self.assertEqual(server.accepted, 2)
            self.assertEqual(client.connect_count, 2)
            self.assertEqual(client.request_count, 2)

    def test_phase_timeout_raises_and_next_call_redials(self) -> None:
        with _FakeControllerServer([("stall",), ("json", {"connections": []})]) as server:
            client = self._client(server)

            started = time.perf_counter()
            with self.assertRaises(MihomoAPIError) as raised:
                client.connections(timeout_seconds=0.2)
            elapsed = time.perf_counter() - started
            server.release.set()

            self.assertIs(type(raised.exception), MihomoAPIError)
            self.assertIn("GET /connections failed", str(raised.exception))
            self.assertLess(elapsed, 1.5)
            self.assertEqual(client.request_count, 1)
            self.assertEqual(client.connections(), {"connections": []})
            self.assertEqual(server.accepted, 2)

    def test_fresh_socket_dropped_without_reply_is_not_retried(self) -> None:
        with _FakeControllerServer([("drop",), ("json", {"connections": []})]) as server:
            client = self._client(server)

            with self.assertRaises(MihomoAPIError) as raised:
                client.connections()

            # The same error classes as a stale reused socket, but this
            # socket was new, so the controller really failed: no retry.
            self.assertIsInstance(
                raised.exception.__cause__,
                (http.client.RemoteDisconnected, ConnectionResetError, ConnectionAbortedError),
            )
            self.assertEqual(client.request_count, 1)
            self.assertEqual(client.connect_count, 1)
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(client.connections(), {"connections": []})
            self.assertEqual(server.accepted, 2)

    def test_refused_dial_raises_without_retry(self) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        client = MihomoControllerKeepAlive(base_url=f"http://127.0.0.1:{port}")
        self.addCleanup(client.close)

        with self.assertRaises(MihomoAPIError):
            client.connections()

        self.assertEqual(client.connect_count, 1)
        self.assertEqual(client.request_count, 1)

    def test_error_status_raises_and_discards_the_socket(self) -> None:
        with _FakeControllerServer(
            [("status", 401), ("json", {"connections": []})]
        ) as server:
            client = self._client(server)

            with self.assertRaises(MihomoAPIError) as raised:
                client.connections()

            self.assertIs(type(raised.exception), MihomoAPIError)
            self.assertIn("HTTP 401", str(raised.exception))
            self.assertEqual(client.connections(), {"connections": []})
            self.assertEqual(server.accepted, 2)

    def test_bad_bodies_raise_without_quoting_destinations(self) -> None:
        with _FakeControllerServer(
            [
                ("raw", b'["private-destination.example"]'),
                ("raw", b'{"host":"private-destination.example"'),
                ("json", {"connections": []}),
            ]
        ) as server:
            client = self._client(server)

            for _ in range(2):
                with self.assertRaises(MihomoAPIError) as raised:
                    client.connections()
                self.assertNotIn("private-destination", str(raised.exception))
                self.assertNotIn("private-destination", repr(raised.exception))
                self.assertIsNone(raised.exception.__cause__)

            # Both bodies were read to the end, so the socket stays usable.
            self.assertEqual(client.connections(), {"connections": []})
            self.assertEqual(server.accepted, 1)

    def test_close_is_idempotent_and_next_call_redials(self) -> None:
        with _FakeControllerServer() as server:
            client = self._client(server)

            client.connections()
            client.close()
            # The idle socket is closed now, not left for the controller's
            # own timeout.
            self.assertTrue(server.wait_disconnects(1, timeout=1.0))
            client.close()
            client.connections()

            self.assertEqual(server.accepted, 2)
            self.assertEqual(client.connect_count, 2)

    def test_close_during_a_reused_socket_read_raises_without_retry(self) -> None:
        with _FakeControllerServer(
            [
                ("json", {"connections": [{"id": "warm"}]}),
                ("delayed", 0.8, {"connections": [{"id": "late"}]}),
                ("json", {"connections": [{"id": "after"}]}),
            ]
        ) as server:
            client = self._client(server)
            client.connections()  # the next call reuses this socket

            worker, outcome = self._start_poll(client, timeout_seconds=2.0)
            self.assertTrue(server.reply_pending.wait(2.0))
            client.close()
            worker.join(4.0)

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], MihomoAPIError)
            # close() shut the socket at once instead of when the reply came.
            self.assertTrue(server.eof_before_reply.wait(1.0))
            # The aborted read looks like a stale socket, but close() came
            # first, so the call was not retried on a fresh one.
            self.assertEqual(client.request_count, 2)
            self.assertEqual(len(server.requests), 2)
            self.assertEqual(server.accepted, 1)

            self.assertEqual(client.connections(), {"connections": [{"id": "after"}]})
            self.assertEqual(server.accepted, 2)
            self.assertEqual(client.connect_count, 2)

    def test_close_while_dialling_raises_without_retry(self) -> None:
        with _FakeControllerServer(
            [
                ("json", {"connections": [{"id": "late"}]}),
                ("json", {"connections": [{"id": "after"}]}),
            ]
        ) as server:
            client = self._client(server)
            dialled = threading.Event()
            proceed = threading.Event()
            real_create_connection = socket.create_connection

            def create_connection(*args: object, **kwargs: object) -> socket.socket:
                sock = real_create_connection(*args, **kwargs)  # type: ignore[arg-type]
                if not dialled.is_set():
                    dialled.set()
                    proceed.wait(2.0)
                return sock

            with patch("socket.create_connection", create_connection):
                worker, outcome = self._start_poll(client, timeout_seconds=1.0)
                self.assertTrue(dialled.wait(2.0))
                # Connected, but http.client has not published the socket
                # yet, so close() has nothing to abort.
                client.close()
                proceed.set()
                worker.join(3.0)

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIs(type(outcome[0]), MihomoAPIError)
            # The request still went out on the new socket; its answer is
            # dropped and the socket closed.
            self.assertTrue(server.wait_disconnects(1, timeout=1.0))
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(client.request_count, 1)
            self.assertEqual(client.connect_count, 1)

            self.assertEqual(client.connections(), {"connections": [{"id": "after"}]})
            self.assertEqual(server.accepted, 2)

    def test_close_before_send_discards_the_redialled_snapshot(self) -> None:
        with _FakeControllerServer(
            [
                ("json", {"connections": [{"id": "warm"}]}),
                ("json", {"connections": [{"id": "late"}]}),
                ("json", {"connections": [{"id": "after"}]}),
            ]
        ) as server:
            client = self._client(server)
            client.connections()  # the next call picks up this socket
            sending = threading.Event()
            proceed = threading.Event()
            real_request = http.client.HTTPConnection.request

            def request(connection: http.client.HTTPConnection, *args: object, **kwargs: object) -> None:
                if not sending.is_set():
                    sending.set()
                    proceed.wait(2.0)
                real_request(connection, *args, **kwargs)  # type: ignore[arg-type]

            with patch.object(http.client.HTTPConnection, "request", request):
                worker, outcome = self._start_poll(client, timeout_seconds=1.0)
                self.assertTrue(sending.wait(2.0))
                # close() lands after the call took the socket and before it
                # sends; http.client then quietly dials a new socket and
                # gets a full answer, taken after close().
                client.close()
                proceed.set()
                worker.join(3.0)

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIs(type(outcome[0]), MihomoAPIError)
            self.assertIn("controller connection closed", str(outcome[0]))
            self.assertEqual(len(server.requests), 2)
            self.assertEqual(server.accepted, 2)
            self.assertEqual(client.request_count, 2)
            # Both the socket close() shut and the redialled one are closed.
            self.assertTrue(server.wait_disconnects(2, timeout=1.0))

            self.assertEqual(client.connections(), {"connections": [{"id": "after"}]})
            self.assertEqual(server.accepted, 3)

    def test_from_client_uses_controller_url_prefix_and_secret(self) -> None:
        with _FakeControllerServer() as server:
            api_client = MihomoAPIClient(base_url=f"{server.base_url}/ctl/", secret="abc")
            client = MihomoControllerKeepAlive.from_client(api_client)
            self.addCleanup(client.close)

            client.connections()

            self.assertEqual(server.requests, [("/ctl/connections", "Bearer abc")])
            self.assertNotIn("abc", repr(client))

    def test_rejects_unsupported_controller_url(self) -> None:
        for base_url in ("ftp://127.0.0.1:9090", "127.0.0.1:9090", ""):
            with self.subTest(base_url=base_url):
                with self.assertRaises(ValueError):
                    MihomoControllerKeepAlive(base_url=base_url)


class MihomoControllerKeepAliveRawTests(unittest.TestCase):
    """``connections_raw``: the same socket rules on a plain socket, body undecoded."""

    def _client(
        self,
        server: _FakeControllerServer,
        **kwargs: object,
    ) -> MihomoControllerKeepAlive:
        client = MihomoControllerKeepAlive(
            base_url=server.base_url,
            secret="s3cret",
            **kwargs,  # type: ignore[arg-type]
        )
        self.addCleanup(client.close)
        return client

    def test_defaults_match_connections(self) -> None:
        raw = inspect.signature(MihomoControllerKeepAlive.connections_raw).parameters["timeout_seconds"]
        self.assertEqual(raw.default, KEEPALIVE_PHASE_TIMEOUT_SECONDS)
        self.assertIs(raw.kind, inspect.Parameter.KEYWORD_ONLY)

    def test_bodies_come_back_undecoded_on_one_socket(self) -> None:
        bodies = [json.dumps({"downloadTotal": index, "connections": [{"id": f"c{index}"}]}).encode() for index in range(3)]
        with _FakeControllerServer([("raw", body) for body in bodies]) as server:
            client = self._client(server)

            results = [client.connections_raw() for _ in range(3)]

            self.assertEqual(results, bodies)
            self.assertTrue(all(type(result) is bytes for result in results))
            self.assertEqual(server.accepted, 1)
            self.assertEqual(client.connect_count, 1)
            self.assertEqual(client.request_count, 3)
            self.assertEqual(server.requests, [("/connections", "Bearer s3cret")] * 3)
            self.assertEqual(client.last_body_bytes, len(bodies[-1]))

    def test_the_request_is_the_one_http_client_sends(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        captured: list[bytes] = []

        def serve() -> None:
            for _ in range(2):
                conn, _addr = listener.accept()
                with conn:
                    data = b""
                    while b"\r\n\r\n" not in data:
                        data += conn.recv(4096)
                    captured.append(data)
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        client = MihomoControllerKeepAlive(base_url=f"http://127.0.0.1:{port}/ctl", secret="abc")
        self.addCleanup(client.close)
        self.assertEqual(client.connections(), {})
        self.assertEqual(client.connections_raw(), b"{}")
        worker.join(3.0)
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[1], captured[0])
        self.assertTrue(captured[1].startswith(b"GET /ctl/connections HTTP/1.1\r\n"))

    def test_chunked_bodies_in_small_or_one_large_chunk(self) -> None:
        large = {"connections": [{"id": f"c{index}", "upload": index} for index in range(3000)]}
        encoded = json.dumps(large).encode()
        self.assertGreater(len(encoded), 64 * 1024)
        with _FakeControllerServer(
            [("chunked", large), ("chunked_one", large), ("chunked_one", large), ("raw", b"{}")]
        ) as server:
            client = self._client(server)

            self.assertEqual(client.connections_raw(), encoded)
            self.assertEqual(client.connections_raw(), encoded)
            self.assertEqual(client.connections_raw(), encoded)
            self.assertEqual(client.connections_raw(), b"{}")
            self.assertEqual(server.accepted, 1)

    def test_the_receive_buffer_is_kept_for_similar_bodies_and_given_back_otherwise(self) -> None:
        from mihomo_api import _KEEPALIVE_RAW_BUFFER_BYTES

        large = _json_body_of_size(600_000)
        similar = _json_body_of_size(500_000)
        with _FakeControllerServer(
            [("raw", large), ("chunked_one", similar), ("raw", b"{}"), ("raw", large), ("chunked", large)]
        ) as server:
            client = self._client(server, max_body_bytes=550_000)

            def buffer_size() -> int | None:
                return None if client._raw_buffer is None else len(client._raw_buffer)

            # Declared over the cap: refused before the body is read.
            self.assertIsNone(client.connections_raw())
            self.assertEqual(buffer_size(), _KEEPALIVE_RAW_BUFFER_BYTES)
            self.assertEqual(client.connections_raw(), similar)
            self.assertGreater(buffer_size() or 0, _KEEPALIVE_RAW_BUFFER_BYTES)  # kept for the next one
            self.assertEqual(client.connections_raw(), b"{}")  # much smaller: given back
            self.assertIsNone(buffer_size())
            self.assertIsNone(client.connections_raw())
            self.assertEqual(buffer_size(), _KEEPALIVE_RAW_BUFFER_BYTES)
            self.assertIsNone(client.connections_raw())  # chunked, cut off at the cap: given back
            self.assertIsNone(buffer_size())

    def test_body_at_the_cap_is_returned(self) -> None:
        body = _json_body_of_size(4096)
        with _FakeControllerServer([("raw", body), ("chunked", body), ("chunked_one", body)]) as server:
            client = self._client(server, max_body_bytes=4096)

            for _ in range(3):
                self.assertEqual(client.connections_raw(), body)
                self.assertEqual(client.last_body_bytes, 4096)
            self.assertEqual(server.accepted, 1)

    def test_oversized_body_returns_none_and_next_call_redials(self) -> None:
        for kind in ("raw", "chunked", "chunked_one"):
            with self.subTest(kind=kind):
                oversized = _json_body_of_size(8192)
                with _FakeControllerServer([(kind, oversized), ("raw", b"{}")]) as server:
                    client = self._client(server, max_body_bytes=4096)

                    self.assertIsNone(client.connections_raw())
                    self.assertGreater(client.last_body_bytes, 4096)
                    self.assertEqual(client.connections_raw(), b"{}")
                    self.assertEqual(server.accepted, 2)
                    self.assertEqual(client.connect_count, 2)
                    self.assertEqual(client.request_count, 2)

    def test_reconnects_once_after_server_closes_the_idle_socket(self) -> None:
        with _FakeControllerServer(
            [("json_silent_close", {"id": "a"}), ("raw", b'{"id":"b"}'), ("raw", b'{"id":"c"}')]
        ) as server:
            client = self._client(server)

            self.assertEqual(client.connections_raw(), json.dumps({"id": "a"}).encode())
            self.assertEqual(client.connections_raw(), b'{"id":"b"}')
            self.assertEqual(client.connections_raw(), b'{"id":"c"}')

            self.assertEqual(server.accepted, 2)
            self.assertEqual(client.connect_count, 2)
            self.assertEqual(client.request_count, 4)
            self.assertEqual(len(server.requests), 3)

    def test_connection_close_response_redials_without_retry(self) -> None:
        with _FakeControllerServer([("json_close", {"connections": []}), ("raw", b"{}")]) as server:
            client = self._client(server)

            client.connections_raw()
            client.connections_raw()

            self.assertEqual(server.accepted, 2)
            self.assertEqual(client.connect_count, 2)
            self.assertEqual(client.request_count, 2)

    def test_phase_timeout_raises_and_next_call_redials(self) -> None:
        with _FakeControllerServer([("stall",), ("raw", b"{}")]) as server:
            client = self._client(server)

            started = time.perf_counter()
            with self.assertRaises(MihomoAPIError) as raised:
                client.connections_raw(timeout_seconds=0.2)
            elapsed = time.perf_counter() - started
            server.release.set()

            self.assertIs(type(raised.exception), MihomoAPIError)
            self.assertIn("GET /connections failed", str(raised.exception))
            self.assertLess(elapsed, 1.5)
            self.assertEqual(client.request_count, 1)
            self.assertEqual(client.connections_raw(), b"{}")
            self.assertEqual(server.accepted, 2)

    def test_fresh_socket_dropped_without_reply_is_not_retried(self) -> None:
        with _FakeControllerServer([("drop",), ("raw", b"{}")]) as server:
            client = self._client(server)

            with self.assertRaises(MihomoAPIError) as raised:
                client.connections_raw()

            self.assertIsInstance(
                raised.exception.__cause__,
                (http.client.RemoteDisconnected, ConnectionResetError, ConnectionAbortedError),
            )
            self.assertEqual(client.request_count, 1)
            self.assertEqual(client.connect_count, 1)
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(client.connections_raw(), b"{}")
            self.assertEqual(server.accepted, 2)

    def test_refused_dial_raises_without_retry(self) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        client = MihomoControllerKeepAlive(base_url=f"http://127.0.0.1:{port}")
        self.addCleanup(client.close)

        with self.assertRaises(MihomoAPIError):
            client.connections_raw()

        self.assertEqual(client.connect_count, 1)
        self.assertEqual(client.request_count, 1)

    def test_error_status_raises_and_discards_the_socket(self) -> None:
        with _FakeControllerServer([("status", 401), ("raw", b"{}")]) as server:
            client = self._client(server)

            with self.assertRaises(MihomoAPIError) as raised:
                client.connections_raw()

            self.assertIs(type(raised.exception), MihomoAPIError)
            self.assertIn("HTTP 401", str(raised.exception))
            self.assertEqual(client.connections_raw(), b"{}")
            self.assertEqual(server.accepted, 2)

    def test_bodies_are_not_checked_and_the_socket_stays_usable(self) -> None:
        with _FakeControllerServer(
            [("raw", b'["private-destination.example"]'), ("raw", b'{"host":"private'), ("raw", b"{}")]
        ) as server:
            client = self._client(server)

            self.assertEqual(client.connections_raw(), b'["private-destination.example"]')
            self.assertEqual(client.connections_raw(), b'{"host":"private')
            self.assertEqual(client.connections_raw(), b"{}")
            self.assertEqual(server.accepted, 1)

    def test_close_is_idempotent_and_next_call_redials(self) -> None:
        with _FakeControllerServer() as server:
            client = self._client(server)

            client.connections_raw()
            client.close()
            self.assertTrue(server.wait_disconnects(1, timeout=1.0))
            client.close()
            client.connections_raw()

            self.assertEqual(server.accepted, 2)
            self.assertEqual(client.connect_count, 2)

    def test_close_during_a_reused_socket_read_raises_without_retry(self) -> None:
        with _FakeControllerServer(
            [("raw", b"{}"), ("delayed", 0.8, {"connections": [{"id": "late"}]}), ("raw", b'{"after":1}')]
        ) as server:
            client = self._client(server)
            client.connections_raw()  # the next call reuses this socket
            outcome: list[object] = []

            def poll() -> None:
                try:
                    outcome.append(client.connections_raw(timeout_seconds=2.0))
                except BaseException as exc:  # asserted below
                    outcome.append(exc)

            worker = threading.Thread(target=poll, daemon=True)
            worker.start()
            self.assertTrue(server.reply_pending.wait(2.0))
            client.close()
            worker.join(4.0)

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], MihomoAPIError)
            self.assertTrue(server.eof_before_reply.wait(1.0))
            self.assertEqual(client.request_count, 2)
            self.assertEqual(len(server.requests), 2)
            self.assertEqual(client.connections_raw(), b'{"after":1}')
            self.assertEqual(server.accepted, 2)

    def test_both_methods_on_one_client(self) -> None:
        with _FakeControllerServer(
            [("raw", b'{"a":1}'), ("raw", b'{"b":2}'), ("raw", b'{"c":3}')]
        ) as server:
            client = self._client(server)

            self.assertEqual(client.connections(), {"a": 1})
            self.assertEqual(client.connections_raw(), b'{"b":2}')
            self.assertEqual(client.connections(), {"c": 3})
            # One socket per transport; close() drops both.
            self.assertEqual(server.accepted, 2)
            client.close()
            self.assertTrue(server.wait_disconnects(2, timeout=1.0))

    def test_a_secret_http_client_would_refuse_is_refused(self) -> None:
        with _FakeControllerServer() as server:
            client = MihomoControllerKeepAlive(base_url=server.base_url, secret="bad\r\nX-Injected: 1")
            self.addCleanup(client.close)

            with self.assertRaises(MihomoAPIError):
                client.connections_raw()
            self.assertEqual(server.requests, [])

    def test_from_client_uses_controller_url_prefix_and_secret(self) -> None:
        with _FakeControllerServer() as server:
            api_client = MihomoAPIClient(base_url=f"{server.base_url}/ctl/", secret="abc")
            client = MihomoControllerKeepAlive.from_client(api_client)
            self.addCleanup(client.close)

            client.connections_raw()

            self.assertEqual(server.requests, [("/ctl/connections", "Bearer abc")])


if __name__ == "__main__":
    unittest.main()
