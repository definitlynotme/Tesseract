"""P2-04 isolated reads: no app start, registry writes, Core, or network probes."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import inspect
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from bridge_runtime_access import (CAPABILITIES, P2_READ_CAPABILITIES, RuntimeAccess,
                                   immutable_record, config_projection, display_filter)
from bridge_vocab import Mode, Phase, Tier, error_message_key, protocol_mode, runtime_mode
from desktop_runtime import DesktopRuntime
from models import Node, NodeStatus
from node_diagnostic import NodeDiagnosticCoreBinding
from runtime_egress_recovery import CoreLoadedGroupState, CoreLoadedRouteSnapshot, RuntimeNodeRef
from tier3_pool_view import Tier3AutoPoolCountSnapshot, Tier3UserPoolHealthSnapshot, Tier3StartupGateProgress


class TrackingLock:
    def __init__(self):
        self.lock = threading.RLock()
        self.depth = 0

    def __enter__(self):
        self.lock.acquire()
        self.depth += 1
        return self

    def __exit__(self, *args):
        self.depth -= 1
        self.lock.release()


class ForbiddenAPI:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            pytest.fail("read attempted a controller request")
        return call


class AutostartRegistry:
    HKEY_CURRENT_USER = "synthetic-hkcu"
    KEY_READ = 1

    def __init__(self, runtime):
        self.runtime = runtime
        self.value = '"C:/private/secret-shell.exe" --private-option'
        self.error = None
        self.calls = []
        self.on_read = lambda: None

    def OpenKey(self, hive, path, reserved, access):
        rt = self.runtime
        assert not any(lock.depth for lock in (rt._config_change_lock,
            rt._runtime_intent_lock, rt._runtime_route_snapshot_lock, rt.manager.lock))
        assert access == self.KEY_READ
        self.calls.append((hive, path, access))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def QueryValueEx(self, key, name):
        assert name == self.runtime.autostart.value_name
        self.on_read()
        if self.error is not None:
            raise self.error
        return self.value, 1


class Manager:
    def __init__(self, config, generation=7):
        self.config = config
        self.runtime_generation = generation
        self.lock = TrackingLock()
        self.start_time_utc = "2026-10-01T10:00:00+00:00"
        self.last_check_at = {tier: "2026-10-01T10:02:00+00:00" for tier in ("tier1", "tier2", "tier3", "exit")}
        self.tier3_last_collection_report = {"runtime_generation": generation, "source_count": 4}
        self.background_actions = {"tier3_collect": {"running": True}}
        self.runtime_nodes = {tier: [] for tier in ("tier1", "tier2", "tier3", "exit")}
        self.runtime_nodes["exit"] = [Node(name="Gateway A", protocol="trojan", tier="exit",
            address="example.invalid", port=443, password="LoadedPasswordOnly", source="https://example.invalid/private-sub",
            status=NodeStatus.ALIVE, latency_ms=12, sources=["https://example.invalid/private-sub"])]
        self.evidence = {"tier2": {}, "exit": {}}
        self.auto = Tier3AutoPoolCountSnapshot(2, 9, 12)
        self.user = Tier3UserPoolHealthSnapshot(3, 2, 3)

    def tier3_auto_pool_count_snapshot(self):
        with self.lock:
            return self.auto

    def tier3_user_pool_health_snapshot(self):
        with self.lock:
            return self.user

    def tier3_startup_gate_progress(self):
        return Tier3StartupGateProgress(self.auto.healthy, 3)

    def compact_tiers_count_status(self):
        return {"runtime_generation": self.runtime_generation, "config_identity": id(self.config),
                "tiers": {tier: {"total": 1, "usable": 1} for tier in self.runtime_nodes}}

    def subscription_source_check_snapshot(self, tier):
        return deepcopy(self.evidence[tier])


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    rt = DesktopRuntime.__new__(DesktopRuntime)
    rt._runtime_intent_lock = TrackingLock()
    rt._config_change_lock = TrackingLock()
    rt._runtime_route_snapshot_lock = TrackingLock()
    rt._node_diagnostic_binding_lock = TrackingLock()
    rt._runtime_generation = 7
    rt._lifecycle_revision = 11
    rt.running = True
    rt._stop_request_pending = False
    rt.config = {"client": {"network_mode": "system_proxy", "mihomo_mixed_port": 7890,
        "mihomo_controller": {"port": 9090, "secret": "ControllerSecretOnly"},
        "egress_recovery": {"node_judgement": {"post_dial_judge": "act", "hidden": "must survive"}},
        # T-P4-C1 #4: a resolver URL with userinfo stays a secret (a plain
        # DoH/DoT address is projected; see tests/test_bridge_p4_c1.py).
        "dns": {"mode": "custom", "nameserver": ["https://dnsuser:dnspass@dns.example.invalid/private"]}},
        "tiers": {"tier1": {"nodes": [{"protocols": {"vless": {"uuid": "ConfiguredUuidOnly"}}}]},
                  "tier2": {}, "tier3": {"max_pool_size": 100, "min_alive_nodes": 3,
                    "pi_controller": {"target_alive": 25}},
                  "exit": {"selected": "Gateway A", "subscription_urls": ["https://example.invalid/private-sub"]}},
        "ui": {"language": "en_US", "theme": "daylight"},
        "alerts": {"channels": [{"id": "ops", "type": "generic_webhook", "label": "Ops",
            "config": {"url": "https://hook.example.invalid/private", "headers": {"X-Auth": "CustomHeaderOnly"}, "timeout": 8}}]}}
    rt.manager = Manager(rt.config)
    rt.sources = {"subscription_urls": [{"url": "https://sources.example.invalid/private", "enabled": True}]}
    def read_sources():
        assert not rt._runtime_intent_lock.depth and not rt._config_change_lock.depth
        assert not rt._runtime_route_snapshot_lock.depth and not rt.manager.lock.depth
        return deepcopy(rt.sources)
    rt.config_store = SimpleNamespace(read_sources=read_sources)
    rt.network_mode = "system_proxy"
    rt.capabilities = lambda: {"is_admin": True}
    rt.connection_info = lambda: {"mixed_port": rt.config["client"]["mihomo_mixed_port"]}
    rt.registry_reads = []
    def registry_read():
        assert not rt._runtime_intent_lock.depth and not rt.system_proxy._lock.depth
        assert not rt._runtime_route_snapshot_lock.depth
        rt.registry_reads.append(True)
        return SimpleNamespace(enabled=True, server="127.0.0.1:7890")
    rt.system_proxy = SimpleNamespace(_lock=TrackingLock(), supported=True, active=True,
        backend=SimpleNamespace(read=registry_read), proxy_server=lambda: "127.0.0.1:7890")
    rt.api_client = ForbiddenAPI()
    rt.core_runner = SimpleNamespace(is_running=lambda: True)
    rt._core_generation = 8
    rt.core_fact = lambda: {"running": True, "pid": 42, "process_generation": 3, "core_generation": 8}
    path = tmp_path / "isolated-runtime.yaml"
    path.write_bytes(b"synthetic loaded configuration")
    rt._runtime_config_path_for_client = lambda: path
    rt._node_diagnostic_core_binding = NodeDiagnosticCoreBinding(1, hashlib.sha256(path.read_bytes()).hexdigest(), (), 8, (("trojan", 1),))
    rt._node_diagnostic_binding_client = rt.api_client
    rt._node_diagnostic_binding_runner = rt.core_runner
    rt._runtime_route_epoch = 2
    rt._runtime_network_context_epoch = 4
    rt._runtime_confirmed_fail_closed_snapshot_key = None
    rt._runtime_route_snapshot = CoreLoadedRouteSnapshot(process_pid=42, process_generation=3,
        core_generation=8, semantic_digest="a" * 64, route_epoch=2, network_context_epoch=4,
        group_states=(CoreLoadedGroupState("FINAL-EXIT", "Selector", ("EXIT-SELECT",), "EXIT-SELECT"),
                      CoreLoadedGroupState("EXIT-SELECT", "Selector", ("Gateway A", "Gateway B"), "Gateway A")),
        proxy_nodes=(RuntimeNodeRef("exit", "gateway", "opaque", "Gateway A"),),
        loaded_at_monotonic=1.0)
    rt._refresh_runtime_route_snapshot_serialized = lambda *a, **k: pytest.fail("route refresh")
    rt.status = lambda *a, **k: pytest.fail("full runtime snapshot")
    rt._node_judgement_state = ForbiddenAPI()
    rt.autostart = SimpleNamespace(value_name="SyntheticTesseract")
    rt.autostart_registry = AutostartRegistry(rt)
    import autostart
    monkeypatch.setattr(autostart, "_import_winreg", lambda: rt.autostart_registry)
    return rt


def wire(value):
    if hasattr(value, "items"):
        return {k: wire(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [wire(v) for v in value]
    return value


def test_vocab_and_complete_access_surface():
    assert {v.value for v in Tier} == {"vanguard", "sentinel", "bastion", "gateway"}
    assert {v.value for v in Phase} == {"stopped", "collecting", "core_standby", "running", "stopping", "failed"}
    for value in Mode:
        assert protocol_mode(runtime_mode(value.value)) == value.value
    assert error_message_key("route_contract_failed", domain="exit") == "errors.exit.route_contract_failed"
    with pytest.raises(ValueError):
        error_message_key("raw exception prose")
    public = {name for name, value in inspect.getmembers(RuntimeAccess, callable) if not name.startswith("_")}
    assert public == CAPABILITIES | P2_READ_CAPABILITIES | {"known_secret_values"}
    assert not CAPABILITIES & P2_READ_CAPABILITIES


def test_all_reads_make_zero_controller_calls_and_no_refresh(runtime):
    access = RuntimeAccess(runtime)
    for name in P2_READ_CAPABILITIES:
        getattr(access, name)()
    access.protocol_mix(source="configured")
    access.known_secret_values()
    assert runtime.api_client.calls == []
    assert runtime._node_judgement_state.calls == []


def test_deep_immutability(runtime):
    access = RuntimeAccess(runtime)
    for name in P2_READ_CAPABILITIES:
        result = getattr(access, name)()
        with pytest.raises(TypeError):
            result["changed"] = True
    result = access.config_values()
    with pytest.raises(TypeError):
        result["values"]["network"]["mode"] = "tun"
    with pytest.raises(TypeError):
        result["values"]["alerts"]["notify"]["sound"] = False
    assert "notify" not in runtime.config["alerts"]
    original = {"nested": [1]}
    result = immutable_record(original)
    original["nested"].append(2)
    assert result["nested"] == (1,)


def test_rebuilt_manager_config_and_store_are_resolved(runtime):
    access = RuntimeAccess(runtime)
    assert access.bastion_pool()["runtime_alive"] == 9
    old = runtime.manager
    config = deepcopy(runtime.config)
    config["ui"]["language"] = "ja_JP"
    config["client"]["mihomo_mixed_port"] = 7999
    runtime.config = config
    runtime.manager = Manager(config)
    runtime.manager.auto = replace(old.auto, healthy=17)
    runtime.manager.runtime_nodes["exit"][0].name = "Rebuilt node"
    runtime.config_store = SimpleNamespace(read_sources=lambda: {"telegram_channels": ["synthetic"]})
    assert access.bastion_pool()["runtime_alive"] == 17
    assert access.app_info()["language"] == "ja_JP"
    assert access.app_info()["listeners"][0]["port"] == 7999
    assert access.node_records()["items"][0]["name"] == "Rebuilt node"
    assert access.config_values()["values"]["sources"][0]["kind"] == "telegram_channels"


def test_cross_owner_changes_are_stale(runtime):
    access = RuntimeAccess(runtime)
    runtime.manager.runtime_generation = 6
    assert access.bastion_pool()["stale"]
    assert access.collect_status()["stale"]
    assert all(row["first_check"] == "unknown" for row in access.collect_status()["tiers"])
    assert access.node_records()["stale"]
    assert access.config_values()["stale"]


def test_registry_observation_is_rate_limited_and_outside_locks(runtime, monkeypatch):
    import desktop_runtime
    clock = [100.0]
    monkeypatch.setattr(desktop_runtime.time, "monotonic", lambda: clock[0])
    access = RuntimeAccess(runtime)
    assert access.network_mode()["proxy_state"] == "owned"
    for _ in range(4):
        access.network_mode()
    clock[0] = 104.99
    access.network_mode()
    assert len(runtime.registry_reads) == 1
    clock[0] = 105.0
    access.network_mode()
    assert len(runtime.registry_reads) == 2


@pytest.mark.parametrize("enabled,server,expected", [(True, "other:9000", "other"), (False, "", "disabled")])
def test_proxy_drift_is_observed_fact(runtime, enabled, server, expected):
    runtime.system_proxy.backend.read = lambda: SimpleNamespace(enabled=enabled, server=server)
    result = RuntimeAccess(runtime).network_mode()
    assert result["proxy_state"] == expected and result["drift"]
    assert not result["switching"]


def test_proxy_rebuild_does_not_bypass_five_second_limit(runtime):
    access = RuntimeAccess(runtime)
    access.network_mode()
    runtime.system_proxy = SimpleNamespace(**vars(runtime.system_proxy))
    assert access.network_mode()["proxy_state"] == "unknown"
    assert len(runtime.registry_reads) == 1


def test_pool_uses_separate_public_counts_and_unknown_ratio(runtime):
    result = RuntimeAccess(runtime).bastion_pool()
    assert (result["runtime_alive"], result["user_alive"], result["min_alive_nodes"]) == (9, 2, 3)
    assert result["capacity"] == 100 and result["target_alive"] >= 25
    assert result["ratio_known"] is False
    assert result["collecting"]
    runtime.manager.tier3_last_collection_report["startup_gate_satisfied"] = True
    assert RuntimeAccess(runtime).bastion_pool()["collecting"]


def test_pool_thresholds_use_existing_normalization(runtime):
    from tier3_threshold_policy import resolve_tier3_thresholds
    runtime.config["tiers"]["tier3"] = {"max_pool_size": "inherit", "min_alive_nodes": None}
    result = RuntimeAccess(runtime).bastion_pool()
    expected = resolve_tier3_thresholds(runtime.config["tiers"]["tier3"])
    assert result["capacity"] == expected.max_pool_size
    assert result["target_alive"] == expected.effective_target_alive


def test_loaded_and_configured_protocol_counts_remain_separate(runtime):
    access = RuntimeAccess(runtime)
    assert access.protocol_mix()["counts"] == {"trojan": 1}
    assert access.protocol_mix(source="configured")["counts"] == {"vless": 1}
    runtime._node_diagnostic_core_binding = replace(runtime._node_diagnostic_core_binding, core_generation=6)
    assert access.protocol_mix()["stale"] and access.protocol_mix()["counts"] == {}
    with pytest.raises(ValueError):
        access.protocol_mix(source="invented")


def test_protocol_names_cannot_leak_arbitrary_config_keys(runtime):
    runtime.config["tiers"]["tier1"]["nodes"][0]["protocols"]["ConfiguredUuidOnly"] = {}
    result = RuntimeAccess(runtime).protocol_mix(source="configured")
    assert result["counts"] == {"vless": 1, "other": 1}
    assert "ConfiguredUuidOnly" not in json.dumps(wire(result))


def test_collect_retained_checks_without_generation_evidence_are_unknown(runtime):
    result = RuntimeAccess(runtime).collect_status()
    rows = {r["tier"]: r for r in result["tiers"]}
    assert rows["vanguard"]["first_check"] == "unknown"
    assert rows["sentinel"]["first_check"] == "unknown"
    assert rows["gateway"]["first_check"] == "unknown"
    assert rows["gateway"]["healthy"] is None
    assert rows["bastion"]["first_check"] == "running"
    runtime.manager.tier3_last_collection_report["runtime_generation"] = 6
    assert RuntimeAccess(runtime).collect_status()["tiers"][2]["source_count"] is None


def test_collect_complete_requires_owned_evidence(runtime):
    runtime.manager.tier3_last_collection_report["startup_gate_satisfied"] = True
    runtime.manager.evidence["exit"] = {"https://example.invalid/private-sub": {"runtime_generation": 7, "checked_at": "2026-10-01T10:01:00+00:00"}}
    result = RuntimeAccess(runtime).collect_status()
    assert result["tiers"][3]["first_check"] == "complete"
    assert result["tiers"][3]["source_count"] == 1
    assert result["tiers"][2]["first_check"] == "complete"
    runtime._stop_request_pending = True
    assert all(r["first_check"] == "unknown" for r in RuntimeAccess(runtime).collect_status()["tiers"])


def test_gateway_intent_effective_missing_and_stale(runtime):
    access = RuntimeAccess(runtime)
    assert access.gateway_status()["applied"]
    runtime.config["tiers"]["exit"]["selected_proxy"] = "obsolete alias"
    assert access.gateway_status()["selected"] == "Gateway A"
    runtime.config["tiers"]["exit"]["selected"] = "Gateway B"
    value = access.gateway_status()
    assert value["selected"] == "Gateway B" and value["effective"] == "Gateway A"
    assert not value["applied"] and not value["missing"]
    runtime.config["tiers"]["exit"]["selected"] = "missing"
    assert access.gateway_status()["missing"]
    runtime._runtime_route_epoch += 1
    value = access.gateway_status()
    assert value["stale"] and value["effective"] is None and not value["applied"]


def test_gateway_without_selection_does_not_claim_applied(runtime):
    runtime.config["tiers"]["exit"].pop("selected")
    assert not RuntimeAccess(runtime).gateway_status()["applied"]


def test_secrets_registered_and_absent_from_all_new_dtos(runtime):
    access = RuntimeAccess(runtime)
    secrets = access.known_secret_values()
    expected = {"LoadedPasswordOnly", "ConfiguredUuidOnly", "ControllerSecretOnly", "CustomHeaderOnly",
        "https://dnsuser:dnspass@dns.example.invalid/private", "https://hook.example.invalid/private",
        "https://example.invalid/private-sub", "https://sources.example.invalid/private"}
    assert expected <= secrets.keys()
    runtime.manager.runtime_nodes["exit"][0].name = "LoadedPasswordOnly"
    runtime.config["alerts"]["channels"][0]["label"] = "CustomHeaderOnly"
    for name in P2_READ_CAPABILITIES:
        payload = json.dumps(wire(getattr(access, name)()), ensure_ascii=False)
        assert all(secret not in payload for secret in expected), name


def test_config_ten_sections_hide_judgement_and_preserve_original(runtime):
    before = deepcopy(runtime.config)
    value = RuntimeAccess(runtime).config_values()
    # T-P4-C1 #1: optional v1 addition ``bastion`` (PROTOCOL §17).
    assert set(value["values"]) == {"network", "dns", "routing", "latency", "sources", "alerts", "ui", "autostart", "traffic", "killswitch", "bastion"}
    text = json.dumps(wire(value))
    # T-P4-C1 #4: only the two egress recovery switches are projected.
    assert "node_judgement" not in text
    assert set(value["values"]["network"]["egress_recovery"]) <= {"enabled", "retry_source_refresh"}
    assert runtime.config == before
    assert "network.controller.secret" in value["secret_fields_set"]
    # 提醒 redesign 10-30: retired channels (and their headers) are never projected.
    assert not [name for name in value["secret_fields_set"] if name.startswith("alerts.")]
    # T-P4-C1 #4: a resolver list holding a secret (userinfo) entry is withheld whole.
    assert "nameserver" not in value["values"]["dns"] and "dns.nameserver" in value["secret_fields_set"]
    assert value["values"]["ui"]["theme"] == "daylight"


def test_config_arbitrary_extra_fields_do_not_escape(runtime):
    runtime.config["client"]["tun"] = {"stack": "mixed", "surprise": "private arbitrary data"}
    runtime.config["ui"]["private_extra"] = "private arbitrary data"
    runtime.config["routing"] = {"rules": {"direct": ["geosite:CN"], "unexpected": "leak"},
        "rule_providers": {"example": {"type": "http", "url": "https://rules.example.invalid/private", "path": "C:/Users/alice/private-rules"}}}
    value = RuntimeAccess(runtime).config_values()
    text = json.dumps(wire(value))
    assert "private arbitrary data" not in text and "C:/Users/alice" not in text
    assert value["values"]["routing"]["rules"]["direct"] == ("geosite:CN",)
    assert "routing.rule_providers.example.url" in value["secret_fields_set"]


def test_config_read_error_is_safe(runtime):
    def broken():
        raise OSError("private path ControllerSecretOnly")
    runtime.config_store.read_sources = broken
    value = RuntimeAccess(runtime).config_values()
    assert value["read_errors"][0]["code"] == "io"
    assert "private path" not in json.dumps(wire(value))


def test_app_info_does_not_advertise_unwired_p2_capabilities(runtime):
    value = RuntimeAccess(runtime).app_info()
    assert "service.start" in value["caps"] and "mode.set" not in value["caps"]
    assert all(row["available"] is None for row in value["listeners"])
    assert "ControllerSecretOnly" not in json.dumps(wire(value))


def test_subscription_ids_and_owned_evidence(runtime):
    access = RuntimeAccess(runtime)
    first = access.subscription_records()["items"][0]
    assert not first["current_generation_evidence"]
    runtime.manager.evidence["exit"] = {"https://example.invalid/private-sub": {"runtime_generation": 7, "checked_at": "2026-10-01T10:01:00+00:00"}}
    second = access.subscription_records()["items"][0]
    assert second["id"] == first["id"] and second["current_generation_evidence"]
    assert second["source_id"] == access.node_records()["items"][0]["source_id"]


def test_node_ids_expire_on_generation_change(runtime):
    access = RuntimeAccess(runtime)
    first = access.node_records()["items"][0]["id"]
    runtime._runtime_generation = runtime.manager.runtime_generation = 8
    assert access.node_records()["items"][0]["id"] != first


@pytest.mark.parametrize("domain", ["route", "network", "core"])
def test_gateway_rechecks_epoch_and_core_after_slow_secret_read(runtime, domain):
    original = runtime.bridge_secret_facts
    def changed():
        result = original()
        if domain == "route":
            runtime._runtime_route_epoch += 1
        elif domain == "network":
            runtime._runtime_network_context_epoch += 1
        else:
            runtime.core_fact = lambda: {"running": True, "pid": 42, "process_generation": 3, "core_generation": 9}
        return result
    runtime.bridge_secret_facts = changed
    value = RuntimeAccess(runtime).gateway_status()
    assert value["stale"] and not value["applied"] and value["effective"] is None


def test_config_old_sample_retains_old_secret_registration_during_reload(runtime):
    runtime.config["alerts"]["channels"][0]["label"] = "ControllerSecretOnly"
    replacement = deepcopy(runtime.config)
    replacement["client"]["mihomo_controller"]["secret"] = "NewControllerOnly"
    def changed():
        runtime.config = replacement
        runtime.manager.config = replacement
        return deepcopy(runtime.sources)
    runtime.config_store.read_sources = changed
    value = RuntimeAccess(runtime).config_values()
    assert value["stale"]
    assert "ControllerSecretOnly" not in json.dumps(wire(value))


def test_collect_new_source_evidence_does_not_validate_retained_old_check(runtime):
    runtime.manager.evidence["exit"] = {"https://example.invalid/private-sub": {"runtime_generation": 7,
        "checked_at": "2026-10-01T10:03:00+00:00"}}
    assert RuntimeAccess(runtime).collect_status()["tiers"][3]["first_check"] == "unknown"


def test_registry_concurrent_reads_reserve_one_observation_slot(runtime):
    entered = threading.Event()
    release = threading.Event()
    calls = []
    def slow():
        calls.append(True)
        entered.set()
        assert release.wait(2)
        return SimpleNamespace(enabled=True, server="127.0.0.1:7890")
    runtime.system_proxy.backend.read = slow
    first = threading.Thread(target=lambda: RuntimeAccess(runtime).network_mode())
    first.start()
    try:
        assert entered.wait(1)
        assert RuntimeAccess(runtime).network_mode()["proxy_state"] == "unknown"
    finally:
        release.set()
        first.join(2)
    assert not first.is_alive() and len(calls) == 1


def test_node_projection_hashing_occurs_outside_manager_lock(runtime, monkeypatch):
    import bridge_runtime_access
    original = bridge_runtime_access.opaque_id
    def checked(*args):
        assert not runtime.manager.lock.depth
        return original(*args)
    monkeypatch.setattr(bridge_runtime_access, "opaque_id", checked)
    RuntimeAccess(runtime).node_records()


def test_node_time_is_numeric_fact_and_never_free_text(runtime):
    runtime.manager.runtime_nodes["exit"][0].last_checked_at = "LoadedPasswordOnly"
    result = RuntimeAccess(runtime).node_records()
    assert result["items"][0]["checked_at_ms"] is None
    assert "LoadedPasswordOnly" not in json.dumps(wire(result))


@pytest.mark.parametrize("enabled", [True, False])
def test_autostart_uses_registry_enablement_instead_of_config(runtime, enabled):
    runtime.config["autostart"] = {"enabled": not enabled, "command": "private-config-path"}
    runtime.autostart_registry.value = '"C:/private/secret-shell.exe"' if enabled else ""
    before = deepcopy(runtime.config)
    value = RuntimeAccess(runtime).config_values()
    assert value["values"]["autostart"] == {"enabled": enabled}
    assert not value["stale"] and not value["read_errors"]
    assert runtime.config == before
    assert "C:/private" not in json.dumps(wire(value))
    assert "command" not in json.dumps(wire(value["values"]["autostart"]))
    assert len(runtime.autostart_registry.calls) == 1


def test_autostart_missing_registration_is_disabled(runtime):
    runtime.autostart_registry.error = FileNotFoundError("missing synthetic value")
    value = RuntimeAccess(runtime).config_values()
    assert value["values"]["autostart"] == {"enabled": False}
    assert not value["stale"] and not value["read_errors"]


@pytest.mark.parametrize("unavailable", [False, True])
def test_autostart_read_failure_is_unknown_and_safe(runtime, monkeypatch, unavailable):
    import autostart
    if unavailable:
        monkeypatch.setattr(autostart, "_import_winreg", lambda: None)
    else:
        runtime.autostart_registry.error = PermissionError("C:/private ControllerSecretOnly")
    runtime.config["autostart"] = {"enabled": True}
    value = RuntimeAccess(runtime).config_values()
    assert value["values"]["autostart"] == {} and value["stale"]
    assert value["read_errors"] == ({"code": "io", "message_key": "errors.config.autostart_read", "params": {}},)
    assert "C:/private" not in json.dumps(wire(value))
    assert "ControllerSecretOnly" not in json.dumps(wire(value))


def test_autostart_read_re_resolves_manager_and_detects_replacement(runtime):
    access = RuntimeAccess(runtime)
    assert access.config_values()["values"]["autostart"]["enabled"]
    replacement = SimpleNamespace(value_name="ReplacementTesseract")
    runtime.autostart_registry.on_read = lambda: setattr(runtime, "autostart", replacement)
    assert access.config_values()["stale"]
    runtime.autostart_registry.on_read = lambda: None
    runtime.autostart_registry.value = ""
    value = access.config_values()
    assert not value["stale"] and value["values"]["autostart"] == {"enabled": False}


def test_autostart_config_owner_change_during_read_is_stale(runtime):
    def reload():
        runtime.config = deepcopy(runtime.config)
        runtime.manager.config = runtime.config
    runtime.autostart_registry.on_read = reload
    assert RuntimeAccess(runtime).config_values()["stale"]


def test_compatibility_rules_coexist_with_normal_rules_and_are_safe(runtime):
    runtime.config["routing"] = {"rules": {"direct": ["geosite:CN"], "proxy": ["domain:example.invalid"],
        "compatibility_defaults": [{"rule_type": "direct", "value": "geoip:CN", "extra": "private-extra"},
            {"rule_type": "proxy", "value": "domain:ControllerSecretOnly"},
            {"rule_type": "reject", "value": "https://private.example.invalid/token"}]}}
    before = deepcopy(runtime.config)
    value = RuntimeAccess(runtime).config_values()
    rules = value["values"]["routing"]["rules"]
    assert rules["direct"] == ("geosite:CN",) and rules["proxy"] == ("domain:example.invalid",)
    assert rules["compatibility_defaults"] == ({"rule_type": "direct", "value": "geoip:CN"},)
    with pytest.raises(TypeError):
        rules["compatibility_defaults"][0]["value"] = "changed"
    assert "routing.rules.compatibility_defaults.1.value" in value["secret_fields_set"]
    assert "routing.rules.compatibility_defaults.2.value" in value["secret_fields_set"]
    assert all(secret not in json.dumps(wire(value)) for secret in ("ControllerSecretOnly", "private-extra", "https://private"))
    assert runtime.config == before


@pytest.mark.parametrize("present", [True, False])
def test_explicit_empty_compatibility_rules_preserve_presence(runtime, present):
    runtime.config["routing"] = {"rules": {"direct": ["geosite:CN"]}}
    if present:
        runtime.config["routing"]["rules"]["compatibility_defaults"] = []
    rules = RuntimeAccess(runtime).config_values()["values"]["routing"]["rules"]
    assert ("compatibility_defaults" in rules) is present
    if present:
        assert rules["compatibility_defaults"] == ()


@pytest.mark.parametrize("kind", ["github_repos", "telegram_channels", "subscription_urls", "local_files"])
def test_source_ids_survive_delete_insert_reorder_and_setting_change(runtime, kind):
    access = RuntimeAccess(runtime)
    first, second, third = (f"https://private.example.invalid/{name}" for name in ("first", "second", "third"))
    runtime.sources = {kind: [first, second, third]}
    def read():
        value = access.config_values()
        return [row["id"] for row in value["values"]["sources"]], set(value["secret_fields_set"])
    ids, _ = read()
    assert len(set(ids)) == 3
    runtime.sources[kind].pop(0)
    after_delete, fields = read()
    assert after_delete == ids[1:]
    assert all(f"sources.{identity}.value" in fields for identity in after_delete)
    assert f"sources.{ids[0]}.value" not in fields
    runtime.sources[kind].insert(0, "https://private.example.invalid/new")
    after_insert, _ = read()
    assert after_insert[1:] == ids[1:] and after_insert[0] not in ids
    runtime.sources[kind].reverse()
    assert read()[0] == after_insert[::-1]
    runtime.sources[kind] = [{"value": f" {third} ", "enabled": False}, {"url": second}]
    assert read()[0] == [ids[2], ids[1]]
    assert "private.example.invalid" not in json.dumps(wire(access.config_values()))


def test_source_persisted_identity_survives_value_change_and_kinds_are_distinct(runtime):
    access = RuntimeAccess(runtime)
    runtime.sources = {"subscription_urls": [{"id": "private-persisted-id", "url": "https://private/old"}],
                       "github_repos": [{"id": "private-persisted-id", "url": "https://private/old"}]}
    before = [row["id"] for row in access.config_values()["values"]["sources"]]
    assert len(set(before)) == 2
    runtime.sources["subscription_urls"][0]["url"] = "https://private/new"
    value = access.config_values()
    assert [row["id"] for row in value["values"]["sources"]] == before
    assert "private-persisted-id" not in json.dumps(wire(value))



def test_read_and_write_layers_share_the_single_secret_registry():
    import bridge_vocab
    import bridge_validation
    import bridge_runtime_access
    assert bridge_runtime_access.SECRET_FIELDS is bridge_validation.SECRET_FIELDS is bridge_vocab.SECRET_FIELDS
    # 提醒 redesign 10-30: neither layer edits alert channel secrets any more (the redactor still
    # knows them through SECRET_FIELDS, see the retired-channel test below).
    assert not hasattr(bridge_validation, "ALERT_SECRETS")
    assert bridge_runtime_access._ALERT_STRIPPED_FIELDS is bridge_vocab.SECRET_FIELDS["alert_stripped"]
    with pytest.raises(TypeError):
        bridge_vocab.SECRET_FIELDS["alerts"]["telegram"] = frozenset()


def test_registered_sender_credentials_stay_hidden_in_read_projection(runtime):
    runtime.config["alerts"]["channels"] = [
        {"id": "twilio", "type": "sms_twilio", "config": {"account_sid": "AC-private-fixture",
            "auth_token": "private-auth-fixture", "messaging_service_sid": "MG-private-fixture"}},
        {"id": "webhook", "type": "generic_webhook", "config": {"headers": {
            "Authorization": "private-header-fixture", "X-Custom": "private-custom-fixture"}}},
    ]
    access = RuntimeAccess(runtime)
    expected = {"AC-private-fixture", "private-auth-fixture", "MG-private-fixture", "private-header-fixture", "private-custom-fixture"}
    assert expected <= access.known_secret_values().keys()
    value = access.config_values()
    assert not [name for name in value["secret_fields_set"] if name.startswith("alerts.")]
    assert all(secret not in json.dumps(wire(value)) for secret in expected)



def test_shared_alert_registry_preserves_legacy_projection_order():
    from bridge_vocab import ALERT_SECRETS, SECRET_FIELDS
    assert tuple(ALERT_SECRETS) == tuple(SECRET_FIELDS["alert_stripped"])


# Independent acceptance table: losing a record must not shrink this coverage.
_EXPECTED_CHANNEL_SECRETS = {
    "telegram": ("bot_token",), "whatsapp": ("access_token", "api_url"),
    "generic_webhook": ("url", "headers.*"),
    **{kind: ("url", "webhook_url", "headers.*") for kind in ("slack", "discord", "teams", "google_chat")},
    "ntfy": ("server", "topic", "token"), "pushover": ("app_token", "user_key"),
    "gotify": ("server", "app_token"), "email_sendgrid": ("api_key", "api_url"),
    "sms_twilio": ("account_sid", "auth_token", "api_url", "messaging_service_sid"),
    "email_smtp": ("username", "password"), "desktop": (), "sound": (),
}
_CHANNEL_SECRET_PATHS = [(kind, leaf) for kind, fields in _EXPECTED_CHANNEL_SECRETS.items()
    for field in fields for leaf in (("headers.Authorization", "headers.Proxy-Authorization",
        "headers.Cookie", "headers.X-Api-Key", "headers.X-Token", "headers.X-Secret", "headers.monKEY")
        if field == "headers.*" else (field,))]


def test_secret_views_are_derived_from_unique_immutable_field_records():
    from dataclasses import FrozenInstanceError
    from bridge_vocab import SECRET_RECORDS, SECRET_FIELDS, ALERT_SECRETS
    assert SECRET_FIELDS["records"] is SECRET_RECORDS
    assert dict(ALERT_SECRETS) == {kind: frozenset(fields) for kind, fields in _EXPECTED_CHANNEL_SECRETS.items()}
    for kind in ALERT_SECRETS:
        assert ALERT_SECRETS[kind] == frozenset("headers.*" if record.header_names else name
            for name, record in SECRET_RECORDS.items() if kind in record.channels)
        assert SECRET_FIELDS["alert_stripped"][kind] == frozenset(
            name.replace("_", "") for name, record in SECRET_RECORDS.items() if kind in record.stripped)
    assert SECRET_FIELDS["projection_names"] == frozenset(
        name.replace("_", "") for name, record in SECRET_RECORDS.items() if record.projection)
    assert SECRET_FIELDS["sources"] == frozenset(name for name, record in SECRET_RECORDS.items() if record.source_edit)
    assert SECRET_FIELDS["source_identity"] == ("value", "url", "path", "channel")
    with pytest.raises(FrozenInstanceError):
        SECRET_RECORDS["topic"].channels = ()
    with pytest.raises(TypeError):
        SECRET_RECORDS["topic"] = None


@pytest.mark.parametrize("kind,field", _CHANNEL_SECRET_PATHS)
@pytest.mark.parametrize("legacy", [False, True], ids=["channels", "legacy"])
def test_each_retired_channel_secret_is_still_redacted_never_projected_or_submittable(kind, field, legacy):
    """提醒 redesign 10-30: alert channels are retired, but their blocks may
    still sit in a config file. Their secrets stay registered for log
    redaction, never reach config.snapshot, and the alerts section no longer
    accepts channel rows or secret edits."""
    from bridge_runtime_access import _config_secret_values
    from bridge_validation import validate_change
    value = "https://secret.example.test/fixture" if field in {"url", "webhook_url", "api_url", "server"} else "private-fixture"
    config = {field: value} if not field.startswith("headers.") else {"headers": {field.split(".", 1)[1]: value}}
    channels = {"alerts": {"channels": [{"id": "fixture-channel", "type": kind,
        "enabled": False, "config": deepcopy(config)}]}}
    read_config = {"alerts": {kind: deepcopy(config)}} if legacy else deepcopy(channels)
    before = deepcopy(read_config)
    assert value in _config_secret_values(read_config)
    projected, fields_set = config_projection(read_config, {}, clean=display_filter(read_config))
    assert set(projected["alerts"]) == {"notify"}
    assert not [name for name in fields_set if name.startswith("alerts.")]
    assert value not in json.dumps(projected)
    assert read_config == before
    current = {"alerts": {"notify": {"enabled": True, "events": ["connection"], "sound": True}}}
    rows = validate_change({"section": "alerts", "values": {"channels": [
        {"id": "fixture-channel", "type": kind, "config": deepcopy(config)}]}}, current)
    assert not rows.valid and rows.values is None and not rows.secret_edits
    edit = {"field": field, "target_id": "fixture-channel", "action": "set", "value": value}
    secret = validate_change({"section": "alerts", "values": {}, "secrets": [edit]}, current)
    assert not secret.valid and secret.values is None and not secret.secret_edits


@pytest.mark.parametrize("kind,field,value,used", [
    ("ntfy", "topic", " /private-topic/ ", "private-topic"),
    ("email_smtp", "username", " private-user ", "private-user"),
])
def test_newly_registered_secret_sender_forms_are_filtered(kind, field, value, used):
    from bridge_runtime_access import _config_secret_values
    config = {"alerts": {"channels": [{"type": kind, "config": {field: value}}]}}
    assert {value, used} <= _config_secret_values(config).keys()
    assert used not in display_filter(config)(f"sender error: {used}")


def test_record_policies_retain_conservative_headers_and_historical_aliases():
    from bridge_runtime_access import _config_secret_values
    config = {"alerts": {"channels": [
        {"type": "generic_webhook", "config": {"headers": {"Accept": "custom-private"}}},
        {"type": "gotify", "config": {"server": " https://fixture.test/ "}},
        {"type": "email_smtp", "config": {"password": " padded-password "}},
    ]}, "nodes": [{"username": "node-user", "uuid": "node-uuid"}],
        "subscription_urls": ["private-subscription"]}
    values = _config_secret_values(config)
    assert values["custom-private"] == "authorization"
    assert {" https://fixture.test/ ", "https://fixture.test/", "https://fixture.test"} <= values.keys()
    assert " padded-password " in values and "padded-password" not in values
    assert values["node-user"] == "authorization" and values["node-uuid"] == "uuid"
    assert values["private-subscription"] == "subscription"
