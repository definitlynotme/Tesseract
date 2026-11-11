"""Tier ladder end to end (spec_tier_ladder_v1 §4, §5.2, §7.1): a real
DesktopRuntime with fakes drives whole ladders through the real judge
service (Track B) and the runtime hooks (Track C).

Every scenario goes through ``_on_runtime_post_dial_trigger`` → admission →
``PostDialJudgeService`` worker, probe and close threads → the runtime's
probe, read, escalation, commit and ``ladder_end`` hooks, over the T0
driver's fake Mihomo controller (``tests/fixtures/ladder_t0/t0_driver.py``)
extended with group delay tests.  Nothing talks to a real Mihomo, opens a
socket or touches a user profile; the controller refuses every write.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load_driver() -> Any:
    name = "ladder_t0_driver_for_e2e"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests" / "fixtures" / "ladder_t0" / "t0_driver.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


drv = _load_driver()
URL = drv.URL
URL3 = drv.URL3
T1A, T1A2, T1B, T1B2 = drv.T1A, drv.T1A2, drv.T1B, drv.T1B2
_BASE_TOPOLOGY = drv.topology


def _topology(name: str) -> dict[str, Any]:
    """The driver's shapes plus the ladder-only ones used here."""

    if name in {"t2pair", "t2pair_pf_pinned"}:
        # Tier2 with two nodes; ``_pf_pinned``: PF held on TIER2-AUTO by a
        # settled D4 fixed pin (spec §5.2 case L2 -> L1 at the PF rung).
        topo = _BASE_TOPOLOGY("t2")
        topo["groups"]["TIER2-AUTO"] = drv._fb(["t2a", "t2b"])
        if name == "t2pair_pf_pinned":
            topo["groups"]["PROXY-FALLBACK"]["fixed"] = "TIER2-AUTO"
        topo["leaves"].pop("t2c")
        topo["proxy_map"].pop("t2c")
        return topo
    if name in {"t3pair_full", "t3pair_full_t2ok"}:
        # The gate-profile shape (dead placeholder Tier1/Tier2 nodes) with
        # two Tier3 nodes; ``_t2ok``: the Tier2 node answers.
        topo = _BASE_TOPOLOGY("t3_full")
        topo["groups"]["TIER3-AUTO"] = drv._fb(["n1", "n2"], url=URL3)
        topo["leaves"].pop("n3")
        topo["proxy_map"].pop("n3")
        topo["provider_members"] = ["n1", "n2"]
        if name == "t3pair_full_t2ok":
            topo["leaves"]["q1"] = True
        return topo
    return _BASE_TOPOLOGY(name)


drv.topology = _topology


class LadderCore(drv.FakeCore):
    """The T0 fake plus group delay tests: a failed one marks the group dead
    in its parents (Mihomo's own move), a pass marks it alive.  A member
    group's alive state is only its parent's record (never derived)."""

    def __init__(self, topo: dict[str, Any]) -> None:
        super().__init__(topo)
        self.group_dead: set[str] = set()
        self.on_delay: list[Any] = []

    def alive(self, name: str, depth: int = 0) -> bool:
        if name in self.groups:
            return name not in self.group_dead
        return super().alive(name, depth)

    def _group_entry(self, name: str, *, read: bool) -> dict[str, Any]:
        entry = super()._group_entry(name, read=read)
        alive = self.alive(name)
        entry["alive"] = alive
        entry["extra"] = {url: {"alive": alive, "history": []} for url in (URL, URL3)}
        return entry

    def _leaf_of(self, name: str) -> str:
        current = name
        for _ in range(16):
            if current not in self.groups:
                return current
            current = self.now(current)
        return current

    def proxy_delay(self, name: str, **kwargs: Any) -> dict[str, Any]:
        for hook in list(self.on_delay):
            hook(name, kwargs)
        if name not in self.groups:
            return super().proxy_delay(name, **kwargs)
        from mihomo_api import MihomoDelayTimeout, MihomoDelayTransportFailed

        sink = kwargs.get("connection_abort_sink")
        if sink is not None:
            sink(lambda: None)
        with self.lock:
            self.delay_calls.append({
                "name": name,
                "url": kwargs.get("url"),
                "timeout_ms": kwargs.get("timeout_ms"),
                "expected_status": kwargs.get("expected_status"),
                "request_timeout": drv._request_window(kwargs.get("request_timeout")),
                "verify": bool(kwargs.get("verify_expected_status")),
                "thread": re.sub(r"\d+", "#", threading.current_thread().name),
            })
            self.calls.append(["delay", name])
            leaf = self._leaf_of(name)
            mode = self.modes.get(name) or self.modes.get(leaf, "ok")
            dead = not self.alive(leaf) or mode in {"zombie", "timeout", "answer"}
            if dead:
                self.group_dead.add(name)
            else:
                self.group_dead.discard(name)
        if dead:
            if mode == "timeout":
                raise MihomoDelayTimeout("controller HTTP 504 Timeout")
            raise MihomoDelayTransportFailed("controller HTTP 503 from delay test")
        return {"delay": 42}


def install_contract(harness: Any, directory: Path) -> None:
    """The Core-loaded runtime config the §4.1 timeout lookup reads."""

    groups = []
    for name, group in harness.topo["groups"].items():
        timeout = 800 if name.startswith(("TIER1-", "TIER2-")) else 5000
        groups.append({"name": name, "type": str(group["type"]).lower(), "timeout": timeout})
    text = yaml.safe_dump({"proxy-groups": groups}, sort_keys=False)
    path = directory / f"runtime-{id(harness)}.yaml"
    path.write_text(text, encoding="utf-8")
    contract = harness.runtime._runtime_loaded_export_contract
    contract["main_semantic_digest"] = hashlib.sha256(text.replace("\n", os.linesep).encode("utf-8")).hexdigest()
    contract["runtime_config_path"] = str(path)


