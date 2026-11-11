from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from runtime_reload_gate import (
    RuntimeReloadDisposition,
    RuntimeReloadGate,
    RuntimeReloadReason,
    RuntimeReloadRequest,
)


def request(
    digest: str,
    reason: RuntimeReloadReason | str,
    **kwargs: object,
) -> RuntimeReloadRequest:
    return RuntimeReloadRequest(
        semantic_digest=digest,
        reason=reason,
        **kwargs,
    )


@pytest.mark.parametrize(
    "reason",
    [
        RuntimeReloadReason.USER_GRAPH_CONFIG,
        RuntimeReloadReason.CAPTURE_LIFECYCLE,
        RuntimeReloadReason.TUN_LIFECYCLE,
        RuntimeReloadReason.DNS_LIFECYCLE,
        RuntimeReloadReason.CORE_RECOVERY,
        RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
        RuntimeReloadReason.COLD_SAME_DIGEST,
    ],
)
def test_same_semantic_digest_is_always_rejected(
    reason: RuntimeReloadReason,
) -> None:
    gate = RuntimeReloadGate("loaded")

    decision = gate.request(request("loaded", reason))

    assert decision.disposition is RuntimeReloadDisposition.REJECT
    assert decision.code == "same_semantic_digest"
    assert gate.snapshot().active_reload_reason == ""


@pytest.mark.parametrize(
    "reason",
    [
        RuntimeReloadReason.RUNTIME_HEALTH,
        RuntimeReloadReason.RUNTIME_FAILOVER,
        RuntimeReloadReason.RUNTIME_RECHECK,
        RuntimeReloadReason.RUNTIME_FAILBACK,
        RuntimeReloadReason.RUNTIME_FACILITY,
        RuntimeReloadReason.COLD_SAME_DIGEST,
    ],
)
def test_runtime_hot_path_reasons_cannot_reload_even_a_new_digest(
    reason: RuntimeReloadReason,
) -> None:
    gate = RuntimeReloadGate("loaded")

    decision = gate.request(request("changed", reason))

    assert decision.rejected
    assert decision.code == "runtime_hot_path_reload_forbidden"


@pytest.mark.parametrize(
    "reason",
    [
        RuntimeReloadReason.USER_GRAPH_CONFIG,
        RuntimeReloadReason.CAPTURE_LIFECYCLE,
        RuntimeReloadReason.TUN_LIFECYCLE,
        RuntimeReloadReason.DNS_LIFECYCLE,
        RuntimeReloadReason.CORE_RECOVERY,
        RuntimeReloadReason.MEMBERSHIP_UNREPRESENTABLE,
    ],
)
def test_explicit_non_runtime_reasons_can_acquire_reload_lease(
    reason: RuntimeReloadReason,
) -> None:
    gate = RuntimeReloadGate("loaded")

    decision = gate.request(request("changed", reason))

    assert decision.allowed
    assert decision.lease is not None
    assert gate.complete_reload(decision.lease, applied=True)
    assert gate.loaded_semantic_digest == "changed"


def test_membership_reload_requires_proof_that_dynamic_update_is_unavailable() -> None:
    gate = RuntimeReloadGate("loaded")

    dynamic = gate.request(
        request(
            "changed",
            RuntimeReloadReason.MEMBERSHIP_CHANGE,
            dynamic_update_supported=True,
        )
    )
    required = gate.request(
        request(
            "changed",
            RuntimeReloadReason.MEMBERSHIP_CHANGE,
            dynamic_update_supported=False,
        )
    )

    assert dynamic.rejected
    assert dynamic.code == "membership_is_dynamically_expressible"
    assert required.allowed


def test_unknown_reason_and_missing_digest_fail_closed() -> None:
    gate = RuntimeReloadGate("loaded")

    assert gate.request(request("changed", "surprise")).code == "unknown_reload_reason"
    assert (
        gate.request(request("", RuntimeReloadReason.USER_GRAPH_CONFIG)).code
        == "missing_semantic_digest"
    )


