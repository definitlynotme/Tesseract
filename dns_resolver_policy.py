"""Region-neutral resolver catalog, degradation rungs and pure DNS renderers.

Nothing in this module performs I/O.  ``dns_resolver_qualification`` produces
a :class:`ResolverQualification` from bounded probes; this module turns that
evidence into one discrete rung and renders the ``dns`` block Mihomo loads.

Design rules (verified against Mihomo 1.19.27):

* Every Mihomo nameserver list is queried concurrently, fastest answer wins.
  A plaintext and an encrypted resolver must therefore never share a list;
  degradation happens by switching the whole block to the next rung.
* Encrypted candidates are IP literals, so no plaintext bootstrap lookup is
  needed.  ``default-nameserver`` still has to be non-empty; ``system`` is
  legal there and is never consulted for literal endpoints.
* Unknown evidence never degrades.  A rung moves down only on positive
  evidence (a link resolver answered while encrypted endpoints did not, or the
  running Core reported resolution failures after the rung was applied).
* Cold start is ordinary expansion with an empty pool: the same rung block
  serves the no-capture bootstrap Core and the formal Core.
"""

from __future__ import annotations

import copy
import ipaddress
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

RUNG_ENCRYPTED = "encrypted"
RUNG_USER_CUSTOM = "user_custom"
RUNG_SYSTEM = "system"
RUNG_INTRANET_ONLY = "intranet_only"

BOOTSTRAP_LADDER_AUTO: tuple[str, ...] = (RUNG_ENCRYPTED, RUNG_SYSTEM, RUNG_INTRANET_ONLY)
BOOTSTRAP_LADDER_USER_CUSTOM: tuple[str, ...] = (RUNG_USER_CUSTOM, RUNG_SYSTEM, RUNG_INTRANET_ONLY)
BOOTSTRAP_LADDER_ENCRYPTED_ONLY: tuple[str, ...] = (RUNG_ENCRYPTED,)
BOOTSTRAP_LADDER_SYSTEM: tuple[str, ...] = (RUNG_SYSTEM, RUNG_INTRANET_ONLY)

# ``client.dns.mode`` policy.
DNS_MODE_AUTO = "auto"
DNS_MODE_ENCRYPTED_ONLY = "encrypted_only"
DNS_MODE_SYSTEM = "system"
DNS_MODE_CUSTOM = "custom"
DNS_MODES: tuple[str, ...] = (DNS_MODE_AUTO, DNS_MODE_ENCRYPTED_ONLY, DNS_MODE_SYSTEM, DNS_MODE_CUSTOM)
_LADDER_BY_MODE = {
    DNS_MODE_AUTO: BOOTSTRAP_LADDER_AUTO,
    DNS_MODE_ENCRYPTED_ONLY: BOOTSTRAP_LADDER_ENCRYPTED_ONLY,
    DNS_MODE_SYSTEM: BOOTSTRAP_LADDER_SYSTEM,
    DNS_MODE_CUSTOM: BOOTSTRAP_LADDER_USER_CUSTOM,
}

# The historical shipped lists. They are recognised (so an unmigrated config
# is classified as "auto", not as a deliberate custom policy) but never
# rendered as a default any more.
LEGACY_SHIPPED_DOH_NAMESERVERS: tuple[str, ...] = (
    "https://doh.pub/dns-query",
    "https://dns.alidns.com/dns-query",
)
LEGACY_SHIPPED_PLAIN_NAMESERVERS: tuple[str, ...] = ("223.5.5.5", "119.29.29.29", "114.114.114.114")
LEGACY_PROXY_SERVER_NAMESERVERS: frozenset[str] = frozenset(
    {"https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query"}
)
# Region-neutral fake-IP exclusions: local suffixes plus the OS connectivity checks.
DEFAULT_FAKE_IP_FILTER: tuple[str, ...] = (
    "*.lan",
    "*.local",
    "+.home.arpa",
    "+.msftconnecttest.com",
    "+.msftncsi.com",
)

TRANSPORT_DOH = "doh"
TRANSPORT_DOT = "dot"

# Link resolver verdicts.
LINK_ANSWERS_PUBLIC = "answers_public"
LINK_ANSWERS_INTERNAL_ONLY = "answers_internal_only"
LINK_CAPTIVE_SUSPECTED = "captive_suspected"
LINK_SILENT = "silent"
LINK_OUTER_FAKE_IP = "outer_fake_ip"
LINK_NOT_PROBED = "not_probed"

