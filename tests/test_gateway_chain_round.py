"""gateway_chain_round: one round in which every chain through the Gateway
failed is held; the layer's next round confirms it."""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import manager as manager_module
import tester as tester_module
from alerts import AlertManager
from gateway_chain_round import (
    CHAIN_FAILURE_PREFIX,
    chain_check_failed,
    chain_helper_not_ready,
    every_chain_failed,
    judge_chain_round,
)
from gateway_check import CHAIN_PROBE_INTERVAL_SECONDS, ChainVerdict
from gateway_selection import gateway_identity
from manager import ProxyTierManager
from models import Node, NodeStatus
from runtime_health_check import health_check_verdict
from tester import GATEWAY_CHAIN_HELPER_NOT_READY_REASON

TIMED_OUT = f"{CHAIN_FAILURE_PREFIX} isp: timed out"


def _node(name: str, *, tier: str = "tier2", status: NodeStatus = NodeStatus.ALIVE) -> Node:
    return Node(
        name=name,
        protocol="ss",
        address=f"{name}.example",
        port=8388,
        tier=tier,
        method="aes-256-gcm",
        password=f"{name}-password",
        status=status,
        latency_ms=180.0 if status in {NodeStatus.ALIVE, NodeStatus.SLOW} else None,
        suspicious_reason=None if status in {NodeStatus.ALIVE, NodeStatus.SLOW} else "earlier failure",
        last_checked_at="2026-11-26T08:00:00+00:00",
    )


def _passed(node: Node) -> Node:
    updated = node.clone()
    updated.status = NodeStatus.ALIVE
    updated.latency_ms = 420.0
    updated.suspicious_reason = None
    updated.extra["tested_via_gateway"] = "isp"
    updated.last_checked_at = "2026-11-26T08:01:00+00:00"
    return updated


def _failed(node: Node, reason: str = TIMED_OUT) -> Node:
    updated = node.clone()
    updated.status = NodeStatus.SUSPICIOUS
    updated.latency_ms = None
    updated.suspicious_reason = reason
    updated.last_checked_at = "2026-11-26T08:01:00+00:00"
    return updated


def _deferred(node: Node) -> Node:
    updated = node.clone()
    updated.status = NodeStatus.UNKNOWN
    updated.suspicious_reason = "validation_deferred: tcp_wave_cancelled"
    return updated


def _key(node: Node) -> tuple[str, str]:
    return node.address, str(node.name)


def _names(nodes: list[Node]) -> list[str]:
    return [str(node.name) for node in nodes]


# -- The round rules ---------------------------------------------------------


def test_a_round_with_one_chain_pass_is_committed_as_it_is() -> None:
    previous = [_node("a"), _node("b"), _node("c")]
    tested = [_passed(previous[0]), _failed(previous[1]), _failed(previous[2])]

    chain_round = judge_chain_round(previous, tested, held_before=False, key=_key)

    assert _names(chain_round.commit) == ["a", "b", "c"]
    assert chain_round.held == []
    assert chain_round.every_chain_failed is False


def test_the_first_round_in_which_every_chain_check_times_out_is_held() -> None:
    previous = [_node("a"), _node("b", status=NodeStatus.SLOW), _node("c")]
    tested = [_failed(node) for node in previous]

    chain_round = judge_chain_round(previous, tested, held_before=False, key=_key)

    assert chain_round.every_chain_failed is True
    assert _names(chain_round.held) == ["a", "b", "c"]
    assert chain_round.commit == []


def test_the_next_round_of_a_held_layer_is_committed_when_it_fails_again() -> None:
    previous = [_node("a"), _node("b")]
    tested = [_failed(node) for node in previous]

    chain_round = judge_chain_round(previous, tested, held_before=True, key=_key)

    assert chain_round.every_chain_failed is True
    assert chain_round.held == []
    assert _names(chain_round.commit) == ["a", "b"]


def test_only_nodes_that_were_usable_are_held() -> None:
    previous = [_node("a"), _node("b", status=NodeStatus.SUSPICIOUS), _node("c", status=NodeStatus.DEAD)]
    tested = [_failed(node) for node in previous]

    chain_round = judge_chain_round(previous, tested, held_before=False, key=_key)

    assert _names(chain_round.held) == ["a"]
    assert _names(chain_round.commit) == ["b", "c"]


