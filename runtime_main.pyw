import multiprocessing
import os

# Match the frozen runtime hook before freeze_support in source root/helpers.
if os.environ.get("TESSERACT_ISOLATED_AUDIT_MANIFEST"):
    from scripts.tier3_audit_entry import install_from_environment
    guard = install_from_environment()
    if guard is not None and guard.socket_guard_installed():
        guard._runtime_hook_pid = os.getpid()


# T-P2-H7: fill platform.uname()'s cache before any dependency imports it
# (aiohttp calls platform.system() at import: WMI, then ``cmd /c ver`` on a
# WMI timeout). Frozen, the custom runtime hook sidecar_platform_seed_rthook
# already did this before PyInstaller's standard hooks; then this is a no-op
# returning that first result. Standard library only; see the module.
from sidecar_platform_seed import ensure_seeded

_PLATFORM_SEED = ensure_seeded()


multiprocessing.freeze_support()


if __name__ == "__main__":
    from bridge_main import main

    raise SystemExit(main())
