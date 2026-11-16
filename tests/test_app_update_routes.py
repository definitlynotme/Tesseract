"""Release traffic routes (11-16: 0.1.0 -> 0.1.1 updates failed after a good check).

Up to 0.1.1 the updater used urllib's global ``urlopen``, which builds one
opener on its first call and keeps that call's proxy settings for the whole
process. The sidecar's first request ran before any Core was up, so every
release download went out directly; where GitHub's release-asset hosts are
blocked the small api.github.com check succeeded and every install failed,
and nothing in bridge.log said why.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import logging
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError
from urllib.parse import urlsplit

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app_update_state
import app_updater
import bridge_update
import bridge_wiring
from app_update_signing import create_signature_document
from bridge_jobs import JobFailure


VERSION = "0.2.0"
INSTALLER = f"Tesseract-Setup-{VERSION}-win-x64.exe"


def _signed_release(installer_body: bytes) -> tuple[str, dict[str, bytes]]:
    digest = hashlib.sha256(installer_body).hexdigest()
    manifest = f"{digest}  {INSTALLER}\n".encode("utf-8")
    private_key = Ed25519PrivateKey.generate()
    public_key = base64.b64encode(private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )).decode("ascii")
    return public_key, {
        INSTALLER: installer_body,
        "SHA256SUMS.txt": manifest,
        "SHA256SUMS.txt.sig": create_signature_document(manifest, private_key),
    }


def _update_info(base_url: str) -> app_updater.AppUpdateInfo:
    def asset(name: str) -> app_updater.ReleaseAsset:
        return app_updater.ReleaseAsset(name, f"{base_url}/{name}")
    return app_updater.AppUpdateInfo(
        current_version="0.1.0", latest_version=VERSION, release_url=base_url,
        release_notes="", installer=asset(INSTALLER), checksum=asset("SHA256SUMS.txt"),
        update_available=True, checksum_signature=asset("SHA256SUMS.txt.sig"),
    )


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _ForwardProxy:
    """A plain-HTTP forward proxy that alone can reach the "release host"."""

    def __init__(self, files: dict[str, bytes]) -> None:
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                seen.append(self.path)
                body = files.get(urlsplit(self.path).path.rsplit("/", 1)[-1])
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Length", str(len(body or b"")))
                self.end_headers()
                self.wfile.write(body or b"")

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_ForwardProxy":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def host_proxies(monkeypatch):
    """Controlled host proxy settings; nothing on this machine bypasses them."""
    current: dict[str, str] = {}
    monkeypatch.setattr(app_updater, "getproxies", lambda: dict(current))
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    return current


def test_a_proxy_enabled_after_the_first_request_carries_the_download(tmp_path, host_proxies):
    """The field failure: direct to the release host fails, the first request
    ran before any proxy existed, and the proxy that appeared later (the
    system proxy Tesseract sets once its Core runs) must carry the download."""
    public_key, files = _signed_release(b"installer bytes")
    blocked = f"http://127.0.0.1:{_closed_port()}/releases/download/v{VERSION}"
    info = _update_info(blocked)

    with _ForwardProxy(files) as proxy:
        # Early request (the automatic check at ready + 6 s): no proxy yet,
        # direct is the only route and it is blocked.
        with pytest.raises(app_updater.AppUpdateError) as early:
            app_updater.download_update(info, tmp_path / "early", public_key=public_key)
        assert "direct=ConnectionRefusedError" in app_updater.failure_summary(early.value)

        host_proxies.update(http=proxy.url, https=proxy.url)  # The Core started.
        downloaded = app_updater.download_update(info, tmp_path / "updates", public_key=public_key)

    assert downloaded.manifest_verified is True
    assert downloaded.installer_path.read_bytes() == files[INSTALLER]
    assert [urlsplit(p).path.rsplit("/", 1)[-1] for p in proxy.seen] == [
        "SHA256SUMS.txt.sig", "SHA256SUMS.txt", INSTALLER]


def test_the_core_route_needs_no_host_proxy_at_all(tmp_path, host_proxies):
    """network_mode=tun or system_proxy.enabled=false: no host proxy is ever
    set, yet the running Core's loopback port is a route (RoutedUpdater)."""
    public_key, files = _signed_release(b"installer via core")
    info = _update_info(f"http://127.0.0.1:{_closed_port()}/dl")
    with _ForwardProxy(files) as core:
        updater = bridge_wiring.RoutedUpdater(lambda: core.url)
        downloaded = app_updater.download_update(
            info, tmp_path, opener=updater._opener(), public_key=public_key)
    assert downloaded.installer_path.read_bytes() == b"installer via core"
    assert len(core.seen) == 3