@pytest.fixture
def make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("TESSERACT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TESSERACT_DATA_DIR", str(tmp_path / "data"))
    ctx = drv.Ctx(ROOT, {}, tmp_path, family="e2e")
    made: list[Any] = []

    def build(topo: str, *, ladder: bool = True, runner: Any = None, group_dead: tuple[str, ...] = (),
              **flags: Any) -> Any:
        options = dict(flags)
        # 10-28: the ladder/rearm/overshoot keys default on; pin the legacy
        # off values so ladder=False and each key stay opt-in here.
        options.setdefault("tier_ladder_judge", bool(ladder))
        options.setdefault("rearm_on_stale_route", False)
        options.setdefault("overshoot_repair", False)
        # 11-01: the ship2 fix and seamless keys default on; pinned off too.
        for key in (
            "exhausted_pool_reexport",
            "exhausted_hold",
            "not_reproduced_override",
            "post_dial_provisional_move",
            "post_dial_fast_successor",
            "post_dial_successor_from_history",
            "post_dial_close_frozen_streams",
        ):
            options.setdefault(key, False)
        core = LadderCore(drv.topology(topo))
        core.group_dead.update(group_dead)
        harness = drv.Harness(ctx, topo, flags=options, controller=core)
        made.append(harness)
        install_contract(harness, tmp_path)
        harness.runner = runner
        if runner is not None:
            harness.runtime._runtime_post_dial_observer = runner
        harness.outcomes = drv._spy_outcomes(harness.runtime)
        return harness

    yield build
    for harness in made:
        harness.close()
        # The overshoot worker waits on its Event; let it see the stop.
        event = harness.runtime.__dict__.get("_runtime_overshoot_event")
        if event is not None:
            event.set()


# ---------------------------------------------------------------------------
# Helpers


ALLOWED_CALLS = {"proxies", "proxy", "provider", "delay", "connections", "close"}
_FIELD = re.compile(r"(?<=[ (])([a-z_0-9]+)=(\S*?)(?=[ )])")


def lines(harness: Any, ev: str) -> list[str]:
    prefix = f"Post-dial observer {ev} ("
    return [r.getMessage() for r in list(harness.logs.records) if r.getMessage().startswith(prefix)]


def fields(line: str) -> dict[str, str]:
    return dict(_FIELD.findall(line))


def events(harness: Any) -> list[tuple[str, dict[str, str]]]:
    """Observer records in emission order: (ev, fields)."""

    out = []
    for record in list(harness.logs.records):
        text = record.getMessage()
        match = re.match(r"Post-dial observer (\w+) \(", text)
        if match:
            out.append((match.group(1), fields(text)))
    return out


def run(harness: Any, leaf: str, *, modes: dict[str, str] | None = None, timeout: float = 30.0) -> None:
    harness.controller.modes.update(modes or {})
    with harness.controller.lock:
        harness.controller.calls = []
    harness.trigger(leaf)
    assert harness.quiesce(timeout)


def calls(harness: Any) -> list[list[Any]]:
    with harness.controller.lock:
        return [list(call) for call in harness.controller.calls]


def delay_names(harness: Any) -> list[str]:
    return [call["name"] for call in harness.controller.delay_calls]


def tok(harness: Any, name: str) -> str:
    return harness.runtime._node_judgement_state().tokenizer.leaf(name)


def ttok(harness: Any, name: str) -> str:
    return "T" + tok(harness, name)


def end_of(harness: Any) -> dict[str, str]:
    ends = lines(harness, "ladder_end")
    assert len(ends) == 1, ends
    return fields(ends[0])


def outcome_of(harness: Any) -> Any:
    outcomes = list(harness.runtime._runtime_recovery_outcomes.values())
    assert len(outcomes) == 1, outcomes
    return outcomes[0]


def assert_read_only(harness: Any) -> None:
    """W13: reads, delay tests and exact-id closes only; no write at all."""

    assert harness.controller.writes == []
    seen = calls(harness)
    assert {call[0] for call in seen} <= ALLOWED_CALLS
    assert all(call[1] == "TIER3B-RUNTIME" for call in seen if call[0] == "provider")
    assert not lines(harness, "probe_refused")


def assert_ordering(harness: Any) -> None:
    """LS3: rung k+1 opens only after rung k's ``rung_verdict``; every A_C
    at an escalated rung follows a passing witness of that rung."""

    verdict_at: dict[int, int] = {}
    witness_pass: dict[int, int] = {}
    for index, (ev, f) in enumerate(events(harness)):
        rung = int(f.get("rung", f.get("to_rung", "0")) or 0)
        if ev == "rung_verdict":
            verdict_at.setdefault(rung, index)
        elif ev == "rung_start" and rung > 1:
            assert rung - 1 in verdict_at and verdict_at[rung - 1] < index
        elif ev == "ladder_witness" and f.get("class") == "pass":
            witness_pass.setdefault(rung, index)
        elif ev == "rung_probe_result" and f.get("name") == "A_C" and rung > 1:
            assert rung in witness_pass and witness_pass[rung] < index


# ---------------------------------------------------------------------------
# Tier1 leaf conviction (H-L1 shape)


