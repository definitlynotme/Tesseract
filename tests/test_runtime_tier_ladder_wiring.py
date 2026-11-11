"""Tier ladder runtime wiring (spec_tier_ladder_v1 §7.1 W1-W18), with fakes.

The DesktopRuntime runs over the T0 driver's fake Mihomo controller
(``tests/fixtures/ladder_t0/t0_driver.py``), extended here with group delay
tests and group alive state, so a ladder can escalate to PROXY-FALLBACK.
Nothing talks to a real Mihomo, opens a socket or touches a user profile.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import importlib.util
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

import desktop_runtime as desktop_runtime_module
import runtime_tier_ladder as tl
from desktop_runtime import DesktopRuntime
from runtime_node_judge import ProbeClass, StartProbe
from runtime_route_lease import LeaseClock

ROOT = Path(__file__).resolve().parents[1]


def _load_driver() -> Any:
    name = "ladder_t0_driver_for_wiring"
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
_BASE_TOPOLOGY = drv.topology


def _topology(name: str) -> dict[str, Any]:
    """The driver's shapes plus a few ladder-only ones."""

    if name == "t2pair":
        topo = _BASE_TOPOLOGY("t2")
        topo["groups"]["TIER2-AUTO"] = drv._fb(["t2a", "t2b"])
        topo["leaves"].pop("t2c")
        topo["proxy_map"].pop("t2c")
        return topo
    if name == "t2solo":
        topo = _BASE_TOPOLOGY("t2")
        topo["groups"]["TIER2-AUTO"] = drv._fb(["t2a"])
        topo["groups"]["PROXY-FALLBACK"] = drv._fb(["TIER2-AUTO"])
        for leaf in ("t2b", "t2c"):
            topo["leaves"].pop(leaf)
            topo["proxy_map"].pop(leaf)
        return topo
    if name == "t3direct":
        topo = _BASE_TOPOLOGY("t3")
        topo["groups"]["FINAL-EXIT"] = drv._sel(["TIER3-AUTO", "PROXY-FALLBACK", "REJECT"], "TIER3-AUTO")
        return topo
    if name == "gw_off":
        topo = _BASE_TOPOLOGY("t3")
        topo["groups"]["EXIT-SELECT"] = drv._sel(["gw1"])
        topo["leaves"]["gw1"] = True
        topo["proxy_map"]["gw1"] = {"tier": "exit", "pool_kind": "gateway", "identity": "identity-gw1",
                                    "proxy": {"name": "gw1", "type": "http"}}
        return topo
    return _BASE_TOPOLOGY(name)


drv.topology = _topology


class LadderCore(drv.FakeCore):
    """The T0 fake plus group delay tests (a failed one marks the group dead
    for its parents, Mihomo's own move) and group alive state in reads."""

    def __init__(self, topo: dict[str, Any]) -> None:
        super().__init__(topo)
        self.group_dead: set[str] = set()
        self.sticky_fixed = False
        self.now_override: dict[str, str] = {}
        self.proxy_errors: set[str] = set()

    def alive(self, name: str, depth: int = 0) -> bool:
        # A member group's state is its parent's own record: only a group
        # delay test changes it (Mihomo never derives it from the leaves).
        if name in self.groups:
            return name not in self.group_dead
        return super().alive(name, depth)

    def now(self, name: str, depth: int = 0) -> str:
        if name in self.now_override:
            return self.now_override[name]
        return super().now(name, depth)

    def _group_entry(self, name: str, *, read: bool) -> dict[str, Any]:
        entry = super()._group_entry(name, read=read and not self.sticky_fixed)
        alive = self.alive(name)
        entry["alive"] = alive
        entry["extra"] = {url: {"alive": alive, "history": []} for url in (URL, URL3)}
        return entry

    def proxy(self, name: str, *, timeout: float | None = None) -> dict[str, Any]:
        if name in self.proxy_errors:
            with self.lock:
                self.calls.append(["proxy", name])
            raise RuntimeError("controller read failed")
        return super().proxy(name, timeout=timeout)

    def _leaf_of(self, name: str) -> str:
        current = name
        for _ in range(16):
            if current not in self.groups:
                return current
            current = self.now(current)
        return current

    def proxy_delay(self, name: str, **kwargs: Any) -> dict[str, Any]:
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


def install_contract(harness: Any, directory: Path, *, overrides: dict[str, Any] | None = None) -> Path:
    """Write the Core-loaded runtime config the timeout lookup reads (§4.1)."""

    groups = []
    for name, group in harness.topo["groups"].items():
        timeout = 800 if name.startswith(("TIER1-", "TIER2-")) else 5000
        groups.append({"name": name, "type": str(group["type"]).lower(), "timeout": timeout})
    document: Any = {"proxy-groups": groups}
    if overrides is not None:
        document = overrides
    text = yaml.safe_dump(document, sort_keys=False)
    path = directory / f"runtime-{id(harness)}.yaml"
    path.write_text(text, encoding="utf-8")
    main = hashlib.sha256(text.replace("\n", os.linesep).encode("utf-8")).hexdigest()
    contract = harness.runtime._runtime_loaded_export_contract
    contract["main_semantic_digest"] = main
    contract["runtime_config_path"] = str(path)
    return path


@pytest.fixture
def make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("TESSERACT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TESSERACT_DATA_DIR", str(tmp_path / "data"))
    ctx = drv.Ctx(ROOT, {}, tmp_path, family="wiring")
    made: list[Any] = []

    def build(topo: str = "t2", *, ladder: bool = True, contract: bool = True, arm: bool = True,
              **flags: Any) -> Any:
        options = dict(flags)
        # 10-28: the ladder/rearm/overshoot keys default on; pin the legacy
        # off values so ladder=False and each key stay opt-in here.
        options.setdefault("tier_ladder_judge", bool(ladder))
        options.setdefault("rearm_on_stale_route", False)
        options.setdefault("overshoot_repair", False)
        core = LadderCore(drv.topology(topo))
        harness = drv.Harness(ctx, topo, flags=options, arm=arm, controller=core)
        made.append(harness)
        if contract:
            install_contract(harness, tmp_path)
        return harness

    yield build
    for harness in made:
        harness.close()


def messages(harness: Any, prefix: str) -> list[str]:
    return [record.getMessage() for record in list(harness.logs.records) if record.getMessage().startswith(prefix)]


def observer_lines(harness: Any, ev: str) -> list[str]:
    return messages(harness, f"Post-dial observer {ev} (")


def admit(harness: Any, leaf: str, *, register: bool = True, outcome: dict[str, Any] | None = None) -> Any:
    runtime = harness.runtime
    judge = runtime._runtime_post_dial_judge()
    return runtime._runtime_post_dial_admission(
        harness.trigger_obj(leaf),
        judge,
        now=runtime._runtime_recovery_now(),
        register=register,
        outcome=outcome,
    )


def refs(harness: Any) -> tuple[Any, Any]:
    return harness.runtime._runtime_post_dial_route_refs()


def call_kinds(harness: Any) -> list[list[Any]]:
    with harness.controller.lock:
        out, harness.controller.calls = list(harness.controller.calls), []
    return out


# ---------------------------------------------------------------------------
# W1: loaded group timeouts


