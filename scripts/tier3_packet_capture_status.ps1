param([Parameter(Mandatory = $true)][string]$OutputDirectory)

$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$root = [IO.Path]::Combine($workspace, '.build', 'takeover-tests') + [IO.Path]::DirectorySeparatorChar
$output = [IO.Path]::GetFullPath($OutputDirectory)
if (-not $output.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Packet-monitor diagnostics must remain in the workspace audit directory.'
}
if (-not (Test-Path -LiteralPath $output -PathType Container)) {
    throw 'Create the dedicated audit output directory before elevation.'
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$administrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$results = @{}
foreach ($command in @('status', 'list')) {
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = [IO.Path]::Combine($env:SystemRoot, 'System32', 'pktmon.exe')
    $info.Arguments = $command
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($info)
    try {
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(10000)) {
            $process.Kill()
            $process.WaitForExit()
            throw "Packet-monitor $command read timed out."
        }
        [IO.File]::WriteAllText([IO.Path]::Combine($output, "$command.txt"), $stdout.Result)
        [IO.File]::WriteAllText([IO.Path]::Combine($output, "$command-error.txt"), $stderr.Result)
        $results[$command] = $process.ExitCode
    } finally {
        $process.Dispose()
    }
}
$record = @{
    schema = 1
    administrator = $administrator
    read_only = $true
    captured_packets = $false
    commands = $results
    recorded_at_utc = [DateTime]::UtcNow.ToString('o')
}
[IO.File]::WriteAllText([IO.Path]::Combine($output, 'access.json'), ($record | ConvertTo-Json -Depth 4))
if (-not $administrator -or $results['status'] -ne 0 -or $results['list'] -ne 0) { exit 1 }
