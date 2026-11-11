from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

import runtime_network_context as network_context
from runtime_network_context import (
    RuntimeNetworkContextObserver,
    RuntimeNetworkContextSnapshot,
    capture_runtime_network_context,
)


def _runner_from(outputs: dict[str, str], calls: list[tuple[str, ...]]):
    def run(argv: tuple[str, ...], _timeout: float) -> str:
        calls.append(argv)
        return outputs.get(argv[0].lower(), "")

    return run


def test_readonly_snapshot_labels_external_tun_without_claiming_ownership(
    monkeypatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        network_context,
        "_read_system_proxy",
        lambda: (False, "", None),
    )
    snapshot = capture_runtime_network_context(
        "generation-1",
        command_runner=_runner_from(
            {
                "tasklist.exe": '"verge-mihomo.exe","4242","Console","1","1 K"\n',
                "ipconfig.exe": (
                    "Ethernet adapter Meta Tunnel:\n"
                    "   DNS Servers . . . . . . . . . . . : 198.18.0.2\n"
                ),
                "route.exe": (
                    "0.0.0.0 0.0.0.0 10.0.0.1 10.0.0.2 25\n"
                    "198.18.0.0 255.254.0.0 On-link 198.18.0.1 0\n"
                ),
            },
            calls,
        ),
    )

    assert snapshot.state == "external_tun_present"
    assert snapshot.related_processes == ({"name": "verge-mihomo.exe", "pid": 4242},)
    assert calls == [
        ("tasklist.exe", "/fo", "csv", "/nh"),
        ("ipconfig.exe", "/all"),
        ("route.exe", "print", "-4"),
    ]
    assert "orphan" not in repr(snapshot).lower()


def test_readonly_snapshot_does_not_infer_owner_when_tun_evidence_is_ambiguous(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        network_context,
        "_read_system_proxy",
        lambda: (False, "", None),
    )
    snapshot = capture_runtime_network_context(
        "generation-2",
        command_runner=_runner_from(
            {
                "tasklist.exe": "",
                "ipconfig.exe": "Ethernet adapter Meta Tunnel:\n",
                "route.exe": "198.18.0.0 255.254.0.0 On-link 198.18.0.1 0\n",
            },
            [],
        ),
    )

    assert snapshot.state == "indeterminate"
    assert snapshot.related_processes == ()


def test_network_context_command_allowlist_rejects_mutation() -> None:
    with pytest.raises(ValueError):
        network_context._run_readonly_command(
            ("pnputil.exe", "/remove-device", "SWD\\WINTUN\\fixture")
        )


def test_packaged_audit_network_context_is_observational_only() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "audit_startup_windows.ps1"
    ).read_text(encoding="utf-8")
    lowered = script.lower()

    assert "orphaned_mihomo" not in lowered
    assert "pnputil" not in lowered
    assert "remove-device" not in lowered
    assert "verge-mihomo" in lowered
    assert "observational_only = $true" in lowered


def test_packaged_startup_audit_requires_atomic_qt_evidence() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "audit_startup_windows.ps1"
    ).read_text(encoding="utf-8")

    assert "$qtAuditPublishMarginMs" in script
    assert "$DurationMs - $qtAuditPublishMarginMs" in script
    assert "Qt startup audit did not publish its evidence file" in script
    assert "Qt startup audit evidence is not valid JSON" in script
    assert "$startupActionRequestedElapsedMs" in script
    assert "$startupFeedbackLatencyMs" in script
    assert "$startupFeedbackLatencyMs -le 250" in script
    assert (
        "Math.Max(100, durationMs) + Math.Max(1000, qtPublishTimeoutMs)"
        in script
    )


def test_packaged_startup_audit_identifies_main_window_by_native_role() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "audit_startup_windows.ps1"
    ).read_text(encoding="utf-8")

    assert "public static bool IsExpectedMainWindow(" in script
    assert 'className.EndsWith("QWindowIcon"' in script
    assert "(window.Style & WS_THICKFRAME) != 0" in script
    assert "(window.Style & WS_SYSMENU) != 0" in script
    assert "window.Width >= 900 && window.Height >= 600" not in script
    assert "$qtMainWindowHwnds" in script
    assert "$qtMainWindowHwnds.Contains([int64]$record.hwnd_int)" in script
    assert "unexpected_framed_qt_top_level" in script


