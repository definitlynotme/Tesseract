from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from scripts.collect_release_licenses import audit_frozen_distribution, collect
from scripts.verify_release_environment import (
    direct_requirement_names,
    locked_dependency_closure,
    locked_requirements,
    verify_lock_closure,
)


ROOT = Path(__file__).resolve().parents[1]


def _powershell_python_here_string(source: str, variable_name: str) -> str:
    match = re.search(
        rf"^\${re.escape(variable_name)} = @'\n(?P<body>.*?)\n'@$",
        source,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"missing PowerShell Python block: {variable_name}"
    return match.group("body")


def test_stable_source_pe_and_inno_fallback_versions_are_synchronized() -> None:
    app_info_source = (ROOT / "app_info.py").read_text(encoding="utf-8")
    pe_version = (
        ROOT / "packaging" / "windows" / "version_info.txt"
    ).read_text(encoding="utf-8")
    installer = (
        ROOT / "packaging" / "windows" / "Tesseract-shell.iss"
    ).read_text(encoding="utf-8")

    assert 'APP_RELEASE_BASE_VERSION = "0.1.0"' in app_info_source
    assert "APP_RELEASE_CANDIDATE_NUMBER: int | None = None" in app_info_source
    assert 'StringStruct("FileVersion", "0.1.0")' in pe_version
    assert 'StringStruct("ProductVersion", "0.1.0")' in pe_version
    assert "filevers=(0, 1, 0, 65535)" in pe_version
    assert "prodvers=(0, 1, 0, 65535)" in pe_version
    assert '#define AppVersion "0.1.0"' in installer
    assert '#define AppVersionNumeric "0.1.0.65535"' in installer


def test_windows_powershell_5_executes_release_python_c_blocks_without_quote_loss() -> None:
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is required for native argument regression")
    build_script = (ROOT / "scripts" / "build_windows.ps1").read_text(
        encoding="utf-8"
    )
    variable_names = (
        "VersionContractPython",
        "SourceVersionContractPython",
        "AppRepositoryIdentityPython",
        "TrustedUpdateKeyIdsPython",
    )
    python_blocks = {
        name: _powershell_python_here_string(build_script, name)
        for name in variable_names
    }
    # Windows PowerShell 5 removes embedded double quotes while constructing a
    # native command line.  These exact -c payloads deliberately use only
    # Python single-quoted literals.
    assert all('"' not in block for block in python_blocks.values())
    assignments = "\n".join(
        f"${name} = @'\n{block}\n'@" for name, block in python_blocks.items()
    )
    root_literal = str(ROOT).replace("'", "''")
    command = f"""
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath '{root_literal}'
$Python = (Get-Command 'python' -ErrorAction Stop).Source
$Version = '0.1.0'
{assignments}
$Results = [ordered]@{{}}
$Results.version = [string] (& $Python -c $VersionContractPython $Version)
if ($LASTEXITCODE -ne 0) {{ throw 'version contract failed' }}
$Results.source = [string] (& $Python -c $SourceVersionContractPython)
if ($LASTEXITCODE -ne 0) {{ throw 'source contract failed' }}
$Results.repository = [string] (& $Python -c $AppRepositoryIdentityPython)
if ($LASTEXITCODE -ne 0) {{ throw 'repository identity failed' }}
$Results.keys = [string] (& $Python -c $TrustedUpdateKeyIdsPython)
if ($LASTEXITCODE -ne 0) {{ throw 'update key query failed' }}
$Results | ConvertTo-Json -Compress
"""
    encoded_command = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded_command,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    version = json.loads(payload["version"])
    source = json.loads(payload["source"])
    repository = json.loads(payload["repository"])
    key_ids = json.loads(payload["keys"])
    assert version["version"] == "0.1.0"
    assert version["windows_version_numeric"] == "0.1.0.65535"
    assert source == version
    assert repository == {
        "repository_url": "https://github.com/definitlynotme/Tessract",
        "releases_url": "https://github.com/definitlynotme/Tessract/releases",
        "update_api_url": (
            "https://api.github.com/repos/definitlynotme/Tessract/releases/latest"
        ),
    }
    assert len(key_ids) >= 1
    assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", key_id) for key_id in key_ids)


def _minimal_frozen_distribution(root: Path) -> Path:
    files = (
        "Tesseract.exe",
        "_internal/bin/mihomo.exe",
        "_internal/bin/wintun.dll",
        "_internal/python312.dll",
        "_internal/PySide6/Qt6Core.dll",
        "_internal/PySide6/QtCore.pyd",
        "_internal/cryptography/hazmat/bindings/_rust.pyd",
        "_internal/aiohttp/_http_parser.cp312-win_amd64.pyd",
        "_internal/_cffi_backend.cp312-win_amd64.pyd",
        "_internal/concrt140.dll",
        "_internal/msvcp140.dll",
        "_internal/vcruntime140.dll",
        "_internal/libcrypto-3.dll",
        "_internal/libffi-8.dll",
    )
    for relative in files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative == "_internal/bin/wintun.dll":
            shutil.copy2(ROOT / "bin" / "wintun.dll", path)
        elif relative == "_internal/libcrypto-3.dll":
            path.write_bytes(b"frozen test fixture OpenSSL 3.0.15\x00")
        else:
            path.write_bytes(relative.encode("utf-8"))
    return root


def _minimal_runtime_owner_mapping() -> dict[str, dict[str, object]]:
    versions = {
        "tesseract": "0.1.0",
        "mihomo": "1.19.27",
        "wintun": "0.14.1",
        "cpython": "3.12.7",
        "qt": "6.11.0",
        "pyside6": "6.11.0",
        "cryptography": "46.0.7",
        "aiohttp": "3.14.1",
        "cffi": "2.0.0",
        "openssl": "3.0.15",
        "libffi": "3.4.4",
        "microsoft-windows-runtime": None,
    }
    mapping: dict[str, dict[str, object]] = {}
    for owner_id, version in versions.items():
        mapping[owner_id] = {
            "version": version,
            "license_expression": "Test-License",
            "license_path": f"third-party-licenses/{owner_id}",
            "version_evidence": "test-evidence",
        }
    mapping["libffi"]["dll_abi"] = "8"
    return mapping


def _requirements(path: Path) -> list[Requirement]:
    return [
        Requirement(line)
        for raw in path.read_text(encoding="utf-8").splitlines()
        if (line := raw.strip()) and not line.startswith("#")
    ]


def test_windows_release_lock_covers_exact_runtime_and_build_requirements() -> None:
    locked = locked_requirements(ROOT / "requirements-windows.lock.txt")
    direct = _requirements(ROOT / "requirements.txt") + _requirements(
        ROOT / "requirements-build.txt"
    )

    for requirement in direct:
        name = canonicalize_name(requirement.name)
        specs = list(requirement.specifier)
        assert len(specs) == 1
        assert specs[0].operator == "=="
        assert locked[name] == specs[0].version


def test_windows_release_lock_is_the_exact_installed_dependency_closure() -> None:
    lock_path = ROOT / "requirements-windows.lock.txt"
    locked = locked_requirements(lock_path)
    closure = verify_lock_closure(
        lock_path,
        (ROOT / "requirements.txt", ROOT / "requirements-build.txt"),
    )

    assert closure == set(locked)
    assert "apprise" not in closure
    assert "notification-bridge" not in closure


def test_release_lock_rejects_duplicate_or_conditional_entries(
    tmp_path: Path,
) -> None:
    digest = "0" * 64
    duplicate = tmp_path / "duplicate.txt"
    duplicate.write_text(
        f"Example_Package==1.0 --hash=sha256:{digest}\n"
        f"example-package==1.0 --hash=sha256:{digest}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate release lock entry"):
        locked_requirements(duplicate)

    conditional = tmp_path / "conditional.txt"
    conditional.write_text(
        f'example-package==1.0;python_version>="3.12" --hash=sha256:{digest}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="plain pinned distribution"):
        locked_requirements(conditional)


def test_release_lock_requires_one_canonical_sha256_hash(tmp_path: Path) -> None:
    for value in (
        "example-package==1.0\n",
        "example-package==1.0 --hash=sha256:abc\n",
        f"example-package==1.0 --hash=SHA256:{'0' * 64}\n",
        f"example-package==1.0 --hash=sha256:{'A' * 64}\n",
        f"example-package==1.0 --hash=sha256:{'0' * 64} --only-binary=:all:\n",
    ):
        path = tmp_path / "invalid-hash-lock.txt"
        path.write_text(value, encoding="utf-8")
        with pytest.raises(ValueError, match="exactly one lowercase SHA-256"):
            locked_requirements(path)


def test_project_license_is_complete_gpl_v3_only_text() -> None:
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")

    assert len(text) > 34_000
    assert "GNU GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 29 June 2007" in text
    assert "END OF TERMS AND CONDITIONS" in text


def test_checkout_preserves_hash_pinned_release_inputs_as_lf_bytes() -> None:
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in attributes.splitlines()

    wintun_license = (
        ROOT / "packaging" / "licenses" / "WINTUN-PREBUILT-LICENSE.txt"
    )
    assert hashlib.sha256(wintun_license.read_bytes()).hexdigest() == (
        "9aaf948856ce8845a762121306039ef09d0eeb4d9e4f4c355647d4081e818087"
    )
    completed = subprocess.run(
        [
            "git",
            "check-attr",
            "text",
            "eol",
            "--",
            "packaging/licenses/WINTUN-PREBUILT-LICENSE.txt",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "text: auto" in completed.stdout
    assert "eol: lf" in completed.stdout


def test_frozen_distribution_audit_maps_every_binary_and_hashes_it(
    tmp_path: Path,
) -> None:
    dist_dir = _minimal_frozen_distribution(tmp_path / "dist")

    components = audit_frozen_distribution(
        dist_dir,
        runtime_owners=_minimal_runtime_owner_mapping(),
    )

    assert len(components) == 14
    owners = {
        property_["value"]
        for component in components
        for property_ in component["properties"]
        if property_["name"] == "tesseract:runtime-owner"
    }
    assert {
        "tesseract",
        "mihomo",
        "wintun",
        "cpython",
        "qt",
        "pyside6",
        "cryptography",
        "aiohttp",
        "cffi",
        "microsoft-windows-runtime",
        "openssl",
        "libffi",
    } <= owners
    assert all(str(component.get("version") or "").strip() for component in components)
    assert all(component["hashes"][0]["alg"] == "SHA-256" for component in components)
    assert all(len(component["hashes"][0]["content"]) == 64 for component in components)


def test_frozen_distribution_audit_rejects_unknown_binary(tmp_path: Path) -> None:
    dist_dir = _minimal_frozen_distribution(tmp_path / "dist")
    (dist_dir / "_internal" / "mystery.dll").write_bytes(b"unknown")

    with pytest.raises(RuntimeError, match="unmapped executable artifacts"):
        audit_frozen_distribution(
            dist_dir,
            runtime_owners=_minimal_runtime_owner_mapping(),
        )


def test_frozen_distribution_audit_rejects_nonofficial_wintun(tmp_path: Path) -> None:
    dist_dir = _minimal_frozen_distribution(tmp_path / "dist")
    (dist_dir / "_internal" / "bin" / "wintun.dll").write_bytes(b"substituted")

    with pytest.raises(RuntimeError, match="WinTun amd64 DLL hash mismatch"):
        audit_frozen_distribution(
            dist_dir,
            runtime_owners=_minimal_runtime_owner_mapping(),
        )


@pytest.mark.parametrize(
    "relative",
    (
        "_internal/PySide6/Qt6Pdf.dll",
        "_internal/PySide6/Qt6Quick.dll",
        "_internal/PySide6/Qt6Qml.dll",
        "_internal/PySide6/Qt6VirtualKeyboard.dll",
        "_internal/PySide6/plugins/imageformats/qpdf.dll",
        "_internal/PySide6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll",
        "_internal/PySide6/plugins/imageformats/qwebp.dll",
    ),
)
def test_frozen_distribution_audit_rejects_forbidden_qt_payload(
    tmp_path: Path,
    relative: str,
) -> None:
    dist_dir = _minimal_frozen_distribution(tmp_path / "dist")
    forbidden = dist_dir / relative
    forbidden.parent.mkdir(parents=True, exist_ok=True)
    forbidden.write_bytes(b"forbidden")

    with pytest.raises(RuntimeError, match="forbidden Qt artifacts"):
        audit_frozen_distribution(
            dist_dir,
            runtime_owners=_minimal_runtime_owner_mapping(),
        )


def test_packaged_spec_requires_notices_licenses_sbom_and_runtime_binaries() -> None:
    spec = (ROOT / "tesseract-runtime.spec").read_text(encoding="utf-8")

    for required in (
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "third-party-licenses",
        "sbom.cdx.json",
        "mihomo.exe",
        "wintun.dll",
        "app_version.txt",
        "tier3_build_id.json",
    ):
        assert required in spec
    assert "require_file(" in spec
    assert "without_foreign_root_dependencies" in spec
    assert 'name == "icuuc.dll"' in spec
    assert 'name.startswith("icudt")' in spec
    assert '"libcrypto-3-x64.dll"' in spec
    assert '"libssl-3-x64.dll"' in spec
    for excluded in (
        "PySide6",
        "shiboken6",
        "apprise",
        "bridges",
        "email_outlook_profile",
        "notification_bridge",
        "signal_bridge",
        "telegram_user_bridge",
        "whatsapp_web_bridge",
    ):
        assert f'"{excluded}"' in spec
    # The retired Qt flavor has no spec or installer any more.
    assert not (ROOT / "Tesseract.spec").exists()


def test_installer_displays_the_project_license() -> None:
    installer = (ROOT / "packaging" / "windows" / "Tesseract-shell.iss").read_text(
        encoding="utf-8"
    )

    assert "LicenseFile=..\\..\\LICENSE" in installer
    assert not (ROOT / "packaging" / "windows" / "Tesseract.iss").exists()


def test_build_script_preflights_before_cleanup_and_closes_release_evidence() -> None:
    build_script = (ROOT / "scripts" / "build_windows.ps1").read_text(
        encoding="utf-8"
    )
    verify_index = build_script.index(
        'Invoke-Native "locked Python/build dependency verification"'
    )
    cleanup_index = build_script.index(
        "foreach ($dir in @($BuildDir, $DistDir, $ReleaseDir))"
    )
    assert verify_index < cleanup_index
    mutex_index = build_script.index(
        '$BuildMutexName = "Local\\TesseractWindowsBuild_$BuildMutexId"'
    )
    assert mutex_index < cleanup_index
    assert "Another Windows release build is already running" in build_script
    assert "$BuildMutex.ReleaseMutex()" in build_script
    for parameter in (
        "$ExpectedGitCommit",
        "$Repository",
        "$WorkflowRunId",
    ):
        assert parameter in build_script
    assert "$OfficialRelease = $OfficialFieldCount -eq 3" in build_script
    assert "Official release parameters do not match the GitHub Actions tag context" in build_script
    assert "Official releases forbid every partial-build and Authenticode switch" in build_script
    assert "$SkipInstallerAcceptance\n    ) {" in build_script
    assert "Official release builds require a clean tracked working tree" in build_script
    assert "APP_REPOSITORY_URL is not a canonical GitHub repository URL" in build_script
    assert "APP_RELEASES_URL" in build_script
    assert "APP_UPDATE_API_URL" in build_script
    assert "Embedded repository, release, and updater identities must exactly" in build_script
    update_key_gate = build_script.index(
        "Official releases require at least one valid embedded update-signing public key"
    )
    assert update_key_gate < cleanup_index
    assert "APP_UPDATE_SIGNING_PUBLIC_KEYS" in build_script
    assert "trusted_update_key_ids = [string[]] $TrustedUpdateKeyIds" in build_script
    assert "from app_info import release_version_contract" in build_script
    assert "from app_info import DEFAULT_RELEASE_VERSION_CONTRACT" in build_script
    assert "$SourceVersionNumeric -cne $VersionNumeric" in build_script
    assert "Release-version contract numeric forms disagree" in build_script
    assert "$VersionNumeric = [string] $VersionContract.windows_version_numeric" in build_script
    assert "app_version.txt did not round-trip the source release version" in build_script
    assert "$PreviousBuildVersionBytes" in build_script
    assert '"--no-deps"' in build_script
    assert '"--require-hashes"' in build_script
    assert '"--only-binary=:all:"' in build_script
    assert '"--requirements", "requirements.txt", "requirements-build.txt"' in build_script
    # Shell toolchain and the locked web / Rust inputs.
    assert '"--prefix", "ui", "ci", "--ignore-scripts"' in build_script
    assert '"fetch", "--locked", "--manifest-path", "shell\\Cargo.toml"' in build_script
    assert "cargo-about 0.9.3 is required" in build_script
    assert "cargo-deny 0.20.2 is required" in build_script
    assert '$DistDir = Resolve-RepoPath "dist-release"' in build_script
    assert 'Invoke-Native "source smoke test"' in build_script
    assert "qt_tainted_modules" in build_script
    assert '"--app-version", $Version' in build_script
    assert '"--runtime-requirements", "requirements.txt"' in build_script
    assert '"--build-requirements", "requirements-build.txt"' in build_script
    assert '"--flavor", "shell"' in build_script
    assert '"cargo-inputs"' in build_script
    assert '"mpl-sources"' in build_script
    assert 'Invoke-Native "third-party source bundle"' in build_script
    assert "THIRD_PARTY_NOTICES.shell.md" in build_script
    assert "Sync-ThirdPartySourceInputs" in build_script
    assert "Tesseract-$Version-third-party-sources.zip" in build_script
    assert build_script.index(
        "Sync-ThirdPartySourceInputs $ThirdPartySourceManifest $ThirdPartySourceRoot"
    ) < cleanup_index
    assert 'Invoke-Native "Mihomo configuration validation"' in build_script
    assert '"tests/test_mihomo_generated_config.py"' in build_script
    assert 'Invoke-Native "frozen distribution license and SBOM audit"' in build_script
    assert '"--dist-dir", $DistAppDir' in build_script
    shell_build_index = build_script.index('Invoke-Native "shell, web UI and sidecar build"')
    assert build_script.index('"--dist-dir", $DistAppDir') > shell_build_index
    assert '"scripts\\build_shell_preview.py", "build"' in build_script
    assert "Tesseract.spec" not in build_script
    assert "release-metadata.json" in build_script
    assert "schema_version = 2" in build_script
    assert 'flavor = "shell"' in build_script
    assert "authenticode = $SigningMode" in build_script
    assert '"unsigned-by-policy"' in build_script
    assert 'release_signature = "pending"' in build_script
    assert "Official release build changed HEAD or the tracked working tree" in build_script
    assert "git_commit = $FinalGitCommit" in build_script
    assert "dirty = $FinalDirty" in build_script
    assert "mihomo_config = $MihomoConfigStatus" in build_script
    assert 'public_release_audit = "pending"' in build_script
    assert '"/DAppVersionNumeric=$VersionNumeric"' in build_script
    assert '"packaging\\windows\\Tesseract-shell.iss"' in build_script
    assert "fetch_webview2_bootstrapper.ps1" in build_script
    assert "function Get-InnoSetupVersion" in build_script
    assert "Inno Setup 6_is1" in build_script
    assert "DisplayVersion" in build_script
    assert "InstallLocation" in build_script
    assert "$InnoSetupVersion = Get-InnoSetupVersion $IsccPath" in build_script
    assert ".VersionInfo.ProductVersion" not in build_script[
        build_script.index("$InnoSetupVersion =") : build_script.index("$MetadataTag =")
    ]
    assert '"portable-only"' in build_script
    assert '"skipped-partial"' in build_script
    assert "Compare-Object" in build_script
    assert "SHA256SUMS.txt does not match" in build_script
    assert "Official releases must contain exactly five unsigned manifest payloads" in build_script
    assert "[System.Text.UTF8Encoding]::new($false)" in build_script
    assert '($hashLines -join "`n") + "`n"' in build_script
    assert "SHA256SUMS.txt must use exact UTF-8 LF bytes" in build_script
    assert "Copy-Item -LiteralPath $ThirdPartyLicenseDir" in build_script
    assert "Authenticode verification failed" in build_script
    assert "Remove-Item -LiteralPath $GeneratedVersionInfoFile" in build_script
    freeze_index = build_script.index(
        'Invoke-Native "release candidate identity freeze"'
    )
    packaged_smoke_index = build_script.index(
        'Invoke-Native "packaged shell and 16-helper smoke"'
    )
    portable_create_index = build_script.index(
        'Invoke-Native "portable candidate archive creation"'
    )
    installer_compile_index = build_script.index(
        '"/DSourceDir=$FrozenCandidateDir"'
    )
    installed_smoke_index = build_script.index(
        '-Description "installed sidecar helper smoke"'
    )
    installed_scope_index = build_script.index(
        'Invoke-Native "installed shell scope check"'
    )
    installed_verify_index = build_script.index(
        'Invoke-Native "installed candidate identity verification"'
    )
    metadata_index = build_script.index("$ReleaseMetadata = [ordered]@{")
    assert (
        shell_build_index
        < freeze_index
        < packaged_smoke_index
        < portable_create_index
        < installer_compile_index
        < installed_smoke_index
        < installed_scope_index
        < installed_verify_index
        < metadata_index
    )
    assert "$CandidateManifestFile = Join-Path $BuildDir" in build_script
    assert '"create-zip"' in build_script
    assert '"verify-zip"' in build_script
    assert '"verify-installed"' in build_script
    assert "Compress-Archive" not in build_script
    assert "New-PortablePackageStage" not in build_script
    assert build_script.count("Assert-ReleaseCandidateIdentity") >= 9
    assert "candidate_directory = $CandidateDirectoryStatus" in build_script
    assert "portable_zip = $PortableIdentityStatus" in build_script
    assert "installer_tree = $InstallerIdentityStatus" in build_script
    assert "candidate_shell_smoke = $CandidateShellSmokeStatus" in build_script
    assert "installed_smoke = $InstalledSmokeStatus" in build_script
    assert "installed_shell_scope = $InstalledShellScopeStatus" in build_script
    assert "--node-card-native-smoke" not in build_script
    assert "--smoke-test" not in build_script
    assert 'ExpectedMarker "Tesseract validator helper smoke test passed"' in build_script
    exact_smoke_function = build_script[
        build_script.index("function Invoke-ExactSmokeProcess") : build_script.index(
            "function Wait-PathsAbsent"
        )
    ]
    assert '$startInfo.EnvironmentVariables["TESSERACT_CONFIG_DIR"]' in exact_smoke_function
    assert '$startInfo.EnvironmentVariables["TESSERACT_DATA_DIR"]' in exact_smoke_function
    assert "$process.WaitForExit($TimeoutMilliseconds)" in exact_smoke_function
    assert "if ($timedOut)" in exact_smoke_function
    assert "$exitCode -ne 0 -or $normalizedStdout -cne $expectedStdout" in exact_smoke_function
    assert "$startInfo.WorkingDirectory = $WorkingDirectory" in exact_smoke_function
    assert build_script.count("-WorkingDirectory $InstallerValidationDir") == 1
    assert (
        'Invoke-Native "pre-smoke installed candidate identity verification"'
        in build_script
    )
    assert (
        'Invoke-Native "post-smoke installed candidate identity verification"'
        in build_script
    )
    assert "manifest_sha256 = $CandidateManifestSha256" in build_script
    assert "tree_sha256 = [string] $CandidateIdentity.tree_sha256" in build_script
    assert "Installer acceptance refuses to replace an existing Tesseract" in build_script
    assert build_script.index("Installer acceptance refuses to replace") < cleanup_index
    assert "Installer acceptance requires an elevated Windows build process" in build_script
    assert '"/DIR=`"$InstallerValidationDir`""' in build_script
    assert 'Join-Path $InstallerValidationDir "unins000.exe"' in build_script
    assert (
        '$PackagedReleaseMetadataPath = Join-Path $DistAppDir '
        '"release-metadata.json"'
    ) in build_script
    assert "WriteAllText(\n    $PackagedReleaseMetadataPath" not in build_script
    assert "Frozen release candidate must not embed mutable qualification metadata" in build_script


def test_build_windows_script_parses_with_windows_powershell_5() -> None:
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is required for build-script syntax regression")
    source = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    parser_harness = r"""
$source = [Text.Encoding]::UTF8.GetString(
  [Convert]::FromBase64String($env:TESSERACT_BUILD_WINDOWS_SOURCE)
)
$tokens = $null
$errors = $null
[System.Management.Automation.Language.Parser]::ParseInput(
  $source,
  [ref] $tokens,
  [ref] $errors
) | Out-Null
if ($errors.Count -ne 0) {
  $errors | ForEach-Object { [Console]::Error.WriteLine($_.Message) }
  exit 1
}
"""
    environment = os.environ.copy()
    environment["TESSERACT_BUILD_WINDOWS_SOURCE"] = base64.b64encode(
        source.encode("utf-8")
    ).decode("ascii")
    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(parser_harness.encode("utf-16-le")).decode("ascii"),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_release_workflow_pins_python_requires_honest_signing_and_rechecks_hashes() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "release-windows.yml"
    ).read_text(encoding="utf-8")

    assert 'PYTHON_VERSION: "3.12.7"' in workflow
    assert "choco" not in workflow.lower()
    assert "https://github.com/jrsoftware/issrc/releases/download/is-6_7_1/innosetup-6.7.1.exe" in workflow
    assert "10619024" in workflow
    assert "4d11e8050b6185e0d49bd9e8cc661a7a59f44959a621d31d11033124c4e8a7b0" in workflow
    assert '"/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"' in workflow
    assert "Inno Setup installer SHA-256 mismatch" in workflow
    assert "Inno Setup 6_is1" in workflow
    assert "DisplayVersion -cne $env:INNO_SETUP_VERSION" in workflow
    assert "InstallLocation" in workflow
    assert ".VersionInfo.ProductVersion" not in workflow[workflow.index("Install pinned Inno Setup"):workflow.index("Build unsigned-by-policy release artifacts")]
    assert "python-version: ${{ env.PYTHON_VERSION }}" in workflow
    assert "The exact release Python version is unavailable" in workflow
    assert "persist-credentials: false" in workflow
    checkout_revision = "de0fac2e4500dabe0009e67214ff5f5447ce83dd"
    # test-windows, build-windows, sign-release-manifest and publish-release check out the tag.
    assert workflow.count(f"actions/checkout@{checkout_revision}") == 4
    assert "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5" not in workflow
    # Leave ref unset so checkout v6 binds both the tag ref and the immutable
    # event commit.  Supplying ref explicitly drops that moved-tag check.
    parsed_workflow = yaml.safe_load(workflow)
    checkout_steps = [
        step
        for job in parsed_workflow["jobs"].values()
        for step in job.get("steps", ())
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert len(checkout_steps) == 4
    assert all("ref" not in step.get("with", {}) for step in checkout_steps)
    assert workflow.count("# v6.0.2") == 4
    assert "tag_object_sha: ${{ steps.release.outputs.tag_object_sha }}" in workflow
    assert '"tag_object_sha=$tagObjectSha"' in workflow
    # Once more in test-windows, whose tag validation is the same text as build-windows.
    assert workflow.count("scripts\\verify_release_tag_identity.ps1") == 11
    assert workflow.count("needs.build-windows.outputs.tag_object_sha") == 4
    assert "GITHUB_OUTPUT" in workflow
    assert "Remove private signing material" in workflow
    assert "publish-release:" in workflow
    assert "Official releases must be triggered by a pushed tag" in workflow
    assert "Release tag must be canonical vX.Y.Z or vX.Y.Z-rc.N" in workflow
    assert "Lightweight tags are not releasable" in workflow
    assert "DEFAULT_RELEASE_VERSION_CONTRACT" in workflow
    assert "release_windows_version=$sourceWindowsVersion" in workflow
    assert "RELEASE_WINDOWS_VERSION" in workflow
    assert "FilePrivatePart" in workflow
    assert "ProductPrivatePart" in workflow
    assert "Release metadata Windows numeric version mismatch" in workflow
    uses = re.findall(r"^\s*uses:\s*[^@\s]+@([^\s#]+)", workflow, re.MULTILINE)
    assert uses
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in uses)
    assert workflow.count("contents: write") == 1
    assert workflow.count("contents: read") == 4  # test-windows, build-windows, signing, attestation
    assert workflow.count("id-token: write") == 1
    assert workflow.count("attestations: write") == 1
    assert workflow.index("contents: write") > workflow.index("publish-release:")
    assert "permissions: {}" in workflow
    assert "write-all" not in workflow
    assert "sign-release-manifest:" in workflow
    assert "name: release" in workflow
    assert "PUBLIC_RELEASE_PRIVATE_DENYLIST" in workflow
    assert "scripts/audit_public_release.py" in workflow
    assert workflow.count("fetch-depth: 0") >= 2
    assert "--artifact release" in workflow
    audit_index = workflow.index("python scripts/audit_public_release.py")
    key_material_index = workflow.index("Materialize encrypted update-signing key")
    sign_index = workflow.index("python scripts/sign_release_manifest.py")
    assert audit_index < key_material_index < sign_index
    assert "Public release anonymity/secret audit failed" in workflow
    assert '$metadata.tests.public_release_audit = "passed"' in workflow
    assert "Signed metadata must record the protected public-release audit as passed" in workflow
    assert "Remove-Item -LiteralPath $denylistPath" in workflow
    assert "unsigned-by-policy" in workflow
    assert "SignatureStatus]::NotSigned" in workflow
    assert "Anonymous release artifact is not Authenticode-unsigned" in workflow
    assert "scripts/sign_release_manifest.py" in workflow
    assert "scripts/verify_release_manifest.py" in workflow
    assert "signature key_id is not in the embedded update-signing key ring" in workflow
    assert "Signed metadata key_id is not in trusted_update_key_ids" in workflow
    assert "Tesseract-$env:RELEASE_VERSION-third-party-sources.zip" in workflow
    assert "actions/attest-build-provenance@" in workflow
    assert "attestation.sigstore.json" in workflow
    assert 'git fetch --no-tags origin' not in workflow
    assert 'git merge-base --is-ancestor' not in workflow
    assert '/git/ref/heads/main' in workflow
    assert '/compare/${releaseCommit}...${mainCommit}' in workflow
    assert 'Invoke-RestMethod -Method Get -Uri $mainRefUri -Headers $headers' in workflow
    assert 'Invoke-RestMethod -Method Get -Uri $compareUri -Headers $headers' in workflow
    assert '$comparison.base_commit.sha' in workflow
    assert '$comparison.merge_base_commit.sha' in workflow
    assert '$comparison.head_commit.sha' not in workflow
    assert '[string] $comparison.url -cne $compareUri' in workflow
    assert 'switch -CaseSensitive ($comparisonStatus)' in workflow
    assert '"identical" {' in workflow
    assert '"ahead" {' in workflow
    assert '$compareCommits[-1].sha' in workflow
    assert "does not prove release ancestry" in workflow
    assert "A GitHub Release already exists" in workflow
    assert "--latest=false" in workflow
    assert 'packaged_smoke -ne "passed"' in workflow
    assert '$metadata.tests.$testName -ne "passed"' in workflow
    for required_test_name in (
        "source_smoke",
        "pytest",
        "mihomo_config",
        "packaged_smoke",
        "helper_smoke",
        "candidate_shell_smoke",
        "candidate_directory",
        "portable_zip",
        "installed_smoke",
        "installed_shell_scope",
        "installer_tree",
    ):
        assert f'"{required_test_name}"' in workflow
    assert "Get-FileHash" in workflow
    assert "SHA256 mismatch" in workflow
    assert "SHA256SUMS.txt.sig" in workflow
    assert "generate_release_notes: true" not in workflow
    assert '"--generate-notes"' not in workflow
    assert "/releases/generate-notes" in workflow
    assert "tag_name = $env:RELEASE_TAG" in workflow
    assert "target_commitish = $env:RELEASE_COMMIT" in workflow
    assert '$generatedNotes.body) -replace "`r`n?", "`n"' in workflow
    assert "<!-- tesseract-app-notes:start -->" in workflow
    assert "<!-- tesseract-app-notes:end -->" in workflow
    assert workflow.index("<!-- tesseract-app-notes:start -->") < workflow.index(
        "<!-- tesseract-app-notes:end -->"
    ) < workflow.index("## Installation and signature verification")
    assert "Generated changelog contains a reserved Tesseract app-notes marker" in workflow
    assert '"--notes-file", $releaseBodyPath' in workflow
    assert '"--draft"' in workflow
    assert '"release", "edit", $env:RELEASE_TAG' in workflow
    assert '"--draft=false"' in workflow
    assert '$asset.digest -cne $localDigest' in workflow
    assert '"tagName,isDraft,isImmutable,isPrerelease,name,assets,publishedAt"' in workflow
    assert '$publishedRelease.isImmutable -ne $true' in workflow
    assert "[Text.UTF8Encoding]::new($false)" in workflow
    assert "Remove-Item -LiteralPath $releaseBodyPath" in workflow
    assert "--clobber" not in workflow
    assert "SkipPackagedSmoke" not in workflow
    assert "allow_unsigned" not in workflow
    assert "SIGN_CERT" not in workflow

    signing_step = next(
        step
        for step in parsed_workflow["jobs"]["sign-release-manifest"]["steps"]
        if step.get("name") == "Sign release metadata and SHA256 manifest"
    )
    signing_run = signing_step["run"]
    assert signing_run.count("verify_release_tag_identity.ps1") == 2
    assert signing_run.index("verify_release_tag_identity.ps1") < signing_run.index(
        "scripts/sign_release_manifest.py"
    ) < signing_run.rindex("verify_release_tag_identity.ps1")

    sign_steps = parsed_workflow["jobs"]["sign-release-manifest"]["steps"]
    sign_step_names = [step.get("name") for step in sign_steps]
    assert sign_step_names.index(
        "Revalidate build-bound signed tag before signed artifact upload"
    ) < sign_step_names.index("Upload signed release staging")

    publish_step = next(
        step
        for step in parsed_workflow["jobs"]["publish-release"]["steps"]
        if step.get("name") == "Publish new GitHub Release without replacement"
    )
    publish_run = publish_step["run"]
    assert publish_run.count("verify_release_tag_identity.ps1") == 5
    publish_arguments_block = publish_run[
        publish_run.index("$publishArguments = @(") : publish_run.index(
            "& gh @publishArguments"
        )
    ]
    assert '"--verify-tag"' not in publish_arguments_block
    assert publish_run.index("& gh @createArguments") < publish_run.index(
        "$draftJson = & gh release view"
    ) < publish_run.index("& gh @publishArguments")
    assert publish_run.index(
        "# Recheck only after all assets are uploaded"
    ) < publish_run.index("& gh @publishArguments")
    assert publish_run.index("& gh @publishArguments") < publish_run.index(
        "$publishedJson = & gh release view"
    ) < publish_run.rindex("verify_release_tag_identity.ps1")
    assert "--clobber" not in publish_run
    assert "automatic retry" in publish_run


def test_release_workflow_pwsh_blocks_parse_with_windows_powershell_5() -> None:
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is required for workflow syntax regression")

    workflow_path = ROOT / ".github" / "workflows" / "release-windows.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    powershell_blocks = [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", ())
        if step.get("shell") == "pwsh"
    ]
    assert len(powershell_blocks) == 23
    powershell_sources = [
        *powershell_blocks,
        (ROOT / "scripts" / "verify_release_tag_identity.ps1").read_text(
            encoding="utf-8"
        ),
    ]

    parser_harness = r"""
$source = [Text.Encoding]::UTF8.GetString(
  [Convert]::FromBase64String($env:TESSERACT_WORKFLOW_PWSH_BLOCK)
)
$tokens = $null
$errors = $null
[System.Management.Automation.Language.Parser]::ParseInput(
  $source,
  [ref] $tokens,
  [ref] $errors
) | Out-Null
if ($errors.Count -ne 0) {
  $errors | ForEach-Object { [Console]::Error.WriteLine($_.Message) }
  exit 1
}
"""
    encoded_harness = base64.b64encode(
        parser_harness.encode("utf-16-le")
    ).decode("ascii")
    for index, block in enumerate(powershell_sources, start=1):
        process_environment = os.environ.copy()
        process_environment["TESSERACT_WORKFLOW_PWSH_BLOCK"] = (
            base64.b64encode(block.encode("utf-8")).decode("ascii")
        )
        completed = subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded_harness,
            ],
            cwd=ROOT,
            env=process_environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, (
            f"PowerShell block {index} failed to parse:\n"
            f"{completed.stdout}{completed.stderr}"
        )


