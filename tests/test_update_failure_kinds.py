"""0.1.6: an update failure says what failed, a bad mirror copy is not final, and it leaves a trace.

Field report 11-22 (0.1.4 -> 0.1.5): the hand-off launched Setup, the user opened the app again
and 立即更新 failed with 更新操作失败。检查网络后重新检查更新。 although the network was fine
(the retry could not replace the installer file the waiting Setup still held), and the
diagnostic bundle had no line of it. Up to 0.1.5:

* every failure showed that one network text (a locked file, a mirror's HTML page, a 403);
* the first source that answered was final: a mirror's oversize or truncated copy, a signature
  or SHA-256 mismatch ended the update although another source had the right bytes;
* a transfer the server closed early ended the read loop like a whole one;
* bridge.log, the update state and Setup's own log never reached a bundle.
"""
from __future__ import annotations

import http.client
import io
import json
import socket
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest

import app_update_state
import app_updater
import bridge_protocol as protocol
import bridge_update
from bridge_exports import BundlePaths
from bridge_jobs import JobFailure
from tests.test_app_update_routes import INSTALLER, LATEST, VERSION, _signed_release, _update_info
from tests.test_bridge_p2_19 import CTX, bundle_fixture, grant, service, zip_texts

MIRROR = "https://ghfast.top/"


class _Answer:
    """One response: a body, its Content-Length, and whether the server closes it early."""

    def __init__(self, body: bytes, *, declared: int | None = None, cut_at: int | None = None) -> None:
        self.body, self.offset, self.cut_at = body, 0, cut_at
        self.headers = {"Content-Length": str(len(body) if declared is None else declared)}
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self, size: int = -1) -> bytes:
        end = len(self.body) if size < 0 else self.offset + size
        if self.cut_at is not None:
            end = min(end, self.cut_at)  # a clean early close: b"" from then on
        chunk = self.body[self.offset:end]
        self.offset += len(chunk)
        return chunk


class _Sources:
    """A routed opener that answers per source (github.com or a mirror prefix) and file name."""

    def __init__(self, by_source: dict[str, dict[str, bytes]]) -> None:
        self.by_source, self.asked = by_source, []

    def __call__(self, request, timeout=0):  # noqa: ANN001
        url = request.full_url
        source = "mirror" if url.startswith(MIRROR) else "github"
        name = url.rsplit("/", 1)[-1]
        self.asked.append((source, name))
        files = self.by_source.get(source, {})
        if name not in files:
            raise app_updater.RouteFailure([("direct", "HTTP 404")])
        return _Answer(files[name])


def _opener(by_source):
    routed = _Sources(by_source)
    return app_updater.SourcedOpener(routed, mirrors=(MIRROR,)), routed


# ---------------------------------------------------------------- failure kinds

def test_failure_kinds_name_what_failed():
    timeouts = app_updater.RouteFailure([("github/direct", "TimeoutError")])
    refused = app_updater.RouteFailure([("github/system proxy", "HTTP 403"), ("github/direct", "HTTP 429")])
    kind = app_updater.failure_kind
    assert kind(timeouts) == "network"
    assert kind(app_updater._network_failure("x", timeouts)) == "network"
    limited = app_updater._network_failure("x", refused)
    assert isinstance(limited, app_updater.UpdateRateLimitedError) and kind(limited) == "rate_limited"
    # The signed manifest's outage outranks the API fallback's rate limit behind it.
    try:
        try:
            raise app_updater.UpdateNetworkError("Network request failed") from timeouts
        except app_updater.UpdateNetworkError as manifest:
            raise app_updater.UpdateRateLimitedError("Network request failed") from manifest
    except app_updater.UpdateRateLimitedError as both:
        assert kind(both) == "network"
    assert kind(app_updater.UpdateIntegrityError("x")) == "integrity"
    assert kind(app_updater.UpdateStorageError("x")) == "storage"
    assert kind(PermissionError(13, "in use")) == "storage"
    assert kind(socket.gaierror(11001, "no such host")) == "network"
    assert kind(app_updater.AppUpdateError("x")) == "other"
    assert kind(RuntimeError("x")) == "other"