# Encrypted candidate verdicts.
CANDIDATE_QUALIFIED = "qualified"
CANDIDATE_ANSWER_MISMATCH = "answer_mismatch"
CANDIDATE_TLS_FAILED = "tls_failed"
CANDIDATE_CONNECT_FAILED = "connect_failed"
CANDIDATE_TIMEOUT = "timeout"
CANDIDATE_NOT_PROBED = "not_probed"

# Decision reasons (stable strings; they are logged and published).
REASON_ENCRYPTED_QUALIFIED = "encrypted_candidates_qualified"
REASON_INCONCLUSIVE_DEFAULT_ENCRYPTED = "qualification_inconclusive_default_encrypted"
REASON_ENCRYPTED_UNREACHABLE = "encrypted_unreachable_link_resolver_answers"
REASON_ENCRYPTED_UNVERIFIED = "encrypted_unverified_no_link_evidence"
REASON_OUTER_FAKE_IP = "outer_fake_ip_resolver_active"
REASON_USER_CUSTOM = "user_custom_policy"
REASON_LINK_RESOLVERS_ANSWER = "link_resolvers_answer"
REASON_LINK_INTERNAL_ONLY = "link_resolvers_internal_only"
REASON_INTRANET_ONLY = "intranet_only_policy"
REASON_ENCRYPTED_ONLY_PINNED = "encrypted_only_pinned"
REASON_ESCALATED_DNS_FAULTS = "escalated_after_dns_resolve_failures"
REASON_ESCALATED_CONTROL_QUERY = "escalated_after_control_query_failed"

MAX_ENCRYPTED_ENDPOINTS = 3
TIE_BREAK_WINDOW_MS = 30
LOCAL_SUFFIX_POLICY_KEYS: tuple[str, ...] = ("+.lan", "+.local", "+.home.arpa")
OUTER_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


@dataclass(frozen=True, slots=True)
class ResolverProvider:
    """One encrypted resolver operator, addressed only by IP literals."""

    provider_id: str
    display_name: str
    ipv4: tuple[str, ...]
    ipv6: tuple[str, ...]
    control_name: str
    control_answers: frozenset[str]
    doh_path: str = "/dns-query"
    # Higher ranks lose ties inside TIE_BREAK_WINDOW_MS; latency decides otherwise.
    tie_break_rank: int = 0


RESOLVER_CATALOG: tuple[ResolverProvider, ...] = (
    ResolverProvider(
        "alidns", "AliDNS",
        ("223.5.5.5", "223.6.6.6"), ("2400:3200::1",),
        "dns.alidns.com", frozenset({"223.5.5.5", "223.6.6.6"}),
    ),
    ResolverProvider(
        "cloudflare", "Cloudflare",
        ("1.1.1.1", "1.0.0.1"), ("2606:4700:4700::1111",),
        "one.one.one.one", frozenset({"1.1.1.1", "1.0.0.1"}),
    ),
    ResolverProvider(
        "dnspod", "DNSPod",
        ("1.12.12.12", "120.53.53.53"), ("2402:4e00::",),
        "dot.pub", frozenset({"1.12.12.12", "120.53.53.53"}),
    ),
    ResolverProvider(
        "google", "Google",
        ("8.8.8.8", "8.8.4.4"), ("2001:4860:4860::8888",),
        "dns.google", frozenset({"8.8.8.8", "8.8.4.4"}),
        tie_break_rank=1,
    ),
    ResolverProvider(
        "quad9", "Quad9",
        ("9.9.9.9", "149.112.112.112"), ("2620:fe::fe",),
        "dns.quad9.net", frozenset({"9.9.9.9", "149.112.112.112"}),
    ),
)

# Two unrelated operators; either matching answer proves a link resolver
# reaches the public namespace without lying about it.
LINK_RESOLVER_CONTROL_NAMES: tuple[str, ...] = ("one.one.one.one", "dns.google")
LINK_RESOLVER_CONTROL_ANSWERS: frozenset[str] = frozenset(
    {"1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4"}
)


def _bracket_if_ipv6(address: str) -> str:
    return f"[{address}]" if ":" in address else address


def encrypted_endpoint_url(address: str, transport: str, doh_path: str = "/dns-query") -> str:
    host = _bracket_if_ipv6(address)
    if transport == TRANSPORT_DOT:
        return f"tls://{host}"
    return f"https://{host}{doh_path}"


