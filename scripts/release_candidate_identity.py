from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable


SCHEMA_VERSION = 1
HASH_ALGORITHM = "SHA-256"
ARCHIVE_ROOT = "Tesseract"
EXECUTABLE_PATH = "Tesseract.exe"
INSTALLER_GENERATED_FILES = frozenset({"unins000.exe", "unins000.dat"})

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_MANIFEST_KEYS = {
    "algorithm",
    "archive_root",
    "file_count",
    "files",
    "schema_version",
    "tesseract_exe_sha256",
    "tree_sha256",
}
_FILE_KEYS = {"path", "sha256", "size"}
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_MAX_MANIFEST_FILES = 100_000
_MAX_FILE_SIZE = (1 << 63) - 1
_READ_BLOCK_SIZE = 1024 * 1024
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_WINDOWS_FORBIDDEN_CHARACTERS = frozenset('<>:"/\\|?*')


class ReleaseCandidateIdentityError(ValueError):
    """Raised when a release candidate cannot be identified exactly."""


@dataclass(frozen=True)
class FileIdentity:
    path: str
    size: int
    sha256: str

    def as_document(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class CandidateManifest:
    files: tuple[FileIdentity, ...]
    tree_sha256: str
    tesseract_exe_sha256: str

    def as_document(self) -> dict[str, object]:
        return {
            "algorithm": HASH_ALGORITHM,
            "archive_root": ARCHIVE_ROOT,
            "file_count": len(self.files),
            "files": [entry.as_document() for entry in self.files],
            "schema_version": SCHEMA_VERSION,
            "tesseract_exe_sha256": self.tesseract_exe_sha256,
            "tree_sha256": self.tree_sha256,
        }


@dataclass(frozen=True)
class _DirectoryInventory:
    files: tuple[FileIdentity, ...]
    directories: frozenset[str]


class _DuplicateJsonKey(ValueError):
    pass


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number: {value}")


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(attributes & _REPARSE_ATTRIBUTE)


def _inspect_regular_input(path: Path, label: str) -> os.stat_result:
    try:
        result = path.lstat()
    except OSError as exc:
        raise ReleaseCandidateIdentityError(f"Could not inspect {label}: {path}") from exc
    if stat.S_ISLNK(result.st_mode) or _is_reparse_point(result):
        raise ReleaseCandidateIdentityError(f"{label.capitalize()} must not be a link or reparse point.")
    if not stat.S_ISREG(result.st_mode):
        raise ReleaseCandidateIdentityError(f"{label.capitalize()} is not a regular file: {path}")
    return result


def _same_file_snapshot(before: os.stat_result, after: os.stat_result) -> bool:
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    return all(getattr(before, field, None) == getattr(after, field, None) for field in fields)


def _validate_component(component: str) -> None:
    if not component or component in {".", ".."}:
        raise ReleaseCandidateIdentityError("Candidate paths must not contain empty or dot components.")
    if unicodedata.normalize("NFC", component) != component:
        raise ReleaseCandidateIdentityError("Candidate paths must use NFC-normalized Unicode.")
    if any(ord(character) < 32 or ord(character) == 127 for character in component):
        raise ReleaseCandidateIdentityError("Candidate paths must not contain control characters.")
    if any(character in _WINDOWS_FORBIDDEN_CHARACTERS for character in component):
        raise ReleaseCandidateIdentityError("Candidate paths contain a character forbidden by Windows.")
    if component.endswith((" ", ".")):
        raise ReleaseCandidateIdentityError("Candidate path components must not end in a space or dot.")
    if component.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise ReleaseCandidateIdentityError("Candidate paths must not use Windows reserved device names.")


def _validate_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseCandidateIdentityError("Every candidate path must be a non-empty string.")
    if "\\" in value or value.startswith("/"):
        raise ReleaseCandidateIdentityError("Candidate paths must be relative POSIX paths.")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise ReleaseCandidateIdentityError("Candidate paths must be canonical relative POSIX paths.")
    for component in path.parts:
        _validate_component(component)
    return value


def _casefold_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _validate_unique_paths(paths: Iterable[str]) -> None:
    seen: dict[str, str] = {}
    for path in paths:
        key = _casefold_key(path)
        prior = seen.get(key)
        if prior is not None:
            raise ReleaseCandidateIdentityError(
                f"Candidate paths collide under Windows case folding: {prior!r} and {path!r}."
            )
        seen[key] = path


def _calculate_tree_sha256(files: Iterable[FileIdentity]) -> str:
    digest = hashlib.sha256()
    digest.update(b"Tesseract release candidate tree\x00v1\x00")
    for entry in files:
        path_bytes = entry.path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(4, "big"))
        digest.update(path_bytes)
        digest.update(entry.size.to_bytes(8, "big"))
        digest.update(bytes.fromhex(entry.sha256))
    return digest.hexdigest()


