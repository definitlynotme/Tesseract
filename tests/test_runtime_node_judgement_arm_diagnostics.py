"""F01: compare control truth to the frozen handoff; verify only new diagnostics.

No application, Core, browser, live configuration or external network is used.
"""
from __future__ import annotations

import ast
import dataclasses
import logging
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import desktop_runtime as module
from desktop_runtime import DesktopRuntime, _InitialEgressAdmissionLease, _RuntimeRecoveryArm
from runtime_egress_recovery import RuntimeEgressReadinessState
from runtime_post_dial import PostDialTokenizer
from runtime_post_dial_judge import PostDialJudgeService
from runtime_post_dial_observer import PostDialObserverRunner, format_observer_record


BASE = Path(__file__).resolve().parents[1] / ".build/claude-fallback/handoff-baseline-20261003"
# The oracle is the frozen handoff copy kept in local evidence (.build/ is
# never published), so the public tree and CI have nothing to compare with.
if not (BASE / "desktop_runtime.py").is_file():
    pytest.skip("needs the frozen handoff baseline under .build/ (local evidence only)",
                allow_module_level=True)


def original(name: str):
    tree = ast.parse((BASE / "desktop_runtime.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DesktopRuntime")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = dict(vars(module))
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<handoff-oracle>", "exec"), namespace)
    return namespace[name]


OLD_ARM = original("_runtime_recovery_is_armed")
OLD_TRY = original("_try_arm_runtime_recovery")
OLD_INITIAL = original("_runtime_initial_activation_complete")


def fixture(*, enabled=False):
    rt = object.__new__(DesktopRuntime)
    rt.config = {"client": {"egress_recovery": {"node_judgement": {
        "enabled": enabled, "post_dial_judge": "act" if enabled else "off"}}}}
    rt._runtime_failure_monitor_lock = threading.RLock()
    rt._runtime_route_snapshot_lock = threading.RLock()
    rt._core_lifecycle_lock = threading.RLock()
    rt._runtime_route_epoch = 3
    rt._runtime_facility_epoch = 2
    rt._runtime_selector_intent_revision = 4
    rt._lifecycle_revision = 5
    rt._runtime_generation = 6
    rt._core_generation = 7
    rt._runtime_initial_activation_generation = 7
    rt._tier3_egress_activation_generation = 8
    rt._runtime_incident_recovery_state = {}
    rt._runtime_egress_readiness = NS(state=RuntimeEgressReadinessState.READY, validation_active=False)
    rt.manager = NS(effective_egress_ok=True)
    snapshot = NS(snapshot_key=(1, 2, 7, "d", 3, 0), route_epoch=3)
    binding = NS(node=NS(key=("tier3", "pool", "private-leaf", "private.example")))
    snapshot.owns = lambda candidate: candidate is binding
    payload = {"proxies": {}}
    rt._runtime_route_snapshot = snapshot
    rt._runtime_route_snapshot_payload = payload
    rt._runtime_egress_coordinator = NS(current_route_snapshot=lambda: snapshot)
    rt._runtime_route_snapshot_state = lambda: (snapshot, payload)
    rt._runtime_route_binding_for_outbound = lambda *_args: binding
    rt._runtime_worker_token = lambda: (6, 7, 8)
    rt._runtime_worker_token_is_current = lambda token: True
    rt._runtime_uses_early_single_coordinator = lambda: False
    rt._runtime_facility_hold_reason = lambda: ""
    rt._commit_runtime_monitoring_lease = lambda: True
    rt._source_control_final_exit_ready = lambda manager: False
    rt._revoke_tier3_source_control_qualification = lambda reason: None
    rt.core_runner = NS(status=lambda: {"pid": 11, "process_generation": 12}, is_running=lambda: True)
    rt._runtime_recovery_arm = _RuntimeRecoveryArm(
        worker_token=(6, 7, 8), snapshot_key=snapshot.snapshot_key,
        binding_key=binding.node.key, route_epoch=3, facility_epoch=2,
        selector_intent_revision=4, lifecycle_revision=5, armed_at_monotonic=1.0,
    )
    return rt, snapshot, payload, binding


ARM_CASES = (
    "no_arm", "initial_activation:readiness_state", "initial_activation:validation_active",
    "initial_activation:activation_generation", "initial_activation:effective_egress_ok_none",
    "initial_activation:effective_egress_ok_false", "route_epoch", "facility_epoch",
    "selector_intent", "lifecycle", "worker_token", "snapshot_missing",
    "snapshot_key_local", "snapshot_key_coordinator", "snapshot_key_caller",
    "binding_none", "binding_not_owned", "binding_key",
)


def arm_case(name):
    rt, snap, payload, binding = fixture()
    caller = None
    if name == "no_arm": rt._runtime_recovery_arm = None
    elif name.endswith(":readiness_state"): rt._runtime_egress_readiness.state = None
    elif name.endswith(":validation_active"): rt._runtime_egress_readiness.validation_active = True
    elif name.endswith(":activation_generation"): rt._runtime_initial_activation_generation = -1
    elif name.endswith(":effective_egress_ok_none"): rt.manager.effective_egress_ok = None
    elif name.endswith(":effective_egress_ok_false"): rt.manager.effective_egress_ok = False
    elif name == "route_epoch": rt._runtime_route_epoch += 1
    elif name == "facility_epoch": rt._runtime_facility_epoch += 1
    elif name == "selector_intent": rt._runtime_selector_intent_revision += 1
    elif name == "lifecycle": rt._lifecycle_revision += 1
    elif name == "worker_token": rt._runtime_worker_token_is_current = lambda _: False
    elif name == "snapshot_missing": rt._runtime_egress_coordinator.current_route_snapshot = lambda: None
    elif name == "snapshot_key_local": snap.snapshot_key = ("different",)
    elif name == "snapshot_key_coordinator": rt._runtime_egress_coordinator.current_route_snapshot = lambda: NS(snapshot_key=("different",))
    elif name == "snapshot_key_caller": caller = NS(snapshot_key=("different",))
    elif name == "binding_none": rt._runtime_route_binding_for_outbound = lambda *_: None
    elif name == "binding_not_owned": snap.owns = lambda _: False
    elif name == "binding_key": binding.node.key = ("other",)
    return rt, caller


@pytest.mark.parametrize("check", ARM_CASES)
def test_arm_every_branch_matches_handoff_and_names_first_failure(check):
    rt, caller = arm_case(check)
    before = OLD_ARM(rt, caller)
    with rt._node_judgement_arm_capture() as trace:
        after = rt._runtime_recovery_is_armed(caller)
    assert before is after is False
    assert trace["arm_check"] == check
    # Passing published refs must take the same route_state branch without a copy.
    route_state = rt._runtime_route_snapshot_state()
    assert OLD_ARM(rt, caller, route_state=route_state) == rt._runtime_recovery_is_armed(caller, route_state=route_state)


def test_success_compatibility_early_profile_and_initial_overrides():
    rt, *_ = fixture()
    assert OLD_ARM(rt) is rt._runtime_recovery_is_armed() is True
    rt._runtime_facility_epoch += 1
    rt._runtime_uses_early_single_coordinator = lambda: True
    assert OLD_ARM(rt) is rt._runtime_recovery_is_armed() is True
    del rt._runtime_recovery_arm
    assert OLD_ARM(rt) is rt._runtime_recovery_is_armed() is True
    rt, *_ = fixture()
    rt._runtime_initial_activation_complete = lambda: False
    with rt._node_judgement_arm_capture() as trace:
        assert rt._runtime_recovery_is_armed() is False
    assert trace["arm_check"] == "initial_activation:override_unknown"


@pytest.mark.parametrize("check", ARM_CASES)
def test_label_mutation_is_caught_by_branch_oracle(check):
    rt, caller = arm_case(check)
    method = "_node_judgement_arm_initial_refusal" if check.startswith("initial_activation:") else "_node_judgement_arm_refusal"
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DesktopRuntime")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method)
    label = check.split(":", 1)[1] if method.endswith("initial_refusal") else check
    changed = 0
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and node.value == label:
            node.value = "mutated_label"
            changed += 1
    assert changed
    namespace = dict(vars(module))
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<mutant>", "exec"), namespace)
    setattr(rt, method, types.MethodType(namespace[method], rt))
    with rt._node_judgement_arm_capture() as trace:
        rt._runtime_recovery_is_armed(caller)
    with pytest.raises(AssertionError):
        assert trace["arm_check"] == check


def lease():
    return _InitialEgressAdmissionLease(activation_generation=8, lifecycle_revision=5,
        runtime_generation=6, core_generation=7, process_pid=11, process_generation=12,
        selector_intent_revision=4, worker_token=(6, 7, 8), deadline_monotonic=100.0)


TRY_CASES = (
    "not_ready", "facility_hold", "incident_active", "snapshot_missing",
    "snapshot_key_mismatch", "binding_none", "binding_not_owned", "worker_token",
    "lock_recheck:initial_activation:validation_active", "lock_recheck:pre_ready_lease",
    "installed_then_unarmed:route_epoch", "pre_ready_lease:missing",
    "pre_ready_lease:runner_exception", "pre_ready_lease:core_generation",
    "pre_ready_lease:runtime_generation", "pre_ready_lease:lifecycle",
    "pre_ready_lease:activation_generation", "pre_ready_lease:selector_intent",
    "pre_ready_lease:runner_not_live", "pre_ready_lease:process_pid",
    "pre_ready_lease:process_generation", "pre_ready_lease:worker_token",
)


def try_case(check):
    rt, snap, payload, binding = fixture()
    rt._runtime_recovery_arm = None
    kwargs = {}
    if check.startswith("pre_ready_lease:") or check == "lock_recheck:pre_ready_lease":
        token = lease()
        kwargs = {"allow_pre_ready": True, "admission_lease": token}
        sub = check.split(":", 1)[1]
        fields = {"core_generation": "core_generation", "runtime_generation": "runtime_generation",
                  "lifecycle": "lifecycle_revision", "activation_generation": "activation_generation",
                  "selector_intent": "selector_intent_revision", "process_pid": "process_pid",
                  "process_generation": "process_generation"}
        if sub in fields:
            kwargs["admission_lease"] = dataclasses.replace(token, **{fields[sub]: -7})
        if sub == "missing": kwargs["admission_lease"] = None
        if sub == "runner_exception": rt.core_runner.status = lambda: (_ for _ in ()).throw(RuntimeError("fixture"))
        if sub == "runner_not_live": rt.core_runner.is_running = lambda: False
        if sub == "worker_token": rt._runtime_worker_token_is_current = lambda _: False
    if check == "not_ready": rt.manager.effective_egress_ok = None
    elif check == "facility_hold": rt._runtime_facility_hold_reason = lambda: "hold"
    elif check == "incident_active": rt._runtime_incident_recovery_state = {"private.example": "pending", "private-leaf": "recovering"}
    elif check == "snapshot_missing": rt._runtime_route_snapshot_state = lambda: (None, None)
    elif check == "snapshot_key_mismatch": rt._runtime_egress_coordinator.current_route_snapshot = lambda: NS(snapshot_key=("other",))
    elif check == "binding_none": rt._runtime_route_binding_for_outbound = lambda *_: None
    elif check == "binding_not_owned": snap.owns = lambda _: False
    elif check == "worker_token": rt._runtime_worker_token_is_current = lambda _: False
    elif check.startswith("lock_recheck:") or check.startswith("installed_then_unarmed:"):
        def worker():
            if check.endswith("validation_active"): rt._runtime_egress_readiness.validation_active = True
            elif check.endswith("pre_ready_lease"): rt._tier3_egress_activation_generation += 1
            else: rt._runtime_route_epoch += 1
            return (6, 7, 8)
        rt._runtime_worker_token = worker
    return rt, kwargs


@pytest.mark.parametrize("check", TRY_CASES)
def test_every_try_rejection_matches_handoff_including_installed_epoch_race(check):
    before, kw0 = try_case(check)
    after, kw1 = try_case(check)
    expected = OLD_TRY(before, **kw0)
    captured = []
    def capture(result, trace, *, ticket):
        assert ticket is None  # This control-difference fixture has diagnostics disabled.
        captured.append((result, trace))
    after._node_judgement_arm_try_result = capture
    actual = after._try_arm_runtime_recovery(**kw1)
    assert expected is actual is False
    trace = captured[0][1]
    diagnostic = trace.get("refusal", "installed_then_unarmed:" + str(trace.get("arm_check")))
    assert diagnostic == check
    assert (before._runtime_recovery_arm is None) == (after._runtime_recovery_arm is None)
    if check == "installed_then_unarmed:route_epoch":
        assert after._runtime_recovery_arm.route_epoch == 3 and after._runtime_route_epoch == 4


@pytest.mark.parametrize("pre_ready", [False, True])
def test_try_success_return_and_preserved_existing_arm_timestamp(pre_ready):
    rt, *_ = fixture()
    arm = rt._runtime_recovery_arm
    kwargs = {"allow_pre_ready": pre_ready, "admission_lease": lease() if pre_ready else None}
    assert OLD_TRY(rt, **kwargs) is rt._try_arm_runtime_recovery(**kwargs) is True
    assert rt._runtime_recovery_arm is arm


def drain(rt):
    state = rt.__dict__.get("_node_judgement_arm_diagnostics_state")
    if state is None: return
    for _ in range(20):
        with state["lock"]: worker = state["worker"]
        if worker is None: return
        worker.join(1.0)
    pytest.fail("diagnostic worker did not retire")


def collector(rt):
    records = []
    def emit(level, record):
        assert not rt._runtime_failure_monitor_lock._is_owned()
        assert not rt._runtime_route_snapshot_lock._is_owned()
        assert not rt._core_lifecycle_lock._is_owned()
        fields = dict(record)
        fields.pop("_arm_enveloped", None)
        records.append(fields)
    rt._log_runtime_post_dial_record = emit
    return records


def test_off_and_disabled_create_no_thread_or_line_and_do_not_evaluate_again():
    for enabled, mode in [(True, "off"), (False, "act")]:
        rt, *_ = fixture()
        rt.config["client"]["egress_recovery"]["node_judgement"].update(enabled=enabled, post_dial_judge=mode)
        records = collector(rt)
        rt._node_judgement_arm_observe("publish")
        rt._node_judgement_arm_epoch("fixture")
        rt._try_arm_runtime_recovery()
        assert records == []
        assert "_node_judgement_arm_diagnostics_state" not in rt.__dict__


def test_epoch_calls_cover_all_twelve_existing_mutations_without_control_calls():
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "_node_judgement_arm_epoch"]
    assert len(calls) == 12
    assert len({n.args[0].value for n in calls}) == 12
    rt, *_ = fixture(enabled=True)
    records = collector(rt)
    rt._runtime_recovery_is_armed = lambda *_a, **_k: pytest.fail("epoch hook must not evaluate the predicate")
    with rt._runtime_route_snapshot_lock:
        rt._runtime_route_epoch += 1
        rt._node_judgement_arm_epoch("fixture")
    drain(rt)
    row = next(r for r in records if r["ev"] == "route_epoch")
    assert (row["old"], row["new"], row["armed_before"]) == (3, 4, "unknown")


