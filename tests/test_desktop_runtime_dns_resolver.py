"""Runtime wiring of the resolver ladder: plan, escalation, terminal state."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import desktop_runtime as runtime_module
from core_runner import CoreOutputEvent
from desktop_runtime import DesktopRuntime
from dns_resolver_policy import (
    BOOTSTRAP_LADDER_AUTO,
    CANDIDATE_QUALIFIED,
    CANDIDATE_TLS_FAILED,
    LINK_ANSWERS_INTERNAL_ONLY,
    LINK_ANSWERS_PUBLIC,
    REASON_ESCALATED_CONTROL_QUERY,
    REASON_ESCALATED_DNS_FAULTS,
    RUNG_ENCRYPTED,
    RUNG_INTRANET_ONLY,
    RUNG_SYSTEM,
    EncryptedCandidateResult,
    LinkResolverResult,
    ResolverQualification,
    encrypted_candidates,
)
from dns_resolver_qualification import LinkDiscovery
from runtime_reload_gate import RuntimeReloadReason

CANDIDATES = {candidate.url: candidate for candidate in encrypted_candidates()}


def qualification(*, candidates=(), links=(), inconclusive=False, outer_fake_ip=False, captive=False):
    return ResolverQualification(
        fingerprint="fp-test",
        wall_time_ms=5,
        link_resolvers=tuple(links),
        dns_suffixes=("campus.example.edu",),
        candidates=tuple(candidates),
        ipv4_available=True,
        ipv6_available=False,
        ipv6_only=False,
        outer_fake_ip=outer_fake_ip,
        captive_suspected=captive,
        inconclusive=inconclusive,
    )


QUALIFIED = qualification(
    candidates=(EncryptedCandidateResult(CANDIDATES["tls://9.9.9.9"], CANDIDATE_QUALIFIED, 30),),
    links=(LinkResolverResult("222.198.178.68", LINK_ANSWERS_PUBLIC, 5),),
)
ENCRYPTED_BLOCKED = qualification(
    candidates=(EncryptedCandidateResult(CANDIDATES["tls://9.9.9.9"], CANDIDATE_TLS_FAILED),),
    links=(LinkResolverResult("222.198.178.68", LINK_ANSWERS_PUBLIC, 5),),
)


class FakeManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.tier3_last_collection_report = {}
        self.runtime_nodes = {"tier3": []}
        self.parked = []
        self.resumed = []
        self.alerts = SimpleNamespace(send_event=lambda event: self.events.append(event))
        self.events = []
        self.woken = []

    def park_tier3_source_supply(self, delay, reason):
        self.parked.append((delay, reason))

    def resume_tier3_source_supply(self, reason):
        self.resumed.append(reason)

    def notify_tier3_source_supply_route_changed(self, reason):
        self.woken.append(reason)


class FakeRunner:
    def __init__(self, running=True):
        self.running = running

    def is_running(self):
        return self.running


def make_runtime(*, manager=None, dns_config=None, runner=None):
    runtime = DesktopRuntime.__new__(DesktopRuntime)
    runtime.logger = logging.getLogger("test-dns-resolver-runtime")
    runtime.stop_event = threading.Event()
    runtime.manager = manager if manager is not None else FakeManager()
    runtime.config = {"client": {"dns": dict(dns_config or {})}}
    runtime.core_runner = runner if runner is not None else FakeRunner()
    runtime.api_client = None
    runtime._dns_resolver_plan = None
    runtime._dns_resolver_plan_lock = threading.Lock()
    runtime._dns_resolver_fault_count = 0
    runtime._dns_resolver_fault_base = 0
    runtime._dns_resolver_fault_scope = None
    runtime._dns_resolver_memory = None
    runtime._source_supply_no_egress_state = {}
    runtime._source_supply_no_egress_timer = None
    runtime._source_supply_no_egress_reason = ""
    runtime._source_supply_no_egress_detail = ""
    runtime._tier3_required_for_start = True
    runtime._tier3_startup_terminal_failure = False
    runtime._last_tier3_startup_error = None
    runtime.last_error = None
    runtime.reloads = []
    runtime.marked = []
    runtime.notified = []
    runtime.terminal_calls = []
    runtime._client_config = lambda: runtime.config["client"]
    runtime._current_core_runtime_phase = lambda: "bootstrap"
    runtime._tier3_startup_target_alive = lambda manager: 3
    runtime._mark_core_export_sync_success = lambda marker=None: runtime.marked.append(marker)
    runtime._notify_tier3_source_control_route_changed = lambda reason: runtime.notified.append(reason)
    runtime._ensure_bootstrap_core_proxy = lambda: runtime.terminal_calls.append("bootstrap_restart")

    def reload(*, mode, reason, user_commit_id=""):
        runtime.reloads.append((mode, reason))
        return {"reloaded": True, "mode": mode}

    runtime.reload_core_config = reload

    def fail_terminal(manager, metadata=None):
        runtime.terminal_calls.append("fail_terminal")
        runtime._tier3_startup_terminal_failure = True
        with manager.lock:
            manager.tier3_last_collection_report["startup_failed"] = True
            manager.tier3_last_collection_report["startup_failure_reason"] = "source_supply_terminal"
        return True

    runtime._fail_terminal_tier3_bootstrap = fail_terminal
    runtime._clear_tier3_terminal_startup_failure = lambda: setattr(runtime, "_tier3_startup_terminal_failure", False)
    return runtime


def prepared(runtime, evidence, **kwargs):
    with patch.object(runtime_module, "discover_link_resolvers", return_value=LinkDiscovery(fingerprint="fp-test")), \
            patch.object(runtime_module, "qualify_resolvers", return_value=evidence):
        return runtime._prepare_dns_resolver_plan(reason=kwargs.pop("reason", "test"), **kwargs)


# ------------------------------------------------------------------ plan

def test_prepare_plan_publishes_encrypted_rung_and_reuses_fresh_plan():
    runtime = make_runtime()
    plan = prepared(runtime, QUALIFIED)
    assert plan is not None and plan.rung == RUNG_ENCRYPTED and plan.revision == 1
    report = runtime.manager.tier3_last_collection_report
    assert report["dns_resolver_rung"] == RUNG_ENCRYPTED
    assert report["dns_resolver_summary"] == "encrypted (Quad9)"
    assert report["dns_resolver_link_resolvers"] == ["222.198.178.68"]
    with patch.object(runtime_module, "qualify_resolvers") as probe:
        again = runtime._prepare_dns_resolver_plan(reason="writer")
    probe.assert_not_called()
    assert again is plan
    forced = prepared(runtime, ENCRYPTED_BLOCKED, force=True)
    assert forced.rung == RUNG_SYSTEM and forced.revision == 2
    assert runtime._dns_resolver_status_summary()["rung"] == RUNG_SYSTEM


def test_prepare_plan_respects_user_custom_and_encrypted_only_policy():
    custom = make_runtime(dns_config={"nameserver": ["tls://dns.example"]})
    plan = prepared(custom, ENCRYPTED_BLOCKED)
    assert plan.rung == "user_custom" and plan.user_custom is True
    assert plan.block["nameserver"] == ["tls://dns.example"]

    pinned = make_runtime(dns_config={"encrypted_only": True})
    plan = prepared(pinned, ENCRYPTED_BLOCKED)
    assert plan.rung == RUNG_ENCRYPTED and plan.encrypted_only and plan.remaining == 0

    disabled = make_runtime(dns_config={"enable": False})
    assert prepared(disabled, QUALIFIED) is None
    assert disabled._apply_dns_resolver_plan_to_payload("dns:\n  enable: true\n") == "dns:\n  enable: true\n"


def test_qualification_failure_keeps_previous_plan():
    runtime = make_runtime()
    plan = prepared(runtime, QUALIFIED)
    with patch.object(runtime_module, "discover_link_resolvers", side_effect=OSError("boom")):
        assert runtime._prepare_dns_resolver_plan(reason="retry", force=True) is plan


# -------------------------------------------------------------- escalation

def event(pid=100, generation=1, at=None):
    return CoreOutputEvent(
        "[TCP] dial DIRECT ... error: dns resolve failed",
        pid,
        generation,
        time.monotonic() if at is None else at,
    )


def settled(plan, seconds=10):
    """Pretend the plan was applied ``seconds`` ago (past grace and min-escalation)."""

    object.__setattr__(plan, "applied_at_monotonic", time.monotonic() - seconds)
    return plan


def test_fault_counter_is_scoped_to_the_core_process():
    runtime = make_runtime()
    settled(prepared(runtime, ENCRYPTED_BLOCKED))
    for _ in range(3):
        runtime._note_dns_resolver_fault(event())
    assert runtime._dns_resolver_faults_since_apply() == 3
    runtime._note_dns_resolver_fault(event(pid=101))
    assert runtime._dns_resolver_faults_since_apply() == 1


def test_faults_inside_the_post_apply_grace_window_belong_to_the_previous_rung():
    runtime = make_runtime()
    plan = prepared(runtime, ENCRYPTED_BLOCKED)
    applied = plan.applied_at_monotonic
    # Mihomo reports a dial that started resolving under the old rung up to
    # its 5 s resolver timeout after the reload: not evidence against this rung.
    for offset in (0.1, 2.0, runtime_module._DNS_RESOLVER_FAULT_GRACE_SECONDS - 0.05):
        runtime._note_dns_resolver_fault(event(at=applied + offset))
    assert runtime._dns_resolver_faults_since_apply() == 0
    assert runtime._dns_resolver_fault_ignored == 3
    runtime._note_dns_resolver_fault(event(at=applied + runtime_module._DNS_RESOLVER_FAULT_GRACE_SECONDS + 0.01))
    assert runtime._dns_resolver_faults_since_apply() == 1


def test_faults_escalate_one_rung_with_dns_lifecycle_reload_and_wake():
    runtime = make_runtime()
    plan = prepared(runtime, QUALIFIED)
    assert plan.rung == RUNG_ENCRYPTED
    for _ in range(3):
        runtime._note_dns_resolver_fault(event())
    assert runtime._dns_resolver_faults_since_apply() == 0  # grace window
    assert runtime._maybe_escalate_dns_resolver_rung() is False  # too soon after apply
    settled(plan)
    for _ in range(3):
        runtime._note_dns_resolver_fault(event())
    assert runtime._maybe_escalate_dns_resolver_rung() is True
    current = runtime._current_dns_resolver_plan()
    assert current.rung == RUNG_SYSTEM
    assert current.reason == REASON_ESCALATED_DNS_FAULTS
    assert current.revision == 2
    assert runtime.reloads == [("incremental_export", RuntimeReloadReason.DNS_LIFECYCLE)]
    assert runtime.marked == [None]
    assert runtime.notified == ["dns_rung_changed"]
    assert runtime._dns_resolver_faults_since_apply() == 0
    assert runtime.manager.tier3_last_collection_report["dns_resolver_rung"] == RUNG_SYSTEM
    # One escalation per evidence window: the next call needs new faults.
    object.__setattr__(current, "applied_at_monotonic", time.monotonic() - 10)
    assert runtime._maybe_escalate_dns_resolver_rung(allow_control_query=False) is False


def test_failed_reload_restores_previous_plan():
    runtime = make_runtime()
    plan = prepared(runtime, QUALIFIED)
    object.__setattr__(plan, "applied_at_monotonic", time.monotonic() - 10)
    runtime.reload_core_config = lambda **kwargs: {"reloaded": False, "reason": "busy"}
    for _ in range(3):
        runtime._note_dns_resolver_fault(event())
    assert runtime._maybe_escalate_dns_resolver_rung() is False
    assert runtime._current_dns_resolver_plan() is plan
    assert runtime.notified == []


def test_escalation_without_running_core_swaps_plan_without_reload():
    runtime = make_runtime(runner=FakeRunner(running=False))
    plan = prepared(runtime, QUALIFIED)
    object.__setattr__(plan, "applied_at_monotonic", time.monotonic() - 10)
    for _ in range(3):
        runtime._note_dns_resolver_fault(event())
    assert runtime._maybe_escalate_dns_resolver_rung() is True
    assert runtime.reloads == []
    assert runtime._current_dns_resolver_plan().rung == RUNG_SYSTEM


class FakeApiClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def _request(self, method, path, *, query=None, timeout=None):
        if self.error is not None:
            raise self.error
        return self.payload


@pytest.mark.parametrize("client,outer_fake,expected", [
    (None, False, False),
    (FakeApiClient(error=RuntimeError("controller down")), False, False),
    (FakeApiClient({"Status": 0, "Answer": [{"data": "1.1.1.1"}]}), False, False),
    (FakeApiClient({"Status": 0, "Answer": [{"data": "198.18.0.7"}]}), False, True),
    (FakeApiClient({"Status": 0, "Answer": [{"data": "198.18.0.7"}]}), True, False),
    (FakeApiClient({"Status": 2, "Answer": []}), False, True),
    (FakeApiClient({"Status": 0}), False, True),
])
def test_control_query_failure_requires_live_controller_and_respects_outer_fake_ip(client, outer_fake, expected):
    runtime = make_runtime()
    runtime.api_client = client
    plan = prepared(runtime, qualification(
        candidates=(EncryptedCandidateResult(CANDIDATES["tls://9.9.9.9"], CANDIDATE_QUALIFIED, 30),),
        links=(LinkResolverResult("222.198.178.68", LINK_ANSWERS_PUBLIC, 5),),
        outer_fake_ip=outer_fake,
    ))
    assert runtime._dns_resolver_control_query_failed(plan) is expected


class FakeHangingResolverClient(FakeApiClient):
    """Controller alive, but the Core's resolver never answers in time."""

    def _request(self, method, path, *, query=None, timeout=None):
        if path == "/version":
            return {"version": "test"}
        raise TimeoutError("read timed out")