def test_a_layer_that_was_already_down_is_committed() -> None:
    previous = [_node("a", status=NodeStatus.SUSPICIOUS), _node("b", status=NodeStatus.UNKNOWN)]
    tested = [_failed(node) for node in previous]

    chain_round = judge_chain_round(previous, tested, held_before=False, key=_key)

    assert chain_round.held == []
    assert _names(chain_round.commit) == ["a", "b"]


def test_pending_results_neither_pass_nor_fail_a_round() -> None:
    previous = [_node("a"), _node("b"), _node("c")]
    tested = [_deferred(previous[0]), _failed(previous[1]), _failed(previous[2])]

    chain_round = judge_chain_round(previous, tested, held_before=False, key=_key)

    assert _names(chain_round.held) == ["b", "c"]
    assert _names(chain_round.commit) == ["a"]
    assert every_chain_failed([_deferred(node) for node in previous]) is False


def test_a_failure_that_is_not_a_chain_failure_is_committed() -> None:
    previous = [_node("a"), _node("b")]
    tested = [_failed(previous[0]), _failed(previous[1], "mihomo binary not found: bin/mihomo.exe")]

    chain_round = judge_chain_round(previous, tested, held_before=False, key=_key)

    assert chain_round.held == []
    assert _names(chain_round.commit) == ["a", "b"]


def _not_ready(node: Node) -> Node:
    updated = node.clone()
    updated.status = NodeStatus.SUSPICIOUS
    updated.latency_ms = None
    updated.suspicious_reason = f"{GATEWAY_CHAIN_HELPER_NOT_READY_REASON}: isp: helper did not listen within 5s"
    return updated


def test_nodes_whose_helper_never_listened_are_left_out_and_decide_nothing() -> None:
    previous = [_node("a"), _node("b")]

    chain_round = judge_chain_round(previous, [_not_ready(node) for node in previous], held_before=True, key=_key)

    assert chain_round.commit == []
    assert _names(chain_round.not_ready) == ["a", "b"]
    assert chain_round.decided is False
    assert chain_round.every_chain_failed is False


def test_a_round_of_chain_failures_and_helpers_that_never_listened_is_held() -> None:
    previous = [_node("a"), _node("b"), _node("c")]
    tested = [_not_ready(previous[0]), _failed(previous[1]), _failed(previous[2])]

    chain_round = judge_chain_round(previous, tested, held_before=False, key=_key)

    assert _names(chain_round.held) == ["b", "c"]
    assert _names(chain_round.not_ready) == ["a"]
    assert chain_round.commit == []
    assert chain_round.decided is True


def test_a_pass_beside_helpers_that_never_listened_is_committed() -> None:
    previous = [_node("a"), _node("b")]
    tested = [_not_ready(previous[0]), _passed(previous[1])]

    chain_round = judge_chain_round(previous, tested, held_before=False, key=_key)

    assert _names(chain_round.commit) == ["b"]
    assert _names(chain_round.not_ready) == ["a"]
    assert chain_round.decided is True


# -- tester._gateway_chain_validate: how a helper start ends -----------------


class _HelperProcess:
    """subprocess.Popen stand-in: running (exit_code None) or exited."""

    pid = 4242

    def __init__(self, exit_code: int | None) -> None:
        self.exit_code = exit_code

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.exit_code = 1

    def wait(self, timeout: float | None = None) -> int:
        return 1

    def kill(self) -> None:
        self.exit_code = 1


class _Connected:
    def __enter__(self) -> "_Connected":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False


class _Response(_Connected):
    status = 204

    def getcode(self) -> int:
        return 204

    def read(self, _size: int = -1) -> bytes:
        return b""


class _Opener:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def open(self, request: Any, timeout: float | None = None) -> _Response:
        self.urls.append(request.full_url)
        return _Response()


