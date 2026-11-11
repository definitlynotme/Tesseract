from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import uuid
import zipfile

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
DOWNLOAD_SCRIPT = REPO_ROOT / "scripts" / "download_mihomo.ps1"
EXPECTED_URI = (
    "https://github.com/MetaCubeX/mihomo/releases/download/v1.19.27/"
    "mihomo-windows-amd64-v1-v1.19.27.zip"
)


def _powershell() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if not executable:
        pytest.skip("Windows PowerShell is required for this contract test.")
    return executable


def _decode_powershell_output(payload: bytes | None) -> str:
    if not payload:
        return ""
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16", errors="replace")
    return payload.decode("utf-8-sig", errors="replace")


def _run_offline_download(
    *,
    corrupt_archive: bool = False,
    fail_download: bool = False,
    version: str | None = None,
    binary_hash_override: str | None = None,
) -> subprocess.CompletedProcess[str]:
    temp_root = REPO_ROOT / ".tmp" / f"mihomo-download-test-{uuid.uuid4().hex}"
    temp_root.mkdir(parents=True)
    source_archive = temp_root / "source.zip"
    output_relative = str((temp_root / "output" / "mihomo.exe").relative_to(REPO_ROOT))
    binary = b"offline deterministic Mihomo fixture\n"
    with zipfile.ZipFile(source_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mihomo-windows-amd64-v1.exe", binary)

    expected_archive_hash = hashlib.sha256(source_archive.read_bytes()).hexdigest()
    if corrupt_archive:
        source_archive.write_bytes(source_archive.read_bytes() + b"tampered")

    harness = r"""
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$utilityManifest = Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1'
$archiveManifest = Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Archive\Microsoft.PowerShell.Archive.psd1'
foreach ($moduleManifest in @($utilityManifest, $archiveManifest)) {
    if (-not (Test-Path -LiteralPath $moduleManifest -PathType Leaf)) {
        throw "Required Windows PowerShell module manifest is unavailable: $moduleManifest"
    }
    Import-Module -Name $moduleManifest -ErrorAction Stop
}
Get-Command -Name Get-FileHash, Expand-Archive -ErrorAction Stop | Out-Null

$global:observedUri = $null
function global:Invoke-RestMethod {
    throw 'GitHub REST discovery must not be used.'
}
function global:Invoke-WebRequest {
    param(
        [Parameter(Mandatory = $true)][string] $Uri,
        [Parameter(Mandatory = $true)][string] $OutFile,
        [hashtable] $Headers
    )
    if ($Uri -cne $env:TESSERACT_EXPECTED_MIHOMO_URI) {
        throw "Unexpected Mihomo asset URI: $Uri"
    }
    if ($env:TESSERACT_FAIL_MIHOMO_DOWNLOAD -ceq '1') {
        throw 'Simulated direct asset HTTP 404.'
    }
    if ($null -eq $Headers -or $Headers['User-Agent'] -cne 'Tesseract-build') {
        throw 'Expected the fixed Tesseract build User-Agent.'
    }
    Copy-Item -LiteralPath $env:TESSERACT_MIHOMO_FIXTURE -Destination $OutFile
    $global:observedUri = $Uri
}

$downloadArguments = @{
    MihomoArchiveSha256 = $env:TESSERACT_MIHOMO_ARCHIVE_SHA256
    MihomoBinarySha256 = $env:TESSERACT_MIHOMO_BINARY_SHA256
    OutputPath = $env:TESSERACT_MIHOMO_OUTPUT_RELATIVE
    SkipWintun = $true
    Force = $true
}
if ($env:TESSERACT_OVERRIDE_MIHOMO_VERSION -ceq '1') {
    $downloadArguments.Version = $env:TESSERACT_MIHOMO_VERSION
}
$downloadScriptItem = Get-Item -LiteralPath $env:TESSERACT_MIHOMO_DOWNLOAD_SCRIPT -ErrorAction Stop
if ($downloadScriptItem.PSIsContainer) {
    throw "Mihomo download script path is not a file: $($downloadScriptItem.FullName)"
}
& $downloadScriptItem.FullName @downloadArguments

if ($global:observedUri -cne $env:TESSERACT_EXPECTED_MIHOMO_URI) {
    throw 'The exact direct Mihomo release URL was not requested.'
}
"""
    encoded = base64.b64encode(harness.encode("utf-16-le")).decode("ascii")
    environment = os.environ.copy()
    environment.update(
        {
            "TESSERACT_EXPECTED_MIHOMO_URI": EXPECTED_URI,
            "TESSERACT_MIHOMO_ARCHIVE_SHA256": expected_archive_hash,
            "TESSERACT_FAIL_MIHOMO_DOWNLOAD": "1" if fail_download else "0",
            "TESSERACT_MIHOMO_BINARY_SHA256": (
                binary_hash_override or hashlib.sha256(binary).hexdigest()
            ),
            "TESSERACT_MIHOMO_DOWNLOAD_SCRIPT": str(DOWNLOAD_SCRIPT),
            "TESSERACT_MIHOMO_FIXTURE": str(source_archive),
            "TESSERACT_MIHOMO_OUTPUT_RELATIVE": output_relative,
            "TESSERACT_MIHOMO_VERSION": version or "",
            "TESSERACT_OVERRIDE_MIHOMO_VERSION": "1" if version is not None else "0",
        }
    )
    try:
        raw = subprocess.run(
            [
                _powershell(),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-OutputFormat",
                "Text",
                "-EncodedCommand",
                encoded,
            ],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=False,
            timeout=30,
            check=False,
        )
        return subprocess.CompletedProcess(
            args=raw.args,
            returncode=raw.returncode,
            stdout=_decode_powershell_output(raw.stdout),
            stderr=_decode_powershell_output(raw.stderr),
        )
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_powershell_output_decoder_is_independent_of_windows_locale() -> None:
    payload = "prefix: \u2764 Mihomo archive SHA-256 mismatch".encode("utf-8")
    assert "\u2764 Mihomo archive SHA-256 mismatch" in _decode_powershell_output(payload)


def test_mihomo_download_harness_uses_builtin_windows_powershell_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PSModulePath", r"Z:\definitely-missing-powershell-modules")
    completed = _run_offline_download(corrupt_archive=True)
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "Mihomo archive SHA-256 mismatch" in output
    assert "CommandNotFoundException" not in output


def test_mihomo_download_uses_exact_asset_without_release_api() -> None:
    script = DOWNLOAD_SCRIPT.read_text(encoding="utf-8")
    assert "api.github.com" not in script
    assert "Invoke-RestMethod" not in script
    assert "/releases/latest" not in script
    assert (
        '"https://github.com/MetaCubeX/mihomo/releases/download/$tag/$exactName"'
        in script
    )

    completed = _run_offline_download()
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_mihomo_download_rejects_archive_tampering_before_extraction() -> None:
    completed = _run_offline_download(corrupt_archive=True)
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "Mihomo archive SHA-256 mismatch" in output


def test_mihomo_download_rejects_executable_hash_mismatch() -> None:
    completed = _run_offline_download(binary_hash_override="0" * 64)
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "Mihomo executable SHA-256 mismatch" in output


def test_mihomo_download_fails_closed_on_missing_direct_asset() -> None:
    completed = _run_offline_download(fail_download=True)
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "Simulated direct asset HTTP 404" in output


@pytest.mark.parametrize("version", ("", "latest", "1.19", "1.19.27-rc.1"))
def test_mihomo_download_rejects_unpinned_or_noncanonical_version(version: str) -> None:
    completed = _run_offline_download(version=version)
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert (
        "A pinned Mihomo version is required" in output
        or "Mihomo version must be a canonical stable" in output
    )
    assert "Downloading Mihomo from" not in output
