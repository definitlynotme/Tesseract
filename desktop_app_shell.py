from __future__ import annotations

import argparse
import faulthandler
import json
import os
import sys
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from alert_config import disable_external_desktop_notifications
from app_info import APP_NAME, APP_WINDOWS_APP_ID
from app_paths import AppPaths, apply_release_path_overrides, bootstrap_user_files
from main import load_config


APP_ICON_FILENAMES = ("app_icon.ico", "app_icon.png", "app_icon.svg")
APP_LOGO_FILENAMES = ("app_logo.svg", "app_logo.png")
SINGLE_INSTANCE_MUTEX_NAME = "Local\\TesseractProxyManagerSingleInstance"
SINGLE_INSTANCE_RESTORE_EVENT_NAME = "Local\\TesseractProxyManagerRestoreWindow"
_SINGLE_INSTANCE_MUTEX_HANDLE: int | None = None
_SINGLE_INSTANCE_RESTORE_EVENT_HANDLE: int | None = None


def configure_windows_app_id() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_WINDOWS_APP_ID)
    except Exception:
        pass


def acquire_single_instance_lock(name: str = SINGLE_INSTANCE_MUTEX_NAME) -> bool:
    global _SINGLE_INSTANCE_MUTEX_HANDLE
    if sys.platform != "win32":
        return True
    if _SINGLE_INSTANCE_MUTEX_HANDLE:
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            return True
        if kernel32.GetLastError() == 183:
            kernel32.CloseHandle(handle)
            return False
        _SINGLE_INSTANCE_MUTEX_HANDLE = int(handle)
        return True
    except Exception:
        return True


def release_single_instance_lock() -> None:
    global _SINGLE_INSTANCE_MUTEX_HANDLE
    close_single_instance_restore_event()
    if sys.platform != "win32" or not _SINGLE_INSTANCE_MUTEX_HANDLE:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.CloseHandle(_SINGLE_INSTANCE_MUTEX_HANDLE)
    except Exception:
        pass
    _SINGLE_INSTANCE_MUTEX_HANDLE = None


def create_single_instance_restore_event(name: str = SINGLE_INSTANCE_RESTORE_EVENT_NAME) -> bool:
    global _SINGLE_INSTANCE_RESTORE_EVENT_HANDLE
    if sys.platform != "win32":
        return True
    if _SINGLE_INSTANCE_RESTORE_EVENT_HANDLE:
        return True
    try:
        import ctypes

        handle = ctypes.windll.kernel32.CreateEventW(None, False, False, name)
        if not handle:
            return False
        _SINGLE_INSTANCE_RESTORE_EVENT_HANDLE = int(handle)
        return True
    except Exception:
        return False


def close_single_instance_restore_event() -> None:
    global _SINGLE_INSTANCE_RESTORE_EVENT_HANDLE
    if sys.platform != "win32" or not _SINGLE_INSTANCE_RESTORE_EVENT_HANDLE:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.CloseHandle(_SINGLE_INSTANCE_RESTORE_EVENT_HANDLE)
    except Exception:
        pass
    _SINGLE_INSTANCE_RESTORE_EVENT_HANDLE = None


def signal_existing_instance(name: str = SINGLE_INSTANCE_RESTORE_EVENT_NAME) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        try:
            ctypes.windll.user32.AllowSetForegroundWindow(0xFFFFFFFF)
        except Exception:
            pass
        handle = kernel32.OpenEventW(0x0002, False, name)
        if not handle:
            return False
        try:
            return bool(kernel32.SetEvent(handle))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def consume_single_instance_restore_request() -> bool:
    if sys.platform != "win32" or not _SINGLE_INSTANCE_RESTORE_EVENT_HANDLE:
        return False
    try:
        import ctypes

        return ctypes.windll.kernel32.WaitForSingleObject(_SINGLE_INSTANCE_RESTORE_EVENT_HANDLE, 0) == 0
    except Exception:
        return False


def find_app_icon(resource_root: Path) -> Path | None:
    for filename in APP_ICON_FILENAMES:
        path = resource_root / "static" / filename
        if path.exists():
            return path
    return None


