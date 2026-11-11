from __future__ import annotations

import concurrent.futures
import logging
import socket
import struct
import threading
import time
from unittest.mock import patch

import pytest
import yaml

from clash_formatter import build_full_clash_config, resolved_client_dns_block
from desktop_runtime import DesktopRuntime
from dns_resolver_policy import (
    BOOTSTRAP_LADDER_AUTO,
    RUNG_SYSTEM,
    DnsResolverPlan,
    LinkResolverResult,
    LEGACY_SHIPPED_DOH_NAMESERVERS,
    LEGACY_SHIPPED_PLAIN_NAMESERVERS,
    ResolverQualification,
    RungDecision,
    formal_dns_is_shipped_default,
    normalize_dns_mode,
    rendered_dns_uses_hostnames,
)
from runtime_source_dns import (
    PlainAnswer,
    _dns_query_packet,
    _parse_dns_answers,
    detect_outer_fake_ip,
    encrypted_upstream_hostnames,
    probe_plain_resolvers,
)
from tier3_tcp_dns import Tier3TcpDnsBusy


def answer(address):
    future = concurrent.futures.Future()
    future.set_result([(2, 1, 6, "", (address, 443))])
    return future


# ------------------------------------------------------- upstream policy

@pytest.mark.parametrize("dns,expected", [
    ({"enable": True, "nameserver": ["https://doh.pub/dns-query", "https://dns.alidns.com/dns-query"]}, ("doh.pub", "dns.alidns.com")),
    (None, ()), ({}, ()), ({"enable": False}, ()),
    ({"enable": True, "nameserver": ["1.1.1.1"]}, ()),
    ({"enable": True, "nameserver": ["https://1.1.1.1/dns-query"]}, ()),
    ({"enable": True, "nameserver": ["https://resolver.example/dns-query#PROXY"]}, ()),
    ({"enable": True, "nameserver": ["https://resolver.example/dns-query"], "direct-nameserver": ["127.0.0.1"]}, ()),
])
def test_encrypted_upstream_hostnames_only_for_hostname_only_encrypted_policy(dns, expected):
    assert encrypted_upstream_hostnames(dns) == expected


def test_shipped_default_dns_is_classified_as_auto_not_user_policy():
    rendered = yaml.safe_load(build_full_clash_config(
        {"tier1": [], "tier2": [], "tier3": [], "exit": []},
        dns_config={"enable": True},
    ))["dns"]
    # The static default is region-neutral and needs no plaintext bootstrap.
    assert rendered["default-nameserver"] == ["system"]
    assert len(rendered["nameserver"]) == 5
    assert not rendered_dns_uses_hostnames(rendered)
    assert normalize_dns_mode({}) == "auto"
    assert normalize_dns_mode({
        "nameserver": list(LEGACY_SHIPPED_DOH_NAMESERVERS),
        "default_nameserver": list(LEGACY_SHIPPED_PLAIN_NAMESERVERS),
    }) == "auto"
    assert formal_dns_is_shipped_default({"nameserver": list(LEGACY_SHIPPED_DOH_NAMESERVERS)}) is True
    assert formal_dns_is_shipped_default({"nameserver": ["tls://dns.example"]}) is False
    assert resolved_client_dns_block({}) == rendered
    assert resolved_client_dns_block({"enable": False}) is None


# ------------------------------------------------------ outer fake-IP check

@pytest.mark.parametrize("address,expected", [
    ("198.18.0.12", True), ("198.19.255.2", True), ("1.1.1.1", False),
    ("127.0.0.1", False), ("2001:db8::1", False),
])
def test_detect_outer_fake_ip_reports_only_positive_evidence(address, expected):
    with patch("runtime_source_dns.tier3_tcp_dns_resolver.submit_getaddrinfo", return_value=answer(address)):
        assert detect_outer_fake_ip(("resolver.example",)) is expected


def test_detect_outer_fake_ip_is_bounded_and_unknown_when_slow_or_busy():
    future = concurrent.futures.Future()
    future.set_running_or_notify_cancel()
    start = time.monotonic()
    with patch("runtime_source_dns.tier3_tcp_dns_resolver.submit_getaddrinfo", return_value=future):
        assert detect_outer_fake_ip(("resolver.example",)) is None
    assert .2 <= time.monotonic() - start < .75
    with patch("runtime_source_dns.tier3_tcp_dns_resolver.submit_getaddrinfo", side_effect=Tier3TcpDnsBusy("busy")):
        assert detect_outer_fake_ip(("resolver.example",)) is None
    failed = concurrent.futures.Future()
    failed.set_exception(OSError("no such host"))
    with patch("runtime_source_dns.tier3_tcp_dns_resolver.submit_getaddrinfo", return_value=failed):
        assert detect_outer_fake_ip(("resolver.example",)) is None
    assert detect_outer_fake_ip(()) is None


# ----------------------------------------------------------- plain probe

def _reply(query: bytes, addresses: list[str]) -> bytes:
    offset = 12
    while query[offset] != 0:
        offset += 1 + query[offset]
    offset += 5
    header = query[:2] + struct.pack("!HHHHH", 0x8180, 1, len(addresses), 0, 0)
    answers = b"".join(
        b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton(address)
        for address in addresses
    )
    return header + query[12:offset] + answers


