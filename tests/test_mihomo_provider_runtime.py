from __future__ import annotations

import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

import yaml

from desktop_runtime import DesktopRuntime
from mihomo_api import MihomoAPIClient, MihomoAPIError
from tier3_core_provider import atomic_replace_if_changed
from windows_subprocess import hidden_subprocess_kwargs


class _ProbeAndStreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    stream_opened = threading.Event()
    release_stream = threading.Event()

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        if self.path == "/generate_204":
            # Mihomo's delay endpoint treats a rounded 0 ms result as an
            # error.  Give this loopback fixture a small deterministic RTT so
            # a successful 204 cannot be reported as controller HTTP 503.
            time.sleep(0.005)
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Length", "9")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"start")
            self.wfile.flush()
            type(self).stream_opened.set()
            type(self).release_stream.wait(timeout=5.0)
            self.wfile.write(b"-end")
            self.wfile.flush()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler contract
        # Mihomo v1.19.27 uses HEAD for URLTest.  Returning the same status as
        # GET is essential: /delay can still report a non-zero RTT when an
        # unexpected status marks the per-URL health record unavailable.
        if self.path == "/generate_204":
            time.sleep(0.005)
            self.send_response(204)
        else:
            self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _ProbeAndStreamServer(ThreadingHTTPServer):
    # Provider and fallback startup checks may arrive as two concurrent waves.
    # The stdlib default backlog of five can manufacture refused connections
    # before the test ever exercises Mihomo's provider-update contract.
    request_queue_size = 64


