"""Section 14: controlled facts and isolated process/pipe, no network startup."""
from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from bridge_messages import classify_start_failure, startup_diagnostic
from bridge_runtime_access import RuntimeAccess, failure_code, service_state_data
import bridge_protocol as protocol


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "bridge_schema/fixtures"
CASES = json.loads((FIXTURES / "p2_05_startup_failures.json").read_bytes())
# Ruling T-P2-12 item 2 (决定_第一波裁定2): P2-05 no longer emits params.field;
# Error.field (listener_error) is authoritative. The fixture's diagnostics keep
# the legacy params.field as input (which must be dropped); its expected DTOs
# carry no field (T-P2-31b cleaned the fixture, so no stripping loop here).
EMPTY = json.loads((FIXTURES / "p2_05_p1_empty_sources.json").read_bytes())


def lifecycle(diagnostic=None):
    data = json.loads((FIXTURES / "v1/valid/startup_listener_conflict.json").read_bytes())["data"]
    data["runtime"].pop("failure")
    data["runtime"]["failure_code"] = None
    if diagnostic is not None:
        data["runtime"]["failure_diagnostic"] = diagnostic
    return data


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["expected"]["reason_code"])
def test_section14_mapping(case):
    diagnostic = deepcopy(case["diagnostic"])
    expected = case["expected"]
    assert classify_start_failure(diagnostic=diagnostic) == expected
    assert failure_code(diagnostic) == expected["code"]
    assert diagnostic == case["diagnostic"]
    state = service_state_data(lifecycle(diagnostic))
    assert state["runtime"]["failure"] == expected
    assert state["runtime"]["failure_code"] == expected["code"]
    assert protocol.validate_service_state(state) == []
    wire = json.dumps(state)
    assert "C:/private" not in wire and "Traceback" not in wire
    errors = []
    protocol._schema(expected, "Error", "failure", errors)
    assert errors == []


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["expected"]["reason_code"])
def test_section14_does_not_guess_from_similar_reason(case):
    reason = case["expected"]["reason_code"]
    assert classify_start_failure(RuntimeError("unrelated " + reason),
        {"reason_code": reason + "_other", "nodes": 0})["code"] == "internal"


@pytest.mark.parametrize("exception,diagnostic", [
    (RuntimeError("nodes=0"), {"nodes": 0, "healthy": 0, "enabled_sources": 0}),
    (PermissionError("C:/private/config.yaml"), {}),
    (RuntimeError("arbitrary timeout in a file name"), {}),
    (FileNotFoundError("C:/private/settings.yaml"), {}),
    (RuntimeError("unrelated no_sources substring"), {}),
    (RuntimeError("unknown"), {"reason_code": "source_supply_terminal", "subreason": "unknown"}),
])
def test_negative_evidence(exception, diagnostic):
    assert classify_start_failure(exception, diagnostic) == dict(
        code="internal", message_key="errors.start.unexpected", params={}, reason_code="unexpected")


def test_diagnostic_then_authority_then_compatibility():
    exc = RuntimeError("Fallback has no validated proxy nodes; startup remains fail-closed")
    exc.diagnostic = {"reason_code": "core_missing"}
    assert classify_start_failure(exc, {"reason_code": "no_sources"},
        {"runtime": {"failure_code": "source_supply_no_egress"}})["reason_code"] == "core_missing"
    del exc.diagnostic
    assert classify_start_failure(exc, lifecycle={"runtime": {
        "failure_code": "source_supply_no_egress"}})["code"] == "source_supply_no_egress"
    assert classify_start_failure(exc)["reason_code"] == "no_validated_nodes"
    assert classify_start_failure(diagnostic={"reason_code": "no_sources",
        "enabled_sources_empty": True, "caused_start_failure": True})["code"] == "no_sources"


@pytest.mark.parametrize("code", ["io", "validation", "needs_route", "timeout", "not_running", "lifecycle_busy"])
def test_legacy_machine_code_is_retained_without_inventing_a_reason(code):
    sample = lifecycle()
    sample["runtime"]["failure_code"] = code
    dto = service_state_data(sample)["runtime"]["failure"]
    assert dto["code"] == failure_code(code) == code
    assert dto["message_key"] == "errors.start.unexpected"
    assert classify_start_failure(diagnostic=dto) == dto


