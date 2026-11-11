from __future__ import annotations

import pytest

import dns_resolver_policy as policy
from dns_resolver_policy import (
    BOOTSTRAP_LADDER_AUTO,
    BOOTSTRAP_LADDER_ENCRYPTED_ONLY,
    BOOTSTRAP_LADDER_USER_CUSTOM,
    CANDIDATE_QUALIFIED,
    CANDIDATE_TLS_FAILED,
    LINK_ANSWERS_INTERNAL_ONLY,
    LINK_ANSWERS_PUBLIC,
    LINK_CAPTIVE_SUSPECTED,
    LINK_OUTER_FAKE_IP,
    LINK_SILENT,
    RESOLVER_CATALOG,
    RUNG_ENCRYPTED,
    RUNG_INTRANET_ONLY,
    RUNG_SYSTEM,
    RUNG_USER_CUSTOM,
    EncryptedCandidateResult,
    LinkResolverResult,
    ResolverQualification,
    apply_dns_block_to_payload,
    bootstrap_ladder,
    choose_rung,
    display_summary,
    encrypted_candidates,
    formal_dns_is_shipped_default,
    next_rung,
    render_dns_block,
    rendered_dns_uses_hostnames,
    select_encrypted_endpoints,
    suffix_policy_keys,
)

FORMAL_DNS = {
    "enable": True,
    "listen": "127.0.0.1:1053",
    "ipv6": False,
    "enhanced-mode": "fake-ip",
    "respect-rules": False,
    "default-nameserver": ["223.5.5.5", "119.29.29.29", "114.114.114.114"],
    "nameserver": ["https://doh.pub/dns-query", "https://dns.alidns.com/dns-query"],
    "proxy-server-nameserver": ["223.5.5.5", "119.29.29.29", "114.114.114.114"],
    "fake-ip-range": "198.18.0.1/16",
    "fake-ip-filter": ["*.lan", "*.local"],
}

CANDIDATES = {candidate.url: candidate for candidate in encrypted_candidates()}


def candidate(url: str, verdict: str, latency_ms: int | None = None) -> EncryptedCandidateResult:
    return EncryptedCandidateResult(CANDIDATES[url], verdict, latency_ms)


def link(address: str, verdict: str, latency_ms: int | None = 10) -> LinkResolverResult:
    return LinkResolverResult(address, verdict, latency_ms)


def qualification(**overrides) -> ResolverQualification:
    base = dict(
        fingerprint="fp",
        wall_time_ms=100,
        link_resolvers=(),
        dns_suffixes=(),
        candidates=(),
        ipv4_available=True,
        ipv6_available=False,
        ipv6_only=False,
        outer_fake_ip=False,
        captive_suspected=False,
        inconclusive=False,
    )
    base.update(overrides)
    return ResolverQualification(**base)


def test_catalog_is_neutral_ip_literal_and_alphabetical():
    ids = [provider.provider_id for provider in RESOLVER_CATALOG]
    assert ids == sorted(ids)
    assert {"cloudflare", "quad9", "alidns", "dnspod", "google"} <= set(ids)
    for provider in RESOLVER_CATALOG:
        assert provider.ipv4 and provider.control_answers
    google = next(provider for provider in RESOLVER_CATALOG if provider.provider_id == "google")
    assert google.tie_break_rank > 0
    for entry in encrypted_candidates(include_ipv6=True):
        assert not rendered_dns_uses_hostnames({"nameserver": [entry.url]})


def test_encrypted_selection_takes_fastest_one_transport_per_provider_and_google_loses_ties():
    result = qualification(candidates=(
        candidate("https://8.8.8.8/dns-query", CANDIDATE_QUALIFIED, 20),
        candidate("tls://1.1.1.1", CANDIDATE_QUALIFIED, 35),
        candidate("https://1.1.1.1/dns-query", CANDIDATE_QUALIFIED, 40),
        candidate("tls://9.9.9.9", CANDIDATE_QUALIFIED, 45),
        candidate("https://223.5.5.5/dns-query", CANDIDATE_QUALIFIED, 200),
        candidate("https://1.12.12.12/dns-query", CANDIDATE_TLS_FAILED, None),
    ))
    selected = [item.candidate.url for item in select_encrypted_endpoints(result)]
    # Google is 15 ms faster than Cloudflare, inside the tie window: it loses.
    assert selected == ["tls://1.1.1.1", "tls://9.9.9.9", "https://8.8.8.8/dns-query"]


