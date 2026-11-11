"""T-P2-17: automatic exit lookups, node diagnostics and their cancellation."""
from __future__ import annotations

from collections import deque
import threading
import time
from types import SimpleNamespace

import pytest

import bridge_diagnostics as bd
import bridge_exit as bx
import bridge_protocol as protocol
from bridge_jobs import CancellationReport, JobContext, JobEventRecorder, JobExecutor, JobFailure
from bridge_state import PublicationState

BIND = "a1b2c3d4e5f6a1b2c3d4e5f6"
INSTANCE = "1:1:101"
ROUTE = {"publication_revision": 3, "valid": True}
VISIBLE = {"visible": True, "focused": False, "page": "overview"}


# --------------------------------------------------------------------------
# helpers: exit identity
# --------------------------------------------------------------------------

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


def payload(*, v4="203.0.113.7", binding=BIND, state="complete", error=None, failure=None,
            retry_after=None):
    def family(ip):
        if not ip:
            item = {"state": "unavailable", "ip": None, "evidence": []}
        else:
            item = {"state": "confirmed", "ip": ip, "evidence": [],
                    "location": {"country_code": "JP", "state": "reported"}}
        if retry_after is not None:
            item["evidence"] = [{"source": "ident.me", "attempts": [
                {"status": "rate_limited", "transport_retries": [
                    {"retry_after_seconds": retry_after}]}]}]
        return item

    diagnostics = {"overview_failure_reason": failure} if failure else {}
    result = {
        "schema_version": 2, "state": state, "checked_at": "2026-10-06T01:02:03+00:00",
        "freshness": {"state": "fresh", "reason": "auto_lookup"},
        "route": {"binding_marker": binding, "core_instance": INSTANCE, "confirmed": True},
        "identity": {"ipv4": family(v4), "ipv6": family(None)},
        "diagnostics": diagnostics,
    }
    if error:
        result["error"] = error
    return result


