from __future__ import annotations

import json
import os
import shutil
import stat
import zipfile
from pathlib import Path

import pytest

from scripts.release_candidate_identity import (
    ARCHIVE_ROOT,
    EXECUTABLE_PATH,
    HASH_ALGORITHM,
    ReleaseCandidateIdentityError,
    create_manifest,
    create_zip,
    load_manifest,
    verify_directory,
    verify_installed,
    verify_zip,
)


def _candidate(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / EXECUTABLE_PATH).write_bytes(b"tesseract-executable-v1")
    internal = root / "_internal"
    internal.mkdir()
    (internal / "payload.bin").write_bytes(b"runtime-payload")
    (internal / "empty.dat").write_bytes(b"")
    return root


def _manifested_candidate(tmp_path: Path) -> tuple[Path, Path]:
    candidate = _candidate(tmp_path / "candidate")
    manifest = tmp_path / "candidate-identity.json"
    create_manifest(candidate, manifest)
    return candidate, manifest


def _write_candidate_zip(
    zip_path: Path,
    candidate: Path,
    *,
    root: str = ARCHIVE_ROOT,
    omit: frozenset[str] = frozenset(),
    replacements: dict[str, bytes] | None = None,
    extras: dict[str, bytes] | None = None,
) -> None:
    replacements = replacements or {}
    extras = extras or {}
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(path for path in candidate.rglob("*") if path.is_file()):
            relative = source.relative_to(candidate).as_posix()
            if relative in omit:
                continue
            archive.writestr(
                f"{root}/{relative}",
                replacements.get(relative, source.read_bytes()),
            )
        for relative, content in extras.items():
            archive.writestr(relative, content)


def _read_document(manifest: Path) -> dict[str, object]:
    return json.loads(manifest.read_text(encoding="utf-8"))


def _write_document(path: Path, document: dict[str, object]) -> None:
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_create_manifest_records_sorted_exact_file_and_tree_identity(tmp_path: Path) -> None:
    candidate, manifest_path = _manifested_candidate(tmp_path)

    document = _read_document(manifest_path)

    assert document["schema_version"] == 1
    assert document["algorithm"] == HASH_ALGORITHM
    assert document["archive_root"] == ARCHIVE_ROOT
    files = document["files"]
    assert isinstance(files, list)
    paths = [entry["path"] for entry in files]
    assert paths == sorted(paths)
    assert paths == ["Tesseract.exe", "_internal/empty.dat", "_internal/payload.bin"]
    assert document["file_count"] == len(paths)
    executable = next(entry for entry in files if entry["path"] == EXECUTABLE_PATH)
    assert document["tesseract_exe_sha256"] == executable["sha256"]
    assert len(document["tree_sha256"]) == 64
    assert verify_directory(candidate, manifest_path).tree_sha256 == document["tree_sha256"]


