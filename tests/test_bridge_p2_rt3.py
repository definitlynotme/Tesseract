"""T-P2-RT3 (Fallback 10-07): the Core-loaded network mode follows what was
actually loaded — a reused staged capture payload reports the mode it was
rendered with, and the generic serialized reload records its render."""
from __future__ import annotations

import inspect
import logging
import threading
from types import SimpleNamespace

import pytest

from desktop_runtime import DesktopRuntime
from tests.test_bridge_p2_rt2 import HotReloadRuntime


class StagedHotReloadRuntime(HotReloadRuntime):
    def _write_mihomo_runtime_payload(self, payload, path=None):
        self.events.append(("write_staged", payload))
        return "runtime.yaml"

    def _record_pending_runtime_export_contract(self, payload, runtime_config, *, contract=None):
        pass

    def _runtime_export_contract_for_payload(self, payload, **kwargs):
        return {}


def staged(mode_at_staging):
    rt = StagedHotReloadRuntime(reloaded=True)
    rt._staged_capture_runtime_config_payload = "formal-payload"
    rt._staged_capture_runtime_config_path = "runtime.yaml"
    rt._staged_capture_runtime_export_contract = {}
    rt._staged_capture_runtime_network_mode = mode_at_staging
    return rt


def test_reused_staged_payload_reports_the_mode_it_was_rendered_with():
    # Staged under system_proxy, then set_network_mode moved to tun before the
    # capture hot reload reused that staged payload.
    rt = staged("system_proxy")
    rt.network_mode = "tun"
    rt._hot_reload_runtime_config_phase_locked(rt.core_runner, capture_enabled=True)
    assert rt.events == [("write_staged", "formal-payload"), "hot_reload"]
    assert rt._core_loaded_network_mode == "system_proxy"
    assert rt._staged_capture_runtime_config_payload is None
    assert rt._staged_capture_runtime_network_mode is None  # Fallback 特许 4319


def test_a_live_render_still_reports_the_current_mode():
    rt = staged("system_proxy")
    rt._staged_capture_runtime_config_payload = None  # cleared: render live
    rt.network_mode = "mixed"
    rt._hot_reload_runtime_config_phase_locked(rt.core_runner, capture_enabled=True)
    assert rt.events == ["write_capture", "hot_reload"]
    assert rt._core_loaded_network_mode == "mixed"  # stale staged mode ignored


def test_no_capture_reload_never_uses_the_staged_mode():
    rt = staged("system_proxy")
    rt.network_mode = "tun"
    rt._hot_reload_runtime_config_phase_locked(rt.core_runner, capture_enabled=False)
    assert rt._core_loaded_network_mode == "tun"


def test_failed_reuse_keeps_the_previous_loaded_mode():
    rt = staged("tun")
    rt.core_runner.hot_reload = lambda **kw: {"reloaded": False, "reason": "x"}
    with pytest.raises(RuntimeError):
        rt._hot_reload_runtime_config_phase_locked(rt.core_runner, capture_enabled=True)
    assert rt._core_loaded_network_mode == "system_proxy"


def test_staging_records_the_render_mode_and_clearing_sites_clear_it():
    source = inspect.getsource(DesktopRuntime.write_mihomo_no_capture_runtime_config)
    captured = source.index("rendered_network_mode = ")
    assert captured < source.index("self.write_mihomo_runtime_config()")
    stage = source.index("self._staged_capture_runtime_config_payload = formal_payload")
    assert stage < source.index(
        "self._staged_capture_runtime_network_mode = rendered_network_mode") < source.index(
        "build_capture_disabled_clash_config(formal_payload)")
    # Invariant: staged mode is set iff the staged payload is (Fallback 10-07).
    for function in (DesktopRuntime.stop_core, DesktopRuntime._fail_terminal_tier3_bootstrap,
                     DesktopRuntime._hot_reload_runtime_config_phase_locked):
        text = inspect.getsource(function)
        assert text.count("self._staged_capture_runtime_config_payload = None") == text.count(
            "self._staged_capture_runtime_network_mode = None") == 1


@pytest.mark.parametrize("phase,expected", [
    ("formal_capture", "tun"), ("formal_no_capture", "tun"),
    ("bootstrap", "system_proxy"), ("stopped", "system_proxy")])
