# -*- mode: python ; coding: utf-8 -*-
# Independent onedir sidecar; never writes the Qt build or source identity.
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(SPECPATH)


def require_file(path, label):
    if not path.is_file():
        raise FileNotFoundError(f"Missing required {label}: {path}")
    return path


# T-P6-01 (plan §9.3): the sidecar's VERSIONINFO comes from a generated PyInstaller
# version file, the way the Qt build uses build/version_info.generated.txt;
# scripts/build_shell_preview.ps1 writes it from app_version.txt. Without the
# variable (scripts/build_runtime_sidecar.ps1) the sidecar has no version resource.
_version_info = os.environ.get("TESSERACT_RUNTIME_VERSION_INFO_PATH", "").strip()
VERSION_INFO = (str(require_file(Path(_version_info), "generated sidecar version metadata"))
                if _version_info else None)


license_root = ROOT / "build" / "third-party-licenses"
license_files = sorted(path for path in license_root.rglob("*") if path.is_file())
if not license_files:
    raise FileNotFoundError(f"Missing required license tree: {license_root}")

# Preserve AppPaths' _internal/bin and SBOM/license resource locations.
datas = [
    (str(require_file(ROOT / source, source)), destination)
    for source, destination in (
        ("static/app_icon.ico", "static"),
        ("static/app_icon.png", "static"),
        ("static/app_icon.svg", "static"),
        ("static/app_logo.svg", "static"),
        ("config.example.yaml", "."),
        ("sources.example.yaml", "."),
        ("LICENSE", "."),
        ("THIRD_PARTY_NOTICES.md", "."),
        ("app_version.txt", "."),
        ("release/sbom.cdx.json", "."),
        ("bin/mihomo.exe", "bin"),
        ("bin/wintun.dll", "bin"),
    )
]
for path in license_files:
    datas.append((str(path), str(Path("third-party-licenses") / path.parent.relative_to(license_root))))
for path in sorted((ROOT / "locales").glob("*.json")):
    datas.append((str(path), "locales"))

hiddenimports = [
    'tier3_endpoint_controller',
    'tier3_build_identity',
    'tier3_expansion_actor',
    'tier3_admission_commit_queue',
    'tier3_validation_domain',
    'tier3_validation_protocol',
    'tier3_validator_helper',
    'tier3_validator_prewarm',
    'tier3_validator_runtime',
    'tier3_validator_supervisor',
    'tier3_batch_validator_helper',
    'tier3_batch_validator_runtime',
    'tier3_batch_validator_supervisor',
    'tier3_batch_pipe_io',
    'tier3_windows_job',
    'aiohttp',
    'segno',
    'cryptography.hazmat.primitives.asymmetric.ed25519',
    'atomic_file',
    'desktop_runtime',
    'runtime_network_recovery',
    'bridge_audit',
    'bridge_cli',
    'bridge_instance_scope',
    'bridge_jobs',
    'bridge_logs',
    'bridge_main',
    'bridge_protocol',
    'bridge_publish',
    'bridge_redact',
    'bridge_runtime_access',
    'bridge_shutdown',
    'bridge_state',
    'bridge_traffic',
    'bridge_transport',
    # P2 sidecar modules (kept literal: tests/test_runtime_sidecar_spec.py
    # requires every bridge_*.py to be listed here).
    'bridge_alerts',
    'bridge_alerts_desktop',
    'bridge_alerts_history',
    'bridge_autostart',
    'bridge_catalog',
    'bridge_commands',
    'bridge_diagnostics',
    'bridge_exit',
    'bridge_exports',
    'bridge_import',
    'bridge_killswitch',
    'bridge_log_view',
    'bridge_messages',
    'bridge_node_commands',
    'bridge_nodes',
    'bridge_onboarding',
    'bridge_queries',
    'bridge_secrets',
    'bridge_settings',
    'bridge_settings_apply',
    'bridge_sources',
    'bridge_tiers',
    'bridge_update',
    'bridge_validation',
    'bridge_vocab',
    'bridge_wiring',
    'alert_desktop_policy',
    'log_run_boundary',
    'runtime_session_end',
    'runtime_settings_apply',
    'settings_effects',
    # Second-wave support module imported only inside functions
    # (bridge_diagnostics, DesktopRuntime.bridge_node_diagnostic_target).
    'tier3_origin',
    'scripts.tier3_audit_acceptance',
    'scripts.tier3_audit_activity',
    'scripts.tier3_audit_deadline',
    'scripts.tier3_audit_entry',
    'scripts.tier3_audit_environment',
    'scripts.tier3_audit_import_hooks',
    'scripts.tier3_audit_job_inventory',
    'scripts.tier3_audit_job_launch',
    'scripts.tier3_audit_liveness',
    'scripts.tier3_audit_process_census',
    'scripts.tier3_audit_process_registry',
    'scripts.tier3_audit_runtime_hook',
    'scripts.tier3_audit_socket_policy',
    'scripts.tier3_audit_socket_witness',
]

