from __future__ import annotations

import bisect
import importlib.util
import json
import random
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import pytest

from runtime_post_dial import (
    DEFAULT_POST_DIAL_CONSTANTS,
    RULE_AGED,
    RULE_AGED_SINGLE,
    RULE_FAST,
    RULE_FAST_SPREAD,
    RULE_SLOW,
    RULE_WAITING,
    VARIANT_ONSET,
    VARIANT_RULES,
    VARIANT_SHARE,
    OpenOrigin,
    OriginRules,
    PostDialCpuThrottle,
    PostDialObserverCore,
    PostDialOpen,
    PostDialTokenizer,
    RawBodyError,
    ShadowTrigger,
    Trigger,
    constants_with,
    decode_connections_body,
    go_unquote,
    gated_record,
    parse_open_line,
    parse_rfc3339,
    split_using,
    system_rule_hosts,
    system_rule_log_form,
    trigger_record,
)
from runtime_post_dial import _raw_layout


def _load_replay():
    path = Path(__file__).resolve().parent / "fixtures" / "post_dial" / "replay.py"
    spec = importlib.util.spec_from_file_location("post_dial_replay", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


replay = _load_replay()
TOKENIZER = replay.TOKENIZER
T0 = 1_790_000_000.0
# perf == wall, and perf on its own epoch (perf_counter starts near zero).
PERF_OFFSETS = (0.0, T0 - 12_345.678)
SYSTEM_RULES = (
    "DOMAIN,sys.test,FINAL-EXIT",
    "IP-CIDR,127.0.0.2/32,FINAL-EXIT,no-resolve",
    "IP-CIDR6,2001:db8::1/128,FINAL-EXIT,no-resolve",
)
RULES = OriginRules.from_system_rules(SYSTEM_RULES)
# The rules alone: the failed-share guard off (``trigger_min_failed_share: 0``,
# the default since 10-01).
UNGUARDED = constants_with(trigger_min_failed_share=0.0)
# The failed-share guard at 0.5, which has to be set explicitly.
GUARDED = constants_with(trigger_min_failed_share=0.5)


def _parse(raw: str, rules: OriginRules = RULES) -> PostDialOpen | None:
    return parse_open_line(raw, 12.5, tokenizer=TOKENIZER, origin_rules=rules)


# ---------------------------------------------------------------------------
# Raw-bytes ingest in lockstep with the classic one (V4 cost fix)
#
# Every test below that builds its core with ``_core()`` runs twice: once on
# the classic core, and once on ``DualCore``, which feeds each snapshot to the
# classic core as a dict and to a second core as Go-compact JSON bytes
# (``ingest_raw``), and requires equal results from every call and equal
# state after every poll.

_CONN_FIELDS = (
    "oid", "t_open", "key", "token", "port", "leaf", "origin", "legacy", "conn_id",
    "gateway", "answered", "up", "down", "up_at_down", "n_samples", "last_seen",
    "t_last_down_inc", "t_up_after_down", "t_stream_rate", "t_last_change", "aged",
    "waiting_logged_at", "waiting_trigger_at", "closed", "gone_t", "stale", "t_ingest",
    "chains", "waiting_eval_at", "down_fell",
)
_CORE_FIELDS = (
    "_last_poll_wall", "_last_poll_perf", "_last_failed_perf", "_due_after_poll_perf",
    "_due_after_poll_wall", "_offset", "_last_wall_seen", "_fast_period", "_epoch_floor",
    "_wake_floor", "_wake_since_digest", "_answered_since_digest", "_evidence_since_digest",
    "_oid", "_answered_open_max", "_last_match", "_episodes", "_acted", "_variant_acted", "_watches",
)


def _conn_state(conn: Any, threshold: float) -> tuple[Any, ...]:
    # ``peak_rate`` is read only against STREAM_RATE_BPS; a lazy replay does
    # not track the sub-threshold values in between.
    return tuple(getattr(conn, name) for name in _CONN_FIELDS) + (
        list(conn.rates),
        conn.peak_rate >= threshold,
    )


def _core_state(core: PostDialObserverCore) -> dict[str, Any]:
    threshold = core.constants.stream_rate_bps
    metrics = {key: value for key, value in core._metrics.items() if value}
    return {
        "tracked": [(oid, _conn_state(conn, threshold)) for oid, conn in core._tracked.items()],
        "present": {cid: _conn_state(conn, threshold) for cid, conn in core._present.items()},
        "pending": sorted(core._pending),
        "unbound": {key: [conn.oid for conn in conns] for key, conns in core._unbound.items()},
        "by_id": {cid: conn.oid for cid, conn in core._by_id.items()},
        "legacy_gone": {
            key: [_conn_state(conn, threshold) for conn in conns] for key, conns in core._legacy_gone.items()
        },
        "events": {
            leaf: [(e.t, e.kind, e.token, e.oid, e.t_open, e.port) for e in events]
            for leaf, events in core._events.items()
        },
        "answered": {leaf: list(times) for leaf, times in core._answered.items()},
        "new_event_leaves": sorted(core._new_event_leaves),
        "open_order": list(core._open_order),
        "poll_walls": list(core._poll_walls),
        "records": list(core._records),
        "metrics": metrics,
        "series": [list(core._gaps), list(core._costs), list(core._total_costs), list(core._lags), list(core._drain_lags)],
        **{name: getattr(core, name) for name in _CORE_FIELDS},
    }


def _assert_same_state(classic: PostDialObserverCore, raw: PostDialObserverCore) -> None:
    # Bring every lazy entry up to date for the comparison only, then put the
    # lazy state back, so the raw core keeps running exactly as it would.
    saved = []
    for conn in raw._present.values():
        saved.append((conn, conn.n_samples, conn.last_seen, list(conn.rates), conn.tick_seq))
        raw._materialize(conn, raw._seq)
    try:
        expected, got = _core_state(classic), _core_state(raw)
    finally:
        for conn, n_samples, last_seen, rates, tick_seq in saved:
            conn.n_samples, conn.last_seen, conn.tick_seq = n_samples, last_seen, tick_seq
            conn.rates.clear()
            conn.rates.extend(rates)
    if expected != got:
        for key in expected:
            assert got[key] == expected[key], key
    assert "lazy_history_miss" not in raw._metrics


def _go_json(payload: Any, *, ascii_only: bool = False) -> bytes:
    """Mihomo's framing: Go ``encoding/json`` compact output plus a newline."""

    return json.dumps(payload, separators=(",", ":"), ensure_ascii=ascii_only).encode("utf-8") + b"\n"


class DualCore:
    """The classic core (dict snapshots) and the raw-bytes core in lockstep."""

    def __init__(self, *, style: str = "compact", deep_every: int = 1, seed: int = 7, **kwargs: Any) -> None:
        self.classic = PostDialObserverCore(**kwargs)
        self.raw = PostDialObserverCore(**kwargs)
        self.style = style
        self.deep_every = max(1, int(deep_every))
        self._rng = random.Random(seed)
        self.polls = 0

    def __repr__(self) -> str:
        return f"DualCore({self.classic!r})"

    @property
    def tokenizer(self) -> PostDialTokenizer:
        return self.classic.tokenizer

    @property
    def constants(self):
        return self.classic.constants

    def ingest_snapshot(
        self,
        payload: Any,
        t_req_wall: float,
        t_req_perf: float,
        *,
        cost_s: float | None = None,
        style: str | None = None,
    ):
        style = style or self.style
        if style == "shuffled" and isinstance(payload, dict) and isinstance(payload.get("connections"), list):
            items = list(payload["connections"])
            self._rng.shuffle(items)  # Mihomo lists its tracker map in no fixed order
            payload = {**payload, "connections": items}
        expected = self.classic.ingest_snapshot(payload, t_req_wall, t_req_perf, cost_s=cost_s)
        if isinstance(payload, dict) and style != "classic":
            if style == "pretty":  # not Mihomo's framing: the raw core parses it as JSON
                body = json.dumps(payload, indent=1).encode("utf-8")
            else:
                body = _go_json(payload, ascii_only=style == "ascii")
            got = self.raw.ingest_raw(body, t_req_wall, t_req_perf, cost_s=cost_s)
        else:
            got = self.raw.ingest_snapshot(payload, t_req_wall, t_req_perf, cost_s=cost_s)
        assert got == expected
        self.polls += 1
        if self.polls % self.deep_every == 0:
            _assert_same_state(self.classic, self.raw)
        return expected

    def check_state(self) -> None:
        _assert_same_state(self.classic, self.raw)

    def __getattr__(self, name: str) -> Any:
        first = getattr(self.classic, name)
        if not callable(first):
            return first
        second = getattr(self.raw, name)

        def both(*args: Any, **kwargs: Any) -> Any:
            result = first(*args, **kwargs)
            assert second(*args, **kwargs) == result, name
            return result

        return both


_CORE_MODE = {"mode": "classic"}


@pytest.fixture(autouse=True, params=["classic", "raw"])
def core_mode(request):
    _CORE_MODE["mode"] = request.param
    yield request.param
    _CORE_MODE["mode"] = "classic"


def _core(*, deep_every: int = 1, **kwargs) -> PostDialObserverCore:
    kwargs.setdefault("tier3_members", ("n1", "n2", "n3"))
    if _CORE_MODE["mode"] == "raw":
        return DualCore(tokenizer=TOKENIZER, deep_every=deep_every, **kwargs)  # type: ignore[return-value]
    return PostDialObserverCore(tokenizer=TOKENIZER, **kwargs)


class Scenario:
    """Ground-truth connections plus their open lines for ``replay.drive``."""

    def __init__(self) -> None:
        self.mihomo = replay.FakeMihomo()
        self.feed: list[tuple[float, str]] = []
        self._sport = 40000

    def conn(
        self,
        t: float,
        *,
        host: str,
        leaf: str = "n1",
        port: int = 443,
        life: float | None = None,
        up: int = 517,
        up_at: float = 0.001,
        down: int = 0,
        down_at: float | None = None,
        rule: str | None = "Match",
        outer: str = "GENERAL",
        line: bool = True,
        line_lag: float = 0.001,
        up_fn=None,
        down_fn=None,
    ) -> replay.FakeConn:
        self._sport += 1
        sport = self._sport
        conn = replay.FakeConn(
            conn_id=f"id-{sport}",
            t_open=t,
            sport=sport,
            host=host,
            port=port,
            leaf=leaf,
            t_close=None if life is None else t + life,
            up=up_fn or (replay.step(t + up_at, up) if up else (lambda _t: 0)),
            down=down_fn or (replay.step(t + down_at, down) if down_at is not None else (lambda _t: 0)),
        )
        self.mihomo.add(conn)
        if line:
            raw = replay.open_line(t, sport=sport, host=host, port=port, leaf=leaf, outer=outer, rule=rule)
            self.feed.append((t + line_lag, raw))
        return conn

    def run(self, core: PostDialObserverCore, *, t_end: float, t_start: float = T0, **kwargs):
        return replay.drive(core, self.feed, self.mihomo, t_start=t_start, t_end=t_end, rules=RULES, **kwargs)


# ---------------------------------------------------------------------------
# Parsing (§18.3; Conf-2..5)


def test_parse_user_open_line_tokenises_destination_and_keeps_port() -> None:
    raw = replay.open_line(T0, sport=50929, host="Go.Dev", port=443)
    record = _parse(raw)

    assert record is not None
    assert record.t_open == pytest.approx(T0, abs=1e-6)
    assert record.received_at_monotonic == 12.5
    assert record.key == ("127.0.0.1", 50929)
    assert record.port == 443
    assert record.leaf == "n1"
    assert record.outer == "GENERAL"
    assert record.origin is OpenOrigin.USER
    assert record.token == TOKENIZER.destination("go.dev", 443)
    assert len(record.token) == 32
    assert "go.dev" not in repr(record).lower()
    assert "n1" not in repr(record)


def test_parse_real_names_cjk_emoji_zwj_quotes_tab_and_parentheses() -> None:
    lines = replay.load_fixture("conformance_lines.json.gz")["lines"]
    leaves = {}
    for raw in lines:
        record = _parse(raw)
        if record is not None:
            leaves.setdefault(record.leaf, record)

    for name in (
        "HK 01 [IPLC] x2",
        "🇭🇰 香港 02",
        "👨‍💻 dev ZWJ",
        'q"uote\\back\tTab',
        "x/y 50% #frag ?q (p)",
    ):
        assert name in leaves, name
    process_line = next(raw for raw in lines if "(c u r l (x).exe)" in raw)
    record = _parse(process_line)
    assert record is not None and record.leaf == "HK 01 [IPLC] x2"
    assert record.origin is OpenOrigin.USER


def test_conformance_corpus_parses_every_client_tcp_line() -> None:
    lines = replay.load_fixture("conformance_lines.json.gz")["lines"]
    assert len(lines) >= 698
    failures = []
    inner = 0
    for raw in lines:
        record = _parse(raw)
        if record is None:
            if 'msg="[TCP] mihomo --> ' in raw:
                inner += 1
                continue
            failures.append(raw)
    assert failures == []
    assert inner >= 1


def test_origin_classification_follows_system_rules_and_modes() -> None:
    lines = replay.load_fixture("conformance_lines.json.gz")["lines"]

    def origin_of(fragment: str) -> OpenOrigin | None:
        raw = next(raw for raw in lines if fragment in raw)
        record = _parse(raw)
        return None if record is None else record.origin

    assert origin_of("match Domain(sys.test) using FINAL-EXIT") is OpenOrigin.APP
    assert origin_of("match IPCIDR(127.0.0.2/32) using") is OpenOrigin.APP
    assert origin_of("match IPCIDR(2001:db8::1/128) using") is OpenOrigin.APP
    assert origin_of("match DomainSuffix(suffix.test) using GENERAL") is OpenOrigin.USER
    assert origin_of("match Domain(direct.test) using DIRECT") is OpenOrigin.IGNORED
    assert origin_of("match Domain(reject.test) using REJECT") is OpenOrigin.IGNORED
    # Private source listeners: no match, FINAL-EXIT[leaf].
    assert origin_of("--> conf.test:80 using FINAL-EXIT[") is OpenOrigin.APP
    # GLOBAL mode: user traffic unless the host is a system-rule host.
    assert origin_of("--> conf.test:80 using GLOBAL") is OpenOrigin.USER
    assert origin_of("--> sys.test:80 using GLOBAL") is OpenOrigin.APP
    assert origin_of("[TCP] mihomo --> conf.test:80 using GENERAL[REJECT]") is None
    global_record = _parse(next(raw for raw in lines if "--> conf.test:80 using GLOBAL" in raw))
    assert global_record is not None and global_record.leaf is None and global_record.outer == "GLOBAL"
    reject_leaf = replay.open_line(T0, sport=1, host="a.test", leaf="REJECT")
    assert _parse(reject_leaf).origin is OpenOrigin.IGNORED


def test_ipv6_source_and_destination() -> None:
    raw = replay.open_line(T0, sport=5000, host="2001:db8::7", port=8443, source_ip="::1")
    record = _parse(raw)

    assert record is not None
    assert record.key == ("::1", 5000)
    assert record.port == 8443
    assert record.token == TOKENIZER.destination("2001:db8::7", 8443)


def test_parser_rejects_non_open_lines() -> None:
    open_raw = replay.open_line(T0, sport=10, host="a.test")
    assert _parse(open_raw.replace("level=info", "level=debug")) is None
    assert _parse(open_raw.replace("[TCP]", "[UDP]")) is None
    assert _parse(open_raw.replace('time="', 'time="x')) is None
    assert _parse(
        'time="2026-09-26T07:39:17.458945500-04:00" level=warning msg="[TCP] dial GENERAL '
        '(match Match/) 127.0.0.1:50138 --> api.github.com:443 error: context deadline exceeded"'
    ) is None
    assert _parse('time="2026-09-26T07:39:17.4-04:00" level=info msg="Start initial configuration"') is None
    assert _parse("") is None
    assert _parse(open_raw[:-1]) is None  # unterminated quoted value


def test_go_unquote_and_rfc3339() -> None:
    assert go_unquote(r"q\"uote\\back\tTab") == 'q"uote\\back\tTab'
    assert go_unquote(r"a‍b") == "a‍b"
    assert go_unquote(r"\U0001F468") == "\U0001F468"
    assert go_unquote(r"\xe9\x99\x88") == "陈"
    assert go_unquote(r"\101") == "A"
    assert go_unquote("bad\\") is None
    assert go_unquote(r"\q") is None
    assert parse_rfc3339("2026-09-26T11:27:27.625896000-04:00") == pytest.approx(1790436447.625896, abs=1e-6)
    assert parse_rfc3339("2026-09-26T15:27:27Z") == pytest.approx(1790436447.0)
    assert parse_rfc3339("2026-09-26T11:27:27.6258960-04:00") == pytest.approx(1790436447.625896, abs=1e-6)
    assert parse_rfc3339("2026-13-26T11:27:27Z") is None
    assert parse_rfc3339(None) is None


def test_split_using_takes_first_and_last_bracket() -> None:
    assert split_using("GENERAL[HK 01 [IPLC] x2]") == ("GENERAL", "HK 01 [IPLC] x2")
    assert split_using("GLOBAL") == ("GLOBAL", None)
    assert split_using("DIRECT") == ("DIRECT", None)


def test_system_rule_forms_match_the_formatter_rules() -> None:
    from clash_formatter import build_final_exit_system_rules

    rules = build_final_exit_system_rules(["https://1.2.3.4/x", "https://[2001:db8::9]:8443/y"])
    forms = {system_rule_log_form(rule) for rule in rules}

    assert "IPCIDR(1.2.3.4/32)" in forms
    assert "IPCIDR(2001:db8::9/128)" in forms
    assert all(form and "no-resolve" not in form for form in forms)
    domain_forms = [form for form in forms if form.startswith("Domain(")]
    assert domain_forms
    hosts = system_rule_hosts(rules)
    assert "1.2.3.4" in hosts and "2001:db8::9" in hosts
    assert system_rule_log_form("MATCH,GENERAL") is None
    assert system_rule_log_form("garbage") is None


def test_privacy_of_parser_objects() -> None:
    rules = OriginRules.from_system_rules(["DOMAIN,secret-system.example,FINAL-EXIT"])
    assert "secret-system" not in repr(rules)
    assert "key" in repr(TOKENIZER) and bytes(range(32)).hex() not in repr(TOKENIZER)
    with pytest.raises(ValueError):
        PostDialTokenizer(b"short")


# ---------------------------------------------------------------------------
# Observer scenarios (fake clock; §15 PF rows expressible purely)


@pytest.mark.parametrize("perf_offset", PERF_OFFSETS)
def test_pf1_fast_zombie_triggers_fast_within_the_first_failed_attempt(perf_offset: float) -> None:
    scenario = Scenario()
    scenario.conn(T0 + 0.00, host="www.python.org", life=2.0, up=900, down=5000, down_at=0.3)
    first_fail = T0 + 1.0
    for index, life in enumerate((0.006, 0.012, 0.030, 0.045, 0.060, 0.008)):
        scenario.conn(first_fail - 0.006 + index * 0.02, host=f"z{index % 2}.go.dev", life=life)
    core = _core()
    result = scenario.run(core, t_end=T0 + 6.0, perf_offset=perf_offset)

    fast = [trigger for trigger in result.triggers if trigger.kind == RULE_FAST]
    assert fast, "FAST must fire"
    assert fast[0].leaf == "n1"
    assert fast[0].new_episode is True
    assert fast[0].t - first_fail <= 0.25
    assert set(fast[0].tokens) <= {TOKENIZER.destination(f"z{i}.go.dev", 443) for i in range(2)}
    assert fast[0].ratio_failed >= 2
    assert all(not trigger.new_episode for trigger in fast[1:])
    assert not [trigger for trigger in result.triggers if trigger.kind == RULE_AGED]


@pytest.mark.parametrize("perf_offset", PERF_OFFSETS)
def test_pf4_black_hole_triggers_aged_with_one_exact_poll(perf_offset: float) -> None:
    scenario = Scenario()
    opens = (T0 + 1.00, T0 + 1.05, T0 + 1.40)
    for t in opens:
        scenario.conn(t, host="www.kernel.org", life=None)
    core = _core()
    result = scenario.run(core, t_end=T0 + 5.5, perf_offset=perf_offset)

    aged = [trigger for trigger in result.triggers if trigger.kind == RULE_AGED]
    assert aged and aged[0].leaf == "n1"
    assert opens[1] + 3.0 <= aged[0].t <= opens[1] + 3.02 + 0.11
    assert any(opens[0] + 3.02 - 1e-6 <= t <= opens[0] + 3.05 for t in result.polls)
    # 100 ms polling ends with the fast window; the AGED poll is exact.
    gap_polls = [t for t in result.polls if opens[2] + 2.3 < t < opens[0] + 3.0]
    assert gap_polls == []


def test_aged_needs_an_upload_so_idle_preopened_sockets_are_no_evidence() -> None:
    # §3.2: AGED is listed with 0 bytes down AND upload > 0 at age >= 3 s.
    # Pre-opened sockets that never sent a byte are not a black hole.
    def run(up: int):
        scenario = Scenario()
        for t in (T0 + 1.00, T0 + 1.05):
            scenario.conn(t, host="preopen.example", life=None, up=up)
        return scenario.run(_core(), t_end=T0 + 6.0)

    idle = run(0)
    assert [trigger for trigger in idle.triggers if trigger.kind == RULE_AGED] == []
    assert RULE_AGED_SINGLE not in [shadow.rule for shadow in idle.shadow]
    assert not [r for r in idle.records if r.get("ev") == "outcome" and r.get("class") == "AGED"]
    sent = run(517)
    assert [trigger for trigger in sent.triggers if trigger.kind == RULE_AGED]
    assert [r for r in sent.records if r.get("ev") == "outcome" and r.get("class") == "AGED"]


def test_healthy_browsing_never_triggers_and_goes_dormant() -> None:
    scenario = Scenario()
    for index in range(60):
        t = T0 + index * 0.37
        scenario.conn(t, host=f"site{index % 9}.example", life=1.2 + (index % 5) * 0.4, up=800, down=4200, down_at=0.25)
    # Isolated aborted handshakes (F18 shape) are not a FAST pair.
    scenario.conn(T0 + 3.0, host="abort.example", life=0.3)
    scenario.conn(T0 + 9.0, host="abort2.example", life=0.3)
    core = _core()
    result = scenario.run(core, t_end=T0 + 60.0)

    assert result.triggers == []
    assert [s for s in result.shadow if s.rule in {RULE_FAST_SPREAD, RULE_SLOW}] == []
    last_activity = T0 + 59 * 0.37 + 3.0
    assert max(result.polls) <= last_activity + DEFAULT_POST_DIAL_CONSTANTS.linger_s + 1.1
    assert core.need_poll(T0 + 60.0, T0 + 60.0) is False


def test_pf13_refuse_produces_no_opens_no_polls_and_no_trigger() -> None:
    core = _core()
    scenario = Scenario()
    result = scenario.run(core, t_end=T0 + 30.0)

    assert result.polls == []
    assert result.triggers == []
    assert core.next_poll_time(T0, T0) is None


def test_single_or_spaced_failures_do_not_trigger_fast() -> None:
    scenario = Scenario()
    scenario.conn(T0 + 1.0, host="a.example", life=0.01)
    scenario.conn(T0 + 2.5, host="b.example", life=0.01)
    result = scenario.run(_core(), t_end=T0 + 8.0)

    assert [t for t in result.triggers if t.kind == RULE_FAST] == []


def test_poll_gap_guard_turns_unbracketed_failures_into_unknown() -> None:
    core = _core()
    mihomo = replay.FakeMihomo()
    lines = []
    for index in range(3):
        t = T0 + index * 0.01
        mihomo.add(replay.FakeConn(f"g{index}", t, 41000 + index, "g.example", 443, "n1", t_close=t + 0.02))
        lines.append((t, replay.open_line(t, sport=41000 + index, host="g.example")))
    for t, raw in lines:
        core.ingest_open(_parse(raw), now_wall=t, now_perf=t)
    # One poll only 0.4 s after the opens: the ends are not bracketed.
    core.ingest_snapshot(mihomo.snapshot(T0 + 0.4), T0 + 0.4, T0 + 0.4, cost_s=0.004)
    evaluation = core.evaluate_triggers(T0 + 0.4)
    records = core.drain_records()

    assert evaluation.triggers == ()
    outcomes = [record for record in records if record["ev"] == "outcome"]
    assert {record["class"] for record in outcomes} == {"UNKNOWN"}
    assert {record["would"] for record in outcomes} == {"FAILED_FAST"}
    assert all(record["bracket_ok"] is False for record in outcomes)
    assert core.summary(T0 + 1.0)["unknown_share"] == 1.0


def test_app_origin_and_non_tier3_opens_never_schedule_polls() -> None:
    scenario = Scenario()
    for index in range(4):
        scenario.conn(T0 + index * 0.01, host="source.example", outer="FINAL-EXIT", rule=None, life=0.005)
        scenario.conn(T0 + index * 0.01, host="sys.test", outer="FINAL-EXIT", rule="Domain(sys.test)", life=0.005)
        scenario.conn(T0 + index * 0.01, host="tier1.example", leaf="tier1-node", life=0.005)
    core = _core()
    result = scenario.run(core, t_end=T0 + 5.0)

    assert result.polls == []
    assert result.triggers == []


def test_gateway_chain_is_never_evidence() -> None:
    def run(gateway_groups: tuple[str, ...]):
        mihomo = replay.FakeMihomo()
        lines = []
        for index in range(3):
            t = T0 + index * 0.05
            conn = mihomo.add(replay.FakeConn(f"s{index}", t, 42000 + index, "gw.example", 443, "n1", t_close=t + 0.25))
            conn.chains_tail = ("GATEWAY-GROUP", "GENERAL")
            lines.append((t + 0.001, replay.open_line(t, sport=42000 + index, host="gw.example")))
        core = _core(gateway_groups=gateway_groups)
        return replay.drive(core, lines, mihomo, t_start=T0, t_end=T0 + 3.0, rules=RULES)

    assert run(()).triggers  # control: the same failures count without the Gateway group
    gated = run(("GATEWAY-GROUP",))
    assert gated.triggers == []
    assert [r for r in gated.records if r["ev"] == "outcome"] == []


def test_network_epoch_discards_evidence_for_the_quiet_period() -> None:
    def zombie_burst(scenario: Scenario, at: float) -> None:
        for index in range(3):
            scenario.conn(at + index * 0.02, host=f"e{index}.example", life=0.01)

    early = Scenario()
    zombie_burst(early, T0 + 5.0)
    core = _core()
    core.note_network_epoch(T0)
    assert early.run(core, t_end=T0 + 8.0).triggers == []

    late = Scenario()
    zombie_burst(late, T0 + 16.0)
    core = _core()
    core.note_network_epoch(T0)
    assert late.run(core, t_end=T0 + 19.0).triggers


def test_wake_gap_discards_pre_gap_evidence() -> None:
    core = _core()
    mihomo = replay.FakeMihomo()
    t_open = T0
    for index in range(2):
        mihomo.add(replay.FakeConn(f"w{index}", t_open + index * 0.01, 43000 + index, "w.example", 443, "n1", up=replay.step(t_open, 517)))
        core.ingest_open(
            _parse(replay.open_line(t_open + index * 0.01, sport=43000 + index, host="w.example")),
            now_wall=t_open + 0.02,
            now_perf=t_open + 0.02,
        )
    core.ingest_snapshot(mihomo.snapshot(T0 + 0.1), T0 + 0.1, T0 + 0.1)
    # The machine slept: the next poll arrives 40 s after it was due.
    digest = core.ingest_snapshot(mihomo.snapshot(T0 + 40.2), T0 + 40.2, T0 + 40.2)
    evaluation = core.evaluate_triggers(T0 + 40.2)

    assert digest.wake_gap is True
    assert evaluation.triggers == ()
    assert [r for r in core.drain_records() if r["ev"] == "outcome"] == []


@pytest.mark.parametrize("perf_offset", PERF_OFFSETS)
@pytest.mark.parametrize("failure", ["black_hole", "zombie"])
def test_first_use_after_idle_with_a_keep_alive_open_is_not_a_wake_gap(failure: str, perf_offset: float) -> None:
    # The last linger poll ran with the keep-alive present, so it left a due
    # behind; dormancy must withdraw it, or the first poll after >30 s idle
    # looks like a wake gap and discards the very opens that woke the observer.
    scenario = Scenario()
    scenario.conn(T0, host="keepalive.example", life=None, up=900, down=5000, down_at=0.2)
    t_bad = T0 + 60.0
    if failure == "black_hole":
        scenario.conn(t_bad, host="hole.example", life=None)
        scenario.conn(t_bad + 0.004, host="hole.example", life=None)
    else:
        for index in range(3):
            scenario.conn(t_bad + index * 0.02, host=f"z{index}.example", life=0.01)
    result = scenario.run(_core(), t_end=t_bad + 6.0, perf_offset=perf_offset)

    assert [t for t in result.polls if T0 + 7.0 < t < t_bad] == []  # dormant: 0 calls while idle
    assert [record for record in result.records if record["ev"] == "wake_gap"] == []
    assert result.triggers
    first = result.triggers[0]
    if failure == "black_hole":
        assert first.kind == RULE_AGED
        assert t_bad + 3.0 <= first.t <= t_bad + 3.02 + 0.11
    else:
        assert first.kind == RULE_FAST
        assert first.t - t_bad <= 0.25


def _stall_lines(mihomo, opens):
    lines = []
    for sport, t, host in opens:
        mihomo.add(replay.FakeConn(f"s{sport}", t, sport, host, 443, "n1", up=replay.step(t, 517)))
        lines.append((t + 0.001, replay.open_line(t, sport=sport, host=host)))
    return lines


def test_a_late_poll_is_a_wake_gap_that_keeps_the_opens_that_woke_the_observer() -> None:
    core = _core()
    mihomo = replay.FakeMihomo()
    before = _stall_lines(mihomo, [(43100, T0, "before.example"), (43101, T0 + 0.01, "before.example")])
    after = _stall_lines(mihomo, [(43102, T0 + 40.0, "after.example"), (43103, T0 + 40.004, "after.example")])
    early = replay.drive(core, before, mihomo, t_start=T0, t_end=T0 + 0.15, rules=RULES)
    assert early.polls  # the fast window's next poll is pending when the machine sleeps
    # Resumed 40 s later (perf counted the sleep): new opens wake the loop.
    late = replay.drive(core, after, mihomo, t_start=T0 + 40.0, t_end=T0 + 44.0, rules=RULES)

    wake = [record for record in late.records if record["ev"] == "wake_gap"]
    assert len(wake) == 1 and wake[0]["t"] < T0 + 1.0
    aged = [trigger for trigger in late.triggers if trigger.kind == RULE_AGED]
    assert aged and aged[0].tokens == (TOKENIZER.destination("after.example", 443),)
    assert aged[0].t <= T0 + 40.004 + 3.02 + 0.11


def test_a_wall_clock_jump_is_a_wake_gap_that_keeps_later_opens() -> None:
    core = _core()
    mihomo = replay.FakeMihomo()
    before = _stall_lines(mihomo, [(43200, T0, "before.example"), (43201, T0 + 0.01, "before.example")])
    after = _stall_lines(mihomo, [(43202, T0 + 40.0, "after.example"), (43203, T0 + 40.004, "after.example")])
    replay.drive(core, before, mihomo, t_start=T0, t_end=T0 + 0.15, rules=RULES)
    # Sleep on a platform whose perf counter pauses: 40 s of wall, 0.2 s of perf.
    late = replay.drive(core, after, mihomo, t_start=T0 + 40.0, t_end=T0 + 44.0, rules=RULES, perf_offset=39.8)

    wake = [record for record in late.records if record["ev"] == "wake_gap"]
    assert len(wake) == 1 and wake[0]["t"] < T0 + 1.0
    aged = [trigger for trigger in late.triggers if trigger.kind == RULE_AGED]
    assert aged and aged[0].tokens == (TOKENIZER.destination("after.example", 443),)


def test_a_failed_poll_moves_the_schedule_by_one_period() -> None:
    core = _core()
    mihomo = replay.FakeMihomo()
    lines = _stall_lines(mihomo, [(49000, T0, "err.example")])
    core.ingest_open(_parse(lines[0][1]), now_wall=lines[0][0], now_perf=lines[0][0])
    core.ingest_snapshot(mihomo.snapshot(T0 + 0.01), T0 + 0.01, T0 + 0.01, cost_s=0.002)
    t = core.next_poll_time(T0 + 0.05, T0 + 0.05)
    assert t == pytest.approx(T0 + 0.11, abs=1e-6)
    period = DEFAULT_POST_DIAL_CONSTANTS.poll_fast_s
    for _ in range(5):
        digest = core.note_poll_failed(t, t, cost_s=0.002, reason="HTTP error")
        assert digest.accepted is False and digest.reason == "http_error"
        due = core.next_poll_time(t, t)
        assert due is not None and due >= t + period - 1e-9  # never spins
        t = due
    rejected = core.ingest_snapshot(None, t, t)
    assert rejected.accepted is False and rejected.reason == "invalid_body"
    assert core.next_poll_time(t, t) >= t + period - 1e-9
    # Failed attempts are not observations.
    assert core.latest_snapshot_age(t) == pytest.approx(t - (T0 + 0.01))
    good = core.ingest_snapshot(mihomo.snapshot(t + period), t + period, t + period, cost_s=0.002)
    assert good.accepted is True and good.gap_ok is False
    # On the 1 Hz schedule a failure costs one slow period.
    t = T0 + 3.6
    core.ingest_snapshot(mihomo.snapshot(t), t, t, cost_s=0.002)
    slow_due = core.next_poll_time(t, t)
    assert slow_due == pytest.approx(t + DEFAULT_POST_DIAL_CONSTANTS.poll_slow_s)
    core.note_poll_failed(slow_due, slow_due)
    assert core.next_poll_time(slow_due, slow_due) == pytest.approx(slow_due + DEFAULT_POST_DIAL_CONSTANTS.poll_slow_s)
    assert core.summary(T0 + 10.0)["snapshots_skipped"] == 7


@pytest.mark.parametrize("lag", [0.2, 0.6, 0.9, 1.5])
def test_answered_requests_whose_lines_are_drained_late_never_fail_fast(lag: float) -> None:
    # A vanished legacy entry is kept for JOIN_WINDOW_S only; after that, polls
    # taken before the line was drained cannot prove the connection absent.
    scenario = Scenario()
    scenario.conn(T0, host="bg.example", life=None)  # keeps the 100 ms polling going
    for index in range(2):
        scenario.conn(T0 + 0.3 + index * 0.05, host="ok.example", life=0.15, down=4000, down_at=0.03, line_lag=lag)
    result = scenario.run(_core(), t_end=T0 + 3.0)
    outcomes = [record for record in result.records if record["ev"] == "outcome"]

    assert [trigger for trigger in result.triggers if trigger.kind == RULE_FAST] == []
    assert {record["class"] for record in outcomes} <= {"UNKNOWN"}
    if lag == 0.9:
        assert {record.get("would") for record in outcomes} == {"FAILED_FAST"}


def test_a_zombie_burst_drained_within_the_join_window_still_counts() -> None:
    scenario = Scenario()
    scenario.conn(T0, host="bg.example", life=None)
    for index in range(3):
        scenario.conn(T0 + 0.5 + index * 0.02, host=f"z{index}.example", life=0.01, line_lag=0.2)
    result = scenario.run(_core(), t_end=T0 + 2.5)
    fast = [trigger for trigger in result.triggers if trigger.kind == RULE_FAST]
    assert fast and fast[0].t - (T0 + 0.51) <= 0.35


def test_reader_lag_is_measured_from_the_tap_receive_time() -> None:
    core = _core()
    # The tap received the line 20 ms after Mihomo's time= (at monotonic 500.0);
    # the observer drained it 300 ms after time=.
    record = parse_open_line(replay.open_line(T0, sport=44200, host="lag.example"), 500.0, tokenizer=TOKENIZER, origin_rules=RULES)
    core.ingest_open(record, now_wall=T0 + 0.3, now_perf=T0 + 0.3, now_monotonic=500.28)
    summary = core.summary(T0 + 1.0)
    assert summary["reader_lag_p50_ms"] == pytest.approx(20.0, abs=0.01)
    assert summary["drain_lag_p50_ms"] == pytest.approx(300.0, abs=0.01)
    core.ingest_open(record, now_wall=T0 + 0.3, now_perf=T0 + 0.3, received_at_wall=T0 + 0.05)
    assert core.summary(T0 + 1.0)["reader_lag_p50_ms"] == pytest.approx(50.0, abs=0.01)
    core.ingest_open(record, now_wall=T0 + 0.3, now_perf=T0 + 0.3)
    summary = core.summary(T0 + 1.0)
    assert summary["reader_lag_p50_ms"] is None  # drain time is not reader lag
    assert summary["drain_lag_p50_ms"] == pytest.approx(300.0, abs=0.01)


def test_tracked_opens_are_bounded_by_age_and_the_cap() -> None:
    core = _core()

    def app_open(t: float, sport: int) -> PostDialOpen:
        return PostDialOpen(t, 0.0, "127.0.0.1", sport, "a" * 32, 443, "n1", "FINAL-EXIT", OpenOrigin.APP)

    for index in range(6000):  # 100/s for 60 s while dormant: only OPEN_TTL_S worth stays
        t = T0 + index / 100.0
        core.ingest_open(app_open(t, 10000 + index), now_wall=t, now_perf=t)
        assert core.next_poll_time(t, t) is None  # app opens never schedule a poll
    assert core.summary(T0 + 60.0, reset=False)["tracked"] <= 30 * 100 + 1
    for index in range(6000):  # 400/s: the cap holds
        t = T0 + 60.0 + index / 400.0
        core.ingest_open(app_open(t, 20000 + index), now_wall=t, now_perf=t)
    assert core.summary(T0 + 75.0, reset=False)["tracked"] == 4096
    user = _parse(replay.open_line(T0 + 75.0, sport=40000, host="u.example"))
    core.ingest_open(user, now_wall=T0 + 75.0, now_perf=T0 + 75.0)
    assert core.next_poll_time(T0 + 75.0, T0 + 75.0) is not None


def test_exonerated_tokens_do_not_count_toward_triggers() -> None:
    def burst() -> Scenario:
        scenario = Scenario()
        for index in range(4):
            scenario.conn(T0 + 1.0 + index * 0.03, host="down-everywhere.example", life=0.01)
        return scenario

    exonerated_token = TOKENIZER.destination("down-everywhere.example", 443)
    result = burst().run(_core(), t_end=T0 + 3.0, is_exonerated=lambda leaf, token: token == exonerated_token)
    assert result.triggers == []
    assert burst().run(_core(), t_end=T0 + 3.0).triggers


def test_late_bind_merge_uses_the_legacy_entry_history() -> None:
    core = _core()
    mihomo = replay.FakeMihomo()
    t = T0
    mihomo.add(replay.FakeConn("late", t, 44000, "late.example", 443, "n1", up=replay.step(t, 517), down=replay.step(t + 0.05, 4000), t_close=t + 0.4))
    core.ingest_snapshot(mihomo.snapshot(t + 0.1), t + 0.1, t + 0.1)  # the line has not been drained yet
    core.ingest_open(_parse(replay.open_line(t, sport=44000, host="late.example")), now_wall=t + 0.12, now_perf=t + 0.12)
    core.ingest_snapshot(mihomo.snapshot(t + 0.2), t + 0.2, t + 0.2)
    core.ingest_snapshot(mihomo.snapshot(t + 0.5), t + 0.5, t + 0.5)
    records = core.drain_records()

    assert core.summary(t + 1)["late_bind_merges"] == 1
    assert [record for record in records if record["ev"] == "outcome"] == []
    assert [record["oid"] for record in records if record["ev"] == "open"]


def test_late_bind_merge_of_an_answered_connection_counts_as_answered_user_traffic() -> None:
    core = _core()
    mihomo = replay.FakeMihomo()
    mihomo.add(replay.FakeConn("lb", T0, 44100, "lb.example", 443, "n1", up=replay.step(T0, 517), down=replay.step(T0 + 0.02, 4000)))
    first = core.ingest_snapshot(mihomo.snapshot(T0 + 0.05), T0 + 0.05, T0 + 0.05)  # the snapshot beat the reader
    core.ingest_open(_parse(replay.open_line(T0, sport=44100, host="lb.example")), now_wall=T0 + 0.06, now_perf=T0 + 0.06)
    second = core.ingest_snapshot(mihomo.snapshot(T0 + 0.15), T0 + 0.15, T0 + 0.15)

    assert first.answered_tier3_user is False  # a legacy entry is never user evidence
    assert second.answered_tier3_user is True  # releases a facility/exhausted hold
    assert core.summary(T0 + 1.0)["answered_tier3_user"] == 1


def test_line_drained_after_its_legacy_entry_vanished_is_not_a_phantom_failure() -> None:
    core = _core()
    mihomo = replay.FakeMihomo()
    t = T0
    for index in range(2):
        mihomo.add(
            replay.FakeConn(f"ph{index}", t + index * 0.01, 45000 + index, "ph.example", 443, "n1",
                            up=replay.step(t, 517), down=replay.step(t + 0.03, 4000), t_close=t + 0.15)
        )
    core.ingest_snapshot(mihomo.snapshot(t + 0.1), t + 0.1, t + 0.1)
    core.ingest_snapshot(mihomo.snapshot(t + 0.2), t + 0.2, t + 0.2)
    for index in range(2):
        core.ingest_open(
            _parse(replay.open_line(t + index * 0.01, sport=45000 + index, host="ph.example")),
            now_wall=t + 0.25,
            now_perf=t + 0.25,
        )
    core.ingest_snapshot(mihomo.snapshot(t + 0.3), t + 0.3, t + 0.3)

    assert core.evaluate_triggers(t + 0.3).triggers == ()
    assert [r for r in core.drain_records() if r["ev"] == "outcome"] == []


# ---------------------------------------------------------------------------
# Schedule (§18.4)


@pytest.mark.parametrize("perf_offset", PERF_OFFSETS)
def test_schedule_fast_window_aged_poll_linger_and_dormancy(perf_offset: float) -> None:
    scenario = Scenario()
    t_open = T0 + 1.0
    scenario.conn(t_open, host="slow.example", life=None, up=517, down=4000, down_at=3.5)
    core = _core()
    result = scenario.run(core, t_end=T0 + 20.0, perf_offset=perf_offset)
    window = DEFAULT_POST_DIAL_CONSTANTS.fast_window_s
    fast = [t for t in result.polls if t < t_open + window]
    gaps = [b - a for a, b in zip(fast, fast[1:])]

    assert window == pytest.approx(2.2)  # SLOW is evaluated in shadow
    assert gaps and max(gaps) <= 0.1 + 0.004
    assert any(t_open + 3.02 - 1e-6 <= t <= t_open + 3.03 for t in result.polls)
    # The download at +3.5 s is seen by the 1 Hz linger poll at +4.02 s, which
    # extends the linger by LINGER_S; after that the observer is dormant.
    after = [t for t in result.polls if t > t_open + 3.1]
    assert all(0.99 <= b - a <= 1.01 for a, b in zip(after, after[1:]))
    assert t_open + 7.9 <= max(after) <= t_open + 4.03 + DEFAULT_POST_DIAL_CONSTANTS.linger_s + 0.01
    assert core.need_poll(T0 + 20.0 - perf_offset, T0 + 20.0) is False


def test_bytes_after_the_linger_expired_are_not_polled_for() -> None:
    scenario = Scenario()
    t_open = T0 + 1.0
    scenario.conn(t_open, host="quiet.example", life=None, up=517, down=4000, down_at=6.5)
    result = scenario.run(_core(), t_end=T0 + 20.0)
    # Last byte change at the first sample: 1 Hz until LINGER_S, then dormant (0 calls).
    assert max(result.polls) <= t_open + DEFAULT_POST_DIAL_CONSTANTS.linger_s


def test_fast_window_is_one_point_two_seconds_without_the_slow_candidate() -> None:
    constants = constants_with(evaluate_slow_candidate=False)
    assert constants.fast_window_s == pytest.approx(1.2)
    scenario = Scenario()
    scenario.conn(T0 + 1.0, host="x.example", life=None, up=0)
    result = scenario.run(_core(constants=constants), t_end=T0 + 2.9)
    assert max(result.polls) <= T0 + 1.0 + 1.2 + 0.11


def test_stream_watch_keeps_one_hertz_for_sixty_seconds_after_progress() -> None:
    def run(stream_watch_s: float):
        scenario = Scenario()
        t_open = T0 + 1.0
        scenario.conn(
            t_open,
            host="video.example",
            life=None,
            up=900,
            down_fn=lambda t: 0 if t < t_open + 0.2 else int(min(t, t_open + 10.0) - t_open) * 400_000,
        )
        core = _core(constants=constants_with(stream_watch_s=stream_watch_s))
        return scenario.run(core, t_end=T0 + 120.0).polls

    watched = run(60.0)
    assert max(watched) >= T0 + 1.0 + 10.0 + 59.0
    assert max(watched) <= T0 + 1.0 + 10.0 + 61.5
    unwatched = run(0.0)
    assert max(unwatched) <= T0 + 1.0 + 10.0 + DEFAULT_POST_DIAL_CONSTANTS.linger_s + 1.5


def test_cost_adaptation_stretches_the_fast_period() -> None:
    scenario = Scenario()
    scenario.conn(T0 + 1.0, host="c.example", life=None, up=0)
    core = _core()
    result = scenario.run(core, t_end=T0 + 2.0, cost=0.05)
    gaps = [b - a for a, b in zip(result.polls, result.polls[1:]) if b < T0 + 2.0]
    assert gaps and min(gaps) >= 0.15 - 1e-6


def test_cost_adaptation_uses_the_whole_poll_cost_when_the_request_is_cheap() -> None:
    # A 10 ms request whose ingest makes the poll 50 ms in all: the observer
    # reports the total with note_poll_cost, and the period stretches on it.
    def run(total: float | None):
        scenario = Scenario()
        scenario.conn(T0 + 1.0, host="c.example", life=None, up=0)
        core = _core()
        on_poll = None if total is None else (lambda _t, _evaluation: core.note_poll_cost(total))
        result = scenario.run(core, t_end=T0 + 2.0, cost=0.01, on_poll=on_poll)
        gaps = [b - a for a, b in zip(result.polls, result.polls[1:]) if b < T0 + 2.0]
        return core, gaps

    _core_cheap, cheap = run(None)
    assert cheap and max(cheap) <= 0.1 + 0.01 + 1e-6
    core, whole = run(0.05)
    assert whole and min(whole) >= 0.15 - 1e-6
    summary = core.summary(T0 + 2.0)
    assert summary["poll_cost_p50_ms"] == 10.0
    assert summary["poll_total_p50_ms"] == 50.0


# ---------------------------------------------------------------------------
# Closes (§6 rules 1, 2, 4, 5) and demotion watch


def test_stuck_connection_ids_selects_only_aged_and_waiting_on_the_leaf() -> None:
    core = _core()
    mihomo = replay.FakeMihomo()
    t = T0
    specs = {
        "aged": dict(up=replay.step(t, 517)),
        "waiting": dict(up=lambda x: 800 if x < t + 1.0 else 1600, down=replay.step(t + 0.3, 9000)),
        "progress": dict(up=replay.step(t, 900), down=lambda x: int((x - t) * 50_000)),
        "idle": dict(up=replay.step(t, 900), down=replay.step(t + 0.3, 6000)),
        "other_leaf": dict(up=replay.step(t, 517)),
    }
    for index, (name, fns) in enumerate(specs.items()):
        leaf = "n2" if name == "other_leaf" else "n1"
        mihomo.add(replay.FakeConn(name, t, 46000 + index, f"{name}.example", 443, leaf, **fns))
        core.ingest_open(_parse(replay.open_line(t, sport=46000 + index, host=f"{name}.example", leaf=leaf)), now_wall=t, now_perf=t)
    legacy = mihomo.add(replay.FakeConn("legacy", t - 30, 46100, "legacy.example", 443, "n1", up=replay.step(t - 30, 517)))
    app = mihomo.add(replay.FakeConn("app", t, 46101, "app.example", 443, "n1", up=replay.step(t, 517), special="FINAL-EXIT"))
    del legacy, app
    for step_t in (0.1, 0.5, 1.2, 2.0, 3.1, 3.6, 4.1):
        core.ingest_snapshot(mihomo.snapshot(t + step_t), t + step_t, t + step_t)
    late = mihomo.add(replay.FakeConn("after", t + 4.2, 46200, "after.example", 443, "n1", up=replay.step(t + 4.2, 517)))
    del late
    core.ingest_snapshot(mihomo.snapshot(t + 4.3), t + 4.3, t + 4.3)
    stuck = core.stuck_connection_ids("n1", t_demote_wall=t + 4.15, now_wall=t + 4.3)

    assert stuck.ids[:2] == ("legacy", "aged")
    assert set(stuck.ids) == {"legacy", "aged", "waiting"}
    assert stuck.aged == 2 and stuck.waiting == 1
    assert stuck.kept_progressing == 1
    assert stuck.kept_not_stuck == 1
    assert stuck.kept_after_demotion == 1
    assert stuck.kept_other == 1
    core.mark_closed(["aged"])
    assert "aged" not in core.stuck_connection_ids("n1", t + 4.15, t + 4.3).ids


def test_demotion_watch_polls_at_one_hertz_while_user_connections_remain() -> None:
    core = _core()
    mihomo = replay.FakeMihomo()
    mihomo.add(replay.FakeConn("ws", T0, 47000, "ws.example", 443, "n1", up=replay.step(T0, 900), down=replay.step(T0 + 0.1, 900)))
    core.ingest_open(_parse(replay.open_line(T0, sport=47000, host="ws.example")), now_wall=T0, now_perf=T0)
    core.ingest_snapshot(mihomo.snapshot(T0 + 0.2), T0 + 0.2, T0 + 0.2)
    assert core.need_poll(T0 + 10, T0 + 10) is False

    core.start_demotion_watch("n1", t_demote_wall=T0 + 10, now_perf=T0 + 10)
    assert core.next_poll_time(T0 + 10, T0 + 10) == pytest.approx(T0 + 1.2, abs=1e-6)
    assert core.need_poll(T0 + 69, T0 + 69) is True
    assert core.need_poll(T0 + 70.5, T0 + 70.5) is False
    assert core.active_demotion_watches(T0 + 70.5) == ()


# ---------------------------------------------------------------------------
# Shadow candidates and rule promotion (§3.3)


def test_fast_spread_is_shadow_by_default_and_promotable() -> None:
    def spread() -> Scenario:
        scenario = Scenario()
        for index in range(4):
            scenario.conn(T0 + 1.0 + index * 2.0, host=f"sp{index}.example", life=0.01)
        return scenario

    result = spread().run(_core(), t_end=T0 + 10.0)
    assert [t for t in result.triggers] == []
    assert [s.rule for s in result.shadow] == [RULE_FAST_SPREAD]

    promoted = spread().run(_core(act_rules=("FAST", "AGED", "FAST_SPREAD")), t_end=T0 + 10.0)
    assert [t.kind for t in promoted.triggers] == [RULE_FAST_SPREAD]
    assert promoted.shadow == []


def test_slow_candidate_needs_three_tokens_within_one_second() -> None:
    scenario = Scenario()
    for index in range(3):
        scenario.conn(T0 + 1.0 + index * 0.1, host=f"slow{index}.example", life=1.5)
    result = scenario.run(_core(), t_end=T0 + 5.0)
    assert RULE_SLOW in [s.rule for s in result.shadow]

    same_token = Scenario()
    for index in range(3):
        same_token.conn(T0 + 1.0 + index * 0.1, host="one.example", life=1.5)
    assert RULE_SLOW not in [s.rule for s in same_token.run(_core(), t_end=T0 + 5.0).shadow]


def test_waiting_candidate_on_a_frozen_stream_with_a_concurrent_aged() -> None:
    scenario = Scenario()
    t_open = T0 + 1.0
    freeze = t_open + 6.0
    scenario.conn(
        t_open,
        host="video.example",
        life=None,
        up_fn=lambda t: 900 if t < freeze + 0.5 else 1800,
        down_fn=lambda t: 0 if t < t_open + 0.2 else int(min(t, freeze) - t_open) * 300_000,
    )
    scenario.conn(freeze + 2.0, host="video.example", life=None)  # a new tunnel that hangs
    result = scenario.run(_core(), t_end=freeze + 12.0)
    rules = [s.rule for s in result.shadow]
    assert RULE_WAITING in rules
    assert RULE_AGED_SINGLE in rules


def test_aged_single_is_negated_by_a_later_answered_open() -> None:
    scenario = Scenario()
    scenario.conn(T0 + 1.0, host="hang.example", life=None)
    scenario.conn(T0 + 1.5, host="fine.example", life=2.0, up=900, down=5000, down_at=0.2)
    result = scenario.run(_core(), t_end=T0 + 6.0)
    assert RULE_AGED_SINGLE not in [s.rule for s in result.shadow]


# ---------------------------------------------------------------------------
# Failed-share guard (09-30): FAST/AGED act only while most of L's recent
# connections fail (failed / (failed + answered) >= 0.5 over 10 s).  Off by
# default since 10-01 (``GUARDED`` sets 0.5); its counts and the 0.5 / onset
# variants are logged at every setting.


def _held(result, rule: str) -> list[ShadowTrigger]:
    return [s for s in result.shadow if s.rule == rule and s.share_ok is False]


def _first(result, kind: str) -> Trigger | None:
    return next((t for t in result.triggers if t.kind == kind), None)


def _single_site_failure(t_fail: float, *, answered: int = 8) -> Scenario:
    # Other sites keep answering on n1 (every one within 10 s of the burst);
    # then Edge's six tunnels to the one failing site die with 0 bytes.
    scenario = Scenario()
    for index in range(answered):
        scenario.conn(t_fail - 8.0 + index * 0.9, host=f"ok{index}.example", life=2.0, up=800, down=4200, down_at=0.2)
    for index in range(6):
        scenario.conn(t_fail + index * 0.02, host="blocked.example", life=0.01 + index * 0.004)
    return scenario


def test_guard_holds_a_single_site_failure_on_a_leaf_that_answers_other_sites() -> None:
    t_fail = T0 + 10.0
    unguarded = _single_site_failure(t_fail).run(_core(constants=UNGUARDED), t_end=T0 + 16.0)
    core = _core(constants=GUARDED)
    guarded = _single_site_failure(t_fail).run(core, t_end=T0 + 16.0)

    rule_only = _first(unguarded, RULE_FAST)
    assert rule_only is not None and rule_only.new_episode is True  # the rule alone fires
    assert [t for t in guarded.triggers if t.kind in {RULE_FAST, RULE_AGED}] == []
    held = _held(guarded, RULE_FAST)
    # Logged once, at the unguarded trigger's time and episode, with the guard's inputs.
    assert len(held) == 1
    assert (held[0].t, held[0].episode, held[0].tokens) == (rule_only.t, rule_only.episode, rule_only.tokens)
    assert (held[0].ratio_failed, held[0].ratio_answered) == (rule_only.ratio_failed, rule_only.ratio_answered)
    assert held[0].share_answered == 8 and 2 <= held[0].share_failed < held[0].share_answered
    assert rule_only.share_ok is True and rule_only.share_answered == 8
    assert core.summary(T0 + 16.0)["share_blocked"] >= 1
    record = trigger_record(held[0], TOKENIZER, mode="act")
    assert record["ev"] == "would_trigger" and record["rule"] == RULE_FAST and record["share_ok"] is False


def test_guard_lets_a_zombie_with_only_one_site_loading_trigger_at_once() -> None:
    def zombie() -> Scenario:
        scenario = Scenario()
        # Answered traffic on n1 ended more than 10 s before the user loads one site.
        for index in range(5):
            scenario.conn(T0 + index * 0.8, host=f"ok{index}.example", life=1.5, up=800, down=4200, down_at=0.2)
        t_load = T0 + 16.0
        for index in range(6):  # every tunnel to the one site dies: the node is a zombie
            scenario.conn(t_load + index * 0.02, host="only-site.example", life=0.01 + index * 0.004)
        return scenario

    unguarded = zombie().run(_core(constants=UNGUARDED), t_end=T0 + 22.0)
    guarded = zombie().run(_core(constants=GUARDED), t_end=T0 + 22.0)

    rule_only, fast = _first(unguarded, RULE_FAST), _first(guarded, RULE_FAST)
    assert rule_only is not None and fast is not None
    assert (fast.t, fast.episode, fast.new_episode, fast.tokens) == (
        rule_only.t, rule_only.episode, rule_only.new_episode, rule_only.tokens,
    )
    assert fast.new_episode is True and len(fast.tokens) == 1
    assert fast.share_ok is True and fast.share_answered == 0 and fast.share_failed >= 2
    assert _held(guarded, RULE_FAST) == []


def _share_window_scenario(t_fail: float, *, counted_answered: int) -> Scenario:
    scenario = Scenario()
    # Counted: user-origin answers on n1 classified within 10 s.
    for index in range(counted_answered):
        scenario.conn(t_fail - 6.0 + index * 0.7, host=f"ok{index}.example", life=1.5, up=800, down=4200, down_at=0.2)
    # Not counted, five of each: another leaf, a leaf outside TIER3, an APP
    # open (a system-rule host), a gateway chain, a legacy entry (no open
    # line), and answers on n1 classified more than 10 s before.
    for index in range(5):
        t = t_fail - 5.0 + index * 0.5
        scenario.conn(t, host=f"other-leaf{index}.example", leaf="n2", life=1.5, up=800, down=4200, down_at=0.2)
        scenario.conn(t, host=f"no-tier3-{index}.example", leaf="x9", life=1.5, up=800, down=4200, down_at=0.2)
        scenario.conn(
            t, host="sys.test", outer="FINAL-EXIT", rule="Domain(sys.test)", life=1.5, up=800, down=4200, down_at=0.2
        )
        gateway = scenario.conn(t, host=f"gw{index}.example", life=1.5, up=800, down=4200, down_at=0.2)
        gateway.chains_tail = ("GW-GROUP", *gateway.chains_tail)
        scenario.conn(t, host=f"legacy{index}.example", life=1.5, up=800, down=4200, down_at=0.2, line=False)
        scenario.conn(t_fail - 14.0 + index * 0.3, host=f"old{index}.example", life=1.5, up=800, down=4200, down_at=0.2)
    # A failure classified more than 10 s before is not counted either.
    scenario.conn(t_fail - 12.0, host="old-fail.example", life=0.01)
    # The burst: exactly two FAILED_FAST on n1.
    scenario.conn(t_fail, host="z.example", life=0.01)
    scenario.conn(t_fail + 0.02, host="z.example", life=0.012)
    return scenario


@pytest.mark.parametrize(("counted_answered", "acts"), [(0, True), (2, True), (3, False)])
def test_guard_window_counts_only_user_origin_tier3_connections_of_the_leaf_within_ten_seconds(
    counted_answered: int, acts: bool
) -> None:
    t_fail = T0 + 20.0
    core = _core(gateway_groups=("GW-GROUP",), constants=GUARDED)
    result = _share_window_scenario(t_fail, counted_answered=counted_answered).run(core, t_end=T0 + 24.0)
    unguarded = _share_window_scenario(t_fail, counted_answered=counted_answered).run(
        _core(gateway_groups=("GW-GROUP",), constants=UNGUARDED), t_end=T0 + 24.0
    )

    rule_only = _first(unguarded, RULE_FAST)
    assert rule_only is not None
    # 2 failed; answered = only the counted ones: 2/2 = 1.0, 2/4 = 0.5 (acts), 2/5 = 0.4 (held).
    assert (rule_only.share_failed, rule_only.share_answered) == (2, counted_answered)
    if acts:
        fast = _first(result, RULE_FAST)
        assert fast is not None and (fast.t, fast.new_episode) == (rule_only.t, True)
        assert (fast.share_failed, fast.share_answered, fast.share_ok) == (2, counted_answered, True)
    else:
        assert _first(result, RULE_FAST) is None
        held = _held(result, RULE_FAST)
        assert [(h.t, h.share_failed, h.share_answered) for h in held] == [(rule_only.t, 2, 3)]


def _black_hole(t_hang: float, *, answered: int) -> Scenario:
    scenario = Scenario()
    for index in range(answered):
        scenario.conn(t_hang - 5.0 + index * 0.8, host=f"ok{index}.example", life=1.5, up=800, down=4200, down_at=0.2)
    for t in (t_hang, t_hang + 0.05):  # two tunnels that send and never hear back
        scenario.conn(t, host="www.kernel.org", life=None)
    return scenario


@pytest.mark.parametrize(("answered", "acts"), [(0, True), (2, True), (5, False)])
def test_guard_holds_aged_the_same_way(answered: int, acts: bool) -> None:
    t_hang = T0 + 10.0
    unguarded = _black_hole(t_hang, answered=answered).run(_core(constants=UNGUARDED), t_end=T0 + 15.0)
    guarded = _black_hole(t_hang, answered=answered).run(_core(constants=GUARDED), t_end=T0 + 15.0)

    rule_only = _first(unguarded, RULE_AGED)
    assert rule_only is not None and rule_only.new_episode is True
    assert (rule_only.share_failed, rule_only.share_answered) == (2, answered)
    aged = _first(guarded, RULE_AGED)
    if acts:
        assert aged is not None and (aged.t, aged.new_episode, aged.share_ok) == (rule_only.t, True, True)
        assert _held(guarded, RULE_AGED) == []
    else:
        assert aged is None
        assert [(h.t, h.episode, h.share_failed, h.share_answered) for h in _held(guarded, RULE_AGED)] == [
            (rule_only.t, rule_only.episode, 2, 5)
        ]


def _dies_mid_browsing() -> Scenario:
    # Three answers on n1 in the 10 s before the node dies, then the user
    # retries one site: every retry fails.
    scenario = Scenario()
    for index in range(3):
        scenario.conn(T0 + 2.0 + index * 0.5, host=f"ok{index}.example", life=1.5, up=800, down=4200, down_at=0.2)
    t_fail = T0 + 8.0
    for index in range(6):
        scenario.conn(t_fail + index * 0.15, host="retry.example", life=0.01)
    return scenario


def test_a_node_that_dies_mid_browsing_acts_once_its_failures_outnumber_its_answers() -> None:
    # With the guard at 0.5 the first match (2 failed, 3 answered) is held;
    # the guarded trigger follows as soon as failed >= answered, in the same episode.
    core = _core(constants=GUARDED)
    result = _dies_mid_browsing().run(core, t_end=T0 + 12.0)

    held = _held(result, RULE_FAST)
    fast = _first(result, RULE_FAST)
    assert len(held) == 1 and held[0].share_failed < 3
    assert fast is not None and fast.new_episode is True and fast.episode == held[0].episode
    assert fast.share_failed >= fast.share_answered == 3
    assert fast.t > held[0].t
    assert len([t for t in result.triggers if t.new_episode]) == 1
    # Every answer came from a connection opened before the node died.
    assert held[0].onset_answered == 0 and fast.onset_answered == 0
    # The held match is the rules' trigger and the onset variant's; the
    # guarded trigger is the 0.5 variant's.  Nothing is variant-only here.
    assert held[0].opens == (VARIANT_RULES, VARIANT_ONSET)
    assert (held[0].share_ok_at_0_5, held[0].onset_ok_at_0_5) == (False, True)
    assert fast.opens == (VARIANT_SHARE,) and fast.share_ok_at_0_5 is True
    assert [s for s in result.shadow if s.variant_only] == []
    assert "variant_triggers" not in core.summary(T0 + 12.0)


def test_by_default_the_rules_act_and_the_variants_are_logged_where_they_would_open() -> None:
    # The guard is off by default (10-01): the node that dies mid-browsing
    # triggers at the rules' own time.  That trigger carries the 0.5 guard's
    # verdict (false: 2 failed, 3 answered) and the onset variant's (true: no
    # answer opened since the fault), and a variant-only would_trigger marks
    # the later match where the 0.5 guard would first have passed.  So one
    # log holds all three variants.
    assert DEFAULT_POST_DIAL_CONSTANTS == UNGUARDED
    core = _core()
    result = _dies_mid_browsing().run(core, t_end=T0 + 12.0)
    unguarded = _dies_mid_browsing().run(_core(constants=UNGUARDED), t_end=T0 + 12.0)
    guarded = _dies_mid_browsing().run(_core(constants=GUARDED), t_end=T0 + 12.0)

    assert result.triggers == unguarded.triggers and result.shadow == unguarded.shadow
    fast = _first(result, RULE_FAST)
    assert fast is not None and fast.new_episode is True and fast.share_ok is True
    assert (fast.share_failed, fast.share_answered, fast.onset_answered) == (2, 3, 0)
    assert (fast.share_ok_at_0_5, fast.onset_ok_at_0_5) == (False, True)
    assert fast.opens == (VARIANT_RULES, VARIANT_ONSET)
    assert fast.t == _held(guarded, RULE_FAST)[0].t
    variant = [s for s in result.shadow if s.variant_only]
    guarded_fast = _first(guarded, RULE_FAST)
    assert guarded_fast is not None
    assert [(s.rule, s.t, s.episode, s.opens, s.share_ok, s.share_ok_at_0_5) for s in variant] == [
        (RULE_FAST, guarded_fast.t, fast.episode, (VARIANT_SHARE,), True, True)
    ]
    assert (variant[0].share_failed, variant[0].share_answered) == (
        guarded_fast.share_failed,
        guarded_fast.share_answered,
    )
    # Every later match of the episode is still a Trigger (act mode judges each).
    assert all(t.kind == RULE_FAST and t.episode == fast.episode for t in result.triggers)
    assert len([t for t in result.triggers if t.new_episode]) == 1

    first = trigger_record(fast, TOKENIZER, mode="shadow")
    assert first["ev"] == "would_trigger" and first["share_ok"] is True
    assert (first["share_ok_at_0_5"], first["onset_ok_at_0_5"]) == (False, True)
    assert first["opens"] == [VARIANT_RULES, VARIANT_ONSET] and "variant_only" not in first
    marker = trigger_record(variant[0], TOKENIZER, mode="act")
    assert marker["ev"] == "would_trigger" and marker["variant_only"] is True
    assert marker["opens"] == [VARIANT_SHARE] and marker["share_ok_at_0_5"] is True
    summary = core.summary(T0 + 12.0)
    assert summary["variant_triggers"] == 1 and "share_blocked" not in summary
    assert summary["shadow_triggers"] == len(result.shadow) - 1


def test_onset_answered_counts_answers_opened_at_or_after_the_earliest_failure() -> None:
    # ``onset_answered`` (shadow-only) is the "answers since the fault began"
    # variant of the guard's denominator, for the next gate run to compare.
    scenario = Scenario()
    t_fail = T0 + 10.0
    for index in range(3):  # answered before the first failure: not onset answers
        scenario.conn(t_fail - 6.0 + index * 0.7, host=f"pre{index}.example", life=1.5, up=800, down=4200, down_at=0.2)
    scenario.conn(t_fail, host="blocked.example", life=0.01)
    scenario.conn(t_fail + 0.02, host="blocked.example", life=0.012)
    # Other sites answer on connections opened exactly at the onset and after it.
    scenario.conn(t_fail, host="at-onset.example", life=1.5, up=800, down=4200, down_at=0.2)
    scenario.conn(t_fail + 0.3, host="after.example", life=1.5, up=800, down=4200, down_at=0.2)
    scenario.conn(t_fail + 1.5, host="blocked.example", life=0.01)  # the user retries the one site
    scenario.conn(t_fail + 1.52, host="blocked.example", life=0.012)
    result = scenario.run(_core(constants=GUARDED), t_end=T0 + 14.0)

    held = [(s.t - t_fail, s.share_failed, s.share_answered, s.onset_answered) for s in _held(result, RULE_FAST)]
    assert result.triggers == []
    # First match: only the 3 earlier answers are in; the retry: 4 failed
    # against 5 answered, 2 of them (at the onset, and after it) onset answers.
    assert [row[1:] for row in held] == [(2, 3, 0), (4, 5, 2)]
    assert held[0][0] < 1.0 < held[1][0]


def test_exonerated_failures_are_left_out_of_the_failed_share() -> None:
    # A token exonerated on L (down everywhere) is not evidence against L:
    # its FAILED_FAST and AGED events drop out of the share as well.
    exonerated = "down-everywhere.example"

    def scenario() -> Scenario:
        scenario = Scenario()
        t_fail = T0 + 10.0
        for index in range(3):
            scenario.conn(t_fail - 8.0 + index * 0.7, host=f"ok{index}.example", life=1.5, up=800, down=4200, down_at=0.2)
        scenario.conn(t_fail - 5.0, host=exonerated, life=None)  # hangs: AGED
        scenario.conn(t_fail - 3.0, host=exonerated, life=0.01)
        scenario.conn(t_fail - 2.98, host=exonerated, life=0.012)
        scenario.conn(t_fail, host="z.example", life=0.01)
        scenario.conn(t_fail + 0.02, host="z.example", life=0.012)
        return scenario

    token = TOKENIZER.destination(exonerated, 443)
    plain = scenario().run(_core(constants=GUARDED), t_end=T0 + 13.0)
    filtered = scenario().run(
        _core(constants=GUARDED), t_end=T0 + 13.0, is_exonerated=lambda leaf, tok: tok == token
    )

    # Without exoneration the Z burst counts all 5 failures (2 FAILED_FAST +
    # 1 AGED to the exonerated token, 2 FAILED_FAST to Z) against 3 answers.
    last = [t for t in plain.triggers if t.kind == RULE_FAST][-1]
    assert last.t - T0 == pytest.approx(10.1, abs=0.01)
    assert (last.share_failed, last.share_answered, last.share_ok) == (5, 3, True)
    # Exonerated: the same burst counts only Z's 2 failures and is held.
    assert [t for t in filtered.triggers if t.kind in {RULE_FAST, RULE_AGED}] == []
    held = _held(filtered, RULE_FAST)
    assert [(h.t, h.share_failed, h.share_answered) for h in held] == [(last.t, 2, 3)]
    assert all(token not in s.tokens for s in filtered.shadow)


def test_an_aged_connection_answered_later_counts_once_as_answered() -> None:
    # Two slow connections go AGED and then get their first download: slow,
    # not failing.  At a later FAST match they count as answered only.
    def scenario() -> Scenario:
        scenario = Scenario()
        for index in range(2):
            scenario.conn(T0 + 1.0 + index * 0.05, host=f"slow{index}.example", life=8.0, up=600, down=5000, down_at=3.5)
        scenario.conn(T0 + 5.0, host="ok.example", life=1.5, up=800, down=4200, down_at=0.2)
        scenario.conn(T0 + 6.0, host="z.example", life=0.01)
        scenario.conn(T0 + 6.02, host="z.example", life=0.012)
        return scenario

    unguarded = scenario().run(_core(constants=UNGUARDED), t_end=T0 + 9.0)
    guarded = scenario().run(_core(constants=GUARDED), t_end=T0 + 9.0)

    aged = _first(guarded, RULE_AGED)  # before the answers: 2 failed, 0 answered
    assert aged is not None and (aged.share_failed, aged.share_answered, aged.share_ok) == (2, 0, True)
    rule_only = _first(unguarded, RULE_FAST)
    assert rule_only is not None and (rule_only.share_failed, rule_only.share_answered) == (2, 3)
    # Counted as failed and answered, it would be 4/7 and pass; once, 2/5 is held.
    assert _first(guarded, RULE_FAST) is None
    assert [(h.t, h.share_failed, h.share_answered, h.onset_answered) for h in _held(guarded, RULE_FAST)] == [
        (rule_only.t, 2, 3, 0)
    ]


def test_guard_off_restores_the_rules_alone() -> None:
    # 0, the default, disables the guard: the rules act alone.  (Checked
    # exactly against the pre-guard core on every fixture in the 09-30 and
    # 10-01 change records.)
    t_fail = T0 + 10.0
    core = _core(constants=UNGUARDED)
    result = _single_site_failure(t_fail).run(core, t_end=T0 + 16.0)
    guarded = _single_site_failure(t_fail).run(_core(constants=GUARDED), t_end=T0 + 16.0)
    default = _single_site_failure(t_fail).run(_core(), t_end=T0 + 16.0)
    assert (default.triggers, default.shadow) == (result.triggers, result.shadow)

    fast = _first(result, RULE_FAST)
    held = _held(guarded, RULE_FAST)[0]
    assert fast is not None and fast.new_episode is True
    assert (fast.t, fast.episode, fast.tokens, fast.n_events) == (held.t, held.episode, held.tokens, held.n_events)
    assert all(t.share_ok is True for t in result.triggers)
    assert [s for s in result.shadow if s.share_ok is False] == []
    assert "share_blocked" not in core.summary(T0 + 16.0)
    # A negative or NaN minimum is off as well; the policy clamps to 0-1.
    for minimum in (-1.0, float("nan")):
        other = _single_site_failure(t_fail).run(
            _core(constants=constants_with(trigger_min_failed_share=minimum)), t_end=T0 + 16.0
        )
        assert [(t.kind, t.t, t.new_episode) for t in other.triggers] == [
            (t.kind, t.t, t.new_episode) for t in result.triggers
        ]


# ---------------------------------------------------------------------------
# Output records and privacy (§18.8)


def test_records_and_reprs_never_contain_raw_hosts_or_leaf_names() -> None:
    secret_host = "secret-site.example"
    secret_leaf = "node.example.net"
    scenario = Scenario()
    for index in range(4):
        scenario.conn(T0 + 1.0 + index * 0.02, host=secret_host, leaf=secret_leaf, life=0.01)
    scenario.conn(T0 + 3.0, host=secret_host, leaf=secret_leaf, life=None)
    core = _core(tier3_members=(secret_leaf,))
    result = scenario.run(core, t_end=T0 + 8.0)
    assert result.triggers
    trigger = result.triggers[0]
    exported = [
        *result.records,
        core.summary(T0 + 8.0),
        trigger_record(trigger, TOKENIZER, mode="act"),
        trigger_record(trigger, TOKENIZER, mode="shadow"),
        gated_record(trigger, TOKENIZER, reason="judging", now_wall=T0 + 8.0),
        *(trigger_record(item, TOKENIZER, mode="shadow") for item in result.shadow),
    ]
    blob = json.dumps(exported) + repr(core) + repr(trigger) + repr(result.shadow) + str(trigger)
    blob += repr(core.stuck_connection_ids(secret_leaf, T0 + 9.0, T0 + 9.0))
    assert secret_host not in blob
    assert secret_leaf not in blob
    assert "secret" not in blob
    assert TOKENIZER.leaf(secret_leaf) in json.dumps(exported)
    assert {record["ev"] for record in exported} >= {"open", "outcome", "summary", "trigger", "would_trigger", "gated"}


def test_trigger_record_shape() -> None:
    trigger = Trigger(
        kind=RULE_FAST,
        leaf="n1",
        tokens=("a" * 32, "b" * 32),
        t=T0 + 1.0,
        n_events=2,
        first_event_t=T0 + 0.9,
        episode=1,
        new_episode=True,
        ratio_failed=2,
        ratio_answered=0,
        share_failed=3,
        share_answered=1,
        share_ok=True,
        onset_answered=0,
        share_ok_at_0_5=True,
        onset_ok_at_0_5=True,
        opens=(VARIANT_RULES, VARIANT_SHARE, VARIANT_ONSET),
    )
    record = trigger_record(trigger, TOKENIZER, mode="act")
    assert record["ev"] == "trigger"
    assert record["toks"] == ["a" * 12, "b" * 12]
    assert record["first_ev_ms"] == pytest.approx(100.0)
    assert record["ratio_failed"] == 2 and record["ratio_answered"] == 0
    assert (record["share_failed"], record["share_answered"], record["share_ok"]) == (3, 1, True)
    assert record["onset_answered"] == 0
    assert (record["share_ok_at_0_5"], record["onset_ok_at_0_5"]) == (True, True)
    assert record["opens"] == ["rules", "share_0_5", "onset_0_5"] and "variant_only" not in record
    assert trigger_record(trigger, TOKENIZER, mode="shadow")["ev"] == "would_trigger"
    # An act-rule match the guard held back is a would_trigger even in act mode.
    held = ShadowTrigger(
        rule=RULE_FAST, leaf="n1", tokens=("a" * 32,), t=T0 + 1.0, n_events=2, first_event_t=T0 + 0.9,
        episode=1, ratio_failed=2, ratio_answered=6, share_failed=2, share_answered=6, share_ok=False,
        onset_answered=0,
    )
    held_record = trigger_record(held, TOKENIZER, mode="act")
    assert held_record["ev"] == "would_trigger" and held_record["share_ok"] is False
    assert held_record["opens"] == [] and "share_ok_at_0_5" not in held_record
    # A variant-only record (the guard off, the 0.5 guard passing later in the episode).
    marker = replace(held, share_ok=True, share_ok_at_0_5=True, onset_ok_at_0_5=False, opens=(VARIANT_SHARE,), variant_only=True)
    marker_record = trigger_record(marker, TOKENIZER, mode="shadow")
    assert marker_record["ev"] == "would_trigger" and marker_record["variant_only"] is True
    assert (marker_record["share_ok"], marker_record["share_ok_at_0_5"], marker_record["onset_ok_at_0_5"]) == (
        True, True, False,
    )
    assert marker_record["opens"] == ["share_0_5"]
    # Records without guard inputs keep the old shape.
    plain = replace(
        trigger,
        share_failed=None, share_answered=None, share_ok=None, onset_answered=None,
        share_ok_at_0_5=None, onset_ok_at_0_5=None, opens=(),
    )
    plain_record = trigger_record(plain, TOKENIZER, mode="act")
    assert not {"share_ok", "share_ok_at_0_5", "onset_ok_at_0_5", "opens", "variant_only"} & set(plain_record)


# ---------------------------------------------------------------------------
# Fixtures from the harness runs (see fixtures/post_dial/README.md)


X1_FIRST_FAST_S = {"R1": 0.35, "R2": 2.97, "R3": 0.50, "R4": 0.90, "R5": 0.54}


@pytest.mark.parametrize("tag", ["R1", "R2", "R3", "R4", "R5"])
def test_x1_tables_are_reproduced_from_real_snapshots(tag: str) -> None:
    fixture = replay.load_fixture(f"x1_{tag}.json.gz")
    core = _core(tier3_members=("A", "B", "C"))
    lines = fixture["lines"]
    index = 0
    first_fast: dict[str, float] = {}
    for t, snapshot in replay.x1_snapshots(fixture):
        while index < len(lines) and lines[index][0] <= t:
            record = parse_open_line(lines[index][1], lines[index][0], tokenizer=TOKENIZER, origin_rules=OriginRules())
            if record is not None:
                core.ingest_open(record, now_wall=lines[index][0], now_perf=lines[index][0])
            index += 1
        core.ingest_snapshot(snapshot, t, t, cost_s=0.003)
        for trigger in core.evaluate_triggers(t).triggers:
            if trigger.kind == RULE_FAST:
                first_fast.setdefault(trigger.leaf, trigger.t - fixture["t_switch"])
    outcomes = replay.outcomes_by_line([raw for _, raw in lines], core.drain_records())
    expected = fixture["expected"]

    def score(flagged_classes: set[str]) -> tuple[float, float]:
        tp = fp = fn = 0
        for line_index, category in fixture["rows"]:
            flagged = line_index >= 0 and outcomes.get(line_index) in flagged_classes
            positive = category == "zombie_fail"
            tp += flagged and positive
            fp += flagged and not positive
            fn += positive and not flagged
        return tp / (tp + fp), tp / (tp + fn)

    # The §3.2 classifier without the poll-gap guard is the X1 T=1.0 classifier.
    precision, recall = score({"FAILED_FAST", "UNKNOWN/FAILED_FAST"})
    assert precision == pytest.approx(expected["precision"], abs=0.02)
    assert recall == pytest.approx(expected["recall"], abs=0.02)
    # With the guard, UNKNOWN is not evidence: precision holds, recall can only drop.
    guarded_precision, guarded_recall = score({"FAILED_FAST"})
    assert guarded_precision >= expected["precision"] - 0.02
    assert guarded_recall <= recall
    # FAST fires only on the zombie leaf, at the X1 "2 in 1 s" latency.
    assert set(first_fast) == {"A"}
    assert first_fast["A"] == pytest.approx(X1_FIRST_FAST_S[tag], abs=0.06)


def _vproto_run(rep, *, perf_offset: float = 0.0, poll_scale: float = 1.0, **core_kwargs):
    mihomo, feed = replay.vproto_mihomo(rep, t_end=rep["t_exit"])
    core = _core(tier3_members=tuple(rep["members"]), **core_kwargs)
    if poll_scale != 1.0:
        core.set_poll_scale(poll_scale)
    result = replay.drive(core, feed, mihomo, t_start=rep["t_attach"], t_end=rep["t_exit"], perf_offset=perf_offset)
    outcomes = replay.outcomes_by_line([line[1] for line in rep["lines"]], result.records)
    return core, result, outcomes


# The prototype's BLACKHOLE kind is v3's AGED rule.
PROTO_TRIGGER_KINDS = {"FAST": RULE_FAST, "BLACKHOLE": RULE_AGED}
# (fixture, reps, scenario): see fixtures/post_dial/README.md.
VPROTO_FIXTURES = [
    ("P1", 6, "fast zombie"),
    ("P3", 6, "black hole"),
    ("P5", 5, "healthy browsing, 10 min each"),
    ("P4", 10, "frozen h2 session, new tunnels black-holed"),
    ("P6", 5, "every node stalls (facility)"),
    ("P6b", 6, "every node down / every node a zombie"),
    ("P7", 10, "one site down on every node (FIN / stall)"),
    ("IDLE", 3, "3 min idle, then a zombie"),
]


def _aged_poll_phase_race(line) -> bool:
    """The prototype's +3.02 s poll saw download 0; the replay's poll landed
    just after the first download.  Poll phase, not a classifier difference."""

    schedule = line[2]
    if schedule is None or schedule[4] is None:
        return False
    t_open = replay.line_identity(line[1])[0]
    return 0.0 <= schedule[4] - (t_open + DEFAULT_POST_DIAL_CONSTANTS.t_age_s) <= 0.1


@pytest.mark.parametrize(("scenario", "reps"), [(tag, reps) for tag, reps, _what in VPROTO_FIXTURES])
def test_vproto_outcomes_and_triggers_match_the_prototype(scenario: str, reps: int) -> None:
    fixture = replay.load_fixture(f"vproto_{scenario}.json.gz")
    assert len(fixture["reps"]) == reps
    races = 0
    for rep in fixture["reps"]:
        # The prototype had no failed-share guard: compare the rules alone.
        _core_, result, outcomes = _vproto_run(rep, constants=UNGUARDED)
        expected = {index: line[3] for index, line in enumerate(rep["lines"]) if line[3]}
        mine = {index: value for index, value in outcomes.items() if value in {"FAILED_FAST", "AGED"}}
        differ = {index for index in set(expected) | set(mine) if expected.get(index) != mine.get(index)}
        for index in differ:
            assert expected.get(index) == "AGED" and index not in mine, (rep["label"], index)
            assert _aged_poll_phase_race(rep["lines"][index]), (rep["label"], index)
        races += len(differ)
        proto_triggers = rep["proto"]["triggers"]
        if not proto_triggers:
            assert result.triggers == [], rep["label"]
            continue
        first = result.triggers[0]
        assert first.kind == PROTO_TRIGGER_KINDS[proto_triggers[0][1]], rep["label"]
        assert first.leaf == proto_triggers[0][2] == "n1", rep["label"]
        assert first.t == pytest.approx(proto_triggers[0][0], abs=0.1), rep["label"]
        assert first.new_episode is True
    # One line in 49 reps (P6b P6zombie-r0) is such a race.
    assert races <= 1


@pytest.mark.parametrize("scenario", ["P1", "P3"])
def test_vproto_replay_does_not_depend_on_the_perf_epoch(scenario: str) -> None:
    fixture = replay.load_fixture(f"vproto_{scenario}.json.gz")
    for rep in fixture["reps"][:3]:
        _a, same, _ = _vproto_run(rep)
        _b, shifted, _ = _vproto_run(rep, perf_offset=rep["t_attach"] - 1234.5)
        assert len(shifted.polls) == len(same.polls), rep["label"]
        assert max(abs(a - b) for a, b in zip(same.polls, shifted.polls)) < 1e-5
        assert [(t.kind, round(t.t, 4)) for t in shifted.triggers] == [(t.kind, round(t.t, 4)) for t in same.triggers]


# The failed-share guard set to 0.5 on the fixtures (09-30; off by default
# since 10-01, when this held-first behaviour was the reason).  A zombie on a
# leaf with no answered traffic in the 10 s before it (IDLE; X1, which
# switches onto the zombie leaf) still triggers at the unguarded time.  A
# node that dies while the user browses has answers from just before the
# fault in its window: the first match is held (logged as would_trigger,
# share_ok false) and the guarded trigger comes only once failures outnumber
# those answers - or not at all in these fixtures, which stop soon after the
# prototype moved the traffic.  P7 (one site down on every node) is held the
# same way.
GUARD_SAME_TIME = ("IDLE",)
GUARD_HELD_FIRST = ("P1", "P3", "P4", "P6", "P6b", "P7")


def _trigger_keys(triggers) -> list[tuple]:
    return [(t.kind, t.leaf, t.t, t.episode, t.tokens, t.n_events) for t in triggers]


@pytest.mark.parametrize("scenario", ["P5", *GUARD_SAME_TIME, *GUARD_HELD_FIRST])
def test_vproto_fixtures_under_the_failed_share_guard(scenario: str) -> None:
    fixture = replay.load_fixture(f"vproto_{scenario}.json.gz")
    for rep in fixture["reps"]:
        _a, rules_only, _ = _vproto_run(rep, constants=UNGUARDED)
        _b, guarded, _ = _vproto_run(rep, constants=GUARDED)
        # The guard only removes act evaluations; it never adds or moves one.
        remaining = iter(_trigger_keys(rules_only.triggers))
        assert all(item in remaining for item in _trigger_keys(guarded.triggers)), rep["label"]
        # Every unguarded episode stays measurable: its first match is either
        # the guarded trigger or a held would_trigger at the same time.
        held = {
            (s.rule, s.leaf, s.t, s.episode)
            for s in guarded.shadow
            if s.share_ok is False and s.rule in {RULE_FAST, RULE_AGED}
        }
        acted = {(t.kind, t.leaf, t.t, t.episode) for t in guarded.triggers if t.new_episode}
        for t in rules_only.triggers:
            if t.new_episode:
                assert (t.kind, t.leaf, t.t, t.episode) in held | acted, rep["label"]
        if scenario == "P5" or not rules_only.triggers:
            assert guarded.triggers == [] and held == set(), rep["label"]
            continue
        first = rules_only.triggers[0]
        if scenario in GUARD_SAME_TIME:
            assert (guarded.triggers[0].kind, guarded.triggers[0].t) == (first.kind, first.t), rep["label"]
            assert guarded.triggers[0].share_answered == 0
        else:
            assert (first.kind, first.leaf, first.t, first.episode) in held, rep["label"]
            assert first.share_failed < first.share_answered, rep["label"]
            assert all(t.t > first.t for t in guarded.triggers), rep["label"]
            # The default (the guard off) acts at the rules' time and logs that
            # the 0.5 guard would not have.
            assert first.share_ok is True and first.share_ok_at_0_5 is False, rep["label"]


class _OnsetGuardCore(PostDialObserverCore):
    """Test reference: the onset variant acted on (the 0.5 guard with only
    the answers opened at or after the earliest counted failure)."""

    def _failed_share(self, leaf, events, now):
        failed, _answered, onset_answered = PostDialObserverCore._failed_share(self, leaf, events, now)
        return failed, onset_answered, onset_answered


def _logged_triggers(result) -> set[tuple]:
    # What a run logs as its trigger, once per episode.
    return {(t.kind, t.leaf, t.t, t.episode) for t in result.triggers if t.new_episode}


def _opened(result, variant: str) -> set[tuple]:
    # The logged records (triggers once per episode, every would_trigger) that open ``variant``.
    logged = [(t.kind, t) for t in result.triggers if t.new_episode] + [(s.rule, s) for s in result.shadow]
    return {(rule, item.leaf, item.t, item.episode) for rule, item in logged if variant in item.opens}


@pytest.mark.parametrize("scenario", [tag for tag, _reps, _what in VPROTO_FIXTURES])
def test_logged_variants_open_exactly_where_each_variant_would_trigger(scenario: str) -> None:
    # One default run (the guard off) logs, per episode, where each variant
    # would have opened its trigger: the rules alone, the 0.5 guard (a run
    # with the key at 0.5) and the onset variant (a reference core that acts
    # on it).  Variant-only records come only when the guard is off here.
    fixture = replay.load_fixture(f"vproto_{scenario}.json.gz")
    for rep in fixture["reps"]:
        _a, default, _ = _vproto_run(rep)
        _b, guarded, _ = _vproto_run(rep, constants=GUARDED)
        mihomo, feed = replay.vproto_mihomo(rep, t_end=rep["t_exit"])
        onset_core = _OnsetGuardCore(tokenizer=TOKENIZER, tier3_members=tuple(rep["members"]), constants=GUARDED)
        onset = replay.drive(onset_core, feed, mihomo, t_start=rep["t_attach"], t_end=rep["t_exit"])

        assert _opened(default, VARIANT_RULES) == _logged_triggers(default), rep["label"]
        assert _opened(default, VARIANT_SHARE) == _logged_triggers(guarded), rep["label"]
        assert _opened(default, VARIANT_ONSET) == _logged_triggers(onset), rep["label"]
        for variant in (VARIANT_RULES, VARIANT_SHARE, VARIANT_ONSET):
            assert _opened(guarded, variant) == _opened(default, variant), (rep["label"], variant)
        # A variant-only record opens a logged-only variant, never the rules'
        # trigger; at 0.5 the guard's own trigger is the 0.5 variant's.
        assert all(s.opens and VARIANT_RULES not in s.opens for s in default.shadow if s.variant_only)
        assert [s for s in guarded.shadow if s.variant_only and VARIANT_SHARE in s.opens] == []



# ---------------------------------------------------------------------------
# CPU budget (V4 follow-up): the poll-period stretch and its throttle

MIN_SCALE = DEFAULT_POST_DIAL_CONSTANTS.min_poll_scale
# The latest an AGED observation lands at any stretch: the AGED poll's due
# plus its stretched floor (one smallest stretched period after the previous poll).
AGED_LATEST_S = DEFAULT_POST_DIAL_CONSTANTS.aged_poll_offset_s + 0.1 * MIN_SCALE


def _slow_download(scale: float | None):
    scenario = Scenario()
    t_open = T0 + 1.0
    scenario.conn(t_open, host="slow.example", life=None, up=517, down=4000, down_at=3.5)
    core = _core()
    if scale is not None:
        core.set_poll_scale(scale)
    return t_open, core, scenario.run(core, t_end=T0 + 30.0)


@pytest.mark.parametrize("scale", [1.0, 0.5, float("nan"), float("inf")])
def test_a_poll_scale_of_one_or_less_keeps_the_schedule_exactly(scale: float) -> None:
    _t, _plain_core, plain = _slow_download(None)
    _t, core, same = _slow_download(scale)
    assert core.poll_scale == 1.0
    assert same.polls == plain.polls
    assert same.records == plain.records


@pytest.mark.parametrize("scale", [1.001, 1.12, 1.4, 1.69])
def test_an_engaged_poll_scale_is_at_least_the_smallest_stretch(scale: float) -> None:
    # 1.7 x POLL_FAST_S = POLL_GAP_MAX_S + 20 ms: a stretched gap is never a bracket,
    # even read on a wall clock that ticks in 15.6 ms steps.
    assert MIN_SCALE == pytest.approx(1.7)
    constants = DEFAULT_POST_DIAL_CONSTANTS
    assert constants.poll_fast_s * MIN_SCALE - 0.0157 > constants.poll_gap_max_s
    core = _core()
    core.set_poll_scale(scale)
    assert core.poll_scale == MIN_SCALE
    core.set_poll_scale(2.5)
    assert core.poll_scale == 2.5
    core.set_poll_scale(1.0)
    assert core.poll_scale == 1.0
    assert constants_with(poll_gap_max_s=0.4).min_poll_scale == pytest.approx(4.2)
    assert constants_with(poll_fast_s=0.0).min_poll_scale == 1.0


def test_a_poll_scale_stretches_the_fast_window_the_aged_floor_and_the_slow_period() -> None:
    t_open, _plain_core, plain = _slow_download(None)
    t_open, core, stretched = _slow_download(3.0)
    polls = stretched.polls
    gaps = [b - a for a, b in zip(polls, polls[1:])]

    assert core.poll_scale == 3.0
    assert polls[0] == plain.polls[0]  # the first poll after dormancy is still immediate
    # No two polls closer than the smallest stretched period (the AGED floor),
    # which is past POLL_GAP_MAX_S.
    assert min(gaps) >= 0.1 * MIN_SCALE - 1e-6
    fast = [b - a for a, b in zip(polls, polls[1:]) if b < t_open + DEFAULT_POST_DIAL_CONSTANTS.fast_window_s]
    assert fast and min(fast) >= 0.3 - 1e-6 and max(fast) <= 0.3 + 0.004 + 1e-6
    # The AGED poll keeps its due unless the (smallest) stretched floor delays it.
    assert any(t_open + 3.02 - 1e-6 <= t <= t_open + AGED_LATEST_S + 0.004 for t in polls)
    slow = [b - a for a, b in zip(polls, polls[1:]) if a > t_open + 3.5]
    assert slow and all(3.0 - 1e-6 <= gap <= 3.0 + 0.004 + 1e-6 for gap in slow)
    assert len(polls) < len(plain.polls) / 2
    assert core.need_poll(T0 + 30.0, T0 + 30.0) is False  # dormant afterwards, as before
    assert stretched.triggers == plain.triggers == []


def _zombie_quartet() -> Scenario:
    # Four tunnels that send a ClientHello, get no byte back and die after
    # 0.7 s: every poll sees them alive, so only a poll gap brackets the end.
    scenario = Scenario()
    for index in range(4):
        scenario.conn(T0 + 1.0 + index * 0.05, host=f"quartet{index % 2}.example", life=0.7)
    return scenario


@pytest.mark.parametrize("scale", [1.12, 3.0])
def test_a_stretched_schedule_leaves_would_be_failed_fast_unknown_and_never_triggers(scale: float) -> None:
    plain = _zombie_quartet().run(_core(), t_end=T0 + 8.0)
    plain_outcomes = [record for record in plain.records if record.get("ev") == "outcome"]
    assert [record["class"] for record in plain_outcomes].count("FAILED_FAST") == 4
    assert [trigger for trigger in plain.triggers if trigger.kind == RULE_FAST]

    core = _core()
    core.set_poll_scale(scale)  # at least 170 ms between polls > POLL_GAP_MAX_S
    stretched = _zombie_quartet().run(core, t_end=T0 + 8.0)
    outcomes = [record for record in stretched.records if record.get("ev") == "outcome"]

    assert not [record for record in outcomes if record["class"] in {"FAILED_FAST", "UNANSWERED_SHORT"}]
    unknown = [record for record in outcomes if record["class"] == "UNKNOWN"]
    assert len(unknown) == 4
    assert {record["would"] for record in unknown} <= {"FAILED_FAST", "UNANSWERED_SHORT"}
    assert all(record["bracket_ok"] is False for record in unknown)
    assert stretched.triggers == [] and stretched.shadow == []
    assert core.summary(T0 + 8.0)["unknown"] == 4


@pytest.mark.parametrize(("scale", "reps"), [(1.12, None), (3.0, None), (10.0, 2)])
@pytest.mark.parametrize("scenario", [tag for tag, _reps, _what in VPROTO_FIXTURES])
def test_a_stretched_schedule_adds_no_evidence_and_no_trigger_on_the_fixtures(
    scenario: str, scale: float, reps: int | None
) -> None:
    fixture = replay.load_fixture(f"vproto_{scenario}.json.gz")
    for rep in fixture["reps"][:reps]:
        _plain_core, plain, plain_outcomes = _vproto_run(rep)
        core, stretched, outcomes = _vproto_run(rep, poll_scale=scale)
        assert core.poll_scale == max(scale, MIN_SCALE)
        gaps = [b - a for a, b in zip(stretched.polls, stretched.polls[1:])]
        # Every gap is past POLL_GAP_MAX_S, the AGED poll's included.
        assert not gaps or min(gaps) >= 0.1 * MIN_SCALE - 1e-6, rep["label"]
        for record in stretched.records:
            if record.get("ev") != "outcome":
                continue
            if record["class"] in {"FAILED_FAST", "UNANSWERED_SHORT"}:
                # No two polls bracket an ending any more: only the open line
                # can, for an ending within POLL_GAP_MAX_S of the open.
                assert record["life_ms"] <= 150.0 + 0.05, (rep["label"], record)
            if record["class"] == "AGED":
                assert record["life_ms"] <= 1000.0 * AGED_LATEST_S + 5.0, (rep["label"], record)
        races = 0
        for index, value in outcomes.items():
            if value in {"FAILED_FAST", "UNANSWERED_SHORT", "AGED"} and plain_outcomes.get(index) != value:
                # Only the known AGED poll-phase race: the stretched poll came
                # before the first download and agrees with the prototype.
                line = rep["lines"][index]
                assert value == line[3] == "AGED" and _aged_poll_phase_race(line), (rep["label"], index)
                races += 1
        assert {(t.kind, t.leaf) for t in stretched.triggers} <= {(t.kind, t.leaf) for t in plain.triggers}, rep["label"]
        # Variant-only records are left out: the stretch moves the share counts
        # within an episode the rules open in both runs (checked just above).
        new_shadow = {(t.rule, t.leaf) for t in stretched.shadow if not t.variant_only} - {
            (t.rule, t.leaf) for t in plain.shadow if not t.variant_only
        }
        # A race line's AGED can be a single AGED on 443 (a shadow rule); nothing else is new.
        assert new_shadow <= ({(RULE_AGED_SINGLE, "n1")} if races else set()), (rep["label"], new_shadow)


def _aged_pair(scale: float, a_off: float, gap: float, busy: float):
    """Two never-answered tunnels on n1 ``gap`` s apart, beside two answered
    connections whose 1 Hz polls set the phase before the AGED polls."""

    scenario = Scenario()
    scenario.conn(T0 + 1.0, host="busy.example", life=50.0, down=5000, down_at=0.2, up=300)
    scenario.conn(T0 + 1.0 + busy, host="busy2.example", life=50.0, down=9000, down_at=0.3, up=300)
    scenario.conn(T0 + 1.0 + a_off, host="a.example", life=30.0)
    scenario.conn(T0 + 1.0 + a_off + gap, host="b.example", life=30.0)
    core = _core()
    core.set_poll_scale(scale)
    result = scenario.run(core, t_end=T0 + 45.0)
    ages = [record["life_ms"] / 1000.0 for record in result.records if record.get("ev") == "outcome" and record["class"] == "AGED"]
    return ages, {trigger.kind for trigger in result.triggers}


@pytest.mark.parametrize("scale", [3.0, 5.0, 10.0])
def test_a_stretch_never_starves_the_aged_poll_or_bunches_aged_evidence(scale: float) -> None:
    # A stretched 1 Hz poll just before the AGED due used to push the AGED
    # poll past the point where its candidate was dropped, so the AGED
    # observation waited for the next stretched poll (up to 10 s later) and
    # two AGED events 5-9 s apart could newly make the AGED rule (2 in 5 s).
    rng = random.Random(7)
    for _trial in range(12):
        a_off, gap, busy = rng.uniform(1.0, 4.0), rng.uniform(5.2, 9.0), rng.uniform(0.0, 4.0)
        plain_ages, plain_kinds = _aged_pair(1.0, a_off, gap, busy)
        ages, kinds = _aged_pair(scale, a_off, gap, busy)
        assert len(ages) == len(plain_ages) == 2
        assert all(DEFAULT_POST_DIAL_CONSTANTS.t_age_s <= age <= AGED_LATEST_S + 1e-6 for age in ages), ages
        assert kinds <= plain_kinds, (a_off, gap, busy)
    # The case the review found: A's AGED at age 6.85 s and an AGED trigger at 10x.
    ages, kinds = _aged_pair(scale, 1.354, 6.847, 3.029)
    assert max(ages) <= AGED_LATEST_S + 1e-6 and RULE_AGED not in kinds


def test_a_stretch_keeps_the_demotion_watch_at_one_hertz() -> None:
    def watched(scale: float) -> list[float]:
        core = _core()
        core.set_poll_scale(scale)
        mihomo = replay.FakeMihomo()
        mihomo.add(replay.FakeConn("ws", T0, 47000, "ws.example", 443, "n1", up=replay.step(T0, 900), down=replay.step(T0 + 0.1, 900)))
        core.ingest_open(_parse(replay.open_line(T0, sport=47000, host="ws.example")), now_wall=T0, now_perf=T0)
        core.ingest_snapshot(mihomo.snapshot(T0 + 0.2), T0 + 0.2, T0 + 0.2)
        t = T0 + 20.0  # past the (stretched) linger
        assert core.need_poll(t, t) is False
        core.start_demotion_watch("n1", t_demote_wall=t, now_perf=t)
        polls = []
        while t < T0 + 30.0:
            due = core.next_poll_time(t, t)
            assert due is not None
            t = max(t, due)
            core.ingest_snapshot(mihomo.snapshot(t), t, t)
            polls.append(t)
        return polls

    plain = watched(1.0)
    assert watched(10.0) == plain
    assert all(b - a == pytest.approx(1.0) for a, b in zip(plain[1:], plain[2:]))


@pytest.mark.parametrize("scale", [1.0, 3.0, 7.0, 9.0])
def test_a_stretch_extends_the_stream_watch_by_the_extra_slow_period(scale: float) -> None:
    # A stream's last byte change at +10 s: 1 Hz polls go on for STREAM_WATCH_S
    # after the poll that saw it.  Stretched, the watch grows by the extra
    # 1 Hz period, so the last poll still comes less than one 1 Hz period
    # before the watch runs out (at 7 x a 60 s watch would end at 56 s).
    scenario = Scenario()
    t_open = T0 + 1.0
    scenario.conn(
        t_open,
        host="video.example",
        life=None,
        up=900,
        down_fn=lambda t: 0 if t < t_open + 0.2 else int(min(t, t_open + 10.0) - t_open) * 400_000,
    )
    core = _core()
    core.set_poll_scale(scale)
    polls = scenario.run(core, t_end=T0 + 160.0).polls
    seen = min(t for t in polls if t >= t_open + 10.0)
    watch = DEFAULT_POST_DIAL_CONSTANTS.stream_watch_s
    assert watch - 1.0 <= max(polls) - seen <= watch + (scale - 1.0) + 1e-6


def test_a_late_pass_still_takes_the_stretched_aged_poll() -> None:
    # The AGED candidate outlives its stretched floor by one fast period, so a
    # loop pass that comes late (a slow poll, a contended GIL handoff) still
    # polls for it instead of dropping it at T_AGE_S + 0.2 s.
    core = _core()
    core.set_poll_scale(3.0)
    mihomo = replay.FakeMihomo()
    mihomo.add(replay.FakeConn("z", T0, 48000, "z.example", 443, "n1", up=replay.step(T0, 517)))
    core.ingest_open(_parse(replay.open_line(T0, sport=48000, host="z.example")), now_wall=T0, now_perf=T0)
    core.ingest_snapshot(mihomo.snapshot(T0 + 2.99), T0 + 2.99, T0 + 2.99)  # age 2.99: not AGED yet
    due = core.next_poll_time(T0 + 2.99, T0 + 2.99)
    assert due == pytest.approx(T0 + 2.99 + 0.1 * MIN_SCALE, abs=1e-6)  # the AGED poll's stretched floor
    late = T0 + 3.27
    assert core.next_poll_time(late, late) == pytest.approx(due, abs=1e-6)  # still due: poll now
    core.ingest_snapshot(mihomo.snapshot(late), late, late)
    assert [record["class"] for record in core.drain_records() if record.get("ev") == "outcome"] == ["AGED"]


class _CpuSim:
    """A synthetic observer thread: ``share`` percent of one core in polls at
    scale 1 (1/scale of it stretched) plus ``background`` the stretch cannot
    remove.  With ``split`` the throttle is told the poll part; without, it
    counts everything as poll CPU (``note(perf, cpu)``)."""

    def __init__(
        self,
        throttle: PostDialCpuThrottle,
        *,
        background: float = 0.0,
        step: float = 0.1,
        split: bool = False,
    ) -> None:
        self.throttle = throttle
        self.background = background
        self.step = step
        self.split = split
        self.cpu = 0.0
        self.poll_cpu = 0.0
        self.trace: list[tuple[float, float]] = []  # (perf, cumulative cpu)
        self.changes: list[tuple[float, float]] = []  # (perf, new scale)

    def run(self, start: float, end: float, *, share: float | Callable[[float], float]) -> list[dict[str, Any]]:
        records = []
        steps = int(round((end - start) / self.step))
        for index in range(steps):
            t = start + index * self.step
            before = self.throttle.scale
            record = self.throttle.note(t, self.cpu, self.poll_cpu if self.split else None)
            if record is not None:
                records.append(record)
            if self.throttle.scale != before:
                self.changes.append((t, self.throttle.scale))
            self.trace.append((t, self.cpu))
            now = share(t) if callable(share) else share
            polls = now / self.throttle.scale / 100.0 * self.step
            self.poll_cpu += polls
            self.cpu += polls + (self.background / 100.0 * self.step if now > 0 else 0.0)
        return records

    def share(self, start: float, end: float) -> float:
        inside = [(t, cpu) for t, cpu in self.trace if start - 1e-6 <= t <= end + 1e-6]
        (t0, c0), (t1, c1) = inside[0], inside[-1]
        return 100.0 * (c1 - c0) / (t1 - t0)

    def worst_window(self, start: float, window: float = 10.0) -> float:
        worst = 0.0
        times = [t for t, _cpu in self.trace]
        for index, (t, cpu) in enumerate(self.trace):
            if t < start:
                continue
            first = bisect.bisect_left(times, t - window)
            if t > times[first]:
                worst = max(worst, 100.0 * (cpu - self.trace[first][1]) / (t - times[first]))
        return worst


def test_cpu_throttle_engages_above_the_budget_and_releases_below() -> None:
    throttle = PostDialCpuThrottle(2.0)  # the defaults: 10 s window, 5 s settle, 60 s hold
    sim = _CpuSim(throttle)
    assert sim.run(0.0, 30.0, share=1.5) == [] and throttle.scale == 1.0  # under budget

    records = sim.run(30.0, 90.0, share=6.0)  # three times the budget
    assert len(records) == 1  # one record per episode
    record = records[0]
    assert record["ev"] == "observer_throttled"
    assert set(record) == {
        "ev", "budget_pct", "measured_pct", "poll_pct", "window_s", "scale", "period_ms", "slow_period_ms",
    }
    assert all(isinstance(value, (int, float)) for key, value in record.items() if key != "ev")
    assert record["budget_pct"] == 2.0 and record["measured_pct"] > 2.0
    assert record["scale"] == pytest.approx(MIN_SCALE)  # an engaged stretch is at least 1.7
    assert 30.0 < sim.changes[0][0] <= 30.0 + 2.0  # engaged within the window's first seconds
    assert record["period_ms"] == pytest.approx(100.0 * record["scale"], abs=0.1)
    assert record["slow_period_ms"] == pytest.approx(1000.0 * record["scale"], abs=1.0)
    # Just enough: the last window is under the budget, at the target.
    assert 0.8 * 2.0 <= sim.share(80.0, 89.9) <= 2.0
    assert throttle.scale == pytest.approx(6.0 / 1.8, rel=0.02)
    # Judged on readings since each change: no overshoot past what 6 % needs.
    assert max(scale for _t, scale in sim.changes) <= 6.0 / 1.8 * 1.02
    changes = [t for t, _scale in sim.changes]
    assert all(b - a >= 5.0 - 1e-9 for a, b in zip(changes, changes[1:]))  # settle between changes

    # The load drops: the stretch holds while the worst window of the last
    # minute still needs it, then goes back to 1.
    assert sim.run(90.0, 145.0, share=0.5) == []
    assert throttle.scale > 1.0
    assert sim.run(145.0, 175.0, share=0.5) == []
    assert throttle.scale == 1.0  # released
    summary = throttle.summary()
    assert summary["throttle_episodes"] == 1 and summary["throttle_changes"] == len(sim.changes)
    assert summary["throttle_scale"] == 1.0 and summary["throttle_scale_peak"] > 2.5
    assert summary["cpu_budget_pct"] == 2.0 and summary["observer_cpu_pct"] > 0.0
    assert summary["observer_poll_cpu_pct"] == summary["observer_cpu_pct"]  # no split given
    assert summary["throttle_unreachable"] == 0

    sim.run(175.0, 205.0, share=6.0)  # a new overload is a new episode ...
    assert throttle.episodes_total == 2 and throttle.summary()["throttle_episodes"] == 1


def test_cpu_throttle_logs_at_most_one_record_per_interval_and_counts_every_episode() -> None:
    throttle = PostDialCpuThrottle(2.0, record_interval_s=300.0)
    sim = _CpuSim(throttle)
    records = []
    for start in (0.0, 120.0, 240.0, 360.0):  # an overload every 2 min, idle in between
        records += sim.run(start, start + 30.0, share=6.0)
        records += sim.run(start + 30.0, start + 120.0, share=0.0)
    assert throttle.episodes_total == 4
    assert [record["ev"] for record in records] == ["observer_throttled"] * 2  # at 0 s and after 300 s
    assert throttle.summary()["throttle_episodes"] == 4


@pytest.mark.parametrize(("period", "burst"), [(15.0, 5.0), (15.0, 10.0), (30.0, 10.0), (60.0, 10.0)])
def test_cpu_throttle_holds_one_stretch_under_periodic_bursts(period: float, burst: float) -> None:
    # Page loads: a 6 s burst every ``period`` s, idle in between.  The
    # stretch is held through the quiet phases instead of engaging and
    # releasing per burst, and every 10 s window keeps to the budget.
    throttle = PostDialCpuThrottle(2.0)
    sim = _CpuSim(throttle, split=True)
    records = sim.run(0.0, 1800.0, share=lambda t: burst if t % period < 6.0 else 0.0)
    assert throttle.episodes_total == 1 and len(records) == 1
    assert len(sim.changes) <= 3
    assert sim.worst_window(60.0) <= 2.0
    assert sim.share(0.0, 1799.9) <= 2.0


def test_cpu_throttle_sizes_the_stretch_on_the_poll_share_alone() -> None:
    # 3 % in polls plus 1 % the stretch cannot cut: one step to the scale
    # that leaves 0.8 % for polls beside the 1 %, no ratcheting.
    throttle = PostDialCpuThrottle(2.0)
    sim = _CpuSim(throttle, background=1.0, split=True)
    records = sim.run(0.0, 60.0, share=3.0)
    assert [scale for _t, scale in sim.changes] == [pytest.approx(3.0 / 0.8)]
    assert [record["ev"] for record in records] == ["observer_throttled"]
    assert records[0]["poll_pct"] == pytest.approx(3.0, rel=0.01)
    assert sim.share(50.0, 59.9) == pytest.approx(1.8, rel=0.02)


def test_cpu_throttle_does_not_stretch_further_than_a_stretch_can_help() -> None:
    # 2.5 % the stretch cannot cut already breaks the budget: the poll part
    # (0.5 %) is only cut to a tenth of the budget, the throttle says so once,
    # and it never pins the largest stretch.
    throttle = PostDialCpuThrottle(2.0)
    sim = _CpuSim(throttle, background=2.5, split=True)
    records = sim.run(0.0, 120.0, share=0.5)
    assert throttle.scale == pytest.approx(0.5 / 0.2, rel=0.02)
    assert max(scale for _t, scale in sim.changes) < 3.0
    assert [record["ev"] for record in records] == ["observer_throttled", "observer_budget_unreachable"]
    unreachable = records[1]
    assert set(unreachable) == {"ev", "budget_pct", "measured_pct", "poll_pct", "other_pct", "window_s", "scale"}
    assert unreachable["other_pct"] == pytest.approx(2.5, rel=0.02)
    assert throttle.summary()["throttle_unreachable"] == 1
    # Nothing a stretch could cut at all: no stretch.
    idle_polls = PostDialCpuThrottle(2.0)
    sim = _CpuSim(idle_polls, background=3.0, split=True)
    records = sim.run(0.0, 60.0, share=0.1)
    assert idle_polls.scale == 1.0 and [record["ev"] for record in records] == ["observer_budget_unreachable"]


def test_cpu_throttle_with_a_zero_budget_never_stretches_but_still_measures() -> None:
    throttle = PostDialCpuThrottle(0.0)
    sim = _CpuSim(throttle)
    assert not throttle.enabled
    assert sim.run(0.0, 60.0, share=50.0) == []
    assert throttle.scale == 1.0 and sim.changes == []
    summary = throttle.summary()
    assert summary["cpu_budget_pct"] == 0.0 and summary["throttle_episodes"] == 0
    assert summary["observer_cpu_pct"] == pytest.approx(50.0, rel=0.01)


def test_cpu_throttle_counts_dormant_time_as_idle_and_is_deterministic() -> None:
    def run():
        throttle = PostDialCpuThrottle(2.0)
        sim = _CpuSim(throttle)
        records = sim.run(0.0, 20.0, share=1.0)
        # Dormant for 60 s (no reading), then a 3 s burst at 5 % of a core:
        # 1.5 % over the 10 s window, so no stretch.
        records += sim.run(80.0, 83.0, share=5.0)
        return throttle.scale, records, sim.changes

    scale, records, changes = run()
    assert (scale, records, changes) == (1.0, [], [])
    assert run() == (scale, records, changes)


def test_cpu_throttle_stops_at_its_largest_stretch_and_restarts_after_a_clock_step() -> None:
    throttle = PostDialCpuThrottle(2.0, scale_max=4.0)
    sim = _CpuSim(throttle, background=5.0)  # counted as poll CPU: over budget whatever the stretch
    records = sim.run(0.0, 60.0, share=5.0)
    assert [record["ev"] for record in records] == ["observer_throttled", "observer_budget_unreachable"]
    assert throttle.scale == 4.0
    assert sim.changes[-1][1] == 4.0 and sim.changes[-1][0] < 30.0  # then held at the cap
    # A clock that goes back restarts the measurement and keeps the stretch.
    assert throttle.note(1.0, 0.0) is None and throttle.scale == 4.0
    assert throttle.note(1.0 + 4.9, 0.001) is None and throttle.scale == 4.0
    # A clock that fails ends the budget and the stretch with it.
    throttle.disable()
    assert throttle.scale == 1.0 and throttle.enabled is False
    assert throttle.note(100.0, 50.0) is None and throttle.scale == 1.0


def test_cpu_throttle_takes_its_settings_from_the_constants() -> None:
    constants = constants_with(
        cpu_budget_pct=3.5, cpu_window_s=4.0, cpu_settle_s=9.0, cpu_hold_s=2.0, cpu_scale_max=6.0,
    )
    throttle = PostDialCpuThrottle.from_constants(constants, record_interval_s=60.0)
    assert (throttle.budget_pct, throttle.window_s, throttle.settle_s, throttle.scale_max) == (3.5, 4.0, 4.0, 6.0)
    assert throttle.hold_s == 4.0  # never shorter than the window
    assert throttle.scale_min == pytest.approx(MIN_SCALE) and throttle.record_interval_s == 60.0
    assert (throttle.fast_period_s, throttle.slow_period_s) == (0.1, 1.0)
    assert PostDialCpuThrottle.from_constants(DEFAULT_POST_DIAL_CONSTANTS).budget_pct == 2.0
    assert PostDialCpuThrottle.from_constants(DEFAULT_POST_DIAL_CONSTANTS).hold_s == 60.0
    assert PostDialCpuThrottle(2.0).scale_min == pytest.approx(MIN_SCALE)
    assert PostDialCpuThrottle(float("nan")).enabled is False
    assert "scale=1" in repr(throttle)


# ---------------------------------------------------------------------------
# Raw ingest (V4 cost fix): equivalence with the classic path, edge cases
#
# The module-wide ``core_mode`` parametrisation already replays every test
# above, fixtures included, through ``DualCore`` with Go-compact bodies.  The
# class below adds the other encodings, randomised workloads and the corner
# cases of the raw framing.


def _dual(**kwargs: Any) -> DualCore:
    kwargs.setdefault("tier3_members", ("n1", "n2", "n3"))
    return DualCore(tokenizer=TOKENIZER, **kwargs)


def _uuid(rng: random.Random) -> str:
    text = "%032x" % rng.getrandbits(128)
    return f"{text[:8]}-{text[8:12]}-4{text[13:16]}-a{text[17:20]}-{text[20:]}"


_FUZZ_LEAVES = ("n1", "n2", "n3", "tier1-x", "HK 01 [IPLC] x2", 'q"uote\\back', "\U0001f1ed\U0001f1f0 香港 02")
_FUZZ_HOSTS = ("a.example", "b.example", "视频.example", "c.example", "2001:db8::5", "203.0.113.9", "sys.test")


class _FuzzConn:
    """One connection of a randomised workload: its entry and its open line."""

    def __init__(self, rng: random.Random, t: float, *, weird_ids: bool) -> None:
        self.t_open = t
        self.cid = _uuid(rng)
        if weird_ids and rng.random() < 0.05:
            self.cid = rng.choice(["c%d" % rng.randrange(10**6), "x" * 40 + str(rng.random()), "id\\u0041b"])
        self.kind = rng.choices(
            ["user", "direct", "app_line", "app_entry", "inner", "udp", "legacy"],
            weights=[60, 10, 5, 5, 4, 6, 10],
        )[0]
        self.sip = rng.choice(["127.0.0.1", "127.0.0.1", "::1", "192.168.1.9"])
        self.sport = 50000 + rng.randrange(400)  # reused often: late binds and join ties
        self.host = rng.choice(_FUZZ_HOSTS)
        self.dport = rng.choice([443, 443, 80])
        self.leaf = rng.choice(_FUZZ_LEAVES)
        self.gateway = rng.random() < 0.08
        self.reordered = rng.random() < 0.04  # download before upload: no counter frame
        self.weird_counters = rng.random() < 0.03
        self.t_close: float | None = None
        self.steps: list[tuple[float, int, int]] = []
        up = down = 0
        pattern = rng.choices(["zombie", "healthy", "stream", "waiting", "idle"], weights=[30, 30, 10, 15, 15])[0]
        if pattern != "idle":
            up += rng.randint(300, 1900)
            self.steps.append((t + rng.uniform(0.0, 0.02), up, down))
        if pattern == "zombie":
            if rng.random() < 0.7:
                self.t_close = t + rng.uniform(0.004, 1.6)
        elif pattern == "idle":
            if rng.random() < 0.4:
                self.steps.append((t + rng.uniform(1.0, 8.0), rng.randint(1, 900), 0))
            if rng.random() < 0.5:
                self.t_close = t + rng.uniform(0.5, 15.0)
        else:
            tt = t + rng.uniform(0.02, 2.8)
            for _ in range(rng.randint(1, 6)):
                for _step in range(rng.randint(1, 5)):
                    tt += rng.uniform(0.02, 0.3)
                    if rng.random() < 0.7:
                        up += rng.randint(50, 3000)
                    down += rng.randint(1, 90_000) if pattern != "stream" else rng.randint(20_000, 90_000)
                    self.steps.append((tt, up, down))
                tt += rng.uniform(0.2, 6.0)
            if pattern == "waiting":
                self.steps.append((tt, up + rng.randint(100, 900), down))
            if rng.random() < 0.6:
                self.t_close = tt + rng.uniform(0.05, 10.0)
        lag = rng.choices(
            [0.0, rng.uniform(0.0, 0.02), rng.uniform(0.02, 0.3), rng.uniform(0.3, 1.4), None],
            weights=[20, 40, 25, 10, 5],
        )[0]
        self.line: tuple[float, str] | None = None
        if self.kind in {"user", "direct", "app_line"} and lag is not None:
            outer, rule, leaf = "GENERAL", "Match", self.leaf
            if self.kind == "direct":
                outer, rule, leaf = "DIRECT", "GeoSite(cn)", None
            elif self.kind == "app_line":
                outer, rule = "FINAL-EXIT", None
            raw = replay.open_line(
                t, sport=self.sport, host=self.host, port=self.dport, leaf=leaf,
                outer=outer, rule=rule, source_ip=self.sip,
            )
            self.line = (t + lag, raw)

    def counters(self, t: float) -> tuple[int, int]:
        up = down = 0
        for t_step, step_up, step_down in self.steps:
            if t_step > t:
                break
            up, down = step_up, step_down
        return up, down

    def listed(self, t: float) -> bool:
        return t >= self.t_open + 0.0005 and (self.t_close is None or t < self.t_close)

    def entry(self, t: float) -> dict[str, Any]:
        up, down = self.counters(t)
        if self.kind == "direct":
            chains = ["DIRECT"]
        else:
            chains = [self.leaf, "TIER3-AUTO", "PROXY-FALLBACK", "FINAL-EXIT", "GENERAL"]
            if self.gateway:
                chains.insert(2, "EXIT-SELECT")
        inner = self.kind == "inner"
        metadata = {
            "network": "udp" if self.kind == "udp" else "tcp",
            "type": "Inner" if inner else "HTTPS",
            "sourceIP": "" if inner else self.sip,
            "destinationIP": "203.0.113.7",
            "sourcePort": "0" if inner else str(self.sport),
            "destinationPort": str(self.dport),
            "inboundName": "source-probe" if self.kind == "app_entry" else "DEFAULT-MIXED",
            "host": self.host,
            "specialProxy": "",
        }
        upload: Any = up
        download: Any = down
        if self.weird_counters:
            upload, download = float(up), str(down)
        entry: dict[str, Any] = {"id": self.cid, "metadata": metadata}
        if self.reordered:
            entry.update({"download": download, "upload": upload})
        else:
            entry.update({"upload": upload, "download": download})
        entry.update(
            {
                "start": replay.rfc3339(self.t_open),
                "chains": chains,
                "providerChains": ["TIER3B-RUNTIME"] + [""] * (len(chains) - 1),
                "rule": "Match",
                "rulePayload": "",
            }
        )
        return entry


def _fuzz(seed: int, *, seconds: float = 60.0, rate: float = 8.0, **core_kwargs: Any) -> DualCore:
    """A randomised workload on the core's own schedule, both paths in lockstep.

    Covers user, DIRECT, app, inner and UDP entries, lines drained late or
    never, reused source ports, bursts, streams, WAITING episodes, Gateway
    chains, reordered and non-integer counters, odd ids, route context and
    epoch changes, failed polls, demotion watches with closes, cap
    evictions, a machine sleep, wall-clock steps back, and polls that reach
    the raw core as JSON (other framing) or as a dict (the classic path).
    """

    rng = random.Random(seed)
    dual = _dual(style="compact", seed=seed, tier3_members=_FUZZ_LEAVES[:3] + _FUZZ_LEAVES[4:6], **core_kwargs)
    if seed % 4 == 3:
        dual.classic._MAX_TRACKED = dual.raw._MAX_TRACKED = 48  # evicts listed entries
    conns: list[_FuzzConn] = []
    lines: list[tuple[float, str]] = []
    wall = T0
    offset = T0 - 5000.0  # perf = wall - offset, until a clock event moves one alone
    t_end = T0 + seconds
    next_open = T0
    watches: list[str] = []
    slept = False
    while wall < t_end:
        while next_open <= wall:
            conn = _FuzzConn(rng, next_open, weird_ids=seed % 6 == 5)
            conns.append(conn)
            if conn.line is not None:
                lines.append(conn.line)
            next_open += rng.expovariate(rate)
        lines.sort()
        while lines and lines[0][0] <= wall:
            _t_line, raw = lines.pop(0)
            record = parse_open_line(raw, 0.0, tokenizer=TOKENIZER, origin_rules=RULES)
            if record is not None:
                dual.ingest_open(record, now_wall=wall, now_perf=wall - offset)
        perf = wall - offset
        due = dual.next_poll_time(perf, wall)
        if due is None or due > perf:
            events = [next_open]
            if lines:
                events.append(lines[0][0])
            if due is not None:
                events.append(due + offset)
            wall = max(wall + 0.001, min(events))
            continue
        roll = rng.random()
        if roll < 0.03:
            dual.note_poll_failed(wall, perf, cost_s=0.002, reason="controller_error")
        elif roll < 0.04:
            dual.ingest_snapshot(None, wall, perf)
        else:
            listed = [conn.entry(wall) for conn in conns if conn.listed(wall)]
            payload = {"downloadTotal": 1, "uploadTotal": 2, "connections": listed or None, "memory": 3}
            style = rng.choices(["compact", "shuffled", "ascii", "pretty", "classic"], weights=[60, 20, 10, 3, 7])[0]
            cost = rng.choice([0.002, 0.004, 0.01, 0.045])
            dual.ingest_snapshot(payload, wall, perf, cost_s=cost, style=style)
            dual.note_poll_cost(cost + rng.uniform(0.0, 0.02))
            dual.evaluate_triggers(wall)
        roll = rng.random()
        if roll < 0.03:
            members = tuple(rng.sample(_FUZZ_LEAVES, rng.randint(1, len(_FUZZ_LEAVES))))
            gateways = ("EXIT-SELECT",) if rng.random() < 0.7 else ()
            dual.set_route_context(tier3_members=members, gateway_groups=gateways)
        elif roll < 0.04:
            dual.note_network_epoch(wall - rng.uniform(0.0, 20.0))
        elif roll < 0.06:
            leaf = rng.choice(_FUZZ_LEAVES)
            dual.start_demotion_watch(leaf, t_demote_wall=wall - rng.uniform(0.0, 5.0), now_perf=perf)
            watches.append(leaf)
        if watches and rng.random() < 0.3:
            leaf = rng.choice(watches)
            stuck = dual.stuck_connection_ids(leaf, wall - 2.0, wall)
            if stuck.ids and rng.random() < 0.5:
                dual.mark_closed(stuck.ids[:2])
            dual.active_demotion_watches(perf)
        roll = rng.random()
        if roll < 0.01 and not slept and wall > T0 + 0.7 * seconds:
            slept = True
            wall += 40.0  # the machine slept and the perf clock stood still
            offset += 40.0
            t_end += 40.0
        elif roll < 0.012:
            step = rng.uniform(0.05, 1.5)  # the wall clock stepped back; perf did not
            wall -= step
            offset -= step
        wall += 0.001
    dual.check_state()
    dual.summary(wall)
    dual.drain_records()
    return dual


def _entry(cid: str, sport: int, t_open: float, *, up: int = 517, down: int = 0, leaf: str = "n1",
           chains_tail: tuple[str, ...] = ("TIER3-AUTO", "GENERAL"), network: str = "tcp",
           source_ip: str = "127.0.0.1", host: str = "e.example") -> dict[str, Any]:
    return {
        "id": cid,
        "metadata": {
            "network": network, "type": "HTTPS", "sourceIP": source_ip, "sourcePort": str(sport),
            "destinationPort": "443", "inboundName": "DEFAULT-MIXED", "host": host, "specialProxy": "",
        },
        "upload": up,
        "download": down,
        "start": replay.rfc3339(t_open),
        "chains": [leaf, *chains_tail],
        "rule": "Match",
        "rulePayload": "",
    }


def _both(dual: DualCore, body: bytes, t: float) -> Any:
    """Hand-written bytes: the classic core gets what the keep-alive client
    decodes, the raw core the bytes themselves."""

    try:
        payload = decode_connections_body(body)
    except ValueError:
        payload = None
    if payload is None:
        with pytest.raises(ValueError):
            dual.raw.ingest_raw(body, t, t)
        return None
    expected = dual.classic.ingest_snapshot(payload, t, t)
    assert dual.raw.ingest_raw(body, t, t) == expected
    dual.check_state()
    return expected


class TestRawIngest:
    """Runs once: the class overrides the module's classic/raw parametrisation."""

    @pytest.fixture(autouse=True)
    def core_mode(self):
        _CORE_MODE["mode"] = "classic"
        yield "dual"

    # -- recorded fixtures, other encodings of the same snapshots -----------

    @pytest.mark.parametrize("style", ["compact", "ascii", "shuffled"])
    @pytest.mark.parametrize("tag", ["R1", "R2", "R3", "R4", "R5"])
    def test_x1_fixture_outcomes_and_triggers_are_identical(self, tag: str, style: str) -> None:
        fixture = replay.load_fixture(f"x1_{tag}.json.gz")
        dual = _dual(tier3_members=("A", "B", "C"), style=style)
        lines = fixture["lines"]
        index = 0
        triggers = []
        entries = 0
        for t, snapshot in replay.x1_snapshots(fixture):
            while index < len(lines) and lines[index][0] <= t:
                record = parse_open_line(lines[index][1], lines[index][0], tokenizer=TOKENIZER, origin_rules=OriginRules())
                if record is not None:
                    dual.ingest_open(record, now_wall=lines[index][0], now_perf=lines[index][0])
                index += 1
            entries += len(snapshot["connections"])
            dual.ingest_snapshot(snapshot, t, t, cost_s=0.003)
            # DualCore asserts both paths return the same triggers, times included.
            triggers.extend(dual.evaluate_triggers(t).triggers)
        records = dual.drain_records()
        counts = dual.raw.raw_counts
        assert counts["json_polls"] == 0 and counts["fast_polls"] == dual.polls
        # Fewer observations than the classic loop's one per listed entry.
        assert counts["ticks"] + counts["counters"] + counts["parsed"] < entries
        assert triggers and [record for record in records if record["ev"] == "outcome"]

    @pytest.mark.parametrize("style", ["ascii", "shuffled"])
    @pytest.mark.parametrize("scenario", [tag for tag, _reps, _what in VPROTO_FIXTURES])
    def test_vproto_fixture_outcomes_and_triggers_are_identical(self, scenario: str, style: str) -> None:
        fixture = replay.load_fixture(f"vproto_{scenario}.json.gz")
        for rep in fixture["reps"]:
            mihomo, feed = replay.vproto_mihomo(rep, t_end=rep["t_exit"])
            dual = _dual(tier3_members=tuple(rep["members"]), style=style)
            replay.drive(dual, feed, mihomo, t_start=rep["t_attach"], t_end=rep["t_exit"])
            dual.check_state()
            assert dual.raw.raw_counts["json_polls"] == 0, rep["label"]
            assert dual.raw.raw_counts["fast_polls"] == dual.polls, rep["label"]

    # -- randomised workloads -------------------------------------------------

    @pytest.mark.parametrize("seed", range(24))
    def test_randomised_workloads_match_the_classic_path(self, seed: int) -> None:
        dual = _fuzz(seed)
        counts = dual.raw.raw_counts
        assert dual.polls > 30
        if seed % 4 == 3:
            # The 48-entry cap evicts listed entries, which the next poll
            # binds afresh from a parse: warm polls parse most entries, so
            # the core backs off to json.loads (and back) along the way.
            assert counts["backoffs"] and counts["fast_polls"]
        elif seed % 6 != 5:  # no id the memo cannot key
            assert counts["fast_polls"] > 5 * counts["json_polls"]
            assert counts["ticks"] and counts["counters"] and counts["parsed"]

    @pytest.mark.parametrize("seed", range(100, 108))
    def test_randomised_workloads_with_hair_trigger_time_rules(self, seed: int) -> None:
        # A 1 kB/s stream rate and sub-second WAITING thresholds make the
        # time rules act on most entries, so lazy observation is exercised
        # at its edges (entries turning hot and cold every few polls).
        constants = constants_with(
            stream_rate_bps=1000.0, t_wait_s=0.3, t_wait_trigger_s=0.6, linger_s=2.0, stream_watch_s=5.0,
        )
        dual = _fuzz(seed, seconds=40.0, constants=constants, act_rules=("FAST", "AGED", "WAITING"))
        assert dual.polls > 30

    # -- framing ----------------------------------------------------------------

    def test_decode_matches_the_keep_alive_client_and_never_quotes_the_body(self) -> None:
        assert decode_connections_body(b"") == {}
        assert decode_connections_body(b'{"connections":[]}') == {"connections": []}
        for bad in (b"private-destination.example", b'["private-destination.example"]', b'{"a":'):
            with pytest.raises(ValueError) as raised:
                decode_connections_body(bad)
            assert "private-destination" not in str(raised.value)

    def test_layout_accepts_only_mihomo_compact_framing(self) -> None:
        body = b'{"downloadTotal":1,"uploadTotal":2,"connections":[{"id":"a","x":1},{"id":"b","y":2}],"memory":3}\n'
        first, close = _raw_layout(body)
        assert body[first:close] == b'a","x":1},{"id":"b","y":2'
        assert _raw_layout(b'{"connections":null}') == (-1, -1)
        assert _raw_layout(b'{"downloadTotal":0,"connections":[],"memory":0}') == (-1, -1)
        assert _raw_layout(b'{"connections":[{"id":"a"}]}') is not None
        for other in (
            b'{"downloadTotal": 1, "connections": [{"id": "a"}]}',  # whitespace
            b'{"version":"x","connections":[{"id":"a"}]}',  # a string before the array
            b'{"connections":[{"id":"a"}],"connections":5}',  # the last key wins in JSON
            b'{"connections":[{"id":"a"}],"extra":[1]}',
            b'{"connections":{}}',
            b'{"memory":1}',
            b'[{"id":"a"}]',
        ):
            assert _raw_layout(other) is None, other

    def test_a_body_that_is_not_a_json_object_changes_nothing(self) -> None:
        core = _dual().raw
        for body in (
            b"not json",
            b"[]",
            b'{"connections":[{"id":"a","metadata":{]}',
            b'{"connections":[{"id":"a"}',
            # Mihomo's framing, but the second entry is not JSON.
            b'{"connections":[{"id":"a","x":1},{"id":"b","y":}]}',
        ):
            with pytest.raises(ValueError):
                core.ingest_raw(body, T0, T0)
        assert core._seq == 0 and core.summary(T0).get("polls", 0) == 0
        assert core._memo == {} and core._tracked == {}

    def test_counter_changes_skip_json_and_any_other_change_reparses(self) -> None:
        dual = _dual()
        entries = [_entry(f"c{index}", 41000 + index, T0 - 20.0, up=900, down=4000) for index in range(8)]

        def poll(t: float) -> None:
            dual.ingest_snapshot({"downloadTotal": 1, "connections": entries}, t, t)

        poll(T0)
        assert dual.raw.raw_counts["parsed"] == 8
        for entry in entries[:3]:
            entry["download"] += 1000
        poll(T0 + 1.0)
        assert dual.raw.raw_counts["counters"] == 3 and dual.raw.raw_counts["parsed"] == 8
        entries[4]["chains"] = ["n2", "TIER3-AUTO", "GENERAL"]
        entries[5]["metadata"]["host"] = "changed.example"
        poll(T0 + 2.0)
        assert dual.raw.raw_counts["parsed"] == 10
        poll(T0 + 3.0)  # nothing changed: nothing parsed or observed
        assert dual.raw.raw_counts["parsed"] == 10 and dual.raw.raw_counts["counters"] == 3
        assert dual.raw.raw_counts["json_polls"] == 0
        dual.check_state()

    def test_counters_that_are_not_plain_integers_are_parsed_as_json(self) -> None:
        dual = _dual()
        t = T0
        head = b'{"downloadTotal":1,"connections":['
        entry = (
            b'{"id":"w1","metadata":{"network":"tcp","type":"HTTPS","sourceIP":"127.0.0.1","sourcePort":"40100",'
            b'"destinationPort":"443","inboundName":"DEFAULT-MIXED","host":"w.example","specialProxy":""},'
            b'"upload":%s,"download":%s,"start":"' + replay.rfc3339(T0 - 5.0).encode() + b'","chains":["n1","GENERAL"]}'
        )
        for up, down in ((b"10", b"0"), (b"12.5", b"3"), (b"NaN", b"4"), (b"-1", b"5"), (b" 7", b"6"),
                         (b"13", b"1e3"), (b"13", b"2000")):
            _both(dual, head + entry % (up, down) + b"]}", t)
            t += 0.1
        # Not JSON (leading zeros): refused on both paths, nothing changes.
        assert _both(dual, head + entry % (b"007", b"1") + b"]}", t) is None
        assert dual.raw.raw_counts["parsed"] >= 5

    def test_duplicate_ids_take_the_classic_loop_and_back_off(self) -> None:
        dual = _dual()
        dual.raw._RAW_BACKOFF_POLLS = 2
        entries = [_entry("dup", 41200, T0 - 1.0), _entry("other", 41201, T0 - 1.0)]
        dual.ingest_snapshot({"connections": entries}, T0, T0)
        dual.ingest_snapshot({"connections": [entries[0], entries[1], dict(entries[0], download=9)]}, T0 + 0.1, T0 + 0.1)
        counts = dual.raw.raw_counts
        assert counts["json_polls"] == 1 and counts["backoffs"] == 1
        for step in (2, 3):  # the back-off: json.loads without a walk
            dual.ingest_snapshot({"connections": entries}, T0 + 0.1 * step, T0 + 0.1 * step)
        assert counts["json_polls"] == 3 and counts["fast_polls"] == 1
        dual.ingest_snapshot({"connections": entries}, T0 + 0.4, T0 + 0.4)
        assert counts["fast_polls"] == 2 and counts["backoffs"] == 1

    def test_ids_are_keyed_exactly_whatever_their_length(self) -> None:
        # A short id is followed by the entry's other bytes; its key must not
        # take them in (a change there is not a new connection).
        dual = _dual()
        raw_line = replay.open_line(T0, sport=41250, host="e.example")
        dual.ingest_open(_parse(raw_line), now_wall=T0 + 0.001, now_perf=T0 + 0.001)
        entry = _entry("s1", 41250, T0, up=517, down=0)
        long_entry = _entry("L" * 100, 41251, T0 - 5.0, up=900, down=4000)
        for step in range(1, 4):
            dual.ingest_snapshot({"connections": [entry, long_entry]}, T0 + 0.1 * step, T0 + 0.1 * step)
        changed = json.loads(json.dumps(entry))
        changed["metadata"]["network"] = "TCP"  # still TCP for the classic loop
        dual.ingest_snapshot({"connections": [changed, long_entry]}, T0 + 0.4, T0 + 0.4)
        dual.ingest_snapshot({"connections": [changed, long_entry]}, T0 + 0.5, T0 + 0.5)
        assert dual.raw.raw_counts["json_polls"] == 0
        assert not [record for record in dual.drain_records() if record["ev"] == "outcome"]
        assert len(dual.raw._present) == 2

    @pytest.mark.parametrize("cid", ["x" * 128, "e\\u0073c", 'q\\"uote', "idé"])
    def test_ids_the_memo_cannot_key_switch_the_core_to_json(self, cid: str) -> None:
        dual = _dual()
        body_id = cid
        entry = _entry("placeholder", 41300, T0 - 1.0)
        for step in range(3):
            raw_entry = dict(entry, download=step)
            body = _go_json({"connections": [raw_entry]}).replace(b'"placeholder"', b'"' + body_id.encode() + b'"')
            _both(dual, body, T0 + step * 0.1)
        assert dual.raw.raw_counts["fast_polls"] == 0 and dual.raw.raw_counts["json_polls"] == 3

    def test_ipv6_and_udp_entries(self) -> None:
        dual = _dual()
        raw_line = replay.open_line(T0, sport=5000, host="v6.example", source_ip="::1")
        dual.ingest_open(_parse(raw_line), now_wall=T0 + 0.001, now_perf=T0 + 0.001)
        user = _entry("v6", 5000, T0, source_ip="::1", host="v6.example")
        udp = [_entry(f"u{index}", 42000 + index, T0 - 3.0, network="udp") for index in range(5)]
        dual.ingest_snapshot({"connections": [user, *udp]}, T0 + 0.05, T0 + 0.05)
        parsed = dual.raw.raw_counts["parsed"]
        for step in range(1, 4):
            for entry in udp:
                entry["download"] += 5000  # QUIC keeps moving: never parsed again
            dual.ingest_snapshot({"connections": udp}, T0 + 0.05 + 0.1 * step, T0 + 0.05 + 0.1 * step)
        assert dual.raw.raw_counts["parsed"] == parsed
        outcomes = [record for record in dual.drain_records() if record["ev"] == "outcome"]
        assert [record["class"] for record in outcomes] == ["FAILED_FAST"]

    def test_a_large_body_takes_the_fast_path(self) -> None:
        dual = _dual()
        entries = [_entry(_uuid(random.Random(index)), 20000 + index, T0 - 60.0, up=900, down=4000 + index)
                   for index in range(2500)]
        body = _go_json({"downloadTotal": 1, "uploadTotal": 1, "connections": entries, "memory": 1})
        assert len(body) > 800_000
        for step in range(3):
            for entry in entries[step::7]:
                entry["download"] += 100
            dual.ingest_snapshot({"downloadTotal": 1, "uploadTotal": 1, "connections": entries, "memory": 1},
                                 T0 + step, T0 + step)
        counts = dual.raw.raw_counts
        assert counts["fast_polls"] == 3 and counts["json_polls"] == 0
        assert counts["parsed"] == 2500 and counts["counters"] == 2 * len(entries[1::7])

    def test_a_gateway_change_reaches_unchanged_entries(self) -> None:
        dual = _dual()
        entries = [_entry(f"g{index}", 43000 + index, T0 - 10.0, chains_tail=("EXIT-SELECT", "GENERAL"))
                   for index in range(4)]
        dual.ingest_snapshot({"connections": entries}, T0, T0)
        dual.set_route_context(tier3_members=("n1",), gateway_groups=("EXIT-SELECT",))
        dual.ingest_snapshot({"connections": entries}, T0 + 1.0, T0 + 1.0)
        assert all(conn.gateway for conn in dual.raw._present.values())
        dual.set_route_context(tier3_members=("n1",), gateway_groups=())
        dual.ingest_snapshot({"connections": entries}, T0 + 2.0, T0 + 2.0)
        assert not any(conn.gateway for conn in dual.raw._present.values())

    def test_a_wake_gap_or_a_clock_step_back_observes_every_entry(self) -> None:
        dual = _dual()
        entries = [_entry(f"w{index}", 44000 + index, T0 - 10.0, up=900, down=4000) for index in range(6)]
        dual.ingest_snapshot({"connections": entries}, T0, T0)
        dual.ingest_snapshot({"connections": entries}, T0 + 1.0, T0 + 1.0)
        ticks = dual.raw.raw_counts["ticks"]
        # The machine slept 40 s with the perf clock stopped.
        dual.ingest_snapshot({"connections": entries}, T0 + 41.0, T0 + 1.2)
        assert dual.raw.raw_counts["ticks"] == ticks + 6
        dual.ingest_snapshot({"connections": entries}, T0 + 42.0, T0 + 2.2)
        ticks = dual.raw.raw_counts["ticks"]
        # The wall clock steps back 1 s: every entry is observed until it
        # passes the old maximum plus the 2 s rate window.
        for step, wall in enumerate((T0 + 41.0, T0 + 42.5, T0 + 44.0, T0 + 44.5, T0 + 45.0)):
            dual.ingest_snapshot({"connections": entries}, wall, T0 + 3.2 + step)
        assert dual.raw.raw_counts["ticks"] == ticks + 6 * 4

    def test_an_evicted_listed_entry_is_bound_afresh(self) -> None:
        dual = _dual()
        dual.classic._MAX_TRACKED = dual.raw._MAX_TRACKED = 4
        entries = []
        for index in range(4):
            t_open = T0 + index * 0.01
            dual.ingest_open(_parse(replay.open_line(t_open, sport=45000 + index, host="e.example")),
                             now_wall=T0 + 0.05, now_perf=T0 + 0.05)
            entries.append(_entry(f"v{index}", 45000 + index, t_open, up=900, down=4000))
        dual.ingest_snapshot({"connections": entries}, T0 + 0.1, T0 + 0.1)
        dual.ingest_snapshot({"connections": entries}, T0 + 0.2, T0 + 0.2)
        # Two more opens push the oldest listed connections out of the cap.
        for index in range(2):
            raw_line = replay.open_line(T0 + 0.25 + index * 0.01, sport=45100 + index, host="z.example")
            dual.ingest_open(_parse(raw_line), now_wall=T0 + 0.28, now_perf=T0 + 0.28)
        assert len(dual.raw._purge_present) == 2
        dual.ingest_snapshot({"connections": entries}, T0 + 0.3, T0 + 0.3)
        dual.ingest_snapshot({"connections": entries[1:]}, T0 + 0.4, T0 + 0.4)
        assert dual.summary(T0 + 1.0)["legacy_entries"] == 2

    def test_a_late_line_merges_into_a_lazy_legacy_entry_with_its_whole_history(self) -> None:
        dual = _dual()
        t = T0
        entry = _entry("late", 46000, t, up=517, down=0)
        for step in range(6):  # listed, unchanged, never observed by the raw core
            dual.ingest_snapshot({"connections": [entry]}, t + 0.1 * step, t + 0.1 * step)
        raw_line = replay.open_line(t, sport=46000, host="e.example")
        dual.ingest_open(_parse(raw_line), now_wall=t + 0.55, now_perf=t + 0.55)
        conn = next(iter(dual.raw._present.values()))
        assert conn.n_samples == 6 and conn.last_seen == pytest.approx(t + 0.5)
        dual.ingest_snapshot({"connections": []}, t + 0.6, t + 0.6)
        dual.check_state()

    # -- review fixes (09-29): shapes the walk must refuse or handle exactly -----

    @pytest.mark.parametrize("prefix,suffix", [(b"\x0c", b""), (b"\x0b", b""), (b"", b"\x0c"), (b"", b"\x0b\n")])
    def test_whitespace_json_does_not_allow_is_refused_as_json_loads_refuses_it(self, prefix, suffix) -> None:
        entry = _entry("f1", 47000, T0 - 1.0, up=10, down=0)
        body = prefix + _go_json({"downloadTotal": 1, "connections": [entry], "memory": 1}).rstrip(b"\n") + suffix
        core = _dual().raw
        with pytest.raises(ValueError):
            decode_connections_body(body)
        with pytest.raises(RawBodyError):
            core.ingest_raw(body, T0, T0)
        assert core._seq == 0 and core._memo == {}
        spaced = b" \t\r\n" + body.strip(b"\x0b\x0c\n") + b"\r\n \t"
        assert _raw_layout(spaced) is not None

    def test_a_gateway_change_keeps_the_old_flag_where_the_classic_loop_does(self) -> None:
        # Same poll: the route context adds a Gateway group, and the entry's
        # chains turn empty (AGED is still judged on the old flag) ...
        dual = _dual(tier3_members=("n1",))
        raw_line = replay.open_line(T0, sport=48000, host="g.example")
        dual.ingest_open(_parse(raw_line), now_wall=T0 + 0.001, now_perf=T0 + 0.001)
        entry = _entry(_uuid(random.Random(48000)), 48000, T0, up=517, chains_tail=("EXIT-SELECT", "GENERAL"))
        t = T0 + 0.1
        while t < T0 + 2.95:
            dual.ingest_snapshot({"connections": [entry]}, t, t)
            t += 0.25
        dual.set_route_context(tier3_members=("n1",), gateway_groups=("EXIT-SELECT",))
        dual.ingest_snapshot({"connections": [dict(entry, chains=[])]}, T0 + 3.05, T0 + 3.05)
        assert [record["class"] for record in dual.drain_records() if record["ev"] == "outcome"] == ["AGED"]
        # ... or the entry stops being a TCP entry (then it is gone: FAILED_FAST).
        dual = _dual(tier3_members=("n1",))
        raw_line = replay.open_line(T0, sport=48100, host="g.example")
        dual.ingest_open(_parse(raw_line), now_wall=T0 + 0.001, now_perf=T0 + 0.001)
        entry = _entry(_uuid(random.Random(48100)), 48100, T0, up=517, chains_tail=("EXIT-SELECT", "GENERAL"))
        for step in range(1, 4):
            dual.ingest_snapshot({"connections": [entry]}, T0 + 0.1 * step, T0 + 0.1 * step)
        dual.set_route_context(tier3_members=("n1",), gateway_groups=("EXIT-SELECT",))
        udp = json.loads(json.dumps(entry))
        udp["metadata"]["network"] = "udp"
        dual.ingest_snapshot({"connections": [udp]}, T0 + 0.4, T0 + 0.4)
        dual.ingest_snapshot({"connections": []}, T0 + 0.5, T0 + 0.5)
        assert [record["class"] for record in dual.drain_records() if record["ev"] == "outcome"] == ["FAILED_FAST"]

    def test_short_duplicate_ids_are_duplicates_whatever_follows_them(self) -> None:
        dual = _dual(tier3_members=("n1",))
        raw_line = replay.open_line(T0, sport=48200, host="d.example")
        dual.ingest_open(_parse(raw_line), now_wall=T0 + 0.001, now_perf=T0 + 0.001)
        first = _entry("d", 48200, T0, up=5)
        second = json.loads(json.dumps(first))
        second["metadata"]["network"] = "TCP"
        second["upload"] = 7
        for step in range(1, 6):
            dual.ingest_snapshot({"connections": [first, second]}, T0 + 0.1 * step, T0 + 0.1 * step)
        assert dual.raw.raw_counts["fast_polls"] == 0 and dual.raw.raw_counts["backoffs"] == 1

    def test_an_entry_whose_parsed_id_is_not_its_leading_one_goes_to_json(self) -> None:
        dual = _dual(tier3_members=("n1",))
        raw_line = replay.open_line(T0, sport=48300, host="e.example")
        dual.ingest_open(_parse(raw_line), now_wall=T0 + 0.001, now_perf=T0 + 0.001)
        base = _entry("zz", 48300, T0, up=5)
        body = _go_json({"connections": [base, dict(base, upload=9)]})
        body = body.replace(b'"rulePayload":""}', b'"rulePayload":"","id":"real"}')
        body = body.replace(b'[{"id":"zz"', b'[{"id":"aa"', 1)
        for step in range(1, 6):
            _both(dual, body, T0 + 0.1 * step)
        assert dual.raw.raw_counts["fast_polls"] == 0

    def test_a_download_counter_that_goes_down_keeps_the_rate_bound_exact(self) -> None:
        # Mihomo's counters only grow; with another source the oldest sample
        # need not be the lowest, and an unchanged observation can still
        # cross STREAM_RATE_BPS.
        dual = _dual(tier3_members=("n1",))
        entry = _entry(_uuid(random.Random(48500)), 48500, T0 - 10.0, up=900, down=50_000)
        dual.ingest_snapshot({"connections": [entry]}, T0, T0)
        dual.ingest_snapshot({"connections": [dict(entry, download=0)]}, T0 + 0.5, T0 + 0.5)
        dual.ingest_snapshot({"connections": [dict(entry, download=150_000)]}, T0 + 0.6, T0 + 0.6)
        for t in (1.0, 1.5, 2.0, 2.5, 3.0):
            dual.ingest_snapshot({"connections": [dict(entry, download=150_000)]}, T0 + t, T0 + t)
        conn = next(iter(dual.raw._present.values()))
        assert conn.down_fell and conn.t_stream_rate == pytest.approx(T0 + 2.5, abs=1e-6)
        assert dual.raw.next_poll_time(T0 + 3.0, T0 + 3.0) is not None

    def test_warm_polls_that_parse_most_entries_back_off_to_json_and_return(self) -> None:
        dual = _dual()
        dual.raw._RAW_BACKOFF_POLLS = 4
        entries = [_entry(_uuid(random.Random(index)), 49000 + index, T0 - 30.0, up=900, down=4000)
                   for index in range(24)]
        t = T0
        for poll in range(30):
            if poll < 18:
                for entry in entries:  # a field other than the counters moves each poll
                    entry["rulePayload"] = f"p{poll}"
            elif poll % 5 == 0:
                entries[0]["download"] += 100
            dual.ingest_snapshot({"connections": entries}, t, t)
            t += 0.1
        counts = dual.raw.raw_counts
        assert counts["backoffs"] >= 2 and counts["json_polls"] >= 8
        backoffs = counts["backoffs"]
        fast = counts["fast_polls"]
        for _ in range(10):  # quiet again: the fast path stays
            dual.ingest_snapshot({"connections": entries}, t, t)
            t += 0.1
        assert counts["backoffs"] == backoffs and counts["fast_polls"] >= fast + 8

    def test_a_large_wall_clock_step_back_keeps_history_and_rate_windows_bounded(self) -> None:
        dual = _dual()
        entries = [_entry(_uuid(random.Random(index)), 50000 + index, T0 - 30.0, up=900, down=4000)
                   for index in range(12)]
        perf = 100.0
        wall = T0
        for _ in range(5):
            dual.ingest_snapshot({"connections": entries}, wall, perf)
            wall += 1.0
            perf += 1.0
        wall -= 3600.0  # the wall clock is set back an hour; perf keeps running
        for step in range(40):
            entries[step % 12]["download"] += 10
            dual.ingest_snapshot({"connections": entries}, wall, perf)
            wall += 1.0
            perf += 1.0
        for core in (dual.classic, dual.raw):
            assert max(len(conn.rates) for conn in core._present.values()) <= 4
        assert len(dual.raw._hist) <= 2

    @pytest.mark.parametrize("seed", range(3000, 3008))
    def test_randomised_workloads_with_mihomo_shaped_entries(self, seed: int, monkeypatch) -> None:
        # Mihomo 1.19.27's full metadata order, providerChains, Windows
        # process paths with backslashes and strings that look like framing.
        plain_entry = _FuzzConn.entry
        hostile = ['},{"id":"00000000-0000-4000-a000-000000000000', '},"upload":1,"download":2,"start":"x',
                   'a\\"b\\\\c},{\\"id\\":\\"', "\u2028\u2029<>&", ":443"]

        def entry(conn: _FuzzConn, t: float) -> dict[str, Any]:
            base = plain_entry(conn, t)
            metadata = base["metadata"]
            rng = random.Random(conn.cid + "|metadata")  # str seeds are stable across processes
            process = rng.choice(["chrome.exe", "msedge.exe", "", rng.choice(hostile)])
            full = {
                "network": metadata["network"], "type": metadata["type"], "sourceIP": metadata["sourceIP"],
                "destinationIP": metadata.get("destinationIP", ""), "sourceGeoIP": None, "destinationGeoIP": None,
                "sourceIPASN": "", "destinationIPASN": "", "sourcePort": metadata["sourcePort"],
                "destinationPort": metadata["destinationPort"], "inboundIP": "127.0.0.1", "inboundPort": "7890",
                "inboundName": metadata["inboundName"], "inboundUser": "", "host": metadata["host"],
                "dnsMode": "normal", "uid": 0, "process": process,
                "processPath": f"C:\\Program Files\\{process}\\{process}" if process else "",
                "specialProxy": metadata["specialProxy"], "specialRules": "",
                "remoteDestination": "203.0.113.7:" + metadata["destinationPort"], "dscp": 0,
                "sniffHost": rng.choice(["", metadata["host"]]),
            }
            return {"id": base["id"], "metadata": full,
                    **{key: value for key, value in base.items() if key not in {"id", "metadata"}}}

        monkeypatch.setattr(_FuzzConn, "entry", entry)
        dual = _fuzz(seed, seconds=40.0, rate=12.0)
        assert dual.polls > 20 and dual.raw.raw_counts["fast_polls"]
