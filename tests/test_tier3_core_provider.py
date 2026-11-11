"""Tests for the Tier3B local Mihomo proxy-provider projection."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import yaml

from tier3_core_provider import (
    Tier3CoreProviderError,
    atomic_replace_if_changed,
    project_tier3b_file_provider,
    replace_provider_file_transactionally,
    validate_proxy_provider_readback,
)


def _runtime_yaml() -> str:
    return yaml.safe_dump(
        {
            "mixed-port": 7890,
            "proxies": [
                {"name": "tier1", "type": "ss", "server": "tier1.example"},
                {"name": "tier3-b", "type": "ss", "server": "b.example"},
                {"name": "tier3-a", "type": "vmess", "server": "a.example"},
                {"name": "user-tier3", "type": "trojan", "server": "user.example"},
            ],
            "proxy-groups": [
                {
                    "name": "TIER3-AUTO",
                    "type": "fallback",
                    "empty-fallback": "REJECT",
                    "proxies": ["user-tier3", "tier3-a", "tier3-b", "REJECT"],
                    "url": "https://www.gstatic.com/generate_204",
                    "interval": 300,
                },
                {
                    "name": "PROXY-FALLBACK",
                    "type": "fallback",
                    "empty-fallback": "REJECT",
                    "proxies": ["TIER3-AUTO"],
                    "url": "https://www.gstatic.com/generate_204",
                    "interval": 300,
                    "lazy": False,
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
    )


@contextmanager
def _writable_temporary_directory() -> Iterator[str]:
    workspace = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(
        prefix=".tmp-tier3-core-provider-",
        dir=workspace,
    ) as directory:
        yield directory


class Tier3CoreProviderProjectionTests(unittest.TestCase):
    def test_projection_moves_requested_nodes_in_group_order(self) -> None:
        projection = project_tier3b_file_provider(
            _runtime_yaml(),
            ["tier3-b", "tier3-a"],
            provider_name="TIER3B-FILE",
            provider_path="./runtime/tier3b.yaml",
        )

        self.assertEqual(projection.proxy_names, ("tier3-a", "tier3-b"))
        provider = yaml.safe_load(projection.provider_yaml)
        self.assertEqual(
            [proxy["name"] for proxy in provider["proxies"]],
            ["tier3-a", "tier3-b", "REJECT"],
        )
        self.assertEqual(provider["proxies"][0]["server"], "a.example")
        self.assertEqual(provider["proxies"][-1], {"name": "REJECT", "type": "reject"})
        self.assertEqual(
            projection.provider_member_names,
            ("tier3-a", "tier3-b", "REJECT"),
        )

        main = yaml.safe_load(projection.main_yaml)
        self.assertEqual(
            [proxy["name"] for proxy in main["proxies"]],
            ["tier1", "user-tier3"],
        )
        groups = {group["name"]: group for group in main["proxy-groups"]}
        tier3 = groups["TIER3-AUTO"]
        self.assertEqual(tier3["proxies"], ["user-tier3"])
        self.assertEqual(tier3["use"], ["TIER3B-FILE"])
        self.assertEqual(tier3["empty-fallback"], "REJECT")
        self.assertFalse(tier3["lazy"])
        self.assertEqual(tier3["url"], "https://www.gstatic.com/generate_204")
        self.assertEqual(
            main["proxy-providers"]["TIER3B-FILE"],
            {"type": "file", "path": "./runtime/tier3b.yaml"},
        )
        self.assertEqual(groups["FINAL-EXIT"]["proxies"], ["TIER3-AUTO", "REJECT"])
        self.assertEqual(groups["PROXY-FALLBACK"]["proxies"], ["TIER3-AUTO"])
        self.assertFalse(groups["PROXY-FALLBACK"]["lazy"])
        self.assertEqual(len(projection.provider_digest), 64)
        self.assertEqual(len(projection.member_digest), 64)

    def test_projection_does_not_mutate_mapping_input(self) -> None:
        source = yaml.safe_load(_runtime_yaml())
        original = yaml.safe_dump(source, sort_keys=False)

        project_tier3b_file_provider(source, ["tier3-a", "tier3-b"])

        self.assertEqual(yaml.safe_dump(source, sort_keys=False), original)

    def test_projection_with_only_tier3b_members_uses_provider_only(self) -> None:
        payload = yaml.safe_load(_runtime_yaml())
        payload["proxy-groups"][0]["proxies"] = ["tier3-a", "tier3-b", "REJECT"]

        projection = project_tier3b_file_provider(
            payload,
            ["tier3-a", "tier3-b"],
        )

        main = yaml.safe_load(projection.main_yaml)
        group = next(
            group for group in main["proxy-groups"] if group["name"] == "TIER3-AUTO"
        )
        self.assertNotIn("proxies", group)
        self.assertEqual(group["use"], ["TIER3B-RUNTIME"])
        self.assertEqual(group["empty-fallback"], "REJECT")
        self.assertFalse(group["lazy"])
        parent = next(
            group for group in main["proxy-groups"] if group["name"] == "PROXY-FALLBACK"
        )
        self.assertEqual(parent["proxies"], ["TIER3-AUTO"])
        self.assertFalse(parent["lazy"])
        provider = yaml.safe_load(projection.provider_yaml)
        self.assertEqual(
            [proxy["name"] for proxy in provider["proxies"]],
            ["tier3-a", "tier3-b", "REJECT"],
        )

    def test_main_graph_is_invariant_between_zero_and_nonzero_provider_inventory(self) -> None:
        populated = yaml.safe_load(_runtime_yaml())
        populated_projection = project_tier3b_file_provider(
            populated,
            ["tier3-a", "tier3-b"],
        )

        empty = yaml.safe_load(_runtime_yaml())
        empty["proxies"] = [
            proxy
            for proxy in empty["proxies"]
            if proxy["name"] not in {"tier3-a", "tier3-b"}
        ]
        empty["proxy-groups"][0]["proxies"] = ["user-tier3", "REJECT"]
        empty_projection = project_tier3b_file_provider(empty, [])

        self.assertEqual(
            yaml.safe_load(populated_projection.main_yaml),
            yaml.safe_load(empty_projection.main_yaml),
        )

    def test_empty_durable_pool_remains_provider_backed_and_fail_closed(self) -> None:
        payload = yaml.safe_load(_runtime_yaml())
        payload["proxies"] = [
            proxy
            for proxy in payload["proxies"]
            if proxy["name"] not in {"tier3-a", "tier3-b", "user-tier3"}
        ]
        payload["proxy-groups"][0]["proxies"] = ["REJECT"]

        projection = project_tier3b_file_provider(payload, [])

        self.assertEqual(projection.proxy_names, ())
        self.assertEqual(projection.provider_member_names, ("REJECT",))
        provider = yaml.safe_load(projection.provider_yaml)
        self.assertEqual(
            provider["proxies"],
            [{"name": "REJECT", "type": "reject"}],
        )
        main = yaml.safe_load(projection.main_yaml)
        group = next(
            group for group in main["proxy-groups"] if group["name"] == "TIER3-AUTO"
        )
        self.assertNotIn("proxies", group)
        self.assertEqual(group["use"], ["TIER3B-RUNTIME"])
        self.assertEqual(group["empty-fallback"], "REJECT")

    def test_projection_rejects_missing_or_ambiguous_members(self) -> None:
        with self.assertRaisesRegex(Tier3CoreProviderError, "definitions are missing"):
            project_tier3b_file_provider(_runtime_yaml(), ["missing"])

        payload = yaml.safe_load(_runtime_yaml())
        payload["proxy-groups"][0]["proxies"].remove("tier3-b")
        with self.assertRaisesRegex(Tier3CoreProviderError, "does not route"):
            project_tier3b_file_provider(payload, ["tier3-b"])

        with self.assertRaisesRegex(Tier3CoreProviderError, "duplicate name"):
            project_tier3b_file_provider(_runtime_yaml(), ["tier3-a", "tier3-a"])

        interleaved = yaml.safe_load(_runtime_yaml())
        interleaved["proxy-groups"][0]["proxies"] = [
            "tier3-a",
            "user-tier3",
            "tier3-b",
            "REJECT",
        ]
        with self.assertRaisesRegex(Tier3CoreProviderError, "interleaves retained member"):
            project_tier3b_file_provider(interleaved, ["tier3-a", "tier3-b"])

    def test_projection_preserves_compatible_existing_provider_options(self) -> None:
        payload = yaml.safe_load(_runtime_yaml())
        payload["proxy-providers"] = {
            "other": {"type": "http", "url": "https://provider.example/list.yaml"},
            "TIER3B-RUNTIME": {
                "type": "file",
                "path": "./tier3b-runtime.yaml",
                "override": {"udp": True},
            },
        }

        main = yaml.safe_load(
            project_tier3b_file_provider(
                payload,
                ["tier3-a", "tier3-b"],
            ).main_yaml
        )

        self.assertIn("other", main["proxy-providers"])
        self.assertEqual(
            main["proxy-providers"]["TIER3B-RUNTIME"]["override"],
            {"udp": True},
        )

    def test_projection_rejects_overwriting_an_existing_group_provider_set(self) -> None:
        payload = yaml.safe_load(_runtime_yaml())
        payload["proxy-groups"][0]["use"] = ["unrelated-provider"]

        with self.assertRaisesRegex(Tier3CoreProviderError, "already points"):
            project_tier3b_file_provider(payload, ["tier3-a", "tier3-b"])

    def test_projection_keeps_formatter_real_site_health_contract_on_group(self) -> None:
        # Mihomo registers a ``use`` group's url/interval/expected-status as a
        # health-check task on the provider, so the provider-backed members are
        # checked on the real site only if the projection keeps the group's
        # contract and gives the file provider no health check of its own.
        from clash_formatter import build_full_clash_config
        from models import Node

        nodes = [
            Node(
                name=name,
                protocol="ss",
                uri=f"ss://{name}",
                address=f"{name}.example.com",
                port=8388,
                method="aes-128-gcm",
                password="pass",
                tier="tier3",
            )
            for name in ("tier3-a", "tier3-b")
        ]
        rendered = build_full_clash_config(
            {"tier1": [], "tier2": [], "tier3": nodes, "exit": []},
            probe_url="https://neutral.example/generate_204",
            tier3_health_check_url="https://real-site.example/generate_204",
        )

        projection = project_tier3b_file_provider(rendered, ["tier3-a", "tier3-b"])

        main = yaml.safe_load(projection.main_yaml)
        groups = {group["name"]: group for group in main["proxy-groups"]}
        tier3 = groups["TIER3-AUTO"]
        self.assertEqual(tier3["use"], ["TIER3B-RUNTIME"])
        self.assertEqual(tier3["url"], "https://real-site.example/generate_204")
        self.assertEqual(tier3["expected-status"], 204)
        self.assertEqual(tier3["interval"], 120)
        self.assertEqual(groups["PROXY-FALLBACK"]["url"], "https://neutral.example/generate_204")
        self.assertNotIn("health-check", main["proxy-providers"]["TIER3B-RUNTIME"])
        self.assertEqual(
            projection.provider_member_names,
            ("tier3-a", "tier3-b", "REJECT"),
        )


class Tier3CoreProviderFileTests(unittest.TestCase):
    def test_atomic_replace_skips_identical_bytes(self) -> None:
        with _writable_temporary_directory() as directory:
            path = Path(directory) / "provider.yaml"
            path.write_bytes(b"proxies: []\n")

            with patch("tier3_core_provider.os.replace") as replace:
                changed = atomic_replace_if_changed(path, b"proxies: []\n")

            self.assertFalse(changed)
            replace.assert_not_called()
            self.assertEqual(path.read_bytes(), b"proxies: []\n")

    def test_atomic_replace_changes_content_without_leaving_stage_file(self) -> None:
        with _writable_temporary_directory() as directory:
            path = Path(directory) / "provider.yaml"

            self.assertTrue(atomic_replace_if_changed(path, "proxies:\n- name: a\n"))

            self.assertEqual(path.read_text(encoding="utf-8"), "proxies:\n- name: a\n")
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_readback_requires_exact_member_order(self) -> None:
        valid = validate_proxy_provider_readback(
            {
                "providers": {
                    "TIER3B-RUNTIME": {
                        "name": "TIER3B-RUNTIME",
                        "proxies": [{"name": "a"}, {"name": "b"}],
                    }
                }
            },
            provider_name="TIER3B-RUNTIME",
            expected_proxy_names=["a", "b"],
        )
        reordered = validate_proxy_provider_readback(
            {"name": "TIER3B-RUNTIME", "proxies": [{"name": "b"}, {"name": "a"}]},
            provider_name="TIER3B-RUNTIME",
            expected_proxy_names=["a", "b"],
        )

        self.assertTrue(valid.valid)
        self.assertEqual(valid.verification_basis, "member_digest")
        self.assertEqual(valid.expected_member_digest, valid.observed_member_digest)
        self.assertFalse(reordered.valid)
        self.assertNotEqual(reordered.expected_member_digest, reordered.observed_member_digest)

    def test_readback_uses_exposed_version_when_available(self) -> None:
        matching = validate_proxy_provider_readback(
            {"name": "TIER3B-RUNTIME", "version": "v2", "proxies": [{"name": "a"}]},
            provider_name="TIER3B-RUNTIME",
            expected_proxy_names=["a"],
            expected_version="v2",
        )
        stale = validate_proxy_provider_readback(
            {"name": "TIER3B-RUNTIME", "version": "v1", "proxies": [{"name": "a"}]},
            provider_name="TIER3B-RUNTIME",
            expected_proxy_names=["a"],
            expected_version="v2",
        )

        self.assertTrue(matching.valid)
        self.assertEqual(matching.verification_basis, "version")
        self.assertFalse(stale.valid)
        self.assertIn("version", stale.reason)

    def test_transaction_commits_only_after_controller_readback(self) -> None:
        with _writable_temporary_directory() as directory:
            path = Path(directory) / "provider.yaml"
            events: list[str] = []

            result = replace_provider_file_transactionally(
                path,
                "proxies:\n- name: a\n- name: b\n",
                provider_name="TIER3B-RUNTIME",
                expected_proxy_names=["a", "b"],
                activate=lambda: events.append("activate"),
                readback=lambda: {
                    "name": "TIER3B-RUNTIME",
                    "proxies": [{"name": "a"}, {"name": "b"}],
                },
            )

            self.assertTrue(result.committed)
            self.assertTrue(result.changed)
            self.assertFalse(result.rolled_back)
            self.assertIsNone(result.activation_error)
            self.assertEqual(events, ["activate"])
            self.assertIn("name: b", path.read_text(encoding="utf-8"))

    def test_transaction_reads_back_after_ambiguous_activation_error(self) -> None:
        with _writable_temporary_directory() as directory:
            path = Path(directory) / "provider.yaml"

            def ambiguous_activation() -> None:
                raise TimeoutError("provider update response timed out")

            result = replace_provider_file_transactionally(
                path,
                "proxies:\n- name: candidate\n",
                provider_name="TIER3B-RUNTIME",
                expected_proxy_names=["candidate"],
                activate=ambiguous_activation,
                readback=lambda: {
                    "name": "TIER3B-RUNTIME",
                    "proxies": [{"name": "candidate"}],
                },
            )

            self.assertTrue(result.committed)
            self.assertIn("timed out", result.activation_error or "")
            self.assertIsNone(result.error)
            self.assertTrue(path.exists())

    def test_transaction_restores_previous_valid_file_before_rollback_activation(self) -> None:
        with _writable_temporary_directory() as directory:
            path = Path(directory) / "provider.yaml"
            previous = b"proxies:\n- name: stable\n"
            path.write_bytes(previous)
            rollback_observed: list[bytes] = []

            result = replace_provider_file_transactionally(
                path,
                "proxies:\n- name: candidate\n",
                provider_name="TIER3B-RUNTIME",
                expected_proxy_names=["candidate"],
                activate=lambda: None,
                readback=lambda: {
                    "name": "TIER3B-RUNTIME",
                    "proxies": [{"name": "unexpected"}],
                },
                rollback_activate=lambda: rollback_observed.append(path.read_bytes()),
            )

            self.assertFalse(result.committed)
            self.assertTrue(result.changed)
            self.assertTrue(result.rolled_back)
            self.assertFalse(result.validation.valid if result.validation else True)
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(rollback_observed, [previous])

    def test_transaction_failure_without_previous_file_removes_candidate(self) -> None:
        with _writable_temporary_directory() as directory:
            path = Path(directory) / "provider.yaml"

            def fail_activation() -> None:
                raise RuntimeError("controller unavailable")

            result = replace_provider_file_transactionally(
                path,
                "proxies:\n- name: candidate\n",
                provider_name="TIER3B-RUNTIME",
                expected_proxy_names=["candidate"],
                activate=fail_activation,
                readback=lambda: {},
            )

            self.assertFalse(result.committed)
            self.assertTrue(result.rolled_back)
            self.assertIn("controller unavailable", result.error or "")
            self.assertIn("controller unavailable", result.activation_error or "")
            self.assertFalse(path.exists())


class Tier3CoreProviderBundledRefreshTests(unittest.TestCase):
    """Real Mihomo: a provider refresh resets an unfixed real-site pick."""

    # The "bad" node tunnels the neutral origin but opens and then drops the
    # real-site origin (the measured SSL-EOF free node) after this delay, so
    # Mihomo's reset to the first member is observable before it re-settles.
    BAD_REAL_SITE_EOF_SECONDS = 2.0

    def test_unfixed_real_site_pick_resettles_after_provider_refresh(self) -> None:
        import select
        import socket
        import socketserver
        import subprocess
        import threading
        import time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.request import ProxyHandler, build_opener

        from clash_formatter import build_full_clash_config
        from desktop_runtime import DesktopRuntime
        from mihomo_api import MihomoAPIClient, MihomoAPIError
        from parser import parse_node_uri
        from windows_subprocess import hidden_subprocess_kwargs

        binary = Path(__file__).resolve().parents[1] / "bin" / "mihomo.exe"
        if not binary.is_file():
            self.skipTest("bundled mihomo.exe is unavailable")
        bad_delay = self.BAD_REAL_SITE_EOF_SECONDS

        class Origin(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
                # A rounded 0 ms delay is an error for Mihomo's URLTest.
                time.sleep(0.005)
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_HEAD = do_GET

            def log_message(self, _format: str, *_args: object) -> None:
                return

        class OriginServer(ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 64

        class TunnelServer(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True
            request_queue_size = 64

        def tunnel(dropped_port: int | None):
            class Tunnel(socketserver.StreamRequestHandler):
                def handle(self) -> None:
                    request_line = self.rfile.readline().decode("ascii", "replace")
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    parts = request_line.split()
                    if len(parts) < 2:
                        return
                    host, _sep, port = parts[1].rpartition(":")
                    if dropped_port is not None and int(port) == dropped_port:
                        self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                        self.wfile.flush()
                        time.sleep(bad_delay)
                        return
                    with socket.create_connection((host, int(port)), timeout=2) as upstream:
                        self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                        self.wfile.flush()
                        peers = (self.connection, upstream)
                        until = time.monotonic() + 5.0
                        while time.monotonic() < until:
                            readable, _w, _x = select.select(peers, (), (), 0.1)
                            for peer in readable:
                                data = peer.recv(65536)
                                if not data:
                                    return
                                target = upstream if peer is self.connection else self.connection
                                target.sendall(data)

            return Tunnel

        def free_port() -> int:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind(("127.0.0.1", 0))
                return int(sock.getsockname()[1])

        servers: list[socketserver.BaseServer] = []

        def serve(server: socketserver.BaseServer) -> socketserver.BaseServer:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
            return server

        def stop_servers() -> None:
            for server in servers:
                server.shutdown()
                server.server_close()

        self.addCleanup(stop_servers)
        neutral_origin = serve(OriginServer(("127.0.0.1", 0), Origin))
        real_origin = serve(OriginServer(("127.0.0.1", 0), Origin))
        bad_tunnel = serve(TunnelServer(("127.0.0.1", 0), tunnel(real_origin.server_address[1])))
        good_tunnel = serve(TunnelServer(("127.0.0.1", 0), tunnel(None)))
        neutral_url = f"http://127.0.0.1:{neutral_origin.server_address[1]}/generate_204"
        real_url = f"http://127.0.0.1:{real_origin.server_address[1]}/generate_204"
        mixed_port = free_port()
        controller_port = free_port()

        def node(name: str, server: socketserver.BaseServer):
            parsed = parse_node_uri(
                f"http://test:test@127.0.0.1:{server.server_address[1]}#{name}"
            )
            assert parsed is not None
            parsed.tier = "tier3"
            return parsed

        initial = [node("bad", bad_tunnel), node("good1", good_tunnel)]
        grown = [*initial, node("good2", good_tunnel)]

        def project(nodes: list[object]):
            document = yaml.safe_load(
                build_full_clash_config(
                    {"tier1": [], "tier2": [], "tier3": nodes, "exit": []},
                    probe_url=neutral_url,
                    probe_urls=[neutral_url],
                    external_controller=f"127.0.0.1:{controller_port}",
                    mixed_port=mixed_port,
                    dns_config={"enable": False},
                    tier3_health_check_url=real_url,
                )
            )
            # Keep only the route graph under test: no geodata, DNS or TUN.
            document["rules"] = ["MATCH,GENERAL"]
            document.pop("rule-providers", None)
            document.pop("dns", None)
            document.pop("tun", None)
            document["geodata-mode"] = False
            document["log-level"] = "silent"
            names = [str(proxy["name"]) for proxy in document["proxies"]]
            return project_tier3b_file_provider(
                document,
                names,
                provider_path="./tier3b-runtime.yaml",
            )

        initial_projection = project(initial)
        grown_projection = project(grown)
        self.assertEqual(
            grown_projection.provider_member_names,
            ("bad", "good1", "good2", "REJECT"),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            provider_path = root / "tier3b-runtime.yaml"
            config_path.write_bytes(initial_projection.main_yaml.encode("utf-8"))
            provider_path.write_bytes(initial_projection.provider_yaml.encode("utf-8"))
            process = subprocess.Popen(
                [str(binary), "-f", str(config_path), "-d", str(root)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **hidden_subprocess_kwargs(),
            )
            try:
                client = MihomoAPIClient(
                    base_url=f"http://127.0.0.1:{controller_port}",
                    timeout=2.0,
                    opener=build_opener(ProxyHandler({})).open,
                )
                runtime = object.__new__(DesktopRuntime)
                runtime.stop_event = threading.Event()
                runtime.mihomo_proxies = lambda *, timeout_seconds=None: client.proxies(
                    timeout=timeout_seconds or 2.0
                )
                deadline = time.monotonic() + 15.0
                previous: dict[str, object] = {}
                while time.monotonic() < deadline:
                    self.assertIsNone(process.poll(), "Mihomo exited during startup")
                    try:
                        previous = runtime._tier3_provider_route_guard(
                            {"proxies": client.proxies(timeout=1.0)}
                        )
                    except MihomoAPIError:
                        previous = {}
                    if previous.get("tier3_now") == "good1":
                        break
                    time.sleep(0.1)
                self.assertEqual(previous.get("tier3_now"), "good1", previous)
                self.assertEqual(previous.get("tier3_fixed"), "")
                # Mihomo reuses a health-check result for one second; leave
                # that window so the refresh gets its own URLTest round.
                time.sleep(1.2)

                provider_path.write_bytes(grown_projection.provider_yaml.encode("utf-8"))
                client.update_proxy_provider("TIER3B-RUNTIME", timeout=2.0)
                payload = client.proxies(timeout=2.0)
                current = runtime._tier3_provider_route_guard({"proxies": payload})

                # The refresh itself moved the native pick to the real-site
                # dead first member; that is not provider selection drift.
                self.assertEqual(current["tier3_now"], "bad", current)
                settled_guard, settled_payload = (
                    runtime._settle_unfixed_tier3_provider_pick(
                        previous,
                        current,
                        payload,
                        expected_group_members=grown_projection.provider_member_names,
                    )
                )
                self.assertEqual(settled_guard, previous)
                self.assertEqual(
                    settled_payload["proxies"]["TIER3-AUTO"]["all"],
                    ["bad", "good1", "good2", "REJECT"],
                )
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5.0)


if __name__ == "__main__":
    unittest.main()
