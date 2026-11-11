from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path


IDENTITY_FILE = "tier3_build_id.json"
COMPONENT_FILES = (
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


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)


def finalize(source_root: Path, dist_root: Path) -> dict[str, object]:
    source_root = source_root.resolve()
    dist_root = dist_root.resolve()
    executable = dist_root / "Tesseract.exe"
    if not executable.is_file():
        raise FileNotFoundError(f"Missing packaged executable: {executable}")
    components = {
        name: sha256(source_root / name)
        for name in COMPONENT_FILES
    }
    material = "\n".join(
        f"{name}:{components[name]}" for name in COMPONENT_FILES
    )
    build_id = hashlib.sha256(material.encode("ascii", "replace")).hexdigest()
    executable_sha256 = sha256(executable)
    payload: dict[str, object] = {
        "schema_version": 2,
        "build_id": build_id,
        "components": components,
        "declared_executable_sha256": executable_sha256,
        "executable_sha256": executable_sha256,
        "identity_match": True,
    }
    destinations = (
        dist_root / IDENTITY_FILE,
        dist_root / "_internal" / IDENTITY_FILE,
    )
    for destination in destinations:
        atomic_write_json(destination, payload)
    if sha256(executable) != executable_sha256:
        raise RuntimeError(
            "Packaged executable changed while finalizing Tier3 identity"
        )
    for destination in destinations:
        persisted = json.loads(destination.read_text(encoding="utf-8-sig"))
        if persisted != payload:
            raise RuntimeError(
                f"Tier3 build identity readback mismatch: {destination}"
            )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Finalize the Tier3 identity for one onedir build."
    )
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--dist-root", type=Path, required=True)
    args = parser.parse_args()
    payload = finalize(args.source_root, args.dist_root)
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
