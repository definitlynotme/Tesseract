from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


REQUIRED_COMPONENTS = frozenset(
    {
        "mihomo",
        "pyside-shiboken",
        "qtbase",
        "qtsvg",
        "cpython-windows-license",
        "wintun-license",
    }
)
SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
GPL_V3_SHA256 = "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"
BUNDLE_DOCUMENTS = (
    ("LICENSES/GPL-3.0-only.txt", "project GPL v3 license"),
    ("THIRD_PARTY_NOTICES.md", "third-party notices"),
    ("BUILD-AND-BINARY-MAPPING.md", "build and binary mapping"),
)
CPYTHON_WINDOWS_LICENSE_COMPONENT = "cpython-windows-license"
CPYTHON_WINDOWS_VERSION = "3.12.7"
CPYTHON_WINDOWS_LICENSE_MARKERS = (
    "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2",
    "Additional Conditions for this Windows binary build",
    'This program, "bzip2", the associated library "libbzip2"',
    "libffi - Copyright",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _required_document(
    path: Path,
    *,
    label: str,
    expected_sha256: str | None = None,
) -> bytes:
    if not path.is_file():
        raise FileNotFoundError(f"required {label} is unavailable: {path}")
    payload = path.read_bytes()
    if not payload:
        raise ValueError(f"required {label} is empty: {path}")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"required {label} must be UTF-8: {path}") from exc
    if expected_sha256 is not None:
        actual_hash = hashlib.sha256(payload).hexdigest()
        if actual_hash != expected_sha256:
            raise ValueError(
                f"{label} hash mismatch: expected {expected_sha256}, "
                f"got {actual_hash}"
            )
    return payload


def _safe_relative_path(value: object, *, field: str) -> PurePosixPath:
    raw = str(value or "").replace("\\", "/").strip()
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or ".." in path.parts
        or ":" in path.parts[0]
    ):
        raise ValueError(f"{field} must be a safe relative path: {raw!r}")
    return path