def test_route_order_and_the_core_is_not_tried_twice(host_proxies):
    core = "http://127.0.0.1:7890"
    labels = lambda routes: [label for label, _ in routes]  # noqa: E731
    assert labels(app_updater.network_routes()) == ["direct"]
    assert labels(app_updater.network_routes(core)) == ["core proxy", "direct"]
    host_proxies.update(http=core + "/", https=core)  # Tesseract's own system proxy.
    assert labels(app_updater.network_routes(core)) == ["core proxy", "direct"], "system proxy = Core"
    host_proxies.update(https="http://127.0.0.1:7897")  # Another proxy tool.
    assert labels(app_updater.network_routes(core)) == ["core proxy", "system proxy", "direct"]
    assert labels(app_updater.network_routes(None)) == ["system proxy", "direct"]


class _Response:
    def __init__(self, body: bytes, *, drop_after: int | None = None) -> None:
        self.body, self.offset, self.drop_after = body, 0, drop_after

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def read(self, size: int = -1) -> bytes:
        if self.drop_after is not None and self.offset >= self.drop_after:
            raise http.client.IncompleteRead(b"", len(self.body) - self.offset)
        end = len(self.body) if size < 0 else self.offset + size
        if self.drop_after is not None:
            end = min(end, self.drop_after)
        chunk = self.body[self.offset:end]
        self.offset += len(chunk)
        return chunk


class _Route:
    def __init__(self, outcome) -> None:  # noqa: ANN001 - exception or callable
        self.outcome, self.requests = outcome, []

    def open(self, request, timeout=None):  # noqa: ANN001
        self.requests.append(request)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome(request)


def test_a_failed_route_falls_through_with_a_fresh_request():
    def rewrite_then_fail(request):
        request.set_proxy("127.0.0.1:1", "https")  # What ProxyHandler does.
        raise URLError(ConnectionRefusedError())
    proxy = _Route(rewrite_then_fail)
    direct = _Route(lambda request: _Response(b"ok"))
    opener = app_updater.RoutedOpener(lambda: [("core proxy", proxy), ("direct", direct)])
    request = app_updater._request("https://github.com/o/r/releases/download/v1/x")
    with opener(request, timeout=1) as response:
        assert response.read() == b"ok"
    assert opener.last_route == "direct"
    sent = direct.requests[0]
    assert sent.host == "github.com" and not sent.has_proxy()
    assert sent.get_header("User-agent", "").startswith("Tesseract/")


def test_every_route_failing_names_routes_and_errors_but_no_url():
    secret = "https://release-assets.example/x?sig=SECRET"
    routes = [("core proxy", _Route(URLError(TimeoutError(secret)))),
              ("direct", _Route(urllib.error.HTTPError(secret, 502, secret, None, None)))]
    with pytest.raises(app_updater.RouteFailure) as caught:
        app_updater.RoutedOpener(lambda: routes)(app_updater._request(secret), timeout=1)
    assert caught.value.attempts == (("core proxy", "TimeoutError"), ("direct", "HTTP 502"))
    try:
        raise app_updater.AppUpdateError(f"Download failed for SHA256SUMS.txt.sig: {caught.value}") from caught.value
    except app_updater.AppUpdateError as wrapped:
        summary = app_updater.failure_summary(wrapped)
    assert summary == ("AppUpdateError(Download failed for SHA256SUMS.txt.sig) <- "
                       "RouteFailure(core proxy=TimeoutError, direct=HTTP 502)")
    assert "SECRET" not in summary and "://" not in summary
    assert app_updater.failure_summary(app_updater.AppUpdateError("https://secret.invalid/x")) == "AppUpdateError"


