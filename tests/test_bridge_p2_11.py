"""T-P2-11 B-26(1)(2): deferred apply intent and lifecycle serialization.

Substitutes only: a DesktopRuntime subclass with fake Core entries, the real
JobExecutor mutation lock, no Mihomo, no network, no registry, no live profile.
"""

from copy import deepcopy
import json
import logging
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

import bridge_commands as commands
import bridge_protocol as protocol
import bridge_settings_apply as bsa
import runtime_settings_apply as rsa
from bridge_jobs import JobEventRecorder, JobExecutor
from desktop_runtime import DesktopRuntime
from runtime_reload_gate import RuntimeReloadReason
from settings_effects import plan


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "bridge_schema/fixtures/v1/valid"
GRAPH = RuntimeReloadReason.USER_GRAPH_CONFIG.value
DNS = RuntimeReloadReason.DNS_LIFECYCLE.value
NET0 = {"network": {"network_mode": "system_proxy", "allow_lan": False}}
NET1 = {"network": {"network_mode": "tun", "allow_lan": False}}


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.005)
    return predicate()


def assert_pending_dto(dto):
    errors = []
    protocol._schema(dto, "PendingApply", "pending", errors)
    assert errors == []
    frame = json.loads((FIXTURES / "topic_config.changed.json").read_bytes())
    frame["data"]["pending"] = deepcopy(dto)
    assert protocol.validate_frame(frame) == []


class Runner:
    def __init__(self):
        self.running = False
        self.cleanup_pending = False

    def lightweight_fact(self):
        return {"running": self.running, "pid": 100 if self.running else None,
                "process_generation": 1, "cleanup_pending": self.cleanup_pending}

    def is_running(self):
        return self.running


class LatencyManager:
    """Manager stand-in for the real DesktopRuntime.recheck_latency_policy."""

    def __init__(self, events):
        self.events = events
        self.fail = None  # scope whose check raises

    def _check(self, scope):
        self.events.append(("check", scope))
        if self.fail == scope:
            raise RuntimeError("probe failed")
        return []

    def check_exit(self, force_remote_refresh=False):
        return self._check("exit")

    def check_tier1(self):
        return self._check("tier1")

    def check_tier2(self, force_remote_refresh=False):
        return self._check("tier2")

    def evaluate_state(self):
        self.events.append("evaluate_state")


class StubRuntime(DesktopRuntime):
    """Real B-26 methods; fake Core entries that honor the real Core locks."""

    def __init__(self):  # noqa: D401 - deliberately not DesktopRuntime.__init__
        self._runtime_intent_lock = threading.RLock()
        self._core_transition_lock = threading.RLock()
        self._core_lifecycle_lock = threading.Lock()
        self._runtime_generation = 1
        self._stop_request_pending = False
        self._desired_running = True
        self._core_generation = 0
        self.core_runner = Runner()
        self.logger = logging.getLogger("test.p2_11")
        self.phase = "collecting"
        self.failure = None
        self.events = []
        self.restart_ok = True
        self.reload_results = []
        self.recheck = None
        # T-P2-RT2: the real DesktopRuntime.recheck_latency_policy runs against
        # this manager stand-in (no Mihomo; its Core reload is the fake below).
        self.manager = LatencyManager(self.events)

    # Lifecycle facts in the public lifecycle_status() shape.
    def lifecycle_status(self):
        with self._runtime_intent_lock:
            return {"intent": {"runtime_generation": self._runtime_generation,
                               "stop_requested": self._stop_request_pending,
                               "desired_running": self._desired_running},
                    "runtime": {"startup_phase": self.phase, "failure_code": self.failure}}

    def launch_core(self, label):
        """What _start_core_once does: render+launch, then bump generation."""
        self.events.append(label)
        self.core_runner.running = True
        with self._core_lifecycle_lock:
            self._core_generation += 1

    def restart_core(self):
        with self._core_transition_lock:
            self.events.append("restart_core")
            if self._stop_request_pending:
                self.core_runner.running = False
                return {"stopped": True}
            if self.restart_ok:
                self.launch_core("restart_launch")
            return {"running": self.restart_ok}

    def reload_core_config(self, *, mode="strict", reason=None, user_commit_id=""):
        with self._core_transition_lock:
            self.events.append(("reload", mode, reason, user_commit_id))
            if self.reload_results:
                return self.reload_results.pop(0)
            return {"reloaded": True, "mode": mode}

    def refresh_dns_resolver_policy(self, reason="settings_changed"):
        self.events.append(("dns_refresh", reason))
        return {}

    def request_stop_fake(self):
        with self._runtime_intent_lock:
            self._stop_request_pending = True
            self._desired_running = False
        self.phase = "stopping"