class Clock:
    def __init__(self, start=1000.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class ExitPort:
    def __init__(self):
        self.life = lifecycle()
        self.results: deque = deque()
        self.calls: list[dict] = []
        # ``confirmed`` is a required getter field (SHOULD-5).
        self.binding = {"binding_marker": BIND, "core_instance": INSTANCE, "confirmed": True}
        self.enabled = True
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()
        self.called = threading.Event()

    def lifecycle_status(self):
        return self.life

    def proxy_url(self):
        return "http://127.0.0.1:7890"

    def exit_binding(self):
        return self.binding

    def auto_lookup_enabled(self):
        return self.enabled

    def check_current_exit_identity(self, **kwargs):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append(kwargs)
        self.called.set()
        try:
            item = self.results.popleft()
            if callable(item):
                item = item(kwargs)
            return {**item, "request_id": kwargs["request_id"]}
        finally:
            with self.lock:
                self.active -= 1


def make_owner(port, clock=None, *, threaded=False, **overrides):
    published = []
    threads = []

    def auto_submit(work):
        if threaded:
            thread = threading.Thread(target=work, daemon=True)
            threads.append(thread)
            thread.start()
        else:
            work()
        return True

    options = dict(publish=published.append, attempt_seconds=2.0, poll_seconds=0.01,
                   lifecycle_poll_seconds=0.02, grace_seconds=0.05, defer_max_wait=2.0,
                   auto_submit=auto_submit)
    if clock is not None:
        options["monotonic"] = clock
    options.update(overrides)
    owner = bx.ExitIdentityOwner(port, **options)
    owner.threads = threads
    return owner, published


def prime(owner, clock, *, presence=VISIBLE, route=ROUTE, sample=None):
    """Feed the three observations, then let the binding settle (5 s)."""
    owner.observe_presence(presence)
    owner.observe_route(route)
    owner.notify_lifecycle(sample if sample is not None else lifecycle())
    clock.advance(6.0)
    return owner.schedule_auto()


def wait_for(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def valid_topic(dto):
    assert protocol.validate_topic_payload("exit.identity", dto) == [], dto


# --------------------------------------------------------------------------
# automatic scheduling: presence x ready x binding
# --------------------------------------------------------------------------

@pytest.mark.parametrize("case, expect", [
    ({}, "start"),
    ({"presence": {"visible": True, "focused": True, "page": "overview"}}, "start"),
    ({"presence": {"visible": False, "focused": False, "page": "overview"}}, "not_visible"),
    ({"presence": {"visible": True, "focused": True, "page": "nodes"}}, "not_visible"),
    ({"sample": lifecycle(readiness="serving")}, "not_ready"),
    ({"sample": lifecycle(validation_active=True)}, "not_ready"),
    ({"sample": lifecycle(phase="collecting", core_running=False)}, "busy"),
    ({"binding": None}, "not_ready"),
    ({"binding": {"binding_marker": BIND, "core_instance": INSTANCE, "confirmed": False}},
     "not_ready"),
    # SHOULD-5: confirmed is required and read like Qt (``is True``).
    ({"binding": {"binding_marker": BIND, "core_instance": INSTANCE}}, "not_ready"),
    ({"route": {"publication_revision": 3, "valid": False}}, "not_ready"),
    ({"enabled": False}, "disabled"),
])
def test_auto_lookup_needs_visible_overview_ready_service_and_settled_binding(case, expect):
    clock = Clock()
    port = ExitPort()
    port.results.append(payload())
    if "binding" in case:
        port.binding = case["binding"]
    if "enabled" in case:
        port.enabled = case["enabled"]
    owner, published = make_owner(port, clock)
    decision = prime(owner, clock, presence=case.get("presence", VISIBLE),
                     route=case.get("route", ROUTE), sample=case.get("sample"))
    assert decision.reason == expect
    if expect == "start":
        # focused=False never blocks (PROTOCOL §13); expected marker is the
        # decided binding; the identity-only automatic path is used.
        assert len(port.calls) == 1
        call = port.calls[0]
        assert call["trigger"] == "auto" and call["expected_binding_marker"] == BIND
        assert owner.snapshot()["current"]["status"] == "partial"
    else:
        assert port.calls == [] and published == []


def test_binding_must_settle_and_a_binding_change_restarts_the_window():
    clock = Clock()
    port = ExitPort()
    port.results.append(payload())
    owner, _ = make_owner(port, clock)
    owner.observe_presence(VISIBLE)
    owner.observe_route(ROUTE)
    owner.notify_lifecycle(lifecycle())
    clock.advance(3.0)
    assert owner.schedule_auto().reason == "wait_settle"
    port.binding = {"binding_marker": "b" * 24, "core_instance": INSTANCE, "confirmed": True}
    clock.advance(3.0)
    assert owner.schedule_auto().reason == "wait_settle"      # new binding, new window
    port.binding = {"binding_marker": BIND, "core_instance": INSTANCE, "confirmed": True}
    owner.schedule_auto()
    clock.advance(5.5)
    assert owner.schedule_auto().reason == "start"
    assert port.calls and port.calls[0]["expected_binding_marker"] == BIND


def test_presence_lifecycle_and_route_observations_each_trigger_a_decision():
    clock = Clock()
    port = ExitPort()
    port.results.append(payload())
    owner, _ = make_owner(port, clock)
    owner.observe_route(ROUTE)
    owner.notify_lifecycle(lifecycle())
    clock.advance(6.0)
    assert port.calls == []
    owner.observe_presence(VISIBLE)              # the shell's presence alone starts it
    assert len(port.calls) == 1
    assert owner.last_auto_decision.reason == "start"


def test_auto_lookup_publishes_nothing_while_running_then_installs_usable_result():
    clock = Clock()
    port = ExitPort()
    port.results.append(payload(v4="198.51.100.9"))
    owner, published = make_owner(port, clock)
    assert prime(owner, clock).start
    statuses = [dto["current"]["status"] for dto in published]
    assert statuses == ["partial"]                  # no running edge for an automatic lookup
    for dto in published:
        valid_topic(dto)
    assert owner.snapshot()["current"]["v4"]["address"] == "198.51.100.9"
    # The automatic result is current for this binding for 30 minutes.
    clock.advance(60.0)
    assert owner.schedule_auto().reason == "fresh"
    assert len(port.calls) == 1


def test_scheduling_never_waits_for_the_network():
    clock = Clock()
    port = ExitPort()
    release = threading.Event()
    port.results.append(lambda kwargs: (release.wait(5), payload())[1])
    owner, _ = make_owner(port, clock, threaded=True)
    started = time.monotonic()
    assert prime(owner, clock).start
    assert time.monotonic() - started < 0.5
    assert port.called.wait(2)
    # While the lookup runs, every further observation answers at once.
    started = time.monotonic()
    assert owner.schedule_auto(presence=VISIBLE).reason == "busy"
    assert time.monotonic() - started < 0.5
    release.set()
    assert wait_for(lambda: owner.snapshot()["current"]["status"] == "partial")


def test_owner_without_a_runner_never_starts_automatic_lookups():
    clock = Clock()
    port = ExitPort()
    owner, _ = make_owner(port, clock, auto_submit=None)
    decision = prime(owner, clock)
    assert decision.start is False and decision.reason == bx.AUTO_NOT_WIRED
    assert port.calls == []


# --------------------------------------------------------------------------
# 429 / Retry-After
# --------------------------------------------------------------------------

def test_automatic_429_pauses_automatic_lookups_for_retry_after():
    clock = Clock()
    port = ExitPort()
    port.results.append(payload(v4=None, state="unavailable", error="identity_rate_limited",
                                failure="identity_rate_limited", retry_after=900.0))
    port.results.append(payload())
    owner, _ = make_owner(port, clock)
    assert prime(owner, clock).start
    assert len(port.calls) == 1
    clock.advance(60.0)                 # past spacing (20 s) and first back-off (30 s)
    decision = owner.schedule_auto()
    assert decision.start is False and decision.reason == "rate_limited"
    assert decision.retry_at == pytest.approx(clock.t - 60.0 + 900.0)
    assert len(port.calls) == 1
    clock.advance(900.0)
    assert owner.schedule_auto().reason == "start"
    assert len(port.calls) == 2


def test_manual_429_also_pauses_automatic_lookups():
    clock = Clock()
    port = ExitPort()
    port.results.append(payload(v4=None, state="unavailable", error="identity_rate_limited",
                                failure="identity_rate_limited", retry_after=600.0))
    owner, _ = make_owner(port, clock)
    with pytest.raises(JobFailure):
        owner.verify({"trigger": "manual"}, JobContext("job-1"))
    decision = prime(owner, clock)
    clock.advance(60.0)
    decision = owner.schedule_auto()
    assert decision.reason == "rate_limited" and len(port.calls) == 1


# --------------------------------------------------------------------------
# manual upgrade
# --------------------------------------------------------------------------

def test_manual_request_upgrades_a_running_automatic_lookup():
    clock = Clock()
    port = ExitPort()
    release = threading.Event()
    port.results.append(lambda kwargs: (release.wait(5), payload(v4="192.0.2.50"))[1])
    port.results.append(payload(v4="203.0.113.80"))
    owner, published = make_owner(port, clock, threaded=True)
    assert prime(owner, clock).start
    assert port.called.wait(2)
    assert owner.progress()["trigger"] == "auto"
    box = {}

    def manual():
        try:
            box["result"] = owner.verify({"trigger": "manual"}, JobContext("job-m"))
        except JobFailure as exc:
            box["failure"] = exc
    thread = threading.Thread(target=manual, daemon=True)
    thread.start()
    # The automatic request yields at once; the manual one is published as
    # running while the automatic probe finishes; no second runtime check.
    assert wait_for(lambda: (owner.progress() or {}).get("trigger") == "manual")
    assert wait_for(lambda: owner.snapshot()["current"]["reason_code"] == "upgrading")
    # SHOULD-8: the upgrading progress carries the Core generation, not 0.
    assert owner.snapshot()["current"]["generation"] == 1
    assert len(port.calls) == 1
    release.set()
    thread.join(5)
    assert "failure" not in box, box
    result = box["result"]
    assert result["status"] == "partial" and result["v4"]["address"] == "203.0.113.80"
    assert [call["trigger"] for call in port.calls] == ["auto", "manual"]
    assert port.calls[1]["expected_binding_marker"] == ""
    assert port.peak == 1                                   # never two checks at once
    # The yielded automatic result never reached the topic.
    assert all(dto["current"]["v4"]["address"] != "192.0.2.50" for dto in published)
    for dto in published:
        valid_topic(dto)


def test_manual_request_without_auto_in_flight_is_unchanged_single_flight():
    port = ExitPort()
    release = threading.Event()
    port.results.append(lambda kwargs: (release.wait(5), payload())[1])
    owner, _ = make_owner(port)
    box = {}
    first = threading.Thread(target=lambda: box.setdefault(
        "first", owner.verify({"trigger": "manual"}, JobContext("job-1"))), daemon=True)
    first.start()
    assert port.called.wait(2)
    with pytest.raises(JobFailure) as caught:
        owner.verify({"trigger": "manual"}, JobContext("job-2"))
    assert caught.value.code == "conflict"
    release.set()
    first.join(5)


# --------------------------------------------------------------------------
# automatic outcomes keep the card (Qt rules)
# --------------------------------------------------------------------------

def test_failed_automatic_refresh_never_replaces_a_usable_result():
    clock = Clock()
    port = ExitPort()
    port.results.append(payload(v4="203.0.113.7"))
    port.results.append(payload(v4=None, state="data_plane_failed", error="exit_unreachable",
                                failure="exit_unreachable"))
    owner, published = make_owner(port, clock)
    manual = owner.verify({"trigger": "manual"}, JobContext("job-1"))
    assert prime(owner, clock).reason == "fresh"           # a click is current for 300 s
    clock.advance(301.0)
    assert owner.schedule_auto().reason == "start"
    assert len(port.calls) == 2
    assert owner.snapshot()["current"]["request_id"] == manual["request_id"]
    clock.advance(25.0)
    # The failure still counts: the next automatic lookup backs off (30 s).
    assert owner.schedule_auto().reason == "backoff"


def test_skipped_automatic_outcome_keeps_card_and_does_not_back_off():
    clock = Clock()
    port = ExitPort()
    # The runtime mirrors ``error`` into diagnostics.overview_failure_reason.
    port.results.append({**payload(v4=None, state="stale", error="route_changed",
                                   failure="route_changed"), "identity": {}})
    owner, published = make_owner(port, clock)
    assert prime(owner, clock).start
    assert owner.snapshot()["current"]["status"] == "idle" and published == []
    clock.advance(21.0)                                     # past the 20 s spacing
    # Not evidence about the exit: the settle window restarts, no back-off
    # (a counted failure would answer ``backoff`` until +30 s here).
    decision = owner.schedule_auto()
    assert decision.reason == "wait_settle" and decision.retry_at == pytest.approx(clock.t + 5.0)


def test_automatic_timeout_keeps_card_and_counts_against_automatic_lookups():
    clock = Clock()
    port = ExitPort()
    release = threading.Event()
    port.results.append(lambda kwargs: (release.wait(5), payload())[1])
    owner, published = make_owner(port, clock, threaded=True)
    assert prime(owner, clock).start
    assert port.called.wait(2)
    clock.advance(3.0)                                      # past the 2 s attempt deadline
    assert wait_for(lambda: owner.progress() is None)
    assert owner.snapshot()["current"]["status"] == "idle" and published == []
    release.set()                                           # the late result is ignored
    assert wait_for(lambda: owner.live_probes == 0)
    assert owner.snapshot()["current"]["status"] == "idle" and published == []
    # The hang counted as one failure: past the 20 s spacing the first
    # back-off step (30 s) still holds the next automatic lookup.
    clock.advance(21.0)
    decision = owner.schedule_auto()
    assert decision.reason == "backoff" and len(port.calls) == 1


def test_lifecycle_change_during_automatic_lookup_is_skipped_silently():
    clock = Clock()
    port = ExitPort()

    def change(kwargs):
        port.life = lifecycle(revision=2)
        return payload()
    port.results.append(change)
    owner, published = make_owner(port, clock)
    assert prime(owner, clock).start
    assert published == [] and owner.snapshot()["current"]["status"] == "idle"
    clock.advance(21.0)
    # Skipped (validation_cancelled): the settle window restarts, no back-off.
    assert owner.schedule_auto().reason == "wait_settle"


def test_failed_automatic_lookup_without_a_usable_card_installs_the_failure():
    """Qt 16530: with nothing usable shown for this binding, the failure is shown."""
    clock = Clock()
    port = ExitPort()
    port.results.append(payload(v4=None, state="data_plane_failed", error="exit_unreachable",
                                failure="exit_unreachable"))
    owner, published = make_owner(port, clock)
    assert prime(owner, clock).start
    current = owner.snapshot()["current"]
    assert current["status"] == "failed" and current["reason_code"] == "exit_unreachable"
    assert [dto["current"]["status"] for dto in published] == ["failed"]
    for dto in published:
        valid_topic(dto)
    clock.advance(21.0)
    assert owner.schedule_auto().reason == "backoff"        # and it counts as a failure


# --------------------------------------------------------------------------
# MUST-1: an accepted automatic start the executor never ran
# --------------------------------------------------------------------------

def test_dropped_automatic_start_is_reclaimed_after_its_start_deadline():
    clock = Clock()
    port = ExitPort()
    port.results.append(payload())
    dropped = []

    def drop_first(work):
        if not dropped:
            dropped.append(work)          # accepted, never run (queue discarded)
        else:
            work()
        return True
    owner, published = make_owner(port, clock, auto_submit=drop_first)
    assert prime(owner, clock).start
    assert port.calls == [] and owner.progress()["trigger"] == "auto"
    clock.advance(1.0)                                  # inside the start deadline
    assert owner.schedule_auto().reason == "busy"
    clock.advance(24.0)                                 # past deadline and 20 s spacing
    decision = owner.schedule_auto()
    assert decision.start, decision
    assert len(port.calls) == 1 and owner.snapshot()["current"]["status"] == "partial"
    # Should the dropped work ever run late, it returns at its start gate.
    dropped[0]()
    assert len(port.calls) == 1 and owner.progress() is None


def test_late_automatic_start_after_its_deadline_gives_the_slot_back():
    clock = Clock()
    port = ExitPort()
    held = []
    owner, _ = make_owner(port, clock, auto_submit=lambda work: held.append(work) or True)
    assert prime(owner, clock).start
    clock.advance(5.0)                                  # began 5 s late (deadline 2.05 s)
    held[0]()
    assert port.calls == [] and owner.progress() is None


def test_auto_start_dropped_by_advance_epoch_does_not_stall_scheduling():
    """Reviewer repro: both normal workers busy, the start is queued, then
    ``advance_epoch`` discards the queue without running it."""
    clock = Clock()
    port = ExitPort()
    port.results.append(payload())
    executors = {"current": JobExecutor(JobEventRecorder(), epoch="epoch-1",
                                        require_acceptance=True)}
    old = executors["current"]
    gate = threading.Event()
    owner, _ = make_owner(port, clock, auto_submit=lambda work: executors[
        "current"].submit_normal(work))
    new = None
    try:
        assert old.submit_normal(lambda: gate.wait(5))
        assert old.submit_normal(lambda: gate.wait(5))
        time.sleep(0.1)                                 # both normal workers occupied
        assert prime(owner, clock).start                # accepted into the queue
        new = old.advance_epoch("epoch-2", JobEventRecorder())
        executors["current"] = new
        gate.set()
        time.sleep(0.2)
        assert port.calls == []                         # the queued start was dropped
        assert owner.schedule_auto().reason == "busy"   # still inside its deadline
        clock.advance(25.0)
        assert owner.schedule_auto().start              # reclaimed, not busy forever
        assert wait_for(lambda: owner.snapshot()["current"]["status"] == "partial")
        assert len(port.calls) == 1
    finally:
        gate.set()
        old.shutdown(time.monotonic() + 1)
        if new is not None:
            new.shutdown(time.monotonic() + 1)


# --------------------------------------------------------------------------
# SHOULD-1: a click after an automatic timeout waits for the orphan probe
# --------------------------------------------------------------------------

def test_manual_click_after_an_automatic_timeout_waits_instead_of_conflict():
    clock = Clock()
    port = ExitPort()
    release = threading.Event()
    port.results.append(lambda kwargs: (release.wait(5), payload(v4="192.0.2.50"))[1])
    port.results.append(payload(v4="203.0.113.80"))
    owner, published = make_owner(port, clock, threaded=True)
    assert prime(owner, clock).start
    assert port.called.wait(2)
    clock.advance(3.0)                                  # the automatic attempt timed out
    assert wait_for(lambda: owner.progress() is None)
    assert owner.live_probes == 1                       # its probe is still in the runtime
    box = {}

    def manual():
        try:
            box["result"] = owner.verify({"trigger": "manual"}, JobContext("job-m"))
        except JobFailure as exc:
            box["failure"] = exc
    thread = threading.Thread(target=manual, daemon=True)
    thread.start()
    # Visible progress instead of an unexplained "in progress" conflict.
    assert wait_for(lambda: owner.snapshot()["current"]["reason_code"] == "upgrading")
    assert len(port.calls) == 1
    release.set()
    thread.join(5)
    assert "failure" not in box, box
    assert box["result"]["v4"]["address"] == "203.0.113.80"
    assert port.peak == 1
    for dto in published:
        valid_topic(dto)


def test_manual_click_beside_a_manual_orphan_is_still_a_conflict():
    """Only automatic orphans are waited for; a revoked manual probe keeps
    the P2-16 single-flight conflict."""
    port = ExitPort()
    release = threading.Event()
    port.results.append(lambda kwargs: (release.wait(5), payload())[1])
    owner, _ = make_owner(port)
    first = JobContext("job-1")
    thread = threading.Thread(target=lambda: owner.verify({"trigger": "manual"}, first),
                              daemon=True)
    thread.start()
    assert port.called.wait(2)
    first.owner_revoked.set()
    assert wait_for(lambda: owner.progress() is None)
    with pytest.raises(JobFailure) as caught:
        owner.verify({"trigger": "manual"}, JobContext("job-2"))
    assert caught.value.code == "conflict"
    release.set()
    thread.join(5)


# --------------------------------------------------------------------------
# ui.presence through the real executor
# --------------------------------------------------------------------------

def _exit_session(port, **owner_options):
    recorder = JobEventRecorder()
    executor = JobExecutor(recorder, epoch="epoch-1", require_acceptance=True)
    hooks = []
    session = SimpleNamespace(executor=executor, state=PublicationState("epoch-1"),
                              register_owner_revoke_hook=hooks.append)
    owner = bx.register(session, port=port, owner_options=owner_options or None)
    return session, owner


def test_register_wires_presence_and_normal_domain_auto_runner():
    port = ExitPort()
    port.results.append(payload())
    clock = Clock()
    session, owner = _exit_session(port, monotonic=clock)
    try:
        session.state.subscribe(["exit.identity"])
        receipt = session.executor.submit("ui.presence", dict(VISIBLE), req_id="p1",
                                          owner="shell-1")
        response = receipt.wait(5)
        assert response.ok and response.data == {"recorded": True}
        frame = dict(t="res", epoch="epoch-1", produced_at_ms=1, sent_at_ms=1, id=1,
                     req_id="p1", owner="shell-1", cmd="ui.presence", ok=True,
                     data=response.data)
        assert protocol.validate_frame(frame) == []
        assert owner.presence() == {"visible": True, "focused": False, "page": "overview"}
        bad = session.executor.submit("ui.presence", {"visible": True, "focused": False,
                                                      "page": "elsewhere"},
                                      req_id="p2", owner="shell-1")
        assert bad.accepted is False
        # A start runs on its own driver thread (T-P2-31a), not inline.
        owner.observe_route(ROUTE)
        owner.notify_lifecycle(lifecycle())
        clock.advance(6.0)
        assert owner.schedule_auto().start
        assert wait_for(lambda: owner.snapshot()["current"]["status"] == "partial")
        assert port.calls[0]["trigger"] == "auto"
        assert session.state.snapshot("exit.identity")["data"]["current"]["status"] == "partial"
    finally:
        session.executor.shutdown(time.monotonic() + 1)


def test_presence_handler_rejects_malformed_values():
    owner, _ = make_owner(ExitPort())
    with pytest.raises(JobFailure) as caught:
        owner.observe_presence({"visible": "yes", "focused": False, "page": "overview"})
    assert caught.value.code == "validation"


def test_runtime_port_reads_the_auto_lookup_preference_from_config():
    runtime = SimpleNamespace(config={"ui": {"overview_auto_exit_lookup": "off"}})
    assert bx.RuntimeExitPort(runtime).auto_lookup_enabled() is False
    runtime.config = {}
    assert bx.RuntimeExitPort(runtime).auto_lookup_enabled() is True


# --------------------------------------------------------------------------
# helpers: node diagnostics
# --------------------------------------------------------------------------

NODE = "0123456789abcdef01234567"
GATEWAY = "89abcdef0123456789abcdef"
AUTO = "fedcba9876543210fedcba98"


def success(applied=True, latency=123.4):
    return {"state": "success", "reason": "ok", "latency_ms": latency,
            "status_applied": applied}


class DiagPort:
    def __init__(self):
        self.targets = {
            NODE: bd.NodeTarget(NODE, "tier2|vless|1.2.3.4|443|u|", "tier2", False, False),
            GATEWAY: bd.NodeTarget(GATEWAY, "exit|trojan|5.6.7.8|443|p|", "exit", False, True),
            AUTO: bd.NodeTarget(AUTO, "tier3|ss|9.9.9.9|8388|x|", "tier3", True, False),
        }
        self.results: deque = deque()
        self.calls: list[dict] = []
        self.cancels: list[str] = []
        self.resolves: list[str] = []
        self.blocking = False
        self.release = threading.Event()
        self.started = threading.Event()
        self.refuse_cancels = 0
        self.unwind_seconds = 0.0
        self.cancelled = False
        self.resolve_gate: threading.Event | None = None

    def resolve_node(self, node_id):
        self.resolves.append(node_id)
        if self.resolve_gate is not None:
            self.resolve_gate.wait(5)
        target = self.targets.get(node_id)
        if isinstance(target, Exception):
            raise target
        return target

    def diagnose_node(self, node_identity, *, probe_mode, request_id):
        self.calls.append({"identity": node_identity, "mode": probe_mode,
                           "request_id": request_id})
        self.started.set()
        if self.blocking:
            self.release.wait(5)
        if self.cancelled:
            time.sleep(self.unwind_seconds)
            return {"state": "cancelled", "reason": "cancelled", "latency_ms": None}
        item = self.results.popleft() if self.results else success()
        return item(request_id) if callable(item) else item

    def cancel_node_diagnostic(self, request_id):
        self.cancels.append(request_id)
        if self.refuse_cancels > 0:
            self.refuse_cancels -= 1
            return False
        if not self.calls or self.calls[-1]["request_id"] != request_id:
            return False
        self.cancelled = True
        self.release.set()
        return True


def _diag_session(port, **options):
    recorder = JobEventRecorder()
    executor = JobExecutor(recorder, epoch="epoch-1", require_acceptance=True)
    hooks = []
    session = SimpleNamespace(executor=executor, register_owner_revoke_hook=hooks.append)
    if options:
        owner = bd.DiagnosticsOwner(port, owner_of=bd.executor_owner_resolver(
            lambda: session.executor), **options)
        bd.register(session, owner=owner)
    else:
        owner = bd.register(session, port=port)
    return session, owner, recorder, hooks


def submit_diagnose(session, *, node_id=NODE, mode="direct", request_id="d1", owner="w1",
                    req_id="r1"):
    ack = session.executor.submit("nodes.diagnose", {"node_id": node_id, "probe_mode": mode,
                                                     "request_id": request_id},
                                  req_id=req_id, owner=owner)
    assert ack.accepted, ack
    assert session.executor.acceptance_sent(ack.op, epoch="epoch-1")
    return ack


def cancel_diagnose(session, request_id="d1", owner="w1", req_id="c1"):
    receipt = session.executor.submit("nodes.cancelDiagnose", {"request_id": request_id},
                                      req_id=req_id, owner=owner)
    assert receipt.accepted, receipt
    response = receipt.wait(10)
    frame = dict(t="res", epoch="epoch-1", produced_at_ms=1, sent_at_ms=1, id=1,
                 req_id=req_id, owner=owner, cmd="nodes.cancelDiagnose", ok=response.ok)
    if response.ok:
        frame["data"] = response.data
    else:
        frame["error"] = response.error
    assert protocol.validate_frame(frame) == [], frame
    return response


def frames_valid(recorder):
    events = recorder.prepare_for_send(0)
    for event in events:
        assert protocol.validate_frame(event) == [], event
    return events


def diag_valid(dto):
    errors: list[str] = []
    protocol._schema(dto, "Diagnostic", "diagnostic", errors)
    assert errors == [], (dto, errors)


# --------------------------------------------------------------------------
# nodes.diagnose
# --------------------------------------------------------------------------

def test_diagnose_success_is_done_with_status_applied_and_keeps_latency():
    port = DiagPort()
    session, owner, recorder, _ = _diag_session(port)
    try:
        ack = submit_diagnose(session)
        job = session.executor.wait(ack.op, 5)
        assert job["state"] == "done", job
        assert job["result"] == {"request_id": "d1", "status_applied": True,
                                 "reason_code": None, "cleanup_pending": False}
        assert port.calls == [{"identity": "tier2|vless|1.2.3.4|443|u|", "mode": "direct",
                               "request_id": "d1"}]
        frames_valid(recorder)
        record = owner.last_diagnostic(NODE)
        assert record == {"request_id": "d1", "mode": "direct", "state": "ok",
                          "latency_ms": 123.4, "reason_code": None, "status_applied": True,
                          "cleanup_pending": False}
        diag_valid(record)
    finally:
        session.executor.shutdown(time.monotonic() + 1)


def test_automatic_pool_target_is_refused_after_resolution():
    port = DiagPort()
    session, owner, recorder, _ = _diag_session(port)
    try:
        ack = submit_diagnose(session, node_id=AUTO)
        job = session.executor.wait(ack.op, 5)
        assert job["state"] == "failed"
        assert job["error"]["code"] == "not_allowed"
        assert job["result"]["reason_code"] == "tier3_auto_pool_self_check"
        assert port.resolves == [AUTO] and port.calls == []      # resolved, never diagnosed
        frames_valid(recorder)
        assert owner.last_diagnostic(AUTO) is None
    finally:
        session.executor.shutdown(time.monotonic() + 1)


@pytest.mark.parametrize("node_id, target, code, reason", [
    ("ffffffffffffffffffffffff", None, "conflict", "node_not_found"),
    (NODE, bd.TargetUnavailable("stopped"), "runtime_unavailable", "node_index_unavailable"),
])
def test_unresolvable_targets_fail_without_running_a_diagnostic(node_id, target, code, reason):
    port = DiagPort()
    port.targets[node_id] = target
    owner = bd.DiagnosticsOwner(port)
    with pytest.raises(JobFailure) as caught:
        owner.diagnose({"node_id": node_id, "probe_mode": "direct", "request_id": "d1"},
                       JobContext("job-1"))
    assert caught.value.code == code
    assert caught.value.partial_result["reason_code"] == reason
    assert caught.value.error["message_key"] == f"errors.diagnose.{reason}"
    assert port.calls == []


def test_chain_probe_is_gateway_only_like_qt():
    port = DiagPort()
    owner = bd.DiagnosticsOwner(port)
    with pytest.raises(JobFailure) as caught:
        owner.diagnose({"node_id": NODE, "probe_mode": "chain", "request_id": "d1"},
                       JobContext("job-1"))
    assert caught.value.code == "validation"
    assert caught.value.partial_result["reason_code"] == "chain_requires_gateway"
    result = owner.diagnose({"node_id": GATEWAY, "probe_mode": "chain", "request_id": "d2"},
                            JobContext("job-2"))
    assert result["reason_code"] is None and port.calls[-1]["mode"] == "chain"


def test_diagnose_is_single_flight():
    port = DiagPort()
    port.blocking = True
    session, owner, recorder, _ = _diag_session(port)
    try:
        first = submit_diagnose(session)
        assert port.started.wait(2)
        second = submit_diagnose(session, request_id="d2", req_id="r2")
        job = session.executor.wait(second.op, 5)
        assert job["state"] == "failed" and job["error"]["code"] == "conflict"
        assert job["result"]["reason_code"] == "diagnostic_in_progress"
        assert len(port.calls) == 1
        port.release.set()
        assert session.executor.wait(first.op, 5)["state"] == "done"
        frames_valid(recorder)
    finally:
        session.executor.shutdown(time.monotonic() + 1)


@pytest.mark.parametrize("raw, code, reason, pending", [
    ({"state": "failed", "reason": "timeout", "latency_ms": None}, "timeout", "timeout", False),
    ({"state": "failed", "reason": "probe_failed", "latency_ms": None}, "timeout",
     "probe_failed", False),
    ({"state": "failed", "reason": "cleanup_failed", "latency_ms": None},
     "runtime_unavailable", "cleanup_failed", True),
    ({"state": "failed", "reason": "node_changed", "latency_ms": None}, "conflict",
     "node_changed", False),
    ({"state": "cancelled", "reason": "cancelled", "latency_ms": None}, "not_running",
     "cancelled", False),
])
def test_failures_keep_reason_status_applied_and_cleanup(raw, code, reason, pending):
    port = DiagPort()
    port.results.append(raw)
    session, owner, recorder, _ = _diag_session(port)
    try:
        ack = submit_diagnose(session)
        job = session.executor.wait(ack.op, 5)
        assert job["state"] == "failed"
        assert job["error"]["code"] == code
        assert job["result"] == {"request_id": "d1", "status_applied": False,
                                 "reason_code": reason, "cleanup_pending": pending}
        frames_valid(recorder)
        record = owner.last_diagnostic(NODE)
        assert record["reason_code"] == reason and record["cleanup_pending"] is pending
        diag_valid(record)
    finally:
        session.executor.shutdown(time.monotonic() + 1)


def test_this_module_sets_no_lifecycle_gate_on_diagnose():
    """Owner layer only: this module never consults the lifecycle, so a
    stopped service does not block a diagnose by itself.  Whether the runtime
    can resolve a stopped node depends on the runtime getter
    ``bridge_node_diagnostic_target`` (report §4); see the two adapter-level
    tests below for the production behaviour with and without it."""
    port = DiagPort()
    port.results.append(success(applied=False, latency=88.0))
    session, owner, recorder, _ = _diag_session(port)
    try:
        ack = submit_diagnose(session)
        job = session.executor.wait(ack.op, 5)
        assert job["state"] == "done"
        assert job["result"]["status_applied"] is False
        assert owner.last_diagnostic(NODE)["latency_ms"] == 88.0
        frames_valid(recorder)
    finally:
        session.executor.shutdown(time.monotonic() + 1)


class _StoppedRuntime:
    """Service stopped (manager None); the runtime diagnoses from its
    persisted inventory with a temporary Core and writes nothing back."""

    manager = None

    def __init__(self):
        self.calls = []

    def lifecycle_status(self):
        return lifecycle(phase="stopped", core_running=False, readiness="facility_hold")

    def diagnose_node(self, identity, *, probe_mode, request_id):
        self.calls.append((identity, probe_mode, request_id))
        return success(applied=False, latency=91.0)

    def cancel_node_diagnostic(self, request_id):
        return False


def test_stopped_service_without_the_getter_fails_node_index_unavailable():
    """Production adapter today: requirement 4 waits for the getter."""
    runtime = _StoppedRuntime()
    session, owner, recorder, _ = _diag_session(bd.RuntimeDiagnosticPort(runtime))
    try:
        ack = submit_diagnose(session)
        job = session.executor.wait(ack.op, 5)
        assert job["state"] == "failed" and job["error"]["code"] == "runtime_unavailable"
        assert job["result"]["reason_code"] == "node_index_unavailable"
        assert runtime.calls == []
        frames_valid(recorder)
    finally:
        session.executor.shutdown(time.monotonic() + 1)


def test_stopped_service_with_the_getter_runs_the_offline_diagnosis_end_to_end():
    class Runtime(_StoppedRuntime):
        def bridge_node_diagnostic_target(self, node_id):
            return {"node_identity": "tier2|vless|1.2.3.4|443|u|", "tier": "tier2",
                    "auto_pool": False, "stale": False} if node_id == NODE else None

    runtime = Runtime()
    session, owner, recorder, _ = _diag_session(bd.RuntimeDiagnosticPort(runtime))
    try:
        ack = submit_diagnose(session)
        job = session.executor.wait(ack.op, 5)
        assert job["state"] == "done", job
        assert job["result"] == {"request_id": "d1", "status_applied": False,
                                 "reason_code": None, "cleanup_pending": False}
        assert runtime.calls == [("tier2|vless|1.2.3.4|443|u|", "direct", "d1")]
        frames_valid(recorder)
    finally:
        session.executor.shutdown(time.monotonic() + 1)


# --------------------------------------------------------------------------
# cancellation
# --------------------------------------------------------------------------

def test_cancel_diagnose_calls_real_cancel_and_waits_for_cleanup():
    port = DiagPort()
    port.blocking = True
    port.unwind_seconds = 0.2
    session, owner, recorder, _ = _diag_session(port)
    try:
        ack = submit_diagnose(session)
        assert port.started.wait(2)
        started = time.monotonic()
        response = cancel_diagnose(session)
        assert response.ok and response.data == {"requested": True, "cleanup_pending": False}
        assert time.monotonic() - started >= 0.15            # waited for the unwind
        assert port.cancels and port.cancels[0] == "d1"
        job = session.executor.wait(ack.op, 5)
        assert job["state"] == "cancelled" and job.get("result") is None
        frames_valid(recorder)
        record = owner.last_diagnostic(NODE)
        assert record["state"] == "cancelled" and record["latency_ms"] is None
        diag_valid(record)
        # Finished: a second cancel is answered, not requested again.
        again = cancel_diagnose(session, req_id="c2")
        assert again.data == {"requested": False, "cleanup_pending": False}
    finally:
        session.executor.shutdown(time.monotonic() + 1)


def test_requested_is_not_cleaned_when_cleanup_outlasts_the_wait():
    port = DiagPort()
    port.blocking = True
    port.unwind_seconds = 0.6
    session, owner, recorder, _ = _diag_session(port, cancel_wait_seconds=0.1)
    try:
        ack = submit_diagnose(session)
        assert port.started.wait(2)
        response = cancel_diagnose(session)
        assert response.data == {"requested": True, "cleanup_pending": True}
        assert session.executor.get_job(ack.op)["state"] == "running"
        job = session.executor.wait(ack.op, 5)
        assert job["state"] == "cancelled"
    finally:
        session.executor.shutdown(time.monotonic() + 1)


def test_cancel_is_retried_until_the_runtime_registered_the_request():
    port = DiagPort()
    port.blocking = True
    port.refuse_cancels = 3            # runtime still resolving its inventory
    session, owner, recorder, _ = _diag_session(port, cancel_retry_seconds=0.01)
    try:
        ack = submit_diagnose(session)
        assert port.started.wait(2)
        response = cancel_diagnose(session)
        assert response.data["requested"] is True
        assert len(port.cancels) >= 4
        assert session.executor.wait(ack.op, 5)["state"] == "cancelled"
    finally:
        session.executor.shutdown(time.monotonic() + 1)


def test_executor_cancel_ends_cancelled_only_after_cleanup():
    port = DiagPort()
    port.blocking = True
    port.unwind_seconds = 0.3
    session, owner, recorder, _ = _diag_session(port)
    try:
        ack = submit_diagnose(session)
        assert port.started.wait(2)
        cancel = session.executor.cancel(ack.op, owner="w1")
        assert cancel.accepted
        assert cancel.cancel_receipt.reported.wait(2)
        assert cancel.cancel_receipt.requested is True
        # Requested is not cleaned: the job stays running during the unwind.
        assert session.executor.get_job(ack.op)["state"] == "running"
        job = session.executor.wait(ack.op, 5)
        assert job["state"] == "cancelled"
        assert cancel.cancel_receipt.cleanup_complete.is_set()
        frames_valid(recorder)
    finally:
        session.executor.shutdown(time.monotonic() + 1)


def test_cancel_before_the_runtime_started_never_starts_it():
    port = DiagPort()
    port.resolve_gate = threading.Event()
    owner = bd.DiagnosticsOwner(port)
    context = JobContext("job-1")
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault("result", owner.diagnose(
        {"node_id": NODE, "probe_mode": "direct", "request_id": "d1"}, context)), daemon=True)
    thread.start()
    assert wait_for(lambda: port.resolves == [NODE])
    report = owner.cancel_job({}, context)
    assert report == CancellationReport(True, True)
    port.resolve_gate.set()
    thread.join(5)
    assert port.calls == [] and context.cancelled.is_set()
    assert box["result"]["reason_code"] == "cancelled"
    assert owner.last_diagnostic(NODE) is None          # nothing ran, nothing recorded
    assert owner.active_request() is None


def test_cancel_diagnose_checks_the_request_owner_and_unknown_ids():
    port = DiagPort()
    port.blocking = True
    session, owner, recorder, _ = _diag_session(port)
    try:
        ack = submit_diagnose(session)
        assert port.started.wait(2)
        other = cancel_diagnose(session, owner="w2")
        assert other.ok is False and other.error["code"] == "not_allowed"
        assert port.cancels == []
        unknown = cancel_diagnose(session, request_id="nope", req_id="c2")
        assert unknown.ok is False and unknown.error["code"] == "conflict"
        port.release.set()
        assert session.executor.wait(ack.op, 5)["state"] == "done"
    finally:
        session.executor.shutdown(time.monotonic() + 1)


# --------------------------------------------------------------------------
# owner revoke and late results
# --------------------------------------------------------------------------

def test_owner_revoke_cancels_the_owners_diagnostic_and_drops_the_late_result():
    port = DiagPort()
    port.blocking = True
    session, owner, recorder, hooks = _diag_session(port)
    try:
        ack = submit_diagnose(session)
        assert port.started.wait(2)
        session.executor.revoke_owner("w1")           # bridge_main: executor first,
        started = time.monotonic()
        for hook in hooks:                            # then the business hooks
            hook("w1")
        assert time.monotonic() - started < 0.2       # the hook never blocks
        assert wait_for(lambda: port.cancels == ["d1"])
        job = session.executor.wait(ack.op, 5)
        assert job["state"] == "cancelled" and job.get("result") is None
        record = owner.last_diagnostic(NODE)
        assert record["state"] == "cancelled" and record["latency_ms"] is None
        frames_valid(recorder)
    finally:
        session.executor.shutdown(time.monotonic() + 1)


def test_late_success_after_revoke_never_becomes_the_job_result():
    port = DiagPort()
    port.blocking = True
    port.refuse_cancels = 10 ** 6         # the runtime finishes before any cancel lands
    session, owner, recorder, hooks = _diag_session(port, cancel_retry_seconds=0.01)
    try:
        ack = submit_diagnose(session)
        assert port.started.wait(2)
        for hook in hooks:
            hook("w1")
        port.release.set()                # late success (already written back)
        job = session.executor.wait(ack.op, 5)
        assert job["state"] == "cancelled" and job.get("result") is None
        record = owner.last_diagnostic(NODE)
        # The write-back really happened; the late latency is not kept.
        assert record == {"request_id": "d1", "mode": "direct", "state": "cancelled",
                          "latency_ms": None, "reason_code": "cancelled",
                          "status_applied": True, "cleanup_pending": False}
        events = frames_valid(recorder)
        assert all(event["data"].get("result") is None for event in events
                   if event["data"]["op"] == ack.op)
    finally:
        session.executor.shutdown(time.monotonic() + 1)


def test_revoking_another_owner_leaves_the_diagnostic_running():
    port = DiagPort()
    port.blocking = True
    session, owner, recorder, hooks = _diag_session(port)
    try:
        ack = submit_diagnose(session)
        assert port.started.wait(2)
        for hook in hooks:
            hook("w2")
        time.sleep(0.05)
        assert port.cancels == []
        port.release.set()
        assert session.executor.wait(ack.op, 5)["state"] == "done"
    finally:
        session.executor.shutdown(time.monotonic() + 1)


def test_reused_request_id_is_refused():
    port = DiagPort()
    owner = bd.DiagnosticsOwner(port)
    owner.diagnose({"node_id": NODE, "probe_mode": "direct", "request_id": "d1"},
                   JobContext("job-1"))
    with pytest.raises(JobFailure) as caught:
        owner.diagnose({"node_id": NODE, "probe_mode": "direct", "request_id": "d1"},
                       JobContext("job-2"))
    assert caught.value.code == "conflict"
    assert caught.value.partial_result["reason_code"] == "request_id_reused"


# --------------------------------------------------------------------------
# fix round 1: queued jobs of a revoked owner, early cancels, tombstones
# --------------------------------------------------------------------------

def test_queued_diagnose_of_a_revoked_owner_never_starts():
    """M1 (reviewer repro): the executor's revoke does not end a queued
    diagnostic job; the hook must stop it at its first gate."""
    port = DiagPort()
    session, owner, recorder, hooks = _diag_session(port)
    gate = threading.Event()
    try:
        assert session.executor.submit_normal(lambda: gate.wait(5))
        assert session.executor.submit_normal(lambda: gate.wait(5))
        time.sleep(0.1)                               # both normal workers occupied
        ack = submit_diagnose(session)
        assert session.executor.get_job(ack.op)["state"] == "queued"
        session.executor.revoke_owner("w1")
        for hook in hooks:
            hook("w1")
        assert session.executor.get_job(ack.op)["state"] == "queued"
        gate.set()
        job = session.executor.wait(ack.op, 5)
        assert job["state"] == "cancelled" and job.get("result") is None, job
        assert port.resolves == [] and port.calls == [] and port.cancels == []
        assert owner.last_diagnostic(NODE) is None and owner.active_request() is None
        frames_valid(recorder)
    finally:
        gate.set()
        session.executor.shutdown(time.monotonic() + 1)


def test_owner_revoked_on_the_context_is_honoured_at_the_gate():
    port = DiagPort()
    owner = bd.DiagnosticsOwner(port)
    context = JobContext("job-1")
    context.owner_revoked.set()
    result = owner.diagnose({"node_id": NODE, "probe_mode": "direct", "request_id": "d1"},
                            context)
    assert result["reason_code"] == "cancelled" and context.cancelled.is_set()
    assert port.resolves == [] and port.calls == []


def test_cancel_before_the_handler_registered_is_honoured_at_the_gate():
    """S2 / SHOULD-4: cancel{op} after running but before registration."""
    port = DiagPort()
    owner = bd.DiagnosticsOwner(port)
    context = JobContext("job-1")
    # Nothing has started: reporting cleanup complete is true.
    assert owner.cancel_job({}, context) == CancellationReport(True, True)
    result = owner.diagnose({"node_id": NODE, "probe_mode": "direct", "request_id": "d1"},
                            context)
    assert result["reason_code"] == "cancelled" and context.cancelled.is_set()
    assert port.resolves == [] and port.calls == []
    # Finished: a later cancel is refused; the next request runs normally.
    assert owner.cancel_job({}, context) == CancellationReport(False)
    done = owner.diagnose({"node_id": NODE, "probe_mode": "direct", "request_id": "d2"},
                          JobContext("job-2"))
    assert done["reason_code"] is None and len(port.calls) == 1


def test_early_cancel_through_the_executor_ends_cancelled_without_running():
    port = DiagPort()
    session, owner, recorder, _ = _diag_session(port)
    original = owner._owner_of
    entered = threading.Event()
    hold = threading.Event()

    def slow_owner_of(context):               # widen the pre-registration window
        entered.set()
        hold.wait(5)
        return original(context)
    owner._owner_of = slow_owner_of
    try:
        ack = submit_diagnose(session)
        assert entered.wait(2)
        cancel = session.executor.cancel(ack.op, owner="w1")
        assert cancel.accepted and cancel.cancel_receipt.reported.wait(2)
        assert cancel.cancel_receipt.requested is True
        hold.set()
        job = session.executor.wait(ack.op, 5)
        assert job["state"] == "cancelled" and job.get("result") is None
        assert wait_for(lambda: owner.active_request() is None)
        assert port.resolves == [] and port.calls == []
        frames_valid(recorder)
    finally:
        hold.set()
        session.executor.shutdown(time.monotonic() + 1)


def test_tombstone_is_written_with_the_finish():
    """S3: once a flight is ``finished`` its tombstone already holds the
    true cleanup_pending, before the handler's ``finally`` runs."""
    port = DiagPort()
    port.results.append({"state": "failed", "reason": "cleanup_failed", "latency_ms": None})
    owner = bd.DiagnosticsOwner(port)
    seen = {}
    finish = owner._finish

    def spy(flight, outcome):
        try:
            return finish(flight, outcome)
        finally:
            seen["answer"] = owner.cancel_request({"request_id": flight.request_id},
                                                  flight.context)
    owner._finish = spy
    with pytest.raises(JobFailure):
        owner.diagnose({"node_id": NODE, "probe_mode": "direct", "request_id": "d1"},
                       JobContext("job-1"))
    assert seen["answer"] == {"requested": False, "cleanup_pending": True}


def _diagnose_d1(owner):
    return owner.diagnose({"node_id": NODE, "probe_mode": "direct", "request_id": "d1"},
                          JobContext("job-1"))


def test_repeated_cancel_diagnose_shares_one_wait_deadline():
    """SHOULD-3: only the first cancelDiagnose waits on the control worker."""
    port = DiagPort()
    port.blocking = True
    port.unwind_seconds = 1.0
    owner = bd.DiagnosticsOwner(port, cancel_wait_seconds=0.3, cancel_retry_seconds=0.01)
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault("job", _diagnose_d1(owner)),
                              daemon=True)
    thread.start()
    assert port.started.wait(2)
    started = time.monotonic()
    first = owner.cancel_request({"request_id": "d1"}, None)
    assert time.monotonic() - started >= 0.25
    assert first == {"requested": True, "cleanup_pending": True}
    started = time.monotonic()
    second = owner.cancel_request({"request_id": "d1"}, None)
    assert time.monotonic() - started < 0.1
    assert second == {"requested": True, "cleanup_pending": True}
    thread.join(5)
    assert box["job"]["reason_code"] == "cancelled"


