from __future__ import annotations

import threading
from collections.abc import Callable
from types import SimpleNamespace

from desktop_runtime import DesktopRuntime
from runtime_reload_gate import RuntimeReloadGate, RuntimeReloadReason


class _Runner:
    def __init__(self, *, running: bool) -> None:
        self.running = running

    def is_running(self) -> bool:
        return self.running

    def status(self) -> dict[str, object]:
        return {"running": self.running}


def _runtime_with_reload_gate(
    *,
    core_running: bool,
    loaded_digest: str,
    target_digest: str,
    apply: Callable[[], dict[str, object]],
) -> tuple[DesktopRuntime, RuntimeReloadGate, list[str]]:
    runtime = object.__new__(DesktopRuntime)
    runtime._core_transition_lock = threading.RLock()
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._core_runtime_phase = "formal_capture" if core_running else "stopped"
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_origin_diagnostic_bindings = {}
    runtime._runtime_selector_intent_revision = 0
    runtime.core_runner = _Runner(running=core_running)
    gate = RuntimeReloadGate(loaded_digest)
    runtime._runtime_reload_gate = gate
    runtime._runtime_loaded_semantic_digest = lambda: loaded_digest
    runtime._render_mihomo_runtime_payload_snapshot = lambda: "target-payload"
    runtime._runtime_export_contract_for_payload = lambda _payload: {
        "semantic_digest": target_digest
    }
    runtime._last_core_export_sync_marker = None
    runtime._core_export_sync_marker = lambda: ("target", target_digest)
    runtime._reload_core_config_serialized = lambda **_kwargs: apply()
    degradations: list[str] = []
    runtime._record_runtime_facility_degradation = (
        lambda reason: degradations.append(str(reason)) or {}
    )
    return runtime, gate, degradations


def test_stopped_core_same_digest_enters_apply_path_instead_of_unchanged() -> None:
    apply_calls: list[str] = []
    runtime, gate, degradations = _runtime_with_reload_gate(
        core_running=False,
        loaded_digest="same",
        target_digest="same",
        apply=lambda: apply_calls.append("apply")
        or {"reloaded": True, "core": {"running": True}},
    )

    result = runtime.reload_core_config(
        mode="incremental_export",
        reason=RuntimeReloadReason.CAPTURE_LIFECYCLE,
    )

    assert apply_calls == ["apply"]
    assert result["reloaded"] is True
    assert "unchanged" not in result
    assert gate.loaded_semantic_digest == "same"
    assert degradations == []


def test_lost_reload_lease_reports_unknown_authority_and_degradation() -> None:
    gate_holder: dict[str, RuntimeReloadGate] = {}

    def apply_after_losing_lease() -> dict[str, object]:
        gate = gate_holder["gate"]
        assert gate.snapshot().active_reload_reason == "capture_lifecycle"
        gate.reset_for_new_core()
        return {"reloaded": True, "core": {"running": True}}

    runtime, gate, degradations = _runtime_with_reload_gate(
        core_running=True,
        loaded_digest="loaded",
        target_digest="changed",
        apply=apply_after_losing_lease,
    )
    gate_holder["gate"] = gate

    result = runtime.reload_core_config(
        mode="incremental_export",
        reason=RuntimeReloadReason.CAPTURE_LIFECYCLE,
    )

    expected_reason = (
        "Core apply completed but reload-gate authority could not be settled"
    )
    assert result["reload_gate_commit_lost"] is True
    assert result["authority_unknown"] is True
    assert result["reason"] == expected_reason
    assert degradations == [expected_reason]
    assert gate.loaded_semantic_digest == ""


def test_active_reload_lease_publishes_loaded_digest() -> None:
    active_reasons: list[str] = []
    gate_holder: dict[str, RuntimeReloadGate] = {}

    def apply_with_live_lease() -> dict[str, object]:
        active_reasons.append(
            gate_holder["gate"].snapshot().active_reload_reason
        )
        return {"reloaded": True, "core": {"running": True}}

    runtime, gate, degradations = _runtime_with_reload_gate(
        core_running=True,
        loaded_digest="loaded",
        target_digest="changed",
        apply=apply_with_live_lease,
    )
    gate_holder["gate"] = gate

    result = runtime.reload_core_config(
        mode="incremental_export",
        reason=RuntimeReloadReason.CAPTURE_LIFECYCLE,
    )

    assert result["reloaded"] is True
    assert "authority_unknown" not in result
    assert active_reasons == ["capture_lifecycle"]
    assert gate.loaded_semantic_digest == "changed"
    assert gate.snapshot().active_reload_reason == ""
    assert degradations == []


def test_reload_without_reason_is_refused_and_logged() -> None:
    runtime, gate, degradations = _runtime_with_reload_gate(
        core_running=True,
        loaded_digest="loaded",
        target_digest="changed",
        apply=lambda: (_ for _ in ()).throw(
            AssertionError("a reason-less reload must not apply")
        ),
    )
    warnings: list[str] = []
    runtime.logger = SimpleNamespace(
        warning=lambda message, *args, **_kwargs: warnings.append(message % args)
    )

    result = runtime.reload_core_config(mode="strict")

    assert result == {
        "reloaded": False,
        "mode": "strict",
        "reload_blocked": True,
        "reason": "explicit_reload_reason_required",
    }
    assert warnings == [
        "Core reload refused: no reload reason was supplied (mode=strict)"
    ]
    assert gate.snapshot().active_reload_reason == ""
    assert gate.loaded_semantic_digest == "loaded"
    assert degradations == []