@dataclass(frozen=True, slots=True)
class EncryptedCandidate:
    provider_id: str
    display_name: str
    address: str
    transport: str
    url: str
    control_name: str
    control_answers: frozenset[str]
    family: int
    tie_break_rank: int = 0
    # Only tests point a probe at a loopback server; the catalog uses defaults.
    port: int | None = None


def encrypted_candidates(
    *,
    include_ipv4: bool = True,
    include_ipv6: bool = False,
    transports: Sequence[str] = (TRANSPORT_DOH, TRANSPORT_DOT),
    catalog: Sequence[ResolverProvider] = RESOLVER_CATALOG,
) -> tuple[EncryptedCandidate, ...]:
    """Expand the catalog into probeable IP-literal endpoints (primary address)."""

    candidates: list[EncryptedCandidate] = []
    for provider in catalog:
        addresses: list[tuple[str, int]] = []
        if include_ipv4 and provider.ipv4:
            addresses.append((provider.ipv4[0], 4))
        if include_ipv6 and provider.ipv6:
            addresses.append((provider.ipv6[0], 6))
        for address, family in addresses:
            for transport in transports:
                candidates.append(
                    EncryptedCandidate(
                        provider_id=provider.provider_id,
                        display_name=provider.display_name,
                        address=address,
                        transport=transport,
                        url=encrypted_endpoint_url(address, transport, provider.doh_path),
                        control_name=provider.control_name,
                        control_answers=provider.control_answers,
                        family=family,
                        tie_break_rank=provider.tie_break_rank,
                    )
                )
    return tuple(candidates)


def full_encrypted_pool(
    *, include_ipv4: bool = True, include_ipv6: bool = False,
) -> tuple[str, ...]:
    """DoH URL per provider, used when qualification produced no verdict."""

    return tuple(
        candidate.url
        for candidate in encrypted_candidates(
            include_ipv4=include_ipv4, include_ipv6=include_ipv6, transports=(TRANSPORT_DOH,),
        )
    )


# ------------------------------------------------------------ mode policy

def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return []


def dns_config_list(dns_config: Mapping[str, Any] | None, key: str) -> list[str]:
    """Read a resolver list accepting both ``snake_case`` and ``hyphen-case`` keys."""

    if not isinstance(dns_config, Mapping):
        return []
    value = dns_config.get(key)
    if value is None:
        value = dns_config.get(key.replace("_", "-") if "_" in key else key.replace("-", "_"))
    return _string_list(value)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def dns_lists_are_legacy_defaults(dns_config: Mapping[str, Any] | None) -> bool:
    """Whether the configured lists are exactly the historical shipped lists.

    Absent lists count as default; anything else is deliberate user policy.
    The shipped ``proxy_server_nameserver`` existed in two spellings: the
    plain triple and the older Cloudflare/Google DoH pair, which the previous
    renderer silently rewrote to the triple.  Neither was ever user policy.
    """

    if not isinstance(dns_config, Mapping):
        return True
    nameserver = dns_config_list(dns_config, "nameserver")
    default_ns = dns_config_list(dns_config, "default_nameserver")
    proxy_ns = dns_config_list(dns_config, "proxy_server_nameserver")
    return (
        (not nameserver or set(nameserver) == set(LEGACY_SHIPPED_DOH_NAMESERVERS))
        and (not default_ns or set(default_ns) == set(LEGACY_SHIPPED_PLAIN_NAMESERVERS))
        and (
            not proxy_ns
            or set(proxy_ns) == set(LEGACY_SHIPPED_PLAIN_NAMESERVERS)
            or set(proxy_ns) == set(LEGACY_PROXY_SERVER_NAMESERVERS)
        )
    )


# Kept for callers written against the first round of this work.
formal_dns_is_shipped_default = dns_lists_are_legacy_defaults


def normalize_dns_mode(dns_config: Mapping[str, Any] | None) -> str:
    """Return the effective ``client.dns.mode``.

    An explicit valid ``mode`` wins.  Otherwise (unmigrated configs) the mode
    is derived: ``encrypted_only: true`` pins encryption, lists that differ
    from the historical shipped lists are a deliberate ``custom`` policy, and
    everything else is ``auto``.
    """

    if not isinstance(dns_config, Mapping):
        return DNS_MODE_AUTO
    mode = str(dns_config.get("mode") or "").strip().lower().replace("-", "_")
    if mode in DNS_MODES:
        return mode
    if _truthy(dns_config.get("encrypted_only")):
        return DNS_MODE_ENCRYPTED_ONLY
    if not dns_lists_are_legacy_defaults(dns_config):
        return DNS_MODE_CUSTOM
    return DNS_MODE_AUTO


