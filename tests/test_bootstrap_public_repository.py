from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.bootstrap_public_repository import (
    BootstrapFailure,
    PROJECT_GIT_NAME,
    bootstrap_public_repository,
    main,
)


GITHUB_ID = "12345678"
GITHUB_LOGIN = "tesseract-release"
GITHUB_EMAIL = (
    f"{GITHUB_ID}+{GITHUB_LOGIN}@users.noreply.github.com"
)


def _run(*command: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        cwd=str(cwd) if cwd is not None else None,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr
    return completed


def _git(repo: Path, *args: str) -> str:
    return _run("git", "-C", str(repo), *args).stdout


def _source_repo(root: Path) -> Path:
    source = root / "private-source"
    source.mkdir()
    _git(source, "init", "--quiet", "--initial-branch=private-main")
    _git(source, "config", "user.name", "Legacy Private Author")
    _git(source, "config", "user.email", "legacy-private@example.invalid")
    (source / ".gitignore").write_text("ignored.txt\nforced.txt\n", encoding="utf-8")
    (source / "README.md").write_text("safe tracked tree\n", encoding="utf-8")
    _git(source, "add", ".gitignore", "README.md")
    _git(source, "commit", "--quiet", "-m", "Private history")
    _git(source, "branch", "legacy-private-ref")
    _git(source, "tag", "private-v0")
    _git(source, "remote", "add", "legacy-private-remote", "example.invalid/repo")
    (source / "new.txt").write_text("safe untracked candidate\n", encoding="utf-8")
    (source / "ignored.txt").write_text("must stay private\n", encoding="utf-8")
    return source


def _key(root: Path) -> Path:
    key = root / "root-commit-signing-key"
    _run(
        "ssh-keygen",
        "-q",
        "-t",
        "ed25519",
        "-N",
        "",
        "-C",
        "tesseract-bootstrap-test",
        "-f",
        str(key),
    )
    return key


def _deny(root: Path, value: str = "private-marker-not-present-824731") -> Path:
    deny = root / "private-deny.txt"
    deny.write_text(f"# external private values\n{value}\n", encoding="utf-8")
    return deny


def _target(root: Path) -> Path:
    target = root / "public-root"
    target.mkdir()
    return target


def _bootstrap(root: Path):
    source = _source_repo(root)
    key = _key(root)
    deny = _deny(root)
    target = _target(root)
    result = bootstrap_public_repository(
        source_repo=source,
        target_dir=target,
        github_id=GITHUB_ID,
        github_login=GITHUB_LOGIN,
        ssh_signing_key=key,
        deny_file=deny,
    )
    return source, target, key, result


def test_bootstrap_copies_exact_candidate_tree_into_one_signed_root(
    tmp_path: Path,
) -> None:
    source, target, key, result = _bootstrap(tmp_path)

    assert result.candidate_files == 3
    assert _git(target, "branch", "--show-current").strip() == "main"
    assert _git(target, "rev-list", "--all", "--count").strip() == "1"
    assert _git(target, "remote").strip() == ""
    assert _git(target, "status", "--porcelain", "--untracked-files=all") == ""
    assert _git(target, "show", "-s", "--format=%P", "HEAD").strip() == ""
    assert _git(target, "show", "-s", "--format=%an", "HEAD").strip() == PROJECT_GIT_NAME
    assert _git(target, "show", "-s", "--format=%ae", "HEAD").strip() == GITHUB_EMAIL
    assert set(_git(target, "ls-tree", "-r", "--name-only", "HEAD").splitlines()) == {
        ".gitignore",
        "README.md",
        "new.txt",
    }
    assert not (target / "ignored.txt").exists()
    assert not (target / ".git" / "refs" / "tags" / "private-v0").exists()
    assert "legacy-private-ref" not in _git(target, "for-each-ref")
    assert "legacy-private-remote" in _git(source, "remote")

    allowed_signers = tmp_path / "allowed_signers"
    public_key = Path(str(key) + ".pub").read_text(encoding="ascii").split()
    allowed_signers.write_text(
        f"{GITHUB_EMAIL} {public_key[0]} {public_key[1]}\n",
        encoding="ascii",
    )
    _git(
        target,
        "-c",
        f"gpg.ssh.allowedSignersFile={allowed_signers}",
        "verify-commit",
        "HEAD",
    )


def test_bootstrap_excludes_worktree_deleted_tracked_files(tmp_path: Path) -> None:
    source = _source_repo(tmp_path)
    internal_record = source / "INTERNAL_RELEASE_RECORD.md"
    internal_record.write_text("private release notes\n", encoding="utf-8")
    _git(source, "add", internal_record.name)
    _git(source, "commit", "--quiet", "-m", "Private internal record")
    internal_record.unlink()

    target = _target(tmp_path)
    result = bootstrap_public_repository(
        source_repo=source,
        target_dir=target,
        github_id=GITHUB_ID,
        github_login=GITHUB_LOGIN,
        ssh_signing_key=_key(tmp_path),
        deny_file=_deny(tmp_path),
    )

    assert result.candidate_files == 3
    assert "INTERNAL_RELEASE_RECORD.md" not in _git(
        target,
        "ls-tree",
        "-r",
        "--name-only",
        "HEAD",
    ).splitlines()


@pytest.mark.parametrize(
    ("github_id", "github_login", "expected_code"),
    [
        ("", GITHUB_LOGIN, "github-id-invalid"),
        ("0", GITHUB_LOGIN, "github-id-invalid"),
        (GITHUB_ID, "Uppercase-Login", "github-login-invalid"),
        (GITHUB_ID, "bad--login", "github-login-invalid"),
    ],
)
def test_missing_or_invalid_identity_fails_before_copy(
    tmp_path: Path,
    github_id: str,
    github_login: str,
    expected_code: str,
) -> None:
    source = _source_repo(tmp_path)
    key = _key(tmp_path)
    target = _target(tmp_path)

    with pytest.raises(BootstrapFailure) as error:
        bootstrap_public_repository(
            source_repo=source,
            target_dir=target,
            github_id=github_id,
            github_login=github_login,
            ssh_signing_key=key,
            deny_file=_deny(tmp_path),
        )

    assert error.value.code == expected_code
    assert list(target.iterdir()) == []


def test_unusable_signing_key_fails_before_copy(tmp_path: Path) -> None:
    source = _source_repo(tmp_path)
    invalid_key = tmp_path / "not-an-ssh-key"
    invalid_key.write_text("not a key\n", encoding="utf-8")
    target = _target(tmp_path)

    with pytest.raises(BootstrapFailure) as error:
        bootstrap_public_repository(
            source_repo=source,
            target_dir=target,
            github_id=GITHUB_ID,
            github_login=GITHUB_LOGIN,
            ssh_signing_key=invalid_key,
            deny_file=_deny(tmp_path),
        )

    assert error.value.code in {
        "ssh-public-key-unavailable",
        "ssh-signing-key-not-ed25519",
    }
    assert list(target.iterdir()) == []


def test_tracked_file_matching_ignore_rule_is_refused_without_force_add(
    tmp_path: Path,
) -> None:
    source = _source_repo(tmp_path)
    (source / "forced.txt").write_text("historically force-added\n", encoding="utf-8")
    _git(source, "add", "-f", "forced.txt")
    _git(source, "commit", "--quiet", "-m", "Old forced file")
    target = _target(tmp_path)

    with pytest.raises(BootstrapFailure) as error:
        bootstrap_public_repository(
            source_repo=source,
            target_dir=target,
            github_id=GITHUB_ID,
            github_login=GITHUB_LOGIN,
            ssh_signing_key=_key(tmp_path),
            deny_file=_deny(tmp_path),
        )

    assert error.value.code == "candidate-matches-ignore-rule"
    assert list(target.iterdir()) == []


def test_deny_finding_removes_staging_and_leaves_target_empty(tmp_path: Path) -> None:
    secret = "maintainer-private-marker-519284"
    source = _source_repo(tmp_path)
    (source / "README.md").write_text(f"unsafe {secret}\n", encoding="utf-8")
    target = _target(tmp_path)

    with pytest.raises(BootstrapFailure) as error:
        bootstrap_public_repository(
            source_repo=source,
            target_dir=target,
            github_id=GITHUB_ID,
            github_login=GITHUB_LOGIN,
            ssh_signing_key=_key(tmp_path),
            deny_file=_deny(tmp_path, secret),
        )

    assert error.value.code == "public-audit-precommit-findings"
    assert list(target.iterdir()) == []
    assert not any(
        path.name.startswith(".tesseract-public-bootstrap-")
        for path in tmp_path.iterdir()
    )


def test_external_sensitive_inputs_are_required(tmp_path: Path) -> None:
    source = _source_repo(tmp_path)
    key = source / "signing-key"
    _run(
        "ssh-keygen",
        "-q",
        "-t",
        "ed25519",
        "-N",
        "",
        "-f",
        str(key),
    )
    deny = source / "deny.txt"
    deny.write_text("private-marker-718295\n", encoding="utf-8")
    target = _target(tmp_path)

    with pytest.raises(BootstrapFailure) as deny_error:
        bootstrap_public_repository(
            source_repo=source,
            target_dir=target,
            github_id=GITHUB_ID,
            github_login=GITHUB_LOGIN,
            ssh_signing_key=key,
            deny_file=deny,
        )
    assert deny_error.value.code == "deny-file-must-be-external"
    assert list(target.iterdir()) == []

    with pytest.raises(BootstrapFailure) as key_error:
        bootstrap_public_repository(
            source_repo=source,
            target_dir=target,
            github_id=GITHUB_ID,
            github_login=GITHUB_LOGIN,
            ssh_signing_key=key,
            deny_file=_deny(tmp_path),
        )
    assert key_error.value.code == "ssh-signing-key-must-be-external"
    assert list(target.iterdir()) == []


def test_nonempty_target_is_refused(tmp_path: Path) -> None:
    source = _source_repo(tmp_path)
    target = _target(tmp_path)
    marker = target / "keep.txt"
    marker.write_text("do not overwrite\n", encoding="utf-8")

    with pytest.raises(BootstrapFailure) as error:
        bootstrap_public_repository(
            source_repo=source,
            target_dir=target,
            github_id=GITHUB_ID,
            github_login=GITHUB_LOGIN,
            ssh_signing_key=_key(tmp_path),
            deny_file=_deny(tmp_path),
        )

    assert error.value.code == "target-directory-not-empty"
    assert marker.read_text(encoding="utf-8") == "do not overwrite\n"


def test_cli_error_does_not_echo_sensitive_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _source_repo(tmp_path)
    target = _target(tmp_path)
    sensitive_path = tmp_path / "maintainer-private-key-name"
    sensitive_path.write_text("invalid\n", encoding="utf-8")

    exit_code = main(
        [
            "--source-repo",
            str(source),
            "--target-dir",
            str(target),
            "--github-id",
            GITHUB_ID,
            "--github-login",
            GITHUB_LOGIN,
            "--ssh-signing-key",
            str(sensitive_path),
            "--deny-file",
            str(_deny(tmp_path)),
        ]
    )
    output = capsys.readouterr()

    assert exit_code == 2
    assert "PUBLIC_REPOSITORY_BOOTSTRAP=ERROR" in output.err
    assert str(sensitive_path) not in output.out + output.err