@pytest.fixture
def rt():
    return StubRuntime()


def deferred(before, after):
    return plan(before, after, running=False, phase="collecting")


def make_ready(runtime, *, running=True):
    runtime.phase = "running"
    runtime.core_runner.running = running


# --- plan extraction and ledger -------------------------------------------

def test_plan_extraction_keeps_reasons_and_deferred_actions():
    core, actions = rsa.core_request_from_plan(deferred(NET0, NET1))
    assert core.effect == "restart" and actions == frozenset()
    core, actions = rsa.core_request_from_plan(deferred({"routing": {"a": 1}}, {"routing": {"a": 2}}))
    assert (core.effect, core.reason, core.mode) == ("reload", GRAPH, "strict")
    core, actions = rsa.core_request_from_plan(deferred({"dns": {"m": 1}}, {"dns": {"m": 2}}))
    assert (core.effect, core.reason, core.mode, core.fallback_effect) == (
        "reload", DNS, "incremental_export", "restart")
    assert actions == {"refresh_dns_policy"}
    core, actions = rsa.core_request_from_plan(deferred({"latency": {"x": 1}}, {"latency": {"x": 2}}))
    assert core.effect == "recheck" and actions == {"latency_recheck"}
    # A live plan or an independent-only plan has no deferred Core work.
    assert rsa.core_request_from_plan(deferred({"ui": {"l": 1}}, {"ui": {"l": 2}}))[0] is None


def live(before, after):
    return plan(before, after, running=True, phase="running")


def test_live_plans_carry_no_core_work():
    # The save orchestrator already ran these; they must not become intents.
    for before, after in ((NET0, NET1), ({"routing": {"r": 1}}, {"routing": {"r": 2}}),
                          ({"latency": {"x": 1}}, {"latency": {"x": 2}}),
                          ({"dns": {"m": 1}}, {"dns": {"m": 2}})):
        assert live(before, after).core.effect in {"restart", "reload", "recheck"}
        assert rsa.core_request_from_plan(live(before, after)) == (None, frozenset())
        assert rsa.core_request_from_plan(live(before, after).core) == (None, frozenset())


def facts(generation=1, **kw):
    return rsa.ApplyFacts(generation=generation, stop_requested=kw.pop("stop", False), **kw)


def test_merge_same_rev_in_place_and_newer_rev_supersedes_with_successor():
    ledger = rsa.SettingsApplyLedger()
    dns_core, dns_actions = rsa.core_request_from_plan(deferred({"dns": {"m": 1}}, {"dns": {"m": 2}}))
    first = ledger.request(dns_core, dns_actions, config_rev=3, user_commit_id="c1",
                           facts=facts(), core_floor=0)
    graph = rsa.CoreSpec("reload", GRAPH, "strict")
    same = ledger.request(graph, (), config_rev=3, user_commit_id="c2", facts=facts(), core_floor=0)
    assert same.intent_id == first.intent_id and same.core == graph
    assert same.actions == {"refresh_dns_policy"} and same.user_commit_id == "c2"
    restart = rsa.CoreSpec("restart")
    newer = ledger.request(restart, {"latency_recheck"}, config_rev=4, user_commit_id="c3",
                           facts=facts(), core_floor=None)
    old = ledger.get(first.intent_id)
    assert old.state == "superseded" and old.successor == newer.intent_id
    assert old.last_error["reason_code"] == "config_rev"
    assert (newer.state, newer.config_rev, newer.effect, newer.user_commit_id) == (
        "pending", 4, "restart", "c3")
    assert newer.actions == {"refresh_dns_policy", "latency_recheck"}
    # Lighter later work never downgrades the merged effect.
    lighter = ledger.request(rsa.CoreSpec("recheck"), {"latency_recheck"}, config_rev=5,
                             user_commit_id="c4", facts=facts(), core_floor=0)
    assert lighter.effect == "restart" and ledger.current().intent_id == lighter.intent_id
    live = [r for r in ledger._records.values() if r.state not in rsa.TERMINAL]
    assert len(live) == 1
    # A late request for an older revision is recorded as superseded only.
    stale = ledger.request(graph, (), config_rev=2, user_commit_id="late", facts=facts(), core_floor=0)
    assert stale.state == "superseded" and ledger.current().intent_id == lighter.intent_id
    for record in ledger._records.values():
        assert_pending_dto(record.dto())