def test_error_names_carry_the_windows_code_and_never_the_text():
    name = app_updater._error_name
    assert name(URLError(socket.gaierror(11001, "getaddrinfo failed for secret.invalid"))) == "gaierror[11001]"
    assert name(ConnectionResetError(10054, "reset by https://secret.invalid")) == "ConnectionResetError[10054]"
    assert name(TimeoutError()) == "TimeoutError"
    summary = app_updater.failure_summary(app_updater.UpdateStorageError("Cannot save x") )
    assert summary == "UpdateStorageError(Cannot save x)"
    try:
        try:
            raise PermissionError(32, "used by another process: D:\\private-folder\\updates\\x.exe")
        except PermissionError as locked:
            raise app_updater.UpdateStorageError(f"Cannot save {INSTALLER}: {locked}") from locked
    except app_updater.UpdateStorageError as failure:
        summary = app_updater.failure_summary(failure)
    assert summary.endswith("<- PermissionError[32]") and "private-folder" not in summary


# ---------------------------------------------------------------- sources

def test_a_mirror_answering_with_a_page_is_dropped_and_the_check_asks_the_next_source(monkeypatch):
    monkeypatch.setattr(app_updater.app_info, "APP_REPOSITORY_URL", "https://github.com/o/r")
    public_key, files = _signed_release(b"installer")
    opener, routed = _opener({
        "github": {"SHA256SUMS.txt.sig": b"<html>blocked</html>" * 400, "SHA256SUMS.txt": b"<html>"},
        "mirror": {name: files[name] for name in ("SHA256SUMS.txt.sig", "SHA256SUMS.txt")},
    })
    info = app_updater.check_latest_release(current_version="0.1.0", opener=opener, public_key=public_key,
                                            latest_url=LATEST, notes_api_url=None)
    assert info.latest_version == VERSION and info.update_available
    assert opener.last_source == "mirror ghfast.top"
    assert routed.asked[0] == ("github", "SHA256SUMS.txt.sig")


def test_every_source_failing_verification_is_an_integrity_failure(monkeypatch):
    monkeypatch.setattr(app_updater.app_info, "APP_REPOSITORY_URL", "https://github.com/o/r")
    public_key, files = _signed_release(b"installer")
    _other_key, forged = _signed_release(b"other")  # signed with a key nobody trusts
    bad = {name: forged[name] for name in ("SHA256SUMS.txt.sig", "SHA256SUMS.txt")}
    opener, routed = _opener({"github": bad, "mirror": bad})
    with pytest.raises(app_updater.UpdateIntegrityError) as caught:
        app_updater.check_latest_release(current_version="0.1.0", opener=opener, public_key=public_key,
                                         latest_url=LATEST, notes_api_url=None)
    assert app_updater.failure_kind(caught.value) == "integrity"
    assert [source for source, name in routed.asked if name == "SHA256SUMS.txt.sig"] == ["github", "mirror"]


def test_an_installer_that_fails_its_sha256_is_fetched_again_from_the_next_source(tmp_path):
    public_key, files = _signed_release(b"x" * 300_000)
    wrong = dict(files, **{INSTALLER: b"y" * 300_000})
    opener, routed = _opener({"github": wrong, "mirror": files})
    downloaded = app_updater.download_update(
        _update_info("https://github.com/o/r/releases/download/v0.2.0"), tmp_path,
        opener=opener, public_key=public_key)
    assert downloaded.installer_path.read_bytes() == files[INSTALLER]
    assert [source for source, name in routed.asked if name == INSTALLER] == ["github", "mirror"]


def test_a_transfer_closed_early_is_resumed_not_condemned(tmp_path):
    public_key, files = _signed_release(b"z" * (3 * 1024 * 1024))
    calls = []

    def opener(request, timeout=0):  # noqa: ANN001
        name = request.full_url.rsplit("/", 1)[-1]
        calls.append((name, request.get_header("Range")))
        body = files[name]
        if name == INSTALLER and len(calls) == 3:  # the first installer request is cut cleanly
            return _Answer(body, cut_at=1024 * 1024)
        if request.get_header("Range"):
            start = int(request.get_header("Range").split("=")[1].rstrip("-"))
            answer = _Answer(body[start:])
            answer.status = 206
            answer.headers["Content-Range"] = f"bytes {start}-{len(body) - 1}/{len(body)}"
            return answer
        return _Answer(body)

    downloaded = app_updater.download_update(_update_info("https://example.invalid/dl"), tmp_path,
                                             opener=opener, public_key=public_key)
    assert downloaded.installer_path.read_bytes() == files[INSTALLER]
    assert calls[-2:] == [(INSTALLER, None), (INSTALLER, f"bytes={1024 * 1024}-")]