def test_probe_plain_resolvers_accepts_matching_replies_and_measures_latency():
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(1.0)

    def respond() -> None:
        for _ in range(2):
            payload, address = server.recvfrom(1232)
            server.sendto(_reply(payload, ["1.1.1.1"]), address)

    responder = threading.Thread(target=respond, daemon=True)
    responder.start()
    try:
        results = probe_plain_resolvers(
            ["127.0.0.1"], ("one.one.one.one", "dns.google"), port=server.getsockname()[1],
        )
    finally:
        responder.join(1.5)
        server.close()
    result = results["127.0.0.1"]
    assert result.replied and result.latency_ms is not None
    assert result.answers["one.one.one.one"] == PlainAnswer(0, ("1.1.1.1",))
    assert result.answers["dns.google"] == PlainAnswer(0, ("1.1.1.1",))


def test_probe_plain_resolvers_reports_silence_within_its_budget():
    closed = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    started = time.monotonic()
    results = probe_plain_resolvers(["127.0.0.1"], ("doh.example",), port=port, budget_seconds=0.3)
    assert time.monotonic() - started < 1.0
    assert results["127.0.0.1"].replied is False


def test_probe_plain_resolvers_cannot_judge_an_unencodable_hostname():
    assert probe_plain_resolvers(["223.5.5.5"], ("a" * 64,)) == {}
    assert probe_plain_resolvers([], ("doh.example",)) == {}
    assert _dns_query_packet("a" * 64) is None
    assert _parse_dns_answers(b"") is None


# ---------------------------------------------------- runtime render seam

def _plan(block: dict) -> DnsResolverPlan:
    qualification = ResolverQualification(
        fingerprint="fp", wall_time_ms=1,
        link_resolvers=(LinkResolverResult("222.198.178.68", "answers_public", 4),),
        inconclusive=False,
    )
    decision = RungDecision(RUNG_SYSTEM, "link_resolvers_answer", 1, BOOTSTRAP_LADDER_AUTO)
    return DnsResolverPlan(decision, qualification, block, revision=1, applied_at_monotonic=time.monotonic())


def test_bootstrap_writer_carries_the_formal_dns_block_unchanged(tmp_path):
    plan_block = {
        "enable": True, "listen": "127.0.0.1:1053", "ipv6": False, "enhanced-mode": "fake-ip",
        "respect-rules": False, "default-nameserver": ["system"],
        "nameserver": ["222.198.178.68"], "proxy-server-nameserver": ["222.198.178.68"],
        "fake-ip-range": "198.18.0.1/16",
    }
    runtime = DesktopRuntime.__new__(DesktopRuntime)
    runtime.config = {}
    runtime.logger = logging.getLogger("test-bootstrap-writer")
    runtime._dns_resolver_plan_lock = threading.Lock()
    runtime._dns_resolver_plan = _plan(plan_block)
    formal = runtime._apply_dns_resolver_plan_to_payload(build_full_clash_config(
        {"tier1": [], "tier2": [], "tier3": [], "exit": []},
        dns_config={"enable": True, "nameserver": ["https://resolver.example/dns-query"]},
    ))
    assert yaml.safe_load(formal)["dns"] == plan_block
    path = tmp_path / "runtime.yaml"
    records = []

    def write_formal():
        path.write_text(formal, encoding="utf-8")
        return path

    def write_payload(payload, target):
        target.write_text(payload, encoding="utf-8")
        return target

    runtime.write_mihomo_runtime_config = write_formal
    runtime._write_mihomo_runtime_payload = write_payload
    runtime._runtime_export_contract_for_payload = lambda payload: {"payload": payload}
    runtime._record_pending_runtime_export_contract = lambda *args, **kwargs: records.append(kwargs)
    assert runtime.write_mihomo_bootstrap_runtime_config() == path
    bootstrap = yaml.safe_load(path.read_text(encoding="utf-8"))
    # Bootstrap and formal resolve identically; only capture/routing differ.
    assert bootstrap["dns"] == plan_block
    assert records[-1]["contract"]["payload"] == path.read_text(encoding="utf-8")


def test_render_seam_never_invents_a_dns_block_and_ignores_missing_plan():
    runtime = DesktopRuntime.__new__(DesktopRuntime)
    runtime._dns_resolver_plan_lock = threading.Lock()
    runtime._dns_resolver_plan = None
    payload = "rules:\n- MATCH,GENERAL\n"
    assert runtime._apply_dns_resolver_plan_to_payload(payload) == payload
    runtime._dns_resolver_plan = _plan({"enable": True, "nameserver": ["system"]})
    assert yaml.safe_load(runtime._apply_dns_resolver_plan_to_payload(payload)) == {"rules": ["MATCH,GENERAL"]}
    with_dns = "dns:\n  enable: true\n  nameserver:\n  - https://doh.pub/dns-query\nrules:\n- MATCH,GENERAL\n"
    rendered = yaml.safe_load(runtime._apply_dns_resolver_plan_to_payload(with_dns))
    assert rendered["dns"] == {"enable": True, "nameserver": ["system"]}
    assert rendered["rules"] == ["MATCH,GENERAL"]
