from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shutil
import sys
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path

from packaging.utils import canonicalize_name

try:
    from .verify_release_environment import (
        direct_requirement_names,
        locked_dependency_closure,
        locked_requirements,
    )
except ImportError:
    from verify_release_environment import (
        direct_requirement_names,
        locked_dependency_closure,
        locked_requirements,
    )


def _shell_licenses():
    # Imported only for --flavor shell, so the Qt path loads nothing new.
    try:
        from . import shell_release_licenses
    except ImportError:
        import shell_release_licenses
    return shell_release_licenses


LICENSE_TOKENS = ("license", "copying", "notice", "authors")
FORBIDDEN_RELEASE_DISTRIBUTIONS = frozenset(
    {"apprise", "notification-bridge"}
)
QT_PYTHON_DISTRIBUTIONS = frozenset(
    {
        "pyside6",
        "pyside6-addons",
        "pyside6-essentials",
        "shiboken6",
    }
)
BINARY_SUFFIXES = frozenset({".dll", ".exe", ".pyd"})
WINTUN_ARCHIVE_SHA256 = {
    "0.14.1": "07c256185d6ee3652e09fa55c0b673e2624b565e02c4b9091c79ca7d2f24ef51"
}
WINTUN_AMD64_DLL_SHA256 = {
    "0.14.1": "e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce"
}
WINTUN_SIGNER = {
    "subject": "WireGuard LLC",
    "thumbprint": "DF98E075A012ED8C86FBCF14854B8F9555CB3D45",
}
FORBIDDEN_QT_FRAGMENTS = (
    "qt6pdf",
    "qt6qml",
    "qt6quick",
    "qt6virtualkeyboard",
    "/qml/",
    "/qpdf.dll",
    "/qtvirtualkeyboardplugin.dll",
    "/opengl32sw.dll",
    "/qicns.dll",
    "/qtga.dll",
    "/qtiff.dll",
    "/qwbmp.dll",
    "/qwebp.dll",
)
# T-P6-02: ``--flavor shell`` packages the Tauri shell (Tesseract.exe) and the
# Python sidecar (tesseract-runtime.exe) without Qt. The default stays "qt"
# until P8 (migration plan §9.4); the Qt path is unchanged.
RELEASE_FLAVORS = ("qt", "shell")
SHELL_FORBIDDEN_FRAGMENTS = ("/_internal/pyside6/", "/_internal/shiboken6/")
SHELL_EXECUTABLE_OWNERS = {
    "tesseract.exe": "tesseract-shell",
    "tesseract-runtime.exe": "tesseract",
}
APPROVED_QT_LIBRARY_FILES = frozenset(
    {
        "qt6core.dll",
        "qt6gui.dll",
        "qt6network.dll",
        "qt6opengl.dll",
        "qt6svg.dll",
        "qt6widgets.dll",
    }
)
APPROVED_PYSIDE_BINARY_FILES = frozenset(
    {
        "pyside6.abi3.dll",
        "qtcore.pyd",
        "qtgui.pyd",
        "qtnetwork.pyd",
        "qtsvg.pyd",
        "qtwidgets.pyd",
    }
)
APPROVED_QT_PLUGIN_PATHS = frozenset(
    {
        "plugins/generic/qtuiotouchplugin.dll",
        "plugins/iconengines/qsvgicon.dll",
        "plugins/imageformats/qgif.dll",
        "plugins/imageformats/qico.dll",
        "plugins/imageformats/qjpeg.dll",
        "plugins/imageformats/qsvg.dll",
        "plugins/networkinformation/qnetworklistmanager.dll",
        "plugins/platforms/qdirect2d.dll",
        "plugins/platforms/qminimal.dll",
        "plugins/platforms/qoffscreen.dll",
        "plugins/platforms/qwindows.dll",
        "plugins/styles/qmodernwindowsstyle.dll",
        "plugins/tls/qcertonlybackend.dll",
        "plugins/tls/qopensslbackend.dll",
        "plugins/tls/qschannelbackend.dll",
    }
)
CPYTHON_EXTENSION_FILES = frozenset(
    {
        "_asyncio.pyd",
        "_bz2.pyd",
        "_ctypes.pyd",
        "_decimal.pyd",
        "_hashlib.pyd",
        "_lzma.pyd",
        "_multiprocessing.pyd",
        "_overlapped.pyd",
        "_queue.pyd",
        "_socket.pyd",
        "_ssl.pyd",
        "_uuid.pyd",
        "_wmi.pyd",
        "_zoneinfo.pyd",
        "pyexpat.pyd",
        "select.pyd",
        "unicodedata.pyd",
        "winsound.pyd",
    }
)
EXTENSION_PACKAGE_OWNERS = {
    "aiohttp": "aiohttp",
    "cryptography": "cryptography",
    "frozenlist": "frozenlist",
    "multidict": "multidict",
    "propcache": "propcache",
    "yaml": "pyyaml",
    "yarl": "yarl",
}
SPECIAL_FROZEN_OWNER_IDS = frozenset(
    {"microsoft-windows-runtime", "openssl", "libffi"}
)
CPYTHON_BUNDLED_RUNTIME_PROVENANCE = {
    "3.12.7": {
        "manifest_url": (
            "https://github.com/python/cpython/blob/v3.12.7/PCbuild/python.props"
        ),
        "openssl_version": "3.0.15",
        "libffi_version": "3.4.4",
        "libffi_dll_abi": "8",
    }
}
DISALLOWED_COMPONENT_VERSIONS = frozenset(
    {"", "unknown", "none", "n/a", "na", "noassertion", "pending"}
)


def _require_component_version(value: object, *, owner_id: str) -> str:
    version = str(value or "").strip()
    if version.casefold() in DISALLOWED_COMPONENT_VERSIONS:
        raise RuntimeError(
            f"runtime owner {owner_id!r} has no traceable component version"
        )
    return version


def _license_property_path(value: object, *, owner_id: str) -> str:
    path = str(value or "").strip().replace("\\", "/")
    parts = path.split("/")
    if (
        not path
        or path.startswith("/")
        or re.match(r"^[A-Za-z]:", path)
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise RuntimeError(f"runtime owner {owner_id!r} has an unsafe license path")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_nonempty_file(path: Path, label: str) -> Path:
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"missing required {label}: {path}")
    return path


def _validate_gpl_v3(path: Path) -> Path:
    _require_nonempty_file(path, "GPL-3.0-only license")
    text = path.read_text(encoding="utf-8")
    required = (
        "GNU GENERAL PUBLIC LICENSE",
        "Version 3, 29 June 2007",
        "TERMS AND CONDITIONS",
        "END OF TERMS AND CONDITIONS",
    )
    if len(text) < 34_000 or any(marker not in text for marker in required):
        raise RuntimeError(f"{path} is not the complete GNU GPL version 3 text")
    return path


