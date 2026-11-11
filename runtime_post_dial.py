"""Pure post-dial observer core (node-judgement design v3 §3, §4, §6, §18.2-18.5, §18.8).

The integration layer owns every side effect: it drains the stdout tap, runs
``GET /connections`` on its own thread and feeds the results here together with
explicit clock readings (Mihomo/wall time for comparisons with ``time=`` and
``start``, ``perf_counter`` for intervals).  Nothing in this module performs
I/O, starts a thread or arms a timer.

Privacy: destinations are HMAC-tokenised while the open line is parsed and the
raw host is dropped immediately.  Records, ``repr`` output and log dictionaries
produced here carry tokens and hashed leaves only.

Cost (V4): ``ingest_raw`` takes the raw ``/connections`` body and keeps the
exact semantics of ``ingest_snapshot(json.loads(body))`` while it parses only
the entries whose bytes changed.  Entries whose bytes are unchanged and that
no time rule can act on at this poll are observed lazily: their per-poll
bookkeeping (sample count, last-seen time, the 2 s rate window) is replayed
from the poll history when something reads it.  To compare, ``ingest_raw``
keeps each listed entry's bytes (host names included) in memory until the
entry is gone; they never reach a record, a ``repr`` or a log.

CPU budget (V4 follow-up): ``PostDialCpuThrottle`` turns the observer
thread's measured CPU share into a poll-period stretch that the core applies
with ``set_poll_scale``; classification never changes.
"""

from __future__ import annotations

import bisect
import hashlib
import hmac
import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence


# ---------------------------------------------------------------------------
# Constants (§18.2)


@dataclass(frozen=True, slots=True)
class PostDialConstants:
    """Every §18.2 constant; ``NodeJudgementPolicy`` may override some."""

    poll_fast_s: float = 0.100
    # FAST_WINDOW_S = T_FAST_S + 0.2, or T_SHORT_S + 0.2 while SLOW is evaluated.
    fast_window_margin_s: float = 0.2
    evaluate_slow_candidate: bool = True
    # AGED_POLL_OFFSET_S = T_AGE_S + 0.02
    aged_poll_margin_s: float = 0.02
    poll_slow_s: float = 1.0
    linger_s: float = 5.0
    stream_watch_s: float = 60.0
    stream_rate_bps: float = 65536.0
    stream_rate_window_s: float = 2.0
    poll_gap_max_s: float = 0.150
    cost_adapt_threshold_s: float = 0.030
    cost_adapt_factor: float = 3.0
    reg_slack_s: float = 0.020
    join_window_s: float = 0.5
    open_ttl_s: float = 30.0
    x_bytes: int = 0
    t_fast_s: float = 1.0
    t_short_s: float = 2.0
    t_age_s: float = 3.0
    t_wait_s: float = 2.0
    t_wait_trigger_s: float = 5.0
    k_fast: int = 2
    w_fast_s: float = 1.0
    k_aged: int = 2
    w_aged_s: float = 5.0
    k_spread: int = 4
    w_spread_s: float = 10.0
    k_slow: int = 3
    k_slow_tokens: int = 3
    w_slow_s: float = 1.0
    k_waiting: int = 2
    w_waiting_s: float = 5.0
    # Act-trigger guard ("most of this node's connections are failing"): an
    # act rule opens a judgement only while, among leaf L's evidence
    # connections classified within TRIGGER_SHARE_WINDOW_S, failed / (failed
    # + answered) >= TRIGGER_MIN_FAILED_SHARE (failed = FAILED_FAST + AGED,
    # answered = first download; an AGED connection answered later counts
    # once, as answered).  0, the default (user decision 10-01), turns the
    # guard off: the rules alone.  The guard's inputs and the variants of
    # ``SHARE_VARIANTS`` are logged at every setting.
    trigger_min_failed_share: float = 0.0
    trigger_share_window_s: float = 10.0
    native_timeout_ms: int = 5000
    probe_request_timeout_s: float = 5.65
    s_c0_delay_s: float = 2.0
    min_probe_remaining_s: float = 1.0
    succ_cap_max: int = 3
    ticket_s: float = 6.0
    native_tail_s: float = 0.5
    ctrl_timeout_s: float = 0.35
    leaf_backoff_s: float = 10.0
    # Leaf back-off after a NOT_REPRODUCED verdict (ship1 VJ-3, 10-28): the
    # trigger was site-side, so the same leaf is not judged again for this
    # long.  INDETERMINATE keeps LEAF_BACKOFF_S.
    not_reproduced_backoff_s: float = 420.0
    # Fix 3 (10-28): inside that back-off a FAST trigger on the same leaf
    # still opens one judgement per window when the failed-share guard's
    # inputs show the leaf clearly failing: failed / (failed + answered)
    # >= this share with failed + answered >= this count.
    not_reproduced_override_min_share: float = 0.8
    not_reproduced_override_min_n: int = 4
    # Fixes 1 and 2 (10-28; keys exhausted_pool_reexport / exhausted_hold): the TIER3-AUTO pool
    # re-export when the loaded group is exhausted, or keeps <= this many
    # alive members while the healthy pool exceeds the loaded members by at
    # least the surplus; at most once per interval.
    pool_reexport_interval_s: float = 120.0
    pool_reexport_max_alive: int = 1
    pool_reexport_min_surplus: int = 4
    # 10-28 seamless keys (each default False; judge act only):
    # post_dial_provisional_move - when A_C has no result this long after it
    # was issued and a successor is already proven (S_C0 PASS, or a fresh
    # provider history with post_dial_successor_from_history), the route is
    # moved provisionally to it through the route-lease write.
    provisional_move_s: float = 2.5
    # post_dial_successor_from_history - the newest provider history sample
    # of the successor must be alive and at most this old.
    successor_history_max_age_s: float = 60.0
    # post_dial_fast_successor - S_C0_DELAY_S while that key is on.
    fast_successor_s_c0_delay_s: float = 1.0
    # post_dial_close_frozen_streams - rule 6: a stream that had downlink at
    # >= STREAM_RATE_BPS peak and has received nothing for this long.
    stall_close_s: float = 2.0
    exonerate_s: float = 600.0
    leaf_check_cap: int = 3
    leaf_check_window_s: float = 600.0
    global_max: int = 10
    global_window_s: float = 600.0
    epoch_quiet_s: float = 15.0
    wake_gap_s: float = 30.0
    post_demotion_watch_s: float = 60.0
    progress_window_s: float = 2.0
    max_close: int = 64
    max_body_bytes: int = 1_500_000
    tap_maxlen: int = 512
    # CPU budget of the observer thread (V4 follow-up, ``PostDialCpuThrottle``):
    # percent of one core over CPU_WINDOW_S (0: no throttle), the time a
    # stretch is kept before it is judged again, how long the worst window
    # is remembered before a stretch is released, and the largest stretch.
    cpu_budget_pct: float = 2.0
    cpu_window_s: float = 10.0
    cpu_settle_s: float = 5.0
    cpu_hold_s: float = 60.0
    cpu_scale_max: float = 10.0
    # A stretched poll gap exceeds POLL_GAP_MAX_S by at least this much, so
    # the wall-clock reading of the gap (Windows ``time.time`` ticks up to
    # 15.6 ms) still leaves the endings it spans unbracketed.
    stretch_gap_margin_s: float = 0.020

    @property
    def fast_window_s(self) -> float:
        base = self.t_short_s if self.evaluate_slow_candidate else self.t_fast_s
        return base + self.fast_window_margin_s

    @property
    def aged_poll_offset_s(self) -> float:
        return self.t_age_s + self.aged_poll_margin_s

    @property
    def min_poll_scale(self) -> float:
        """The smallest engaged stretch: POLL_FAST_S x it = POLL_GAP_MAX_S +
        STRETCH_GAP_MARGIN_S (1.7 by default)."""

        if self.poll_fast_s <= 0.0:
            return 1.0
        return max(1.0, (self.poll_gap_max_s + self.stretch_gap_margin_s) / self.poll_fast_s)


DEFAULT_POST_DIAL_CONSTANTS = PostDialConstants()

RULE_FAST = "FAST"
RULE_AGED = "AGED"
RULE_FAST_SPREAD = "FAST_SPREAD"
RULE_SLOW = "SLOW"
RULE_WAITING = "WAITING"
RULE_AGED_SINGLE = "AGED_SINGLE"
# Priority order: the first matching act rule opens the judgement (§18.5).
TRIGGER_RULES = (
    RULE_FAST,
    RULE_AGED,
    RULE_FAST_SPREAD,
    RULE_SLOW,
    RULE_WAITING,
    RULE_AGED_SINGLE,
)
DEFAULT_ACT_RULES = frozenset({RULE_FAST, RULE_AGED})

# Trigger variants an act-rule match is also scored on, for logs only, so
# that one gate run compares them on the same data (``Trigger.opens``):
# VARIANT_RULES is the rules alone (TRIGGER_MIN_FAILED_SHARE 0); VARIANT_SHARE
# is the failed-share guard at SHARE_VARIANT_MIN (failed / (failed +
# answered)); VARIANT_ONSET counts in the denominator only the answers opened
# at or after the earliest counted failure (failed / (failed + onset_answered)).
VARIANT_RULES = "rules"
VARIANT_SHARE = "share_0_5"
VARIANT_ONSET = "onset_0_5"
SHARE_VARIANT_MIN = 0.5
SHARE_VARIANTS = (VARIANT_SHARE, VARIANT_ONSET)


# ---------------------------------------------------------------------------
# Tokens and hashes


class PostDialTokenizer:
    """Keyed tokens for destinations and leaves; the key never appears in repr."""

    __slots__ = ("_key",)

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, (bytes, bytearray)) or len(key) < 16:
            raise ValueError("post-dial HMAC key must be at least 16 bytes")
        self._key = bytes(key)

    def __repr__(self) -> str:
        return "PostDialTokenizer(<key>)"

    def destination(self, host: str, port: int | str) -> str:
        # §18.3: hmac_sha256(K_process, lower(host) + ":" + port)[:32]
        # (one-shot hmac.digest: the same bytes as hmac.new(...).hexdigest())
        message = f"{str(host).lower()}:{port}".encode("utf-8", "surrogatepass")
        return hmac.digest(self._key, message, "sha256").hex()[:32]

    def leaf(self, name: str | None) -> str | None:
        if name is None:
            return None
        message = b"leaf\x00" + str(name).encode("utf-8", "surrogatepass")
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()[:12]


def short_token(token: str | None) -> str | None:
    """The 12-character token prefix used in log records."""

    return token[:12] if token else None


# ---------------------------------------------------------------------------
# Origin rules (§3.2, §18.3; Conf-3/4/5)


class OpenOrigin(str, Enum):
    USER = "user"
    APP = "app"
    IGNORED = "ignored"
    INNER = "inner"
    LEGACY = "legacy"


# Origins whose connections keep the 1 Hz polls alive and can be closed (§6).
_LIVE_ORIGINS = frozenset({OpenOrigin.USER, OpenOrigin.LEGACY})


_MIHOMO_RULE_LOG_NAMES = {
    "DOMAIN": "Domain",
    "DOMAIN-SUFFIX": "DomainSuffix",
    "DOMAIN-KEYWORD": "DomainKeyword",
    "DOMAIN-REGEX": "DomainRegex",
    "IP-CIDR": "IPCIDR",
    "IP-CIDR6": "IPCIDR",
    "SRC-IP-CIDR": "SrcIPCIDR",
    "DST-PORT": "DstPort",
    "SRC-PORT": "SrcPort",
    "PROCESS-NAME": "Process",
    "PROCESS-PATH": "ProcessPath",
}


def system_rule_log_form(rule: str) -> str | None:
    """Render a Clash rule as Mihomo prints it after ``match`` (Conf-3).

    ``DOMAIN,h,X`` -> ``Domain(h)``; ``IP-CIDR,a/n,X,no-resolve`` and
    ``IP-CIDR6,a/n,X`` -> ``IPCIDR(a/n)`` (no ``,no-resolve``; IPv6 as IPCIDR).
    """

    parts = [part.strip() for part in str(rule or "").split(",")]
    if len(parts) < 2 or not parts[1]:
        return None
    name = _MIHOMO_RULE_LOG_NAMES.get(parts[0].upper())
    if name is None:
        return None
    return f"{name}({parts[1]})"


def system_rule_hosts(rules: Iterable[str]) -> frozenset[str]:
    """Hosts routed by system rules (DOMAIN hosts and single-address CIDRs)."""

    hosts: set[str] = set()
    for rule in rules:
        parts = [part.strip() for part in str(rule or "").split(",")]
        if len(parts) < 2 or not parts[1]:
            continue
        kind = parts[0].upper()
        if kind == "DOMAIN":
            hosts.add(parts[1].lower())
        elif kind in {"IP-CIDR", "IP-CIDR6"}:
            address, _, prefix = parts[1].partition("/")
            if (kind == "IP-CIDR" and prefix == "32") or (kind == "IP-CIDR6" and prefix == "128"):
                hosts.add(address.lower())
    return frozenset(hosts)


@dataclass(frozen=True, slots=True)
class OriginRules:
    """Injected origin context.  Holds raw system hosts, so repr shows counts only."""

    system_rule_log_forms: frozenset[str] = frozenset()
    system_hosts: frozenset[str] = frozenset()
    user_inbound_names: frozenset[str] = frozenset({"DEFAULT-MIXED"})

    @classmethod
    def from_system_rules(
        cls,
        rules: Iterable[str],
        *,
        user_inbound_names: Iterable[str] = ("DEFAULT-MIXED",),
    ) -> "OriginRules":
        values = [str(rule) for rule in rules]
        forms = frozenset(
            form for form in (system_rule_log_form(rule) for rule in values) if form
        )
        return cls(
            system_rule_log_forms=forms,
            system_hosts=system_rule_hosts(values),
            user_inbound_names=frozenset(str(name) for name in user_inbound_names),
        )

    def __repr__(self) -> str:
        return (
            "OriginRules("
            f"system_rule_log_forms=<{len(self.system_rule_log_forms)}>, "
            f"system_hosts=<{len(self.system_hosts)}>, "
            f"user_inbound_names=<{len(self.user_inbound_names)}>)"
        )


# ---------------------------------------------------------------------------
# Open-line parsing (§18.3)


@dataclass(frozen=True, slots=True)
class PostDialOpen:
    """One ``[TCP] ... using OUTER[leaf]`` open record; no raw host is kept."""

    t_open: float
    received_at_monotonic: float
    source_ip: str = field(repr=False)
    source_port: int = field(repr=False)
    token: str
    port: int
    leaf: str | None = field(repr=False)
    outer: str = field(repr=False)
    origin: OpenOrigin

    @property
    def key(self) -> tuple[str, int]:
        return (self.source_ip, self.source_port)


# Conf-2 regex (698 of 698 lines).
OPEN_LINE_RE = re.compile(
    r"^\[(?P<net>TCP|UDP)\] "
    r"(?P<src>mihomo|\[[0-9A-Fa-f:.%]+\]:\d+|[0-9.]+:\d+)"
    r"(?:\((?P<proc>.*?)\))? --> (?P<dst>\S+) "
    r"(?:match (?P<rule>.+?) )?using (?P<using>.+)$"
)
_RFC3339_RE = re.compile(
    r"^(\d{4})-(\d\d)-(\d\d)[Tt ](\d\d):(\d\d):(\d\d)(?:\.(\d+))?(Z|z|[+-]\d\d:\d\d)$"
)
_GO_SIMPLE_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    '"': '"',
    "'": "'",
}
_HEX = frozenset("0123456789abcdefABCDEF")