def ladder_for_mode(mode: str) -> tuple[str, ...]:
    return _LADDER_BY_MODE.get(str(mode or "").strip().lower(), BOOTSTRAP_LADDER_AUTO)


def bootstrap_ladder(*, encrypted_only: bool = False, user_custom: bool = False) -> tuple[str, ...]:
    if encrypted_only:
        return BOOTSTRAP_LADDER_ENCRYPTED_ONLY
    if user_custom:
        return BOOTSTRAP_LADDER_USER_CUSTOM
    return BOOTSTRAP_LADDER_AUTO


def static_dns_lists(mode: str) -> dict[str, list[str]] | None:
    """Resolver lists rendered when no runtime qualification exists yet.

    ``custom`` returns ``None`` (the caller owns the lists).  The static
    encrypted pool is IPv4-only: the runtime plan handles IPv6-only links.
    """

    normalized = str(mode or "").strip().lower()
    if normalized == DNS_MODE_CUSTOM:
        return None
    if normalized == DNS_MODE_SYSTEM:
        return {
            "nameserver": ["system"],
            "default-nameserver": ["system"],
            "proxy-server-nameserver": ["system"],
        }
    pool = list(full_encrypted_pool(include_ipv4=True, include_ipv6=False))
    return {
        "nameserver": list(pool),
        "default-nameserver": ["system"],
        "proxy-server-nameserver": list(pool),
    }


# ------------------------------------------------- active block for helpers

_ACTIVE_DNS_BLOCK_LOCK = threading.Lock()
_ACTIVE_DNS_BLOCK: dict[str, Any] | None = None


def publish_active_dns_block(block: Mapping[str, Any] | None) -> None:
    """Publish the rung the runtime rendered so helper Cores resolve alike."""

    global _ACTIVE_DNS_BLOCK
    with _ACTIVE_DNS_BLOCK_LOCK:
        _ACTIVE_DNS_BLOCK = copy.deepcopy(dict(block)) if isinstance(block, Mapping) else None


def active_dns_block() -> dict[str, Any] | None:
    with _ACTIVE_DNS_BLOCK_LOCK:
        return copy.deepcopy(_ACTIVE_DNS_BLOCK) if _ACTIVE_DNS_BLOCK is not None else None


def helper_dns_block(*, ipv6: bool = False) -> dict[str, Any]:
    """DNS block for validator/diagnostic helper Cores.

    Helper Cores proxy every request (``MATCH,<group>``), so this only matters
    for resolving node hostnames.  They follow the runtime's active rung when
    one is published and otherwise use the neutral static defaults; no region
    specific resolver is written anywhere.
    """

    block: dict[str, Any] = {
        "enable": True,
        "enhanced-mode": "fake-ip",
        "fake-ip-range": "198.18.0.1/16",
    }
    active = active_dns_block()
    lists = None
    if isinstance(active, Mapping):
        lists = {
            key: list(active.get(key) or [])
            for key in ("nameserver", "default-nameserver", "proxy-server-nameserver")
        }
        if not all(lists.values()):
            lists = None
    if lists is None:
        lists = static_dns_lists(DNS_MODE_AUTO) or {}
    block.update(lists)
    # Only the runtime DNS policy may authorize a resolver fallback.
    if isinstance(active, Mapping) and isinstance(active.get("nameserver-policy"), Mapping):
        block["nameserver-policy"] = copy.deepcopy(dict(active["nameserver-policy"]))
    block["fake-ip-filter"] = ["*.lan", "*.local"]
    if ipv6:
        block["ipv6"] = True
    return block


# ------------------------------------------------------------ qualification

@dataclass(frozen=True, slots=True)
class LinkResolverResult:
    address: str
    verdict: str
    latency_ms: int | None = None
    interface_index: int | None = None
    has_gateway: bool = False


