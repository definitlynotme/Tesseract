from __future__ import annotations

import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
import yaml

from parser import parse_node_uri
from tier3_validation_config import (
    build_batch_proxy_validation_config,
    build_single_proxy_validation_config,
)
from windows_subprocess import hidden_subprocess_kwargs


def _node(index: int, uri: str | None = None):
    node = parse_node_uri(
        uri or f"ss://aes-256-gcm:secret@127.0.0.{index + 1}:8388#fixture-{index}",
        source="fixture",
        tier="tier3",
    )
    assert node is not None
    return node


def _free_ports(count: int) -> list[int]:
    holders, ports = [], []
    while len(ports) < count:
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        holders.append(holder)
        ports.append(holder.getsockname()[1])
    for holder in holders:
        holder.close()
    return ports


def test_each_candidate_gets_its_own_listener_and_proxy():
    nodes = [_node(index) for index in range(3)]
    ports = _free_ports(4)
    config, names = build_batch_proxy_validation_config(
        list(zip(nodes, ports[1:])), ports[0],
    )
    assert names == ("TEST-PROXY-0", "TEST-PROXY-1", "TEST-PROXY-2")
    assert [proxy["name"] for proxy in config["proxies"]] == list(names)
    assert [listener["proxy"] for listener in config["listeners"]] == list(names)
    assert [listener["port"] for listener in config["listeners"]] == ports[1:]
    assert all(listener["listen"] == "127.0.0.1" for listener in config["listeners"])
    # Every candidate keeps its own address, so results map back one to one.
    assert [proxy["server"] for proxy in config["proxies"]] == [
        node.address for node in nodes
    ]


def test_nothing_may_fall_through_to_a_direct_path():
    """A listener's binding is the only egress; an unbound path must be rejected."""

    nodes = [_node(index) for index in range(2)]
    ports = _free_ports(3)
    config, _names = build_batch_proxy_validation_config(
        list(zip(nodes, ports[1:])), ports[0],
    )
    assert config["rules"] == ["MATCH,REJECT"]
    assert config["allow-lan"] is False
    assert config["proxy-groups"] == []
    single = build_single_proxy_validation_config(nodes[0], ports[0])
    assert single is not None
    # The batch config keeps the single-candidate config's DNS contract.
    assert config["dns"] == single["dns"]
    assert config["ipv6"] == single["ipv6"]


def test_an_unrenderable_candidate_fails_alone_and_keeps_its_position():
    good_first, good_last = _node(0), _node(1)
    broken = _node(2)
    broken.protocol = "not-a-protocol"
    ports = _free_ports(4)
    config, names = build_batch_proxy_validation_config(
        [(good_first, ports[1]), (broken, ports[2]), (good_last, ports[3])], ports[0],
    )
    assert names[1] == ""
    assert names[0] and names[2]
    assert len(config["proxies"]) == 2
    assert len(config["listeners"]) == 2
    # The surviving candidates keep the ports they were assigned.
    assert [listener["port"] for listener in config["listeners"]] == [ports[1], ports[3]]


def test_empty_batch_is_a_valid_inert_config():
    config, names = build_batch_proxy_validation_config([], _free_ports(1)[0])
    assert names == ()
    assert config["proxies"] == [] and config["listeners"] == []
    assert config["rules"] == ["MATCH,REJECT"]


@pytest.mark.skipif(not Path("bin/mihomo.exe").exists(), reason="bundled mihomo required")
def test_bundled_mihomo_accepts_a_batch_and_serves_every_listener():
    """One process must really carry the whole batch, not just parse it."""

    nodes = [_node(index) for index in range(8)]
    ports = _free_ports(9)
    config, names = build_batch_proxy_validation_config(
        list(zip(nodes, ports[1:])), ports[0],
    )
    assert all(names)
    work = Path(tempfile.mkdtemp(prefix="batch-config-test-"))
    (work / "config.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8",
    )
    process = subprocess.Popen(
        [str(Path("bin/mihomo.exe").resolve()), "-d", str(work)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        **hidden_subprocess_kwargs(),
    )
    try:
        deadline = time.monotonic() + 20.0
        serving = []
        while time.monotonic() < deadline and len(serving) < len(ports) - 1:
            serving = []
            for port in ports[1:]:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        serving.append(port)
                except OSError:
                    break
            if len(serving) < len(ports) - 1:
                time.sleep(0.05)
        assert len(serving) == len(ports) - 1, (
            process.stdout.read()[:800] if process.stdout else "no output"
        )
    finally:
        process.kill()