def test_reconcile_states_and_validation():
    for observed, state, reason in (
            (facts(stop=True), "cancelled", "stopped"),
            (facts(phase="stopped"), "cancelled", "stopped"),
            (facts(generation=2), "superseded", "generation"),
            (facts(phase="failed", failure_code="needs_route"), "failed", "start_failed"),
            (facts(phase="failed", failure_code="free text"), "failed", "start_failed")):
        ledger = rsa.SettingsApplyLedger()
        ledger.request(rsa.CoreSpec("restart"), (), config_rev=1, user_commit_id="c",
                       facts=facts(), core_floor=0)
        intent = ledger.reconcile(observed)
        assert intent.state == state and intent.last_error["reason_code"] == reason
        assert_pending_dto(intent.dto())
    assert intent.last_error["code"] == "runtime_unavailable"
    with pytest.raises(ValueError):
        rsa.SettingsApplyLedger().request(rsa.CoreSpec("restart"), (), config_rev=1,
                                          user_commit_id="", facts=facts(), core_floor=0)
    with pytest.raises(ValueError):
        rsa.SettingsApplyLedger().request(rsa.CoreSpec("restart"), (), config_rev=True,
                                          user_commit_id="c", facts=facts(), core_floor=0)


def test_save_result_with_pending_passes_settings_apply_adapter():
    dto = rsa.Intent("apply-1", 1, rsa.CoreSpec("restart"), "c", 1, frozenset()).dto()
    raw = {"rev": 1, "sections": [{"section": "network", "saved": True, "applied": False,
                                   "effect": "deferred", "error": None}], "pending": dto}
    ok, result, _ = commands.adapt_result("settings.apply", raw)
    assert ok and result["pending"] == dto


# --- runtime entry points (real DesktopRuntime methods) ------------------

def test_saved_during_startup_is_applied_after_ready(rt):
    rt.phase = "core_standby"
    rt.launch_core("bootstrap_core")  # Core already up before the save
    dto = rt.request_settings_apply(deferred(NET0, NET1), 7, "commit-7")
    assert dto["state"] == "pending" and dto["effect"] == "restart" and dto["generation"] == 1
    assert_pending_dto(dto)
    # Not ready yet: one attempt changes nothing and runs no Core entry.
    assert rt.apply_settings_intent(dto["intent_id"])["state"] == "pending"
    assert "restart_core" not in rt.events
    make_ready(rt)
    result = rt.apply_settings_intent(dto["intent_id"], config_rev=7)
    assert result["state"] == "applied" and result["error"] is None
    assert rt.events.count("restart_core") == 1
    assert rt.bridge_config_pending()["state"] == "applied"
    # Terminal intents are never applied twice.
    assert rt.apply_settings_intent(dto["intent_id"])["state"] == "applied"
    assert rt.events.count("restart_core") == 1


def test_graph_and_dns_reloads_reuse_existing_entries_with_reason_codes(rt):
    rt.phase = "core_standby"
    rt.launch_core("bootstrap_core")
    dto = rt.request_settings_apply(deferred({"routing": {"r": 1}}, {"routing": {"r": 2}}), 1, "commit-g")
    make_ready(rt)
    assert rt.apply_settings_intent(dto["intent_id"])["state"] == "applied"
    assert rt.events[-1] == ("reload", "strict", GRAPH, "commit-g")

    other = StubRuntime()
    other.phase = "core_standby"
    other.launch_core("bootstrap_core")
    other.reload_results = [{"reloaded": False, "reason": "automatic_route_lease_is_active"}]
    dto = other.request_settings_apply(deferred({"dns": {"m": 1}}, {"dns": {"m": 2}}), 1, "commit-d")
    make_ready(other)
    assert other.apply_settings_intent(dto["intent_id"])["state"] == "applied"
    # Qt order: refresh policy, incremental DNS reload, restart fallback.
    assert other.events[1:] == [("dns_refresh", "settings_changed"),
                                ("reload", "incremental_export", DNS, ""),
                                "restart_core", "restart_launch"]


def test_reload_failure_without_fallback_is_failed_and_keeps_saved_config(rt):
    rt.phase = "core_standby"
    rt.launch_core("bootstrap_core")
    rt.reload_results = [{"reloaded": False, "reload_blocked": True,
                          "reason": "explicit_reload_reason_required"}]
    dto = rt.request_settings_apply(deferred({"routing": {"r": 1}}, {"routing": {"r": 2}}), 1, "c")
    make_ready(rt)
    result = rt.apply_settings_intent(dto["intent_id"])
    assert result["state"] == "failed"
    assert result["error"]["code"] == "io"
    assert result["error"]["reason_code"] == "explicit_reload_reason_required"
    assert_pending_dto(result)


def latency_intent(runtime, rev=1):
    runtime.phase = "core_standby"
    runtime.launch_core("bootstrap_core")
    dto = runtime.request_settings_apply(
        deferred({"latency": {"x": 1}}, {"latency": {"x": 2}}), rev, "c")
    assert dto["effect"] == "recheck"
    make_ready(runtime)
    return dto


