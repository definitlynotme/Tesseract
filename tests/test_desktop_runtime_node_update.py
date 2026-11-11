import copy
import logging
import threading
import time
import unittest

from desktop_runtime import DesktopRuntime
from gateway_selection import GatewaySelection, gateway_identity
from models import Node, NodeStatus
from runtime_reload_gate import RuntimeReloadReason


class RecordingRLock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.depth = 0

    @property
    def held(self) -> bool:
        return self.depth > 0

    def __enter__(self):
        self._lock.acquire()
        self.depth += 1
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.depth -= 1
        self._lock.release()


class FakeConfigStore:
    def __init__(self, payload: dict, *, config_lock: RecordingRLock, operation_lock: RecordingRLock) -> None:
        self.payload = copy.deepcopy(payload)
        self.config_lock = config_lock
        self.operation_lock = operation_lock
        self.update_calls: list[tuple[dict, dict]] = []
        self.restore_calls: list[dict] = []

    def read_local(self) -> dict:
        return copy.deepcopy(self.payload)

    def update_config_item(self, item: dict, updates: dict) -> dict:
        if not self.config_lock.held or not self.operation_lock.held:
            raise AssertionError("node update must hold both configuration and manager operation locks")
        self.update_calls.append((copy.deepcopy(item), copy.deepcopy(updates)))
        self.payload["updated"] = True
        return {"saved": True}

    def write_local(self, payload: dict) -> dict:
        self.restore_calls.append(copy.deepcopy(payload))
        self.payload = copy.deepcopy(payload)
        return copy.deepcopy(payload)


class FakeManager:
    def __init__(self, config: dict, operation_lock: RecordingRLock) -> None:
        self.config = config
        self.operation_lock = operation_lock
        self.lock = threading.RLock()
        self.runtime_nodes = {
            "tier1": [
                Node(
                    name="old-node",
                    protocol="ss",
                    uri="ss://old",
                    address="old.example",
                    port=8388,
                    tier="tier1",
                    status=NodeStatus.ALIVE,
                )
            ],
            "tier2": [],
            "tier3": [],
            "exit": [],
        }


class FakeRunner:
    def __init__(self, running: bool = True) -> None:
        self.running = running
        self.stop_calls = 0

    def is_running(self) -> bool:
        return self.running

    def stop(self):
        self.stop_calls += 1
        self.running = False
        return {"running": False}