def test_state_change_only_and_refusal_rate_limit_accounting(monkeypatch):
    rt, *_ = fixture(enabled=True)
    records = collector(rt)
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    for _ in range(1000):
        rt._node_judgement_arm_try_result(False, {"refusal": "not_ready", "arm_check": "no_arm"})
    drain(rt)
    assert len([r for r in records if r["ev"] == "arm_refused"]) <= 2
    assert len([r for r in records if r["ev"] == "arm_state"]) == 1
    clock[0] += 5.0
    rt._node_judgement_arm_try_result(False, {"refusal": "not_ready", "arm_check": "no_arm"})
    drain(rt)
    assert [r for r in records if r["ev"] == "arm_refused"][-1]["suppressed"] == 999


def test_new_gated_check_counter_keeps_original_output_dedupe():
    records = []
    service = PostDialJudgeService(hooks=NS(), tokenizer=PostDialTokenizer(b"f01-diagnostics-key-32-bytes-long!"), emit=lambda level, record: records.append(record))
    trigger = NS(kind="FAST", leaf="private-leaf", episode=9)
    for check in ["no_arm", "route_epoch", "route_epoch", "worker_token"]:
        service.note_gated(trigger, "not_armed", arm_check=check)
    assert len(records) == 1 and records[0]["arm_check"] == "no_arm"
    assert records[0]["episode"] == 9
    assert service.metrics["gated_not_armed_check_changes"] == 2
    assert "private-leaf" not in format_observer_record(records[0])


def test_original_gated_sequence_and_existing_metrics_are_identical():
    import runtime_post_dial as post_dial
    import runtime_post_dial_judge as judge_module
    original_core = ast.parse((BASE / "runtime_post_dial.py").read_text(encoding="utf-8"))
    record_fn = next(n for n in original_core.body if isinstance(n, ast.FunctionDef) and n.name == "gated_record")
    namespace = dict(vars(post_dial))
    exec(compile(ast.Module(body=[record_fn], type_ignores=[]), "<old-gated-record>", "exec"), namespace)
    original_judge = ast.parse((BASE / "runtime_post_dial_judge.py").read_text(encoding="utf-8"))
    cls = next(n for n in original_judge.body if isinstance(n, ast.ClassDef) and n.name == "PostDialJudgeService")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "note_gated")
    env = {**vars(judge_module), "gated_record": namespace["gated_record"]}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<old-note-gated>", "exec"), env)
    old_rows, new_rows = [], []
    key = PostDialTokenizer(b"f01-diagnostics-key-32-bytes-long!")
    old = PostDialJudgeService(hooks=NS(), tokenizer=key, emit=lambda level, row: old_rows.append(row), wall=lambda: 1.0)
    new = PostDialJudgeService(hooks=NS(), tokenizer=key, emit=lambda level, row: new_rows.append(row), wall=lambda: 1.0)
    for episode, reason, check in [(1, "not_armed", "no_arm"), (1, "not_armed", "route_epoch"),
                                   (1, "judging", None), (1, "not_armed", "worker_token"),
                                   (2, "not_armed", "no_arm"), (2, "not_armed", "no_arm")]:
        trigger = NS(kind="FAST", leaf="private-leaf", episode=episode)
        env["note_gated"](old, trigger, reason)
        new.note_gated(trigger, reason, arm_check=check)
    assert old_rows == [{k: v for k, v in row.items() if k not in {"episode", "arm_check"}} for row in new_rows]
    assert dict(old.metrics) == {k: v for k, v in new.metrics.items() if k != "gated_not_armed_check_changes"}


def test_all_observation_paths_make_zero_controller_calls():
    rt, snap, payload, _ = fixture(enabled=True)
    records = collector(rt)
    calls = []
    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("diagnostics must not call a controller")
    rt.mihomo_proxies = forbidden
    rt._mihomo_proxies_bounded = forbidden
    rt._runtime_post_dial_controller = forbidden
    rt._runtime_tier3_route_view = lambda *_: None
    rt._observe_published_runtime_route_view(snap, payload)
    rt._try_arm_runtime_recovery()
    rt._disarm_runtime_recovery("private.example")
    rt._runtime_route_epoch += 1
    rt._node_judgement_arm_epoch("fixture")
    drain(rt)
    assert calls == []
    assert {r["ev"] for r in records} >= {"arm_state", "route_epoch"}
    assert "private.example" not in repr(records)


def test_prefixed_ids_and_event_dedupe_metadata_bounds(monkeypatch):
    rt, *_ = fixture(enabled=True)
    monkeypatch.setattr(module.os, "urandom", lambda size: b"\x11" * size)
    records = collector(rt)
    rt._node_judgement_arm_kick = lambda state: None
    scope = rt._node_judgement_arm_scope()
    assert scope["instance"].startswith("rt-") and scope["observer"].startswith("ob-") and scope["scope"].startswith("sc-")
    local = rt.__dict__.setdefault("_node_judgement_arm_local", threading.local())
    local.scope = scope
    judge = NS(tokenizer=PostDialTokenizer(b"f01-diagnostics-key-32-bytes-long!"))
    for i in range(300):
        rt._node_judgement_arm_admission(NS(kind="FAST", leaf=f"private-{i}", episode=i), judge,
            "not_armed", check="no_arm", source="checked", evaluated=True)
    assert len(scope["last"]) == 256
    rt._node_judgement_arm_retire(scope)
    rt._node_judgement_arm_drain(scope["state"])
    assert scope["last"] == {}
    assert "private-" not in repr(records)


def test_logger_failure_marks_fence_incomplete_even_without_a_loss_line():
    rt, *_ = fixture(enabled=True)
    rows = []
    def emit(level, record):
        if record["ev"] == "admission":
            raise OSError("controlled sink failure")
        rows.append(dict(record))
    rt._log_runtime_post_dial_record = emit
    rt._node_judgement_arm_kick = lambda state: None
    scope = rt._node_judgement_arm_scope()
    rt._node_judgement_arm_submit({"ev": "admission", "reason": "not_armed"}, scope)
    rt._node_judgement_arm_retire(scope)
    rt._node_judgement_arm_drain(scope["state"])
    fence = next(r for r in rows if r["ev"] == "admission_fence")
    assert not fence["complete"] and fence["loss_total"] == 1
    assert fence["final_emitted_seq"] == 0 and fence["final_produced_seq"] == 1


def test_closed_fence_proves_late_tail_instead_of_stopped_or_summary():
    rt, *_ = fixture(enabled=True)
    records = collector(rt)
    scope = rt._node_judgement_arm_scope()
    rt._node_judgement_arm_kick = lambda state: None  # deterministically hold the new drain only
    rt._node_judgement_arm_observer_emit("info", {"ev": "observer_started_context", "act_rules": ["FAST"]}, scope)
    judge = NS(tokenizer=PostDialTokenizer(b"f01-diagnostics-key-32-bytes-long!"))
    local = rt.__dict__.setdefault("_node_judgement_arm_local", threading.local())
    local.scope = scope
    for i in range(10):
        trigger = NS(kind="FAST", leaf="private-leaf", episode=i)
        rt._node_judgement_arm_observer_emit("info", {"ev": "trigger", "leaf": judge.tokenizer.leaf(trigger.leaf), "rule": "FAST", "episode": i}, scope)
        rt._node_judgement_arm_admission(trigger, judge, None, check="armed", source="checked", evaluated=True)
    for i in range(7, 10):
        rt._node_judgement_arm_admission(NS(kind="FAST", leaf="private-leaf", episode=i), judge,
            "not_armed", check="route_epoch", source="cached", evaluated=False)
    rt._node_judgement_arm_observer_emit("info", {"ev": "observer_stopped"}, scope)
    assert not any(r["ev"] == "admission_fence" for r in records)
    rt._node_judgement_arm_retire(scope)
    rt._node_judgement_arm_drain(scope["state"])
    fence = next(r for r in records if r["ev"] == "admission_fence")
    data = [r for r in records if "scope_seq" in r and r["scope"] == scope["scope"]]
    assert sorted(r["scope_seq"] for r in data) == list(range(1, fence["final_produced_seq"] + 1))
    assert fence["complete"] is True and fence["closed"] is True
    assert fence["final_emitted_seq"] == fence["emitted_count"] == fence["final_produced_seq"]
    final = {r["episode"]: r["reason"] for r in sorted(data, key=lambda r: r["scope_seq"]) if r["ev"] == "admission"}
    assert sum(reason != "not_armed" for reason in final.values()) == 7
    assert fence["cut_t"] >= max(r["t"] for r in data)


def test_queue_loss_cannot_produce_complete_closure_and_metadata_is_bounded():
    rt, *_ = fixture(enabled=True)
    records = collector(rt)
    rt._node_judgement_arm_kick = lambda state: None
    scope = rt._node_judgement_arm_scope()
    for i in range(1200):
        rt._node_judgement_arm_submit({"ev": "admission", "episode": i}, scope)
    assert len(scope["state"]["queue"]) <= 1024
    rt._node_judgement_arm_retire(scope)
    rt._node_judgement_arm_drain(scope["state"])
    fence = next(r for r in records if r["ev"] == "admission_fence")
    assert fence["complete"] is False and fence["loss_total"] > 0
    assert scope["scope"] not in scope["state"]["scopes"]
    scopes = [rt._node_judgement_arm_scope() for _ in range(20)]
    assert len(scope["state"]["scopes"]) == 16
    assert sum(bool(s["bad"]) for s in scopes) == 4


def test_real_monitor_handler_sees_origin_and_preserves_original_payload(tmp_path):
    from alerts import setup_logging
    rt, *_ = fixture(enabled=True)
    logger = setup_logging(str(tmp_path / "monitor.log"))
    rt.logger = logger
    captured = []
    class Capture(logging.Handler):
        def emit(self, record):
            if getattr(record, "event_key", None) == "routing.runtime_recovery":
                captured.append(record.runtime_recovery_event)
    capture = Capture(); logger.addHandler(capture)
    ticket = NS(incident_id="post-dial-private.example", route_epoch=3)
    try:
        rt._log_runtime_recovery_phase("terminal", ticket=ticket, reason_code="post_dial_native_rebind")
        rt._log_runtime_recovery_phase("terminal", reason_code="private.example")
        drain(rt)
        for handler in logger.handlers: handler.flush()
        text = (tmp_path / "monitor.log").read_text(encoding="utf-8")
        assert "origin=post_dial" in text and "origin=recovery" in text
        assert "private.example" not in text
        assert captured[0]["phase"] == "terminal" and captured[0]["route_epoch"] == 3
        assert captured[0]["reason_code"] == "post_dial_native_rebind"
        assert captured[0]["origin"] == "post_dial"
    finally:
        for handler in tuple(logger.handlers):
            handler.close(); logger.removeHandler(handler)