def test_deferred_latency_recheck_runs_the_real_runtime_entry(rt):
    # T-P2-RT2 (决定_第二波裁定 P2-10 #8): the real recheck_latency_policy --
    # exit/tier1/tier2 checks, evaluate_state, then the graph reload.
    dto = latency_intent(rt)
    result = rt.apply_settings_intent(dto["intent_id"])
    assert result["state"] == "applied" and result["error"] is None
    checks = [e for e in rt.events if isinstance(e, tuple) and e[0] in {"check", "reload"}]
    assert [e[:2] for e in checks] == [("check", "exit"), ("check", "tier1"),
                                       ("check", "tier2"), ("reload", "incremental_export")]
    assert checks[-1][2] == GRAPH and "evaluate_state" in rt.events
    assert_pending_dto(result)


def test_deferred_latency_recheck_failure_of_the_real_entry_is_failed(rt):
    dto = latency_intent(rt)
    rt.manager.fail = "tier1"
    result = rt.apply_settings_intent(dto["intent_id"])
    assert result["state"] == "failed"
    assert result["error"]["code"] == "io"
    assert result["error"]["reason_code"] == "latency_recheck_failed"
    assert_pending_dto(result)


def test_latency_recheck_unavailable_only_without_the_runtime_entry():
    other = StubRuntime()
    other.recheck_latency_policy = None  # a stand-in that removed the entry
    dto = latency_intent(other)
    result = other.apply_settings_intent(dto["intent_id"])
    assert result["state"] == "failed"
    assert result["error"]["reason_code"] == "latency_recheck_unavailable"
    assert not any(isinstance(e, tuple) and e[0] == "reload" for e in other.events)


def test_stop_during_startup_cancels_without_core_work(rt):
    dto = rt.request_settings_apply(deferred(NET0, NET1), 1, "c")
    rt.request_stop_fake()
    pending = rt.bridge_config_pending()
    assert pending["state"] == "cancelled" and pending["error"]["reason_code"] == "stopped"
    assert rt.apply_settings_intent(dto["intent_id"])["state"] == "cancelled"
    assert "restart_core" not in rt.events


def test_startup_failure_marks_failed(rt):
    dto = rt.request_settings_apply(deferred(NET0, NET1), 1, "c")
    rt.phase, rt.failure = "failed", "source_supply_no_egress"
    result = rt.apply_settings_intent(dto["intent_id"])
    assert result["state"] == "failed"
    assert result["error"] == {"code": "source_supply_no_egress",
                               "message_key": "errors.settings.apply_start_failed",
                               "params": {}, "reason_code": "start_failed"}
    assert "restart_core" not in rt.events


def test_newer_config_revision_supersedes_old_intent_explicitly(rt):
    rt.phase = "core_standby"
    rt.launch_core("bootstrap_core")
    dto = rt.request_settings_apply(deferred(NET0, NET1), 3, "c")
    make_ready(rt)
    result = rt.apply_settings_intent(dto["intent_id"], config_rev=5)
    assert result["intent_id"] != dto["intent_id"] and result["config_rev"] == 5
    assert result["state"] == "applied" and rt.events.count("restart_core") == 1
    old = rt._settings_apply_ledger_instance.get(dto["intent_id"])
    assert old.state == "superseded" and old.successor == result["intent_id"]


def test_cross_generation_never_runs_the_old_intent(rt):
    dto = rt.request_settings_apply(deferred(NET0, NET1), 1, "c")
    with rt._runtime_intent_lock:
        rt._runtime_generation = 2  # stopped and started again
    make_ready(rt)
    result = rt.apply_settings_intent(dto["intent_id"])
    assert result["state"] == "superseded" and result["error"]["reason_code"] == "generation"
    assert "restart_core" not in rt.events


def test_multiple_startup_saves_merge_into_one_intent(rt):
    first = rt.request_settings_apply(deferred({"routing": {"r": 1}}, {"routing": {"r": 2}}), 1, "c1")
    second = rt.request_settings_apply(deferred(NET0, NET1), 2, "c2")
    third = rt.request_settings_apply(deferred({"dns": {"m": 1}}, {"dns": {"m": 2}}), 3, "c3")
    assert third["effect"] == "restart" and third["config_rev"] == 3
    ledger = rt._settings_apply_ledger_instance
    assert ledger.get(first["intent_id"]).state == "superseded"
    assert ledger.get(second["intent_id"]).state == "superseded"
    assert rt.bridge_config_pending()["intent_id"] == third["intent_id"]


