"""T-P2-RT2 runtime small changes (决定_第二波裁定 RT2 items + Fallback 10-06).

Real DesktopRuntime methods on the existing stand-in runtimes:
``tests.test_bridge_p2_10.FakeRuntime`` (real ConfigStore in tmp_path),
``tests.test_bridge_p2_11.StubRuntime`` (real B-26 ledger, fake Core),
``tests.test_bridge_p2_rg.build_runtime`` (real route getters, a controller
that fails on any request). No app start, Mihomo, registry, firewall or
network access; nothing touches the live profile.
"""

from __future__ import annotations

from copy import deepcopy
import inspect
import threading
import time
from types import SimpleNamespace

import pytest
import yaml

import bridge_commands as commands
import bridge_node_commands as node_commands
import bridge_settings_apply as bsa
import desktop_runtime as dr
from bridge_jobs import JobEventRecorder, JobExecutor, JobFailure
from bridge_node_commands import NodeCommands, RuntimeNodeCommandAdapter
from bridge_exit import RuntimeExitPort
from bridge_settings import RECHECK_STOPPED, RuntimeSettingsPort, SettingsService
from desktop_runtime import DesktopRuntime
from runtime_reload_gate import RuntimeReloadReason
from settings_effects import plan
from tests.test_bridge_p2_10 import FakeRuntime
from tests.test_bridge_p2_11 import StubRuntime
from tests.test_bridge_p2_14 import FakePort, sid
from tests.test_bridge_p2_rg import PAYLOAD, SUB, build_runtime, select_happened


GRAPH = RuntimeReloadReason.USER_GRAPH_CONFIG.value


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------

class SplitRuntime(FakeRuntime):
    """P2-10 FakeRuntime plus the T-P2-RT2 runtime entries (real methods)."""

    update_local_preferences = DesktopRuntime.update_local_preferences
    probe_latency_policy = DesktopRuntime.probe_latency_policy
    reload_latency_policy = DesktopRuntime.reload_latency_policy
    _latency_recheck_owner = DesktopRuntime._latency_recheck_owner

    def __init__(self, root):
        super().__init__(root)
        self._runtime_generation = 3
        self._stop_request_pending = False
        self._desired_running = True
        self.between_probe_and_reload = None  # hook run at the end of the probe
        original = self.manager.evaluate_state

        def evaluate_state():
            original()
            if self.between_probe_and_reload is not None:
                self.between_probe_and_reload()
        self.manager.evaluate_state = evaluate_state

    def core_fact(self):
        return {"running": self.core_runner.running, "pid": 4242, "cleanup_pending": False,
                "core_generation": self.core_generation}


def guarded_service(rt, *, coordinator=None):
    """SettingsService whose network guard is a real lock that logs entry/exit."""
    lock = threading.RLock()

    from contextlib import contextmanager

    @contextmanager
    def guard():
        with lock:
            rt.events.append("guard_enter")
            try:
                yield
            finally:
                rt.events.append("guard_exit")
    service = SettingsService(RuntimeSettingsPort(rt), coordinator=lambda: coordinator,
                              network_guard=guard, record_edge=lambda *a: None)
    return service, lock


def apply(service, rt, changes):
    return service.apply({"base_rev": rt.bridge_config_revision(), "changes": changes})


def local(rt):
    return yaml.safe_load(rt.local_path.read_text(encoding="utf-8"))


@pytest.fixture
def srt(tmp_path):
    return SplitRuntime(tmp_path)


# ---------------------------------------------------------------------------
# P2-10 #1: update_local_preferences (no reload_config)
# ---------------------------------------------------------------------------

def test_update_local_preferences_writes_under_the_lock_without_reload(srt):
    held = []
    read = srt.config_store.read_local
    srt.config_store.read_local = lambda: held.append(srt._config_change_lock._is_owned()) or read()

    def updater(payload):
        payload.setdefault("ui", {}).update({"language": "zh_CN", "start_in_tray_on_autostart": True})
        payload.setdefault("traffic", {})["history_seconds"] = 7200
        return payload

    srt.update_local_preferences(updater)
    assert held == [True] and srt.reloads == []
    saved = local(srt)
    assert saved["ui"]["language"] == "zh_CN" and saved["ui"]["start_in_tray_on_autostart"] is True
    assert saved["ui"]["theme"] == "custom-retained"  # untouched key kept
    assert srt.config["ui"]["language"] == "zh_CN"
    assert srt.config["ui"]["start_in_tray_on_autostart"] is True
    assert srt.config["traffic"]["history_seconds"] == 7200
    assert srt.events.count(("set_language", "zh_CN")) == 1
    with pytest.raises(TypeError):
        srt.update_local_preferences(lambda payload: ["not", "a", "mapping"])


def test_ui_only_save_never_reloads(srt):
    service, _ = guarded_service(srt)
    result = apply(service, srt, [{"section": "ui", "values": {"language": "zh_CN"}}])
    assert result["sections"] == [{"section": "ui", "saved": True, "applied": True,
                                   "effect": "ui", "error": None}]
    assert srt.reloads == []  # 决定_第二波裁定 P2-10 #1
    assert local(srt)["ui"]["language"] == "zh_CN"
    # The runtime entry synced the language; the orchestrator did not repeat it.
    assert srt.events.count(("set_language", "zh_CN")) == 1


def test_d8_and_traffic_save_never_reloads(srt):
    service, _ = guarded_service(srt)
    writes = srt.local_writes
    result = apply(service, srt, [
        {"section": "ui", "values": {"start_in_tray_on_autostart": True}},
        {"section": "traffic", "values": {"history_seconds": 7200}}])
    assert all(item["saved"] and item["error"] is None for item in result["sections"])
    assert srt.reloads == [] and srt.local_writes == writes + 1  # one locked write
    saved = local(srt)
    assert saved["ui"]["start_in_tray_on_autostart"] is True
    assert saved["traffic"]["history_seconds"] == 7200