def test_a_dropped_transfer_starts_the_asset_over(tmp_path):
    public_key, files = _signed_release(b"x" * (3 * 1024 * 1024))
    calls: list[str] = []
    dropped = {"done": False}

    def opener(request, timeout=0):  # noqa: ANN001
        name = request.full_url.rsplit("/", 1)[-1]
        calls.append(name)
        if name == INSTALLER and not dropped["done"]:
            dropped["done"] = True
            return _Response(files[name], drop_after=1024 * 1024)
        return _Response(files[name])

    downloaded = app_updater.download_update(
        _update_info("https://example.invalid/dl"), tmp_path, opener=opener, public_key=public_key)
    assert downloaded.installer_path.read_bytes() == files[INSTALLER]
    assert calls == ["SHA256SUMS.txt.sig", "SHA256SUMS.txt", INSTALLER, INSTALLER]
    assert not list(tmp_path.glob("*.download"))


def test_a_transfer_that_keeps_failing_gives_up_after_the_attempts(tmp_path):
    calls = []

    def opener(request, timeout=0):  # noqa: ANN001
        calls.append(request.full_url)
        raise ConnectionResetError("reset")

    with pytest.raises(app_updater.AppUpdateError, match="Download failed for SHA256SUMS.txt.sig"):
        app_updater.download_update(_update_info("https://example.invalid/dl"), tmp_path,
                                    opener=opener, public_key="unused")
    assert len(calls) == app_updater.DOWNLOAD_ATTEMPTS
    assert not list(tmp_path.glob("*.download"))


def test_the_default_scope_routes_release_traffic_through_the_core(tmp_path, monkeypatch, host_proxies):
    registered = []
    runtime = SimpleNamespace(update_download_proxy_url=lambda: "http://127.0.0.1:7890")
    session = SimpleNamespace(scope=SimpleNamespace(data_dir=tmp_path), runtime=runtime)
    monkeypatch.setattr(bridge_update, "register", lambda session, owner: registered.append(owner))
    bridge_wiring.Wiring(session, scope_kind="default")._update()
    updater = registered[0]._updater
    assert isinstance(updater, bridge_wiring.RoutedUpdater)
    assert [label for label, _ in updater._routes()] == ["core proxy", "direct"]

    def broken():
        raise RuntimeError("core lifecycle busy")
    runtime.update_download_proxy_url = broken
    assert [label for label, _ in updater._routes()] == ["direct"], "a Core failure is no route"
    del runtime.update_download_proxy_url
    assert [label for label, _ in updater._routes()] == ["direct"]


class _FailingUpdater:
    def __init__(self, info: app_updater.AppUpdateInfo) -> None:
        self.info = info

    def check_latest_update(self, *, current_version: str) -> app_updater.AppUpdateInfo:
        return self.info

    def download_update(self, info, download_dir):  # noqa: ANN001
        failure = app_updater.RouteFailure([("direct", "TimeoutError")])
        raise app_updater.AppUpdateError(
            f"Download failed for SHA256SUMS.txt.sig: https://secret.invalid/x {failure}") from failure


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_an_install_failure_reaches_bridge_log_without_urls(tmp_path):
    owner = bridge_update.UpdateOwner(
        app_update_state.update_state_path(tmp_path), tmp_path / "updates",
        current_version="0.1.0", updater=_FailingUpdater(_update_info("https://example.invalid/dl")))
    owner.check()
    # bridge_main attaches the bridge.log handler to "tesseract.bridge".
    bridge_log = logging.getLogger("tesseract.bridge")
    handler = _Records()
    bridge_log.addHandler(handler)
    try:
        with pytest.raises(JobFailure):
            owner.install()
    finally:
        bridge_log.removeHandler(handler)
    messages = [r.getMessage() for r in handler.records if r.name == "tesseract.bridge.update"]
    assert messages == [
        "update install failed: code=io AppUpdateError(Download failed for SHA256SUMS.txt.sig) "
        "<- RouteFailure(direct=TimeoutError)"]
    assert not any("secret" in m for m in messages)