def test_serialized_reload_records_only_in_a_formal_phase(phase, expected):
    rt = HotReloadRuntime(reloaded=True)
    rt._core_runtime_phase = phase
    written = rt._record_reloaded_network_mode("tun")
    assert rt._core_loaded_network_mode == expected
    assert written == ((True, "system_proxy") if expected == "tun" else (False, None))


@pytest.mark.parametrize("phase,current,reverted", [
    ("formal_capture", "tun", True), ("formal_no_capture", "tun", True),
    ("formal_capture", "mixed", False),  # a later writer is never overwritten
    ("stopped", "tun", False), ("bootstrap", "tun", False)])
def test_revert_is_a_compare_and_set_in_a_formal_phase(phase, current, reverted):
    rt = HotReloadRuntime(reloaded=True)
    rt._core_runtime_phase = phase
    rt._core_loaded_network_mode = current
    assert rt._revert_reloaded_network_mode(expected="tun", previous="system_proxy") is reverted
    assert rt._core_loaded_network_mode == ("system_proxy" if reverted else current)


def test_serialized_reload_records_after_each_successful_reload_only():
    source = inspect.getsource(DesktopRuntime._reload_core_config_serialized)
    assert source.count("self._record_reloaded_network_mode(loaded_network_mode)") == 1
    standby_render = source.index('loaded_network_mode = str(getattr(self, "network_mode"')
    standby_write = source.index("self.write_mihomo_no_capture_runtime_config()")
    standby_failed = source.index('"no-capture standby reload failed: "')
    standby_record = source.index("self._record_reloaded_network_mode(loaded_network_mode)")
    assert standby_render < standby_write < standby_failed < standby_record
    main_render = source.index('loaded_network_mode = str(getattr(self, "network_mode"',
                               standby_record)
    main_write = source.index("runtime_config_path = self.write_mihomo_runtime_config()",
                              main_render)
    main_failed = source.index('return keep_existing(f"hot reload failed:', main_write)
    main_record = source.index("record_loaded_network_mode(loaded_network_mode)", main_failed)
    assert main_render < main_write < main_failed < main_record
    assert source.count("record_stage_reload(") == 3  # definition + pruned + repair
    assert source.count("revert_recorded_network_mode(result)") == 1


# --- fix round 1: behaviour of _reload_core_config_serialized (stand-ins) ---

def _mode_of(text):
    return text.split("mode: ", 1)[1].strip() if "mode: " in text else None