def test_google_wins_only_when_clearly_faster():
    result = qualification(candidates=(
        candidate("https://8.8.8.8/dns-query", CANDIDATE_QUALIFIED, 20),
        candidate("tls://1.1.1.1", CANDIDATE_QUALIFIED, 80),
    ))
    assert [item.candidate.provider_id for item in select_encrypted_endpoints(result)] == ["google", "cloudflare"]


def test_encrypted_rung_renders_exact_block_with_link_policy():
    result = qualification(
        candidates=(
            candidate("tls://9.9.9.9", CANDIDATE_QUALIFIED, 30),
            candidate("https://1.1.1.1/dns-query", CANDIDATE_QUALIFIED, 50),
        ),
        link_resolvers=(link("222.198.178.68", LINK_ANSWERS_PUBLIC, 8), link("222.198.178.168", LINK_ANSWERS_PUBLIC, 12)),
        dns_suffixes=("Campus.Example.EDU",),
    )
    decision = choose_rung(result, ladder=BOOTSTRAP_LADDER_AUTO)
    assert (decision.rung, decision.reason) == (RUNG_ENCRYPTED, policy.REASON_ENCRYPTED_QUALIFIED)
    block = render_dns_block(FORMAL_DNS, decision, result)
    assert block == {
        "enable": True,
        "listen": "127.0.0.1:1053",
        "ipv6": False,
        "enhanced-mode": "fake-ip",
        "respect-rules": False,
        "default-nameserver": ["system"],
        "nameserver": ["tls://9.9.9.9", "https://1.1.1.1/dns-query"],
        "proxy-server-nameserver": ["tls://9.9.9.9", "https://1.1.1.1/dns-query"],
        # DIRECT-routed public names keep the encrypted resolver: a link
        # resolver that answers the control names may still sinkhole others.
        "fake-ip-range": "198.18.0.1/16",
        # Intranet suffixes never receive a fake IP.
        "fake-ip-filter": ["*.lan", "*.local", "+.campus.example.edu"],
        "nameserver-policy": {
            "+.campus.example.edu": ["222.198.178.68", "222.198.178.168"],
            "+.lan": ["222.198.178.68", "222.198.178.168"],
            "+.local": ["222.198.178.68", "222.198.178.168"],
            "+.home.arpa": ["222.198.178.68", "222.198.178.168"],
        },
    }
    assert not rendered_dns_uses_hostnames(block)
    assert "direct-nameserver" not in block
    assert "direct-nameserver-follow-policy" not in block
    assert display_summary(decision.rung, result) == "encrypted (Quad9, Cloudflare)"


def test_respect_rules_is_user_policy_and_direct_nameserver_needs_public_link_answers():
    result = qualification(
        candidates=(candidate("tls://9.9.9.9", CANDIDATE_QUALIFIED, 30),),
        link_resolvers=(link("10.0.0.53", LINK_ANSWERS_INTERNAL_ONLY, 8),),
        dns_suffixes=("corp.example",),
    )
    decision = choose_rung(result, ladder=BOOTSTRAP_LADDER_AUTO)
    block = render_dns_block(dict(FORMAL_DNS, **{"respect-rules": True}), decision, result)
    assert block["respect-rules"] is True
    # An internal-only resolver still gets the suffix policy but never becomes
    # the resolver for every DIRECT-routed public name.
    assert "direct-nameserver" not in block
    assert block["nameserver-policy"]["+.corp.example"] == ["10.0.0.53"]
    assert "+.corp.example" in block["fake-ip-filter"]
    without_formal_filter = render_dns_block(
        {key: value for key, value in FORMAL_DNS.items() if key != "fake-ip-filter"}, decision, result,
    )
    assert without_formal_filter["fake-ip-filter"] == list(policy.DEFAULT_FAKE_IP_FILTER) + ["+.corp.example"]


