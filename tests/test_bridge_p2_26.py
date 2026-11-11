from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
from threading import Event, Lock, current_thread
from time import monotonic

import pytest

import app_update_state as state
import app_updater
import bridge_protocol as protocol
import bridge_update as module
from bridge_commands import COMMAND_SPECS, adapt_result
from bridge_jobs import JobEventRecorder, JobExecutor, JobFailure
from bridge_update import UpdateOwner


def release(version="2.0.0", *, available=True):
    def asset(name):
        return app_updater.ReleaseAsset(name, "https://example.invalid/" + name)
    return app_updater.AppUpdateInfo(
        "1.0.0", version, "https://example.invalid/releases", "Release notes",
        asset(f"Tesseract-Setup-{version}-win-x64.exe"), asset("SHA256SUMS.txt"),
        available, asset("SHA256SUMS.txt.sig"),
    )


class FakeUpdater:
    def __init__(self):
        self.info = release()
        self.failure = None
        self.download_failure = None
        self.checks = 0
        self.downloads = 0
        self.verified = True
        self.entered = Event()
        self.release_check = None
        self.download_entered = Event()
        self.release_download = None
        self.threads = []

    def check_latest_update(self, *, current_version):
        assert current_version == "1.0.0"
        self.checks += 1
        self.threads.append(current_thread().name)
        self.entered.set()
        if self.release_check is not None:
            assert self.release_check.wait(3)
        if self.failure is not None:
            raise self.failure
        return self.info

    def download_update(self, info, download_dir):
        self.downloads += 1
        self.threads.append(current_thread().name)
        self.download_entered.set()
        if self.release_download is not None:
            assert self.release_download.wait(3)
        if self.download_failure is not None:
            raise self.download_failure
        download_dir.mkdir(parents=True, exist_ok=True)
        path = download_dir / info.installer.name
        path.write_bytes(b"fake installer")
        return app_updater.DownloadedUpdate(
            info, path, "a" * 64, "sha256:" + "b" * 64, self.verified,
        )