def test_refusal_uses_the_authoritative_auto_pool_predicate_too():
    """S6: the manager's own predicate (origin stripped, node.tier) and the
    public projection (runtime tier key) are both honoured."""
    from models import Node
    stored_under_other_key = Node(name="x", protocol="ss", address="9.9.9.8", port=1,
                                  password="p", method="aes-128-gcm", tier="tier3", extra={})
    assert bd.is_automatic_pool("tier2", stored_under_other_key) is True
    padded = Node(name="y", protocol="ss", address="9.9.9.7", port=1, password="p",
                  method="aes-128-gcm", tier="tier3", extra={"tier3_origin": " tier3a "})
    assert bd.is_automatic_pool("tier3", padded) is True        # projection says automatic
    user = Node(name="z", protocol="ss", address="9.9.9.6", port=1, password="p",
                method="aes-128-gcm", tier="tier3", extra={"tier3_origin": "tier3a"})
    assert bd.is_automatic_pool("tier3", user) is False


# --------------------------------------------------------------------------
# adapter: P2-04 ID rule and pool rule, public methods only
# --------------------------------------------------------------------------

def _loaded_runtime():
    from models import Node
    user = Node(name="a", protocol="vless", address="1.2.3.4", port=443, uuid="u1", tier="tier2")
    gateway = Node(name="g", protocol="trojan", address="5.6.7.8", port=443, password="p",
                   tier="exit")
    automatic = Node(name="t", protocol="ss", address="9.9.9.9", port=8388, password="x",
                     method="aes-128-gcm", tier="tier3", extra={"tier3_origin": "tier3b"})
    own = Node(name="o", protocol="ss", address="8.8.4.4", port=8388, password="y",
               method="aes-128-gcm", tier="tier3", extra={"tier3_origin": "tier3a"})
    # Same diagnostic identity as the automatic node, but user origin.
    twin = Node(name="t2", protocol="ss", address="9.9.9.9", port=8388, password="x",
                method="aes-256-gcm", tier="tier3", extra={"tier3_origin": "tier3a"})
    manager = SimpleNamespace(lock=threading.Lock(), runtime_generation=7, config={},
                              runtime_nodes={"tier2": [user], "exit": [gateway],
                                             "tier3": [automatic, own, twin]})
    calls = []

    class Runtime:
        def lifecycle_status(self):
            calls.append("lifecycle_status")
            return {"intent": {"runtime_generation": 7}}

        def diagnose_node(self, identity, *, probe_mode, request_id):
            calls.append(("diagnose_node", identity, probe_mode, request_id))
            return success()

        def cancel_node_diagnostic(self, request_id):
            calls.append(("cancel_node_diagnostic", request_id))
            return True

    runtime = Runtime()
    runtime.manager = manager
    return runtime, manager, calls, (user, gateway, automatic, own, twin)