def test_packaged_startup_audit_flags_visible_qt_internal_helpers() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "audit_startup_windows.ps1"
    ).read_text(encoding="utf-8")

    assert "public bool internal_helper_candidate { get; set; }" in script
    assert "public static bool IsInternalHelperClass(" in script
    assert "ThemeChangeObserverWindow" in script
    assert "ScreenChangeObserverWindow" in script
    assert "TrayIconMessageWindowClass" in script
    assert 'String.Equals(className, "_q_titlebar"' in script
    assert (
        "bool suspiciousCandidate = blankCandidate || shellCandidate || "
        "samePidQtShellCandidate || internalHelperCandidate;"
    ) in script


def test_packaged_startup_audit_native_role_classifier_compiles() -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is required for the packaged Windows audit fixture")
    script_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "audit_startup_windows.ps1"
    )
    quoted_script_path = str(script_path).replace("'", "''")
    fixture = rf"""
$source = Get-Content -LiteralPath '{quoted_script_path}' -Raw -Encoding UTF8
$match = [regex]::Match(
    $source,
    "(?s)\$typeDefinition = @'\r?\n(.*?)\r?\n'@"
)
if (-not $match.Success) {{ throw 'Embedded C# audit type not found' }}
Add-Type -TypeDefinition $match.Groups[1].Value
function Window([string]$className, [long]$style, [int]$width, [int]$height) {{
    $window = [StartupWindowAudit2+WindowRecord]::new()
    $window.Hwnd = 9001
    $window.Pid = 42
    $window.Visible = $true
    $window.EffectiveVisible = $true
    $window.ClassName = $className
    $window.Width = $width
    $window.Height = $height
    $window.Style = $style
    return $window
}}
$main = Window 'Qt6110QWindowIcon' ([long]0x000C0000) 1280 745
$observer = Window 'Qt6110ThemeChangeObserverWindow' ([long]0x04C00000) 1280 745
$tray = Window 'Qt6110TrayIconMessageWindowClass' ([long]0x04C00000) 1280 745
$shadow = Window 'SysShadow' ([long]0x94000000) 476 36
$metadataLost = Window '' ([long]0) 0 0
$metadataLost.Pid = 0
$baseline = [System.Collections.Generic.HashSet[long]]::new()
$targets = [System.Collections.Generic.HashSet[int]]::new()
[void]$targets.Add(42)
$auditedObserver = [StartupWindowAudit2]::ToAuditedWindow(
    $observer,
    $baseline,
    $targets,
    $false
)
$auditedShadow = [StartupWindowAudit2]::ToAuditedWindow(
    $shadow,
    $baseline,
    $targets,
    $false
)
$auditedMetadataLost = [StartupWindowAudit2]::ToAuditedWindow(
    $metadataLost,
    $baseline,
    $targets,
    $true,
    42
)
[pscustomobject]@{{
    main = [StartupWindowAudit2]::IsExpectedMainWindow($main, $true, $true)
    observer_main = [StartupWindowAudit2]::IsExpectedMainWindow($observer, $true, $true)
    observer_helper = [StartupWindowAudit2]::IsInternalHelperClass($observer.ClassName)
    tray_helper = [StartupWindowAudit2]::IsInternalHelperClass($tray.ClassName)
    observer_suspicious = [bool]$auditedObserver.suspicious_candidate
    observer_same_pid = [bool]$auditedObserver.same_pid_suspicious_candidate
    shadow_role = [bool]$auditedShadow.system_shadow
    shadow_blank = [bool]$auditedShadow.blank_candidate
    shadow_suspicious = [bool]$auditedShadow.suspicious_candidate
    metadata_lost_observed_pid = [int]$auditedMetadataLost.pid
    metadata_lost_hook_pid = [int]$auditedMetadataLost.hook_target_pid
    metadata_lost_target = [bool]$auditedMetadataLost.target_process
    metadata_lost_from_hook = [bool]$auditedMetadataLost.target_attributed_from_hook
    metadata_lost = [bool]$auditedMetadataLost.metadata_lost
}} | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-Command", fixture],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip())

    assert result == {
        "main": True,
        "observer_main": False,
        "observer_helper": True,
        "tray_helper": True,
        "observer_suspicious": True,
        "observer_same_pid": True,
        "shadow_role": True,
        "shadow_blank": False,
        "shadow_suspicious": False,
        "metadata_lost_observed_pid": 0,
        "metadata_lost_hook_pid": 42,
        "metadata_lost_target": True,
        "metadata_lost_from_hook": True,
        "metadata_lost": True,
    }


def test_packaged_startup_audit_captures_native_show_events() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "audit_startup_windows.ps1"
    ).read_text(encoding="utf-8")

    assert "SetWinEventHook" in script
    assert "Dictionary<IntPtr, int> hookTargetPidByHandle" in script
    assert "hookTargetPidByHandle[systemHook] = targetPid" in script
    assert "hookTargetPidByHandle[objectHook] = targetPid" in script
    assert "hookTargetPidByHandle.TryGetValue(hook, out hookTargetPid)" in script
    assert "EVENT_OBJECT_SHOW" in script
    assert "EVENT_OBJECT_UNCLOAKED" in script
    assert "LastWindowEvents" in script
    assert 'hookThread.Name = "TesseractWinEventAudit"' in script
    assert "while (!hookStop.WaitOne(1))" in script
    assert "Get-QtPopupEventMatch" in script
    assert "Get-MotionComboPopupEventAssignments" in script
    consensus_source = script.split(
        "function Get-MotionComboPopupEventAssignments", 1
    )[1].split("function Get-TargetProcessIds", 1)[0]
    assert "QtToWin32OffsetMs" not in consensus_source
    assert "bestMedianOffsetMs" in consensus_source
    assert "legal_motion_combo_popup" in script
    assert "$motionComboPopupMatchesByEventIndex" in script
    assert "Each action and each SHOW can be consumed at most once" in script
    assert "motion_combo_assignment_required" in script
    assert "motion_combo_clock_offset_ms=" in script
    assert '[string]$record.class_name -like "Qt*QWindowPopup*"' in script
    assert "unmatched_same_pid_qt_popup_show" in script
    assert "native_motion_combo_popup_show_count=" in script
    assert "native_system_shadow_show_count=" in script
    assert "native_unmatched_qt_popup_show_count=" in script
    assert "native_metadata_lost_target_show_count=" in script
    assert "native_unclassified_target_show_count=" in script
    assert "public bool metadata_lost { get; set; }" in script
    assert "public int hook_target_pid { get; set; }" in script
    assert "unclassified_target_show" in script
    assert '$_ .event_name -eq "SHOW"'.replace(" ", "") in script.replace(" ", "")
    assert "native_suspicious_show_seen=$nativeSuspiciousShowSeen" in script
    assert 'Join-Path $OutputDir "win32-window-events.json"' in script
    assert "looksLikeNewTesseractWindow" not in script
    assert "if (!(isTargetProcess || includeAsNewWindow))" in script
    assert "-not $nativeWindowEventHookActive" in script
    assert "exit 10" in script


def test_popup_focused_motion_audit_keeps_window_failures_strict() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "audit_startup_windows.ps1"
    ).read_text(encoding="utf-8")

    assert "[switch]$SkipMotionContract" in script
    assert "SkipMotionContract requires FailOnSuspiciousCandidate" in script
    assert (
        "$ExerciseMotionAction -and -not $SkipMotionContract -and "
        "$motionSequenceSeen -ne $true"
    ) in script
    assert "$FailOnSuspiciousCandidate -and" in script
    assert "$nativeUnclassifiedTargetShowCount -gt 0" in script
    assert "exit 3" in script
    assert "if ($ExerciseMotionAction -and $deletedWidgetErrorSeen)" in script
    assert "exit 9" in script


def test_metadata_lost_target_show_requires_exact_motion_assignment() -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is required for the packaged Windows audit fixture")
    script_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "audit_startup_windows.ps1"
    )
    quoted_script_path = str(script_path).replace("'", "''")
    fixture = rf"""
