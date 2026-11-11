"""routing.rules.list (配置整理 P2): compile, the legacy layout read as a list, the
page's validation (the conflict guard) and rule sets."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from clash_formatter import build_custom_routing_rules, build_full_clash_config, build_rule_providers
from routing_rules import (
    RuleError,
    compile_rule,
    compile_rule_list,
    effective_rule_list,
    legacy_rule_list,
    normalize_providers,
    normalize_rule_list,
    provider_rows,
    reserved_hosts,
    rule_from_text,
)
from windows_subprocess import hidden_subprocess_kwargs


# ---------------------------------------------------------------- compile

def test_compile_simple_rows_and_targets() -> None:
    rows = [
        {"to": "direct", "kind": "suffix", "value": "example.cn"},
        {"to": "proxy", "kind": "domain", "value": "api.example.com"},
        {"to": "reject", "kind": "keyword", "value": "tracker"},
        {"to": "reject", "kind": "regex", "value": r"^ad[0-9]+\.example\.com$"},
        {"to": "direct", "kind": "geosite", "value": "cn"},
        {"to": "proxy", "kind": "src-ip-cidr", "value": "192.0.2.0/24"},
        {"to": "proxy", "kind": "dst-port", "value": "8000-9000"},
        {"to": "reject", "kind": "src-port", "value": "5000"},
        {"to": "direct", "kind": "process", "value": "chrome.exe"},
        {"to": "direct", "kind": "process-path", "value": r"C:\Program Files\Example\app.exe"},
        {"to": "reject", "kind": "network", "value": "udp"},
        {"to": "reject", "kind": "rule-set", "value": "ads"},
    ]
    assert compile_rule_list(rows) == [
        "DOMAIN-SUFFIX,example.cn,DIRECT",
        "DOMAIN,api.example.com,GENERAL",
        "DOMAIN-KEYWORD,tracker,REJECT",
        r"DOMAIN-REGEX,^ad[0-9]+\.example\.com$,REJECT",
        "GEOSITE,cn,DIRECT",
        "SRC-IP-CIDR,192.0.2.0/24,GENERAL",
        "DST-PORT,8000-9000,GENERAL",
        "SRC-PORT,5000,REJECT",
        "PROCESS-NAME,chrome.exe,DIRECT",
        r"PROCESS-PATH,C:\Program Files\Example\app.exe,DIRECT",
        "NETWORK,udp,REJECT",
        "RULE-SET,ads,REJECT",
    ]


def test_ip_conditions_are_no_resolve_unless_resolve() -> None:
    assert compile_rule({"to": "proxy", "kind": "ip-cidr", "value": "203.0.113.0/24"}) == "IP-CIDR,203.0.113.0/24,GENERAL,no-resolve"
    assert compile_rule({"to": "proxy", "kind": "ip-cidr", "value": "2001:db8::/32"}) == "IP-CIDR6,2001:db8::/32,GENERAL,no-resolve"
    assert compile_rule({"to": "direct", "kind": "geoip", "value": "CN"}) == "GEOIP,CN,DIRECT,no-resolve"
    assert compile_rule({"to": "proxy", "kind": "ip-asn", "value": "13335"}) == "IP-ASN,13335,GENERAL,no-resolve"
    assert compile_rule({"to": "proxy", "kind": "ip-cidr", "value": "203.0.113.0/24", "resolve": True}) == "IP-CIDR,203.0.113.0/24,GENERAL"


def test_not_and_combinations() -> None:
    assert compile_rule({"to": "proxy", "kind": "suffix", "value": "example.edu", "not": True}) == "NOT,((DOMAIN-SUFFIX,example.edu)),GENERAL"
    assert compile_rule({"to": "reject", "kind": "and", "of": [
        {"kind": "suffix", "value": "example.org"}, {"kind": "network", "value": "udp"},
    ]}) == "AND,((DOMAIN-SUFFIX,example.org),(NETWORK,udp)),REJECT"
    assert compile_rule({"to": "proxy", "kind": "and", "of": [
        {"kind": "suffix", "value": "example.io", "not": True}, {"kind": "dst-port", "value": "443"},
    ]}) == "AND,((NOT,((DOMAIN-SUFFIX,example.io))),(DST-PORT,443)),GENERAL"
    assert compile_rule({"to": "direct", "kind": "or", "of": [
        {"kind": "ip-cidr", "value": "198.51.100.0/24"}, {"kind": "domain", "value": "a.example.net"},
    ]}) == "OR,((IP-CIDR,198.51.100.0/24,no-resolve),(DOMAIN,a.example.net)),DIRECT"


def test_off_unusable_and_repeated_rows_are_not_compiled() -> None:
    rows = [
        {"to": "direct", "kind": "suffix", "value": "a.example", "off": True, "note": "dropped_policy"},
        {"to": "nowhere", "kind": "suffix", "value": "b.example"},
        {"to": "proxy", "kind": "suffix", "value": "c,example"},
        {"to": "proxy", "kind": "and", "of": [{"kind": "suffix", "value": "d.example"}]},
        {"to": "proxy", "kind": "clash", "value": "NOT A RULE"},
        {"to": "proxy", "kind": "suffix", "value": "e.example"},
        {"to": "proxy", "kind": "suffix", "value": "e.example"},
    ]
    assert compile_rule_list(rows) == ["DOMAIN-SUFFIX,e.example,GENERAL"]


def test_clash_rows_compile_as_written() -> None:
    row = {"to": "proxy", "kind": "clash", "value": "SUB-RULE,(NETWORK,tcp)", "params": []}
    assert compile_rule(row) == "SUB-RULE,(NETWORK,tcp),GENERAL"
    assert compile_rule({"to": "reject", "kind": "clash", "value": "IN-TYPE,HTTP", "params": ["x"]}) == "IN-TYPE,HTTP,REJECT,x"


# ---------------------------------------------------------------- the legacy layout as a list

LEGACY_CASES = [
    {"rules": {"direct": ["geosite:CN", "geoip:CN"]}},
    {"rules": {"direct": ["example.cn", "203.0.113.0/24", "2001:db8::/48", "ip-cidr:198.51.100.0/24",
                          "suffix:Example.org", "domain:a.example", "keyword:ads", "process:app.exe",
                          "DOMAIN-SUFFIX,x.example,PROXY", "GEOIP,US", "GEOIP,CN,no-resolve",
                          "IP-CIDR,192.0.2.0/24,no-resolve", "+.wild.example"],
               "proxy": "openai.com\nanthropic.com", "reject": ["keyword:tracker"]}},
    {"rules": {"direct": ["a.example"], "proxy": ["b.example"], "reject": ["c.example"]},
     "rule_order": ["reject", "proxy", "direct"]},
    {"rules": {"proxy": ["dup.example"], "direct": ["dup.example"]}},
    {"rules": {"rule_sets": ["ads,REJECT", "RULE-SET,stream,GENERAL", {"provider": "work", "target": "proxy"}],
               "raw": ["DOMAIN,raw.example,GENERAL", "AND,((DOMAIN-SUFFIX,g.example),(NETWORK,UDP)),REJECT",
                       "NOT,((DOMAIN-SUFFIX,n.example)),GENERAL", "IP-CIDR,203.0.113.7/32,REJECT,no-resolve",
                       "SUB-RULE,(NETWORK,tcp),GENERAL", "# a comment, with a comma", "no comma here"]}},
    # a flat (pre-routing.rules) layout
    {"direct": ["flat.example"], "proxy": ["flat2.example"]},
    # what the page's checks refuse: a value the old compile took as written, conditions
    # Tesseract's own rules always matched first
    {"rules": {"direct": ["domain-suffix:example.cn"], "proxy": ["geoip:private", "192.168.1.0/24"],
               "reject": ["process:mihomo.exe", "suffix:localhost"],
               "raw": ["IP-CIDR,10.0.0.0/8,REJECT,no-resolve", "AND,((DOMAIN-SUFFIX,local),(NETWORK,UDP)),REJECT",
                       "DOMAIN,www.gstatic.com,DIRECT"]}},
]


@pytest.mark.parametrize("routing", LEGACY_CASES)
def test_legacy_layout_reads_as_a_list_that_compiles_identically(routing: dict) -> None:
    assert compile_rule_list(legacy_rule_list(routing)) == build_custom_routing_rules(routing)
    # and the list, stored, compiles the same through the formatter
    stored = {"rules": {"list": legacy_rule_list(routing)}}
    assert build_custom_routing_rules(stored) == build_custom_routing_rules(routing)


def test_rows_read_from_legacy_strings() -> None:
    rows = legacy_rule_list({"rules": {"direct": ["geosite:CN", "ip-cidr:198.51.100.0/24", "203.0.113.0/24"], "reject": ["keyword:ads"]}})
    assert rows == [
        {"kind": "geosite", "value": "CN", "to": "direct"},
        {"kind": "ip-cidr", "value": "198.51.100.0/24", "resolve": True, "to": "direct"},
        {"kind": "ip-cidr", "value": "203.0.113.0/24", "to": "direct"},
        {"kind": "keyword", "value": "ads", "to": "reject"},
    ]


def test_legacy_rules_the_old_compile_dropped_or_cannot_be_named_stay_off() -> None:
    rows = legacy_rule_list({"rules": {
        "rule_sets": ["cn-sites,DIRECT"],
        "raw": ["DOMAIN,d.example,DIRECT", "DOMAIN,t.example,TIER1-SELECT", "DOMAIN,f.example,FINAL-EXIT",
                "MATCH,DIRECT", "garbage"],
    }})
    assert rows == [
        {"kind": "rule-set", "value": "cn-sites", "to": "direct", "off": True, "note": "dropped_policy"},
        {"kind": "domain", "value": "d.example", "to": "direct", "off": True, "note": "dropped_policy"},
        {"kind": "domain", "value": "t.example", "to": "proxy", "off": True, "note": "target_gone"},
        {"kind": "domain", "value": "f.example", "to": "proxy"},
        {"to": "proxy", "kind": "clash", "value": "MATCH,DIRECT", "off": True, "note": "unreadable"},
        {"to": "proxy", "kind": "clash", "value": "garbage", "off": True, "note": "unreadable"},
    ]
    # FINAL-EXIT reads as the proxy action (GENERAL selects FINAL-EXIT)
    assert compile_rule_list(rows) == ["DOMAIN,f.example,GENERAL"]


def test_legacy_rows_the_checks_refuse_stay_whole_clash_rules() -> None:
    routing = {"rules": {"direct": ["domain-suffix:example.cn", "lan.example"], "proxy": ["geoip:private"],
                         "reject": ["process:mihomo.exe"],
                         "raw": ["AND,((DOMAIN-SUFFIX,local),(NETWORK,UDP)),REJECT", "DOMAIN,www.gstatic.com,DIRECT"]}}
    rows = legacy_rule_list(routing)
    assert rows == [
        {"kind": "clash", "value": "DOMAIN-SUFFIX,domain-suffix:example.cn", "to": "direct"},
        {"kind": "suffix", "value": "lan.example", "to": "direct"},
        {"kind": "clash", "value": "GEOIP,private", "params": ["no-resolve"], "to": "proxy"},
        {"kind": "clash", "value": "PROCESS-NAME,mihomo.exe", "to": "reject"},
        {"kind": "clash", "value": "AND,((DOMAIN-SUFFIX,local),(NETWORK,UDP))", "to": "reject"},
        {"kind": "clash", "value": "DOMAIN,www.gstatic.com", "to": "direct", "off": True, "note": "dropped_policy"},
    ]
    # compiled exactly as before, and a resave of the list (Tesseract's real hosts) is refused nowhere
    assert compile_rule_list(rows) == build_custom_routing_rules(routing)
    assert normalize_rule_list(rows) == rows


def test_logic_rules_read_back_as_combinations() -> None:
    row = rule_from_text("AND,((NOT,((DOMAIN-SUFFIX,example.io))),(DST-PORT,443)),GENERAL")
    assert row == {"kind": "and", "to": "proxy", "of": [
        {"kind": "suffix", "value": "example.io", "not": True}, {"kind": "dst-port", "value": "443"}]}
    assert compile_rule(row) == "AND,((NOT,((DOMAIN-SUFFIX,example.io))),(DST-PORT,443)),GENERAL"
    assert rule_from_text("NOT,((DOMAIN,x.example)),REJECT") == {"to": "reject", "kind": "domain", "value": "x.example", "not": True}


def test_effective_list_prefers_the_stored_list() -> None:
    routing = {"rules": {"direct": ["geosite:CN"], "list": [{"to": "proxy", "kind": "suffix", "value": "only.example"}]}}
    assert effective_rule_list(routing) == [{"to": "proxy", "kind": "suffix", "value": "only.example"}]
    assert build_custom_routing_rules(routing) == ["DOMAIN-SUFFIX,only.example,GENERAL"]
    # an empty stored list is the whole truth too: no legacy rule comes back
    assert build_custom_routing_rules({"rules": {"direct": ["geosite:CN"], "list": []}}) == []


# ---------------------------------------------------------------- the page's list: validation

HOSTS = frozenset({"cp.cloudflare.com", "www.gstatic.com"})


def _bad(rows, reason="invalid", providers=()):
    with pytest.raises(RuleError) as exc:
        normalize_rule_list(rows, providers=providers, hosts=HOSTS)
    assert exc.value.reason == reason, exc.value.path
    return exc.value.path


def test_normalize_keeps_values_as_written() -> None:
    rows = [
        {"to": "direct", "kind": "suffix", "value": " +.Example.ORG "},
        {"to": "proxy", "kind": "ip-cidr", "value": "203.0.113.5"},
        {"to": "proxy", "kind": "ip-cidr", "value": "198.51.100.7/24"},
        {"to": "proxy", "kind": "ip-asn", "value": "AS13335"},
        {"to": "reject", "kind": "network", "value": "UDP"},
        {"to": "proxy", "kind": "rule-set", "value": "stream"},
        {"to": "direct", "kind": "suffix", "value": "old.example", "off": True, "note": "dropped_policy"},
    ]
    assert normalize_rule_list(rows, providers={"stream"}, hosts=HOSTS) == [
        {"to": "direct", "kind": "suffix", "value": "Example.ORG"},
        {"to": "proxy", "kind": "ip-cidr", "value": "203.0.113.5/32"},
        {"to": "proxy", "kind": "ip-cidr", "value": "198.51.100.7/24"},
        {"to": "proxy", "kind": "ip-asn", "value": "13335"},
        {"to": "reject", "kind": "network", "value": "UDP"},
        {"to": "proxy", "kind": "rule-set", "value": "stream"},
        {"to": "direct", "kind": "suffix", "value": "old.example", "off": True, "note": "dropped_policy"},
    ]


def test_a_legacy_list_sent_back_unchanged_is_the_same_list() -> None:
    for routing in LEGACY_CASES:
        rows = legacy_rule_list(routing)
        names = {row["value"] for row in rows if row.get("kind") == "rule-set"}
        try:
            assert normalize_rule_list(rows, providers=names, hosts=HOSTS) == rows
        except RuleError as exc:
            raise AssertionError((routing, exc.path, exc.reason)) from exc


def test_conflict_guard() -> None:
    # local and LAN destinations are always direct: a rule for them alone never matches
    assert _bad([{"to": "proxy", "kind": "suffix", "value": "printer.local"}], "local_only").endswith("[0].value")
    _bad([{"to": "proxy", "kind": "ip-cidr", "value": "192.168.1.0/24"}], "local_only")
    _bad([{"to": "reject", "kind": "geoip", "value": "private"}], "local_only")
    _bad([{"to": "proxy", "kind": "and", "of": [{"kind": "ip-cidr", "value": "10.1.0.0/16"}, {"kind": "dst-port", "value": "22"}]}], "local_only")
    # ... but a negated one matches everything else, and a direct one only repeats Tesseract's rule
    normalize_rule_list([{"to": "proxy", "kind": "ip-cidr", "value": "192.168.0.0/16", "not": True}], hosts=HOSTS)
    normalize_rule_list([{"to": "direct", "kind": "geoip", "value": "private"}], hosts=HOSTS)
    normalize_rule_list([{"to": "proxy", "kind": "domain", "value": "cp.cloudflare.com"}], hosts=HOSTS)
    # a wider network that only contains the LAN is not local-only
    normalize_rule_list([{"to": "proxy", "kind": "ip-cidr", "value": "192.0.0.0/8"}], hosts=HOSTS)
    # Tesseract's own hosts and programs
    _bad([{"to": "direct", "kind": "domain", "value": "cp.cloudflare.com"}], "reserved_host")
    _bad([{"to": "direct", "kind": "suffix", "value": "WWW.gstatic.com"}], "reserved_host")
    normalize_rule_list([{"to": "direct", "kind": "suffix", "value": "gstatic.com"}], hosts=HOSTS)
    _bad([{"to": "reject", "kind": "process", "value": "Mihomo.exe"}], "own_program")
    _bad([{"to": "reject", "kind": "process-path", "value": r"C:\Program Files\Tesseract\tesseract-runtime.exe"}], "own_program")
    # a rule set must exist
    _bad([{"to": "reject", "kind": "rule-set", "value": "nope"}], "unknown_set")
    # Tesseract's actions only; no fallback rule; no nesting
    _bad([{"to": "GENERAL", "kind": "suffix", "value": "a.example"}])
    _bad([{"to": "proxy", "kind": "match", "value": "x"}])
    _bad([{"to": "proxy", "kind": "and", "of": [{"kind": "and", "of": []}, {"kind": "network", "value": "udp"}]}])
    _bad([{"to": "proxy", "kind": "and", "of": [{"kind": "network", "value": "udp"}]}])


def test_values_are_checked() -> None:
    for kind, value in [("suffix", "bad domain"), ("suffix", "a,b"), ("regex", "(?=x)y"), ("regex", "(a"),
                        ("regex", r"(a)\1"), ("dst-port", "0"), ("dst-port", "9-1"), ("dst-port", "70000"),
                        ("ip-asn", "ASX"), ("ip-cidr", "300.1.1.1"), ("process", "a/b.exe"),
                        ("process-path", "noslash.exe"), ("network", "icmp"), ("geosite", "a b")]:
        _bad([{"to": "proxy", "kind": kind, "value": value}])
    _bad([{"to": "proxy", "kind": "suffix", "value": "a.example", "resolve": True}])
    _bad([{"to": "proxy", "kind": "suffix", "value": "a.example", "off": True, "note": "whatever"}])
    with pytest.raises(RuleError) as exc:
        normalize_rule_list([{"to": "proxy", "kind": "network", "value": "udp"}] * 2001, hosts=HOSTS)
    assert exc.value.reason == "too_many"


def test_reserved_hosts_hold_the_probes_and_rule_data() -> None:
    hosts = reserved_hosts()
    assert {"cp.cloudflare.com", "www.gstatic.com", "testingcf.jsdelivr.net"} <= hosts


# ---------------------------------------------------------------- rule sets

def test_providers_from_the_page() -> None:
    stored = {
        "secret-set": {"type": "http", "url": "https://rules.example/set.yaml?token=abc", "behavior": "domain", "interval": 3600},
        "local": {"type": "file", "path": "./rules.txt", "behavior": "classical"},
        "old": {"type": "http", "url": "https://rules.example/old.txt", "behavior": "domain", "format": "text"},
    }
    rows = provider_rows(stored)
    assert rows == [
        {"name": "secret-set", "type": "http", "behavior": "domain", "url_hidden": True},
        {"name": "local", "type": "file", "behavior": "classical"},
        {"name": "old", "type": "http", "behavior": "domain", "format": "text", "url": "https://rules.example/old.txt"},
    ]
    out = normalize_providers([
        {"name": "secret-set"},
        {"name": "local"},
        {"name": "ads", "url": "https://rules.example/ads.mrs", "behavior": "domain"},
    ], stored)
    assert out == {
        "secret-set": stored["secret-set"],
        "local": stored["local"],
        "ads": {"type": "http", "url": "https://rules.example/ads.mrs", "behavior": "domain", "format": "mrs", "interval": 86400},
    }
    # a provider left out is removed; the download still goes through the proxy
    assert set(out) == {"secret-set", "local", "ads"}
    assert build_rule_providers({"rule_providers": out})["ads"]["proxy"] == "FINAL-EXIT"


def test_provider_checks() -> None:
    for items, reason in [
        ([{"name": "a", "url": "https://u:p@rules.example/a.yaml", "behavior": "domain"}], "url"),
        ([{"name": "a", "url": "https://rules.example/a.yaml?x=1", "behavior": "domain"}], "url"),
        ([{"name": "a", "url": "ftp://rules.example/a.yaml", "behavior": "domain"}], "url"),
        ([{"name": "a", "url": "https://rules.example/a.mrs", "behavior": "classical"}], "invalid"),
        ([{"name": "a", "url": "https://rules.example/a.yaml", "behavior": "everything"}], "invalid"),
        ([{"name": "a,b", "url": "https://rules.example/a.yaml", "behavior": "domain"}], "invalid"),
        ([{"name": "A", "url": "https://rules.example/a.yaml", "behavior": "domain"},
          {"name": "a", "url": "https://rules.example/b.yaml", "behavior": "domain"}], "duplicate"),
        ([{"name": "new"}], "invalid"),
    ]:
        with pytest.raises(RuleError) as exc:
            normalize_providers(items, {})
        assert exc.value.reason == reason, items


# ---------------------------------------------------------------- Tesseract's part never changes

def _rules(config_yaml: str) -> list[str]:
    return yaml.safe_load(config_yaml)["rules"]


def test_user_rules_sit_between_tesseracts_rules_and_never_change_them() -> None:
    rows = [
        {"to": "direct", "kind": "suffix", "value": "example.cn"},
        {"to": "reject", "kind": "and", "of": [{"kind": "suffix", "value": "example.org"}, {"kind": "network", "value": "udp"}]},
        {"to": "proxy", "kind": "ip-cidr", "value": "203.0.113.0/24"},
    ]
    with_rows = _rules(build_full_clash_config({}, routing_config={"rules": {"list": rows}}))
    without = _rules(build_full_clash_config({}, routing_config={"rules": {"list": []}}))
    user = compile_rule_list(rows)
    assert without[-1] == "MATCH,GENERAL" and with_rows[-1] == "MATCH,GENERAL"
    start = len(without) - 1
    assert with_rows[start:start + len(user)] == user
    # strip the user's rules: what is left is exactly the configuration without them
    assert with_rows[:start] + with_rows[start + len(user):] == without


def test_compiled_rows_pass_mihomo_config_test_when_binary_exists() -> None:
    binary = Path("bin/mihomo.exe")
    if not binary.exists():
        pytest.skip("bin/mihomo.exe is not available")
    rows = [
        {"to": "direct", "kind": "suffix", "value": "example.cn"},
        {"to": "proxy", "kind": "domain", "value": "api.example.com"},
        {"to": "reject", "kind": "regex", "value": r"^ad[0-9]+\.example\.com$"},
        {"to": "proxy", "kind": "ip-cidr", "value": "203.0.113.0/24"},
        {"to": "proxy", "kind": "ip-cidr", "value": "2001:db8::/32", "resolve": True},
        {"to": "proxy", "kind": "src-ip-cidr", "value": "192.0.2.0/24"},
        {"to": "proxy", "kind": "dst-port", "value": "80/443/8000-9000"},
        {"to": "reject", "kind": "src-port", "value": "5000"},
        {"to": "direct", "kind": "process", "value": "chrome.exe"},
        {"to": "direct", "kind": "process-path", "value": r"C:\Program Files\Example\app.exe"},
        {"to": "reject", "kind": "network", "value": "udp", "not": True},
        {"to": "reject", "kind": "and", "of": [{"kind": "suffix", "value": "example.org"}, {"kind": "network", "value": "udp"}]},
        {"to": "proxy", "kind": "or", "of": [{"kind": "ip-cidr", "value": "198.51.100.0/24"}, {"kind": "keyword", "value": "tracker", "not": True}]},
        {"to": "reject", "kind": "rule-set", "value": "ads"},
    ]
    config = {
        "mixed-port": 17891, "mode": "rule", "log-level": "info", "proxies": [],
        "proxy-groups": [{"name": "GENERAL", "type": "select", "proxies": ["DIRECT"]}],
        "rule-providers": {"ads": {"type": "inline", "behavior": "domain", "payload": ["+.ads.example.com"]}},
        "rules": [*compile_rule_list(rows), "MATCH,GENERAL"],
    }
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "config.yaml"
        path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        result = subprocess.run(
            [str(binary.resolve()), "-t", "-f", str(path), "-d", temp_dir],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            **hidden_subprocess_kwargs(),
        )
    assert result.returncode == 0, result.stdout + result.stderr