def test_core_launched_after_the_save_covers_the_restart(rt):
    # Saved while collecting; the startup Core launches afterwards and loads
    # the saved file, so ready does not restart it a second time.
    dto = rt.request_settings_apply(deferred(NET0, NET1), 1, "c")
    with rt._core_transition_lock:
        rt.launch_core("startup_core")
    make_ready(rt)
    result = rt.apply_settings_intent(dto["intent_id"])
    assert result["state"] == "applied" and "restart_core" not in rt.events
    assert rt._settings_apply_ledger_instance.get(dto["intent_id"]).covered


def internal_restart(rt, entered, release, label="recovery"):
    def run():
        with rt._core_transition_lock:  # _start_core_for_runtime_recovery
            rt.events.append(label + "_begin")
            entered.set()
            release.wait(3)
            rt.launch_core(label + "_launch")
            rt.events.append(label + "_end")
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert entered.wait(3)
    return thread


def test_internal_recovery_restart_after_save_is_not_repeated(rt):
    rt.phase = "core_standby"
    rt.launch_core("bootstrap_core")
    dto = rt.request_settings_apply(deferred(NET0, NET1), 1, "c")
    assert rt._settings_apply_ledger_instance.current().core_floor == 1
    make_ready(rt)
    entered, release = threading.Event(), threading.Event()
    recovery = internal_restart(rt, entered, release)
    results = []
    applier = threading.Thread(target=lambda: results.append(
        rt.apply_settings_intent(dto["intent_id"])), daemon=True)
    applier.start()
    time.sleep(.1)
    assert not results  # the intent waits for the in-flight internal restart
    release.set()
    recovery.join(3)
    applier.join(3)
    assert results[0]["state"] == "applied"
    assert "restart_core" not in rt.events  # no duplicate restart
    assert rt.events[-2:] == ["recovery_launch", "recovery_end"]


def test_internal_restart_in_flight_at_save_is_followed_by_one_ordered_apply(rt):
    rt.phase = "core_standby"
    rt.launch_core("bootstrap_core")
    entered, release = threading.Event(), threading.Event()
    recovery = internal_restart(rt, entered, release, "failback")
    # The in-flight restart may have rendered the old file: no floor yet.
    dto = rt.request_settings_apply(deferred(NET0, NET1), 1, "c")
    assert rt._settings_apply_ledger_instance.current().core_floor is None
    make_ready(rt)
    results = []
    applier = threading.Thread(target=lambda: results.append(
        rt.apply_settings_intent(dto["intent_id"])), daemon=True)
    applier.start()
    time.sleep(.1)
    assert not results
    release.set()
    recovery.join(3)
    applier.join(3)
    assert results[0]["state"] == "applied"
    order = [e for e in rt.events if isinstance(e, str)]
    assert order.index("failback_end") < order.index("restart_core")
    assert order.count("restart_core") == 1


def test_stop_intent_wins_over_an_apply_waiting_for_internal_restart(rt):
    rt.phase = "core_standby"
    rt.launch_core("bootstrap_core")
    dto = rt.request_settings_apply(deferred(NET0, NET1), 1, "c")
    make_ready(rt)
    entered, release = threading.Event(), threading.Event()
    recovery = internal_restart(rt, entered, release)
    results = []
    applier = threading.Thread(target=lambda: results.append(
        rt.apply_settings_intent(dto["intent_id"])), daemon=True)
    applier.start()
    time.sleep(.05)
    rt.request_stop_fake()  # control-domain stop intent while apply is queued
    release.set()
    recovery.join(3)
    applier.join(3)
    assert results[0]["state"] == "cancelled"
    assert "restart_core" not in rt.events


def test_stop_during_restart_reports_cancelled_not_failed(rt):
    rt.phase = "core_standby"
    rt.launch_core("bootstrap_core")
    dto = rt.request_settings_apply(deferred(NET0, NET1), 1, "c")
    make_ready(rt)
    original = rt.restart_core

    def restart_with_stop():
        rt.request_stop_fake()
        return original()
    rt.restart_core = restart_with_stop
    result = rt.apply_settings_intent(dto["intent_id"])
    assert result["state"] == "cancelled" and result["error"]["code"] == "cancelled"


def test_pending_getter_has_no_core_side_effects(rt):
    assert rt.bridge_config_pending() is None
    dto = rt.request_settings_apply(deferred(NET0, NET1), 1, "c")
    assert rt.bridge_config_pending() == dto
    assert rt.events == []


