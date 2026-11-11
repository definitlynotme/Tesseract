from __future__ import annotations

import io
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts.audit_public_release import (
    AuditFailure,
    AuditLimits,
    AuditReport,
    REQUIRED_SOURCE_COMPONENTS,
    SOURCE_BUNDLE_DOCUMENTS,
    audit_public_release,
    load_private_deny_file,
    main,
    scan_bytes,
    scan_payload,
)
from scripts.build_third_party_sources import (
    CPYTHON_WINDOWS_LICENSE_COMPONENT,
    CPYTHON_WINDOWS_LICENSE_MARKERS,
    CPYTHON_WINDOWS_VERSION,
)


PROJECT_NAME = "Tesseract contributors"
PROJECT_EMAIL = "123456+tesseract-proxy@users.noreply.github.com"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr


def _repo(
    root: Path,
    *,
    name: str = PROJECT_NAME,
    email: str = PROJECT_EMAIL,
) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", name)
    _git(repo, "config", "user.email", email)
    (repo / "README.md").write_text("safe public tree\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--quiet", "-m", "Initial public tree")
    return repo


def _deny_file(root: Path, value: str) -> Path:
    path = root / "private-deny.txt"
    path.write_text(f"# private values; never commit\n{value}\n", encoding="utf-8")
    return path


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _pinned_source_bundle(
    repo: Path,
    *,
    mihomo_payload: bytes,
    cpython_payload: bytes | None = None,
    source_mutation: tuple[str, bytes] | None = None,
    extra_member: tuple[str, bytes] | None = None,
    shell_rust: list[dict[str, str]] | None = None,
    shell_drop: frozenset[str] = frozenset(),
) -> Path:
    """The template holds every pinned component. With ``shell_rust`` the bundle
    is the Tauri-shell form: Qt sources (and ``shell_drop``) left out, the given
    MPL-2.0 crate components (name, version, source_url; optional license,
    source_path) added."""
    source_payloads: dict[str, bytes] = {}
    components: list[dict[str, str]] = []
    for name in sorted(REQUIRED_SOURCE_COMPONENTS):
        suffix = ".zip" if name == "mihomo" else ".source"
        source_path = f"{name}{suffix}"
        if name == "mihomo":
            payload = mihomo_payload
        elif name == CPYTHON_WINDOWS_LICENSE_COMPONENT:
            payload = cpython_payload or (
                "\n".join(CPYTHON_WINDOWS_LICENSE_MARKERS) + "\n"
            ).encode()
        else:
            payload = f"source:{name}\n".encode()
        source_payloads[name] = payload
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
                "source_url": f"https://example.invalid/{source_path}",
                "license": "GPL-3.0-only",
            }
        )
    manifest = {"schema_version": 1, "components": components}
    packaging = repo / "packaging"
    packaging.mkdir(exist_ok=True)
    manifest_path = packaging / "third_party_sources.template.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _git(repo, "add", "packaging/third_party_sources.template.json")
    _git(repo, "commit", "--quiet", "-m", "Pin source inputs")

    bundled = components
    if shell_rust is not None:
        bundled = [
            component
            for component in components
            if component["name"] not in {"pyside-shiboken", "qtbase", "qtsvg", *shell_drop}
        ]
        for crate in shell_rust:
            payload = f"crate:{crate['name']}\n".encode()
            source_path = crate.get(
                "source_path", f"{crate['name']}-{crate['version']}-vendored-source.zip"
            )
            source_payloads[crate["name"]] = payload
            bundled = [*bundled, {
                "name": crate["name"],
                "version": crate["version"],
                "source_path": source_path,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "source_url": crate["source_url"],
                "license": crate.get("license", "MPL-2.0"),
            }]
    entries = {
        "SOURCE-MANIFEST.json": json.dumps(
            {"schema_version": 1, "components": bundled}
        ).encode("utf-8"),
        **{name: f"document:{name}\n".encode() for name in SOURCE_BUNDLE_DOCUMENTS},
    }
    for component in bundled:
        name = component["name"]
        member_name = (
            f"sources/{name}-{component['version']}/"
            f"{Path(component['source_path']).name}"
        )
        entries[member_name] = source_payloads[name]
    if source_mutation is not None:
        entries[source_mutation[0]] = source_mutation[1]
    if extra_member is not None:
        entries[extra_member[0]] = extra_member[1]
    release = repo / "release"
    release.mkdir(exist_ok=True)
    bundle = release / "Tesseract-0.1.0-third-party-sources.zip"
    bundle.write_bytes(_zip_bytes(entries))
    return bundle