def test_tier1_leaf_conviction_moves_the_variant_group_natively(make: Any) -> None:
    harness = make("t1")
    run(harness, T1A, modes={T1A: "zombie"})
    admit = fields(lines(harness, "ladder_admit")[0])
    assert admit["rungs"] == "3" and admit["timeouts"] == "800,800,5000"
    assert admit["path"] == "FE,PF,T1S,T1,T1XS,T1X,L"
    verdict = fields(lines(harness, "verdict")[0])
    assert verdict["verdict"] == "CONVICT_CONTRACT" and verdict["rung"] == "1"
    assert verdict["rverdict"] == "CONVICT_CONTRACT" and verdict["ladder"] == admit["ladder"]
    commit = fields(lines(harness, "commit")[0])
    assert commit["accepted"] == "true" and commit["proven"] == "1" and commit["sacrificial"] == "0"
    assert commit["pick"] == ttok(harness, T1A2) and commit["successor"] == tok(harness, T1A2)
    end = end_of(harness)
    assert (end["outcome"], end["rung"], end["rungs_run"], end["commit"]) == ("convicted", "1", "1", "accepted")
    assert float(end["elapsed_s"]) <= 3.7
    assert delay_names(harness) == [T1A, T1A2]
    assert all(call["timeout_ms"] == 800 and call["url"] == URL for call in harness.controller.delay_calls)
    core = harness.controller
    assert core.now("TIER1-nodeA-AUTO") == T1A2
    assert (core.now("TIER1-AUTO"), core.now("PROXY-FALLBACK")) == ("TIER1-nodeA-SELECT", "TIER1-SELECT")
    outcome = outcome_of(harness)
    assert outcome.reason == "post_dial_rung_native_rebind" and outcome.to_ref.proxy_name == T1A2
    assert harness.binding_view()["leaf"] == T1A2
    assert not lines(harness, "ladder_escalate") and not lines(harness, "rung_commit")
    assert_read_only(harness)


# ---------------------------------------------------------------------------
# Tier1 group escalation (H-L2 / H-L15 shape)


def test_tier1_exhausted_variant_group_escalates_to_tier1_auto_via_witness(make: Any) -> None:
    harness = make("t1", runner=drv.FakeRunner())
    run(harness, T1A, modes={T1A: "zombie", T1A2: "zombie"})
    verdicts = [fields(line) for line in lines(harness, "rung_verdict")]
    assert [(v["rung"], v["verdict"]) for v in verdicts] == [
        ("1", "EXHAUSTED_COMPLETE"),
        ("2", "CONVICT_CONTRACT"),
    ]
    escalate = fields(lines(harness, "ladder_escalate")[0])
    assert (escalate["from_rung"], escalate["to_rung"], escalate["cause"]) == ("1", "2", "exhausted_complete")
    assert escalate["refresh"] == "ok" and escalate["deeper_moved"] == "0"
    start = fields(lines(harness, "rung_start")[1])
    assert (start["rung"], start["grp"], start["akind"], start["witnessed"]) == ("2", "T1", "group", "1")
    assert start["acc"] == ttok(harness, "TIER1-nodeA-SELECT") and start["timeout_ms"] == "800"
    witness = [fields(line) for line in lines(harness, "ladder_witness")]
    assert [(w["name"], w["class"]) for w in witness] == [("W0", "pass")]
    assert witness[0]["leaf"] == tok(harness, "TIER1-nodeB-SELECT")
    deferred = fields(lines(harness, "demotion_deferred")[0])
    assert deferred["rung"] == "2" and deferred["acc"] == ttok(harness, "TIER1-nodeA-SELECT")
    commit = fields(lines(harness, "rung_commit")[0])
    assert commit["accepted"] == "true" and commit["proven"] == "1" and commit["grp"] == "T1"
    assert commit["pick"] == ttok(harness, "TIER1-nodeB-SELECT")
    end = end_of(harness)
    assert (end["outcome"], end["rung"], end["rungs_run"], end["witnesses"]) == ("convicted", "2", "2", "1")
    assert float(end["elapsed_s"]) <= 20.5
    # Witness before A_C at rung 2, both at TIER1-AUTO's contract.
    assert delay_names(harness) == [T1A, T1A2, "TIER1-nodeB-SELECT", "TIER1-nodeA-SELECT"]
    assert all(call["timeout_ms"] == 800 and call["url"] == URL for call in harness.controller.delay_calls)
    core = harness.controller
    assert core.now("TIER1-AUTO") == "TIER1-nodeB-SELECT" and core.now("PROXY-FALLBACK") == "TIER1-SELECT"
    # The user's selects were never written: TIER1-SELECT and the X select
    # keep their selection (the controller refuses writes and saw none).
    assert core.groups["TIER1-SELECT"]["selected"] == "TIER1-AUTO"
    outcome = outcome_of(harness)
    assert outcome.reason == "post_dial_rung_native_rebind" and outcome.to_ref.proxy_name == T1B
    facts = harness.outcomes[-1]["facts"]
    assert (facts["rung"], facts["depth"], facts["kind"], facts["put"]) == (2, 4, "group", False)
    assert harness.binding_view()["leaf"] == T1B
    assert_ordering(harness)
    assert_read_only(harness)
    # The PF rung was never opened and TIER1-AUTO's member groups were
    # never read one by one (the group rung reads a full refresh).
    seen = calls(harness)
    assert ["proxy", "TIER1-nodeB-SELECT"] not in seen and ["proxy", "TIER1-AUTO"] not in seen


# ---------------------------------------------------------------------------
# Tier2 leaf (H-L4 shape)


def test_tier2_leaf_conviction_and_stuck_connection_closes(make: Any) -> None:
    runner = drv.FakeRunner()
    harness = make("t2", runner=runner)
    tail = ("TIER2-AUTO", "PROXY-FALLBACK", "FINAL-EXIT", "GENERAL")
    harness.controller.connections_list = [drv._connection("c1", "t2a", tail), drv._connection("c2", "t2b", tail)]
    run(harness, "t2a", modes={"t2a": "zombie"})
    assert fields(lines(harness, "ladder_admit")[0])["timeouts"] == "800,5000"
    demotion = fields(lines(harness, "demotion")[0])
    assert demotion["rung"] == "1" and demotion["leaf"] == tok(harness, "t2a")
    assert runner.watches == ["t2a"]
    end = end_of(harness)
    assert (end["outcome"], end["commit"], end["hold"]) == ("convicted", "accepted", "none")
    harness.judge.on_watch_step(runner, runner.last_watch, drv.stuck("c1"))
    assert harness.quiesce(10.0)
    assert harness.controller.closed == ["c1"]
    close = fields(lines(harness, "close")[-1])
    assert close["rung"] == "1" and close["ladder"] == end["ladder"]
    assert harness.controller.now("TIER2-AUTO") == "t2b"
    assert harness.controller.now("PROXY-FALLBACK") == "TIER2-AUTO"
    assert delay_names(harness) == ["t2a", "t2b"]
    assert outcome_of(harness).to_ref.proxy_name == "t2b"
    assert_read_only(harness)


