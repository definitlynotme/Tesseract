from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from clash_formatter import node_to_clash_proxy
from parser import parse_node_uri
from tier3_validation_config import build_single_proxy_validation_config
from windows_subprocess import hidden_subprocess_kwargs


URIS = (
    "hysteria2://test-secret@127.0.0.1:18443?alpn=h3#fixture",
    "anytls://test-secret@127.0.0.1:18443?alpn=h3#fixture",
    "tuic://11111111-1111-4111-8111-111111111111:test-secret@127.0.0.1:18443?alpn=h3#fixture",
)


@pytest.mark.parametrize("uri", URIS)
@pytest.mark.parametrize("value", ["h3", ["h3"], "h3,h2", ("h3", "h2")])
def test_alpn_uri_and_structured_input_preserve_order_as_list(uri, value):
    node = parse_node_uri(uri, source="fixture", tier="tier3")
    assert node is not None
    node.extra["alpn"] = value
    proxy = node_to_clash_proxy(node)
    assert proxy is not None
    assert proxy["alpn"] == (["h3"] if value in ("h3", ["h3"]) else ["h3", "h2"])
    assert node.extra["alpn"] == value


@pytest.mark.parametrize("uri", URIS)
def test_uri_alpn_passes_actual_mihomo_candidate_config_parser(uri, tmp_path):
    binary = Path("bin/mihomo.exe").resolve()
    if not binary.is_file():
        pytest.skip("bundled Mihomo is unavailable")
    node = parse_node_uri(uri, source="fixture", tier="tier3")
    assert node is not None
    config = build_single_proxy_validation_config(node, 18444, controller_port=18445)
    assert config is not None
    path = tmp_path / "candidate.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    result = subprocess.run(
        [str(binary), "-t", "-f", str(path), "-d", str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=15, **hidden_subprocess_kwargs(),
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("uri", URIS)
@pytest.mark.parametrize("value", [42, {"invalid": "shape"}, ["h3", 7]])
def test_malformed_alpn_is_rejected_before_helper_facility(uri, value):
    node = parse_node_uri(uri, source="fixture", tier="tier3")
    assert node is not None
    node.extra["alpn"] = value
    assert build_single_proxy_validation_config(node, 18444) is None