def _validate_with_helper(*, exit_code: int | None, up_after_the_deadline: bool) -> tuple[Node, _Opener]:
    """One chain validation whose helper wait passed its deadline ("timed out",
    the field report's line); the helper then exited, stayed silent, or is up."""

    opener = _Opener()

    def deadline_passed(*_args: Any, **_kwargs: Any) -> None:
        raise TimeoutError("timed out")

    def look_once_more(*_args: Any, **_kwargs: Any) -> _Connected:
        if up_after_the_deadline:
            return _Connected()
        raise TimeoutError("timed out")

    with tempfile.TemporaryDirectory() as temp_dir:
        binary = Path(temp_dir) / "mihomo.exe"
        binary.write_text("fake", encoding="utf-8")
        gateway = Node(
            name="isp",
            protocol="socks5",
            address="isp.example.net",
            port=1080,
            tier="exit",
            password="isp-pass",
            extra={"username": "isp-user", "exit_node": True},
            status=NodeStatus.ALIVE,
        )
        with (
            patch.object(tester_module.subprocess, "Popen", lambda *_args, **_kwargs: _HelperProcess(exit_code)),
            patch.object(tester_module, "_wait_for_tcp_port", deadline_passed),
            patch.object(tester_module.socket, "create_connection", look_once_more),
            patch.object(tester_module.urllib.request, "build_opener", lambda *_args, **_kwargs: opener),
        ):
            result = tester_module._gateway_chain_validate(
                _node("jms"),
                [gateway],
                {
                    "mihomo_binary_path": str(binary),
                    "mihomo_helper": {"port_attempts": 1, "startup_timeout": 5},
                    "validation_temp_dir": str(Path(temp_dir) / "validation"),
                },
                1000,
                logging.getLogger("test-gateway-chain-round-helper-start"),
            )
    return result, opener


def test_a_helper_that_never_listened_gives_no_verdict_on_the_node() -> None:
    result, opener = _validate_with_helper(exit_code=None, up_after_the_deadline=False)

    assert result.suspicious_reason == (
        f"{GATEWAY_CHAIN_HELPER_NOT_READY_REASON}: isp: helper did not listen within 5s"
    )
    assert health_check_verdict(result) == "pending"
    assert chain_helper_not_ready(result)
    assert not chain_check_failed(result)
    assert opener.urls == []


def test_a_helper_that_exited_before_listening_is_a_chain_failure() -> None:
    result, opener = _validate_with_helper(exit_code=1, up_after_the_deadline=False)

    assert result.status is NodeStatus.SUSPICIOUS
    assert result.suspicious_reason == (
        f"{CHAIN_FAILURE_PREFIX} isp: chain helper exited before listening (exit code 1)"
    )
    assert chain_check_failed(result)
    assert opener.urls == []


def test_a_helper_that_is_up_once_a_stall_passed_its_deadline_is_still_probed() -> None:
    result, opener = _validate_with_helper(exit_code=None, up_after_the_deadline=True)

    assert result.status is NodeStatus.ALIVE
    assert result.extra["tested_via_gateway"] == "isp"
    assert len(opener.urls) == 1


# -- In the Manager: Sentinel through a selected Gateway ---------------------