# ---------------------------------------------------------------------------
# Tier3 rung 1 equals key-off semantics (H-L5)


def _tier3_run(make: Any, ladder: bool, modes: dict[str, str], *, stuck_ids: tuple[str, ...] = ("c1", "c2")) -> Any:
    runner = drv.FakeRunner()
    harness = make("t3", ladder=ladder, runner=runner)
    tail = ("TIER3-AUTO", "PROXY-FALLBACK", "FINAL-EXIT", "GENERAL")
    harness.controller.connections_list = [
        drv._connection("c1", "n1", tail), drv._connection("c2", "n1", tail), drv._connection("c3", "n2", tail)]
    run(harness, "n1", modes=modes)
    for _ in range(2):
        if runner.last_watch is None:
            break
        harness.judge.on_watch_step(runner, runner.last_watch, drv.stuck(*stuck_ids))
        assert harness.quiesce(10.0)
    return harness


def _semantics(harness: Any) -> dict[str, Any]:
    verdict = [fields(line) for line in lines(harness, "verdict")]
    commit = [fields(line) for line in lines(harness, "commit")]
    hold = harness.judge.gates.hold
    return {
        "delays": [(c["name"], c["url"], c["timeout_ms"], c["expected_status"]) for c in harness.controller.delay_calls],
        "verdicts": [(v["verdict"], v.get("successor"), v.get("commit_allowed")) for v in verdict],
        "commits": [(c["accepted"], c.get("successor")) for c in commit],
        "demotions": [fields(line)["probe"] for line in lines(harness, "demotion")],
        "closes": [(fields(line).get("action"), fields(line).get("ids")) for line in lines(harness, "close")],
        "closed": list(harness.controller.closed),
        "watches": list(harness.runner.watches),
        "ended": list(harness.runner.ended),
        "marked": list(harness.runner.marked),
        "outcomes": [(o.status.value, getattr(o.to_ref, "proxy_name", None))
                     for o in harness.runtime._runtime_recovery_outcomes.values()],
        "tier3_now": harness.controller.now("TIER3-AUTO"),
        "hold": None if hold is None else hold.value,
        "writes": list(harness.controller.writes),
    }


@pytest.mark.parametrize(
    "modes",
    [
        {"n1": "zombie"},
        {"n1": "zombie", "n2": "timeout"},
        {"n1": "zombie", "n2": "zombie", "n3": "zombie"},
        {},
    ],
    ids=["convict", "successor_timeout", "exhausted", "not_reproduced"],
)
def test_tier3_rung_one_equals_key_off_semantics(make: Any, modes: dict[str, str]) -> None:
    off = _tier3_run(make, False, dict(modes))
    on = _tier3_run(make, True, dict(modes))
    assert _semantics(on) == _semantics(off)
    assert not lines(off, "ladder_admit") and all("ladder=" not in line for line in lines(off, "verdict"))
    admit = fields(lines(on, "ladder_admit")[0])
    assert admit["rungs"] == "1" and admit["timeouts"] == "5000"
    for ev in ("verdict", "commit", "demotion", "close"):
        assert all(fields(line)["rung"] == "1" for line in lines(on, ev))
    assert not lines(on, "ladder_escalate") and not lines(on, "late_hold")
    off_reasons = [o.reason for o in off.runtime._runtime_recovery_outcomes.values()]
    on_reasons = [o.reason for o in on.runtime._runtime_recovery_outcomes.values()]
    # The expected differences: the reason codes.  The commit's is
    # ``post_dial_rung_native_rebind``; a ticket settled without a commit
    # carries the ladder outcome (spec §4.11), here EXHAUSTED_PARTIAL's
    # ``moved_unproven`` for today's ``exhausted`` (same CONTROLLER_HOLD).
    renamed = {"post_dial_native_rebind": "post_dial_rung_native_rebind",
               "post_dial_exhausted": "post_dial_moved_unproven"}
    assert [renamed.get(reason, reason) for reason in off_reasons] == on_reasons
    # Ticket 20 s key on vs 6 s key off (the probe evidence windows differ).
    for harness, window in ((off, 6.0), (on, 20.0)):
        for ticket in harness.runtime._runtime_recovery_tickets.values():
            assert round(ticket.deadline_monotonic - ticket.observed_at_monotonic, 3) <= window


# ---------------------------------------------------------------------------
# PF escalation via the witness (H-L3 / H-L12 shapes)


