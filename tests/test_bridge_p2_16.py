"""T-P2-16: exit identity request owner (exit.verify / exit.identity)."""
from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

import bridge_exit as bx
import bridge_protocol as protocol
from bridge_jobs import JobContext, JobEventRecorder, JobExecutor, JobFailure
from bridge_state import PublicationState


def lifecycle(*, phase="running", core_running=True, revision=1, network=1, generation=1,
              readiness="ready", validation_active=False):
    return {
        "intent": {"desired_running": True, "stop_requested": False,
                   "lifecycle_revision": revision, "runtime_generation": 1},
        "runtime": {"runtime_running": True, "startup_phase": phase},
        "core": {"running": core_running, "pid": 101, "process_generation": 1,
                 "core_generation": generation, "cleanup_pending": False},
        "readiness": {"state": readiness, "validation_active": validation_active,
                      "network_context_epoch": network, "route_epoch": 1,
                      "lifecycle_revision": revision},
    }


def payload(*, v4="203.0.113.7", v6=None, binding="a1b2c3d4e5f6a1b2c3d4e5f6", state="complete",
            error=None, failure=None, switch_reason=None, retry_after=None, freshness="fresh",
            data_plane_reason=None, country="JP"):
    def family(ip):
        if not ip:
            item = {"state": "unavailable", "ip": None, "evidence": []}
        else:
            item = {"state": "confirmed", "ip": ip, "evidence": [],
                    "location": {"country_code": country, "state": "reported"}}
        if retry_after is not None:
            item["evidence"] = [{"source": "ident.me", "attempts": [
                {"status": "rate_limited", "transport_retries": [
                    {"retry_after_seconds": retry_after}]}]}]
        return item

    diagnostics = {}
    if failure:
        diagnostics["overview_failure_reason"] = failure
    if switch_reason:
        diagnostics["neutral_connectivity"] = {"switch_reason": switch_reason}
    if data_plane_reason:
        diagnostics["data_plane_reason"] = data_plane_reason
    result = {
        "schema_version": 2, "state": state, "checked_at": "2026-10-05T01:02:03+00:00",
        "freshness": {"state": freshness, "reason": "manual_lookup"},
        "route": {"binding_marker": binding, "core_instance": "1:1:101", "confirmed": True},
        "identity": {"ipv4": family(v4), "ipv6": family(v6)},
        "diagnostics": diagnostics,
    }
    if error:
        result["error"] = error
    return result


class FakePort:
    def __init__(self, life=None):
        self.life = life or lifecycle()
        self.results: deque = deque()
        self.calls: list[dict] = []
        self.binding = None
        self.called = threading.Event()

    def lifecycle_status(self):
        value = self.life
        return value() if callable(value) else value

    def proxy_url(self):
        return "http://127.0.0.1:7890"

    def exit_binding(self):
        return self.binding

    def check_current_exit_identity(self, **kwargs):
        self.calls.append(kwargs)
        self.called.set()
        item = self.results.popleft()
        if callable(item):
            item = item(kwargs)
        return {**item, "request_id": kwargs["request_id"]}


def make_owner(port, **overrides):
    published = []
    options = dict(publish=published.append, attempt_seconds=2.0, poll_seconds=0.01,
                   lifecycle_poll_seconds=0.02, grace_seconds=0.05, defer_max_wait=2.0,
                   defer_retry_seconds=30.0)
    options.update(overrides)
    owner = bx.ExitIdentityOwner(port, **options)
    return owner, published


def valid_topic(dto):
    assert protocol.validate_topic_payload("exit.identity", dto) == [], dto


def run_async(owner, ctx=None, **kwargs):
    ctx = ctx or JobContext("job-1")
    box = {}

    def target():
        try:
            box["result"] = owner.verify({"trigger": "manual"}, ctx, **kwargs)
        except JobFailure as exc:
            box["failure"] = exc
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return ctx, thread, box