@dataclass(frozen=True, slots=True)
class EncryptedCandidateResult:
    candidate: EncryptedCandidate
    verdict: str
    latency_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ResolverQualification:
    """Immutable evidence from one bounded qualification run."""

    fingerprint: str
    wall_time_ms: int
    link_resolvers: tuple[LinkResolverResult, ...] = ()
    dns_suffixes: tuple[str, ...] = ()
    candidates: tuple[EncryptedCandidateResult, ...] = ()
    ipv4_available: bool = True
    ipv6_available: bool = False
    ipv6_only: bool = False
    outer_fake_ip: bool = False
    captive_suspected: bool = False
    inconclusive: bool = True

    def qualified_candidates(self) -> tuple[EncryptedCandidateResult, ...]:
        return tuple(
            sorted(
                (item for item in self.candidates if item.verdict == CANDIDATE_QUALIFIED),
                key=_candidate_sort_key,
            )
        )

    def public_link_resolvers(self) -> tuple[LinkResolverResult, ...]:
        return tuple(
            sorted(
                (item for item in self.link_resolvers if item.verdict == LINK_ANSWERS_PUBLIC),
                key=lambda item: (item.latency_ms if item.latency_ms is not None else 10**9, item.address),
            )
        )

    def answering_link_resolvers(self) -> tuple[LinkResolverResult, ...]:
        """Resolvers that answered something (public or internal), fastest first."""

        return tuple(
            sorted(
                (
                    item for item in self.link_resolvers
                    if item.verdict in {LINK_ANSWERS_PUBLIC, LINK_ANSWERS_INTERNAL_ONLY}
                ),
                key=lambda item: (item.latency_ms if item.latency_ms is not None else 10**9, item.address),
            )
        )

    def link_resolvers_internal_only(self) -> bool:
        verdicts = {item.verdict for item in self.link_resolvers}
        return bool(verdicts) and verdicts <= {LINK_ANSWERS_INTERNAL_ONLY}

    def link_path_answers(self) -> bool:
        """Positive evidence that the link's plaintext resolver path is alive.

        Any reply counts (public, internal-only, captive, or an outer fake-IP
        tunnel answering the host resolver): the network demonstrably carries
        DNS, so encrypted endpoints that stayed silent were not merely
        unprobed.  What the reply is worth is decided by the later rungs.
        """

        if self.outer_fake_ip:
            return True
        return any(
            item.verdict in {LINK_ANSWERS_PUBLIC, LINK_ANSWERS_INTERNAL_ONLY, LINK_CAPTIVE_SUSPECTED}
            for item in self.link_resolvers
        )


def _candidate_sort_key(item: EncryptedCandidateResult) -> tuple[int, int, str, str]:
    # A ranked operator only wins when it is faster by more than the tie
    # window; expressed as a latency penalty so ordering stays monotonic.
    latency = item.latency_ms if item.latency_ms is not None else 10**9
    return (
        latency + item.candidate.tie_break_rank * TIE_BREAK_WINDOW_MS,
        item.candidate.tie_break_rank,
        item.candidate.provider_id,
        item.candidate.transport,
    )


def select_encrypted_endpoints(
    qualification: ResolverQualification,
    *,
    limit: int = MAX_ENCRYPTED_ENDPOINTS,
) -> tuple[EncryptedCandidateResult, ...]:
    """Fastest qualified endpoints, one transport per provider, ties by rank."""

    chosen: list[EncryptedCandidateResult] = []
    seen_providers: set[str] = set()
    for item in qualification.qualified_candidates():
        if item.candidate.provider_id in seen_providers:
            continue
        seen_providers.add(item.candidate.provider_id)
        chosen.append(item)
        if len(chosen) >= max(1, int(limit)):
            break
    return tuple(chosen)


@dataclass(frozen=True, slots=True)
class RungDecision:
    rung: str
    reason: str
    position: int
    ladder: tuple[str, ...]

    @property
    def remaining(self) -> int:
        return max(0, len(self.ladder) - self.position - 1)


def choose_rung(
    qualification: ResolverQualification,
    *,
    ladder: Sequence[str],
    position: int = 0,
    reason_override: str = "",
) -> RungDecision:
    """Pick the rung at ``position``, skipping only on positive evidence."""

    steps = tuple(ladder)
    if not steps:
        raise ValueError("resolver ladder must not be empty")
    index = max(0, min(int(position), len(steps) - 1))
    reason = ""
    # The evidence that skipped an earlier rung explains the final choice
    # better than the fact that a later rung is never skipped.
    skip_reason = ""
    while True:
        rung = steps[index]
        last = index >= len(steps) - 1
        if rung == RUNG_ENCRYPTED:
            qualified = qualification.qualified_candidates()
            if qualified:
                reason = REASON_ENCRYPTED_QUALIFIED
                break
            if qualification.inconclusive:
                reason = REASON_INCONCLUSIVE_DEFAULT_ENCRYPTED
                break
            if last:
                # A pinned encrypted-only ladder stays encrypted; the terminal
                # state reports that the network cannot reach encrypted DNS.
                reason = REASON_ENCRYPTED_ONLY_PINNED
                break
            if qualification.link_path_answers():
                skip_reason = (
                    REASON_OUTER_FAKE_IP
                    if qualification.outer_fake_ip
                    else REASON_ENCRYPTED_UNREACHABLE
                )
                index += 1
                continue
            # Nothing qualified but no resolver proved the network works either:
            # that is not evidence against encrypted DNS.
            reason = REASON_ENCRYPTED_UNVERIFIED
            break
        if rung == RUNG_USER_CUSTOM:
            reason = REASON_USER_CUSTOM
            break
        if rung == RUNG_SYSTEM:
            if qualification.link_resolvers_internal_only() and not last:
                skip_reason = REASON_LINK_INTERNAL_ONLY
                index += 1
                continue
            reason = skip_reason or REASON_LINK_RESOLVERS_ANSWER
            break
        # intranet_only or an unknown terminal rung: never skipped.
        reason = skip_reason or REASON_INTRANET_ONLY
        break
    return RungDecision(
        rung=steps[index],
        reason=str(reason_override or reason),
        position=index,
        ladder=steps,
    )


