from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from unittest import mock

import tier3_build_identity as build_identity_module
from manager import tier3_runtime_feature_report
from scripts.finalize_tier3_build_identity import COMPONENT_FILES, finalize
from tier3_build_identity import tier3_build_identity


HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_tier3_build_identity_covers_runtime_protocol_and_scheduler() -> None:
    identity = tier3_build_identity()
    components = identity["components"]

    assert HEX64.fullmatch(identity["build_id"])
    assert set(components) == {
        "scripts/tier3_audit_runtime_hook.py",
        "scripts/tier3_audit_entry.py",
        "scripts/tier3_audit_import_hooks.py",
        "scripts/tier3_audit_socket_policy.py",
        "scripts/tier3_audit_process_registry.py",
        "scripts/tier3_audit_activity.py",
        "scripts/tier3_audit_liveness.py",
        "manager.py",
        "tier_cache.py",
        "tester.py",
        "tier3_transport_contract.py",
        "tier3_refresh_types.py",
        "collector.py",
        "tier3_sources.py",
        "tier3_source_supply.py",
        "runtime_source_transport.py",
        "runtime_source_dns.py",
        "clash_formatter.py",
        "parser.py",
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
        "tier3_candidate_scheduler.py",
        "tier3_refresh_controller.py",
        "tier3_expansion_actor.py",
        "tier3_endpoint_controller.py",
        "runtime_network_context.py",
        "desktop_runtime.py",
        "runtime_egress_recovery.py",
    }
    assert all(HEX64.fullmatch(value) for value in components.values())
    assert tuple(components) == COMPONENT_FILES
    assert identity["schema_version"] == 2
    assert identity["declared_executable_sha256"] == "source-run"
    assert identity["executable_sha256"] == "source-run"
    assert identity["identity_match"] is True

    report = tier3_runtime_feature_report()
    assert report["feature_marker"] == identity["build_id"]
    assert report["build_components"] == components
    assert report["declared_executable_sha256"] == "source-run"
    assert report["executable_sha256"] == "source-run"
    assert report["identity_match"] is True
    assert report["performance_sample_valid"] is True
    assert report["packaged"] is False
    assert report["feature_marker"] != "tier3-success-baseline-audit-20260716-v61"


def test_frozen_identity_uses_actual_executable_hash_once() -> None:
    root = Path.cwd() / ".pytest-work" / "tier3-build-identity-frozen"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    executable = root / "Tesseract.exe"
    executable.write_bytes(b"actual-executable")
    actual = hashlib.sha256(executable.read_bytes()).hexdigest()
    manifest = root / "tier3_build_id.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "build_id": "b" * 64,
                "components": {},
                "declared_executable_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    build_identity_module._frozen_executable_sha256.cache_clear()
    with (
        mock.patch.object(build_identity_module.sys, "frozen", True, create=True),
        mock.patch.object(build_identity_module.sys, "executable", str(executable)),
        mock.patch.object(
            build_identity_module,
            "_identity_paths",
            return_value=(manifest,),
        ),
    ):
        identity = build_identity_module.tier3_build_identity()
        assert identity["executable_sha256"] == actual
        assert identity["declared_executable_sha256"] == "a" * 64
        assert identity["identity_match"] is False

        report = tier3_runtime_feature_report()
        assert report["identity_match"] is False
        assert report["performance_sample_valid"] is False
        assert report["packaged"] is True

        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "build_id": "b" * 64,
                    "components": {},
                    "declared_executable_sha256": actual,
                }
            ),
            encoding="utf-8",
        )
        matched = build_identity_module.tier3_build_identity()
        assert matched["executable_sha256"] == actual
        assert matched["identity_match"] is True
    build_identity_module._frozen_executable_sha256.cache_clear()
    shutil.rmtree(root, ignore_errors=True)


def test_endpoint_policy_change_changes_source_build_identity() -> None:
    before = build_identity_module._source_identity()
    original_sha256 = build_identity_module._sha256

    def changed_endpoint(path: Path) -> str:
        if path.name == "tier3_endpoint_controller.py":
            return "f" * 64
        return original_sha256(path)

    with mock.patch.object(
        build_identity_module, "_sha256", side_effect=changed_endpoint,
    ):
        after = build_identity_module._source_identity()

    assert before["build_id"] != after["build_id"]
    assert after["components"]["tier3_endpoint_controller.py"] == "f" * 64
    for name, digest in before["components"].items():
        if name != "tier3_endpoint_controller.py":
            assert after["components"][name] == digest


def test_build_identity_finalizer_writes_root_and_internal_manifests() -> None:
    root = Path.cwd() / ".pytest-work" / "tier3-build-identity-finalizer"
    shutil.rmtree(root, ignore_errors=True)
    source_root = root / "source"
    dist_root = root / "dist" / "Tesseract"
    source_root.mkdir(parents=True, exist_ok=True)
    (dist_root / "_internal").mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(build_identity_module._COMPONENT_FILES):
        (source_root / name).parent.mkdir(parents=True, exist_ok=True)
        (source_root / name).write_text(f"component-{index}\n", encoding="utf-8")
    executable = dist_root / "Tesseract.exe"
    executable.write_bytes(b"packaged-executable")

    payload = finalize(source_root, dist_root)

    actual = hashlib.sha256(executable.read_bytes()).hexdigest()
    assert payload["schema_version"] == 2
    assert payload["declared_executable_sha256"] == actual
    assert payload["executable_sha256"] == actual
    assert payload["identity_match"] is True
    for manifest in (
        dist_root / "tier3_build_id.json",
        dist_root / "_internal" / "tier3_build_id.json",
    ):
        assert json.loads(manifest.read_text(encoding="utf-8")) == payload
    shutil.rmtree(root, ignore_errors=True)


def test_packaged_isolation_entry_is_a_build_input_and_pre_freeze_runtime_hook() -> None:
    source = (Path.cwd() / "tesseract-runtime.spec").read_text(encoding="utf-8")
    # T-P2-H7: the audit hook stays the first runtime hook; the stdlib-only
    # platform seed hook follows it (still before PyInstaller's standard hooks
    # and the entry).
    assert 'runtime_hooks = [str(ROOT / "scripts" / "tier3_audit_runtime_hook.py"),' in source
    assert source.index('"tier3_audit_runtime_hook.py"') < source.index('"sidecar_platform_seed_rthook.py"')
    before = build_identity_module._source_identity()
    original = build_identity_module._sha256

    def changed_hook(path: Path) -> str:
        return "f" * 64 if path.name == "tier3_audit_entry.py" else original(path)

    with mock.patch.object(build_identity_module, "_sha256", side_effect=changed_hook):
        after = build_identity_module._source_identity()
    assert before["build_id"] != after["build_id"]
    assert after["components"]["scripts/tier3_audit_entry.py"] == "f" * 64
