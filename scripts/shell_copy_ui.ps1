[CmdletBinding()]
param()
# Fills shell/app/ui, the directory build.rs embeds (P4 architecture §1.3, T-P4-01).
# Builds the app target (ui/build.mjs) into ui/dist/app and mirrors it here;
# shell/app/ui/build-manifest.json is the one canonical manifest path. The manifest must say
# target=app and demo_free=true (T-P4-07: the demo gate is hard; the transitional -Prototype
# switch, which copied the prototype with its simulator, is gone).
# Never goes online: without ui/node_modules it stops and says what to run.
$ErrorActionPreference = "Stop"
$Repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$Target = Join-Path $Repo "shell/app/ui"
# Prepare everything first; shell/app/ui is only emptied once the new content is known
# good, so a failed build never leaves an empty directory for cargo to embed.
$Ui = Join-Path $Repo "ui"
if (-not (Test-Path -LiteralPath (Join-Path $Ui "node_modules"))) {
    throw "ui/node_modules is missing. Run 'npm --prefix ui ci --ignore-scripts' first (downloads packages: needs the user's approval)."
}
$Dist = Join-Path $Ui "dist/app"
& node (Join-Path $Ui "build.mjs") --target app --out $Dist --clean
if ($LASTEXITCODE -ne 0) { throw "ui/build.mjs --target app failed (exit $LASTEXITCODE); shell/app/ui left unchanged" }
$Manifest = Get-Content -LiteralPath (Join-Path $Dist "build-manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json
if ($Manifest.target -ne "app") { throw "ui/dist/app/build-manifest.json has target '$($Manifest.target)', expected 'app'; shell/app/ui left unchanged" }
if ($Manifest.demo_free -ne $true) { throw "ui/dist/app/build-manifest.json says demo_free=$($Manifest.demo_free): the app bundle carries demo code (node ui/tools/scan.mjs --rule demo); shell/app/ui left unchanged" }
foreach ($Entry in $Manifest.files) {
    if (-not (Test-Path -LiteralPath (Join-Path $Dist $Entry.path))) { throw "ui/dist/app/$($Entry.path) is listed in build-manifest.json but missing; shell/app/ui left unchanged" }
}
# shell/app/ui is generated (gitignored); start empty so no stale hashed file survives.
if (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Target | Out-Null
# Copy exactly the files the manifest lists, plus the manifest itself.
foreach ($Entry in $Manifest.files) {
    $Dest = Join-Path $Target $Entry.path
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Dest) | Out-Null
    Copy-Item -LiteralPath (Join-Path $Dist $Entry.path) -Destination $Dest -Force
}
Copy-Item -LiteralPath (Join-Path $Dist "build-manifest.json") -Destination $Target -Force
# Debug files have a canonical source in app/dev and are excluded by build.rs in release.
Copy-Item -Path (Join-Path $Repo "shell/app/dev/dev-rt.*") -Destination $Target -Force
Write-Output "App target built and copied to $Target (build-manifest.json target=app, demo_free=true)."