def test_recovery_owner_is_exclusive_and_must_finish_reload_before_release() -> None:
    gate = RuntimeReloadGate("loaded")
    recovery = gate.begin_recovery("incident-a")
    assert recovery is not None
    assert gate.begin_recovery("incident-a") is recovery
    assert gate.begin_recovery("incident-b") is None

    outsider = gate.request(
        request("changed", RuntimeReloadReason.CAPTURE_LIFECYCLE)
    )
    rejected = gate.request(
        request(
            "changed",
            RuntimeReloadReason.CAPTURE_LIFECYCLE,
            wait_if_busy=False,
        )
    )
    owner = gate.request(
        request(
            "changed",
            RuntimeReloadReason.CORE_RECOVERY,
            recovery_token=recovery.token,
        )
    )

    assert outsider.should_wait
    assert rejected.rejected
    assert owner.allowed and owner.lease is not None
    assert not gate.end_recovery(recovery)
    assert gate.complete_reload(owner.lease, applied=True)
    assert gate.end_recovery(recovery)


def test_recovery_cannot_start_while_reload_transaction_is_active() -> None:
    gate = RuntimeReloadGate("loaded")
    reload_decision = gate.request(
        request("changed", RuntimeReloadReason.DNS_LIFECYCLE)
    )
    assert reload_decision.lease is not None

    assert gate.begin_recovery("incident") is None
    assert gate.cancel_reload(reload_decision.lease)
    assert gate.begin_recovery("incident") is not None


def test_stale_or_foreign_recovery_token_cannot_authorize_reload() -> None:
    gate = RuntimeReloadGate("loaded")
    recovery = gate.begin_recovery("incident")
    assert recovery is not None

    foreign = gate.request(
        request(
            "changed",
            RuntimeReloadReason.CORE_RECOVERY,
            recovery_token="foreign",
        )
    )
    assert foreign.rejected
    assert foreign.code == "stale_recovery_token"

    assert gate.end_recovery(recovery)
    stale = gate.request(
        request(
            "changed",
            RuntimeReloadReason.CORE_RECOVERY,
            recovery_token=recovery.token,
        )
    )
    assert stale.rejected
    assert stale.code == "stale_recovery_token"


def test_one_user_commit_waits_and_duplicate_requests_share_it() -> None:
    gate = RuntimeReloadGate("loaded")
    recovery = gate.begin_recovery("incident")
    assert recovery is not None
    user_request = request(
        "user-change",
        RuntimeReloadReason.USER_GRAPH_CONFIG,
        user_commit_id="commit-1",
    )

    waiting = gate.request(user_request)
    duplicate = gate.request(user_request)
    second_commit = gate.request(
        request(
            "other-change",
            RuntimeReloadReason.USER_GRAPH_CONFIG,
            user_commit_id="commit-2",
        )
    )

    assert waiting.should_wait and waiting.deferred_token
    assert duplicate.deduplicated
    assert duplicate.deferred_token == waiting.deferred_token
    assert second_commit.rejected
    assert second_commit.code == "another_user_commit_is_already_waiting"

    assert gate.end_recovery(recovery)
    claimed = gate.claim_deferred_user_commit(waiting.deferred_token)
    assert claimed.allowed and claimed.lease is not None
    assert gate.complete_reload(claimed.lease, applied=True)

    completed_duplicate = gate.request(user_request)
    assert completed_duplicate.rejected
    assert completed_duplicate.code == "same_semantic_digest"

    # Commit identity remains deduplicated even if a later, different graph
    # becomes the loaded digest.
    gate.set_loaded_semantic_digest("later-change")
    completed_duplicate = gate.request(user_request)
    assert completed_duplicate.deduplicated
    assert completed_duplicate.code == "user_commit_already_completed"