def test_write_ui_preferences_skips_language_sync_without_alerts(srt):
    # 决定_第二波裁定 RG #8: a runtime without an AlertManager.
    del srt.alerts
    srt.write_ui_preferences({"language": "en_US", "theme": "daylight"})
    assert local(srt)["ui"]["theme"] == "daylight" and srt.config["ui"]["theme"] == "daylight"


# ---------------------------------------------------------------------------
# P2-10 #4 / #5: latency recheck split, commit id, Qt parity
# ---------------------------------------------------------------------------

def checks(rt):
    return [event for event in rt.events if event in {"check_exit", "check_tier1", "check_tier2",
                                                       "evaluate_state"}]


def reloads(rt):
    return [event for event in rt.events if isinstance(event, tuple) and event[0] == "reload_core"]


def test_probe_never_reloads_and_reload_uses_the_given_commit_id(srt):
    probe = srt.probe_latency_policy()
    assert checks(srt) == ["check_exit", "check_tier1", "check_tier2", "evaluate_state"]
    assert reloads(srt) == [] and probe["recheck_attempted"] is True
    assert probe["probe_owner"] == (3, 1, 4242)
    result = srt.reload_latency_policy(probe, user_commit_id="save-7:latency-recheck")
    assert result["errors"] == [] and "probe_owner" not in result
    assert reloads(srt) == [("reload_core", "incremental_export", GRAPH, "save-7:latency-recheck")]


@pytest.mark.parametrize("change", ["stop", "not_desired", "core_restarted", "core_stopped"])
def test_reload_is_abandoned_when_a_stop_or_new_core_follows_the_probe(srt, change):
    probe = srt.probe_latency_policy()
    if change == "stop":
        srt._stop_request_pending = True
    elif change == "not_desired":
        srt._desired_running = False
    elif change == "core_restarted":
        srt.core_generation += 1
    else:
        srt.core_runner.running = False
    result = srt.reload_latency_policy(probe, user_commit_id="c")
    assert result["reload_skipped"] == "stopped" and result["errors"]
    assert reloads(srt) == []


def test_recheck_entry_carries_the_caller_commit_id(srt):
    # 决定_第二波裁定 P2-10 #5.
    assert srt.recheck_latency_policy(user_commit_id="settings-apply:4:abc")["errors"] == []
    assert reloads(srt)[-1][3] == "settings-apply:4:abc"
    srt.recheck_latency_policy()
    assert reloads(srt)[-1][3].startswith("latency-policy:")


def test_qt_latency_setter_is_unchanged(srt):
    # Qt never passes the new parameters: save, then probe + reload in one
    # call with the old commit id, and no new stop check between them.
    srt._stop_request_pending = True
    result = srt.apply_latency_policy_settings(900)
    assert result["saved"] is True and result["errors"] == []
    assert checks(srt) == ["check_exit", "check_tier1", "check_tier2", "evaluate_state"]
    assert len(reloads(srt)) == 1 and reloads(srt)[0][3].startswith("latency-policy:900:")
    assert "reload_skipped" not in result


def test_live_save_probes_outside_the_network_lock_and_reloads_inside(srt):
    service, lock = guarded_service(srt)
    owned = []
    srt.between_probe_and_reload = lambda: owned.append(lock._is_owned())
    result = apply(service, srt, [{"section": "latency",
                                   "values": {"primary_gateway_max_latency_ms": 900}}])
    assert result["sections"] == [{"section": "latency", "saved": True, "applied": True,
                                   "effect": "recheck", "error": None}]
    assert owned == [False]  # the probe ran without the network lock
    tail = [event for event in srt.events if event in {"guard_enter", "guard_exit", "evaluate_state"}
            or (isinstance(event, tuple) and event[0] == "reload_core")]
    assert tail[0] == "evaluate_state" and tail[1] == "guard_enter" and tail[-1] == "guard_exit"
    assert tail[2][0] == "reload_core" and tail[2][3].endswith(":latency-recheck")
    assert tail[2][3].startswith("settings-apply:1:")


def test_live_save_stop_between_probe_and_reload_is_saved_not_applied(srt):
    service, _ = guarded_service(srt)

    def stop():
        srt._stop_request_pending = True
        srt.intent = {"desired_running": False, "stop_requested": True}
    srt.between_probe_and_reload = stop
    with pytest.raises(JobFailure) as caught:
        apply(service, srt, [{"section": "latency",
                              "values": {"primary_gateway_max_latency_ms": 900}}])
    assert caught.value.code == "lifecycle_busy"
    item = caught.value.partial_result["sections"][0]
    # 决定_第二波裁定 P2-10 #4: the reload is abandoned and applied=false,
    # never turned into effect=none by the "Core stopped meanwhile" rule.
    assert item["saved"] is True and item["applied"] is False and item["effect"] == "recheck"
    assert item["error"]["code"] == "lifecycle_busy"
    assert item["error"]["reason_code"] == RECHECK_STOPPED
    assert reloads(srt) == []
    success, safe, _ = commands.adapt_result("settings.apply", caught.value.partial_result)
    assert safe == caught.value.partial_result
    assert local(srt)["testing"]["primary_gateway_max_latency_ms"] == 900


