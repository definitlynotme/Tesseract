from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import manager as manager_module
from alerts import AlertManager
from manager import ProxyTierManager
from models import Node, NodeStatus


def _auto_node(name: str, *, status: NodeStatus = NodeStatus.ALIVE) -> Node:
    return Node(
        name=name,
        protocol="ss",
        uri=f"ss://{name}",
        address=f"{name}.example.com",
        port=8388,
        tier="tier3",
        method="aes-128-gcm",
        password=f"password-{name}",
        status=status,
        latency_ms=30.0 if status in {NodeStatus.ALIVE, NodeStatus.SLOW} else None,
        last_checked_at="2026-08-23T00:00:00+00:00",
    )


def _user_node(name: str, *, status: NodeStatus = NodeStatus.ALIVE) -> Node:
    node = _auto_node(name, status=status)
    node.extra["tier3_origin"] = "tier3a"
    return node


def _build_manager(
    root: Path,
    *,
    tier3_overrides: dict | None = None,
) -> ProxyTierManager:
    root.mkdir(parents=True, exist_ok=True)
    nodes_file = root / "nodes.json"
    nodes_file.write_text(
        '{"generated_at":"","metadata":{"tier":"tier3","source_count":0},"nodes":[]}',
        encoding="utf-8",
    )
    tier2_cache_file = root / "tier2-cache.json"
    tier2_cache_file.write_text(
        '{"generated_at":"","metadata":{"tier":"tier2","source_urls":[]},"nodes":[]}',
        encoding="utf-8",
    )
    sources_file = root / "sources.yaml"
    sources_file.write_text(
        "github_repos: []\nsubscription_urls: []\nlocal_files: []\n",
        encoding="utf-8",
    )
    tier3 = {
        "enabled": True,
        "check_interval": 120,
        "auto_collect_interval": 3600,
        "max_pool_size": 100,
    }
    tier3.update(tier3_overrides or {})
    config = {
        "paths": {
            "sources_file": str(sources_file),
            "tier3_nodes_file": str(nodes_file),
            "tier2_cache_file": str(tier2_cache_file),
            "exit_cache_file": str(root / "exit-cache.json"),
            "tier_observation_cache_file": str(root / "observations.json"),
        },
        "tiers": {
            "tier1": {"nodes": [], "check_interval": 30},
            "tier2": {
                "subscription_urls": [],
                "cache_file": str(tier2_cache_file),
                "check_interval": 60,
            },
            "tier3": tier3,
            "exit": {
                "cache_file": str(root / "exit-cache.json"),
                "check_interval": 60,
            },
        },
        "testing": {
            "tcp_timeout": 1,
            "concurrency": 2,
            "slow_threshold_ms": 1000,
            "validation_mode": "tcp_only",
        },
    }
    manager = ProxyTierManager(
        config=config,
        logger=logging.getLogger("test-manager-periodic-runtime-cas"),
        alerts=AlertManager(
            {
                "desktop": {"enabled": False},
                "telegram": {"enabled": False},
                "sound": {"enabled": False},
            },
            logging.getLogger("test-manager-periodic-runtime-cas-alerts"),
        ),
    )
    manager.runtime_generation = 7
    manager.tier3_startup_gate_required = False
    return manager


@pytest.fixture
def manager_factory(tmp_path: Path):
    managers: list[ProxyTierManager] = []

    def factory(*, tier3_overrides: dict | None = None) -> ProxyTierManager:
        manager = _build_manager(
            tmp_path / f"manager-{len(managers)}",
            tier3_overrides=tier3_overrides,
        )
        managers.append(manager)
        return manager

    yield factory

    for manager in managers:
        manager.close()


def _confirm_runtime_failure(
    manager: ProxyTierManager,
    node: Node,
    *,
    incident_id: str,
) -> dict:
    transition = manager.apply_runtime_health_observation(
        {
            "tier": "tier3",
            "pool_kind": "tier3b",
            "identity": manager._node_identity(node),
            "proxy_name": node.name,
            "source": "mihomo_stdout",
            "verdict": "confirmed",
            "failure_domain": "node",
            "incident_id": incident_id,
            "failure_epoch": incident_id,
            "quarantine_token": f"token-{incident_id}",
            "runtime_generation": manager.runtime_generation,
            "core_generation": 3,
            "semantic_digest": "digest-a",
            "observed_monotonic": time.monotonic(),
        }
    )
    assert transition["action"] == "quarantined"
    return manager.runtime_health_snapshot()["entries"][0]


