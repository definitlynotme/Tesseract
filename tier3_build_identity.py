from __future__ import annotations

import hashlib
import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any


_IDENTITY_FILE = "tier3_build_id.json"
_COMPONENT_FILES = (
    "scripts/tier3_audit_runtime_hook.py",
    "scripts/tier3_audit_entry.py",
    "scripts/tier3_audit_import_hooks.py",
    "scripts/tier3_audit_socket_policy.py",
    "scripts/tier3_audit_process_registry.py",
    "scripts/tier3_audit_activity.py",
    "scripts/tier3_audit_liveness.py",
    "manager.py",
    "tier_cache.py",
    "collector.py",
    "parser.py",
    "tier3_sources.py",
    "tier3_source_supply.py",
    "runtime_source_transport.py",
    "runtime_source_dns.py",
    "clash_formatter.py",
    "tier3_validation_protocol.py",
    "tier3_validation_domain.py",
    "tier3_validation_config.py",
    "tier3_validator_helper.py",
    "tier3_validator_prewarm.py",
    "tier3_validator_runtime.py",
    "tier3_validator_supervisor.py",
    "tier3_batch_validator_helper.py",
    "tier3_batch_validator_runtime.py",
    "tier3_batch_validator_supervisor.py",
    "tier3_batch_pipe_io.py",
    "tier3_pipe_wait.py",
    "tier3_tcp_dns.py",
    "tester.py",
    "tier3_transport_contract.py",
    "tier3_refresh_types.py",
    "tier3_candidate_scheduler.py",
    "tier3_refresh_controller.py",
    "tier3_expansion_actor.py",
    "tier3_endpoint_controller.py",
    "runtime_network_context.py",
    "desktop_runtime.py",
    "runtime_egress_recovery.py",
)


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unavailable"


def _source_identity() -> dict[str, Any]:
    root = Path(__file__).resolve().parent
    components = {
        name: _sha256(root / name)
        for name in _COMPONENT_FILES
    }
    material = "\n".join(
        f"{name}:{components[name]}" for name in _COMPONENT_FILES
    )
    build_id = hashlib.sha256(material.encode("ascii", "replace")).hexdigest()
    return {
        "schema_version": 2,
        "build_id": build_id,
        "components": components,
        "declared_executable_sha256": "source-run",
        "executable_sha256": "source-run",
        "identity_match": True,
    }


def _identity_paths() -> tuple[Path, ...]:
    paths: list[Path] = []
    if getattr(sys, "frozen", False):
        paths.append(Path(sys.executable).resolve().parent / _IDENTITY_FILE)
    resource_root = Path(
        str(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    )
    paths.append(resource_root / _IDENTITY_FILE)
    return tuple(dict.fromkeys(paths))


@lru_cache(maxsize=1)
def _frozen_executable_sha256() -> str:
    if not getattr(sys, "frozen", False):
        return "source-run"
    return _sha256(Path(sys.executable).resolve())


def tier3_build_identity() -> dict[str, Any]:
    if not getattr(sys, "frozen", False):
        return _source_identity()
    for path in _identity_paths():
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or not payload.get("build_id"):
            continue
        components = payload.get("components")
        declared_executable_sha256 = str(
            payload.get("declared_executable_sha256")
            or payload.get("executable_sha256")
            or "unavailable"
        )
        executable_sha256 = _frozen_executable_sha256()
        return {
            "schema_version": 2,
            "build_id": str(payload["build_id"]),
            "components": (
                dict(components) if isinstance(components, dict) else {}
            ),
            "declared_executable_sha256": declared_executable_sha256,
            "executable_sha256": executable_sha256,
            "identity_match": bool(
                executable_sha256 != "unavailable"
                and declared_executable_sha256 == executable_sha256
            ),
        }
    source_identity = _source_identity()
    source_identity.update(
        {
            "declared_executable_sha256": "unavailable",
            "executable_sha256": _frozen_executable_sha256(),
            "identity_match": False,
        }
    )
    return source_identity