def test_runtime_without_split_entries_keeps_the_combined_recheck(tmp_path):
    rt = FakeRuntime(tmp_path)  # no probe/reload entries: old path under the lock
    service, lock = guarded_service(rt)
    owned = []
    original = rt.manager.check_exit
    rt.manager.check_exit = lambda **kw: owned.append(lock._is_owned()) or original(**kw)
    result = apply(service, rt, [{"section": "latency",
                                  "values": {"primary_gateway_max_latency_ms": 900}}])
    assert result["sections"][0]["applied"] is True and owned == [True]


# ---------------------------------------------------------------------------
# P2-10 #4 deferred path (P2-11 coordinator)
# ---------------------------------------------------------------------------

@pytest.fixture
def executor():
    instance = JobExecutor(JobEventRecorder(8192), epoch="p2-rt2", require_acceptance=True)
    yield instance
    instance.shutdown(time.monotonic() + 1)


def session_for(runtime, executor):
    gate = SimpleNamespace(require_idle=lambda: None)
    return SimpleNamespace(runtime=runtime, executor=executor, network_lock=threading.RLock(),
                           report=SimpleNamespace(gate=gate), refresh_secrets=lambda: None,
                           record_service=lambda: None)


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.005)
    return predicate()


def deferred_latency(runtime, coordinator):
    runtime.phase = "core_standby"
    runtime.launch_core("bootstrap_core")
    dto = coordinator.request(plan({"latency": {"x": 1}}, {"latency": {"x": 2}},
                                   running=False, phase="collecting"), 1, "c")
    assert dto["state"] == "pending" and dto["effect"] == "recheck"
    return dto


def test_deferred_recheck_probes_without_any_bridge_lock(executor):
    rt = StubRuntime()
    session = session_for(rt, executor)
    coordinator = bsa.register(session, interval=.02)
    seen = []
    original_check = rt.manager._check
    original_reload = rt.reload_core_config

    def check(scope):
        seen.append(("probe", session.network_lock._is_owned(), executor._mutation_lock.locked()))
        return original_check(scope)

    def reload_core_config(**kwargs):
        seen.append(("reload", session.network_lock._is_owned(), executor._mutation_lock.locked()))
        return original_reload(**kwargs)
    rt.manager._check = check
    rt.reload_core_config = reload_core_config
    deferred_latency(rt, coordinator)
    rt.phase, rt.core_runner.running = "running", True
    assert wait_for(lambda: coordinator.pending()["state"] == "applied")
    assert seen[:3] == [("probe", False, False)] * 3
    assert seen[3:] == [("reload", True, True)]
    reload_events = [e for e in rt.events if isinstance(e, tuple) and e[0] == "reload"]
    assert reload_events == [("reload", "incremental_export", GRAPH, "c:latency-recheck")]
    coordinator.close()


def test_deferred_stop_between_probe_and_reload_cancels_without_reload(executor):
    rt = StubRuntime()
    coordinator = bsa.register(session_for(rt, executor), interval=.02)
    original = rt.manager.evaluate_state
    rt.manager.evaluate_state = lambda: (original(), rt.request_stop_fake())
    deferred_latency(rt, coordinator)
    rt.phase, rt.core_runner.running = "running", True
    assert wait_for(lambda: (coordinator.pending() or {}).get("state") in {"cancelled", "failed",
                                                                           "applied"})
    record = rt._settings_apply_ledger().current()
    assert record.state == "cancelled" and record.last_error["reason_code"] == "stopped"
    assert not any(isinstance(e, tuple) and e[0] == "reload" for e in rt.events)
    coordinator.close()


def test_parked_intent_without_a_probe_finishes_without_core_work():
    rt = StubRuntime()
    rt.phase = "core_standby"
    rt.launch_core("bootstrap_core")
    dto = rt.request_settings_apply(plan({"latency": {"x": 1}}, {"latency": {"x": 2}},
                                         running=False, phase="collecting"), 1, "c")
    rt.phase, rt.core_runner.running = "running", True
    parked = rt.apply_settings_intent(dto["intent_id"], split_latency=True)
    assert parked["state"] == "applying" and ("check", "exit") not in rt.events
    final = rt.finish_settings_intent_latency(dto["intent_id"], None)
    assert final["state"] == "failed" and final["error"]["code"] == "internal"
    assert not any(isinstance(e, tuple) and e[0] == "reload" for e in rt.events)
    # A second finish never re-applies a terminal record.
    assert rt.finish_settings_intent_latency(dto["intent_id"], {})["state"] == "failed"


# ---------------------------------------------------------------------------
# P2-10 #11 concurrent stop; #2 / #9 config.snapshot projection
# ---------------------------------------------------------------------------

def test_runtime_stopping_phase_after_plan_settles_effect_none(tmp_path):
    rt = FakeRuntime(tmp_path)
    rt.intent = {"desired_running": True, "stop_requested": False}
    rt.restart_ok = False
    original = rt.restart_core

    def restart():
        rt.phase = "stopping"  # a stop overtook the planned restart
        return original()
    rt.restart_core = restart
    service, _ = guarded_service(rt)
    result = apply(service, rt, [{"section": "network", "values": {"allow_lan": True}}])
    assert result["sections"] == [{"section": "network", "saved": True, "applied": True,
                                   "effect": "none", "error": None}]


def test_config_snapshot_projects_d8_and_the_notice_switches():
    rt = build_runtime()
    rt.config["ui"]["start_in_tray_on_autostart"] = True
    values = rt.bridge_config_values()["values"]
    assert values["ui"]["start_in_tray_on_autostart"] is True
    # 提醒 redesign 10-30: the alerts section is alerts.notify (defaults when unset).
    notify = values["alerts"]["notify"]
    assert set(values["alerts"]) == {"notify"}
    assert notify["enabled"] is True and notify["sound"] is True
    assert list(notify["events"]) == ["connection", "tier_fallback", "update"]
    rt.config["ui"]["start_in_tray_on_autostart"] = "yes"  # not projectable
    assert "start_in_tray_on_autostart" not in rt.bridge_config_values()["values"]["ui"]