@pytest.fixture(autouse=True)
def forbid_real_updater(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("real network/updater/installer entry must not run")
    for name in ("check_latest_update", "download_update", "launch_update_installer", "urlopen"):
        monkeypatch.setattr(app_updater, name, forbidden)


@pytest.fixture
def setup_owner(tmp_path):
    updater = FakeUpdater()
    now = [100.0]
    owner = UpdateOwner(state.update_state_path(tmp_path), tmp_path / "downloads",
                        current_version="1.0.0", updater=updater, clock=lambda: now[0])
    return owner, updater, now


def restart(owner, updater):
    return UpdateOwner(owner.state_path, owner.download_dir,
                       current_version="1.0.0", updater=updater)


def delivered(owner):
    candidate = owner.take_notice_candidate()
    assert candidate is not None
    assert state.notice_due(owner.state_path, candidate["version"])
    owner.record_notice_delivered(candidate["version"], delivery_id=candidate["delivery_id"])
    return candidate


def test_ready_six_seconds_one_check_no_failure_retry_manual_still_works(setup_owner):
    owner, updater, now = setup_owner
    work = []
    submit = lambda callback: work.append(callback) or True
    now[0] = 999
    assert not owner.poll_auto_check(submit)  # Before ready is not a deadline.
    owner.on_ready()
    now[0] += 5.99
    owner.on_ready()  # Repeated ready does not move the deadline.
    assert not owner.poll_auto_check(submit)
    now[0] += .01
    assert owner.poll_auto_check(submit)
    assert updater.checks == 0  # Tick submits; it never performs network work.
    updater.failure = app_updater.AppUpdateError("failure")
    work.pop()()
    assert updater.checks == 1
    assert owner.snapshot()["data"]["state"] == "failed"
    now[0] += 600
    assert not owner.poll_auto_check(submit)
    updater.failure = None
    assert owner.check_handler({}, None)["state"] == "available"
    assert updater.checks == 2


def test_executor_refusal_does_not_schedule_retries(setup_owner):
    owner, updater, now = setup_owner
    owner.on_ready()
    now[0] += 6
    assert not owner.poll_auto_check(lambda work: False)
    assert not owner.poll_auto_check(lambda work: pytest.fail("unexpected retry"))
    assert owner.check()["update_available"]


@pytest.mark.parametrize("failure", [None, app_updater.AppUpdateError("no network")])
def test_automatic_and_manual_checks_join_one_flight(setup_owner, monkeypatch, failure):
    owner, updater, _ = setup_owner
    joined = Event()

    class ObservedFuture(Future):
        def result(self, *args, **kwargs):
            joined.set()
            return super().result(*args, **kwargs)

    monkeypatch.setattr(module, "Future", ObservedFuture)
    updater.release_check = Event()
    updater.failure = failure
    with ThreadPoolExecutor(2) as pool:
        automatic = pool.submit(owner._automatic_check)
        try:
            assert updater.entered.wait(2)
            manual = pool.submit(owner.check)
            assert joined.wait(2)  # Ensure the second caller joined before release.
        finally:
            updater.release_check.set()
        automatic.result(2)
        if failure:
            with pytest.raises(JobFailure) as caught:
                manual.result(2)
            assert caught.value.code == "io"
        else:
            assert manual.result(2)["version"] == "2.0.0"
    assert updater.checks == 1


def test_success_persists_only_after_delivery_keeps_update_entry(setup_owner):
    owner, updater, _ = setup_owner
    dto = owner.check()
    assert not owner.state_path.exists()
    candidate = delivered(owner)
    assert not state.notice_due(owner.state_path, " v2.0.0 ")
    # T-P4-C2 #10: last_check_at_ms is each check's own finish time.
    same = lambda result: {k: v for k, v in result.items() if k != "last_check_at_ms"}
    assert same(owner.check()) == same(dto)
    assert owner.take_notice_candidate() is None
    other = restart(owner, updater)
    assert same(other.check()) == same(dto)
    assert other.take_notice_candidate() is None
    assert owner.snapshot()["data"]["update_available"] is True
    with pytest.raises(ValueError):
        owner.record_notice_delivered(candidate["version"], delivery_id=candidate["delivery_id"])


@pytest.mark.parametrize("outcome", ["failed", "dropped", "lost_before_ack"])
def test_failed_or_lost_delivery_reoffered_next_start(setup_owner, outcome):
    owner, updater, _ = setup_owner
    owner.check()
    candidate = owner.take_notice_candidate()
    if outcome != "lost_before_ack":
        owner.notice_not_delivered(candidate["delivery_id"])
    assert state.notice_due(owner.state_path, candidate["version"])
    owner.check()  # Manual checks do not re-offer this epoch's notice.
    assert owner.take_notice_candidate() is None
    other = restart(owner, updater)
    other.check()
    retry = other.take_notice_candidate()
    assert retry["version"] == candidate["version"]
    assert retry["delivery_id"] != candidate["delivery_id"]


def test_wrong_delivery_or_version_cannot_record(setup_owner):
    owner, _, _ = setup_owner
    owner.check()
    candidate = owner.take_notice_candidate()
    with pytest.raises(ValueError):
        owner.record_notice_delivered("3.0.0", delivery_id=candidate["delivery_id"])
    with pytest.raises(ValueError):
        owner.record_notice_delivered(candidate["version"], delivery_id="unrelated")
    assert not owner.state_path.exists()


def test_a_b_a_no_duplicate_and_manual_always_returns_latest(setup_owner):
    owner, updater, _ = setup_owner
    for version in ("2.0.0", "3.0.0"):
        updater.info = release(version)
        assert owner.check()["version"] == version
        delivered(owner)
    updater.info = release("2.0.0")
    owner = restart(owner, updater)
    assert owner.check()["version"] == "2.0.0"
    assert owner.take_notice_candidate() is None


def test_seventeenth_version_evicts_oldest_normalized_recent_order(tmp_path):
    path = state.update_state_path(tmp_path)
    for i in range(17):
        state.record_notice_delivered(path, f" v2.0.{i} ")
    payload = state._read_state(path)
    assert payload["noticed_versions"] == [f"2.0.{i}" for i in range(1, 17)]
    assert state.notice_due(path, "v2.0.0")
    assert not state.notice_due(path, "v2.0.1")
    state.record_notice_delivered(path, "v2.0.1")
    assert state._read_state(path)["noticed_versions"][-1] == "2.0.1"
    assert len(state._read_state(path)["noticed_versions"]) == 16


def test_qt_writers_keep_new_and_unknown_fields(setup_owner):
    owner, updater, _ = setup_owner
    state.record_notice_delivered(owner.state_path, "v2.0.0")
    payload = state._read_state(owner.state_path)
    payload["future_compatibility"] = {"keep": [1, 2]}
    state._write_state(owner.state_path, payload)
    downloaded = updater.download_update(updater.info, owner.download_dir)
    state.record_pending_update(owner.state_path, downloaded)
    assert state._read_state(owner.state_path)["noticed_versions"] == ["2.0.0"]
    assert state.completed_update_notice(owner.state_path, "2.0.0") is not None
    state.mark_update_notice_shown(owner.state_path, "2.0.0")
    payload = state._read_state(owner.state_path)
    assert payload["noticed_versions"] == ["2.0.0"]
    assert payload["future_compatibility"] == {"keep": [1, 2]}
    assert "pending" not in payload


@pytest.mark.parametrize("extra", [{}, {"noticed_versions": None},
                                  {"noticed_versions": [None, {}, "", " v2.0.0 ", "2.0.0"]}])
def test_legacy_or_malformed_notice_collection_is_safe(tmp_path, extra):
    path = state.update_state_path(tmp_path)
    state._write_state(path, {"schema_version": 1, "shown_versions": [], **extra})
    assert state.notice_due(path, "3.0.0")
    state.record_notice_delivered(path, "v3.0.0")
    assert not state.notice_due(path, "3.0.0")
    assert not state.notice_due(path, "")


def test_old_binary_losing_field_causes_one_renewed_notice_not_crash(setup_owner):
    owner, updater, _ = setup_owner
    owner.check()
    delivered(owner)
    # Simulate an older Qt binary rebuilding only its known root fields.
    state._write_state(owner.state_path, {"schema_version": 1, "shown_versions": []})
    owner = restart(owner, updater)
    assert owner.check()["state"] == "available"
    delivered(owner)
    owner = restart(owner, updater)
    owner.check()
    assert owner.take_notice_candidate() is None


@pytest.mark.parametrize("failure,code", [
    (app_updater.AppUpdateError("https://secret.invalid/credentials"), "io"),
    (TimeoutError("timed out"), "timeout"), (RuntimeError("unexpected"), "internal"),
])
def test_check_failure_retains_available_info_and_safe_error(setup_owner, failure, code):
    owner, updater, _ = setup_owner
    old = owner.check()
    updater.failure = failure
    with pytest.raises(JobFailure) as caught:
        owner.check()
    dto = owner.snapshot()["data"]
    assert dto == caught.value.partial_result
    assert dto["state"] == "failed" and dto["error"]["code"] == code
    for field in ("version", "update_available", "installable"):
        assert dto[field] == old[field]
    assert protocol.validate_topic_payload("update.state", dto) == []
    assert str(failure) not in json.dumps(dto)
    updater.failure = None
    assert owner.check()["error"] is None


def test_update_dto_schema_and_detached_publication(setup_owner):
    owner, updater, _ = setup_owner
    assert protocol.validate_topic_payload("update.state", owner.snapshot()["data"]) == []
    frame = owner.take_state()
    frame["data"]["state"] = "bad"
    assert owner.snapshot()["data"]["state"] == "idle"
    assert owner.take_state() is None
    for info in (release(), replace(release(), checksum_signature=None), release(available=False)):
        updater.info = info
        dto = owner.check()
        # T-P4-C1 #7: optional v1 additions current_version / previous_version;
        # T-P4-C2 #10: notes / enabled / last_check_at_ms.
        assert set(dto) == {"state", "version", "update_available", "installable", "error",
                            "current_version", "previous_version",
                            "notes", "enabled", "last_check_at_ms"}
        assert protocol.validate_topic_payload("update.state", dto) == []
        assert adapt_result("app.update.check", dto)[0] is True
        assert owner.take_state()["data"] == dto


def test_install_verified_durable_opaque_receipt_no_launch(setup_owner, monkeypatch):
    owner, updater, _ = setup_owner
    owner.check()
    delivered(owner)
    replacements = []
    real_replace = state.os.replace
    monkeypatch.setattr(state.os, "replace", lambda *args: replacements.append(args) or real_replace(*args))
    result = owner.install_handler({}, None)
    assert set(result) == {"verified", "handoff_pending", "receipt_ref"}
    assert result["verified"] is True and result["handoff_pending"] is True
    assert adapt_result("app.update.install", result)[0] is True
    assert len(replacements) == 1
    assert owner.snapshot()["data"]["state"] == "verified"
    assert not state.notice_due(owner.state_path, "2.0.0")
    receipt = restart(owner, updater).resolve_receipt(result["receipt_ref"])
    assert receipt["installer_path"] == owner.download_dir / updater.info.installer.name
    assert receipt["manifest_verified"] is True
    assert owner.resolve_receipt("unknown") is None
    assert owner.check()["state"] == "verified"
    updater.info = release("3.0.0")
    assert owner.check()["state"] == "available"


@pytest.mark.parametrize("failure", ["unsigned", "download", "receipt_write"])
def test_install_failure_never_reports_verified_or_returns_receipt(setup_owner, monkeypatch, failure):
    owner, updater, _ = setup_owner
    owner.check()
    if failure == "unsigned":
        updater.verified = False
    elif failure == "download":
        updater.download_failure = app_updater.AppUpdateError("checksum failure")
    else:
        def denied(*args):
            raise OSError("write denied")
        monkeypatch.setattr(state.os, "replace", denied)
    with pytest.raises(JobFailure) as caught:
        owner.install()
    assert caught.value.code == "io"
    assert owner.snapshot()["data"]["state"] == "failed"
    assert state._read_state(owner.state_path).get("pending") is None


def test_repeat_install_verification_failure_revokes_receipt_before_download(setup_owner, monkeypatch):
    owner, updater, _ = setup_owner
    owner.check()
    candidate = delivered(owner)
    old_receipt = owner.install()["receipt_ref"]
    installer = owner.resolve_receipt(old_receipt)["installer_path"]
    payload = state._read_state(owner.state_path)
    payload["future_compatibility"] = {"keep": [1, 2]}
    state._write_state(owner.state_path, payload)

    def failed_redownload(info, download_dir):
        # The real updater can delete the old installer on verification failure.
        assert owner.resolve_receipt(old_receipt) is None
        assert restart(owner, updater).resolve_receipt(old_receipt) is None
        installer.unlink()
        raise app_updater.AppUpdateError("signature verification failed")

    monkeypatch.setattr(updater, "download_update", failed_redownload)
    with pytest.raises(JobFailure) as caught:
        owner.install()
    assert caught.value.code == "io"
    assert owner.snapshot()["data"]["state"] == "failed"
    assert not installer.exists()
    assert owner.resolve_receipt(old_receipt) is None
    assert owner.check()["state"] == "available"
    other = restart(owner, updater)
    assert other.resolve_receipt(old_receipt) is None
    assert other.check()["state"] == "available"
    payload = state._read_state(owner.state_path)
    assert "pending" not in payload
    assert payload["noticed_versions"] == [candidate["version"]]
    assert payload["future_compatibility"] == {"keep": [1, 2]}


def test_repeat_install_cannot_download_if_receipt_revocation_read_fails(setup_owner, monkeypatch):
    owner, updater, _ = setup_owner
    owner.check()
    receipt = owner.install()["receipt_ref"]
    installer = owner.resolve_receipt(receipt)["installer_path"]
    original_bytes = installer.read_bytes()
    original_state = owner.state_path.read_bytes()
    real_read = Path.read_text
    denied_reads = []

    def denied_once(path, *args, **kwargs):
        if path == owner.state_path and not denied_reads:
            denied_reads.append(path)
            raise PermissionError("receipt revocation read denied")
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied_once)
    with pytest.raises(JobFailure) as caught:
        owner.install()
    assert denied_reads == [owner.state_path]
    assert caught.value.code == "io"
    dto = owner.snapshot()["data"]
    assert dto["state"] == "failed" and dto["error"]["code"] == "io"
    assert protocol.validate_topic_payload("update.state", dto) == []
    assert updater.downloads == 1
    assert installer.read_bytes() == original_bytes
    assert owner.state_path.read_bytes() == original_state
    assert owner._receipt_ref is None
    assert owner._installing is False
    assert owner.check()["state"] == "available"
    # No installer bytes were changed: a retry can safely revoke and download.
    assert owner.install()["receipt_ref"] != receipt
    assert updater.downloads == 2
    assert restart(owner, updater).resolve_receipt(receipt) is None