def test_hung_resolution_on_a_live_controller_is_positive_failure():
    runtime = make_runtime()
    runtime.api_client = FakeHangingResolverClient()
    plan = prepared(runtime, QUALIFIED)
    assert runtime._dns_resolver_control_query_verdict(plan) == runtime_module._DNS_CONTROL_FAILED
    runtime.api_client = FakeApiClient(error=RuntimeError("controller down"))
    assert runtime._dns_resolver_control_query_verdict(plan) == runtime_module._DNS_CONTROL_UNKNOWN


def test_control_query_alone_needs_consecutive_failures_before_it_escalates():
    runtime = make_runtime()
    runtime.api_client = FakeApiClient({"Status": 2, "Answer": []})
    plan = settled(prepared(runtime, QUALIFIED))
    assert runtime._maybe_escalate_dns_resolver_rung() is False
    assert runtime._dns_resolver_control_failures == 1
    # Rate limited: an immediate second round does not re-query the controller.
    assert runtime._maybe_escalate_dns_resolver_rung() is False
    assert runtime._dns_resolver_control_failures == 1
    runtime._dns_resolver_control_checked_at = time.monotonic() - 10
    assert runtime._maybe_escalate_dns_resolver_rung() is True
    assert runtime._current_dns_resolver_plan().reason == REASON_ESCALATED_CONTROL_QUERY
    assert runtime._current_dns_resolver_plan().rung == RUNG_SYSTEM
    assert runtime._dns_resolver_control_failures == 0


