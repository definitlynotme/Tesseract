from __future__ import annotations

import asyncio
import base64
import copy
import http.client
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from clash_formatter import (
    build_bootstrap_clash_config,
    build_capture_disabled_clash_config,
    build_full_clash_config,
)
from runtime_source_transport import (
    RUNTIME_SOURCE_EGRESS_PROXY_NAME,
    RuntimeSourceListenerCredentials,
    RuntimeSourceListenerPorts,
    RuntimeSourcePhase,
    RuntimeSourceRouteKind,
    build_runtime_source_transport_generation,
    choose_runtime_source_listener_ports,
    race_runtime_source_fetch,
)
from windows_subprocess import hidden_subprocess_kwargs


def _credentials() -> dict[
    RuntimeSourceRouteKind,
    RuntimeSourceListenerCredentials,
]:
    return {
        RuntimeSourceRouteKind.FINAL_EXIT: RuntimeSourceListenerCredentials(
            "final-user",
            "final-secret",
        ),
        RuntimeSourceRouteKind.EGRESS: RuntimeSourceListenerCredentials(
            "egress-user",
            "egress-secret",
        ),
        RuntimeSourceRouteKind.DIRECT: RuntimeSourceListenerCredentials(
            "direct-user",
            "direct-secret",
        ),
    }


def _generation(*, include_egress: bool = True):
    ports = RuntimeSourceListenerPorts(
        final_exit=18101,
        egress=18102 if include_egress else None,
        direct=18103,
    )
    return build_runtime_source_transport_generation(
        runtime_generation="runtime-7",
        config_generation=11,
        ports=ports,
        egress_proxy_name=(
            RUNTIME_SOURCE_EGRESS_PROXY_NAME if include_egress else None
        ),
        credentials=_credentials(),
    )