$auditScriptPath = '{quoted_script_path}'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $auditScriptPath,
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) {{
    throw ($parseErrors | ForEach-Object {{ $_.Message }} | Out-String)
}}
$definition = $ast.FindAll(
    {{
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq 'Get-NativeMetadataLostShowAssessment'
    }},
    $true
) | Select-Object -First 1
if ($null -eq $definition) {{ throw 'Missing metadata-lost assessment function' }}
. ([scriptblock]::Create($definition.Extent.Text))

$lostHwnd = [int64]7001
$event = [pscustomobject]@{{
    event_name = 'SHOW'
    window = [pscustomobject]@{{
        hwnd_int = $lostHwnd
        pid = 0
        target_process = $true
        metadata_lost = $true
    }}
}}
$unmatched = Get-NativeMetadataLostShowAssessment `
    -NativeEvent $event `
    -NativeShowEventIndex 0 `
    -MotionComboMatch $null
$exact = Get-NativeMetadataLostShowAssessment `
    -NativeEvent $event `
    -NativeShowEventIndex 0 `
    -MotionComboMatch ([pscustomobject]@{{
        event_index = 0
        popup_hwnd_int = $lostHwnd
    }})
$wrongHwnd = Get-NativeMetadataLostShowAssessment `
    -NativeEvent $event `
    -NativeShowEventIndex 0 `
    -MotionComboMatch ([pscustomobject]@{{
        event_index = 0
        popup_hwnd_int = [int64]7002
    }})
[pscustomobject]@{{
    unmatched_metadata_lost = [bool]$unmatched.metadata_lost_target_show
    unmatched_unclassified = [bool]$unmatched.unclassified_target_show
    exact_explained = [bool]$exact.exact_motion_action_explained
    exact_unclassified = [bool]$exact.unclassified_target_show
    wrong_hwnd_explained = [bool]$wrongHwnd.exact_motion_action_explained
    wrong_hwnd_unclassified = [bool]$wrongHwnd.unclassified_target_show
}} | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-Command", fixture],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip())

    assert result == {
        "unmatched_metadata_lost": True,
        "unmatched_unclassified": True,
        "exact_explained": True,
        "exact_unclassified": False,
        "wrong_hwnd_explained": False,
        "wrong_hwnd_unclassified": True,
    }


def test_packaged_startup_audit_keeps_native_capture_through_qt_publication() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "audit_startup_windows.ps1"
    ).read_text(encoding="utf-8")

    assert "[int]$QtPublishTimeoutMs = 30000" in script
    assert "File.Exists(qtAuditPath)" in script
    assert 'LastNativeAuditStopReason = "qt_evidence_published"' in script
    assert '"qt_evidence_timeout"' in script
    assert "LastNativeQtEvidenceObservedElapsedMs" in script
    assert "LastNativeQtCompletionTailMs" in script
    assert "native_qt_coverage_complete=$nativeQtCoverageComplete" in script
    assert "$strictWindowAudit -and -not $nativeQtCoverageComplete" in script
    assert "exit 11" in script
    assert "AddSeconds(20)" not in script


def test_native_sampler_waits_for_late_atomic_qt_evidence(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is required for the packaged Windows audit fixture")
    script_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "audit_startup_windows.ps1"
    )
    ready_path = tmp_path / "native-sampler-ready.txt"
    qt_evidence_path = tmp_path / "qt-startup-windows.json"
    quoted_script_path = str(script_path).replace("'", "''")
    quoted_ready_path = str(ready_path).replace("'", "''")
    quoted_qt_evidence_path = str(qt_evidence_path).replace("'", "''")
    publisher_fixture = rf"""
$readyPath = '{quoted_ready_path}'
$evidencePath = '{quoted_qt_evidence_path}'
$deadline = [DateTime]::UtcNow.AddSeconds(15)
while (-not (Test-Path -LiteralPath $readyPath)) {{
    if ([DateTime]::UtcNow -ge $deadline) {{ throw 'sampler never became ready' }}
    Start-Sleep -Milliseconds 20
}}
# The sampler call crosses a PowerShell-to-C# boundary after publishing the
# ready marker. Under a loaded full suite, Add-Type/JIT work can consume part
# of the original delay before SampleWindows starts its own stopwatch.
Start-Sleep -Milliseconds 1200
[System.IO.File]::WriteAllText($evidencePath, '[]')
"""
    sampler_fixture = rf"""
$source = Get-Content -LiteralPath '{quoted_script_path}' -Raw -Encoding UTF8
$match = [regex]::Match(
    $source,
    "(?s)\$typeDefinition = @'\r?\n(.*?)\r?\n'@"
)
if (-not $match.Success) {{ throw 'Embedded C# audit type not found' }}
Add-Type -TypeDefinition $match.Groups[1].Value
[System.IO.File]::WriteAllText('{quoted_ready_path}', 'ready')
$samples = [StartupWindowAudit2]::SampleWindows(
    $PID,
    (Get-Process -Id $PID).Path,
    [long[]]::new(0),
    100,
    20,
    $false,
    '{quoted_qt_evidence_path}',
    5000,
    150
)
[pscustomobject]@{{
    sample_count = @($samples).Count
    evidence_observed = [StartupWindowAudit2]::LastNativeQtEvidenceObserved
    evidence_elapsed_ms = [StartupWindowAudit2]::LastNativeQtEvidenceObservedElapsedMs
    audit_elapsed_ms = [StartupWindowAudit2]::LastNativeAuditElapsedMs
    completion_tail_ms = [StartupWindowAudit2]::LastNativeQtCompletionTailMs
    hard_deadline_ms = [StartupWindowAudit2]::LastNativeAuditHardDeadlineMs
    stop_reason = [StartupWindowAudit2]::LastNativeAuditStopReason
}} | ConvertTo-Json -Compress
"""
    publisher = subprocess.Popen(
        [powershell, "-NoProfile", "-Command", publisher_fixture],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-Command", sampler_fixture],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        publisher_stdout, publisher_stderr = publisher.communicate(timeout=10)
    finally:
        if publisher.poll() is None:
            publisher.kill()
            publisher.communicate()

    assert publisher.returncode == 0, publisher_stderr or publisher_stdout
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip())
    assert result["sample_count"] > 0
    assert result["evidence_observed"] is True
    assert result["evidence_elapsed_ms"] >= 500
    assert result["audit_elapsed_ms"] >= (
        result["evidence_elapsed_ms"] + result["completion_tail_ms"]
    )
    assert result["audit_elapsed_ms"] < result["hard_deadline_ms"]
    assert result["stop_reason"] == "qt_evidence_published"


def test_packaged_startup_audit_fails_single_same_pid_suspicious_sample() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "audit_startup_windows.ps1"
    ).read_text(encoding="utf-8")

    assert "public bool same_pid_suspicious_candidate { get; set; }" in script
    assert "bool samePidQtShellCandidate =" in script
    assert "className.StartsWith(\"Qt\", StringComparison.OrdinalIgnoreCase)" in script
    assert (
        "bool suspiciousCandidate = blankCandidate || shellCandidate || "
        "samePidQtShellCandidate || internalHelperCandidate;"
    ) in script
    assert "same_pid_suspicious_candidate = isTargetProcess && suspiciousCandidate" in script
    assert "if ($record.same_pid_suspicious_candidate)" in script
    assert "$nativeSuspiciousShowSeen" in script
    assert "$persistentSuspiciousSeen -or" in script
    assert "same_pid_suspicious_candidate_seen=$samePidSuspiciousSeen" in script


def test_packaged_startup_audit_popup_exemption_is_sample_scoped() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "audit_startup_windows.ps1"
    ).read_text(encoding="utf-8")

    assert "function Get-QtToWin32AuditOffsetMs" in script
    assert "function Get-QtPopupSampleMatch" in script
    assert "$mappedQtElapsedMs = [int]$Win32Sample.elapsed_ms" in script
    assert "$nearestPopupHwnd -eq $PopupHwnd" in script
    assert "$nearestDeltaMs -le [Math]::Max(1, $MaxDeltaMs)" in script
    assert "$legalQtPopupHwnds = New-Object" not in script
    assert "qt_popup_match_delta_ms" in script
    assert "qt_popup_nearest_elapsed_ms" in script


def test_packaged_startup_audit_popup_matching_rejects_reused_hwnd() -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is required for the packaged Windows audit fixture")
    script_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "audit_startup_windows.ps1"
    )
    quoted_script_path = str(script_path).replace("'", "''")
    fixture = rf"""
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
$auditScriptPath = '{quoted_script_path}'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $auditScriptPath,
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) {{
    throw ($parseErrors | ForEach-Object {{ $_.Message }} | Out-String)
}}
foreach ($functionName in @(
    'Get-QtToWin32AuditOffsetMs',
    'Get-QtPopupSampleMatch',
    'Get-QtPopupEventMatch'
)) {{
    $definition = $ast.FindAll(
        {{
            param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq $functionName
        }},
        $true
    ) | Select-Object -First 1
    if ($null -eq $definition) {{
        throw "Missing function: $functionName"
    }}
    . ([scriptblock]::Create($definition.Extent.Text))
}}

$mainHwnd = [int64]9001
$popupHwnd = [int64]4242
$mainQtRecord = [pscustomobject]@{{
    is_main_window = $true
    visible = $true
    hwnd_int = $mainHwnd
}}
$qtSamples = @(
    [pscustomobject]@{{
        elapsed_ms = 0
        top_levels = @($mainQtRecord)
        active_popup = $null
    }},
    [pscustomobject]@{{
        elapsed_ms = 40
        top_levels = @($mainQtRecord)
        active_popup = $null
    }},
    [pscustomobject]@{{
        elapsed_ms = 50
        top_levels = @($mainQtRecord)
        active_popup = [pscustomobject]@{{ hwnd_int = $popupHwnd }}
    }},
    [pscustomobject]@{{
        elapsed_ms = 60
        top_levels = @($mainQtRecord)
        active_popup = $null
    }}
)
$win32Samples = @(
    [pscustomobject]@{{
        elapsed_ms = 100
        windows = @(
            [pscustomobject]@{{
                hwnd_int = $mainHwnd
                main_candidate = $true
            }}
        )
    }}
)
$offset = Get-QtToWin32AuditOffsetMs `
    -Win32Samples $win32Samples `
    -QtSamples $qtSamples
function Match-At([int]$elapsedMs, [int64]$hwnd, [object[]]$samples = $qtSamples) {{
    return Get-QtPopupSampleMatch `
        -Win32Sample ([pscustomobject]@{{ elapsed_ms = $elapsedMs }}) `
        -PopupHwnd $hwnd `
        -QtSamples $samples `
        -QtToWin32OffsetMs $offset `
        -MaxDeltaMs 5
}}
$active = Match-At 150 $popupHwnd
$earlyInactive = Match-At 140 $popupHwnd
$nearestLaterInactive = Match-At 156 $popupHwnd
$tiePrefersLaterInactive = Match-At 155 $popupHwnd
$lateReused = Match-At 160 $popupHwnd
$wrongHandle = Match-At 150 ([int64]9999)
$tooFar = Match-At 180 $popupHwnd @(
    [pscustomobject]@{{
        elapsed_ms = 50
        top_levels = @($mainQtRecord)
        active_popup = [pscustomobject]@{{ hwnd_int = $popupHwnd }}
    }}
)
$eventActive = Get-QtPopupEventMatch `
    -Win32ElapsedMs 150 `
    -PopupHwnd $popupHwnd `
    -QtSamples $qtSamples `
    -QtToWin32OffsetMs $offset `
    -MaxDeltaMs 5
$eventTieFindsActive = Get-QtPopupEventMatch `
    -Win32ElapsedMs 155 `
    -PopupHwnd $popupHwnd `
    -QtSamples $qtSamples `
    -QtToWin32OffsetMs $offset `
    -MaxDeltaMs 5