def _hash_stream(stream: BinaryIO, *, expected_size: int | None = None) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    while True:
        block = stream.read(_READ_BLOCK_SIZE)
        if not block:
            break
        size += len(block)
        if expected_size is not None and size > expected_size:
            raise ReleaseCandidateIdentityError("Candidate file expanded beyond its declared size.")
        digest.update(block)
    return size, digest.hexdigest()


def _hash_regular_file(path: Path) -> tuple[int, str]:
    try:
        before = path.lstat()
        if _is_reparse_point(before) or stat.S_ISLNK(before.st_mode):
            raise ReleaseCandidateIdentityError(f"Candidate contains a link or reparse point: {path}")
        if not stat.S_ISREG(before.st_mode):
            raise ReleaseCandidateIdentityError(f"Candidate contains a non-regular file: {path}")
        with path.open("rb") as handle:
            size, digest = _hash_stream(handle, expected_size=before.st_size)
        after = path.lstat()
    except ReleaseCandidateIdentityError:
        raise
    except OSError as exc:
        raise ReleaseCandidateIdentityError(f"Could not hash candidate file: {path}") from exc
    if not _same_file_snapshot(before, after):
        raise ReleaseCandidateIdentityError(f"Candidate file changed while it was being hashed: {path}")
    if size != before.st_size:
        raise ReleaseCandidateIdentityError(f"Candidate file size changed while it was being hashed: {path}")
    return size, digest


def _validate_directory_root(root: Path) -> Path:
    try:
        resolved = root.resolve(strict=True)
        root_stat = root.lstat()
    except OSError as exc:
        raise ReleaseCandidateIdentityError(f"Candidate directory is not accessible: {root}") from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise ReleaseCandidateIdentityError(f"Candidate path is not a directory: {root}")
    if stat.S_ISLNK(root_stat.st_mode) or _is_reparse_point(root_stat):
        raise ReleaseCandidateIdentityError("Candidate directory must not be a link or reparse point.")
    return resolved


def _inventory_directory(root: Path) -> _DirectoryInventory:
    resolved = _validate_directory_root(root)
    pending: list[tuple[Path, tuple[str, ...]]] = [(resolved, ())]
    file_paths: list[tuple[str, Path]] = []
    directory_paths: list[str] = []
    all_paths: list[str] = []
    while pending:
        current, relative_parts = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise ReleaseCandidateIdentityError(f"Could not enumerate candidate directory: {current}") from exc
        for entry in entries:
            candidate_parts = (*relative_parts, entry.name)
            relative = "/".join(candidate_parts)
            _validate_relative_path(relative)
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ReleaseCandidateIdentityError(f"Could not inspect candidate path: {relative}") from exc
            if entry.is_symlink() or _is_reparse_point(entry_stat):
                raise ReleaseCandidateIdentityError(
                    f"Candidate contains a link or reparse point: {relative}"
                )
            if stat.S_ISDIR(entry_stat.st_mode):
                directory_paths.append(relative)
                all_paths.append(relative)
                pending.append((Path(entry.path), candidate_parts))
            elif stat.S_ISREG(entry_stat.st_mode):
                file_paths.append((relative, Path(entry.path)))
                all_paths.append(relative)
            else:
                raise ReleaseCandidateIdentityError(
                    f"Candidate contains a non-regular filesystem entry: {relative}"
                )
    _validate_unique_paths(all_paths)
    file_paths.sort(key=lambda item: item[0])
    files = tuple(
        FileIdentity(path=relative, size=size, sha256=digest)
        for relative, absolute in file_paths
        for size, digest in (_hash_regular_file(absolute),)
    )
    # Detect additions, removals, and renames that happened during hashing.
    final_paths = _list_directory_paths_without_hashing(resolved)
    if final_paths != frozenset(all_paths):
        raise ReleaseCandidateIdentityError("Candidate directory changed while it was being inventoried.")
    return _DirectoryInventory(files=files, directories=frozenset(directory_paths))