@pytest.mark.parametrize("kind", ["mixed", "http", "socks", "controller", "subscription", "dns"])
def test_listener_types_preserve_safe_fields(kind):
    from desktop_runtime import RuntimeListenerPreflightError, RuntimeListenerSetupError
    from runtime_listener_preflight import (RuntimeListenerSpec, RuntimeListenerPreflightResult,
                                           RuntimeListenerPreflightState, RuntimeListenerProbeFailure)
    conflict = RuntimeListenerPreflightError(((kind, "tcp", "private-host", 12345),))
    # No params.field (ruling T-P2-12 item 2); Error.field is listener_error's.
    expected = {"kind": kind, "port": 12345}
    assert classify_start_failure(conflict)["params"] == expected
    assert "field" not in classify_start_failure(conflict)
    assert failure_code(conflict) == "runtime_listener_conflict"
    setup = RuntimeListenerSetupError(RuntimeListenerPreflightResult(
        state=RuntimeListenerPreflightState.INDETERMINATE,
        specs=(RuntimeListenerSpec(kind, "tcp", "private-host", 12345),),
        failures=(RuntimeListenerProbeFailure(
            RuntimeListenerSpec(kind, "tcp", "private-host", 12345), "bind"),)))
    assert classify_start_failure(setup)["params"] == expected
    assert classify_start_failure(setup)["code"] == "runtime_listener_unavailable"
    assert startup_diagnostic(conflict)["exception_type"] == "RuntimeListenerPreflightError"


@pytest.mark.parametrize("with_diagnostic", [False, True])
def test_actual_listener_exception_overrides_old_diagnostics(with_diagnostic):
    from desktop_runtime import RuntimeListenerPreflightError
    exc = RuntimeListenerPreflightError((("controller", "tcp", "private-host", 9090),))
    if not with_diagnostic:
        del exc.diagnostic  # The actual exception type is sufficient too.
    stale = {"reason_code": "no_validated_nodes", "healthy": 0,
             "params": {"kind": "mixed", "port": 7890}, "timed_out": True}
    dto = classify_start_failure(exc, stale, lifecycle(stale))
    assert dto == dict(code="runtime_listener_conflict",
        message_key="errors.start.listener_conflict", reason_code="runtime_listener_conflict",
        params={"kind": "controller", "port": 9090})
    assert classify_start_failure(diagnostic=startup_diagnostic(exc, diagnostic=stale)) == dto
    assert failure_code(exc) == dto["code"]


def test_diagnostic_sources_do_not_share_params_or_timeout():
    exc = RuntimeError("private")
    exc.diagnostic = {"reason_code": "source_supply_terminal.transport_failed"}
    stale = {"reason_code": "geodata_download_failed", "timed_out": True, "code": "timeout"}
    assert classify_start_failure(exc, stale)["code"] == "io"
    exc.diagnostic = {"reason_code": "insufficient_validated_nodes", "healthy": 1}
    assert classify_start_failure(exc, {"required": 7}, lifecycle({"required": 9}))["params"] == {"healthy": 1}
    assert classify_start_failure(diagnostic={"exception_type": "RuntimeListenerSetupError"},
        lifecycle={"runtime": {"reason_code": "core_missing", "kind": "mixed", "port": 7890}})["params"] == {}
    exc.diagnostic = {"params": {"kind": "mixed", "port": 7890}}
    authority = {"runtime": {"failure_code": "runtime_listener_conflict",
                             "kind": "controller", "port": 9090}}
    assert classify_start_failure(exc, lifecycle=authority)["params"] == {
        "kind": "controller", "port": 9090}
    exc.__cause__ = TimeoutError("private")
    assert classify_start_failure(exc,
        {"reason_code": "source_supply_terminal.transport_failed"})["code"] == "io"