def _manager(temp_path: Path) -> ProxyTierManager:
    nodes_file = temp_path / "nodes.json"
    nodes_file.write_text('{"generated_at":"","metadata":{"tier":"tier3","source_count":0},"nodes":[]}', encoding="utf-8")
    tier2_cache_file = temp_path / "tier2_cache.json"
    tier2_cache_file.write_text('{"generated_at":"","metadata":{"tier":"tier2","source_urls":[]},"nodes":[]}', encoding="utf-8")
    sources_file = temp_path / "sources.yaml"
    sources_file.write_text("github_repos: []\nsubscription_urls: []\nlocal_files: []\n", encoding="utf-8")
    config = {
        "paths": {
            "sources_file": str(sources_file),
            "tier3_nodes_file": str(nodes_file),
            "tier2_cache_file": str(tier2_cache_file),
            "exit_cache_file": str(temp_path / "exit_cache.json"),
            "tier_observation_cache_file": str(temp_path / "tier_observation_cache.json"),
        },
        "tiers": {
            "tier1": {"nodes": [], "check_interval": 60},
            "tier2": {"nodes": ["configured"], "cache_file": str(tier2_cache_file), "check_interval": 60},
            "tier3": {"enabled": False, "check_interval": 120, "auto_collect_interval": 3600, "max_pool_size": 100},
            "exit": {
                "nodes": ["configured"],
                "selected": "isp",
                "cache_file": str(temp_path / "exit_cache.json"),
                "check_interval": 60,
            },
        },
        "testing": {"tcp_timeout": 1, "concurrency": 5, "slow_threshold_ms": 1000, "validation_mode": "tcp_only"},
    }
    manager = ProxyTierManager(
        config=config,
        logger=logging.getLogger("test-gateway-chain-round"),
        alerts=AlertManager(
            {"desktop": {"enabled": False}, "telegram": {"enabled": False}, "sound": {"enabled": False}},
            logging.getLogger("test-gateway-chain-round-alerts"),
        ),
    )
    manager.runtime_nodes["tier2"] = [_node("jms-1"), _node("jms-2"), _node("jms-3")]
    manager.runtime_nodes["exit"] = [
        Node(
            name="isp",
            protocol="socks5",
            address="isp.example.net",
            port=1080,
            tier="exit",
            password="isp-pass",
            extra={"username": "isp-user", "exit_node": True},
            status=NodeStatus.ALIVE,
            latency_ms=95.0,
        )
    ]
    return manager


def _chain_round(*passing: str, calls: list[list[Node]] | None = None, not_ready: tuple[str, ...] = ()):
    """test_nodes for a layer round through the Gateway: only ``passing`` pass;
    the helpers of ``not_ready`` never listened ("*" for every node)."""

    def result(node: Node) -> Node:
        if "*" in not_ready or node.name in not_ready:
            return _not_ready(node)
        return _passed(node) if node.name in passing else _failed(node)

    async def fake_test_nodes(nodes, **kwargs):
        assert [node.name for node in kwargs.get("gateway_nodes") or []] == ["isp"]
        if calls is not None:
            calls.append(list(nodes))
        return [result(node) for node in nodes]

    return fake_test_nodes


def _record_alerts(manager: ProxyTierManager) -> list[Any]:
    sent: list[Any] = []
    manager.alerts.send_event = lambda event: sent.append(event) or True
    return sent


def _statuses(manager: ProxyTierManager, tier_id: str = "tier2") -> list[NodeStatus]:
    return [node.status for node in manager.runtime_effective_nodes(tier_id)]


def test_one_round_of_chain_timeouts_keeps_sentinel_up_without_an_alert() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = _manager(Path(temp_dir))
        assert manager.evaluate_state()["mode"] == "tier2_active"
        sent = _record_alerts(manager)

        with patch.object(manager_module, "test_nodes", _chain_round()):
            manager.check_tier2()
        state = manager.evaluate_state()

        assert state["mode"] == "tier2_active"
        assert state["tier2_alive"] == 3
        assert _statuses(manager) == [NodeStatus.ALIVE] * 3
        assert [node.last_checked_at for node in manager.runtime_nodes["tier2"]] == [
            "2026-11-26T08:00:00+00:00"
        ] * 3
        assert [event.title for event in sent] == []
        assert manager._gateway_chain_round_held_tiers() == {"tier2"}


def test_a_genuinely_dead_sentinel_layer_still_goes_offline_on_the_next_round() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = _manager(Path(temp_dir))
        manager.evaluate_state()
        sent = _record_alerts(manager)

        with patch.object(manager_module, "test_nodes", _chain_round()):
            manager.check_tier2()
            assert manager.evaluate_state()["mode"] == "tier2_active"
            manager.check_tier2()
        state = manager.evaluate_state()

        assert state["mode"] == "offline"
        assert _statuses(manager) == [NodeStatus.SUSPICIOUS] * 3
        assert all(
            str(node.suspicious_reason).startswith(CHAIN_FAILURE_PREFIX)
            for node in manager.runtime_nodes["tier2"]
        )
        assert "No nodes available" in [event.title for event in sent]
        assert manager._gateway_chain_round_held_tiers() == set()
        # Committed: the layer is marked checked like any other round.
        assert not manager._is_due("tier2", time.monotonic(), 60)