def _list_directory_paths_without_hashing(root: Path) -> frozenset[str]:
    pending: list[tuple[Path, tuple[str, ...]]] = [(root, ())]
    paths: list[str] = []
    while pending:
        current, relative_parts = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise ReleaseCandidateIdentityError(f"Could not re-enumerate candidate directory: {current}") from exc
        for entry in entries:
            candidate_parts = (*relative_parts, entry.name)
            relative = "/".join(candidate_parts)
            _validate_relative_path(relative)
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ReleaseCandidateIdentityError(f"Could not re-inspect candidate path: {relative}") from exc
            if entry.is_symlink() or _is_reparse_point(entry_stat):
                raise ReleaseCandidateIdentityError(
                    f"Candidate contains a link or reparse point: {relative}"
                )
            if stat.S_ISDIR(entry_stat.st_mode):
                paths.append(relative)
                pending.append((Path(entry.path), candidate_parts))
            elif stat.S_ISREG(entry_stat.st_mode):
                paths.append(relative)
            else:
                raise ReleaseCandidateIdentityError(
                    f"Candidate contains a non-regular filesystem entry: {relative}"
                )
    _validate_unique_paths(paths)
    return frozenset(paths)


def _manifest_from_files(files: tuple[FileIdentity, ...]) -> CandidateManifest:
    if not 1 <= len(files) <= _MAX_MANIFEST_FILES:
        raise ReleaseCandidateIdentityError("Candidate contains an unsupported number of files.")
    if any(not 0 <= entry.size <= _MAX_FILE_SIZE for entry in files):
        raise ReleaseCandidateIdentityError("Candidate contains a file whose size cannot be represented safely.")
    executable = next((entry for entry in files if entry.path == EXECUTABLE_PATH), None)
    if executable is None:
        raise ReleaseCandidateIdentityError(f"Candidate is missing required root file {EXECUTABLE_PATH}.")
    return CandidateManifest(
        files=files,
        tree_sha256=_calculate_tree_sha256(files),
        tesseract_exe_sha256=executable.sha256,
    )


def _ensure_path_outside_directory(
    directory: Path,
    external_path: Path,
    label: str,
) -> None:
    root = _validate_directory_root(directory)
    try:
        external_resolved = external_path.resolve(strict=False)
    except OSError as exc:
        raise ReleaseCandidateIdentityError(f"Could not resolve {label.lower()} path: {external_path}") from exc
    try:
        external_resolved.relative_to(root)
    except ValueError:
        return
    raise ReleaseCandidateIdentityError(f"{label} must be stored outside the candidate directory.")


