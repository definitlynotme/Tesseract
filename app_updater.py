from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
# urlopen stays importable for callers and tests that guard it; release traffic
# no longer goes through it (see RoutedOpener).
from urllib.request import OpenerDirector, ProxyHandler, Request, build_opener, getproxies, urlopen  # noqa: F401

import app_info
from app_update_signing import (
    ReleaseSignatureError,
    verify_signature_document_with_keyring,
)
from app_info import APP_NAME, APP_RELEASES_URL, APP_UPDATE_API_URL, APP_VERSION
from windows_subprocess import hidden_subprocess_kwargs


DEFAULT_TIMEOUT_SECONDS = 20
INSTALLER_NAME_RE = re.compile(r"^Tesseract-Setup-(?P<version>.+)-win-x64\.exe$", re.IGNORECASE)
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
STABLE_RELEASE_TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+$", re.IGNORECASE)
SEMVER_RE = re.compile(
    r"^v?(?P<core>\d+(?:\.\d+)*)(?:-(?P<prerelease>[0-9A-Za-z.-]+))?"
    r"(?:\+[0-9A-Za-z.-]+)?$"
)
MANIFEST_KEY_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_SIGNATURE_DOCUMENT_BYTES = 4096
MAX_CHECKSUM_MANIFEST_BYTES = 4 * 1024 * 1024
APP_RELEASE_NOTES_START_MARKER = "<!-- tesseract-app-notes:start -->"
APP_RELEASE_NOTES_END_MARKER = "<!-- tesseract-app-notes:end -->"
# Whole-asset attempts; each attempt walks every network route again.
DOWNLOAD_ATTEMPTS = 5
UrlOpener = Callable[..., Any]
NetworkRoute = tuple[str, OpenerDirector]
_NETWORK_ERRORS = (URLError, OSError, http.client.HTTPException)
_SAFE_HEADLINE_RE = re.compile(r"^[A-Za-z0-9 ._()-]{1,120}$")


class AppUpdateError(RuntimeError):
    pass


class RouteFailure(URLError):
    """Every network route failed for one request.

    ``str`` and ``attempts`` name only the routes and the error classes:
    never a URL (release-asset redirects carry signed query strings), a host
    or a proxy address, so the text may go to bridge.log as it is.
    """

    def __init__(self, attempts: Sequence[tuple[str, str]]) -> None:
        self.attempts = tuple(attempts)
        summary = "; ".join(f"{label}: {error}" for label, error in self.attempts)
        super().__init__(f"every network route failed ({summary or 'no route'})")


def _error_name(exc: BaseException) -> str:
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, URLError) and isinstance(exc.reason, BaseException):
        return type(exc.reason).__name__
    return type(exc).__name__


def _same_proxy(left: str, right: str) -> bool:
    return left.strip().rstrip("/").lower() == right.strip().rstrip("/").lower()


def network_routes(core_proxy_url: str | None = None) -> list[NetworkRoute]:
    """Routes for one release request, in the order they are tried.

    1. ``core_proxy_url``: Tesseract's own running Core (loopback mixed port);
    2. the host proxy settings as they are *now* (another proxy tool, or the
       system proxy Tesseract itself set), unless that is the Core again;
    3. a direct connection, which also covers a stale or dead host proxy.

    The host settings are read on every call on purpose. urllib's
    ``urlopen`` builds one opener on its first call and keeps that call's
    proxy settings for the rest of the process. The sidecar's first request
    runs before any Core is up, so up to 0.1.1 every release download went
    out directly even while Tesseract's system proxy was on, and from
    networks that block GitHub's release-asset hosts every update failed
    although the (small, api.github.com) check succeeded.
    """

    routes: list[NetworkRoute] = []
    core = str(core_proxy_url or "").strip()
    if core:
        routes.append(("core proxy", build_opener(ProxyHandler({"http": core, "https": core}))))
    try:
        host = {key: value for key, value in getproxies().items()
                if key in ("http", "https") and isinstance(value, str) and value.strip()}
    except Exception:  # An unreadable host setting is no route, not a failure.
        host = {}
    if host and not (core and all(_same_proxy(value, core) for value in host.values())):
        routes.append(("system proxy", build_opener(ProxyHandler(host))))
    # An empty ProxyHandler: the default one would read the host settings again.
    routes.append(("direct", build_opener(ProxyHandler({}))))
    return routes