def test_create_manifest_refuses_self_inclusion_and_overwrite(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path / "candidate")

    with pytest.raises(ReleaseCandidateIdentityError, match="outside"):
        create_manifest(candidate, candidate / "identity.json")

    manifest = tmp_path / "identity.json"
    manifest.write_text("already here", encoding="utf-8")
    with pytest.raises(ReleaseCandidateIdentityError, match="overwrite"):
        create_manifest(candidate, manifest)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("same-size-tamper", "SHA-256"),
        ("different-size-tamper", "size"),
        ("missing", "missing files"),
        ("extra", "unexpected files"),
    ],
)
def test_verify_directory_fails_closed_on_every_file_set_or_content_change(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    candidate, manifest = _manifested_candidate(tmp_path)
    payload = candidate / "_internal" / "payload.bin"
    if mutation == "same-size-tamper":
        payload.write_bytes(b"runtime-tamper!")
    elif mutation == "different-size-tamper":
        payload.write_bytes(b"short")
    elif mutation == "missing":
        payload.unlink()
    else:
        (candidate / "unexpected.bin").write_bytes(b"extra")

    with pytest.raises(ReleaseCandidateIdentityError, match=message):
        verify_directory(candidate, manifest)


def test_verify_directory_rejects_links_instead_of_following_them(tmp_path: Path) -> None:
    candidate, manifest = _manifested_candidate(tmp_path)
    target = tmp_path / "outside.bin"
    target.write_bytes(b"outside")
    link = candidate / "linked.bin"
    try:
        os.symlink(target, link)
    except (NotImplementedError, OSError):
        pytest.skip("This Windows account cannot create symbolic links")

    with pytest.raises(ReleaseCandidateIdentityError, match="link or reparse"):
        verify_directory(candidate, manifest)


def test_load_manifest_rejects_unknown_keys_and_duplicate_json_keys(tmp_path: Path) -> None:
    _candidate(tmp_path / "candidate")
    unknown = tmp_path / "unknown.json"
    unknown.write_text(
        '{"schema_version":1,"schema_version":1,"unexpected":true}',
        encoding="utf-8",
    )
    with pytest.raises(ReleaseCandidateIdentityError, match="strict UTF-8 JSON"):
        load_manifest(unknown)

    candidate, manifest = _manifested_candidate(tmp_path / "second")
    del candidate
    document = _read_document(manifest)
    document["unexpected"] = True
    _write_document(manifest, document)
    with pytest.raises(ReleaseCandidateIdentityError, match="top-level schema"):
        load_manifest(manifest)


@pytest.mark.parametrize(
    "invalid_path",
    ["../escape.bin", "folder\\payload.bin", "C:/payload.bin", "bad?.bin"],
)
def test_load_manifest_rejects_unsafe_paths(tmp_path: Path, invalid_path: str) -> None:
    _candidate(tmp_path / "candidate")
    manifest = tmp_path / "unsafe.json"
    document = {
        "algorithm": HASH_ALGORITHM,
        "archive_root": ARCHIVE_ROOT,
        "file_count": 1,
        "files": [{"path": invalid_path, "sha256": "0" * 64, "size": 0}],
        "schema_version": 1,
        "tesseract_exe_sha256": "0" * 64,
        "tree_sha256": "0" * 64,
    }
    _write_document(manifest, document)

    with pytest.raises(ReleaseCandidateIdentityError, match="path|POSIX|Windows"):
        load_manifest(manifest)


def test_load_manifest_rejects_unsorted_and_case_colliding_file_tables(tmp_path: Path) -> None:
    _, manifest = _manifested_candidate(tmp_path)
    document = _read_document(manifest)
    document["files"] = list(reversed(document["files"]))
    _write_document(manifest, document)
    with pytest.raises(ReleaseCandidateIdentityError, match="sorted"):
        load_manifest(manifest)

    _, second_manifest = _manifested_candidate(tmp_path / "second")
    document = _read_document(second_manifest)
    duplicate = dict(document["files"][0])
    duplicate["path"] = "tesseract.EXE"
    document["files"].append(duplicate)
    document["files"].sort(key=lambda entry: entry["path"])
    document["file_count"] += 1
    _write_document(second_manifest, document)
    with pytest.raises(ReleaseCandidateIdentityError, match="case folding"):
        load_manifest(second_manifest)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("file-count", "file list"),
        ("uppercase-hash", "invalid SHA-256"),
        ("boolean-size", "invalid size"),
        ("missing-executable", "missing required root file"),
        ("tree-digest", "tree_sha256 does not match"),
        ("executable-digest", "executable digest does not match"),
    ],
)
def test_load_manifest_fails_closed_on_internally_inconsistent_identity_fields(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    _, manifest = _manifested_candidate(tmp_path)
    document = _read_document(manifest)
    if mutation == "file-count":
        document["file_count"] += 1
    elif mutation == "uppercase-hash":
        document["files"][0]["sha256"] = document["files"][0]["sha256"].upper()
    elif mutation == "boolean-size":
        document["files"][0]["size"] = True
    elif mutation == "missing-executable":
        document["files"] = [
            entry for entry in document["files"] if entry["path"] != EXECUTABLE_PATH
        ]
        document["file_count"] = len(document["files"])
    elif mutation == "tree-digest":
        document["tree_sha256"] = "0" * 64
    else:
        document["tesseract_exe_sha256"] = "0" * 64
    _write_document(manifest, document)

    with pytest.raises(ReleaseCandidateIdentityError, match=message):
        load_manifest(manifest)


def test_verify_zip_accepts_only_the_exact_manifested_tree(tmp_path: Path) -> None:
    candidate, manifest = _manifested_candidate(tmp_path)
    archive = tmp_path / "portable.zip"
    _write_candidate_zip(archive, candidate)

    verified = verify_zip(archive, manifest)

    assert verified.tesseract_exe_sha256 == _read_document(manifest)["tesseract_exe_sha256"]


def test_create_zip_uses_canonical_posix_members_and_exact_candidate_identity(
    tmp_path: Path,
) -> None:
    candidate, manifest = _manifested_candidate(tmp_path)
    archive = tmp_path / "portable.zip"

    created = create_zip(candidate, manifest, archive)

    with zipfile.ZipFile(archive, "r") as payload:
        names = [info.orig_filename for info in payload.infolist()]
    assert names == [f"{ARCHIVE_ROOT}/{entry.path}" for entry in created.files]
    assert all("\\" not in name for name in names)
    assert all(not name.endswith("/") for name in names)
    assert verify_zip(archive, manifest) == created


def test_create_zip_refuses_candidate_self_inclusion_and_existing_output(tmp_path: Path) -> None:
    candidate, manifest = _manifested_candidate(tmp_path)

    with pytest.raises(ReleaseCandidateIdentityError, match="outside"):
        create_zip(candidate, manifest, candidate / "portable.zip")

    archive = tmp_path / "portable.zip"
    archive.write_bytes(b"existing")
    with pytest.raises(ReleaseCandidateIdentityError, match="overwrite"):
        create_zip(candidate, manifest, archive)
    assert archive.read_bytes() == b"existing"


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("tamper", "SHA-256"),
        ("missing", "missing files"),
        ("extra", "unexpected files"),
    ],
)
def test_verify_zip_rejects_content_or_file_set_changes(
    tmp_path: Path,
    case: str,
    message: str,
) -> None:
    candidate, manifest = _manifested_candidate(tmp_path)
    archive = tmp_path / "portable.zip"
    if case == "tamper":
        _write_candidate_zip(
            archive,
            candidate,
            replacements={"_internal/payload.bin": b"runtime-tamper!"},
        )
    elif case == "missing":
        _write_candidate_zip(archive, candidate, omit=frozenset({"_internal/payload.bin"}))
    else:
        _write_candidate_zip(archive, candidate, extras={f"{ARCHIVE_ROOT}/extra.bin": b"extra"})

    with pytest.raises(ReleaseCandidateIdentityError, match=message):
        verify_zip(archive, manifest)