def find_brand_logo(resource_root: Path) -> Path | None:
    for filename in APP_LOGO_FILENAMES:
        path = resource_root / "static" / filename
        if path.exists():
            return path
    return None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=APP_NAME)
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument("--smoke-test", action="store_true", help="validate packaged resources and exit")
    parser.add_argument(
        "--node-card-native-smoke",
        action="store_true",
        help="rebuild node cards under the native Windows window manager and exit",
    )
    parser.add_argument(
        "--validator-helper-smoke",
        nargs="?",
        const=1,
        default=0,
        type=int,
        choices=range(1, 17),
        metavar="WORKERS",
        help="start 1-16 isolated Tier3 validator helpers and exit",
    )
    parser.add_argument(
        "--validator-helper-slots",
        default=1,
        type=int,
        choices=range(1, 17),
        metavar="SLOTS",
        help="worker identities per helper for --validator-helper-smoke (batch backend when > 1)",
    )
    parser.add_argument(
        "--tier3-validation-parity-audit",
        action="store_true",
        help="compare isolated helper and legacy Tier3 validation without admission",
    )
    parser.add_argument(
        "--tier3-parity-sample-size",
        type=int,
        default=24,
        metavar="COUNT",
        help="responsive candidates to sample for the Tier3 parity audit",
    )
    return parser.parse_args(argv)


def load_desktop_config(paths: AppPaths) -> dict:
    bootstrap_user_files(paths)
    config = load_config(str(paths.config_path), local_path=str(paths.local_config_path))
    config = apply_release_path_overrides(config, paths)
    return disable_external_desktop_notifications(config)