_GITHUB_API_URL = "https://api.github.com"
_GITHUB_REPOSITORY = "definitlynotme/Tessract"
_RELEASE_TAG = "v0.1.0"
_TAG_OBJECT_SHA = "a" * 40
_ANCESTOR_SHA = "1" * 40
_INTERMEDIATE_SHA = "2" * 40
_MAIN_SHA = "3" * 40
_UNRELATED_SHA = "4" * 40


def _github_commit_fixture(
    sha: str,
    *,
    parents: tuple[str, ...] = (),
) -> dict[str, object]:
    commit_url = f"{_GITHUB_API_URL}/repos/{_GITHUB_REPOSITORY}/git/commits/{sha}"
    api_url = f"{_GITHUB_API_URL}/repos/{_GITHUB_REPOSITORY}/commits/{sha}"
    return {
        "sha": sha,
        "node_id": f"C_kwDOFixture{sha[:8]}",
        "commit": {
            "author": {
                "name": "Tesseract contributors",
                "email": "283048617+definitlynotme@users.noreply.github.com",
                "date": "2026-08-09T00:00:00Z",
            },
            "committer": {
                "name": "Tesseract contributors",
                "email": "283048617+definitlynotme@users.noreply.github.com",
                "date": "2026-08-09T00:00:00Z",
            },
            "message": "Release fixture",
            "tree": {
                "sha": "f" * 40,
                "url": f"{_GITHUB_API_URL}/repos/{_GITHUB_REPOSITORY}/git/trees/{'f' * 40}",
            },
            "url": commit_url,
            "comment_count": 0,
            "verification": {
                "verified": True,
                "reason": "valid",
                "signature": "fixture-signature",
                "payload": "fixture-payload",
                "verified_at": "2026-08-09T00:00:01Z",
            },
        },
        "url": api_url,
        "html_url": f"https://github.com/{_GITHUB_REPOSITORY}/commit/{sha}",
        "comments_url": f"{api_url}/comments",
        "author": None,
        "committer": None,
        "parents": [
            {
                "sha": parent,
                "url": f"{_GITHUB_API_URL}/repos/{_GITHUB_REPOSITORY}/commits/{parent}",
                "html_url": f"https://github.com/{_GITHUB_REPOSITORY}/commit/{parent}",
            }
            for parent in parents
        ],
    }