def test_a_passing_round_after_a_held_one_clears_the_hold() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = _manager(Path(temp_dir))
        manager.evaluate_state()
        sent = _record_alerts(manager)

        with patch.object(manager_module, "test_nodes", _chain_round()):
            manager.check_tier2()
        with patch.object(manager_module, "test_nodes", _chain_round("jms-1", "jms-2", "jms-3")):
            manager.check_tier2()
        state = manager.evaluate_state()

        assert state["mode"] == "tier2_active"
        assert [node.latency_ms for node in manager.runtime_nodes["tier2"]] == [420.0] * 3
        assert manager._gateway_chain_round_held_tiers() == set()
        assert [event.title for event in sent] == []

        # The hold is over: a later all-failing round is held afresh, not
        # taken as the confirmation of the first one.
        with patch.object(manager_module, "test_nodes", _chain_round()):
            manager.check_tier2()
        assert _statuses(manager) == [NodeStatus.ALIVE] * 3


def test_rounds_whose_helpers_never_listen_keep_sentinel_up_however_many_there_are() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = _manager(Path(temp_dir))
        manager.evaluate_state()
        sent = _record_alerts(manager)

        with patch.object(manager_module, "test_nodes", _chain_round(not_ready=("*",))):
            for _round in range(3):
                manager.check_tier2()
                assert manager.evaluate_state()["mode"] == "tier2_active"

        assert _statuses(manager) == [NodeStatus.ALIVE] * 3
        assert [event.title for event in sent] == []
        # Nothing was held, and the next try follows the schedule, not the next tick.
        assert manager._gateway_chain_round_held_tiers() == set()
        assert not manager._is_due("tier2", time.monotonic(), 60)


def test_a_round_whose_helpers_never_listened_keeps_the_hold_for_the_next_real_round() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = _manager(Path(temp_dir))
        manager.evaluate_state()

        with patch.object(manager_module, "test_nodes", _chain_round()):
            manager.check_tier2()
        with patch.object(manager_module, "test_nodes", _chain_round(not_ready=("*",))):
            manager.check_tier2()
        assert manager._gateway_chain_round_held_tiers() == {"tier2"}
        assert _statuses(manager) == [NodeStatus.ALIVE] * 3
        with patch.object(manager_module, "test_nodes", _chain_round()):
            manager.check_tier2()

        assert _statuses(manager) == [NodeStatus.SUSPICIOUS] * 3
        assert manager.evaluate_state()["mode"] == "offline"


def test_a_round_with_one_pass_commits_every_other_failure_at_once() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = _manager(Path(temp_dir))

        with patch.object(manager_module, "test_nodes", _chain_round("jms-2")):
            manager.check_tier2()

        statuses = {node.name: node.status for node in manager.runtime_nodes["tier2"]}
        assert statuses == {
            "jms-1": NodeStatus.SUSPICIOUS,
            "jms-2": NodeStatus.ALIVE,
            "jms-3": NodeStatus.SUSPICIOUS,
        }
        assert manager._gateway_chain_round_held_tiers() == set()


def test_without_a_selected_gateway_a_failing_round_is_committed() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = _manager(Path(temp_dir))
        manager.config["tiers"]["exit"].pop("selected")

        async def direct_failures(nodes, **kwargs):
            assert not kwargs.get("gateway_nodes")
            return [_failed(node, "tcp connection timed out") for node in nodes]

        with patch.object(manager_module, "test_nodes", direct_failures):
            manager.check_tier2()

        assert _statuses(manager) == [NodeStatus.SUSPICIOUS] * 3
        assert manager.evaluate_state()["mode"] == "offline"