def test_tier2_exhausted_escalates_to_pf_and_moves_onto_the_passed_witness(make: Any) -> None:
    harness = make("t2pair")
    run(harness, "t2a", modes={"t2a": "zombie", "t2b": "zombie"})
    assert [fields(v)["verdict"] for v in lines(harness, "rung_verdict")] == ["EXHAUSTED_COMPLETE", "CONVICT_CONTRACT"]
    witness = [fields(line) for line in lines(harness, "ladder_witness")]
    assert [(w["rung"], w["name"], w["class"], w["leaf"]) for w in witness] == [
        ("2", "W0", "pass", tok(harness, "TIER3-AUTO"))]
    commit = fields(lines(harness, "rung_commit")[0])
    assert (commit["grp"], commit["accepted"], commit["proven"]) == ("PF", "true", "1")
    assert commit["pick"] == ttok(harness, "TIER3-AUTO")
    end = end_of(harness)
    assert (end["outcome"], end["rung"], end["pf_probe"]) == ("convicted", "2", "1")
    assert harness.controller.now("PROXY-FALLBACK") == "TIER3-AUTO"
    # Witness first, then the A_C on the exhausted TIER2-AUTO, at PF's contract.
    assert delay_names(harness) == ["t2a", "t2b", "TIER3-AUTO", "TIER2-AUTO"]
    pf_calls = harness.controller.delay_calls[2:]
    assert all(call["timeout_ms"] == 5000 and call["url"] == URL for call in pf_calls)
    outcome = outcome_of(harness)
    assert outcome.to_ref.proxy_name == "n1"
    facts = harness.outcomes[-1]["facts"]
    assert (facts["rung"], facts["depth"], facts["kind"]) == (2, 2, "group")
    assert_ordering(harness)
    assert_read_only(harness)
    seen = calls(harness)
    assert ["proxy", "TIER3-AUTO"] not in seen and ["proxy", "PROXY-FALLBACK"] not in seen


def test_tier3_exhausted_never_moves_pf_onto_a_dead_placeholder(make: Any) -> None:
    # PF's own checks already marked the placeholder groups dead.
    harness = make("t3pair_full", group_dead=("TIER1-SELECT", "TIER2-AUTO"))
    run(harness, "n1", modes={"n1": "zombie", "n2": "zombie"})
    witness = [fields(line) for line in lines(harness, "ladder_witness")]
    assert [(w["name"], w["class"]) for w in witness] == [("W0", "transport_fail"), ("W1", "transport_fail")]
    assert [w["leaf"] for w in witness] == [tok(harness, "TIER1-SELECT"), tok(harness, "TIER2-AUTO")]
    end = end_of(harness)
    assert (end["outcome"], end["hold"], end["commit"], end["witnesses"]) == ("exhausted_hold", "EXHAUSTED", "none", "2")
    assert not [line for line in lines(harness, "rung_probe_result") if fields(line)["rung"] == "2"]
    assert harness.controller.now("PROXY-FALLBACK") == "TIER3-AUTO"
    assert harness.judge.gates.hold is not None and harness.judge.gates.hold.value == "EXHAUSTED"
    assert outcome_of(harness).status.value != "recovered"
    assert_read_only(harness)


def test_tier3_exhausted_escalates_past_a_dead_placeholder_to_the_live_tier2(make: Any) -> None:
    harness = make("t3pair_full_t2ok", group_dead=("TIER1-SELECT", "TIER2-AUTO"))
    run(harness, "n1", modes={"n1": "zombie", "n2": "zombie"})
    witness = [fields(line) for line in lines(harness, "ladder_witness")]
    assert [(w["name"], w["class"]) for w in witness] == [("W0", "transport_fail"), ("W1", "pass")]
    commit = fields(lines(harness, "rung_commit")[0])
    assert (commit["grp"], commit["accepted"], commit["proven"]) == ("PF", "true", "1")
    assert commit["pick"] == ttok(harness, "TIER2-AUTO")
    assert end_of(harness)["outcome"] == "convicted"
    assert harness.controller.now("PROXY-FALLBACK") == "TIER2-AUTO"
    assert outcome_of(harness).to_ref.proxy_name == "q1"
    assert_ordering(harness)
    assert_read_only(harness)


# ---------------------------------------------------------------------------
# gateway_scope_off (spec §1.2)


def test_gateway_on_the_path_is_gated_scope_off_with_no_refresh(make: Any) -> None:
    on = make("gw")
    run(on, "n1", modes={"n1": "zombie"})
    gated = [fields(line) for line in lines(on, "gated")]
    assert gated and gated[-1]["reason"] == "gateway_scope_off"
    assert not lines(on, "judge_refresh") and not lines(on, "ladder_admit")
    assert on.controller.delay_calls == [] and ["proxies"] not in calls(on)
    assert on.runtime._runtime_recovery_tickets == {}
    off = make("gw", ladder=False)
    run(off, "n1", modes={"n1": "zombie"})
    assert fields(lines(off, "gated")[-1])["reason"] == "route_binding_mismatch"
    assert lines(off, "judge_refresh")


# ---------------------------------------------------------------------------
# d4_txn_busy (spec §5.2)


def test_d4_transaction_at_admission_is_gated(make: Any) -> None:
    harness = make("t2")
    with harness.runtime._runtime_failure_monitor_lock:
        harness.runtime._runtime_passive_failback_requests = {"k": {"running": True}}
    run(harness, "t2a", modes={"t2a": "zombie"})
    assert fields(lines(harness, "gated")[-1])["reason"] == "d4_txn_busy_passive_failback"
    assert harness.controller.delay_calls == [] and not lines(harness, "ladder_admit")


def test_d4_transaction_mid_ladder_stops_before_the_next_probe(make: Any) -> None:
    harness = make("t2")
    runtime = harness.runtime

    def start_d4(name: str, _kwargs: Any) -> None:
        if name == "t2a":
            with runtime._runtime_failure_monitor_lock:
                runtime._runtime_passive_failback_requests = {"k": {"running": True}}

    harness.controller.on_delay.append(start_d4)
    run(harness, "t2a", modes={"t2a": "zombie"})
    # A_C ran; S_C0 was never issued.
    assert delay_names(harness) == ["t2a"]
    end = end_of(harness)
    assert (end["outcome"], end["commit"], end["hold"]) == ("d4_txn_busy_passive_failback", "none", "none")
    verdict = fields(lines(harness, "rung_verdict")[0])
    assert verdict["verdict"] == "INDETERMINATE" and verdict["reason"] == "d4_txn_busy_passive_failback"
    assert not lines(harness, "commit")
    assert harness.judge.gates.hold is None
    assert outcome_of(harness).status.value != "recovered"
    assert_read_only(harness)