def test_observer_rules_use_existing_reads_and_dynamic_values_are_info_only():
    runner = object.__new__(PostDialObserverRunner)
    modes = iter([("act", frozenset({"FAST"})), ("act", frozenset({"FAST", "AGED"}))])
    count = []
    runner._mode = lambda: count.append(1) or next(modes)
    runner._perf = lambda: 1.0
    runner._act_rules = None
    runner._core_lock = threading.RLock()
    actual = []
    runner.core = NS(set_act_rules=lambda rules: actual.append(rules))
    runner._diagnostics_policy_revision = 0
    runner._diagnostics_policy_value = None
    rows = []
    runner._diagnostics_emit = lambda level, record: rows.append(record)
    runner._safe_emit = lambda *_args: pytest.fail("new policy must use diagnostics callback")
    assert runner._read_mode() and runner._read_mode()
    assert len(count) == 2 and len(actual) == 2
    assert rows[0]["actual_rules"] == ("FAST",)
    assert rows[1]["actual_rules"] == ("AGED", "FAST")
    assert all(row["ev"] == "observer_policy" for row in rows)


def test_product_file_newlines_remain_original():
    root = Path(module.__file__).parent
    for name in ("desktop_runtime.py", "runtime_post_dial.py", "runtime_post_dial_judge.py", "runtime_post_dial_observer.py"):
        data = (root / name).read_bytes()
        # .gitattributes checks out LF everywhere; a file must not mix styles.
        assert data.count(b"\r\n") in (0, data.count(b"\n"))


@pytest.mark.parametrize("off_kind", ["judge", "disabled", "recovery"])
def test_existing_mode_off_read_discards_queue_without_an_extra_policy_read(off_kind):
    rt, *_ = fixture(enabled=True)
    records = collector(rt)
    rt._node_judgement_arm_kick = lambda state: None
    scope = rt._node_judgement_arm_scope()
    rt._node_judgement_arm_submit({"ev": "admission", "reason": "not_armed"}, scope)
    state, first_instance = scope["state"], scope["instance"]
    assert state["queue"]
    recovery = rt.config["client"]["egress_recovery"]
    flags = recovery["node_judgement"]
    if off_kind == "judge": flags["post_dial_judge"] = "off"
    elif off_kind == "disabled": flags["enabled"] = False
    else: recovery["enabled"] = False
    original_policy, reads = rt._node_judgement_policy, []
    def policy():
        reads.append(1)
        return original_policy()
    rt._node_judgement_policy = policy
    assert rt._runtime_post_dial_mode() == ("off", frozenset())
    assert len(reads) == 1
    assert not state["enabled"] and not state["queue"] and not state["scopes"]
    assert scope["bad"]
    rt._node_judgement_arm_retire(scope)
    rt._node_judgement_arm_drain(state)
    assert records == []
    # Node judgement alone remains enabled for recovery=off; a diagnostic
    # attempt must not silently undo the off boundary already observed.
    rt._node_judgement_arm_submit({"ev": "arm_state", "armed": True})
    assert records == [] and not state["enabled"]
    flags.update(enabled=True, post_dial_judge="act")
    recovery["enabled"] = True
    before = len(reads)
    assert rt._runtime_post_dial_mode()[0] == "act"
    assert len(reads) == before + 1
    rt._node_judgement_arm_submit({"ev": "arm_state", "armed": True})
    rt._node_judgement_arm_drain(state)
    assert state["instance"] != first_instance
    assert not any(r.get("scope") == scope["scope"] for r in records)
    assert any(r["ev"] == "arm_diagnostics_state" and r["state"] == "started" for r in records)


def test_fence_has_its_own_sample_clock_and_preserves_scope_cutoff(monkeypatch):
    rt, *_ = fixture(enabled=True)
    records = collector(rt)
    rt._node_judgement_arm_kick = lambda state: None
    clocks = [100.0, 100_000_000_000]
    monkeypatch.setattr(module.time, "time", lambda: clocks[0])
    monkeypatch.setattr(module.time, "monotonic_ns", lambda: clocks[1])
    scope = rt._node_judgement_arm_scope()
    rt._node_judgement_arm_submit({"ev": "admission", "reason": "not_armed"}, scope)
    clocks[:] = [101.0, 101_000_000_000]
    rt._node_judgement_arm_retire(scope)
    clocks[:] = [102.0, 102_000_000_000]
    rt._node_judgement_arm_submit({"ev": "arm_state", "armed": False})
    clocks[:] = [103.0, 103_000_000_000]
    rt._node_judgement_arm_drain(scope["state"])
    fence = next(r for r in records if r["ev"] == "admission_fence")
    after_cut = next(r for r in records if r["ev"] == "arm_state")
    assert fence["seq"] > after_cut["seq"]
    assert fence["t"] == 103.0 and fence["mono_ns"] == 103_000_000_000
    assert fence["cut_t"] == 101.0 and fence["cut_mono_ns"] == 101_000_000_000
    assert fence["complete"] and fence["final_produced_seq"] == 1


@pytest.mark.parametrize("diagnostics", [False, True])
def test_started_legacy_line_and_same_read_context_bare_runner_compatibility(diagnostics):
    rt, *_ = fixture(enabled=True)
    records = collector(rt)
    rt._node_judgement_arm_kick = lambda state: None
    scope = rt._node_judgement_arm_scope() if diagnostics else None
    runner = object.__new__(PostDialObserverRunner)
    values = iter([("shadow", frozenset({"FAST"})), ("shadow", frozenset({"FAST", "AGED"}))])
    reads = []
    runner._mode = lambda: reads.append(1) or next(values)
    runner._emit = lambda level, row: rt._node_judgement_arm_observer_emit(level, row, scope)
    runner._diagnostics_emit = runner._emit if diagnostics else None
    runner._on_diagnostics_retired = (lambda: rt._node_judgement_arm_retire(scope)) if diagnostics else None
    runner._stopping = threading.Event()
    runner._state_lock = threading.RLock()
    runner._core_lock = threading.RLock()
    runner._stop_reason = ""
    runner._calls_since_summary = 0
    runner.metrics = {"controller_calls": 0}
    runner._client = None
    runner.tap = NS(close=lambda: None)
    runner._load_origin_rules = lambda: True
    runner._perf = lambda: 1.0
    runner._act_rules = None
    runner.core = NS(set_act_rules=lambda rules: None)
    runner._diagnostics_policy_revision = 0
    runner._diagnostics_policy_value = None
    runner._loop = lambda: "fixture" if runner._read_mode() else "off"
    runner._run()
    if diagnostics: rt._node_judgement_arm_drain(scope["state"])
    assert len(reads) == 2  # the same two original mode read sites
    starts = [r for r in records if r["ev"] == "observer_started"]
    assert starts == [{"ev": "observer_started", "mode": "shadow"}]
    assert "Post-dial observer observer_started (" + format_observer_record({"mode": starts[0]["mode"]}) + ")" == "Post-dial observer observer_started (mode=shadow)"
    contexts = [r for r in records if r["ev"] == "observer_started_context"]
    policies = [r for r in records if r["ev"] == "observer_policy"]
    if diagnostics:
        assert len(contexts) == len(policies) == 1
        assert contexts[0]["act_rules"] == ("FAST",)
        assert policies[0]["actual_rules"] == ("AGED", "FAST")
        assert contexts[0]["scope_seq"] == 1
        fence = next(r for r in records if r["ev"] == "admission_fence")
        assert fence["complete"] and fence["final_produced_seq"] == 3
    else:
        assert not contexts and not policies
        assert [r["ev"] for r in records] == ["observer_started", "observer_stopped"]
    stopped = next(r for r in records if r["ev"] == "observer_stopped")
    fields = dict(stopped); fields.pop("ev")
    assert format_observer_record(fields).startswith("reason=fixture controller_calls=0")


@pytest.mark.parametrize("mode", ["shadow", "act"])
def test_variant_mode_stays_last_and_original_trigger_time_is_preserved(mode):
    rt, *_ = fixture(enabled=True)
    records = collector(rt)
    rt._node_judgement_arm_kick = lambda state: None
    scope = rt._node_judgement_arm_scope()
    original = {"ev": "would_trigger", "t": 17.25, "rule": "FAST", "leaf": "hashed-leaf",
                "episode": 2, "opens": "share_0_5", "variant_only": True, "mode": mode}
    rt._node_judgement_arm_observer_emit("info", original, scope)
    row = next(r for r in records if r["ev"] == "would_trigger")
    assert all(row[key] == value for key, value in original.items())
    fields = dict(row); fields.pop("ev")
    rendered = "Post-dial observer would_trigger (" + format_observer_record(fields) + ")"
    assert "opens=share_0_5 " in rendered and rendered.endswith(f"mode={mode})")
    assert original["t"] == row["t"] == 17.25


def test_diagnostic_callback_exception_does_not_change_existing_mode_application():
    runner = object.__new__(PostDialObserverRunner)
    runner._mode = lambda: ("act", frozenset({"FAST"}))
    runner._perf = lambda: 1.0
    runner._act_rules = None
    runner._core_lock = threading.RLock()
    applied = []
    runner.core = NS(set_act_rules=lambda rules: applied.append(rules))
    runner._diagnostics_policy_revision = 0
    runner._diagnostics_policy_value = None
    def failure(*args): raise OSError("controlled diagnostic sink failure")
    runner._diagnostics_emit = failure
    assert runner._read_mode() is True
    assert applied == [frozenset({"FAST"})]


class _PausedProducer:
    """Event-only boundary; even swallowed diagnostic exceptions fail the test."""
    def __init__(self, operation):
        self.entered, self.release = threading.Event(), threading.Event()
        self.errors, self.results = [], []
        def run():
            try: self.results.append(operation())
            except BaseException as exc: self.errors.append(exc)
        self.thread = threading.Thread(target=run, name="f01-paused-producer")

    def checkpoint(self):
        if threading.current_thread() is self.thread:
            self.entered.set()
            if not self.release.wait(5):
                error = AssertionError("diagnostic checkpoint timed out")
                self.errors.append(error)
                raise error

    def __enter__(self):
        self.thread.start()
        try:
            assert self.entered.wait(5), "producer never reached the diagnostic boundary"
        except BaseException:
            self.release.set(); self.thread.join(5)
            raise
        return self

    def __exit__(self, *exc):
        self.release.set(); self.thread.join(5)
        assert not self.thread.is_alive(), "controlled producer leaked"
        if self.errors: raise self.errors[0]


def _diagnostic_cycle(rt):
    flags = rt.config["client"]["egress_recovery"]["node_judgement"]
    flags["post_dial_judge"] = "off"
    assert rt._runtime_post_dial_mode()[0] == "off"
    flags["post_dial_judge"] = "act"
    assert rt._runtime_post_dial_mode()[0] == "act"
    ticket = rt._node_judgement_arm_permit()
    assert ticket is not None
    return ticket


def _metadata(state):
    with state["lock"]:
        return (state["instance"], state["seq"], state["loss"], dict(state["arm"]),
                state["refusal"], dict(state["cache"]), state["suppressed"],
                tuple(state["queue"]), tuple(state["scopes"]))


def test_mr2_prepared_old_row_cannot_enqueue_after_off_on():
    rt, *_ = fixture(enabled=True)
    records = collector(rt)
    rt._node_judgement_arm_kick = lambda state: None
    scope = rt._node_judgement_arm_scope()
    old_prepare = rt._node_judgement_arm_prepare
    pause = _PausedProducer(lambda: rt._node_judgement_arm_submit({"ev": "admission"}, scope))
    def prepare(*args, **kwargs):
        result = old_prepare(*args, **kwargs)
        pause.checkpoint()
        return result
    rt._node_judgement_arm_prepare = prepare
    with pause:
        ticket = _diagnostic_cycle(rt)
        pristine = _metadata(ticket[0])
    assert _metadata(ticket[0]) == pristine
    assert scope["bad"] and not ticket[0]["queue"]
    rt._node_judgement_arm_submit({"ev": "arm_state", "armed": False}, ticket=ticket)
    rt._node_judgement_arm_drain(ticket[0])
    assert [r["seq"] for r in records] == [1, 2]
    assert {r["instance"] for r in records} == {ticket[2]}


@pytest.mark.parametrize("existing", [False, True])
def test_mr2_stale_true_policy_result_does_not_undo_observed_off(existing):
    rt, *_ = fixture(enabled=True)
    records = collector(rt)
    if existing: rt._node_judgement_arm_permit()
    reads, result = [], []
    original_policy = rt._node_judgement_policy
    pause = _PausedProducer(lambda: result.append(rt._node_judgement_arm_permit()))
    def policy():
        value = original_policy(); reads.append(value.observer_enabled)
        pause.checkpoint()
        return value
    rt._node_judgement_policy = policy
    with pause:
        rt.config["client"]["egress_recovery"]["node_judgement"]["post_dial_judge"] = "off"
        assert rt._runtime_post_dial_mode()[0] == "off"
        generation = rt._node_judgement_arm_lifecycle["generation"]
    assert result == [None] and reads == [True, False]
    assert rt._node_judgement_arm_lifecycle["generation"] == generation
    state = rt.__dict__.get("_node_judgement_arm_diagnostics_state")
    assert (state is not None and not state["enabled"] and state["worker"] is None) if existing else state is None
    assert records == []