def test_adapter_resolves_public_ids_with_the_p2_04_rule():
    from bridge_node_commands import node_item_id
    from node_diagnostic import diagnostic_node_identity
    runtime, manager, calls, (user, gateway, automatic, own, twin) = _loaded_runtime()
    port = bd.RuntimeDiagnosticPort(runtime)
    target = port.resolve_node(node_item_id(7, "tier2", user))
    assert target.node_identity == diagnostic_node_identity(user)
    assert target.tier == "tier2" and not target.auto_pool and not target.gateway
    assert port.resolve_node(node_item_id(7, "exit", gateway)).gateway is True
    assert port.resolve_node(node_item_id(7, "tier3", automatic)).auto_pool is True
    assert port.resolve_node(node_item_id(7, "tier3", own)).auto_pool is False
    # A user node sharing its runtime identity with an automatic one is
    # refused too: diagnose_node resolves by identity, not by Node.id.
    assert port.resolve_node(node_item_id(7, "tier3", twin)).auto_pool is True
    assert port.resolve_node(node_item_id(6, "tier2", user)) is None   # old generation
    assert port.diagnose_node("x", probe_mode="direct", request_id="d1")["state"] == "success"
    assert port.cancel_node_diagnostic("d1") is True
    assert set(name if isinstance(name, str) else name[0] for name in calls) == {
        "lifecycle_status", "diagnose_node", "cancel_node_diagnostic"}


