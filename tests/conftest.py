"""Suite-wide pytest hooks."""

import os


def pytest_runtest_logstart(nodeid, location):
    """Release workflow only (it sets RELEASE_PROGRESS_FILE): name the test that is
    starting, for scripts/ci_heartbeat.ps1. A hosted runner that goes silent keeps
    no log, so the heartbeat's last commit status is the only trace of where it was.
    Anywhere else this does nothing."""
    path = os.environ.get("RELEASE_PROGRESS_FILE")
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(nodeid)
    except OSError:
        pass
