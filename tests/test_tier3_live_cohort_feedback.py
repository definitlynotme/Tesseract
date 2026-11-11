"""Local transport fixture; real Actor, process helper, Mihomo and commit queue.

This tests asynchronous tail feedback, not public-source availability or a
real-network performance baseline. Only TCP outcomes and the upstream HTTP
proxy response are controlled; no PASS or promotion is injected.
"""

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import json
import threading
import time

import pytest

from models import Node
from tests.test_tier3_candidate_scheduler import feature
from tests.test_tier3_validator_process_domain import (
    MIHOMO, _ProxyServer, _PassProxyHandler,
)
from tier3_candidate_scheduler import Tier3CandidateLookaheadEvidence, Tier3CandidateScheduler
from tier3_validation_domain import Tier3ValidationDomain, validation_fresh_supply_depth
from tier3_validation_feedback import Tier3CandidateDispatchContext, Tier3ValidationFeedbackKind
from tier3_validation_protocol import AdmissionCommitted


@pytest.mark.parametrize("replay_seed", [False, True], ids=["cold", "current-replay-trial"])
def test_async_tail_confirmation_keeps_real_helper_and_commit_contract(replay_seed):
    server = _ProxyServer(("127.0.0.1", 0), _PassProxyHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    tcp_alive = set()
    tcp_observed = set()
    scheduler = Tier3CandidateScheduler(
        max_active_arms_per_family=1,
        lookahead_evidence_provider=lambda item: Tier3CandidateLookaheadEvidence(
            validated_positive=replay_seed and item.node.name == "seed",
            tcp_only=item.node.name in tcp_observed,
        ),
    )
    scheduler.set_credit_envelope(16, handoff_window=16, tcp_execution_limit=16)
    committed = {}
    commit_lock = threading.Lock()
    feedback_errors = []
    started = time.monotonic()

    def commit(_node, _report, intent):
        with commit_lock:
            assert intent.candidate_digest not in committed
            committed[intent.candidate_digest] = time.monotonic() - started
            revision = len(committed)
        return AdmissionCommitted(
            refresh_epoch=intent.refresh_epoch, candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id, sequence=intent.sequence, pool_revision=revision,
            net_new_pool_growth=True,
        )

    def feedback(event):
        try:
            if event.kind is Tier3ValidationFeedbackKind.FAST_OBSERVATION:
                scheduler.on_fast_observation(
                    event.ticket, outcome=event.outcome, passed=event.passed,
                    right_censored=event.right_censored,
                    facility_failure=event.facility_failure,
                    busy_worker_ms=event.busy_worker_ms,
                )
            elif event.kind is Tier3ValidationFeedbackKind.CONTINUATION:
                scheduler.on_fast_yield(event.ticket, right_censored=event.right_censored)
            elif event.kind is Tier3ValidationFeedbackKind.TERMINAL:
                scheduler.on_terminal(
                    event.ticket, passed=event.passed, outcome=event.outcome,
                    stage=event.stage.value, busy_worker_ms=event.busy_worker_ms,
                )
            elif event.kind is Tier3ValidationFeedbackKind.ADMISSION:
                scheduler.on_commit_complete(event.ticket, net_new=event.net_new_pool_growth)
        except Exception as error:
            feedback_errors.append(type(error).__name__)
            raise

    domain = Tier3ValidationDomain(
        refresh_epoch="local-async-tail", binary_path=str(MIHOMO),
        probe_urls=["http://probe-a.invalid/generate_204", "http://probe-b.invalid/generate_204"],
        expected_statuses={204}, probe_timeout_seconds=5.0, slow_threshold_ms=3000,
        commit_node=commit, terminal_observer=lambda *_: None,
        feedback_observer=feedback, min_workers=4, max_workers=16,
    )

    def add(name, *, alive, observed=False, leaf="precise-leaf"):
        if alive:
            tcp_alive.add(name)
        if observed:
            tcp_observed.add(name)
        node = Node(
            name=name, protocol="http", address="127.0.0.1", port=server.server_address[1],
            password=f"local-fixture-{name}", source="opaque-root", tier="tier3",
            extra={"username": "local-user"},
        )
        scheduler.enqueue(
            node, {}, arm_key=name, source_root="opaque-root", family="opaque-family",
            coarse_key=name, feature_key=feature(
                family="opaque-family", service_profiles=("l0", "l1", "l2", leaf),
            ),
        )

    def tcp(reservation):
        scheduler.commit_handoff(reservation)
        alive = reservation.candidate.node.name in tcp_alive
        # The socket transport is the controlled boundary. Slow failures are
        # delivered independently, after other FAST results and reservations.
        time.sleep(0.025 if alive else 1.25)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=alive)
        if alive:
            node = reservation.candidate.node
            assert domain.submit(node, {}, dispatch_context=Tier3CandidateDispatchContext(
                ticket=reservation.ticket, node_identity=node.name,
            ))

    pending = set()
    pool = ThreadPoolExecutor(max_workers=16)
    domain.start()
    domain.set_envelope(16, pressure=1.0, deficit=25)
    add("seed", alive=True)
    tail_added = False
    ready_peak = 0
    try:
        while time.monotonic() - started < 61.0:
            snapshot = domain.snapshot()
            ready_peak = max(ready_peak, snapshot.supervisor.active_workers)
            with commit_lock:
                count = len(committed)
            if count >= 25:
                # Finishing the local fixture quickly does not prove the
                # configured helper ramp. Keep its separate readiness gate.
                if ready_peak == 16 or time.monotonic() - started >= 12.0:
                    break
                time.sleep(0.01)
                continue
            if count and not tail_added:
                for index in range(128 if not replay_seed else 4096):
                    add(f"unknown-prefix-{index}", alive=False,
                        leaf="precise-leaf" if not replay_seed else f"bad-leaf-{index // 64}")
                for index in range(48):
                    add(f"tcp-peer-{index}", alive=True, observed=not replay_seed)
                for index in range(256):
                    add(f"ordinary-{index}", alive=False, leaf=f"sibling-{index}")
                tail_added = True
            if validation_fresh_supply_depth(snapshot) + len(pending) < 32:
                for reservation in scheduler.reserve_handoffs(16 - len(pending)):
                    pending.add(pool.submit(tcp, reservation))
            if pending:
                done, pending = wait(pending, timeout=0.01, return_when=FIRST_COMPLETED)
                for future in done:
                    future.result()
            else:
                time.sleep(0.01)
        with commit_lock:
            timeline = sorted(committed.values())
        metrics = scheduler.quick_metrics()
        print({"fixture": "local_async_tail", "committed": len(timeline),
               "n3": round(timeline[2], 3) if len(timeline) >= 3 else None,
               "n20": sum(at <= 20 for at in timeline),
               "n25": round(timeline[24], 3) if len(timeline) >= 25 else None,
               "ready_helpers": ready_peak,
               "service_net_new": metrics["reward_service_net_new_count"]})
        assert len(timeline) >= 25, (len(timeline), metrics["reward_service_selected_count"])
        assert timeline[2] <= 10.0
        assert sum(at <= 20.0 for at in timeline) >= 15
        assert timeline[24] <= 61.0
        helper_snapshot = domain.snapshot().supervisor
        helper_diagnostic = {
            "desired": helper_snapshot.desired_workers,
            "active": helper_snapshot.active_workers,
            "starting": helper_snapshot.starting_workers,
            "waiting": helper_snapshot.waiting_start_workers,
            "pending_spawn": helper_snapshot.pending_spawn_workers,
            "resource_limited": helper_snapshot.resource_headroom_limited,
            "private_bytes": helper_snapshot.private_bytes,
            "handle_count": helper_snapshot.handle_count,
            "system_commit_headroom_bytes": helper_snapshot.system_commit_headroom_bytes,
            "failures": helper_snapshot.failure_reason_counts,
            "ready_elapsed": [
                (item["worker_id"], round(item["ready_at"] - started, 3) if item["ready_at"] else None,
                 item["prewarm_started"], item["prewarm_membership_pending"])
                for item in helper_snapshot.workers
            ],
            "phases": [
                (item["worker_id"], item["phase"], item["startup_phase"],
                 round(item["startup_deadline_at"] - time.monotonic(), 3))
                for item in helper_snapshot.workers
            ],
        }
        assert ready_peak == 16, json.dumps(helper_diagnostic, sort_keys=True)
        assert metrics["reward_service_selected_count"] >= 4
        assert metrics["reward_service_net_new_count"] >= 2
        if replay_seed:
            assert metrics["replay_leaf_discovery"]["net_new"] == 1
        assert metrics["reward_family_fallback_hit_count"] == 0
        assert not feedback_errors
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        for future in pending:
            if not future.cancelled():
                future.result()
        stopped = domain.stop(timeout_seconds=6.0)
        scheduler.release_outstanding()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1.0)
        assert stopped == "STOPPED"
        assert scheduler._tickets == {}
        assert scheduler.pending_handoff_count() == 0