def _github_release_validation_fixtures(
    scenario: str,
) -> dict[str, object]:
    ahead_scenarios = {
        "ahead",
        "ahead_large",
        "missing_merge_base",
        "wrong_compare_url",
        "ahead_head_mismatch",
        "ahead_count_mismatch",
        "ahead_by_mismatch",
        "malformed_main_type",
        "malformed_main_sha",
        "api_404",
        "api_403",
    }
    if scenario == "identical" or scenario in ahead_scenarios:
        release_sha = _ANCESTOR_SHA
    elif scenario == "behind":
        release_sha = _MAIN_SHA
    elif scenario == "diverged":
        release_sha = _INTERMEDIATE_SHA
    else:  # pragma: no cover - test construction guard
        raise AssertionError(f"unknown release ancestry fixture: {scenario}")

    if scenario == "identical":
        main_sha = release_sha
        merge_base_sha = release_sha
        status = "identical"
        ahead_by = 0
        behind_by = 0
        commits: list[dict[str, object]] = []
    elif scenario in ahead_scenarios:
        main_sha = _MAIN_SHA
        merge_base_sha = release_sha
        status = "ahead"
        ahead_by = 2
        behind_by = 0
        commits = [
            _github_commit_fixture(_INTERMEDIATE_SHA, parents=(release_sha,)),
            _github_commit_fixture(main_sha, parents=(_INTERMEDIATE_SHA,)),
        ]
    elif scenario == "behind":
        main_sha = _ANCESTOR_SHA
        merge_base_sha = main_sha
        status = "behind"
        ahead_by = 0
        behind_by = 2
        commits = []
    else:
        main_sha = _MAIN_SHA
        merge_base_sha = _ANCESTOR_SHA
        status = "diverged"
        ahead_by = 1
        behind_by = 1
        commits = [_github_commit_fixture(main_sha, parents=(merge_base_sha,))]

    tag_ref_url = (
        f"{_GITHUB_API_URL}/repos/{_GITHUB_REPOSITORY}/git/ref/tags/{_RELEASE_TAG}"
    )
    tag_object_url = (
        f"{_GITHUB_API_URL}/repos/{_GITHUB_REPOSITORY}/git/tags/{_TAG_OBJECT_SHA}"
    )
    main_ref_url = (
        f"{_GITHUB_API_URL}/repos/{_GITHUB_REPOSITORY}/git/ref/heads/main"
    )
    compare_url = (
        f"{_GITHUB_API_URL}/repos/{_GITHUB_REPOSITORY}/compare/"
        f"{release_sha}...{main_sha}"
    )
    compare: dict[str, object] = {
        "url": compare_url,
        "html_url": (
            f"https://github.com/{_GITHUB_REPOSITORY}/compare/"
            f"{release_sha}...{main_sha}"
        ),
        "permalink_url": (
            f"https://github.com/{_GITHUB_REPOSITORY}/compare/"
            f"{release_sha}...{main_sha}"
        ),
        "diff_url": f"{compare_url}.diff",
        "patch_url": f"{compare_url}.patch",
        "base_commit": _github_commit_fixture(release_sha),
        "merge_base_commit": _github_commit_fixture(merge_base_sha),
        "status": status,
        "ahead_by": ahead_by,
        "behind_by": behind_by,
        "total_commits": len(commits),
        "commits": commits,
        "files": [],
    }
    if scenario == "missing_merge_base":
        compare.pop("merge_base_commit")
    elif scenario == "ahead_large":
        hidden_sha = f"{0x500:040x}"
        previous_sha = hidden_sha
        large_commits: list[dict[str, object]] = []
        for index in range(249):
            commit_sha = f"{0x501 + index:040x}"
            large_commits.append(
                _github_commit_fixture(commit_sha, parents=(previous_sha,))
            )
            previous_sha = commit_sha
        large_commits.append(
            _github_commit_fixture(main_sha, parents=(previous_sha,))
        )
        compare["ahead_by"] = 251
        compare["total_commits"] = 251
        compare["commits"] = large_commits
    elif scenario == "wrong_compare_url":
        compare["url"] = f"{compare_url}?unexpected=1"
    elif scenario == "ahead_head_mismatch":
        commits[-1] = _github_commit_fixture(
            _UNRELATED_SHA,
            parents=(_INTERMEDIATE_SHA,),
        )
    elif scenario == "ahead_count_mismatch":
        compare["total_commits"] = 1
    elif scenario == "ahead_by_mismatch":
        compare["ahead_by"] = 1

    main_ref_object = {
        "sha": main_sha,
        "type": "commit",
        "url": (
            f"{_GITHUB_API_URL}/repos/{_GITHUB_REPOSITORY}/git/commits/"
            f"{main_sha}"
        ),
    }
    if scenario == "malformed_main_type":
        main_ref_object["type"] = "tag"
    elif scenario == "malformed_main_sha":
        main_ref_object["sha"] = "not-a-commit-sha"
    main_ref: dict[str, object] = {
        "ref": "refs/heads/main",
        "node_id": "REF_kwDOFixtureMain",
        "url": main_ref_url,
        "object": main_ref_object,
    }

    fixtures: dict[str, object] = {
        "tag_ref": {
            "ref": f"refs/tags/{_RELEASE_TAG}",
            "node_id": "REF_kwDOFixtureTag",
            "url": tag_ref_url,
            "object": {
                "sha": _TAG_OBJECT_SHA,
                "type": "tag",
                "url": tag_object_url,
            },
        },
        "tag_object": {
            "node_id": "TA_kwDOFixtureTag",
            "sha": _TAG_OBJECT_SHA,
            "url": tag_object_url,
            "tagger": {
                "name": "Tesseract contributors",
                "email": "283048617+definitlynotme@users.noreply.github.com",
                "date": "2026-08-09T00:00:00Z",
            },
            "object": {
                "sha": release_sha,
                "type": "commit",
                "url": (
                    f"{_GITHUB_API_URL}/repos/{_GITHUB_REPOSITORY}/git/commits/"
                    f"{release_sha}"
                ),
            },
            "tag": _RELEASE_TAG,
            "message": "Tesseract v0.1.0",
            "verification": {
                "verified": True,
                "reason": "valid",
                "signature": "fixture-signature",
                "payload": "fixture-payload",
                "verified_at": "2026-08-09T00:00:01Z",
            },
        },
        "main_ref": main_ref,
        "compare": compare,
    }
    if scenario == "api_404":
        fixtures["api_error"] = {"target": "tag_ref", "status": 404}
    elif scenario == "api_403":
        fixtures["api_error"] = {"target": "compare", "status": 403}
    return fixtures