def test_d4_transaction_before_an_escalation_stops_the_ladder(make: Any) -> None:
    harness = make("t2pair")
    runtime = harness.runtime

    def start_d4(name: str, _kwargs: Any) -> None:
        if name == "t2b":
            with runtime._runtime_failure_monitor_lock:
                runtime._runtime_failure_recovery_bindings = {("k", "other")}

    harness.controller.on_delay.append(start_d4)
    run(harness, "t2a", modes={"t2a": "zombie", "t2b": "zombie"})
    escalate = fields(lines(harness, "ladder_escalate")[0])
    assert escalate["refresh"] == "ok"
    end = end_of(harness)
    assert end["outcome"] == "d4_txn_busy_recovery_worker" and end["commit"] == "none"
    assert not lines(harness, "ladder_witness")
    assert delay_names(harness) == ["t2a", "t2b"]
    assert harness.controller.now("PROXY-FALLBACK") == "TIER2-AUTO"
    with runtime._runtime_failure_monitor_lock:
        runtime._runtime_failure_recovery_bindings = set()


# ---------------------------------------------------------------------------
# Track H: lease deferral during a ladder (spec §5.2 L5)


@pytest.mark.parametrize("ladder", [True, False])
def test_track_h_lease_watcher_defers_only_while_a_ladder_is_in_flight(make: Any, ladder: bool) -> None:
    harness = make("t3", ladder=ladder)
    runtime = harness.runtime
    during: list[str] = []
    wakes: list[tuple[str, str]] = []
    harness.controller.on_delay.append(lambda _name, _kwargs: during.append(runtime._route_lease_busy(None).hard))
    original_wake = runtime._route_lease_wake

    def wake(reason: str = "") -> None:
        wakes.append((reason, runtime._route_lease_busy(None).hard))
        original_wake(reason)

    runtime._route_lease_wake = wake
    run(harness, "n1", modes={"n1": "zombie"})
    assert during and set(during) == ({"post_dial_ladder"} if ladder else {""})
    assert runtime._route_lease_busy(None).hard == ""
    if ladder:
        # ladder_end wakes the watcher once the ladder is no longer in flight.
        assert ("ladder_end", "") in wakes
        assert end_of(harness)["outcome"] == "convicted"
    else:
        assert all(reason != "ladder_end" for reason, _hard in wakes)


# ---------------------------------------------------------------------------
# Sacrificial-read commit (design §19.3; spec §4.9, H-L10)


def test_pinned_pf_accused_gets_the_sacrificial_read_before_the_commit_refresh(make: Any) -> None:
    harness = make("t2pair_pf_pinned")
    runtime = harness.runtime
    with runtime._runtime_failure_monitor_lock:
        runtime._runtime_route_mutation_lease = {"group": "PROXY-FALLBACK", "to": "TIER2-AUTO", "mode": "fixed"}
    run(harness, "t2a", modes={"t2a": "zombie", "t2b": "zombie"})
    admit = fields(lines(harness, "ladder_admit")[0])
    assert admit["lease"] == "fixed_PF"
    pf_start = fields(lines(harness, "rung_start")[1])
    assert (pf_start["grp"], pf_start["fixed0"]) == ("PF", "1")
    commit = fields(lines(harness, "rung_commit")[0])
    assert (commit["accepted"], commit["sacrificial"], commit["fixed0"], commit["proven"]) == ("true", "1", "1", "1")
    assert commit["pick"] == ttok(harness, "TIER3-AUTO")
    seen = calls(harness)
    sacrificial = seen.index(["proxy", "PROXY-FALLBACK"])
    a_c = seen.index(["delay", "TIER2-AUTO"])
    assert a_c < sacrificial and ["proxies"] in seen[sacrificial + 1:]
    assert seen.count(["proxy", "PROXY-FALLBACK"]) == 1
    core = harness.controller
    assert core.groups["PROXY-FALLBACK"]["fixed"] == "" and core.now("PROXY-FALLBACK") == "TIER3-AUTO"
    assert end_of(harness)["outcome"] == "convicted"
    assert outcome_of(harness).to_ref.proxy_name == "n1"
    assert_read_only(harness)


def test_pinned_leaf_accused_drop_is_consumed_by_the_rung_one_read(make: Any) -> None:
    harness = make("t2_pinned")
    run(harness, "t2b", modes={"t2b": "zombie"})
    commit = fields(lines(harness, "commit")[0])
    # Rung ①'s legacy readback after the failure already consumed the drop:
    # no extra discarded read (an extra one would be harmless, P3).
    assert (commit["accepted"], commit["fixed0"], commit["sacrificial"]) == ("true", "1", "0")
    assert commit["pick"] == ttok(harness, "t2a") and commit["proven"] == "1"
    assert harness.controller.groups["TIER2-AUTO"]["fixed"] == ""
    assert harness.controller.now("TIER2-AUTO") == "t2a"
    assert outcome_of(harness).to_ref.proxy_name == "t2a"
    assert_read_only(harness)


# ---------------------------------------------------------------------------
# More shapes through the same wiring: FACILITY escalation (H-L2), the
# facility witness stop (H-L7), the 3-rung Tier1 path to PF, a native move
# above the next rung (H-L3c) and an authority change mid-ladder.