def test_control_query_answer_vetoes_core_faults_and_resets_the_window():
    runtime = make_runtime()
    runtime.api_client = FakeApiClient({"Status": 0, "Answer": [{"data": "1.1.1.1"}]})
    plan = settled(prepared(runtime, QUALIFIED))
    for _ in range(5):
        runtime._note_dns_resolver_fault(event())
    assert runtime._dns_resolver_faults_since_apply() == 5
    # The Core failed to resolve some names, but its resolver answers: the
    # names (or their upstream) are the problem, not the rung.
    assert runtime._maybe_escalate_dns_resolver_rung() is False
    assert runtime._current_dns_resolver_plan() is plan
    assert runtime._dns_resolver_faults_since_apply() == 0
    assert runtime.reloads == [] and runtime.notified == []


def test_core_faults_confirmed_by_a_failed_control_query_escalate_at_once():
    runtime = make_runtime()
    runtime.api_client = FakeApiClient({"Status": 2, "Answer": []})
    settled(prepared(runtime, QUALIFIED))
    for _ in range(3):
        runtime._note_dns_resolver_fault(event())
    assert runtime._maybe_escalate_dns_resolver_rung() is True
    assert runtime._current_dns_resolver_plan().reason == REASON_ESCALATED_DNS_FAULTS


