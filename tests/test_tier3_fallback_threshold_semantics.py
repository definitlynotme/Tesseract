import threading
import unittest
from types import SimpleNamespace

from desktop_runtime import DesktopRuntime
from manager import ProxyTierManager
from manager_state_metrics import state_mode
from models import Node, NodeStatus


class RecordingCoreRunner:
    def __init__(self, *, running: bool) -> None:
        self.running = running
        self.stop_calls = 0

    def is_running(self) -> bool:
        return self.running

    def stop(self) -> None:
        self.stop_calls += 1
        self.running = False


class RecordingKillSwitch:
    def __init__(self) -> None:
        self.active = False
        self.activate_calls = 0
        self.deactivate_calls = 0

    def get_state(self) -> dict[str, object]:
        return {
            "state": "active" if self.active else "armed",
            "active": self.active,
            "manual_override": None,
        }

    def activate(self) -> bool:
        self.activate_calls += 1
        changed = not self.active
        self.active = True
        return changed

    def deactivate(self) -> bool:
        self.deactivate_calls += 1
        changed = self.active
        self.active = False
        return changed


def healthy_tier3_nodes(count: int) -> list[Node]:
    return [
        Node(
            name=f"tier3-{index}",
            address=f"tier3-{index}.example.com",
            port=443,
            tier="tier3",
            status=NodeStatus.ALIVE,
        )
        for index in range(count)
    ]


def runtime_for_gate(
    *,
    minimum: int,
    core_running: bool,
    runtime_running: bool,
    required_for_start: bool,
) -> tuple[DesktopRuntime, RecordingCoreRunner]:
    runtime = object.__new__(DesktopRuntime)
    runtime.stop_event = threading.Event()
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._core_runtime_phase = "formal_capture" if core_running else "stopped"
    runtime.running = runtime_running
    runtime._tier3_required_for_start = required_for_start
    runtime.manager = SimpleNamespace(
        closed=False,
        _tier3_min_alive_nodes=lambda: minimum,
    )
    runner = RecordingCoreRunner(running=core_running)
    runtime.core_runner = runner
    runtime._tier3_core_sync_marker = lambda: ("committed-tier3-pool",)
    return runtime, runner


class Tier3FallbackThresholdSemanticsTests(unittest.TestCase):
    def test_only_zero_healthy_tier3_nodes_is_offline(self) -> None:
        minimum = 5

        for healthy in range(1, minimum):
            with self.subTest(healthy=healthy):
                self.assertEqual(
                    state_mode(
                        tier1_total=0,
                        tier1_alive=0,
                        tier2_alive=0,
                        tier3_alive=healthy,
                    ),
                    ("tier3_active", "tier3"),
                )

        self.assertEqual(
            state_mode(
                tier1_total=0,
                tier1_alive=0,
                tier2_alive=0,
                tier3_alive=0,
            ),
            ("offline", "offline"),
        )

    def test_below_minimum_node_count_alone_does_not_activate_killswitch(self) -> None:
        kill_switch = RecordingKillSwitch()
        manager = SimpleNamespace(
            kill_switch=kill_switch,
            tier3_core_running=True,
            tier3_traffic_capture_active=True,
            lock=threading.RLock(),
            _tier3_pre_gate_side_effects_suppressed=lambda: False,
            _tier3_business_side_effects_allowed=lambda **_kwargs: True,
        )

        alerts = ProxyTierManager._sync_killswitch(
            manager,
            {
                "mode": "tier3_active",
                "tier3_alive": 1,
                "effective_egress_ok": None,
                "effective_egress_reason": "",
            },
        )

        self.assertEqual(alerts, [])
        self.assertEqual(kill_switch.activate_calls, 0)
        self.assertFalse(kill_switch.active)

    def test_effective_egress_failure_activates_killswitch_while_tier3_is_active(self) -> None:
        kill_switch = RecordingKillSwitch()
        manager = SimpleNamespace(
            kill_switch=kill_switch,
            tier3_core_running=True,
            tier3_traffic_capture_active=True,
            lock=threading.RLock(),
            _tier3_pre_gate_side_effects_suppressed=lambda: False,
            _tier3_business_side_effects_allowed=lambda **_kwargs: True,
        )

        alerts = ProxyTierManager._sync_killswitch(
            manager,
            {
                "mode": "tier3_active",
                "tier3_alive": 1,
                "effective_egress_ok": False,
                "effective_egress_reason": "FINAL-EXIT is unavailable",
            },
        )

        self.assertEqual(kill_switch.activate_calls, 1)
        self.assertTrue(kill_switch.active)
        self.assertEqual(len(alerts), 1)

    def test_running_core_is_not_stopped_when_pool_falls_below_minimum(self) -> None:
        runtime, runner = runtime_for_gate(
            minimum=3,
            core_running=True,
            runtime_running=True,
            required_for_start=False,
        )

        decision = runtime._tier3_core_gate_check(healthy_tier3_nodes(1))

        self.assertEqual(decision, (False, 1, 3, "core_already_running"))
        self.assertTrue(runner.running)
        self.assertEqual(runner.stop_calls, 0)

    def test_cold_start_and_restart_are_blocked_below_minimum(self) -> None:
        cases = (
            ("cold_start", False, True),
            ("restart", True, False),
        )

        for label, runtime_running, required_for_start in cases:
            with self.subTest(case=label):
                runtime, runner = runtime_for_gate(
                    minimum=3,
                    core_running=False,
                    runtime_running=runtime_running,
                    required_for_start=required_for_start,
                )

                decision = runtime._tier3_core_gate_check(healthy_tier3_nodes(1))

                self.assertEqual(decision, (False, 1, 3, "below_min_alive_nodes"))
                self.assertFalse(runner.running)
                self.assertEqual(runner.stop_calls, 0)


if __name__ == "__main__":
    unittest.main()
