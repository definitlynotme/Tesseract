from __future__ import annotations

import threading
import traceback
from unittest.mock import patch

import pytest

import manager as manager_module
from models import Node, NodeStatus
from tests import test_manager as helpers


@pytest.mark.parametrize("failed", [False, True])
def test_periodic_cas_commit_can_persist_without_reentering_a_plain_lock(tmp_path, failed):
    owner = helpers.ManagerTests()._minimal_manager(
        tmp_path, tier3_enabled=True,
        tier3_overrides={"pool_eviction_failure_threshold": 1},
    )
    previous = [Node(name=f"old-{i}", protocol="ss", address=f"node-{i}.invalid",
                     port=443, method="aes-128-gcm", password="test-secret",
                     tier="tier3", status=NodeStatus.ALIVE) for i in range(3)]
    owner.runtime_nodes["tier3"] = previous
    result, errors = [], []

    async def check(nodes, **_kwargs):
        tested = [node.clone() for node in nodes]
        if failed:
            tested[0].status = NodeStatus.SUSPICIOUS
            tested[0].suspicious_reason = "mihomo_validation_failed: timed out"
        return tested

    def recheck():
        try:
            result.extend(owner._recheck_existing_tier3_pool(previous))
        except BaseException:
            errors.append(traceback.format_exc())

    with patch.object(manager_module, "test_nodes", check):
        worker = threading.Thread(target=recheck, daemon=True)
        worker.start()
        worker.join(2)
        # A regression fails in bounded time rather than hanging the full suite.
        assert not worker.is_alive(), "periodic CAS snapshot publication self-deadlocked"
    try:
        assert not errors
        assert len(result) == 3 - int(failed)
        assert owner._tier3_active_stream_lock.acquire(blocking=False)
        owner._tier3_active_stream_lock.release()
    finally:
        owner.close(timeout_seconds=3)