@dataclass(frozen=True, slots=True)
class DnsResolverPlan:
    """The rung currently rendered into every Core config of this runtime run.

    One plan serves the no-capture bootstrap Core and the formal Core alike;
    cold start is ordinary expansion with an empty pool, so the resolver
    decision must not depend on the phase.
    """

    decision: RungDecision
    qualification: ResolverQualification
    block: Mapping[str, Any]
    revision: int
    applied_at_monotonic: float
    fault_count_at_apply: int = 0
    encrypted_only: bool = False
    user_custom: bool = False
    prepared_reason: str = ""
    mode: str = DNS_MODE_AUTO

    @property
    def rung(self) -> str:
        return self.decision.rung

    @property
    def reason(self) -> str:
        return self.decision.reason

    @property
    def remaining(self) -> int:
        return self.decision.remaining

    def summary(self) -> str:
        return display_summary(self.rung, self.qualification)

    def report_fields(self) -> dict[str, Any]:
        qualification = self.qualification
        return {
            "dns_resolver_mode": self.mode,
            "dns_resolver_rung": self.rung,
            "dns_resolver_reason": self.reason,
            "dns_resolver_revision": int(self.revision),
            "dns_resolver_encrypted": self.rung == RUNG_ENCRYPTED,
            "dns_resolver_encrypted_only": bool(self.encrypted_only),
            "dns_resolver_summary": self.summary(),
            "dns_resolver_link_resolvers": [
                item.address for item in qualification.answering_link_resolvers()
            ],
            "dns_resolver_candidates": [
                {
                    "provider": item.candidate.provider_id,
                    "transport": item.candidate.transport,
                    "verdict": item.verdict,
                    "latency_ms": item.latency_ms,
                }
                for item in qualification.candidates
            ],
            "dns_resolver_ladder_remaining": int(self.remaining),
            "dns_resolver_fingerprint": qualification.fingerprint,
            "dns_resolver_outer_fake_ip": bool(qualification.outer_fake_ip),
            "dns_resolver_captive_suspected": bool(qualification.captive_suspected),
        }


def next_rung(decision: RungDecision, qualification: ResolverQualification, *, reason: str) -> RungDecision | None:
    """Escalate exactly one position; ``None`` when the ladder is exhausted."""

    if decision.remaining <= 0:
        return None
    return choose_rung(
        qualification,
        ladder=decision.ladder,
        position=decision.position + 1,
        reason_override=reason,
    )


# ---------------------------------------------------------------- rendering

def _normalize_suffix(value: str) -> str:
    text = str(value or "").strip().strip(".").lower()
    if not text or len(text) > 253:
        return ""
    try:
        encoded = text.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return ""
    labels = encoded.split(".")
    for label in labels:
        if not label or len(label) > 63:
            return ""
        if not all(ch.isalnum() or ch in "-_" for ch in label):
            return ""
    return encoded


def suffix_policy_keys(suffixes: Iterable[str]) -> tuple[str, ...]:
    keys: list[str] = []
    for suffix in suffixes:
        normalized = _normalize_suffix(suffix)
        if not normalized:
            continue
        key = f"+.{normalized}"
        if key not in keys:
            keys.append(key)
    for key in LOCAL_SUFFIX_POLICY_KEYS:
        if key not in keys:
            keys.append(key)
    return tuple(keys)