class RoutedOpener:
    """An ``urlopen``-compatible opener that chooses the network route per request.

    ``routes`` is called for every request, so a Core that started or
    stopped since the previous request is taken into account. Routes are
    tried in order and the first response wins; a route that fails (no
    connection, timeout, TLS error, an HTTP error status) falls through to
    the next. Integrity never depends on the route: the manifest is
    signature-checked and the installer pinned by its SHA-256.
    """

    def __init__(self, routes: Callable[[], Sequence[NetworkRoute]] | None = None) -> None:
        self._routes = routes  # None: network_routes() without a Core route.
        self.last_route: str | None = None

    def __call__(self, request: Request, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Any:
        # ProxyHandler rewrites the Request it opens (host, tunnel), so every
        # route gets a fresh copy of the original.
        url, data, method = request.full_url, request.data, request.get_method()
        headers = dict(request.header_items())
        attempts: list[tuple[str, str]] = []
        for label, opener in (self._routes or network_routes)():
            fresh = Request(url, data=data, headers=headers, method=method)
            try:
                response = opener.open(fresh, timeout=timeout)
            except _NETWORK_ERRORS as exc:
                if isinstance(exc, HTTPError):
                    try:
                        exc.close()  # Release the error body's connection.
                    except Exception:
                        pass  # No body at all (fp=None).
                attempts.append((label, _error_name(exc)))
                continue
            self.last_route = label
            return response
        raise RouteFailure(attempts)


# Public GitHub download accelerators, tried after github.com itself (prefix + the full
# github.com URL). Reachable 11-16; they come and go, so they are only fallbacks. They can
# never hand over altered bytes that pass: the manifest is Ed25519-signed and the installer
# pinned by its SHA-256 in that manifest.
GITHUB_MIRROR_PREFIXES = ("https://ghfast.top/", "https://gh-proxy.com/")


def source_urls(url: str, mirrors: Sequence[str] = GITHUB_MIRROR_PREFIXES) -> list[tuple[str, str]]:
    """(label, URL) sources for one release file: github.com first, then each mirror.

    Only https://github.com/ URLs are mirrored (release downloads); anything else
    (api.github.com, a test host) has the one source.
    """

    sources = [("github", url)]
    if url.startswith("https://github.com/"):
        for prefix in mirrors:
            host = prefix.split("://", 1)[-1].strip("/")
            sources.append((f"mirror {host}", prefix + url))
    return sources


class SourcedOpener:
    """An ``urlopen``-compatible opener over several sources of the same file.

    Each source (github.com, then the mirrors of ``source_urls``) goes through
    ``routed`` (a RoutedOpener: Core proxy, system proxy, direct). The source
    that answered last is tried first next time, so the rest of one update
    does not wait for a blocked github.com again.
    """

    def __init__(self, routed: UrlOpener | None = None,
                 mirrors: Sequence[str] = GITHUB_MIRROR_PREFIXES) -> None:
        self._routed = RoutedOpener() if routed is None else routed
        self._mirrors = tuple(mirrors)
        self._preferred: str | None = None
        self.last_source: str | None = None

    def __call__(self, request: Request, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Any:
        data, method = request.data, request.get_method()
        headers = dict(request.header_items())
        sources = source_urls(request.full_url, self._mirrors)
        sources.sort(key=lambda source: source[0] != self._preferred)
        attempts: list[tuple[str, str]] = []
        for label, url in sources:
            try:
                response = self._routed(Request(url, data=data, headers=headers, method=method),
                                        timeout=timeout)
            except RouteFailure as exc:
                attempts.extend((f"{label}/{route}", error) for route, error in exc.attempts)
                continue
            except _NETWORK_ERRORS as exc:
                if isinstance(exc, HTTPError):
                    try:
                        exc.close()
                    except Exception:
                        pass
                attempts.append((label, _error_name(exc)))
                continue
            self._preferred = self.last_source = label
            return response
        raise RouteFailure(attempts)


def failure_summary(exc: BaseException) -> str:
    """What bridge.log may keep about an update failure.

    The exception classes of the chain, each route's outcome for a
    RouteFailure, and an AppUpdateError's fixed headline (its text before
    the first ``": "``) when that headline is plain words. Never a URL.
    """

    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(parts) < 6:
        seen.add(id(current))
        if isinstance(current, RouteFailure):
            parts.append("RouteFailure(" + ", ".join(
                f"{label}={error}" for label, error in current.attempts) + ")")
        elif isinstance(current, HTTPError):
            parts.append(f"HTTPError({current.code})")
        elif isinstance(current, AppUpdateError):
            headline = str(current).split(": ", 1)[0].strip()
            parts.append(f"AppUpdateError({headline})" if _SAFE_HEADLINE_RE.fullmatch(headline)
                         else "AppUpdateError")
        else:
            parts.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    download_url: str
    size: int | None = None


@dataclass(frozen=True)
class AppUpdateInfo:
    current_version: str
    latest_version: str
    release_url: str
    release_notes: str
    installer: ReleaseAsset | None
    checksum: ReleaseAsset | None
    update_available: bool
    checksum_signature: ReleaseAsset | None = None

    @property
    def installable(self) -> bool:
        return (
            self.update_available
            and self.installer is not None
            and self.checksum is not None
            and self.checksum_signature is not None
        )


@dataclass(frozen=True)
class DownloadedUpdate:
    update: AppUpdateInfo
    installer_path: Path
    sha256: str
    manifest_key_id: str | None = None
    manifest_verified: bool = False


def normalize_version(value: str) -> str:
    text = str(value or "").strip()
    return text[1:] if text.lower().startswith("v") else text


def extract_app_release_notes(body: object) -> str:
    """Return only the release-body section intended for the in-app dialog.

    Releases created before the marker contract remain compatible and expose
    their complete body.  Once either reserved marker is present, malformed or
    ambiguous marker structure fails closed so GitHub-page-only safety text
    cannot leak into the update explanation window.
    """

    text = str(body or "")
    start_count = text.count(APP_RELEASE_NOTES_START_MARKER)
    end_count = text.count(APP_RELEASE_NOTES_END_MARKER)
    if start_count == 0 and end_count == 0:
        return text
    if start_count != 1 or end_count != 1:
        return ""

    start = text.index(APP_RELEASE_NOTES_START_MARKER) + len(
        APP_RELEASE_NOTES_START_MARKER
    )
    end = text.index(APP_RELEASE_NOTES_END_MARKER)
    if end < start:
        return ""
    return text[start:end].strip()


def _version_numbers(value: str) -> tuple[int, ...]:
    normalized = normalize_version(value).split("+", 1)[0].split("-", 1)[0]
    numbers: list[int] = []
    for part in normalized.split("."):
        match = re.match(r"^(\d+)", part)
        if match is None:
            numbers.append(0)
        else:
            numbers.append(int(match.group(1)))
    while numbers and numbers[-1] == 0:
        numbers.pop()
    return tuple(numbers)


def _prerelease_identifiers(value: str) -> tuple[str, ...] | None:
    match = SEMVER_RE.fullmatch(str(value or "").strip())
    if match is None:
        return None
    prerelease = match.group("prerelease")
    if prerelease is None:
        return ()
    return tuple(prerelease.split("."))


def _compare_prerelease(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    # Per SemVer, a final release outranks any prerelease of the same core
    # version. Numeric identifiers compare numerically and rank below text.
    if not left and right:
        return 1
    if left and not right:
        return -1
    for left_part, right_part in zip(left, right):
        if left_part == right_part:
            continue
        left_numeric = left_part.isdigit()
        right_numeric = right_part.isdigit()
        if left_numeric and right_numeric:
            return 1 if int(left_part) > int(right_part) else -1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return 1 if left_part > right_part else -1
    if len(left) == len(right):
        return 0
    return 1 if len(left) > len(right) else -1


def compare_versions(left: str, right: str) -> int:
    left_numbers = _version_numbers(left)
    right_numbers = _version_numbers(right)
    width = max(len(left_numbers), len(right_numbers), 1)
    padded_left = left_numbers + (0,) * (width - len(left_numbers))
    padded_right = right_numbers + (0,) * (width - len(right_numbers))
    if padded_left > padded_right:
        return 1
    if padded_left < padded_right:
        return -1
    left_prerelease = _prerelease_identifiers(left)
    right_prerelease = _prerelease_identifiers(right)
    if left_prerelease is None or right_prerelease is None:
        return 0
    return _compare_prerelease(left_prerelease, right_prerelease)


def is_newer_version(latest: str, current: str) -> bool:
    return compare_versions(latest, current) > 0


def _request(url: str, *, accept: str = "application/octet-stream",
             headers: dict[str, str] | None = None) -> Request:
    return Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": f"{APP_NAME}/{APP_VERSION} (+{APP_RELEASES_URL})",
            **(headers or {}),
        },
    )


def _read_url(url: str, *, timeout: float, accept: str, opener: UrlOpener,
              max_bytes: int | None = None) -> bytes:
    try:
        with opener(_request(url, accept=accept), timeout=timeout) as response:
            body = response.read() if max_bytes is None else response.read(max_bytes + 1)
    except _NETWORK_ERRORS as exc:
        raise AppUpdateError(f"Network request failed: {exc}") from exc
    if max_bytes is not None and len(body) > max_bytes:
        raise AppUpdateError("Release file exceeds the allowed size.")
    return body


def _asset_from_payload(payload: dict[str, Any]) -> ReleaseAsset | None:
    name = str(payload.get("name") or "").strip()
    url = str(payload.get("browser_download_url") or "").strip()
    if not name or not url:
        return None
    size_value = payload.get("size")
    size = int(size_value) if isinstance(size_value, int) and size_value >= 0 else None
    return ReleaseAsset(name=name, download_url=url, size=size)


def _release_assets(release: dict[str, Any]) -> list[ReleaseAsset]:
    assets = release.get("assets") if isinstance(release.get("assets"), list) else []
    result: list[ReleaseAsset] = []
    for asset_payload in assets:
        if not isinstance(asset_payload, dict):
            continue
        asset = _asset_from_payload(asset_payload)
        if asset is not None:
            result.append(asset)
    return result


def select_windows_installer_asset(assets: list[ReleaseAsset], latest_version: str) -> ReleaseAsset | None:
    exact_name = f"Tesseract-Setup-{normalize_version(latest_version)}-win-x64.exe"
    for asset in assets:
        if asset.name.lower() == exact_name.lower():
            return asset
    return None


def select_checksum_asset(assets: list[ReleaseAsset]) -> ReleaseAsset | None:
    for asset in assets:
        if asset.name.lower() == "sha256sums.txt":
            return asset
    return None


def select_checksum_signature_asset(assets: list[ReleaseAsset]) -> ReleaseAsset | None:
    for asset in assets:
        if asset.name.lower() == "sha256sums.txt.sig":
            return asset
    return None


def update_info_from_release(release: dict[str, Any], *, current_version: str = APP_VERSION) -> AppUpdateInfo:
    if bool(release.get("draft")):
        raise AppUpdateError("Draft releases cannot be used for automatic updates.")
    if bool(release.get("prerelease")):
        raise AppUpdateError("Prereleases cannot be used for automatic updates.")
    tag_name = str(release.get("tag_name") or "").strip()
    if not tag_name:
        raise AppUpdateError("Latest release does not include a version tag.")
    if STABLE_RELEASE_TAG_RE.fullmatch(tag_name) is None:
        raise AppUpdateError("Latest release tag is not a stable MAJOR.MINOR.PATCH version.")
    latest_version = normalize_version(tag_name)
    assets = _release_assets(release)
    installer = select_windows_installer_asset(assets, latest_version)
    checksum = select_checksum_asset(assets)
    checksum_signature = select_checksum_signature_asset(assets)
    release_url = str(release.get("html_url") or APP_RELEASES_URL).strip() or APP_RELEASES_URL
    release_notes = extract_app_release_notes(release.get("body"))
    return AppUpdateInfo(
        current_version=current_version,
        latest_version=latest_version,
        release_url=release_url,
        release_notes=release_notes,
        installer=installer,
        checksum=checksum,
        update_available=is_newer_version(latest_version, current_version),
        checksum_signature=checksum_signature,
    )


def check_latest_update(
    *,
    current_version: str = APP_VERSION,
    api_url: str = APP_UPDATE_API_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: UrlOpener | None = None,
) -> AppUpdateInfo:
    payload = _read_url(
        api_url,
        timeout=timeout,
        accept="application/vnd.github+json",
        opener=RoutedOpener() if opener is None else opener,
    )
    try:
        release = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AppUpdateError("Latest release response was not valid JSON.") from exc
    if not isinstance(release, dict):
        raise AppUpdateError("Latest release response was not an object.")
    return update_info_from_release(release, current_version=current_version)


LATEST_DOWNLOAD_URL = f"{app_info.APP_REPOSITORY_URL}/releases/latest/download"
NOTES_TIMEOUT_SECONDS = 8
STABLE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _trusted_keys(public_key: str | bytes | Iterable[str | bytes] | None) -> tuple[str | bytes, ...]:
    if public_key is None:
        configured_keyring = getattr(app_info, "APP_UPDATE_SIGNING_PUBLIC_KEYS", ())
        if configured_keyring:
            return tuple(configured_keyring)
        bootstrap_key = getattr(app_info, "APP_UPDATE_SIGNING_PUBLIC_KEY", "")
        return (bootstrap_key,) if bootstrap_key else ()
    if isinstance(public_key, (str, bytes)):
        return (public_key,)
    return tuple(public_key)


def installer_from_manifest(text: str) -> tuple[str, str]:
    """(installer name, version) of the one Windows installer a signed SHA256SUMS.txt lists."""

    names = []
    for raw_line in text.splitlines():
        match = re.fullmatch(r"[0-9a-fA-F]{64} [ *](.+)", raw_line.strip())
        if match and INSTALLER_NAME_RE.fullmatch(match.group(1)):
            names.append(match.group(1))
    if len(names) != 1:
        raise AppUpdateError("The signed manifest must list exactly one Windows installer.")
    version = INSTALLER_NAME_RE.fullmatch(names[0]).group("version")
    if STABLE_VERSION_RE.fullmatch(version) is None:
        raise AppUpdateError("The signed manifest's installer is not a stable MAJOR.MINOR.PATCH version.")
    return names[0], version


def check_latest_release(
    *,
    current_version: str = APP_VERSION,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: UrlOpener | None = None,
    public_key: str | bytes | Iterable[str | bytes] | None = None,
    latest_url: str = LATEST_DOWNLOAD_URL,
    notes_api_url: str | None = APP_UPDATE_API_URL,
) -> AppUpdateInfo:
    """The latest stable release from its signed manifest, without the GitHub API.

    Reads ``releases/latest/download/SHA256SUMS.txt`` and its ``.sig`` (plain
    release downloads: mirrored, never rate-limited, unlike the API's 60
    requests per hour per address), verifies the signature with the trusted
    keys and takes the version from the one installer the manifest lists.
    The download URLs are pinned to that version's tag. The release notes are
    a best-effort API read; without them the update is still offered.
    """

    if opener is None:
        opener = SourcedOpener()
    keys = _trusted_keys(public_key)
    if not keys:
        raise AppUpdateError("No trusted release-signing public key is configured.")
    signature = _read_url(f"{latest_url}/SHA256SUMS.txt.sig", timeout=timeout,
                          accept="application/octet-stream", opener=opener,
                          max_bytes=MAX_SIGNATURE_DOCUMENT_BYTES)
    manifest = _read_url(f"{latest_url}/SHA256SUMS.txt", timeout=timeout,
                         accept="application/octet-stream", opener=opener,
                         max_bytes=MAX_CHECKSUM_MANIFEST_BYTES)
    try:
        verify_signature_document_with_keyring(manifest, signature, keys)
        text = manifest.decode("utf-8")
    except (ReleaseSignatureError, UnicodeDecodeError) as exc:
        raise AppUpdateError(f"Release manifest signature verification failed: {exc}") from exc
    installer_name, version = installer_from_manifest(text)
    tag = f"v{version}"
    base = f"{app_info.APP_REPOSITORY_URL}/releases/download/{tag}"
    notes = ""
    if notes_api_url:
        try:
            body = _read_url(notes_api_url, timeout=NOTES_TIMEOUT_SECONDS,
                             accept="application/vnd.github+json", opener=opener,
                             max_bytes=MAX_CHECKSUM_MANIFEST_BYTES)
            release = json.loads(body.decode("utf-8"))
            if isinstance(release, dict) and release.get("tag_name") == tag:
                notes = extract_app_release_notes(release.get("body"))
        except Exception:
            notes = ""  # Rate limit or no route: the notes window just stays empty.
    return AppUpdateInfo(
        current_version=current_version,
        latest_version=version,
        release_url=f"{APP_RELEASES_URL}/tag/{tag}",
        release_notes=notes,
        installer=ReleaseAsset(installer_name, f"{base}/{installer_name}"),
        checksum=ReleaseAsset("SHA256SUMS.txt", f"{base}/SHA256SUMS.txt"),
        update_available=is_newer_version(version, current_version),
        checksum_signature=ReleaseAsset("SHA256SUMS.txt.sig", f"{base}/SHA256SUMS.txt.sig"),
    )


def checksum_from_sums_text(text: str, filename: str) -> str | None:
    target = filename.strip()
    matches: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([0-9a-fA-F]{64}) ([ *])(.+)", line)
        if match is None:
            continue
        digest = match.group(1).lower()
        candidate = match.group(3)
        if candidate == target:
            matches.append(digest)
    if len(matches) != 1:
        return None
    return matches[0]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resumes_at(response: Any, offset: int) -> bool:
    """Whether a Range response continues exactly at ``offset`` (206 + Content-Range)."""
    status = getattr(response, "status", None)
    headers = getattr(response, "headers", None)
    content_range = str(headers.get("Content-Range", "")) if headers is not None else ""
    return status == 206 and content_range.startswith(f"bytes {offset}-")


def _download_file(
    asset: ReleaseAsset,
    target: Path,
    *,
    timeout: float,
    opener: UrlOpener,
    max_bytes: int | None = None,
) -> None:
    if max_bytes is not None and asset.size is not None and asset.size > max_bytes:
        raise AppUpdateError(f"Download for {asset.name} exceeds the allowed size.")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(target.name + ".download")
    # A partial file of an earlier run may belong to another release: start clean.
    tmp_path.unlink(missing_ok=True)
    # A dropped or stalled transfer is retried (a RoutedOpener/SourcedOpener
    # walks the routes and sources again) and continues where it stopped when
    # the server answers the Range request with 206; otherwise it starts over.
    # The bytes are only trusted after the SHA-256 check, so a bad splice fails
    # there. A size violation is final.
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        offset = tmp_path.stat().st_size if tmp_path.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else None
        try:
            with opener(_request(asset.download_url, headers=headers), timeout=timeout) as response:
                resumed = offset > 0 and _resumes_at(response, offset)
                downloaded_bytes = offset if resumed else 0
                with tmp_path.open("ab" if resumed else "wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        downloaded_bytes += len(chunk)
                        if max_bytes is not None and downloaded_bytes > max_bytes:
                            raise AppUpdateError(f"Download for {asset.name} exceeds the allowed size.")
                        output.write(chunk)
        except _NETWORK_ERRORS as exc:
            if attempt >= DOWNLOAD_ATTEMPTS:
                tmp_path.unlink(missing_ok=True)
                raise AppUpdateError(f"Download failed for {asset.name}: {exc}") from exc
            continue
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        break
    try:
        os.replace(tmp_path, target)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise AppUpdateError(f"Download failed for {asset.name}: {exc}") from exc


def download_update(
    update: AppUpdateInfo,
    download_dir: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: UrlOpener | None = None,
    public_key: str | bytes | Iterable[str | bytes] | None = None,
) -> DownloadedUpdate:
    if opener is None:
        opener = RoutedOpener()
    if not update.update_available:
        raise AppUpdateError("No newer version is available.")
    if update.installer is None:
        raise AppUpdateError("Latest release does not include a Windows x64 installer.")
    if update.checksum is None:
        raise AppUpdateError("Latest release is missing SHA256SUMS.txt; refusing to auto-update.")
    if update.checksum_signature is None:
        raise AppUpdateError("Latest release is missing SHA256SUMS.txt.sig; refusing to auto-update.")

    trusted_public_keys = _trusted_keys(public_key)
    if not trusted_public_keys:
        raise AppUpdateError("No trusted release-signing public key is configured.")

    download_dir.mkdir(parents=True, exist_ok=True)
    installer_path = download_dir / update.installer.name
    checksum_path = download_dir / update.checksum.name
    signature_path = download_dir / update.checksum_signature.name
    _download_file(
        update.checksum_signature,
        signature_path,
        timeout=timeout,
        opener=opener,
        max_bytes=MAX_SIGNATURE_DOCUMENT_BYTES,
    )
    _download_file(
        update.checksum,
        checksum_path,
        timeout=timeout,
        opener=opener,
        max_bytes=MAX_CHECKSUM_MANIFEST_BYTES,
    )

    try:
        checksum_bytes = checksum_path.read_bytes()
        signature_bytes = signature_path.read_bytes()
        manifest_key_id = verify_signature_document_with_keyring(
            checksum_bytes,
            signature_bytes,
            trusted_public_keys,
        )
    except (OSError, ReleaseSignatureError) as exc:
        raise AppUpdateError(f"Release manifest signature verification failed: {exc}") from exc

    try:
        checksum_text = checksum_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AppUpdateError("SHA256SUMS.txt is not valid UTF-8.") from exc
    expected = checksum_from_sums_text(checksum_text, update.installer.name)
    if expected is None:
        raise AppUpdateError(
            f"SHA256SUMS.txt must include exactly one valid entry for {update.installer.name}."
        )
    _download_file(update.installer, installer_path, timeout=timeout, opener=opener)
    actual = sha256_file(installer_path)
    if actual.lower() != expected.lower():
        installer_path.unlink(missing_ok=True)
        raise AppUpdateError("Downloaded installer checksum did not match SHA256SUMS.txt.")
    return DownloadedUpdate(
        update=update,
        installer_path=installer_path,
        sha256=actual.lower(),
        manifest_key_id=manifest_key_id,
        manifest_verified=True,
    )


def launch_update_installer(
    downloaded: DownloadedUpdate,
    *,
    popen: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
) -> list[str]:
    if (
        not downloaded.manifest_verified
        or downloaded.manifest_key_id is None
        or MANIFEST_KEY_ID_RE.fullmatch(downloaded.manifest_key_id) is None
    ):
        raise AppUpdateError("Refusing to launch an installer without a verified release manifest.")
    if SHA256_RE.fullmatch(downloaded.sha256) is None:
        raise AppUpdateError("Refusing to launch an installer with an invalid recorded checksum.")
    try:
        current_sha256 = sha256_file(downloaded.installer_path)
    except OSError as exc:
        raise AppUpdateError("The verified update installer is no longer readable.") from exc
    if current_sha256.lower() != downloaded.sha256.lower():
        raise AppUpdateError("The verified update installer changed before launch.")
    if sys.platform != "win32":
        raise AppUpdateError("Automatic installer launch is only supported on Windows.")
    args = [
        str(downloaded.installer_path),
        "/SILENT",
        "/SUPPRESSMSGBOXES",
        "/CLOSEAPPLICATIONS",
        "/RESTARTAPPLICATIONS",
        "/NORESTART",
    ]
    popen(
        args,
        cwd=str(downloaded.installer_path.parent),
        close_fds=True,
        **hidden_subprocess_kwargs(),
    )
    return args


def download_and_launch_update(
    update: AppUpdateInfo,
    download_dir: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: UrlOpener | None = None,
    public_key: str | bytes | Iterable[str | bytes] | None = None,
) -> DownloadedUpdate:
    downloaded = download_update(
        update,
        download_dir,
        timeout=timeout,
        opener=opener,
        public_key=public_key,
    )
    launch_update_installer(downloaded)
    return downloaded