# ---------------------------------------------------------------------------
# RG #6: set_network_mode generations
# ---------------------------------------------------------------------------

def mode_runtime(mode="system_proxy", *, running=True):
    rt = DesktopRuntime.__new__(DesktopRuntime)
    rt._core_lifecycle_lock = threading.Lock()
    rt._core_generation = 4
    rt.network_mode = mode
    rt.core_runner = SimpleNamespace(is_running=lambda: running)
    rt._process_is_admin = lambda: True
    rt.update_local_config = lambda updater: updater({})

    def restart():
        with rt._core_lifecycle_lock:
            rt._core_generation += 1
    rt.restart_core = restart
    return rt


def test_set_network_mode_reports_both_generations_and_keeps_qt_fields():
    rt = mode_runtime()
    assert rt.set_network_mode("tun") == {
        "mode": "tun", "changed": True, "restarted": True,
        "core_generation_before": 4, "core_generation_after": 5}
    rt.network_mode = "tun"
    assert rt.set_network_mode("tun") == {
        "mode": "tun", "changed": False,
        "core_generation_before": 5, "core_generation_after": 5}
    stopped = mode_runtime(running=False)
    result = stopped.set_network_mode("mixed")
    assert result["restarted"] is False
    assert (result["core_generation_before"], result["core_generation_after"]) == (4, 4)


@pytest.mark.parametrize("raw, observed, expected", [
    ({"restarted": True, "core_generation_before": 4, "core_generation_after": 5}, 9, True),
    # Not restarted by the call: another restart moved the generation.
    ({"restarted": False, "core_generation_before": 4, "core_generation_after": 4}, 5, False),
    ({"restarted": True, "core_generation_before": 4, "core_generation_after": 4}, 5, False),
    # More than one step: not attributable, the bridge's own samples decide.
    ({"restarted": True, "core_generation_before": 4, "core_generation_after": 6}, 6, True),
    ({"restarted": True}, 5, True),  # an older runtime without the fields
    (None, 4, False),
])
def test_restarted_is_attributed_with_the_runtime_generations(raw, observed, expected):
    assert node_commands._restarted_by(raw, 4, {"core_generation": observed}) is expected


def handler_for(port, executor):
    instance = NodeCommands(port, register_secrets=lambda values: None, refresh_secrets=lambda: None)
    for name, method in (("mode.set", instance.mode_set),
                         ("subscriptions.refresh", instance.subscriptions_refresh)):
        executor.register(name, method)
    return instance


def run(executor, name, args):
    ack = executor.submit(name, args, req_id="request-a", owner="window-a")
    assert ack.accepted, ack
    assert executor.acceptance_sent(ack.op, epoch=executor.epoch)
    job = executor.wait(ack.op, 5)
    assert job is not None
    return job


def test_mode_set_ignores_a_foreign_restart_during_the_switch(executor):
    port = FakePort()
    port.core = dict(port.core, running=False)
    port.expose_core_mode = True

    def set_mode(mode):
        port.persisted = port.loaded_mode = mode
        # Core was not running: no restart by this call; another start
        # (e.g. the user's service.start) launched generation 11 meanwhile.
        port.core = dict(port.core, running=True, core_generation=11)
        port.core_mode = mode
        return {"mode": mode, "changed": True, "restarted": False,
                "core_generation_before": 10, "core_generation_after": 10}
    port.set_mode = set_mode
    handler_for(port, executor)
    job = run(executor, "mode.set", {"mode": "tun"})
    assert job["state"] == "done"
    assert job["result"]["restarted"] is False and job["result"]["applied"] is True


# ---------------------------------------------------------------------------
# RG #7: refresh_subscription bounded acquire
# ---------------------------------------------------------------------------

def refresh_runtime():
    rt = build_runtime()
    rt._ensure_subscription_refresh_manager = lambda target_tier: rt.manager
    rt._refresh_subscription_tier_system = lambda tier, **kwargs: {"stored": False, "total": 0}
    return rt


ITEM = {"tier": "exit", "kind": "subscription", "url": SUB}


def hold(lock):
    held, release = threading.Event(), threading.Event()

    def holder():
        with lock:
            held.set()
            release.wait(5)
    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(5)
    return thread, release


def test_bounded_refresh_raises_typed_busy_and_runs_nothing():
    rt = refresh_runtime()
    thread, release = hold(rt._runtime_lifecycle_lock)
    try:
        started = time.monotonic()
        with pytest.raises(dr.RefreshLockBusy) as caught:
            rt.refresh_subscription(dict(ITEM), acquire_timeout=0.05)
        assert time.monotonic() - started < 1.0
        assert caught.value.bridge_code == "lifecycle_busy"
        assert not getattr(rt, "_bridge_subscription_refresh_jobs", None)  # nothing ran
        with pytest.raises(node_commands.RefreshLockBusy):
            RuntimeNodeCommandAdapter(rt).refresh_subscription(dict(ITEM))
    finally:
        release.set()
        thread.join(5)
    result = rt.refresh_subscription(dict(ITEM), acquire_timeout=0.05)
    assert result["stored"] is False and rt._bridge_subscription_refresh_jobs


def test_refresh_without_a_bound_waits_like_qt():
    rt = refresh_runtime()
    thread, release = hold(rt._runtime_lifecycle_lock)
    done = threading.Event()
    worker = threading.Thread(target=lambda: (rt.refresh_subscription(dict(ITEM)), done.set()))
    worker.start()
    try:
        assert not done.wait(0.2)  # Qt's call blocks on the held lock
    finally:
        release.set()
        thread.join(5)
    worker.join(5)
    assert done.is_set()
    assert inspect.signature(DesktopRuntime.refresh_subscription).parameters[
        "acquire_timeout"].default is None