# ------------------------------------------------------------- no egress

def report_round(runtime, retry, success, fetched_ok=0, **extra):
    with runtime.manager.lock:
        runtime.manager.tier3_last_collection_report.update({
            "runtime_proxy_retry_count": retry,
            "runtime_proxy_retry_success_count": success,
            "source_fetch_ok": fetched_ok,
            **extra,
        })
    runtime._observe_source_supply_health()


def test_exhausted_ladder_and_three_zero_success_rounds_park_supply_pre_gate():
    runtime = make_runtime()
    plan = prepared(runtime, qualification(
        candidates=(EncryptedCandidateResult(CANDIDATES["tls://9.9.9.9"], CANDIDATE_TLS_FAILED),),
        links=(LinkResolverResult("10.0.0.53", LINK_ANSWERS_INTERNAL_ONLY, 4),),
    ))
    assert plan.rung == RUNG_INTRANET_ONLY and plan.remaining == 0
    report_round(runtime, retry=5, success=0)   # baseline
    state = runtime._source_supply_no_egress_state
    assert not state.get("active")
    report_round(runtime, retry=10, success=0)  # one round without success on the terminal rung
    assert state["active"] is True and state["detail"] == "dns_unresolvable"
    manager = runtime.manager
    assert runtime.terminal_calls == ["fail_terminal"]
    report = manager.tier3_last_collection_report
    assert report["startup_failure_reason"] == "source_supply_no_egress"
    assert report["startup_failure_detail"] == "dns_unresolvable"
    assert report["source_supply_terminal"] is True
    assert report["source_supply_terminal_reason"] == "no_egress"
    assert runtime._source_supply_no_egress_reason == "source_supply_no_egress"
    assert manager.parked == [(300.0, "no_egress")]
    assert [item.event_key for item in manager.events] == ["tier3.source_supply_no_egress"]
    timer = runtime._source_supply_no_egress_timer
    assert timer is not None and timer.is_alive()
    timer.cancel()
    # A second observation while parked does nothing.
    report_round(runtime, retry=15, success=0)
    assert manager.parked == [(300.0, "no_egress")]


