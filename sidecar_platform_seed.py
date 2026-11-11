"""T-P2-H7: fill platform.uname()'s cache without WMI or ``cmd /c ver``.

Standard library only: this module runs before any dependency is imported.

Who calls platform.system() before the sidecar's hello:

- source and frozen: aiohttp.helpers at import (desktop_runtime -> app_paths
  -> tier3_sources -> aiohttp);
- frozen only, and earlier still: PyInstaller's standard runtime hook
  pyi_rth_setuptools imports setuptools, whose vendored jaraco.context calls
  platform.system() at import (setuptools -> dist -> _entry_points ->
  jaraco.text -> jaraco.context).

On CPython 3.12 that runs a WMI query of Win32_OperatingSystem and
Win32_Processor; when WMI times out (OSError 258) platform falls back to
``cmd /c ver``. Both block the sidecar before hello, past the shell's 10 s
window.

Callers, each before any dependency is imported:

- frozen: the custom runtime hook scripts/sidecar_platform_seed_rthook.py,
  listed in tesseract-runtime.spec after tier3_audit_runtime_hook.py.
  PyInstaller runs custom runtime hooks first, in list order, then the
  standard ones (pyi_rth_setuptools among them), then runtime_main.pyw;
- source, and again (as a no-op) frozen: runtime_main.pyw right after its
  audit guard, before freeze_support, so multiprocessing children seed too.

``ensure_seeded()`` runs the seed once per process and keeps that first
result, which ``status()`` reports (bridge_main logs it). The stdlib computes
uname() itself, but with its WMI query failing fast and its ``ver`` step
answered in-process from the registry (CurrentMajorVersionNumber.
CurrentMinorVersionNumber.CurrentBuildNumber, the value WMI reports; on
failure the stdlib default, i.e. sys.getwindowsversion().platform_version).
The rest are the stdlib's own non-WMI fallbacks: the client/server release
table with getwindowsversion().product_type, the CurrentType registry value,
socket.gethostname(), PROCESSOR_ARCHITEW6432/PROCESSOR_ARCHITECTURE and
PROCESSOR_IDENTIFIER. Both originals are restored before return, so later
win32_ver()/platform.platform() calls and an audit guard's wrapper of
_syscmd_ver are untouched; only the uname cache stays filled.

Python dependency (checked on 3.12.7, locked by tests/test_bridge_h7.py):
platform._uname_cache, platform._wmi_query (3.12+), platform._syscmd_ver
and the lazily cached uname_result.processor. If any is missing, the cache
is already filled, or the result lacks release/version, nothing is kept and
the stdlib behaves exactly as before. Used by the sidecar (T-P2-H7) and, as a
user-approved bug fix under D15, by the Qt app too (desktop_app.pyw,
Tesseract.spec; T-P2-H9).
"""
import sys

_STATUS = None


def seed_platform_uname():
    """One seeding attempt; returns ``"seeded"`` or ``"skipped:<why>"``."""
    if sys.platform != "win32":
        return "skipped:not_windows"
    import platform
    if not all(hasattr(platform, name) for name in ("_uname_cache", "_wmi_query", "_syscmd_ver")):
        return "skipped:unsupported"
    if platform._uname_cache is not None:
        return "skipped:cached"
    wmi_query, syscmd_ver = platform._wmi_query, platform._syscmd_ver

    def no_wmi(*_args):
        raise OSError("WMI skipped at sidecar startup")

    def no_ver(system="", release="", version="", *_args, **_kwargs):
        # The registry values WMI's Win32_OperatingSystem.Version reports
        # (10.0.<CurrentBuildNumber>), independent of the exe's manifest.
        try:
            import winreg
            key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
            with winreg.OpenKeyEx(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                parts = [int(winreg.QueryValueEx(key, name)[0]) for name in (
                    "CurrentMajorVersionNumber", "CurrentMinorVersionNumber",
                    "CurrentBuildNumber")]
        except (OSError, ValueError, TypeError):
            return system, release, version
        return system, release, ".".join(map(str, parts))

    platform._wmi_query, platform._syscmd_ver = no_wmi, no_ver
    try:
        result = platform.uname()
        result.processor
        if result.system != "Windows" or not (result.release and result.version and result.machine):
            platform._uname_cache = None
            return "skipped:incomplete"
    except Exception:
        platform._uname_cache = None
        return "skipped:error"
    finally:
        platform._wmi_query, platform._syscmd_ver = wmi_query, syscmd_ver
    return "seeded"


def ensure_seeded():
    """Seed once per process; later calls return the first attempt's result."""
    global _STATUS
    if _STATUS is None:
        _STATUS = seed_platform_uname()
    return _STATUS


def status():
    """The first attempt's result, or ``"not_run"``."""
    return "not_run" if _STATUS is None else _STATUS
