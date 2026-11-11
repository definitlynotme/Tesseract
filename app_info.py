from __future__ import annotations

import re
import sys
from pathlib import Path

APP_RELEASE_BASE_VERSION = "0.1.0"
APP_RELEASE_CANDIDATE_NUMBER: int | None = None
DEFAULT_APP_VERSION = (
    f"{APP_RELEASE_BASE_VERSION}-rc.{APP_RELEASE_CANDIDATE_NUMBER}"
    if APP_RELEASE_CANDIDATE_NUMBER is not None
    else APP_RELEASE_BASE_VERSION
)

_RELEASE_VERSION_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-rc\.(?P<rc>0|[1-9]\d*))?$"
)
_WINDOWS_VERSION_COMPONENT_MAX = 65535
_WINDOWS_STABLE_BUILD = _WINDOWS_VERSION_COMPONENT_MAX
_WINDOWS_MAX_RC_BUILD = _WINDOWS_STABLE_BUILD - 1


def release_version_contract(version: str) -> dict[str, object]:
    """Return the canonical SemVer/Windows metadata contract for a release."""

    match = _RELEASE_VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise ValueError("release version must be canonical x.y.z or x.y.z-rc.n")
    major, minor, patch = (
        int(match.group(name)) for name in ("major", "minor", "patch")
    )
    if any(
        component > _WINDOWS_VERSION_COMPONENT_MAX
        for component in (major, minor, patch)
    ):
        raise ValueError("Windows release-version components cannot exceed 65535")
    rc_group = match.group("rc")
    rc_number = int(rc_group) if rc_group is not None else None
    if rc_number is not None and rc_number > _WINDOWS_MAX_RC_BUILD:
        raise ValueError("RC number cannot exceed 65534")
    windows_build = rc_number if rc_number is not None else _WINDOWS_STABLE_BUILD
    return {
        "version": version,
        "base_version": f"{major}.{minor}.{patch}",
        "is_prerelease": rc_number is not None,
        "rc_number": rc_number,
        "windows_version_numeric": f"{major}.{minor}.{patch}.{windows_build}",
        "windows_version_tuple": [major, minor, patch, windows_build],
    }


DEFAULT_RELEASE_VERSION_CONTRACT = release_version_contract(DEFAULT_APP_VERSION)


def _app_version_resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(str(sys._MEIPASS)).resolve()
    return Path(__file__).resolve().parent


def _load_app_version() -> str:
    version_path = _app_version_resource_root() / "app_version.txt"
    try:
        version = version_path.read_text(encoding="ascii").strip()
    except OSError:
        return DEFAULT_APP_VERSION
    return version or DEFAULT_APP_VERSION


APP_NAME = "Tesseract"
APP_VERSION = _load_app_version()
APP_AUTHOR = "Tesseract contributors"
APP_ID = "com.tesseract.proxy-app"
APP_WINDOWS_APP_ID = "Tesseract.ProxyApp"
APP_EXECUTABLE = "Tesseract.exe"
APP_REPOSITORY_URL = "https://github.com/definitlynotme/Tessract"
APP_RELEASES_URL = f"{APP_REPOSITORY_URL}/releases"
APP_UPDATE_API_URL = "https://api.github.com/repos/definitlynotme/Tessract/releases/latest"
# Base64 of the project's raw 32-byte Ed25519 public key. The encrypted PKCS#8
# private key is kept outside the repository and is used only by the protected
# release-signing environment.
APP_UPDATE_SIGNING_PUBLIC_KEY = "chAnBBHuyGqekaLIwc95IANmn6zdPy3kujrdY4zFtT0="
# Runtime verification accepts this key ring so a transition release can
# contain both the old and new public keys.  APP_UPDATE_SIGNING_PUBLIC_KEY is
# the key used by the release workflow for the current tag; keep it first.
APP_UPDATE_SIGNING_PUBLIC_KEYS = tuple(
    key for key in (APP_UPDATE_SIGNING_PUBLIC_KEY,) if key
)
# Checks are active by policy. Installation remains fail-closed unless the
# release carries a valid project-signed checksum manifest and matching assets.
APP_UPDATE_ENABLED = True