def test_a_held_round_is_confirmed_one_urgent_interval_later() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = _manager(Path(temp_dir))
        # Vanguard carries traffic, so Sentinel has no urgent recheck of its own.
        manager.runtime_nodes["tier1"] = [_node("vanguard", tier="tier1")]
        calls: list[list[Node]] = []

        with patch.object(manager_module, "test_nodes", _chain_round(calls=calls)):
            manager.check_tier2()
            held_at = time.monotonic()
            # The regular schedule does not rerun the held layer on the next tick...
            assert not manager._is_due("tier2", held_at, 60)
            manager._run_due_urgent_checks(now=held_at + 1)
            assert len(calls) == 1
            # ...the urgent cycle confirms it once the interval has passed.
            manager._run_due_urgent_checks(now=held_at + 6)

        assert len(calls) == 2
        assert _statuses(manager) == [NodeStatus.SUSPICIOUS] * 3


def test_vanguard_through_the_gateway_is_held_and_confirmed_the_same_way() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = _manager(Path(temp_dir))
        manager.runtime_nodes["tier1"] = [_node("vanguard", tier="tier1")]
        manager.evaluate_state()
        sent = _record_alerts(manager)
        calls: list[list[Node]] = []

        with patch.object(manager_module, "test_nodes", _chain_round(calls=calls)):
            manager.check_tier1()
            assert _statuses(manager, "tier1") == [NodeStatus.ALIVE]
            assert manager.evaluate_state()["mode"] == "tier1_healthy"
            assert [event.title for event in sent] == []
            manager._run_due_urgent_checks(now=time.monotonic() + 6)

        # Vanguard's confirming round, then Sentinel's own urgent recheck
        # now that Vanguard is down (held in turn: its nodes were usable).
        assert [[node.tier for node in call] for call in calls] == [["tier1"], ["tier1"], ["tier2"] * 3]
        assert _statuses(manager, "tier1") == [NodeStatus.SUSPICIOUS]
        assert manager.evaluate_state()["mode"] == "tier2_active"


# -- Against the Gateway's own health (gateway_check.py) ---------------------


class _ChainProber:
    def __init__(self) -> None:
        self.calls: list[tuple[Node, list[Node]]] = []

    def __call__(self, node: Node, upstream: list[Node]) -> dict[str, Any]:
        self.calls.append((node, upstream))
        return {"state": "failed", "latency_ms": None, "reason": "timeout"}


def _direct_failure(node: Node) -> Node:
    updated = node.clone()
    updated.status = NodeStatus.DEAD
    updated.latency_ms = None
    updated.suspicious_reason = "tcp connection timed out"
    return updated


def _remember_old_pass(manager: ProxyTierManager) -> None:
    (gateway,) = manager.runtime_nodes["exit"]
    manager._gateway_chain_verdicts[gateway_identity(gateway)] = ChainVerdict(
        time.monotonic() - CHAIN_PROBE_INTERVAL_SECONDS - 1, "pass", 300.0, "ok"
    )


def test_a_held_layer_stays_the_gateways_upstream_and_keeps_its_remembered_verdict() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = _manager(Path(temp_dir))
        prober = _ChainProber()
        manager.gateway_chain_prober = prober
        with patch.object(manager_module, "test_nodes", _chain_round()):
            manager.check_tier2()
        _remember_old_pass(manager)

        outcome = manager._resolve_gateway_direct_failures(
            [_direct_failure(node) for node in manager.runtime_nodes["exit"]]
        )

        # No new chain probe through the hop every Sentinel chain just failed,
        # and no gateway_no_upstream either: the remembered pass stands.
        assert prober.calls == []
        assert outcome.waiting_for_upstream is False
        (gateway,) = outcome.nodes
        assert gateway.status is NodeStatus.ALIVE
        assert gateway.latency_ms == 300.0


def test_once_the_layer_is_confirmed_the_gateway_is_probed_again() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = _manager(Path(temp_dir))
        prober = _ChainProber()
        manager.gateway_chain_prober = prober
        with patch.object(manager_module, "test_nodes", _chain_round()):
            manager.check_tier2()
        with patch.object(manager_module, "test_nodes", _chain_round("jms-1", "jms-2", "jms-3")):
            manager.check_tier2()
        _remember_old_pass(manager)

        manager._resolve_gateway_direct_failures(
            [_direct_failure(node) for node in manager.runtime_nodes["exit"]]
        )

        assert len(prober.calls) == 1
        _gateway, upstream = prober.calls[0]
        assert _names(upstream) == ["jms-1", "jms-2", "jms-3"]