def _days_from_civil(year: int, month: int, day: int) -> int:
    year -= month <= 2
    era = (year if year >= 0 else year - 399) // 400
    yoe = year - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def parse_rfc3339(value: Any) -> float | None:
    """Epoch seconds for Mihomo's RFC3339Nano (``time=`` and ``start``)."""

    match = _RFC3339_RE.match(str(value or "").strip())
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60 and second < 61):
        return None
    fraction = match.group(7)
    zone = match.group(8)
    offset = 0
    if zone not in {"Z", "z"}:
        sign = -1 if zone[0] == "-" else 1
        offset = sign * (int(zone[1:3]) * 3600 + int(zone[4:6]) * 60)
    epoch = (
        _days_from_civil(year, month, day) * 86400
        + hour * 3600
        + minute * 60
        + second
        - offset
    )
    if fraction:
        return epoch + int(fraction) / (10 ** len(fraction))
    return float(epoch)


def go_unquote(body: str) -> str | None:
    """Undo Go ``%q``/logrus quoting of a value body (quotes already removed)."""

    if "\\" not in body:
        return body
    out = bytearray()
    index = 0
    length = len(body)
    while index < length:
        char = body[index]
        if char != "\\":
            out += char.encode("utf-8", "surrogatepass")
            index += 1
            continue
        if index + 1 >= length:
            return None
        escape = body[index + 1]
        simple = _GO_SIMPLE_ESCAPES.get(escape)
        if simple is not None:
            out += simple.encode("utf-8")
            index += 2
            continue
        if escape in {"x", "u", "U"}:
            width = {"x": 2, "u": 4, "U": 8}[escape]
            digits = body[index + 2 : index + 2 + width]
            if len(digits) != width or any(item not in _HEX for item in digits):
                return None
            number = int(digits, 16)
            if escape == "x":
                out.append(number)
            else:
                try:
                    out += chr(number).encode("utf-8", "surrogatepass")
                except (ValueError, OverflowError):
                    return None
            index += 2 + width
            continue
        if escape in "01234567":
            digits = body[index + 1 : index + 4]
            if len(digits) != 3 or any(item not in "01234567" for item in digits):
                return None
            number = int(digits, 8)
            if number > 255:
                return None
            out.append(number)
            index += 4
            continue
        return None
    return out.decode("utf-8", "replace")


def _logrus_fields(line: str) -> dict[str, str] | None:
    """Split ``key=value key="quoted value"`` pairs; quoted values are Go-unquoted."""

    fields: dict[str, str] = {}
    index = 0
    length = len(line)
    while index < length:
        while index < length and line[index] == " ":
            index += 1
        if index >= length:
            break
        equals = line.find("=", index)
        if equals < 0:
            return None
        key = line[index:equals]
        if not key or " " in key:
            return None
        index = equals + 1
        if index < length and line[index] == '"':
            # The closing quote is the first one preceded by an even run of
            # backslashes (a backslash escapes the next character).
            cursor = line.find('"', index + 1)
            while cursor >= 0:
                back = cursor - 1
                while back > index and line[back] == "\\":
                    back -= 1
                if (cursor - 1 - back) % 2 == 0:
                    break
                cursor = line.find('"', cursor + 1)
            if cursor < 0:
                return None
            value = go_unquote(line[index + 1 : cursor])
            if value is None:
                return None
            fields[key] = value
            index = cursor + 1
        else:
            space = line.find(" ", index)
            end = length if space < 0 else space
            fields[key] = line[index:end]
            index = end
    return fields


def split_host_port(value: str) -> tuple[str, int] | None:
    text = str(value or "")
    if text.startswith("["):
        closing = text.find("]")
        if closing < 1 or text[closing + 1 : closing + 2] != ":":
            return None
        host, port_text = text[1:closing], text[closing + 2 :]
    else:
        host, sep, port_text = text.rpartition(":")
        if not sep:
            return None
    if not host or not port_text.isdigit():
        return None
    port = int(port_text)
    if port > 65535:
        return None
    return host, port


def split_using(using: str) -> tuple[str, str | None]:
    """Outer group and leaf: the leaf lies between the first ``[`` and the last ``]``."""

    if using.endswith("]") and "[" in using:
        start = using.index("[")
        return using[:start], using[start + 1 : using.rindex("]")]
    return using, None


def classify_line_origin(
    *,
    rule: str | None,
    outer: str,
    leaf: str | None,
    host: str,
    rules: OriginRules,
) -> OpenOrigin:
    if rule is None and outer != "GLOBAL":
        return OpenOrigin.APP
    if rule is not None and rule in rules.system_rule_log_forms:
        return OpenOrigin.APP
    if outer == "GLOBAL" and host.lower() in rules.system_hosts:
        return OpenOrigin.APP
    if outer in {"DIRECT", "REJECT"} or leaf == "REJECT":
        return OpenOrigin.IGNORED
    return OpenOrigin.USER


def parse_open_message(
    message: str,
    *,
    t_open: float,
    received_at_monotonic: float,
    tokenizer: PostDialTokenizer,
    origin_rules: OriginRules,
) -> PostDialOpen | None:
    match = OPEN_LINE_RE.match(message)
    if match is None or match.group("net") != "TCP":
        return None
    source = match.group("src")
    if source == "mihomo":
        return None
    source_parts = split_host_port(source)
    destination_parts = split_host_port(match.group("dst"))
    if source_parts is None or destination_parts is None:
        return None
    host, port = destination_parts
    outer, leaf = split_using(match.group("using"))
    origin = classify_line_origin(
        rule=match.group("rule"),
        outer=outer,
        leaf=leaf,
        host=host,
        rules=origin_rules,
    )
    return PostDialOpen(
        t_open=t_open,
        received_at_monotonic=float(received_at_monotonic),
        source_ip=source_parts[0],
        source_port=source_parts[1],
        # An ignored open (DIRECT, REJECT) is never tracked: no token needed.
        token="" if origin is OpenOrigin.IGNORED else tokenizer.destination(host, port),
        port=port,
        leaf=leaf,
        outer=outer,
        origin=origin,
    )


def parse_open_line(
    raw_line: str,
    received_at_monotonic: float,
    *,
    tokenizer: PostDialTokenizer,
    origin_rules: OriginRules,
) -> PostDialOpen | None:
    """Parse one Mihomo logrus stdout line into an open record (§18.3).

    Returns None for anything that is not an INFO ``[TCP]`` open with a valid
    ``time=``, including UDP and Mihomo-internal (``mihomo``) sources.
    """

    line = str(raw_line or "").strip()
    if "[TCP] " not in line or " using " not in line:
        return None
    fields = _logrus_fields(line)
    if fields is None or fields.get("level") != "info":
        return None
    t_open = parse_rfc3339(fields.get("time"))
    message = fields.get("msg")
    if t_open is None or not message:
        return None
    return parse_open_message(
        message,
        t_open=t_open,
        received_at_monotonic=received_at_monotonic,
        tokenizer=tokenizer,
        origin_rules=origin_rules,
    )


# ---------------------------------------------------------------------------
# Observer results


class ConnectionClass(str, Enum):
    ANSWERED = "ANSWERED"
    FAILED_FAST = "FAILED_FAST"
    AGED = "AGED"
    WAITING = "WAITING"
    UNANSWERED_SHORT = "UNANSWERED_SHORT"
    UNKNOWN = "UNKNOWN"
    ANSWERED_CLOSED = "ANSWERED_CLOSED"
    UNANSWERED_CLOSED = "UNANSWERED_CLOSED"


_EVIDENCE_CLASSES = frozenset(
    {
        ConnectionClass.FAILED_FAST,
        ConnectionClass.AGED,
        ConnectionClass.UNANSWERED_SHORT,
        ConnectionClass.WAITING,
    }
)


@dataclass(frozen=True, slots=True)
class Trigger:
    """An act-rule match on one leaf.  Opens a judgement; never convicts."""

    kind: str
    leaf: str = field(repr=False)
    tokens: tuple[str, ...]
    t: float
    n_events: int
    first_event_t: float
    episode: int
    new_episode: bool
    ratio_failed: int | None = None
    ratio_answered: int | None = None
    # The failed-share guard's inputs at this evaluation (None: not computed);
    # ``onset_answered`` is the part of ``share_answered`` opened at or after
    # the earliest open among the counted failures (logged, never acted on).
    # ``share_ok`` is the configured guard (True when it is off); the two
    # ``*_ok_at_0_5`` fields are VARIANT_SHARE / VARIANT_ONSET at this
    # evaluation, whatever the setting.
    share_failed: int | None = None
    share_answered: int | None = None
    share_ok: bool | None = None
    onset_answered: int | None = None
    share_ok_at_0_5: bool | None = None
    onset_ok_at_0_5: bool | None = None
    # The variants (VARIANT_RULES / VARIANT_SHARE / VARIANT_ONSET) whose
    # trigger for this (leaf, rule) episode this match is: the first match
    # of the episode each would have acted on.  Act rules only.
    opens: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ShadowTrigger:
    """A shadow-candidate match, an act-rule match the failed-share guard
    held back (``share_ok`` False), or a variant-only record; logged as
    ``would_trigger``.  The first two come once per episode.  A variant-only
    record (``variant_only``) is an act-rule match that opens a variant
    (``opens``, never VARIANT_RULES) at a match nothing else logs: e.g. with
    the guard off, the later match of an episode at which the 0.5 guard
    would first pass.  Dropping those records leaves the stream as it was."""

    rule: str
    leaf: str = field(repr=False)
    tokens: tuple[str, ...]
    t: float
    n_events: int
    first_event_t: float
    episode: int
    ratio_failed: int | None = None
    ratio_answered: int | None = None
    share_failed: int | None = None
    share_answered: int | None = None
    share_ok: bool | None = None
    onset_answered: int | None = None
    share_ok_at_0_5: bool | None = None
    onset_ok_at_0_5: bool | None = None
    opens: tuple[str, ...] = ()
    variant_only: bool = False


@dataclass(frozen=True, slots=True)
class TriggerEvaluation:
    triggers: tuple[Trigger, ...] = ()
    shadow: tuple[ShadowTrigger, ...] = ()


@dataclass(frozen=True, slots=True)
class SnapshotDigest:
    """Result of one poll attempt.

    ``new_evidence``, ``answered_tier3_user`` and ``wake_gap`` cover everything
    since the previous accepted digest, including late-bind merges between
    polls; a rejected attempt (``accepted`` False) leaves the first two for the
    next accepted one.
    """

    accepted: bool
    t_req_wall: float
    gap_ok: bool
    cost_s: float | None
    tcp_connections: int
    new_evidence: int
    answered_tier3_user: bool
    wake_gap: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class StuckConnections:
    """§6 rules 1, 2, 4, 5 for one leaf; rules 3 and 6 belong to the judge."""

    ids: tuple[str, ...]
    aged: int
    waiting: int
    kept_progressing: int
    kept_not_stuck: int
    kept_after_demotion: int
    kept_other: int
    snapshot_wall: float | None
    # Rule 6 (key post_dial_close_frozen_streams): frozen streams, listed
    # after the AGED and WAITING ids.  Always 0 while the key is off.
    frozen: int = 0


@dataclass(frozen=True, slots=True)
class DemotionWatch:
    leaf: str = field(repr=False)
    t_demote_wall: float
    until_perf: float


# ---------------------------------------------------------------------------
# Internal mutable state


class _Conn:
    __slots__ = (
        "oid",
        "t_open",
        "key",
        "token",
        "port",
        "leaf",
        "origin",
        "legacy",
        "conn_id",
        "gateway",
        "answered",
        "up",
        "down",
        "up_at_down",
        "n_samples",
        "last_seen",
        "t_last_down_inc",
        "t_up_after_down",
        "rates",
        "peak_rate",
        "t_stream_rate",
        "t_last_change",
        "aged",
        "waiting_logged_at",
        "waiting_trigger_at",
        "closed",
        "gone_t",
        "stale",
        "t_ingest",
        # Raw-ingest bookkeeping (``ingest_raw``): the last observed chain
        # names, the poll sequence number the state is exact at, whether the
        # next poll must observe the entry even if its bytes are unchanged,
        # the entry's memo key while it is listed, the up-after-down time
        # whose WAITING thresholds have all been evaluated, and whether a
        # download counter ever went down (``_needs_tick``'s rate bound).
        "chains",
        "tick_seq",
        "hot",
        "memo_key",
        "waiting_eval_at",
        "down_fell",
    )

    def __init__(
        self,
        oid: int,
        t_open: float,
        key: tuple[str, int],
        token: str | None,
        port: int,
        leaf: str | None,
        origin: OpenOrigin,
        legacy: bool,
    ) -> None:
        self.oid = oid
        self.t_open = t_open
        self.key = key
        self.token = token
        self.port = port
        self.leaf = leaf
        self.origin = origin
        self.legacy = legacy
        self.conn_id: str | None = None
        self.gateway = False
        self.answered = False
        self.up = 0
        self.down = 0
        self.up_at_down = 0
        self.n_samples = 0
        self.last_seen: float | None = None
        self.t_last_down_inc: float | None = None
        self.t_up_after_down: float | None = None
        self.rates: deque[tuple[float, int]] = deque()
        if not legacy:
            # A user open starts at zero bytes at its line time.
            self.rates.append((t_open, 0))
        self.peak_rate = 0.0
        self.t_stream_rate: float | None = None
        self.t_last_change: float | None = None
        self.aged = False
        self.waiting_logged_at: float | None = None
        self.waiting_trigger_at: float | None = None
        self.closed = False
        self.gone_t: float | None = None
        self.stale = False
        # Wall time the open line was drained (None for snapshot-only entries).
        self.t_ingest: float | None = None
        self.chains: tuple[str, ...] | None = None
        self.tick_seq = 0
        self.hot = False
        self.memo_key: bytes | None = None
        self.waiting_eval_at: float | None = None
        self.down_fell = False

    def __repr__(self) -> str:
        return f"_Conn(oid={self.oid}, origin={self.origin.value}, legacy={self.legacy})"


class _Event:
    __slots__ = ("t", "kind", "token", "oid", "t_open", "port")

    def __init__(
        self,
        t: float,
        kind: ConnectionClass,
        token: str | None,
        oid: int,
        t_open: float,
        port: int,
    ) -> None:
        self.t = t
        self.kind = kind
        self.token = token
        self.oid = oid
        self.t_open = t_open
        self.port = port


def _oid_of(conn: _Conn) -> int:
    return conn.oid


def _share_reaches(failed: int, answered: int, minimum: float) -> bool:
    """failed / (failed + answered) >= minimum, with at least one failure."""

    return failed > 0 and failed >= minimum * (failed + answered)


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):  # OverflowError: a non-finite JSON number
        return 0


def _chain_names(chains: Any) -> tuple[str, ...] | None:
    """``chains`` as ``_observe`` reads it: names of a non-empty list, else None."""

    if isinstance(chains, list) and chains:
        return tuple(str(name) for name in chains)
    return None


# ---------------------------------------------------------------------------
# Raw ``/connections`` bodies (``ingest_raw``)
#
# Mihomo (Go ``encoding/json``, chi ``render.JSON``) writes the snapshot
# compactly: ``{"downloadTotal":N,"uploadTotal":N,"connections":[{"id":"<uuid>",
# "metadata":{...},"upload":N,"download":N,"start":"...","chains":[...],...},
# ...],"memory":N}``.  An entry can never contain ``},{"id":"`` (a quote inside
# a JSON string is escaped), so that separator splits the array exactly.  Any
# other shape (non-JSON or inner whitespace, new fields around the array, an
# id with escapes or non-ASCII bytes, an entry whose parsed id differs from
# its leading one) is handed to ``json.loads`` and the classic path instead.