@pytest.mark.parametrize("off_kind", ["judge", "recovery"])
def test_mr2_old_mode_on_notification_cannot_cross_an_off_read(off_kind):
    rt, *_ = fixture(enabled=True)
    collector(rt)
    old = rt._node_judgement_arm_permit()
    observed = rt._node_judgement_arm_mode_observed
    pause = _PausedProducer(rt._runtime_post_dial_mode)
    def notify(enabled, read_token=None):
        pause.checkpoint()
        return observed(enabled, read_token)
    rt._node_judgement_arm_mode_observed = notify
    recovery = rt.config["client"]["egress_recovery"]
    with pause:
        if off_kind == "judge": recovery["node_judgement"]["post_dial_judge"] = "off"
        else: recovery["enabled"] = False
        assert rt._runtime_post_dial_mode()[0] == "off"
    # The original old on return is preserved, but its diagnostics stay revoked.
    assert pause.results[0][0] == "act"
    assert not old[0]["enabled"] and old[0]["worker"] is None
    recovery["enabled"] = True
    recovery["node_judgement"]["post_dial_judge"] = "act"
    assert rt._runtime_post_dial_mode()[0] == "act"
    new = rt._node_judgement_arm_permit()
    assert new[2] != old[2] and new[1] != old[1]


@pytest.mark.parametrize("boundary", ["predicate", "prepare"])
def test_mr2_old_arm_sample_cannot_suppress_first_equal_new_state(boundary):
    rt, *_ = fixture(enabled=True)
    records = collector(rt)
    rt._node_judgement_arm_kick = lambda state: None
    rt._runtime_route_epoch += 1
    pause = _PausedProducer(lambda: rt._node_judgement_arm_observe("publish"))
    original = rt._runtime_recovery_is_armed if boundary == "predicate" else rt._node_judgement_arm_prepare
    def delayed(*args, **kwargs):
        value = original(*args, **kwargs)
        pause.checkpoint()
        return value
    if boundary == "predicate": rt._runtime_recovery_is_armed = delayed
    else: rt._node_judgement_arm_prepare = delayed
    with pause:
        ticket = _diagnostic_cycle(rt)
        pristine = _metadata(ticket[0])
    assert _metadata(ticket[0]) == pristine
    rt._node_judgement_arm_observe("publish")
    rt._node_judgement_arm_drain(ticket[0])
    rows = [r for r in records if r["ev"] == "arm_state"]
    assert len(rows) == 1 and rows[0]["check"] == "route_epoch" and rows[0]["armed"] is False
    assert rows[0]["instance"] == ticket[2] and rows[0]["since_ms"] == 0.0


def test_mr2_captured_bool_without_original_ticket_does_not_get_new_identity():
    rt, *_ = fixture(enabled=True)
    records = collector(rt)
    rt._node_judgement_arm_observe("admission", armed=False, check="route_epoch", captured=True)
    assert records == [] and "_node_judgement_arm_diagnostics_state" not in rt.__dict__


def test_mr2_old_refusal_sample_cannot_mutate_new_rate_metadata(monkeypatch):
    rt, *_ = fixture(enabled=True)
    collector(rt)
    rt._node_judgement_arm_kick = lambda state: None
    ticket = rt._node_judgement_arm_permit()
    original_clock = module.time.monotonic
    pause = _PausedProducer(lambda: rt._node_judgement_arm_try_result(False,
        {"refusal": "snapshot_missing"}, ticket=ticket))
    def clock():
        value = original_clock(); pause.checkpoint(); return value
    monkeypatch.setattr(module.time, "monotonic", clock)
    with pause:
        new = _diagnostic_cycle(rt)
        pristine = _metadata(new[0])
    assert _metadata(new[0]) == pristine and new[0]["refusal"] is None


@pytest.mark.parametrize("cached", [False, True])
def test_mr2_act_cache_and_admission_keep_the_pre_gate_ticket(cached):
    rt, *_ = fixture(enabled=True)
    records = collector(rt)
    rt._node_judgement_arm_kick = lambda state: None
    scope = rt._node_judgement_arm_scope()
    rt._node_judgement_arm_local = threading.local()
    trigger = NS(leaf="private-leaf", kind="FAST", episode=4)
    noted, remembered = [], []
    judge = NS(blocked_cheap=lambda *_: None, tokenizer=PostDialTokenizer(b"fixed-salt-at-least-16-bytes"),
               remember_gate=lambda *args: remembered.append(args),
               note_gated=lambda *args, **kw: noted.append((args, kw)))
    rt._runtime_post_dial_judge = lambda: judge
    def operation():
        rt._node_judgement_arm_local.scope = scope
        return rt._start_runtime_post_dial_judgement_act(trigger)
    pause = _PausedProducer(operation)
    def recent(*args):
        if cached: pause.checkpoint()
        return cached, "not_armed" if cached else None
    judge.recent_gate = recent
    def admission(*args, **kwargs):
        rt._node_judgement_arm_trace("admission_arm_check", "route_epoch")
        pause.checkpoint()
        return None, "not_armed"
    rt._runtime_post_dial_admission = admission
    with pause:
        new = _diagnostic_cycle(rt)
        pristine = _metadata(new[0])
    assert pause.results == [False]
    assert len(noted) == 1 and noted[0][0] == (trigger, "not_armed")
    assert len(remembered) == (0 if cached else 1)
    assert _metadata(new[0]) == pristine and new[0]["cache"] == {}
    assert records == [] and scope["bad"]


@pytest.mark.parametrize("dispatch", [False, True])
def test_mr2_original_sync_record_survives_revocation_and_old_ack_is_isolated(dispatch):
    rt, *_ = fixture(enabled=True)
    rows = collector(rt)
    rt._node_judgement_arm_kick = lambda state: None
    scope = rt._node_judgement_arm_scope()
    row = {"ev": "trigger", "t": 3.0, "rule": "FAST", "leaf": "hashed", "episode": 9, "mode": "act"}
    pause = _PausedProducer(lambda: rt._node_judgement_arm_observer_emit("info", row, scope))
    if dispatch:
        sink = rt._log_runtime_post_dial_record
        def log(level, record):
            pause.checkpoint(); return sink(level, record)
        rt._log_runtime_post_dial_record = log
    else:
        prepare = rt._node_judgement_arm_prepare
        def before_dispatch(*args, **kwargs):
            result = prepare(*args, **kwargs); pause.checkpoint(); return result
        rt._node_judgement_arm_prepare = before_dispatch
    with pause:
        new = _diagnostic_cycle(rt)
        pristine = _metadata(new[0])
    assert _metadata(new[0]) == pristine
    assert len(rows) == 1 and all(rows[0][k] == v for k, v in row.items())
    if dispatch: assert rows[0]["instance"] == scope["instance"] != new[2]
    else: assert "instance" not in rows[0] and "seq" not in rows[0]
    assert scope["emitted"] == 0 and scope["bad"]


def test_mr2_old_async_ack_does_not_touch_new_loss_or_sequence():
    rt, *_ = fixture(enabled=True)
    rows = collector(rt)
    rt._node_judgement_arm_kick = lambda state: None
    old = rt._node_judgement_arm_permit()
    rt._node_judgement_arm_submit({"ev": "arm_state", "armed": False}, ticket=old)
    pause = _PausedProducer(lambda: rt._node_judgement_arm_drain(old[0]))
    sink = rt._log_runtime_post_dial_record
    def log(level, record):
        if record["ev"] == "arm_state": pause.checkpoint()
        return sink(level, record)
    rt._log_runtime_post_dial_record = log
    with pause:
        new = _diagnostic_cycle(rt)
        pristine = _metadata(new[0])
    assert _metadata(new[0]) == pristine
    assert len(rows) == 2 and all(r["instance"] == old[2] for r in rows)


def test_mr2_old_fence_cleanup_cannot_remove_colliding_new_scope(monkeypatch):
    rt, *_ = fixture(enabled=True)
    rows = collector(rt)
    rt._node_judgement_arm_kick = lambda state: None
    old_scope = rt._node_judgement_arm_scope()
    rt._node_judgement_arm_retire(old_scope)
    pause = _PausedProducer(lambda: rt._node_judgement_arm_drain(old_scope["state"]))
    sink = rt._log_runtime_post_dial_record
    def log(level, row):
        if row["ev"] == "admission_fence": pause.checkpoint()
        return sink(level, row)
    rt._log_runtime_post_dial_record = log
    with pause:
        new = _diagnostic_cycle(rt)
        ids = iter([bytes.fromhex(old_scope["observer"][3:]), bytes.fromhex(old_scope["scope"][3:])])
        monkeypatch.setattr(module.os, "urandom", lambda size: next(ids))
        new_scope = rt._node_judgement_arm_scope()
        assert new_scope is not old_scope and new_scope["scope"] == old_scope["scope"]
    assert new[0]["scopes"][new_scope["scope"]] is new_scope
    assert len(rows) == 1 and rows[0]["instance"] == old_scope["instance"]


def test_mr2_enqueue_while_worker_finishes_dispatch_is_not_lost():
    rt, *_ = fixture(enabled=True)
    rows = collector(rt)
    original_kick = rt._node_judgement_arm_kick
    rt._node_judgement_arm_kick = lambda state: None
    ticket = rt._node_judgement_arm_permit()
    rt._node_judgement_arm_submit({"ev": "arm_state", "tag": "first"}, ticket=ticket)
    pause = _PausedProducer(lambda: rt._node_judgement_arm_drain(ticket[0]))
    ticket[0]["worker"] = pause.thread
    sink = rt._log_runtime_post_dial_record
    def log(level, row):
        if row.get("tag") == "first": pause.checkpoint()
        return sink(level, row)
    rt._log_runtime_post_dial_record = log
    rt._node_judgement_arm_kick = original_kick
    with pause:
        rt._node_judgement_arm_submit({"ev": "arm_state", "tag": "last"}, ticket=ticket)
        assert ticket[0]["worker"] is pause.thread
    assert [r.get("tag") for r in rows if r["ev"] == "arm_state"] == ["first", "last"]
    assert ticket[0]["worker"] is None and not ticket[0]["queue"]
    # Enqueue after the worker has cleared its slot must start a fresh drain.
    rt._node_judgement_arm_submit({"ev": "arm_state", "tag": "after-exit"}, ticket=ticket)
    drain(rt)
    assert [r.get("tag") for r in rows if r["ev"] == "arm_state"] == ["first", "last", "after-exit"]


def test_mr2_old_start_failure_preserves_new_queue_and_releases_worker_slot(monkeypatch):
    rt, *_ = fixture(enabled=True)
    rows = collector(rt)
    actual_thread = threading.Thread
    starts = []
    original_kick = rt._node_judgement_arm_kick
    rt._node_judgement_arm_kick = lambda state: None
    old = rt._node_judgement_arm_permit()
    rt._node_judgement_arm_submit({"ev": "arm_state", "tag": "old"}, ticket=old)
    pause = _PausedProducer(lambda: original_kick(old[0]))
    class FailedStart:
        def start(self):
            starts.append("failed-old")
            pause.checkpoint()
            raise RuntimeError("controlled start failure")
    def factory(*args, **kwargs):
        if not starts: return FailedStart()
        starts.append("new")
        return actual_thread(*args, **kwargs)
    monkeypatch.setattr(module.threading, "Thread", factory)
    with pause:
        new = _diagnostic_cycle(rt)
        rt._node_judgement_arm_submit({"ev": "arm_state", "tag": "new"}, ticket=new)
        assert new[0]["worker"] is not None and new[0]["queue"]
    drain(rt)
    assert starts == ["failed-old", "new"]
    assert [r.get("tag") for r in rows if r["ev"] == "arm_state"] == ["new"]
    assert all(r["instance"] == new[2] for r in rows)
    assert new[0]["loss"] == 0 and new[0]["worker"] is None and not new[0]["queue"]


class _ObservedDiagnosticEvent:
    """Observe real Event waits without injecting a timer or idle poll."""
    def __init__(self, state):
        self.state = state
        self.inner = threading.Event()
        self.waits = []
        self.entered = [threading.Event() for _ in range(12)]
        self.before_first_wait = None
        self.errors = []

    def set(self):
        if not self.state["lock"].locked(): self.errors.append("set outside diagnostic lock")
        self.inner.set()

    def clear(self):
        if not self.state["lock"].locked(): self.errors.append("clear outside diagnostic lock")
        self.inner.clear()

    def wait(self, *args, **kwargs):
        if args or kwargs: self.errors.append("worker requested a timed wait")
        index = len(self.waits)
        self.waits.append((args, kwargs))
        if index < len(self.entered): self.entered[index].set()
        if index == 0 and self.before_first_wait is not None:
            self.before_first_wait()
        return self.inner.wait()


