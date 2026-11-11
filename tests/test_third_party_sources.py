from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

from scripts.build_third_party_sources import (
    BUNDLE_DOCUMENTS,
    CPYTHON_WINDOWS_LICENSE_COMPONENT,
    CPYTHON_WINDOWS_LICENSE_MARKERS,
    CPYTHON_WINDOWS_VERSION,
    REQUIRED_COMPONENTS,
    build_source_bundle,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _bundle_documents(tmp_path: Path) -> dict[str, Path]:
    notices = tmp_path / "THIRD_PARTY_NOTICES.md"
    mapping = tmp_path / "BUILD-AND-BINARY-MAPPING.md"
    notices.write_text("# Third-party notices\n", encoding="utf-8")
    mapping.write_text("# Build and binary mapping\n", encoding="utf-8")
    return {
        "project_license_path": REPO_ROOT / "LICENSE",
        "third_party_notices_path": notices,
        "binary_mapping_path": mapping,
    }


def _manifest(source_root: Path) -> dict[str, object]:
    components: list[dict[str, str]] = []
    for name in sorted(REQUIRED_COMPONENTS):
        source_path = f"{name}.source"
        payload = (
            ("\n".join(CPYTHON_WINDOWS_LICENSE_MARKERS) + "\n").encode("utf-8")
            if name == CPYTHON_WINDOWS_LICENSE_COMPONENT
            else f"source material for {name}\n".encode("utf-8")
        )
        (source_root / source_path).write_bytes(payload)
        components.append(
            {
                "name": name,
                "version": (
                    CPYTHON_WINDOWS_VERSION
                    if name == CPYTHON_WINDOWS_LICENSE_COMPONENT
                    else "1.0.0"
                ),
                "source_path": source_path,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "source_url": f"https://example.invalid/{name}",
                "license": "GPL-3.0-only",
            }
        )
    return {"schema_version": 1, "components": components}


def test_source_bundle_is_deterministic_and_contains_exact_manifest(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    manifest_payload = _manifest(source_root)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest_payload),
        encoding="utf-8",
    )
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    documents = _bundle_documents(tmp_path)

    build_source_bundle(
        manifest_path=manifest_path,
        source_root=source_root,
        **documents,
        output_path=first,
    )
    build_source_bundle(
        manifest_path=manifest_path,
        source_root=source_root,
        **documents,
        output_path=second,
    )

    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        names = set(archive.namelist())
        assert "SOURCE-MANIFEST.json" in names
        assert {item[0] for item in BUNDLE_DOCUMENTS} <= names
        assert len(names) == len(REQUIRED_COMPONENTS) + len(BUNDLE_DOCUMENTS) + 1
        bundled_manifest = json.loads(archive.read("SOURCE-MANIFEST.json"))
    assert {item["name"] for item in bundled_manifest["components"]} == REQUIRED_COMPONENTS


def test_release_manifest_pins_full_cpython_windows_license() -> None:
    manifest = json.loads(
        (REPO_ROOT / "packaging" / "third_party_sources.template.json").read_text(
            encoding="utf-8"
        )
    )
    component = next(
        item
        for item in manifest["components"]
        if item["name"] == CPYTHON_WINDOWS_LICENSE_COMPONENT
    )
    license_path = Path(sys.base_prefix) / "LICENSE.txt"
    payload = license_path.read_bytes()
    text = payload.decode("utf-8")

    assert component["version"] == "3.12.7"
    assert component["source_path"] == (
        "CPython-3.12.7-WINDOWS-LICENSE-AND-NOTICES.txt"
    )
    assert component["source_url"].endswith(
        "python-3.12.7-embed-amd64.zip#LICENSE.txt"
    )
    assert component["sha256"] == hashlib.sha256(payload).hexdigest()
    assert all(marker in text for marker in CPYTHON_WINDOWS_LICENSE_MARKERS)


def test_source_bundle_rejects_unresolved_hash_placeholder(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    manifest_payload = _manifest(source_root)
    manifest_payload["components"][0]["sha256"] = "REPLACE_WITH_64_HEX_SHA256"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    documents = _bundle_documents(tmp_path)

    with pytest.raises(ValueError, match="resolved 64-character hex digest"):
        build_source_bundle(
            manifest_path=manifest_path,
            source_root=source_root,
            **documents,
            output_path=tmp_path / "bundle.zip",
        )


def test_source_bundle_rejects_missing_or_hash_drifted_sources(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    manifest_payload = _manifest(source_root)
    first = manifest_payload["components"][0]
    (source_root / first["source_path"]).write_bytes(b"changed")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    documents = _bundle_documents(tmp_path)

    with pytest.raises(ValueError, match="source hash mismatch"):
        build_source_bundle(
            manifest_path=manifest_path,
            source_root=source_root,
            **documents,
            output_path=tmp_path / "bundle.zip",
        )

    (source_root / first["source_path"]).unlink()
    with pytest.raises(FileNotFoundError, match="required source material"):
        build_source_bundle(
            manifest_path=manifest_path,
            source_root=source_root,
            **documents,
            output_path=tmp_path / "bundle.zip",
        )


def test_source_bundle_rejects_source_only_cpython_license(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    manifest_payload = _manifest(source_root)
    component = next(
        item
        for item in manifest_payload["components"]
        if item["name"] == CPYTHON_WINDOWS_LICENSE_COMPONENT
    )
    source_path = source_root / component["source_path"]
    payload = b"PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2\n"
    source_path.write_bytes(payload)
    component["sha256"] = hashlib.sha256(payload).hexdigest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    documents = _bundle_documents(tmp_path)

    with pytest.raises(ValueError, match="incorporated-software acknowledgements"):
        build_source_bundle(
            manifest_path=manifest_path,
            source_root=source_root,
            **documents,
            output_path=tmp_path / "bundle.zip",
        )


def test_source_bundle_rejects_wrong_cpython_windows_version(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    manifest_payload = _manifest(source_root)
    component = next(
        item
        for item in manifest_payload["components"]
        if item["name"] == CPYTHON_WINDOWS_LICENSE_COMPONENT
    )
    component["version"] = "3.12.8"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")

    with pytest.raises(ValueError, match="version must be 3.12.7"):
        build_source_bundle(
            manifest_path=manifest_path,
            source_root=source_root,
            **_bundle_documents(tmp_path),
            output_path=tmp_path / "bundle.zip",
        )


def test_source_bundle_rejects_missing_or_wrong_project_license(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(source_root)), encoding="utf-8")
    documents = _bundle_documents(tmp_path)

    documents["project_license_path"] = tmp_path / "missing-license"
    with pytest.raises(FileNotFoundError, match="project GPL v3 license"):
        build_source_bundle(
            manifest_path=manifest_path,
            source_root=source_root,
            **documents,
            output_path=tmp_path / "missing.zip",
        )

    wrong_license = tmp_path / "wrong-license"
    wrong_license.write_text("not the GPL v3 text\n", encoding="utf-8")
    documents["project_license_path"] = wrong_license
    with pytest.raises(ValueError, match="project GPL v3 license hash mismatch"):
        build_source_bundle(
            manifest_path=manifest_path,
            source_root=source_root,
            **documents,
            output_path=tmp_path / "wrong.zip",
        )