def test_bridge_refresh_no_longer_checks_the_busy_getter(executor):
    port = FakePort()
    port.busy = True  # the old check-then-call input is ignored now
    calls = []
    port.subscription_refresh_busy = lambda: calls.append("busy") or True
    handler_for(port, executor)
    job = run(executor, "subscriptions.refresh", {"item_id": sid(port)})
    assert job["state"] == "done" and calls == []
    assert port.called("refresh_subscription")


# ---------------------------------------------------------------------------
# Fallback 10-06 #2: Core-loaded network mode
# ---------------------------------------------------------------------------

def test_loaded_mode_is_written_after_success_and_cleared_on_stop():
    reload_source = inspect.getsource(DesktopRuntime._hot_reload_runtime_config_phase_locked)
    # T-P2-RT3: the local now comes from the staged-aware helper.
    local_at = reload_source.index("loaded_network_mode = self._hot_reload_loaded_network_mode(")
    assert local_at < reload_source.index("self.write_mihomo_runtime_config()")
    assert local_at < reload_source.index("self.write_mihomo_no_capture_runtime_config()")
    phase_at = reload_source.index('"formal_capture" if capture_enabled else "formal_no_capture"')
    write_at = reload_source.index("self._core_loaded_network_mode = loaded_network_mode")
    assert phase_at < write_at < reload_source.index("self._invalidate_exit_route_context()")
    assert reload_source.count("_core_loaded_network_mode") == 1
    start_source = inspect.getsource(DesktopRuntime._start_core_once)
    assert start_source.index("loaded_network_mode = str(") < start_source.index(
        "self.write_mihomo_no_capture_runtime_config()")
    assert start_source.index('self._set_core_runtime_phase("formal_no_capture")') < \
        start_source.index("self._core_loaded_network_mode = loaded_network_mode")
    rt = build_runtime()
    rt._runtime_egress_coordinator = SimpleNamespace(set_current_snapshot=lambda value: None)
    rt._stop_runtime_post_dial_observer = lambda reason: None
    rt._set_core_runtime_phase("stopping")
    assert rt._core_loaded_network_mode == "system_proxy"
    rt._set_core_runtime_phase("stopped")
    assert rt._core_loaded_network_mode is None


# ---------------------------------------------------------------------------
# Fallback 10-06 #1: automatic exit lookup reads only the published snapshot
# ---------------------------------------------------------------------------

class CountingController:
    """Controller stand-in: counts GET /proxies, fails anything else."""

    def __init__(self):
        self.proxies_calls = 0

    def proxies(self, *args, **kwargs):
        self.proxies_calls += 1
        return deepcopy(PAYLOAD["proxies"])

    def __getattr__(self, name):
        pytest.fail(f"controller request {name}")


IDENTITY = {"identity": {"ipv4": {"ip": "203.0.113.5", "state": "confirmed", "evidence": []}}}


@pytest.fixture
def exit_runtime(monkeypatch):
    rt = build_runtime()
    del rt.mihomo_proxies  # the real method, over the counting controller
    rt.api_client = CountingController()
    rt._refresh_runtime_route_snapshot = lambda *a, **k: pytest.fail("route refresh")
    rt._overview_auto_identity_token = lambda: ("ready",)
    rt._system_proxy_capture_proof_required = lambda: False
    rt._register_runtime_internal_probe = lambda *a, **k: None
    rt._exit_quality_route_lock = threading.Lock()
    rt._exit_quality_route_cache, rt._exit_quality_route_cache_at = {}, 0.0
    rt._exit_quality_route_cache_route_epoch = -1
    sent = []
    monkeypatch.setattr(dr, "collect_exit_identity",
                        lambda **kwargs: sent.append(kwargs["proxy_url"]) or deepcopy(IDENTITY))
    rt.sent = sent
    return rt


def auto_lookup(rt, trigger="auto", **kwargs):
    # Through the bridge adapter: it adds route_source="published" to an
    # automatic lookup (the runtime default "live" stays Qt's behavior).
    return RuntimeExitPort(rt).check_current_exit_identity(
        proxy_url="http://127.0.0.1:7890", request_id="r-1", timeout_seconds=3.0,
        trigger=trigger, **kwargs)


@pytest.mark.parametrize("profile", ["early", "legacy"])
def test_auto_lookup_sends_zero_get_proxies(exit_runtime, profile):
    rt = exit_runtime
    if profile == "early":
        rt._runtime_egress_recovery_profile = dr._RUNTIME_EGRESS_RECOVERY_PROFILE_EARLY
    binding = rt.bridge_exit_binding()
    result = auto_lookup(rt, expected_binding_marker=binding["binding_marker"])
    assert result["state"] == "complete", result
    assert result["route"]["binding_marker"] == binding["binding_marker"]
    assert rt.api_client.proxies_calls == 0
    assert rt.sent == ["http://127.0.0.1:7890"]  # only the IP echo via the mixed port


@pytest.mark.parametrize("profile", ["early", "legacy"])
def test_auto_lookup_without_a_snapshot_is_unknown_and_sends_nothing(exit_runtime, profile):
    rt = exit_runtime
    if profile == "early":
        rt._runtime_egress_recovery_profile = dr._RUNTIME_EGRESS_RECOVERY_PROFILE_EARLY
    select_happened(rt)
    assert rt.bridge_exit_binding() is None
    result = auto_lookup(rt)
    assert result["state"] == "unavailable"
    assert result["diagnostics"]["overview_failure_reason"] == "route_unconfirmed"
    assert rt.api_client.proxies_calls == 0 and rt.sent == []