def test_dns_ok_but_sources_unreachable_needs_six_rounds_and_leaves_core_alone_post_gate():
    runtime = make_runtime()
    runtime._current_core_runtime_phase = lambda: "formal_no_capture"
    plan = prepared(runtime, QUALIFIED)
    assert plan.remaining > 0
    report_round(runtime, retry=0, success=0)
    for retry in range(1, 6):
        report_round(runtime, retry=retry * 5, success=0)
        assert not runtime._source_supply_no_egress_state.get("active"), retry
    report_round(runtime, retry=30, success=0)
    state = runtime._source_supply_no_egress_state
    assert state["active"] and state["detail"] == "sources_unreachable"
    assert runtime.terminal_calls == []  # formal Core keeps serving other layers
    report = runtime.manager.tier3_last_collection_report
    assert "startup_failure_reason" not in report
    assert report["source_supply_terminal_reason"] == "no_egress"
    assert runtime.manager.parked == [(300.0, "no_egress")]
    runtime._source_supply_no_egress_timer.cancel()


def test_a_successful_fetch_resets_the_round_counter():
    runtime = make_runtime()
    prepared(runtime, QUALIFIED)
    report_round(runtime, retry=0, success=0)
    report_round(runtime, retry=5, success=0)
    report_round(runtime, retry=10, success=1)
    assert runtime._source_supply_no_egress_state["rounds"] == 0
    for retry in (15, 20, 25, 30, 35, 40):
        report_round(runtime, retry=retry, success=1, fetched_ok=1)
    assert not runtime._source_supply_no_egress_state.get("active")


def test_retry_timer_requalifies_clears_terminal_and_resumes_supply():
    runtime = make_runtime()
    prepared(runtime, qualification(
        candidates=(EncryptedCandidateResult(CANDIDATES["tls://9.9.9.9"], CANDIDATE_TLS_FAILED),),
        links=(LinkResolverResult("10.0.0.53", LINK_ANSWERS_INTERNAL_ONLY, 4),),
    ))
    report_round(runtime, retry=0, success=0)
    report_round(runtime, retry=5, success=0)
    assert runtime._source_supply_no_egress_state["active"]
    runtime._source_supply_no_egress_timer.cancel()
    runtime._current_core_runtime_phase = lambda: "stopped"
    with patch.object(runtime_module, "discover_link_resolvers", return_value=LinkDiscovery(fingerprint="fp-new")), \
            patch.object(runtime_module, "qualify_resolvers", return_value=QUALIFIED):
        runtime._retry_after_source_supply_no_egress()
    assert runtime._source_supply_no_egress_state == {}
    assert runtime._current_dns_resolver_plan().rung == RUNG_ENCRYPTED
    report = runtime.manager.tier3_last_collection_report
    assert report["source_supply_terminal"] is False and report["startup_failed"] is False
    assert "startup_failure_reason" not in report
    assert runtime._tier3_startup_terminal_failure is False
    assert runtime.manager.resumed == ["no_egress_retry"]
    assert runtime.terminal_calls[-1] == "bootstrap_restart"


