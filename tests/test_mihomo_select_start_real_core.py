"""FINAL-EXIT starts on REJECT on the bundled Mihomo (1.19.28+ select start).

Up to 1.19.27 a new select group's pick was its empty-fallback, so the
generated FINAL-EXIT (members [PROXY-FALLBACK or EXIT-SELECT, REJECT],
empty-fallback REJECT) started on REJECT: ordinary traffic stayed rejected
until the runtime selected the verified route (gate-before-READY).  From 1.19.28
the pick is ``default-selected``, and the first member when that is unset, so
the renderer now names it.  These tests start the real binary on rendered
configs and read the pick back.
"""

from __future__ import annotations

import socket
import subprocess
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

import pytest
import yaml

from clash_formatter import (
    build_bootstrap_clash_config,
    build_capture_disabled_clash_config,
    build_full_clash_config,
)
from mihomo_api import MihomoAPIClient, MihomoAPIError
from parser import parse_node_uri
from windows_subprocess import hidden_subprocess_kwargs

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "bin" / "mihomo.exe"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _formal_config(controller_port: int, *, active_exit: str | None) -> str:
    tier3 = parse_node_uri(
        "ss://YWVzLTI1Ni1nY206cGFzc0AxMjcuMC4wLjE6OQ#Bastion", source="test", tier="tier3"
    )
    exit_node = parse_node_uri(
        "ss://YWVzLTI1Ni1nY206cGFzc0AxMjcuMC4wLjE6MTA#Exit-SS", source="test", tier="exit"
    )
    assert tier3 is not None and exit_node is not None
    return build_full_clash_config(
        {"tier1": [], "tier2": [], "tier3": [tier3], "exit": [exit_node]},
        active_exit_proxy=active_exit,
        external_controller=f"127.0.0.1:{controller_port}",
        mixed_port=_free_port(),
        dns_config={"enable": False},
    )


def _final_exit_pick(tmp_path: Path, rendered: str, controller_port: int) -> str:
    payload = yaml.safe_load(rendered)
    payload["log-level"] = "silent"
    payload.pop("geox-url", None)
    # No GeoData on a test machine: keep only rules that need none.
    payload["rules"] = ["MATCH,GENERAL"]
    payload.pop("rule-providers", None)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    process = subprocess.Popen(
        [str(BINARY), "-d", str(tmp_path), "-f", str(config_path)],
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **hidden_subprocess_kwargs(),
    )
    try:
        client = MihomoAPIClient(
            base_url=f"http://127.0.0.1:{controller_port}",
            timeout=1.0,
            opener=build_opener(ProxyHandler({})).open,
        )
        deadline = time.monotonic() + 10.0
        while True:
            assert process.poll() is None, "Mihomo exited during startup"
            try:
                return str(client.proxy("FINAL-EXIT", timeout=0.5)["now"])
            except MihomoAPIError:
                assert time.monotonic() < deadline, "Mihomo did not start"
                time.sleep(0.02)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)


@pytest.fixture
def core_binary():
    if not BINARY.is_file():
        pytest.skip("bundled mihomo.exe is unavailable")
    return BINARY


@pytest.mark.parametrize("active_exit", [None, "Exit-SS"])
@pytest.mark.parametrize("variant", ["formal", "capture_disabled", "bootstrap"])
def test_rendered_final_exit_starts_on_reject(core_binary, tmp_path, variant, active_exit):
    controller_port = _free_port()
    formal = _formal_config(controller_port, active_exit=active_exit)
    rendered = {
        "formal": lambda: formal,
        "capture_disabled": lambda: build_capture_disabled_clash_config(formal),
        "bootstrap": lambda: build_bootstrap_clash_config(formal),
    }[variant]()
    assert _final_exit_pick(tmp_path, rendered, controller_port) == "REJECT"


def test_without_default_selected_the_core_starts_on_the_first_member(core_binary, tmp_path):
    controller_port = _free_port()
    payload = yaml.safe_load(_formal_config(controller_port, active_exit=None))
    for group in payload["proxy-groups"]:
        group.pop("default-selected", None)
    # The upstream change this renderer works around: the first member wins.
    assert _final_exit_pick(tmp_path, yaml.safe_dump(payload), controller_port) == "PROXY-FALLBACK"