def _p2_source(rt):
    scope = rt._node_judgement_arm_scope()
    assert scope is not None
    return scope


def _p2_context(rt, scope):
    rt._node_judgement_arm_observer_emit("info", {
        "ev": "observer_started_context", "mode": "act", "act_rules": ("FAST",)}, scope)


def _p2_cleanup(rt):
    # Explicit final fixture shutdown, never used as a substitute for an
    # assertion about a live source or an in-flight registered producer.
    rt.config["client"]["egress_recovery"]["node_judgement"]["post_dial_judge"] = "off"
    rt._runtime_post_dial_mode()
    drain(rt)


def test_p2_live_source_reuses_one_worker_and_waits_without_timeout_or_policy_reads():
    rt, *_ = fixture(enabled=True)
    rows = collector(rt)
    scope = _p2_source(rt)
    state = scope["state"]
    wake = state["wakeup"] = _ObservedDiagnosticEvent(state)
    reads = []
    original_policy = rt._node_judgement_policy
    rt._node_judgement_policy = lambda: reads.append(1) or original_policy()
    try:
        _p2_context(rt, scope)
        rt._node_judgement_arm_submit({"ev": "arm_state", "tag": "first-async"}, ticket=scope["ticket"])
        assert wake.entered[0].wait(5)
        worker = state["worker"]
        assert worker is not None and worker.is_alive() and not state["queue"]
        assert scope["source_started"] and scope["emitted"] == 1
        at_idle = len(reads)
        rt._node_judgement_arm_submit({"ev": "arm_state", "tag": "second"}, ticket=scope["ticket"])
        assert wake.entered[1].wait(5)
        assert state["worker"] is worker and len(reads) == at_idle
        assert any(r.get("tag") == "second" for r in rows)
        rt._node_judgement_arm_retire(scope)
        worker.join(5)
        assert not worker.is_alive() and state["worker"] is None
        fence = next(r for r in rows if r["ev"] == "admission_fence")
        assert fence["complete"] and fence["final_produced_seq"] == 1
        assert wake.waits == [((), {}), ((), {})] and wake.errors == []
    finally:
        _p2_cleanup(rt)


@pytest.mark.parametrize("kind", ["no_scope", "unstarted", "overflow"])
def test_p2_no_started_tracked_source_still_drains_and_exits(kind):
    rt, *_ = fixture(enabled=True)
    rows = collector(rt)
    ticket = rt._node_judgement_arm_permit()
    state = ticket[0]
    wake = state["wakeup"] = _ObservedDiagnosticEvent(state)
    try:
        if kind == "overflow":
            tracked = [_p2_source(rt) for _ in range(16)]
            overflow = _p2_source(rt)
            assert overflow["bad"] and overflow["scope"] not in state["scopes"]
            _p2_context(rt, overflow)
            assert overflow["source_started"]
            assert all(not scope["source_started"] for scope in tracked)
            rt._node_judgement_arm_submit({"ev": "arm_state", "tag": "overflow-async"}, ticket=ticket)
        else:
            if kind == "unstarted":
                scope = _p2_source(rt)
                assert not scope["source_started"] and not scope["retired"]
            rt._node_judgement_arm_submit({"ev": "route_epoch", "site": "fixture"}, ticket=ticket)
        drain(rt)
        assert state["worker"] is None and not state["queue"] and wake.waits == []
        assert rows and wake.errors == []
    finally:
        _p2_cleanup(rt)


@pytest.mark.parametrize("change", ["enqueue", "off", "off_on"])
def test_p2_clear_to_wait_window_does_not_lose_enqueue_or_generation_notifications(change):
    rt, *_ = fixture(enabled=True)
    rows = collector(rt)
    old_scope = _p2_source(rt)
    state = old_scope["state"]
    wake = state["wakeup"] = _ObservedDiagnosticEvent(state)
    release = threading.Event()
    errors = []
    def hold_before_wait():
        if not release.wait(5): errors.append("test did not release Event.wait boundary")
    wake.before_first_wait = hold_before_wait
    new_scope = None
    try:
        _p2_context(rt, old_scope)
        rt._node_judgement_arm_submit({"ev": "arm_state", "tag": "first-async"}, ticket=old_scope["ticket"])
        assert wake.entered[0].wait(5)
        worker = state["worker"]
        if change == "enqueue":
            rt._node_judgement_arm_submit({"ev": "arm_state", "tag": "arrived-after-clear"},
                                           ticket=old_scope["ticket"])
        else:
            rt.config["client"]["egress_recovery"]["node_judgement"]["post_dial_judge"] = "off"
            assert rt._runtime_post_dial_mode()[0] == "off"
            if change == "off_on":
                rt.config["client"]["egress_recovery"]["node_judgement"]["post_dial_judge"] = "act"
                assert rt._runtime_post_dial_mode()[0] == "act"
                new_scope = _p2_source(rt)
                _p2_context(rt, new_scope)
                assert new_scope["instance"] != old_scope["instance"]
        release.set()
        if change == "off":
            worker.join(5)
            assert not worker.is_alive() and state["worker"] is None
        else:
            assert wake.entered[1].wait(5)
            assert state["worker"] is worker
            if change == "enqueue":
                assert any(r.get("tag") == "arrived-after-clear" for r in rows)
            else:
                assert new_scope["source_started"] and new_scope["emitted"] == 1
                assert old_scope["scope"] not in state["scopes"] and old_scope["bad"]
                assert not any(r["ev"] == "admission_fence" and r["scope"] == old_scope["scope"] for r in rows)
            rt._node_judgement_arm_retire(new_scope if new_scope is not None else old_scope)
            worker.join(5)
            assert not worker.is_alive()
        assert errors == [] and wake.errors == []
    finally:
        release.set()
        _p2_cleanup(rt)


def test_p2_source_retirement_waits_for_the_last_real_registered_producer():
    rt, *_ = fixture(enabled=True)
    rows = collector(rt)
    scope = _p2_source(rt)
    state = scope["state"]
    wake = state["wakeup"] = _ObservedDiagnosticEvent(state)
    pause = _PausedProducer(lambda: rt._node_judgement_arm_trigger(scope, NS(), "act", None))
    rt._on_runtime_post_dial_trigger = lambda *args, **kwargs: pause.checkpoint()
    try:
        _p2_context(rt, scope)
        rt._node_judgement_arm_submit({"ev": "arm_state", "tag": "first-async"}, ticket=scope["ticket"])
        assert wake.entered[0].wait(5)
        worker = state["worker"]
        with pause:
            assert scope["active"] == 1
            rt._node_judgement_arm_retire(scope)
            assert wake.entered[1].wait(5)
            assert state["worker"] is worker and worker.is_alive()
            assert not any(r["ev"] == "admission_fence" for r in rows)
        worker.join(5)
        assert not worker.is_alive() and scope["active"] == 0
        fence = next(r for r in rows if r["ev"] == "admission_fence")
        assert fence["complete"] and fence["inflight_before_cut"] == 0 and wake.errors == []
    finally:
        pause.release.set()
        _p2_cleanup(rt)


def test_p2_replacement_source_keeps_worker_until_both_scopes_retire():
    rt, *_ = fixture(enabled=True)
    rows = collector(rt)
    first = _p2_source(rt)
    state = first["state"]
    wake = state["wakeup"] = _ObservedDiagnosticEvent(state)
    try:
        _p2_context(rt, first)
        rt._node_judgement_arm_submit({"ev": "arm_state", "tag": "first-async"}, ticket=first["ticket"])
        assert wake.entered[0].wait(5)
        worker = state["worker"]
        second = _p2_source(rt)
        _p2_context(rt, second)
        rt._node_judgement_arm_submit({"ev": "arm_state", "tag": "second-async"}, ticket=second["ticket"])
        assert wake.entered[1].wait(5)
        rt._node_judgement_arm_retire(first)
        assert wake.entered[2].wait(5)
        assert state["worker"] is worker and worker.is_alive()
        assert any(r["ev"] == "admission_fence" and r["scope"] == first["scope"] for r in rows)
        assert not any(r["ev"] == "admission_fence" and r["scope"] == second["scope"] for r in rows)
        rt._node_judgement_arm_retire(second)
        worker.join(5)
        assert not worker.is_alive() and state["scopes"] == {}
        assert len([r for r in rows if r["ev"] == "admission_fence" and r["complete"]]) == 2
        assert wake.errors == []
    finally:
        _p2_cleanup(rt)


def test_p2_scope_with_start_failure_can_resume_but_cannot_claim_lossless_fence(monkeypatch):
    rt, *_ = fixture(enabled=True)
    rows = collector(rt)
    scope = _p2_source(rt)
    state = scope["state"]
    wake = state["wakeup"] = _ObservedDiagnosticEvent(state)
    original_thread = module.threading.Thread
    attempts = []
    class Failed:
        def start(self):
            attempts.append(1)
            raise RuntimeError("controlled first start failure")
    def factory(*args, **kwargs):
        return Failed() if not attempts else original_thread(*args, **kwargs)
    monkeypatch.setattr(module.threading, "Thread", factory)
    try:
        _p2_context(rt, scope)
        assert not attempts and scope["emitted"] == 1
        rt._node_judgement_arm_observer_emit("info", {"ev": "observer_policy",
            "policy_revision": 1, "actual_rules": ("FAST",)}, scope)
        assert state["worker"] is None and state["loss"] == 2 and scope["loss"] == 1
        rt._node_judgement_arm_submit({"ev": "arm_state", "tag": "next-event"}, ticket=scope["ticket"])
        assert wake.entered[0].wait(5)
        worker = state["worker"]
        rt._node_judgement_arm_retire(scope)
        worker.join(5)
        assert not worker.is_alive()
        fence = next(r for r in rows if r["ev"] == "admission_fence")
        assert not fence["complete"] and fence["loss_total"] == 1 and wake.errors == []
    finally:
        _p2_cleanup(rt)


@pytest.mark.parametrize("following", ["policy", "producer_finally", "retire"])
def test_r5_cold_context_is_synchronous_and_existing_followup_flushes_original_marker(monkeypatch, following):
    rt, *_ = fixture(enabled=True)
    rows = collector(rt)
    scope = _p2_source(rt)
    state = scope["state"]
    caller = threading.current_thread()
    contexts, constructions = [], []
    marker_emitted = threading.Event()
    sink = rt._log_runtime_post_dial_record
    actual_thread = module.threading.Thread
    def factory(*args, **kwargs):
        constructions.append(kwargs.get("name"))
        return actual_thread(*args, **kwargs)
    monkeypatch.setattr(module.threading, "Thread", factory)
    def emit(level, row):
        if row["ev"] == "observer_started_context":
            assert state["lock"].acquire(blocking=False)
            state["lock"].release()
            contexts.append(threading.current_thread())
        sink(level, row)
        if row["ev"] == "arm_diagnostics_state": marker_emitted.set()
    rt._log_runtime_post_dial_record = emit
    try:
        _p2_context(rt, scope)
        assert contexts == [caller] and constructions == [] and state["worker"] is None
        assert scope["source_started"] and not scope["retired"]
        assert scope["produced"] == scope["emitted"] == scope["contiguous"] == 1
        assert [r["ev"] for r in rows] == ["observer_started_context"]
        assert rows[0]["scope_seq"] == 1 and rows[0]["seq"] == 2
        assert len(state["queue"]) == 1 and not marker_emitted.is_set()
        marker = dict(state["queue"][0][1])
        assert marker["ev"] == "arm_diagnostics_state" and marker["seq"] == 1
        if following == "policy":
            rt._node_judgement_arm_observer_emit("info", {"ev": "observer_policy",
                "policy_revision": 1, "actual_rules": ("FAST",)}, scope)
        elif following == "producer_finally":
            rt._on_runtime_post_dial_trigger = lambda *args, **kwargs: None
            rt._node_judgement_arm_trigger(scope, NS(), "act", None)
        else:
            rt._node_judgement_arm_retire(scope)
        assert marker_emitted.wait(5)
        if following != "retire": rt._node_judgement_arm_retire(scope)
        drain(rt)
        assert constructions == ["post-dial-arm-diagnostics"]
        assert next(r for r in rows if r["ev"] == "arm_diagnostics_state") == marker
        fence = next(r for r in rows if r["ev"] == "admission_fence")
        expected = 2 if following == "policy" else 1
        assert fence["complete"] and fence["closed"] and fence["loss_total"] == 0
        assert fence["final_produced_seq"] == fence["final_emitted_seq"] == fence["emitted_count"] == expected
        assert state["worker"] is None and state["scopes"] == {} and state["queue"] == []
    finally:
        _p2_cleanup(rt)