def test_w1_timeouts_follow_the_loaded_contract_and_are_cached(make: Any, tmp_path: Path) -> None:
    harness = make("t1")
    runtime = harness.runtime
    snapshot, _payload = refs(harness)
    timeouts = runtime._runtime_loaded_group_timeouts(snapshot)
    assert timeouts["TIER1-nodeA-AUTO"] == 800
    assert timeouts["TIER2-AUTO"] == 800
    assert timeouts["PROXY-FALLBACK"] == 5000 and timeouts["TIER3-AUTO"] == 5000
    assert isinstance(runtime._runtime_group_timeouts_parse_ms, float)
    # Cached under the main digest: a second call reads no file.
    path = Path(runtime._runtime_loaded_export_contract["runtime_config_path"])
    path.write_text("proxy-groups: []\n", encoding="utf-8")
    assert runtime._runtime_loaded_group_timeouts(snapshot) is timeouts
    # The route-authority digest also matches (a provider rebase).
    contract = runtime._runtime_loaded_export_contract
    contract["route_authority_semantic_digest"] = snapshot.semantic_digest
    contract["semantic_digest"] = "rebased"
    assert runtime._runtime_loaded_group_timeouts(snapshot) is timeouts
    contract["route_authority_semantic_digest"] = "other"
    assert runtime._runtime_loaded_group_timeouts(snapshot) is None
    contract["semantic_digest"] = snapshot.semantic_digest


