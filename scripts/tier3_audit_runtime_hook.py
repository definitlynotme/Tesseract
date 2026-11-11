import os

if os.environ.get('TESSERACT_ISOLATED_AUDIT_MANIFEST'):
    from scripts.tier3_audit_entry import install_from_environment
    guard = install_from_environment()
    if guard is not None and guard.socket_guard_installed():
        guard._runtime_hook_pid = os.getpid()