@pytest.mark.parametrize("boundary", ["permit", "prepare", "dispatch", "ack"])
def test_r5_context_dispatch_is_cancelled_or_keeps_only_its_old_identity_across_off_on(boundary):
    rt, *_ = fixture(enabled=True)
    rows = collector(rt)
    scope = _p2_source(rt)
    pause = _PausedProducer(lambda: _p2_context(rt, scope))
    if boundary == "permit":
        permit = rt._node_judgement_arm_permit
        def stopped_permit():
            result = permit(); pause.checkpoint(); return result
        rt._node_judgement_arm_permit = stopped_permit
    elif boundary == "prepare":
        prepare = rt._node_judgement_arm_prepare
        def stopped_prepare(*args, **kwargs):
            result = prepare(*args, **kwargs); pause.checkpoint(); return result
        rt._node_judgement_arm_prepare = stopped_prepare
    elif boundary == "dispatch":
        sink = rt._log_runtime_post_dial_record
        def stopped_log(*args, **kwargs):
            pause.checkpoint(); return sink(*args, **kwargs)
        rt._log_runtime_post_dial_record = stopped_log
    else:
        ack = rt._node_judgement_arm_ack
        def stopped_ack(*args, **kwargs):
            pause.checkpoint(); return ack(*args, **kwargs)
        rt._node_judgement_arm_ack = stopped_ack
    try:
        with pause:
            new = _diagnostic_cycle(rt)
            pristine = _metadata(new[0])
        assert _metadata(new[0]) == pristine
        assert scope["bad"] and scope["emitted"] == 0
        if boundary in {"permit", "prepare"}:
            assert rows == []  # no un-enveloped fallback for a new diagnostic
        else:
            assert len(rows) == 1 and rows[0]["ev"] == "observer_started_context"
            assert rows[0]["instance"] == scope["instance"] != new[2]
            assert rows[0]["scope"] == scope["scope"] and rows[0]["scope_seq"] == 1
        assert new[0]["worker"] is None and not new[0]["queue"]
    finally:
        pause.release.set()
        _p2_cleanup(rt)


@pytest.mark.parametrize("exit_path", ["origin_failure", "lease_revoked"])
@pytest.mark.parametrize("sink_failure", [False, True])
def test_r5_actual_runner_without_policy_retires_and_preserves_context_loss(exit_path, sink_failure):
    rt, *_ = fixture(enabled=True)
    rows = collector(rt)
    scope = _p2_source(rt)
    state = scope["state"]
    runner = object.__new__(PostDialObserverRunner)
    calls, contexts, sink_failures = [], [], []
    def mode():
        calls.append("mode"); return "act", frozenset({"FAST"})
    runner._mode = mode
    runner._emit = lambda level, row: rt._node_judgement_arm_observer_emit(level, row, scope)
    runner._diagnostics_emit = runner._emit
    def retired():
        calls.append("retire"); rt._node_judgement_arm_retire(scope)
    runner._on_diagnostics_retired = retired
    runner._stopping = threading.Event()
    runner._state_lock = threading.RLock()
    runner._core_lock = threading.RLock()
    runner._stop_reason = ""
    runner._calls_since_summary = 0
    runner.metrics = {"controller_calls": 0}
    runner._client = None
    runner.tap = NS(close=lambda: calls.append("tap_close"))
    runner.core = NS(constants=NS(poll_fast_s=1.0))
    def origin():
        calls.append("origin"); return exit_path != "origin_failure"
    runner._load_origin_rules = origin
    def is_current():
        calls.append("is_current"); return False
    runner._is_current = is_current
    sink = rt._log_runtime_post_dial_record
    def emit(level, row):
        if row["ev"] in {"observer_started", "observer_started_context", "observer_stopped"}:
            calls.append(row["ev"])
        if row["ev"] == "observer_started_context":
            contexts.append(threading.current_thread())
            assert state["worker"] is None
            assert not runner._state_lock._is_owned() and not runner._core_lock._is_owned()
            assert state["lock"].acquire(blocking=False)
            state["lock"].release()
            if sink_failure:
                sink_failures.append("controlled")
                raise OSError("controlled synchronous context failure")
        sink(level, row)
    rt._log_runtime_post_dial_record = emit
    try:
        caller = threading.current_thread()
        runner._run()
        drain(rt)
        expected_calls = ["mode", "observer_started", "observer_started_context", "origin"]
        if exit_path == "lease_revoked": expected_calls.append("is_current")
        expected_calls += ["tap_close", "observer_stopped", "retire"]
        assert calls == expected_calls and contexts == [caller]
        assert sink_failures == (["controlled"] if sink_failure else [])
        assert [r for r in rows if r["ev"] == "observer_started"] == [{"ev": "observer_started", "mode": "act"}]
        assert not any(r["ev"] == "observer_policy" for r in rows)
        started_context = [r for r in rows if r["ev"] == "observer_started_context"]
        assert len(started_context) == (0 if sink_failure else 1)
        if not sink_failure: assert started_context[0]["act_rules"] == ("FAST",)
        stopped = next(r for r in rows if r["ev"] == "observer_stopped")
        assert stopped["reason"] == ("origin_rules_unavailable" if exit_path == "origin_failure" else "lease_revoked")
        assert stopped["controller_calls"] == 0
        assert any(r["ev"] == "arm_diagnostics_state" and r["seq"] == 1 for r in rows)
        fence = next(r for r in rows if r["ev"] == "admission_fence")
        assert fence["closed"] and fence["final_produced_seq"] == 2
        assert fence["complete"] is (not sink_failure)
        assert fence["loss_total"] == int(sink_failure)
        assert fence["emitted_count"] == (1 if sink_failure else 2)
        assert fence["final_emitted_seq"] == (0 if sink_failure else 2)
        assert scope["retired"] and state["worker"] is None and state["scopes"] == {}
    finally:
        _p2_cleanup(rt)


# R5 schema supplement, approved 2026-10-04. The preceding 1432 lines stay
# byte-identical. Expected fields below are handwritten contract literals.
import re as _schema_re
from collections import Counter as _SchemaCounter


_SCHEMA_E = frozenset({"ev", "v", "t", "mono_ns", "instance", "seq", "loss_total"})
_SCHEMA_S = frozenset({"observer", "scope", "scope_seq"})
_SCHEMA_MARKER = _SCHEMA_E | {"state"}
_SCHEMA_ARM = _SCHEMA_E | {"armed", "check", "site", "route_epoch", "arm_route_epoch", "predicate", "since_ms"}
_SCHEMA_REFUSED = _SCHEMA_E | {"check", "site", "route_epoch", "arm_route_epoch", "suppressed"}
_SCHEMA_EPOCH = _SCHEMA_E | {"site", "old", "new", "armed_before", "armed_before_check", "arm_route_epoch"}
_SCHEMA_RECOVERY = _SCHEMA_E | {"origin", "phase", "incident", "reason"}
_SCHEMA_CONTEXT = _SCHEMA_E | _SCHEMA_S | {"mode", "act_rules"}
_SCHEMA_POLICY = _SCHEMA_E | _SCHEMA_S | {"mode", "actual_rules", "policy_revision"}
_SCHEMA_ADMISSION = _SCHEMA_E | _SCHEMA_S | {
    "mode", "leaf", "rule", "episode", "result", "reason", "predicate",
    "arm_evaluated", "arm_check", "source", "policy_revision",
}
_SCHEMA_FENCE = frozenset({
    "ev", "v", "t", "mono_ns", "seq", "instance", "observer", "scope", "cut_t", "cut_mono_ns",
    "final_produced_seq", "final_emitted_seq", "emitted_count", "loss_total", "inflight_before_cut", "closed", "complete",
})
_SCHEMA_TRIGGER = _SCHEMA_E | _SCHEMA_S | {
    "rule", "leaf", "toks", "n", "first_ev_ms", "episode", "mode", "actual_rules", "policy_revision",
}
_SCHEMA_RATIO = frozenset({"ratio_failed", "ratio_answered"})
_SCHEMA_SHARE = frozenset({"share_failed", "share_answered", "share_ok", "onset_answered", "opens"})
_SCHEMA_OPTIONAL = frozenset({"share_ok_at_0_5", "onset_ok_at_0_5"})
_SCHEMA_GATED = frozenset({"ev", "t", "rule", "leaf", "reason", "episode"})
_SCHEMA_SUMMARY = frozenset({
    "ev", "t", "unknown_share", "poll_gap_p50_ms", "poll_gap_p90_ms", "poll_cost_p50_ms", "poll_cost_p90_ms",
    "poll_total_p50_ms", "poll_total_p90_ms", "reader_lag_p50_ms", "reader_lag_p90_ms",
    "drain_lag_p50_ms", "drain_lag_p90_ms", "tracked", "watches", "cpu_budget_pct", "observer_cpu_pct",
    "observer_poll_cpu_pct", "throttle_episodes", "throttle_changes", "throttle_unreachable", "throttle_scale",
    "throttle_scale_peak", "total_controller_calls", "total_poll_errors", "total_poll_oversize",
    "total_lines_drained", "total_lines_rejected", "total_dormant_waits", "total_raw_polls",
    "total_throttle_episodes", "total_throttled_polls", "total_raw_fast_polls", "total_raw_json_polls",
    "total_raw_parsed", "total_raw_counters", "total_raw_ticks", "total_raw_backoffs", "total_tap_offered", "total_tap_overflow",
})
_SCHEMA_SECRETS = ("private-leaf", "private.example", "_arm_enveloped")


class _SchemaLog:
    """Spy on public formatter input and actual LogRecords, never build rows."""
    def __init__(self, path):
        self.path = path
        self.cv = threading.Condition()
        self.local = threading.local()
        self.pending = {}
        self.rows = []
        self.errors = []
        self.recovery = []
        self.workers = set()
        self.runtimes = []
        self.drops = []
        self.forbidden_calls = []
        self.logger = logging.getLogger("proxy_manager")
        self.saved = (list(self.logger.handlers), list(self.logger.filters), self.logger.level,
                      self.logger.propagate, self.logger.disabled)
        self.original_formatter = module.format_observer_record
        self.installed = False

    def install(self):
        from alerts import setup_logging
        for handler in self.saved[0]: self.logger.removeHandler(handler)
        for item in self.saved[1]: self.logger.removeFilter(item)
        self.logger.disabled = False
        self.installed = True
        self.logger = setup_logging(str(self.path))
        owner = self

        def spy(fields):
            ident = threading.get_ident()
            with owner.cv:
                if ident in owner.pending:
                    owner.errors.append("formatter input overwritten before log dispatch")
                owner.local.fields = dict(fields)
                owner.pending[ident] = True
            try:
                return owner.original_formatter(fields)
            except BaseException as exc:
                with owner.cv:
                    owner.errors.append("formatter exception: " + repr(exc))
                raise

        class Capture(logging.Handler):
            def emit(self, record):
                try:
                    message = record.getMessage()
                    with owner.cv:
                        if getattr(record, "event_key", None) == "routing.runtime_recovery":
                            owner.recovery.append(dict(record.runtime_recovery_event))
                        if not message.startswith("Post-dial observer "):
                            return
                        match = _schema_re.fullmatch(r"Post-dial observer ([^ ]+) \((.*)\)", message)
                        if match is None:
                            owner.errors.append("unparseable observer message: " + message)
                            return
                        ident = threading.get_ident()
                        fields = getattr(owner.local, "fields", None)
                        if ident not in owner.pending or fields is None:
                            owner.errors.append("log dispatch has no same-thread formatter input")
                            return
                        owner.pending.pop(ident)
                        del owner.local.fields
                        tokens = {}
                        for token in match[2].split():
                            if "=" not in token:
                                owner.errors.append("non key=value token: " + token)
                                continue
                            key, value = token.split("=", 1)
                            if key in tokens: owner.errors.append("duplicate rendered field: " + key)
                            tokens[key] = value
                        owner.rows.append(NS(raw={"ev": match[1], **fields}, tokens=tokens, message=message))
                        if threading.current_thread() is not threading.main_thread():
                            owner.workers.add(threading.current_thread())
                        owner.cv.notify_all()
                except BaseException as exc:
                    with owner.cv:
                        owner.errors.append("capture exception: " + repr(exc))
                        owner.cv.notify_all()

        self.spy = spy
        module.format_observer_record = spy
        self.capture = Capture()
        self.logger.addHandler(self.capture)

    def attach(self, rt):
        rt.config["client"]["egress_recovery"]["node_judgement"].update(enabled=True, post_dial_judge="act")
        rt.logger = self.logger
        self.runtimes.append(rt)
        def forbidden(*args, **kwargs):
            self.forbidden_calls.append((args, kwargs))
            raise AssertionError("schema diagnostics attempted an external controller")
        rt.mihomo_proxies = forbidden
        rt._mihomo_proxies_bounded = forbidden
        rt._runtime_post_dial_controller = forbidden
        return rt

    def fail_once(self, rt, event):
        original = rt._log_runtime_post_dial_record
        def fail(level, record):
            if record.get("ev") == event:
                rt._log_runtime_post_dial_record = original
                with self.cv:
                    self.drops.append(event)
                    self.cv.notify_all()
                raise OSError("schema controlled sink loss")
            return original(level, record)
        rt._log_runtime_post_dial_record = fail

    def settle(self, rt):
        # No source is retired by this helper. Only the lifecycle/fence test's
        # real retirement may make a started source eligible to stop.
        state = rt.__dict__.get("_node_judgement_arm_diagnostics_state")
        with self.cv: workers = set(self.workers)
        if state is not None:
            with state["lock"]:
                assert not any(s["source_started"] and not s["retired"] for s in state["scopes"].values())
                if state["worker"] is not None: workers.add(state["worker"])
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive(), "schema diagnostic worker did not stop"
        if state is not None:
            with state["lock"]:
                assert state["worker"] is None and not state["queue"]
        self.verify_monitor()

    def verify_monitor(self):
        for handler in self.logger.handlers: handler.flush()
        text = self.path.read_text(encoding="utf-8")
        with self.cv:
            assert self.errors == [], self.errors
            assert self.pending == {}, "unpaired public formatter inputs"
            entries = list(self.rows)
        actual = [line[line.index("Post-dial observer "):] for line in text.splitlines() if "Post-dial observer " in line]
        assert _SchemaCounter(actual) == _SchemaCounter(entry.message for entry in entries)
        assert self.forbidden_calls == []
        for secret in _SCHEMA_SECRETS:
            assert secret not in text
            assert secret not in repr([entry.raw for entry in entries])

    def event(self, event, expected_count=1):
        with self.cv: result = [entry for entry in self.rows if entry.raw["ev"] == event]
        assert len(result) == expected_count, (event, len(result), expected_count)
        return result

    def shutdown(self):
        # Do not pre-retire scopes to satisfy assertions. Explicit off happens
        # only in final cleanup, after the test has consumed its own evidence.
        try:
            for rt in self.runtimes:
                state = rt.__dict__.get("_node_judgement_arm_diagnostics_state")
                worker = None
                if state is not None:
                    with state["lock"]: worker = state["worker"]
                rt.config["client"]["egress_recovery"]["node_judgement"]["post_dial_judge"] = "off"
                rt._runtime_post_dial_mode()
                if worker is not None:
                    worker.join(5)
                    assert not worker.is_alive(), "schema cleanup left its worker running"
            self.verify_monitor()
        finally:
            module.format_observer_record = self.original_formatter
            if self.installed:
                for handler in tuple(self.logger.handlers):
                    self.logger.removeHandler(handler)
                    handler.close()
                for item in tuple(self.logger.filters): self.logger.removeFilter(item)
                handlers, filters, level, propagate, disabled = self.saved
                for handler in handlers: self.logger.addHandler(handler)
                for item in filters: self.logger.addFilter(item)
                self.logger.setLevel(level)
                self.logger.propagate = propagate
                self.logger.disabled = disabled