class RuntimeSourceTransportTests(unittest.TestCase):
    @staticmethod
    def _reserve_loopback_port(used: set[int]) -> int:
        while True:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind(("127.0.0.1", 0))
                port = int(sock.getsockname()[1])
            finally:
                sock.close()
            if port not in used:
                used.add(port)
                return port

    def test_generation_builds_three_authenticated_loopback_listeners(self) -> None:
        generation = _generation()

        self.assertEqual(
            [listener.name for listener in generation.listeners],
            ["source-final-exit", "source-egress", "source-direct"],
        )
        payloads = generation.listener_payloads()
        self.assertEqual(
            [listener["proxy"] for listener in payloads],
            ["FINAL-EXIT", RUNTIME_SOURCE_EGRESS_PROXY_NAME, "DIRECT"],
        )
        for listener in payloads:
            self.assertEqual(listener["type"], "http")
            self.assertEqual(listener["listen"], "127.0.0.1")
            self.assertEqual(len(listener["users"]), 1)
            self.assertTrue(listener["users"][0]["username"])
            self.assertTrue(listener["users"][0]["password"])

    def test_credentials_and_authenticated_url_are_redacted_from_representations(self) -> None:
        generation = _generation()
        endpoints = generation.fetch_endpoints(
            phase=RuntimeSourcePhase.FORMAL,
            readiness=True,
        )
        endpoint = endpoints[0]

        self.assertIn("final-user:final-secret@", endpoint.proxy_url)
        self.assertEqual(
            endpoint.safe_summary()["proxy_url"],
            "http://<redacted>@127.0.0.1:18101",
        )
        rendered = " ".join(
            (
                repr(generation),
                repr(generation.listeners[0]),
                repr(endpoint),
                repr(endpoint.safe_summary()),
            )
        )
        for secret in (
            "final-user",
            "final-secret",
            "egress-user",
            "egress-secret",
            "direct-user",
            "direct-secret",
        ):
            self.assertNotIn(secret, rendered)

    def test_endpoint_order_reuses_last_success_then_applies_phase_default(self) -> None:
        generation = _generation()

        formal = generation.fetch_endpoints(
            phase="formal",
            readiness=True,
        )
        bootstrap = generation.fetch_endpoints(
            phase="bootstrap",
            readiness=True,
        )
        reused = generation.fetch_endpoints(
            phase="formal",
            readiness=True,
            last_successful_route="direct",
        )

        self.assertEqual(
            [endpoint.route_kind for endpoint in formal],
            [
                RuntimeSourceRouteKind.FINAL_EXIT,
                RuntimeSourceRouteKind.EGRESS,
                RuntimeSourceRouteKind.DIRECT,
            ],
        )
        self.assertEqual(
            [endpoint.route_kind for endpoint in bootstrap],
            [
                RuntimeSourceRouteKind.EGRESS,
                RuntimeSourceRouteKind.DIRECT,
                RuntimeSourceRouteKind.FINAL_EXIT,
            ],
        )
        self.assertEqual(reused[0].route_kind, RuntimeSourceRouteKind.DIRECT)
        self.assertEqual(
            [endpoint.stagger_ms for endpoint in formal],
            [0, 250, 500],
        )

    def test_port_choice_excludes_conflicts_and_never_reads_a_fourth_attempt(self) -> None:
        yielded: list[int] = []

        def attempts():
            for index, ports in enumerate(
                (
                    (7890, 19002, 19003),
                    (19011, 19011, 19013),
                    (19021, 19022, 19023),
                    (19031, 19032, 19033),
                ),
                start=1,
            ):
                yielded.append(index)
                yield ports

        selected = choose_runtime_source_listener_ports(
            attempts(),
            excluded_ports={7890, 9090, 1053},
        )

        self.assertEqual(selected.as_tuple(), (19021, 19022, 19023))
        self.assertEqual(yielded, [1, 2, 3])

    def test_port_choice_fails_after_three_conflicting_attempts(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "after 3 attempt"):
            choose_runtime_source_listener_ports(
                (
                    (7890, 18002, 18003),
                    (18011, 9090, 18013),
                    (18021, 18022, 1053),
                    (18031, 18032, 18033),
                ),
                excluded_ports={7890, 9090, 1053},
            )

    def test_packaged_mihomo_parser_accepts_private_listener_contract(self) -> None:
        binary = Path("bin/mihomo.exe")
        if not binary.exists():
            self.skipTest("bin/mihomo.exe is not available")
        generation = _generation(include_egress=False)
        config_yaml = build_full_clash_config(
            {"tier1": [], "tier2": [], "tier3": [], "exit": []},
            external_controller=None,
            mixed_port=18100,
            runtime_source_listeners=generation.listeners,
        )

        runtime_data = Path.cwd() / ".codex-test-runtime"
        runtime_data.mkdir(exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix="-runtime-source.yaml",
            dir=Path.cwd(),
            delete=False,
        )
        config_path = Path(handle.name)
        try:
            with handle:
                handle.write(config_yaml)
            result = subprocess.run(
                [
                    str(binary.resolve()),
                    "-t",
                    "-f",
                    str(config_path),
                    "-d",
                    str(runtime_data),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                **hidden_subprocess_kwargs(),
            )
        finally:
            config_path.unlink(missing_ok=True)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_capture_disabled_source_direct_preserves_ordinary_dns_and_routes(self) -> None:
        generation = _generation(include_egress=False)
        formal = build_full_clash_config(
            {"tier1": [], "tier2": [], "tier3": [], "exit": []},
            runtime_source_listeners=generation.listeners,
        )

        formal_payload = yaml.safe_load(formal)
        capture_disabled = yaml.safe_load(build_capture_disabled_clash_config(formal))

        self.assertNotIn("direct-nameserver", formal_payload["dns"])
        self.assertEqual(capture_disabled["dns"], formal_payload["dns"])
        self.assertEqual(capture_disabled["listeners"], formal_payload["listeners"])
        self.assertEqual(formal_payload["rules"], capture_disabled["rules"])
        self.assertEqual(formal_payload["mixed-port"], capture_disabled["mixed-port"])

    def test_capture_disabled_source_direct_preserves_explicit_direct_dns(self) -> None:
        generation = _generation(include_egress=False)
        formal_payload = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
                runtime_source_listeners=generation.listeners,
            )
        )
        formal_payload["dns"]["direct-nameserver"] = ["tcp://9.9.9.9"]
        formal_payload["dns"]["direct-nameserver-follow-policy"] = True

        capture_disabled = yaml.safe_load(
            build_capture_disabled_clash_config(formal_payload)
        )

        self.assertEqual(
            capture_disabled["dns"]["direct-nameserver"],
            ["tcp://9.9.9.9"],
        )
        self.assertTrue(
            capture_disabled["dns"]["direct-nameserver-follow-policy"]
        )

    def test_source_listener_startup_preserves_absent_disabled_and_empty_dns(self) -> None:
        generation = _generation(include_egress=False)
        base = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
            )
        )
        dns_cases = (
            None,
            {},
            {"enable": False},
            {
                "enable": True,
                "nameserver": ["tcp://127.0.0.1:15353"],
                "nameserver-policy": {"+.example.test": "tcp://127.0.0.1:15354"},
                "direct-nameserver": [],
            },
        )
        for builder in (build_capture_disabled_clash_config, build_bootstrap_clash_config):
            for dns in dns_cases:
                with self.subTest(builder=builder.__name__, dns=dns):
                    config = copy.deepcopy(base)
                    if dns is None:
                        config.pop("dns", None)
                    else:
                        config["dns"] = copy.deepcopy(dns)
                    original = copy.deepcopy(config)

                    result = yaml.safe_load(
                        builder(config, runtime_source_listeners=generation.listeners)
                    )

                    self.assertEqual(config, original)
                    self.assertEqual("dns" in result, "dns" in original)
                    self.assertEqual(result.get("dns"), dns)
                    listeners = generation.listener_payloads()
                    if builder is build_bootstrap_clash_config:
                        for listener in listeners:
                            if listener["proxy"] == "FINAL-EXIT":
                                listener["proxy"] = "PROXY-FALLBACK"
                    self.assertEqual(result["listeners"], listeners)

    def test_real_mihomo_private_listeners_keep_auth_routes_and_user_dns(self) -> None:
        binary = Path("bin/mihomo.exe").resolve()
        if not binary.exists():
            self.skipTest("bin/mihomo.exe is not available")

        origin_hits: list[str] = []

        class OriginHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                origin_hits.append(self.path)
                body = b"origin-ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args) -> None:
                return

        origin = ThreadingHTTPServer(("127.0.0.1", 0), OriginHandler)
        origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
        origin_thread.start()

        dns_queries: list[str] = []

        class DnsHandler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                self.connection.settimeout(2)
                while length_bytes := self.rfile.read(2):
                    if len(length_bytes) != 2:
                        return
                    query = self.rfile.read(struct.unpack("!H", length_bytes)[0])
                    offset = 12
                    labels: list[str] = []
                    while query[offset]:
                        size = query[offset]
                        offset += 1
                        labels.append(query[offset:offset + size].decode("ascii"))
                        offset += size
                    question_end = offset + 5
                    qtype = struct.unpack("!H", query[offset + 1:offset + 3])[0]
                    dns_queries.append(".".join(labels))
                    answer = b""
                    if qtype == 1:
                        answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 30, 4)
                        answer += socket.inet_aton("127.0.0.1")
                    response = query[:2] + struct.pack("!HHHHH", 0x8180, 1, bool(answer), 0, 0)
                    response += query[12:question_end] + answer
                    self.wfile.write(struct.pack("!H", len(response)) + response)
                    self.wfile.flush()

        dns_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), DnsHandler)
        dns_server.daemon_threads = True
        dns_thread = threading.Thread(target=dns_server.serve_forever, daemon=True)
        dns_thread.start()

        used_ports = {int(origin.server_address[1]), int(dns_server.server_address[1])}
        mixed_port = self._reserve_loopback_port(used_ports)
        listener_ports = RuntimeSourceListenerPorts(
            final_exit=self._reserve_loopback_port(used_ports),
            egress=self._reserve_loopback_port(used_ports),
            direct=self._reserve_loopback_port(used_ports),
        )
        generation = build_runtime_source_transport_generation(
            runtime_generation="real-runtime",
            config_generation="real-config",
            ports=listener_ports,
            egress_proxy_name="REJECT",
            credentials=_credentials(),
        )
        config = {
            "mixed-port": mixed_port,
            "allow-lan": False,
            "mode": "rule",
            "log-level": "silent",
            "ipv6": False,
            "dns": {
                "enable": True,
                "ipv6": False,
                "use-hosts": False,
                "use-system-hosts": False,
                "nameserver": [f"tcp://127.0.0.1:{dns_server.server_address[1]}"],
            },
            "proxies": [],
            "proxy-groups": [
                {
                    "name": "FINAL-EXIT",
                    "type": "select",
                    "proxies": ["DIRECT"],
                }
            ],
            "listeners": generation.listener_payloads(),
            "rules": [
                "DOMAIN,ordinary-direct.test,DIRECT",
                "IP-CIDR,127.0.0.1/32,REJECT,no-resolve",
                "MATCH,REJECT",
            ],
        }
        origin_url = f"http://private-source.test:{int(origin.server_address[1])}/source"
        ordinary_url = f"http://ordinary-direct.test:{int(origin.server_address[1])}/ordinary"
        runtime_data = Path.cwd() / ".codex-test-runtime"
        runtime_data.mkdir(exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix="-runtime-source-live.yaml",
            dir=Path.cwd(),
            delete=False,
        )
        config_path = Path(handle.name)
        process: subprocess.Popen[str] | None = None

        def proxy_get(
            port: int,
            credentials: RuntimeSourceListenerCredentials | None = None,
            *,
            url: str = origin_url,
        ) -> tuple[int, bytes]:
            headers = {"Connection": "close"}
            if credentials is not None:
                token = base64.b64encode(
                    f"{credentials.username}:{credentials.password}".encode("utf-8")
                ).decode("ascii")
                headers["Proxy-Authorization"] = f"Basic {token}"
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            try:
                connection.request("GET", url, headers=headers)
                response = connection.getresponse()
                return int(response.status), response.read()
            finally:
                connection.close()

        try:
            with handle:
                handle.write(build_capture_disabled_clash_config(config))
            process = subprocess.Popen(
                [
                    str(binary),
                    "-f",
                    str(config_path),
                    "-d",
                    str(runtime_data),
                ],
                cwd=str(Path.cwd()),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                **hidden_subprocess_kwargs(),
            )
            ready_deadline = time.monotonic() + 8.0
            ports_to_wait = {
                mixed_port,
                listener_ports.final_exit,
                int(listener_ports.egress or 0),
                listener_ports.direct,
            }
            while ports_to_wait and time.monotonic() < ready_deadline:
                if process.poll() is not None:
                    break
                for port in tuple(ports_to_wait):
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            ports_to_wait.remove(port)
                    except OSError:
                        pass
                if ports_to_wait:
                    time.sleep(0.05)
            if ports_to_wait:
                output = process.communicate(timeout=2)[0] if process.poll() is not None else ""
                self.fail(
                    f"mihomo listener startup failed for {sorted(ports_to_wait)}: {output}"
                )

            listeners_by_route = {
                listener.route_kind: listener for listener in generation.listeners
            }
            for listener in generation.listeners:
                status, _body = proxy_get(listener.port)
                self.assertEqual(status, 407, listener.route_kind.value)

            final_listener = listeners_by_route[RuntimeSourceRouteKind.FINAL_EXIT]
            direct_listener = listeners_by_route[RuntimeSourceRouteKind.DIRECT]
            egress_listener = listeners_by_route[RuntimeSourceRouteKind.EGRESS]
            self.assertEqual(
                proxy_get(final_listener.port, final_listener.credentials),
                (200, b"origin-ok"),
            )
            self.assertEqual(
                proxy_get(direct_listener.port, direct_listener.credentials),
                (200, b"origin-ok"),
            )
            self.assertEqual(
                egress_listener.proxy,
                "REJECT",
            )
            try:
                egress_status, _egress_body = proxy_get(
                    egress_listener.port,
                    egress_listener.credentials,
                )
            except (OSError, http.client.HTTPException):
                egress_status = 0
            self.assertNotEqual(egress_status, 200)

            try:
                mixed_status, _mixed_body = proxy_get(mixed_port)
            except (OSError, http.client.HTTPException):
                mixed_status = 0
            self.assertNotEqual(mixed_status, 200)
            self.assertEqual(proxy_get(mixed_port, url=ordinary_url), (200, b"origin-ok"))
            self.assertEqual(len(origin_hits), 3)
            self.assertIn("private-source.test", dns_queries)
            self.assertIn("ordinary-direct.test", dns_queries)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process is not None and process.stdout is not None:
                process.stdout.close()
            origin.shutdown()
            origin.server_close()
            origin_thread.join(timeout=2)
            dns_server.shutdown()
            dns_server.server_close()
            dns_thread.join(timeout=2)
            config_path.unlink(missing_ok=True)


class RuntimeSourceFetchRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_ready_lane_is_rebased_to_zero_stagger(self) -> None:
        generation = _generation()
        endpoints = generation.fetch_endpoints(
            phase="bootstrap",
            readiness={
                RuntimeSourceRouteKind.FINAL_EXIT: False,
                RuntimeSourceRouteKind.EGRESS: True,
                RuntimeSourceRouteKind.DIRECT: True,
            },
            last_successful_route=RuntimeSourceRouteKind.FINAL_EXIT,
        )
        calls: list[RuntimeSourceRouteKind] = []
        started_at = time.monotonic()

        async def fetch(endpoint):
            calls.append(endpoint.route_kind)
            return endpoint.route_kind.value

        result = await race_runtime_source_fetch(
            endpoints,
            fetch,
            lambda _endpoint, _value: True,
        )

        self.assertTrue(result.succeeded)
        self.assertEqual(result.winner.route_kind, RuntimeSourceRouteKind.EGRESS)
        self.assertEqual(calls, [RuntimeSourceRouteKind.EGRESS])
        self.assertLess(time.monotonic() - started_at, 0.15)

    async def test_winner_callback_runs_once_and_other_routes_are_cancelled(self) -> None:
        generation = _generation()
        endpoints = tuple(
            listener.endpoint(ready=True, priority=0, stagger_ms=0)
            for listener in generation.listeners
        )
        cancelled: list[RuntimeSourceRouteKind] = []
        published: list[RuntimeSourceRouteKind] = []

        async def fetch(endpoint):
            if endpoint.route_kind is RuntimeSourceRouteKind.FINAL_EXIT:
                await asyncio.sleep(0.01)
                return "winner"
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(endpoint.route_kind)
                raise
            return "late"

        async def publish(endpoint, _value):
            published.append(endpoint.route_kind)

        result = await race_runtime_source_fetch(
            endpoints,
            fetch,
            lambda _endpoint, value: value == "winner",
            on_winner=publish,
        )

        self.assertEqual(result.value, "winner")
        self.assertEqual(published, [RuntimeSourceRouteKind.FINAL_EXIT])
        self.assertCountEqual(
            cancelled,
            [RuntimeSourceRouteKind.EGRESS, RuntimeSourceRouteKind.DIRECT],
        )

    async def test_exception_details_cannot_leak_credentials(self) -> None:
        endpoints = _generation(include_egress=False).fetch_endpoints(
            phase="formal",
            readiness=True,
        )

        async def fetch(endpoint):
            raise RuntimeError(endpoint.proxy_url)

        result = await race_runtime_source_fetch(
            endpoints,
            fetch,
            lambda _endpoint, _value: True,
        )

        self.assertFalse(result.succeeded)
        rendered = repr(result)
        self.assertNotIn("final-user", rendered)
        self.assertNotIn("final-secret", rendered)
        self.assertNotIn("direct-user", rendered)
        self.assertNotIn("direct-secret", rendered)
        self.assertEqual(
            result.errors,
            ("final_exit:RuntimeError", "direct:RuntimeError"),
        )

    async def test_race_without_winner_reports_every_attempted_lane(self) -> None:
        # The collector persists these per-lane outcomes and tells the route
        # memory owner which lanes failed, so a lost race must account for
        # every lane that ran, in a safe ``route:outcome`` form.
        endpoints = _generation().fetch_endpoints(
            phase="formal",
            readiness=True,
            stagger_ms=0,
        )
        published: list[RuntimeSourceRouteKind] = []

        async def fetch(endpoint):
            if endpoint.route_kind is RuntimeSourceRouteKind.EGRESS:
                raise ConnectionResetError(endpoint.proxy_url)
            return "no nodes"

        result = await race_runtime_source_fetch(
            endpoints,
            fetch,
            lambda _endpoint, _value: False,
            on_winner=lambda endpoint, _value: published.append(endpoint.route_kind),
        )

        self.assertFalse(result.succeeded)
        self.assertIsNone(result.value)
        self.assertEqual(published, [])
        self.assertCountEqual(
            result.attempted_routes,
            [
                RuntimeSourceRouteKind.FINAL_EXIT,
                RuntimeSourceRouteKind.EGRESS,
                RuntimeSourceRouteKind.DIRECT,
            ],
        )
        self.assertCountEqual(
            result.errors,
            (
                "final_exit:rejected",
                "egress:ConnectionResetError",
                "direct:rejected",
            ),
        )
        self.assertNotIn("egress-secret", repr(result))


if __name__ == "__main__":
    unittest.main()