def _release_workflow_remote_ancestry_block() -> str:
    workflow = (
        ROOT / ".github" / "workflows" / "release-windows.yml"
    ).read_text(encoding="utf-8")
    start_marker = "          $escapedTag = [Uri]::EscapeDataString($env:RELEASE_TAG)"
    end_marker = "          $localTagRef = \"refs/tags/$env:RELEASE_TAG\""
    start = workflow.index(start_marker)
    end = workflow.index(end_marker, start)
    block = textwrap.dedent(workflow[start:end]).strip()
    assert "Invoke-RestMethod -Method Get -Uri $compareUri" in block
    assert "switch -CaseSensitive ($comparisonStatus)" in block
    return block


def _release_workflow_local_tag_block() -> str:
    workflow = (
        ROOT / ".github" / "workflows" / "release-windows.yml"
    ).read_text(encoding="utf-8")
    start_marker = "          $localTagRef = \"refs/tags/$env:RELEASE_TAG\""
    end_marker = "          & .\\scripts\\verify_release_tag_identity.ps1 `"
    start = workflow.index(start_marker)
    end = workflow.index(end_marker, start)
    block = textwrap.dedent(workflow[start:end]).strip()
    assert "function Invoke-ReleaseGitCapture" in block
    assert '"rev-parse",' in block
    assert '"cat-file",' in block
    assert '"-t",' in block
    assert '"-p",' in block
    assert '"HEAD^{commit}"' in block
    return block