@pytest.fixture
def schema_log(tmp_path):
    sink = _SchemaLog(tmp_path / "monitor.log")
    try:
        sink.install()
        yield sink
    finally:
        sink.shutdown()


def _schema_assert_record(entry, expected_keys, literal_values=None):
    # These two exact-key assertions are the registered mutation oracles.
    assert set(entry.raw) == expected_keys, "schema raw exact fields"
    assert set(entry.tokens) == expected_keys - {"ev"}, "schema rendered exact fields"
    for key, (raw_value, rendered_value) in (literal_values or {}).items():
        assert entry.raw[key] == raw_value, key
        assert entry.tokens[key] == rendered_value, key
    for key in ("seq", "mono_ns", "scope_seq", "loss_total", "policy_revision"):
        if key in expected_keys:
            assert type(entry.raw[key]) is int and entry.raw[key] >= 0
            assert entry.tokens[key] == str(entry.raw[key])
    if "v" in expected_keys:
        assert entry.raw["v"] == 1 and entry.tokens["v"] == "1"
    for key in ("t", "cut_t", "cut_mono_ns"):
        if key in expected_keys: assert isinstance(entry.raw[key], (int, float)) and entry.raw[key] > 0
    for key, prefix in (("instance", "rt-"), ("observer", "ob-"), ("scope", "sc-")):
        if key in expected_keys:
            value = entry.raw[key]
            assert value == "unknown" or _schema_re.fullmatch(prefix + r"[0-9a-f]{16}", value)
            assert entry.tokens[key] == value
    return entry.raw


def _schema_marker(sink):
    entry, = sink.event("arm_diagnostics_state")
    _schema_assert_record(entry, _SCHEMA_MARKER, {"state": ("started", "started")})


def _schema_judge(rt):
    return PostDialJudgeService(hooks=NS(), tokenizer=PostDialTokenizer(b"f01-schema-oracle-key-32-bytes!!!"),
                               emit=rt._log_runtime_post_dial_record, wall=lambda: 123.456)


def _schema_scope(rt):
    scope = rt._node_judgement_arm_scope()
    assert scope is not None
    local = rt.__dict__.setdefault("_node_judgement_arm_local", threading.local())
    local.scope = scope
    return scope


def _schema_runner(rt, scope=None, mode=None):
    from runtime_post_dial import PostDialObserverCore
    from runtime_post_dial_observer import PostDialTap
    core = PostDialObserverCore(tokenizer=PostDialTokenizer(b"f01-schema-oracle-key-32-bytes!!!"))
    def no_controller():
        raise AssertionError("schema runner must not construct a controller")
    return PostDialObserverRunner(
        tap=PostDialTap(pid=11, process_generation=12), core=core,
        controller_factory=no_controller, is_current=lambda: True,
        mode=mode or (lambda: ("act", frozenset({"FAST"}))), route_snapshot=lambda: None,
        route_context_for=lambda _: None, network_epoch=lambda: (0, None),
        on_trigger=lambda *_: None, emit=lambda level, row: rt._node_judgement_arm_observer_emit(level, row, scope),
        diagnostics_emit=None if scope is None else lambda level, row: rt._node_judgement_arm_observer_emit(level, row, scope),
        on_diagnostics_retired=None if scope is None else lambda: rt._node_judgement_arm_retire(scope),
        wall=lambda: 123.456, perf=lambda: 50.0, monotonic=lambda: 50.0,
    )


@pytest.mark.parametrize("check", ["armed", "no_arm", "route_epoch"])
def test_schema_arm_state_public_and_monitor(schema_log, check):
    rt, *_ = fixture(enabled=True)
    schema_log.attach(rt)
    if check == "no_arm": rt._runtime_recovery_arm = None
    if check == "route_epoch": rt._runtime_route_epoch = 4
    rt._node_judgement_arm_observe("publish")
    schema_log.settle(rt)
    entry, = schema_log.event("arm_state")
    _schema_assert_record(entry, _SCHEMA_ARM, {
        "armed": (check == "armed", "true" if check == "armed" else "false"),
        "check": (check, check), "site": ("publish", "publish"), "predicate": ("codex_arm", "codex_arm"),
        "route_epoch": (4 if check == "route_epoch" else 3, "4" if check == "route_epoch" else "3"),
        "arm_route_epoch": (-1 if check == "no_arm" else 3, "-1" if check == "no_arm" else "3"),
        "since_ms": (0.0, "0.0"),
    })
    _schema_marker(schema_log)


@pytest.mark.parametrize("check,extra", [
    ("facility_hold", frozenset()), ("pre_ready_lease:missing", frozenset()),
    ("installed_then_unarmed:route_epoch", frozenset()),
    ("not_ready", frozenset({"initial_check"})),
    ("incident_active", frozenset({"pending", "recovering", "incidents"})),
])
def test_schema_refused_conditional_facts(schema_log, check, extra):
    rt, kwargs = try_case(check)
    schema_log.attach(rt)
    assert rt._try_arm_runtime_recovery(**kwargs) is False
    schema_log.settle(rt)
    entry, = schema_log.event("arm_refused")
    _schema_assert_record(entry, _SCHEMA_REFUSED | extra, {
        "check": (check, check), "site": ("try_arm", "try_arm"), "suppressed": (0, "0"),
    })
    if check == "not_ready":
        assert entry.raw["initial_check"] == entry.tokens["initial_check"] == "effective_egress_ok_none"
    if check == "incident_active":
        assert entry.raw["pending"] == entry.raw["recovering"] == 1
        assert entry.tokens["pending"] == entry.tokens["recovering"] == "1"
        assert len(entry.raw["incidents"]) == 2
        assert all(_schema_re.fullmatch(r"hmac:[0-9a-f]{24}", value) for value in entry.raw["incidents"])
        assert entry.tokens["incidents"] == ",".join(entry.raw["incidents"])
    if check == "installed_then_unarmed:route_epoch":
        assert (entry.raw["route_epoch"], entry.raw["arm_route_epoch"]) == (4, 3)
        assert (entry.tokens["route_epoch"], entry.tokens["arm_route_epoch"]) == ("4", "3")
    _schema_marker(schema_log)


def test_schema_route_epoch_public_and_monitor(schema_log):
    rt, *_ = fixture(enabled=True)
    schema_log.attach(rt)
    with rt._runtime_route_snapshot_lock:
        rt._runtime_route_epoch += 1
        rt._node_judgement_arm_epoch("schema_epoch")
    schema_log.settle(rt)
    entry, = schema_log.event("route_epoch")
    _schema_assert_record(entry, _SCHEMA_EPOCH, {
        "site": ("schema_epoch", "schema_epoch"), "old": (3, "3"), "new": (4, "4"),
        "armed_before": ("unknown", "unknown"),
        "armed_before_check": ("not_sampled_under_route_lock", "not_sampled_under_route_lock"),
        "arm_route_epoch": (3, "3"),
    })
    _schema_marker(schema_log)


@pytest.mark.parametrize("kind", ["post_dial", "recovery", "no_ticket"])
def test_schema_recovery_origin_and_old_payload(schema_log, kind):
    rt, *_ = fixture(enabled=True)
    schema_log.attach(rt)
    ticket = None if kind == "no_ticket" else NS(incident_id=("post-dial-" if kind == "post_dial" else "ticket-") + "private.example", route_epoch=3)
    reason = {"post_dial": "post_dial_native_rebind", "recovery": "private.example", "no_ticket": ""}[kind]
    rt._log_runtime_recovery_phase("terminal", ticket=ticket, reason_code=reason)
    schema_log.settle(rt)
    entry, = schema_log.event("recovery_phase")
    origin = "post_dial" if kind == "post_dial" else "recovery"
    _schema_assert_record(entry, _SCHEMA_RECOVERY, {"origin": (origin, origin), "phase": ("terminal", "terminal")})
    if kind == "no_ticket":
        assert entry.raw["incident"] == "" and entry.tokens["incident"] == "-"
        assert entry.raw["reason"] == entry.tokens["reason"] == "none"
    else:
        assert _schema_re.fullmatch(r"hmac:[0-9a-f]{24}", entry.raw["incident"])
        assert entry.tokens["incident"] == entry.raw["incident"]
        if kind == "post_dial": assert entry.raw["reason"] == entry.tokens["reason"] == "post_dial_native_rebind"
        else:
            assert _schema_re.fullmatch(r"reason-hmac:[0-9a-f]{24}", entry.raw["reason"])
            assert entry.tokens["reason"] == entry.raw["reason"]
    old, = schema_log.recovery
    assert set(old) == {"phase", "incident_hash", "route_epoch", "reason_code", "origin"}
    assert old == {"phase": "terminal", "incident_hash": entry.raw["incident"], "route_epoch": 3, "reason_code": reason, "origin": origin}
    _schema_marker(schema_log)


@pytest.mark.parametrize("kind,rules,text", [("rules", ("FAST",), "FAST"), ("empty", (), "-"), ("unknown", "unknown", "unknown")])
def test_schema_runner_lifecycle_uses_startup_read(schema_log, kind, rules, text):
    rt, *_ = fixture(enabled=True)
    schema_log.attach(rt)
    scope = _schema_scope(rt)
    reads = []
    def mode():
        reads.append(1)
        if kind == "unknown": raise RuntimeError("controlled startup mode read")
        return "act", frozenset(rules)
    runner = _schema_runner(rt, scope, mode)
    runner._load_origin_rules = lambda: False
    runner._run()
    schema_log.settle(rt)
    assert reads == [1] and runner.thread is None and runner.metrics["controller_calls"] == 0
    actual_mode = "unknown" if kind == "unknown" else "act"
    started, = schema_log.event("observer_started")
    _schema_assert_record(started, {"ev", "mode"}, {"mode": (actual_mode, actual_mode)})
    assert started.message == "Post-dial observer observer_started (mode=" + actual_mode + ")"
    context, = schema_log.event("observer_started_context")
    _schema_assert_record(context, _SCHEMA_CONTEXT, {"mode": (actual_mode, actual_mode), "act_rules": (rules, text)})
    stopped, = schema_log.event("observer_stopped")
    _schema_assert_record(stopped, _SCHEMA_E | _SCHEMA_S | {"reason", "controller_calls"}, {
        "reason": ("origin_rules_unavailable", "origin_rules_unavailable"), "controller_calls": (0, "0"),
    })
    assert stopped.message.startswith("Post-dial observer observer_stopped (reason=")
    fence, = schema_log.event("admission_fence")
    _schema_assert_record(fence, _SCHEMA_FENCE, {"closed": (True, "true"), "complete": (True, "true"),
        "final_produced_seq": (2, "2"), "final_emitted_seq": (2, "2"), "emitted_count": (2, "2"), "loss_total": (0, "0")})
    _schema_marker(schema_log)