@pytest.mark.parametrize(
    "unsafe_member",
    [
        f"{ARCHIVE_ROOT}/../escape.bin",
        f"{ARCHIVE_ROOT}\\escape.bin",
        "/Tesseract/escape.bin",
        "C:/Tesseract/escape.bin",
    ],
)
def test_verify_zip_rejects_zip_slip_and_non_posix_members(
    tmp_path: Path,
    unsafe_member: str,
) -> None:
    candidate, manifest = _manifested_candidate(tmp_path)
    archive = tmp_path / "unsafe.zip"
    stored_member = unsafe_member.replace("\\", "/")
    _write_candidate_zip(archive, candidate, extras={stored_member: b"escape"})
    if "\\" in unsafe_member:
        payload = archive.read_bytes()
        stored_bytes = stored_member.encode("ascii")
        unsafe_bytes = unsafe_member.encode("ascii")
        assert payload.count(stored_bytes) == 2  # local header and central directory
        archive.write_bytes(payload.replace(stored_bytes, unsafe_bytes))

    with pytest.raises(ReleaseCandidateIdentityError, match="unsafe|POSIX|outside|Windows"):
        verify_zip(archive, manifest)


def test_verify_zip_rejects_wrong_top_level_root(tmp_path: Path) -> None:
    candidate, manifest = _manifested_candidate(tmp_path)
    archive = tmp_path / "wrong-root.zip"
    _write_candidate_zip(archive, candidate, root="NotTesseract")

    with pytest.raises(ReleaseCandidateIdentityError, match="required Tesseract"):
        verify_zip(archive, manifest)