_RAW_CONNECTIONS_KEY = b'"connections":'
_RAW_ENTRY_SEP = b'},{"id":"'
_RAW_ENTRY_SEP_LEN = len(_RAW_ENTRY_SEP)
# The memo key is the exact id plus its closing quote (Mihomo: a 36-character
# UUID); an id longer than this bound is not keyed.
_RAW_KEY_MAX = 128
_RAW_UUID_KEY_LEN = 37  # a 36-character UUID and its closing quote
# JSON whitespace only (``json.loads``' own set; ``\s`` would also take \v, \f).
_RAW_HEAD_RE = re.compile(rb'[ \t\n\r]*\{(?:"(?!connections")[A-Za-z0-9_]*":-?(?:0|[1-9][0-9]*),)*')
_RAW_TAIL_RE = re.compile(rb'(?:,"(?!connections")[A-Za-z0-9_]*":-?(?:0|[1-9][0-9]*))*\}[ \t\n\r]*')
_RAW_UPLOAD = b'},"upload":'
_RAW_BAD = object()
_UNCHANGED = object()
# Work items of one raw poll, in body order.
_W_TICK = 0  # unchanged bytes, observed exactly this poll
_W_COUNTERS = 1  # only upload/download changed
_W_PARSED = 2  # a new entry, or one that changed elsewhere: parsed with json


class RawBodyError(ValueError):
    """A ``GET /connections`` body that is not a JSON object.

    Raised by ``decode_connections_body`` (and so by ``ingest_raw``) before
    the poll changes any state; the message never quotes the body.  The
    observer counts it as a failed poll, as the keep-alive client's
    ``connections()`` failure.
    """


def decode_connections_body(body: bytes | bytearray) -> dict[str, Any]:
    """The ``GET /connections`` body as the keep-alive client decodes it.

    An empty body is ``{}``.  Raises RawBodyError when it is not a JSON
    object; the message never quotes the body.
    """

    if not body:
        return {}
    try:
        payload = json.loads(bytes(body).decode("utf-8", errors="replace"))
    except (ValueError, RecursionError):
        raise RawBodyError("body is not JSON") from None
    if not isinstance(payload, dict):
        raise RawBodyError("body is not a JSON object")
    return payload


def _raw_layout(body: bytes) -> tuple[int, int] | None:
    """(first entry offset, offset of the array's closing ``}``), (-1, -1) for
    no entries, or None when the body is not in Mihomo's compact shape."""

    key = body.find(_RAW_CONNECTIONS_KEY)
    if key < 0 or _RAW_HEAD_RE.fullmatch(body, 0, key) is None:
        return None
    value = key + len(_RAW_CONNECTIONS_KEY)
    if body.startswith(b"null", value):
        first = close = -1
        end = value + 4
    elif body.startswith(b"[]", value):
        first = close = -1
        end = value + 2
    elif body.startswith(b'[{"id":"', value):
        close = body.rfind(b"}]")
        if close <= value:
            return None
        first = value + 8
        end = close + 2
    else:
        return None
    if _RAW_TAIL_RE.fullmatch(body, end) is None:
        return None
    return first, close


# ``},"upload":N,"download":N`` with JSON non-negative integer literals.
_RAW_COUNTERS_RE = re.compile(rb'\},"upload":(0|[1-9][0-9]*),"download":(0|[1-9][0-9]*)')


def _raw_frame(piece: bytes) -> tuple[bytes | None, bytes | None]:
    """The bytes around ``"upload":N,"download":N`` of one entry, or (None, None)."""

    upload = piece.find(_RAW_UPLOAD)
    match = None if upload < 0 else _RAW_COUNTERS_RE.match(piece, upload)
    if match is None or not piece.startswith(b',"', match.end()):
        return None, None
    return piece[:upload], piece[match.end() :]


def _raw_counters(piece: bytes, prefix: bytes | None, suffix: bytes | None) -> tuple[int, int] | None:
    """(upload, download) when only those two values differ from the memo."""

    if prefix is None or suffix is None:
        return None
    match = _RAW_COUNTERS_RE.match(piece, len(prefix))
    if (
        match is None
        or match.end() + len(suffix) != len(piece)
        or not piece.startswith(prefix)
        or not piece.endswith(suffix)
    ):
        return None
    up, down = match.groups()
    return int(up), int(down)


def _parse_raw_entry(piece: bytes) -> Any:
    """One entry parsed as ``json.loads`` parses it inside the whole body."""

    try:
        return json.loads('{"id":"' + piece.decode("utf-8", errors="replace") + "}")
    except (ValueError, RecursionError):
        return _RAW_BAD


# ---------------------------------------------------------------------------
# Observer core (§18.4, §18.5, §6)