def _license_expression(distribution: metadata.Distribution) -> str:
    expression = str(distribution.metadata.get("License-Expression") or "").strip()
    if expression:
        return expression
    legacy = str(distribution.metadata.get("License") or "").strip()
    if legacy and "\n" not in legacy and len(legacy) <= 200:
        return legacy
    classifiers = distribution.metadata.get_all("Classifier") or []
    licenses = [
        item.split("::")[-1].strip()
        for item in classifiers
        if item.startswith("License ::")
    ]
    return " OR ".join(dict.fromkeys(licenses)) or "NOASSERTION"


def _copy_license_files(
    distribution: metadata.Distribution,
    destination: Path,
) -> list[str]:
    copied: list[str] = []
    for entry in distribution.files or []:
        source = distribution.locate_file(entry)
        name = Path(str(entry)).name
        if not source.is_file() or not any(
            token in name.lower() for token in LICENSE_TOKENS
        ):
            continue
        target = destination / name
        collision = 1
        while target.exists():
            target = destination / (
                f"{Path(name).stem}-{collision}{Path(name).suffix}"
            )
            collision += 1
        shutil.copy2(source, target)
        copied.append(target.name)
    return sorted(copied)


def _write_package_metadata(
    directory: Path,
    *,
    name: str,
    version: str,
    license_expression: str,
    homepage: str,
    files: list[str],
    dependency_role: str = "runtime",
    provenance: Mapping[str, str] | None = None,
) -> None:
    payload = {
        "name": name,
        "version": version,
        "license": license_expression,
        "dependency_role": dependency_role,
        "homepage": homepage,
        "copied_license_files": sorted(files),
    }
    if provenance:
        payload["provenance"] = dict(sorted(provenance.items()))
    (directory / "PACKAGE-METADATA.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _sbom_component(
    *,
    name: str,
    version: str,
    license_expression: str,
    owner_id: str,
    license_path: str,
    version_evidence: str,
    purl: str | None = None,
    homepage: str | None = None,
    scope: str = "required",
) -> dict[str, object]:
    version = _require_component_version(version, owner_id=owner_id)
    license_path = _license_property_path(license_path, owner_id=owner_id)
    component: dict[str, object] = {
        "type": "library",
        "name": name,
        "version": version,
        "licenses": [_cyclonedx_license_choice(license_expression)],
        "scope": scope,
        "properties": [
            {"name": "tesseract:runtime-owner", "value": owner_id},
            {"name": "tesseract:license-path", "value": license_path},
            {"name": "tesseract:version-evidence", "value": version_evidence},
        ],
    }
    if purl:
        component["purl"] = purl
    if homepage:
        component["externalReferences"] = [
            {"type": "website", "url": homepage}
        ]
    return component


# CycloneDX 1.5 restricts ``license.id`` to the SPDX license identifier
# enumeration.  Project-specific LicenseRef values remain auditable license
# labels, but must use ``license.name`` rather than impersonating an SPDX ID.
# Keeping the small set of SPDX IDs emitted by this generator explicit makes
# an unfamiliar value fail safely into the schema-valid name field.
_CYCLONEDX_SPDX_LICENSE_IDS = frozenset(
    {
        "Apache-2.0",
        "GPL-3.0-only",
        "MIT",
        "PSF-2.0",
    }
)


def _cyclonedx_license_choice(license_expression: str) -> dict[str, object]:
    value = str(license_expression or "").strip()
    if not value:
        raise ValueError("CycloneDX license value must not be empty")
    field = "id" if value in _CYCLONEDX_SPDX_LICENSE_IDS else "name"
    return {"license": {field: value}}