def test_w1_sha_mismatch_and_bad_documents_give_none(make: Any, tmp_path: Path) -> None:
    harness = make("t2")
    runtime = harness.runtime
    snapshot, _payload = refs(harness)
    path = Path(runtime._runtime_loaded_export_contract["runtime_config_path"])
    path.write_text(path.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    assert runtime._runtime_loaded_group_timeouts(snapshot) is None
    for document in ({"proxy-groups": None}, ["not", "a", "dict"]):
        install_contract(harness, tmp_path, overrides=document)
        assert runtime._runtime_loaded_group_timeouts(snapshot) is None
    # Two-entry cache: three digests leave the newest two.
    digests = []
    for index in range(3):
        install_contract(harness, tmp_path, overrides={"proxy-groups": [{"name": f"G{index}", "timeout": 7}]})
        assert runtime._runtime_loaded_group_timeouts(snapshot) == {f"G{index}": 7}
        digests.append(runtime._runtime_loaded_export_contract["main_semantic_digest"])
    assert list(runtime._runtime_group_timeouts_cache) == digests[1:]


def test_w1_never_called_with_the_key_off(make: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = make("t3", ladder=False)
    runtime = harness.runtime
    calls: list[Any] = []
    monkeypatch.setattr(DesktopRuntime, "_runtime_loaded_group_timeouts", lambda self, s: calls.append(s))
    job, reason = admit(harness, "n1")
    assert reason is None and job is not None and job.ladder is None
    harness.trigger("n1", episode=2)
    harness.quiesce(10.0)
    assert calls == []


# ---------------------------------------------------------------------------
# W2: observer route context


def test_w2_route_context_key_off_is_legacy_and_key_on_is_every_pf_leaf(make: Any) -> None:
    off = make("t1", ladder=False)
    snapshot, _payload = refs(off)
    assert off.runtime._runtime_post_dial_route_context_for(snapshot) == off.runtime._runtime_post_dial_route_context(
        snapshot
    )
    on = make("t1")
    snapshot, _payload = refs(on)
    context = on.runtime._runtime_post_dial_route_context_for(snapshot)
    assert context.tier3_members == frozenset({drv.T1A, drv.T1A2, drv.T1B, drv.T1B2, "t2a", "t2b", "n1", "n2", "n3"})
    assert context.gateway_groups == frozenset({"EXIT-SELECT"})


# ---------------------------------------------------------------------------
# W3: rung gate matrix


@pytest.mark.parametrize(
    "topo,leaf,expected",
    [
        ("t2", "t2a", [("TIER2-AUTO", "t2a", 800), ("PROXY-FALLBACK", "TIER2-AUTO", 5000)]),
        ("t3", "n1", [("TIER3-AUTO", "n1", 5000)]),
        ("t1", drv.T1A, [("TIER1-nodeA-AUTO", drv.T1A, 800), ("TIER1-AUTO", "TIER1-nodeA-SELECT", 800),
                         ("PROXY-FALLBACK", "TIER1-SELECT", 5000)]),
        ("t1_user", drv.T1A2, [("PROXY-FALLBACK", "TIER1-SELECT", 5000)]),
        ("gw_off", "n1", [("TIER3-AUTO", "n1", 5000)]),
    ],
)
def test_w3_rung_gate_names_the_native_rungs(make: Any, topo: str, leaf: str, expected: list[Any]) -> None:
    harness = make(topo)
    snapshot, payload = refs(harness)
    reason, binding, result = harness.runtime._runtime_post_dial_rung_gate(leaf, snapshot, payload)
    assert reason is None and binding.node.proxy_name == leaf
    assert [(r.group, r.accused, r.timeout_ms) for r in result.rungs] == expected


@pytest.mark.parametrize(
    "topo,leaf,reason",
    [
        ("gw", "gw1", "gateway_scope_off"),
        ("t2", "t2b", "route_binding_mismatch"),
        ("t3direct", "n1", "route_outside_ladder"),
        ("t2solo", "t2a", "no_rung"),
    ],
)
def test_w3_rung_gate_reasons(make: Any, topo: str, leaf: str, reason: str) -> None:
    harness = make(topo)
    snapshot, payload = refs(harness)
    assert harness.runtime._runtime_post_dial_rung_gate(leaf, snapshot, payload)[0] == reason


def test_w3_missing_timeouts_are_contract_unavailable(make: Any) -> None:
    harness = make("t2", contract=False)
    snapshot, payload = refs(harness)
    assert harness.runtime._runtime_post_dial_rung_gate("t2a", snapshot, payload)[0] == "contract_unavailable"


def test_w3_gateway_scope_off_queues_no_refresh_but_mismatch_does(make: Any) -> None:
    harness = make("gw")
    runtime = harness.runtime
    starts = int(runtime._runtime_route_snapshot_refresh_starts)
    assert admit(harness, "gw1") == (None, "gateway_scope_off")
    harness.quiesce(5.0)
    assert int(runtime._runtime_route_snapshot_refresh_starts) == starts
    assert not runtime._node_judgement_state().binding_refresh_keys
    other = make("t2")
    assert admit(other, "t2b") == (None, "route_binding_mismatch")
    assert other.runtime._node_judgement_state().binding_refresh_keys


# ---------------------------------------------------------------------------
# W4: admission, ticket window, commits late in the ladder


def test_w4_admission_status_sample_uses_rung_one(make: Any) -> None:
    harness = make("t1")
    runtime = harness.runtime
    judge = runtime._runtime_post_dial_judge()
    seen: list[Any] = []
    original = judge.latest_sample_is_status_failure

    def spy(leaf: str, proxy: Any, url: str) -> bool:
        seen.append((leaf, url, isinstance(proxy, dict)))
        return original(leaf, proxy, url)

    judge.latest_sample_is_status_failure = spy
    job, reason = admit(harness, drv.T1A)
    assert reason is None
    assert seen == [(drv.T1A, URL, True)]
    assert job.test_url == URL and job.context.provider_name == ""
    assert job.ladder is not None and len(job.ladder.rungs) == 3


def test_w4_ticket_window_is_twenty_seconds_on_and_six_off(make: Any) -> None:
    on = make("t3")
    job, _reason = admit(on, "n1")
    ticket = job.context.ticket
    assert round(ticket.deadline_monotonic - ticket.observed_at_monotonic, 3) == 20.0
    assert job.ladder.deadline == ticket.deadline_monotonic
    off = make("t3", ladder=False)
    job, _reason = admit(off, "n1")
    ticket = job.context.ticket
    assert round(ticket.deadline_monotonic - ticket.observed_at_monotonic, 3) == 6.0


def test_w4_durable_only_for_a_tier3_leaf_rung(make: Any) -> None:
    tier3 = make("t3", durable_conviction=True)
    assert admit(tier3, "n1")[0].durable is True
    tier2 = make("t2", durable_conviction=True)
    assert admit(tier2, "t2a")[0].durable is False


@pytest.mark.parametrize("offset", [8.0, 15.0])
def test_w4_rung_commit_late_in_the_ladder_is_accepted(make: Any, offset: float) -> None:
    harness = make("t2")
    runtime = harness.runtime
    shift = [0.0]
    runtime._runtime_recovery_monotonic = lambda: time.monotonic() + shift[0]
    job, reason = admit(harness, "t2a")
    assert reason is None
    harness.controller.sample("t2a", False)
    job.ladder.passed[0] = {"t2b"}
    shift[0] = offset
    accepted, why = runtime._runtime_post_dial_native_commit(job, SimpleNamespace(successor="t2b"))
    assert (accepted, why) == (True, "post_dial_rung_native_rebind")
    outcome = runtime._runtime_recovery_outcomes[job.context.incident_id]
    assert outcome.reason == "post_dial_rung_native_rebind"
    assert outcome.to_ref.proxy_name == "t2b"
    assert round(job.context.ticket.deadline_monotonic - job.ladder.deadline, 6) == 0.0


def test_w4_shadow_would_judge_carries_the_rung_count(make: Any) -> None:
    harness = make("t2", post_dial_judge="shadow")
    harness.trigger("t2a")
    harness.quiesce(5.0)
    lines = observer_lines(harness, "would_judge")
    assert lines and "ladder=1 rungs=2" in lines[-1]
    off = make("t2", ladder=False, post_dial_judge="shadow")
    off.trigger("t2a")
    off.quiesce(5.0)
    assert all("ladder=" not in line for line in observer_lines(off, "would_judge"))


# ---------------------------------------------------------------------------
# W5: scoped rearm


def _stale_arm(harness: Any, *, leaf: str = "t2a") -> tuple[list[Any], list[str]]:
    runtime = harness.runtime
    judge = runtime._runtime_post_dial_judge()
    records: list[Any] = []
    queued: list[str] = []
    runtime._node_judgement_stale_arm_refresh_refusal = lambda revision: None
    runtime._queue_post_dial_binding_refresh = lambda *a, **k: queued.append("queued") or "queued"
    runtime._node_judgement_arm_submit = lambda record, scope, ticket=None: records.append(dict(record))
    snapshot, payload = refs(harness)
    runtime._runtime_post_dial_stale_arm_refresh(
        harness.trigger_obj(leaf, episode=len(records) + 7),
        judge,
        now=runtime._runtime_recovery_now(),
        snapshot=snapshot,
        payload=payload,
        route_reason=None,
        d4_busy=False,
        status_failure=False,
        intent_revision=int(runtime._runtime_selector_intent_revision),
        arm_check="route_epoch",
        outcome={},
        ticket=({"lock": threading.Lock()},),
    )
    return records, queued


def test_w5_ladder_scope_queues_with_rearm_off(make: Any) -> None:
    harness = make("t2")
    records, queued = _stale_arm(harness)
    assert queued == ["queued"]
    assert records[-1]["disposition"] == "queued"
    assert records[-1]["key_on"] == 0 and records[-1]["rearm_source"] == "ladder"
    keyed = make("t2", rearm_on_stale_route=True)
    records, queued = _stale_arm(keyed)
    assert queued and records[-1]["key_on"] == 1 and records[-1]["rearm_source"] == "key"


def test_w5_out_of_scope_would_queue(make: Any) -> None:
    for topo, leaf in (("gw", "gw1"), ("t3direct", "n1")):
        harness = make(topo)
        records, queued = _stale_arm(harness, leaf=leaf)
        assert queued == [] and records[-1]["disposition"] == "would_queue"
    harness = make("t2")
    with harness.runtime._runtime_failure_monitor_lock:
        harness.runtime._runtime_route_mutation_lease = {"group": "FINAL-EXIT", "to": "REJECT", "mode": "selector"}
    records, queued = _stale_arm(harness)
    assert queued == [] and records[-1]["disposition"] == "would_queue"
    # A settled selector lease does not stop the scope.
    with harness.runtime._runtime_failure_monitor_lock:
        harness.runtime._runtime_route_mutation_lease = {"group": "TIER1-SELECT", "to": "TIER1-AUTO",
                                                         "mode": "selector", "release_state": "pending"}
    records, queued = _stale_arm(harness)
    assert queued == ["queued"]


def test_w5_both_keys_off_keep_todays_record(make: Any) -> None:
    harness = make("t2", ladder=False)
    records, queued = _stale_arm(harness)
    assert queued == [] and records[-1]["disposition"] == "would_queue"
    assert "rearm_source" not in records[-1] and records[-1]["key_on"] == 0


# ---------------------------------------------------------------------------
# W6: rung reads


def test_w6_inline_leaf_rung_reads_the_group_and_its_members(make: Any) -> None:
    harness = make("t2")
    runtime = harness.runtime
    job, _reason = admit(harness, "t2a")
    call_kinds(harness)
    read = runtime._runtime_post_dial_route_read(job)
    calls = call_kinds(harness)
    assert calls == [["proxy", "TIER2-AUTO"], ["proxy", "t2a"], ["proxy", "t2b"], ["proxy", "t2c"]]
    assert read.now == "t2a" and read.alive == {"t2a": True, "t2b": True, "t2c": True}
    assert job.ladder.drop_consumed == {}
    job.ladder.accused_failed_known[0] = True
    runtime._runtime_post_dial_route_read(job)
    assert job.ladder.drop_consumed == {0: True}


def test_w6_tier3_rung_reads_the_single_provider(make: Any) -> None:
    harness = make("t3")
    job, _reason = admit(harness, "n1")
    call_kinds(harness)
    harness.runtime._runtime_post_dial_route_read(job)
    calls = call_kinds(harness)
    assert ["provider", "TIER3B-RUNTIME"] in calls
    assert all(call[0] in {"proxy", "provider"} for call in calls)


def test_w6_group_rung_uses_one_full_refresh_and_no_member_group_read(make: Any) -> None:
    harness = make("t2")
    job, _reason = admit(harness, "t2a")
    with job.ladder.lock:
        job.ladder.index = 1
    call_kinds(harness)
    read = harness.runtime._runtime_post_dial_route_read(job)
    assert call_kinds(harness) == [["proxies"]]
    assert read.now == "TIER2-AUTO" and set(read.alive) == {"TIER2-AUTO", "TIER3-AUTO"}
    assert harness.controller.writes == []


def test_w6_fresh_state_is_none_on_a_busy_refresh_lock(make: Any) -> None:
    harness = make("t2")
    runtime = harness.runtime
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with runtime._runtime_route_snapshot_refresh_lock:
            held.set()
            release.wait(5.0)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert held.wait(2.0)
        assert runtime._runtime_post_dial_fresh_state(0.1) is None
    finally:
        release.set()
        thread.join(2.0)
    assert runtime._runtime_post_dial_fresh_state(0.5) is not None


# ---------------------------------------------------------------------------
# W7: rung commit with the sacrificial read


def test_w7_pinned_accused_gets_one_sacrificial_read_before_the_refresh(make: Any) -> None:
    harness = make("t2_pinned")
    runtime = harness.runtime
    arms: list[int] = []
    original_arm = runtime._try_arm_runtime_recovery
    runtime._try_arm_runtime_recovery = lambda: arms.append(1) or original_arm()
    job, reason = admit(harness, "t2b")
    assert reason is None and job.ladder.rungs[0].fixed0 == "t2b"
    harness.controller.sample("t2b", False)
    job.ladder.passed[0] = {"t2a"}
    call_kinds(harness)
    accepted, why = runtime._runtime_post_dial_native_commit(job, SimpleNamespace(successor="t2a"))
    calls = call_kinds(harness)
    assert (accepted, why) == (True, "post_dial_rung_native_rebind")
    assert calls[0] == ["proxy", "TIER2-AUTO"] and ["proxies"] in calls[1:]
    assert job.ladder.commit_sacrificial is True and job.ladder.commit_pick == "t2a"
    assert job.ladder.commit_proven is True
    assert runtime._runtime_recovery_outcomes[job.context.incident_id].to_ref.proxy_name == "t2a"
    assert arms == [1]


def test_w7_unpinned_commit_has_no_sacrificial_read(make: Any) -> None:
    harness = make("t2")
    runtime = harness.runtime
    job, _reason = admit(harness, "t2a")
    harness.controller.sample("t2a", False)
    job.ladder.passed[0] = {"t2b"}
    call_kinds(harness)
    accepted, _why = runtime._runtime_post_dial_native_commit(job, SimpleNamespace(successor="t2b"))
    assert accepted is True
    assert call_kinds(harness)[0] == ["proxies"]
    assert job.ladder.commit_sacrificial is False


def test_w7_pin_still_set_is_pin_drop_unsettled_with_one_refresh(make: Any) -> None:
    harness = make("t2_pinned")
    runtime = harness.runtime
    job, _reason = admit(harness, "t2b")
    harness.controller.sample("t2b", False)
    harness.controller.sticky_fixed = True
    queued: list[int] = []
    runtime._queue_runtime_route_snapshot_refresh = lambda: queued.append(1)
    accepted, why = runtime._runtime_post_dial_native_commit(job, SimpleNamespace(successor="t2a"))
    assert (accepted, why) == (False, "pin_drop_unsettled") and queued == [1]
    outcome = runtime._runtime_recovery_outcomes[job.context.incident_id]
    assert outcome.reason == "post_dial_pin_drop_unsettled"


def test_w7_native_pick_mismatch_and_moved_unproven(make: Any) -> None:
    harness = make("t2")
    runtime = harness.runtime
    job, _reason = admit(harness, "t2a")
    harness.controller.sample("t2a", False)
    harness.controller.now_override["TIER2-AUTO"] = "t2c"
    queued: list[int] = []
    runtime._queue_runtime_route_snapshot_refresh = lambda: queued.append(1)
    assert runtime._runtime_post_dial_native_commit(job, SimpleNamespace(successor="t2b")) == (
        False,
        "native_pick_mismatch",
    )
    assert queued == [1]
    other = make("t2")
    job, _reason = admit(other, "t2a")
    other.controller.sample("t2a", False)
    assert other.runtime._runtime_post_dial_native_commit(job, SimpleNamespace(successor=None)) == (
        False,
        "moved_unproven",
    )
    assert job.ladder.commit_proven is False and job.ladder.commit_pick == "t2b"
    outcome = other.runtime._runtime_recovery_outcomes[job.context.incident_id]
    assert outcome.reason == "post_dial_moved_unproven"


@pytest.mark.parametrize(
    "fixed,alive,expected",
    [
        ("", {"t2a": False, "t2b": True, "t2c": True}, "t2b"),
        ("t2c", {"t2a": True, "t2b": True, "t2c": True}, "t2c"),
        ("t2c", {"t2a": False, "t2b": True, "t2c": False}, "t2b"),
        ("", {"t2a": False, "t2b": False, "t2c": False}, "t2a"),
    ],
)
def test_w7_expected_native_pick_cells(fixed: str, alive: dict[str, bool], expected: str) -> None:
    assert tl.expected_native_pick(("t2a", "t2b", "t2c", "REJECT"), fixed, alive).pick == expected


# ---------------------------------------------------------------------------
# W8: authority


def test_w8_each_authority_change_stops_the_ladder(make: Any) -> None:
    harness = make("t2")
    runtime = harness.runtime
    job, _reason = admit(harness, "t2a")
    check = runtime._runtime_post_dial_ladder_check
    assert check(job) is None
    cases = [
        ("_runtime_selector_intent_revision", "authority_changed_selector_intent_revision"),
        ("_runtime_facility_epoch", "authority_changed_facility_epoch"),
        ("_lifecycle_revision", "authority_changed_lifecycle_revision"),
        ("_runtime_generation", "authority_changed_worker_token"),
    ]
    for name, expected in cases:
        before = int(getattr(runtime, name, 0) or 0)
        setattr(runtime, name, before + 1)
        assert check(job) == expected
        setattr(runtime, name, before)
        assert check(job) is None
    snapshot = runtime._runtime_route_snapshot
    for name, value in (("core_generation", 99), ("semantic_digest", "x"), ("network_context_epoch", 5),
                        ("group_graph", (("G", ("a",)),))):
        runtime._runtime_route_snapshot = dataclasses.replace(snapshot, **{name: value})
        assert check(job) == f"authority_changed_{name}"
    runtime._runtime_route_snapshot = snapshot
    runtime.config["client"]["egress_recovery"]["node_judgement"]["post_dial_judge"] = "shadow"
    assert check(job) == "authority_changed_policy"
    runtime.config["client"]["egress_recovery"]["node_judgement"]["post_dial_judge"] = "act"
    with runtime._runtime_failure_monitor_lock:
        runtime._runtime_route_mutation_lease = {"group": "TIER2-AUTO", "to": "t2b", "mode": "fixed"}
    assert check(job) == "authority_changed_lease"


def _clock(group: str = "TIER2-AUTO", member: str = "t2b", **changes: Any) -> LeaseClock:
    clock = LeaseClock(group=group, member=member, started_mono=10.0, started_wall=1.0,
                       last_decision_mono=10.0, last_decision_wall=1.0)
    for name, value in changes.items():
        setattr(clock, name, value)
    return clock


def test_w8_lease_digest_ignores_counters_and_reacts_to_clock_state(make: Any) -> None:
    harness = make("t2")
    runtime = harness.runtime
    runtime._route_lease_clocks = {"TIER2-AUTO": _clock()}
    job, _reason = admit(harness, "t2a")
    check = runtime._runtime_post_dial_ladder_check
    assert check(job) is None
    runtime._route_lease_clocks = {"TIER2-AUTO": _clock(reads=7, deferred_s=3.0, attempts=2, defer_reason="x")}
    runtime._route_lease_clocks["TIER2-AUTO"].skipped.add("y")
    assert check(job) is None
    for changes in ({"ended": "core_cleared"}, {"purge_pending": True}, {"verify_retry": True}):
        runtime._route_lease_clocks = {"TIER2-AUTO": _clock(**changes)}
        assert check(job) == "authority_changed_lease"
    runtime._route_lease_clocks = {"TIER2-AUTO": _clock(), "TIER3-AUTO": _clock("TIER3-AUTO", "n2")}
    assert check(job) == "authority_changed_lease"


# ---------------------------------------------------------------------------
# W9: D4 transactions


def _bare(**attrs: Any) -> DesktopRuntime:
    runtime = object.__new__(DesktopRuntime)
    runtime.config = {}
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_passive_failback_requests = {}
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_confirmed_fail_closed_snapshot_key = None
    runtime._runtime_egress_coordinator = SimpleNamespace(core_mutation_active=lambda: False)
    for name, value in attrs.items():
        setattr(runtime, name, value)
    return runtime


@pytest.mark.parametrize(
    "attrs,expected",
    [
        ({"_runtime_failure_recovery_bindings": {("k",)}}, "d4_txn_busy:recovery_worker"),
        ({"_runtime_route_mutation_lease": {"group": "TIER2-AUTO", "mode": "fixed", "release_state": "releasing"}},
         "d4_txn_busy:lease_releasing"),
        ({"_runtime_passive_failback_requests": {"a": {"running": True}}}, "d4_txn_busy:passive_failback"),
        ({"_runtime_route_mutation_lease": {"group": "FINAL-EXIT", "to": "REJECT"}}, "d4_txn_busy:fail_closed"),
        ({"_runtime_confirmed_fail_closed_snapshot_key": ("s",)}, "d4_txn_busy:fail_closed"),
        ({"_runtime_egress_coordinator": SimpleNamespace(core_mutation_active=lambda: True)},
         "d4_txn_busy:core_mutation"),
        ({"_runtime_route_mutation_lease": {"group": "TIER2-AUTO", "to": "t2b", "mode": "fixed",
                                            "release_state": "pending"}}, None),
        ({"_runtime_route_mutation_lease": {"group": "TIER1-SELECT", "to": "TIER1-AUTO", "mode": "selector",
                                            "release_state": "pending"}}, None),
        ({}, None),
    ],
)
def test_w9_d4_transaction_busy(attrs: dict[str, Any], expected: str | None) -> None:
    assert _bare(**attrs)._runtime_post_dial_d4_txn_busy() == expected


def test_w9_admission_gates_a_d4_transaction(make: Any) -> None:
    harness = make("t2")
    with harness.runtime._runtime_failure_monitor_lock:
        harness.runtime._runtime_passive_failback_requests = {"x": {"running": True}}
    assert admit(harness, "t2a") == (None, "d4_txn_busy_passive_failback")


# ---------------------------------------------------------------------------
# W10: D4 select-write guard


def _switch_rows(harness: Any, **kwargs: Any) -> dict[str, Any]:
    from runtime_egress_recovery import RuntimeNodeRef

    runtime = harness.runtime
    snapshot, payload = refs(harness)
    failure = drv._failure(snapshot, payload, runtime)
    rows = {}
    for alt in ["n1", "n2", drv.T1A, drv.T1A2, drv.T1B, drv.T1B2, "t2a", "t2b", "t2c"]:
        info = harness.topo["proxy_map"].get(alt, {"tier": "tier3", "pool_kind": "tier3b"})
        ref = RuntimeNodeRef(tier=info["tier"], pool_kind=info["pool_kind"], identity=f"identity-{alt}",
                             proxy_name=alt)
        rows[alt] = runtime._runtime_alternative_switch_action(failure, ref, payload, **kwargs)
    return rows


@pytest.mark.parametrize("topo", ["t1", "t1_user", "t1_flat", "t2", "t2_pinned", "t3_full"])
def test_w10_guard_never_returns_a_pf_subtree_select(make: Any, topo: str) -> None:
    on = make(topo)
    off = make(topo, ladder=False)
    _snapshot, payload = refs(on)
    subtree = tl.descendant_groups_of_payload(on.runtime._runtime_proxy_entries(payload))
    guarded = _switch_rows(on)
    legacy = _switch_rows(off)
    for alt, row in guarded.items():
        if isinstance(row, dict) and row["mode"] == "selector":
            assert row["group"] not in subtree, (topo, alt, row)
        if isinstance(row, dict) and row["group"] == "FINAL-EXIT":
            assert row == legacy[alt]
        if isinstance(legacy[alt], dict) and legacy[alt]["mode"] == "fixed":
            # The deepest fallback layer stays reachable.
            assert row is not None
    assert _switch_rows(on, allow_pf_subtree_select=True) == legacy
    assert _switch_rows(off, allow_pf_subtree_select=True) == legacy


def test_w10_call_sites_pass_the_opt_out_only_for_the_user_check() -> None:
    sites = drv._switch_call_sites(ROOT)
    assert len(sites) == 8
    for site in sites:
        caller = site["caller"].split(".")[-1]
        if caller == "_check_exit_quality_impl":
            assert site["keywords"] == {"allow_pf_subtree_select": "True"}
        else:
            assert site["keywords"] == {}, site


# ---------------------------------------------------------------------------
# W11: overshoot_repair


class _SpyLock:
    def __init__(self) -> None:
        self.entered = 0

    def __enter__(self) -> Any:
        self.entered += 1
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def acquire(self, *args: Any, **kwargs: Any) -> bool:
        self.entered += 1
        return True

    def release(self) -> None:
        return None


def test_w11_detection_is_lock_free_and_only_for_a_downward_unfixed_move(
    make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make("t1", overshoot_repair=True)
    runtime = harness.runtime
    snapshot, _payload = refs(harness)
    graph = dict(snapshot.group_graph)
    pf = snapshot.group_state("PROXY-FALLBACK")
    chain = ("FINAL-EXIT", "PROXY-FALLBACK", "TIER2-AUTO", "t2a")
    locks = [_SpyLock() for _ in range(3)]
    runtime._runtime_failure_monitor_lock, runtime._runtime_route_snapshot_lock = locks[0], locks[1]
    runtime.__dict__["_runtime_overshoot_lock"] = locks[2]
    started: list[Any] = []
    monkeypatch.setattr(desktop_runtime_module.threading, "Thread", lambda *a, **k: started.append(a))
    assert runtime._runtime_overshoot_detect(snapshot, pf, chain, graph, "TIER2-AUTO", "TIER1-SELECT") is False
    assert runtime._runtime_overshoot_detect(snapshot, pf, chain, graph, "TIER1-SELECT", "TIER2-AUTO") is True
    # One per episode.
    assert runtime._runtime_overshoot_detect(snapshot, pf, chain, graph, "TIER1-SELECT", "TIER2-AUTO") is False
    pinned = dataclasses.replace(pf, fixed="TIER2-AUTO")
    snapshot2 = dataclasses.replace(snapshot, route_epoch=snapshot.route_epoch + 1)
    assert runtime._runtime_overshoot_detect(snapshot2, pinned, chain, graph, "TIER1-SELECT", "TIER2-AUTO") is False
    assert [lock.entered for lock in locks] == [0, 0, 0] and started == []
    assert runtime._runtime_overshoot_pending[:2] == ("TIER1-SELECT", "TIER2-AUTO")
    assert runtime._runtime_overshoot_event.is_set()


def _overshoot_harness(make: Any, **flags: Any) -> Any:
    harness = make("t1", overshoot_repair=True, **flags)
    # PF's own check failed on TIER1-SELECT although its subtree is alive.
    harness.controller.group_dead.add("TIER1-SELECT")
    assert harness.runtime._refresh_runtime_route_snapshot(timeout_seconds=1.0) is not None
    return harness


def _decide(harness: Any, detected_at: float | None = None) -> tuple[Any, ...]:
    runtime = harness.runtime
    episode = (int(runtime._runtime_route_snapshot.route_epoch), "TIER1-SELECT", "TIER2-AUTO")
    return runtime._runtime_overshoot_decide(
        "TIER1-SELECT", "TIER2-AUTO", episode,
        time.monotonic() - 30.0 if detected_at is None else detected_at, runtime.stop_event,
    )


def test_w11_worker_skips(make: Any) -> None:
    harness = _overshoot_harness(make, post_dial_judge="shadow")
    runtime = harness.runtime
    assert _decide(harness) == ("would_probe", "shadow", "-", None, "-")
    runtime._runtime_overshoot_last_judge_pf_probe = time.monotonic()
    assert _decide(harness)[:2] == ("skipped", "judge_pf_probe")
    runtime._runtime_overshoot_last_judge_pf_probe = None
    with runtime._runtime_failure_monitor_lock:
        runtime._runtime_route_mutation_lease = {"group": "PROXY-FALLBACK", "to": "TIER2-AUTO", "mode": "fixed"}
    assert _decide(harness)[:2] == ("skipped", "d4_busy")
    with runtime._runtime_failure_monitor_lock:
        runtime._runtime_route_mutation_lease = None
    runtime._runtime_overshoot_history = [time.monotonic() - 5.0]
    assert _decide(harness)[:2] == ("skipped", "cap")
    runtime._runtime_overshoot_history = [time.monotonic() - 100.0 - i for i in range(6)]
    assert _decide(harness)[:2] == ("skipped", "cap")
    runtime._runtime_overshoot_history = []
    for leaf in (drv.T1A, drv.T1A2, drv.T1B, drv.T1B2):
        harness.controller.sample(leaf, False)
    assert _decide(harness)[:2] == ("skipped", "subtree_dead")
    harness.controller.group_dead.discard("TIER1-SELECT")
    harness.controller.sample(drv.T1A, True)
    runtime._refresh_runtime_route_snapshot(timeout_seconds=1.0)
    assert _decide(harness)[:2] == ("skipped", "superseded")


def test_w11_act_probe_uses_the_pf_contract_once_per_episode(make: Any) -> None:
    harness = _overshoot_harness(make)
    runtime = harness.runtime
    result = _decide(harness)
    assert result[0] == "probed" and result[2] == "pass"
    delay = [call for call in harness.controller.delay_calls if call["name"] == "TIER1-SELECT"]
    assert delay and delay[-1]["url"] == URL and delay[-1]["timeout_ms"] == 5000
    assert delay[-1]["expected_status"] == "204"
    assert _decide(harness)[:2] == ("skipped", "cap")
    assert harness.controller.writes == []


def test_w11_candidate_during_a_ladder_waits_for_ladder_end(make: Any) -> None:
    harness = _overshoot_harness(make)
    runtime = harness.runtime
    judge = runtime._runtime_post_dial_judge()
    judge._ladder_job = object()
    pending = ("TIER1-SELECT", "TIER2-AUTO", (1, "TIER1-SELECT", "TIER2-AUTO"), time.monotonic(), True)
    runtime._runtime_overshoot_pending = pending
    runtime._runtime_overshoot_evaluate(pending, runtime.stop_event)
    assert runtime._runtime_overshoot_pending is pending
    judge._ladder_job = None
    event = runtime.__dict__.setdefault("_runtime_overshoot_event", threading.Event())
    event.clear()
    runtime._runtime_post_dial_ladder_end(SimpleNamespace(), "convicted")
    assert event.is_set()


def test_w11_worker_exits_when_its_token_is_stale(make: Any) -> None:
    harness = _overshoot_harness(make)
    runtime = harness.runtime
    assert runtime._runtime_overshoot_ensure_worker() is True
    thread = runtime._runtime_overshoot_thread
    assert thread.name == "runtime-post-dial-overshoot" and thread.is_alive()
    runtime._runtime_generation = int(runtime._runtime_generation) + 1
    runtime._runtime_overshoot_event.set()
    thread.join(3.0)
    assert not thread.is_alive()


def _bump_facility_epoch(runtime: Any) -> None:
    runtime._runtime_facility_epoch = int(getattr(runtime, "_runtime_facility_epoch", 0) or 0) + 1


def _evaluate_recorder(runtime: Any) -> tuple[list[Any], threading.Event]:
    calls: list[Any] = []
    done = threading.Event()

    def evaluate(pending: Any, _stop_event: Any, _token: Any = None) -> None:
        calls.append(pending)
        runtime._runtime_overshoot_handled = pending
        done.set()

    runtime._runtime_overshoot_evaluate = evaluate
    return calls, done


def _candidate() -> tuple[Any, ...]:
    return ("TIER1-SELECT", "TIER2-AUTO", (1, "TIER1-SELECT", "TIER2-AUTO"), time.monotonic(), False)


@pytest.mark.parametrize("ensure_between", [True, False])
def test_w11_a_stale_worker_never_consumes_a_candidate(make: Any, ensure_between: bool) -> None:
    """Review fix: a worker whose token went stale (facility epoch bump) exits
    without consuming the wake; the next current worker evaluates the
    candidate exactly once."""

    runtime = _overshoot_harness(make).runtime
    calls, done = _evaluate_recorder(runtime)
    assert runtime._runtime_overshoot_ensure_worker() is True
    stale = runtime._runtime_overshoot_thread
    _bump_facility_epoch(runtime)
    if ensure_between:  # the observer's ensure replaces the stale live worker
        assert runtime._runtime_overshoot_ensure_worker() is True
        assert runtime._runtime_overshoot_thread is not stale
    pending = _candidate()
    runtime._runtime_overshoot_pending = pending  # detection: write, then wake
    runtime._runtime_overshoot_event.set()
    stale.join(3.0)
    assert not stale.is_alive()
    if not ensure_between:
        assert pending not in calls and runtime._runtime_overshoot_event.is_set()
        assert runtime._runtime_overshoot_ensure_worker() is True
    deadline = time.monotonic() + 3.0
    while pending not in calls and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.1)
    # (the harness's own publications may add real candidates of other episodes)
    assert calls.count(pending) == 1


def test_w11_a_new_worker_evaluates_an_already_pending_candidate(make: Any) -> None:
    runtime = _overshoot_harness(make).runtime
    calls, done = _evaluate_recorder(runtime)
    pending = _candidate()
    runtime._runtime_overshoot_pending = pending
    runtime.__dict__.setdefault("_runtime_overshoot_event", threading.Event()).clear()
    assert runtime._runtime_overshoot_ensure_worker() is True
    assert done.wait(3.0) and calls[0] is pending


def _gated_settle(runtime: Any, parties: int) -> list[int]:
    """Hold every worker's settle wait until ``parties`` threads are in it;
    later waits (the publication loop) are real."""

    arrivals: list[int] = []
    released = threading.Event()
    real_wait = runtime._runtime_overshoot_wait

    def wait(seconds: float, stop: Any) -> bool:
        ident = threading.get_ident()
        if ident in arrivals:
            return real_wait(seconds, stop)
        arrivals.append(ident)
        if len(arrivals) >= parties:
            released.set()
        assert released.wait(5.0)
        return False

    runtime._runtime_overshoot_wait = wait
    return arrivals


def _overshoot_effects(harness: Any, episode: int, before: int) -> tuple[int, list[str]]:
    with harness.controller.lock:
        probes = [c for c in harness.controller.delay_calls if c["name"] == "TIER1-SELECT"][before:]
    records = [
        r.getMessage() for r in list(harness.logs.records)
        if "overshoot_repair (" in r.getMessage() and f"episode={episode})" in r.getMessage()
    ]
    return len(probes), records


def _tier1_probe_count(harness: Any) -> int:
    with harness.controller.lock:
        return len([c for c in harness.controller.delay_calls if c["name"] == "TIER1-SELECT"])


def _decide_spy(runtime: Any) -> list[tuple[int, Any]]:
    decides: list[tuple[int, Any]] = []
    real_decide = runtime._runtime_overshoot_decide

    def decide(*args: Any) -> Any:
        decides.append((threading.get_ident(), args[2]))
        return real_decide(*args)

    runtime._runtime_overshoot_decide = decide
    return decides


def test_w11_a_replacement_worker_in_the_same_settle_wait_decides_once(make: Any) -> None:
    """Review fix (round 2): the token goes stale while worker A is in the
    settle wait and the observer's ensure starts worker B on the same
    candidate; exactly one decide, one PF-contract probe and one record."""

    harness = _overshoot_harness(make)
    runtime = harness.runtime
    arrivals = _gated_settle(runtime, 2)
    decides = _decide_spy(runtime)
    before = _tier1_probe_count(harness)
    episode = (int(runtime._runtime_route_snapshot.route_epoch), "TIER1-SELECT", "TIER2-AUTO")
    pending = ("TIER1-SELECT", "TIER2-AUTO", episode, time.monotonic(), False)
    runtime._runtime_overshoot_pending = pending
    assert runtime._runtime_overshoot_ensure_worker() is True
    a = runtime._runtime_overshoot_thread
    runtime._runtime_overshoot_event.set()
    deadline = time.monotonic() + 3.0
    while not arrivals and time.monotonic() < deadline:
        time.sleep(0.01)
    assert arrivals == [a.ident]  # A is inside the settle wait
    _bump_facility_epoch(runtime)
    assert runtime._runtime_overshoot_ensure_worker() is True  # observer context refresh
    b = runtime._runtime_overshoot_thread
    assert b is not a
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        probes, records = _overshoot_effects(harness, episode[0], before)
        if records:
            break
        time.sleep(0.05)
    time.sleep(0.5)
    probes, records = _overshoot_effects(harness, episode[0], before)
    mine = [ident for ident, ep in decides if ep == episode]
    assert len(arrivals) == 2 and mine == [b.ident]
    assert probes == 1 and len(records) == 1 and "state=probed" in records[0]
    assert runtime._runtime_overshoot_handled is pending


def test_w11_two_concurrent_evaluations_of_one_candidate_decide_once(make: Any) -> None:
    """The handled claim is atomic: two evaluators of the same candidate past
    the settle wait together yield one decide and one record."""

    harness = _overshoot_harness(make)
    runtime = harness.runtime
    _gated_settle(runtime, 2)
    decides = _decide_spy(runtime)
    before = _tier1_probe_count(harness)
    episode = (int(runtime._runtime_route_snapshot.route_epoch), "TIER1-SELECT", "TIER2-AUTO")
    pending = ("TIER1-SELECT", "TIER2-AUTO", episode, time.monotonic(), False)
    runtime._runtime_overshoot_pending = pending
    threads = [
        threading.Thread(target=runtime._runtime_overshoot_evaluate, args=(pending, runtime.stop_event))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10.0)
        assert not thread.is_alive()
    probes, records = _overshoot_effects(harness, episode[0], before)
    assert len([1 for _ident, ep in decides if ep == episode]) == 1
    assert probes == 1 and len(records) == 1


def test_w11_a_candidate_written_during_evaluation_is_kept(make: Any) -> None:
    """The worker marks its own candidate handled and never writes the pending
    slot, so a detection during the decision is not overwritten."""

    runtime = _overshoot_harness(make).runtime
    pending = _candidate()
    newer = ("TIER1-SELECT", "TIER2-AUTO", (2, "TIER1-SELECT", "TIER2-AUTO"), time.monotonic(), False)
    runtime._runtime_overshoot_pending = pending
    runtime._runtime_overshoot_wait = lambda _seconds, _stop: False
    checks: list[int] = []

    def in_flight() -> bool:
        checks.append(1)
        if len(checks) == 2:  # the re-check right after the identity check
            runtime._runtime_overshoot_pending = newer
        return False

    runtime._route_lease_ladder_in_flight = in_flight
    runtime._runtime_overshoot_decide = lambda *_a: ("skipped", "cap", "-", None, "-")
    runtime._runtime_overshoot_evaluate(pending, runtime.stop_event)
    assert runtime._runtime_overshoot_pending is newer
    assert runtime._runtime_overshoot_handled is pending


def test_w11_a_ladder_started_after_detection_still_gets_the_settle(make: Any) -> None:
    """Spec §6.2 step 1: sleep until max(t, ladder_end_t) + 1.0 s also when the
    ladder was not yet in flight at detection."""

    runtime = _overshoot_harness(make).runtime
    pending = ("TIER1-SELECT", "TIER2-AUTO", (1, "TIER1-SELECT", "TIER2-AUTO"), time.monotonic() - 30.0, False)
    runtime._runtime_overshoot_pending = pending
    runtime._runtime_overshoot_ladder_end_t = time.monotonic()
    waits: list[float] = []

    def wait(seconds: float, _stop: Any) -> bool:
        waits.append(seconds)
        return False

    runtime._runtime_overshoot_wait = wait
    runtime._runtime_overshoot_decide = lambda *_a: ("skipped", "cap", "-", None, "-")
    runtime._runtime_overshoot_evaluate(pending, runtime.stop_event)
    assert waits and 0.8 < waits[0] <= 1.0
    # an old ladder_end (before t) changes nothing
    waits.clear()
    runtime._runtime_overshoot_ladder_end_t = time.monotonic() - 60.0
    runtime._runtime_overshoot_evaluate(pending, runtime.stop_event)
    assert waits and waits[0] < -28.0


def test_w11_now_after_is_dash_without_a_publication(make: Any) -> None:
    harness = _overshoot_harness(make)
    runtime = harness.runtime
    runtime._runtime_overshoot_wait = lambda _seconds, _stop: True  # no publication before the loop ends
    result = _decide(harness)
    assert result[0] == "probed" and result[2] == "pass" and result[4] == "-"


def test_w11_now_after_reads_the_next_publication(make: Any) -> None:
    harness = _overshoot_harness(make)
    runtime = harness.runtime

    def wait(_seconds: float, _stop: Any) -> bool:
        assert runtime._refresh_runtime_route_snapshot(timeout_seconds=1.0) is not None
        return False

    runtime._runtime_overshoot_wait = wait
    result = _decide(harness)
    assert result[0] == "probed" and result[2] == "pass" and result[4] == "T1S"


# ---------------------------------------------------------------------------
# W12: ladder view line


VIEW = re.compile(
    r"^Runtime ladder view \(pf_now=[A-Z0-9]+ pf_fixed=(?:[A-Z0-9]+|none) path=[A-Z0-9,]+ "
    r"leaf=(?:[0-9a-f]{12}|none) core_generation=\d+ route_epoch=\d+\)$"
)


def test_w12_view_line_format_and_absent_with_the_key_off(make: Any) -> None:
    on = make("t1")
    lines = messages(on, "Runtime ladder view")
    assert lines and all(VIEW.match(line) for line in lines)
    assert "path=FE,PF,T1S,T1,T1XS,T1X,L" in lines[-1]
    off = make("t1", ladder=False)
    assert messages(off, "Runtime ladder view") == []
    # The cross-check against the gate runner's markers needs its gatelib,
    # which is local evidence under .build/ (never published).
    gates = ROOT / ".build" / "claude-fallback" / "gates"
    if not (gates / "gatelib.py").is_file():
        pytest.skip("gatelib lives under .build/ (local evidence only); format checks above passed")
    sys.path.insert(0, str(gates))
    try:
        import gatelib
    finally:
        sys.path.pop(0)
    for line in lines:
        assert not line.startswith((gatelib.START_TEXT, gatelib.STOP_TEXT) + tuple(gatelib.READY_TEXTS))
        assert not gatelib.RELOAD_INTENT.search(line) and not gatelib.RELOAD_APPLY.search(line)
        assert not gatelib.POOL_COMMIT.search(line)
        assert not line.startswith(("Runtime route view", "Runtime route lease"))


# ---------------------------------------------------------------------------
# W13: API spy over full ladders (end to end through the judge service)

ALLOWED_CALLS = {"proxies", "proxy", "provider", "delay", "connections", "close"}


def test_w13_single_rung_ladder_reads_and_probes_only(make: Any) -> None:
    harness = make("t2")
    harness.controller.modes["t2a"] = "zombie"
    harness.trigger("t2a")
    assert harness.quiesce(20.0)
    end = observer_lines(harness, "ladder_end")
    assert end and "outcome=convicted" in end[-1] and "commit=accepted" in end[-1]
    calls = call_kinds(harness)
    assert {call[0] for call in calls} <= ALLOWED_CALLS
    assert all(call[1] == "TIER3B-RUNTIME" for call in calls if call[0] == "provider")
    assert harness.controller.writes == []
    assert harness.controller.now("TIER2-AUTO") == "t2b"


def test_w13_escalation_to_pf_moves_onto_the_passed_witness(make: Any) -> None:
    harness = make("t2pair")
    harness.controller.modes.update({"t2a": "zombie", "t2b": "zombie"})
    harness.trigger("t2a")
    assert harness.quiesce(25.0)
    witness = observer_lines(harness, "ladder_witness")
    assert witness and "class=pass" in witness[0]
    commit = observer_lines(harness, "rung_commit")
    assert commit and "accepted=true" in commit[-1] and "proven=1" in commit[-1]
    assert harness.controller.now("PROXY-FALLBACK") == "TIER3-AUTO"
    calls = call_kinds(harness)
    assert {call[0] for call in calls} <= ALLOWED_CALLS
    # The PF rung's member state came from full refreshes, never a group read.
    assert ["proxy", "TIER3-AUTO"] not in calls and ["proxy", "PROXY-FALLBACK"] not in calls
    assert harness.controller.writes == []
    assert not observer_lines(harness, "probe_refused")


# ---------------------------------------------------------------------------
# W14: lease interplay (Track H's own test is in test_runtime_route_lease_wiring)


def test_w14_ladder_end_wakes_the_lease_watcher(make: Any) -> None:
    harness = make("t2")
    runtime = harness.runtime
    wakes: list[str] = []
    runtime._route_lease_wake = lambda reason="": wakes.append(reason)
    runtime._runtime_post_dial_ladder_end(SimpleNamespace(), "convicted")
    assert wakes == ["ladder_end"]


def test_w14_track_h_reason_only_with_the_key_on(make: Any) -> None:
    on = make("t2")
    on.runtime._runtime_post_dial_judge()._ladder_job = object()
    assert on.runtime._route_lease_busy(None).hard == "post_dial_ladder"
    on.runtime._runtime_post_dial_judge()._ladder_job = None
    assert on.runtime._route_lease_busy(None).hard == ""
    off = make("t2", ladder=False)
    off.runtime._runtime_post_dial_judge()._ladder_job = object()
    assert off.runtime._route_lease_busy(None).hard == ""
    off.runtime._runtime_post_dial_judge()._ladder_job = None


# ---------------------------------------------------------------------------
# W15: config warning


def test_w15_config_warning_once_per_observer_for_overshoot_alone(make: Any) -> None:
    alone = make("t3", ladder=False, overshoot_repair=True)
    assert observer_lines(alone, "config_warning") == [
        "Post-dial observer config_warning (key=overshoot_repair reason=requires_tier_ladder_judge)"
    ]
    for flags in ({}, {"overshoot_repair": False, "tier_ladder_judge": False}):
        harness = make("t3", ladder=False, **flags)
        assert observer_lines(harness, "config_warning") == []
    both = make("t3", overshoot_repair=True)
    assert observer_lines(both, "config_warning") == []


# ---------------------------------------------------------------------------
# W16 / W18: the probe hook


def _hook_spies(runtime: Any) -> tuple[list[Any], list[Any]]:
    broker: list[Any] = []
    delay: list[Any] = []

    def run_brokered(callback: Any, **kwargs: Any) -> Any:
        broker.append(kwargs)
        return callback(None)

    def leased(name: str, urls: list[str], **kwargs: Any) -> Any:
        delay.append((name, list(urls), kwargs.get("expected_status"), kwargs.get("timeout_ms")))
        return {"delay": 1}

    runtime._run_brokered_runtime_probe = run_brokered
    runtime._probe_proxy_delay_with_urls_leased = leased
    return broker, delay


def test_w18_ladder_probe_fields_reach_the_leased_probe_and_the_broker(make: Any) -> None:
    harness = make("t2")
    runtime = harness.runtime
    job, _reason = admit(harness, "t2a")
    broker, delay = _hook_spies(runtime)
    action = StartProbe("W0", "TIER3-AUTO", 5000, 5.65, url="http://pf.test/x", expected="200",
                        group="PROXY-FALLBACK", admission_s=1.9, await_s=7.0)
    outcome = runtime._runtime_post_dial_contract_probe(job, action)
    assert outcome.probe is ProbeClass.PASS
    assert delay == [("TIER3-AUTO", ["http://pf.test/x"], "200", 5000)]
    assert broker[0]["queue_timeout_seconds"] == 1.9
    refused = runtime._runtime_post_dial_contract_probe(
        job, StartProbe("W1", "x", 800, 1.45, url=URL, expected="204", group="TIER1-AUTO", admission_s=1.9)
    )
    assert refused.probe is ProbeClass.INDETERMINATE and len(delay) == 1
    assert observer_lines(harness, "probe_refused")[-1].endswith("name=W1 reason=off_path)")


def test_w18_legacy_probe_uses_the_job_contract_and_1_5_s(make: Any) -> None:
    harness = make("t3", ladder=False)
    runtime = harness.runtime
    job, _reason = admit(harness, "n1")
    broker, delay = _hook_spies(runtime)
    runtime._runtime_post_dial_contract_probe(job, StartProbe("A_C", "n1", 5000, 5.65))
    assert delay == [("n1", [job.test_url], job.expected_status, 5000)]
    assert broker[0]["queue_timeout_seconds"] == 1.5


def test_w16_judge_probes_go_through_the_recording_delay_call(make: Any) -> None:
    harness = make("t2")
    harness.controller.modes["t2a"] = "zombie"
    harness.trigger("t2a")
    assert harness.quiesce(20.0)
    probes = [call for call in harness.controller.delay_calls if call["name"] in {"t2a", "t2b"}]
    # ``MihomoAPIClient.proxy_delay`` records the app delay windows that
    # lease early release ignores (tests/test_mihomo_api_delay_windows.py).
    assert probes and all(call["url"] == URL and call["timeout_ms"] == 800 for call in probes)
