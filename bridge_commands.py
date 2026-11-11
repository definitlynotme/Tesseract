"""Executor metadata for the frozen v1 contract; no business implementations.

Contract fields are properties of the original tuple, never a second command
table. The explicit metadata inventory makes an unreviewed contract addition
fail at import instead of silently granting executor capabilities.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from types import MappingProxyType
from typing import Any, Callable

import bridge_protocol as protocol


COMMON_ERRORS = frozenset({
    "not_allowed", "validation", "conflict", "runtime_unavailable",
    "runtime_gone", "job_capacity", "internal", "missing_field",
    "invalid_type", "invalid_enum",
})

# cancel{op} is an independent control frame, not a V1_COMMAND_SPECS job.
# These acknowledgements confirm an actual intent/request; receipt loss must
# never roll it back or replace the resulting facts with cancellation.
INTENT_CONFIRMING_COMMANDS = frozenset({"service.stop", "app.quit", "cancel"})


def adapt_result(name: str, raw: Any, before: int | None = None):
    """Return (success, complete safe facts or None, failure code)."""
    if name in protocol.P1_COMMANDS and isinstance(raw, dict):
        # RuntimeAccess's established stop result also carries local admission
        # diagnostics. Preserve P1's projection; those are not wire fields.
        if raw.get("saved") is True and raw.get("applied") is False:
            return False, None, "internal"
        raw = {key: raw[key] for key in protocol.V1_COMMAND_SPECS[name][5] if key in raw}
    errors: list[str] = []
    protocol._schema(raw, protocol.V1_COMMAND_SPECS[name][5], "result", errors)
    protocol._job_result(name, raw, "failed", errors)
    if errors:
        return False, None, "internal"
    result = deepcopy(raw)
    errors = []
    protocol._job_result(name, result, "done", errors)
    if name == "service.restart" and (before is None or result["core_generation"] <= before):
        errors.append("invariant:result.core_generation")
    # Validate the actual public surface too, including structural privacy.
    frame = dict(t="evt", epoch="adapter", produced_at_ms=0, sent_at_ms=0,
                 topic="jobs", rev=1, kind="edge", seq=1,
                 data=dict(op="adapter", name=name, state="failed",
                           started_at_ms=0, result=result,
                           error=dict(code="internal", message_key="errors.internal", params={})))
    if protocol.validate_frame(frame):
        return False, None, "internal"
    code = "conflict"
    if name == "service.start":
        failure = result["failure_code"]
        code = failure if failure in protocol.ERROR_CODES else "conflict"
    elif name in {"service.stop", "app.quit"}:
        code = "io" if result["shutdown_exit_code"] not in (None, 0) else "timeout"
    else:
        for field in ("apply_error", "error"):
            if isinstance(result.get(field), dict):
                code = result[field]["code"]
                break
    return not errors, result, code


@dataclass(frozen=True)
class CommandSpec:
    name: str
    contract: tuple
    adapter: Callable | None
    capacity_class: str
    needs_before: bool = False
    persisted_transaction: bool = False
    # T-P2-31a: "run by the admitting ingress thread itself" (never queued
    # behind a worker). T-P2-31b (决定_第三波裁定 T-P2-31a 第 1 轮 #2, plan b):
    # ``self_gated`` commands answer the startup-recovery gate themselves
    # (richer partial results), so the executor's mutation gate lets them in.
    inline: bool = False
    self_gated: bool = False

    intent_confirming = property(lambda self: self.name in INTENT_CONFIRMING_COMMANDS)
    domain = property(lambda self: self.contract[0])
    origin = property(lambda self: self.contract[1])
    reply = property(lambda self: self.contract[2])
    cancellation = property(lambda self: self.contract[3])
    args_schema = property(lambda self: self.contract[4])
    result_schema = property(lambda self: self.contract[5])
    errors = property(lambda self: COMMON_ERRORS | frozenset(self.contract[6].split()))


# Only executor-only facts are maintained here. In particular, persisted
# transactions must finish truthfully once begun, even after stop/owner loss.
_METADATA = {
    "service.start": (False, False), "service.stop": (False, False),
    "service.restart": (True, False), "mode.set": (False, True),
    "exit.verify": (False, False), "settings.apply": (False, True),
    "settings.validate": (False, False), "nodes.diagnose": (False, False),
    "nodes.cancelDiagnose": (False, False), "nodes.update": (False, True),
    "nodes.delete": (False, True), "nodes.reveal": (False, False),
    "subscriptions.refresh": (False, True), "import.preview": (False, False),
    "import.apply": (False, True), "gateway.select": (False, True),
    "geo.update": (False, True), "app.update.check": (False, False),
    "app.update.install": (False, True), "logs.setMihomoLevel": (False, False),
    "logs.exportBundle": (False, True), "logs.exportFiltered": (False, True),
    "alerts.test": (False, True), "alerts.desktopDelivered": (False, True),
    "ui.presence": (False, False), "app.quit": (False, False),
    "shell.saveGrant": (False, False), "update.exitResponse": (False, False),
    "update.installResult": (False, True), "app.sessionEnd": (False, True),
    "host.saveFile": (False, False), "host.openFolder": (False, False),
    "host.openUrl": (False, False), "host.reveal": (False, False),
}


# T-P2-31a (决定_第一波裁定 T-P2-22 #6): alerts.desktopDelivered only records the
# receipt in memory and hands its disk write to an ordinary worker, so it runs
# in the control domain (the contract tuple itself since T-P2-31b, which
# removed the override table), directly on the admitting ingress thread, and
# never waits behind ordinary work (a waiting alerts.test cannot starve it).
# No other command may be listed here without a contract review.
_INLINE_COMMANDS = frozenset({"alerts.desktopDelivered"})
# T-P2-31b (决定_第三波裁定, plan b): mutation commands whose handler checks the
# startup-recovery gate itself and answers with its own partial result
# (settings.apply: lifecycle_busy/recovery_in_progress hold with per-section
# SaveResult; subscriptions.refresh: RefreshResult). The executor's uniform
# conflict/recovery_pending gate is skipped for them only.
SELF_GATED_COMMANDS = frozenset({"settings.apply", "subscriptions.refresh"})


def validate_specs(specs) -> None:
    if set(specs) != set(protocol.V1_COMMAND_SPECS):
        raise ValueError("executor metadata and v1 command inventory differ")
    for name, spec in specs.items():
        if spec.self_gated and (name not in SELF_GATED_COMMANDS or spec.domain != "mutation"):
            raise ValueError("only reviewed mutation commands may gate themselves")
        if spec.inline and (name not in _INLINE_COMMANDS or spec.domain != "control"
                            or spec.reply != "res"):
            raise ValueError("only reviewed control res commands may run inline")
    shell = {name for name, spec in specs.items() if spec.domain == "shell"}
    if shell != {"host.saveFile", "host.openFolder", "host.openUrl", "host.reveal"}:
        raise ValueError("host commands must remain shell-only")
    for name, spec in specs.items():
        if spec.contract is not protocol.V1_COMMAND_SPECS[name]:
            raise ValueError("command spec must derive from contract")
        if spec.reply == "job" and not callable(spec.adapter):
            raise ValueError("job command has no result adapter")


def build_specs():
    if set(_METADATA) != set(protocol.V1_COMMAND_SPECS):
        raise ValueError("executor metadata and v1 command inventory differ")
    specs = {}
    for name, contract in protocol.V1_COMMAND_SPECS.items():
        before, persisted = _METADATA[name]
        capacity = ("stop" if name == "service.stop" else "quit" if name == "app.quit"
                    else "session_end" if name == "app.sessionEnd"
                    else "shell" if contract[0] == "shell" else "ordinary")
        specs[name] = CommandSpec(name, contract,
                                  partial(adapt_result, name) if contract[2] == "job" else None,
                                  capacity, before, persisted,
                                  inline=name in _INLINE_COMMANDS,
                                  self_gated=name in SELF_GATED_COMMANDS)
    validate_specs(specs)
    return MappingProxyType(specs)


COMMAND_SPECS = build_specs()
PYTHON_COMMANDS = frozenset(name for name, spec in COMMAND_SPECS.items() if spec.domain != "shell")