def test_runtime_reset_clears_plan_faults_and_terminal_state():
    runtime = make_runtime()
    prepared(runtime, QUALIFIED)
    runtime._note_dns_resolver_fault(event())
    runtime._source_supply_no_egress_state.update(active=True)
    runtime._reset_dns_resolver_runtime_state()
    assert runtime._current_dns_resolver_plan() is None
    assert runtime._dns_resolver_faults_since_apply() == 0
    assert runtime._source_supply_no_egress_state == {}


# ------------------------------------------------- background plan apply

def test_background_plan_applies_itself_to_a_core_rendered_without_it(tmp_path):
    runtime = make_runtime()
    rendered = tmp_path / "mihomo-runtime.yaml"
    rendered.write_text(
        "dns:\n  enable: true\n  nameserver:\n  - https://doh.pub/dns-query\nrules:\n- MATCH,GENERAL\n",
        encoding="utf-8",
    )
    runtime._current_mihomo_runtime_config_path = lambda: rendered
    with patch.object(runtime_module, "discover_link_resolvers", return_value=LinkDiscovery(fingerprint="fp-test")), \
            patch.object(runtime_module, "qualify_resolvers", return_value=QUALIFIED):
        runtime._schedule_dns_resolver_plan(reason="core_start")
        worker = runtime._dns_resolver_plan_worker
        worker.join(5.0)
    assert not worker.is_alive()
    plan = runtime._current_dns_resolver_plan()
    assert plan is not None and plan.rung == RUNG_ENCRYPTED
    assert runtime.reloads == [("incremental_export", RuntimeReloadReason.DNS_LIFECYCLE)]
    assert runtime.notified == ["dns_plan_applied"]
    # A fresh plan short-circuits scheduling entirely.
    runtime.reloads.clear()
    with patch.object(runtime_module, "qualify_resolvers") as probe:
        runtime._schedule_dns_resolver_plan(reason="core_start")
    probe.assert_not_called()
    assert runtime.reloads == []


def test_background_plan_does_not_reload_when_core_already_matches_or_is_stopped(tmp_path):
    runtime = make_runtime()
    plan = prepared(runtime, QUALIFIED)
    rendered = tmp_path / "mihomo-runtime.yaml"
    rendered.write_text(
        "dns:\n" + "\n".join(f"  {key}: {value!r}" for key, value in plan.block.items() if not isinstance(value, (list, dict)))
        + "\n", encoding="utf-8",
    )
    runtime._current_mihomo_runtime_config_path = lambda: rendered
    # Written block differs (lists dropped) -> would reload, but the Core is stopped.
    runtime.core_runner = FakeRunner(running=False)
    assert runtime._apply_dns_resolver_plan_to_running_core(plan) is False
    assert runtime.reloads == []
    # Matching block -> nothing to apply even with a running Core.
    import yaml as _yaml
    rendered.write_text(_yaml.safe_dump({"dns": dict(plan.block)}), encoding="utf-8")
    runtime.core_runner = FakeRunner(running=True)
    assert runtime._apply_dns_resolver_plan_to_running_core(plan) is False
    assert runtime.reloads == []
    # Unreadable file is unknown, not evidence.
    runtime._current_mihomo_runtime_config_path = lambda: tmp_path / "missing.yaml"
    assert runtime._apply_dns_resolver_plan_to_running_core(plan) is False


# --------------------------------------------------- mode, watch, refresh

def test_system_mode_starts_the_ladder_at_the_link_resolver():
    runtime = make_runtime(dns_config={"mode": "system"})
    plan = prepared(runtime, QUALIFIED)
    assert plan.rung == RUNG_SYSTEM and plan.mode == "system"
    assert plan.reason == "link_resolvers_answer"
    assert plan.remaining == 1
    assert runtime.manager.tier3_last_collection_report["dns_resolver_mode"] == "system"
    from dns_resolver_policy import active_dns_block, publish_active_dns_block

    try:
        assert active_dns_block() == dict(plan.block)
    finally:
        publish_active_dns_block(None)