def _record_commit_side_effects(manager: ProxyTierManager) -> list[str]:
    calls: list[str] = []
    manager._save_tier3_snapshot = lambda *_args, **_kwargs: calls.append("save")
    manager._observe_nodes = lambda *_args, **_kwargs: calls.append("observe")
    manager._notify_tier3_pool_committed = (
        lambda *_args, **_kwargs: calls.append("notify")
    )
    return calls


def _run_blocked_periodic_recheck(
    manager: ProxyTierManager,
    *,
    tested_status: NodeStatus,
) -> tuple[
    threading.Event,
    threading.Event,
    threading.Thread,
    list[list[Node]],
    list[BaseException],
]:
    started = threading.Event()
    release = threading.Event()
    results: list[list[Node]] = []
    errors: list[BaseException] = []

    def fake_test(nodes: list[Node], **_kwargs):
        started.set()
        assert release.wait(5.0), "test barrier was not released"
        tested = []
        for node in nodes:
            updated = node.clone()
            updated.status = tested_status
            updated.latency_ms = 20.0 if tested_status is NodeStatus.ALIVE else None
            updated.suspicious_reason = (
                None
                if tested_status is NodeStatus.ALIVE
                else "mihomo_validation_failed: timed out"
            )
            tested.append(updated)
        return tested, {}

    manager._test_tier3_candidates_until_usable = fake_test

    def run() -> None:
        try:
            results.append(
                manager._recheck_existing_tier3_pool(
                    list(manager.runtime_nodes["tier3"])
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted by caller
            errors.append(exc)

    worker = threading.Thread(target=run, name="periodic-runtime-cas-test")
    worker.start()
    return started, release, worker, results, errors


def _finish_blocked_recheck(
    release: threading.Event,
    worker: threading.Thread,
    results: list[list[Node]],
    errors: list[BaseException],
) -> list[Node]:
    release.set()
    worker.join(5.0)
    assert not worker.is_alive()
    assert errors == []
    assert len(results) == 1
    return results[0]


def test_active_incident_is_removed_from_retention_and_delegated_exactly(
    manager_factory: Callable[..., ProxyTierManager],
) -> None:
    manager = manager_factory()
    bad = _auto_node("active-bad")
    ordinary = _auto_node("ordinary")
    manager.runtime_nodes["tier3"] = [bad, ordinary]
    entry = _confirm_runtime_failure(manager, bad, incident_id="active-at-start")
    delegated: list[dict] = []
    tested_names: list[str] = []

    def fake_enqueue(node_ref, **kwargs):
        delegated.append({"node_ref": dict(node_ref), **kwargs})
        return {"accepted": True}

    def fake_test(nodes: list[Node], **_kwargs):
        tested_names.extend(node.name for node in nodes)
        failed = nodes[0].clone()
        failed.status = NodeStatus.SUSPICIOUS
        failed.latency_ms = None
        failed.suspicious_reason = "mihomo_validation_failed: timed out"
        return [failed], {}

    manager.enqueue_runtime_leaf_recheck = fake_enqueue
    manager._test_tier3_candidates_until_usable = fake_test
    _record_commit_side_effects(manager)

    result = manager._recheck_existing_tier3_pool(
        list(manager.runtime_nodes["tier3"])
    )

    by_name = {node.name: node for node in result}
    assert tested_names == ["ordinary"]
    assert len(delegated) == 1
    assert delegated[0]["node_ref"]["incident_id"] == entry["incident_id"]
    assert delegated[0]["source"] == "tier3_periodic_recheck"
    assert by_name["active-bad"].status is NodeStatus.ALIVE
    assert "tier3_consecutive_failures" not in by_name["active-bad"].extra
    assert by_name["ordinary"].status is NodeStatus.SUSPICIOUS
    assert by_name["ordinary"].extra["tier3_consecutive_failures"] == 1
    assert manager.runtime_health_snapshot()["entries"][0]["incident_id"] == (
        "active-at-start"
    )


def test_incident_confirmed_mid_probe_rejects_periodic_commit_without_side_effects(
    manager_factory: Callable[..., ProxyTierManager],
) -> None:
    manager = manager_factory()
    node = _auto_node("mid-probe")
    manager.runtime_nodes["tier3"] = [node]
    side_effects = _record_commit_side_effects(manager)
    initial_clock = manager.last_check_monotonic["tier3"]
    started, release, worker, results, errors = _run_blocked_periodic_recheck(
        manager,
        tested_status=NodeStatus.SUSPICIOUS,
    )
    assert started.wait(5.0)

    _confirm_runtime_failure(manager, node, incident_id="mid-probe")
    result = _finish_blocked_recheck(release, worker, results, errors)

    assert result[0].status is NodeStatus.ALIVE
    assert manager.runtime_nodes["tier3"][0].status is NodeStatus.ALIVE
    assert "tier3_consecutive_failures" not in manager.runtime_nodes["tier3"][0].extra
    assert manager.last_check_monotonic["tier3"] == initial_clock
    assert side_effects == []


def test_runtime_incident_aba_is_rejected_by_monotonic_health_revision(
    manager_factory: Callable[..., ProxyTierManager],
) -> None:
    manager = manager_factory()
    node = _auto_node("aba")
    manager.runtime_nodes["tier3"] = [node]
    side_effects = _record_commit_side_effects(manager)
    initial_clock = manager.last_check_monotonic["tier3"]
    started, release, worker, results, errors = _run_blocked_periodic_recheck(
        manager,
        tested_status=NodeStatus.SUSPICIOUS,
    )
    assert started.wait(5.0)

    entry = _confirm_runtime_failure(manager, node, incident_id="aba")
    recovered = manager.apply_runtime_health_observation(
        {
            **entry,
            "source": "runtime_leaf_recheck",
            "verdict": "pass",
            "authoritative": True,
            "tested_status": NodeStatus.ALIVE.value,
            "recheck_started_monotonic": time.monotonic() + 1.0,
            "observed_monotonic": time.monotonic() + 1.0,
        }
    )
    assert recovered["action"] == "recovered"
    assert manager.runtime_health_snapshot()["entries"] == []
    assert manager._tier3_runtime_health_revision >= 2
    result = _finish_blocked_recheck(release, worker, results, errors)

    assert result[0].status is NodeStatus.ALIVE
    assert manager.runtime_nodes["tier3"][0].status is NodeStatus.ALIVE
    assert "tier3_consecutive_failures" not in manager.runtime_nodes["tier3"][0].extra
    assert manager.last_check_monotonic["tier3"] == initial_clock
    assert side_effects == []


def test_targeted_eviction_mid_probe_cannot_be_reinserted_by_periodic_batch(
    manager_factory: Callable[..., ProxyTierManager],
) -> None:
    manager = manager_factory(
        tier3_overrides={"pool_eviction_failure_threshold": 1}
    )
    node = _auto_node("evicted")
    manager.runtime_nodes["tier3"] = [node]
    side_effects = _record_commit_side_effects(manager)
    initial_clock = manager.last_check_monotonic["tier3"]
    started, release, worker, results, errors = _run_blocked_periodic_recheck(
        manager,
        tested_status=NodeStatus.ALIVE,
    )
    assert started.wait(5.0)

    manager.mark_runtime_audit_node_failure(
        "tier3",
        manager._node_identity(node),
        reason="runtime targeted check failed",
        marker="targeted-eviction",
        pool_kind="tier3b",
        proxy_name=node.name,
    )
    assert manager.runtime_nodes["tier3"] == []
    side_effects.clear()
    result = _finish_blocked_recheck(release, worker, results, errors)

    assert result == []
    assert manager.runtime_nodes["tier3"] == []
    assert manager.last_check_monotonic["tier3"] == initial_clock
    assert side_effects == []


def test_tier3a_health_change_mid_probe_is_not_overwritten_by_auto_batch(
    manager_factory: Callable[..., ProxyTierManager],
) -> None:
    manager = manager_factory()
    auto = _auto_node("auto")
    user = _user_node("user")
    manager.runtime_nodes["tier3"] = [auto, user]
    side_effects = _record_commit_side_effects(manager)
    initial_clock = manager.last_check_monotonic["tier3"]
    started, release, worker, results, errors = _run_blocked_periodic_recheck(
        manager,
        tested_status=NodeStatus.ALIVE,
    )
    assert started.wait(5.0)

    changed_user = user.clone()
    changed_user.status = NodeStatus.SUSPICIOUS
    changed_user.latency_ms = None
    changed_user.suspicious_reason = "tier3a user-pool health failed"
    with manager.lock:
        assert manager._replace_tier3_pool_locked(
            [auto.clone(), changed_user],
            actor="user_pool_health",
            reason="Tier3A process health result",
        )
    result = _finish_blocked_recheck(release, worker, results, errors)

    by_name = {node.name: node for node in result}
    assert by_name["user"].status is NodeStatus.SUSPICIOUS
    assert manager.tier3_user_pool_health_revision == 1
    assert manager.tier3_pool_revision == 0
    assert manager.last_check_monotonic["tier3"] == initial_clock
    assert side_effects == []


def test_targeted_runtime_pass_snapshot_failure_rolls_back_before_recovery(
    manager_factory: Callable[..., ProxyTierManager],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = manager_factory()
    node = _auto_node("pass-save-failure")
    node.extra.update(
        {
            "tier3_consecutive_failures": 1,
            "tier3_retained_after_failure": True,
        }
    )
    manager.runtime_nodes["tier3"] = [node]
    entry = _confirm_runtime_failure(
        manager,
        node,
        incident_id="pass-save-failure",
    )
    request = {
        **entry,
        "recheck_started_monotonic": time.monotonic() + 1.0,
    }
    identity = manager._node_identity(node)
    request_key = manager._runtime_leaf_recheck_key(request)
    manager._runtime_leaf_recheck_inflight.add(request_key)
    manager.tier3_targeted_recheck_queue = [
        {
            "identity": identity,
            "reason": "runtime recovery",
            "source": "runtime",
            "runtime_recheck": request,
        }
    ]
    tested = node.clone()
    tested.status = NodeStatus.ALIVE
    tested.latency_ms = 17.0
    tested.last_checked_at = "2026-08-23T00:01:00+00:00"
    tested.suspicious_reason = None

    async def fake_test_nodes(_nodes, **_kwargs):
        return [tested]

    def fail_snapshot(*_args, **_kwargs):
        raise OSError("snapshot write failed")

    monkeypatch.setattr(manager_module, "test_nodes", fake_test_nodes)
    manager._save_tier3_snapshot = fail_snapshot
    callbacks: list[dict] = []
    recovered_publications: list[dict] = []
    manager.on_runtime_leaf_recheck_result = callbacks.append
    manager._notify_tier3_pool_committed = (
        lambda *_args, **kwargs: recovered_publications.append(dict(kwargs))
    )
    manager._observe_nodes = lambda *_args, **_kwargs: None
    before_nodes = [item.to_dict() for item in manager.runtime_nodes["tier3"]]
    before_pool_revision = manager.tier3_pool_revision
    before_user_revision = manager.tier3_user_pool_health_revision
    before_mutations = [dict(item) for item in manager.tier3_pool_mutation_ledger]

    result = manager._run_tier3_targeted_rechecks()

    assert result["healthy"] == 0
    assert result["skipped"] == 1
    assert result["errors"]
    assert [item.to_dict() for item in manager.runtime_nodes["tier3"]] == before_nodes
    assert manager.tier3_pool_revision == before_pool_revision
    assert manager.tier3_user_pool_health_revision == before_user_revision
    assert manager.tier3_pool_mutation_ledger == before_mutations
    health = manager.runtime_health_snapshot()
    assert len(health["entries"]) == 1
    assert health["entries"][0]["incident_id"] == "pass-save-failure"
    assert health["entries"][0]["pending_recheck"] is True
    assert manager.effective_node_usable("tier3", manager.runtime_nodes["tier3"][0]) is False
    assert request_key not in manager._runtime_leaf_recheck_inflight
    assert not any(
        str((payload.get("transition") or {}).get("action") or "") == "recovered"
        for payload in callbacks
    )
    assert recovered_publications == []