# Excluding package roots excludes every submodule, including future Qt modules.
excludes = [
    "PySide6", "shiboken6", "PySide2", "shiboken2", "PyQt6", "PyQt5",
    "PyQt4", "PySide", "qtpy", "qtconsole",
    "test_support", "apprise", "bridges", "email_outlook_profile",
    "notification_bridge", "signal_bridge", "telegram_user_bridge", "whatsapp_web_bridge",
]

# PyInstaller executes custom runtime hooks before standard hooks and the entry.
# This is the same first custom hook as Tesseract.spec, before freeze_support.
# T-P2-H7: the second seeds platform.uname() (no WMI / ``cmd /c ver``) before
# the standard pyi_rth_setuptools imports setuptools -> jaraco.context, which
# calls platform.system(). Tesseract.spec uses the same two hooks (T-P2-H9).
runtime_hooks = [str(ROOT / "scripts" / "tier3_audit_runtime_hook.py"),
                 str(ROOT / "scripts" / "sidecar_platform_seed_rthook.py")]

# Snapshot tracked inputs before Analysis; concurrent edits must not produce an
# identity whose source hashes describe a different revision from frozen code.
# The Qt finalizer hardcodes Tesseract.exe, so reuse only its atomic writer and
# backend component list. Never call finalize or write the source manifest.
from scripts.finalize_tier3_build_identity import COMPONENT_FILES, atomic_write_json, sha256
# T-P2-33: every repository file of the sidecar's static import closure is an
# identity component too, so "current" (frozen_build_current, the T-P2-32
# preflight) means the whole frozen closure matches the sources. The graph
# helper module imports no pytest.
from tests.test_bridge_qt_guard import sidecar_source_files

component_files = tuple(dict.fromkeys(
    [name for name in COMPONENT_FILES if name not in {
        "Tesseract.spec", "desktop_app.py", "ui_buttons.py",
    }]
    + ["tesseract-runtime.spec", "runtime_main.pyw", "tier3_build_identity.py",
       "runtime_network_recovery.py", "atomic_file.py",
       "sidecar_platform_seed.py", "scripts/sidecar_platform_seed_rthook.py"]
    + [path.name for path in sorted(ROOT.glob("bridge_*.py"))]
    + [str(path.relative_to(ROOT)).replace("\\", "/")
       for path in sorted((ROOT / "scripts").glob("tier3_audit_*.py"))]
    + sidecar_source_files()
))
components = {name: sha256(ROOT / name) for name in component_files}

a = Analysis(
    ["runtime_main.pyw"],
    pathex=[str(ROOT)], binaries=[], datas=datas,
    hiddenimports=hiddenimports, hookspath=[], hooksconfig={},
    runtime_hooks=runtime_hooks, excludes=excludes, noarchive=False, optimize=0,
)


def without_foreign_root_dependencies(entries):
    # Match the Qt spec's PATH-contamination filter without importing PySide6.
    result = []
    for entry in entries:
        name = str(entry[0]).replace("\\", "/").lower().lstrip("./")
        if "/" not in name and (
            name == "icuuc.dll"
            or (name.startswith("icudt") and name.endswith(".dll"))
            or name in {"libcrypto-3-x64.dll", "libssl-3-x64.dll"}
        ):
            continue
        result.append(entry)
    return result


a.binaries = without_foreign_root_dependencies(a.binaries)
a.datas = without_foreign_root_dependencies(a.datas)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="tesseract-runtime", contents_directory="_internal",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    # T-P6-01 (plan §9.2): GUI subsystem like the Qt exe, so the sidecar and every
    # helper it spawns start without a console. An uncaught exception must not
    # open PyInstaller's modal traceback box in a process without a window (it
    # would keep the sidecar alive behind the shell's hello timeout): the trace
    # goes to stderr instead (plan §2.2.1 step 5.3 gives the sidecar its own
    # data/logs/sidecar-stderr.log; until then it is lost under the shell).
    console=False, disable_windowed_traceback=True, uac_admin=False,
    icon=str(ROOT / "static" / "app_icon.ico"), version=VERSION_INFO,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="tesseract-runtime")

# Identity is finalized only after the last EXE write, and only for stable inputs.
changed = [name for name, digest in components.items() if sha256(ROOT / name) != digest]
if changed:
    raise RuntimeError(f"Sidecar inputs changed during build; rebuild required: {changed}")
material = "\n".join(f"{name}:{components[name]}" for name in component_files)
dist_root = Path(DISTPATH) / "tesseract-runtime"
executable = require_file(dist_root / "tesseract-runtime.exe", "sidecar executable")
executable_hash = sha256(executable)
payload = {
    "schema_version": 2,
    "build_id": hashlib.sha256(material.encode("ascii", "replace")).hexdigest(),
    "components": components,
    "declared_executable_sha256": executable_hash,
    "executable_sha256": executable_hash,
    "identity_match": True,
}
for path in (dist_root / "tier3_build_id.json", dist_root / "_internal" / "tier3_build_id.json"):
    atomic_write_json(path, payload)
    if json.loads(path.read_bytes()) != payload:
        raise RuntimeError(f"Sidecar identity readback mismatch: {path}")
if sha256(executable) != executable_hash:
    raise RuntimeError("Sidecar executable changed while finalizing identity")
print(f"Sidecar identity finalized: {executable_hash}")