def test_mode_policy_ladders_and_static_defaults():
    assert policy.normalize_dns_mode(None) == policy.DNS_MODE_AUTO
    assert policy.normalize_dns_mode({"mode": "custom"}) == policy.DNS_MODE_CUSTOM
    assert policy.normalize_dns_mode({"mode": "bogus", "encrypted_only": "yes"}) == policy.DNS_MODE_ENCRYPTED_ONLY
    assert policy.ladder_for_mode("system") == (RUNG_SYSTEM, RUNG_INTRANET_ONLY)
    assert policy.ladder_for_mode("encrypted_only") == (RUNG_ENCRYPTED,)
    assert policy.ladder_for_mode("custom") == BOOTSTRAP_LADDER_USER_CUSTOM
    assert policy.ladder_for_mode("nonsense") == BOOTSTRAP_LADDER_AUTO
    auto = policy.static_dns_lists("auto")
    assert auto["default-nameserver"] == ["system"]
    assert len(auto["nameserver"]) == 5 and auto["proxy-server-nameserver"] == auto["nameserver"]
    assert not rendered_dns_uses_hostnames(auto)
    assert policy.static_dns_lists("system") == {
        "nameserver": ["system"], "default-nameserver": ["system"], "proxy-server-nameserver": ["system"],
    }
    assert policy.static_dns_lists("custom") is None


def test_helper_dns_block_follows_the_published_rung_or_neutral_defaults():
    policy.publish_active_dns_block(None)
    try:
        neutral = policy.helper_dns_block()
        assert neutral["default-nameserver"] == ["system"]
        assert len(neutral["nameserver"]) == 5
        assert neutral["fake-ip-filter"] == ["*.lan", "*.local"]
        assert "ipv6" not in neutral
        policy.publish_active_dns_block({
            "enable": True,
            "nameserver": ["tls://9.9.9.9"],
            "default-nameserver": ["system"],
            "proxy-server-nameserver": ["tls://9.9.9.9"],
            "nameserver-policy": {"+.lan": ["10.0.0.53"]},
            "listen": "127.0.0.1:1053",
        })
        following = policy.helper_dns_block(ipv6=True)
        assert following["nameserver"] == ["tls://9.9.9.9"]
        assert following["proxy-server-nameserver"] == ["tls://9.9.9.9"]
        assert "system" not in neutral["proxy-server-nameserver"]
        assert following["nameserver-policy"] == {"+.lan": ["10.0.0.53"]}
        assert following["ipv6"] is True and "listen" not in following
        policy.publish_active_dns_block({"enable": True, "nameserver": []})
        assert policy.helper_dns_block()["nameserver"] == neutral["nameserver"]
    finally:
        policy.publish_active_dns_block(None)


def test_inconclusive_qualification_renders_full_pool_over_one_family():
    result = qualification(inconclusive=True)
    decision = choose_rung(result, ladder=BOOTSTRAP_LADDER_AUTO)
    assert (decision.rung, decision.reason) == (RUNG_ENCRYPTED, policy.REASON_INCONCLUSIVE_DEFAULT_ENCRYPTED)
    block = render_dns_block(FORMAL_DNS, decision, result)
    assert block["nameserver"] == [
        "https://223.5.5.5/dns-query",
        "https://1.1.1.1/dns-query",
        "https://1.12.12.12/dns-query",
        "https://8.8.8.8/dns-query",
        "https://9.9.9.9/dns-query",
    ]
    assert block["proxy-server-nameserver"] == block["nameserver"]
    assert "nameserver-policy" not in block

    ipv6_only = qualification(inconclusive=True, ipv4_available=False, ipv6_available=True, ipv6_only=True)
    block6 = render_dns_block(FORMAL_DNS, choose_rung(ipv6_only, ladder=BOOTSTRAP_LADDER_AUTO), ipv6_only)
    assert block6["ipv6"] is True
    assert all(url.startswith("https://[") for url in block6["nameserver"])
    assert not rendered_dns_uses_hostnames(block6)