def test_schema_policy_first_application_and_change(schema_log):
    rt, *_ = fixture(enabled=True)
    schema_log.attach(rt)
    scope = _schema_scope(rt)
    values = iter([("act", frozenset({"FAST"})), ("shadow", frozenset({"FAST", "AGED"}))])
    reads = []
    runner = _schema_runner(rt, scope, lambda: reads.append(1) or next(values))
    assert runner._read_mode() and runner._read_mode()
    schema_log.settle(rt)
    assert reads == [1, 1] and runner.metrics["controller_calls"] == 0
    first, second = schema_log.event("observer_policy", 2)
    _schema_assert_record(first, _SCHEMA_POLICY, {"mode": ("act", "act"), "actual_rules": (("FAST",), "FAST"), "policy_revision": (1, "1")})
    _schema_assert_record(second, _SCHEMA_POLICY, {"mode": ("shadow", "shadow"), "actual_rules": (("AGED", "FAST"), "AGED,FAST"), "policy_revision": (2, "2")})
    _schema_marker(schema_log)


@pytest.mark.parametrize("kind,reason,check,source,evaluated,scoped", [
    ("active", None, "armed", "checked", True, True),
    ("not_armed", "not_armed", "no_arm", "checked", True, True),
    ("cheap", "judging", "not_evaluated", "cheap", False, True),
    ("cached", "not_armed", "route_epoch", "cached", False, True),
    ("no_scope", "not_armed", "cached_unknown", "cached", False, False),
])
def test_schema_admission_result_and_unknown_scope(schema_log, kind, reason, check, source, evaluated, scoped):
    rt, *_ = fixture(enabled=True)
    schema_log.attach(rt)
    if scoped: _schema_scope(rt)
    judge = _schema_judge(rt)
    rt._node_judgement_arm_admission(NS(kind="FAST", leaf="private-leaf", episode=9), judge, reason,
                                    check=check, source=source, evaluated=evaluated)
    schema_log.settle(rt)
    entry, = schema_log.event("admission")
    _schema_assert_record(entry, _SCHEMA_ADMISSION, {
        "mode": ("act", "act"), "rule": ("FAST", "FAST"), "episode": (9, "9"),
        "result": ("active" if reason is None else "gated", "active" if reason is None else "gated"),
        "reason": (reason or "none", reason or "none"), "predicate": ("codex_arm", "codex_arm"),
        "arm_evaluated": (evaluated, "true" if evaluated else "false"), "arm_check": (check, check),
        "source": (source, source), "policy_revision": (0, "0"),
    })
    assert _schema_re.fullmatch(r"[0-9a-f]{12}", entry.raw["leaf"])
    assert entry.tokens["leaf"] == entry.raw["leaf"]
    if not scoped:
        assert (entry.raw["observer"], entry.raw["scope"], entry.raw["scope_seq"]) == ("unknown", "unknown", 0)
    _schema_marker(schema_log)


@pytest.mark.parametrize("kind,extra", [
    ("act_base", frozenset()), ("shadow_base", frozenset()), ("act_ratio", _SCHEMA_RATIO),
    ("share_none", _SCHEMA_SHARE), ("share_both", _SCHEMA_SHARE | _SCHEMA_OPTIONAL),
    ("variant", _SCHEMA_RATIO | _SCHEMA_SHARE | _SCHEMA_OPTIONAL | {"variant_only"}),
    ("ratio_share", _SCHEMA_RATIO | _SCHEMA_SHARE | _SCHEMA_OPTIONAL),
    ("share_only", _SCHEMA_SHARE | {"share_ok_at_0_5"}),
    ("onset_only", _SCHEMA_SHARE | {"onset_ok_at_0_5"}),
])
def test_schema_trigger_real_upstream_and_conditional_fields(schema_log, kind, extra):
    from runtime_post_dial import Trigger, ShadowTrigger
    rt, *_ = fixture(enabled=True)
    schema_log.attach(rt)
    scope = _schema_scope(rt)
    runner = _schema_runner(rt, scope)
    assert runner._read_mode()
    values = dict(leaf="private-leaf", tokens=("0123456789abcdef0123456789abcdef",), t=123.456,
                  n_events=3, first_event_t=122.456, episode=9)
    # Input scenarios and expected sets are independent handwritten tables.
    if kind in {"act_ratio", "ratio_share", "variant"}:
        values.update(ratio_failed=3, ratio_answered=5)
    if kind in {"share_none", "share_both", "variant", "ratio_share", "share_only", "onset_only"}:
        values.update(share_failed=3, share_answered=5, share_ok=True, onset_answered=4, opens=("VARIANT_SHARE",))
    if kind in {"share_both", "variant", "ratio_share", "share_only"}: values["share_ok_at_0_5"] = True
    if kind in {"share_both", "variant", "ratio_share", "onset_only"}: values["onset_ok_at_0_5"] = False
    mode = "shadow" if kind == "shadow_base" else "act"
    noops = []
    if kind == "variant":
        trigger = ShadowTrigger(rule="FAST", variant_only=True, **values)
        polls = []
        runner._client = NS(connections=lambda **kw: polls.append(kw) or {})
        runner.core.ingest_snapshot = lambda *_a, **_k: NS(accepted=False)
        runner.core.evaluate_triggers = lambda *_a, **_k: NS(triggers=(), shadow=(trigger,))
        runner._poll(mode)
        assert len(polls) == runner.metrics["controller_calls"] == 1
    else:
        trigger = Trigger(kind="FAST", new_episode=True, **values)
        rt._start_runtime_post_dial_judgement = lambda *_a, **_k: noops.append(1)
        rt._node_judgement_arm_trigger(scope, trigger, mode, runner.core.tokenizer)
        assert noops == [1] and runner.metrics["controller_calls"] == 0
    schema_log.settle(rt)
    event = "would_trigger" if kind in {"shadow_base", "variant"} else "trigger"
    entry, = schema_log.event(event)
    _schema_assert_record(entry, _SCHEMA_TRIGGER | extra, {
        "mode": (mode, mode), "t": (123.456, "123.456"), "rule": ("FAST", "FAST"),
        "toks": (["0123456789ab"], "0123456789ab"), "n": (3, "3"),
        "first_ev_ms": (1000.0, "1000.0"), "episode": (9, "9"),
        "actual_rules": (("FAST",), "FAST"), "policy_revision": (1, "1"),
    })
    assert entry.message.endswith("mode=" + mode + ")")
    if kind in {"act_ratio", "ratio_share", "variant"}:
        assert (entry.raw["ratio_failed"], entry.raw["ratio_answered"]) == (3, 5)
        assert (entry.tokens["ratio_failed"], entry.tokens["ratio_answered"]) == ("3", "5")
    if kind in {"share_none", "share_both", "variant", "ratio_share", "share_only", "onset_only"}:
        assert (entry.raw["share_failed"], entry.raw["share_answered"], entry.raw["share_ok"], entry.raw["onset_answered"]) == (3, 5, True, 4)
        assert (entry.tokens["share_failed"], entry.tokens["share_answered"], entry.tokens["share_ok"], entry.tokens["onset_answered"]) == ("3", "5", "true", "4")
        assert entry.raw["opens"] == ["VARIANT_SHARE"] and entry.tokens["opens"] == "VARIANT_SHARE"
    if kind in {"share_both", "variant", "ratio_share", "share_only"}:
        assert entry.raw["share_ok_at_0_5"] is True and entry.tokens["share_ok_at_0_5"] == "true"
    if kind in {"share_both", "variant", "ratio_share", "onset_only"}:
        assert entry.raw["onset_ok_at_0_5"] is False and entry.tokens["onset_ok_at_0_5"] == "false"
    if kind == "variant": assert entry.raw["variant_only"] is True and entry.tokens["variant_only"] == "true"
    _schema_marker(schema_log)


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("reason", ["not_armed", "judging"])
def test_schema_gated_real_service_and_scope(schema_log, scoped, reason):
    rt, *_ = fixture(enabled=True)
    schema_log.attach(rt)
    if scoped: _schema_scope(rt)
    judge = _schema_judge(rt)
    check = "no_arm" if reason == "not_armed" else None
    judge.note_gated(NS(kind="FAST", leaf="private-leaf", episode=9), reason, arm_check=check)
    schema_log.settle(rt)
    entry, = schema_log.event("gated")
    expected = _SCHEMA_GATED | ({"arm_check"} if reason == "not_armed" else set())
    if scoped: expected |= {"instance", "observer", "scope"}
    _schema_assert_record(entry, expected, {"reason": (reason, reason), "episode": (9, "9"), "rule": ("FAST", "FAST"), "t": (123.456, "123.456")})
    if reason == "not_armed": assert entry.raw["arm_check"] == entry.tokens["arm_check"] == "no_arm"
    assert _schema_re.fullmatch(r"[0-9a-f]{12}", entry.raw["leaf"])
    assert entry.tokens["leaf"] == entry.raw["leaf"]


@pytest.mark.parametrize("lost", [False, True])
def test_schema_retired_fence_and_real_sink_loss(schema_log, lost):
    rt, *_ = fixture(enabled=True)
    schema_log.attach(rt)
    scope = _schema_scope(rt)
    if lost: schema_log.fail_once(rt, "admission")
    rt._node_judgement_arm_admission(NS(kind="FAST", leaf="private-leaf", episode=9), _schema_judge(rt), None,
                                    check="armed", source="checked", evaluated=True)
    schema_log.settle(rt)
    assert schema_log.drops == (["admission"] if lost else [])
    assert schema_log.event("admission_fence", 0) == []
    rt._node_judgement_arm_retire(scope)
    schema_log.settle(rt)
    entry, = schema_log.event("admission_fence")
    emitted = 0 if lost else 1
    _schema_assert_record(entry, _SCHEMA_FENCE, {
        "closed": (True, "true"), "complete": (not lost, "false" if lost else "true"),
        "final_produced_seq": (1, "1"), "final_emitted_seq": (emitted, str(emitted)),
        "emitted_count": (emitted, str(emitted)), "loss_total": (int(lost), str(int(lost))),
        "inflight_before_cut": (0, "0"),
    })
    assert scope["retired"] and scope["scope"] not in scope["state"]["scopes"]
    _schema_marker(schema_log)


@pytest.mark.parametrize("with_judge", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_schema_summary_increment_conditions(schema_log, monkeypatch, with_judge, enabled):
    rt, *_ = fixture(enabled=True)
    schema_log.attach(rt)
    rt._node_judgement_arm_epoch("schema_initial")
    schema_log.settle(rt)
    if with_judge:
        judge = _schema_judge(rt)
        rt._node_judgement_runtime_state = NS(judge=judge)
        trigger = NS(kind="FAST", leaf="private-leaf", episode=9)
        judge.note_gated(trigger, "not_armed", arm_check="no_arm")
        judge.note_gated(trigger, "not_armed", arm_check="route_epoch")
    if enabled:
        schema_log.fail_once(rt, "route_epoch")
        rt._node_judgement_arm_epoch("schema_lost")
        schema_log.settle(rt)
        monkeypatch.setattr(module.time, "monotonic", lambda: 100.0)
        rt._runtime_facility_hold_reason = lambda: "hold"
        assert rt._try_arm_runtime_recovery() is False
        assert rt._try_arm_runtime_recovery() is False
        schema_log.settle(rt)
        assert schema_log.drops == ["route_epoch"]
    else:
        rt.config["client"]["egress_recovery"]["node_judgement"]["post_dial_judge"] = "off"
        rt._runtime_post_dial_mode()
    runner = _schema_runner(rt)
    runner._emit_summary(123.456)
    schema_log.settle(rt)
    entry, = schema_log.event("summary")
    expected = _SCHEMA_SUMMARY
    if with_judge: expected |= {"gated_not_armed_check_changes"}
    if enabled: expected |= {"arm_diagnostics_loss_total", "arm_refused_suppressed_total"}
    _schema_assert_record(entry, expected, {
        "t": (123.456, "123.456"), "unknown_share": (None, "none"), "tracked": (0, "0"),
        "watches": (0, "0"), "total_controller_calls": (0, "0"),
    })
    if with_judge:
        assert entry.raw["gated_not_armed_check_changes"] == 1 and entry.tokens["gated_not_armed_check_changes"] == "1"
    if enabled:
        assert entry.raw["arm_diagnostics_loss_total"] == 1 and entry.tokens["arm_diagnostics_loss_total"] == "1"
        assert entry.raw["arm_refused_suppressed_total"] == 1 and entry.tokens["arm_refused_suppressed_total"] == "1"
    _schema_marker(schema_log)