def test_tier1_facility_escalates_witness_first_and_convicts_the_variant_group(make: Any) -> None:
    harness = make("t1")
    run(harness, T1A, modes={T1A: "timeout", T1A2: "timeout"})
    verdicts = [fields(line) for line in lines(harness, "rung_verdict")]
    assert [(v["rung"], v["verdict"]) for v in verdicts] == [("1", "FACILITY"), ("2", "CONVICT_CONTRACT")]
    escalate = fields(lines(harness, "ladder_escalate")[0])
    assert escalate["cause"] == "facility"
    assert [fields(w)["class"] for w in lines(harness, "ladder_witness")] == ["pass"]
    commit = fields(lines(harness, "rung_commit")[0])
    assert (commit["accepted"], commit["pick"]) == ("true", ttok(harness, "TIER1-nodeB-SELECT"))
    end = end_of(harness)
    assert (end["outcome"], end["hold"]) == ("convicted", "none")
    assert harness.judge.gates.hold is None
    assert harness.controller.now("TIER1-AUTO") == "TIER1-nodeB-SELECT"
    assert float(end["elapsed_s"]) <= 6.0
    assert_ordering(harness)
    assert_read_only(harness)


def test_tier1_facility_with_a_timed_out_witness_stops_facility_confirmed(make: Any) -> None:
    harness = make("t1")
    run(harness, T1A, modes={T1A: "timeout", T1A2: "timeout", T1B: "timeout", T1B2: "timeout"})
    witness = [fields(line) for line in lines(harness, "ladder_witness")]
    assert [(w["name"], w["class"]) for w in witness] == [("W0", "timeout")]
    end = end_of(harness)
    assert (end["outcome"], end["hold"], end["commit"]) == ("facility_confirmed", "FACILITY", "none")
    assert harness.judge.gates.hold is not None and harness.judge.gates.hold.value == "FACILITY"
    # No A_C at rung 2 and no PF probe; nothing moved by the judge.
    assert delay_names(harness) == [T1A, T1A2, "TIER1-nodeB-SELECT"]
    assert harness.controller.now("PROXY-FALLBACK") == "TIER1-SELECT"
    assert not lines(harness, "rung_commit") and not lines(harness, "late_hold")
    assert_read_only(harness)


def test_tier1_default_three_rungs_reach_pf_and_move_onto_the_live_tier2(make: Any) -> None:
    harness = make("t1")
    run(harness, T1A, modes={T1A: "zombie", T1A2: "zombie", T1B: "zombie", T1B2: "zombie"})
    verdicts = [fields(line) for line in lines(harness, "rung_verdict")]
    assert [(v["rung"], v["verdict"]) for v in verdicts] == [
        ("1", "EXHAUSTED_COMPLETE"),
        ("2", "EXHAUSTED_COMPLETE"),
        ("3", "CONVICT_CONTRACT"),
    ]
    escalations = [fields(line) for line in lines(harness, "ladder_escalate")]
    assert [(e["from_rung"], e["to_rung"]) for e in escalations] == [("1", "2"), ("2", "3")]
    witness = [fields(line) for line in lines(harness, "ladder_witness")]
    assert [(w["rung"], w["class"]) for w in witness] == [("2", "transport_fail"), ("3", "pass")]
    assert witness[1]["leaf"] == tok(harness, "TIER2-AUTO")
    commit = fields(lines(harness, "rung_commit")[0])
    assert (commit["rung"], commit["grp"], commit["accepted"], commit["proven"]) == ("3", "PF", "true", "1")
    end = end_of(harness)
    assert (end["outcome"], end["rung"], end["rungs_run"], end["pf_probe"]) == ("convicted", "3", "3", "1")
    assert float(end["elapsed_s"]) <= 20.5
    core = harness.controller
    assert core.now("PROXY-FALLBACK") == "TIER2-AUTO"
    assert core.groups["TIER1-SELECT"]["selected"] == "TIER1-AUTO"
    assert outcome_of(harness).to_ref.proxy_name == "t2a"
    assert_ordering(harness)
    assert_read_only(harness)


def test_native_pf_move_before_the_escalation_stops_ladder_route_moved(make: Any) -> None:
    harness = make("t2pair")
    core = harness.controller

    def pf_check(name: str, _kwargs: Any) -> None:
        if name == "t2b":
            # Mihomo's own PF check marks TIER2-AUTO dead meanwhile.
            with core.lock:
                core.group_dead.add("TIER2-AUTO")

    core.on_delay.append(pf_check)
    run(harness, "t2a", modes={"t2a": "zombie", "t2b": "zombie"})
    escalate = fields(lines(harness, "ladder_escalate")[0])
    assert escalate["refresh"] == "moved"
    end = end_of(harness)
    assert (end["outcome"], end["commit"], end["pf_probe"]) == ("ladder_route_moved", "none", "0")
    assert not lines(harness, "ladder_witness")
    assert delay_names(harness) == ["t2a", "t2b"]
    assert core.now("PROXY-FALLBACK") == "TIER3-AUTO"
    assert_read_only(harness)


def test_selector_intent_change_mid_ladder_stops_before_the_next_probe(make: Any) -> None:
    harness = make("t2")
    runtime = harness.runtime

    def user_selects(name: str, _kwargs: Any) -> None:
        if name == "t2a":
            runtime._runtime_selector_intent_revision = int(runtime._runtime_selector_intent_revision) + 1

    harness.controller.on_delay.append(user_selects)
    run(harness, "t2a", modes={"t2a": "zombie"})
    end = end_of(harness)
    assert (end["outcome"], end["commit"]) == ("authority_changed_selector_intent_revision", "none")
    assert delay_names(harness) == ["t2a"]
    assert harness.judge.gates.hold is None
    assert_read_only(harness)