def test_system_rung_lists_answering_link_resolvers_fastest_first():
    result = qualification(
        candidates=(candidate("tls://9.9.9.9", CANDIDATE_TLS_FAILED),),
        link_resolvers=(
            link("10.0.0.53", LINK_ANSWERS_INTERNAL_ONLY, 30),
            link("222.198.178.68", LINK_ANSWERS_PUBLIC, 5),
            link("192.0.2.9", LINK_SILENT, None),
        ),
        dns_suffixes=("corp.example",),
    )
    decision = choose_rung(result, ladder=BOOTSTRAP_LADDER_AUTO)
    assert (decision.rung, decision.reason, decision.position) == (
        RUNG_SYSTEM, policy.REASON_ENCRYPTED_UNREACHABLE, 1,
    )
    block = render_dns_block(FORMAL_DNS, decision, result)
    assert block["nameserver"] == ["222.198.178.68", "10.0.0.53"]
    assert block["proxy-server-nameserver"] == ["222.198.178.68", "10.0.0.53"]
    assert block["default-nameserver"] == ["system"]
    assert block["nameserver-policy"]["+.corp.example"] == ["222.198.178.68", "10.0.0.53"]
    assert display_summary(decision.rung, result) == "system (222.198.178.68, 10.0.0.53)"


def test_outer_fake_ip_tunnel_uses_bare_system_client():
    result = qualification(
        candidates=(candidate("tls://9.9.9.9", CANDIDATE_TLS_FAILED),),
        link_resolvers=(link("222.198.178.68", LINK_OUTER_FAKE_IP, 0),),
        outer_fake_ip=True,
    )
    decision = choose_rung(result, ladder=BOOTSTRAP_LADDER_AUTO)
    assert (decision.rung, decision.reason) == (RUNG_SYSTEM, policy.REASON_OUTER_FAKE_IP)
    block = render_dns_block(FORMAL_DNS, decision, result)
    assert block["nameserver"] == ["system"]
    assert "nameserver-policy" not in block


def test_intranet_only_link_skips_system_and_terminates_with_policy():
    result = qualification(
        candidates=(candidate("https://1.1.1.1/dns-query", CANDIDATE_TLS_FAILED),),
        link_resolvers=(link("10.0.0.53", LINK_ANSWERS_INTERNAL_ONLY, 4),),
        dns_suffixes=("corp.example",),
    )
    decision = choose_rung(result, ladder=BOOTSTRAP_LADDER_AUTO)
    assert (decision.rung, decision.reason) == (RUNG_INTRANET_ONLY, policy.REASON_LINK_INTERNAL_ONLY)
    assert decision.remaining == 0
    block = render_dns_block(FORMAL_DNS, decision, result)
    assert block["nameserver"] == ["rcode://name_error"]
    assert block["proxy-server-nameserver"] == ["rcode://name_error"]
    assert block["nameserver-policy"]["+.corp.example"] == ["10.0.0.53"]
    assert not rendered_dns_uses_hostnames(block)


def test_captive_portal_answers_are_not_public_but_do_move_past_encrypted():
    result = qualification(
        candidates=(candidate("https://1.1.1.1/dns-query", CANDIDATE_TLS_FAILED),),
        link_resolvers=(link("192.168.1.1", LINK_CAPTIVE_SUSPECTED, 3),),
        captive_suspected=True,
    )
    decision = choose_rung(result, ladder=BOOTSTRAP_LADDER_AUTO)
    assert decision.rung == RUNG_SYSTEM
    block = render_dns_block(FORMAL_DNS, decision, result)
    # A lying resolver is not listed as an explicit upstream.
    assert block["nameserver"] == ["system"]


def test_silent_link_and_failed_encrypted_is_not_evidence_to_degrade():
    result = qualification(
        candidates=(candidate("https://1.1.1.1/dns-query", CANDIDATE_TLS_FAILED),),
        link_resolvers=(link("192.0.2.9", LINK_SILENT, None),),
    )
    decision = choose_rung(result, ladder=BOOTSTRAP_LADDER_AUTO)
    assert (decision.rung, decision.reason) == (RUNG_ENCRYPTED, policy.REASON_ENCRYPTED_UNVERIFIED)


def test_encrypted_only_ladder_never_degrades():
    result = qualification(
        candidates=(candidate("https://1.1.1.1/dns-query", CANDIDATE_TLS_FAILED),),
        link_resolvers=(link("222.198.178.68", LINK_ANSWERS_PUBLIC, 5),),
    )
    decision = choose_rung(result, ladder=BOOTSTRAP_LADDER_ENCRYPTED_ONLY)
    assert (decision.rung, decision.reason, decision.remaining) == (
        RUNG_ENCRYPTED, policy.REASON_ENCRYPTED_ONLY_PINNED, 0,
    )
    assert next_rung(decision, result, reason="x") is None