@pytest.mark.parametrize("has_failure", [False, True])
def test_listener_setup_uses_failed_endpoint_only(has_failure):
    from desktop_runtime import RuntimeListenerSetupError
    from runtime_listener_preflight import (RuntimeListenerSpec, RuntimeListenerPreflightResult,
        RuntimeListenerPreflightState, RuntimeListenerProbeFailure)
    good = RuntimeListenerSpec("mixed", "tcp", "private-host", 7890)
    bad = RuntimeListenerSpec("controller", "tcp", "private-host", 9090)
    exc = RuntimeListenerSetupError(RuntimeListenerPreflightResult(
        state=RuntimeListenerPreflightState.INDETERMINATE, specs=(good, bad),
        failures=(RuntimeListenerProbeFailure(bad, "bind"),) if has_failure else ()))
    dto = classify_start_failure(exc, {"kind": "mixed", "port": 7890})
    assert dto["code"] == "runtime_listener_unavailable"
    assert dto["params"] == ({"kind": "controller", "port": 9090} if has_failure else {})
    assert "private-host" not in json.dumps(dto)


def test_parameter_whitelist_and_numeric_types():
    assert classify_start_failure(diagnostic={"reason_code": "insufficient_validated_nodes",
        "params": {"healthy": 0, "required": 3, "nodes": 0, "detail": "C:/private"}})["params"] == {
            "healthy": 0, "required": 3}
    assert classify_start_failure(diagnostic={"reason_code": "insufficient_validated_nodes",
        "params": {"healthy": True, "required": "3"}})["params"] == {}
    dto = classify_start_failure(diagnostic={"reason_code": "runtime_listener_conflict",
        "params": {"kind": "C:/private", "field": "Traceback", "port": True}})
    assert dto["params"] == {}


@pytest.mark.parametrize("reason", ["geodata_download_failed", "source_supply_terminal.transport_failed"])
@pytest.mark.parametrize("timed_out", [False, True])
def test_stage_timeout_evidence(reason, timed_out):
    diagnostic = {"reason_code": reason, "timed_out": timed_out}
    dto = classify_start_failure(RuntimeError("timeout text is irrelevant"), diagnostic)
    assert dto["code"] == ("timeout" if timed_out else "io")
    assert dto["reason_code"] == reason
    assert classify_start_failure(diagnostic=startup_diagnostic(diagnostic=diagnostic)) == dto


def test_exception_type_and_geodata_cause_timeout():
    from desktop_runtime import RecoverableCoreStartError, RuntimeStartCancelled
    assert classify_start_failure(TimeoutError("private endpoint"))["reason_code"] == "core_start_timeout"
    assert classify_start_failure(RuntimeStartCancelled("private detail"))["code"] == "superseded"
    exc = RuntimeError("GeoData preflight through the explicit egress proxy failed: private")
    exc.__cause__ = TimeoutError("private")
    dto = classify_start_failure(exc)
    assert (dto["code"], dto["reason_code"]) == ("timeout", "geodata_download_failed")
    wrapped = RecoverableCoreStartError("private")
    wrapped.__cause__ = TimeoutError("private")
    assert classify_start_failure(wrapped)["reason_code"] == "core_start_timeout"


@pytest.mark.parametrize("child,code", [("rule_blocked", "validation"),
    ("no_proxy", "needs_route"), ("transport_failed", "io"), ("no_egress", "source_supply_no_egress")])
def test_structured_supply_subreason(child, code):
    dto = classify_start_failure(diagnostic={"reason_code": "source_supply_terminal",
        "source_supply": {"reason": child, "detail": "C:/private"}})
    assert dto["code"] == code and dto["params"] == {}


@pytest.mark.parametrize("facts,code", [
    ({"source_retry_blocked_reason": "runtime_source_retry_disabled"}, "validation"),
    ({"source_retry_blocked_reason": "blocked_no_route"}, "needs_route"),
    ({"source_fetch_failed": 2}, "io"),
])
def test_supply_terminal_authoritative_fallbacks(facts, code):
    assert classify_start_failure(diagnostic={"reason_code": "source_supply_terminal", **facts})["code"] == code
    assert classify_start_failure(diagnostic=facts)["code"] == "internal"


def test_cleanup_and_superseded_lifecycle_evidence():
    assert classify_start_failure(lifecycle={"runtime": {
        "start_blocked_by_cleanup": True}})["code"] == "lifecycle_busy"
    assert classify_start_failure(lifecycle={"intent": {
        "stop_requested": True}})["code"] == "superseded"
    # A new stop supersedes the attempted start even when a business cause exists.
    facts = lifecycle({"reason_code": "no_validated_nodes"})
    facts["intent"].update(desired_running=False, stop_requested=True)
    facts["runtime"]["startup_phase"] = "stopping"
    assert service_state_data(facts)["runtime"]["failure"]["code"] == "superseded"