class DesktopRuntimeNodeUpdateTests(unittest.TestCase):
    def _runtime(self, *, running: bool = True, core_running: bool | None = None):
        runtime = DesktopRuntime.__new__(DesktopRuntime)
        config_lock = RecordingRLock()
        operation_lock = RecordingRLock()
        original_local = {
            "tiers": {"tier1": {"nodes": [{"name": "old-node", "enabled": True}]}}
        }
        runtime._config_change_lock = config_lock
        runtime.config = copy.deepcopy(original_local)
        runtime.config_store = FakeConfigStore(
            original_local,
            config_lock=config_lock,
            operation_lock=operation_lock,
        )
        runtime.manager = FakeManager(runtime.config, operation_lock)
        runtime.core_runner = FakeRunner(
            running=running if core_running is None else core_running
        )
        runtime.running = running
        runtime.logger = logging.getLogger("desktop-runtime-node-update-test")
        runtime.logger.handlers.clear()
        runtime.logger.addHandler(logging.NullHandler())
        return runtime, config_lock, operation_lock, original_local

    @staticmethod
    def _item() -> dict:
        return {"tier": "tier1", "kind": "node", "key": "old-node"}

    def test_running_node_update_is_locked_and_strictly_reloaded_once(self) -> None:
        runtime, config_lock, operation_lock, _original_local = self._runtime()
        calls: list[object] = []

        def reload_config(*, target_tier=None):
            self.assertTrue(config_lock.held)
            self.assertTrue(operation_lock.held)
            calls.append(("config", target_tier))
            runtime.manager.runtime_nodes["tier1"] = []
            return {}

        def reload_core_config(*, mode="strict", reason=None, user_commit_id=None):
            self.assertTrue(config_lock.held)
            self.assertTrue(operation_lock.held)
            calls.append(("core", mode, reason, user_commit_id))
            return {"reloaded": True}

        runtime.reload_config = reload_config
        runtime.reload_core_config = reload_core_config

        result = runtime.update_config_item(self._item(), {"enabled": False})

        self.assertEqual(
            result,
            {
                "saved": True,
                "node_config_transaction": True,
                "kind": "node",
                "enabled": False,
            },
        )
        self.assertEqual(calls[0], ("config", "tier1"))
        self.assertEqual(calls[1][0:3], ("core", "strict", RuntimeReloadReason.USER_GRAPH_CONFIG))
        self.assertTrue(str(calls[1][3]).startswith("node-config:"))
        self.assertEqual(runtime.config_store.restore_calls, [])

    def test_strict_reload_confirmation_accepts_restart_but_not_kept_existing_core(self) -> None:
        self.assertTrue(DesktopRuntime._strict_core_reload_confirmed({"running": True}))
        self.assertFalse(
            DesktopRuntime._strict_core_reload_confirmed(
                {"core": {"running": True}}
            )
        )
        self.assertFalse(
            DesktopRuntime._strict_core_reload_confirmed(
                {
                    "kept_existing_core": True,
                    "reloaded": True,
                    "core": {"running": True},
                }
            )
        )
        self.assertFalse(DesktopRuntime._strict_core_reload_confirmed(None))

    def test_stopped_runtime_updates_config_without_starting_or_reloading_core(self) -> None:
        runtime, _config_lock, _operation_lock, _original_local = self._runtime(running=False)
        runtime.reload_config = lambda **_kwargs: {}
        runtime.reload_core_config = lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("stopped runtime must not reload or start the core")
        )

        result = runtime.update_config_item(self._item(), {"enabled": False})

        self.assertEqual(
            result,
            {
                "saved": True,
                "node_config_transaction": True,
                "kind": "node",
                "enabled": False,
            },
        )
        self.assertFalse(runtime.core_runner.running)

    def test_subscription_update_uses_the_same_strict_transaction(self) -> None:
        runtime, _config_lock, _operation_lock, _original_local = self._runtime()
        calls: list[tuple[str, object]] = []
        runtime.reload_config = lambda **kwargs: calls.append(("config", kwargs.get("target_tier"))) or {}
        runtime.reload_core_config = lambda **kwargs: calls.append(("core", kwargs.get("mode"))) or {"reloaded": True}
        item = {
            "tier": "tier2",
            "kind": "subscription",
            "key": "https://old.example/sub",
            "url": "https://old.example/sub",
        }

        result = runtime.update_config_item(
            item,
            {"url": "https://new.example/sub", "enabled": "false"},
        )

        self.assertEqual(
            result,
            {
                "saved": True,
                "node_config_transaction": True,
                "kind": "subscription",
                "enabled": False,
            },
        )
        self.assertEqual(calls, [("config", "tier2"), ("core", "strict")])
        self.assertEqual(
            runtime.config_store.update_calls[-1][1],
            {"url": "https://new.example/sub", "enabled": "false"},
        )

    def test_exit_subscription_only_clears_selection_when_selected_child_matches(self) -> None:
        source_url = "https://exit.example/sub"
        runtime, _config_lock, _operation_lock, _original_local = self._runtime(running=False)
        runtime.reload_config = lambda **_kwargs: {}
        runtime.manager.runtime_nodes["exit"] = [
            Node(name="subscription-child", tier="exit", source=source_url, sources=[source_url])
        ]
        runtime.manager.active_exit_proxy_name = lambda: "subscription-child"
        runtime.manager._node_has_subscription_source = (
            lambda node, url: url in {node.source, *node.sources}
        )

        runtime.update_config_item(
            {"tier": "exit", "kind": "subscription", "key": source_url, "url": source_url},
            {"enabled": False},
        )

        self.assertTrue(runtime.config_store.update_calls[-1][1]["_clear_exit_selection"])

        runtime.manager.runtime_nodes["exit"] = [
            Node(name="manual-child", tier="exit", source="manual-import")
        ]
        runtime.manager.active_exit_proxy_name = lambda: "manual-child"
        runtime.update_config_item(
            {"tier": "exit", "kind": "subscription", "key": source_url, "url": source_url},
            {"enabled": False},
        )
        self.assertNotIn("_clear_exit_selection", runtime.config_store.update_calls[-1][1])

    def test_exit_subscription_keeps_selection_when_child_has_another_enabled_source(self) -> None:
        source_url = "https://exit.example/a"
        other_url = "https://exit.example/b"
        runtime, _config_lock, _operation_lock, _original_local = self._runtime(running=False)
        runtime.reload_config = lambda **_kwargs: {}
        runtime.manager.runtime_nodes["exit"] = [
            Node(
                name="shared-child",
                tier="exit",
                source=source_url,
                sources=[source_url, other_url],
            )
        ]
        runtime.manager.active_exit_proxy_name = lambda: "shared-child"
        runtime.manager._exit_subscription_urls = lambda: [source_url, other_url]
        runtime.manager._node_has_subscription_source = (
            lambda node, url: url in {node.source, *node.sources}
        )

        runtime.update_config_item(
            {"tier": "exit", "kind": "subscription", "key": source_url, "url": source_url},
            {"enabled": False},
        )

        self.assertNotIn("_clear_exit_selection", runtime.config_store.update_calls[-1][1])

    def test_exit_subscription_disable_matches_the_bound_identity_not_the_name(self) -> None:
        """T-GW-ID: the saved selection names B's identity.  Disabling the
        subscription of A, a same-named child of another subscription, keeps
        the Gateway; disabling B's own (last enabled) source turns it off."""

        first_url = "https://exit.example/u"
        second_url = "https://exit.example/v"
        runtime, _config_lock, _operation_lock, _original_local = self._runtime(running=False)
        runtime.reload_config = lambda **_kwargs: {}
        child_a = Node(
            name="child",
            protocol="trojan",
            address="a.exit.example",
            port=443,
            password="password-a",
            tier="exit",
            status=NodeStatus.ALIVE,
            source=first_url,
            sources=[first_url],
        )
        child_b = Node(
            name="child",
            protocol="trojan",
            address="b.exit.example",
            port=443,
            password="password-b",
            tier="exit",
            status=NodeStatus.ALIVE,
            source=second_url,
            sources=[second_url],
        )
        self.assertNotEqual(gateway_identity(child_a), gateway_identity(child_b))
        runtime.manager.runtime_nodes["exit"] = [child_a, child_b]
        selection = GatewaySelection("bound", "child", identity=gateway_identity(child_b))
        runtime.manager.active_exit_selection = lambda: selection
        runtime.manager.active_exit_proxy_name = lambda: selection.label
        enabled_urls = [first_url, second_url]
        runtime.manager._exit_subscription_urls = lambda: list(enabled_urls)
        runtime.manager._node_has_subscription_source = (
            lambda node, url: url in {node.source, *node.sources}
        )

        runtime.update_config_item(
            {"tier": "exit", "kind": "subscription", "key": first_url, "url": first_url},
            {"enabled": False},
        )
        self.assertEqual(runtime.config_store.update_calls[-1][1], {"enabled": False})

        enabled_urls.remove(first_url)
        runtime.update_config_item(
            {"tier": "exit", "kind": "subscription", "key": second_url, "url": second_url},
            {"enabled": False},
        )
        self.assertEqual(len(runtime.config_store.update_calls), 2)
        self.assertEqual(
            runtime.config_store.update_calls[-1][1],
            {"enabled": False, "_clear_exit_selection": True},
        )

    def test_exit_subscription_disable_keeps_the_name_rule_for_a_name_only_selection(self) -> None:
        """Review F1: a name-only selection 'child' with same-named children
        in two subscriptions.  Disabling either source turns the Gateway off
        (the name rule, as before), so the surviving namesake is never served
        and later bound as if it had been picked."""

        first_url = "https://exit.example/u"
        second_url = "https://exit.example/v"
        runtime, _config_lock, _operation_lock, _original_local = self._runtime(running=False)
        runtime.reload_config = lambda **_kwargs: {}
        children = [
            Node(name="child", protocol="trojan", address=f"{tag}.exit.example", port=443,
                 password=f"password-{tag}", tier="exit", status=NodeStatus.ALIVE,
                 source=url, sources=[url])
            for tag, url in (("a", first_url), ("b", second_url))
        ]
        runtime.manager.runtime_nodes["exit"] = children
        selection = GatewaySelection("legacy", "child")
        runtime.manager.active_exit_selection = lambda: selection
        runtime.manager.active_exit_proxy_name = lambda: selection.label
        runtime.manager._exit_subscription_urls = lambda: [first_url, second_url]
        runtime.manager._node_has_subscription_source = (
            lambda node, url: url in {node.source, *node.sources}
        )

        runtime.update_config_item(
            {"tier": "exit", "kind": "subscription", "key": first_url, "url": first_url},
            {"enabled": False},
        )
        self.assertEqual(
            runtime.config_store.update_calls[-1][1],
            {"enabled": False, "_clear_exit_selection": True},
        )

    def test_live_core_is_strictly_reloaded_even_if_service_flag_is_false(self) -> None:
        runtime, _config_lock, _operation_lock, _original_local = self._runtime(
            running=False,
            core_running=True,
        )
        calls: list[str] = []
        runtime.reload_config = lambda **_kwargs: {}
        runtime.reload_core_config = lambda **kwargs: calls.append(
            str(kwargs.get("mode"))
        ) or {"reloaded": True}

        result = runtime.update_config_item(self._item(), {"enabled": False})

        self.assertEqual(
            result,
            {
                "saved": True,
                "node_config_transaction": True,
                "kind": "node",
                "enabled": False,
            },
        )
        self.assertEqual(calls, ["strict"])

    def test_validation_failure_before_write_does_not_reload_or_stop_core(self) -> None:
        runtime, _config_lock, _operation_lock, _original_local = self._runtime()
        runtime.config_store.update_config_item = lambda *_args, **_kwargs: (
            _ for _ in ()
        ).throw(ValueError("invalid node"))
        runtime.reload_config = lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("unchanged invalid input must not reload configuration")
        )
        runtime.reload_core_config = lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("unchanged invalid input must not reload the core")
        )

        with self.assertRaisesRegex(ValueError, "invalid node"):
            runtime.update_config_item(self._item(), {"enabled": False})

        self.assertEqual(runtime.config_store.restore_calls, [])
        self.assertEqual(runtime.core_runner.stop_calls, 0)

    def test_failed_strict_reload_restores_config_and_manager_then_reloads_old_core(self) -> None:
        runtime, _config_lock, _operation_lock, original_local = self._runtime()
        reload_config_calls = 0
        core_results = iter(
            [
                {"reloaded": False, "reason": "rejected"},
                {"reloaded": True},
            ]
        )

        def reload_config(*, target_tier=None):
            nonlocal reload_config_calls
            reload_config_calls += 1
            runtime.config = copy.deepcopy(runtime.config_store.payload)
            runtime.manager.config = runtime.config
            runtime.manager.runtime_nodes["tier1"] = []
            return {"tier": target_tier}

        runtime.reload_config = reload_config
        runtime.reload_core_config = lambda **_kwargs: next(core_results)

        with self.assertRaisesRegex(RuntimeError, "rolled back"):
            runtime.update_config_item(self._item(), {"enabled": False})

        self.assertEqual(reload_config_calls, 2)
        self.assertEqual(runtime.config_store.payload, original_local)
        self.assertEqual(runtime.config_store.restore_calls, [original_local])
        self.assertEqual([node.name for node in runtime.manager.runtime_nodes["tier1"]], ["old-node"])
        self.assertEqual(runtime.core_runner.stop_calls, 0)

    def test_unconfirmed_rollback_stops_core(self) -> None:
        runtime, _config_lock, _operation_lock, _original_local = self._runtime()
        reload_config_calls = 0
        stop_calls: list[dict] = []

        def reload_config(*, target_tier=None):
            nonlocal reload_config_calls
            reload_config_calls += 1
            runtime.config = copy.deepcopy(runtime.config_store.payload)
            return {"tier": target_tier}

        def stop_core(**kwargs):
            stop_calls.append(dict(kwargs))
            runtime.core_runner.running = False
            return {"running": False}

        runtime.reload_config = reload_config
        runtime.reload_core_config = lambda **_kwargs: {"reloaded": False, "reason": "still rejected"}
        runtime.stop_core = stop_core

        with self.assertRaisesRegex(RuntimeError, "rolled back"):
            runtime.update_config_item(self._item(), {"enabled": False})

        self.assertEqual(reload_config_calls, 2)
        self.assertEqual(len(stop_calls), 1)
        self.assertEqual(stop_calls[0]["actor"], "node-config-rollback")
        self.assertFalse(runtime.core_runner.running)

    def test_concurrent_node_updates_are_serialized(self) -> None:
        runtime, _config_lock, _operation_lock, _original_local = self._runtime(running=False)
        first_entered = threading.Event()
        release_first = threading.Event()
        order: list[str] = []
        failures: list[BaseException] = []

        def update_config_item(_item: dict, updates: dict) -> dict:
            label = str(updates["name"])
            order.append(f"start-{label}")
            if label == "first":
                first_entered.set()
                if not release_first.wait(timeout=2.0):
                    raise TimeoutError("concurrency test did not release first update")
            order.append(f"end-{label}")
            return {"saved": label}

        runtime.config_store.update_config_item = update_config_item
        runtime.reload_config = lambda **_kwargs: {}

        def run(label: str) -> None:
            try:
                runtime.update_config_item(self._item(), {"name": label})
            except BaseException as exc:
                failures.append(exc)

        first = threading.Thread(target=run, args=("first",))
        second = threading.Thread(target=run, args=("second",))
        first.start()
        self.assertTrue(first_entered.wait(timeout=2.0))
        second.start()
        time.sleep(0.05)
        self.assertEqual(order, ["start-first"])
        release_first.set()
        first.join(timeout=2.0)
        second.join(timeout=2.0)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(
            order,
            ["start-first", "end-first", "start-second", "end-second"],
        )

    def test_ui_preferences_write_uses_node_configuration_lock(self) -> None:
        runtime, config_lock, _operation_lock, _original_local = self._runtime(
            running=False
        )
        original_read = runtime.config_store.read_local
        original_write = runtime.config_store.write_local

        def read_local() -> dict:
            self.assertTrue(config_lock.held)
            return original_read()

        def write_local(payload: dict) -> dict:
            self.assertTrue(config_lock.held)
            return original_write(payload)

        runtime.config_store.read_local = read_local
        runtime.config_store.write_local = write_local

        result = runtime.write_ui_preferences(
            {"language": "en_US", "theme": "daylight"}
        )

        self.assertEqual(result["ui"], {"language": "en_US", "theme": "daylight"})
        self.assertEqual(runtime.config["ui"], result["ui"])

    def test_structured_local_update_waits_for_node_transaction_and_preserves_it(self) -> None:
        runtime, _config_lock, _operation_lock, _original_local = self._runtime(
            running=False
        )
        first_entered = threading.Event()
        release_first = threading.Event()
        failures: list[BaseException] = []
        observed_payloads: list[dict] = []
        original_update = runtime.config_store.update_config_item

        def update_node(item: dict, updates: dict) -> dict:
            first_entered.set()
            if not release_first.wait(timeout=2.0):
                raise TimeoutError("concurrency test did not release node update")
            return original_update(item, updates)

        runtime.config_store.update_config_item = update_node
        runtime.reload_config = lambda **_kwargs: {}

        def run_node_update() -> None:
            try:
                runtime.update_config_item(self._item(), {"enabled": False})
            except BaseException as exc:
                failures.append(exc)

        def run_structured_update() -> None:
            try:
                def apply_setting(payload: dict) -> dict:
                    observed_payloads.append(copy.deepcopy(payload))
                    payload.setdefault("client", {})["allow_lan"] = True
                    return payload

                runtime.update_local_config(apply_setting)
            except BaseException as exc:
                failures.append(exc)

        node_thread = threading.Thread(target=run_node_update)
        settings_thread = threading.Thread(target=run_structured_update)
        node_thread.start()
        self.assertTrue(first_entered.wait(timeout=2.0))
        settings_thread.start()
        time.sleep(0.05)
        self.assertEqual(observed_payloads, [])
        release_first.set()
        node_thread.join(timeout=2.0)
        settings_thread.join(timeout=2.0)

        self.assertEqual(failures, [])
        self.assertEqual(len(observed_payloads), 1)
        self.assertTrue(observed_payloads[0]["updated"])
        self.assertTrue(runtime.config_store.payload["updated"])
        self.assertTrue(runtime.config_store.payload["client"]["allow_lan"])

    def test_killswitch_node_config_filters_disabled_drafts(self) -> None:
        runtime, _config_lock, _operation_lock, _original_local = self._runtime()
        runtime.config = {
            "tiers": {
                "tier1": {
                    "nodes": [
                        {"name": "active"},
                        {"name": "draft", "enabled": "false"},
                        "legacy",
                    ]
                }
            }
        }

        self.assertEqual(
            runtime._tier_nodes_config("tier1"),
            [{"name": "active"}, "legacy"],
        )


if __name__ == "__main__":
    unittest.main()
