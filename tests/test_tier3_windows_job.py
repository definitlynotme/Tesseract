from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tier3_windows_job import (
    WindowsJob,
    process_resource_snapshot,
    system_resource_snapshot,
)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object test")
def test_job_termination_clears_assigned_process() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    job = WindowsJob()
    try:
        job.assign_pid(process.pid)
        assert process.pid in job.snapshot().process_ids
        job.terminate(74)
        assert job.wait_empty(1.0)
        process.wait(timeout=1.0)
        assert process.returncode is not None
    finally:
        if process.poll() is None:
            process.kill()
        job.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows resource API test")
def test_resource_snapshots_report_commit_private_bytes_and_handles() -> None:
    system = system_resource_snapshot()
    process = process_resource_snapshot(os.getpid())
    assert system.commit_headroom_bytes > 0
    assert system.available_physical_bytes > 0
    assert process.private_bytes > 0
    assert process.handle_count > 0