@pytest.mark.parametrize("text,reason", [
    ("Fallback has only 2/3 validated proxy nodes; startup remains fail-closed", "insufficient_validated_nodes"),
    ("mihomo binary not found: C:/private/mihomo.exe", "core_missing"),
    ("Mihomo bootstrap core is not available for promotion", "core_not_running"),
    ("Could not verify fallback after core launch: private", "route_verification_failed"),
    ("Could not pre-warm Bastion Layer: private", "fallback_warmup_failed"),
    ("Required GeoData is missing and the GeoData updater is disabled: private", "geodata_missing"),
    ("Required GeoData is missing before Mihomo startup; preflight download requires an explicit egress proxy: private", "geodata_needs_route"),
    ("Required GeoData is still missing after explicit-proxy preflight: private", "geodata_download_failed"),
    ("TUN mode requires running Tesseract as Administrator (WinTun driver)", "admin_required"),
    ("source_supply_no_egress: exhausted", "source_supply_no_egress"),
])
def test_known_legacy_producers(text, reason):
    assert classify_start_failure(RuntimeError(text))["reason_code"] == reason
    assert classify_start_failure(RuntimeError("unrelated " + text))["code"] == "internal"


def test_p1_empty_sources_rereads_real_reason():
    from runtime_tier_state import tier3_startup_readiness_error
    text = tier3_startup_readiness_error(required_for_start=True, core_sync_marker=None,
        current_healthy_count=0, required_healthy_count=1, failure_detail=EMPTY["failure_detail"])
    assert text == EMPTY["runtime_text"]
    assert classify_start_failure(RuntimeError(text)) == EMPTY["expected"]
    assert EMPTY["expected"]["code"] == "needs_route"


def test_qt_text_and_record_generation():
    from tests.test_runtime_lifecycle_status import minimal_runtime
    from desktop_runtime import DesktopRuntime, RecoverableCoreStartError
    runtime = minimal_runtime()
    runtime.manager = None
    runtime.logger = logging.getLogger("p2-05-controlled")
    runtime._set_effective_egress_state = lambda *_: None
    runtime._record_recoverable_core_start_error(RecoverableCoreStartError(EMPTY["runtime_text"]),
                                              update_last_error=True)
    assert runtime.last_error == EMPTY["runtime_text"]
    assert runtime._start_deferred_reason == EMPTY["runtime_text"]
    assert runtime._bridge_start_failure_record[2]["exception_type"] == "RecoverableCoreStartError"
    # Fail-closed without Core; classification survives sampling and publication.
    runtime.core_runner.lightweight_fact = lambda: dict(running=False, pid=None,
        process_generation=3, cleanup_pending=False)
    assert RuntimeAccess(runtime).lifecycle_status()["runtime"]["failure"]["code"] == "needs_route"
    runtime._runtime_generation += 1
    assert DesktopRuntime.lifecycle_status(runtime)["runtime"]["failure_diagnostic"] is None
    runtime.last_error = None
    assert RuntimeAccess(runtime).lifecycle_status()["runtime"]["failure"] is None


class FakeRuntime:
    def __init__(self, diagnostic, *, raises):
        self.diagnostic = deepcopy(diagnostic)
        self.raises = raises
        self.facts = lifecycle()
        self.facts["runtime"]["startup_phase"] = "collecting"

    def lifecycle_status(self):
        return deepcopy(self.facts)

    def start(self):
        self.facts["runtime"].update(startup_phase="failed", failure_diagnostic=self.diagnostic)
        if self.diagnostic.get("reason_code") == "superseded":
            self.facts["runtime"]["startup_phase"] = "stopping"
            self.facts["intent"].update(desired_running=False, stop_requested=True)
        if self.raises:
            exc = RuntimeError("C:/private/start.yaml\nTraceback (most recent call last)")
            exc.diagnostic = self.diagnostic
            raise exc
        return {"startup_phase": self.facts["runtime"]["startup_phase"]}