def test_adapter_ids_and_pool_kind_match_the_runtime_projection():
    """Cross-check with DesktopRuntime.bridge_node_records (P2-04).

    Calls the runtime method on a SimpleNamespace, so it depends on that
    method's internals (P2-10 is editing desktop_runtime.py); the integration
    ticket re-runs this cross-check on the merged tree (review S7)."""
    import desktop_runtime
    runtime, manager, _calls, nodes = _loaded_runtime()
    fake = SimpleNamespace(
        _bridge_read_owner=lambda: (7, 1, manager, {}, True, False),
        _bridge_owner_current=lambda owner: True,
        bridge_secret_facts=lambda: {"config": {}, "sources": {}, "nodes": []})
    records = desktop_runtime.DesktopRuntime.bridge_node_records(fake)
    port = bd.RuntimeDiagnosticPort(runtime)
    rows = {row["name"]: row for row in records["items"]}
    assert set(rows) == {"a", "g", "t", "o", "t2"}
    # The runtime's own projection: only the tier3b node is the automatic pool.
    assert {name for name, row in rows.items() if row["pool_kind"] == "automatic"} == {"t"}
    refused = set()
    for name, row in rows.items():
        target = port.resolve_node(row["id"])        # every public Node.id resolves
        assert target is not None, row
        if target.auto_pool:
            refused.add(name)
    # The automatic node, plus the user twin sharing its runtime identity.
    assert refused == {"t", "t2"}