class SerializedRuntime(DesktopRuntime):
    """Only what _reload_core_config_serialized and its keep-existing,
    pruned and repair helpers touch. The runtime file holds ``mode: <m>``;
    ``core_text`` is what the stand-in Core loaded on its last hot reload."""

    def __init__(self, tmp_path, *, phase="formal_capture", primary=True,
                 core_mode="system_proxy", configured="tun"):  # noqa: D401
        self._core_lifecycle_lock = threading.Lock()
        self._runtime_route_snapshot_lock = threading.RLock()
        self._runtime_pending_export_contract = None
        self._core_runtime_phase = phase
        self._core_loaded_network_mode = core_mode
        self._staged_capture_runtime_config_payload = None
        self._staged_capture_runtime_config_path = None
        self._staged_capture_runtime_export_contract = None
        self._staged_capture_runtime_network_mode = None
        self._tier3_core_provider_pending_projection = None
        self.network_mode = configured
        self.stop_event = threading.Event()
        self.manager = object()
        self.logger = logging.getLogger("test_bridge_p2_rt3")
        self.primary = primary
        self.runtime_path = tmp_path / "runtime.yaml"
        self.runtime_path.write_text(f"mode: {core_mode}\n", encoding="utf-8")
        self.core_text = f"mode: {core_mode}\n"
        self.reload_outcomes = []  # False = that hot reload fails; empty = succeed
        self.verify_failures = 0
        self.on_stage_render = None  # e.g. a concurrent set_network_mode
        self.runner = SimpleNamespace(
            is_running=lambda: True, hot_reload=self._hot_reload,
            status=lambda: {"running": True})

    def _hot_reload(self, **kwargs):
        if self.reload_outcomes and not self.reload_outcomes.pop(0):
            return {"reloaded": False, "reason": "rejected"}
        self.core_text = self.runtime_path.read_text(encoding="utf-8")
        return {"reloaded": True}

    # rendering
    def write_mihomo_runtime_config(self):
        self.runtime_path.write_text(f"mode: {self.network_mode}\n", encoding="utf-8")
        return self.runtime_path

    def write_mihomo_no_capture_runtime_config(self):
        return self.write_mihomo_runtime_config()

    # surroundings of the serialized reload
    def _get_core_runner(self):
        return self.runner

    def _current_core_runtime_phase(self):
        return self._core_runtime_phase

    def _capture_core_reload_context(self):
        return SimpleNamespace(
            controller={"enabled": True}, current_tier3_nodes=[],
            runtime_config_path=self.runtime_path,
            previous_config_text=self.runtime_path.read_text(encoding="utf-8"),
            provider_path=None, rollback_tier3_nodes=[])

    def _tier3_is_primary_export(self):
        return self.primary

    def _prepare_tier3_core_repair_override(self, **kwargs):
        if self.on_stage_render:
            self.on_stage_render()
        return object()

    def _repair_tier3_export_order_from_core_delays(self):
        if self.on_stage_render:
            self.on_stage_render()
        return True

    def _verify_tier3_after_core_reload(self, save_snapshot=True):
        if self.verify_failures:
            self.verify_failures -= 1
            raise RuntimeError("204 verification failed")

    def _tier3_core_repair_target_alive(self):
        return 1

    def _tier3_core_repair_min_alive(self):
        return 1

    def _prime_tier3_pool_before_core_start(self): pass
    def _tier3_core_sync_marker(self): return ("m",)
    def _should_keep_existing_core_for_empty_tier3_export(self): return False
    def _core_export_sync_marker(self): return ("m",)
    def _core_export_removes_loaded_proxy(self, marker): return False
    def _tier3_export_removes_last_synced_proxy(self): return False
    def _begin_core_route_mutation(self, reason): pass
    def _core_route_mutation_is_pending(self): return True
    def _mark_runtime_source_transport_loaded(self): pass
    def _mark_loaded_runtime_export_contract(self): pass
    def _record_tier3_core_sync_event(self, event): pass
    def _invalidate_exit_route_context(self): pass
    def _set_effective_egress_state(self, available, reason): pass
    def _queue_incremental_egress_recheck(self): pass
    def _queue_tier3_egress_activation(self): pass
    def _clear_tier3_core_export_override(self): pass
    def _current_tier3_pool_has_tcp_prelaunch_nodes(self): return False
    def _mark_tier3_core_sync_success(self): pass
    def _runtime_traffic_capture_wanted(self): return False
    def _ensure_geodata_ready_for_core_start(self, path): pass


def test_rollback_after_a_failed_pruned_reload_reverts_the_recorded_mode(tmp_path):
    # Review MUST-1 / M1: the main reload loads tun and is recorded; the pruned
    # primary reload fails; keep_existing reloads the previous (system_proxy)
    # config, so the field must follow the Core back.
    rt = SerializedRuntime(tmp_path)
    rt.reload_outcomes = [True, False]  # main ok, pruned rejected, restore ok
    result = rt._reload_core_config_serialized(mode="strict")
    assert result["reloaded"] is False and result["restore_reload"]["reloaded"] is True
    assert _mode_of(rt.core_text) == "system_proxy"
    assert rt._core_loaded_network_mode == "system_proxy"


def test_a_failed_restore_keeps_the_mode_the_core_still_runs(tmp_path):
    rt = SerializedRuntime(tmp_path)
    rt.reload_outcomes = [True, False, False]  # main ok, pruned and restore rejected
    result = rt._reload_core_config_serialized(mode="strict")
    assert result["reloaded"] is False and result["restore_reload"]["reloaded"] is False
    assert _mode_of(rt.core_text) == "tun"
    assert rt._core_loaded_network_mode == "tun"


def test_rollback_after_a_failed_primary_verification_reverts_too(tmp_path):
    rt = SerializedRuntime(tmp_path)
    rt.verify_failures = 1  # primary export: the repair stage refuses at once
    result = rt._reload_core_config_serialized(mode="strict")
    assert result["reloaded"] is False and result["restore_reload"]["reloaded"] is True
    assert _mode_of(rt.core_text) == "system_proxy"
    assert rt._core_loaded_network_mode == "system_proxy"


def test_successful_strict_reload_records_the_pruned_render(tmp_path):
    # Review S3: the mode changes while the pruned stage prepares; the Core
    # ends up with the pruned render, and so does the field.
    rt = SerializedRuntime(tmp_path)
    rt.on_stage_render = lambda: setattr(rt, "network_mode", "mixed")
    assert rt._reload_core_config_serialized(mode="strict")["reloaded"] is True
    assert _mode_of(rt.core_text) == "mixed"
    assert rt._core_loaded_network_mode == "mixed"