def test_live_save_with_pending_intent_never_duplicates_the_core_work(rt):
    # Review A MUST-1: a live plan was already executed by the save
    # orchestrator; reporting it only advances the live intent's revision.
    assert StubRuntime().request_settings_apply(live(NET0, NET1), 1, "c") is None
    rt.phase = "core_standby"
    rt.launch_core("bootstrap_core")
    first = rt.request_settings_apply(deferred({"routing": {"r": 1}}, {"routing": {"r": 2}}), 1, "c1")
    make_ready(rt)
    rt.restart_core()  # P2-10 ran the live network restart immediately (§15)
    second = rt.request_settings_apply(live(NET0, NET1), 2, "c2")
    assert second["intent_id"] != first["intent_id"] and second["config_rev"] == 2
    assert second["effect"] == "reload"  # never a restart successor
    for latency in (live({"latency": {"x": 1}}, {"latency": {"x": 2}}),
                    live({"routing": {"r": 2}}, {"routing": {"r": 3}})):
        assert rt.request_settings_apply(latency, 2, "c2")["effect"] == "reload"
    result = rt.apply_settings_intent(second["intent_id"])
    assert result["state"] == "applied" and result["error"] is None
    # The live restart launched after the first save covers the graph reload.
    assert rt.events.count("restart_core") == 1
    assert not any(isinstance(e, tuple) and e[0] == "reload" for e in rt.events)


def test_running_phase_with_core_down_waits_and_refreshes_dns_once(rt):
    # Review B m5 / Review A SHOULD-1: phase running but the Core is not
    # usable (a recovery took it down): no Core entry, retry later, and the
    # deferred DNS policy refresh runs once per intent content.
    rt.phase = "core_standby"
    rt.launch_core("bootstrap_core")
    dto = rt.request_settings_apply(deferred({"dns": {"m": 1}}, {"dns": {"m": 2}}), 1, "c")
    make_ready(rt)
    rt.core_runner.cleanup_pending = True
    assert rt.apply_settings_intent(dto["intent_id"])["state"] == "pending"
    assert rt.events == ["bootstrap_core"]  # no refresh, reload or restart
    rt.core_runner.cleanup_pending = False
    original = rt.refresh_dns_resolver_policy

    def refresh_then_core_drops(reason="settings_changed"):
        result = original(reason)
        rt.core_runner.cleanup_pending = True  # Core went down after the claim
        return result
    rt.refresh_dns_resolver_policy = refresh_then_core_drops
    assert rt.apply_settings_intent(dto["intent_id"])["state"] == "pending"
    rt.core_runner.cleanup_pending = False
    assert rt.apply_settings_intent(dto["intent_id"])["state"] == "applied"
    assert rt.events.count(("dns_refresh", "settings_changed")) == 1
    assert rt.events[-1] == ("reload", "incremental_export", DNS, "")


def test_failed_phase_after_running_keeps_waiting_for_recovery(rt):
    # Review B SHOULD-3: after this generation reached running, ``failed`` is
    # a recovery window (Core down + last_error), not a start failure.
    rt.phase = "core_standby"
    rt.launch_core("bootstrap_core")
    dto = rt.request_settings_apply(deferred(NET0, NET1), 1, "c")
    make_ready(rt, running=False)
    assert rt.apply_settings_intent(dto["intent_id"])["state"] == "pending"
    rt.phase, rt.failure = "failed", "io"
    assert rt.bridge_config_pending()["state"] == "pending"
    assert rt.apply_settings_intent(dto["intent_id"])["state"] == "pending"
    with rt._core_transition_lock:
        rt.launch_core("recovery_launch")  # recovery loads the saved file
    rt.phase, rt.failure = "running", None
    result = rt.apply_settings_intent(dto["intent_id"])
    assert result["state"] == "applied" and "restart_core" not in rt.events


def test_core_applied_is_reported_when_only_the_latency_recheck_fails(rt):
    # Review A SHOULD-2: the restart succeeded; a failed latency recheck (the
    # real entry, T-P2-RT2) must not read as an unapplied Core change.
    rt.phase = "core_standby"
    rt.launch_core("bootstrap_core")
    rt.request_settings_apply(deferred({"latency": {"x": 1}}, {"latency": {"x": 2}}), 1, "c1")
    dto = rt.request_settings_apply(deferred(NET0, NET1), 2, "c2")
    assert dto["effect"] == "restart"
    make_ready(rt)
    rt.manager.fail = "exit"
    result = rt.apply_settings_intent(dto["intent_id"])
    assert result["state"] == "failed" and result["effect"] == "restart"
    assert result["error"]["reason_code"] == "latency_recheck_failed"
    assert result["error"]["params"] == {"core_applied": True}
    assert rt.events.count("restart_core") == 1
    assert_pending_dto(result)