def test_pinned_source_bundle_requires_cpython_windows_acknowledgements(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    deny = _deny_file(tmp_path, "private-marker-not-present-43852")
    bundle = _pinned_source_bundle(
        repo,
        mihomo_payload=_zip_bytes({"README.md": b"reviewed source\n"}),
        cpython_payload=b"PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2\n",
    )

    with pytest.raises(
        AuditFailure,
        match="cpython-windows-license-acknowledgements-missing",
    ):
        audit_public_release(
            repo_root=repo,
            deny_file=deny,
            artifact_paths=(bundle,),
        )


def test_clean_project_identity_and_tree_pass(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    deny = _deny_file(tmp_path, "private-marker-not-present-43852")

    report = audit_public_release(repo_root=repo, deny_file=deny)

    assert report.passed
    assert report.tracked_files == 1
    assert report.git_objects >= 3
    assert report.artifact_files == 0


def test_git_identity_rejects_personal_author_and_committer_without_leaking(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    personal_name = "Private Maintainer 94731"
    personal_email = "private-maintainer-94731@outlook.invalid"
    repo = _repo(tmp_path, name=personal_name, email=personal_email)
    deny = _deny_file(tmp_path, personal_email)

    exit_code = main(
        ["--repo-root", str(repo), "--deny-file", str(deny)]
    )
    output = capsys.readouterr()

    assert exit_code == 1
    assert "git-author-name" in output.out
    assert "git-author-email" in output.out
    assert "git-committer-name" in output.out
    assert "git-committer-email" in output.out
    assert personal_name not in output.out + output.err
    assert personal_email not in output.out + output.err


def test_forbidden_value_is_found_in_history_after_tracked_file_is_deleted(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    secret = "historical-private-value-583927"
    leaked = repo / "leaked.txt"
    leaked.write_text(secret, encoding="utf-8")
    _git(repo, "add", "leaked.txt")
    _git(repo, "commit", "--quiet", "-m", "Temporary private material")
    leaked.unlink()
    _git(repo, "add", "-u")
    _git(repo, "commit", "--quiet", "-m", "Remove private material")
    deny = _deny_file(tmp_path, secret)

    report = audit_public_release(repo_root=repo, deny_file=deny)

    assert not report.passed
    assert any(
        finding.rule == "caller-forbidden-1"
        and finding.source_kind == "git-object"
        for finding in report.findings
    )


def test_recursive_zip_and_utf16_binary_strings_are_scanned_without_echoing_secret(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _repo(tmp_path)
    secret = "nested-installer-private-value-492781"
    release = repo / "release"
    release.mkdir()
    inner = _zip_bytes(
        {"payload.exe": b"MZ\x00\x00" + secret.encode("utf-16-le") + b"\x00\x00"}
    )
    outer = release / "portable.zip"
    outer.write_bytes(_zip_bytes({"nested/update.zip": inner}))
    deny = _deny_file(tmp_path, secret)

    exit_code = main(
        [
            "--repo-root",
            str(repo),
            "--deny-file",
            str(deny),
            "--artifact",
            str(outer),
        ]
    )
    output = capsys.readouterr()

    assert exit_code == 1
    assert "caller-forbidden-1" in output.out
    assert "archive-member" in output.out
    assert secret not in output.out + output.err


def test_hash_pinned_upstream_source_fixture_skips_only_generic_secret_rules(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    private_key_fixture = (
        "-----BEGIN " + "PRIVATE KEY-----\n" + ("A" * 80)
    ).encode("ascii")
    proxy_fixture = (
        "vless" + "://11111111-2222-4333-8444-555555555555@"
        "upstream-fixture.invalidtld:443"
    ).encode("ascii")
    mihomo_source = _zip_bytes(
        {"test/fixtures/credentials.txt": private_key_fixture + b"\n" + proxy_fixture}
    )
    bundle = _pinned_source_bundle(repo, mihomo_payload=mihomo_source)
    deny = _deny_file(tmp_path, "maintainer-private-value-not-present-98127")

    report = audit_public_release(
        repo_root=repo,
        deny_file=deny,
        artifact_paths=(bundle,),
    )

    assert report.passed


def test_pinned_upstream_source_still_scans_private_deny_values(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    private_value = "real-maintainer-private-value-12759"
    mihomo_source = _zip_bytes(
        {"internal/private.txt": private_value.encode("utf-8")}
    )
    bundle = _pinned_source_bundle(repo, mihomo_payload=mihomo_source)
    deny = _deny_file(tmp_path, private_value)

    report = audit_public_release(
        repo_root=repo,
        deny_file=deny,
        artifact_paths=(bundle,),
    )

    assert any(
        finding.rule == "caller-forbidden-1" for finding in report.findings
    )


def test_pinned_source_hash_mismatch_and_extra_source_fail_closed(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "hash-mismatch"
    first_root.mkdir()
    repo = _repo(first_root)
    original = _zip_bytes({"source.txt": b"reviewed upstream fixture"})
    component_member = "sources/mihomo-1.0.0/mihomo.zip"
    mismatched = _pinned_source_bundle(
        repo,
        mihomo_payload=original,
        source_mutation=(component_member, b"changed after manifest review"),
    )
    deny = _deny_file(first_root, "private-value-38472")

    with pytest.raises(AuditFailure) as hash_error:
        audit_public_release(
            repo_root=repo,
            deny_file=deny,
            artifact_paths=(mismatched,),
        )
    assert hash_error.value.code == "pinned-source-hash-mismatch"

    second_root = tmp_path / "extra-source"
    second_root.mkdir()
    repo = _repo(second_root)
    extra = _pinned_source_bundle(
        repo,
        mihomo_payload=original,
        extra_member=("sources/unreviewed-1.0/source.zip", b"unreviewed"),
    )
    deny = _deny_file(second_root, "private-value-92714")
    with pytest.raises(AuditFailure) as member_error:
        audit_public_release(
            repo_root=repo,
            deny_file=deny,
            artifact_paths=(extra,),
        )
    assert member_error.value.code == "pinned-source-member-set-mismatch"


_SHELL_CRATE = {
    "name": "rust-option-ext",
    "version": "0.2.0",
    "source_url": "https://crates.io/api/v1/crates/option-ext/0.2.0/download",
}


def test_shell_source_bundle_without_qt_and_with_mpl_crates_passes(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    bundle = _pinned_source_bundle(
        repo,
        mihomo_payload=_zip_bytes({"README.md": b"reviewed source\n"}),
        shell_rust=[_SHELL_CRATE],
    )
    deny = _deny_file(tmp_path, "private-value-not-present-55120")

    report = audit_public_release(
        repo_root=repo, deny_file=deny, artifact_paths=(bundle,)
    )

    assert report.passed


@pytest.mark.parametrize(
    "crate,dropped,code",
    [
        ({**_SHELL_CRATE, "license": "GPL-3.0-only"}, frozenset(),
         "bundled-rust-source-component-invalid"),
        ({**_SHELL_CRATE, "source_url": "https://example.invalid/option-ext.zip"},
         frozenset(), "bundled-rust-source-component-invalid"),
        ({**_SHELL_CRATE, "source_path": "anything.zip"}, frozenset(),
         "bundled-rust-source-component-invalid"),
        (_SHELL_CRATE, frozenset({"wintun-license"}), "bundled-source-manifest-mismatch"),
    ],
)
def test_shell_source_bundle_fails_closed(
    tmp_path: Path, crate: dict[str, str], dropped: frozenset[str], code: str
) -> None:
    repo = _repo(tmp_path)
    bundle = _pinned_source_bundle(
        repo,
        mihomo_payload=_zip_bytes({"README.md": b"reviewed source\n"}),
        shell_rust=[crate],
        shell_drop=dropped,
    )
    deny = _deny_file(tmp_path, "private-value-not-present-55121")

    with pytest.raises(AuditFailure) as error:
        audit_public_release(repo_root=repo, deny_file=deny, artifact_paths=(bundle,))
    assert error.value.code == code


def test_binary_strings_detect_local_user_path_and_real_proxy_uri() -> None:
    report = AuditReport()
    user = "maintainer" + "83927"
    proxy = (
        "vless" + "://11111111-2222-4333-8444-555555555555@"
        "edge.private-network.invalidtld:443#primary"
    )
    separator = "\\"
    payload = (
        b"MZ\x00\x00"
        + f"C:{separator}Users{separator}{user}{separator}project"
        f"{separator}build.pdb".encode("utf-16-le")
        + b"\x00\x00"
        + proxy.encode("ascii")
    )

    scan_bytes(
        payload,
        deny_values=(),
        report=report,
        source_kind="artifact",
        location="installer",
        allowed_local_users=frozenset(),
    )

    rules = {finding.rule for finding in report.findings}
    assert "local-user-path" in rules
    assert "real-proxy-uri" in rules


def test_only_exact_hash_pinned_upstream_binary_skips_upstream_build_path() -> None:
    payload = (REPO_ROOT / "bin" / "wintun.dll").read_bytes()
    trusted = AuditReport()
    scan_payload(
        payload,
        name_hint="_internal/bin/wintun.dll",
        deny_values=(),
        report=trusted,
        source_kind="artifact",
        location="trusted-wintun",
        allowed_local_users=frozenset(),
        limits=AuditLimits(),
    )
    assert not any(finding.rule == "local-user-path" for finding in trusted.findings)

    modified = AuditReport()
    scan_payload(
        payload + b"modified",
        name_hint="_internal/bin/wintun.dll",
        deny_values=(),
        report=modified,
        source_kind="artifact",
        location="modified-wintun",
        allowed_local_users=frozenset(),
        limits=AuditLimits(),
    )
    assert any(finding.rule == "local-user-path" for finding in modified.findings)


def test_exact_pinned_upstream_binary_is_recognized_in_git_object_scan(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    runtime = repo / "bin" / "wintun.dll"
    runtime.parent.mkdir()
    runtime.write_bytes((REPO_ROOT / "bin" / "wintun.dll").read_bytes())
    _git(repo, "add", "bin/wintun.dll")
    _git(repo, "commit", "--quiet", "-m", "Add pinned runtime")
    deny = _deny_file(tmp_path, "private-value-not-present-287451")

    report = audit_public_release(repo_root=repo, deny_file=deny)

    assert report.passed
    assert report.git_objects >= 6


def test_private_deny_file_inside_repository_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    deny = repo / "private-deny.txt"
    deny.write_text("private-value-92837\n", encoding="utf-8")

    with pytest.raises(AuditFailure) as error:
        load_private_deny_file(deny, repo.resolve())

    assert error.value.code == "deny-file-inside-repository"


def test_additional_allowed_git_email_must_still_be_numeric_id_noreply(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    deny = _deny_file(tmp_path, "private-value-72193")

    with pytest.raises(AuditFailure) as error:
        audit_public_release(
            repo_root=repo,
            deny_file=deny,
            allowed_git_emails=("maintainer@example.invalid",),
        )

    assert error.value.code == "allowed-git-email-not-id-noreply"


def test_shallow_repository_is_refused_because_not_all_objects_are_available(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    deny = _deny_file(tmp_path, "private-value-84219")
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="ascii",
    ).stdout.strip()
    (repo / ".git" / "shallow").write_text(head + "\n", encoding="ascii")

    with pytest.raises(AuditFailure) as error:
        audit_public_release(repo_root=repo, deny_file=deny)

    assert error.value.code == "shallow-repository-refused"


def test_user_data_artifact_directory_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    deny = _deny_file(tmp_path, "private-value-31982")
    data = repo / "data"
    data.mkdir()
    (data / "runtime-state.json").write_text("{}", encoding="utf-8")

    with pytest.raises(AuditFailure) as error:
        audit_public_release(
            repo_root=repo,
            deny_file=deny,
            artifact_paths=(data,),
        )

    assert error.value.code == "artifact-user-data-refused"


def test_private_key_container_artifact_is_a_finding(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    deny = _deny_file(tmp_path, "private-value-21679")
    release = repo / "release"
    release.mkdir()
    key_container = release / "signing.pfx"
    key_container.write_bytes(b"binary-key-container")

    report = audit_public_release(
        repo_root=repo,
        deny_file=deny,
        artifact_paths=(key_container,),
    )

    assert any(
        finding.rule == "private-key-container" for finding in report.findings
    )


def test_malformed_zip_and_size_limits_fail_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    deny = _deny_file(tmp_path, "private-value-67129")
    release = repo / "release"
    release.mkdir()
    malformed = release / "bad.zip"
    malformed.write_bytes(b"not a zip")

    with pytest.raises(AuditFailure) as malformed_error:
        audit_public_release(
            repo_root=repo,
            deny_file=deny,
            artifact_paths=(malformed,),
        )
    assert malformed_error.value.code == "zip-extension-without-zip-data"

    oversized = release / "large.bin"
    oversized.write_bytes(b"x" * 2048)
    with pytest.raises(AuditFailure) as size_error:
        audit_public_release(
            repo_root=repo,
            deny_file=deny,
            artifact_paths=(oversized,),
            limits=AuditLimits(max_file_bytes=1024),
        )
    assert size_error.value.code == "artifact-file-size-limit"


def test_high_confidence_token_rule_does_not_require_private_value() -> None:
    token = "gh" + "p_" + ("A" * 40)
    report = AuditReport()

    scan_bytes(
        token.encode("ascii"),
        deny_values=(),
        report=report,
        source_kind="tracked-file",
        location="source",
        allowed_local_users=frozenset(),
    )

    assert any(finding.rule == "github-classic-token" for finding in report.findings)