def test_manual_lookup_keeps_the_qt_route_readback(exit_runtime):
    # Manual 「查询出口」 is unchanged: its route readback is Qt's GET /proxies.
    rt = exit_runtime
    result = auto_lookup(rt, trigger="manual")
    assert result["state"] == "complete"
    assert rt.api_client.proxies_calls >= 1
    # ... and both paths agree on the binding marker of the same route.
    assert result["route"]["binding_marker"] == rt.bridge_exit_binding()["binding_marker"]


@pytest.mark.parametrize("profile", ["early", "legacy"])
def test_qt_auto_lookup_without_route_source_keeps_the_live_readback(exit_runtime, profile):
    # Qt calls check_current_exit_identity(trigger="auto") without
    # route_source: its route readback stays current_route_context.
    rt = exit_runtime
    if profile == "early":
        rt._runtime_egress_recovery_profile = dr._RUNTIME_EGRESS_RECOVERY_PROFILE_EARLY
    published = []
    original = rt._published_exit_route_context
    rt._published_exit_route_context = lambda: published.append(1) or original()
    marker = rt.bridge_exit_binding()["binding_marker"]
    published.clear()
    result = rt.check_current_exit_identity(
        proxy_url="http://127.0.0.1:7890", request_id="r-1", timeout_seconds=3.0,
        trigger="auto", expected_binding_marker=marker)
    assert result["state"] == "complete", result
    assert rt.api_client.proxies_calls >= 1 and published == []
    assert inspect.signature(DesktopRuntime.check_current_exit_identity).parameters[
        "route_source"].default == "live"


def test_qt_auto_lookup_without_a_snapshot_still_reads_the_live_route(exit_runtime):
    # No published snapshot: the bridge's lookup is unknown (above), Qt's
    # default auto lookup still reads the live route as before.
    rt = exit_runtime
    select_happened(rt)
    result = rt.check_current_exit_identity(
        proxy_url="http://127.0.0.1:7890", request_id="r-1", timeout_seconds=3.0,
        trigger="auto")
    assert result["state"] == "complete", result
    assert rt.api_client.proxies_calls >= 1


def test_route_source_published_is_ignored_for_manual_lookups(exit_runtime):
    rt = exit_runtime
    result = rt.check_current_exit_identity(
        proxy_url="http://127.0.0.1:7890", request_id="r-1", timeout_seconds=3.0,
        trigger="manual", route_source="published")
    assert result["state"] == "complete"
    assert rt.api_client.proxies_calls >= 1


def test_published_route_context_is_read_only(exit_runtime):
    rt = exit_runtime
    route = rt._published_exit_route_context()
    assert route["selected_chain"] == ["FINAL-EXIT", "EXIT-SELECT", "Gateway A"]
    assert route["leaf_proxy"] == "Gateway A" and route["confirmed"] is True
    assert route["managed_pool_kind"] == "gateway"
    assert rt.api_client.proxies_calls == 0
    source = inspect.getsource(DesktopRuntime._published_exit_route_state)
    body = source.split('"""')[2]
    assert "mihomo_proxies" not in body and "_refresh_runtime_route_snapshot" not in body
    assert "_queue_runtime_route_snapshot_refresh" not in body
    assert "_runtime_route_snapshot_state()" in body
    assert "_runtime_route_binding_for_outbound(snapshot, payload, \"FINAL-EXIT\")" in body


# ---------------------------------------------------------------------------
# Fallback 10-06 #3: sidecar spec
# ---------------------------------------------------------------------------

def test_sidecar_spec_lists_the_second_wave_modules():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "tesseract-runtime.spec").read_bytes().decode("utf-8-sig"))
    value = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                 and any(getattr(target, "id", None) == "hiddenimports" for target in node.targets))
    assert {"bridge_settings", "bridge_diagnostics", "tier3_origin"} <= set(ast.literal_eval(value))


# ---------------------------------------------------------------------------
# 修复第 1 轮 (T-P2-RT2 review): parked finish retry, probe prechecks,
# Core-loaded mode edges, published binding reuse
# ---------------------------------------------------------------------------

class GateBusy(RuntimeError):
    pass


def gated_session(runtime, executor, state):
    def require_idle():
        if state["busy"]:
            raise GateBusy("RT-1 recovery running")
    session = session_for(runtime, executor)
    session.report = SimpleNamespace(gate=SimpleNamespace(require_idle=require_idle))
    return session


def test_parked_finish_refused_by_the_gate_is_retried_to_a_terminal_state(executor):
    # MUST-1: the RT-1 gate turns busy while the probe runs; the finish is
    # refused, retried once the gate is idle, and the intent ends applied.
    rt = StubRuntime()
    state = {"busy": False}
    coordinator = bsa.register(gated_session(rt, executor, state), interval=.02,
                               max_backoff=.05)
    original = rt.manager.evaluate_state

    def evaluate_state():
        original()
        state["busy"] = True  # RT-1 recovery starts while the probe runs
    rt.manager.evaluate_state = evaluate_state
    deferred_latency(rt, coordinator)
    rt.phase, rt.core_runner.running = "running", True
    assert wait_for(lambda: state["busy"])
    time.sleep(.2)
    assert (coordinator.pending() or {}).get("state") == "applying"
    assert not any(isinstance(e, tuple) and e[0] == "reload" for e in rt.events)
    rt.manager.evaluate_state = original
    state["busy"] = False  # recovery finished
    assert wait_for(lambda: (coordinator.pending() or {}).get("state") == "applied"), \
        coordinator.pending()
    assert coordinator._parked_intents() == []
    assert not getattr(rt, "_settings_apply_latency_parked", {})
    reload_events = [e for e in rt.events if isinstance(e, tuple) and e[0] == "reload"]
    assert reload_events == [("reload", "incremental_export", GRAPH, "c:latency-recheck")]
    coordinator.close()


