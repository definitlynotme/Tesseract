"""T-P2-27: update install handoff (prepareExit / exitResponse / installResult).

All updater, exit and publication collaborators are doubles; nothing here
touches the network, the registry, a real installer or the live profile.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
from time import monotonic
from types import SimpleNamespace

import pytest

import app_update_state as state
import app_updater
import bridge_protocol as protocol
import bridge_update as module
from bridge_commands import COMMAND_SPECS
from bridge_jobs import JobEventRecorder, JobExecutor, JobFailure
from bridge_shutdown import ShutdownCoordinator
from bridge_state import PublicationState
from bridge_update import UpdateOwner

FIXTURE = Path(__file__).resolve().parents[1] / "bridge_schema" / "fixtures" / "v1" / "valid" / "topic_update.prepareExit.json"
INSTALLER_BYTES = b"verified fake installer bytes"


def release(version="2.0.0", *, available=True):
    def asset(name):
        return app_updater.ReleaseAsset(name, "https://example.invalid/" + name)
    return app_updater.AppUpdateInfo(
        "1.0.0", version, "https://example.invalid/releases", "Release notes",
        asset(f"Tesseract-Setup-{version}-win-x64.exe"), asset("SHA256SUMS.txt"),
        available, asset("SHA256SUMS.txt.sig"),
    )


class FakeUpdater:
    """Writes real bytes whose SHA-256 matches the returned receipt."""

    def __init__(self):
        self.info = release()
        self.checks = 0
        self.downloads = 0
        self.release_check: Event | None = None
        self.check_entered = Event()
        self.release_download: Event | None = None
        self.download_entered = Event()

    def check_latest_update(self, *, current_version):
        self.checks += 1
        self.check_entered.set()
        if self.release_check is not None:
            assert self.release_check.wait(3)
        return self.info

    def download_update(self, info, download_dir):
        self.downloads += 1
        self.download_entered.set()
        if self.release_download is not None:
            assert self.release_download.wait(3)
        download_dir.mkdir(parents=True, exist_ok=True)
        path = download_dir / info.installer.name
        path.write_bytes(INSTALLER_BYTES)
        return app_updater.DownloadedUpdate(
            info, path, hashlib.sha256(INSTALLER_BYTES).hexdigest(), "sha256:" + "b" * 64, True,
        )


class FakePublisher:
    def __init__(self):
        self.frames: list[dict] = []
        self.failure: Exception | None = None

    def publish_prepare_exit(self, data):
        if self.failure is not None:
            raise self.failure
        assert protocol.validate_topic_payload("update.prepareExit", data) == []
        self.frames.append(deepcopy(data))


class FakeExitPort:
    """Read-only exit observation; the sidecar never requests an exit."""

    def __init__(self):
        self.reads = 0
        self.started: bool | Exception = False

    def exit_started(self):
        self.reads += 1
        if isinstance(self.started, Exception):
            raise self.started
        return self.started

    def request_quit(self, *args, **kwargs):  # pragma: no cover - must not exist in use
        raise AssertionError("the sidecar must not request its own update exit")


@pytest.fixture(autouse=True)
def forbid_installer_launch(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Python must never launch the installer or touch the network")
    for name in ("check_latest_update", "download_update", "launch_update_installer",
                 "download_and_launch_update", "urlopen"):
        monkeypatch.setattr(app_updater, name, forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "startfile", forbidden, raising=False)


@pytest.fixture
def env(tmp_path):
    updater, publisher, exit_port = FakeUpdater(), FakePublisher(), FakeExitPort()
    now = [100.0]
    owner = UpdateOwner(state.update_state_path(tmp_path), tmp_path / "downloads",
                        current_version="1.0.0", updater=updater, clock=lambda: now[0],
                        wall_ms=lambda: 1_790_812_800_000 + int(now[0] * 1000))
    owner.attach_handoff(publisher, exit_port)
    return owner, updater, publisher, exit_port, now


def dto(owner):
    data = owner.snapshot()["data"]
    assert protocol.validate_topic_payload("update.state", data) == []
    return data


def installed(owner):
    owner.check()
    return owner.install()


# ---- contract / fixture ------------------------------------------------------

def test_prepare_exit_fixture_carries_shell_launch_facts_and_validates():
    frame = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert protocol.validate_frame(frame) == []
    assert frame["data"]["args"] == list(module.INSTALLER_ARGS)
    for field, value, error in (
            ("installer_path", "updates\\Tesseract-Setup-1.2.3-win-x64.exe", "installer_path"),
            ("installer_path", "C:\\updates\\..\\Tesseract-Setup-1.2.3-win-x64.exe", "installer_path"),
            ("installer_path", "C:\\updates\\Tesseract-Setup-9.9.9-win-x64.exe", "installer_path"),
            ("sha256", "A" * 64, "sha256"), ("sha256", "0" * 63, "sha256"),
            # Drive-letter only: no UNC share, no \\?\ or \\.\ device prefix.
            ("installer_path", r"\\server\share\Tesseract-Setup-1.2.3-win-x64.exe", "installer_path"),
            ("installer_path", r"\\?\C:\updates\Tesseract-Setup-1.2.3-win-x64.exe", "installer_path"),
            ("installer_path", r"\\.\C:\updates\Tesseract-Setup-1.2.3-win-x64.exe", "installer_path"),
            ("args", [], "args"), ("args", ["/S"] * 17, "args"),
            # Switches only: never a path, shell text or a valued switch.
            ("args", [r"C:\x\a.exe", "--x"], "args"), ("args", ["& calc"], "args"),
            ("args", ["/SILENT", r"/DIR=C:\x"], "args"), ("args", ["/silent"], "args"),
            ("args", ["/SILENT", 3], "args")):
        bad = deepcopy(frame)
        bad["data"][field] = value
        assert f"invariant:update.prepareExit.{error}" in protocol.validate_frame(bad)
    for field in ("installer_path", "sha256", "args"):
        missing = deepcopy(frame)
        del missing["data"][field]
        assert f"missing:update.prepareExit.{field}" in protocol.validate_frame(missing)
    # Shell-only: page can neither subscribe nor request its snapshot.
    assert "update.prepareExit" in protocol.SHELL_TOPICS
    assert "update.prepareExit" not in protocol.V1_SNAPSHOT_TOPICS


@pytest.mark.skipif(sys.platform != "win32", reason="launch_update_installer is Windows-only")
def test_installer_args_match_app_updater_launch_argv(tmp_path, monkeypatch):
    monkeypatch.undo()  # Real launcher with a capturing popen double only.
    path = tmp_path / "Tesseract-Setup-2.0.0-win-x64.exe"
    path.write_bytes(INSTALLER_BYTES)
    downloaded = app_updater.DownloadedUpdate(
        release(), path, hashlib.sha256(INSTALLER_BYTES).hexdigest(), "sha256:" + "b" * 64, True)
    calls = []
    argv = app_updater.launch_update_installer(downloaded, popen=lambda args, **kw: calls.append(args))
    assert calls == [argv]
    assert argv[0] == str(path) and tuple(argv[1:]) == module.INSTALLER_ARGS


# ---- happy path / publication --------------------------------------------------

def test_install_publishes_handoff_with_receipt_path_digest_args(env):
    owner, updater, publisher, exit_port, now = env
    result = installed(owner)
    assert result["verified"] and result["handoff_pending"]
    assert dto(owner)["state"] == "handoff" and dto(owner)["error"] is None
    [data] = publisher.frames
    receipt = owner.resolve_receipt(result["receipt_ref"])
    assert data["receipt_ref"] == result["receipt_ref"] and data["version"] == "2.0.0"
    assert data["verified"] is True
    assert data["expires_at_ms"] == 1_790_812_800_000 + int((now[0] + 30) * 1000)
    assert Path(data["installer_path"]).is_absolute()
    assert Path(data["installer_path"]) == Path(os.path.abspath(receipt["installer_path"]))
    assert Path(data["installer_path"]).read_bytes() == INSTALLER_BYTES
    assert data["sha256"] == hashlib.sha256(INSTALLER_BYTES).hexdigest() == receipt["installer_sha256"]
    assert data["args"] == list(module.INSTALLER_ARGS)
    assert exit_port.reads == 0  # Exit is only observed after an accepted takeover.


def test_real_publication_state_records_shell_edge_frame(env, tmp_path):
    owner, *_ = env
    publication = PublicationState("p227")
    publication.subscribe(["update.prepareExit"])
    owner.attach_handoff(module.StateHandoffPublisher(publication), FakeExitPort())
    owner._wall_ms = lambda: monotonic_wall()
    installed(owner)
    [event] = [item for item in publication.pending() if item["topic"] == "update.prepareExit"]
    frame = {**event, "sent_at_ms": event["produced_at_ms"]}
    assert protocol.validate_frame(frame) == []
    assert event["kind"] == "edge"


def monotonic_wall():
    import time
    return time.time_ns() // 1_000_000


def test_unwired_owner_keeps_p2_26_behaviour(tmp_path):
    updater = FakeUpdater()
    owner = UpdateOwner(state.update_state_path(tmp_path), tmp_path / "d",
                        current_version="1.0.0", updater=updater)
    installed(owner)
    assert dto(owner)["state"] == "verified" and owner._handoff is None


# ---- exitResponse ------------------------------------------------------------

def test_declined_exit_returns_to_verified_and_reinstall_reuses_receipt(env):
    owner, updater, publisher, exit_port, _ = env
    receipt_ref = installed(owner)["receipt_ref"]
    first = publisher.frames[0]["handoff_id"]
    assert owner.exit_response(first, False, "user_declined") == {"recorded": True}
    data = dto(owner)
    assert data["state"] == "verified" and data["error"]["code"] == "cancelled"
    assert exit_port.reads == 0
    with pytest.raises(JobFailure) as caught:
        owner.exit_response(first, True)  # Consumed handoff cannot be revived.
    assert caught.value.code == "conflict"
    again = owner.install()
    assert again["receipt_ref"] == receipt_ref and updater.downloads == 1
    second = publisher.frames[1]["handoff_id"]
    assert second != first and dto(owner)["state"] == "handoff"


def test_accepted_exit_only_records_takeover_then_observes_shell_quit(env):
    owner, updater, publisher, exit_port, now = env
    installed(owner)
    handoff_id = publisher.frames[0]["handoff_id"]
    # PROTOCOL §13: accepted=true is a takeover only; the shell sends app.quit.
    assert owner.exit_response(handoff_id, True) == {"recorded": True}
    assert dto(owner)["state"] == "handoff" and dto(owner)["error"] is None
    for call in (owner.check, owner.install):
        with pytest.raises(JobFailure) as caught:
            call()
        assert caught.value.code == "conflict"
    with pytest.raises(JobFailure):
        owner.exit_response(handoff_id, True)  # No second accept.
    now[0] += 29  # Within the exit window the shell's app.quit begins the exit.
    exit_port.started = True
    assert not owner.poll_handoff() and owner._exit_observed
    reads = exit_port.reads
    now[0] += 3600  # Once the exit began the lock stays until the process ends.
    assert not owner.poll_handoff() and dto(owner)["state"] == "handoff"
    assert exit_port.reads == reads  # Observed once; no further port reads.
    for call in (owner.check, owner.install):
        with pytest.raises(JobFailure) as caught:
            call()
        assert caught.value.code == "conflict"
    assert updater.checks == 1


@pytest.mark.parametrize("started", [False, RuntimeError("coordinator not wired yet")])
def test_accepted_without_shell_quit_withdraws_takeover_for_retry(env, started):
    owner, updater, publisher, exit_port, now = env
    receipt_ref = installed(owner)["receipt_ref"]
    exit_port.started = started  # An unreadable port is "not started", never an exit.
    assert owner.exit_response(publisher.frames[0]["handoff_id"], True) == {"recorded": True}
    now[0] += 29.99
    assert not owner.poll_handoff() and dto(owner)["state"] == "handoff"
    now[0] += .01
    assert owner.poll_handoff()
    data = dto(owner)
    assert data["state"] == "verified" and data["error"]["code"] == "lifecycle_busy"
    assert owner.resolve_receipt(receipt_ref) is not None
    # Nothing was requested from the coordinator, so a retry is genuinely possible.
    exit_port.started = False
    assert owner.install()["receipt_ref"] == receipt_ref and updater.downloads == 1
    assert owner.exit_response(publisher.frames[1]["handoff_id"], True) == {"recorded": True}
    exit_port.started = True
    assert not owner.poll_handoff() and dto(owner)["state"] == "handoff"


def test_withdrawal_is_also_enforced_lazily_by_check(env):
    owner, updater, publisher, _, now = env
    installed(owner)
    owner.exit_response(publisher.frames[0]["handoff_id"], True)
    now[0] += 30
    assert owner.check()["state"] == "verified" and updater.checks == 2


def test_exit_started_keeps_handoff_even_if_quit_is_later_refused(env):
    """Unit-level S1: the coordinator's quitting flag is permanent, so a quit
    whose stop intent the runtime refuses still ends this epoch; the owner
    honestly stays locked and refuses a same-epoch installResult."""
    owner, _, publisher, exit_port, now = env
    receipt_ref = installed(owner)["receipt_ref"]
    owner.exit_response(publisher.frames[0]["handoff_id"], True)
    exit_port.started = True
    owner.poll_handoff()
    exit_port.started = False  # A port can never "un-start"; ignored anyway.
    now[0] += 3600
    assert not owner.poll_handoff() and dto(owner)["state"] == "handoff"
    with pytest.raises(JobFailure) as caught:
        owner.install_result(receipt_ref, False, "spawn_failed")
    assert caught.value.code == "conflict"
    assert not hasattr(owner, "exit_aborted")


def test_unknown_handoff_id_is_conflict(env):
    owner, *_ = env
    installed(owner)
    with pytest.raises(JobFailure) as caught:
        owner.exit_response("update-handoff-forged", True)
    assert caught.value.code == "conflict" and dto(owner)["state"] == "handoff"


# ---- shell disappears ----------------------------------------------------------

def test_shell_gone_handoff_expires_to_verified_and_late_reply_conflicts(env):
    owner, updater, publisher, exit_port, now = env
    receipt_ref = installed(owner)["receipt_ref"]
    handoff_id = publisher.frames[0]["handoff_id"]
    now[0] += 29.99
    assert not owner.poll_handoff() and dto(owner)["state"] == "handoff"
    now[0] += .01
    assert owner.poll_handoff()
    data = dto(owner)
    assert data["state"] == "verified" and data["error"]["code"] == "timeout"
    with pytest.raises(JobFailure) as caught:
        owner.exit_response(handoff_id, True)
    assert caught.value.code == "conflict" and exit_port.reads == 0
    assert owner.resolve_receipt(receipt_ref) is not None  # Receipt retained.
    assert owner.install()["receipt_ref"] == receipt_ref and updater.downloads == 1


def test_expiry_is_also_enforced_lazily_without_tick(env):
    owner, _, publisher, exit_port, now = env
    installed(owner)
    now[0] += 31
    with pytest.raises(JobFailure):
        owner.exit_response(publisher.frames[0]["handoff_id"], True)
    assert exit_port.reads == 0 and dto(owner)["state"] == "verified"


def test_tick_expires_publishes_state_and_retries_failed_record(env):
    owner, _, _, _, now = env
    installed(owner)
    owner.take_state()
    now[0] += 30
    published, fail = [], [True]

    def record_state(topic, data):
        if fail and fail.pop():
            raise ValueError("queue full")
        published.append((topic, data))

    owner.tick(record_state, lambda work: False)
    assert published == []
    owner.tick(record_state, lambda work: False)
    assert published == [("update.state", dto(owner))]
    assert published[0][1]["state"] == "verified"


# ---- publication failure -------------------------------------------------------

def test_prepare_exit_publication_failure_keeps_verified_receipt(env):
    owner, updater, publisher, _, _ = env
    owner.check()
    publisher.failure = ValueError("invalid edge DTO")
    with pytest.raises(JobFailure) as caught:
        owner.install()
    assert caught.value.code == "io"
    data = dto(owner)
    assert data["state"] == "verified" and data["error"]["code"] == "io"
    assert owner._handoff is None and owner._installing is False
    publisher.failure = None
    owner.install()
    assert updater.downloads == 1 and dto(owner)["state"] == "handoff"


# ---- installResult -------------------------------------------------------------

def test_installer_launch_failure_records_failed_and_keeps_receipt(env):
    """Unit-level only (S2): on the real wiring the shell's app.quit closes
    this epoch first, so installResult arrives in a new epoch (next test)."""
    owner, updater, publisher, exit_port, _ = env
    receipt_ref = installed(owner)["receipt_ref"]
    owner.exit_response(publisher.frames[0]["handoff_id"], True)
    assert owner.install_result(receipt_ref, False, "spawn_failed") == {"recorded": True}
    data = dto(owner)
    assert data["state"] == "failed" and data["error"]["code"] == "io"
    assert owner.last_install_result() == {"receipt_ref": receipt_ref, "launched": False,
                                           "reason_code": "spawn_failed"}
    assert owner.resolve_receipt(receipt_ref) is not None
    assert owner.install()["receipt_ref"] == receipt_ref and updater.downloads == 1
    assert len(publisher.frames) == 2


def test_installer_launch_failure_after_restart_resolves_durable_receipt(env, tmp_path):
    """The live path: sidecar exited, the shell failed to launch, new epoch."""
    owner, updater, publisher, _, _ = env
    receipt_ref = installed(owner)["receipt_ref"]
    # New epoch after the sidecar exited: a fresh owner with no memory.
    fresh_publisher = FakePublisher()
    fresh = UpdateOwner(owner.state_path, owner.download_dir, current_version="1.0.0", updater=updater)
    fresh.attach_handoff(fresh_publisher, FakeExitPort())
    assert fresh.install_result(receipt_ref, False, "Bad Reason!\nC:\\x") == {"recorded": True}
    assert dto(fresh)["state"] == "failed"
    assert fresh.last_install_result()["reason_code"] == "unrecognized"
    with pytest.raises(JobFailure) as caught:
        fresh.install()  # No release facts yet: a check must run first.
    assert caught.value.code == "not_allowed"
    # A done check result must carry error=null, so the kept receipt reads as
    # plain verified; the launch failure stays in last_install_result().
    assert fresh.check()["state"] == "verified" and dto(fresh)["error"] is None
    assert fresh.last_install_result()["launched"] is False
    assert fresh.install()["receipt_ref"] == receipt_ref and updater.downloads == 1
    assert fresh_publisher.frames[0]["receipt_ref"] == receipt_ref


def test_an_epoch_after_a_failed_launch_keeps_saying_so_until_the_user_checks(env):
    """11-20: the automatic check would turn failed (检查或安装更新失败, 前往发布页) into a plain
    verified, which reads 安装包已就绪 ✓ with a busy 更新已就绪 although nothing is installing."""
    owner, updater, _, _, _ = env
    receipt_ref = installed(owner)["receipt_ref"]
    now = [500.0]
    fresh = UpdateOwner(owner.state_path, owner.download_dir, current_version="1.0.0",
                        updater=updater, clock=lambda: now[0])
    fresh.attach_handoff(FakePublisher(), FakeExitPort())
    fresh.on_ready()
    assert fresh.install_result(receipt_ref, False, "spawn_failed") == {"recorded": True}
    now[0] += 6
    checks = updater.checks
    assert not fresh.poll_auto_check(lambda work: pytest.fail("no automatic check after a failed launch"))
    assert not fresh.poll_auto_check(lambda work: pytest.fail("and none later in this epoch"))
    data = dto(fresh)
    assert (data["state"], data["error"]["code"], updater.checks) == ("failed", "io", checks)
    # 检查更新 brings the kept installer back, and 立即更新 hands it off again without a download.
    downloads = updater.downloads
    assert fresh.check()["state"] == "verified"
    assert fresh.install()["receipt_ref"] == receipt_ref and updater.downloads == downloads


def test_a_failed_launch_reported_after_the_check_was_queued_still_skips_it(env):
    """The second review (11-20): the skip is asked again when the worker runs the check."""
    owner, updater, _, _, _ = env
    receipt_ref = installed(owner)["receipt_ref"]
    now = [500.0]
    fresh = UpdateOwner(owner.state_path, owner.download_dir, current_version="1.0.0",
                        updater=updater, clock=lambda: now[0])
    fresh.attach_handoff(FakePublisher(), FakeExitPort())
    fresh.on_ready()
    now[0] += 6
    work = []
    assert fresh.poll_auto_check(lambda callback: work.append(callback) or True)  # queued first
    assert fresh.install_result(receipt_ref, False, "spawn_failed") == {"recorded": True}
    checks = updater.checks
    work.pop()()  # the worker runs the queued check now
    data = dto(fresh)
    assert (data["state"], data["error"]["code"], updater.checks) == ("failed", "io", checks)


def test_the_automatic_check_decides_where_it_opens_its_flight(env):
    """The third review (11-20): the skip is decided in the critical section that opens the flight."""
    owner, updater, _, _, _ = env
    receipt_ref = installed(owner)["receipt_ref"]
    fresh = UpdateOwner(owner.state_path, owner.download_dir, current_version="1.0.0", updater=updater)
    fresh.attach_handoff(FakePublisher(), FakeExitPort())
    assert fresh.install_result(receipt_ref, False, "spawn_failed") == {"recorded": True}
    checks = updater.checks
    assert fresh.check(automatic=True) is None and updater.checks == checks
    assert dto(fresh)["state"] == "failed"
    # the user's own check is never skipped
    assert fresh.check()["state"] == "verified" and updater.checks == checks + 1


def test_an_epoch_without_a_failed_launch_still_checks_automatically(env):
    owner, updater, _, _, now = env
    receipt_ref = installed(owner)["receipt_ref"]
    owner.on_ready()
    assert owner.install_result(receipt_ref, True, None) == {"recorded": True}  # launched: no failure
    now[0] += 6
    work = []
    assert owner.poll_auto_check(lambda callback: work.append(callback) or True) and len(work) == 1


def test_install_result_unknown_or_unreadable_receipt(env, monkeypatch):
    owner, *_ = env
    receipt_ref = installed(owner)["receipt_ref"]
    owner.exit_response(owner._handoff["id"], False)
    with pytest.raises(JobFailure) as caught:
        owner.install_result("update-receipt-forged", False)  # Another receipt is in memory.
    assert caught.value.code == "conflict"
    fresh = UpdateOwner(owner.state_path, owner.download_dir, current_version="1.0.0", updater=FakeUpdater())
    with pytest.raises(JobFailure) as caught:
        fresh.install_result("update-receipt-forged", False)  # Not the durable receipt.
    assert caught.value.code == "conflict"
    real_read = Path.read_text
    reads = []

    def denied(path, *args, **kwargs):
        if path == owner.state_path:
            reads.append(path)
            raise PermissionError("denied")
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(JobFailure) as caught:
        fresh.install_result(receipt_ref, False)
    assert caught.value.code == "io" and len(reads) == 1  # One strict read only.
    assert dto(fresh)["state"] == "idle" and fresh.last_install_result() is None


def test_launched_true_is_recorded_without_state_change(env):
    owner, _, publisher, _, _ = env
    receipt_ref = installed(owner)["receipt_ref"]
    owner.exit_response(publisher.frames[0]["handoff_id"], True)
    assert owner.install_result(receipt_ref, True) == {"recorded": True}
    assert dto(owner)["state"] == "handoff"
    assert owner.last_install_result()["launched"] is True


def test_late_install_result_cannot_clear_a_newer_unanswered_handoff(env):
    """SHOULD-1: decline H1, re-install H2, then a late/duplicate launched=false."""
    owner, updater, publisher, _, _ = env
    receipt_ref = installed(owner)["receipt_ref"]
    owner.exit_response(publisher.frames[0]["handoff_id"], False)
    owner.install()
    second = publisher.frames[1]["handoff_id"]
    with pytest.raises(JobFailure) as caught:
        owner.install_result(receipt_ref, False, "late")
    assert caught.value.code == "conflict" and owner.last_install_result() is None
    assert dto(owner)["state"] == "handoff"
    assert owner.exit_response(second, True) == {"recorded": True}
    assert updater.downloads == 1


def test_install_result_conflicts_with_in_flight_check_and_keeps_receipt(env):
    owner, updater, publisher, _, _ = env
    receipt_ref = installed(owner)["receipt_ref"]
    owner.exit_response(publisher.frames[0]["handoff_id"], False)
    updater.release_check = Event()
    updater.check_entered.clear()
    with ThreadPoolExecutor(2) as pool:
        checking = pool.submit(owner.check)
        assert updater.check_entered.wait(2)
        with pytest.raises(JobFailure) as caught:
            owner.install_result(receipt_ref, False, "spawn_failed")
        assert caught.value.code == "conflict"
        updater.release_check.set()
        assert checking.result(3)["state"] == "verified"
    assert owner._receipt_ref == receipt_ref and owner.last_install_result() is None
    assert owner.install()["receipt_ref"] == receipt_ref and updater.downloads == 1


def test_install_result_conflicts_with_in_flight_install(env):
    owner, updater, publisher, _, _ = env
    receipt_ref = installed(owner)["receipt_ref"]
    owner.exit_response(publisher.frames[0]["handoff_id"], False)
    Path(publisher.frames[0]["installer_path"]).write_bytes(b"tampered")  # Forces a download.
    updater.release_download = Event()
    updater.download_entered.clear()
    with ThreadPoolExecutor(2) as pool:
        installing = pool.submit(owner.install)
        assert updater.download_entered.wait(2)
        with pytest.raises(JobFailure) as caught:
            owner.install_result(receipt_ref, False, "spawn_failed")
        assert caught.value.code == "conflict"
        updater.release_download.set()
        new_ref = installing.result(3)["receipt_ref"]
    assert new_ref != receipt_ref and dto(owner)["state"] == "handoff"
    assert owner.last_install_result() is None


def test_cross_restart_install_result_conflicts_with_automatic_check(env):
    owner, updater, _, _, _ = env
    receipt_ref = installed(owner)["receipt_ref"]
    fresh = UpdateOwner(owner.state_path, owner.download_dir, current_version="1.0.0", updater=updater)
    fresh.attach_handoff(FakePublisher(), FakeExitPort())
    updater.release_check = Event()
    updater.check_entered.clear()
    with ThreadPoolExecutor(2) as pool:
        checking = pool.submit(fresh.check)
        assert updater.check_entered.wait(2)
        with pytest.raises(JobFailure) as caught:
            fresh.install_result(receipt_ref, False, "spawn_failed")
        assert caught.value.code == "conflict"
        updater.release_check.set()
        assert checking.result(3)["state"] == "available"  # No receipt in memory yet.
    # The shell's retry after the check lands normally.
    assert fresh.install_result(receipt_ref, False, "spawn_failed") == {"recorded": True}
    assert dto(fresh)["state"] == "failed"


def test_tampered_installer_bytes_force_redownload_before_handoff(env):
    owner, updater, publisher, _, _ = env
    receipt_ref = installed(owner)["receipt_ref"]
    owner.exit_response(publisher.frames[0]["handoff_id"], False)
    Path(publisher.frames[0]["installer_path"]).write_bytes(b"tampered")
    result = owner.install()
    assert updater.downloads == 2 and result["receipt_ref"] != receipt_ref
    assert owner.resolve_receipt(receipt_ref) is None
    assert Path(publisher.frames[1]["installer_path"]).read_bytes() == INSTALLER_BYTES


# ---- repeated install / concurrency with check -------------------------------

def test_repeated_install_during_handoff_and_download_conflicts(env):
    owner, updater, publisher, _, _ = env
    owner.check()
    updater.release_download = Event()
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(owner.install)
        assert updater.download_entered.wait(2)
        for call in (owner.install, owner.check):
            with pytest.raises(JobFailure) as caught:
                call()
            assert caught.value.code == "conflict"
        updater.release_download.set()
        first.result(3)
    with pytest.raises(JobFailure) as caught:
        owner.install()
    assert caught.value.code == "conflict"
    assert updater.downloads == 1 and len(publisher.frames) == 1


def test_install_conflicts_with_in_flight_check_and_vice_versa(env):
    owner, updater, publisher, _, _ = env
    owner.check()
    owner.install()
    owner.exit_response(publisher.frames[0]["handoff_id"], False)
    updater.release_check = Event()
    updater.check_entered.clear()
    with ThreadPoolExecutor(2) as pool:
        checking = pool.submit(owner.check)
        assert updater.check_entered.wait(2)
        with pytest.raises(JobFailure) as caught:
            owner.install()
        assert caught.value.code == "conflict"
        updater.release_check.set()
        assert checking.result(3)["state"] == "verified"
    assert len(publisher.frames) == 1
    owner.install()
    assert dto(owner)["state"] == "handoff" and updater.downloads == 1


def test_check_is_refused_while_handoff_pending(env):
    owner, updater, *_ = env
    installed(owner)
    with pytest.raises(JobFailure) as caught:
        owner.check()
    assert caught.value.code == "conflict" and updater.checks == 1


# ---- registration through the real executor -----------------------------------

class FakeSession:
    def __init__(self, executor, publication, shutdown):
        self.executor, self.state, self.shutdown = executor, publication, shutdown


class FakeShutdown:
    """Exposes only the read the adapter may use; request_quit must not run."""

    def __init__(self):
        self.quitting = False

    def request_quit(self, *, disconnected=False):
        raise AssertionError("the sidecar must not request its own update exit")


def test_register_wires_four_commands_and_shell_replies_on_control(env):
    owner, updater, _, _, _ = env
    executor = JobExecutor(JobEventRecorder())
    publication = PublicationState("p227")
    publication.subscribe(["update.prepareExit"])
    shutdown = FakeShutdown()
    owner._wall_ms = monotonic_wall
    module.register(FakeSession(executor, publication, shutdown), owner)
    try:
        assert {"app.update.check", "app.update.install", "update.exitResponse",
                "update.installResult"} <= set(executor._specs)
        assert COMMAND_SPECS["update.exitResponse"].domain == "control"
        for index, name in enumerate(("app.update.check", "app.update.install")):
            ack = executor.submit(name, {}, owner="page", req_id=f"r{index}")
            assert ack.accepted and executor.acceptance_sent(ack.op)
            assert executor.wait(ack.op, 3)["state"] == "done"
        [event] = [e for e in publication.pending() if e["topic"] == "update.prepareExit"]
        handoff_id = event["data"]["handoff_id"]
        receipt = executor.submit("update.exitResponse", {"handoff_id": handoff_id, "accepted": True, "reason_code": None},
                                  owner="shell", req_id="s1")
        assert receipt.accepted
        response = receipt.wait(3)
        assert response.ok and response.data == {"recorded": True}
        bad = executor.submit("update.exitResponse", {"handoff_id": handoff_id, "accepted": True, "reason_code": None},
                              owner="shell", req_id="s2").wait(3)
        assert not bad.ok and bad.error["code"] == "conflict"
        shutdown.quitting = True  # The adapter reads the coordinator lazily.
        owner.tick(lambda topic, data: None, lambda work: False)
        assert owner._exit_observed and dto(owner)["state"] == "handoff"
        assert updater.downloads == 1
    finally:
        assert executor.shutdown(monotonic() + 2)


def test_register_before_shutdown_exists_reads_as_not_started(env):
    owner, _, publisher, _, now = env
    session = FakeSession(JobExecutor(JobEventRecorder()), PublicationState("p227"), None)
    module.register(session, owner, publisher=publisher)
    try:
        installed(owner)
        owner.exit_response(publisher.frames[0]["handoff_id"], True)
        assert not owner.poll_handoff() and not owner._exit_observed
        now[0] += 30
        assert owner.poll_handoff() and dto(owner)["error"]["code"] == "lifecycle_busy"
    finally:
        assert session.executor.shutdown(monotonic() + 2)


# ---- MUST-1 / M1: the real exit coordinator on the real executor --------------

class _StopRuntime:
    def __init__(self, accept):
        self.accept, self.actors = accept, []

    def request_stop(self, actor):
        self.actors.append(actor)
        return SimpleNamespace(accepted=self.accept, generation=1, lifecycle_revision=1,
                               skipped_reason=None if self.accept else "lifecycle_busy")


def _real_coordinator(executor, runtime):
    access = SimpleNamespace(runtime=runtime, request_stop=lambda **kw: None,
                             stop=lambda **kw: {"startup_phase": "stopped", "shutdown_exit_code": None,
                                                "cleanup_pending": False})
    return ShutdownCoordinator(
        executor, access, lease=object(),
        startup_recovery=SimpleNamespace(gate=SimpleNamespace(require_idle=lambda: None)),
        recover_network=lambda budget: None, send=lambda frame: None, exit_process=lambda code: None)


@pytest.mark.parametrize("quit_accepted", [True, False])
def test_real_coordinator_exit_response_is_recorded_not_runtime_gone(env, quit_accepted):
    """Real ShutdownCoordinator + JobExecutor(require_acceptance=True): the
    accepted exitResponse reaches the shell as ok {recorded:true}; only the
    shell's own app.quit (here: the coordinator's public request_quit, which
    submits app.quit and calls prepare_quit) starts the exit."""
    owner, updater, _, _, _ = env
    executor = JobExecutor(JobEventRecorder(4096), require_acceptance=True, terminal_capacity=512)
    publication = PublicationState("p227")
    publication.subscribe(["update.prepareExit"])
    runtime = _StopRuntime(quit_accepted)
    coordinator = _real_coordinator(executor, runtime)
    owner._wall_ms = monotonic_wall
    module.register(SimpleNamespace(executor=executor, state=publication, shutdown=coordinator), owner)
    try:
        for index, name in enumerate(("app.update.check", "app.update.install")):
            ack = executor.submit(name, {}, owner="page", req_id=f"r{index}")
            assert ack.accepted and executor.acceptance_sent(ack.op)
            assert executor.wait(ack.op, 3)["state"] == "done"
        [event] = [e for e in publication.pending() if e["topic"] == "update.prepareExit"]
        response = executor.submit(
            "update.exitResponse", {"handoff_id": event["data"]["handoff_id"], "accepted": True,
                                    "reason_code": None}, owner="shell", req_id="s1").wait(3)
        assert response is not None and response.ok, response and response.error
        assert response.data == {"recorded": True}
        assert not coordinator.quitting and runtime.actors == []  # Sidecar asked nothing.
        owner.tick(lambda topic, data: None, lambda work: False)
        assert not owner._exit_observed
        assert coordinator.request_quit().accepted  # The shell's app.quit{reason:update}.
        owner.tick(lambda topic, data: None, lambda work: False)
        assert owner._exit_observed and dto(owner)["state"] == "handoff"
        coordinator._quit_ack.wait_stop_intent(3)
        assert runtime.actors == ["app_exit"]
        # Refused or not, quitting is permanent: the epoch is ending (S1).
        assert coordinator.quitting and dto(owner)["state"] == "handoff"
        assert updater.downloads == 1
    finally:
        executor.shutdown(monotonic() + 2)


# ---- 11-18: the shell's own frames through the real ingress -------------------
# Every test above hands update.exitResponse straight to the executor, with
# reason_code=None. The shell built its acceptance without the key, and
# BridgeSession.dispatch refused that frame before any handler ran
# (missing:args.reason_code). So 0.1.0-0.1.3 never reached app.quit{reason:update}
# while all of these tests passed. The tests below go through dispatch, frame by
# frame, exactly as shell/app/src/update.rs (request_frame, response) builds them.

from bridge_main import BridgeSession


class _WirePipe:
    def __init__(self):
        self.sent = []

    def send(self, frame):
        assert protocol.validate_frame(frame) == [], frame
        self.sent.append(deepcopy(frame))
        return "complete"

    def poll_write(self):
        return "complete"


@pytest.fixture
def wire(env):
    owner, updater, publisher, _, _ = env
    gate = SimpleNamespace(require_idle=lambda: None)
    session = BridgeSession(None, SimpleNamespace(gate=gate), None, "epoch-1118",
                            runtime_factory=lambda *_: SimpleNamespace())
    session.transport = _WirePipe()
    session.publisher = SimpleNamespace(pending_write=False, next_tick=monotonic() + 100, tick=lambda: None)
    session.shutdown = SimpleNamespace(quitting=False)
    owner._wall_ms = monotonic_wall
    # As bridge_wiring wires it: the four commands on the session's own executor.
    module.register(session, owner, publisher=publisher)
    yield session, owner, publisher
    session._ingress_stop.set()
    for worker in session._ingress_threads:
        worker.join(1)
    session.executor.shutdown(monotonic() + 2)


def _shell_request(session, ident, cmd, args):
    """update.rs request_frame, stamped like main.rs stamp()."""
    return session.envelope("req", id=ident, cmd=cmd, args=args, req_id=f"shell-update-{ident}",
                            owner="shell", **{"from": "shell"})


def _reply(session, ident):
    deadline = monotonic() + 3
    while monotonic() < deadline:
        session.pump()
        for frame in session.transport.sent:
            if frame.get("id") == ident and frame.get("t") in {"res", "error"}:
                return frame
    raise AssertionError(f"no reply to request {ident}: {session.transport.sent}")


@pytest.mark.parametrize("args_of", [
    # The fixed shell (update.rs response): reason_code is always there, null on an acceptance.
    lambda handoff_id: {"handoff_id": handoff_id, "accepted": True, "reason_code": None},
    # Exactly what 0.1.0-0.1.3 sent; the sidecar takes it too now (reason_code may be left out).
    lambda handoff_id: {"handoff_id": handoff_id, "accepted": True},
], ids=["shell_fixed", "shell_0.1.3"])
def test_the_shells_acceptance_frame_is_recorded_through_dispatch(wire, args_of):
    session, owner, publisher = wire
    installed(owner)
    handoff_id = publisher.frames[0]["handoff_id"]
    frame = _shell_request(session, 41, "update.exitResponse", args_of(handoff_id))
    assert protocol.validate_frame(frame) == []
    session.dispatch(frame)
    reply = _reply(session, 41)
    assert (reply["t"], reply.get("ok"), reply.get("data")) == ("res", True, {"recorded": True}), reply
    assert owner._handoff["responded"] is True  # the takeover is recorded: the shell may quit now
    assert dto(owner)["state"] == "handoff"


def test_the_shells_decline_and_install_result_frames_are_recorded_through_dispatch(wire):
    session, owner, publisher = wire
    installed(owner)
    first = publisher.frames[0]
    session.dispatch(_shell_request(session, 51, "update.exitResponse",
                                    {"handoff_id": first["handoff_id"], "accepted": False, "reason_code": "hash_mismatch"}))
    assert _reply(session, 51)["data"] == {"recorded": True}
    assert dto(owner)["state"] == "verified" and dto(owner)["error"]["code"] == "cancelled"
    # The kept receipt is handed off again; accepted, then the launch fails (update.rs take_failure).
    owner.install()
    second = publisher.frames[1]
    assert second["receipt_ref"] == first["receipt_ref"]
    session.dispatch(_shell_request(session, 52, "update.exitResponse",
                                    {"handoff_id": second["handoff_id"], "accepted": True, "reason_code": None}))
    assert _reply(session, 52)["data"] == {"recorded": True}
    session.dispatch(_shell_request(session, 53, "update.installResult",
                                    {"receipt_ref": second["receipt_ref"], "launched": False, "reason_code": "launch_failed"}))
    assert _reply(session, 53)["data"] == {"recorded": True}
    assert dto(owner)["state"] == "failed"


def test_the_shells_update_quit_frame_is_valid():
    # main.rs: app.quit is a P1 command, sent without req_id/owner.
    frame = {"t": "req", "id": 9, "cmd": "app.quit", "args": {"reason": "update"}, "from": "shell",
             "epoch": "e", "produced_at_ms": 1, "sent_at_ms": 1}
    assert protocol.validate_frame(frame) == []


# ---- 11-19: only the installed copy updates in the app -----------------------------

def test_installed_copy_is_the_folder_the_installer_registered():
    loc = lambda: "C:\\Program Files\\Tesseract\\"  # InstallLocation keeps Inno's trailing backslash
    assert module.installed_copy(r"C:\Program Files\Tesseract\tesseract-runtime.exe", frozen=True, install_location=loc)
    assert module.installed_copy(r"c:\program files\tesseract\tesseract-runtime.exe", frozen=True, install_location=loc)
    # the portable zip: another folder, or no installed copy at all
    assert not module.installed_copy(r"D:\Tools\Tesseract\tesseract-runtime.exe", frozen=True, install_location=loc)
    assert not module.installed_copy(r"D:\Tools\Tesseract\tesseract-runtime.exe", frozen=True, install_location=lambda: None)
    # source runs keep the release's own answer
    assert module.installed_copy(r"D:\src\bridge_main.py", frozen=False, install_location=lambda: None)


def test_installed_copy_reads_the_verbatim_path_the_shell_starts_the_sidecar_with():
    # 11-20: the shell starts the sidecar through fs::canonicalize (main.rs finish_options), so a
    # frozen sys.executable is \\?\C:\... (seen in the 11-19 e2e trace of a real install). Compared
    # as spelled, every installed copy looked portable and none offered 立即更新.
    loc = lambda: "C:\\Program Files\\Tesseract\\"
    assert module.installed_copy(r"\\?\C:\Program Files\Tesseract\tesseract-runtime.exe", frozen=True, install_location=loc)
    assert module.installed_copy(r"\\?\c:\PROGRAM FILES\Tesseract\tesseract-runtime.exe", frozen=True, install_location=loc)
    share = lambda: "\\\\nas\\apps\\Tesseract\\"
    assert module.installed_copy(r"\\?\UNC\nas\apps\Tesseract\tesseract-runtime.exe", frozen=True, install_location=share)
    # a portable copy stays portable under the verbatim spelling too
    assert not module.installed_copy(r"\\?\D:\Tools\Tesseract\tesseract-runtime.exe", frozen=True, install_location=loc)


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions")
def test_installed_copy_knows_the_installed_folder_under_another_spelling(tmp_path):
    import _winapi
    real = tmp_path / "Tesseract"
    real.mkdir()
    link = tmp_path / "linked"
    _winapi.CreateJunction(str(real), str(link))
    try:
        exe = str(link / "tesseract-runtime.exe")
        registered = lambda: str(real) + "\\"
        assert module.installed_copy(exe, frozen=True, install_location=registered)
        assert module.installed_copy("\\\\?\\" + exe, frozen=True, install_location=registered)
        # a registered folder that is gone: portable, not an error
        assert not module.installed_copy(exe, frozen=True, install_location=lambda: str(tmp_path / "gone") + "\\")
    finally:
        os.rmdir(link)  # the junction only, never its target


def test_installed_copy_never_stats_a_share_before_hello(monkeypatch):
    # 11-20: installed_copy runs before hello; a stat of a share that is gone can outlast the shell's
    # 10 s hello timeout, so another spelling is looked up by identity on local drives only.
    monkeypatch.setattr(module.os.path, "samefile", lambda *a: pytest.fail("no stat of a remote folder"))
    share = lambda: "\\\\nas\\apps\\Tesseract\\"
    assert not module.installed_copy(r"D:\Tools\Tesseract\tesseract-runtime.exe", frozen=True, install_location=share)
    # an installed copy on the share still matches by its spelling, with no stat at all
    assert module.installed_copy(r"\\?\UNC\nas\apps\Tesseract\tesseract-runtime.exe", frozen=True, install_location=share)
    assert module._drive_type("C:\\Program Files\\Tesseract\\") == 3 and module._drive_type(r"\\?\C:\x") == 3
    assert module._drive_type("\\\\nas\\apps\\Tesseract\\") is None and module._drive_type(r"\\?\UNC\nas\apps") is None
    assert module._mapped_share("C:") is None, "a local drive maps to no share"


def test_installed_copy_on_a_mapped_network_drive_compares_by_its_share(monkeypatch):
    # The third review (11-20): with InstallLocation Z:\Apps\Tesseract\ (Z: = \\nas\apps), canonicalize
    # starts the sidecar as \\?\UNC\nas\apps\Apps\Tesseract\...; a remote drive is never stat'ed.
    monkeypatch.setattr(module, "_drive_type", lambda path: 4 if path.upper().startswith("Z:") else 3)
    monkeypatch.setattr(module, "_mapped_share", lambda drive: "\\\\nas\\apps" if drive.upper() == "Z:" else None)
    monkeypatch.setattr(module.os.path, "samefile", lambda *a: pytest.fail("no stat of a remote folder"))
    mapped = lambda: "Z:\\Apps\\Tesseract\\"
    assert module.installed_copy(r"\\?\UNC\nas\apps\Apps\Tesseract\tesseract-runtime.exe", frozen=True, install_location=mapped)
    assert module.installed_copy(r"Z:\Apps\Tesseract\tesseract-runtime.exe", frozen=True, install_location=mapped)
    assert not module.installed_copy(r"\\?\UNC\nas\apps\Tools\Tesseract\tesseract-runtime.exe", frozen=True, install_location=mapped)
    assert not module.installed_copy(r"D:\Tools\Tesseract\tesseract-runtime.exe", frozen=True, install_location=mapped)
    # the drive is no longer mapped: portable, and still no stat
    monkeypatch.setattr(module, "_mapped_share", lambda drive: None)
    assert not module.installed_copy(r"\\?\UNC\nas\apps\Apps\Tesseract\tesseract-runtime.exe", frozen=True, install_location=mapped)
    assert module._mapped_share_path("Z:\\x\\") is None


def test_the_installed_copy_question_waits_for_the_first_check(tmp_path):
    """The final review (11-20): installed_copy() may stat a folder, so it is never asked before
    hello (wiring passes the function); the first check asks it once, on its worker."""
    calls = []
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir(), second.mkdir()
    owner = UpdateOwner(state.update_state_path(first), first / "downloads", current_version="1.0.0",
                        updater=FakeUpdater(), installed_copy=lambda: calls.append(1) or True)
    assert calls == [] and dto(owner)["installable"] is False
    owner.check()
    assert calls == [1] and dto(owner)["installable"] is True
    owner.check()
    assert calls == [1], "asked once"
    # a probe that fails answers portable and never fails the check
    broken = UpdateOwner(state.update_state_path(second), second / "downloads", current_version="1.0.0",
                         updater=FakeUpdater(), installed_copy=lambda: 1 / 0)
    assert broken.check()["installable"] is False
    # That the wiring hands over the function, unasked: test_app_update_routes.py
    # test_the_wiring_never_asks_the_installed_copy_question_before_hello.


@pytest.mark.skipif(os.name != "nt", reason="Windows error modes")
def test_the_identity_lookup_never_shows_a_no_disk_box(monkeypatch):
    import ctypes
    before = ctypes.windll.kernel32.GetThreadErrorMode()
    with module._no_critical_error_box():
        assert ctypes.windll.kernel32.GetThreadErrorMode() & 0x0001
    assert ctypes.windll.kernel32.GetThreadErrorMode() == before
    # installed_copy's own lookup runs inside it: a card reader without a card fails at once.
    seen = []

    def samefile(a, b):
        seen.append(ctypes.windll.kernel32.GetThreadErrorMode() & 0x0001)
        raise OSError("no media")
    monkeypatch.setattr(module, "_drive_type", lambda path: 2)  # DRIVE_REMOVABLE
    monkeypatch.setattr(module.os.path, "samefile", samefile)
    assert not module.installed_copy(r"D:\x\tesseract-runtime.exe", frozen=True, install_location=lambda: "E:\\Tesseract\\")
    assert seen == [1] and ctypes.windll.kernel32.GetThreadErrorMode() == before


def test_a_portable_copy_offers_the_release_page_not_the_installer(tmp_path):
    updater = FakeUpdater()
    owner = UpdateOwner(state.update_state_path(tmp_path), tmp_path / "downloads", current_version="1.0.0",
                        updater=updater, installed_copy=False)
    owner.check()
    data = dto(owner)
    assert (data["update_available"], data["installable"]) == (True, False)
    with pytest.raises(JobFailure) as caught:
        owner.install()
    assert caught.value.code == "not_allowed" and updater.downloads == 0
