# Start this, then press 检查更新 or 立即更新 in Tesseract.
# The trace is written under %LOCALAPPDATA%\Tesseract\logs\update-trace-*.log.
$ErrorActionPreference = "Stop"
$scriptPath = Join-Path $PSScriptRoot "trace_update_button.py"
if (-not (Test-Path -LiteralPath $scriptPath)) {
    Write-Error "找不到 $scriptPath"
}
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 $scriptPath @args
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python $scriptPath @args
} else {
    Write-Error "找不到 Python。安装 Python 3 后重新运行这个脚本。"
}
exit $LASTEXITCODE