def test_parked_finish_never_probes_while_the_gate_is_busy(executor):
    rt = StubRuntime()
    state = {"busy": False}
    coordinator = bsa.register(gated_session(rt, executor, state), interval=.02,
                               max_backoff=.05)
    original_apply = rt.apply_settings_intent

    def apply_then_busy(*args, **kwargs):
        result = original_apply(*args, **kwargs)
        state["busy"] = True  # recovery starts right after the Core half
        return result
    rt.apply_settings_intent = apply_then_busy
    deferred_latency(rt, coordinator)
    rt.phase, rt.core_runner.running = "running", True
    assert wait_for(lambda: state["busy"])
    time.sleep(.2)
    assert ("check", "exit") not in rt.events  # the precheck skipped the probe
    state["busy"] = False
    assert wait_for(lambda: (coordinator.pending() or {}).get("state") == "applied")
    coordinator.close()


def test_close_finishes_a_refused_parked_intent_without_core_work(executor):
    rt = StubRuntime()
    state = {"busy": False}
    coordinator = bsa.register(gated_session(rt, executor, state), interval=.02,
                               max_backoff=.05)
    original = rt.manager.evaluate_state
    rt.manager.evaluate_state = lambda: (original(), state.__setitem__("busy", True))
    deferred_latency(rt, coordinator)
    rt.phase, rt.core_runner.running = "running", True
    assert wait_for(lambda: coordinator._parked_intents() and state["busy"])
    assert coordinator.close(timeout=2.0)
    record = rt._settings_apply_ledger().current()
    assert record.state == "failed" and record.last_error["code"] == "internal"
    assert not any(isinstance(e, tuple) and e[0] == "reload" for e in rt.events)
    assert coordinator._parked_intents() == []


def test_deferred_core_generation_change_after_the_probe_is_abandoned(executor):
    # latency_recheck_abandoned: another Core (no stop) after the probe.
    rt = StubRuntime()
    coordinator = bsa.register(session_for(rt, executor), interval=.02)
    original = rt.manager.evaluate_state

    def evaluate_state():
        original()
        with rt._core_lifecycle_lock:
            rt._core_generation += 1
    rt.manager.evaluate_state = evaluate_state
    deferred_latency(rt, coordinator)
    rt.phase, rt.core_runner.running = "running", True
    assert wait_for(lambda: (coordinator.pending() or {}).get("state") in {
        "applied", "failed", "cancelled"})
    record = rt._settings_apply_ledger().current()
    assert record.state == "failed"
    assert record.last_error["reason_code"] == "latency_recheck_abandoned"
    assert not any(isinstance(e, tuple) and e[0] == "reload" for e in rt.events)
    coordinator.close()


@pytest.mark.parametrize("stop", [False, True])
def test_deferred_core_gone_before_the_probe_is_not_applied(stop):
    rt = StubRuntime()
    rt.phase = "core_standby"
    rt.launch_core("bootstrap_core")
    dto = rt.request_settings_apply(plan({"latency": {"x": 1}}, {"latency": {"x": 2}},
                                         running=False, phase="collecting"), 1, "c")
    rt.phase, rt.core_runner.running = "running", True
    assert rt.apply_settings_intent(dto["intent_id"], split_latency=True)["state"] == "applying"
    rt.core_runner.running = False  # crashed / stopped between park and probe
    if stop:
        rt.request_stop_fake()
    probe = rt.probe_latency_policy()
    assert probe["probe_owner"] is None and probe["recheck_attempted"] is False
    assert ("check", "exit") not in rt.events  # nothing probed
    final = rt.finish_settings_intent_latency(dto["intent_id"], probe)
    assert final["state"] == ("cancelled" if stop else "failed")
    if not stop:
        assert final["error"]["reason_code"] == "latency_recheck_abandoned"
    assert not any(isinstance(e, tuple) and e[0] == "reload" for e in rt.events)


def test_probe_with_a_stop_requested_probes_nothing(srt):
    srt._stop_request_pending = True
    probe = srt.probe_latency_policy()
    assert probe["probe_owner"] is None and checks(srt) == []
    result = srt.reload_latency_policy(probe, user_commit_id="c")
    assert result["reload_skipped"] == "not_running" and result["errors"]
    assert reloads(srt) == []


def test_live_save_with_core_stopped_before_the_probe_settles_effect_none(srt):
    # 决定_第二波裁定 P2-10 #11: Core gone after the plan, before the probe.
    service, _ = guarded_service(srt)
    original = DesktopRuntime.probe_latency_policy

    def stopped_then_probe():
        srt.core_runner.running = False
        srt.intent = {"desired_running": False, "stop_requested": True}
        srt._desired_running = False
        return original(srt)
    srt.probe_latency_policy = stopped_then_probe
    result = apply(service, srt, [{"section": "latency",
                                   "values": {"primary_gateway_max_latency_ms": 900}}])
    assert result["sections"] == [{"section": "latency", "saved": True, "applied": True,
                                   "effect": "none", "error": None}]
    assert checks(srt) == [] and reloads(srt) == []


def test_live_save_probe_waits_for_an_idle_recovery_gate(srt):
    gate = {"busy": True}

    def recovery_gate():
        if gate["busy"]:
            raise GateBusy("RT-1 recovery running")
    service = SettingsService(RuntimeSettingsPort(srt), recovery_gate=recovery_gate,
                              record_edge=lambda *a: None)
    item = service._latency_recheck("c")
    assert item["code"] == "lifecycle_busy" and item["reason_code"] == "recovery_in_progress"
    assert checks(srt) == []  # no network probe while the gate is busy


