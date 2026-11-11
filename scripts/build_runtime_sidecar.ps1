[CmdletBinding()]
param([string] $Python = "")

$ErrorActionPreference = "Stop"
$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if (-not $Python) { $Python = Join-Path $RepoRoot ".venv/Scripts/python.exe" }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python not found: $Python. Install build dependencies separately."
}
$Python = (Resolve-Path -LiteralPath $Python).Path
# Migration CPU convention (.build/<work-order dir>/evidence/CPU_*.txt, kept by
# the lead session): while a timing experiment runs (mode=timing) no build may
# start. Found by wildcard so this script stays ASCII; absent in a release tree.
$BuildDir = Join-Path $RepoRoot ".build"
if (Test-Path -LiteralPath $BuildDir -PathType Container) {
    foreach ($Convention in @(Get-ChildItem -LiteralPath $BuildDir -Directory | ForEach-Object {
            $Evidence = Join-Path $_.FullName "evidence"
            if (Test-Path -LiteralPath $Evidence -PathType Container) {
                Get-ChildItem -LiteralPath $Evidence -File -Filter "CPU_*.txt" } })) {
        $Mode = Get-Content -LiteralPath $Convention.FullName -Encoding UTF8 |
            Where-Object { $_ -match '^\s*mode\s*=' } | Select-Object -First 1
        if ($Mode -and ($Mode -replace '^\s*mode\s*=\s*', '').Trim() -eq "timing") {
            throw "CPU convention $($Convention.FullName) is mode=timing; no build now."
        }
    }
}
# The closure check below imports tests/test_bridge_qt_guard.py, a graph
# helper that needs no pytest (only its tests import pytest).
$WorkPath = Join-Path $RepoRoot "build-sidecar"
$DistPath = Join-Path $RepoRoot "dist-sidecar"
New-Item -ItemType Directory -Force -Path $WorkPath | Out-Null
$BuildLog = Join-Path $WorkPath "build.log"
Push-Location -LiteralPath $RepoRoot
try {
    # Never invoke the Qt release script, dependency installation, or host runtime.
    # Windows PowerShell 5 turns ordinary native stderr into ErrorRecords.
    # PyInstaller logs INFO there; use its exit code for this native call.
    $ErrorActionPreference = "Continue"
    try {
        & $Python -m PyInstaller --noconfirm --workpath $WorkPath --distpath $DistPath tesseract-runtime.spec 2>&1 |
            ForEach-Object { $_.ToString() } |
            Tee-Object -FilePath $BuildLog
        $BuildExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = "Stop"
    }
    if ($BuildExitCode -ne 0) { throw "Sidecar build failed (exit $BuildExitCode). See $BuildLog" }
    $Verification = @'
import hashlib
import json
import sys
from pathlib import Path
from PyInstaller.archive.readers import CArchiveReader

root = Path(sys.argv[1])
bundle = root / 'dist-sidecar' / 'tesseract-runtime'
exe = bundle / 'tesseract-runtime.exe'
assert exe.is_file(), f'Missing executable: {exe}'
qt_roots = {'pyside', 'pyside2', 'pyside6', 'shiboken2', 'shiboken6',
            'pyqt4', 'pyqt5', 'pyqt6', 'qtpy', 'qtconsole'}

def forbidden_file(path):
    parts = [part.casefold() for part in path.relative_to(bundle).parts]
    name = parts[-1]
    return (any(part in qt_roots or part in {'qt', 'qt5', 'qt6'} for part in parts)
            or (name.startswith(('qt', 'pyside', 'shiboken'))
                and name.endswith(('.dll', '.pyd'))))

forbidden = [str(path.relative_to(bundle)) for path in bundle.rglob('*')
             if forbidden_file(path)]
archive = CArchiveReader(str(exe))
modules = list(archive.toc)
for name in archive.toc:
    if name.endswith('.pyz'):
        modules.extend(archive.open_embedded_archive(name).toc)
assert {'desktop_runtime', 'bridge_main', 'runtime_network_recovery',
        'tier3_validator_helper', 'tier3_batch_validator_helper'} <= set(modules)
forbidden += [name for name in modules if name.split('.')[0].casefold() in qt_roots]
assert not forbidden, f'Qt artifacts remain: {forbidden}'
# T-P2-33: every repository module of the sidecar's static import closure
# (tests/test_bridge_qt_guard.py; Qt-only modules excluded) is in the bundle,
# so no lazily imported module is left to the source tree.
sys.path.insert(0, str(root))
sys.modules['pytest'] = None  # the graph helpers must not need pytest
from tests.test_bridge_qt_guard import sidecar_modules, sidecar_source_files
closure = sidecar_modules()
missing = sorted(set(closure) - set(modules))
assert not missing, f'Sidecar modules missing from the bundle: {missing}'
# Verify actual script order, not only the spec declaration.
scripts = [name for name, entry in archive.toc.items() if entry[-1] == 's']
assert scripts.index('tier3_audit_runtime_hook') < scripts.index('pyi_rth_multiprocessing') < scripts.index('runtime_main'), scripts
sha = hashlib.sha256(exe.read_bytes()).hexdigest()
manifests = [json.loads((bundle / name).read_bytes())
             for name in ('tier3_build_id.json', '_internal/tier3_build_id.json')]
assert manifests[0] == manifests[1]
assert all(m['declared_executable_sha256'] == m['executable_sha256'] == sha
           and m['identity_match'] is True for m in manifests)
# The identity components cover the whole closure, so "current" means it.
undeclared = sorted(set(sidecar_source_files()) - set(manifests[0]['components']))
assert not undeclared, f'Closure files missing from the identity components: {undeclared}'
for resource in ('bin/mihomo.exe', 'bin/wintun.dll', 'sbom.cdx.json'):
    assert (bundle / '_internal' / resource).is_file(), resource

def size(directory):
    return sum(path.stat().st_size for path in directory.rglob('*') if path.is_file())

print(json.dumps({'qt_artifacts': forbidden, 'runtime_scripts': scripts,
                  'executable_sha256': sha, 'build_id': manifests[0]['build_id'],
                  'closure_modules': len(closure),
                  'identity_components': len(manifests[0]['components']),
                  'bundle_bytes': size(bundle),
                  'bundle_files': sum(1 for path in bundle.rglob('*') if path.is_file()),
                  'bundle_bin_bytes': size(bundle / '_internal' / 'bin'),
                  'executable_bytes': exe.stat().st_size,
                  'dist_sidecar_bytes': size(root / 'dist-sidecar'),
                  'dist_bytes': size(root / 'dist')}, indent=2))
'@
    $Verification | & $Python - $RepoRoot |
        Tee-Object -FilePath (Join-Path $WorkPath "verification.json")
    if ($LASTEXITCODE -ne 0) { throw "Sidecar artifact verification failed (exit $LASTEXITCODE)." }
} finally {
    Pop-Location
}