def _validate_cpython_windows_license(path: Path, *, version: str) -> None:
    if version != CPYTHON_WINDOWS_VERSION:
        raise ValueError(
            "CPython Windows license version must be "
            f"{CPYTHON_WINDOWS_VERSION}, got {version or '<empty>'}"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(
            f"invalid CPython Windows license/acknowledgements: {path}"
        ) from exc
    missing = [marker for marker in CPYTHON_WINDOWS_LICENSE_MARKERS if marker not in text]
    if missing:
        raise ValueError(
            "CPython Windows license is missing redistribution conditions or "
            "incorporated-software acknowledgements: " + ", ".join(missing)
        )


def _validated_manifest(
    manifest_path: Path,
    source_root: Path,
) -> tuple[dict[str, Any], list[tuple[dict[str, str], Path]]]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid source manifest {manifest_path}: {exc}") from exc
    if payload.get("schema_version") != 1:
        raise ValueError("source manifest schema_version must be 1")
    raw_components = payload.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        raise ValueError("source manifest components must be a non-empty list")

    seen: set[str] = set()
    validated: list[tuple[dict[str, str], Path]] = []
    for index, raw_component in enumerate(raw_components):
        if not isinstance(raw_component, dict):
            raise ValueError(f"source component {index} must be an object")
        component = {
            key: str(raw_component.get(key) or "").strip()
            for key in (
                "name",
                "version",
                "source_path",
                "sha256",
                "source_url",
                "license",
            )
        }
        missing = [key for key, value in component.items() if not value]
        if missing:
            raise ValueError(
                f"source component {index} is missing: {', '.join(missing)}"
            )
        name = component["name"]
        if name in seen:
            raise ValueError(f"duplicate source component: {name}")
        seen.add(name)
        expected_hash = component["sha256"].lower()
        if not SHA256_PATTERN.fullmatch(expected_hash):
            raise ValueError(
                f"{name} sha256 must be a resolved 64-character hex digest"
            )
        relative_path = _safe_relative_path(
            component["source_path"],
            field=f"{name}.source_path",
        )
        source_path = source_root.joinpath(*relative_path.parts).resolve()
        try:
            source_path.relative_to(source_root.resolve())
        except ValueError as exc:
            raise ValueError(f"{name} source escapes source root") from exc
        if not source_path.is_file():
            raise FileNotFoundError(
                f"required source material is unavailable: {source_path}"
            )
        actual_hash = _sha256(source_path)
        if actual_hash != expected_hash:
            raise ValueError(
                f"{name} source hash mismatch: expected {expected_hash}, "
                f"got {actual_hash}"
            )
        if name == CPYTHON_WINDOWS_LICENSE_COMPONENT:
            _validate_cpython_windows_license(
                source_path,
                version=component["version"],
            )
        component["sha256"] = expected_hash
        component["source_path"] = relative_path.as_posix()
        validated.append((component, source_path))

    missing_components = sorted(REQUIRED_COMPONENTS - seen)
    unexpected_components = sorted(seen - REQUIRED_COMPONENTS)
    if missing_components or unexpected_components:
        details: list[str] = []
        if missing_components:
            details.append("missing " + ", ".join(missing_components))
        if unexpected_components:
            details.append("unexpected " + ", ".join(unexpected_components))
        raise ValueError("source manifest component set is not exact: " + "; ".join(details))

    canonical = {
        "schema_version": 1,
        "components": [item[0] for item in sorted(validated, key=lambda item: item[0]["name"])],
    }
    return canonical, sorted(validated, key=lambda item: item[0]["name"])


def build_source_bundle(
    *,
    manifest_path: Path,
    source_root: Path,
    project_license_path: Path,
    third_party_notices_path: Path,
    binary_mapping_path: Path,
    output_path: Path,
) -> None:
    canonical, components = _validated_manifest(manifest_path, source_root)
    document_payloads = (
        _required_document(
            project_license_path,
            label=BUNDLE_DOCUMENTS[0][1],
            expected_sha256=GPL_V3_SHA256,
        ),
        _required_document(
            third_party_notices_path,
            label=BUNDLE_DOCUMENTS[1][1],
        ),
        _required_document(
            binary_mapping_path,
            label=BUNDLE_DOCUMENTS[2][1],
        ),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite source bundle: {output_path}")

    manifest_bytes = (
        json.dumps(canonical, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    with zipfile.ZipFile(
        output_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        manifest_info = zipfile.ZipInfo("SOURCE-MANIFEST.json", ZIP_TIMESTAMP)
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        manifest_info.external_attr = 0o100644 << 16
        archive.writestr(manifest_info, manifest_bytes)
        for (archive_name, _label), payload in zip(
            BUNDLE_DOCUMENTS,
            document_payloads,
            strict=True,
        ):
            info = zipfile.ZipInfo(archive_name, ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, payload)
        for component, source_path in components:
            source_name = PurePosixPath(component["source_path"]).name
            archive_name = (
                PurePosixPath("sources")
                / f"{component['name']}-{component['version']}"
                / source_name
            ).as_posix()
            info = zipfile.ZipInfo(archive_name, ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, source_path.read_bytes())

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError(f"source bundle was not created: {output_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build a deterministic third-party source bundle from already "
            "downloaded and hash-pinned source materials. This command never "
            "downloads or substitutes missing sources."
        )
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--project-license", type=Path, required=True)
    parser.add_argument("--third-party-notices", type=Path, required=True)
    parser.add_argument("--binary-mapping", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build_source_bundle(
        manifest_path=args.manifest.resolve(),
        source_root=args.source_root.resolve(),
        project_license_path=args.project_license.resolve(),
        third_party_notices_path=args.third_party_notices.resolve(),
        binary_mapping_path=args.binary_mapping.resolve(),
        output_path=args.output.resolve(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