def _run_release_workflow_local_tag_fixture(
    scenario: str,
) -> subprocess.CompletedProcess[str]:
    powershell = shutil.which("powershell.exe")
    git = shutil.which("git.exe") or shutil.which("git")
    if not powershell or not git:
        pytest.skip("Windows PowerShell and Git are required for tag regression")

    temporary_parent = ROOT / ".tmp"
    temporary_parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="release-tag-fixture-", dir=temporary_parent
    ) as temporary_directory:
        repository = Path(temporary_directory)

        def run_git(*arguments: str) -> str:
            completed = subprocess.run(
                [git, *arguments],
                cwd=repository,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr
            return completed.stdout.strip()

        run_git("init", "--quiet", "--initial-branch=main")
        run_git("config", "user.name", "Tesseract test")
        run_git("config", "user.email", "tesseract-test@example.invalid")
        (repository / "payload.txt").write_text("first\n", encoding="utf-8")
        run_git("add", "--", "payload.txt")
        run_git("commit", "--quiet", "-m", "first")
        release_commit = run_git("rev-parse", "HEAD").lower()
        expected_tag_object_override: str | None = None

        if scenario != "missing_tag":
            if scenario == "lightweight":
                run_git("tag", "v0.1.0")
            elif scenario == "checkout_v4_rewritten_tag":
                run_git("tag", "--annotate", "v0.1.0", "-m", "Tesseract 0.1.0")
                expected_tag_object_override = run_git(
                    "rev-parse", "refs/tags/v0.1.0"
                ).lower()
                # Reproduce checkout v4's exact broken topology: the remote API
                # still reports the annotated tag object, while the local tag
                # ref has been force-updated to the event commit.
                run_git("update-ref", "refs/tags/v0.1.0", release_commit)
            elif scenario == "nested_tag":
                run_git("tag", "--annotate", "inner", "-m", "inner")
                inner_tag = run_git("rev-parse", "refs/tags/inner")
                run_git(
                    "tag",
                    "--annotate",
                    "v0.1.0",
                    inner_tag,
                    "-m",
                    "Tesseract 0.1.0",
                )
            elif scenario == "tag_to_tree":
                release_tree = run_git("rev-parse", "HEAD^{tree}")
                run_git(
                    "tag",
                    "--annotate",
                    "v0.1.0",
                    release_tree,
                    "-m",
                    "Tesseract 0.1.0",
                )
            elif scenario == "wrong_name":
                run_git("tag", "--annotate", "other", "-m", "other")
                other_tag = run_git("rev-parse", "refs/tags/other")
                run_git("update-ref", "refs/tags/v0.1.0", other_tag)
            elif scenario == "wrong_target":
                (repository / "payload.txt").write_text("second\n", encoding="utf-8")
                run_git("add", "--", "payload.txt")
                run_git("commit", "--quiet", "-m", "second")
                run_git("tag", "--annotate", "v0.1.0", "-m", "Tesseract 0.1.0")
                run_git("reset", "--hard", "--quiet", release_commit)
            else:
                run_git("tag", "--annotate", "v0.1.0", "-m", "Tesseract 0.1.0")
            local_tag_object = run_git("rev-parse", "refs/tags/v0.1.0").lower()
        else:
            local_tag_object = "0" * 40

        if scenario == "tag_object_mismatch":
            expected_tag_object = "f" * 40
        elif expected_tag_object_override is not None:
            expected_tag_object = expected_tag_object_override
        else:
            expected_tag_object = local_tag_object

        if scenario == "head_mismatch":
            (repository / "payload.txt").write_text("second\n", encoding="utf-8")
            run_git("add", "--", "payload.txt")
            run_git("commit", "--quiet", "-m", "second")

        harness = f"""
$ErrorActionPreference = 'Stop'
$env:RELEASE_TAG = 'v0.1.0'
$tagObjectSha = '{expected_tag_object}'
$releaseCommit = '{release_commit}'

{_release_workflow_local_tag_block()}

Write-Output ('LOCAL_TAG_OK object={{0}} commit={{1}} head={{2}}' -f `
  $localTagObject, $localCommit, $localHead)
"""
        encoded_command = base64.b64encode(harness.encode("utf-16-le")).decode("ascii")
        return subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded_command,
            ],
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )


def _run_cross_job_tag_identity_fixture(
    scenario: str,
) -> subprocess.CompletedProcess[str]:
    powershell = shutil.which("powershell.exe")
    git = shutil.which("git.exe") or shutil.which("git")
    if not powershell or not git:
        pytest.skip("Windows PowerShell and Git are required for tag regression")

    temporary_parent = ROOT / ".tmp"
    temporary_parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="release-tag-cross-job-", dir=temporary_parent
    ) as temporary_directory:
        repository = Path(temporary_directory)

        def run_git(*arguments: str) -> str:
            completed = subprocess.run(
                [git, *arguments],
                cwd=repository,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr
            return completed.stdout.strip()

        run_git("init", "--quiet", "--initial-branch=main")
        run_git("config", "user.name", "Tesseract test")
        run_git("config", "user.email", "tesseract-test@example.invalid")
        (repository / "payload.txt").write_text("release\n", encoding="utf-8")
        run_git("add", "--", "payload.txt")
        run_git("commit", "--quiet", "-m", "release")
        release_commit = run_git("rev-parse", "HEAD").lower()
        run_git("tag", "--annotate", "v0.1.0", "-m", "original release")
        expected_tag_object = run_git("rev-parse", "refs/tags/v0.1.0").lower()

        current_ref_type = "tag"
        current_tag_object = expected_tag_object
        final_ref_type = current_ref_type
        final_tag_object = current_tag_object
        verification_valid = True
        signature_evidence = "fixture SSH signature"
        payload_evidence = "fixture annotated-tag payload"
        if scenario == "same_commit_new_annotated":
            run_git("tag", "--delete", "v0.1.0")
            run_git("tag", "--annotate", "v0.1.0", "-m", "replacement release")
            current_tag_object = run_git("rev-parse", "refs/tags/v0.1.0").lower()
            assert current_tag_object != expected_tag_object
        elif scenario == "same_commit_lightweight":
            run_git("tag", "--delete", "v0.1.0")
            run_git("tag", "v0.1.0")
            current_ref_type = "commit"
            current_tag_object = release_commit
        elif scenario == "signature_no_longer_verified":
            verification_valid = False
        elif scenario == "signature_evidence_missing":
            signature_evidence = ""
        elif scenario == "remote_changes_during_validation":
            final_tag_object = "b" * 40
        elif scenario != "exact":  # pragma: no cover - test construction guard
            raise AssertionError(f"unknown cross-job tag fixture: {scenario}")

        final_ref_type = current_ref_type
        if scenario != "remote_changes_during_validation":
            final_tag_object = current_tag_object

        api_root = "https://api.github.test"
        repository_slug = "definitlynotme/Tessract"
        tag_ref_uri = (
            f"{api_root}/repos/{repository_slug}/git/ref/tags/v0.1.0"
        )
        tag_object_uri = (
            f"{api_root}/repos/{repository_slug}/git/tags/{current_tag_object}"
        )
        fixtures = {
            "tag_ref_uri": tag_ref_uri,
            "tag_object_uri": tag_object_uri,
            "tag_ref": {
                "ref": "refs/tags/v0.1.0",
                "object": {
                    "type": current_ref_type,
                    "sha": current_tag_object,
                },
            },
            "tag_ref_final": {
                "ref": "refs/tags/v0.1.0",
                "object": {
                    "type": final_ref_type,
                    "sha": final_tag_object,
                },
            },
            "tag_object": {
                "sha": current_tag_object,
                "tag": "v0.1.0",
                "object": {
                    "type": "commit",
                    "sha": release_commit,
                },
                "verification": {
                    "verified": verification_valid,
                    "reason": "valid" if verification_valid else "unsigned",
                    "signature": signature_evidence,
                    "payload": payload_evidence,
                },
            },
        }
        encoded_fixtures = base64.b64encode(
            json.dumps(fixtures, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        verifier_path = ROOT / "scripts" / "verify_release_tag_identity.ps1"
        harness = f"""
$ErrorActionPreference = 'Stop'
$fixtures = [Text.Encoding]::UTF8.GetString(
  [Convert]::FromBase64String($env:TESSERACT_TAG_IDENTITY_FIXTURES)
) | ConvertFrom-Json
function global:Invoke-RestMethod {{
  param(
    [string] $Method,
    [string] $Uri,
    [hashtable] $Headers
  )
  if ($Method -cne 'Get') {{ throw "Fixture only permits GET: $Method" }}
  if ($Uri -ceq [string] $fixtures.tag_ref_uri) {{
    $global:TesseractTagRefReads += 1
    if ($global:TesseractTagRefReads -eq 1) {{ return $fixtures.tag_ref }}
    return $fixtures.tag_ref_final
  }}
  if ($Uri -ceq [string] $fixtures.tag_object_uri) {{ return $fixtures.tag_object }}
  throw "No tag identity fixture for URI: $Uri"
}}
$env:GH_TOKEN = 'fixture-token'
$global:TesseractTagRefReads = 0
& '{verifier_path}' `
  -Repository '{repository_slug}' `
  -Tag 'v0.1.0' `
  -ExpectedTagObjectSha '{expected_tag_object}' `
  -ExpectedCommit '{release_commit}' `
  -ApiBaseUrl '{api_root}'
"""
        encoded_command = base64.b64encode(harness.encode("utf-16-le")).decode("ascii")
        process_environment = os.environ.copy()
        process_environment["TESSERACT_TAG_IDENTITY_FIXTURES"] = encoded_fixtures
        return subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded_command,
            ],
            cwd=repository,
            env=process_environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )


def _run_release_workflow_remote_ancestry_fixture(
    scenario: str,
) -> subprocess.CompletedProcess[str]:
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is required for release ancestry regression")

    fixtures = _github_release_validation_fixtures(scenario)
    encoded_fixtures = base64.b64encode(
        gzip.compress(
            json.dumps(fixtures, separators=(",", ":")).encode("utf-8"),
            mtime=0,
        )
    ).decode("ascii")
    harness = f"""
$ErrorActionPreference = 'Stop'
$env:GITHUB_API_URL = '{_GITHUB_API_URL}'
$env:EXPECTED_REPOSITORY = '{_GITHUB_REPOSITORY}'
$env:RELEASE_TAG = '{_RELEASE_TAG}'
$headers = @{{}}

$fixtureBytes = [Convert]::FromBase64String(
  $env:TESSERACT_ANCESTRY_FIXTURES_GZIP
)
$fixtureStream = [IO.MemoryStream]::new($fixtureBytes)
$gzipStream = [IO.Compression.GzipStream]::new(
  $fixtureStream,
  [IO.Compression.CompressionMode]::Decompress
)
$fixtureReader = [IO.StreamReader]::new($gzipStream, [Text.Encoding]::UTF8)
try {{
  $fixtures = $fixtureReader.ReadToEnd() | ConvertFrom-Json
}}
finally {{
  $fixtureReader.Dispose()
  $gzipStream.Dispose()
  $fixtureStream.Dispose()
}}
$tagRef = $fixtures.tag_ref
$tagObject = $fixtures.tag_object
$mainRef = $fixtures.main_ref
$comparison = $fixtures.compare
$tagRefRequestUri = "$env:GITHUB_API_URL/repos/$env:EXPECTED_REPOSITORY/git/ref/tags/$env:RELEASE_TAG"
$tagObjectRequestUri = "$env:GITHUB_API_URL/repos/$env:EXPECTED_REPOSITORY/git/tags/$($tagRef.object.sha)"
$mainRefRequestUri = "$env:GITHUB_API_URL/repos/$env:EXPECTED_REPOSITORY/git/ref/heads/main"
$compareRequestUri = "$env:GITHUB_API_URL/repos/$env:EXPECTED_REPOSITORY/compare/$($tagObject.object.sha)...$($mainRef.object.sha)"
$script:fixtureByUri = @{{
  $tagRefRequestUri = $tagRef
  $tagObjectRequestUri = $tagObject
  $mainRefRequestUri = $mainRef
  $compareRequestUri = $comparison
}}
$script:fixtureApiErrorStatus = [int] $fixtures.api_error.status
$script:fixtureApiErrorUri = switch -CaseSensitive ([string] $fixtures.api_error.target) {{
  'tag_ref' {{ $tagRefRequestUri }}
  'tag_object' {{ $tagObjectRequestUri }}
  'main_ref' {{ $mainRefRequestUri }}
  'compare' {{ $compareRequestUri }}
  default {{ '' }}
}}

function Invoke-RestMethod {{
  param(
    [Parameter(Mandatory = $true)][string] $Method,
    [Parameter(Mandatory = $true)][string] $Uri,
    [Parameter(Mandatory = $true)][hashtable] $Headers
  )
  if ($Method -cne 'Get') {{ throw "Fixture only permits GET: $Method" }}
  if ($script:fixtureApiErrorStatus -and $Uri -ceq $script:fixtureApiErrorUri) {{
    throw "Offline GitHub API returned HTTP $script:fixtureApiErrorStatus for $Uri"
  }}
  if (-not $script:fixtureByUri.ContainsKey($Uri)) {{
    throw "No offline GitHub API fixture for URI: $Uri"
  }}
  return $script:fixtureByUri[$Uri]
}}

{_release_workflow_remote_ancestry_block()}

Write-Output ('ANCESTRY_OK release={{0}} main={{1}} status={{2}}' -f `
  $releaseCommit, $mainCommit, $comparisonStatus)
"""
    encoded_command = base64.b64encode(harness.encode("utf-16-le")).decode("ascii")
    assert len(encoded_command) < 30_000, "PowerShell ancestry harness is too large"
    assert len(encoded_fixtures) < 20_000, "GitHub ancestry fixture is too large"
    process_environment = os.environ.copy()
    process_environment["TESSERACT_ANCESTRY_FIXTURES_GZIP"] = encoded_fixtures
    return subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded_command,
        ],
        cwd=ROOT,
        env=process_environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("scenario", ("identical", "ahead", "ahead_large"))
