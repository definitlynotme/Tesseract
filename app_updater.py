from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

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
UrlOpener = Callable[..., Any]


class AppUpdateError(RuntimeError):
    pass


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


def _request(url: str, *, accept: str = "application/octet-stream") -> Request:
    return Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": f"{APP_NAME}/{APP_VERSION} (+{APP_RELEASES_URL})",
        },
    )


def _read_url(url: str, *, timeout: float, accept: str, opener: UrlOpener) -> bytes:
    try:
        with opener(_request(url, accept=accept), timeout=timeout) as response:
            return response.read()
    except URLError as exc:
        raise AppUpdateError(f"Network request failed: {exc}") from exc
    except OSError as exc:
        raise AppUpdateError(f"Network request failed: {exc}") from exc


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
    opener: UrlOpener = urlopen,
) -> AppUpdateInfo:
    payload = _read_url(
        api_url,
        timeout=timeout,
        accept="application/vnd.github+json",
        opener=opener,
    )
    try:
        release = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AppUpdateError("Latest release response was not valid JSON.") from exc
    if not isinstance(release, dict):
        raise AppUpdateError("Latest release response was not an object.")
    return update_info_from_release(release, current_version=current_version)


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
    try:
        with opener(_request(asset.download_url), timeout=timeout) as response, tmp_path.open("wb") as output:
            downloaded_bytes = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                downloaded_bytes += len(chunk)
                if max_bytes is not None and downloaded_bytes > max_bytes:
                    raise AppUpdateError(f"Download for {asset.name} exceeds the allowed size.")
                output.write(chunk)
        os.replace(tmp_path, target)
    except (URLError, OSError) as exc:
        tmp_path.unlink(missing_ok=True)
        raise AppUpdateError(f"Download failed for {asset.name}: {exc}") from exc
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def download_update(
    update: AppUpdateInfo,
    download_dir: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: UrlOpener = urlopen,
    public_key: str | bytes | Iterable[str | bytes] | None = None,
) -> DownloadedUpdate:
    if not update.update_available:
        raise AppUpdateError("No newer version is available.")
    if update.installer is None:
        raise AppUpdateError("Latest release does not include a Windows x64 installer.")
    if update.checksum is None:
        raise AppUpdateError("Latest release is missing SHA256SUMS.txt; refusing to auto-update.")
    if update.checksum_signature is None:
        raise AppUpdateError("Latest release is missing SHA256SUMS.txt.sig; refusing to auto-update.")

    trusted_public_keys: tuple[str | bytes, ...]
    if public_key is None:
        configured_keyring = getattr(app_info, "APP_UPDATE_SIGNING_PUBLIC_KEYS", ())
        if configured_keyring:
            trusted_public_keys = tuple(configured_keyring)
        else:
            bootstrap_key = getattr(app_info, "APP_UPDATE_SIGNING_PUBLIC_KEY", "")
            trusted_public_keys = (bootstrap_key,) if bootstrap_key else ()
    elif isinstance(public_key, (str, bytes)):
        trusted_public_keys = (public_key,)
    else:
        trusted_public_keys = tuple(public_key)
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
    opener: UrlOpener = urlopen,
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