@pytest.mark.parametrize("payload", [b"{", b"\xff", b"[]",
                                     b'{"schema_version": 999, "pending": {}}'])
def test_receipt_revocation_unreadable_content_does_not_block_updates(setup_owner, payload):
    # Corrupt or foreign-schema content resolves to no receipt for every
    # reader, so it cannot keep a stale receipt alive and must not block
    # updates forever; only I/O errors stop the download.
    owner, updater, _ = setup_owner
    owner.check()
    owner.state_path.write_bytes(payload)
    assert state._read_state(owner.state_path) == {}
    assert state._read_state(owner.state_path, strict=True) == {}
    assert state.completed_update_notice(owner.state_path, "2.0.0") is None
    result = owner.install()
    assert result["verified"] is True
    assert updater.downloads == 1
    assert owner._installing is False
    assert owner.resolve_receipt(result["receipt_ref"]) is not None


def test_receipt_revocation_missing_file_allows_download(setup_owner):
    owner, updater, _ = setup_owner
    owner.check()
    assert not owner.state_path.exists()
    assert state._read_state(owner.state_path, strict=True) == {}
    result = owner.install()
    assert updater.downloads == 1
    assert result["verified"] is True
    assert owner.snapshot()["data"]["state"] == "verified"
    assert owner._installing is False
    assert restart(owner, updater).resolve_receipt(result["receipt_ref"])["manifest_verified"] is True