class PostDialObserverCore:
    """Join open lines to ``/connections`` snapshots and classify user connections."""

    _MAX_TRACKED = 4096
    _POLL_HISTORY = 256
    _METRIC_HISTORY = 2048
    # ``ingest_raw`` falls back to ``json.loads`` for this many polls after a
    # body it cannot walk (duplicate or unkeyable ids, an entry that does not
    # split), and after warm polls that parsed more than ``_RAW_SHARE_MAX`` of
    # their entries (smoothed): a per-entry parse costs about 1.6x its share
    # of a whole-body ``json.loads``, so the fast path would then be slower.
    _RAW_BACKOFF_POLLS = 32
    _RAW_SHARE_MAX = 0.5
    _RAW_SHARE_ALPHA = 0.25
    _RAW_SHARE_MIN_ENTRIES = 16

    def __init__(
        self,
        *,
        tokenizer: PostDialTokenizer,
        constants: PostDialConstants = DEFAULT_POST_DIAL_CONSTANTS,
        act_rules: Iterable[str] = DEFAULT_ACT_RULES,
        tier3_members: Iterable[str] = (),
        gateway_groups: Iterable[str] = (),
        origin_rules: OriginRules | None = None,
    ) -> None:
        self._c = constants
        self._tokenizer = tokenizer
        self._origin_rules = origin_rules or OriginRules()
        self._act_rules = frozenset(str(rule).upper() for rule in act_rules)
        self._tier3: frozenset[str] = frozenset(tier3_members)
        self._gateways: frozenset[str] = frozenset(gateway_groups)
        self._oid = 0
        self._tracked: dict[int, _Conn] = {}
        # Line opens in ingest order, for O(1) age pruning and cap eviction.
        self._open_order: deque[tuple[float, int]] = deque()
        # User line opens that may still schedule polls (fast window / AGED
        # poll); app, ignored and legacy entries never enter.
        self._pending: dict[int, _Conn] = {}
        self._unbound: dict[tuple[str, int], list[_Conn]] = {}
        self._by_id: dict[str, _Conn] = {}
        self._present: dict[str, _Conn] = {}
        self._legacy_gone: dict[tuple[str, int], list[_Conn]] = {}
        self._events: dict[str, deque[_Event]] = {}
        # (answer time, open time, oid) of each first download, per leaf.
        self._answered: dict[str, deque[tuple[float, float, int]]] = {}
        self._answered_open_max: dict[str, float] = {}
        self._new_event_leaves: set[str] = set()
        self._last_match: dict[tuple[str, str], float] = {}
        self._episodes: dict[tuple[str, str], int] = {}
        # The episode of (leaf, act rule) whose matches have already passed
        # the failed-share guard: the first passing match of an episode is
        # the ``new_episode`` trigger (every match, when the guard is off).
        self._acted: dict[tuple[str, str], int] = {}
        # The same per SHARE_VARIANTS name, keyed (leaf, act rule, variant):
        # the episode each logged-only variant has already opened.
        self._variant_acted: dict[tuple[str, str, str], int] = {}
        self._watches: dict[str, DemotionWatch] = {}
        self._poll_walls: deque[float] = deque(maxlen=self._POLL_HISTORY)
        self._last_poll_wall: float | None = None
        self._last_poll_perf: float | None = None
        # A failed or rejected attempt moves the schedule, never gap_ok.
        self._last_failed_perf: float | None = None
        # The due promised after the last attempt; cleared when the schedule
        # goes dormant, so dormancy is never mistaken for a wake gap.
        self._due_after_poll_perf: float | None = None
        self._due_after_poll_wall: float | None = None
        self._offset: float | None = None  # wall - perf
        self._last_wall_seen: float | None = None
        self._fast_period = constants.poll_fast_s
        # CPU-budget stretch of every poll period (``set_poll_scale``).
        self._poll_scale = 1.0
        self._epoch_floor = float("-inf")
        self._wake_floor = float("-inf")
        self._wake_since_digest = False
        self._answered_since_digest = False
        self._evidence_since_digest = 0
        self._records: list[dict[str, Any]] = []
        self._metrics = self._fresh_metrics()
        self._gaps: deque[float] = deque(maxlen=self._METRIC_HISTORY)
        self._costs: deque[float] = deque(maxlen=self._METRIC_HISTORY)
        # Request + decode + ingest per accepted poll (``note_poll_cost``).
        self._total_costs: deque[float] = deque(maxlen=self._METRIC_HISTORY)
        # Reader lag (tap receive time - Mihomo time=) and drain lag (drain time - time=).
        self._lags: deque[float] = deque(maxlen=self._METRIC_HISTORY)
        self._drain_lags: deque[float] = deque(maxlen=self._METRIC_HISTORY)
        # -- raw ingest (``ingest_raw``) --------------------------------------
        # Accepted polls are numbered; ``_hist[i]`` is the wall time of poll
        # ``_hist_base + i``, kept back to the last poll before the 2 s rate
        # window (plus 1 s), which is all a lazy replay needs.
        self._seq = 0
        self._hist: list[float] = []
        self._hist_base = 1
        self._max_wall: float | None = None
        # After the wall clock stepped back, every listed entry is observed
        # exactly until the clock passes the old maximum plus the rate window.
        self._exact_until: float | None = None
        # Entry memo: key (the id and its closing quote) -> [entry bytes,
        # conn or None for a skipped entry, bytes before the counters,
        # bytes after them, length of the entry bytes].
        self._memo: dict[bytes, list[Any]] = {}
        # Memo keys listed by the last raw poll; None after a classic poll.
        self._present_keys: set[bytes] | None = set()
        # Tracked line opens not bound to an entry yet.
        self._unbound_open: dict[int, _Conn] = {}
        # Bound connections missing from the last snapshot whose open is
        # younger than REG_SLACK_S (re-checked by the next poll).
        self._absent_bound: dict[int, _Conn] = {}
        # Connections untracked by the cap while listed: ``_present`` keeps
        # them until the next poll, as a classic poll's rebuild would.
        self._purge_present: list[_Conn] = []
        self._force_next = False
        self._gateway_dirty = False
        # Polls left that go straight to ``json.loads`` (``_raw_back_off``),
        # and the smoothed share of listed entries a warm raw poll parsed.
        self._raw_backoff = 0
        self._raw_share = 0.0
        self._waiting_eval_s = max(constants.t_wait_s, constants.t_wait_trigger_s)
        self._rate_hot_bytes = constants.stream_rate_bps * constants.stream_rate_window_s
        # Key post_dial_close_frozen_streams (10-28, default False), set by
        # the runtime at each act trigger: stuck_connection_ids' rule 6.
        self.close_frozen_streams = False
        # How ``ingest_raw`` handled its polls and entries, since creation
        # (not part of ``summary``, which must not depend on the input form).
        self.raw_counts: dict[str, int] = {
            "fast_polls": 0,
            "json_polls": 0,
            "parsed": 0,
            "counters": 0,
            "ticks": 0,
            "backoffs": 0,
        }

    def __repr__(self) -> str:
        return (
            f"PostDialObserverCore(tracked={len(self._tracked)}, "
            f"tier3_members=<{len(self._tier3)}>, watches={len(self._watches)})"
        )

    # -- configuration ------------------------------------------------------

    @property
    def constants(self) -> PostDialConstants:
        return self._c

    @property
    def tokenizer(self) -> PostDialTokenizer:
        return self._tokenizer

    def set_route_context(
        self,
        *,
        tier3_members: Iterable[str],
        gateway_groups: Iterable[str] = (),
    ) -> None:
        """TIER3-AUTO members (``all`` minus Reject, prefix included) and Gateway groups."""

        gateways = frozenset(gateway_groups)
        if gateways != self._gateways:
            # Every observation recomputes the Gateway flag; a raw poll that
            # skips unchanged entries recomputes it for all of them once.
            self._gateway_dirty = True
        self._tier3 = frozenset(tier3_members)
        self._gateways = gateways

    def set_act_rules(self, act_rules: Iterable[str]) -> None:
        self._act_rules = frozenset(str(rule).upper() for rule in act_rules)

    def set_poll_scale(self, scale: float) -> None:
        """Stretch the poll periods by ``scale`` >= 1 (``PostDialCpuThrottle``).

        Any engaged stretch is at least ``min_poll_scale`` (1.7: POLL_FAST_S x
        it is POLL_GAP_MAX_S + STRETCH_GAP_MARGIN_S), and a scale <= 1, NaN or
        infinity is 1: the schedule of scale 1 is exactly the unstretched one.

        Stretched: the fast period and the 1 Hz period of lingering user
        traffic are multiplied, and LINGER_S and STREAM_WATCH_S grow by the
        extra 1 Hz period, so the last lingering poll does not come earlier
        than it would unstretched.  The AGED poll keeps its due (open +
        AGED_POLL_OFFSET_S); its floor after the previous poll is the
        smallest stretched period (``min_poll_scale`` x POLL_FAST_S, never
        more), so an AGED observation lands at most 0.07 s later than the
        latest an unstretched poll phase allows, and it is never dropped
        before that poll.  Demotion-watch polls (§6) are not stretched.  The
        first poll after dormancy is still immediate and a dormant schedule
        stays dormant.

        Only the schedule changes, never a classification: every stretched
        gap exceeds POLL_GAP_MAX_S, so an ending between two polls is
        UNKNOWN (the bracket rule).  A stretch can turn FAILED_FAST or
        UNANSWERED_SHORT into UNKNOWN; it cannot bracket an ending more
        coarsely than unstretched polls would, and the open line still
        brackets an ending within POLL_GAP_MAX_S of the open, as before.
        """

        value = float(scale)
        if not 1.0 < value < float("inf"):  # NaN too
            value = 1.0
        else:
            value = max(value, self._c.min_poll_scale)
        self._poll_scale = value

    @property
    def poll_scale(self) -> float:
        return self._poll_scale

    def set_origin_rules(self, origin_rules: OriginRules) -> None:
        """User inbound names for snapshot-only entries (the parser takes its own copy)."""

        self._origin_rules = origin_rules or OriginRules()

    def note_network_epoch(self, changed_at_wall: float) -> None:
        """Evidence counts only for opens after the epoch change plus EPOCH_QUIET_S."""

        self._epoch_floor = max(self._epoch_floor, float(changed_at_wall) + self._c.epoch_quiet_s)
        self._discard_events_before(self._epoch_floor)

    def note_wake_gap(self, at_wall: float) -> None:
        """Discard evidence from opens before a wake gap (sleep/resume).

        ``at_wall`` is the start of the gap: opens after it (the ones that
        woke the observer) stay evidence.
        """

        self._wake_floor = max(self._wake_floor, float(at_wall))
        self._discard_events_before(self._wake_floor)
        # The resets below change state an unchanged entry's next observation
        # acts on: bring every lazy entry up to date, observe all next poll.
        for conn in self._present.values():
            self._materialize(conn, self._seq)
        self._force_next = True
        for conn in self._tracked.values():
            if conn.t_open < self._wake_floor:
                conn.stale = True
                conn.t_up_after_down = None
                conn.rates.clear()
                conn.t_last_change = None
        self._wake_since_digest = True
        self._metrics["wake_gaps"] += 1
        self._records.append({"ev": "wake_gap", "t": round(float(at_wall), 3)})

    def _observe_clocks(self, now_wall: float | None, now_perf: float | None) -> float | None:
        """Recalibrate wall - perf; returns the gap start when the offset jumped.

        A jump over WAKE_GAP_S means the perf clock stood still while the wall
        clock moved (sleep on a platform whose counter pauses, or a clock step).
        The gap started after the last wall reading before the jump.
        """

        if now_wall is None or now_perf is None:
            return None
        wall = float(now_wall)
        offset = wall - float(now_perf)
        gap_start: float | None = None
        if self._offset is not None and abs(offset - self._offset) > self._c.wake_gap_s:
            previous = self._last_wall_seen
            gap_start = wall if previous is None else min(previous, wall)
        self._offset = offset
        self._last_wall_seen = wall
        return gap_start

    def _check_wake(self, now_wall: float | None, now_perf: float) -> None:
        """Wake-gap detection shared by polls, failed polls and schedule queries.

        Two signals: the wall - perf offset jumped (see ``_observe_clocks``), or
        the observer runs more than WAKE_GAP_S past a due that was still
        pending.  Dormancy clears the pending due (``next_poll_time``), so the
        first poll after an idle period is not a wake gap.
        """

        starts: list[float] = []
        due = self._due_after_poll_perf
        if due is not None and float(now_perf) - due > self._c.wake_gap_s:
            due_wall = self._due_after_poll_wall
            if due_wall is None:
                due_wall = self._wall(due, None)
            if due_wall is not None:
                starts.append(due_wall if now_wall is None else min(due_wall, float(now_wall)))
            elif now_wall is not None:
                starts.append(float(now_wall))
        jumped = self._observe_clocks(now_wall, now_perf)
        if jumped is not None:
            starts.append(jumped)
        if starts:
            self._due_after_poll_perf = None
            self._due_after_poll_wall = None
            self.note_wake_gap(max(starts))

    def _set_due(self, due_perf: float | None) -> None:
        self._due_after_poll_perf = due_perf
        self._due_after_poll_wall = None if due_perf is None else self._wall(due_perf, None)

    def _wall(self, now_perf: float, now_wall: float | None) -> float | None:
        if now_wall is not None:
            return float(now_wall)
        if self._offset is None:
            return None
        return float(now_perf) + self._offset

    # -- opens --------------------------------------------------------------

    def ingest_open(
        self,
        record: PostDialOpen,
        *,
        now_wall: float | None = None,
        now_perf: float | None = None,
        received_at_wall: float | None = None,
        now_monotonic: float | None = None,
    ) -> None:
        """Track one parsed open.  App and ignored opens never schedule polls.

        ``now_wall``/``now_perf`` are read when the line is drained.  The V4
        reader-lag metric needs the tap's receive time on the wall clock: pass
        ``received_at_wall``, or ``now_monotonic`` read now on the tap's own
        clock (``time.monotonic()`` in core_runner, which is not
        ``perf_counter`` on Windows), giving
        ``now_wall - (now_monotonic - record.received_at_monotonic)``.
        Without either, only the drain lag is measured.
        """

        if now_perf is not None:
            # After a resume the loop may drain lines before it polls.
            self._check_wake(None if now_wall is None else float(now_wall), float(now_perf))
        self._metrics["opens"] += 1
        self._metrics[f"opens_{record.origin.value}"] += 1
        if now_wall is not None:
            ingest_wall: float | None = float(now_wall)
        elif now_perf is not None:
            ingest_wall = self._wall(float(now_perf), None)
        else:
            ingest_wall = None
        if ingest_wall is not None:
            self._drain_lags.append(max(0.0, ingest_wall - record.t_open))
        receive_wall = received_at_wall
        if receive_wall is None and now_wall is not None and now_monotonic is not None:
            receive_wall = float(now_wall) - (float(now_monotonic) - record.received_at_monotonic)
        if receive_wall is not None:
            self._lags.append(max(0.0, float(receive_wall) - record.t_open))
        if record.origin is OpenOrigin.IGNORED:
            return
        key = record.key
        join_window = self._c.join_window_s
        # Late-bind merge (prototype deviation 10): the snapshot raced ahead.
        for conn in self._unbound.get(key, ()):
            if conn.legacy and conn.conn_id is not None and abs(conn.t_open - record.t_open) <= join_window:
                self._materialize(conn, self._seq)
                self._merge_line(conn, record)
                conn.hot = self._needs_tick(conn)
                return
        # The legacy entry already vanished before its line was drained.
        for conn in self._legacy_gone.get(key, ()):
            if abs(conn.t_open - record.t_open) <= join_window:
                self._legacy_gone[key].remove(conn)
                if not self._legacy_gone[key]:
                    del self._legacy_gone[key]
                self._merge_line(conn, record)
                if conn.gone_t is not None:
                    last_alive = conn.last_seen if conn.last_seen is not None else conn.t_open
                    self._classify_gone(conn, conn.gone_t, last_alive)
                return
        self._oid += 1
        conn = _Conn(
            self._oid,
            record.t_open,
            key,
            record.token,
            record.port,
            record.leaf,
            record.origin,
            legacy=False,
        )
        conn.t_ingest = ingest_wall
        if conn.t_open < self._evidence_floor():
            conn.stale = True
        self._tracked[conn.oid] = conn
        self._open_order.append((conn.t_open, conn.oid))
        if conn.origin is OpenOrigin.USER:
            self._pending[conn.oid] = conn
        self._unbound.setdefault(key, []).append(conn)
        self._unbound_open[conn.oid] = conn
        self._record_open(conn)
        self._prune_unpolled(record.t_open)

    def _record_open(self, conn: _Conn) -> None:
        if conn.origin is OpenOrigin.USER and self._eligible_leaf(conn.leaf):
            self._records.append(
                {
                    "ev": "open",
                    "t": round(conn.t_open, 3),
                    "oid": conn.oid,
                    "leaf": self._tokenizer.leaf(conn.leaf),
                    "tok": short_token(conn.token),
                    "port": conn.port,
                    "origin": conn.origin.value,
                }
            )

    def _merge_line(self, conn: _Conn, record: PostDialOpen) -> None:
        conn.legacy = False
        conn.t_open = record.t_open
        conn.token = record.token
        conn.port = record.port
        conn.origin = record.origin
        if conn.leaf is None:
            conn.leaf = record.leaf
        if conn.t_open < self._evidence_floor():
            conn.stale = True
        self._metrics["late_bind_merges"] += 1
        self._record_open(conn)
        if conn.origin is OpenOrigin.USER and conn.oid in self._tracked:
            self._pending[conn.oid] = conn
        if conn.answered and self._evidence_ok(conn):
            # Answered in its legacy samples: the snapshot beat the reader.
            self._note_answered(conn, conn.last_seen if conn.last_seen is not None else conn.t_open)

    def _evidence_floor(self) -> float:
        return max(self._epoch_floor, self._wake_floor)

    def _prune_unpolled(self, reference_wall: float) -> None:
        # Opens are classified by the first poll that misses them; opens that
        # never meet a poll (app traffic while dormant) age out after OPEN_TTL_S.
        # An unbound open older than OPEN_TTL_S can no longer be joined.
        # Ingest order is close to t_open order, so this is amortised O(1).
        limit = reference_wall - self._c.open_ttl_s
        order = self._open_order
        while order and (order[0][0] < limit or order[0][1] not in self._tracked):
            _, oid = order.popleft()
            conn = self._tracked.get(oid)
            if conn is not None and conn.conn_id is None:
                self._untrack(conn)
        while len(self._tracked) > self._MAX_TRACKED:
            victim: _Conn | None = None
            while order and victim is None:
                victim = self._tracked.get(order.popleft()[1])
            if victim is None:
                victim = next(iter(self._tracked.values()))
            listed = victim.conn_id is not None and self._present.get(victim.conn_id) is victim
            self._untrack(victim)
            if listed:
                self._purge_present.append(victim)

    # -- snapshots ----------------------------------------------------------

    def ingest_snapshot(
        self,
        payload: Mapping[str, Any] | None,
        t_req_wall: float,
        t_req_perf: float,
        *,
        cost_s: float | None = None,
    ) -> SnapshotDigest:
        """Process one ``GET /connections`` body taken at (t_req_wall, t_req_perf).

        A None or malformed body counts as a failed attempt (``note_poll_failed``).
        """

        t_req = float(t_req_wall)
        perf = float(t_req_perf)
        self._check_wake(t_req, perf)
        connections = payload.get("connections") if isinstance(payload, Mapping) else None
        if isinstance(payload, Mapping) and connections is None:
            connections = []
        if not isinstance(connections, list):
            return self._failed_attempt(t_req, perf, cost_s, "invalid_body")
        gap_ok = self._begin_poll(t_req, cost_s)
        seq = self._seq
        # A raw poll may have left entries lazy: bring them up to the last
        # poll; from here on this poll observes (or drops) every one of them.
        for conn in self._present.values():
            self._materialize(conn, seq - 1)
            conn.memo_key = None
        self._purge_present.clear()
        present: dict[str, _Conn] = {}
        tcp_count = 0
        for item in connections:
            if not isinstance(item, Mapping):
                continue
            metadata = item.get("metadata")
            if not isinstance(metadata, Mapping) or str(metadata.get("network") or "").lower() != "tcp":
                continue
            conn_id = item.get("id")
            if not isinstance(conn_id, str) or not conn_id:
                continue
            tcp_count += 1
            conn = self._by_id.get(conn_id)
            if conn is None:
                conn = self._bind(item, metadata, conn_id, t_req)
                if conn is None:
                    continue
            present[conn_id] = conn
            self._observe(conn, item, t_req)
            conn.tick_seq = seq
            conn.memo_key = None
        self._absent_bound.clear()
        for conn in list(self._tracked.values()):
            if conn.conn_id is not None and conn.conn_id in present:
                continue
            if conn.t_open > t_req - self._c.reg_slack_s:
                if conn.conn_id is not None:
                    self._absent_bound[conn.oid] = conn
                continue
            self._vanished(conn, t_req)
        self._present = present
        # The next raw poll parses every entry once and rebuilds the memo.
        self._memo.clear()
        self._present_keys = None
        self._gateway_dirty = False
        return self._finish_poll(t_req, perf, cost_s, tcp_count, gap_ok, all_observed=True)

    def ingest_raw(
        self,
        body: bytes | bytearray,
        t_req_wall: float,
        t_req_perf: float,
        *,
        cost_s: float | None = None,
    ) -> SnapshotDigest:
        """Process one raw ``GET /connections`` body (``connections_raw``).

        Equivalent to ``ingest_snapshot(decode_connections_body(body), ...)``:
        the same records, events, triggers, digests, schedule and closes.
        Only entries whose bytes changed are parsed (upload and download
        straight from the bytes when nothing else changed).  An entry whose
        bytes are unchanged is observed at this poll only when a time rule
        can act on it (an unanswered user open with upload that can turn
        AGED, a WAITING episode not yet evaluated, a download rate that can
        still reach STREAM_RATE_BPS); for the others the sample count,
        last-seen time and rate window are replayed when they are read.

        A body in another shape goes through ``json.loads`` and
        ``ingest_snapshot``.  So does every poll of a back-off
        (``_RAW_BACKOFF_POLLS``) after a body the walk could not use, or
        after warm polls that parsed most of their entries.  Raises
        RawBodyError when the body is not a JSON object (the caller counts a
        failed poll, as for a client error); by then only the clock check
        has run, which ``note_poll_failed`` repeats without effect.
        """

        if not isinstance(body, bytes):
            body = bytes(body)
        counts = self.raw_counts
        if self._raw_backoff > 0:
            payload = decode_connections_body(body)
            self._raw_backoff -= 1
            counts["json_polls"] += 1
            return self.ingest_snapshot(payload, t_req_wall, t_req_perf, cost_s=cost_s)
        layout = _raw_layout(body)
        if layout is None:
            payload = decode_connections_body(body)
            counts["json_polls"] += 1
            return self.ingest_snapshot(payload, t_req_wall, t_req_perf, cost_s=cost_s)
        t_req = float(t_req_wall)
        perf = float(t_req_perf)
        self._check_wake(t_req, perf)
        stepped_back = self._max_wall is not None and t_req < self._max_wall
        force = self._force_next or stepped_back or self._exact_until is not None
        warm = bool(self._memo)
        scan = self._scan_raw(body, layout[0], layout[1], force)
        listed: set[bytes] = set()
        if scan is not None:
            listed = set(scan[1])
            if len(listed) != len(scan[1]):
                scan = None  # a key listed twice: only the classic loop is exact
        if scan is None:
            payload = decode_connections_body(body)
            counts["json_polls"] += 1
            self._raw_back_off()
            return self.ingest_snapshot(payload, t_req_wall, t_req_perf, cost_s=cost_s)
        work, keys, tcp_count = scan
        counts["fast_polls"] += 1
        gap_ok = self._begin_poll(t_req, cost_s)
        seq = self._seq
        present = self._present
        memo = self._memo
        if self._purge_present:
            for conn in self._purge_present:
                if present.get(conn.conn_id) is conn:
                    del present[conn.conn_id]
            self._purge_present.clear()
        if self._gateway_dirty:
            # The classic loop recomputes the flag for every entry it observes
            # with a non-empty chains list.  Entries this poll parses do that
            # in ``_observe_values`` (and keep the old flag, as the classic
            # loop does, when their chains are now empty or they are no
            # longer TCP entries); every other listed entry has the chains it
            # had at its last observation.
            gateways = self._gateways
            parsed_keys = {entry[2] for entry in work if entry[0] == _W_PARSED}
            for conn in present.values():
                key = conn.memo_key
                if conn.chains is not None and key in listed and key not in parsed_keys:
                    conn.gateway = bool(gateways) and any(name in gateways for name in conn.chains)
            self._gateway_dirty = False
        dropped: list[_Conn] = []
        last = seq - 1
        observe = self._observe_values
        needs_tick = self._needs_tick
        kinds = [entry[0] for entry in work]
        parsed = kinds.count(_W_PARSED)
        counts["ticks"] += kinds.count(_W_TICK)
        counts["counters"] += kinds.count(_W_COUNTERS)
        counts["parsed"] += parsed
        for entry in work:
            kind = entry[0]
            if kind == _W_TICK:
                conn = entry[1][1]
                if conn.tick_seq != last:
                    self._materialize(conn, last)
                observe(conn, _UNCHANGED, conn.up, conn.down, t_req)
            elif kind == _W_COUNTERS:
                record = entry[1]
                piece = entry[2]
                record[0] = piece
                record[4] = len(piece)
                conn = record[1]
                if conn is None:
                    continue
                if conn.tick_seq != last:
                    self._materialize(conn, last)
                observe(conn, _UNCHANGED, entry[3], entry[4], t_req)
            else:
                record, key, piece, item = entry[1], entry[2], entry[3], entry[4]
                conn = self._raw_item(item, t_req, seq)
                if conn is not None:
                    tcp_count += 1
                previous = record[1] if record is not None else None
                if previous is not None and previous is not conn:
                    # Listed, but no longer a TCP entry with an id: the
                    # classic loop leaves it out of ``present``.
                    dropped.append(previous)
                prefix, suffix = _raw_frame(piece)
                if record is None:
                    memo[key] = [piece, conn, prefix, suffix, len(piece)]
                else:
                    record[:] = [piece, conn, prefix, suffix, len(piece)]
                if conn is None:
                    continue
                conn.memo_key = key
            conn.tick_seq = seq
            conn.hot = needs_tick(conn)
        # Every tracked connection that is not listed now, in ``_tracked``
        # order (oid order), exactly as the classic loop visits them.
        absent: list[_Conn] = []
        previous_keys = self._present_keys
        if previous_keys is None:
            for conn_id, conn in list(present.items()):
                if conn.tick_seq != seq:
                    del present[conn_id]
                    if conn.oid in self._tracked:
                        absent.append(conn)
        else:
            for key in previous_keys - listed:
                record = memo.pop(key, None)
                if record is None or record[1] is None:
                    continue
                conn = record[1]
                self._materialize(conn, seq - 1)
                conn.memo_key = None
                if present.get(conn.conn_id) is conn:
                    del present[conn.conn_id]
                if conn.oid in self._tracked:
                    absent.append(conn)
        for conn in dropped:
            self._materialize(conn, seq - 1)
            conn.memo_key = None
            if present.get(conn.conn_id) is conn:
                del present[conn.conn_id]
            if conn.oid in self._tracked:
                absent.append(conn)
        absent.extend(self._absent_bound.values())
        self._absent_bound.clear()
        absent.extend(self._unbound_open.values())
        absent.sort(key=_oid_of)
        limit = t_req - self._c.reg_slack_s
        for conn in absent:
            if conn.t_open > limit:
                if conn.conn_id is not None:
                    self._absent_bound[conn.oid] = conn
                continue
            self._vanished(conn, t_req)
        self._present_keys = listed
        if warm and len(keys) >= self._RAW_SHARE_MIN_ENTRIES:
            # Smoothed share of listed entries this walk had to parse; a
            # cold memo (the first raw poll, or the first after a classic
            # one) parses everything and says nothing about the next polls.
            self._raw_share += self._RAW_SHARE_ALPHA * (parsed / len(keys) - self._raw_share)
            if self._raw_share > self._RAW_SHARE_MAX:
                self._raw_back_off()
        # A forced poll observed every listed entry: no lazy replay reaches
        # back before it (after a wall-clock step this keeps the poll history
        # from growing while every poll is forced).
        return self._finish_poll(t_req, perf, cost_s, tcp_count, gap_ok, all_observed=force)

    def _raw_back_off(self) -> None:
        """The next ``_RAW_BACKOFF_POLLS`` raw polls go through ``json.loads``."""

        self._raw_backoff = self._RAW_BACKOFF_POLLS
        self.raw_counts["backoffs"] += 1

    def _scan_raw(self, body: bytes, first: int, close: int, force: bool) -> Any:
        """Walk the entries without changing any state.

        Returns ``(work, keys, tcp_count)``: the entries this poll must
        observe, in body order, and every entry's memo key (the exact id
        bytes and the closing quote); None when the body must go through
        ``json.loads``: an entry that is not JSON, an id the memo cannot key
        (longer than ``_RAW_KEY_MAX``, with escapes or non-ASCII bytes, so
        that two different keys could decode to one id), or an entry whose
        parsed id is not its leading one (a repeated ``"id"`` key, of which
        ``json.loads`` keeps the last).
        """

        keys: list[bytes] = []
        work: list[tuple[Any, ...]] = []
        tcp_count = 0
        if first < 0:
            return work, keys, tcp_count
        add_key = keys.append
        add_work = work.append
        memo_get = self._memo.get
        startswith = body.startswith
        find = body.find
        sep = _RAW_ENTRY_SEP
        sep_len = _RAW_ENTRY_SEP_LEN
        key_max = _RAW_KEY_MAX
        uuid_len = _RAW_UUID_KEY_LEN
        pos = first
        while True:
            # A stored key's only quote is its last byte, so a hit on the
            # UUID-length window is that exact id; otherwise find the id's end.
            key = body[pos : pos + uuid_len]
            record = memo_get(key)
            if record is None:
                quote = find(b'"', pos, pos + key_max)
                if quote < 0:
                    return None
                if quote != pos + uuid_len - 1:
                    key = body[pos : quote + 1]
                    record = memo_get(key)
            add_key(key)
            if record is not None and startswith(record[0], pos):
                end = pos + record[4]
                if end == close or startswith(sep, end):
                    conn = record[1]
                    if conn is not None:
                        tcp_count += 1
                        if force or conn.hot:
                            add_work((_W_TICK, record))
                    if end == close:
                        return work, keys, tcp_count
                    pos = end + sep_len
                    continue
            nxt = find(sep, pos, close)
            end = close if nxt < 0 else nxt
            piece = body[pos:end]
            counters = None if record is None else _raw_counters(piece, record[2], record[3])
            if counters is not None:
                if record[1] is not None:
                    tcp_count += 1
                add_work((_W_COUNTERS, record, piece, counters[0], counters[1]))
            else:
                if record is None and (b"\\" in key or not key.isascii()):
                    return None
                item = _parse_raw_entry(piece)
                if item is _RAW_BAD or item.get("id") != key[:-1].decode("ascii"):
                    return None
                add_work((_W_PARSED, record, key, piece, item))
            if nxt < 0:
                return work, keys, tcp_count
            pos = nxt + sep_len

    def _raw_item(self, item: Any, t_req: float, seq: int) -> _Conn | None:
        """One parsed entry through the classic loop's per-item steps."""

        if not isinstance(item, Mapping):
            return None
        metadata = item.get("metadata")
        if not isinstance(metadata, Mapping) or str(metadata.get("network") or "").lower() != "tcp":
            return None
        conn_id = item.get("id")
        if not isinstance(conn_id, str) or not conn_id:
            return None
        conn = self._by_id.get(conn_id)
        if conn is None:
            conn = self._bind(item, metadata, conn_id, t_req)
            if conn is None:
                return None
        else:
            self._materialize(conn, seq - 1)
        self._present[conn_id] = conn
        self._absent_bound.pop(conn.oid, None)
        self._observe(conn, item, t_req)
        return conn

    def _begin_poll(self, t_req: float, cost_s: float | None) -> bool:
        """Bookkeeping every accepted poll does first; returns ``gap_ok``."""

        gap_ok = False
        if self._last_poll_wall is not None:
            gap = t_req - self._last_poll_wall
            gap_ok = gap <= self._c.poll_gap_max_s
            self._gaps.append(gap)
        if cost_s is not None:
            self._costs.append(max(0.0, float(cost_s)))
            self._adapt_cost(cost_s)
        self._poll_walls.append(t_req)
        self._metrics["polls"] += 1
        self._seq += 1
        if self._max_wall is not None and t_req < self._max_wall:
            until = self._max_wall + self._c.stream_rate_window_s
            if self._exact_until is None or until > self._exact_until:
                self._exact_until = until
        if self._max_wall is None or t_req > self._max_wall:
            self._max_wall = t_req
        self._hist.append(t_req)
        return gap_ok

    def _finish_poll(
        self,
        t_req: float,
        perf: float,
        cost_s: float | None,
        tcp_count: int,
        gap_ok: bool,
        *,
        all_observed: bool = False,
    ) -> SnapshotDigest:
        self._force_next = False
        if self._exact_until is not None and t_req > self._exact_until:
            self._exact_until = None
        self._prune(t_req)
        self._last_poll_wall = t_req
        self._last_poll_perf = perf
        self._set_due(self._compute_next_poll(perf, t_req))
        hist = self._hist
        if all_observed:
            # Every listed entry was observed at this poll, so no replay
            # starts before it.
            if len(hist) > 1:
                del hist[:-1]
                self._hist_base = self._seq
        else:
            # Keep the poll history back to the last poll before the rate window.
            floor = t_req - self._c.stream_rate_window_s - 1.0
            drop = 0
            while len(hist) - drop > 2 and hist[drop + 1] < floor:
                drop += 1
            if drop:
                del hist[:drop]
                self._hist_base += drop
        evidence, self._evidence_since_digest = self._evidence_since_digest, 0
        answered, self._answered_since_digest = self._answered_since_digest, False
        wake_gap, self._wake_since_digest = self._wake_since_digest, False
        return SnapshotDigest(True, t_req, gap_ok, cost_s, tcp_count, evidence, answered, wake_gap)

    # -- lazy observation (``ingest_raw``) ---------------------------------

    def _needs_tick(self, conn: _Conn) -> bool:
        """Whether an unchanged entry must still be observed at the next poll.

        Only three things change on an observation whose upload, download
        and chains are unchanged (besides the replayable sample count,
        last-seen time and rate window): AGED at T_AGE_S, the WAITING record
        and event, and a 2 s download rate at or above STREAM_RATE_BPS
        (``peak_rate``, ``t_stream_rate``).  The rate over the window can
        never exceed (download - lowest sample in the window) / window; the
        lowest sample is the oldest unless a download counter ever went down
        (never for Mihomo's counters, which only grow).
        """

        if conn.origin is OpenOrigin.USER and not conn.legacy:
            if not conn.answered:
                if not conn.aged and conn.up > 0:
                    return True
            elif (
                conn.t_up_after_down is not None
                and not conn.stale
                and conn.waiting_eval_at != conn.t_up_after_down
            ):
                return True
        rates = conn.rates
        if not rates:
            return self._rate_hot_bytes <= 0.0
        base = min(sample[1] for sample in rates) if conn.down_fell else rates[0][1]
        return (conn.down - base) * (1.0 + 1e-9) >= self._rate_hot_bytes

    def _materialize(self, conn: _Conn, upto: int) -> None:
        """Replay the unchanged observations of a lazy entry up to poll ``upto``.

        Exact because the entry was listed at every poll since its last
        observation (a missing id is gone for good), its counters are
        monotonic (equal at both ends, so equal between), and ``_needs_tick``
        kept it observed whenever an unchanged observation could do more
        than count, stamp and slide the rate window.
        """

        if conn.memo_key is None:
            return
        last_tick = conn.tick_seq
        count = upto - last_tick
        if count <= 0:
            return
        hist = self._hist
        base = self._hist_base
        end_index = upto - base + 1
        last_wall = hist[end_index - 1]
        conn.n_samples += count
        conn.last_seen = last_wall
        down = conn.down
        cutoff = last_wall - self._c.stream_rate_window_s
        first_index = last_tick + 1 - base
        rates = conn.rates
        # The window after the last replayed observation: every sample from
        # the last one at or before ``cutoff`` on (``_update_rate`` pops a
        # sample once the next one is ``window`` old).
        if first_index < 0:
            start = bisect.bisect_right(hist, cutoff, 0, end_index) - 1
            if start < 0:
                self._metrics["lazy_history_miss"] += 1
                start = 0
            rates.clear()
            rates.extend([(wall, down) for wall in hist[start:end_index]])
        else:
            inside = bisect.bisect_right(hist, cutoff, first_index, end_index) - first_index
            if inside:
                rates.clear()
                rates.extend([(wall, down) for wall in hist[first_index + inside - 1 : end_index]])
            else:
                old = len(rates)
                keep = 0
                while keep + 1 < old and rates[keep + 1][0] <= cutoff:
                    keep += 1
                for _ in range(keep):
                    rates.popleft()
                rates.extend([(wall, down) for wall in hist[first_index:end_index]])
        conn.tick_seq = upto

    def note_poll_failed(
        self,
        t_req_wall: float,
        t_req_perf: float,
        *,
        cost_s: float | None = None,
        reason: str = "error",
    ) -> SnapshotDigest:
        """A ``GET /connections`` attempt that failed or whose body was rejected
        (for example over MAX_BODY_BYTES).

        The attempt moves the schedule by one period so a persistent error
        cannot spin; it is not an observation, so ``gap_ok``, the bracketing
        polls and the latest-snapshot age still refer to the last good poll.
        """

        t_req = float(t_req_wall)
        perf = float(t_req_perf)
        self._check_wake(t_req, perf)
        code = re.sub(r"[^a-z0-9_]", "_", str(reason or "error").lower())[:32] or "error"
        return self._failed_attempt(t_req, perf, cost_s, code)

    def _failed_attempt(
        self,
        t_req: float,
        perf: float,
        cost_s: float | None,
        reason: str,
    ) -> SnapshotDigest:
        self._metrics["snapshots_skipped"] += 1
        self._metrics[f"snapshots_skipped_{reason}"] += 1
        if cost_s is not None:
            self._adapt_cost(cost_s)
        if self._last_failed_perf is None or perf > self._last_failed_perf:
            self._last_failed_perf = perf
        self._set_due(self._compute_next_poll(perf, t_req))
        wake_gap, self._wake_since_digest = self._wake_since_digest, False
        return SnapshotDigest(False, t_req, False, cost_s, 0, 0, False, wake_gap, reason)

    def _adapt_cost(self, cost_s: float) -> None:
        cost = max(0.0, float(cost_s))
        if cost > self._c.cost_adapt_threshold_s:
            self._fast_period = max(self._c.poll_fast_s, self._c.cost_adapt_factor * cost)
        else:
            self._fast_period = self._c.poll_fast_s

    def note_poll_cost(self, total_s: float) -> None:
        """The whole accepted poll's cost: request, decode and this core's
        ingest (``ingest_snapshot`` sees only the request and decode, and
        ``ingest_raw`` only the request).

        Re-adapts the fast period on the total, so the back-off under load
        reacts to everything a poll holds the GIL for.  The total is never
        below the request cost, so this only ever keeps or lengthens the
        period ``ingest_snapshot`` chose; the next ``next_poll_time`` uses it.
        """

        total = max(0.0, float(total_s))
        self._total_costs.append(total)
        self._adapt_cost(total)

    def _bind(
        self,
        item: Mapping[str, Any],
        metadata: Mapping[str, Any],
        conn_id: str,
        t_req: float,
    ) -> _Conn | None:
        source_ip = str(metadata.get("sourceIP") or "")
        source_port = _int_value(metadata.get("sourcePort"))
        key = (source_ip, source_port)
        start = parse_rfc3339(item.get("start"))
        candidates = self._unbound.get(key)
        if candidates and start is not None:
            best: _Conn | None = None
            for conn in candidates:
                if conn.conn_id is not None or conn.legacy:
                    continue
                if conn.t_open < t_req - self._c.open_ttl_s:
                    continue
                if abs(start - conn.t_open) > self._c.join_window_s:
                    continue
                if best is None or conn.t_open > best.t_open:
                    best = conn
            if best is not None:
                best.conn_id = conn_id
                self._by_id[conn_id] = best
                self._unbound_open.pop(best.oid, None)
                return best
        # Snapshot-only entry (§18.3): inner, app or legacy.
        if str(metadata.get("type") or "") == "Inner" or str(metadata.get("sourcePort") or "") == "0":
            origin = OpenOrigin.INNER
        elif (
            str(metadata.get("inboundName") or "") not in self._origin_rules.user_inbound_names
            or metadata.get("specialProxy")
        ):
            origin = OpenOrigin.APP
        else:
            origin = OpenOrigin.LEGACY
        chains = item.get("chains")
        leaf = str(chains[0]) if isinstance(chains, list) and chains else None
        port = _int_value(metadata.get("destinationPort"))
        # No destination token: a snapshot-only entry is never evidence, and
        # the line a late bind merges into it brings its own token.
        token = None
        self._oid += 1
        conn = _Conn(
            self._oid,
            start if start is not None else t_req,
            key,
            token,
            port,
            leaf,
            origin,
            legacy=True,
        )
        conn.conn_id = conn_id
        self._by_id[conn_id] = conn
        self._tracked[conn.oid] = conn
        self._unbound.setdefault(key, []).append(conn)
        self._metrics["legacy_entries"] += 1
        return conn

    def _observe(self, conn: _Conn, item: Mapping[str, Any], t_req: float) -> None:
        self._observe_values(
            conn,
            _chain_names(item.get("chains")),
            _int_value(item.get("upload")),
            _int_value(item.get("download")),
            t_req,
        )

    def _observe_values(
        self,
        conn: _Conn,
        names: Any,
        up: int,
        down: int,
        t_req: float,
    ) -> None:
        """One observation; ``names`` is ``_UNCHANGED`` when the entry's chains
        are byte-identical to the last observation's."""

        if names is not _UNCHANGED:
            conn.chains = names
            if names is not None:
                conn.leaf = names[0]
                gateways = self._gateways
                conn.gateway = bool(gateways) and any(name in gateways for name in names)
        first = conn.n_samples == 0
        changed = False
        if not first or not conn.legacy:
            if down > conn.down:
                changed = True
                conn.t_last_down_inc = t_req
                conn.up_at_down = up
                conn.t_up_after_down = None
            if up != conn.up:
                changed = True
            if (
                conn.t_last_down_inc is not None
                and conn.t_up_after_down is None
                and up > conn.up_at_down
            ):
                conn.t_up_after_down = t_req
        if down < conn.down:
            conn.down_fell = True
        conn.up = up
        conn.down = down
        conn.n_samples += 1
        conn.last_seen = t_req
        if changed:
            conn.t_last_change = t_req
        self._update_rate(conn, t_req, down)
        c = self._c
        user = conn.origin is OpenOrigin.USER and not conn.legacy
        if down > c.x_bytes and not conn.answered:
            conn.answered = True
            if user and self._evidence_ok(conn):
                self._note_answered(conn, t_req)
        if not user:
            return
        if (
            not conn.answered
            and not conn.aged
            and up > 0
            and t_req - conn.t_open >= c.t_age_s
        ):
            conn.aged = True
            self._emit(conn, ConnectionClass.AGED, t_req, bracket_ok=True)
        up_after_down = conn.t_up_after_down
        if conn.answered and up_after_down is not None and not conn.stale:  # _waiting_for
            waiting_for = t_req - up_after_down
            if waiting_for >= c.t_wait_s and conn.waiting_logged_at != up_after_down:
                conn.waiting_logged_at = up_after_down
                self._record_outcome(conn, ConnectionClass.WAITING, t_req, True)
            if (
                waiting_for >= c.t_wait_trigger_s
                and conn.waiting_trigger_at != up_after_down
                and self._stream_recent(conn, t_req)
            ):
                conn.waiting_trigger_at = up_after_down
                self._emit(conn, ConnectionClass.WAITING, t_req, bracket_ok=True, record=False)
            if waiting_for >= self._waiting_eval_s:
                # Both thresholds were checked for this episode; until upload
                # or download move again, later observations change nothing.
                conn.waiting_eval_at = up_after_down

    def _update_rate(self, conn: _Conn, t_req: float, down: int) -> None:
        window = self._c.stream_rate_window_s
        rates = conn.rates
        if rates and rates[-1][0] - t_req > window:
            # The wall clock stepped back by more than the window: samples
            # of the old timeline would stay in the window (and the deque
            # would grow by one per poll) until the clock caught up.
            rates.clear()
        rates.append((t_req, down))
        if len(rates) < 2:
            return
        cutoff = t_req - window
        while len(rates) >= 2 and rates[1][0] <= cutoff:
            rates.popleft()
        if len(rates) < 2:
            return
        base_t, base_down = rates[0]
        rate = (down - base_down) / max(window, t_req - base_t)
        if rate > conn.peak_rate:
            conn.peak_rate = rate
        if rate >= self._c.stream_rate_bps:
            conn.t_stream_rate = t_req

    def _stream_recent(self, conn: _Conn, now: float) -> bool:
        return conn.t_stream_rate is not None and now - conn.t_stream_rate <= self._c.stream_watch_s

    def _waiting_for(self, conn: _Conn, now: float) -> float | None:
        if not conn.answered or conn.t_up_after_down is None or conn.stale:
            return None
        return now - conn.t_up_after_down

    def _vanished(self, conn: _Conn, t_req: float) -> None:
        if conn.legacy:
            conn.gone_t = t_req
            self._untrack(conn)
            self._legacy_gone.setdefault(conn.key, []).append(conn)
            return
        if conn.last_seen is not None:
            last_alive = conn.last_seen
            end = t_req
        else:
            last_alive = conn.t_open
            threshold = conn.t_open + self._c.reg_slack_s
            if conn.t_ingest is not None and conn.t_ingest - conn.t_open > self._c.join_window_s:
                # Drained late: a poll before the drain may have held this
                # connection as a legacy entry that has since been forgotten
                # (vanished legacy entries are kept for JOIN_WINDOW_S), so only
                # a poll after the drain can show it absent.
                threshold = max(threshold, conn.t_ingest)
            end = self._first_poll_after(threshold, t_req)
        self._classify_gone(conn, end, last_alive)
        self._untrack(conn)

    def _first_poll_after(self, threshold: float, default: float) -> float:
        polls = self._poll_walls
        index = bisect.bisect_left(polls, threshold)
        if index < len(polls):
            return min(polls[index], default)
        return default

    def _classify_gone(self, conn: _Conn, end: float, last_alive: float) -> None:
        """§3.2 endings; the open line counts as the first bracketing observation."""

        bracket_ok = end - last_alive <= self._c.poll_gap_max_s + 1e-9
        life = end - conn.t_open
        if conn.answered:
            candidate = ConnectionClass.ANSWERED_CLOSED
        elif life <= self._c.t_fast_s:
            candidate = ConnectionClass.FAILED_FAST
        elif life <= self._c.t_short_s and conn.up > 0:
            candidate = ConnectionClass.UNANSWERED_SHORT
        else:
            candidate = ConnectionClass.UNANSWERED_CLOSED
        if conn.origin is not OpenOrigin.USER or conn.legacy:
            return
        if candidate not in {ConnectionClass.FAILED_FAST, ConnectionClass.UNANSWERED_SHORT}:
            return
        if bracket_ok:
            self._emit(conn, candidate, end, bracket_ok=True)
        elif self._evidence_ok(conn):
            # Poll-gap guard: unbracketed evidence is UNKNOWN, counted for V4.
            self._metrics["unknown"] += 1
            self._metrics[f"unknown_{candidate.value}"] += 1
            self._record_outcome(conn, ConnectionClass.UNKNOWN, end, False, would=candidate)

    def _untrack(self, conn: _Conn) -> None:
        self._tracked.pop(conn.oid, None)
        self._pending.pop(conn.oid, None)
        self._unbound_open.pop(conn.oid, None)
        self._absent_bound.pop(conn.oid, None)
        key = conn.memo_key
        if key is not None:
            # Evicted while listed: bring it up to date for readers of
            # ``_present`` until the next poll, whose memo no longer knows it
            # (the entry is bound afresh, as the classic loop would).
            self._materialize(conn, self._seq)
            record = self._memo.get(key)
            if record is not None and record[1] is conn:
                del self._memo[key]
            conn.memo_key = None
        if conn.conn_id is not None and self._by_id.get(conn.conn_id) is conn:
            del self._by_id[conn.conn_id]
        pending = self._unbound.get(conn.key)
        if pending is not None:
            try:
                pending.remove(conn)
            except ValueError:
                pass
            if not pending:
                del self._unbound[conn.key]

    def _prune(self, t_req: float) -> None:
        horizon = (
            max(
                self._c.w_spread_s,
                self._c.w_aged_s,
                self._c.w_waiting_s,
                self._c.trigger_share_window_s,
            )
            + 1.0
        )
        for leaf in list(self._events):
            events = self._events[leaf]
            while events and events[0].t < t_req - horizon:
                events.popleft()
            if not events:
                del self._events[leaf]
        for leaf in list(self._answered):
            answered = self._answered[leaf]
            while answered and answered[0][0] < t_req - horizon:
                answered.popleft()
            if not answered:
                del self._answered[leaf]
        for key in list(self._legacy_gone):
            kept = [
                conn
                for conn in self._legacy_gone[key]
                if conn.gone_t is not None and t_req - conn.gone_t <= self._c.join_window_s
            ]
            if kept:
                self._legacy_gone[key] = kept
            else:
                del self._legacy_gone[key]
        for rule_key in [key for key, t in self._last_match.items() if t_req - t > 3600.0]:
            self._last_match.pop(rule_key, None)

    def _discard_events_before(self, floor_wall: float) -> None:
        for leaf in list(self._events):
            kept = deque(event for event in self._events[leaf] if event.t_open >= floor_wall)
            if kept:
                self._events[leaf] = kept
            else:
                del self._events[leaf]

    # -- evidence -----------------------------------------------------------

    def _eligible_leaf(self, leaf: str | None) -> bool:
        return leaf is None or leaf in self._tier3

    def _evidence_ok(self, conn: _Conn) -> bool:
        return (
            conn.origin is OpenOrigin.USER
            and not conn.legacy
            and not conn.stale
            and not conn.gateway
            and conn.leaf is not None
            and conn.leaf in self._tier3
            and conn.t_open >= self._epoch_floor
            and conn.t_open >= self._wake_floor
        )

    def _note_answered(self, conn: _Conn, t_req: float) -> None:
        leaf = conn.leaf
        if leaf is None:
            return
        self._metrics["answered_tier3_user"] += 1
        self._answered_since_digest = True
        self._answered.setdefault(leaf, deque()).append((t_req, conn.t_open, conn.oid))
        previous = self._answered_open_max.get(leaf)
        if previous is None or conn.t_open > previous:
            self._answered_open_max[leaf] = conn.t_open

    def _emit(
        self,
        conn: _Conn,
        kind: ConnectionClass,
        t: float,
        *,
        bracket_ok: bool,
        record: bool = True,
    ) -> None:
        if not self._evidence_ok(conn):
            return
        leaf = conn.leaf
        assert leaf is not None
        self._events.setdefault(leaf, deque()).append(
            _Event(t, kind, conn.token, conn.oid, conn.t_open, conn.port)
        )
        self._new_event_leaves.add(leaf)
        self._evidence_since_digest += 1
        self._metrics["evidence"] += 1
        self._metrics[f"class_{kind.value}"] += 1
        if record:
            self._record_outcome(conn, kind, t, bracket_ok)

    def _record_outcome(
        self,
        conn: _Conn,
        kind: ConnectionClass,
        t: float,
        bracket_ok: bool,
        *,
        would: ConnectionClass | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "ev": "outcome",
            "t": round(t, 3),
            "oid": conn.oid,
            "class": kind.value,
            "leaf": self._tokenizer.leaf(conn.leaf),
            "tok": short_token(conn.token),
            "life_ms": round((t - conn.t_open) * 1000.0, 1),
            "bracket_ok": bool(bracket_ok),
        }
        if would is not None:
            record["would"] = would.value
        self._records.append(record)

    # -- triggers (§18.5) ---------------------------------------------------

    def evaluate_triggers(
        self,
        now_wall: float,
        *,
        is_exonerated: Callable[[str, str], bool] | None = None,
    ) -> TriggerEvaluation:
        """Evaluate every leaf with new events against act rules and shadow candidates.

        ``is_exonerated(leaf, token)`` implements the per-(leaf, token)
        exoneration of the §3.3 event filter; the other filter terms were
        applied when the event was recorded.

        An act-rule match becomes a ``Trigger`` only while the failed-share
        guard passes on L (``_failed_share``; TRIGGER_MIN_FAILED_SHARE 0, the
        default, turns it off).  A match the guard holds back is returned as
        a ``ShadowTrigger`` with ``share_ok`` False, once per episode.  Every
        match carries the guard's counts and the SHARE_VARIANTS verdicts, and
        an act-rule match lists in ``opens`` the variants whose episode
        trigger it is; one that opens a variant at a match nothing else logs
        is returned as a variant-only ``ShadowTrigger``.  So the rules alone,
        the 0.5 guard and its onset variant can be compared on the same data
        at any setting, and none of this moves a ``Trigger``.
        """

        now = float(now_wall)
        leaves = sorted(self._new_event_leaves)
        self._new_event_leaves.clear()
        triggers: list[Trigger] = []
        shadow: list[ShadowTrigger] = []
        variant_records = 0
        c = self._c
        for leaf in leaves:
            if leaf not in self._tier3:
                continue
            events = [
                event
                for event in self._events.get(leaf, ())
                if not (
                    is_exonerated is not None
                    and event.token is not None
                    and is_exonerated(leaf, event.token)
                )
            ]
            matches: list[tuple[str, list[_Event], float]] = []
            fast = self._window(events, ConnectionClass.FAILED_FAST, now, c.w_fast_s)
            if len(fast) >= c.k_fast:
                matches.append((RULE_FAST, fast, c.w_fast_s))
            aged = self._window(events, ConnectionClass.AGED, now, c.w_aged_s)
            if len(aged) >= c.k_aged:
                matches.append((RULE_AGED, aged, c.w_aged_s))
            spread = self._window(events, ConnectionClass.FAILED_FAST, now, c.w_spread_s)
            if len(spread) >= c.k_spread:
                matches.append((RULE_FAST_SPREAD, spread, c.w_spread_s))
            slow = self._window(events, ConnectionClass.UNANSWERED_SHORT, now, c.w_slow_s)
            if len(slow) >= c.k_slow and len({event.token for event in slow}) >= c.k_slow_tokens:
                matches.append((RULE_SLOW, slow, c.w_slow_s))
            waiting = self._waiting_match(leaf, events, now)
            if waiting is not None:
                matches.append((RULE_WAITING, waiting, c.w_waiting_s))
            single = [
                event
                for event in aged
                if event.port == 443
                and self._answered_open_max.get(leaf, float("-inf")) < event.t_open
            ]
            if single:
                matches.append((RULE_AGED_SINGLE, single[-1:], c.w_aged_s))
            if not matches:
                continue
            ratio_failed = len(spread)
            ratio_answered = sum(
                1 for t, _t_open, _oid in self._answered.get(leaf, ()) if now - t <= c.w_spread_s
            )
            share_failed, share_answered, onset_answered = self._failed_share(leaf, events, now)
            share_ok = self._share_passes(share_failed, share_answered)
            share_ok_at_0_5 = _share_reaches(share_failed, share_answered, SHARE_VARIANT_MIN)
            onset_ok_at_0_5 = _share_reaches(share_failed, onset_answered, SHARE_VARIANT_MIN)
            variant_ok = ((VARIANT_SHARE, share_ok_at_0_5), (VARIANT_ONSET, onset_ok_at_0_5))
            act_selected = False
            for rule, rule_events, window in matches:
                episode_key = (leaf, rule)
                last = self._last_match.get(episode_key)
                new_episode = last is None or now - last > window
                if new_episode:
                    self._episodes[episode_key] = self._episodes.get(episode_key, 0) + 1
                self._last_match[episode_key] = now
                episode = self._episodes[episode_key]
                tokens = tuple(
                    dict.fromkeys(event.token for event in rule_events if event.token)
                )
                first_t = min(event.t for event in rule_events)
                with_ratio = rule in {RULE_FAST, RULE_FAST_SPREAD}
                # The first match of the episode that passes the guard opens
                # the logged trigger.  With the guard off every match passes,
                # so this is exactly ``new_episode``.
                first_pass = self._acted.get(episode_key) != episode
                if share_ok:
                    self._acted[episode_key] = episode
                if rule in self._act_rules:
                    # Each logged-only variant follows ``_acted`` on its own
                    # verdict, at every act-rule match (selected or not).
                    opened: list[str] = []
                    for name, ok in variant_ok:
                        variant_key = (leaf, rule, name)
                        if ok and self._variant_acted.get(variant_key) != episode:
                            self._variant_acted[variant_key] = episode
                            opened.append(name)
                    if act_selected:
                        continue
                    act_selected = True
                    opens = ((VARIANT_RULES,) if new_episode else ()) + tuple(opened)
                    if share_ok:
                        triggers.append(
                            Trigger(
                                kind=rule,
                                leaf=leaf,
                                tokens=tokens,
                                t=now,
                                n_events=len(rule_events),
                                first_event_t=first_t,
                                episode=episode,
                                new_episode=first_pass,
                                ratio_failed=ratio_failed if with_ratio else None,
                                ratio_answered=ratio_answered if with_ratio else None,
                                share_failed=share_failed,
                                share_answered=share_answered,
                                share_ok=True,
                                onset_answered=onset_answered,
                                share_ok_at_0_5=share_ok_at_0_5,
                                onset_ok_at_0_5=onset_ok_at_0_5,
                                opens=opens,
                            )
                        )
                        # The caller logs a trigger once per episode (``new_episode``).
                        logged = first_pass
                        held = False
                    else:
                        # Held back by the guard: logged like a shadow candidate
                        # (once per episode), so the unguarded rule stays measurable.
                        self._metrics["share_blocked"] += 1
                        logged = held = new_episode
                    # A new episode is always logged, so a variant-only record
                    # never names VARIANT_RULES.
                    variant_only = bool(opens) and not logged
                    if variant_only:
                        variant_records += 1
                    if held or variant_only:
                        shadow.append(
                            ShadowTrigger(
                                rule=rule,
                                leaf=leaf,
                                tokens=tokens,
                                t=now,
                                n_events=len(rule_events),
                                first_event_t=first_t,
                                episode=episode,
                                ratio_failed=ratio_failed if with_ratio else None,
                                ratio_answered=ratio_answered if with_ratio else None,
                                share_failed=share_failed,
                                share_answered=share_answered,
                                share_ok=share_ok,
                                onset_answered=onset_answered,
                                share_ok_at_0_5=share_ok_at_0_5,
                                onset_ok_at_0_5=onset_ok_at_0_5,
                                opens=opens,
                                variant_only=variant_only,
                            )
                        )
                    continue
                if new_episode:
                    shadow.append(
                        ShadowTrigger(
                            rule=rule,
                            leaf=leaf,
                            tokens=tokens,
                            t=now,
                            n_events=len(rule_events),
                            first_event_t=first_t,
                            episode=episode,
                            ratio_failed=ratio_failed if with_ratio else None,
                            ratio_answered=ratio_answered if with_ratio else None,
                            share_failed=share_failed,
                            share_answered=share_answered,
                            share_ok=share_ok,
                            onset_answered=onset_answered,
                            share_ok_at_0_5=share_ok_at_0_5,
                            onset_ok_at_0_5=onset_ok_at_0_5,
                        )
                    )
        self._metrics["triggers"] += len(triggers)
        self._metrics["shadow_triggers"] += len(shadow) - variant_records
        if variant_records:
            self._metrics["variant_triggers"] += variant_records
        return TriggerEvaluation(tuple(triggers), tuple(shadow))

    def _failed_share(self, leaf: str, events: Sequence[_Event], now: float) -> tuple[int, int, int]:
        """(failed, answered, onset_answered) among L's evidence connections
        classified within TRIGGER_SHARE_WINDOW_S: FAILED_FAST and AGED events
        that pass the event filter, and first downloads (``_note_answered``
        takes the same user-origin TIER3 connections).  Each connection has
        one class: an AGED connection that got its first download afterwards
        counts as answered (slow, not failing), not as failed; its answer is
        always inside the window when its AGED event is.  ``onset_answered``
        counts only the answered connections opened at or after the earliest
        open among the counted failures (a shadow-only measurement)."""

        window = self._c.trigger_share_window_s
        recent = [
            (t_open, oid) for t, t_open, oid in self._answered.get(leaf, ()) if now - t <= window
        ]
        answered_oids = {oid for _t_open, oid in recent}
        failed = 0
        onset = float("inf")
        for event in events:
            if (
                (event.kind is ConnectionClass.FAILED_FAST or event.kind is ConnectionClass.AGED)
                and now - event.t <= window
                and event.oid not in answered_oids
            ):
                failed += 1
                if event.t_open < onset:
                    onset = event.t_open
        onset_answered = sum(1 for t_open, _oid in recent if t_open >= onset)
        return failed, len(recent), onset_answered

    def _share_passes(self, failed: int, answered: int) -> bool:
        minimum = self._c.trigger_min_failed_share
        if not minimum > 0.0:  # 0 (the default, or NaN): the guard is off
            return True
        return _share_reaches(failed, answered, minimum)

    @staticmethod
    def _window(
        events: Sequence[_Event],
        kind: ConnectionClass,
        now: float,
        window: float,
    ) -> list[_Event]:
        return [event for event in events if event.kind is kind and now - event.t <= window]

    def _waiting_match(self, leaf: str, events: Sequence[_Event], now: float) -> list[_Event] | None:
        c = self._c
        waiting = [
            event
            for event in events
            if event.kind is ConnectionClass.WAITING and now - event.t <= c.w_waiting_s
        ]
        if not waiting:
            return None
        live = []
        for event in waiting:
            conn = self._tracked.get(event.oid)
            if conn is None:
                continue
            waited = self._waiting_for(conn, now)
            if waited is not None and waited >= c.t_wait_trigger_s and self._stream_recent(conn, now):
                live.append(event)
        if not live:
            return None
        concurrent = any(
            event.kind in {ConnectionClass.AGED, ConnectionClass.FAILED_FAST}
            and now - event.t <= c.w_waiting_s
            for event in events
        )
        if concurrent or len(live) >= c.k_waiting:
            return live
        return None

    # -- schedule (§18.4) ---------------------------------------------------

    def next_poll_time(self, now_perf: float, now_wall: float | None = None) -> float | None:
        """Perf-clock time of the next ``GET /connections``; None means dormant.

        Also the observer's clock check: a query more than WAKE_GAP_S past a
        pending due records a wake gap, and a dormant answer withdraws the
        pending due (dormancy is not a wake gap).
        """

        perf = float(now_perf)
        self._check_wake(None if now_wall is None else float(now_wall), perf)
        wall = self._wall(perf, now_wall)
        if wall is None:
            return None
        due = self._compute_next_poll(perf, wall)
        if due is None:
            self._set_due(None)
        return due

    def _compute_next_poll(self, now_perf: float, wall: float) -> float | None:
        c = self._c
        last = self._last_poll_perf
        if self._last_failed_perf is not None and (last is None or self._last_failed_perf > last):
            last = self._last_failed_perf
        candidates: list[float] = []
        fast = False
        aged_due: float | None = None
        scale = self._poll_scale  # 1.0 unless the CPU budget stretches it
        aged_cutoff = c.t_age_s + 0.2
        if scale > 1.0:
            # The AGED poll's floor is stretched (to at most ``min_poll_scale``
            # fast periods): keep the candidate until that poll and one fast
            # period of lateness have passed, so it is never dropped unpolled.
            aged_floor_s = c.poll_fast_s * min(scale, c.min_poll_scale)
            aged_cutoff = max(aged_cutoff, c.aged_poll_offset_s + aged_floor_s + c.poll_fast_s)
        horizon = max(c.fast_window_s, aged_cutoff)
        evidence_floor = self._evidence_floor()
        for oid, conn in list(self._pending.items()):
            if conn.answered or conn.legacy or conn.stale or conn.origin is not OpenOrigin.USER:
                del self._pending[oid]
                continue
            age = wall - conn.t_open
            if age >= horizon:
                # Past both the fast window and the AGED poll: never schedules again.
                del self._pending[oid]
                continue
            if not self._eligible_leaf(conn.leaf) or conn.gateway or conn.t_open < evidence_floor:
                continue
            if age < c.fast_window_s:
                fast = True
            if not conn.aged and age < aged_cutoff:
                due = conn.t_open + c.aged_poll_offset_s
                if aged_due is None or due < aged_due:
                    aged_due = due
        now = float(now_perf)
        if fast:
            period = self._fast_period
            if scale > 1.0:
                period = max(period, c.poll_fast_s * scale)
            candidates.append(now if last is None else last + period)
        if aged_due is not None:
            due_perf = aged_due - (wall - now)
            if scale > 1.0:
                floor = now if last is None else last + aged_floor_s
            else:
                floor = now if last is None else last + c.poll_fast_s
            candidates.append(max(due_perf, floor))
        if scale > 1.0:
            # Lingering user traffic polls at the stretched 1 Hz period; a
            # demotion watch (§6, rare, act mode) keeps 1 Hz: its polls
            # decide which stuck connections to close.
            if self._linger_active(wall):
                candidates.append(now if last is None else last + c.poll_slow_s * scale)
            if self._watch_active(now):
                candidates.append(now if last is None else last + c.poll_slow_s)
        elif self._slow_active(wall, now):
            candidates.append(now if last is None else last + c.poll_slow_s)
        if not candidates:
            return None
        return min(candidates)

    def need_poll(self, now_perf: float, now_wall: float | None = None) -> bool:
        return self.next_poll_time(now_perf, now_wall) is not None

    def _slow_active(self, wall: float, now_perf: float) -> bool:
        """Whether 1 Hz polls go on: lingering user traffic or a demotion watch."""

        return self._linger_active(wall) or self._watch_active(now_perf)

    def _linger_active(self, wall: float) -> bool:
        c = self._c
        linger = c.linger_s
        stream_watch = c.stream_watch_s
        if self._poll_scale > 1.0:
            # Stretched 1 Hz polls linger at least as long as 1 Hz polls: the
            # last one lands less than one 1 Hz period before LINGER_S (and
            # STREAM_WATCH_S) runs out, or after it, never earlier.
            extra = c.poll_slow_s * (self._poll_scale - 1.0)
            linger += extra
            if stream_watch > 0:
                stream_watch += extra
        for conn in self._present.values():
            if conn.origin not in _LIVE_ORIGINS or conn.gateway:
                continue
            if conn.leaf is None or conn.leaf not in self._tier3 or conn.t_last_change is None:
                continue
            idle = wall - conn.t_last_change
            if idle <= linger:
                return True
            if (
                stream_watch > 0
                and conn.peak_rate >= c.stream_rate_bps
                and idle <= stream_watch
            ):
                return True
        return False

    def _watch_active(self, now_perf: float) -> bool:
        for leaf, watch in list(self._watches.items()):
            if now_perf >= watch.until_perf:
                del self._watches[leaf]
                continue
            if any(
                conn.leaf == leaf
                and conn.origin in _LIVE_ORIGINS
                and not conn.gateway
                for conn in self._present.values()
            ):
                return True
        return False

    # -- demotion watch and closes (§6) ------------------------------------

    def start_demotion_watch(self, leaf: str, *, t_demote_wall: float, now_perf: float) -> DemotionWatch:
        watch = DemotionWatch(
            leaf=leaf,
            t_demote_wall=float(t_demote_wall),
            until_perf=float(now_perf) + self._c.post_demotion_watch_s,
        )
        self._watches[leaf] = watch
        return watch

    def end_demotion_watch(self, leaf: str) -> None:
        self._watches.pop(leaf, None)

    def active_demotion_watches(self, now_perf: float) -> tuple[DemotionWatch, ...]:
        for leaf in [leaf for leaf, watch in self._watches.items() if now_perf >= watch.until_perf]:
            del self._watches[leaf]
        return tuple(self._watches.values())

    def latest_snapshot_age(self, now_perf: float) -> float | None:
        if self._last_poll_perf is None:
            return None
        return float(now_perf) - self._last_poll_perf

    def stuck_connection_ids(self, leaf: str, t_demote_wall: float, now_wall: float) -> StuckConnections:
        """Rules 1, 2, 4 and 5 of §6 over the latest snapshot, AGED first;
        with ``close_frozen_streams`` also rule 6 (frozen streams, last)."""

        c = self._c
        now = float(now_wall)
        aged: list[tuple[float, str]] = []
        waiting: list[tuple[float, str]] = []
        frozen: list[tuple[float, str]] = []
        frozen_on = bool(self.close_frozen_streams)
        kept_progressing = kept_not_stuck = kept_after = kept_other = 0
        for conn_id, conn in self._present.items():
            if conn.leaf != leaf:
                continue
            self._materialize(conn, self._seq)
            if conn.origin not in _LIVE_ORIGINS or conn.gateway or conn.closed:
                kept_other += 1
                continue
            if conn.t_open >= t_demote_wall:
                kept_after += 1
                continue
            age = now - conn.t_open
            if conn.t_last_down_inc is not None and now - conn.t_last_down_inc < c.progress_window_s:
                kept_progressing += 1
                continue
            if conn.n_samples <= 1 and not (conn.down == 0 and age >= 1.0):
                kept_progressing += 1
                continue
            if conn.up > 0 and conn.down == 0 and age >= c.t_age_s:
                aged.append((conn.t_open, conn_id))
                continue
            waited = self._waiting_for(conn, now)
            if waited is not None and waited >= c.t_wait_s:
                waiting.append((conn.t_open, conn_id))
                continue
            if (
                frozen_on
                and conn.down > 0
                and conn.peak_rate >= c.stream_rate_bps
                and conn.t_last_down_inc is not None
                and now - conn.t_last_down_inc >= c.stall_close_s
            ):
                # Rule 6: opened before the demotion, had downlink at stream
                # rate, and nothing new for STALL_CLOSE_S (the watch only
                # exists after a move, so this closes only on a move).
                frozen.append((conn.t_open, conn_id))
                continue
            kept_not_stuck += 1
        aged.sort()
        waiting.sort()
        frozen.sort()
        return StuckConnections(
            ids=tuple(conn_id for _, conn_id in aged)
            + tuple(conn_id for _, conn_id in waiting)
            + tuple(conn_id for _, conn_id in frozen),
            aged=len(aged),
            waiting=len(waiting),
            kept_progressing=kept_progressing,
            kept_not_stuck=kept_not_stuck,
            kept_after_demotion=kept_after,
            kept_other=kept_other,
            snapshot_wall=self._last_poll_wall,
            frozen=len(frozen),
        )

    def mark_closed(self, conn_ids: Iterable[str]) -> None:
        for conn_id in conn_ids:
            conn = self._by_id.get(conn_id)
            if conn is not None:
                conn.closed = True

    # -- output (§18.8) -----------------------------------------------------

    def drain_records(self) -> list[dict[str, Any]]:
        records, self._records = self._records, []
        return records

    @staticmethod
    def _fresh_metrics() -> defaultdict[str, int]:
        return defaultdict(int)

    def summary(self, now_wall: float, *, reset: bool = True) -> dict[str, Any]:
        """The periodic summary record (tokens and hashes only; here counters only)."""

        metrics = dict(self._metrics)
        candidates = (
            metrics.get("class_FAILED_FAST", 0)
            + metrics.get("class_UNANSWERED_SHORT", 0)
            + metrics.get("unknown", 0)
        )

        def rounded(value: float | None, scale: float = 1000.0) -> float | None:
            return None if value is None else round(value * scale, 1)

        record: dict[str, Any] = {
            "ev": "summary",
            "t": round(float(now_wall), 3),
            **{key: value for key, value in sorted(metrics.items())},
            "unknown_share": round(metrics.get("unknown", 0) / candidates, 4) if candidates else None,
            "poll_gap_p50_ms": rounded(_percentile(self._gaps, 0.5)),
            "poll_gap_p90_ms": rounded(_percentile(self._gaps, 0.9)),
            "poll_cost_p50_ms": rounded(_percentile(self._costs, 0.5)),
            "poll_cost_p90_ms": rounded(_percentile(self._costs, 0.9)),
            "poll_total_p50_ms": rounded(_percentile(self._total_costs, 0.5)),
            "poll_total_p90_ms": rounded(_percentile(self._total_costs, 0.9)),
            "reader_lag_p50_ms": rounded(_percentile(self._lags, 0.5)),
            "reader_lag_p90_ms": rounded(_percentile(self._lags, 0.9)),
            "drain_lag_p50_ms": rounded(_percentile(self._drain_lags, 0.5)),
            "drain_lag_p90_ms": rounded(_percentile(self._drain_lags, 0.9)),
            "tracked": len(self._tracked),
            "watches": len(self._watches),
        }
        if reset:
            self._metrics = self._fresh_metrics()
            self._gaps.clear()
            self._costs.clear()
            self._total_costs.clear()
            self._lags.clear()
            self._drain_lags.clear()
        return record