def test_group_rung_watch_closes_only_after_the_accepted_pf_commit(make: Any) -> None:
    runner = drv.FakeRunner()
    harness = make("t2pair", runner=runner)
    core = harness.controller
    core.sample("t2b", False)  # Core-dead already: rung ① shows no demotion
    tail = ("TIER2-AUTO", "PROXY-FALLBACK", "FINAL-EXIT", "GENERAL")
    core.connections_list = [drv._connection("c1", "t2a", tail), drv._connection("c2", "t2a", tail)]
    run(harness, "t2a", modes={"t2a": "zombie", "t2b": "zombie"})
    assert lines(harness, "demotion_not_visible") and not lines(harness, "demotion")
    commit = fields(lines(harness, "rung_commit")[0])
    assert (commit["grp"], commit["accepted"]) == ("PF", "true")
    # The group-rung watch on L0 starts only after the accepted commit.
    assert runner.watches == ["t2a"]
    watch_at = [ev for ev, _f in events(harness)].index("rung_commit")
    assert "close" not in [ev for ev, _f in events(harness)][:watch_at]
    harness.judge.on_watch_step(runner, runner.last_watch, drv.stuck("c1", "c2"))
    assert harness.quiesce(10.0)
    assert sorted(core.closed) == ["c1", "c2"]
    close = [fields(line) for line in lines(harness, "close")]
    assert close and all(c["rung"] == "2" and c["ladder"] == commit["ladder"] for c in close)
    assert core.now("PROXY-FALLBACK") == "TIER3-AUTO"
    assert_read_only(harness)


def test_group_rung_watch_never_closes_without_a_commit(make: Any) -> None:
    runner = drv.FakeRunner()
    harness = make("t3pair_full", runner=runner, group_dead=("TIER1-SELECT", "TIER2-AUTO"))
    core = harness.controller
    core.sample("n2", False)
    tail = ("TIER3-AUTO", "PROXY-FALLBACK", "FINAL-EXIT", "GENERAL")
    core.connections_list = [drv._connection("c1", "n1", tail)]
    run(harness, "n1", modes={"n1": "zombie", "n2": "zombie"})
    assert end_of(harness)["outcome"] == "exhausted_hold"
    assert runner.watches == [] and core.closed == []
    assert_read_only(harness)


def test_pf_move_by_the_ladder_is_never_an_overshoot_repair(make: Any) -> None:
    # overshoot_repair on: the ladder's own PF move (TIER2-AUTO -> TIER3-AUTO,
    # downward) is a candidate held until ladder_end, then skipped because a
    # judge PF-contract probe resolved within 10 s (spec §6.2 step 3).
    harness = make("t2pair", overshoot_repair=True)
    run(harness, "t2a", modes={"t2a": "zombie", "t2b": "zombie"})
    assert end_of(harness)["pf_probe"] == "1"
    assert drv._wait_for(lambda: bool(lines(harness, "overshoot_repair")), 8.0)
    record = fields(lines(harness, "overshoot_repair")[0])
    assert (record["state"], record["reason"], record["from"], record["to"]) == (
        "skipped", "judge_pf_probe", "T2", "T3")
    views = [r.getMessage() for r in list(harness.logs.records) if r.getMessage().startswith("Runtime ladder view (")]
    assert any("pf_now=T3" in view for view in views)
    # No probe beyond the ladder's own four.
    assert delay_names(harness) == ["t2a", "t2b", "TIER3-AUTO", "TIER2-AUTO"]
    assert_read_only(harness)


def test_escalation_refresh_failure_stops_refresh_unavailable_with_the_cause_hold(make: Any) -> None:
    harness = make("t2pair")
    core = harness.controller

    def controller_down(name: str, _kwargs: Any) -> None:
        if name == "t2b":
            with core.lock:
                core.fail_proxies = 10

    core.on_delay.append(controller_down)
    run(harness, "t2a", modes={"t2a": "zombie", "t2b": "zombie"})
    escalate = fields(lines(harness, "ladder_escalate")[0])
    assert escalate["refresh"] == "unavailable"
    end = end_of(harness)
    assert (end["outcome"], end["hold"], end["commit"]) == ("refresh_unavailable", "EXHAUSTED", "none")
    assert not lines(harness, "ladder_witness") and delay_names(harness) == ["t2a", "t2b"]
    with core.lock:
        core.fail_proxies = 0
    assert_read_only(harness)


@pytest.mark.parametrize(
    "topo,leaf,modes,rung",
    [
        ("t2pair", "t2a", {"t2a": "zombie", "t2b": "zombie"}, "2"),
        ("t1", T1A, {T1A: "zombie", T1A2: "zombie"}, "2"),
    ],
    ids=["pf_rung", "tier1_auto_rung"],
)
def test_committed_group_rung_watch_replaces_the_rung_one_leaf_watch(
    make: Any, topo: str, leaf: str, modes: dict[str, str], rung: str
) -> None:
    # Rung ① shows L demoted at first (its successor still read alive), so
    # its legacy leaf watch starts; rung ① then exhausts, and its own rule
    # no longer shows L demoted.  The accepted group-rung commit hands the
    # watch over to the committed rung, which closes L's stuck connections.
    runner = drv.FakeRunner()
    harness = make(topo, runner=runner)
    core = harness.controller
    tail = ("TIER2-AUTO", "PROXY-FALLBACK", "FINAL-EXIT", "GENERAL")
    core.connections_list = [drv._connection("c1", leaf, tail)]
    run(harness, leaf, modes=modes)
    assert lines(harness, "demotion")
    commit = fields(lines(harness, "rung_commit")[0])
    assert commit["accepted"] == "true" and commit["rung"] == rung
    assert runner.watches == [leaf, leaf]
    record = harness.judge._watches[leaf]
    assert record.ladder.mode == "group" and record.ladder.rung.index == int(rung) - 1
    harness.judge.on_watch_step(runner, runner.last_watch, drv.stuck("c1"))
    assert harness.quiesce(10.0)
    assert core.closed == ["c1"]
    close = [fields(line) for line in lines(harness, "close") if fields(line).get("action") == "close"]
    assert close and close[-1]["rung"] == rung
    assert_read_only(harness)