def test_repair_reload_records_its_render(tmp_path):
    rt = SerializedRuntime(tmp_path, primary=False)
    rt.verify_failures = 1  # main verification fails, repair reload verifies
    rt.on_stage_render = lambda: setattr(rt, "network_mode", "mixed")
    assert rt._reload_core_config_serialized(mode="strict")["reloaded"] is True
    assert _mode_of(rt.core_text) == "mixed"
    assert rt._core_loaded_network_mode == "mixed"


@pytest.mark.parametrize("restore_ok,expected", [(True, "system_proxy"), (False, "mixed")])
def test_repair_reload_then_failed_verification(tmp_path, restore_ok, expected):
    # The repair reload is loaded (and recorded) before its verification fails;
    # a successful restore reverts to the pre-call value, a failed one keeps it.
    rt = SerializedRuntime(tmp_path, primary=False)
    rt.verify_failures = 2
    rt.on_stage_render = lambda: setattr(rt, "network_mode", "mixed")
    rt.reload_outcomes = [True, True, restore_ok]
    result = rt._reload_core_config_serialized(mode="strict")
    assert result["reloaded"] is False
    assert _mode_of(rt.core_text) == expected
    assert rt._core_loaded_network_mode == expected


def test_failed_main_hot_reload_does_not_record(tmp_path):
    rt = SerializedRuntime(tmp_path)
    rt.reload_outcomes = [False]  # main rejected, restore ok
    assert rt._reload_core_config_serialized(mode="strict")["reloaded"] is False
    assert rt._core_loaded_network_mode == "system_proxy"


@pytest.mark.parametrize("phase", ["stopped", "stopping"])
def test_no_record_outside_a_formal_phase(tmp_path, phase):
    rt = SerializedRuntime(tmp_path, phase=phase)
    assert rt._reload_core_config_serialized(mode="strict")["reloaded"] is True
    assert _mode_of(rt.core_text) == "tun"
    assert rt._core_loaded_network_mode == "system_proxy"


def test_no_capture_standby_success_records(tmp_path):
    rt = SerializedRuntime(tmp_path, phase="formal_no_capture")
    assert rt._reload_core_config_serialized(mode="strict")["reloaded"] is True
    assert _mode_of(rt.core_text) == "tun"
    assert rt._core_loaded_network_mode == "tun"


def test_no_capture_standby_failure_does_not_record(tmp_path):
    rt = SerializedRuntime(tmp_path, phase="formal_no_capture")
    rt.reload_outcomes = [False]
    assert rt._reload_core_config_serialized(mode="strict")["reloaded"] is False
    assert rt._core_loaded_network_mode == "system_proxy"
    assert _mode_of(rt.runtime_path.read_text(encoding="utf-8")) == "system_proxy"


class StagingRuntime(SerializedRuntime):
    def write_mihomo_no_capture_runtime_config(self):  # the real staging writer
        return DesktopRuntime.write_mihomo_no_capture_runtime_config(self)

    def write_mihomo_runtime_config(self):
        self.runtime_path.write_text(
            f"mode: {self.network_mode}\ntun:\n  enable: true\n", encoding="utf-8")
        return self.runtime_path

    def _runtime_export_contract_for_payload(self, payload, **kwargs):
        return {}

    def _write_mihomo_runtime_payload(self, payload, path=None):
        self.runtime_path.write_text(payload, encoding="utf-8")
        return self.runtime_path

    def _record_pending_runtime_export_contract(self, payload, written, *, contract=None):
        pass


@pytest.mark.parametrize("mode", ["system_proxy", "tun", "mixed"])
def test_staging_stores_the_render_mode_with_the_payload(tmp_path, mode):
    # Review S2: payload, path and mode are staged together, and the mode is
    # the one the formal payload was rendered with.
    rt = StagingRuntime(tmp_path, phase="formal_no_capture", configured=mode)
    written = rt.write_mihomo_no_capture_runtime_config()
    assert rt._staged_capture_runtime_config_payload == f"mode: {mode}\ntun:\n  enable: true\n"
    assert rt._staged_capture_runtime_config_path == rt.runtime_path == written
    assert rt._staged_capture_runtime_network_mode == mode
    assert "tun:" not in rt.runtime_path.read_text(encoding="utf-8")  # no-capture written