def test_save_while_an_intent_is_parked_reports_the_applying_intent():
    calls = []

    class Parked:
        def request(self, plan, rev, commit_id):
            calls.append(rev)
            return None

        def pending(self):
            return {"intent_id": "i-1", "state": "applying", "config_rev": 1}
    service = SettingsService(SimpleNamespace(), coordinator=lambda: Parked())
    assert service._request_deferred(None, 2, "c")["state"] == "applying"

    class Done(Parked):
        def pending(self):
            return {"intent_id": "i-1", "state": "applied", "config_rev": 1}
    service = SettingsService(SimpleNamespace(), coordinator=lambda: Done())
    assert service._request_deferred(None, 2, "c") is None and calls == [2, 2]


def test_runtime_parked_table_merges_entries():
    rt = StubRuntime()
    rt._settings_apply_latency_parked = {"older": True}
    rt.phase = "core_standby"
    rt.launch_core("bootstrap_core")
    dto = rt.request_settings_apply(plan({"latency": {"x": 1}}, {"latency": {"x": 2}},
                                         running=False, phase="collecting"), 1, "c")
    rt.phase, rt.core_runner.running = "running", True
    rt.apply_settings_intent(dto["intent_id"], split_latency=True)
    assert set(rt._settings_apply_latency_parked) == {"older", dto["intent_id"]}


class HotReloadRuntime(DesktopRuntime):
    """Only what _hot_reload_runtime_config_phase_locked touches (stand-ins)."""

    def __init__(self, *, reloaded):  # noqa: D401 - not DesktopRuntime.__init__
        self._core_lifecycle_lock = threading.Lock()
        self._runtime_route_snapshot_lock = threading.RLock()
        self._runtime_route_epoch = 0
        self._core_generation = 4
        self._core_runtime_phase = "formal_no_capture"
        self._core_loaded_network_mode = "system_proxy"
        self._staged_capture_runtime_config_payload = None
        self._staged_capture_runtime_config_path = None
        self._staged_capture_runtime_export_contract = None
        self.network_mode = "tun"
        self.stop_event = threading.Event()
        self.events = []
        self.core_runner = SimpleNamespace(
            is_running=lambda: True,
            hot_reload=lambda **kw: self.events.append("hot_reload") or (
                {"reloaded": True} if reloaded else {"reloaded": False, "reason": "x"}))

    def write_mihomo_no_capture_runtime_config(self):
        self.events.append("write_no_capture")
        return "runtime.yaml"

    def write_mihomo_runtime_config(self):
        self.events.append("write_capture")
        return "runtime.yaml"

    def _ensure_geodata_ready_for_core_start(self, path):
        pass

    def _mihomo_controller_config(self):
        return {"enabled": True}

    def _mark_loaded_runtime_export_contract(self):
        pass

    def _mark_runtime_source_transport_loaded(self):
        pass

    def _disarm_runtime_recovery(self, reason):
        pass

    def _node_judgement_arm_epoch(self, label):
        pass

    def _mark_runtime_route_attribution_barrier_locked(self):
        pass

    def _queue_runtime_route_snapshot_refresh(self):
        pass

    def _invalidate_exit_route_context(self):
        pass

    def _refresh_node_diagnostic_core_binding(self):
        pass


@pytest.mark.parametrize("capture", [False, True])
def test_hot_reload_records_the_loaded_mode_only_on_success(capture):
    rt = HotReloadRuntime(reloaded=True)
    rt._hot_reload_runtime_config_phase_locked(rt.core_runner, capture_enabled=capture)
    assert rt.events == ["write_capture" if capture else "write_no_capture", "hot_reload"]
    assert rt._core_runtime_phase == ("formal_capture" if capture else "formal_no_capture")
    assert rt._core_loaded_network_mode == "tun"
    failed = HotReloadRuntime(reloaded=False)
    with pytest.raises(RuntimeError):
        failed._hot_reload_runtime_config_phase_locked(failed.core_runner,
                                                       capture_enabled=capture)
    assert failed._core_loaded_network_mode == "system_proxy"  # unchanged on failure


def test_bootstrap_phase_clears_the_loaded_mode():
    rt = build_runtime()
    rt._stop_runtime_post_dial_observer = lambda reason: None
    assert rt._core_loaded_network_mode == "system_proxy"
    rt._set_core_runtime_phase("bootstrap")
    assert rt._core_loaded_network_mode is None
    assert rt.bridge_core_network_mode() is None


def test_cold_start_write_does_not_revive_a_mode_cleared_by_a_stop():
    source = inspect.getsource(DesktopRuntime._start_core_once)
    write_at = source.index("self._core_loaded_network_mode = loaded_network_mode")
    guard_at = source.index('if self._core_runtime_phase == "formal_no_capture":')
    assert source.index('self._set_core_runtime_phase("formal_no_capture")') < guard_at < write_at


def test_published_binding_is_reused_for_the_same_publication(exit_runtime):
    rt = exit_runtime
    copies = []
    original = rt._runtime_route_snapshot_state
    rt._runtime_route_snapshot_state = lambda: copies.append(1) or original()
    first = rt.bridge_exit_binding()
    for _ in range(3):
        assert rt._published_exit_route_context()["binding_marker"] == first["binding_marker"]
    assert len(copies) == 1  # one deep copy per publication
    select_happened(rt)  # a new publication (here: none) is read again
    assert rt.bridge_exit_binding() is None