def test_adapter_reports_unavailable_when_stopped_without_a_getter():
    runtime, manager, calls, _ = _loaded_runtime()
    runtime.manager = None
    with pytest.raises(bd.TargetUnavailable):
        bd.RuntimeDiagnosticPort(runtime).resolve_node(NODE)


def test_adapter_prefers_the_runtime_getter():
    class Runtime:
        manager = None

        def bridge_node_diagnostic_target(self, node_id):
            return {"node_identity": "exit|x", "tier": "exit", "auto_pool": False,
                    "stale": False} if node_id == GATEWAY else None

    port = bd.RuntimeDiagnosticPort(Runtime())
    target = port.resolve_node(GATEWAY)
    assert target.gateway and target.node_identity == "exit|x"
    assert port.resolve_node(NODE) is None


def test_pool_rule_matches_bridge_node_records():
    assert bd.node_pool_kind("tier3", {"tier3_origin": "tier3b"}) == "automatic"
    assert bd.node_pool_kind("tier3", {}) == "automatic"
    assert bd.node_pool_kind("tier3", {"source_origin": "tier3a"}) == "user"
    assert bd.node_pool_kind("tier2", {"tier3_origin": "tier3b"}) == "user"


def test_message_keys_and_codes_are_contract_valid():
    from bridge_commands import COMMAND_SPECS
    errors = COMMAND_SPECS["nodes.diagnose"].errors
    for reason in bd.DIAGNOSE_REASON_CODES:
        error = bd.diagnose_error(reason)
        assert error["code"] in errors, reason
        problems: list[str] = []
        protocol._schema(error, "Error", "error", problems)
        assert problems == [], (reason, problems)