def test_repeat_install_cannot_download_if_receipt_revocation_write_fails(setup_owner, monkeypatch):
    owner, updater, _ = setup_owner
    owner.check()
    receipt = owner.install()["receipt_ref"]
    installer = owner.resolve_receipt(receipt)["installer_path"]
    original_bytes = installer.read_bytes()

    def denied(*args):
        raise OSError("receipt revocation denied")

    monkeypatch.setattr(state.os, "replace", denied)
    with pytest.raises(JobFailure) as caught:
        owner.install()
    assert caught.value.code == "io"
    assert updater.downloads == 1
    assert installer.read_bytes() == original_bytes
    assert owner.snapshot()["data"]["state"] == "failed"
    assert owner.check()["state"] == "available"


@pytest.mark.parametrize("operation,io", [
    ("check", "read"), ("check_verified", "read"), ("lookup", "read"),
    ("delivery", "read"), ("delivery", "write"),
    ("install", "read"), ("install", "write"),
    ("repeat_install", "read"), ("repeat_install", "write"),
])
def test_slow_persistence_does_not_block_tick_readers(setup_owner, monkeypatch, operation, io):
    owner, updater, _ = setup_owner
    owner.check()
    candidate = owner.take_notice_candidate()
    receipt = None
    if operation in {"check_verified", "lookup", "repeat_install"}:
        receipt = owner.install()["receipt_ref"]
    if operation in {"check", "check_verified"}:
        work = owner.check
    elif operation == "lookup":
        work = lambda: owner.resolve_receipt(receipt)
    elif operation == "delivery":
        work = lambda: owner.record_notice_delivered(
            candidate["version"], delivery_id=candidate["delivery_id"])
    else:
        work = owner.install

    entered, release_io = Event(), Event()
    target, name = (state, "_read_state") if io == "read" else (state.os, "fsync")
    real_io = getattr(target, name)

    def slow_io(*args, **kwargs):
        entered.set()
        assert release_io.wait(5)
        return real_io(*args, **kwargs)

    monkeypatch.setattr(target, name, slow_io)

    def tick_readers():
        snapshot = owner.snapshot()
        published = owner.take_state()
        assert protocol.validate_topic_payload("update.state", snapshot["data"]) == []
        if published is not None:
            assert protocol.validate_topic_payload("update.state", published["data"]) == []
        assert owner.take_notice_candidate() is None
        owner.on_ready()
        assert not owner.poll_auto_check(lambda work: pytest.fail("premature auto check"))
        return snapshot["data"]["state"]

    with ThreadPoolExecutor(2) as pool:
        persisted = pool.submit(work)
        try:
            assert entered.wait(2)
            current = pool.submit(tick_readers).result(1)
            expected = ("checking" if operation.startswith("check") else
                        "downloading" if operation in {"install", "repeat_install"} else
                        "verified" if operation == "lookup" else "available")
            assert current == expected
            assert not persisted.done()
        finally:
            release_io.set()
        persisted.result(2)
    if operation == "delivery":
        assert not state.notice_due(owner.state_path, candidate["version"])
    elif operation != "check":
        assert owner.snapshot()["data"]["state"] == "verified"