class MihomoProviderRuntimeTests(unittest.TestCase):
    @staticmethod
    def _reserve_port(used: set[int]) -> int:
        while True:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind(("127.0.0.1", 0))
                port = int(sock.getsockname()[1])
            if port not in used:
                used.add(port)
                return port

    @staticmethod
    def _provider_payload(names: list[str]) -> bytes:
        proxies = [
            {"name": name, "type": "direct", "udp": True}
            for name in names
        ]
        proxies.append({"name": "REJECT", "type": "reject"})
        return yaml.safe_dump(
            {"proxies": proxies},
            allow_unicode=True,
            sort_keys=False,
        ).encode("utf-8")

    def test_bundled_provider_update_fixed_readback_and_connection_drain(self) -> None:
        binary = Path(__file__).resolve().parents[1] / "bin" / "mihomo.exe"
        if not binary.is_file():
            self.skipTest("bundled mihomo.exe is unavailable")

        version = subprocess.run(
            [str(binary), "-v"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            **hidden_subprocess_kwargs(),
        )
        self.assertEqual(version.returncode, 0, version.stdout + version.stderr)
        self.assertIn("v1.19.27", version.stdout + version.stderr)

        used: set[int] = set()
        mixed_port = self._reserve_port(used)
        controller_port = self._reserve_port(used)
        origin_port = self._reserve_port(used)
        _ProbeAndStreamHandler.stream_opened.clear()
        _ProbeAndStreamHandler.release_stream.clear()
        origin = _ProbeAndStreamServer(
            ("127.0.0.1", origin_port),
            _ProbeAndStreamHandler,
        )
        origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
        origin_thread.start()
        self.addCleanup(origin_thread.join, 2.0)
        self.addCleanup(origin.server_close)
        self.addCleanup(origin.shutdown)

        process: subprocess.Popen[str] | None = None
        stream: socket.socket | None = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider_path = root / "tier3b-runtime.yaml"
            provider_path.write_bytes(self._provider_payload(["leaf-a", "leaf-b"]))
            config_path = root / "config.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "mixed-port": mixed_port,
                        "external-controller": f"127.0.0.1:{controller_port}",
                        "allow-lan": False,
                        "mode": "rule",
                        "log-level": "silent",
                        "ipv6": False,
                        "proxy-providers": {
                            "TIER3B-RUNTIME": {
                                "type": "file",
                                "path": "./tier3b-runtime.yaml",
                                "health-check": {
                                    "enable": True,
                                    "url": (
                                        f"http://127.0.0.1:{origin_port}/generate_204"
                                    ),
                                    "interval": 300,
                                    "lazy": False,
                                },
                            }
                        },
                        "proxy-groups": [
                            {
                                "name": "TIER3-AUTO",
                                "type": "fallback",
                                "use": ["TIER3B-RUNTIME"],
                                "url": (
                                    f"http://127.0.0.1:{origin_port}/generate_204"
                                ),
                                "expected-status": 204,
                                "interval": 300,
                                "lazy": False,
                                "empty-fallback": "REJECT",
                            },
                            {
                                "name": "FINAL-EXIT",
                                "type": "select",
                                "proxies": ["TIER3-AUTO", "REJECT"],
                            },
                        ],
                        "rules": ["MATCH,FINAL-EXIT"],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            log_path = root / "mihomo.log"
            with log_path.open("w+", encoding="utf-8") as log:
                try:
                    process = subprocess.Popen(
                        [str(binary), "-f", str(config_path), "-d", str(root)],
                        cwd=str(binary.parent.parent),
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        text=True,
                        **hidden_subprocess_kwargs(),
                    )
                    opener = build_opener(ProxyHandler({}))
                    client = MihomoAPIClient(
                        base_url=f"http://127.0.0.1:{controller_port}",
                        timeout=0.5,
                        opener=opener.open,
                    )
                    deadline = time.monotonic() + 8.0
                    while True:
                        if process.poll() is not None:
                            log.flush()
                            log.seek(0)
                            self.fail("Mihomo exited during startup: " + log.read())
                        try:
                            client.proxy_provider("TIER3B-RUNTIME")
                            break
                        except MihomoAPIError:
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(0.02)

                    fixed = client.select_proxy_with_readback(
                        "TIER3-AUTO",
                        "leaf-a",
                        mode="fixed",
                        timeout=1.0,
                        readback_timeout=1.0,
                    )
                    self.assertEqual(fixed.commit_state, "committed", fixed)
                    self.assertEqual(fixed.now, "leaf-a")
                    self.assertEqual(fixed.fixed, "leaf-a")

                    stream = socket.create_connection(
                        ("127.0.0.1", mixed_port),
                        timeout=2.0,
                    )
                    stream.settimeout(2.0)
                    stream.sendall(
                        (
                            f"GET http://127.0.0.1:{origin_port}/stream HTTP/1.1\r\n"
                            f"Host: 127.0.0.1:{origin_port}\r\n"
                            "Connection: close\r\n\r\n"
                        ).encode("ascii")
                    )
                    received = b""
                    while b"start" not in received:
                        chunk = stream.recv(4096)
                        self.assertTrue(chunk, received)
                        received += chunk
                    self.assertTrue(_ProbeAndStreamHandler.stream_opened.wait(1.0))

                    atomic_replace_if_changed(
                        provider_path,
                        self._provider_payload(["leaf-b"]),
                    )
                    client.update_proxy_provider("TIER3B-RUNTIME", timeout=1.5)
                    provider = client.proxy_provider(
                        "TIER3B-RUNTIME",
                        timeout=1.0,
                    )
                    self.assertEqual(
                        [str(item.get("name") or "") for item in provider["proxies"]],
                        ["leaf-b", "REJECT"],
                    )
                    group = client.proxy("TIER3-AUTO", timeout=1.0)
                    self.assertEqual(group.get("all"), ["leaf-b", "REJECT"])

                    _ProbeAndStreamHandler.release_stream.set()
                    while b"-end" not in received:
                        chunk = stream.recv(4096)
                        if not chunk:
                            break
                        received += chunk
                    self.assertIn(b"start-end", received)

                    atomic_replace_if_changed(
                        provider_path,
                        self._provider_payload([]),
                    )
                    client.update_proxy_provider("TIER3B-RUNTIME", timeout=1.5)
                    empty_group = client.proxy("TIER3-AUTO", timeout=1.0)
                    self.assertEqual(empty_group.get("all"), ["REJECT"])
                    self.assertEqual(empty_group.get("now"), "REJECT")
                finally:
                    _ProbeAndStreamHandler.release_stream.set()
                    if stream is not None:
                        stream.close()
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5.0)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5.0)

    def test_bundled_fallback_uses_healthy_fifth_member_and_survives_replay(
        self,
    ) -> None:
        binary = Path(__file__).resolve().parents[1] / "bin" / "mihomo.exe"
        if not binary.is_file():
            self.skipTest("bundled mihomo.exe is unavailable")

        used: set[int] = set()
        mixed_port = self._reserve_port(used)
        controller_port = self._reserve_port(used)
        origin_port = self._reserve_port(used)
        dead_proxy_port = self._reserve_port(used)
        origin = _ProbeAndStreamServer(
            ("127.0.0.1", origin_port),
            _ProbeAndStreamHandler,
        )
        origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
        origin_thread.start()
        self.addCleanup(origin_thread.join, 2.0)
        self.addCleanup(origin.server_close)
        self.addCleanup(origin.shutdown)

        process: subprocess.Popen[str] | None = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            probe_url = f"http://127.0.0.1:{origin_port}/generate_204"
            config_path = root / "config.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "mixed-port": mixed_port,
                        "external-controller": f"127.0.0.1:{controller_port}",
                        "allow-lan": False,
                        "mode": "rule",
                        "log-level": "silent",
                        "ipv6": False,
                        "proxies": [
                            *[
                                {
                                    "name": f"bad-{index:02d}",
                                    "type": "http",
                                    "server": "127.0.0.1",
                                    "port": dead_proxy_port,
                                }
                                for index in range(1, 5)
                            ],
                            {
                                "name": "healthy-fifth",
                                "type": "direct",
                                "udp": True,
                            },
                        ],
                        "proxy-groups": [
                            {
                                "name": "PROXY-FALLBACK",
                                "type": "fallback",
                                "proxies": [
                                    "bad-01",
                                    "bad-02",
                                    "bad-03",
                                    "bad-04",
                                    "healthy-fifth",
                                ],
                                "url": probe_url,
                                "expected-status": 204,
                                "interval": 300,
                                "lazy": False,
                            },
                            {
                                "name": "FINAL-EXIT",
                                "type": "select",
                                "proxies": ["PROXY-FALLBACK", "REJECT"],
                            },
                        ],
                        "rules": ["MATCH,FINAL-EXIT"],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            log_path = root / "mihomo.log"
            with log_path.open("w+", encoding="utf-8") as log:
                try:
                    process = subprocess.Popen(
                        [str(binary), "-f", str(config_path), "-d", str(root)],
                        cwd=str(binary.parent.parent),
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        text=True,
                        **hidden_subprocess_kwargs(),
                    )
                    client = MihomoAPIClient(
                        base_url=f"http://127.0.0.1:{controller_port}",
                        timeout=0.5,
                        opener=build_opener(ProxyHandler({})).open,
                    )
                    deadline = time.monotonic() + 8.0
                    while True:
                        if process.poll() is not None:
                            log.flush()
                            log.seek(0)
                            self.fail("Mihomo exited during startup: " + log.read())
                        try:
                            group = client.proxy("PROXY-FALLBACK")
                        except MihomoAPIError:
                            group = {}
                        if str(group.get("now") or "") == "healthy-fifth":
                            break
                        if time.monotonic() >= deadline:
                            self.fail(f"fallback did not select second member: {group!r}")
                        time.sleep(0.02)

                    opener = build_opener(
                        ProxyHandler(
                            {
                                "http": f"http://127.0.0.1:{mixed_port}",
                                "https": f"http://127.0.0.1:{mixed_port}",
                            }
                        )
                    )
                    with opener.open(probe_url, timeout=2.0) as response:
                        self.assertEqual(response.getcode(), 204)
                    self.assertEqual(
                        client.proxy("PROXY-FALLBACK").get("now"),
                        "healthy-fifth",
                    )

                    # ApplyConfig can rebuild selector runtime state.  Startup
                    # therefore replays only the policy branch and lets the
                    # native fallback choose from the complete loaded order;
                    # it must never pin one of the first four leaves.
                    rejected = client.select_proxy_with_readback(
                        "FINAL-EXIT",
                        "REJECT",
                        mode="selected",
                        timeout=1.0,
                        readback_timeout=1.0,
                    )
                    self.assertEqual(rejected.commit_state, "committed", rejected)
                    client.reload_config(str(config_path), force=True)
                    replayed = client.select_proxy_with_readback(
                        "FINAL-EXIT",
                        "PROXY-FALLBACK",
                        mode="selected",
                        timeout=1.0,
                        readback_timeout=1.0,
                    )
                    self.assertEqual(replayed.commit_state, "committed", replayed)
                    client.group_delay(
                        "PROXY-FALLBACK",
                        url=probe_url,
                        timeout_ms=5000,
                        request_timeout=7.0,
                    )
                    self.assertEqual(
                        client.proxy("PROXY-FALLBACK").get("now"),
                        "healthy-fifth",
                    )
                    for _ in range(2):
                        with opener.open(probe_url, timeout=2.0) as response:
                            self.assertEqual(response.getcode(), 204)
                finally:
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5.0)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5.0)

    def test_bundled_system_proxy_data_plane_survives_dns_1053_conflict(
        self,
    ) -> None:
        binary = Path(__file__).resolve().parents[1] / "bin" / "mihomo.exe"
        if not binary.is_file():
            self.skipTest("bundled mihomo.exe is unavailable")

        owned_listeners: list[socket.socket] = []
        for socket_type in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            listener = socket.socket(socket.AF_INET, socket_type)
            try:
                listener.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_EXCLUSIVEADDRUSE,
                    1,
                )
                listener.bind(("127.0.0.1", 1053))
                if socket_type == socket.SOCK_STREAM:
                    listener.listen(1)
                owned_listeners.append(listener)
            except OSError:
                listener.close()
        self.addCleanup(
            lambda: [listener.close() for listener in owned_listeners]
        )

        used: set[int] = {1053}
        mixed_port = self._reserve_port(used)
        controller_port = self._reserve_port(used)
        origin_port = self._reserve_port(used)
        origin = _ProbeAndStreamServer(
            ("127.0.0.1", origin_port),
            _ProbeAndStreamHandler,
        )
        origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
        origin_thread.start()
        self.addCleanup(origin_thread.join, 2.0)
        self.addCleanup(origin.server_close)
        self.addCleanup(origin.shutdown)

        process: subprocess.Popen[str] | None = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            probe_url = f"http://127.0.0.1:{origin_port}/generate_204"
            config_path = root / "config.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "mixed-port": mixed_port,
                        "external-controller": f"127.0.0.1:{controller_port}",
                        "allow-lan": False,
                        "mode": "rule",
                        "log-level": "warning",
                        "ipv6": False,
                        "dns": {
                            "enable": True,
                            "listen": "127.0.0.1:1053",
                            "ipv6": False,
                            "nameserver": ["1.1.1.1"],
                        },
                        "proxy-groups": [
                            {
                                "name": "FINAL-EXIT",
                                "type": "select",
                                "proxies": ["DIRECT", "REJECT"],
                            }
                        ],
                        "rules": ["MATCH,FINAL-EXIT"],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            log_path = root / "mihomo.log"
            with log_path.open("w+", encoding="utf-8") as log:
                try:
                    process = subprocess.Popen(
                        [str(binary), "-f", str(config_path), "-d", str(root)],
                        cwd=str(binary.parent.parent),
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        text=True,
                        **hidden_subprocess_kwargs(),
                    )
                    client = MihomoAPIClient(
                        base_url=f"http://127.0.0.1:{controller_port}",
                        timeout=0.5,
                        opener=build_opener(ProxyHandler({})).open,
                    )
                    deadline = time.monotonic() + 8.0
                    while True:
                        if process.poll() is not None:
                            log.flush()
                            log.seek(0)
                            self.fail("Mihomo exited during startup: " + log.read())
                        try:
                            client.proxy("FINAL-EXIT")
                            break
                        except MihomoAPIError:
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(0.02)

                    opener = build_opener(
                        ProxyHandler(
                            {
                                "http": f"http://127.0.0.1:{mixed_port}",
                                "https": f"http://127.0.0.1:{mixed_port}",
                            }
                        )
                    )
                    with opener.open(probe_url, timeout=2.0) as response:
                        self.assertEqual(response.getcode(), 204)
                finally:
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5.0)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5.0)

    def test_bundled_provider_growth_preserves_normal_and_fixed_route_without_reload(
        self,
    ) -> None:
        binary = Path(__file__).resolve().parents[1] / "bin" / "mihomo.exe"
        if not binary.is_file():
            self.skipTest("bundled mihomo.exe is unavailable")

        version = subprocess.run(
            [str(binary), "-v"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            **hidden_subprocess_kwargs(),
        )
        self.assertEqual(version.returncode, 0, version.stdout + version.stderr)
        self.assertIn("v1.19.27", version.stdout + version.stderr)

        used: set[int] = set()
        origin_port = self._reserve_port(used)
        origin = _ProbeAndStreamServer(
            ("127.0.0.1", origin_port),
            _ProbeAndStreamHandler,
        )
        origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
        origin_thread.start()
        self.addCleanup(origin_thread.join, 2.0)
        self.addCleanup(origin.server_close)
        self.addCleanup(origin.shutdown)

        initial_names = [f"leaf-{index:02d}" for index in range(7)]
        grown_names = [f"leaf-{index:02d}" for index in range(16)]
        probe_url = f"http://127.0.0.1:{origin_port}/generate_204"

        def exercise(*, fixed_target: str | None) -> None:
            mixed_port = self._reserve_port(used)
            controller_port = self._reserve_port(used)
            process: subprocess.Popen[str] | None = None
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                provider_path = root / "tier3b-runtime.yaml"
                provider_path.write_bytes(self._provider_payload(initial_names))
                config_path = root / "config.yaml"
                config_path.write_text(
                    yaml.safe_dump(
                        {
                            "mixed-port": mixed_port,
                            "external-controller": (
                                f"127.0.0.1:{controller_port}"
                            ),
                            "allow-lan": False,
                            "mode": "rule",
                            "log-level": "silent",
                            "ipv6": False,
                            "proxy-providers": {
                                "TIER3B-RUNTIME": {
                                    "type": "file",
                                    "path": "./tier3b-runtime.yaml",
                                    "health-check": {
                                        "enable": True,
                                        "url": probe_url,
                                        "interval": 300,
                                        "lazy": False,
                                    },
                                }
                            },
                            "proxy-groups": [
                                {
                                    "name": "TIER3-AUTO",
                                    "type": "fallback",
                                    "use": ["TIER3B-RUNTIME"],
                                    "url": probe_url,
                                    "expected-status": 204,
                                    "interval": 300,
                                    "lazy": False,
                                    "empty-fallback": "REJECT",
                                },
                                {
                                    "name": "FINAL-EXIT",
                                    "type": "select",
                                    "proxies": ["TIER3-AUTO", "REJECT"],
                                },
                            ],
                            "rules": ["MATCH,FINAL-EXIT"],
                        },
                        allow_unicode=True,
                        sort_keys=False,
                    ),
                    encoding="utf-8",
                )
                config_before = config_path.read_bytes()
                log_path = root / "mihomo.log"
                controller_requests: list[tuple[str, str]] = []
                opener = build_opener(ProxyHandler({}))

                def recording_open(request, **kwargs):
                    controller_requests.append(
                        (
                            str(request.get_method()),
                            str(urlsplit(request.full_url).path),
                        )
                    )
                    return opener.open(request, **kwargs)

                with log_path.open("w+", encoding="utf-8") as log:
                    try:
                        process = subprocess.Popen(
                            [str(binary), "-f", str(config_path), "-d", str(root)],
                            cwd=str(binary.parent.parent),
                            stdin=subprocess.DEVNULL,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            text=True,
                            **hidden_subprocess_kwargs(),
                        )
                        started_pid = int(process.pid)
                        client = MihomoAPIClient(
                            base_url=f"http://127.0.0.1:{controller_port}",
                            timeout=0.5,
                            opener=recording_open,
                        )
                        deadline = time.monotonic() + 8.0
                        while True:
                            if process.poll() is not None:
                                log.flush()
                                log.seek(0)
                                self.fail(
                                    "Mihomo exited during startup: " + log.read()
                                )
                            try:
                                provider = client.proxy_provider(
                                    "TIER3B-RUNTIME"
                                )
                                group = client.proxy("TIER3-AUTO")
                            except MihomoAPIError:
                                if time.monotonic() >= deadline:
                                    raise
                                time.sleep(0.02)
                                continue
                            provider_names = [
                                str(item.get("name") or "")
                                for item in provider.get("proxies", [])
                                if isinstance(item, dict)
                            ]
                            initial_health_evidence_ready = all(
                                isinstance(item.get("extra"), dict)
                                and isinstance(item["extra"].get(probe_url), dict)
                                and item["extra"][probe_url].get("alive") is True
                                and bool(item["extra"][probe_url].get("history"))
                                for item in provider.get("proxies", [])
                                if isinstance(item, dict)
                                and str(item.get("name") or "") in initial_names
                            )
                            if (
                                provider_names == [*initial_names, "REJECT"]
                                and group.get("all")
                                == [*initial_names, "REJECT"]
                                and str(group.get("now") or "") in initial_names
                                and initial_health_evidence_ready
                            ):
                                break
                            if time.monotonic() >= deadline:
                                self.fail(
                                    "initial provider/group did not stabilize: "
                                    f"provider={provider_names!r} group={group!r}"
                                )
                            time.sleep(0.02)

                        if fixed_target is not None:
                            # Fallback.Set only preserves a fixed member while
                            # it is alive for the group's exact test URL.  The
                            # provider and group inventories can be readable
                            # before the initial asynchronous health check has
                            # populated that per-URL evidence, so explicitly
                            # prewarm the target just as the production commit
                            # protocol does before issuing its single PUT.
                            warmed = client.proxy_delay(
                                fixed_target,
                                url=probe_url,
                                timeout_ms=1000,
                                expected_status=204,
                                request_timeout=1.5,
                            )
                            self.assertIsInstance(warmed.get("delay"), int, warmed)
                            warmed_target = client.proxy(fixed_target)
                            target_contract = (
                                warmed_target.get("extra", {}).get(probe_url, {})
                                if isinstance(warmed_target.get("extra"), dict)
                                else {}
                            )
                            self.assertIs(target_contract.get("alive"), True, warmed_target)
                            self.assertTrue(target_contract.get("history"), warmed_target)
                            group_before_fixed = client.proxy("TIER3-AUTO")
                            fixed = client.select_proxy_with_readback(
                                "TIER3-AUTO",
                                fixed_target,
                                mode="fixed",
                                timeout=1.0,
                                readback_timeout=1.0,
                            )
                            self.assertEqual(
                                fixed.commit_state,
                                "committed",
                                (fixed, warmed_target, group_before_fixed),
                            )
                            self.assertEqual(fixed.now, fixed_target)
                            self.assertEqual(fixed.fixed, fixed_target)

                        route_runtime = object.__new__(DesktopRuntime)
                        before_payload = client.proxies(timeout=1.0)
                        before_group = before_payload["proxies"]["TIER3-AUTO"]
                        before_guard = route_runtime._tier3_provider_route_guard(
                            {"proxies": before_payload}
                        )
                        active_leaf = str(before_group.get("now") or "")
                        self.assertIn(active_leaf, initial_names)
                        if fixed_target is not None:
                            self.assertEqual(active_leaf, fixed_target)
                            self.assertEqual(before_group.get("fixed"), fixed_target)

                        controller_requests.clear()
                        atomic_replace_if_changed(
                            provider_path,
                            self._provider_payload(grown_names),
                        )
                        client.update_proxy_provider(
                            "TIER3B-RUNTIME",
                            timeout=1.5,
                        )
                        provider_after = client.proxy_provider(
                            "TIER3B-RUNTIME",
                            timeout=1.0,
                        )
                        after_payload = client.proxies(timeout=1.0)
                        after_group = after_payload["proxies"]["TIER3-AUTO"]
                        after_guard = route_runtime._tier3_provider_route_guard(
                            {"proxies": after_payload}
                        )

                        self.assertEqual(after_guard, before_guard)
                        self.assertEqual(after_group.get("now"), active_leaf)
                        if fixed_target is not None:
                            self.assertEqual(after_group.get("fixed"), fixed_target)
                        self.assertEqual(
                            after_group.get("all"),
                            [*grown_names, "REJECT"],
                        )
                        self.assertEqual(
                            [
                                str(item.get("name") or "")
                                for item in provider_after.get("proxies", [])
                                if isinstance(item, dict)
                            ],
                            [*grown_names, "REJECT"],
                        )
                        self.assertEqual(process.poll(), None)
                        self.assertEqual(int(process.pid), started_pid)
                        self.assertEqual(config_path.read_bytes(), config_before)
                        self.assertEqual(
                            [
                                request
                                for request in controller_requests
                                if request[0] in {"PUT", "PATCH"}
                                and request[1] == "/configs"
                            ],
                            [],
                        )
                    finally:
                        if process is not None and process.poll() is None:
                            process.terminate()
                            try:
                                process.wait(timeout=5.0)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=5.0)

        with self.subTest(mode="normal"):
            exercise(fixed_target=None)
        with self.subTest(mode="fixed"):
            exercise(fixed_target="leaf-03")
if __name__ == "__main__":
    unittest.main()