def wait_for(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# -- results, previous, topic ---------------------------------------------------

def test_ok_result_is_done_and_published_with_bindings():
    port = FakePort()
    port.results.append(payload(v4="203.0.113.7", v6="2001:db8::7"))
    owner, published = make_owner(port)
    result = owner.verify({"trigger": "manual"}, JobContext("job-1"))
    assert result["status"] == "ok" and bx.result_is_done(result)
    # T-P4-C2 #1: single_source is an optional v1 addition of an ok leg.
    assert result["v4"] == {"status": "ok", "address": "203.0.113.7", "country": "JP",
                            "reason_code": None, "observed_at_ms": result["observed_at_ms"],
                            "single_source": False}
    assert result["v6"]["address"] == "2001:db8::7"
    assert result["binding_id"] == "a1b2c3d4e5f6a1b2c3d4e5f6" and result["generation"] == 1
    assert result["fresh"] is True and result["observed_at_ms"] > 0
    assert all(result[flag] is False for flag in bx.FALLBACK_FLAGS)
    call = port.calls[0]
    assert call["trigger"] == "manual" and call["request_id"] == result["request_id"]
    assert call["proxy_url"] == "http://127.0.0.1:7890"
    statuses = [dto["current"]["status"] for dto in published]
    assert statuses[0] == "running" and statuses[-1] == "ok"
    for dto in published:
        valid_topic(dto)


def test_partial_keeps_independent_stacks():
    port = FakePort()
    port.results.append(payload(v4="203.0.113.7", v6=None))
    owner, _ = make_owner(port)
    result = owner.verify({"trigger": "manual"}, JobContext("job-1"))
    assert result["status"] == "partial"
    assert result["v4"]["status"] == "ok"
    assert result["v6"] == {"status": "failed", "address": None, "country": None,
                            "reason_code": "identity_unavailable", "observed_at_ms": None}
    assert bx.result_is_done(result)


def test_previous_is_always_the_last_usable_result():
    port = FakePort()
    owner, published = make_owner(port)
    port.results.append(payload(v4="203.0.113.1"))
    first = owner.verify({"trigger": "manual"}, JobContext("job-1"))
    assert owner.snapshot()["previous"] is None
    port.results.append(payload(v4="203.0.113.2"))
    second = owner.verify({"trigger": "manual"}, JobContext("job-2"))
    snap = owner.snapshot()
    assert snap["current"]["request_id"] == second["request_id"]
    assert snap["previous"]["request_id"] == first["request_id"]
    port.results.append(payload(v4=None, state="unavailable", error="identity_unavailable",
                                failure="identity_unavailable"))
    with pytest.raises(JobFailure):
        owner.verify({"trigger": "manual"}, JobContext("job-3"))
    snap = owner.snapshot()
    assert snap["current"]["status"] == "failed"
    # A failed newer request never displaces the last usable result.
    assert snap["previous"]["request_id"] == second["request_id"]
    for dto in published:
        valid_topic(dto)
        if dto["previous"] is not None:
            assert dto["previous"]["request_id"] != dto["current"]["request_id"]
            assert dto["previous"]["status"] in {"ok", "partial"}


# -- deadline -----------------------------------------------------------------

def test_deadline_watchdog_fails_and_late_result_is_discarded():
    release = threading.Event()
    port = FakePort()

    def hang(kwargs):
        release.wait(5)
        return payload(v4="203.0.113.9")
    port.results.append(hang)
    owner, published = make_owner(port, attempt_seconds=0.15)
    started = time.monotonic()
    with pytest.raises(JobFailure) as caught:
        owner.verify({"trigger": "manual"}, JobContext("job-1"))
    assert time.monotonic() - started < 1.5
    assert caught.value.code == "timeout"
    assert caught.value.error["message_key"] == "errors.exit.validation_timeout"
    current = owner.snapshot()["current"]
    assert current["status"] == "failed" and current["reason_code"] == "validation_timeout"
    deadline = port.calls[0]["deadline_monotonic"]
    assert deadline <= started + 0.15 + 0.05
    assert owner.orphan_probes == 1
    count = len(published)
    release.set()
    assert wait_for(lambda: owner.orphan_probes == 0)
    assert owner.snapshot()["current"] == current
    assert len(published) == count


def test_result_completed_after_deadline_is_a_timeout():
    port = FakePort()

    def late(kwargs):
        return {**payload(), "completed_monotonic": kwargs["deadline_monotonic"] + 1.0}
    port.results.append(late)
    owner, _ = make_owner(port)
    with pytest.raises(JobFailure) as caught:
        owner.verify({"trigger": "manual"}, JobContext("job-1"))
    assert caught.value.partial_result["reason_code"] == "validation_timeout"
    assert caught.value.partial_result["v4"]["address"] is None


# -- deferral -------------------------------------------------------------------

def test_starting_service_defers_without_executing_then_runs_when_ready():
    port = FakePort(lifecycle(phase="collecting", core_running=False, readiness="activation_hold"))
    port.results.append(payload())
    owner, published = make_owner(port)
    ctx, thread, box = run_async(owner)
    assert wait_for(lambda: owner.snapshot()["current"]["reason_code"] == "deferred")
    deferred = owner.snapshot()["current"]
    assert deferred["status"] == "running" and deferred["v4"]["status"] == "unknown"
    assert owner.progress()["deferred"] is True
    time.sleep(0.1)
    assert port.calls == []  # not executed while not ready
    port.life = lifecycle(phase="running", generation=2)
    owner.wake()
    thread.join(3)
    assert box["result"]["status"] == "partial"
    assert port.calls[0]["trigger"] == "deferred"
    assert box["result"]["generation"] == 2
    for dto in published:
        valid_topic(dto)


def test_deferral_ends_when_service_goes_offline():
    port = FakePort(lifecycle(phase="collecting", core_running=False))
    owner, _ = make_owner(port)
    ctx, thread, box = run_async(owner)
    assert wait_for(lambda: owner.snapshot()["current"]["reason_code"] == "deferred")
    port.life = lifecycle(phase="stopped", core_running=False)
    thread.join(3)
    assert box["failure"].code == "not_running"
    assert box["failure"].partial_result["reason_code"] == "service_stopped"
    assert port.calls == []


def test_deferral_window_is_not_extended():
    port = FakePort(lifecycle(phase="collecting", core_running=False))
    owner, _ = make_owner(port, defer_max_wait=0.2)
    started = time.monotonic()
    with pytest.raises(JobFailure) as caught:
        owner.verify({"trigger": "manual"}, JobContext("job-1"))
    assert caught.value.code == "needs_route"
    assert caught.value.partial_result["reason_code"] == "connection_not_ready"
    assert time.monotonic() - started < 1.5


def test_result_awaiting_connection_rearms_within_the_same_window():
    port = FakePort(lifecycle(phase="core_standby", readiness="activation_hold"))
    port.results.append(payload(v4=None, state="unavailable", error="connection_not_ready",
                                failure="connection_not_ready",
                                data_plane_reason="runtime_route_not_ready"))
    port.results.append(payload())
    owner, _ = make_owner(port, defer_retry_seconds=0.05)
    ctx, thread, box = run_async(owner)
    assert wait_for(lambda: len(port.calls) == 1)
    assert wait_for(lambda: owner.snapshot()["current"]["reason_code"] == "deferred")
    port.life = lifecycle(phase="running", revision=1)
    owner.wake()
    thread.join(3)
    assert box["result"]["status"] == "partial"
    assert [call["trigger"] for call in port.calls] == ["manual", "deferred"]


def test_pure_deferral_step_matches_qt_rules():
    ready = bx.owner_context(lifecycle())
    starting = bx.owner_context(lifecycle(phase="collecting", core_running=False))
    offline = bx.owner_context(lifecycle(phase="stopped", core_running=False))
    armed = bx.arm_deferral(None, 10.0, starting.signature, periodic=True)
    assert armed.deadline_monotonic == 130.0
    again = bx.arm_deferral(armed, 50.0, starting.signature, periodic=False)
    assert again.deadline_monotonic == 130.0 and again.periodic is False
    assert bx.deferral_step(armed, 11.0, starting)[0] == "wait"
    action, resumed = bx.deferral_step(armed, 11.0, ready)
    assert action == "resume" and resumed.attempts == 1
    assert bx.deferral_step(resumed, 12.0, ready)[0] == "wait"
    assert bx.deferral_step(resumed, 12.0 + 30.0, ready)[0] == "resume"
    assert bx.deferral_step(armed, 11.0, offline)[0] == "offline"
    assert bx.deferral_step(armed, 130.0, ready)[0] == "expired"
    exhausted = bx.Deferral(130.0, 12, True, (), 10.0)
    assert bx.deferral_step(exhausted, 20.0, ready)[0] == "expired"


# -- cancellation (owner) -------------------------------------------------------

def test_owner_cancel_revokes_only_ownership_and_drops_late_result():
    release = threading.Event()
    port = FakePort()

    def hang(kwargs):
        release.wait(5)
        return payload(v4="203.0.113.99")
    port.results.append(hang)
    port.results.append(payload(v4="203.0.113.5"))
    owner, published = make_owner(port)
    port.results.appendleft(payload(v4="203.0.113.1"))
    first = owner.verify({"trigger": "manual"}, JobContext("job-0"))
    ctx, thread, box = run_async(owner, JobContext("job-1"))
    assert wait_for(lambda: len(port.calls) == 2)
    ctx.owner_revoked.set()
    thread.join(2)
    cancelled = box["result"]
    assert cancelled["status"] == "cancelled" and cancelled["reason_code"] == "owner_revoked"
    snap = owner.snapshot()
    assert snap["current"]["status"] == "cancelled"
    # Only the ownership ended: both stacks stay unknown, not "cancelled".
    assert snap["current"]["v4"]["status"] == "unknown"
    assert snap["current"]["v6"]["status"] == "unknown"
    assert snap["previous"]["request_id"] == first["request_id"]
    assert snap["previous"]["fresh"] is False
    # The probe was not stopped: it is still running as an orphan.
    assert owner.orphan_probes == 1 and not release.is_set()
    # Probe-level single flight: no second runtime check while it runs.
    with pytest.raises(JobFailure) as caught:
        owner.verify({"trigger": "manual"}, JobContext("job-2"))
    assert caught.value.code == "conflict"
    assert caught.value.error["message_key"] == "errors.exit.in_progress"
    assert len(port.calls) == 2
    count = len(published)
    release.set()
    assert wait_for(lambda: owner.live_probes == 0)
    assert owner.orphan_probes == 0
    # The orphan's late result was discarded.
    assert owner.snapshot()["current"]["status"] == "cancelled"
    assert len(published) == count
    third = owner.verify({"trigger": "manual"}, JobContext("job-3"))
    assert third["v4"]["address"] == "203.0.113.5"
    assert all(dto["current"]["v4"]["address"] != "203.0.113.99" for dto in published)
    for dto in published:
        valid_topic(dto)


def test_job_gone_counts_as_owner_loss():
    release = threading.Event()
    port = FakePort()
    port.results.append(lambda kwargs: (release.wait(5), payload())[1])
    owner, _ = make_owner(port)
    alive = {"value": True}
    ctx, thread, box = run_async(owner, alive=lambda: alive["value"])
    assert wait_for(lambda: len(port.calls) == 1)
    alive["value"] = False
    thread.join(2)
    assert box["result"]["status"] == "cancelled"
    release.set()


def test_single_flight_refuses_a_second_request():
    release = threading.Event()
    port = FakePort()
    port.results.append(lambda kwargs: (release.wait(5), payload())[1])
    owner, _ = make_owner(port)
    ctx, thread, box = run_async(owner)
    assert wait_for(lambda: len(port.calls) == 1)
    with pytest.raises(JobFailure) as caught:
        owner.verify({"trigger": "manual"}, JobContext("job-2"))
    assert caught.value.code == "conflict"
    assert caught.value.error["message_key"] == "errors.exit.in_progress"
    assert len(port.calls) == 1
    release.set()
    thread.join(2)
    assert box["result"]["status"] == "partial"


class ConcurrencyPort(FakePort):
    """Counts runtime checks running at the same time."""

    def __init__(self):
        super().__init__()
        self.active = 0
        self.peak = 0
        self.gate = threading.Lock()
        self.release = threading.Event()

    def check_current_exit_identity(self, **kwargs):
        with self.gate:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            self.calls.append(kwargs)
            self.release.wait(5)
            return {**payload(), "request_id": kwargs["request_id"]}
        finally:
            with self.gate:
                self.active -= 1


def test_runtime_never_runs_two_manual_checks_at_once():
    port = ConcurrencyPort()
    owner, _ = make_owner(port)
    refused = 0
    for index in range(4):
        ctx, thread, box = run_async(owner, JobContext(f"job-{index}"))
        thread.join(0.3)
        if "failure" in box:
            assert box["failure"].code == "conflict"
            refused += 1
            continue
        assert wait_for(lambda: port.active == 1)
        ctx.owner_revoked.set()
        thread.join(2)
        assert box["result"]["status"] == "cancelled"
    # Only the first request reached the runtime; the orphan blocked the rest.
    assert port.peak == 1 and len(port.calls) == 1 and refused == 3
    port.release.set()
    assert wait_for(lambda: owner.live_probes == 0)
    assert owner.verify({"trigger": "manual"}, JobContext("job-9"))["status"] == "partial"
    assert port.peak == 1


def test_orphan_relaxation_is_explicit_and_bounded():
    port = ConcurrencyPort()
    owner, _ = make_owner(port, max_orphans=1)
    ctx, thread, box = run_async(owner)
    assert wait_for(lambda: port.active == 1)
    ctx.owner_revoked.set()
    thread.join(2)
    ctx2, thread2, box2 = run_async(owner, JobContext("job-2"))
    assert wait_for(lambda: port.active == 2)
    ctx2.owner_revoked.set()
    thread2.join(2)
    with pytest.raises(JobFailure) as caught:
        owner.verify({"trigger": "manual"}, JobContext("job-3"))
    assert caught.value.code == "conflict"
    port.release.set()
    assert wait_for(lambda: owner.orphan_probes == 0 and owner.live_probes == 0)


def test_cancel_then_immediate_retry_takes_over_without_conflict():
    port = FakePort(lifecycle(phase="collecting", core_running=False, readiness="activation_hold"))
    port.results.append(payload())
    # A long poll makes the stale-flight window obvious.
    owner, published = make_owner(port, poll_seconds=0.5, lifecycle_poll_seconds=0.5)
    ctx1, thread1, box1 = run_async(owner, JobContext("job-1"))
    assert wait_for(lambda: owner.snapshot()["current"]["reason_code"] == "deferred")
    first_id = owner.progress()["request_id"]
    ctx1.owner_revoked.set()
    ctx2, thread2, box2 = run_async(owner, JobContext("job-2"))
    # Taken over at once, well inside the old worker's 0.5 s poll.
    assert wait_for(lambda: (owner.progress() or {}).get("request_id") not in {None, first_id},
                    timeout=0.3)
    thread1.join(2)
    assert box1["result"]["status"] == "cancelled"
    assert "failure" not in box2
    port.life = lifecycle(phase="running")
    owner.wake()
    thread2.join(3)
    assert box2["result"]["status"] == "partial"
    assert len(port.calls) == 1
    assert owner.snapshot()["current"]["request_id"] == box2["result"]["request_id"]
    for dto in published:
        valid_topic(dto)


def test_late_progress_from_an_orphan_is_rejected():
    release = threading.Event()
    port = FakePort()

    def hang(kwargs):
        release.wait(5)
        kwargs["progress_callback"]({"phase": "late_phase"})
        return payload(v4="203.0.113.77")
    port.results.append(hang)
    owner, published = make_owner(port)
    ctx, thread, box = run_async(owner)
    assert wait_for(lambda: len(port.calls) == 1)
    ctx.owner_revoked.set()
    thread.join(2)
    cancelled = owner.snapshot()["current"]
    count = len(published)
    release.set()
    assert wait_for(lambda: owner.live_probes == 0)
    assert owner.snapshot()["current"] == cancelled
    assert len(published) == count


def test_revoke_between_owner_check_and_commit_drops_the_result(monkeypatch):
    port = FakePort()
    port.results.append(payload(v4="203.0.113.66"))
    owner, published = make_owner(port)
    ctx = JobContext("job-1")
    original = owner._commit

    def racing_commit(flight, result, probe=None):
        if result.get("status") in {"ok", "partial"}:
            ctx.owner_revoked.set()  # revoke lands after _finish's _owned()
        return original(flight, result, probe)
    monkeypatch.setattr(owner, "_commit", racing_commit)
    result = owner.verify({"trigger": "manual"}, ctx)
    assert result["status"] == "cancelled"
    assert owner.snapshot()["current"]["status"] == "cancelled"
    assert all(dto["current"]["v4"]["address"] != "203.0.113.66" for dto in published)


def test_revoked_before_first_running_keeps_previous():
    port = FakePort()
    port.results.append(payload(v4="203.0.113.1"))
    owner, published = make_owner(port)
    first = owner.verify({"trigger": "manual"}, JobContext("job-1"))
    ctx = JobContext("job-2")
    ctx.owner_revoked.set()
    result = owner.verify({"trigger": "manual"}, ctx)
    assert result["status"] == "cancelled"
    snap = owner.snapshot()
    assert snap["current"]["status"] == "cancelled"
    assert snap["previous"]["request_id"] == first["request_id"]
    assert snap["previous"]["fresh"] is False
    assert len(port.calls) == 1  # a revoked request never starts a runtime check
    for dto in published:
        valid_topic(dto)


# -- 429 / Retry-After ----------------------------------------------------------

def test_retry_after_retry_shares_the_original_deadline():
    port = FakePort()
    port.results.append(payload(v4=None, state="unavailable", error="identity_rate_limited",
                                failure="identity_rate_limited", retry_after=0.1))
    port.results.append(payload(v4="203.0.113.8"))
    owner, published = make_owner(port, attempt_seconds=2.0, rate_limit_min_attempt=0.2,
                                  rate_limit_retries=1)
    result = owner.verify({"trigger": "manual"}, JobContext("job-1"))
    assert result["status"] == "partial"
    assert len(port.calls) == 2
    first, second = port.calls
    assert second["deadline_monotonic"] == first["deadline_monotonic"]
    assert second["timeout_seconds"] < first["timeout_seconds"]
    assert second["request_id"] == first["request_id"]
    assert any(dto["current"]["reason_code"] == "rate_limited" for dto in published)
    # The 429 pause survives the successful retry.
    assert owner.scheduler._pause_until > time.monotonic()


def test_rate_limit_is_not_retried_by_default_like_qt():
    assert bx.RATE_LIMIT_RETRIES == 0
    port = FakePort()
    port.results.append(payload(v4=None, state="unavailable", error="identity_rate_limited",
                                failure="identity_rate_limited", retry_after=0.1))
    owner, _ = make_owner(port)
    with pytest.raises(JobFailure) as caught:
        owner.verify({"trigger": "manual"}, JobContext("job-1"))
    assert len(port.calls) == 1
    assert caught.value.partial_result["reason_code"] == "identity_rate_limited"
    assert owner.scheduler._pause_until > time.monotonic()


def test_retry_after_beyond_deadline_fails_and_pauses_automatic_lookups():
    port = FakePort()
    port.results.append(payload(v4=None, state="unavailable", error="identity_rate_limited",
                                failure="identity_rate_limited", retry_after=900.0))
    owner, _ = make_owner(port)
    with pytest.raises(JobFailure) as caught:
        owner.verify({"trigger": "manual"}, JobContext("job-1"))
    assert len(port.calls) == 1
    assert caught.value.code == "timeout"
    assert caught.value.partial_result["reason_code"] == "identity_rate_limited"
    assert caught.value.error["message_key"] == "errors.exit.identity_rate_limited"
    from overview_auto_identity import AutoLookupInputs
    now = time.monotonic()
    decision = owner.scheduler.decide(AutoLookupInputs(
        now=now + 600.0, enabled=True, visible=True, readiness_state="ready",
        validation_active=False, route_confirmed=True,
        binding_marker="a1b2c3d4e5f6a1b2c3d4e5f6", core_instance="1:1:101", busy=False,
        current_result_age=None))
    assert decision.start is False and decision.reason == "rate_limited"


# -- generation / binding mismatch ---------------------------------------------

def test_core_generation_change_at_completion_is_discarded():
    port = FakePort()

    def switch_generation(kwargs):
        port.life = lifecycle(generation=2)
        return payload(v4="203.0.113.50")
    port.results.append(switch_generation)
    owner, published = make_owner(port, lifecycle_poll_seconds=60.0)
    ctx = JobContext("job-1")
    with pytest.raises(JobFailure):
        owner.verify({"trigger": "manual"}, ctx)
    assert ctx.superseded.is_set()
    current = owner.snapshot()["current"]
    assert current["status"] == "superseded"
    assert current["reason_code"] == "core_generation_changed"
    assert current["v4"]["address"] is None and current["v6"]["address"] is None
    assert all(dto["current"]["v4"]["address"] != "203.0.113.50" for dto in published)
    for dto in published:
        valid_topic(dto)


def test_lifecycle_change_during_probe_supersedes_early():
    release = threading.Event()
    port = FakePort()
    port.results.append(lambda kwargs: (release.wait(5), payload())[1])
    owner, _ = make_owner(port)
    ctx, thread, box = run_async(owner)
    assert wait_for(lambda: len(port.calls) == 1)
    port.life = lifecycle(revision=2)
    thread.join(2)
    assert ctx.superseded.is_set() and box["failure"].code == "conflict"
    assert owner.snapshot()["current"]["reason_code"] == "lifecycle_changed"
    assert owner.orphan_probes == 1
    release.set()
    assert wait_for(lambda: owner.orphan_probes == 0)


def test_route_binding_change_is_discarded_when_runtime_reports_binding():
    port = FakePort()
    port.binding = {"binding_marker": "ffffffffffffffffffffffff"}
    port.results.append(payload())
    owner, _ = make_owner(port)
    ctx = JobContext("job-1")
    with pytest.raises(JobFailure):
        owner.verify({"trigger": "manual"}, ctx)
    assert ctx.superseded.is_set()
    assert owner.snapshot()["current"]["reason_code"] == "route_changed"


def test_lease_owner_mismatch_pure():
    lease = bx.ExitLease("r", 10.0, "manual", 3, 4, 5)
    assert bx.lease_owner_mismatch(lease, bx.owner_context(
        lifecycle(revision=3, network=4, generation=5))) is None
    assert bx.lease_owner_mismatch(lease, bx.owner_context(
        lifecycle(revision=4, network=4, generation=5))) == "lifecycle_changed"
    assert bx.lease_owner_mismatch(lease, bx.owner_context(
        lifecycle(revision=3, network=5, generation=5))) == "network_changed"
    assert bx.lease_owner_mismatch(lease, bx.owner_context(
        lifecycle(revision=3, network=4, generation=6))) == "core_generation_changed"
    assert bx.lease_is_active(lease, 10.2) and not bx.lease_is_active(lease, 10.3)
    assert not bx.lease_is_active(bx.lease_terminal(lease, "unavailable"), 1.0)


# -- Fallback results -----------------------------------------------------------

def _project(raw):
    return bx.identity_result_from_payload({**raw, "request_id": "r1"}, request_id="r1",
                                           generation=3, fallback_ms=1)


def test_route_contract_failed_maps_to_exit_message():
    result = _project(payload(v4="203.0.113.7", state="data_plane_failed",
                              failure="route_contract_failed"))
    assert result["status"] == "failed" and result["reason_code"] == "route_contract_failed"
    assert result["v4"]["address"] is None
    assert bx.exit_error(result) == {"code": "needs_route",
                                     "message_key": "errors.exit.route_contract_failed",
                                     "params": {}, "reason_code": "route_contract_failed"}


def test_native_fallback_verified_is_kept_and_not_called_an_app_switch():
    result = _project(payload(v4="203.0.113.7", switch_reason="native_fallback_verified"))
    assert result["status"] == "partial" and bx.result_is_done(result)
    assert result["native_fallback_verified"] is True
    assert result["reason_code"] == "native_fallback_verified"
    assert bx.identity_message_keys(result) == ["errors.exit.native_fallback_verified"]
    # The DTO is closed: no field may claim that the application selected a node.
    # (T-P4-C2 #2: the optional trigger is added when the owner installs it.)
    assert set(result) == {name.rstrip("?") for name in protocol.V1_DTOS["IdentityResult"]} - {"trigger"}
    assert protocol.validate_topic_payload("exit.identity", {"current": result,
                                                             "previous": None}) == []


@pytest.mark.parametrize("flag", bx.FALLBACK_FLAGS)
def test_other_fallback_flags_map_message_keys(flag):
    result = _project(payload(v4=None, state="data_plane_failed", failure="exit_unreachable",
                              switch_reason=flag))
    assert result[flag] is True and result["status"] == "failed"
    # The Fallback flag is the primary reason; the runtime reason stays on the legs.
    assert result["reason_code"] == flag
    assert result["v4"]["reason_code"] == "exit_unreachable"
    assert result["v6"]["reason_code"] == "exit_unreachable"
    error = bx.exit_error(result)
    assert error["code"] == "needs_route"
    assert error["message_key"] == f"errors.exit.{flag}"
    assert error["params"] == {}
    assert bx.identity_message_keys(result) == [f"errors.exit.{flag}"]
    assert bx.exit_message_key(flag) == f"errors.exit.{flag}"


def test_fallback_failure_priority():
    for flags, expected in [
        (["native_fallback_verified", "manual_execution_binding_stale"],
         "manual_execution_binding_stale"),
        (["native_fallback_verified", "native_fallback_verified_commit_rejected"],
         "native_fallback_verified_commit_rejected"),
        (["native_fallback_verified", "native_fallback_unverified_budget"],
         "native_fallback_unverified_budget"),
    ]:
        raw = payload(v4=None, state="data_plane_failed", failure="exit_unreachable",
                      switch_reason=flags[0])
        raw["error"] = flags[1]
        result = _project(raw)
        assert result["reason_code"] == expected, flags
        assert all(result[flag] for flag in flags)
        assert bx.exit_error(result)["params"] == {
            "fallback": [flag for flag in bx.FALLBACK_FLAGS if flag in flags and flag != expected]}


@pytest.mark.parametrize("warning", sorted(bx.WARNING_REASONS))
def test_usable_result_with_warning_is_not_a_clean_success(warning):
    raw = payload(v4="203.0.113.7", state="partial")
    raw["diagnostics"]["overview_warning_reason"] = warning
    result = _project(raw)
    # The address stays visible (Qt overview_validation_view.py:131-133) ...
    assert result["status"] == "partial" and result["v4"]["address"] == "203.0.113.7"
    # ... but the warning is reported and the result is never fresh.
    assert result["reason_code"] == warning
    assert result["fresh"] is False
    assert f"errors.exit.{warning}" in bx.identity_message_keys(result)
    assert bx.exit_failure_code(warning) == "needs_route"
    assert protocol.validate_topic_payload("exit.identity", {"current": result,
                                                             "previous": None}) == []


def test_missing_freshness_is_not_fresh_and_running_has_no_message_keys():
    raw = payload(v4="203.0.113.7")
    del raw["freshness"]
    assert _project(raw)["fresh"] is False
    running = bx.running_result("r", 1, "querying")
    assert bx.identity_message_keys(running) == []
    assert bx.identity_message_keys(bx.idle_result()) == []
    cancelled = bx.closed_result("r", 1, "cancelled", "owner_revoked")
    assert cancelled["v4"]["status"] == cancelled["v6"]["status"] == "unknown"


def test_message_keys_reuse_bridge_vocab_rule():
    from bridge_vocab import error_message_key
    for reason in bx.EXIT_REASON_CODES:
        assert bx.exit_message_key(reason) == error_message_key(reason, domain="exit")


def test_manual_execution_binding_stale_never_admits_identity():
    result = _project(payload(v4="203.0.113.7", switch_reason="manual_execution_binding_stale"))
    assert result["manual_execution_binding_stale"] is True
    assert result["status"] == "failed" and not bx.result_is_done(result)
    assert result["reason_code"] == "manual_execution_binding_stale"
    assert result["v4"]["address"] is None
    assert bx.exit_error(result)["message_key"] == "errors.exit.manual_execution_binding_stale"


def test_message_keys_are_protocol_valid():
    for reason in bx.EXIT_REASON_CODES:
        key = bx.exit_message_key(reason)
        assert protocol._MESSAGE_KEY.fullmatch(key)
        assert bx.exit_failure_code(reason) in (
            protocol.ERROR_CODES & ({"not_running", "needs_route", "timeout"}
                                    | {"conflict", "runtime_unavailable"}))
    assert bx.exit_message_key("Traceback: boom") == bx.DEFAULT_MESSAGE_KEY


# -- wiring through the real executor and publication state ---------------------

def _session(port):
    recorder = JobEventRecorder()
    executor = JobExecutor(recorder, epoch="epoch-1", require_acceptance=True)
    hooks = []
    session = SimpleNamespace(executor=executor, state=PublicationState("epoch-1"),
                              register_owner_revoke_hook=hooks.append)
    owner = bx.register(session, port=port)
    return session, owner, recorder, hooks


def test_register_runs_exit_verify_as_owner_cancellable_job():
    port = FakePort()
    port.results.append(payload(v4="203.0.113.7"))
    session, owner, recorder, hooks = _session(port)
    try:
        assert hooks and callable(hooks[0])
        seed = session.state.subscribe(["exit.identity"])
        assert seed and seed[0]["data"]["current"]["status"] == "idle"
        ack = session.executor.submit("exit.verify", {"trigger": "manual"}, req_id="r1",
                                      owner="w1")
        assert ack.accepted
        assert session.executor.acceptance_sent(ack.op, epoch="epoch-1")
        job = session.executor.wait(ack.op, 5)
        assert job["state"] == "done", job
        assert job["result"]["status"] == "partial"
        events = recorder.prepare_for_send(0)
        for event in events:
            assert protocol.validate_frame(event) == [], event
        latest = session.state.snapshot("exit.identity")
        assert latest["data"]["current"]["request_id"] == job["result"]["request_id"]
    finally:
        session.executor.shutdown(time.monotonic() + 1)


def test_register_cancel_and_failure_paths():
    release = threading.Event()
    port = FakePort()
    port.results.append(lambda kwargs: (release.wait(5), payload())[1])
    port.results.append(payload(v4=None, state="data_plane_failed",
                                failure="route_contract_failed"))
    session, owner, recorder, _ = _session(port)
    try:
        ack = session.executor.submit("exit.verify", {"trigger": "manual"}, req_id="r1",
                                      owner="w1")
        session.executor.acceptance_sent(ack.op, epoch="epoch-1")
        assert wait_for(lambda: len(port.calls) == 1)
        assert session.executor.cancel(ack.op, owner="w1").accepted
        assert session.executor.wait(ack.op, 2)["state"] == "cancelled"
        assert wait_for(lambda: owner.snapshot()["current"]["status"] == "cancelled")
        # The orphan probe still runs: a new request is refused, not started.
        busy = session.executor.submit("exit.verify", {"trigger": "manual"}, req_id="r1b",
                                       owner="w1")
        session.executor.acceptance_sent(busy.op, epoch="epoch-1")
        assert session.executor.wait(busy.op, 2)["error"]["code"] == "conflict"
        assert len(port.calls) == 1
        release.set()
        assert wait_for(lambda: owner.live_probes == 0)
        ack2 = session.executor.submit("exit.verify", {"trigger": "manual"}, req_id="r2",
                                       owner="w1")
        session.executor.acceptance_sent(ack2.op, epoch="epoch-1")
        job = session.executor.wait(ack2.op, 5)
        assert job["state"] == "failed"
        assert job["error"]["code"] == "needs_route"
        assert job["result"]["reason_code"] == "route_contract_failed"
        for event in recorder.prepare_for_send(0):
            assert protocol.validate_frame(event) == [], event
    finally:
        release.set()
        session.executor.shutdown(time.monotonic() + 1)


FIXTURES = Path(__file__).resolve().parents[1] / "bridge_schema" / "fixtures" / "v1" / "valid"


@pytest.mark.parametrize("reason", ["route_contract_failed", *bx.FALLBACK_FLAGS])
def test_real_executor_failed_frames_match_contract_fixtures(reason):
    fixture = json.loads((FIXTURES / f"exit_{reason}.json").read_text(encoding="utf-8"))["data"]
    port = FakePort()
    if reason == "route_contract_failed":
        port.results.append(payload(v4=None, state="data_plane_failed",
                                    failure="route_contract_failed"))
    else:
        port.results.append(payload(v4=None, state="data_plane_failed",
                                    failure="exit_unreachable", switch_reason=reason))
    session, owner, recorder, _ = _session(port)
    try:
        ack = session.executor.submit("exit.verify", {"trigger": "manual"}, req_id="r1",
                                      owner="w1")
        session.executor.acceptance_sent(ack.op, epoch="epoch-1")
        assert session.executor.wait(ack.op, 5)["state"] == "failed"
        frames = [event for event in recorder.prepare_for_send(0)
                  if event.get("topic") == "jobs" and event["data"].get("state") == "failed"]
        assert len(frames) == 1
        frame = frames[0]
        assert protocol.validate_frame(frame) == [], frame
        data = frame["data"]
        assert data["name"] == fixture["name"] and data["state"] == fixture["state"]
        assert data["error"]["code"] == fixture["error"]["code"] == "needs_route"
        result = data["result"]
        for key in ("status", "reason_code", "fresh", *bx.FALLBACK_FLAGS):
            assert result[key] == fixture["result"][key], key
        assert set(result) == set(fixture["result"])
        # The owner's own Error DTO equals the fixture's (message_key included);
        # the executor currently substitutes errors.<code> (report §9.1 / S-4).
        expected = fixture["error"]
        produced = bx.exit_error(result)
        assert {key: produced[key] for key in expected} == expected
    finally:
        session.executor.shutdown(time.monotonic() + 1)


# -- freshness after completion, publish failure, back-off key -------------------

def test_core_generation_change_after_completion_expires_freshness():
    port = FakePort()
    port.results.append(payload(v4="203.0.113.7"))
    owner, published = make_owner(port)
    owner.verify({"trigger": "manual"}, JobContext("job-1"))
    assert owner.snapshot()["current"]["fresh"] is True
    count = len(published)
    owner.notify_lifecycle(lifecycle(generation=1))
    assert len(published) == count  # same Core: nothing to republish
    owner.notify_lifecycle(lifecycle(generation=2))
    current = owner.snapshot()["current"]
    assert current["fresh"] is False and current["v4"]["address"] == "203.0.113.7"
    assert len(published) == count + 1 and published[-1]["current"]["fresh"] is False
    owner.notify_lifecycle(lifecycle(generation=3))
    assert len(published) == count + 1
    valid_topic(published[-1])


def test_publish_failure_withdraws_the_baseline():
    port = FakePort()
    port.results.append(payload(v4="203.0.113.7"))
    invalidated = []
    calls = {"n": 0}

    def flaky(dto):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("frame_too_large")
    owner = bx.ExitIdentityOwner(port, publish=flaky, invalidate=lambda: invalidated.append(1),
                                 poll_seconds=0.01, lifecycle_poll_seconds=0.02)
    owner.verify({"trigger": "manual"}, JobContext("job-1"))
    assert owner.publish_errors == 1 and invalidated == [1]
    assert calls["n"] >= 2  # the next change republished a complete DTO


def test_publish_failure_reports_gap_through_publication_state():
    port = FakePort()
    session, owner, _recorder, _ = _session(port)
    try:
        session.state.subscribe(["exit.identity"])
        session.state.record_edge = lambda topic, dto: (_ for _ in ()).throw(ValueError("x"))
        owner._current = bx.running_result("r", 1, "querying")
        owner._publish()
        assert owner.publish_errors == 1
        assert session.state.baseline_unavailable("exit.identity")
    finally:
        session.executor.shutdown(time.monotonic() + 1)


def test_click_lifts_back_off_for_the_real_binding_and_core_instance():
    port = FakePort()
    port.binding = {"binding_marker": "a1b2c3d4e5f6a1b2c3d4e5f6", "core_instance": "1:1:101"}
    port.results.append(payload())
    seen = []

    class Spy(bx.OverviewAutoIdentityScheduler):
        def record_user_request(self, **kwargs):
            seen.append(kwargs)
            super().record_user_request(**kwargs)
    owner, _ = make_owner(port, scheduler=Spy())
    owner.verify({"trigger": "manual"}, JobContext("job-1"))
    assert seen == [{"binding_marker": "a1b2c3d4e5f6a1b2c3d4e5f6", "core_instance": "1:1:101"}]


# -- adapter and stub -------------------------------------------------------------

def test_runtime_port_uses_public_methods_only():
    calls = []

    class Runtime:
        def lifecycle_status(self):
            calls.append("lifecycle_status")
            return lifecycle()

        def connection_info(self):
            calls.append("connection_info")
            return {"mixed_port": 7897}

        def check_current_exit_identity(self, **kwargs):
            calls.append("check_current_exit_identity")
            return {"request_id": kwargs["request_id"]}

        def current_route_context(self, **kwargs):  # pragma: no cover - must not be used
            raise AssertionError("bridge must not read Mihomo")

    port = bx.RuntimeExitPort(Runtime())
    assert port.proxy_url() == "http://127.0.0.1:7897"
    assert port.exit_binding() is None
    assert port.check_current_exit_identity(request_id="x") == {"request_id": "x"}
    assert calls == ["lifecycle_status", "connection_info", "check_current_exit_identity"]


def test_schedule_auto_is_a_stub():
    owner, _ = make_owner(FakePort())
    decision = owner.schedule_auto({"visible": True, "focused": False, "page": "overview"},
                                   lifecycle(), {})
    assert decision.start is False and decision.reason == "not_implemented"


def test_rejects_non_manual_trigger():
    owner, _ = make_owner(FakePort())
    with pytest.raises(JobFailure) as caught:
        owner.verify({"trigger": "auto"}, JobContext("job-1"))
    assert caught.value.code == "validation"


def test_unexpected_error_never_leaves_progress_running():
    port = FakePort()

    def boom(kwargs):
        raise RuntimeError("boom")
    port.results.append(boom)
    owner, published = make_owner(port)
    original = bx.identity_result_from_payload
    with pytest.raises(JobFailure) as caught:
        owner.verify({"trigger": "manual"}, JobContext("job-1"))
    assert caught.value.partial_result["reason_code"] == "lookup_unavailable"
    seen = []

    def broken_probe(flight, lease, trigger):
        seen.append(owner.snapshot()["current"]["status"])
        raise ValueError("bug")
    owner._probe = broken_probe
    with pytest.raises(ValueError):
        owner.verify({"trigger": "manual"}, JobContext("job-2"))
    assert seen == ["running"]
    current = owner.snapshot()["current"]
    assert current["status"] == "failed" and current["reason_code"] == "lookup_unavailable"
    assert published[-1]["current"] == current
    assert owner.progress() is None
    assert original is bx.identity_result_from_payload