$eventTooFar = Get-QtPopupEventMatch `
    -Win32ElapsedMs 156 `
    -PopupHwnd $popupHwnd `
    -QtSamples $qtSamples `
    -QtToWin32OffsetMs $offset `
    -MaxDeltaMs 5
[pscustomobject]@{{
    offset_ms = $offset
    active = [bool]$active.legal
    active_delta_ms = $active.delta_ms
    early_inactive = [bool]$earlyInactive.legal
    nearest_later_inactive = [bool]$nearestLaterInactive.legal
    nearest_later_elapsed_ms = $nearestLaterInactive.qt_elapsed_ms
    tie_prefers_later_inactive = [bool]$tiePrefersLaterInactive.legal
    tie_nearest_elapsed_ms = $tiePrefersLaterInactive.qt_elapsed_ms
    late_reused = [bool]$lateReused.legal
    wrong_handle = [bool]$wrongHandle.legal
    too_far = [bool]$tooFar.legal
    too_far_delta_ms = $tooFar.delta_ms
    event_active = [bool]$eventActive.legal
    event_tie_finds_active = [bool]$eventTieFindsActive.legal
    event_too_far = [bool]$eventTooFar.legal
}} | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-Command", fixture],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip())

    assert result == {
        "offset_ms": 100,
        "active": True,
        "active_delta_ms": 0,
        "early_inactive": False,
        "nearest_later_inactive": False,
        "nearest_later_elapsed_ms": 60,
        "tie_prefers_later_inactive": False,
        "tie_nearest_elapsed_ms": 60,
        "late_reused": False,
        "wrong_handle": False,
        "too_far": False,
        "too_far_delta_ms": 30,
        "event_active": True,
        "event_tie_finds_active": True,
        "event_too_far": False,
    }


def test_motion_combo_popup_events_use_consensus_clock_and_one_to_one_matching() -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is required for the packaged Windows audit fixture")
    script_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "audit_startup_windows.ps1"
    )
    quoted_script_path = str(script_path).replace("'", "''")
    fixture = rf"""
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
$auditScriptPath = '{quoted_script_path}'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $auditScriptPath,
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) {{
    throw ($parseErrors | ForEach-Object {{ $_.Message }} | Out-String)
}}
foreach ($functionName in @(
    'Get-MotionComboPopupActions',
    'Get-MotionComboPopupEventAssignments'
)) {{
    $definition = $ast.FindAll(
        {{
            param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq $functionName
        }},
        $true
    ) | Select-Object -First 1
    if ($null -eq $definition) {{ throw "Missing function: $functionName" }}
    . ([scriptblock]::Create($definition.Extent.Text))
}}

$smallHwnd = [int64]4001
$cipherHwnd = [int64]4242
$qtSamples = @(
    [pscustomobject]@{{
        motion = [pscustomobject]@{{
            combo_results = @(
                [pscustomobject]@{{
                    kind = 'small'
                    popup_hwnd_int = $smallHwnd
                    show_request_elapsed_ms = 2015.0
                    show_elapsed_ms = 12.0
                }},
                [pscustomobject]@{{
                    kind = 'shadowsocks-cipher'
                    popup_hwnd_int = $cipherHwnd
                    show_request_elapsed_ms = 3140.0
                    show_elapsed_ms = 18.0
                    reopen_popup_hwnd_int = $cipherHwnd
                    reopen_request_elapsed_ms = 3234.0
                    reopen_elapsed_ms = 16.0
                }}
            )
        }}
    }}
)
function Native-Show(
    [int]$elapsedMs,
    [int64]$hwnd,
    [string]$className = 'Qt6110QWindowPopupDropShadowSaveBits',
    [bool]$targetProcess = $true,
    [bool]$metadataLost = $false
) {{
    [pscustomobject]@{{
        elapsed_ms = $elapsedMs
        event_name = 'SHOW'
        window = [pscustomobject]@{{
            hwnd_int = $hwnd
            class_name = $className
            target_process = $targetProcess
            metadata_lost = $metadataLost
            internal_helper_candidate = $false
            system_shadow = ($className -eq 'SysShadow')
        }}
    }}
}}
$nativeEvents = @(
    (Native-Show 11342 $smallHwnd),
    (Native-Show 12475 $cipherHwnd),
    (Native-Show 12568 $cipherHwnd),
    # A second SHOW at the exact same time cannot consume the same reopen
    # declaration. It models the 0 ms duplicate/late-show regression.
    (Native-Show 12568 $cipherHwnd),
    (Native-Show 12569 ([int64]8888) 'SysShadow')
)
$calibration = Get-MotionComboPopupEventAssignments `
    -NativeShowEvents $nativeEvents `
    -QtSamples $qtSamples `
    -ToleranceMs 25