def create_manifest(directory: Path, manifest_path: Path) -> CandidateManifest:
    _ensure_path_outside_directory(directory, manifest_path, "Candidate manifest")
    if manifest_path.exists():
        raise ReleaseCandidateIdentityError(f"Refusing to overwrite existing manifest: {manifest_path}")
    inventory = _inventory_directory(directory)
    manifest = _manifest_from_files(inventory.files)
    content = (json.dumps(manifest.as_document(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    created_output = False
    try:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with manifest_path.open("xb") as handle:
            created_output = True
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        if created_output:
            try:
                manifest_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise ReleaseCandidateIdentityError(f"Could not create candidate manifest: {manifest_path}") from exc
    return manifest


def load_manifest(manifest_path: Path) -> CandidateManifest:
    before = _inspect_regular_input(manifest_path, "candidate manifest")
    try:
        if before.st_size > _MAX_MANIFEST_BYTES:
            raise ReleaseCandidateIdentityError("Candidate manifest is unreasonably large.")
        raw = manifest_path.read_bytes()
    except ReleaseCandidateIdentityError:
        raise
    except OSError as exc:
        raise ReleaseCandidateIdentityError(f"Could not read candidate manifest: {manifest_path}") from exc
    after = _inspect_regular_input(manifest_path, "candidate manifest")
    if not _same_file_snapshot(before, after) or len(raw) != before.st_size:
        raise ReleaseCandidateIdentityError("Candidate manifest changed while it was being read.")
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey, ValueError, RecursionError) as exc:
        raise ReleaseCandidateIdentityError("Candidate manifest must be strict UTF-8 JSON.") from exc
    if not isinstance(document, dict) or set(document) != _MANIFEST_KEYS:
        raise ReleaseCandidateIdentityError("Candidate manifest has an unsupported top-level schema.")
    if type(document["schema_version"]) is not int or document["schema_version"] != SCHEMA_VERSION:
        raise ReleaseCandidateIdentityError("Candidate manifest schema_version is unsupported.")
    if document["algorithm"] != HASH_ALGORITHM or document["archive_root"] != ARCHIVE_ROOT:
        raise ReleaseCandidateIdentityError("Candidate manifest algorithm or archive root is unsupported.")
    file_count = document["file_count"]
    raw_files = document["files"]
    if type(file_count) is not int or file_count < 1 or file_count > _MAX_MANIFEST_FILES:
        raise ReleaseCandidateIdentityError("Candidate manifest file_count is invalid.")
    if not isinstance(raw_files, list) or len(raw_files) != file_count:
        raise ReleaseCandidateIdentityError("Candidate manifest file list does not match file_count.")
    files: list[FileIdentity] = []
    for raw_entry in raw_files:
        if not isinstance(raw_entry, dict) or set(raw_entry) != _FILE_KEYS:
            raise ReleaseCandidateIdentityError("Candidate manifest contains a malformed file entry.")
        path = _validate_relative_path(raw_entry["path"])
        size_value = raw_entry["size"]
        checksum = raw_entry["sha256"]
        if type(size_value) is not int or not 0 <= size_value <= _MAX_FILE_SIZE:
            raise ReleaseCandidateIdentityError(f"Candidate manifest has an invalid size for {path}.")
        if not isinstance(checksum, str) or _SHA256_RE.fullmatch(checksum) is None:
            raise ReleaseCandidateIdentityError(f"Candidate manifest has an invalid SHA-256 for {path}.")
        files.append(FileIdentity(path=path, size=size_value, sha256=checksum))
    if files != sorted(files, key=lambda entry: entry.path):
        raise ReleaseCandidateIdentityError("Candidate manifest file entries must be sorted by POSIX path.")
    _validate_unique_paths(entry.path for entry in files)
    manifest = _manifest_from_files(tuple(files))
    tree_sha256 = document["tree_sha256"]
    executable_sha256 = document["tesseract_exe_sha256"]
    if not isinstance(tree_sha256, str) or _SHA256_RE.fullmatch(tree_sha256) is None:
        raise ReleaseCandidateIdentityError("Candidate manifest tree_sha256 is invalid.")
    if not isinstance(executable_sha256, str) or _SHA256_RE.fullmatch(executable_sha256) is None:
        raise ReleaseCandidateIdentityError("Candidate manifest tesseract_exe_sha256 is invalid.")
    if tree_sha256 != manifest.tree_sha256:
        raise ReleaseCandidateIdentityError("Candidate manifest tree_sha256 does not match its file table.")
    if executable_sha256 != manifest.tesseract_exe_sha256:
        raise ReleaseCandidateIdentityError(
            f"Candidate manifest executable digest does not match {EXECUTABLE_PATH}."
        )
    return manifest


def _format_paths(paths: Iterable[str]) -> str:
    values = sorted(paths)
    preview = ", ".join(repr(value) for value in values[:5])
    if len(values) > 5:
        preview += f", ... ({len(values)} total)"
    return preview


def _verify_file_identities(
    manifest: CandidateManifest,
    actual_files: tuple[FileIdentity, ...],
    *,
    allowed_extra_paths: frozenset[str] = frozenset(),
) -> None:
    expected_by_path = {entry.path: entry for entry in manifest.files}
    actual_by_path = {entry.path: entry for entry in actual_files}
    missing = set(expected_by_path) - set(actual_by_path)
    unexpected = set(actual_by_path) - set(expected_by_path) - set(allowed_extra_paths)
    if missing:
        raise ReleaseCandidateIdentityError(f"Candidate is missing files: {_format_paths(missing)}")
    if unexpected:
        raise ReleaseCandidateIdentityError(f"Candidate contains unexpected files: {_format_paths(unexpected)}")
    for path, expected in expected_by_path.items():
        actual = actual_by_path[path]
        if actual.size != expected.size:
            raise ReleaseCandidateIdentityError(f"Candidate file size differs from manifest: {path}")
        if actual.sha256 != expected.sha256:
            raise ReleaseCandidateIdentityError(f"Candidate file SHA-256 differs from manifest: {path}")
    verified_files = tuple(actual_by_path[entry.path] for entry in manifest.files)
    if _calculate_tree_sha256(verified_files) != manifest.tree_sha256:
        raise ReleaseCandidateIdentityError("Candidate tree SHA-256 differs from manifest.")
    if actual_by_path[EXECUTABLE_PATH].sha256 != manifest.tesseract_exe_sha256:
        raise ReleaseCandidateIdentityError(f"Candidate {EXECUTABLE_PATH} SHA-256 differs from manifest.")


def verify_directory(directory: Path, manifest_path: Path) -> CandidateManifest:
    _ensure_path_outside_directory(directory, manifest_path, "Candidate manifest")
    manifest = load_manifest(manifest_path)
    inventory = _inventory_directory(directory)
    _verify_file_identities(manifest, inventory.files)
    return manifest


def verify_installed(directory: Path, manifest_path: Path) -> CandidateManifest:
    _ensure_path_outside_directory(directory, manifest_path, "Candidate manifest")
    manifest = load_manifest(manifest_path)
    inventory = _inventory_directory(directory)
    forbidden_directory_names = inventory.directories & INSTALLER_GENERATED_FILES
    if forbidden_directory_names:
        raise ReleaseCandidateIdentityError(
            "Installer-generated whitelist entries must be regular files: "
            + _format_paths(forbidden_directory_names)
        )
    _verify_file_identities(
        manifest,
        inventory.files,
        allowed_extra_paths=INSTALLER_GENERATED_FILES,
    )
    return manifest


def create_zip(directory: Path, manifest_path: Path, zip_path: Path) -> CandidateManifest:
    _ensure_path_outside_directory(directory, manifest_path, "Candidate manifest")
    _ensure_path_outside_directory(directory, zip_path, "Candidate ZIP")
    manifest = verify_directory(directory, manifest_path)
    try:
        if zip_path.resolve(strict=False) == manifest_path.resolve(strict=True):
            raise ReleaseCandidateIdentityError("Candidate ZIP and identity manifest must be separate files.")
    except OSError as exc:
        raise ReleaseCandidateIdentityError("Could not resolve candidate ZIP output path.") from exc
    if zip_path.exists():
        raise ReleaseCandidateIdentityError(f"Refusing to overwrite existing candidate ZIP: {zip_path}")
    created_output = False
    try:
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(
            zip_path,
            "x",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=True,
            strict_timestamps=False,
        ) as archive:
            created_output = True
            for entry in manifest.files:
                source = directory.joinpath(*PurePosixPath(entry.path).parts)
                archive.write(source, f"{ARCHIVE_ROOT}/{entry.path}")
        verify_zip(zip_path, manifest_path)
        verify_directory(directory, manifest_path)
    except ReleaseCandidateIdentityError:
        if created_output:
            zip_path.unlink(missing_ok=True)
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        if created_output:
            zip_path.unlink(missing_ok=True)
        raise ReleaseCandidateIdentityError(f"Could not create candidate ZIP: {zip_path}") from exc
    return manifest


def _validate_zip_member(info: zipfile.ZipInfo) -> tuple[str | None, bool]:
    name = info.orig_filename
    if name != info.filename:
        raise ReleaseCandidateIdentityError(
            f"ZIP member is not a canonical relative POSIX path: {name!r}"
        )
    if not isinstance(name, str) or not name or "\\" in name or name.startswith("/"):
        raise ReleaseCandidateIdentityError("ZIP members must use canonical relative POSIX paths.")
    is_directory = info.is_dir()
    if name.endswith("/") != is_directory:
        raise ReleaseCandidateIdentityError(f"ZIP member has inconsistent directory metadata: {name!r}")
    trimmed = name[:-1] if is_directory else name
    if not trimmed:
        raise ReleaseCandidateIdentityError("ZIP contains an empty member path.")
    parts = trimmed.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ReleaseCandidateIdentityError(f"ZIP contains an unsafe member path: {name!r}")
    for component in parts:
        _validate_component(component)
    if parts[0] != ARCHIVE_ROOT:
        raise ReleaseCandidateIdentityError(f"ZIP member is outside the required {ARCHIVE_ROOT}/ root: {name!r}")
    if len(parts) == 1:
        if not is_directory:
            raise ReleaseCandidateIdentityError(f"ZIP root {ARCHIVE_ROOT} must be a directory.")
        relative: str | None = None
    else:
        relative = _validate_relative_path("/".join(parts[1:]))
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    unix_type = stat.S_IFMT(unix_mode)
    if unix_type == stat.S_IFLNK:
        raise ReleaseCandidateIdentityError(f"ZIP contains a symbolic link: {name!r}")
    if unix_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ReleaseCandidateIdentityError(f"ZIP contains a special filesystem member: {name!r}")
    if unix_type == stat.S_IFDIR and not is_directory:
        raise ReleaseCandidateIdentityError(f"ZIP member type conflicts with its path: {name!r}")
    if unix_type == stat.S_IFREG and is_directory:
        raise ReleaseCandidateIdentityError(f"ZIP member type conflicts with its path: {name!r}")
    if info.external_attr & _REPARSE_ATTRIBUTE:
        raise ReleaseCandidateIdentityError(f"ZIP contains a reparse-point member: {name!r}")
    if info.flag_bits & 0x1:
        raise ReleaseCandidateIdentityError(f"ZIP contains an encrypted member: {name!r}")
    if is_directory and info.file_size != 0:
        raise ReleaseCandidateIdentityError(f"ZIP directory member has non-zero content: {name!r}")
    return relative, is_directory


def verify_zip(zip_path: Path, manifest_path: Path) -> CandidateManifest:
    manifest = load_manifest(manifest_path)
    zip_before = _inspect_regular_input(zip_path, "candidate ZIP")
    expected_by_path = {entry.path: entry for entry in manifest.files}
    expected_directories = {ARCHIVE_ROOT}
    for path in expected_by_path:
        parts = path.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            expected_directories.add(f"{ARCHIVE_ROOT}/{'/'.join(parts[:index])}")
    try:
        archive = zipfile.ZipFile(zip_path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseCandidateIdentityError(f"Could not open candidate ZIP: {zip_path}") from exc
    with archive:
        infos: dict[str, zipfile.ZipInfo] = {}
        seen_members: dict[str, str] = {}
        archive_infos = archive.infolist()
        maximum_members = len(expected_by_path) + len(expected_directories)
        if len(archive_infos) > maximum_members:
            raise ReleaseCandidateIdentityError("Candidate ZIP contains too many members for its manifest.")
        for info in archive_infos:
            relative, is_directory = _validate_zip_member(info)
            normalized_full = info.filename[:-1] if is_directory else info.filename
            key = _casefold_key(normalized_full)
            prior = seen_members.get(key)
            if prior is not None:
                raise ReleaseCandidateIdentityError(
                    f"ZIP members collide under Windows case folding: {prior!r} and {info.filename!r}."
                )
            seen_members[key] = info.filename
            if is_directory:
                if normalized_full not in expected_directories:
                    raise ReleaseCandidateIdentityError(
                        f"ZIP contains an unexpected directory member: {info.filename!r}"
                    )
                continue
            assert relative is not None
            infos[relative] = info
        missing = set(expected_by_path) - set(infos)
        unexpected = set(infos) - set(expected_by_path)
        if missing:
            raise ReleaseCandidateIdentityError(f"Candidate ZIP is missing files: {_format_paths(missing)}")
        if unexpected:
            raise ReleaseCandidateIdentityError(
                f"Candidate ZIP contains unexpected files: {_format_paths(unexpected)}"
            )
        actual_files: list[FileIdentity] = []
        for expected in manifest.files:
            info = infos[expected.path]
            if info.file_size != expected.size:
                raise ReleaseCandidateIdentityError(
                    f"Candidate ZIP member size differs from manifest: {expected.path}"
                )
            try:
                with archive.open(info, "r") as handle:
                    size, digest = _hash_stream(handle, expected_size=expected.size)
            except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as exc:
                raise ReleaseCandidateIdentityError(
                    f"Could not read candidate ZIP member: {expected.path}"
                ) from exc
            actual_files.append(FileIdentity(path=expected.path, size=size, sha256=digest))
    zip_after = _inspect_regular_input(zip_path, "candidate ZIP")
    if not _same_file_snapshot(zip_before, zip_after):
        raise ReleaseCandidateIdentityError("Candidate ZIP changed while it was being verified.")
    _verify_file_identities(manifest, tuple(actual_files))
    return manifest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or verify an immutable Tesseract release-candidate identity."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="Create a candidate identity manifest.")
    create.add_argument("--directory", required=True, type=Path)
    create.add_argument("--manifest", required=True, type=Path)
    verify_dir = subparsers.add_parser("verify-dir", help="Verify an exact candidate directory.")
    verify_dir.add_argument("--directory", required=True, type=Path)
    verify_dir.add_argument("--manifest", required=True, type=Path)
    verify_zip_parser = subparsers.add_parser("verify-zip", help="Verify an exact portable ZIP.")
    verify_zip_parser.add_argument("--zip", required=True, type=Path)
    verify_zip_parser.add_argument("--manifest", required=True, type=Path)
    create_zip_parser = subparsers.add_parser(
        "create-zip", help="Create a canonical portable ZIP from an exact candidate."
    )
    create_zip_parser.add_argument("--directory", required=True, type=Path)
    create_zip_parser.add_argument("--manifest", required=True, type=Path)
    create_zip_parser.add_argument("--zip", required=True, type=Path)
    verify_installed_parser = subparsers.add_parser(
        "verify-installed", help="Verify an installed directory with the strict Inno whitelist."
    )
    verify_installed_parser.add_argument("--directory", required=True, type=Path)
    verify_installed_parser.add_argument("--manifest", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            manifest = create_manifest(args.directory, args.manifest)
        elif args.command == "verify-dir":
            manifest = verify_directory(args.directory, args.manifest)
        elif args.command == "verify-zip":
            manifest = verify_zip(args.zip, args.manifest)
        elif args.command == "create-zip":
            manifest = create_zip(args.directory, args.manifest, args.zip)
        else:
            manifest = verify_installed(args.directory, args.manifest)
    except ReleaseCandidateIdentityError as exc:
        parser.error(str(exc))
    print(
        f"{args.command} verified tree_sha256={manifest.tree_sha256} "
        f"{EXECUTABLE_PATH}={manifest.tesseract_exe_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