def test_release_workflow_executes_real_shape_github_ancestry_success_fixtures(
    scenario: str,
) -> None:
    fixtures = _github_release_validation_fixtures(scenario)
    comparison = fixtures["compare"]
    assert isinstance(comparison, dict)
    # GitHub's compare response has base_commit and merge_base_commit, but no
    # head_commit.  The two-commit ahead case therefore catches the exact API
    # shape assumption that previously broke the private release build.
    assert "head_commit" not in comparison

    completed = _run_release_workflow_remote_ancestry_fixture(scenario)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    expected_status = "ahead" if scenario == "ahead_large" else scenario
    assert f"status={expected_status}" in completed.stdout
    assert "ANCESTRY_OK" in completed.stdout
    if scenario == "ahead_large":
        assert comparison["total_commits"] == 251
        returned_commits = comparison["commits"]
        assert isinstance(returned_commits, list)
        assert len(returned_commits) == 250


def test_release_workflow_accepts_exact_local_annotated_tag_and_head() -> None:
    completed = _run_release_workflow_local_tag_fixture("annotated")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "LOCAL_TAG_OK" in completed.stdout


def test_cross_job_tag_identity_accepts_the_original_signed_tag_object() -> None:
    completed = _run_cross_job_tag_identity_fixture("exact")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "RELEASE_TAG_IDENTITY_OK" in completed.stdout