$assignedIndices = @(
    @($calibration.assignments) |
        ForEach-Object {{ [int]$_.event_index }} |
        Sort-Object
)
$eligibleIndices = @(0, 1, 2, 3)
$unmatchedIndices = @(
    $eligibleIndices | Where-Object {{ $assignedIndices -notcontains $_ }}
)
$singleActionCalibration = Get-MotionComboPopupEventAssignments `
    -NativeShowEvents @((Native-Show 11342 $smallHwnd)) `
    -QtSamples @(
        [pscustomobject]@{{
            motion = [pscustomobject]@{{
                combo_results = @(
                    [pscustomobject]@{{
                        popup_hwnd_int = $smallHwnd
                        show_request_elapsed_ms = 2015.0
                        show_elapsed_ms = 12.0
                    }}
                )
            }}
        }}
    ) `
    -ToleranceMs 25
$inconsistentCalibration = Get-MotionComboPopupEventAssignments `
    -NativeShowEvents @(
        (Native-Show 3015 $smallHwnd),
        (Native-Show 4340 $cipherHwnd)
    ) `
    -QtSamples $qtSamples `
    -ToleranceMs 25
$metadataLostCalibration = Get-MotionComboPopupEventAssignments `
    -NativeShowEvents @(
        (Native-Show 11342 $smallHwnd '' $true $true),
        (Native-Show 12475 $cipherHwnd '' $true $true),
        (Native-Show 12568 $cipherHwnd '' $true $true),
        (Native-Show 12569 ([int64]8888) '' $true $true)
    ) `
    -QtSamples $qtSamples `
    -ToleranceMs 25
[pscustomobject]@{{
    calibrated = [bool]$calibration.calibrated
    offset_ms = $calibration.calibrated_offset_ms
    action_count = $calibration.action_count
    declared_hwnds = @($calibration.declared_popup_hwnds)
    eligible_event_count = $calibration.eligible_event_count
    match_count = $calibration.consensus_match_count
    max_deviation_ms = $calibration.max_offset_deviation_ms
    assigned_indices = $assignedIndices
    unmatched_indices = $unmatchedIndices
    assignment_phases = @(
        @($calibration.assignments) |
            Sort-Object action_index |
            ForEach-Object {{ [string]$_.phase }}
    )
    single_action_calibrated = [bool]$singleActionCalibration.calibrated
    inconsistent_calibrated = [bool]$inconsistentCalibration.calibrated
    metadata_lost_calibrated = [bool]$metadataLostCalibration.calibrated
    metadata_lost_eligible_event_count = $metadataLostCalibration.eligible_event_count
    metadata_lost_match_count = $metadataLostCalibration.consensus_match_count
    metadata_lost_assigned_indices = @(
        @($metadataLostCalibration.assignments) |
            ForEach-Object {{ [int]$_.event_index }} |
            Sort-Object
    )
}} | ConvertTo-Json -Depth 6 -Compress
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-Command", fixture],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip())

    assert result == {
        "calibrated": True,
        "offset_ms": 9334,
        "action_count": 3,
        "declared_hwnds": [4001, 4242],
        "eligible_event_count": 4,
        "match_count": 3,
        "max_deviation_ms": 7,
        "assigned_indices": [0, 1, 2],
        "unmatched_indices": [3],
        "assignment_phases": ["show", "show", "reopen"],
        "single_action_calibrated": False,
        "inconsistent_calibrated": False,
        "metadata_lost_calibrated": True,
        "metadata_lost_eligible_event_count": 3,
        "metadata_lost_match_count": 3,
        "metadata_lost_assigned_indices": [0, 1, 2],
    }


def test_startup_audit_supports_safe_source_process_launch() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "audit_startup_windows.ps1"
    ).read_text(encoding="utf-8")

    assert "[string[]]$ArgumentList = @()" in script
    assert "FilePath = $ExePath" in script
    assert "PassThru = $true" in script
    assert "$startParameters.ArgumentList = $ArgumentList" in script
    assert "return Start-Process @startParameters" in script
    assert "[switch]$RootPidOnlyCleanup" in script
    assert "if (-not $RootPidOnlyCleanup)" in script


def test_network_context_observer_only_publishes_diagnostics() -> None:
    states = iter(("external_proxy_absent", "external_proxy_present"))
    second_sample = threading.Event()

    def capture(generation: str) -> RuntimeNetworkContextSnapshot:
        try:
            state = next(states)
        except StopIteration:
            state = "external_proxy_present"
        if state == "external_proxy_present":
            second_sample.set()
        return RuntimeNetworkContextSnapshot(
            captured_at="2026-07-14T00:00:00+00:00",
            captured_monotonic_ns=time.monotonic_ns(),
            runtime_generation=generation,
            state=state,
            related_processes=(),
            adapter_summary=(),
            default_route_summary=(),
            fake_ip_route_summary=(),
            dns_summary=(),
            system_proxy_enabled=False,
            system_proxy_digest="",
            errors=(),
        )

    observer = RuntimeNetworkContextObserver(
        "generation-3",
        capture=capture,
        interval_seconds=0.01,
    )
    observer.start()
    try:
        assert second_sample.wait(1.5)
        deadline = time.monotonic() + 0.5
        while len(observer.history()) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
    finally:
        observer.stop()

    history = observer.history()
    assert history
    assert {item.state for item in history} == {
        "external_proxy_absent",
        "external_proxy_present",
    }