def test_an_installer_file_that_cannot_be_replaced_is_a_storage_failure_at_once(tmp_path, monkeypatch):
    public_key, files = _signed_release(b"installer")
    calls = []

    def opener(request, timeout=0):  # noqa: ANN001
        calls.append(request.full_url.rsplit("/", 1)[-1])
        return _Answer(files[calls[-1]])

    real_replace = app_updater.os.replace

    def replace(source, target):
        if Path(target).name == INSTALLER:  # the Setup of this version still runs from it
            raise PermissionError(13, "Access is denied")
        return real_replace(source, target)

    monkeypatch.setattr(app_updater.os, "replace", replace)
    with pytest.raises(app_updater.UpdateStorageError) as caught:
        app_updater.download_update(_update_info("https://example.invalid/dl"), tmp_path,
                                    opener=opener, public_key=public_key)
    assert app_updater.failure_kind(caught.value) == "storage"
    assert calls.count(INSTALLER) == 1, "never retried as a dropped transfer"
    assert not list(tmp_path.glob("*.download"))


# ---------------------------------------------------------------- the page and the trace

class _Updater:
    def __init__(self, *, check=None, download=None, routes=("system proxy", "direct")) -> None:
        self.check, self.download, self.routes = check, download, list(routes)

    def check_latest_update(self, *, current_version):
        if isinstance(self.check, BaseException):
            raise self.check
        return self.check

    def download_update(self, info, download_dir):
        raise self.download

    def transport_facts(self):
        return {"routes": self.routes, "source": "mirror ghfast.top", "route": "direct"}


def _failure(cls, *attempts):
    failure = app_updater.RouteFailure(list(attempts) or [("github/direct", "TimeoutError")])
    try:
        raise cls("Network request failed") from failure
    except cls as raised:
        return raised


def _owner(tmp_path, updater, version="0.1.0"):
    return bridge_update.UpdateOwner(app_update_state.update_state_path(tmp_path), tmp_path / "updates",
                                     current_version=version, updater=updater)


@pytest.mark.parametrize("failure, routes, key", [
    (_failure(app_updater.UpdateNetworkError), ("system proxy", "direct"), "errors.update.unreachable_direct"),
    (_failure(app_updater.UpdateNetworkError), ("core proxy", "direct"), "errors.update.unreachable"),
    (_failure(app_updater.UpdateRateLimitedError, ("github/direct", "HTTP 403")), ("direct",), "errors.update.rate_limited"),
    (app_updater.UpdateIntegrityError("Release manifest signature verification failed"), ("direct",), "errors.update.verify_failed"),
    (app_updater.AppUpdateError("Latest release response was not valid JSON."), ("direct",), "errors.update.operation_failed"),
])
def test_a_failed_check_tells_the_page_what_failed(tmp_path, failure, routes, key):
    owner = _owner(tmp_path, _Updater(check=failure, routes=routes))
    with pytest.raises(JobFailure):
        owner.check()
    dto = owner.snapshot()["data"]
    assert dto["state"] == "failed"
    assert dto["error"] == {"code": "io", "message_key": key, "params": {"stage": "check"}}
    assert protocol.validate_topic_payload("update.state", dto) == []


def test_a_failed_install_says_install_and_why(tmp_path):
    info = _update_info("https://example.invalid/dl")
    locked = app_updater.UpdateStorageError(f"Cannot save {INSTALLER}: [WinError 32]")
    owner = _owner(tmp_path, _Updater(check=info, download=locked))
    owner.check()
    with pytest.raises(JobFailure) as caught:
        owner.install()
    assert caught.value.code == "io"
    dto = owner.snapshot()["data"]
    assert dto["error"] == {"code": "io", "message_key": "errors.update.storage_failed",
                            "params": {"stage": "install"}}
    assert protocol.validate_topic_payload("update.state", dto) == []


