"""The provider-member view on the bundled Mihomo (1.19.28+ /proxies contract).

Mihomo 1.19.28 (upstream 85c1798f) stopped merging proxy-provider members into
GET /proxies and the /proxies/{name} routes; up to 1.19.27 the controller did.
The runtime reads every TIER3B-RUNTIME member (each a TIER3-AUTO leaf) through
those routes, so MihomoAPIClient merges the members back from the provider's
own routes.  These tests pin both halves on the real binary: what the core
itself answers now, and that the client's answers are the ones 1.19.27 gave.
"""

from __future__ import annotations

import inspect
import json
import socket
import subprocess
import threading
import time
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

import pytest
import yaml

import runtime_route_lease
import tier3_core_provider
from mihomo_api import (
    MERGED_PROXY_PROVIDERS,
    MihomoAPIClient,
    MihomoAPIError,
    MihomoDelayStatusMismatch,
    MihomoDelayTestFailed,
    MihomoDelayTransportFailed,
)
from windows_subprocess import hidden_subprocess_kwargs

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "bin" / "mihomo.exe"
PROVIDER = "TIER3B-RUNTIME"
# Volatile per read: the adapter id and the delay records.
VOLATILE_KEYS = {"id", "history", "extra"}


class _Origin(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _answer(self) -> None:
        # A rounded 0 ms result reads as a failed test; keep a small RTT.
        time.sleep(0.005)
        self.send_response({"/generate_204": 204, "/ok_200": 200}.get(self.path, 404))
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = _answer
    do_HEAD = _answer


class _OriginServer(ThreadingHTTPServer):
    request_queue_size = 64


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Core:
    def __init__(self, stack: ExitStack, tmp_path: Path) -> None:
        origin = _OriginServer(("127.0.0.1", 0), _Origin)
        thread = threading.Thread(target=origin.serve_forever, daemon=True)
        thread.start()
        stack.callback(thread.join, 2.0)
        stack.callback(origin.server_close)
        stack.callback(origin.shutdown)
        self.origin = f"http://127.0.0.1:{origin.server_address[1]}"
        self.url = f"{self.origin}/generate_204"

        (tmp_path / "tier3b-runtime.yaml").write_text(
            yaml.safe_dump(
                {
                    "proxies": [
                        {"name": "t3-alive", "type": "direct", "udp": True},
                        {"name": "t3-dead", "type": "http", "server": "127.0.0.1", "port": 9},
                        {"name": "REJECT", "type": "reject"},
                    ]
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        contract = {"url": self.url, "expected-status": 204, "interval": 300, "lazy": False}
        controller_port = _free_port()
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "mixed-port": _free_port(),
                    "external-controller": f"127.0.0.1:{controller_port}",
                    "allow-lan": False,
                    "mode": "rule",
                    "log-level": "silent",
                    "ipv6": False,
                    "proxies": [
                        {"name": "t1-alive", "type": "direct", "udp": True},
                        {"name": "t1-dead", "type": "http", "server": "127.0.0.1", "port": 9},
                    ],
                    "proxy-providers": {
                        PROVIDER: {
                            "type": "file",
                            "path": "./tier3b-runtime.yaml",
                            "health-check": {"enable": False, "url": self.url, "interval": 0},
                        }
                    },
                    "proxy-groups": [
                        {"name": "TIER1-AUTO", "type": "fallback", "proxies": ["t1-alive", "t1-dead"], **contract},
                        {"name": "TIER3-AUTO", "type": "fallback", "use": [PROVIDER],
                         "empty-fallback": "REJECT", **contract},
                        {"name": "PROXY-FALLBACK", "type": "fallback",
                         "proxies": ["TIER1-AUTO", "TIER3-AUTO"], **contract},
                        {"name": "FINAL-EXIT", "type": "select", "proxies": ["PROXY-FALLBACK", "REJECT"]},
                    ],
                    "rules": ["MATCH,FINAL-EXIT"],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [str(BINARY), "-d", str(tmp_path), "-f", str(tmp_path / "config.yaml")],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **hidden_subprocess_kwargs(),
        )

        def stop() -> None:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)

        stack.callback(stop)
        self.base = f"http://127.0.0.1:{controller_port}"
        self._opener = build_opener(ProxyHandler({}))
        self.client = MihomoAPIClient(base_url=self.base, timeout=3.0, opener=self._opener.open)
        deadline = time.monotonic() + 10.0
        while True:
            assert process.poll() is None, "Mihomo exited during startup"
            try:
                if self.client.provider_proxies_one(PROVIDER, timeout=0.5).get("proxies"):
                    break
            except MihomoAPIError:
                pass
            assert time.monotonic() < deadline, "Mihomo did not load its provider"
            time.sleep(0.02)

    def raw(self, path: str) -> tuple[int, object]:
        try:
            with self._opener.open(self.base + path, timeout=5.0) as response:
                return response.status, json.loads(response.read() or b"null")
        except HTTPError as exc:
            return exc.code, None


@pytest.fixture
def core(tmp_path):
    if not BINARY.is_file():
        pytest.skip("bundled mihomo.exe is unavailable")
    with ExitStack() as stack:
        yield _Core(stack, tmp_path)


def _shape(entry: dict) -> dict:
    return {key: value for key, value in entry.items() if key not in VOLATILE_KEYS}


def test_merged_provider_is_the_runtimes_tier3_provider():
    assert MERGED_PROXY_PROVIDERS == (PROVIDER,)
    assert runtime_route_lease.TIER3_PROVIDER == PROVIDER
    default = inspect.signature(tier3_core_provider.project_tier3b_file_provider).parameters["provider_name"].default
    assert default == PROVIDER
    assert f'self._tier3_core_provider_name = "{PROVIDER}"' in (ROOT / "desktop_runtime.py").read_text(encoding="utf-8")


def test_the_bundled_core_no_longer_lists_provider_members_under_proxies(core):
    status, listing = core.raw("/proxies")
    assert status == 200
    entries = listing["proxies"]
    assert entries["TIER3-AUTO"]["all"] == ["t3-alive", "t3-dead", "REJECT"]
    assert "t3-alive" not in entries and "t3-dead" not in entries
    assert core.raw("/proxies/t3-alive")[0] == 404
    assert core.raw(f"/proxies/t3-alive/delay?url={core.url}&timeout=1000")[0] == 404
    # The provider's own routes answer for the member.
    assert core.raw(f"/providers/proxies/{PROVIDER}/t3-alive")[0] == 200


def test_the_client_lists_every_provider_member_as_its_provider_reports_it(core):
    entries = core.client.proxies(timeout=2.0)["proxies"]
    provider = core.client.provider_proxies_one(PROVIDER, timeout=2.0)
    members = {member["name"]: member for member in provider["proxies"]}
    assert set(members) == {"t3-alive", "t3-dead", "REJECT"}
    for name, member in members.items():
        assert _shape(entries[name]) == _shape(member), name
    # Inline nodes and groups are as the core listed them.
    status, listing = core.raw("/proxies")
    for name, entry in listing["proxies"].items():
        if name != "REJECT":
            assert _shape(entries[name]) == _shape(entry), name
    # Every name a group lists now has an entry (what readiness walks).
    for entry in entries.values():
        for member in entry.get("all") or ():
            assert member in entries, member
    assert _shape(core.client.proxy("t3-dead", timeout=1.0)) == _shape(members["t3-dead"])
    assert core.client.proxy("TIER3-AUTO", timeout=1.0)["all"] == ["t3-alive", "t3-dead", "REJECT"]


def test_provider_member_delay_tests_keep_their_verdicts(core):
    client = core.client
    assert client.proxy_delay("t3-alive", url=core.url, timeout_ms=2000)["delay"] > 0
    assert client.proxy_delay(
        "t3-alive", url=core.url, timeout_ms=2000, expected_status="204",
        verify_expected_status=True, connection_abort_sink=lambda _close: None,
    )["delay"] > 0
    with pytest.raises(MihomoDelayTransportFailed):
        client.proxy_delay("t3-dead", url=core.url, timeout_ms=2000)
    with pytest.raises(MihomoDelayStatusMismatch):
        client.proxy_delay(
            "t3-alive", url=f"{core.origin}/ok_200", timeout_ms=2000,
            expected_status="204", verify_expected_status=True,
        )
    # The verdict lands on the member, as the merged route recorded it.
    entry = client.proxies(timeout=2.0)["proxies"]["t3-dead"]
    assert entry["extra"][core.url]["alive"] is False
    # An unknown name is still the controller's plain 404, not a verdict.
    with pytest.raises(MihomoAPIError) as raised:
        client.proxy_delay("no-such-node", url=core.url, timeout_ms=2000)
    assert not isinstance(raised.value, MihomoDelayTestFailed)
    assert raised.value.http_status == 404


def test_a_provider_member_is_still_pinned_through_its_group(core):
    commit = core.client.select_proxy_with_readback(
        "TIER3-AUTO", "t3-alive", mode="fixed", timeout=1.0, readback_timeout=1.0,
    )
    assert (commit.commit_state, commit.now, commit.fixed) == ("committed", "t3-alive", "t3-alive")