def run_validator_helper_smoke(
    paths: AppPaths,
    worker_count: int = 1,
    *,
    slots_per_process: int = 1,
) -> int:
    """Exercise frozen spawn, Job membership and bounded helper shutdown.

    ``worker_count`` counts helper processes.  With ``slots_per_process`` > 1
    the batch backend is started instead and every process must publish that
    many ready worker identities behind one Mihomo.
    """

    worker_count = max(1, min(16, int(worker_count)))
    slots_per_process = max(1, min(16, int(slots_per_process)))
    identity_count = worker_count * slots_per_process
    frozen = bool(getattr(sys, "frozen", False))
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    diagnostic_path = paths.data_dir / "validator-helper-smoke.json"
    trace_path = paths.data_dir / "validator-helper-bootstrap.jsonl"
    stack_path = paths.data_dir / "validator-helper-smoke-stacks.log"
    traceback_dumped = False

    def snapshot_payload(snapshot) -> dict:
        if snapshot is None:
            return {}
        if is_dataclass(snapshot):
            return asdict(snapshot)
        raw = getattr(snapshot, "__dict__", None)
        return dict(raw) if isinstance(raw, dict) else {"value": str(snapshot)}

    def write_stage(stage: str, snapshot=None) -> None:
        payload = {
            "stage": stage,
            "worker_count": worker_count,
            "slots_per_process": slots_per_process,
            "snapshot": snapshot_payload(snapshot),
        }
        temporary_path = diagnostic_path.with_suffix(".json.tmp")
        try:
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            temporary_path.replace(diagnostic_path)
        except OSError:
            pass

    def dump_failure_traceback() -> None:
        """Capture one synchronous stack snapshot without a native watchdog."""

        nonlocal traceback_dumped
        if traceback_dumped:
            return
        traceback_dumped = True
        try:
            with stack_path.open("w", encoding="utf-8") as stack_handle:
                # The caller holds the GIL here.  CPython 3.12's native
                # dump_traceback_later watchdog traverses live thread states
                # without it and can access-violate under frozen spawn load.
                faulthandler.dump_traceback(
                    file=stack_handle,
                    all_threads=True,
                )
                stack_handle.flush()
        except BaseException:
            # Failure diagnostics must never replace the original smoke error.
            pass

    write_stage("entered")
    try:
        stack_path.unlink(missing_ok=True)
    except OSError:
        pass
    if slots_per_process > 1:
        from tier3_batch_validator_supervisor import Tier3BatchValidatorSupervisor

        supervisor_factory = partial(
            Tier3BatchValidatorSupervisor,
            slots_per_process=slots_per_process,
        )
    else:
        from tier3_validator_supervisor import Tier3ValidatorSupervisor

        supervisor_factory = Tier3ValidatorSupervisor

    write_stage("supervisor_imported")
    try:
        trace_path.unlink(missing_ok=True)
    except OSError:
        pass
    previous_trace = os.environ.get("TESSERACT_VALIDATOR_BOOTSTRAP_TRACE")
    os.environ["TESSERACT_VALIDATOR_BOOTSTRAP_TRACE"] = str(trace_path)
    supervisor = supervisor_factory(
        binary_path=str(paths.mihomo_binary_path),
        probe_urls=["http://connectivitycheck.gstatic.com/generate_204"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=lambda _attempt: None,
        capacity_sink=lambda _count: None,
        min_workers=identity_count,
        max_workers=identity_count,
    )
    started = False
    error = ""
    last_snapshot = None
    try:
        write_stage("supervisor_starting")
        supervisor.start()
        started = True
        write_stage("supervisor_started")
        # A source-mode Windows spawn imports the desktop __main__ module
        # before entering the lightweight helper target. Frozen children are
        # intercepted by freeze_support and retain the stricter release gate.
        startup_seconds_per_worker = 2.0 if frozen else 4.0
        startup_timeout_seconds = max(
            10.0,
            worker_count * startup_seconds_per_worker,
        )
        deadline = time.monotonic() + startup_timeout_seconds
        next_stage_write_at = 0.0
        while time.monotonic() < deadline:
            snapshot = supervisor.snapshot()
            last_snapshot = snapshot
            if time.monotonic() >= next_stage_write_at:
                next_stage_write_at = time.monotonic() + 0.5
                write_stage("waiting_for_ready", snapshot)
            if snapshot.idle_workers == identity_count:
                if len(snapshot.workers) != identity_count:
                    raise RuntimeError("validator worker registry is incomplete")
                for worker in snapshot.workers:
                    if int(worker.get("helper_pid", 0) or 0) <= 0:
                        raise RuntimeError("validator helper PID was not published")
                    if int(worker.get("mihomo_pid", 0) or 0) <= 0:
                        raise RuntimeError("validator Mihomo PID was not published")
                helper_pids = {int(worker["helper_pid"]) for worker in snapshot.workers}
                if len(helper_pids) != worker_count:
                    raise RuntimeError(
                        f"expected {worker_count} validator helper processes, "
                        f"found {len(helper_pids)}"
                    )
                break
            if snapshot.hard_stall:
                raise RuntimeError(
                    snapshot.last_failure_reason or "validator helper hard stall"
                )
            time.sleep(0.05)
        else:
            raise TimeoutError(
                f"{worker_count} validator helpers ({identity_count} identities) "
                f"did not become ready within "
                f"{startup_timeout_seconds:.0f}s"
            )
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        dump_failure_traceback()
    finally:
        write_stage("stopping", last_snapshot)
        stop_status = (
            supervisor.stop(timeout_seconds=max(4.0, worker_count * 0.5))
            if started
            else "STOPPED"
        )
        if stop_status != "STOPPED":
            dump_failure_traceback()
        write_stage(f"stopped:{stop_status}", last_snapshot)
        if previous_trace is None:
            os.environ.pop("TESSERACT_VALIDATOR_BOOTSTRAP_TRACE", None)
        else:
            os.environ["TESSERACT_VALIDATOR_BOOTSTRAP_TRACE"] = previous_trace
    if error or stop_status != "STOPPED":
        payload = {
            "error": error,
            "stop_status": stop_status,
            "snapshot": snapshot_payload(last_snapshot),
        }
        temporary_path = diagnostic_path.with_suffix(".json.tmp")
        try:
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            temporary_path.replace(diagnostic_path)
        except OSError:
            pass
        if error:
            print(f"Validator helper smoke failed: {error}", file=sys.stderr)
        if stop_status != "STOPPED":
            print(
                f"Validator helper smoke failed to stop cleanly: {stop_status}",
                file=sys.stderr,
            )
        return 1
    try:
        diagnostic_path.unlink(missing_ok=True)
        trace_path.unlink(missing_ok=True)
        stack_path.unlink(missing_ok=True)
    except OSError:
        pass
    print(f"{APP_NAME} validator helper smoke test passed")
    return 0


def run_tier3_validation_parity_audit_cli(
    paths: AppPaths,
    *,
    sample_size: int = 24,
) -> int:
    from tier3_validation_parity_audit import run_tier3_validation_parity_audit

    artifact_path = paths.data_dir / "tier3-validation-parity-audit.json"
    try:
        config = load_desktop_config(paths)
        exit_code, result = run_tier3_validation_parity_audit(
            config=config,
            sources_path=paths.sources_path,
            mihomo_binary_path=paths.mihomo_binary_path,
            artifact_path=artifact_path,
            sample_size=max(1, min(64, int(sample_size))),
        )
    except BaseException as exc:
        failure = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "verdict": "audit_error",
            "sample_size": 0,
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:1000],
        }
        temporary_path = artifact_path.with_suffix(artifact_path.suffix + ".tmp")
        try:
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(
                json.dumps(failure, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            temporary_path.replace(artifact_path)
        except OSError:
            pass
        print(
            f"Tier3 validation parity audit failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    print(
        "Tier3 validation parity audit: "
        f"verdict={result.get('verdict')} "
        f"sample={result.get('sample_size')} "
        f"helper_pass={result.get('helper_pass_count', 0)} "
        f"oracle_pass={result.get('oracle_pass_count', 0)} "
        f"artifact={artifact_path}"
    )
    return int(exit_code)