def _base_keys(formal_dns: Mapping[str, Any] | None) -> dict[str, Any]:
    formal = formal_dns if isinstance(formal_dns, Mapping) else {}
    block: dict[str, Any] = {"enable": True}
    block["listen"] = str(formal.get("listen") or "127.0.0.1:1053")
    block["ipv6"] = bool(formal.get("ipv6", False))
    block["enhanced-mode"] = str(formal.get("enhanced-mode") or "fake-ip")
    # User policy; the bootstrap builder forces it off because that Core has
    # no proxy outbound a rule could route a DNS query through.
    block["respect-rules"] = bool(formal.get("respect-rules", False))
    return block


def _fake_ip_keys(
    formal_dns: Mapping[str, Any] | None,
    block: dict[str, Any],
    suffixes: Iterable[str] = (),
) -> None:
    formal = formal_dns if isinstance(formal_dns, Mapping) else {}
    if block.get("enhanced-mode") != "fake-ip":
        return
    block["fake-ip-range"] = str(formal.get("fake-ip-range") or "198.18.0.1/16")
    fake_ip_filter = formal.get("fake-ip-filter")
    entries = (
        [str(item) for item in fake_ip_filter if str(item or "").strip()]
        if isinstance(fake_ip_filter, list) and fake_ip_filter
        else list(DEFAULT_FAKE_IP_FILTER)
    )
    # Discovered link suffixes are intranet names: never hand them a fake IP.
    for suffix in suffixes:
        normalized = _normalize_suffix(suffix)
        key = f"+.{normalized}" if normalized else ""
        if key and key not in entries:
            entries.append(key)
    block["fake-ip-filter"] = entries


def render_dns_block(
    formal_dns: Mapping[str, Any] | None,
    decision: RungDecision,
    qualification: ResolverQualification,
) -> dict[str, Any]:
    """Render the ``dns`` block for one rung.

    The block keeps the formal listen address, enhanced mode and fake-ip
    keys so the same rung is valid for the no-capture bootstrap Core and the
    formal Core alike: cold start is ordinary expansion with an empty pool.
    """

    rung = decision.rung
    if rung == RUNG_USER_CUSTOM:
        custom = copy.deepcopy(dict(formal_dns)) if isinstance(formal_dns, Mapping) else {}
        custom["enable"] = True
        return custom

    block = _base_keys(formal_dns)
    if qualification.ipv6_only:
        block["ipv6"] = True
    block["default-nameserver"] = ["system"]

    link_addresses = [item.address for item in qualification.answering_link_resolvers()]
    public_addresses = [item.address for item in qualification.public_link_resolvers()]
    if rung == RUNG_ENCRYPTED:
        selected = select_encrypted_endpoints(qualification)
        if selected:
            servers = [item.candidate.url for item in selected]
        else:
            # No verdict at all: let the Core race the whole neutral pool over
            # the one address family the link actually has.
            servers = list(
                full_encrypted_pool(
                    include_ipv4=not qualification.ipv6_only,
                    include_ipv6=qualification.ipv6_only,
                )
            )
        block["nameserver"] = list(servers)
        block["proxy-server-nameserver"] = list(servers)
        # DIRECT-routed public names stay on the qualified encrypted resolver
        # too.  Answering the two control names does not prove a link resolver
        # is unfiltered: one that sinkholes other names (127.0.0.1 answers for
        # public hosts were observed) would poison every DIRECT dial with no
        # fallback, because ``direct-nameserver`` replaces rather than
        # supplements ``nameserver``.  Intranet suffixes are still routed to the
        # link through ``nameserver-policy`` below.
    elif rung == RUNG_SYSTEM:
        # Under an outer fake-IP tunnel the adapter set (including the tunnel's
        # own resolver) is the working path; explicit link addresses would only
        # be hijacked anyway, so hand the whole set to Mihomo's system client.
        servers = ["system"] if qualification.outer_fake_ip else (link_addresses or ["system"])
        block["nameserver"] = list(servers)
        block["proxy-server-nameserver"] = list(servers)
    else:
        block["nameserver"] = ["rcode://name_error"]
        block["proxy-server-nameserver"] = ["rcode://name_error"]

    _fake_ip_keys(formal_dns, block, qualification.dns_suffixes if link_addresses else ())
    if link_addresses:
        policy_keys = suffix_policy_keys(qualification.dns_suffixes)
        block["nameserver-policy"] = {key: list(link_addresses) for key in policy_keys}
    return block


