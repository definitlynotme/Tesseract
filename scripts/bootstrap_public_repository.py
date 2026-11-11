#!/usr/bin/env python3
"""Create a clean, SSH-signed public root repository without network access.

The source repository is treated only as a file-tree provider.  Its refs,
objects, remotes, hooks, and local Git configuration are never copied.  The
destination must already exist and be empty.  A signing probe is completed
before any candidate file is copied.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

try:
    from scripts.audit_public_release import (
        AuditFailure,
        GITHUB_ID_NOREPLY_RE,
        audit_public_release,
        load_private_deny_file,
        resolve_repo_root,
    )
except ModuleNotFoundError:  # Direct execution from the scripts directory.
    from audit_public_release import (  # type: ignore[no-redef]
        AuditFailure,
        GITHUB_ID_NOREPLY_RE,
        audit_public_release,
        load_private_deny_file,
        resolve_repo_root,
    )


PROJECT_GIT_NAME = "Tesseract contributors"
ROOT_COMMIT_MESSAGE = "Initial public release source tree"
LOGIN_RE = re.compile(
    r"[a-z0-9](?:(?:[a-z0-9]|-(?!-)){0,37}[a-z0-9])?"
)
SSH_ED25519_RE = re.compile(
    rb"(?:ssh-ed25519|sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/]+={0,3}"
)
MAX_KEY_BYTES = 1024 * 1024
MAX_CANDIDATE_FILE_BYTES = 512 * 1024 * 1024
MAX_CANDIDATE_TREE_BYTES = 2 * 1024 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 60
SIGNING_TIMEOUT_SECONDS = 30


class BootstrapFailure(RuntimeError):
    """A fail-closed error whose code is safe to print."""

    def __init__(self, code: str, *, safe_details: Iterable[str] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.safe_details = tuple(
            detail
            for detail in safe_details
            if re.fullmatch(r"[a-z0-9-]{1,80}", detail)
        )


@dataclass(frozen=True)
class CandidateFile:
    relative_path: str
    mode: str
    size: int
    sha256: str


@dataclass(frozen=True)
class BootstrapResult:
    commit: str
    candidate_files: int
    candidate_bytes: int
    audit_git_objects: int


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _base_git_environment(*, isolated: bool) -> dict[str, str]:
    environment = os.environ.copy()
    refused_exact = {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_AUTHOR_DATE",
        "GIT_AUTHOR_EMAIL",
        "GIT_AUTHOR_NAME",
        "GIT_COMMITTER_DATE",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    }
    for key in tuple(environment):
        if (
            key in refused_exact
            or key == "GIT_CONFIG_COUNT"
            or key.startswith("GIT_CONFIG_KEY_")
            or key.startswith("GIT_CONFIG_VALUE_")
        ):
            environment.pop(key, None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["SSH_ASKPASS_REQUIRE"] = "never"
    environment["GIT_ATTR_NOSYSTEM"] = "1"
    if isolated:
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
    return environment


def _run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
    isolated_git: bool = False,
    timeout: int = GIT_TIMEOUT_SECONDS,
    allowed_returncodes: Iterable[int] = (0,),
    error_code: str,
) -> subprocess.CompletedProcess[bytes]:
    try:
        run_options: dict[str, object] = {
            "cwd": os.fspath(cwd) if cwd is not None else None,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": _base_git_environment(isolated=isolated_git),
            "timeout": timeout,
            "check": False,
        }
        if input_bytes is None:
            run_options["stdin"] = subprocess.DEVNULL
        else:
            run_options["input"] = input_bytes
        completed = subprocess.run(list(command), **run_options)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BootstrapFailure(error_code) from exc
    if completed.returncode not in frozenset(allowed_returncodes):
        raise BootstrapFailure(error_code)
    return completed


def _git(
    repo: Path,
    *args: str,
    isolated: bool,
    input_bytes: bytes | None = None,
    allowed_returncodes: Iterable[int] = (0,),
    error_code: str = "git-command-failed",
    timeout: int = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    return _run(
        ["git", "-C", os.fspath(repo), *args],
        input_bytes=input_bytes,
        isolated_git=isolated,
        timeout=timeout,
        allowed_returncodes=allowed_returncodes,
        error_code=error_code,
    )


def _decode_nul_paths(payload: bytes, *, error_code: str) -> tuple[str, ...]:
    paths: list[str] = []
    seen: set[str] = set()
    for raw_path in (item for item in payload.split(b"\x00") if item):
        try:
            relative = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BootstrapFailure(error_code) from exc
        pure = PurePosixPath(relative)
        if (
            not relative
            or pure.is_absolute()
            or ".." in pure.parts
            or any(part.casefold() == ".git" for part in pure.parts)
            or "\\" in relative
        ):
            raise BootstrapFailure("candidate-path-unsafe")
        folded = relative.casefold()
        if folded in seen:
            raise BootstrapFailure("candidate-path-collision")
        seen.add(folded)
        paths.append(relative)
    return tuple(paths)


def _tracked_modes(source: Path) -> dict[str, str]:
    payload = _git(
        source,
        "ls-files",
        "--stage",
        "-z",
        isolated=False,
        error_code="source-index-unreadable",
    ).stdout
    modes: dict[str, str] = {}
    for record in (item for item in payload.split(b"\x00") if item):
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, _object_id, stage = metadata.decode("ascii").split(" ")
            relative = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise BootstrapFailure("source-index-malformed") from exc
        if stage != "0" or relative in modes:
            raise BootstrapFailure("source-index-unmerged")
        if mode not in {"100644", "100755"}:
            raise BootstrapFailure("source-index-mode-refused")
        modes[relative] = mode
    return modes


def _has_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except AttributeError:
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _remove_bootstrap_tree(path: Path) -> None:
    """Remove only a bootstrap-owned tree, including read-only Git objects."""

    def make_writable_and_retry(function, target, _error_info) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(path, onerror=make_writable_and_retry)


def _candidate_paths(source: Path) -> tuple[tuple[str, ...], dict[str, str]]:
    if _git(
        source,
        "ls-files",
        "--unmerged",
        "-z",
        isolated=False,
        error_code="source-index-unreadable",
    ).stdout:
        raise BootstrapFailure("source-index-unmerged")
    payload = _git(
        source,
        "ls-files",
        "-c",
        "-o",
        "--exclude-standard",
        "-z",
        isolated=False,
        error_code="candidate-list-failed",
    ).stdout
    listed_paths = _decode_nul_paths(
        payload,
        error_code="candidate-path-not-utf8",
    )
    deleted_payload = _git(
        source,
        "ls-files",
        "--deleted",
        "-z",
        isolated=False,
        error_code="candidate-deleted-list-failed",
    ).stdout
    deleted_paths = frozenset(
        _decode_nul_paths(
            deleted_payload,
            error_code="candidate-deleted-path-not-utf8",
        )
    )
    paths = tuple(path for path in listed_paths if path not in deleted_paths)
    if not paths:
        raise BootstrapFailure("candidate-tree-empty")

    ignored = _git(
        source,
        "check-ignore",
        "--no-index",
        "-z",
        "--stdin",
        isolated=False,
        input_bytes=b"\x00".join(path.encode("utf-8") for path in paths) + b"\x00",
        allowed_returncodes=(0, 1),
        error_code="candidate-ignore-check-failed",
    )
    if ignored.returncode == 0 and ignored.stdout:
        raise BootstrapFailure("candidate-matches-ignore-rule")
    return paths, _tracked_modes(source)


def _validate_regular_source_file(source: Path, relative: str) -> os.stat_result:
    candidate = source.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = candidate.resolve(strict=True)
        metadata = candidate.lstat()
    except OSError as exc:
        raise BootstrapFailure("candidate-file-unreadable") from exc
    if not _is_relative_to(resolved, source):
        raise BootstrapFailure("candidate-resolves-outside-source")
    current = candidate
    while current != source:
        if current.is_symlink() or _has_reparse_point(current):
            raise BootstrapFailure("candidate-reparse-point-refused")
        current = current.parent
    if not stat.S_ISREG(metadata.st_mode):
        raise BootstrapFailure("candidate-not-regular-file")
    if metadata.st_size > MAX_CANDIDATE_FILE_BYTES:
        raise BootstrapFailure("candidate-file-size-limit")
    return metadata


def _snapshot_candidates(
    source: Path,
    paths: Sequence[str],
    tracked_modes: dict[str, str],
) -> tuple[CandidateFile, ...]:
    candidates: list[CandidateFile] = []
    total_size = 0
    for relative in paths:
        candidate = source.joinpath(*PurePosixPath(relative).parts)
        before = _validate_regular_source_file(source, relative)
        try:
            payload = candidate.read_bytes()
            after = candidate.lstat()
        except OSError as exc:
            raise BootstrapFailure("candidate-file-unreadable") from exc
        if (
            len(payload) != before.st_size
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            raise BootstrapFailure("candidate-changed-during-read")
        total_size += len(payload)
        if total_size > MAX_CANDIDATE_TREE_BYTES:
            raise BootstrapFailure("candidate-tree-size-limit")
        mode = tracked_modes.get(relative)
        if mode is None:
            mode = "100755" if before.st_mode & 0o111 else "100644"
        candidates.append(
            CandidateFile(
                relative_path=relative,
                mode=mode,
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
    return tuple(candidates)


def _validate_identity(github_id: str, github_login: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]{0,19}", github_id):
        raise BootstrapFailure("github-id-invalid")
    if not LOGIN_RE.fullmatch(github_login):
        raise BootstrapFailure("github-login-invalid")
    email = f"{github_id}+{github_login}@users.noreply.github.com"
    if not GITHUB_ID_NOREPLY_RE.fullmatch(email):
        raise BootstrapFailure("github-noreply-email-invalid")
    return email


def _validate_external_file(
    candidate: Path,
    *,
    source: Path,
    target: Path,
    invalid_code: str,
) -> Path:
    try:
        lexical = Path(os.path.abspath(candidate))
        if candidate.is_symlink() or _has_reparse_point(candidate):
            raise BootstrapFailure(invalid_code)
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise BootstrapFailure(invalid_code) from exc
    if (
        not resolved.is_file()
        or resolved.is_symlink()
        or _has_reparse_point(resolved)
        or _is_relative_to(lexical, source)
        or _is_relative_to(resolved, source)
        or _is_relative_to(lexical, target)
        or _is_relative_to(resolved, target)
    ):
        raise BootstrapFailure(invalid_code)
    return resolved


def _read_ssh_public_key(signing_key: Path) -> bytes:
    try:
        if signing_key.stat().st_size <= 0 or signing_key.stat().st_size > MAX_KEY_BYTES:
            raise BootstrapFailure("ssh-signing-key-invalid")
        payload = signing_key.read_bytes()
    except OSError as exc:
        raise BootstrapFailure("ssh-signing-key-invalid") from exc

    for line in payload.splitlines():
        stripped = line.strip()
        match = SSH_ED25519_RE.fullmatch(b" ".join(stripped.split()[:2]))
        if match:
            return match.group(0)

    companion = Path(os.fspath(signing_key) + ".pub")
    if companion.is_file() and not companion.is_symlink():
        try:
            for line in companion.read_bytes().splitlines():
                stripped = line.strip()
                match = SSH_ED25519_RE.fullmatch(b" ".join(stripped.split()[:2]))
                if match:
                    return match.group(0)
        except OSError as exc:
            raise BootstrapFailure("ssh-public-key-unreadable") from exc

    derived = _run(
        ["ssh-keygen", "-y", "-f", os.fspath(signing_key)],
        timeout=SIGNING_TIMEOUT_SECONDS,
        error_code="ssh-public-key-unavailable",
    ).stdout.strip()
    match = SSH_ED25519_RE.fullmatch(b" ".join(derived.split()[:2]))
    if not match:
        raise BootstrapFailure("ssh-signing-key-not-ed25519")
    return match.group(0)


def _configure_repository(repo: Path, *, email: str, signing_key: Path) -> None:
    settings = (
        ("user.name", PROJECT_GIT_NAME),
        ("user.email", email),
        ("gpg.format", "ssh"),
        ("user.signingkey", os.fspath(signing_key)),
        ("commit.gpgsign", "true"),
        ("tag.gpgsign", "true"),
        ("core.autocrlf", "false"),
    )
    for key, value in settings:
        _git(
            repo,
            "config",
            "--local",
            key,
            value,
            isolated=True,
            error_code="git-local-config-failed",
        )


def _verify_signed_commit(
    repo: Path,
    *,
    email: str,
    public_key: bytes,
    allowed_signers: Path,
) -> str:
    try:
        allowed_signers.write_bytes(email.encode("ascii") + b" " + public_key + b"\n")
    except OSError as exc:
        raise BootstrapFailure("allowed-signers-write-failed") from exc
    _git(
        repo,
        "-c",
        f"gpg.ssh.allowedSignersFile={allowed_signers}",
        "verify-commit",
        "HEAD",
        isolated=True,
        error_code="ssh-commit-signature-invalid",
        timeout=SIGNING_TIMEOUT_SECONDS,
    )
    raw_commit = _git(
        repo,
        "cat-file",
        "commit",
        "HEAD",
        isolated=True,
        error_code="root-commit-unreadable",
    ).stdout
    if b"gpgsig -----BEGIN SSH SIGNATURE-----" not in raw_commit:
        raise BootstrapFailure("ssh-commit-signature-missing")
    commit = _git(
        repo,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        isolated=True,
        error_code="root-commit-unreadable",
    ).stdout.decode("ascii", errors="strict").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise BootstrapFailure("root-commit-id-invalid")
    return commit


def _signing_preflight(
    *,
    parent: Path,
    email: str,
    signing_key: Path,
    public_key: bytes,
) -> None:
    try:
        with tempfile.TemporaryDirectory(
            prefix=".tesseract-signing-probe-", dir=parent
        ) as temporary:
            probe = Path(temporary)
            hooks = probe / "empty-hooks"
            hooks.mkdir()
            _git(
                probe,
                "init",
                "--quiet",
                "--initial-branch=main",
                "--template=",
                isolated=True,
                error_code="signing-preflight-init-failed",
            )
            _configure_repository(probe, email=email, signing_key=signing_key)
            (probe / "probe.txt").write_bytes(b"Tesseract SSH signing probe\n")
            _git(
                probe,
                "add",
                "--all",
                "--",
                ".",
                isolated=True,
                error_code="signing-preflight-add-failed",
            )
            _git(
                probe,
                "-c",
                f"core.hooksPath={hooks}",
                "commit",
                "--quiet",
                "-S",
                "-m",
                "Tesseract signing preflight",
                isolated=True,
                error_code="ssh-signing-preflight-failed",
                timeout=SIGNING_TIMEOUT_SECONDS,
            )
            _verify_signed_commit(
                probe,
                email=email,
                public_key=public_key,
                allowed_signers=probe / "allowed_signers",
            )
    except BootstrapFailure:
        raise
    except OSError as exc:
        raise BootstrapFailure("signing-preflight-workspace-failed") from exc


def _copy_candidates(
    source: Path,
    stage: Path,
    candidates: Sequence[CandidateFile],
) -> None:
    for candidate in candidates:
        source_file = source.joinpath(*PurePosixPath(candidate.relative_path).parts)
        target_file = stage.joinpath(*PurePosixPath(candidate.relative_path).parts)
        try:
            payload = source_file.read_bytes()
            if (
                len(payload) != candidate.size
                or hashlib.sha256(payload).hexdigest() != candidate.sha256
            ):
                raise BootstrapFailure("source-changed-before-copy")
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_bytes(payload)
            copied = target_file.read_bytes()
        except BootstrapFailure:
            raise
        except OSError as exc:
            raise BootstrapFailure("candidate-copy-failed") from exc
        if copied != payload:
            raise BootstrapFailure("candidate-copy-verification-failed")


def _verify_source_unchanged(
    source: Path,
    candidates: Sequence[CandidateFile],
) -> None:
    paths, tracked_modes = _candidate_paths(source)
    if tuple(candidate.relative_path for candidate in candidates) != paths:
        raise BootstrapFailure("source-candidate-set-changed")
    current = _snapshot_candidates(source, paths, tracked_modes)
    if current != tuple(candidates):
        raise BootstrapFailure("source-candidate-tree-changed")


def _target_index_entries(repo: Path) -> dict[str, tuple[str, str]]:
    payload = _git(
        repo,
        "ls-files",
        "--stage",
        "-z",
        isolated=True,
        error_code="target-index-unreadable",
    ).stdout
    entries: dict[str, tuple[str, str]] = {}
    for record in (item for item in payload.split(b"\x00") if item):
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_id, stage = metadata.decode("ascii").split(" ")
            relative = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise BootstrapFailure("target-index-malformed") from exc
        if stage != "0" or relative in entries:
            raise BootstrapFailure("target-index-malformed")
        entries[relative] = (mode, object_id)
    return entries


def _verify_target_index(
    repo: Path,
    candidates: Sequence[CandidateFile],
) -> None:
    entries = _target_index_entries(repo)
    expected_paths = {candidate.relative_path for candidate in candidates}
    if set(entries) != expected_paths:
        raise BootstrapFailure("target-index-path-mismatch")
    for candidate in candidates:
        mode, object_id = entries[candidate.relative_path]
        if mode != candidate.mode:
            raise BootstrapFailure("target-index-mode-mismatch")
        blob = _git(
            repo,
            "cat-file",
            "blob",
            object_id,
            isolated=True,
            error_code="target-blob-unreadable",
        ).stdout
        if (
            len(blob) != candidate.size
            or hashlib.sha256(blob).hexdigest() != candidate.sha256
        ):
            raise BootstrapFailure("target-blob-content-mismatch")


def _audit(repo: Path, deny_file: Path, email: str, *, phase: str):
    try:
        report = audit_public_release(
            repo_root=repo,
            deny_file=deny_file,
            allowed_git_names=(PROJECT_GIT_NAME,),
            allowed_git_emails=(email,),
        )
    except AuditFailure as exc:
        raise BootstrapFailure(f"public-audit-{phase}-error-{exc.code}") from exc
    if not report.passed:
        raise BootstrapFailure(
            f"public-audit-{phase}-findings",
            safe_details=sorted({finding.rule for finding in report.findings}),
        )
    return report


def _verify_root_contract(
    repo: Path,
    *,
    email: str,
    candidates: Sequence[CandidateFile],
) -> None:
    refs = _git(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        isolated=True,
        error_code="target-ref-check-failed",
    ).stdout.splitlines()
    if refs != [b"refs/heads/main"]:
        raise BootstrapFailure("target-ref-set-invalid")
    if _git(
        repo,
        "remote",
        isolated=True,
        error_code="target-remote-check-failed",
    ).stdout.strip():
        raise BootstrapFailure("target-remote-present")
    count = _git(
        repo,
        "rev-list",
        "--all",
        "--count",
        isolated=True,
        error_code="target-history-check-failed",
    ).stdout.strip()
    if count != b"1":
        raise BootstrapFailure("target-history-not-single-root")
    identity = _git(
        repo,
        "show",
        "-s",
        "--format=%P%x00%an%x00%ae%x00%cn%x00%ce",
        "HEAD",
        isolated=True,
        error_code="root-identity-check-failed",
    ).stdout.rstrip(b"\n").split(b"\x00")
    expected = [
        b"",
        PROJECT_GIT_NAME.encode("utf-8"),
        email.encode("ascii"),
        PROJECT_GIT_NAME.encode("utf-8"),
        email.encode("ascii"),
    ]
    if identity != expected:
        raise BootstrapFailure("root-identity-invalid")
    tree_paths = _decode_nul_paths(
        _git(
            repo,
            "ls-tree",
            "-r",
            "--name-only",
            "-z",
            "HEAD",
            isolated=True,
            error_code="root-tree-check-failed",
        ).stdout,
        error_code="root-tree-path-not-utf8",
    )
    if set(tree_paths) != {
        candidate.relative_path for candidate in candidates
    }:
        raise BootstrapFailure("root-tree-path-mismatch")
    if _git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        isolated=True,
        error_code="target-status-check-failed",
    ).stdout:
        raise BootstrapFailure("target-worktree-not-clean")


def _prepare_target_directory(target: Path) -> tuple[Path, bool]:
    if not target.exists():
        raise BootstrapFailure("target-directory-must-exist")
    try:
        if target.is_symlink() or _has_reparse_point(target):
            raise BootstrapFailure("target-directory-invalid")
        resolved = target.resolve(strict=True)
    except OSError as exc:
        raise BootstrapFailure("target-directory-invalid") from exc
    if (
        not resolved.is_dir()
        or resolved.is_symlink()
        or _has_reparse_point(resolved)
    ):
        raise BootstrapFailure("target-directory-invalid")
    try:
        if any(resolved.iterdir()):
            raise BootstrapFailure("target-directory-not-empty")
    except BootstrapFailure:
        raise
    except OSError as exc:
        raise BootstrapFailure("target-directory-unreadable") from exc
    return resolved, True


def bootstrap_public_repository(
    *,
    source_repo: Path,
    target_dir: Path,
    github_id: str,
    github_login: str,
    ssh_signing_key: Path,
    deny_file: Path,
) -> BootstrapResult:
    """Create and verify a single-root local public repository."""

    try:
        source = resolve_repo_root(source_repo)
    except AuditFailure as exc:
        raise BootstrapFailure(f"source-repository-{exc.code}") from exc
    target, _target_preexisted = _prepare_target_directory(target_dir)
    if _is_relative_to(target, source) or _is_relative_to(source, target):
        raise BootstrapFailure("target-must-be-outside-source")

    email = _validate_identity(github_id, github_login)
    external_deny = _validate_external_file(
        deny_file,
        source=source,
        target=target,
        invalid_code="deny-file-must-be-external",
    )
    try:
        load_private_deny_file(external_deny, source)
    except AuditFailure as exc:
        raise BootstrapFailure(f"deny-file-{exc.code}") from exc
    signing_key = _validate_external_file(
        ssh_signing_key,
        source=source,
        target=target,
        invalid_code="ssh-signing-key-must-be-external",
    )
    public_key = _read_ssh_public_key(signing_key)

    paths, tracked_modes = _candidate_paths(source)
    candidates = _snapshot_candidates(source, paths, tracked_modes)

    # This must succeed before the staging repository (and therefore any source
    # copy) is created.
    _signing_preflight(
        parent=target.parent,
        email=email,
        signing_key=signing_key,
        public_key=public_key,
    )
    if any(target.iterdir()):
        raise BootstrapFailure("target-directory-changed-before-copy")

    stage_path: Path | None = None
    try:
        stage_path = Path(
            tempfile.mkdtemp(prefix=".tesseract-public-bootstrap-", dir=target.parent)
        )
        hooks = stage_path / ".bootstrap-empty-hooks"
        hooks.mkdir()
        _git(
            stage_path,
            "init",
            "--quiet",
            "--initial-branch=main",
            "--template=",
            isolated=True,
            error_code="target-git-init-failed",
        )
        _configure_repository(stage_path, email=email, signing_key=signing_key)
        _copy_candidates(source, stage_path, candidates)
        _verify_source_unchanged(source, candidates)

        _git(
            stage_path,
            "add",
            "--all",
            "--",
            ".",
            isolated=True,
            error_code="target-git-add-failed",
        )
        for candidate in candidates:
            if candidate.mode == "100755":
                _git(
                    stage_path,
                    "update-index",
                    "--chmod=+x",
                    "--",
                    candidate.relative_path,
                    isolated=True,
                    error_code="target-index-mode-update-failed",
                )
        _verify_target_index(stage_path, candidates)

        # The first pass audits the exact staged blobs before a public history
        # exists.  No commit is created if any content finding is present.
        _audit(stage_path, external_deny, email, phase="precommit")

        # The bootstrap-only empty hooks directory must not enter the root tree.
        shutil.rmtree(hooks)
        _git(
            stage_path,
            "-c",
            "core.hooksPath=NUL" if os.name == "nt" else "core.hooksPath=/dev/null",
            "commit",
            "--quiet",
            "-S",
            "-m",
            ROOT_COMMIT_MESSAGE,
            isolated=True,
            error_code="root-commit-signing-failed",
            timeout=SIGNING_TIMEOUT_SECONDS,
        )
        with tempfile.TemporaryDirectory(
            prefix=".tesseract-verify-", dir=target.parent
        ) as verification_directory:
            commit = _verify_signed_commit(
                stage_path,
                email=email,
                public_key=public_key,
                allowed_signers=Path(verification_directory) / "allowed_signers",
            )
        _verify_root_contract(
            stage_path,
            email=email,
            candidates=candidates,
        )
        final_audit = _audit(stage_path, external_deny, email, phase="postcommit")
        _verify_source_unchanged(source, candidates)

        if any(target.iterdir()):
            raise BootstrapFailure("target-directory-changed-before-promotion")
        target.rmdir()
        try:
            os.replace(stage_path, target)
        except OSError as exc:
            target.mkdir(exist_ok=True)
            raise BootstrapFailure("target-promotion-failed") from exc
        stage_path = None
        return BootstrapResult(
            commit=commit,
            candidate_files=len(candidates),
            candidate_bytes=sum(candidate.size for candidate in candidates),
            audit_git_objects=final_audit.git_objects,
        )
    finally:
        if stage_path is not None:
            _remove_bootstrap_tree(stage_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a network-free, SSH-signed single-root public repository from "
            "the exact non-ignored source candidate tree."
        )
    )
    parser.add_argument("--source-repo", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    parser.add_argument("--github-id", required=True)
    parser.add_argument("--github-login", required=True)
    parser.add_argument("--ssh-signing-key", type=Path, required=True)
    parser.add_argument("--deny-file", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = bootstrap_public_repository(
            source_repo=args.source_repo,
            target_dir=args.target_dir,
            github_id=args.github_id,
            github_login=args.github_login,
            ssh_signing_key=args.ssh_signing_key,
            deny_file=args.deny_file,
        )
    except BootstrapFailure as exc:
        details = (
            " rules=" + ",".join(exc.safe_details)
            if exc.safe_details
            else ""
        )
        print(
            f"PUBLIC_REPOSITORY_BOOTSTRAP=ERROR code={exc.code}{details}",
            file=sys.stderr,
        )
        return 2
    except Exception:
        print("PUBLIC_REPOSITORY_BOOTSTRAP=ERROR code=internal-error", file=sys.stderr)
        return 2
    print(
        "PUBLIC_REPOSITORY_BOOTSTRAP=PASS "
        f"commit={result.commit} candidate_files={result.candidate_files} "
        f"candidate_bytes={result.candidate_bytes} "
        f"git_objects={result.audit_git_objects}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