def test_verify_zip_rejects_duplicate_and_case_colliding_members(tmp_path: Path) -> None:
    candidate, manifest = _manifested_candidate(tmp_path)
    duplicate = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(duplicate, "w") as archive:
        archive.writestr(f"{ARCHIVE_ROOT}/{EXECUTABLE_PATH}", b"first")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr(f"{ARCHIVE_ROOT}/{EXECUTABLE_PATH}", b"second")
    with pytest.raises(ReleaseCandidateIdentityError, match="collide"):
        verify_zip(duplicate, manifest)

    collision = tmp_path / "case-collision.zip"
    _write_candidate_zip(
        collision,
        candidate,
        extras={f"{ARCHIVE_ROOT}/tesseract.EXE": b"case collision"},
    )
    with pytest.raises(ReleaseCandidateIdentityError, match="case folding"):
        verify_zip(collision, manifest)


def test_verify_zip_rejects_symbolic_link_members(tmp_path: Path) -> None:
    candidate, manifest = _manifested_candidate(tmp_path)
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for source in sorted(path for path in candidate.rglob("*") if path.is_file()):
            relative = source.relative_to(candidate).as_posix()
            output.writestr(f"{ARCHIVE_ROOT}/{relative}", source.read_bytes())
        link = zipfile.ZipInfo(f"{ARCHIVE_ROOT}/linked.bin")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        output.writestr(link, b"_internal/payload.bin")

    with pytest.raises(ReleaseCandidateIdentityError, match="symbolic link"):
        verify_zip(archive, manifest)


def test_verify_installed_allows_only_exact_root_inno_uninstaller_files(tmp_path: Path) -> None:
    candidate, manifest = _manifested_candidate(tmp_path)
    installed = tmp_path / "installed"
    shutil.copytree(candidate, installed)
    (installed / "unins000.exe").write_bytes(b"inno uninstaller")
    (installed / "unins000.dat").write_bytes(b"inno metadata")

    verify_installed(installed, manifest)

    (installed / "unins000.msg").write_bytes(b"not whitelisted")
    with pytest.raises(ReleaseCandidateIdentityError, match="unexpected files"):
        verify_installed(installed, manifest)


@pytest.mark.parametrize("relative", ["UNINS000.EXE", "_internal/unins000.exe"])
def test_verify_installed_rejects_case_variants_and_nested_uninstaller_names(
    tmp_path: Path,
    relative: str,
) -> None:
    candidate, manifest = _manifested_candidate(tmp_path)
    installed = tmp_path / "installed"
    shutil.copytree(candidate, installed)
    extra = installed / Path(relative)
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_bytes(b"not the exact Inno whitelist path")

    with pytest.raises(ReleaseCandidateIdentityError, match="unexpected files"):
        verify_installed(installed, manifest)


def test_verify_installed_does_not_treat_a_directory_as_a_whitelisted_file(tmp_path: Path) -> None:
    candidate, manifest = _manifested_candidate(tmp_path)
    installed = tmp_path / "installed"
    shutil.copytree(candidate, installed)
    (installed / "unins000.exe").mkdir()

    with pytest.raises(ReleaseCandidateIdentityError, match="regular files"):
        verify_installed(installed, manifest)