# --------------------------------------------------------------------------
# fix round 2: revoke / cancel while the target is being resolved
# --------------------------------------------------------------------------

@pytest.mark.parametrize("node_id, target", [
    (AUTO, "auto"),                                     # would be tier3_auto_pool_self_check
    ("ffffffffffffffffffffffff", None),                  # would be node_not_found
    (NODE, bd.TargetUnavailable("stopped")),            # would be node_index_unavailable
    (NODE, "ok"),                                       # usable target (control)
])
def test_revoke_during_resolution_ends_cancelled_without_a_late_refusal(node_id, target):
    """MUST-A (round 2): a refusal decided after resolution must not reach
    the job of an owner revoked while the target was being resolved."""
    port = DiagPort()
    if target not in ("auto", "ok"):
        port.targets[node_id] = target
    port.resolve_gate = threading.Event()
    session, owner, recorder, hooks = _diag_session(port)
    try:
        ack = submit_diagnose(session, node_id=node_id)
        assert wait_for(lambda: port.resolves == [node_id])
        session.executor.revoke_owner("w1")
        for hook in hooks:
            hook("w1")
        port.resolve_gate.set()
        job = session.executor.wait(ack.op, 5)
        assert job["state"] == "cancelled" and job.get("result") is None, job
        assert port.calls == [] and port.cancels == []
        assert owner.last_diagnostic(node_id) is None
        assert wait_for(lambda: owner.active_request() is None)
        frames_valid(recorder)
    finally:
        port.resolve_gate.set()
        session.executor.shutdown(time.monotonic() + 1)


def test_cancel_op_during_resolution_of_an_auto_pool_target_ends_cancelled():
    port = DiagPort()
    port.resolve_gate = threading.Event()
    session, owner, recorder, _ = _diag_session(port)
    try:
        ack = submit_diagnose(session, node_id=AUTO)
        assert wait_for(lambda: port.resolves == [AUTO])
        cancel = session.executor.cancel(ack.op, owner="w1")
        assert cancel.accepted and cancel.cancel_receipt.reported.wait(2)
        port.resolve_gate.set()
        job = session.executor.wait(ack.op, 5)
        assert job["state"] == "cancelled" and job.get("result") is None, job
        assert port.calls == []
        frames_valid(recorder)
    finally:
        port.resolve_gate.set()
        session.executor.shutdown(time.monotonic() + 1)