def test_check_install_conflicts_and_noninstallable(setup_owner):
    owner, updater, _ = setup_owner
    with pytest.raises(JobFailure) as caught:
        owner.install()
    assert caught.value.code == "not_allowed"
    owner.check()
    updater.release_download = Event()
    with ThreadPoolExecutor(1) as pool:
        downloading = pool.submit(owner.install)
        try:
            assert updater.download_entered.wait(2)
            assert owner.snapshot()["data"]["state"] == "downloading"
            assert protocol.validate_topic_payload("update.state", owner.snapshot()["data"]) == []
            for call in (owner.check, owner.install):
                with pytest.raises(JobFailure) as caught:
                    call()
                assert caught.value.code == "conflict"
        finally:
            updater.release_download.set()
        assert downloading.result(2)["verified"]


def test_receipt_lookup_rejects_path_tampering(setup_owner):
    owner, _, _ = setup_owner
    owner.check()
    result = owner.install()
    payload = state._read_state(owner.state_path)
    payload["pending"]["installer_name"] = "../unexpected.exe"
    state._write_state(owner.state_path, payload)
    assert restart(owner, FakeUpdater()).resolve_receipt(result["receipt_ref"]) is None


def test_handlers_execute_normal_domain_while_mutation_lock_held(setup_owner):
    owner, updater, _ = setup_owner
    mutation_lock = Lock()
    recorder = JobEventRecorder()
    executor = JobExecutor(recorder, _mutation_lock=mutation_lock)
    owner.register(executor)
    mutation_lock.acquire()
    try:
        for index, name in enumerate(("app.update.check", "app.update.install")):
            assert COMMAND_SPECS[name].domain == "normal"
            ack = executor.submit(name, {}, owner="page-owner", req_id=f"req-{index}")
            assert ack.accepted
            assert executor.acceptance_sent(ack.op)
            job = executor.wait(ack.op, 2)
            assert job["state"] == "done"
            assert job["result"] is not None
        assert updater.checks == 1 and updater.downloads == 1
        assert all(name.startswith("bridge-jobs-normal") for name in updater.threads)
    finally:
        mutation_lock.release()
        assert executor.shutdown(monotonic() + 2)