def rendered_dns_uses_hostnames(block: Mapping[str, Any]) -> bool:
    """True when any resolver entry would itself need resolving (test guard)."""

    from urllib.parse import urlsplit

    entries: list[str] = []
    for key in ("nameserver", "default-nameserver", "proxy-server-nameserver", "direct-nameserver"):
        entries.extend(_string_list(block.get(key)))
    policy = block.get("nameserver-policy")
    if isinstance(policy, Mapping):
        for value in policy.values():
            entries.extend(_string_list(value))
    for entry in entries:
        if entry in {"system"} or entry.startswith("rcode://"):
            continue
        host = entry
        if "://" in entry:
            host = urlsplit(entry).hostname or ""
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return True
    return False


def apply_dns_block_to_payload(payload: Mapping[str, Any], dns_block: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a copy of ``payload`` whose ``dns`` block is replaced.

    A payload without a ``dns`` block (DNS disabled by policy) is returned
    unchanged: the resolver plan never invents a DNS listener.
    """

    result = copy.deepcopy(dict(payload))
    if dns_block is None or not isinstance(result.get("dns"), Mapping):
        return result
    result["dns"] = copy.deepcopy(dict(dns_block))
    return result


def display_summary(rung: str, qualification: ResolverQualification | None) -> str:
    """Short operator label such as ``encrypted (Cloudflare, Quad9)``."""

    if rung == RUNG_ENCRYPTED and qualification is not None:
        names = []
        for item in select_encrypted_endpoints(qualification):
            if item.candidate.display_name not in names:
                names.append(item.candidate.display_name)
        return f"{rung} ({', '.join(names)})" if names else rung
    if rung == RUNG_SYSTEM and qualification is not None:
        addresses = [item.address for item in qualification.answering_link_resolvers()][:2]
        return f"{rung} ({', '.join(addresses)})" if addresses else rung
    return rung


__all__ = [
    "BOOTSTRAP_LADDER_AUTO",
    "BOOTSTRAP_LADDER_ENCRYPTED_ONLY",
    "BOOTSTRAP_LADDER_SYSTEM",
    "BOOTSTRAP_LADDER_USER_CUSTOM",
    "CANDIDATE_ANSWER_MISMATCH",
    "CANDIDATE_CONNECT_FAILED",
    "CANDIDATE_NOT_PROBED",
    "CANDIDATE_QUALIFIED",
    "CANDIDATE_TIMEOUT",
    "CANDIDATE_TLS_FAILED",
    "DEFAULT_FAKE_IP_FILTER",
    "DNS_MODES",
    "DNS_MODE_AUTO",
    "DNS_MODE_CUSTOM",
    "DNS_MODE_ENCRYPTED_ONLY",
    "DNS_MODE_SYSTEM",
    "DnsResolverPlan",
    "EncryptedCandidate",
    "EncryptedCandidateResult",
    "LEGACY_PROXY_SERVER_NAMESERVERS",
    "LEGACY_SHIPPED_DOH_NAMESERVERS",
    "LEGACY_SHIPPED_PLAIN_NAMESERVERS",
    "LINK_ANSWERS_INTERNAL_ONLY",
    "LINK_ANSWERS_PUBLIC",
    "LINK_CAPTIVE_SUSPECTED",
    "LINK_NOT_PROBED",
    "LINK_OUTER_FAKE_IP",
    "LINK_RESOLVER_CONTROL_ANSWERS",
    "LINK_RESOLVER_CONTROL_NAMES",
    "LINK_SILENT",
    "LinkResolverResult",
    "OUTER_FAKE_IP_NETWORK",
    "RESOLVER_CATALOG",
    "RUNG_ENCRYPTED",
    "RUNG_INTRANET_ONLY",
    "RUNG_SYSTEM",
    "RUNG_USER_CUSTOM",
    "ResolverProvider",
    "ResolverQualification",
    "RungDecision",
    "TRANSPORT_DOH",
    "TRANSPORT_DOT",
    "active_dns_block",
    "apply_dns_block_to_payload",
    "bootstrap_ladder",
    "choose_rung",
    "display_summary",
    "dns_config_list",
    "dns_lists_are_legacy_defaults",
    "encrypted_candidates",
    "encrypted_endpoint_url",
    "formal_dns_is_shipped_default",
    "full_encrypted_pool",
    "helper_dns_block",
    "ladder_for_mode",
    "next_rung",
    "normalize_dns_mode",
    "publish_active_dns_block",
    "render_dns_block",
    "rendered_dns_uses_hostnames",
    "select_encrypted_endpoints",
    "static_dns_lists",
    "suffix_policy_keys",
]