def test_user_custom_rung_keeps_formal_block_verbatim_and_escalates_one_step():
    custom = dict(FORMAL_DNS, nameserver=["tls://dns.example"], enable=False)
    result = qualification(link_resolvers=(link("222.198.178.68", LINK_ANSWERS_PUBLIC, 5),))
    ladder = bootstrap_ladder(user_custom=not formal_dns_is_shipped_default(custom))
    assert ladder == BOOTSTRAP_LADDER_USER_CUSTOM
    decision = choose_rung(result, ladder=ladder)
    assert (decision.rung, decision.reason) == (RUNG_USER_CUSTOM, policy.REASON_USER_CUSTOM)
    block = render_dns_block(custom, decision, result)
    assert block == dict(custom, enable=True)
    escalated = next_rung(decision, result, reason=policy.REASON_ESCALATED_DNS_FAULTS)
    assert escalated is not None
    assert (escalated.rung, escalated.reason, escalated.position) == (
        RUNG_SYSTEM, policy.REASON_ESCALATED_DNS_FAULTS, 1,
    )
    final = next_rung(escalated, result, reason="again")
    assert final is not None and final.rung == RUNG_INTRANET_ONLY
    assert next_rung(final, result, reason="none") is None


@pytest.mark.parametrize("block,expected", [
    (FORMAL_DNS, True),
    ({}, True),
    (dict(FORMAL_DNS, nameserver=["https://dns.alidns.com/dns-query", "https://doh.pub/dns-query"]), True),
    # The older shipped proxy-server pair, which the previous renderer rewrote.
    (dict(FORMAL_DNS, **{"proxy-server-nameserver": ["https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query"]}), True),
    (dict(FORMAL_DNS, **{"proxy-server-nameserver": ["https://1.1.1.1/dns-query"]}), False),
    (dict(FORMAL_DNS, nameserver=["tls://dns.example"]), False),
    (dict(FORMAL_DNS, **{"default-nameserver": ["192.0.2.53"]}), False),
    ({"enable": True, "nameserver": ["1.1.1.1"]}, False),
])
def test_formal_dns_is_shipped_default(block, expected):
    assert formal_dns_is_shipped_default(block) is expected


def test_suffix_policy_keys_normalize_and_drop_invalid():
    assert suffix_policy_keys(("Campus.Example.EDU.", "bad suffix", "", "lan", "über.example")) == (
        "+.campus.example.edu",
        "+.lan",
        "+.xn--ber-goa.example",
        "+.local",
        "+.home.arpa",
    )


def test_apply_dns_block_to_payload_never_invents_a_dns_block():
    block = {"enable": True, "nameserver": ["tls://9.9.9.9"]}
    with_dns = apply_dns_block_to_payload({"dns": FORMAL_DNS, "rules": ["MATCH,GENERAL"]}, block)
    assert with_dns["dns"] == block
    assert with_dns["rules"] == ["MATCH,GENERAL"]
    without = apply_dns_block_to_payload({"rules": ["MATCH,GENERAL"]}, block)
    assert "dns" not in without
    untouched = apply_dns_block_to_payload({"dns": FORMAL_DNS}, None)
    assert untouched["dns"] == FORMAL_DNS
    assert untouched["dns"] is not FORMAL_DNS


def test_every_rung_stays_free_of_hostnames_for_a_shipped_default_policy():
    result = qualification(
        candidates=(candidate("tls://9.9.9.9", CANDIDATE_QUALIFIED, 30),),
        link_resolvers=(link("222.198.178.68", LINK_ANSWERS_PUBLIC, 5),),
        dns_suffixes=("campus.example.edu",),
    )
    for position in range(len(BOOTSTRAP_LADDER_AUTO)):
        decision = choose_rung(result, ladder=BOOTSTRAP_LADDER_AUTO, position=position)
        block = render_dns_block(FORMAL_DNS, decision, result)
        assert block["enable"] is True
        assert block["respect-rules"] is False
        assert not rendered_dns_uses_hostnames(block), decision