@pytest.mark.parametrize(
    ("scenario", "expected_error"),
    (
        (
            "same_commit_new_annotated",
            "The release tag object changed after the build gate.",
        ),
        (
            "same_commit_lightweight",
            "The release tag is no longer an annotated tag object.",
        ),
        (
            "signature_no_longer_verified",
            "GitHub no longer verifies the annotated release tag signature as valid.",
        ),
        (
            "signature_evidence_missing",
            "GitHub returned incomplete annotated-tag signature evidence.",
        ),
        (
            "remote_changes_during_validation",
            "The release tag changed during identity revalidation.",
        ),
    ),
)
def test_cross_job_tag_identity_rejects_same_commit_replacement(
    scenario: str,
    expected_error: str,
) -> None:
    completed = _run_cross_job_tag_identity_fixture(scenario)

    assert completed.returncode != 0
    assert expected_error in completed.stdout + completed.stderr
    assert "RELEASE_TAG_IDENTITY_OK" not in completed.stdout


@pytest.mark.parametrize(
    ("scenario", "expected_error"),
    (
        ("lightweight", "The local release ref is not an annotated tag object."),
        (
            "checkout_v4_rewritten_tag",
            "Local tag object does not match the GitHub tag object.",
        ),
        ("missing_tag", "Could not resolve the exact local release tag object."),
        ("tag_object_mismatch", "Local tag object does not match the GitHub tag object."),
        ("nested_tag", "Local annotated tag does not directly target a commit."),
        ("tag_to_tree", "Local annotated tag does not directly target a commit."),
        ("wrong_name", "Local annotated tag name does not match the pushed ref."),
        (
            "wrong_target",
            "Local annotated tag target does not match the verified GitHub tag.",
        ),
        ("head_mismatch", "Checked-out HEAD does not match the verified GitHub tag commit."),
    ),
)
def test_release_workflow_rejects_invalid_local_tag_or_checkout_state(
    scenario: str,
    expected_error: str,
) -> None:
    completed = _run_release_workflow_local_tag_fixture(scenario)

    assert completed.returncode != 0
    assert expected_error in completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("scenario", "expected_error"),
    (
        ("missing_merge_base", "GitHub comparison omitted 'merge_base_commit'."),
        (
            "behind",
            "Release tag target is not contained in the current protected main branch.",
        ),
        (
            "diverged",
            "Release tag target is not contained in the current protected main branch.",
        ),
    ),
)
def test_release_workflow_rejects_real_shape_invalid_github_ancestry_fixtures(
    scenario: str,
    expected_error: str,
) -> None:
    completed = _run_release_workflow_remote_ancestry_fixture(scenario)

    assert completed.returncode != 0
    assert expected_error in completed.stdout + completed.stderr
    assert "ANCESTRY_OK" not in completed.stdout


@pytest.mark.parametrize(
    ("scenario", "expected_error"),
    (
        (
            "wrong_compare_url",
            "GitHub comparison did not echo the exact release and main commits.",
        ),
        (
            "ahead_head_mismatch",
            "GitHub comparison head does not match the protected main reference.",
        ),
        (
            "ahead_count_mismatch",
            "GitHub ahead comparison is internally inconsistent.",
        ),
        (
            "ahead_by_mismatch",
            "GitHub ahead comparison is internally inconsistent.",
        ),
        (
            "malformed_main_type",
            "GitHub main reference does not target a commit.",
        ),
        (
            "malformed_main_sha",
            "GitHub main reference returned an invalid commit SHA.",
        ),
        ("api_404", "Offline GitHub API returned HTTP 404"),
        ("api_403", "Offline GitHub API returned HTTP 403"),
    ),
)
def test_release_workflow_fails_closed_on_malformed_or_failed_github_api(
    scenario: str,
    expected_error: str,
) -> None:
    completed = _run_release_workflow_remote_ancestry_fixture(scenario)

    assert completed.returncode != 0
    assert expected_error in completed.stdout + completed.stderr
    assert "ANCESTRY_OK" not in completed.stdout


def test_release_workflow_splits_tests_from_the_build_and_ties_them_to_one_commit() -> None:
    """A hosted runner of this repository is lost at 60 minutes into a job, so the
    tests run in test-windows and build-windows starts only after them, on the same
    commit, with -TestsPassedInJob recording what the test job proved."""
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "release-windows.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    tests, build = jobs["test-windows"], jobs["build-windows"]
    assert "needs" not in tests and build["needs"] == "test-windows"
    assert tests["outputs"] == {"release_commit": "${{ steps.release.outputs.release_commit }}"}
    names = [step.get("name") for step in tests["steps"]]
    assert names[:3] == ["Check out the tagged source", "Set up exact release Python",
                         "Validate signed tag, main ancestry, and source version"]
    assert names[-1] == "Run the release tests"
    test_run = tests["steps"][-1]["run"]
    assert "-TestsOnly" in test_run and "-SkipTests" not in test_run
    build_steps = {step.get("name"): step for step in build["steps"]}
    confirm = build_steps["Confirm the release tests passed on this commit"]
    assert confirm["env"]["TESTED_COMMIT"] == "${{ needs.test-windows.outputs.release_commit }}"
    assert confirm["env"]["RELEASE_COMMIT"] == "${{ steps.release.outputs.release_commit }}"
    order = [step.get("name") for step in build["steps"]]
    assert order.index("Validate signed tag, main ancestry, and source version") <         order.index("Confirm the release tests passed on this commit") <         order.index("Build unsigned-by-policy release artifacts")
    build_run = build_steps["Build unsigned-by-policy release artifacts"]["run"]
    assert "-SkipTests" in build_run and "-TestsPassedInJob" in build_run and "-TestsOnly" not in build_run
    # The setup steps of both jobs are the same text, so both validate the same tag.
    for name in names[:-1]:
        assert next(s for s in tests["steps"] if s.get("name") == name) == build_steps[name], name
    script = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    assert "($SkipTests -and -not $TestsPassedInJob)" in script
    assert "-TestsOnly and -TestsPassedInJob belong to the official two-job release workflow." in script