def test_failed_user_reload_can_retry_the_same_commit() -> None:
    gate = RuntimeReloadGate("loaded")
    user_request = request(
        "changed",
        RuntimeReloadReason.USER_GRAPH_CONFIG,
        user_commit_id="commit-1",
    )

    first = gate.request(user_request)
    assert first.lease is not None
    assert gate.complete_reload(first.lease, applied=False)

    retry = gate.request(user_request)
    assert retry.allowed


def test_concurrent_duplicate_user_commit_issues_exactly_one_reload_lease() -> None:
    gate = RuntimeReloadGate("loaded")
    user_request = request(
        "changed",
        RuntimeReloadReason.USER_GRAPH_CONFIG,
        user_commit_id="commit-1",
    )

    with ThreadPoolExecutor(max_workers=16) as executor:
        decisions = list(executor.map(lambda _: gate.request(user_request), range(64)))

    assert sum(decision.allowed for decision in decisions) == 1
    assert sum(decision.deduplicated for decision in decisions) == 63


def test_stale_leases_cannot_complete_or_release_current_transactions() -> None:
    gate = RuntimeReloadGate("loaded")
    recovery = gate.begin_recovery("incident")
    assert recovery is not None
    assert not gate.end_recovery("stale")
    assert gate.end_recovery(recovery)

    reload_decision = gate.request(
        request("changed", RuntimeReloadReason.TUN_LIFECYCLE)
    )
    assert reload_decision.lease is not None
    assert not gate.complete_reload("stale", applied=True)
    assert gate.cancel_reload(reload_decision.lease)


def test_successful_completion_requires_the_lease_target_digest() -> None:
    gate = RuntimeReloadGate("loaded")
    decision = gate.request(
        request("changed", RuntimeReloadReason.CAPTURE_LIFECYCLE)
    )
    assert decision.lease is not None

    with pytest.raises(ValueError, match="does not match"):
        gate.complete_reload(
            decision.lease,
            applied=True,
            loaded_semantic_digest="different",
        )

    # Validation failure does not silently consume the live transaction.
    assert gate.cancel_reload(decision.lease)


def test_loaded_digest_idle_sync_never_steals_an_active_reload_commit() -> None:
    gate = RuntimeReloadGate("loaded")
    decision = gate.request(
        request("changed", RuntimeReloadReason.CAPTURE_LIFECYCLE)
    )
    assert decision.lease is not None

    assert gate.set_loaded_semantic_digest_if_idle("changed") is False
    assert gate.loaded_semantic_digest == "loaded"
    assert gate.complete_reload(decision.lease, applied=True)
    assert gate.loaded_semantic_digest == "changed"

    assert gate.set_loaded_semantic_digest_if_idle("external") is True
    assert gate.loaded_semantic_digest == "external"


def test_dynamic_recovery_digest_commit_and_release_are_atomic() -> None:
    gate = RuntimeReloadGate("loaded")
    recovery = gate.begin_recovery("provider-update")
    assert recovery is not None

    assert gate.end_recovery_with_loaded_semantic_digest(
        recovery,
        "provider-changed",
    )
    snapshot = gate.snapshot()
    assert snapshot.loaded_semantic_digest == "provider-changed"
    assert snapshot.active_recovery_transaction_id == ""
    assert gate.begin_recovery("next") is not None


def test_new_core_reset_invalidates_process_scoped_work() -> None:
    gate = RuntimeReloadGate("loaded")
    recovery = gate.begin_recovery("incident")
    assert recovery is not None
    waiting = gate.request(
        request(
            "changed",
            RuntimeReloadReason.USER_GRAPH_CONFIG,
            user_commit_id="commit",
        )
    )
    assert waiting.should_wait

    gate.reset_for_new_core()

    snapshot = gate.snapshot()
    assert snapshot.loaded_semantic_digest == ""
    assert snapshot.active_recovery_transaction_id == ""
    assert snapshot.pending_user_commit_id == ""
    core_reload = gate.request(
        request("loaded", RuntimeReloadReason.CORE_RECOVERY)
    )
    assert core_reload.allowed