# ---- Clash-style update sources (11-16): signed manifest check, GitHub mirrors, resume ----

LATEST = "https://github.com/o/r/releases/latest/download"


class _MapOpener:
    """Answers by URL; records each request (URL, Range header)."""

    def __init__(self, files: dict[str, bytes], fail: tuple[str, ...] = ()) -> None:
        self.files, self.fail, self.seen = files, fail, []

    def __call__(self, request, timeout=0):  # noqa: ANN001
        self.seen.append((request.full_url, request.get_header("Range")))
        if any(request.full_url.startswith(prefix) for prefix in self.fail):
            raise URLError(TimeoutError())
        if request.full_url not in self.files:
            raise urllib.error.HTTPError(request.full_url, 404, "missing", None, None)
        return _Response(self.files[request.full_url])


def _latest_files(public_files: dict[str, bytes], notes_body: str | None = "What changed") -> dict[str, bytes]:
    files = {f"{LATEST}/{name}": body for name, body in public_files.items() if name.startswith("SHA256SUMS")}
    if notes_body is not None:
        files["https://api.example/latest"] = (
            '{"tag_name": "v%s", "body": "%s"}' % (VERSION, notes_body)).encode()
    return files


def test_the_check_reads_the_signed_manifest_not_the_api(monkeypatch):
    monkeypatch.setattr(app_updater.app_info, "APP_REPOSITORY_URL", "https://github.com/o/r")
    public_key, files = _signed_release(b"installer")
    opener = _MapOpener(_latest_files(files))
    info = app_updater.check_latest_release(current_version="0.1.0", opener=opener, public_key=public_key,
                                            latest_url=LATEST, notes_api_url="https://api.example/latest")
    assert (info.latest_version, info.update_available, info.installable) == (VERSION, True, True)
    assert info.installer.download_url == f"https://github.com/o/r/releases/download/v{VERSION}/{INSTALLER}"
    assert info.checksum_signature.download_url.endswith(f"/v{VERSION}/SHA256SUMS.txt.sig")
    assert info.release_notes == "What changed"
    # The API is only read for the notes; without it the update is still offered.
    quiet = app_updater.check_latest_release(current_version="0.1.0", opener=_MapOpener(_latest_files(files, None)),
                                             public_key=public_key, latest_url=LATEST,
                                             notes_api_url="https://api.example/latest")
    assert quiet.installable and quiet.release_notes == ""


def test_a_manifest_that_does_not_verify_is_refused():
    public_key, files = _signed_release(b"installer")
    tampered = dict(files, **{"SHA256SUMS.txt": files["SHA256SUMS.txt"].replace(b"Setup-0.2.0", b"Setup-9.9.9")})
    with pytest.raises(app_updater.AppUpdateError, match="signature verification failed"):
        app_updater.check_latest_release(opener=_MapOpener(_latest_files(tampered)), public_key=public_key,
                                         latest_url=LATEST, notes_api_url=None)


@pytest.mark.parametrize("names", [[], [INSTALLER, "Tesseract-Setup-0.2.1-win-x64.exe"],
                                   ["Tesseract-Setup-0.3.0-rc.1-win-x64.exe"]])