def cleanup_blocked_runtime():
    from tests.test_runtime_lifecycle_status import minimal_runtime
    runtime = minimal_runtime()
    runtime.running = runtime._desired_running = False
    runtime.manager = None
    runtime._runtime_lifecycle_lock = threading.RLock()
    runtime.core_runner.lightweight_fact = lambda: dict(running=False, pid=None,
        process_generation=3, cleanup_pending=False)
    # Exercise DesktopRuntime.start's real guard, without an old stop failure
    # or pre-populated diagnostic. No Core/network/config access is required.
    runtime._runtime_failure_monitor_thread = SimpleNamespace(is_alive=lambda: True)
    runtime.status = lambda **_: {"startup_phase": "stopped", "shutdown_cleanup_pending": False}
    return runtime


def test_real_cleanup_guard_survives_job_and_repeated_sampling():
    from bridge_main import BridgeSession
    from bridge_publish import SamplingWorker
    runtime = cleanup_blocked_runtime()
    expected = classify_start_failure(diagnostic={"reason_code": "cleanup_pending"})
    assert not runtime._last_runtime_stop
    assert RuntimeAccess(runtime).lifecycle_status()["runtime"]["failure"] is None
    session = BridgeSession(None, SimpleNamespace(gate=SimpleNamespace(require_idle=lambda: None)),
        None, "p2-05-cleanup", runtime_factory=lambda *_: runtime)
    session.refresh_secrets = lambda: None
    session.state.subscribe(["service.state"])
    try:
        ack = session.executor.submit("service.start", {})
        assert ack.accepted
        session.executor.acceptance_sent(ack.op)
        job = session.executor.wait(ack.op, 3)
        assert job["state"] == "failed" and job["error"] == expected
        assert job["result"] == {"startup_phase": "failed", "failure_code": "lifecycle_busy"}
        worker = SamplingWorker(session.state, session.access)
        for _ in range(3):
            worker._service()
            state = session.state.snapshot("service.state")["data"]
            assert state["runtime"]["startup_phase"] == "failed"
            assert state["runtime"]["failure"] == job["error"]
            assert state["runtime"]["failure_code"] == failure_code(expected)
            assert state["runtime"]["shutdown_cleanup_pending"] is True
            assert protocol.validate_service_state(state) == []
        assert runtime.last_error is None  # Preserve Qt's original text behavior.
        runtime._runtime_failure_monitor_thread = None
        worker._service()
        state = session.state.snapshot("service.state")["data"]
        assert state["runtime"]["startup_phase"] == "stopped"
        assert state["runtime"]["failure"] is None and state["runtime"]["failure_code"] is None
        assert not state["runtime"]["shutdown_cleanup_pending"]
        # Completed cleanup must not resurrect the old failure for later work.
        runtime._runtime_failure_monitor_thread = SimpleNamespace(is_alive=lambda: True)
        assert session.access.lifecycle_status()["runtime"]["failure"] is None
    finally:
        assert session.executor.shutdown(time.monotonic() + 2)


@pytest.mark.parametrize("new_owner", ["generation", "start_intent", "stop_intent"])
def test_cleanup_failure_expires_on_new_owner(new_owner):
    runtime = cleanup_blocked_runtime()
    access = RuntimeAccess(runtime)
    assert access.start()["failure_code"] == "lifecycle_busy"
    assert access.lifecycle_status()["runtime"]["failure"]["reason_code"] == "cleanup_pending"
    if new_owner == "generation":
        runtime._runtime_generation += 1
    else:
        runtime._lifecycle_revision += 1
        runtime._desired_running = new_owner == "start_intent"
        runtime._stop_request_pending = new_owner == "stop_intent"
    state = access.lifecycle_status()
    assert state["runtime"]["failure"] is None
    assert state["runtime"]["failure_code"] is None
    assert state["runtime"]["startup_phase"] == ("stopping" if new_owner == "stop_intent" else "stopped")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["expected"]["reason_code"])