# ---------------------------------------------------------------------------
# CPU budget of the observer thread (V4 follow-up)


def _finite(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


class PostDialCpuThrottle:
    """The observer thread's CPU budget, enforced by stretching its poll periods.

    Pure and deterministic: the runner hands ``note`` its own thread's CPU
    seconds, and the part of them spent inside polls, with the perf time at
    the top of every loop pass.  This class starts no thread and arms no
    timer, and a dormant observer takes no reading, so dormancy is unchanged.
    ``budget_pct`` is percent of one core; 0 turns the stretch off (the
    readings still feed ``summary``).

    Shares are measured over a sliding window (``window_s``), interpolated
    between readings kept at least ``SPACING_S`` apart, so dormant time counts
    as idle.  Only the poll share shrinks with a stretch (polls come 1/scale
    as often); the rest (open lines, schedule passes) does not.

    Raise: above the budget the scale becomes the one that brings the poll
    share down to what the rest leaves of ``TARGET`` x budget, in one step,
    but never to below ``POLL_FLOOR`` x budget: a stretch beyond that moves
    the share by less than the gap between target and budget and would only
    discard evidence.  When no raise can help (the rest alone uses the
    target, or the largest stretch is reached) the scale holds and an
    ``observer_budget_unreachable`` record says so.  A raise is judged only
    on readings since the previous change, at least ``settle_s`` after it.

    Release: each full window's need (the scale that puts it at the target,
    from its poll share normalised to scale 1) is remembered for ``hold_s``.
    The scale drops to the largest need of that time once that is clearly
    lower (at most ``RELEASE / TARGET`` of the scale), and to 1 once no
    window needed a stretch, so a periodic load (page loads every 15-60 s)
    holds one stretch instead of engaging and releasing per burst.

    An engaged scale is at least ``scale_min`` (``min_poll_scale``: every
    stretched gap then exceeds POLL_GAP_MAX_S) and at most ``scale_max``.
    Each episode (scale 1 to above 1) is counted; the ``observer_throttled``
    and ``observer_budget_unreachable`` records (numbers only) are each
    returned at most once per ``record_interval_s``, and ``summary`` counts
    every episode, change and unreachable spell.
    """

    TARGET = 0.9
    RELEASE = 0.8
    POLL_FLOOR = 0.1
    SPACING_S = 0.25

    __slots__ = (
        "budget_pct",
        "window_s",
        "settle_s",
        "hold_s",
        "scale_max",
        "scale_min",
        "fast_period_s",
        "slow_period_s",
        "record_interval_s",
        "scale",
        "episodes_total",
        "_samples",
        "_needs",
        "_last",
        "_norm",
        "_changed_at",
        "_summary_from",
        "_episodes",
        "_changes",
        "_unreachable",
        "_stuck",
        "_peak",
        "_recorded",
    )

    def __init__(
        self,
        budget_pct: float = 0.0,
        *,
        window_s: float = 10.0,
        settle_s: float = 5.0,
        hold_s: float = 60.0,
        scale_max: float = 10.0,
        scale_min: float | None = None,
        fast_period_s: float = 0.1,
        slow_period_s: float = 1.0,
        record_interval_s: float = 300.0,
    ) -> None:
        self.budget_pct = max(0.0, _finite(budget_pct, 0.0))
        self.window_s = max(self.SPACING_S, _finite(window_s, 10.0))
        self.settle_s = min(self.window_s, max(self.SPACING_S, _finite(settle_s, 5.0)))
        self.hold_s = max(self.window_s, _finite(hold_s, 60.0))
        self.scale_max = max(1.0, _finite(scale_max, 10.0))
        default_min = DEFAULT_POST_DIAL_CONSTANTS.min_poll_scale
        smallest = _finite(default_min if scale_min is None else scale_min, default_min)
        self.scale_min = min(self.scale_max, max(1.0, smallest))
        self.fast_period_s = max(0.0, _finite(fast_period_s, 0.1))
        self.slow_period_s = max(0.0, _finite(slow_period_s, 1.0))
        self.record_interval_s = max(0.0, _finite(record_interval_s, 300.0))
        self.scale = 1.0
        self.episodes_total = 0
        # (perf, cpu, poll cpu, poll cpu normalised to scale 1), cumulative.
        self._samples: deque[tuple[float, float, float, float]] = deque()
        # (perf, need) of full windows, decreasing needs (a sliding maximum).
        self._needs: deque[tuple[float, float]] = deque()
        self._last: tuple[float, float, float] | None = None
        self._norm = 0.0
        self._changed_at: float | None = None
        self._summary_from: tuple[float, float, float] | None = None
        self._episodes = 0
        self._changes = 0
        self._unreachable = 0
        self._stuck = False
        self._peak = 1.0
        self._recorded: dict[str, float] = {}

    @classmethod
    def from_constants(
        cls,
        constants: PostDialConstants,
        *,
        budget_pct: float | None = None,
        record_interval_s: float = 300.0,
    ) -> "PostDialCpuThrottle":
        return cls(
            constants.cpu_budget_pct if budget_pct is None else budget_pct,
            window_s=constants.cpu_window_s,
            settle_s=constants.cpu_settle_s,
            hold_s=constants.cpu_hold_s,
            scale_max=constants.cpu_scale_max,
            scale_min=constants.min_poll_scale,
            fast_period_s=constants.poll_fast_s,
            slow_period_s=constants.poll_slow_s,
            record_interval_s=record_interval_s,
        )

    def __repr__(self) -> str:
        return f"PostDialCpuThrottle(budget_pct={self.budget_pct:g}, scale={self.scale:.3g})"

    @property
    def enabled(self) -> bool:
        return self.budget_pct > 0.0

    def disable(self) -> None:
        """No budget from now on (the CPU clock failed): back to scale 1."""

        self.budget_pct = 0.0
        self.scale = 1.0

    def note(self, perf: float, cpu: float, poll_cpu: float | None = None) -> dict[str, Any] | None:
        """One reading: ``cpu`` is the thread's CPU seconds and ``poll_cpu``
        the part of them spent in polls (None: all of it), both cumulative.

        Returns the ``observer_throttled`` record when an episode starts, or
        the ``observer_budget_unreachable`` record (the caller adds its wall
        time), each at most once per ``record_interval_s``.
        """

        perf = float(perf)
        cpu = float(cpu)
        poll = cpu if poll_cpu is None else float(poll_cpu)
        last = self._last
        if last is not None and (perf < last[0] or cpu < last[1] or poll < last[2]):
            # A clock went back: measure afresh from here, keeping the scale.
            self._samples.clear()
            self._needs.clear()
            self._recorded.clear()
            self._changed_at = None
            self._summary_from = None
            last = None
        if last is not None:
            # The poll CPU since the last reading was spent at the scale in force.
            self._norm += (poll - last[2]) * self.scale
        self._last = (perf, cpu, poll)
        if self._summary_from is None:
            self._summary_from = (perf, cpu, poll)
        samples = self._samples
        if samples and perf - samples[-1][0] < self.SPACING_S:
            return None
        samples.append((perf, cpu, poll, self._norm))
        if self._changed_at is None:
            self._changed_at = perf
        horizon = perf - self.window_s
        while len(samples) >= 2 and samples[1][0] <= horizon:
            samples.popleft()
        if not self.enabled:
            return None
        if samples[0][0] <= horizon:
            self._note_need(perf, horizon)
        if perf - self._changed_at < self.settle_s:
            return None
        # Every change time is a kept reading, and so is the newest reading
        # at or before the horizon: the interpolation always has both ends.
        return self._decide(perf, max(self._changed_at, horizon))

    def _shares(self, start: float, perf: float) -> tuple[float, float, float]:
        """Percent of one core over [start, perf]: all, polls, polls at scale 1."""

        cpu0, poll0, norm0 = self._at(start)
        _t, cpu1, poll1, norm1 = self._samples[-1]
        span = perf - start
        total = max(0.0, cpu1 - cpu0)
        polls = min(total, max(0.0, poll1 - poll0))
        return 100.0 * total / span, 100.0 * polls / span, 100.0 * max(0.0, norm1 - norm0) / span

    def _headroom(self, other: float) -> float:
        """The poll share a stretch aims for, beside ``other`` percent."""

        budget = self.budget_pct
        return max(self.TARGET * budget - other, self.POLL_FLOOR * budget)

    def _note_need(self, perf: float, start: float) -> None:
        total, polls, normalised = self._shares(start, perf)
        need = normalised / self._headroom(max(0.0, total - polls))
        needs = self._needs
        while needs and needs[-1][1] <= need:
            needs.pop()
        needs.append((perf, need))
        cutoff = perf - self.hold_s
        while needs[0][0] < cutoff:
            needs.popleft()

    def _decide(self, perf: float, since: float) -> dict[str, Any] | None:
        total, polls, _normalised = self._shares(since, perf)
        other = max(0.0, total - polls)
        scale = self.scale
        if total > self.budget_pct:
            need = scale * polls / self._headroom(other)
            new = min(self.scale_max, max(self.scale_min, need))
            # Over the budget with the headroom above the floor, the need is
            # always more than scale / TARGET; below that a raise could not
            # cut the share by more than the target's margin.
            if need * self.TARGET > scale and new > scale:
                self._stuck = False
                return self._change(perf, new, total, polls, perf - since)
            if self._stuck:
                return None
            # Stretching cannot bring the share under the budget.
            self._stuck = True
            self._unreachable += 1
            return self._record(
                "observer_budget_unreachable",
                perf,
                {
                    "budget_pct": round(self.budget_pct, 3),
                    "measured_pct": round(total, 3),
                    "poll_pct": round(polls, 3),
                    "other_pct": round(other, 3),
                    "window_s": round(perf - since, 3),
                    "scale": round(scale, 3),
                },
            )
        self._stuck = False
        if scale > 1.0 and self._needs:
            need = self._needs[0][1]
            if need <= scale * self.RELEASE / self.TARGET:
                if need >= self.scale_min:
                    new = need
                else:
                    new = 1.0 if need <= 1.0 else self.scale_min
                if new < scale:
                    return self._change(perf, new, total, polls, perf - since)
        return None

    def _change(
        self,
        perf: float,
        new: float,
        total: float,
        polls: float,
        span: float,
    ) -> dict[str, Any] | None:
        old = self.scale
        self.scale = new
        self._changed_at = perf
        self._changes += 1
        if new > self._peak:
            self._peak = new
        if old > 1.0 or new <= 1.0:
            return None
        self._episodes += 1
        self.episodes_total += 1
        return self._record(
            "observer_throttled",
            perf,
            {
                "budget_pct": round(self.budget_pct, 3),
                "measured_pct": round(total, 3),
                "poll_pct": round(polls, 3),
                "window_s": round(span, 3),
                "scale": round(new, 3),
                "period_ms": round(self.fast_period_s * new * 1000.0, 1),
                "slow_period_ms": round(self.slow_period_s * new * 1000.0, 1),
            },
        )

    def _record(self, kind: str, perf: float, fields: dict[str, Any]) -> dict[str, Any] | None:
        last = self._recorded.get(kind)
        if last is not None and perf - last < self.record_interval_s:
            return None  # counted in ``summary``
        self._recorded[kind] = perf
        return {"ev": kind, **fields}

    def _at(self, t: float) -> tuple[float, float, float]:
        previous: tuple[float, float, float, float] | None = None
        for sample in self._samples:
            if sample[0] >= t:
                if previous is None or sample[0] == t:
                    return sample[1], sample[2], sample[3]
                p0 = previous[0]
                frac = (t - p0) / (sample[0] - p0)
                return (
                    previous[1] + (sample[1] - previous[1]) * frac,
                    previous[2] + (sample[2] - previous[2]) * frac,
                    previous[3] + (sample[3] - previous[3]) * frac,
                )
            previous = sample
        if previous is None:
            return 0.0, 0.0, 0.0
        return previous[1], previous[2], previous[3]

    def summary(self, *, reset: bool = True) -> dict[str, Any]:
        """Shares, episodes, changes and scales since the previous summary (numbers only)."""

        begin, last = self._summary_from, self._last
        share: float | None = None
        poll_share: float | None = None
        if begin is not None and last is not None and last[0] > begin[0]:
            span = last[0] - begin[0]
            share = round(100.0 * max(0.0, last[1] - begin[1]) / span, 3)
            poll_share = round(100.0 * max(0.0, last[2] - begin[2]) / span, 3)
        record: dict[str, Any] = {
            "cpu_budget_pct": round(self.budget_pct, 3),
            "observer_cpu_pct": share,
            "observer_poll_cpu_pct": poll_share,
            "throttle_episodes": self._episodes,
            "throttle_changes": self._changes,
            "throttle_unreachable": self._unreachable,
            "throttle_scale": round(self.scale, 3),
            "throttle_scale_peak": round(self._peak, 3),
        }
        if reset:
            self._summary_from = last
            self._episodes = 0
            self._changes = 0
            self._unreachable = 0
            self._peak = self.scale
        return record


# ---------------------------------------------------------------------------
# Log-record helpers (§18.8): tokens and hashed leaves only


def trigger_record(
    trigger: Trigger | ShadowTrigger,
    tokenizer: PostDialTokenizer,
    *,
    mode: str,
) -> dict[str, Any]:
    """``trigger`` in act mode, ``would_trigger`` in shadow mode or for shadow rules."""

    is_act = isinstance(trigger, Trigger) and mode == "act"
    rule = trigger.kind if isinstance(trigger, Trigger) else trigger.rule
    record: dict[str, Any] = {
        "ev": "trigger" if is_act else "would_trigger",
        "t": round(trigger.t, 3),
        "rule": rule,
        "leaf": tokenizer.leaf(trigger.leaf),
        "toks": [short_token(token) for token in trigger.tokens],
        "n": trigger.n_events,
        "first_ev_ms": round((trigger.t - trigger.first_event_t) * 1000.0, 1),
        "episode": trigger.episode,
    }
    if trigger.ratio_failed is not None:
        record["ratio_failed"] = trigger.ratio_failed
        record["ratio_answered"] = trigger.ratio_answered
    if trigger.share_failed is not None:
        # The failed-share guard: its counts and whether it passed.  An act
        # rule logged ``would_trigger`` with ``share_ok`` false was held back.
        record["share_failed"] = trigger.share_failed
        record["share_answered"] = trigger.share_answered
        record["share_ok"] = bool(trigger.share_ok)
        record["onset_answered"] = trigger.onset_answered
        # The 0.5 guard and its onset variant on these counts, whatever the
        # setting, and the variants whose episode trigger this match is.
        if trigger.share_ok_at_0_5 is not None:
            record["share_ok_at_0_5"] = bool(trigger.share_ok_at_0_5)
        if trigger.onset_ok_at_0_5 is not None:
            record["onset_ok_at_0_5"] = bool(trigger.onset_ok_at_0_5)
        record["opens"] = list(trigger.opens)
    if isinstance(trigger, ShadowTrigger) and trigger.variant_only:
        record["variant_only"] = True
    return record


def gated_record(
    trigger: Trigger,
    tokenizer: PostDialTokenizer,
    *,
    reason: str,
    now_wall: float,
    arm_check: str | None = None,
) -> dict[str, Any]:
    record = {
        "ev": "gated",
        "t": round(float(now_wall), 3),
        "rule": trigger.kind,
        "leaf": tokenizer.leaf(trigger.leaf),
        "reason": str(reason),
        "episode": int(getattr(trigger, "episode", 0) or 0),
    }

    if arm_check is not None:
        record["arm_check"] = str(arm_check)
    return record


def activity_record(active: bool, *, now_wall: float, tracked: int = 0) -> dict[str, Any]:
    return {
        "ev": "active" if active else "dormant",
        "t": round(float(now_wall), 3),
        "tracked": int(tracked),
    }


def constants_with(base: PostDialConstants = DEFAULT_POST_DIAL_CONSTANTS, **changes: Any) -> PostDialConstants:
    """``dataclasses.replace`` for callers that must not import dataclasses."""

    return replace(base, **changes)