def test_the_manifest_must_name_one_stable_installer(names):
    text = "".join(f"{'a' * 64}  {name}\n" for name in names + ["sbom.cdx.json"])
    with pytest.raises(app_updater.AppUpdateError):
        app_updater.installer_from_manifest(text)
    assert app_updater.installer_from_manifest(f"{'b' * 64} *{INSTALLER}\n") == (INSTALLER, VERSION)


def test_a_blocked_github_falls_back_to_a_mirror_and_keeps_it():
    url = f"https://github.com/o/r/releases/download/v{VERSION}/{INSTALLER}"
    mirror = app_updater.GITHUB_MIRROR_PREFIXES[0] + url
    routed = _MapOpener({mirror: b"via mirror"}, fail=("https://github.com/",))
    opener = app_updater.SourcedOpener(routed)
    with opener(app_updater._request(url), timeout=1) as response:
        assert response.read() == b"via mirror"
    assert opener.last_source.startswith("mirror ")
    routed.seen.clear()
    with opener(app_updater._request(url), timeout=1):
        pass
    assert [u for u, _ in routed.seen] == [mirror], "the working mirror goes first next time"
    # Not a github.com download: one source only (the API, a test host).
    assert app_updater.source_urls("https://api.github.com/x") == [("github", "https://api.github.com/x")]


def test_every_source_failing_names_each_source_and_route():
    url = "https://github.com/o/r/releases/download/v1/x"
    routed = app_updater.RoutedOpener(lambda: [("direct", _Route(URLError(TimeoutError())))])
    with pytest.raises(app_updater.RouteFailure) as caught:
        app_updater.SourcedOpener(routed)(app_updater._request(url), timeout=1)
    labels = [label for label, _ in caught.value.attempts]
    assert labels[0] == "github/direct" and all(label.endswith("/direct") for label in labels)
    assert len(labels) == 1 + len(app_updater.GITHUB_MIRROR_PREFIXES)
    assert "://" not in str(caught.value)


class _RangeOpener:
    """Drops the first transfer at 1 MiB, then honours Range with 206."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files, self.ranges, self.dropped = files, [], False

    def __call__(self, request, timeout=0):  # noqa: ANN001
        name = request.full_url.rsplit("/", 1)[-1]
        body, header = self.files[name], request.get_header("Range")
        self.ranges.append((name, header))
        if name == INSTALLER and not self.dropped:
            self.dropped = True
            return _Response(body, drop_after=1024 * 1024)
        if header:
            start = int(header.split("=", 1)[1].rstrip("-"))
            response = _Response(body[start:])
            response.status = 206
            response.headers = {"Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}"}
            return response
        return _Response(body)


def test_a_dropped_installer_download_resumes_where_it_stopped(tmp_path):
    public_key, files = _signed_release(bytes(range(256)) * 12 * 1024)  # 3 MiB
    opener = _RangeOpener(files)
    downloaded = app_updater.download_update(
        _update_info("https://example.invalid/dl"), tmp_path, opener=opener, public_key=public_key)
    assert downloaded.installer_path.read_bytes() == files[INSTALLER]
    assert opener.ranges[-2:] == [(INSTALLER, None), (INSTALLER, f"bytes={1024 * 1024}-")]


def test_the_default_scope_check_falls_back_to_the_api(monkeypatch):
    calls = []

    def manifest(**kwargs):
        calls.append(("manifest", type(kwargs["opener"]).__name__))
        raise app_updater.AppUpdateError("Network request failed: x")

    def api(**kwargs):
        calls.append(("api", type(kwargs["opener"]).__name__))
        return "info"
    monkeypatch.setattr(app_updater, "check_latest_release", manifest)
    monkeypatch.setattr(app_updater, "check_latest_update", api)
    assert bridge_wiring.RoutedUpdater(lambda: None).check_latest_update(current_version="0.1.0") == "info"
    assert calls == [("manifest", "SourcedOpener"), ("api", "SourcedOpener")]