def _copy_qt_legal_materials(
    *,
    license_dir: Path,
    project_license_path: Path,
    qt_notice_path: Path,
    qt_version: str,
) -> None:
    # Qt flavor only; moved verbatim out of _copy_core_legal_materials (T-P6-02).
    qt_dir = license_dir / f"qt-{qt_version}"
    qt_dir.mkdir(exist_ok=True)
    shutil.copy2(project_license_path, qt_dir / "GPL-3.0.txt")
    shutil.copy2(qt_notice_path, qt_dir / "COMMUNITY-LICENSE-NOTICE.txt")
    _write_package_metadata(
        qt_dir,
        name="Qt",
        version=qt_version,
        license_expression="GPL-3.0-only",
        homepage="https://www.qt.io/licensing/open-source-lgpl-obligations",
        files=["GPL-3.0.txt", "COMMUNITY-LICENSE-NOTICE.txt"],
    )

    for distribution_name in QT_PYTHON_DISTRIBUTIONS:
        component_dir = license_dir / f"{distribution_name}-{qt_version}"
        if not component_dir.is_dir():
            continue
        for commercial_reference in component_dir.glob("LicenseRef-Qt-Commercial*"):
            commercial_reference.unlink()
        shutil.copy2(project_license_path, component_dir / "GPL-3.0.txt")
        shutil.copy2(
            qt_notice_path,
            component_dir / "COMMUNITY-LICENSE-NOTICE.txt",
        )
        metadata_path = component_dir / "PACKAGE-METADATA.json"
        package_payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        package_payload["license"] = "LGPL-3.0-only OR GPL-3.0-only"
        package_payload["selected_release_license"] = "GPL-3.0-only"
        package_payload["copied_license_files"] = sorted(
            {
                *(
                    filename
                    for filename in package_payload["copied_license_files"]
                    if not filename.startswith("LicenseRef-Qt-Commercial")
                ),
                "GPL-3.0.txt",
                "COMMUNITY-LICENSE-NOTICE.txt",
            }
        )
        metadata_path.write_text(
            json.dumps(package_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _copy_core_legal_materials(
    *,
    license_dir: Path,
    project_license_path: Path,
    wintun_license_path: Path,
    qt_notice_path: Path | None,
    python_license_path: Path,
    mihomo_version: str,
    wintun_version: str,
    qt_version: str | None,
    python_version: str,
    special_owner_versions: Mapping[str, str] | None = None,
    flavor: str = "qt",
) -> None:
    project_license_path = _validate_gpl_v3(project_license_path)
    _require_nonempty_file(wintun_license_path, "WinTun prebuilt-binary license")
    if flavor == "qt":
        _require_nonempty_file(qt_notice_path, "Qt/PySide community notice")
    _require_nonempty_file(python_license_path, "CPython license")

    mihomo_dir = license_dir / f"mihomo-{mihomo_version}"
    mihomo_dir.mkdir(exist_ok=True)
    shutil.copy2(project_license_path, mihomo_dir / "GPL-3.0.txt")
    (mihomo_dir / "SOURCE-ACCESS.txt").write_text(
        "Exact release source must be shipped in the same GitHub Release as "
        "Tesseract-<version>-third-party-sources.zip.\n"
        f"Upstream tag: https://github.com/MetaCubeX/mihomo/tree/v{mihomo_version}\n",
        encoding="utf-8",
    )
    _write_package_metadata(
        mihomo_dir,
        name="mihomo",
        version=mihomo_version,
        license_expression="GPL-3.0-only",
        homepage="https://github.com/MetaCubeX/mihomo",
        files=["GPL-3.0.txt", "SOURCE-ACCESS.txt"],
    )

    wintun_dir = license_dir / f"wintun-{wintun_version}"
    wintun_dir.mkdir(exist_ok=True)
    shutil.copy2(wintun_license_path, wintun_dir / "PREBUILT-BINARIES-LICENSE.txt")
    wintun_provenance = {
        "archive_url": f"https://www.wintun.net/builds/wintun-{wintun_version}.zip",
        "archive_sha256": WINTUN_ARCHIVE_SHA256.get(wintun_version),
        "amd64_dll_sha256": WINTUN_AMD64_DLL_SHA256.get(wintun_version),
        "authenticode_signer": WINTUN_SIGNER,
    }
    if not wintun_provenance["archive_sha256"] or not wintun_provenance["amd64_dll_sha256"]:
        raise RuntimeError(f"WinTun {wintun_version} has no reviewed release provenance")
    (wintun_dir / "SOURCE-PROVENANCE.json").write_text(
        json.dumps(wintun_provenance, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_package_metadata(
        wintun_dir,
        name="WinTun",
        version=wintun_version,
        license_expression="LicenseRef-WinTun-Prebuilt",
        homepage="https://www.wintun.net/",
        files=["PREBUILT-BINARIES-LICENSE.txt", "SOURCE-PROVENANCE.json"],
    )

    cpython_dir = license_dir / f"cpython-{python_version}"
    cpython_dir.mkdir(exist_ok=True)
    shutil.copy2(python_license_path, cpython_dir / "LICENSE.txt")
    _write_package_metadata(
        cpython_dir,
        name="CPython",
        version=python_version,
        license_expression="PSF-2.0",
        homepage="https://www.python.org/",
        files=["LICENSE.txt"],
    )

    if flavor == "qt":
        _copy_qt_legal_materials(
            license_dir=license_dir,
            project_license_path=project_license_path,
            qt_notice_path=qt_notice_path,
            qt_version=qt_version,
        )

    if special_owner_versions is None:
        return
    missing_special_versions = sorted(
        SPECIAL_FROZEN_OWNER_IDS - set(special_owner_versions)
    )
    if missing_special_versions:
        raise RuntimeError(
            "frozen audit did not resolve core runtime versions: "
            + ", ".join(missing_special_versions)
        )
    resolved_special_versions = {
        owner_id: _require_component_version(version, owner_id=owner_id)
        for owner_id, version in special_owner_versions.items()
        if owner_id in SPECIAL_FROZEN_OWNER_IDS
    }
    bundled_provenance = CPYTHON_BUNDLED_RUNTIME_PROVENANCE.get(python_version)
    if bundled_provenance is None:
        raise RuntimeError(
            f"CPython {python_version} has no reviewed bundled-runtime manifest"
        )
    provenance_metadata = {
        "cpython_version": python_version,
        "cpython_build_manifest": bundled_provenance["manifest_url"],
    }

    system_dir = license_dir / "microsoft-windows-runtime"
    system_dir.mkdir(exist_ok=True)
    runtime_redistributors = "CPython and PySide6" if flavor == "qt" else "CPython"
    (system_dir / "NOTICE.txt").write_text(
        "The Windows package contains Microsoft Visual C++ and Universal CRT "
        f"runtime files redistributed with {runtime_redistributors}.\n"
        "Terms: https://visualstudio.microsoft.com/license-terms/vs2022-cruntime/\n",
        encoding="utf-8",
    )
    _write_package_metadata(
        system_dir,
        name="Microsoft Windows Runtime",
        version=resolved_special_versions["microsoft-windows-runtime"],
        license_expression="LicenseRef-Microsoft-Windows-Runtime",
        homepage="https://visualstudio.microsoft.com/license-terms/",
        files=["NOTICE.txt"],
    )

    openssl_dir = license_dir / "openssl-cpython-bundled"
    openssl_dir.mkdir(exist_ok=True)
    (openssl_dir / "NOTICE.txt").write_text(
        "CPython's Windows runtime includes OpenSSL "
        f"{resolved_special_versions['openssl']}. Exact file hashes are "
        "recorded in the release SBOM.\n"
        f"CPython {python_version} build manifest: "
        f"{bundled_provenance['manifest_url']}\n"
        "License and source: https://www.openssl.org/source/license.html\n",
        encoding="utf-8",
    )
    _write_package_metadata(
        openssl_dir,
        name="OpenSSL",
        version=resolved_special_versions["openssl"],
        license_expression="Apache-2.0",
        homepage="https://www.openssl.org/",
        files=["NOTICE.txt"],
        provenance=provenance_metadata,
    )

    libffi_dir = license_dir / "libffi-cpython-bundled"
    libffi_dir.mkdir(exist_ok=True)
    (libffi_dir / "NOTICE.txt").write_text(
        "CPython's Windows runtime includes libffi "
        f"{resolved_special_versions['libffi']} (DLL ABI "
        f"{bundled_provenance['libffi_dll_abi']}). The applicable libffi "
        "license text is included in the CPython LICENSE.txt file.\n"
        f"CPython {python_version} build manifest: "
        f"{bundled_provenance['manifest_url']}\n"
        "Source: https://github.com/libffi/libffi\n",
        encoding="utf-8",
    )
    _write_package_metadata(
        libffi_dir,
        name="libffi",
        version=resolved_special_versions["libffi"],
        license_expression="MIT",
        homepage="https://github.com/libffi/libffi",
        files=["NOTICE.txt"],
        provenance={
            **provenance_metadata,
            "dll_abi": bundled_provenance["libffi_dll_abi"],
        },
    )


def _shell_runtime_owner(relative_path: str) -> str | None:
    # Shell flavor: the root holds exactly the shell and the sidecar. Anything
    # else at the root (Qt-era VC runtime overrides, a WebView2Loader.dll, ...)
    # stays unmapped. Inside _internal the sidecar keeps the Qt-flavor rules,
    # minus every Qt/PySide6/Shiboken6 owner.
    lower = relative_path.replace("\\", "/").lstrip("./").lower()
    if "/" not in lower:
        return SHELL_EXECUTABLE_OWNERS.get(lower)
    owner = _runtime_owner(relative_path)
    if owner in {"tesseract", "qt", "pyside6", "shiboken6"}:
        return None
    return owner


def _runtime_owner(relative_path: str, flavor: str = "qt") -> str | None:
    if flavor == "shell":
        return _shell_runtime_owner(relative_path)
    normalized = relative_path.replace("\\", "/").lstrip("./")
    lower = normalized.lower()
    filename = _path_name(lower)

    if lower == "tesseract.exe":
        return "tesseract"
    if lower == "_internal/bin/mihomo.exe":
        return "mihomo"
    if lower == "_internal/bin/wintun.dll":
        return "wintun"
    if lower.startswith("_internal/pyside6/"):
        local = lower[len("_internal/pyside6/") :]
        if filename.startswith(("vcruntime", "msvcp")):
            return "microsoft-windows-runtime"
        if local in APPROVED_QT_PLUGIN_PATHS:
            return "qt"
        if filename in APPROVED_QT_LIBRARY_FILES:
            return "qt"
        if filename in APPROVED_PYSIDE_BINARY_FILES:
            return "pyside6"
        return None
    if lower.startswith("_internal/shiboken6/"):
        if filename.startswith(("vcruntime", "msvcp")):
            return "microsoft-windows-runtime"
        if filename in {"shiboken.pyd", "shiboken6.abi3.dll"}:
            return "shiboken6"
        return None
    if lower.startswith("_internal/api-ms-win-") or filename in {
        "concrt140.dll",
        "msvcp140.dll",
        "msvcp140_1.dll",
        "msvcp140_2.dll",
        "msvcp140_codecvt_ids.dll",
        "ucrtbase.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
    }:
        return "microsoft-windows-runtime"
    if filename in {"python3.dll", "python312.dll"}:
        return "cpython"
    if lower.startswith("_internal/") and "/" not in lower[len("_internal/") :]:
        if filename in CPYTHON_EXTENSION_FILES:
            return "cpython"
        if filename in {"libcrypto-3.dll", "libssl-3.dll"}:
            return "openssl"
        if filename == "libffi-8.dll":
            return "libffi"
        if filename.startswith("_cffi_backend") and filename.endswith(".pyd"):
            return "cffi"
    if lower.startswith("_internal/") and lower.endswith(".pyd"):
        package_name = lower[len("_internal/") :].split("/", 1)[0]
        return EXTENSION_PACKAGE_OWNERS.get(package_name)
    return None


def _path_name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _frozen_fileset_version(
    records: list[tuple[Path, str, str]],
    *,
    owner_id: str,
) -> str:
    digest = hashlib.sha256()
    for _path, relative, content_hash in sorted(
        records,
        key=lambda item: item[1].casefold(),
    ):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content_hash.encode("ascii"))
        digest.update(b"\n")
    if not records:
        raise RuntimeError(f"runtime owner {owner_id!r} has no frozen files")
    return f"fileset-sha256-{digest.hexdigest()}"


def _embedded_openssl_version(
    records: list[tuple[Path, str, str]],
    *,
    expected_version: str,
) -> str:
    versions: set[str] = set()
    for path, _relative, _content_hash in records:
        if path.name.casefold() != "libcrypto-3.dll":
            continue
        versions.update(
            match.decode("ascii")
            for match in re.findall(
                rb"\bOpenSSL\s+([0-9]+\.[0-9]+\.[0-9]+(?:[-+._a-zA-Z0-9]*)?)",
                path.read_bytes(),
            )
        )
    if len(versions) != 1:
        raise RuntimeError(
            "OpenSSL frozen DLL must expose exactly one embedded product version"
        )
    embedded_version = _require_component_version(versions.pop(), owner_id="openssl")
    if embedded_version != expected_version:
        raise RuntimeError(
            "OpenSSL frozen DLL version disagrees with the reviewed CPython "
            f"build manifest: expected {expected_version}, got {embedded_version}"
        )
    return embedded_version


def _validate_libffi_abi(
    records: list[tuple[Path, str, str]],
    *,
    expected_abi: str,
) -> None:
    matches = [
        re.fullmatch(r"libffi-([0-9]+)\.dll", path.name.casefold())
        for path, _relative, _content_hash in records
    ]
    abi_versions = {match.group(1) for match in matches if match is not None}
    if any(match is None for match in matches) or abi_versions != {expected_abi}:
        raise RuntimeError(
            "libffi frozen DLL name disagrees with the reviewed CPython build "
            f"manifest ABI {expected_abi}"
        )


def _pe_fixed_file_version(path: Path) -> str | None:
    try:
        import pefile
    except ImportError:
        return None
    try:
        pe = pefile.PE(str(path), fast_load=False)
        try:
            entries = getattr(pe, "VS_FIXEDFILEINFO", None) or []
            if len(entries) != 1:
                return None
            entry = entries[0]
            return (
                f"{entry.FileVersionMS >> 16}."
                f"{entry.FileVersionMS & 0xFFFF}."
                f"{entry.FileVersionLS >> 16}."
                f"{entry.FileVersionLS & 0xFFFF}"
            )
        finally:
            pe.close()
    except (OSError, ValueError, AttributeError, pefile.PEFormatError):
        return None


def _audit_frozen_distribution(
    dist_dir: Path,
    *,
    runtime_owners: Mapping[str, Mapping[str, object]],
    wintun_version: str = "0.14.1",
    flavor: str = "qt",
) -> tuple[list[dict[str, object]], dict[str, dict[str, str]]]:
    if flavor not in RELEASE_FLAVORS:
        raise ValueError(f"unknown release flavor: {flavor!r}")
    forbidden_fragments = (
        FORBIDDEN_QT_FRAGMENTS
        if flavor == "qt"
        else (*FORBIDDEN_QT_FRAGMENTS, *SHELL_FORBIDDEN_FRAGMENTS)
    )
    if not dist_dir.is_dir():
        raise FileNotFoundError(f"frozen distribution does not exist: {dist_dir}")
    binaries = sorted(
        (
            path
            for path in dist_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in BINARY_SUFFIXES
        ),
        key=lambda path: path.relative_to(dist_dir).as_posix().lower(),
    )
    if not binaries:
        raise RuntimeError(f"frozen distribution contains no binaries: {dist_dir}")

    denied: list[str] = []
    unknown: list[str] = []
    owners: set[str] = set()
    records_by_owner: dict[str, list[tuple[Path, str, str]]] = {}
    for path in binaries:
        relative = path.relative_to(dist_dir).as_posix()
        lower = "/" + relative.lower()
        if any(fragment in lower for fragment in forbidden_fragments):
            denied.append(relative)
            continue
        owner = _runtime_owner(relative, flavor=flavor)
        if owner is None:
            unknown.append(relative)
            continue
        content_hash = _sha256(path)
        if owner == "wintun":
            expected_hash = WINTUN_AMD64_DLL_SHA256.get(wintun_version)
            if not expected_hash:
                raise RuntimeError(
                    f"WinTun {wintun_version} has no reviewed amd64 DLL hash"
                )
            if content_hash != expected_hash:
                raise RuntimeError(
                    "WinTun amd64 DLL hash mismatch: "
                    f"expected {expected_hash}, got {content_hash}"
                )
        owners.add(owner)
        records_by_owner.setdefault(owner, []).append((path, relative, content_hash))
    if denied:
        raise RuntimeError(
            "forbidden Qt artifacts entered the frozen distribution: "
            + ", ".join(denied)
        )
    if unknown:
        raise RuntimeError(
            "unmapped executable artifacts entered the frozen distribution: "
            + ", ".join(unknown)
        )
    required_owners = {
        "tesseract",
        "mihomo",
        "wintun",
        "cpython",
        "qt",
        "pyside6",
        *SPECIAL_FROZEN_OWNER_IDS,
    }
    if flavor == "shell":
        required_owners = {
            "tesseract",
            "tesseract-shell",
            "mihomo",
            "wintun",
            "cpython",
            *SPECIAL_FROZEN_OWNER_IDS,
        }
    missing_owners = sorted(required_owners - owners)
    if missing_owners:
        raise RuntimeError(
            "frozen distribution is missing required runtime components: "
            + ", ".join(missing_owners)
        )
    resolved_owners: dict[str, dict[str, str]] = {}
    for owner_id in sorted(owners):
        raw_spec = runtime_owners.get(owner_id)
        if raw_spec is None:
            raise RuntimeError(
                f"frozen runtime owner {owner_id!r} has no SBOM evidence mapping"
            )
        license_expression = str(raw_spec.get("license_expression") or "").strip()
        if not license_expression:
            raise RuntimeError(f"runtime owner {owner_id!r} has no license mapping")
        license_path = _license_property_path(
            raw_spec.get("license_path"),
            owner_id=owner_id,
        )
        version_evidence = str(raw_spec.get("version_evidence") or "").strip()
        if not version_evidence:
            raise RuntimeError(
                f"runtime owner {owner_id!r} has no version evidence mapping"
            )
        raw_version = raw_spec.get("version")
        if owner_id == "microsoft-windows-runtime":
            version = _frozen_fileset_version(
                records_by_owner[owner_id],
                owner_id=owner_id,
            )
            version_evidence = "frozen-owner-fileset-sha256"
        elif owner_id == "openssl":
            expected_version = _require_component_version(
                raw_version,
                owner_id=owner_id,
            )
            version = _embedded_openssl_version(
                records_by_owner[owner_id],
                expected_version=expected_version,
            )
            version_evidence = "cpython-build-manifest-and-embedded-version"
        elif owner_id == "libffi":
            version = _require_component_version(raw_version, owner_id=owner_id)
            expected_abi = str(raw_spec.get("dll_abi") or "").strip()
            if not expected_abi:
                raise RuntimeError("libffi has no reviewed DLL ABI mapping")
            _validate_libffi_abi(
                records_by_owner[owner_id],
                expected_abi=expected_abi,
            )
            version_evidence = "cpython-build-manifest-and-dll-abi"
        else:
            version = _require_component_version(raw_version, owner_id=owner_id)
        resolved_owners[owner_id] = {
            "version": version,
            "license_expression": license_expression,
            "license_path": license_path,
            "version_evidence": version_evidence,
        }

    file_components: list[dict[str, object]] = []
    for owner_id in sorted(records_by_owner):
        owner_spec = resolved_owners[owner_id]
        for path, relative, content_hash in sorted(
            records_by_owner[owner_id],
            key=lambda item: item[1].casefold(),
        ):
            file_version = owner_spec["version"]
            file_version_evidence = owner_spec["version_evidence"]
            if owner_id == "microsoft-windows-runtime":
                pe_version = _pe_fixed_file_version(path)
                if pe_version:
                    file_version = pe_version
                    file_version_evidence = "pe-fixed-file-version"
                else:
                    file_version = f"sha256-{content_hash}"
                    file_version_evidence = "file-content-sha256"
            file_components.append(
                {
                    "type": "file",
                    "name": relative,
                    "version": file_version,
                    "bom-ref": f"file:{relative}",
                    "hashes": [{"alg": "SHA-256", "content": content_hash}],
                    "licenses": [
                        {"license": {"name": owner_spec["license_expression"]}}
                    ],
                    "scope": "required",
                    "properties": [
                        {"name": "tesseract:runtime-owner", "value": owner_id},
                        {
                            "name": "tesseract:license-path",
                            "value": owner_spec["license_path"],
                        },
                        {
                            "name": "tesseract:version-evidence",
                            "value": file_version_evidence,
                        },
                    ],
                }
            )
    return file_components, resolved_owners


def audit_frozen_distribution(
    dist_dir: Path,
    *,
    runtime_owners: Mapping[str, Mapping[str, object]],
    wintun_version: str = "0.14.1",
    flavor: str = "qt",
) -> list[dict[str, object]]:
    file_components, _resolved_owners = _audit_frozen_distribution(
        dist_dir,
        runtime_owners=runtime_owners,
        wintun_version=wintun_version,
        flavor=flavor,
    )
    return file_components


def _sync_legal_materials_to_dist(
    *,
    dist_dir: Path,
    project_license_path: Path,
    notices_path: Path,
    license_dir: Path,
    sbom_path: Path,
) -> None:
    shutil.copy2(project_license_path, dist_dir / "LICENSE")
    shutil.copy2(notices_path, dist_dir / "THIRD_PARTY_NOTICES.md")
    shutil.copy2(sbom_path, dist_dir / "sbom.cdx.json")
    destination = dist_dir / "third-party-licenses"
    if destination.resolve() != license_dir.resolve():
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(license_dir, destination)


def collect(
    *,
    lock_path: Path,
    runtime_requirements_path: Path | None = None,
    build_requirements_path: Path | None = None,
    license_dir: Path,
    sbom_path: Path,
    mihomo_version: str,
    app_version: str,
    wintun_version: str = "0.14.1",
    dist_dir: Path | None = None,
    project_license_path: Path | None = None,
    wintun_license_path: Path | None = None,
    qt_notice_path: Path | None = None,
    python_license_path: Path | None = None,
    notices_path: Path | None = None,
    flavor: str = "qt",
    cargo_about_json: Path | None = None,
    cargo_lock_path: Path | None = None,
    deny_config_path: Path | None = None,
    ui_root: Path | None = None,
    allow_substitute_rust_inventory: bool = False,
    cargo_metadata_json: Path | None = None,
    cargo_tree: Path | None = None,
    cargo_tree_linked: Path | None = None,
) -> None:
    if flavor not in RELEASE_FLAVORS:
        raise ValueError(f"unknown release flavor: {flavor!r}")
    if flavor == "shell" and (
        cargo_about_json is None
        or cargo_metadata_json is None
        or cargo_tree is None
        or cargo_tree_linked is None
    ):
        raise ValueError(
            "--flavor shell requires the cargo-about JSON inventory, the "
            "cargo metadata graph and both cargo tree graphs"
        )
    repo_root = lock_path.parent
    project_license_path = project_license_path or repo_root / "LICENSE"
    wintun_license_path = wintun_license_path or (
        repo_root / "packaging" / "licenses" / "WINTUN-PREBUILT-LICENSE.txt"
    )
    qt_notice_path = qt_notice_path or (
        repo_root / "packaging" / "licenses" / "QT-PYSIDE-COMMUNITY-NOTICE.txt"
    )
    python_license_path = python_license_path or Path(sys.base_prefix) / "LICENSE.txt"
    notices_path = notices_path or repo_root / (
        "THIRD_PARTY_NOTICES.md" if flavor == "qt" else "THIRD_PARTY_NOTICES.shell.md"
    )
    _require_nonempty_file(notices_path, "third-party notices")

    locked = locked_requirements(lock_path)
    forbidden = sorted(set(locked) & FORBIDDEN_RELEASE_DISTRIBUTIONS)
    if forbidden:
        raise RuntimeError(
            "retired alert dependencies must not enter the release lock: "
            + ", ".join(forbidden)
        )
    runtime_requirements_path = (
        runtime_requirements_path or lock_path.parent / "requirements.txt"
    )
    build_requirements_path = (
        build_requirements_path or lock_path.parent / "requirements-build.txt"
    )
    runtime_roots = direct_requirement_names((runtime_requirements_path,), locked)
    runtime_dependencies = locked_dependency_closure(locked, runtime_roots)
    all_roots = runtime_roots | direct_requirement_names(
        (build_requirements_path,),
        locked,
    )
    release_environment = locked_dependency_closure(locked, all_roots)
    unreachable = sorted(set(locked) - release_environment)
    if unreachable:
        raise RuntimeError(
            "release lock contains unreachable distributions: "
            + ", ".join(unreachable)
        )

    if license_dir.exists():
        shutil.rmtree(license_dir)
    license_dir.mkdir(parents=True)
    components: list[dict[str, object]] = []
    runtime_owners: dict[str, dict[str, object]] = {
        "tesseract": {
            "version": _require_component_version(app_version, owner_id="tesseract"),
            "license_expression": "GPL-3.0-only",
            "license_path": "LICENSE",
            "version_evidence": "release-version-contract",
        },
        "microsoft-windows-runtime": {
            "version": None,
            "license_expression": "LicenseRef-Microsoft-Windows-Runtime",
            "license_path": "third-party-licenses/microsoft-windows-runtime",
            "version_evidence": "frozen-owner-fileset-sha256",
        },
        "openssl": {
            "version": None,
            "license_expression": "Apache-2.0",
            "license_path": "third-party-licenses/openssl-cpython-bundled",
            "version_evidence": "embedded-openssl-product-version",
        },
        "libffi": {
            "version": None,
            "license_expression": "MIT",
            "license_path": "third-party-licenses/libffi-cpython-bundled",
            "version_evidence": "dll-abi-version",
        },
    }
    shell_excluded = sorted(QT_PYTHON_DISTRIBUTIONS & set(locked)) if flavor == "shell" else []
    if flavor == "shell":
        # The shell is Tesseract itself (project license); the sidecar keeps
        # the "tesseract" owner.
        runtime_owners["tesseract-shell"] = {
            "version": _require_component_version(app_version, owner_id="tesseract-shell"),
            "license_expression": "GPL-3.0-only",
            "license_path": "LICENSE",
            "version_evidence": "release-version-contract",
        }
    for canonical_name, locked_version in sorted(locked.items()):
        if canonical_name in shell_excluded:
            # PySide6/Shiboken6 stay in the shared lock until P8 but are not
            # frozen into the Shell sidecar (tesseract-runtime.spec excludes them).
            continue
        distribution = metadata.distribution(canonical_name)
        actual_version = distribution.version
        if actual_version != locked_version:
            raise RuntimeError(
                f"{canonical_name} version drifted: expected {locked_version}, "
                f"got {actual_version}"
            )
        display_name = str(distribution.metadata.get("Name") or canonical_name)
        component_dir = license_dir / f"{canonicalize_name(display_name)}-{actual_version}"
        component_dir.mkdir()
        copied = _copy_license_files(distribution, component_dir)
        if not copied:
            raise RuntimeError(
                f"{canonical_name}=={actual_version} did not provide a license "
                "or notice file"
            )
        license_expression = _license_expression(distribution)
        if canonical_name in QT_PYTHON_DISTRIBUTIONS:
            license_expression = "LGPL-3.0-only OR GPL-3.0-only"
        dependency_role = "runtime" if canonical_name in runtime_dependencies else "build"
        _write_package_metadata(
            component_dir,
            name=display_name,
            version=actual_version,
            license_expression=license_expression,
            dependency_role=dependency_role,
            homepage=str(
                distribution.metadata.get("Home-page")
                or distribution.metadata.get("Project-URL")
                or ""
            ),
            files=copied,
        )
        license_path = f"third-party-licenses/{component_dir.name}"
        runtime_owners[canonical_name] = {
            "version": actual_version,
            "license_expression": license_expression,
            "license_path": license_path,
            "version_evidence": "hash-locked-python-wheel",
        }
        components.append(
            {
                "type": "library",
                "name": display_name,
                "version": actual_version,
                "purl": f"pkg:pypi/{canonicalize_name(display_name)}@{actual_version}",
                "licenses": [{"license": {"name": license_expression}}],
                "scope": "required" if dependency_role == "runtime" else "excluded",
                "properties": [
                    {"name": "tesseract:dependency-role", "value": dependency_role},
                    {"name": "tesseract:runtime-owner", "value": canonical_name},
                    {"name": "tesseract:license-path", "value": license_path},
                    {
                        "name": "tesseract:version-evidence",
                        "value": "hash-locked-python-wheel",
                    },
                ],
            }
        )

    safe_mihomo_version = re.sub(r"[^0-9A-Za-z._-]+", "-", str(mihomo_version)).strip("-")
    if not safe_mihomo_version:
        raise ValueError("mihomo version must not be empty")
    safe_wintun_version = re.sub(r"[^0-9A-Za-z._-]+", "-", str(wintun_version)).strip("-")
    if not safe_wintun_version:
        raise ValueError("WinTun version must not be empty")
    python_version = platform.python_version()
    bundled_provenance = CPYTHON_BUNDLED_RUNTIME_PROVENANCE.get(python_version)
    if bundled_provenance is None:
        raise RuntimeError(
            f"CPython {python_version} has no reviewed bundled-runtime manifest"
        )
    runtime_owners["openssl"].update(
        {
            "version": bundled_provenance["openssl_version"],
            "version_evidence": "cpython-build-manifest-and-embedded-version",
        }
    )
    runtime_owners["libffi"].update(
        {
            "version": bundled_provenance["libffi_version"],
            "version_evidence": "cpython-build-manifest-and-dll-abi",
            "dll_abi": bundled_provenance["libffi_dll_abi"],
        }
    )
    qt_version = locked.get("pyside6") if flavor == "qt" else None
    if flavor == "qt" and not qt_version:
        raise RuntimeError("release lock must contain PySide6")
    runtime_owners.update(
        {
            "mihomo": {
                "version": safe_mihomo_version,
                "license_expression": "GPL-3.0-only",
                "license_path": f"third-party-licenses/mihomo-{safe_mihomo_version}",
                "version_evidence": "reviewed-upstream-release",
            },
            "wintun": {
                "version": safe_wintun_version,
                "license_expression": "LicenseRef-WinTun-Prebuilt",
                "license_path": f"third-party-licenses/wintun-{safe_wintun_version}",
                "version_evidence": "reviewed-upstream-release-and-dll-hash",
            },
            "cpython": {
                "version": python_version,
                "license_expression": "PSF-2.0",
                "license_path": f"third-party-licenses/cpython-{python_version}",
                "version_evidence": "release-interpreter-version",
            },
        }
    )
    if flavor == "qt":
        runtime_owners["qt"] = {
            "version": qt_version,
            "license_expression": "GPL-3.0-only",
            "license_path": f"third-party-licenses/qt-{qt_version}",
            "version_evidence": "hash-locked-pyside6-qt-runtime",
        }

    file_components: list[dict[str, object]] = []
    resolved_owners = dict(runtime_owners)
    special_owner_versions: dict[str, str] | None = None
    if dist_dir is not None:
        file_components, resolved_owners = _audit_frozen_distribution(
            dist_dir,
            runtime_owners=runtime_owners,
            wintun_version=safe_wintun_version,
            flavor=flavor,
        )
        special_owner_versions = {
            owner_id: resolved_owners[owner_id]["version"]
            for owner_id in SPECIAL_FROZEN_OWNER_IDS
        }

    _copy_core_legal_materials(
        license_dir=license_dir,
        project_license_path=project_license_path,
        wintun_license_path=wintun_license_path,
        qt_notice_path=qt_notice_path,
        python_license_path=python_license_path,
        mihomo_version=safe_mihomo_version,
        wintun_version=safe_wintun_version,
        qt_version=qt_version,
        python_version=python_version,
        special_owner_versions=special_owner_versions,
        flavor=flavor,
    )
    core_components = [
            _sbom_component(
                name="mihomo",
                version=safe_mihomo_version,
                license_expression="GPL-3.0-only",
                owner_id="mihomo",
                license_path=f"third-party-licenses/mihomo-{safe_mihomo_version}",
                version_evidence="reviewed-upstream-release",
                purl=f"pkg:golang/github.com/metacubex/mihomo@v{safe_mihomo_version}",
                homepage="https://github.com/MetaCubeX/mihomo",
            ),
            _sbom_component(
                name="WinTun",
                version=safe_wintun_version,
                license_expression="LicenseRef-WinTun-Prebuilt",
                owner_id="wintun",
                license_path=f"third-party-licenses/wintun-{safe_wintun_version}",
                version_evidence="reviewed-upstream-release-and-dll-hash",
                homepage="https://www.wintun.net/",
            ),
            _sbom_component(
                name="CPython",
                version=python_version,
                license_expression="PSF-2.0",
                owner_id="cpython",
                license_path=f"third-party-licenses/cpython-{python_version}",
                version_evidence="release-interpreter-version",
                purl=f"pkg:generic/cpython@{python_version}",
                homepage="https://www.python.org/",
            ),
    ]
    if flavor == "qt":
        core_components.append(
            _sbom_component(
                name="Qt",
                version=qt_version,
                license_expression="GPL-3.0-only",
                owner_id="qt",
                license_path=f"third-party-licenses/qt-{qt_version}",
                version_evidence="hash-locked-pyside6-qt-runtime",
                purl=f"pkg:generic/qt@{qt_version}",
                homepage="https://www.qt.io/",
            )
        )
    components.extend(core_components)
    shell_inventory = None
    if flavor == "shell":
        shell_inventory = _shell_licenses().collect_shell_inventory(
            license_dir=license_dir,
            app_version=_require_component_version(app_version, owner_id="tesseract-shell"),
            cargo_about_json=cargo_about_json,
            cargo_lock=cargo_lock_path or repo_root / "shell" / "Cargo.lock",
            deny_config=deny_config_path or repo_root / "shell" / "deny.toml",
            ui_root=ui_root or repo_root / "ui",
            cargo_metadata_json=cargo_metadata_json,
            cargo_tree=cargo_tree,
            cargo_tree_linked=cargo_tree_linked,
            allow_substitute=allow_substitute_rust_inventory,
        )
        components.extend(shell_inventory.components)
    if dist_dir is not None:
        components.extend(
            [
                _sbom_component(
                    name="Microsoft Windows Runtime",
                    version=resolved_owners["microsoft-windows-runtime"]["version"],
                    license_expression="LicenseRef-Microsoft-Windows-Runtime",
                    owner_id="microsoft-windows-runtime",
                    license_path="third-party-licenses/microsoft-windows-runtime",
                    version_evidence="frozen-owner-fileset-sha256",
                    homepage="https://visualstudio.microsoft.com/license-terms/",
                ),
                _sbom_component(
                    name="OpenSSL",
                    version=resolved_owners["openssl"]["version"],
                    license_expression="Apache-2.0",
                    owner_id="openssl",
                    license_path="third-party-licenses/openssl-cpython-bundled",
                    version_evidence="cpython-build-manifest-and-embedded-version",
                    homepage="https://www.openssl.org/",
                ),
                _sbom_component(
                    name="libffi",
                    version=resolved_owners["libffi"]["version"],
                    license_expression="MIT",
                    owner_id="libffi",
                    license_path="third-party-licenses/libffi-cpython-bundled",
                    version_evidence="cpython-build-manifest-and-dll-abi",
                    homepage="https://github.com/libffi/libffi",
                ),
            ]
        )
        components.extend(file_components)
    sbom = {
        "$schema": "http://cyclonedx.org/schema/bom-1.5.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "Tesseract",
                "version": _require_component_version(
                    app_version,
                    owner_id="tesseract",
                ),
                "licenses": [{"license": {"id": "GPL-3.0-only"}}],
                "properties": [
                    {"name": "tesseract:runtime-owner", "value": "tesseract"},
                    {"name": "tesseract:license-path", "value": "LICENSE"},
                    {
                        "name": "tesseract:version-evidence",
                        "value": "release-version-contract",
                    },
                ],
            },
            "properties": [
                {"name": "tesseract:release-python", "value": python_version},
                {
                    "name": "tesseract:frozen-distribution-audited",
                    "value": "true" if dist_dir is not None else "false",
                },
                {
                    "name": "tesseract:frozen-binary-count",
                    "value": str(len(file_components)),
                },
            ],
        },
        "components": components,
    }
    if shell_inventory is not None:
        sbom["metadata"]["properties"].extend(
            [
                *shell_inventory.metadata_properties(),
                {
                    "name": "tesseract:excluded-lock-distributions",
                    "value": ",".join(shell_excluded),
                },
            ]
        )
    sbom_path.parent.mkdir(parents=True, exist_ok=True)
    sbom_path.write_text(
        json.dumps(sbom, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if dist_dir is not None:
        _sync_legal_materials_to_dist(
            dist_dir=dist_dir,
            project_license_path=project_license_path,
            notices_path=notices_path,
            license_dir=license_dir,
            sbom_path=sbom_path,
        )
    if shell_inventory is not None:
        shell_runtime = len(set(runtime_dependencies) - set(shell_excluded))
        shell_python = len(locked) - len(shell_excluded)
        print(
            f"Collected shell-flavor notices for {shell_runtime} runtime and "
            f"{shell_python - shell_runtime} build-only Python distributions "
            f"(not frozen: {', '.join(shell_excluded) or 'none'}), "
            f"{len(shell_inventory.rust_crates)} Rust crates "
            f"({len(shell_inventory.mpl_crates)} MPL-2.0), "
            f"{shell_inventory.font_count} fonts and {shell_inventory.npm_count} "
            "production npm packages plus core runtimes; audited "
            f"{len(file_components)} frozen binaries; wrote {sbom_path}."
        )
        return
    print(
        f"Collected notices for {len(runtime_dependencies)} runtime and "
        f"{len(locked) - len(runtime_dependencies)} build-only Python "
        f"distributions plus core runtimes; audited {len(file_components)} "
        f"frozen binaries; wrote {sbom_path}."
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument(
        "--runtime-requirements", type=Path, default=Path("requirements.txt")
    )
    parser.add_argument(
        "--build-requirements", type=Path, default=Path("requirements-build.txt")
    )
    parser.add_argument("--license-dir", type=Path, required=True)
    parser.add_argument("--sbom", type=Path, required=True)
    parser.add_argument("--mihomo-version", required=True)
    parser.add_argument("--wintun-version", default="0.14.1")
    parser.add_argument("--app-version", required=True)
    parser.add_argument("--dist-dir", type=Path)
    parser.add_argument("--project-license", type=Path, default=Path("LICENSE"))
    parser.add_argument(
        "--wintun-license",
        type=Path,
        default=Path("packaging/licenses/WINTUN-PREBUILT-LICENSE.txt"),
    )
    parser.add_argument(
        "--qt-notice",
        type=Path,
        default=Path("packaging/licenses/QT-PYSIDE-COMMUNITY-NOTICE.txt"),
    )
    # T-P6-02: Shell flavor (Tauri shell + Python sidecar, no Qt).
    parser.add_argument("--flavor", choices=RELEASE_FLAVORS, default="qt")
    shell_options = parser.add_argument_group("--flavor shell")
    shell_options.add_argument(
        "--cargo-about-json",
        type=Path,
        help="output of `cargo about generate --format json` for shell/app",
    )
    shell_options.add_argument(
        "--cargo-metadata-json",
        type=Path,
        help="output of `cargo metadata --format-version 1 --frozen --filter-platform "
        "x86_64-pc-windows-msvc` for shell/app (upper bound of the crate graph)",
    )
    shell_options.add_argument(
        "--cargo-tree",
        type=Path,
        help="`cargo tree -p tesseract-shell --target x86_64-pc-windows-msvc --prefix "
        "depth -f {p} -e normal,build` (the crates the shell build compiles)",
    )
    shell_options.add_argument(
        "--cargo-tree-linked",
        type=Path,
        help="the same with `-e normal,no-proc-macro` (crates linked into Tesseract.exe)",
    )
    shell_options.add_argument("--cargo-lock", type=Path, help="default: shell/Cargo.lock")
    shell_options.add_argument("--deny-config", type=Path, help="default: shell/deny.toml")
    shell_options.add_argument("--ui-root", type=Path, help="default: ui")
    shell_options.add_argument(
        "--third-party-notices",
        type=Path,
        help="default: THIRD_PARTY_NOTICES.shell.md",
    )
    shell_options.add_argument(
        "--allow-substitute-rust-inventory",
        action="store_true",
        help="evidence runs only; release builds must use real cargo-about output",
    )
    args = parser.parse_args()
    if args.flavor == "shell":
        if (
            args.cargo_about_json is None
            or args.cargo_metadata_json is None
            or args.cargo_tree is None
            or args.cargo_tree_linked is None
        ):
            parser.error(
                "--flavor shell requires --cargo-about-json, --cargo-metadata-json, "
                "--cargo-tree and --cargo-tree-linked"
            )
        collect(
            lock_path=args.lock.resolve(),
            runtime_requirements_path=args.runtime_requirements.resolve(),
            build_requirements_path=args.build_requirements.resolve(),
            license_dir=args.license_dir.resolve(),
            sbom_path=args.sbom.resolve(),
            mihomo_version=str(args.mihomo_version),
            wintun_version=str(args.wintun_version),
            app_version=str(args.app_version),
            dist_dir=args.dist_dir.resolve() if args.dist_dir else None,
            project_license_path=args.project_license.resolve(),
            wintun_license_path=args.wintun_license.resolve(),
            notices_path=(
                args.third_party_notices or Path("THIRD_PARTY_NOTICES.shell.md")
            ).resolve(),
            flavor="shell",
            cargo_about_json=args.cargo_about_json.resolve(),
            cargo_lock_path=(args.cargo_lock or Path("shell/Cargo.lock")).resolve(),
            deny_config_path=(args.deny_config or Path("shell/deny.toml")).resolve(),
            ui_root=(args.ui_root or Path("ui")).resolve(),
            allow_substitute_rust_inventory=args.allow_substitute_rust_inventory,
            cargo_metadata_json=args.cargo_metadata_json.resolve(),
            cargo_tree=args.cargo_tree.resolve(),
            cargo_tree_linked=args.cargo_tree_linked.resolve(),
        )
        return 0
    if (
        args.cargo_about_json is not None
        or args.cargo_metadata_json is not None
        or args.cargo_tree is not None
        or args.cargo_tree_linked is not None
        or args.cargo_lock is not None
        or args.deny_config is not None
        or args.ui_root is not None
        or args.third_party_notices is not None
        or args.allow_substitute_rust_inventory
    ):
        parser.error("Shell-flavor options require --flavor shell")
    collect(
        lock_path=args.lock.resolve(),
        runtime_requirements_path=args.runtime_requirements.resolve(),
        build_requirements_path=args.build_requirements.resolve(),
        license_dir=args.license_dir.resolve(),
        sbom_path=args.sbom.resolve(),
        mihomo_version=str(args.mihomo_version),
        wintun_version=str(args.wintun_version),
        app_version=str(args.app_version),
        dist_dir=args.dist_dir.resolve() if args.dist_dir else None,
        project_license_path=args.project_license.resolve(),
        wintun_license_path=args.wintun_license.resolve(),
        qt_notice_path=args.qt_notice.resolve(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