@pytest.mark.parametrize("raises", [False, True])
def test_job_state_and_failure_code_agree(case, raises):
    from bridge_main import BridgeSession
    runtime = FakeRuntime(case["diagnostic"], raises=raises)
    session = BridgeSession(None, SimpleNamespace(gate=SimpleNamespace(require_idle=lambda: None)),
        None, "p2-05", runtime_factory=lambda *_: runtime)
    session.refresh_secrets = lambda: None
    session.state.subscribe(["service.state"])
    try:
        ack = session.executor.submit("service.start", {})
        assert ack.accepted
        session.executor.acceptance_sent(ack.op)
        job = session.executor.wait(ack.op, 3)
        assert job is not None
        assert job["state"] == ("superseded" if case["expected"]["code"] == "superseded" else "failed")
        if job["state"] == "superseded":
            assert job["error"] is None  # Protocol excludes error/result on this terminal.
        else:
            assert job["error"] == case["expected"]
        if job["result"]:
            assert job["result"]["failure_code"] == job["error"]["code"]
        sampled = service_state_data(session.access.lifecycle_status())
        assert sampled["runtime"]["failure"] == case["expected"]
        assert sampled["runtime"]["failure_code"] == failure_code(case["diagnostic"])
        # The normal sampling worker must also emit the DTO, never local fields.
        from bridge_publish import SamplingWorker
        SamplingWorker(session.state, session.access)._service()
        state = session.state.snapshot("service.state")["data"]
        assert state["runtime"]["failure"] == case["expected"]
        assert protocol.validate_service_state(state) == []
        assert "failure_diagnostic" not in state["runtime"]
        assert all(protocol.validate_frame(e) == [] for e in session.recorder.prepare_for_send(123))
    finally:
        assert session.executor.shutdown(time.monotonic() + 2)


@pytest.mark.skipif(os.name != "nt", reason="Windows private pipe")
def test_p1_empty_sources_in_real_process_and_pipe(tmp_path):
    from tests.test_bridge_integration import Shell, isolated_env, events
    env = isolated_env(tmp_path)
    env["TESSERACT_INSTANCE_SCOPE"] = "p2-05-empty"
    temp = tmp_path / "temp"
    temp.mkdir()
    env.update(TEMP=str(temp), TMP=str(temp))
    code = f'''
import bridge_main, runpy
from desktop_runtime import RecoverableCoreStartError
from runtime_tier_state import tier3_startup_readiness_error
original = bridge_main.create_runtime
class FakeRuntime:
    def __init__(self, runtime):
        self.runtime = runtime
    def __getattr__(self, name):
        return getattr(self.runtime, name)
    def start(self):
        runtime = self.runtime
        runtime._runtime_generation += 1
        runtime._desired_running = True
        runtime.running = True
        text = tier3_startup_readiness_error(required_for_start=True, core_sync_marker=None,
            current_healthy_count=0, required_healthy_count=1,
            failure_detail={EMPTY["failure_detail"]!r})
        runtime._record_recoverable_core_start_error(RecoverableCoreStartError(text), update_last_error=True)
        return runtime.status(include_runtime_nodes=False)
def create(*args):
    return FakeRuntime(original(*args))
# Default factory argument is bound at definition; replace it explicitly.
bridge_main.BridgeSession.__init__.__kwdefaults__["runtime_factory"] = create
runpy.run_path('runtime_main.pyw', run_name='__main__')
'''
    shell = Shell(env, code)
    try:
        shell.hello()
        shell.subscribe()
        job = shell.job("service.start", 205)
        assert job["state"] == "failed"
        assert job["error"] == EMPTY["expected"]
        assert job["result"]["failure_code"] == "needs_route"
        def has_failure(frame):
            return any(e.get("topic") == "service.state"
                and e["data"]["runtime"].get("failure") == EMPTY["expected"] for e in events(frame))
        # The service edge may precede the terminal job event already read by
        # shell.job(); unchanged subsequent samples need not emit another edge.
        frame = next((f for f in shell.frames if has_failure(f)), None)
        if frame is None:
            frame = shell.until(has_failure)
        assert frame
        assert shell.job("service.stop", 206)["state"] == "done"
        assert shell.job("app.quit", 207)["state"] == "done"
        shell.until(lambda f: f["t"] == "bye")
        assert shell.process.wait(timeout=10) == 0
        assert all(protocol.validate_frame(f) == [] for f in shell.frames)
    finally:
        shell.close()
