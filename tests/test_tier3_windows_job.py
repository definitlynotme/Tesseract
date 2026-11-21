from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tier3_windows_job import (
    WindowsJob,
    WindowsJobError,
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


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object test")
def test_a_process_joins_through_its_creators_handle_and_dies_with_the_job() -> None:
    # The base interpreter: the venv's python.exe would add a launcher process.
    process = subprocess.Popen(
        [getattr(sys, "_base_executable", sys.executable), "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    job = WindowsJob()
    try:
        job.assign_process_handle(int(process._handle))
        assert job.snapshot().process_ids == (process.pid,)
        # The handle stays the caller's: Popen can still wait on it.
        assert process.poll() is None
        # KILL_ON_JOB_CLOSE: closing the last handle to the job ends the process.
        job.close()
        process.wait(timeout=10.0)
        assert process.returncode is not None
    finally:
        if process.poll() is None:
            process.kill()
        job.close()


def test_assign_process_handle_refuses_a_non_handle() -> None:
    job = WindowsJob()
    try:
        with pytest.raises(WindowsJobError):
            job.assign_process_handle(0)
    finally:
        job.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows resource API test")
def test_resource_snapshots_report_commit_private_bytes_and_handles() -> None:
    system = system_resource_snapshot()
    process = process_resource_snapshot(os.getpid())
    assert system.commit_headroom_bytes > 0
    assert system.available_physical_bytes > 0
    assert process.private_bytes > 0
    assert process.handle_count > 0