def test_pending_getter_hides_records_of_an_older_generation(rt):
    # Review B SHOULD-7: after stop + start the old cancelled record is not
    # shown; the newer start already loaded the saved file.
    rt.request_settings_apply(deferred(NET0, NET1), 1, "c")
    rt.request_stop_fake()
    assert rt.bridge_config_pending()["state"] == "cancelled"
    with rt._runtime_intent_lock:
        rt._runtime_generation = 2
        rt._stop_request_pending = False
        rt._desired_running = True
    rt.phase = "collecting"
    assert rt.bridge_config_pending() is None


# --- bridge coordinator: mutation domain, waiter, stop priority ----------

@pytest.fixture
def executor():
    instance = JobExecutor(JobEventRecorder(8192), epoch="p2-11", require_acceptance=True)
    yield instance
    instance.shutdown(time.monotonic() + 1)


def session_for(runtime, executor):
    guard_log = []
    gate = SimpleNamespace(require_idle=lambda: guard_log.append("gate"))
    return SimpleNamespace(runtime=runtime, executor=executor, network_lock=threading.RLock(),
                           report=SimpleNamespace(gate=gate),
                           refresh_secrets=lambda: guard_log.append("secrets"),
                           record_service=lambda: guard_log.append("service")), guard_log


def count_applies(coordinator):
    """Record every apply attempt that reached the runtime, with its result."""
    calls = []
    original = coordinator.port.apply_intent

    def apply_intent(intent_id, *, config_rev):
        result = original(intent_id, config_rev=config_rev)
        calls.append(result)
        return result
    coordinator.port.apply_intent = apply_intent
    return calls


def test_waiter_does_not_hold_mutation_domain_before_ready_then_applies(rt, executor):
    session, guard_log = session_for(rt, executor)
    coordinator = bsa.register(session, interval=.02)
    assert session.settings_apply is coordinator
    seen = []
    coordinator.add_listener(seen.append)
    rt.phase = "core_standby"
    rt.launch_core("bootstrap_core")
    inside = []
    original = rt.restart_core

    def observed_restart():
        # Applied inside the mutation domain and the session network guard.
        inside.append((not executor._mutation_lock.acquire(blocking=False),
                       session.network_lock._is_owned()))
        return original()
    rt.restart_core = observed_restart
    applies = count_applies(coordinator)
    dto = coordinator.request(deferred(NET0, NET1), 1, "c")
    assert dto["state"] == "pending"
    time.sleep(.1)
    # Waiting for ready never occupies the mutation worker/lock, never enters
    # the session guard and never attempts the apply.
    assert executor._mutation_lock.acquire(timeout=.5)
    executor._mutation_lock.release()
    assert "restart_core" not in rt.events
    assert guard_log == [] and applies == []
    make_ready(rt)
    assert wait_for(lambda: coordinator.pending()["state"] == "applied")
    assert inside == [(True, True)]
    assert guard_log == ["gate", "secrets", "secrets", "service"]
    assert [r["state"] for r in applies] == ["applied"]
    assert [d["state"] for d in seen if d] == ["pending", "applied"]
    assert wait_for(lambda: coordinator._thread is None)  # waiter exits when idle
    coordinator.close()


def test_apply_is_serialized_after_a_running_external_mutation(rt, executor):
    session, _ = session_for(rt, executor)
    coordinator = bsa.register(session, interval=.02)
    rt.phase = "core_standby"
    rt.launch_core("bootstrap_core")
    started, release = threading.Event(), threading.Event()

    def external_restart(args, context):  # a page service.restart transaction
        started.set()
        release.wait(3)
        rt.events.append("external_end")
        with rt._core_lifecycle_lock:
            generation = rt._core_generation
        return {"applied": True, "core_running": True, "core_cleanup_pending": False,
                "core_generation": generation + 1}
    executor.register("service.restart", external_restart, before=lambda *_: rt._core_generation)
    ack = executor.submit("service.restart", {}, req_id="r1", owner="w1")
    assert ack.accepted and executor.acceptance_sent(ack.op, epoch=executor.epoch)
    assert started.wait(3)
    coordinator.request(deferred(NET0, NET1), 1, "c")
    make_ready(rt)
    time.sleep(.1)
    assert "restart_core" not in rt.events  # waits for the whole transaction
    release.set()
    assert wait_for(lambda: coordinator.pending()["state"] == "applied")
    order = [e for e in rt.events if isinstance(e, str)]
    assert order.index("external_end") < order.index("restart_core")
    coordinator.close()