def test_every_attempt_is_kept_beside_the_state_file_without_urls(tmp_path):
    info = _update_info("https://example.invalid/dl")
    updater = _Updater(check=_failure(app_updater.UpdateNetworkError))
    owner = _owner(tmp_path, updater)
    with pytest.raises(JobFailure):
        owner.check()
    updater.check = info
    owner.check()
    updater.download = app_updater.UpdateIntegrityError("Downloaded installer checksum did not match SHA256SUMS.txt.")
    with pytest.raises(JobFailure):
        owner.install()
    history = app_update_state.read_update_history(owner.history_path)
    assert [(h["op"], h["result"], h.get("kind")) for h in history] == [
        ("check", "failed", "network"), ("check", "ok", None), ("install", "failed", "integrity")]
    first = history[0]
    assert first["routes"] == ["system proxy", "direct"] and first["automatic"] is False
    assert first["summary"] == "UpdateNetworkError(Network request failed) <- RouteFailure(github/direct=TimeoutError)"
    assert history[1]["latest"] == VERSION and history[1]["update_available"] is True
    assert all(h["app_version"] == "0.1.0" and h["at"] for h in history)
    assert "://" not in json.dumps(history)
    assert not owner.state_path.exists(), "the receipt file is not touched by the trace"
    for _ in range(app_update_state.MAX_ATTEMPT_HISTORY + 5):
        owner.check()
    assert len(app_update_state.read_update_history(owner.history_path)) == app_update_state.MAX_ATTEMPT_HISTORY


def _pending(tmp_path, target="0.2.0", previous="0.1.0"):
    downloaded = app_updater.DownloadedUpdate(
        update=SimpleNamespace(latest_version=target, current_version=previous,
                               release_url="https://github.com/o/r/releases/tag/v" + target, release_notes=""),
        installer_path=tmp_path / "updates" / f"Tesseract-Setup-{target}-win-x64.exe",
        sha256="a" * 64, manifest_key_id="sha256:" + "b" * 64, manifest_verified=True)
    app_update_state.record_pending_update(app_update_state.update_state_path(tmp_path), downloaded,
                                           receipt_ref="update-receipt-1")


def test_a_start_still_older_than_its_receipt_records_the_unfinished_install_once(tmp_path):
    _pending(tmp_path)
    for _ in range(2):
        assert _owner(tmp_path, _Updater()).load_completed_update() is None
    history = app_update_state.read_update_history(app_update_state.update_history_path(
        app_update_state.update_state_path(tmp_path)))
    assert [(h["op"], h["to"]) for h in history] == [("not_installed", "0.2.0")]


def test_a_start_that_reached_its_receipt_records_the_install(tmp_path):
    _pending(tmp_path)
    owner = _owner(tmp_path, _Updater(), version="0.2.0")
    assert owner.load_completed_update() == "0.1.0"
    history = app_update_state.read_update_history(owner.history_path)
    assert [(h["op"], h["from"], h["to"]) for h in history] == [("installed", "0.1.0", "0.2.0")]


# ---------------------------------------------------------------- the bundle

def test_the_bundle_carries_bridge_log_the_update_trace_and_setup_logs(tmp_path):
    sources, view = bundle_fixture(tmp_path)
    data = tmp_path / "data"
    (data / "bridge.log").write_text("2026-11-22 01:20:52 WARNING update install failed: code=io kind=storage "
                                     "UpdateStorageError(Cannot save x) <- PermissionError[5]\n", encoding="utf-8")
    app_update_state.record_update_attempt(data / "app-update-history.json",
                                           {"op": "check", "result": "failed", "kind": "network"})
    temp = tmp_path / "temp"
    temp.mkdir()
    (temp / "Tesseract-Update-1795281587.log").write_text("Log opened.\nUser canceled the elevation.\n",
                                                          encoding="utf-8")
    (temp / "unrelated.log").write_text("no\n", encoding="utf-8")
    paths = sources.paths
    sources.paths = BundlePaths(paths.config_files,
                                {**paths.state_files, "app-update-history.json": data / "app-update-history.json"},
                                paths.log_file, paths.events_file, paths.extra_log_dir, paths.home,
                                data_dir=data, setup_log_dir=temp)
    sources.update_environment = lambda: {"routes": ["system proxy", "direct"], "installed_copy": True,
                                          "system_proxy": {"pac_url_set": False}}
    svc, targets, _ = service(tmp_path, view=view, sources=sources)
    grant(svc, targets, "bundle-1", "bundle", "zip", 70)
    svc.export_bundle({"save_ref": "bundle-1"}, CTX)
    texts = zip_texts(targets.handles[70].read_bytes())
    assert "kind=storage" in texts["logs/bridge.log"]
    assert "User canceled the elevation" in texts["logs/Tesseract-Update-1795281587.log"]
    assert not any(name.endswith("unrelated.log") for name in texts)
    assert json.loads(texts["state/app-update-history.json"])["attempts"][0]["kind"] == "network"
    status = json.loads(texts["runtime-status.json"])
    assert status["update_environment"]["routes"] == ["system proxy", "direct"]
