"""配置整理 P2: routing.rule_list / routing.providers through the bridge — the snapshot
projection, settings.validate / settings.apply (the conflict guard's reasons), the
writes (routing.rules.list, routing.rule_providers) and the unchanged resave.

Isolated: temporary config directories, recording fakes, no live profile, no network."""
from __future__ import annotations

import yaml
import pytest

from clash_formatter import build_custom_routing_rules
from tests.test_bridge_p2_10 import FakeRuntime, Harness, failure, sections
from tests.test_bridge_p4_c1 import projection, write_local


@pytest.fixture
def rt(tmp_path):
    return FakeRuntime(tmp_path)


def local(rt):
    return yaml.safe_load(rt.local_path.read_text(encoding="utf-8")) or {}


def test_snapshot_reads_the_legacy_layout_as_one_list(rt):
    write_local(rt, {"routing": {"rules": {"direct": ["geosite:CN", "203.0.113.0/24"], "reject": ["keyword:ads"]},
                                 "rule_order": ["reject", "direct", "proxy"]}})
    routing = projection(rt)[0]["routing"]
    assert routing["rule_list"] == [
        {"kind": "keyword", "value": "ads", "to": "reject"},
        {"kind": "geosite", "value": "CN", "to": "direct"},
        {"kind": "ip-cidr", "value": "203.0.113.0/24", "to": "direct"},
    ]
    assert routing["providers"] == []


def test_snapshot_shows_public_rule_set_addresses_only(rt):
    write_local(rt, {"routing": {"rule_providers": {
        "ads": {"type": "http", "url": "https://rules.example/ads.mrs", "behavior": "domain", "format": "mrs"},
        "private": {"type": "http", "url": "https://rules.example/p.yaml?token=s3cret", "behavior": "domain"},
    }}})
    values, secret_fields = projection(rt)[:2]
    assert values["routing"]["providers"] == [
        {"name": "ads", "type": "http", "behavior": "domain", "format": "mrs", "url": "https://rules.example/ads.mrs"},
        {"name": "private", "type": "http", "behavior": "domain", "url_hidden": True},
    ]
    assert "s3cret" not in repr(values)


def test_unchanged_list_resave_writes_nothing(rt):
    write_local(rt, {"routing": {"rules": {"direct": ["geosite:CN", "geoip:CN"]}}})
    before = rt.local_path.read_bytes()
    routing = projection(rt)[0]["routing"]
    result = Harness(rt).apply([{"section": "routing", "values": {"rule_list": routing["rule_list"],
                                                                 "providers": [{"name": p["name"]} for p in routing["providers"]]}}])
    state = sections(result)["routing"]
    assert state["saved"] and state["applied"], state
    assert rt.local_path.read_bytes() == before


def test_saving_the_list_writes_routing_rules_list_and_compiles(rt):
    write_local(rt, {"routing": {"rules": {"direct": ["geosite:CN"]}}})
    rows = [
        {"to": "reject", "kind": "and", "of": [{"kind": "suffix", "value": "example.org"}, {"kind": "network", "value": "udp"}]},
        {"to": "direct", "kind": "geosite", "value": "CN"},
        {"to": "proxy", "kind": "rule-set", "value": "stream"},
    ]
    providers = [{"name": "stream", "url": "https://rules.example/stream.txt", "behavior": "classical"}]
    result = Harness(rt).apply([{"section": "routing", "values": {"rule_list": rows, "providers": providers}}])
    assert sections(result)["routing"]["saved"]
    stored = local(rt)["routing"]
    assert stored["rules"]["list"] == rows
    assert stored["rule_providers"] == {"stream": {"type": "http", "url": "https://rules.example/stream.txt",
                                                   "behavior": "classical", "format": "text", "interval": 86400}}
    rt.reload_config()
    assert build_custom_routing_rules(rt.config["routing"]) == [
        "AND,((DOMAIN-SUFFIX,example.org),(NETWORK,udp)),REJECT", "GEOSITE,CN,DIRECT", "RULE-SET,stream,GENERAL"]
    # a rule set left out is removed whole
    result = Harness(rt).apply([{"section": "routing", "values": {"rule_list": rows[:2], "providers": []}}])
    assert sections(result)["routing"]["saved"]
    assert local(rt)["routing"]["rule_providers"] == {}


@pytest.mark.parametrize("row, key", [
    ({"to": "proxy", "kind": "suffix", "value": "nas.local"}, "errors.settings.rule_local_only"),
    ({"to": "direct", "kind": "domain", "value": "cp.cloudflare.com"}, "errors.settings.rule_reserved_host"),
    ({"to": "reject", "kind": "process", "value": "mihomo.exe"}, "errors.settings.rule_own_program"),
    ({"to": "reject", "kind": "rule-set", "value": "missing"}, "errors.settings.rule_unknown_set"),
    ({"to": "GENERAL", "kind": "suffix", "value": "a.example"}, "errors.settings.invalid_field"),
])
def test_the_guard_says_why(rt, row, key):
    exc = failure(Harness(rt), [{"section": "routing", "values": {"rule_list": [row]}}])
    state = sections(exc.partial_result)["routing"]
    assert not state["saved"]
    assert state["error"]["message_key"] == key, state
    assert state["error"]["params"]["field"].startswith("routing.rule_list[0]")


def test_a_rule_set_in_use_cannot_be_removed(rt):
    write_local(rt, {"routing": {"rules": {"list": [{"to": "reject", "kind": "rule-set", "value": "ads"}]},
                                 "rule_providers": {"ads": {"type": "http", "url": "https://rules.example/ads.yaml", "behavior": "domain"}}}})
    exc = failure(Harness(rt), [{"section": "routing", "values": {"providers": []}}])
    state = sections(exc.partial_result)["routing"]
    assert not state["saved"] and state["error"]["message_key"] == "errors.settings.rule_set_in_use"
    # with the row gone, or off, it can
    result = Harness(rt).apply([{"section": "routing", "values": {
        "rule_list": [{"to": "reject", "kind": "rule-set", "value": "ads", "off": True}], "providers": []}}])
    assert sections(result)["routing"]["saved"]


def test_a_hidden_rule_set_address_is_kept(rt):
    write_local(rt, {"routing": {"rule_providers": {
        "private": {"type": "http", "url": "https://rules.example/p.yaml?token=s3cret", "behavior": "domain", "interval": 3600}}}})
    result = Harness(rt).apply([{"section": "routing", "values": {
        "providers": [{"name": "private"}, {"name": "ads", "url": "https://rules.example/ads.yaml", "behavior": "domain"}],
        "rule_list": [{"to": "reject", "kind": "rule-set", "value": "ads"}]}}])
    assert sections(result)["routing"]["saved"]
    providers = local(rt)["routing"]["rule_providers"]
    assert providers["private"]["url"] == "https://rules.example/p.yaml?token=s3cret"
    assert providers["ads"]["format"] == "yaml"