def test_stop_priority_cancels_queued_apply_via_coordinator(rt, executor):
    session, _ = session_for(rt, executor)
    coordinator = bsa.register(session, interval=.02)
    rt.phase = "core_standby"
    rt.launch_core("bootstrap_core")
    applies = count_applies(coordinator)
    entered = threading.Event()
    original_run = coordinator.mutation.run

    def run(work, *, abandoned):
        entered.set()  # the waiter is now queued on the mutation domain
        return original_run(work, abandoned=abandoned)
    coordinator.mutation.run = run
    coordinator.request(deferred(NET0, NET1), 1, "c")
    assert executor._mutation_lock.acquire(timeout=1)  # a long mutation is running
    try:
        make_ready(rt)
        assert entered.wait(3)
        time.sleep(.05)
        rt.request_stop_fake()  # control domain, independent of the mutation lock
    finally:
        executor._mutation_lock.release()
    # The queued apply itself reaches the runtime after the lock and resolves
    # to cancelled there (not only the lazy reconcile of the pending getter).
    assert wait_for(lambda: len(applies) == 1)
    assert applies[0]["state"] == "cancelled"
    assert applies[0]["error"]["reason_code"] == "stopped"
    assert coordinator.pending()["state"] == "cancelled"
    assert "restart_core" not in rt.events
    coordinator.close()


def test_coordinator_retries_when_gate_busy_and_closes(rt, executor, caplog):
    from runtime_network_recovery import RecoveryStillRunning
    session, guard_log = session_for(rt, executor)
    attempts = []

    def busy():
        attempts.append(1)
        if len(attempts) < 3:
            raise RecoveryStillRunning("startup proxy recovery is still running")
    session.report.gate.require_idle = busy
    coordinator = bsa.register(session, interval=.02)
    rt.phase = "core_standby"
    rt.launch_core("bootstrap_core")
    with caplog.at_level(logging.WARNING, logger=bsa.LOGGER.name):
        coordinator.request(deferred(NET0, NET1), 1, "c")
        make_ready(rt)
        assert wait_for(lambda: coordinator.pending()["state"] == "applied")
    assert len(attempts) >= 3 and rt.events.count("restart_core") == 1
    # Same as BridgeSession.restart: service.state is recorded even when the
    # gate refuses; the expected RT-1 retry is not logged.
    assert guard_log.count("service") == len(attempts)
    assert not [r for r in caplog.records if r.name == bsa.LOGGER.name]
    assert coordinator.close()
    assert coordinator.request(deferred(NET0, NET1), 2, "c2")["state"] == "pending"
    time.sleep(.05)
    assert coordinator._thread is None  # closed: no waiter is started again


def test_close_while_waiting_for_network_guard_runs_nothing(rt, executor):
    # Review A SHOULD-7: a transaction abandoned while it waited for the
    # network guard (held by a stop teardown) applies nothing and records no
    # service.state after the session closed.
    session, guard_log = session_for(rt, executor)
    coordinator = bsa.register(session, interval=.02)
    rt.phase = "core_standby"
    rt.launch_core("bootstrap_core")
    applies = count_applies(coordinator)
    session.network_lock.acquire()
    try:
        coordinator.request(deferred(NET0, NET1), 1, "c")
        make_ready(rt)
        assert wait_for(executor._mutation_lock.locked)  # waiter is inside the domain
        time.sleep(.05)
        waiter = coordinator._thread
        coordinator.close(timeout=.05)
    finally:
        session.network_lock.release()
    waiter.join(3)
    assert not waiter.is_alive()
    assert applies == [] and guard_log == [] and "restart_core" not in rt.events
    assert not executor._mutation_lock.locked()


def test_waiter_logs_unexpected_failure_once_and_keeps_the_intent(rt, executor, caplog):
    # Review B SHOULD-1: no silent loop; one raw diagnostic per failure type,
    # bounded backoff, intent kept for a later attempt.
    session, _ = session_for(rt, executor)
    coordinator = bsa.register(session, interval=.01, max_backoff=.04)
    calls = []

    def broken_phase():
        calls.append(time.monotonic())
        raise KeyError("startup_phase")
    coordinator.port.startup_phase = broken_phase
    # Own handler on the target logger: independent of propagation/level state
    # left behind by other tests in the same process.
    records = []
    handler = logging.Handler(logging.WARNING)
    handler.emit = records.append
    logger = logging.getLogger(bsa.LOGGER.name)
    old_level, old_disabled = logger.level, logger.disabled
    logger.setLevel(logging.WARNING); logger.disabled = False; logger.addHandler(handler)
    try:
        coordinator.request(deferred(NET0, NET1), 1, "c")
        assert wait_for(lambda: len(calls) >= 5)
    finally:
        logger.removeHandler(handler); logger.setLevel(old_level); logger.disabled = old_disabled
    assert len([r for r in records if r.name == bsa.LOGGER.name]) == 1
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    assert max(gaps[2:]) >= .03  # backed off from 0.01 s toward the cap
    assert coordinator.pending()["state"] == "pending"
    assert coordinator.close()