def test_network_change_requalifies_and_applies_to_the_running_core(tmp_path):
    runtime = make_runtime()
    plan = prepared(runtime, QUALIFIED)
    rendered = tmp_path / "mihomo-runtime.yaml"
    import yaml as _yaml

    rendered.write_text(_yaml.safe_dump({"dns": dict(plan.block)}), encoding="utf-8")
    runtime._current_mihomo_runtime_config_path = lambda: rendered
    # Same fingerprint: nothing happens.
    with patch.object(runtime_module, "discover_link_resolvers", return_value=LinkDiscovery(fingerprint="fp-test")), \
            patch.object(runtime_module, "qualify_resolvers") as probe:
        assert runtime._dns_resolver_watch_once() is False
    probe.assert_not_called()
    # New fingerprint: re-qualify; a different rung is hot-reloaded into the Core.
    with patch.object(runtime_module, "discover_link_resolvers", return_value=LinkDiscovery(fingerprint="fp-new")), \
            patch.object(runtime_module, "qualify_resolvers", return_value=ENCRYPTED_BLOCKED):
        assert runtime._dns_resolver_watch_once() is True
    assert runtime._current_dns_resolver_plan().rung == RUNG_SYSTEM
    assert runtime.reloads == [("incremental_export", RuntimeReloadReason.DNS_LIFECYCLE)]


def test_network_change_during_no_egress_retries_immediately():
    runtime = make_runtime()
    prepared(runtime, qualification(
        candidates=(EncryptedCandidateResult(CANDIDATES["tls://9.9.9.9"], CANDIDATE_TLS_FAILED),),
        links=(LinkResolverResult("10.0.0.53", LINK_ANSWERS_INTERNAL_ONLY, 4),),
    ))
    report_round(runtime, retry=0, success=0)
    report_round(runtime, retry=5, success=0)
    assert runtime._source_supply_no_egress_state["active"]
    runtime._source_supply_no_egress_timer.cancel()
    with patch.object(runtime_module, "discover_link_resolvers", return_value=LinkDiscovery(fingerprint="fp-new")), \
            patch.object(runtime_module, "qualify_resolvers", return_value=QUALIFIED):
        assert runtime._dns_resolver_watch_once() is True
    assert runtime._source_supply_no_egress_state == {}
    assert runtime.manager.resumed == ["no_egress_retry"]
    assert runtime._current_dns_resolver_plan().rung == RUNG_ENCRYPTED


def test_refresh_dns_resolver_policy_forces_a_new_plan_for_new_settings():
    runtime = make_runtime()
    plan = prepared(runtime, QUALIFIED)
    runtime.config["client"]["dns"] = {"mode": "system"}
    with patch.object(runtime_module, "discover_link_resolvers", return_value=LinkDiscovery(fingerprint="fp-test")), \
            patch.object(runtime_module, "qualify_resolvers", return_value=QUALIFIED):
        summary = runtime.refresh_dns_resolver_policy("settings_changed")
    assert summary["rung"] == RUNG_SYSTEM
    current = runtime._current_dns_resolver_plan()
    assert current is not plan and current.revision == plan.revision + 1 and current.mode == "system"


def test_delivered_supply_lifts_the_park_without_waiting_for_the_retry_timer():
    """The verdict is drawn from rounds that fetched nothing; a delivery outranks it."""

    runtime = make_runtime()
    prepared(runtime, qualification(
        candidates=(EncryptedCandidateResult(CANDIDATES["tls://9.9.9.9"], CANDIDATE_TLS_FAILED),),
        links=(LinkResolverResult("10.0.0.53", LINK_ANSWERS_INTERNAL_ONLY, 4),),
    ))
    report_round(runtime, retry=0, success=0)
    report_round(runtime, retry=5, success=0)
    assert runtime._source_supply_no_egress_state["active"]
    runtime._source_supply_no_egress_timer.cancel()
    runtime._current_core_runtime_phase = lambda: "stopped"

    runtime._on_runtime_source_supply_observed({"source_fetch_ok": 2, "node_count": 900})

    assert runtime._source_supply_no_egress_state == {}
    report = runtime.manager.tier3_last_collection_report
    assert report["source_supply_terminal"] is False and report["startup_failed"] is False
    assert "startup_failure_reason" not in report
    assert runtime._tier3_startup_terminal_failure is False
    assert runtime.manager.resumed == ["supply_observed"]
    assert runtime.terminal_calls[-1] == "bootstrap_restart"


def test_supply_observation_without_an_active_park_changes_nothing():
    runtime = make_runtime()
    prepared(runtime, QUALIFIED)

    runtime._on_runtime_source_supply_observed({"source_fetch_ok": 1, "node_count": 5})

    assert runtime.manager.resumed == []
    assert "source_supply_terminal" not in runtime.manager.tier3_last_collection_report
