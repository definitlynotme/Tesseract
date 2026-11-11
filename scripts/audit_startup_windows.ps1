param(
    [string]$ExePath = "dist\Tesseract\Tesseract.exe",
    [string[]]$ArgumentList = @(),
    [string]$OutputDir = "artifacts\ui-screenshots\startup-window-audit",
    [int]$DurationMs = 2200,
    [int]$QtPublishTimeoutMs = 30000,
    [int]$IntervalMs = 10,
    [int]$SuspiciousPersistenceMs = 100,
    [switch]$FailOnBlankCandidate,
    [switch]$FailOnSuspiciousCandidate,
    [switch]$CaptureScreenshots,
    [switch]$NoQtAudit,
    [switch]$IncludeAllNewWindows,
    [switch]$ExerciseStartAction,
    [switch]$ExerciseMotionAction,
    [switch]$SkipMotionContract,
    [switch]$SkipRuntimeNetworkPreflight,
    [switch]$RootPidOnlyCleanup
)

$ErrorActionPreference = "Stop"

if ($ExerciseStartAction -and $ExerciseMotionAction) {
    throw "ExerciseStartAction and ExerciseMotionAction are mutually exclusive."
}
if ($SkipMotionContract -and -not $ExerciseMotionAction) {
    throw "SkipMotionContract requires ExerciseMotionAction."
}
if ($SkipMotionContract -and -not $FailOnSuspiciousCandidate) {
    throw "SkipMotionContract requires FailOnSuspiciousCandidate so popup-focused audits remain fail-closed."
}
if ($ExerciseMotionAction) {
    # The production window adds a 650 ms warmup and the final deterministic
    # same-target sidebar checkpoint lands at 3810 ms. Keep that evidence plus
    # a substantial clean tail inside the Qt publication window.
    $DurationMs = [Math]::Max(5200, $DurationMs)
}

function Resolve-RepoPath([string]$Path) {
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Path))
}

function Get-QtToWin32AuditOffsetMs {
    param(
        [object[]]$Win32Samples,
        [object[]]$QtSamples
    )

    # The Qt audit starts immediately after window.show(), while the Win32
    # sampler starts when the process is launched. Align their elapsed clocks
    # at the first sample where both audits identify the same main-window HWND.
    foreach ($qtSample in @($QtSamples | Sort-Object { [int]$_.elapsed_ms })) {
        $qtMain = @(
            @($qtSample.top_levels) |
                Where-Object {
                    [bool]$_.is_main_window -and
                    [bool]$_.visible -and
                    [int64]$_.hwnd_int -ne 0
                } |
                Select-Object -First 1
        )
        if ($qtMain.Count -eq 0) {
            continue
        }
        $mainHwnd = [int64]$qtMain[0].hwnd_int
        foreach ($win32Sample in @($Win32Samples | Sort-Object { [int]$_.elapsed_ms })) {
            $sameMainWindow = @(
                @($win32Sample.windows) |
                    Where-Object {
                        [int64]$_.hwnd_int -eq $mainHwnd
                    } |
                    Select-Object -First 1
            )
            if ($sameMainWindow.Count -gt 0) {
                return [int]$win32Sample.elapsed_ms - [int]$qtSample.elapsed_ms
            }
        }
    }
    return $null
}

function Get-QtPopupSampleMatch {
    param(
        [object]$Win32Sample,
        [int64]$PopupHwnd,
        [object[]]$QtSamples,
        [AllowNull()][object]$QtToWin32OffsetMs,
        [int]$MaxDeltaMs
    )

    $result = [ordered]@{
        legal = $false
        delta_ms = $null
        qt_elapsed_ms = $null
        mapped_qt_elapsed_ms = $null
    }
    if (
        $PopupHwnd -eq 0 -or
        $null -eq $QtToWin32OffsetMs -or
        @($QtSamples).Count -eq 0
    ) {
        return [pscustomobject]$result
    }

    $mappedQtElapsedMs = [int]$Win32Sample.elapsed_ms - [int]$QtToWin32OffsetMs
    $result.mapped_qt_elapsed_ms = $mappedQtElapsedMs
    $nearestSample = $null
    $nearestElapsedMs = 0
    $nearestDeltaMs = [int]::MaxValue
    foreach ($qtSample in @($QtSamples)) {
        $qtElapsedMs = [int]$qtSample.elapsed_ms
        $deltaMs = [Math]::Abs($qtElapsedMs - $mappedQtElapsedMs)
        if (
            $deltaMs -lt $nearestDeltaMs -or
            (
                $deltaMs -eq $nearestDeltaMs -and
                # On an exact tie prefer the later Qt sample. This is the
                # conservative choice when a popup closed between samples:
                # the earlier active state must not exempt a reused HWND.
                ($null -eq $nearestSample -or $qtElapsedMs -gt $nearestElapsedMs)
            )
        ) {
            $nearestSample = $qtSample
            $nearestElapsedMs = $qtElapsedMs
            $nearestDeltaMs = $deltaMs
        }
    }
    if ($null -eq $nearestSample) {
        return [pscustomobject]$result
    }

    $result.delta_ms = $nearestDeltaMs
    $result.qt_elapsed_ms = $nearestElapsedMs
    $nearestPopupHwnd = [int64]$nearestSample.active_popup.hwnd_int
    $result.legal = (
        $nearestDeltaMs -le [Math]::Max(1, $MaxDeltaMs) -and
        $nearestPopupHwnd -eq $PopupHwnd
    )
    return [pscustomobject]$result
}

function Get-QtPopupEventMatch {
    param(
        [int]$Win32ElapsedMs,
        [int64]$PopupHwnd,
        [object[]]$QtSamples,
        [AllowNull()][object]$QtToWin32OffsetMs,
        [int]$MaxDeltaMs
    )

    $result = [ordered]@{
        legal = $false
        delta_ms = $null
        qt_elapsed_ms = $null
        mapped_qt_elapsed_ms = $null
    }
    if (
        $PopupHwnd -eq 0 -or
        $null -eq $QtToWin32OffsetMs -or
        @($QtSamples).Count -eq 0
    ) {
        return [pscustomobject]$result
    }

    $mappedQtElapsedMs = $Win32ElapsedMs - [int]$QtToWin32OffsetMs
    $result.mapped_qt_elapsed_ms = $mappedQtElapsedMs
    $matchingSample = $null
    $matchingDeltaMs = [int]::MaxValue
    foreach ($qtSample in @($QtSamples)) {
        $qtElapsedMs = [int]$qtSample.elapsed_ms
        $deltaMs = [Math]::Abs($qtElapsedMs - $mappedQtElapsedMs)
        if (
            $deltaMs -le [Math]::Max(1, $MaxDeltaMs) -and
            [int64]$qtSample.active_popup.hwnd_int -eq $PopupHwnd -and
            $deltaMs -lt $matchingDeltaMs
        ) {
            $matchingSample = $qtSample
            $matchingDeltaMs = $deltaMs
        }
    }
    if ($null -ne $matchingSample) {
        $result.legal = $true
        $result.delta_ms = $matchingDeltaMs
        $result.qt_elapsed_ms = [int]$matchingSample.elapsed_ms
    }
    return [pscustomobject]$result
}

function Get-MotionComboPopupActions {
    param([object[]]$QtSamples)

    $actions = New-Object System.Collections.ArrayList
    $seenActions = @{}
    $phaseDefinitions = @(
        [pscustomobject]@{
            name = "show"
            hwnd_field = "popup_hwnd_int"
            request_field = "show_request_elapsed_ms"
            duration_fields = @("show_elapsed_ms")
        },
        [pscustomobject]@{
            name = "reopen"
            hwnd_field = "reopen_popup_hwnd_int"
            request_field = "reopen_request_elapsed_ms"
            duration_fields = @("reopen_elapsed_ms", "reopen_show_elapsed_ms")
        }
    )
    foreach ($qtSample in @($QtSamples)) {
        foreach ($comboResult in @($qtSample.motion.combo_results)) {
            if ($null -eq $comboResult) {
                continue
            }
            foreach ($phase in $phaseDefinitions) {
                $hwndProperty = $comboResult.PSObject.Properties[$phase.hwnd_field]
                $requestProperty = $comboResult.PSObject.Properties[$phase.request_field]
                $durationProperty = $null
                foreach ($durationField in @($phase.duration_fields)) {
                    $candidateProperty = $comboResult.PSObject.Properties[$durationField]
                    if ($null -ne $candidateProperty) {
                        $durationProperty = $candidateProperty
                        break
                    }
                }
                if (
                    $null -eq $hwndProperty -or
                    $null -eq $requestProperty -or
                    $null -eq $durationProperty -or
                    $null -eq $hwndProperty.Value -or
                    $null -eq $requestProperty.Value -or
                    $null -eq $durationProperty.Value
                ) {
                    continue
                }
                try {
                    $candidateHwnd = [int64]$hwndProperty.Value
                    $requestElapsedMs = [double]$requestProperty.Value
                    $durationMs = [double]$durationProperty.Value
                } catch {
                    continue
                }
                if (
                    $candidateHwnd -eq 0 -or
                    $requestElapsedMs -lt 0.0 -or
                    $durationMs -lt 0.0 -or
                    [double]::IsNaN($requestElapsedMs) -or
                    [double]::IsInfinity($requestElapsedMs) -or
                    [double]::IsNaN($durationMs) -or
                    [double]::IsInfinity($durationMs)
                ) {
                    continue
                }
                $actionKey = "{0}|{1}|{2}" -f (
                    [string]$phase.name,
                    $candidateHwnd,
                    $requestElapsedMs.ToString(
                        "R",
                        [System.Globalization.CultureInfo]::InvariantCulture
                    )
                )
                if ($seenActions.ContainsKey($actionKey)) {
                    continue
                }
                $seenActions[$actionKey] = $true
                [void]$actions.Add([pscustomobject]@{
                    action_index = $actions.Count
                    action_key = $actionKey
                    phase = [string]$phase.name
                    kind = [string]$comboResult.kind
                    popup_hwnd_int = $candidateHwnd
                    request_elapsed_ms = $requestElapsedMs
                    duration_ms = $durationMs
                })
            }
        }
    }
    return @($actions)
}

function Get-MotionComboPopupEventAssignments {
    param(
        [object[]]$NativeShowEvents,
        [object[]]$QtSamples,
        [int]$ToleranceMs
    )

    $tolerance = [Math]::Max(1, $ToleranceMs)
    $actions = @(Get-MotionComboPopupActions -QtSamples $QtSamples)
    $declaredActionHwnds = New-Object 'System.Collections.Generic.HashSet[long]'
    foreach ($action in $actions) {
        [void]$declaredActionHwnds.Add([int64]$action.popup_hwnd_int)
    }
    $eligibleEvents = New-Object System.Collections.ArrayList
    for ($eventIndex = 0; $eventIndex -lt @($NativeShowEvents).Count; $eventIndex += 1) {
        $nativeEvent = @($NativeShowEvents)[$eventIndex]
        $record = $nativeEvent.window
        if ($null -eq $record) {
            continue
        }
        $knownQtPopupSurface = [bool](
            [string]$record.class_name -like "Qt*QWindowPopup*"
        )
        $metadataLostExactActionSurface = [bool](
            [bool]$record.metadata_lost -and
            $declaredActionHwnds.Contains([int64]$record.hwnd_int)
        )
        if (
            -not [bool]$record.target_process -or
            [bool]$record.internal_helper_candidate -or
            [bool]$record.system_shadow -or
            (-not $knownQtPopupSurface -and -not $metadataLostExactActionSurface) -or
            (
                [string]$nativeEvent.event_name -ne "SHOW" -and
                [string]$nativeEvent.event_name -ne "UNCLOAKED"
            )
        ) {
            continue
        }
        [void]$eligibleEvents.Add([pscustomobject]@{
            event_index = $eventIndex
            popup_hwnd_int = [int64]$record.hwnd_int
            elapsed_ms = [double]$nativeEvent.elapsed_ms
        })
    }

    $result = [ordered]@{
        calibrated = $false
        calibrated_offset_ms = $null
        action_count = $actions.Count
        declared_popup_hwnds = @(
            $actions |
                ForEach-Object { [int64]$_.popup_hwnd_int } |
                Sort-Object -Unique
        )
        eligible_event_count = $eligibleEvents.Count
        consensus_match_count = 0
        max_offset_deviation_ms = $null
        assignments = @()
    }
    # A single action/event pair can manufacture any offset. Require multiple
    # independent actions before motion metadata can exempt a native SHOW.
    if ($actions.Count -lt 2 -or $eligibleEvents.Count -lt 2) {
        return [pscustomobject]$result
    }

    $candidateOffsets = New-Object System.Collections.ArrayList
    foreach ($action in $actions) {
        foreach ($event in $eligibleEvents) {
            if ([int64]$event.popup_hwnd_int -eq [int64]$action.popup_hwnd_int) {
                [void]$candidateOffsets.Add(
                    [double]$event.elapsed_ms - [double]$action.request_elapsed_ms
                )
            }
        }
    }
    $bestAssignments = @()
    $bestMedianOffsetMs = $null
    $bestMaxDeviationMs = [double]::PositiveInfinity
    $bestResidualSumMs = [double]::PositiveInfinity
    foreach ($candidateOffsetMs in @($candidateOffsets)) {
        $possiblePairs = @(
            foreach ($action in $actions) {
                foreach ($event in $eligibleEvents) {
                    if ([int64]$event.popup_hwnd_int -ne [int64]$action.popup_hwnd_int) {
                        continue
                    }
                    $pairOffsetMs = (
                        [double]$event.elapsed_ms -
                        [double]$action.request_elapsed_ms
                    )
                    $residualMs = [Math]::Abs($pairOffsetMs - $candidateOffsetMs)
                    if ($residualMs -le $tolerance) {
                        [pscustomobject]@{
                            action = $action
                            event = $event
                            pair_offset_ms = $pairOffsetMs
                            residual_ms = $residualMs
                        }
                    }
                }
            }
        ) | Sort-Object `
            @{ Expression = { [double]$_.residual_ms } }, `
            @{ Expression = { [double]$_.event.elapsed_ms } }
        $usedActions = @{}
        $usedEvents = @{}
        $candidateAssignments = @(
            foreach ($pair in $possiblePairs) {
                $actionKey = [string]$pair.action.action_key
                $eventKey = [string]$pair.event.event_index
                if (
                    $usedActions.ContainsKey($actionKey) -or
                    $usedEvents.ContainsKey($eventKey)
                ) {
                    continue
                }
                $usedActions[$actionKey] = $true
                $usedEvents[$eventKey] = $true
                $pair
            }
        )
        if ($candidateAssignments.Count -eq 0) {
            continue
        }
        $sortedOffsets = @(
            $candidateAssignments |
                ForEach-Object { [double]$_.pair_offset_ms } |
                Sort-Object
        )
        $middle = [int][Math]::Floor($sortedOffsets.Count / 2)
        $medianOffsetMs = if (($sortedOffsets.Count % 2) -eq 1) {
            [double]$sortedOffsets[$middle]
        } else {
            ([double]$sortedOffsets[$middle - 1] + [double]$sortedOffsets[$middle]) / 2.0
        }
        $maxDeviationMs = [double](
            $sortedOffsets |
                ForEach-Object { [Math]::Abs([double]$_ - $medianOffsetMs) } |
                Measure-Object -Maximum
        ).Maximum
        $residualSumMs = [double](
            $candidateAssignments |
                Measure-Object -Property residual_ms -Sum
        ).Sum
        if (
            $candidateAssignments.Count -gt $bestAssignments.Count -or
            (
                $candidateAssignments.Count -eq $bestAssignments.Count -and
                (
                    $maxDeviationMs -lt $bestMaxDeviationMs -or
                    (
                        $maxDeviationMs -eq $bestMaxDeviationMs -and
                        $residualSumMs -lt $bestResidualSumMs
                    )
                )
            )
        ) {
            $bestAssignments = $candidateAssignments
            $bestMedianOffsetMs = $medianOffsetMs
            $bestMaxDeviationMs = $maxDeviationMs
            $bestResidualSumMs = $residualSumMs
        }
    }
    if ($bestAssignments.Count -lt 2 -or $bestMaxDeviationMs -gt $tolerance) {
        return [pscustomobject]$result
    }

    # Reassign around the consensus median using the declared synchronous show
    # duration. Each action and each SHOW can be consumed at most once; any
    # duplicate or late SHOW remains unmatched and therefore suspicious.
    $finalPossiblePairs = @(
        foreach ($action in $actions) {
            foreach ($event in $eligibleEvents) {
                if ([int64]$event.popup_hwnd_int -ne [int64]$action.popup_hwnd_int) {
                    continue
                }
                $relativeMs = (
                    [double]$event.elapsed_ms -
                    [double]$action.request_elapsed_ms -
                    [double]$bestMedianOffsetMs
                )
                if (
                    $relativeMs -lt -$tolerance -or
                    $relativeMs -gt ([double]$action.duration_ms + $tolerance)
                ) {
                    continue
                }
                $distanceToShowWindowMs = if ($relativeMs -lt 0.0) {
                    -$relativeMs
                } elseif ($relativeMs -gt [double]$action.duration_ms) {
                    $relativeMs - [double]$action.duration_ms
                } else {
                    0.0
                }
                [pscustomobject]@{
                    action = $action
                    event = $event
                    pair_offset_ms = (
                        [double]$event.elapsed_ms -
                        [double]$action.request_elapsed_ms
                    )
                    relative_ms = $relativeMs
                    distance_to_show_window_ms = $distanceToShowWindowMs
                }
            }
        }
    ) | Sort-Object `
        @{ Expression = { [double]$_.distance_to_show_window_ms } }, `
        @{ Expression = { [Math]::Abs([double]$_.relative_ms) } }, `
        @{ Expression = { [double]$_.event.elapsed_ms } }
    $usedActions = @{}
    $usedEvents = @{}
    $finalAssignments = New-Object System.Collections.ArrayList
    foreach ($pair in $finalPossiblePairs) {
        $actionKey = [string]$pair.action.action_key
        $eventKey = [string]$pair.event.event_index
        if (
            $usedActions.ContainsKey($actionKey) -or
            $usedEvents.ContainsKey($eventKey)
        ) {
            continue
        }
        $usedActions[$actionKey] = $true
        $usedEvents[$eventKey] = $true
        [void]$finalAssignments.Add([pscustomobject]@{
            event_index = [int]$pair.event.event_index
            action_index = [int]$pair.action.action_index
            phase = [string]$pair.action.phase
            kind = [string]$pair.action.kind
            popup_hwnd_int = [int64]$pair.action.popup_hwnd_int
            request_elapsed_ms = [double]$pair.action.request_elapsed_ms
            duration_ms = [double]$pair.action.duration_ms
            event_elapsed_ms = [double]$pair.event.elapsed_ms
            calibrated_offset_ms = [double]$bestMedianOffsetMs
            pair_offset_ms = [double]$pair.pair_offset_ms
            offset_deviation_ms = [Math]::Abs(
                [double]$pair.pair_offset_ms - [double]$bestMedianOffsetMs
            )
            mapped_qt_elapsed_ms = (
                [double]$pair.event.elapsed_ms - [double]$bestMedianOffsetMs
            )
            window_start_ms = (
                [double]$pair.action.request_elapsed_ms +
                [double]$bestMedianOffsetMs -
                $tolerance
            )
            window_end_ms = (
                [double]$pair.action.request_elapsed_ms +
                [double]$bestMedianOffsetMs +
                [double]$pair.action.duration_ms +
                $tolerance
            )
        })
    }
    if ($finalAssignments.Count -lt 2) {
        return [pscustomobject]$result
    }
    $result.calibrated = $true
    $result.calibrated_offset_ms = [double]$bestMedianOffsetMs
    $result.consensus_match_count = $finalAssignments.Count
    $result.max_offset_deviation_ms = [double](
        @($finalAssignments) |
            Measure-Object -Property offset_deviation_ms -Maximum
    ).Maximum
    $result.assignments = @($finalAssignments)
    return [pscustomobject]$result
}

function Get-NativeMetadataLostShowAssessment {
    param(
        [object]$NativeEvent,
        [int]$NativeShowEventIndex,
        [object]$MotionComboMatch
    )

    $record = $NativeEvent.window
    $isShowEvent = [bool](
        [string]$NativeEvent.event_name -eq "SHOW" -or
        [string]$NativeEvent.event_name -eq "UNCLOAKED"
    )
    $metadataLostTargetShow = [bool](
        $null -ne $record -and
        $isShowEvent -and
        [bool]$record.target_process -and
        [bool]$record.metadata_lost
    )
    $exactMotionActionExplained = [bool](
        $metadataLostTargetShow -and
        $null -ne $MotionComboMatch -and
        $null -ne $MotionComboMatch.event_index -and
        [int]$MotionComboMatch.event_index -eq $NativeShowEventIndex -and
        $null -ne $MotionComboMatch.popup_hwnd_int -and
        [int64]$MotionComboMatch.popup_hwnd_int -eq [int64]$record.hwnd_int
    )
    return [pscustomobject]@{
        metadata_lost_target_show = $metadataLostTargetShow
        exact_motion_action_explained = $exactMotionActionExplained
        unclassified_target_show = [bool](
            $metadataLostTargetShow -and -not $exactMotionActionExplained
        )
    }
}

function Get-TargetProcessIds([int]$RootProcessId, [string]$ResolvedExePath) {
    $ids = New-Object System.Collections.Generic.HashSet[int]
    [void]$ids.Add($RootProcessId)
    foreach ($process in @(Get-Process -Name Tesseract -ErrorAction SilentlyContinue)) {
        try {
            if ([System.String]::Equals($process.Path, $ResolvedExePath, [System.StringComparison]::OrdinalIgnoreCase)) {
                [void]$ids.Add([int]$process.Id)
            }
        } catch {
            continue
        }
    }
    return ,$ids
}

function Start-AuditedProcess {
    $startParameters = @{
        FilePath = $ExePath
        PassThru = $true
        RedirectStandardOutput = $script:ProcessStdoutPath
        RedirectStandardError = $script:ProcessStderrPath
    }
    if ($ArgumentList.Count -gt 0) {
        $startParameters.ArgumentList = $ArgumentList
    }
    return Start-Process @startParameters
}

$ExePath = Resolve-RepoPath $ExePath
$OutputDir = Resolve-RepoPath $OutputDir
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

function Write-ReadonlyRuntimeNetworkContext {
    if ($SkipRuntimeNetworkPreflight -or -not $ExerciseStartAction) {
        return
    }
    $relatedProcesses = @()
    foreach ($processName in @('clash-verge', 'clash-verge-service', 'verge-mihomo')) {
        foreach ($process in @(Get-Process -Name $processName -ErrorAction SilentlyContinue)) {
            $relatedProcesses += [ordered]@{
                name = [string]$process.ProcessName
                pid = [int]$process.Id
            }
        }
    }
    $adapterRecords = @()
    $adapterError = $null
    try {
        foreach ($adapter in @(Get-NetAdapter -ErrorAction Stop | Where-Object {
            $_.Name -eq 'Mihomo' -or
            $_.InterfaceDescription -like '*Meta Tunnel*' -or
            $_.InterfaceDescription -like '*Tailscale*'
        })) {
            $dnsServers = @(
                Get-DnsClientServerAddress -InterfaceIndex $adapter.ifIndex -ErrorAction SilentlyContinue |
                    ForEach-Object { @($_.ServerAddresses) }
            )
            $adapterRecords += [ordered]@{
                name = [string]$adapter.Name
                description = [string]$adapter.InterfaceDescription
                status = [string]$adapter.Status
                interface_index = [int]$adapter.ifIndex
                dns_servers = @($dnsServers)
            }
        }
    } catch {
        $adapterError = "{0}: {1}" -f $_.Exception.GetType().Name, $_.Exception.Message
    }
    $routeRecords = @()
    $routeError = $null
    try {
        foreach ($route in @(Get-NetRoute -AddressFamily IPv4 -ErrorAction Stop | Where-Object {
            $_.DestinationPrefix -eq '0.0.0.0/0' -or
            $_.DestinationPrefix -eq '198.18.0.0/15'
        })) {
            $routeRecords += [ordered]@{
                destination = [string]$route.DestinationPrefix
                interface_index = [int]$route.InterfaceIndex
                next_hop = [string]$route.NextHop
                metric = [int]$route.RouteMetric
            }
        }
    } catch {
        $routeError = "{0}: {1}" -f $_.Exception.GetType().Name, $_.Exception.Message
    }
    $tunSignature = @($adapterRecords | Where-Object {
        $_.description -like '*Meta Tunnel*' -or
        @($_.dns_servers | Where-Object { [string]$_ -like '198.18.*' }).Count -gt 0
    }).Count -gt 0 -or @($routeRecords | Where-Object {
        $_.destination -eq '198.18.0.0/15'
    }).Count -gt 0
    $state = if ($tunSignature) {
        if ($relatedProcesses.Count -gt 0) { 'external_tun_present' } else { 'indeterminate' }
    } elseif ($relatedProcesses.Count -gt 0) {
        'external_proxy_present'
    } else {
        'external_proxy_absent'
    }
    $contextErrors = @($adapterError, $routeError) | Where-Object { $_ }
    $snapshot = [ordered]@{
        diagnosis = 'runtime_network_context'
        observational_only = $true
        captured_at = (Get-Date).ToUniversalTime().ToString('o')
        state = $state
        related_processes = @($relatedProcesses)
        adapters = @($adapterRecords)
        routes = @($routeRecords)
        errors = @($contextErrors)
    }
    $snapshotPath = Join-Path $OutputDir 'runtime-network-context.json'
    $snapshot | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $snapshotPath -Encoding UTF8
}

Write-ReadonlyRuntimeNetworkContext

$typeDefinition = @'
using System;
using System.Text;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Runtime.InteropServices;
public class StartupWindowAudit2 {
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
  [DllImport("user32.dll")] public static extern int GetClassName(IntPtr hWnd, StringBuilder text, int count);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr hWnd, uint command);
  [DllImport("user32.dll")] public static extern IntPtr GetParent(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hwnd, IntPtr hdcBlt, uint nFlags);
  [DllImport("user32.dll", EntryPoint="GetWindowLong")] public static extern int GetWindowLong32(IntPtr hWnd, int nIndex);
  [DllImport("user32.dll", EntryPoint="GetWindowLongPtr")] public static extern IntPtr GetWindowLongPtr64(IntPtr hWnd, int nIndex);
  [DllImport("user32.dll")] public static extern bool GetLayeredWindowAttributes(IntPtr hwnd, out uint colorKey, out byte alpha, out uint flags);
  public delegate void WinEventProc(IntPtr hook, uint eventId, IntPtr hWnd, int objectId, int childId, uint eventThread, uint eventTime);
  [DllImport("user32.dll", SetLastError=true)] public static extern IntPtr SetWinEventHook(uint eventMin, uint eventMax, IntPtr eventHook, WinEventProc callback, uint processId, uint threadId, uint flags);
  [DllImport("user32.dll")] public static extern bool UnhookWinEvent(IntPtr hook);
  [DllImport("user32.dll")] public static extern bool PeekMessage(out MSG message, IntPtr hWnd, uint filterMin, uint filterMax, uint removeMessage);
  [DllImport("user32.dll")] public static extern bool TranslateMessage(ref MSG message);
  [DllImport("user32.dll")] public static extern IntPtr DispatchMessage(ref MSG message);
  public const int GWL_STYLE = -16;
  public const int GWL_EXSTYLE = -20;
  public const uint GW_OWNER = 4;
  public const long WS_EX_LAYERED = 0x00080000;
  public const long WS_THICKFRAME = 0x00040000;
  public const long WS_SYSMENU = 0x00080000;
  public const uint LWA_ALPHA = 0x00000002;
  public const uint WINEVENT_OUTOFCONTEXT = 0x0000;
  public const uint EVENT_SYSTEM_MINIMIZESTART = 0x0016;
  public const uint EVENT_SYSTEM_MINIMIZEEND = 0x0017;
  public const uint EVENT_OBJECT_CREATE = 0x8000;
  public const uint EVENT_OBJECT_DESTROY = 0x8001;
  public const uint EVENT_OBJECT_SHOW = 0x8002;
  public const uint EVENT_OBJECT_HIDE = 0x8003;
  public const uint EVENT_OBJECT_CLOAKED = 0x8017;
  public const uint EVENT_OBJECT_UNCLOAKED = 0x8018;
  public const int OBJID_WINDOW = 0;
  public const int CHILDID_SELF = 0;
  public const uint PM_REMOVE = 0x0001;
  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
  public struct POINT { public int X; public int Y; }
  public struct MSG {
    public IntPtr hwnd;
    public uint message;
    public UIntPtr wParam;
    public IntPtr lParam;
    public uint time;
    public POINT point;
    public uint privateValue;
  }
  public static IntPtr GetWindowLongPtrCompat(IntPtr hWnd, int nIndex) {
    if (IntPtr.Size == 8) {
      return GetWindowLongPtr64(hWnd, nIndex);
    }
    return new IntPtr(GetWindowLong32(hWnd, nIndex));
  }
  public class WindowRecord {
    public long Hwnd;
    public int Pid;
    public bool Visible;
    public string ClassName = "";
    public string Title = "";
    public int Left;
    public int Top;
    public int Right;
    public int Bottom;
    public int Width;
    public int Height;
    public bool Layered;
    public byte Alpha;
    public bool EffectiveVisible;
    public long OwnerHwnd;
    public long ParentHwnd;
    public long Style;
    public long ExStyle;
  }
  public class AuditedWindow {
    public string hwnd { get; set; }
    public long hwnd_int { get; set; }
    public int pid { get; set; }
    public bool visible { get; set; }
    public string class_name { get; set; }
    public string title { get; set; }
    public int[] rect { get; set; }
    public int width { get; set; }
    public int height { get; set; }
    public bool layered { get; set; }
    public int alpha { get; set; }
    public bool effective_visible { get; set; }
    public string owner_hwnd { get; set; }
    public string parent_hwnd { get; set; }
    public long style { get; set; }
    public long ex_style { get; set; }
    public bool baseline_window { get; set; }
    public bool target_process { get; set; }
    public int hook_target_pid { get; set; }
    public bool target_attributed_from_hook { get; set; }
    public bool metadata_lost { get; set; }
    public bool main_candidate { get; set; }
    public bool internal_helper_candidate { get; set; }
    public bool system_shadow { get; set; }
    public bool blank_candidate { get; set; }
    public bool shell_candidate { get; set; }
    public bool suspicious_candidate { get; set; }
    public bool same_pid_suspicious_candidate { get; set; }
  }
  public class WindowSample {
    public int elapsed_ms { get; set; }
    public int[] target_pids { get; set; }
    public AuditedWindow[] windows { get; set; }
  }
  public class NativeWindowEvent {
    public int elapsed_ms { get; set; }
    public uint event_id { get; set; }
    public string event_name { get; set; }
    public int object_id { get; set; }
    public int child_id { get; set; }
    public int hook_target_pid { get; set; }
    public bool target_attributed_from_hook { get; set; }
    public bool metadata_lost { get; set; }
    public AuditedWindow window { get; set; }
  }
  public static NativeWindowEvent[] LastWindowEvents = new NativeWindowEvent[0];
  public static bool LastWindowEventHookActive = false;
  public static int LastWindowEventHookRequestedCount = 0;
  public static int LastWindowEventHookInstalledCount = 0;
  public static string LastWindowEventHookError = "";
  public static int LastNativeAuditElapsedMs = 0;
  public static int LastNativeAuditHardDeadlineMs = 0;
  public static int LastNativeQtCompletionTailMs = 0;
  public static bool LastNativeQtEvidenceObserved = false;
  public static int LastNativeQtEvidenceObservedElapsedMs = -1;
  public static string LastNativeAuditStopReason = "";

  public static bool IsInternalHelperClass(string className) {
    className = className ?? "";
    return
      className.IndexOf("ThemeChangeObserverWindow", StringComparison.OrdinalIgnoreCase) >= 0 ||
      className.IndexOf("ScreenChangeObserverWindow", StringComparison.OrdinalIgnoreCase) >= 0 ||
      className.IndexOf("TrayIconMessageWindowClass", StringComparison.OrdinalIgnoreCase) >= 0 ||
      String.Equals(className, "_q_titlebar", StringComparison.OrdinalIgnoreCase);
  }

  public static bool IsExpectedMainWindow(WindowRecord window, bool effectiveVisible, bool isTargetProcess) {
    string className = window.ClassName ?? "";
    return
      effectiveVisible &&
      isTargetProcess &&
      className.StartsWith("Qt", StringComparison.OrdinalIgnoreCase) &&
      className.EndsWith("QWindowIcon", StringComparison.OrdinalIgnoreCase) &&
      (window.Style & WS_THICKFRAME) != 0 &&
      (window.Style & WS_SYSMENU) != 0;
  }

  public static WindowRecord ReadWindowRecord(IntPtr hWnd) {
    uint processId = 0;
    GetWindowThreadProcessId(hWnd, out processId);
    StringBuilder title = new StringBuilder(512);
    StringBuilder className = new StringBuilder(256);
    GetWindowText(hWnd, title, title.Capacity);
    GetClassName(hWnd, className, className.Capacity);
    RECT rect;
    GetWindowRect(hWnd, out rect);
    int width = Math.Max(0, rect.Right - rect.Left);
    int height = Math.Max(0, rect.Bottom - rect.Top);
    bool visible = IsWindowVisible(hWnd);
    long style = GetWindowLongPtrCompat(hWnd, GWL_STYLE).ToInt64();
    long exStyle = GetWindowLongPtrCompat(hWnd, GWL_EXSTYLE).ToInt64();
    long ownerHwnd = GetWindow(hWnd, GW_OWNER).ToInt64();
    long parentHwnd = GetParent(hWnd).ToInt64();
    bool layered = (exStyle & WS_EX_LAYERED) != 0;
    byte alpha = 255;
    uint colorKey = 0;
    uint flags = 0;
    if (layered) {
      try {
        if (!GetLayeredWindowAttributes(hWnd, out colorKey, out alpha, out flags)) {
          alpha = 255;
          flags = 0;
        }
      } catch {
        alpha = 255;
        flags = 0;
      }
    }
    bool effectiveVisible = visible && !(layered && (flags & LWA_ALPHA) != 0 && alpha <= 5);
    return new WindowRecord {
      Hwnd = hWnd.ToInt64(),
      Pid = (int)processId,
      Visible = visible,
      ClassName = className.ToString(),
      Title = title.ToString(),
      Left = rect.Left,
      Top = rect.Top,
      Right = rect.Right,
      Bottom = rect.Bottom,
      Width = width,
      Height = height,
      Layered = layered,
      Alpha = alpha,
      EffectiveVisible = effectiveVisible,
      OwnerHwnd = ownerHwnd,
      ParentHwnd = parentHwnd,
      Style = style,
      ExStyle = exStyle
    };
  }

  public static bool HasLostHookWindowMetadata(WindowRecord window, int hookTargetPid) {
    if (window == null || hookTargetPid <= 0) {
      return false;
    }
    return
      window.Pid != hookTargetPid ||
      String.IsNullOrWhiteSpace(window.ClassName);
  }

  public static AuditedWindow ToAuditedWindow(WindowRecord window, HashSet<long> baseline, HashSet<int> targetPidSet, bool forceEffectiveVisible) {
    return ToAuditedWindow(window, baseline, targetPidSet, forceEffectiveVisible, 0);
  }

  public static AuditedWindow ToAuditedWindow(WindowRecord window, HashSet<long> baseline, HashSet<int> targetPidSet, bool forceEffectiveVisible, int hookTargetPid) {
    string title = window.Title ?? "";
    string className = window.ClassName ?? "";
    bool isBaseline = baseline.Contains(window.Hwnd);
    bool observedTargetProcess = targetPidSet.Contains(window.Pid);
    bool targetAttributedFromHook =
      hookTargetPid > 0 &&
      targetPidSet.Contains(hookTargetPid) &&
      !observedTargetProcess;
    bool isTargetProcess = observedTargetProcess || targetAttributedFromHook;
    bool metadataLost = HasLostHookWindowMetadata(window, hookTargetPid);
    bool effectiveVisible = forceEffectiveVisible || window.EffectiveVisible;
    bool mainCandidate = IsExpectedMainWindow(window, effectiveVisible, isTargetProcess);
    bool internalHelperCandidate = effectiveVisible && isTargetProcess && IsInternalHelperClass(className);
    bool systemShadow = String.Equals(className, "SysShadow", StringComparison.OrdinalIgnoreCase);
    bool blankCandidate = !systemShadow && effectiveVisible && String.IsNullOrWhiteSpace(title) && window.Width > 40 && window.Height > 20 && window.Width < 700 && window.Height < 500;
    bool titleIsPlainAppName = String.Equals(title, "Tesseract", StringComparison.OrdinalIgnoreCase);
    bool shellCandidate =
      !systemShadow &&
      effectiveVisible &&
      !mainCandidate &&
      window.Width > 40 &&
      window.Height > 20 &&
      window.Width < 700 &&
      window.Height < 500 &&
      (String.IsNullOrWhiteSpace(title) || titleIsPlainAppName) &&
      (className.StartsWith("Qt", StringComparison.OrdinalIgnoreCase) || titleIsPlainAppName);
    bool samePidQtShellCandidate =
      !systemShadow &&
      effectiveVisible &&
      isTargetProcess &&
      !mainCandidate &&
      window.Width > 40 &&
      window.Height > 20 &&
      window.Width < 700 &&
      window.Height < 500 &&
      className.StartsWith("Qt", StringComparison.OrdinalIgnoreCase);
    bool suspiciousCandidate = blankCandidate || shellCandidate || samePidQtShellCandidate || internalHelperCandidate;
    return new AuditedWindow {
      hwnd = "0x" + window.Hwnd.ToString("X"),
      hwnd_int = window.Hwnd,
      pid = window.Pid,
      visible = window.Visible,
      class_name = className,
      title = title,
      rect = new int[] { window.Left, window.Top, window.Right, window.Bottom },
      width = window.Width,
      height = window.Height,
      layered = window.Layered,
      alpha = (int)window.Alpha,
      effective_visible = effectiveVisible,
      owner_hwnd = window.OwnerHwnd == 0 ? "" : "0x" + window.OwnerHwnd.ToString("X"),
      parent_hwnd = window.ParentHwnd == 0 ? "" : "0x" + window.ParentHwnd.ToString("X"),
      style = window.Style,
      ex_style = window.ExStyle,
      baseline_window = isBaseline,
      target_process = isTargetProcess,
      hook_target_pid = hookTargetPid,
      target_attributed_from_hook = targetAttributedFromHook,
      metadata_lost = metadataLost,
      main_candidate = mainCandidate,
      internal_helper_candidate = internalHelperCandidate,
      system_shadow = systemShadow,
      blank_candidate = blankCandidate,
      shell_candidate = shellCandidate,
      suspicious_candidate = suspiciousCandidate,
      same_pid_suspicious_candidate = isTargetProcess && suspiciousCandidate
    };
  }

  public static string WinEventName(uint eventId) {
    switch (eventId) {
      case EVENT_SYSTEM_MINIMIZESTART: return "MINIMIZE_START";
      case EVENT_SYSTEM_MINIMIZEEND: return "MINIMIZE_END";
      case EVENT_OBJECT_CREATE: return "CREATE";
      case EVENT_OBJECT_DESTROY: return "DESTROY";
      case EVENT_OBJECT_SHOW: return "SHOW";
      case EVENT_OBJECT_HIDE: return "HIDE";
      case EVENT_OBJECT_CLOAKED: return "CLOAKED";
      case EVENT_OBJECT_UNCLOAKED: return "UNCLOAKED";
      default: return "0x" + eventId.ToString("X");
    }
  }

  public static bool IsRecordedWinEvent(uint eventId) {
    return
      eventId == EVENT_SYSTEM_MINIMIZESTART ||
      eventId == EVENT_SYSTEM_MINIMIZEEND ||
      eventId == EVENT_OBJECT_CREATE ||
      eventId == EVENT_OBJECT_DESTROY ||
      eventId == EVENT_OBJECT_SHOW ||
      eventId == EVENT_OBJECT_HIDE ||
      eventId == EVENT_OBJECT_CLOAKED ||
      eventId == EVENT_OBJECT_UNCLOAKED;
  }

  public static void PumpWinEvents() {
    MSG message;
    while (PeekMessage(out message, IntPtr.Zero, 0, 0, PM_REMOVE)) {
      TranslateMessage(ref message);
      DispatchMessage(ref message);
    }
  }
  public static WindowRecord[] GetTopLevelWindows() {
    List<WindowRecord> records = new List<WindowRecord>();
    EnumWindows(delegate(IntPtr hWnd, IntPtr lParam) {
      records.Add(ReadWindowRecord(hWnd));
      return true;
    }, IntPtr.Zero);
    return records.ToArray();
  }
  public static int[] GetTargetProcessIds(int rootProcessId, string exePath) {
    HashSet<int> ids = new HashSet<int>();
    ids.Add(rootProcessId);
    foreach (Process process in Process.GetProcessesByName("Tesseract")) {
      try {
        string path = process.MainModule == null ? "" : process.MainModule.FileName;
        if (String.Equals(path, exePath, StringComparison.OrdinalIgnoreCase)) {
          ids.Add(process.Id);
        }
      } catch {
      }
    }
    int[] result = new int[ids.Count];
    ids.CopyTo(result);
    return result;
  }
  public static bool RootProcessExited(int rootProcessId) {
    try {
      using (Process process = Process.GetProcessById(rootProcessId)) {
        return process.HasExited;
      }
    } catch (ArgumentException) {
      return true;
    } catch {
      // An access error is not proof that the process exited. Preserve the
      // bounded audit until Qt evidence appears or the hard deadline expires.
      return false;
    }
  }
  public static WindowSample[] SampleWindows(
    int rootProcessId,
    string exePath,
    long[] baselineHwnds,
    int durationMs,
    int intervalMs,
    bool includeAllNewWindows,
    string qtAuditPath,
    int qtPublishTimeoutMs,
    int qtCompletionTailMs
  ) {
    HashSet<long> baseline = new HashSet<long>(baselineHwnds ?? new long[0]);
    List<WindowSample> samples = new List<WindowSample>();
    Stopwatch stopwatch = Stopwatch.StartNew();
    int sampleIndex = 0;
    int[] targetPids = GetTargetProcessIds(rootProcessId, exePath);
    HashSet<int> targetPidSet = new HashSet<int>(targetPids);
    int[] hookTargetPids = (int[])targetPids.Clone();
    HashSet<int> hookTargetPidSet = new HashSet<int>(hookTargetPids);
    Dictionary<IntPtr, int> hookTargetPidByHandle = new Dictionary<IntPtr, int>();
    List<NativeWindowEvent> nativeEvents = new List<NativeWindowEvent>();
    WinEventProc callback = delegate(IntPtr hook, uint eventId, IntPtr hWnd, int objectId, int childId, uint eventThread, uint eventTime) {
      if (
        hWnd == IntPtr.Zero ||
        !IsRecordedWinEvent(eventId) ||
        (eventId >= EVENT_OBJECT_CREATE && (objectId != OBJID_WINDOW || childId != CHILDID_SELF))
      ) {
        return;
      }
      int hookTargetPid = 0;
      hookTargetPidByHandle.TryGetValue(hook, out hookTargetPid);
      WindowRecord eventWindow = ReadWindowRecord(hWnd);
      bool forceEffectiveVisible = eventId == EVENT_OBJECT_SHOW || eventId == EVENT_OBJECT_UNCLOAKED;
      AuditedWindow auditedEventWindow = ToAuditedWindow(
        eventWindow,
        baseline,
        hookTargetPidSet,
        forceEffectiveVisible,
        hookTargetPid
      );
      lock (nativeEvents) {
        nativeEvents.Add(new NativeWindowEvent {
          elapsed_ms = (int)stopwatch.ElapsedMilliseconds,
          event_id = eventId,
          event_name = WinEventName(eventId),
          object_id = objectId,
          child_id = childId,
          hook_target_pid = hookTargetPid,
          target_attributed_from_hook = auditedEventWindow.target_attributed_from_hook,
          metadata_lost = auditedEventWindow.metadata_lost,
          window = auditedEventWindow
        });
      }
    };
    intervalMs = Math.Max(5, intervalMs);
    int minimumEndMs = Math.Max(100, durationMs) + 400;
    bool waitForQtEvidence = !String.IsNullOrWhiteSpace(qtAuditPath);
    qtCompletionTailMs = Math.Max(100, qtCompletionTailMs);
    int hardDeadlineMs = waitForQtEvidence
      ? Math.Max(
          minimumEndMs + qtCompletionTailMs,
          Math.Max(100, durationMs) + Math.Max(1000, qtPublishTimeoutMs)
        )
      : minimumEndMs;
    LastWindowEvents = new NativeWindowEvent[0];
    LastWindowEventHookActive = false;
    LastWindowEventHookRequestedCount = hookTargetPids.Length * 2;
    LastWindowEventHookInstalledCount = 0;
    LastWindowEventHookError = "";
    LastNativeAuditElapsedMs = 0;
    LastNativeAuditHardDeadlineMs = hardDeadlineMs;
    LastNativeQtCompletionTailMs = waitForQtEvidence ? qtCompletionTailMs : 0;
    LastNativeQtEvidenceObserved = false;
    LastNativeQtEvidenceObservedElapsedMs = -1;
    LastNativeAuditStopReason = "sampling_failed";
    ManualResetEvent hookReady = new ManualResetEvent(false);
    ManualResetEvent hookStop = new ManualResetEvent(false);
    Thread hookThread = new Thread(delegate() {
      List<IntPtr> eventHooks = new List<IntPtr>();
      try {
        foreach (int targetPid in hookTargetPids) {
          IntPtr systemHook = SetWinEventHook(
            EVENT_SYSTEM_MINIMIZESTART,
            EVENT_SYSTEM_MINIMIZEEND,
            IntPtr.Zero,
            callback,
            (uint)targetPid,
            0,
            WINEVENT_OUTOFCONTEXT
          );
          if (systemHook != IntPtr.Zero) {
            eventHooks.Add(systemHook);
            hookTargetPidByHandle[systemHook] = targetPid;
          }
          IntPtr objectHook = SetWinEventHook(
            EVENT_OBJECT_CREATE,
            EVENT_OBJECT_UNCLOAKED,
            IntPtr.Zero,
            callback,
            (uint)targetPid,
            0,
            WINEVENT_OUTOFCONTEXT
          );
          if (objectHook != IntPtr.Zero) {
            eventHooks.Add(objectHook);
            hookTargetPidByHandle[objectHook] = targetPid;
          }
        }
        LastWindowEventHookInstalledCount = eventHooks.Count;
        LastWindowEventHookActive =
          LastWindowEventHookRequestedCount > 0 &&
          LastWindowEventHookInstalledCount == LastWindowEventHookRequestedCount;
        hookReady.Set();
        while (!hookStop.WaitOne(1)) {
          PumpWinEvents();
        }
        PumpWinEvents();
      } catch (Exception exception) {
        LastWindowEventHookActive = false;
        LastWindowEventHookError = exception.GetType().Name + ": " + exception.Message;
        hookReady.Set();
      } finally {
        foreach (IntPtr eventHook in eventHooks) {
          UnhookWinEvent(eventHook);
        }
        hookReady.Set();
        GC.KeepAlive(callback);
      }
    });
    hookThread.IsBackground = true;
    hookThread.Name = "TesseractWinEventAudit";
    hookThread.Start();
    if (!hookReady.WaitOne(2000)) {
      LastWindowEventHookActive = false;
      LastWindowEventHookError = "WinEvent hook thread did not become ready within 2000 ms.";
    }
    try {
      while (true) {
        long iterationStarted = stopwatch.ElapsedMilliseconds;
        if (sampleIndex % 5 == 0) {
          targetPids = GetTargetProcessIds(rootProcessId, exePath);
          targetPidSet = new HashSet<int>(targetPids);
        }
        List<AuditedWindow> windows = new List<AuditedWindow>();
        foreach (WindowRecord window in GetTopLevelWindows()) {
          bool isBaseline = baseline.Contains(window.Hwnd);
          bool isTargetProcess = targetPidSet.Contains(window.Pid);
          bool includeAsNewWindow = includeAllNewWindows && !isBaseline && window.EffectiveVisible && window.Width > 0 && window.Height > 0;
          if (!(isTargetProcess || includeAsNewWindow)) {
            continue;
          }
          windows.Add(ToAuditedWindow(window, baseline, targetPidSet, false));
        }
        samples.Add(new WindowSample {
          elapsed_ms = (int)stopwatch.ElapsedMilliseconds,
          target_pids = targetPids,
          windows = windows.ToArray()
        });
        long elapsedAfterSample = stopwatch.ElapsedMilliseconds;
        if (waitForQtEvidence) {
          if (!LastNativeQtEvidenceObserved && File.Exists(qtAuditPath)) {
            LastNativeQtEvidenceObserved = true;
            LastNativeQtEvidenceObservedElapsedMs = (int)elapsedAfterSample;
          }
          if (
            LastNativeQtEvidenceObserved &&
            elapsedAfterSample >= minimumEndMs &&
            elapsedAfterSample - LastNativeQtEvidenceObservedElapsedMs >= qtCompletionTailMs
          ) {
            LastNativeAuditStopReason = "qt_evidence_published";
            break;
          }
          if (
            !LastNativeQtEvidenceObserved &&
            sampleIndex % 5 == 0 &&
            RootProcessExited(rootProcessId)
          ) {
            // The Qt publisher writes the final path atomically. If the root
            // process is gone and the final path is still absent, no later
            // publication from this audited process is possible.
            LastNativeAuditStopReason = "root_process_exited_without_qt_evidence";
            break;
          }
          if (elapsedAfterSample >= hardDeadlineMs) {
            LastNativeAuditStopReason = LastNativeQtEvidenceObserved
              ? "qt_evidence_completion_tail_timeout"
              : "qt_evidence_timeout";
            break;
          }
        } else if (elapsedAfterSample >= minimumEndMs) {
          LastNativeAuditStopReason = "fixed_duration_no_qt_audit";
          break;
        }
        int elapsedThisIteration = (int)(stopwatch.ElapsedMilliseconds - iterationStarted);
        Thread.Sleep(Math.Max(1, intervalMs - elapsedThisIteration));
        sampleIndex += 1;
      }
    } finally {
      LastNativeAuditElapsedMs = (int)stopwatch.ElapsedMilliseconds;
      hookStop.Set();
      bool hookThreadStopped = hookThread.Join(5000);
      if (!hookThreadStopped) {
        LastWindowEventHookActive = false;
        LastWindowEventHookError = "WinEvent hook thread did not stop within 5000 ms.";
      }
      lock (nativeEvents) {
        LastWindowEvents = nativeEvents.ToArray();
      }
      if (hookThreadStopped) {
        hookReady.Dispose();
        hookStop.Dispose();
      }
    }
    return samples.ToArray();
  }
}
'@
Add-Type -TypeDefinition $typeDefinition
Add-Type -AssemblyName System.Drawing

function Get-AllTopLevelWindows {
    $records = New-Object System.Collections.ArrayList
    foreach ($window in [StartupWindowAudit2]::GetTopLevelWindows()) {
        [void]$records.Add([pscustomobject]@{
            hwnd = "0x$('{0:X}' -f $window.Hwnd)"
            hwnd_int = $window.Hwnd
            pid = [int]$window.Pid
            visible = [bool]$window.Visible
            class = [string]$window.ClassName
            title = [string]$window.Title
            rect = @($window.Left, $window.Top, $window.Right, $window.Bottom)
            width = [int]$window.Width
            height = [int]$window.Height
            effective_visible = [bool]$window.EffectiveVisible
            owner_hwnd = [int64]$window.OwnerHwnd
            parent_hwnd = [int64]$window.ParentHwnd
            style = [int64]$window.Style
            ex_style = [int64]$window.ExStyle
        })
    }
    return @($records)
}

function Stop-TargetAppProcesses([string]$ResolvedExePath) {
    $targetRoot = Split-Path -Parent $ResolvedExePath
    foreach ($targetProcess in @(Get-Process -ErrorAction SilentlyContinue)) {
        try {
            if ($targetProcess.Path.StartsWith($targetRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
                Stop-Process -Id $targetProcess.Id -Force -ErrorAction SilentlyContinue
            }
        } catch {
            continue
        }
    }
}

function Save-WindowScreenshot($Window, [string]$Path) {
    if (-not $Window.visible -or $Window.width -le 0 -or $Window.height -le 0) {
        return $false
    }
    $bitmap = New-Object System.Drawing.Bitmap $Window.width, $Window.height
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $hdc = $graphics.GetHdc()
        try {
            $ok = [StartupWindowAudit2]::PrintWindow([IntPtr]::new([int64]$Window.hwnd_int), $hdc, 2)
        } finally {
            $graphics.ReleaseHdc($hdc)
        }
        if (-not $ok) {
            $graphics.CopyFromScreen($Window.rect[0], $Window.rect[1], 0, 0, [System.Drawing.Size]::new($Window.width, $Window.height))
        }
        $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
        return $true
    } catch {
        return $false
    } finally {
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

function Convert-ToAuditedWindow($Window, $BaselineWindows, $TargetProcessIds) {
    $title = [string]$Window.title
    $class = [string]$Window.class
    $visible = [bool]$Window.visible
    $isBaseline = $BaselineWindows.ContainsKey($Window.hwnd)
    $isTargetProcess = $TargetProcessIds.Contains([int]$Window.pid)
    if (-not $isTargetProcess) {
        return $null
    }

    $effectiveVisible = if ($null -ne $Window.effective_visible) {
        [bool]$Window.effective_visible
    } else {
        $visible
    }
    $qtMainClass = $class -like "Qt*QWindowIcon"
    $hasMainFrameStyle = (
        ([int64]$Window.style -band [int64]0x00040000) -ne 0 -and
        ([int64]$Window.style -band [int64]0x00080000) -ne 0
    )
    $isMainCandidate = (
        $effectiveVisible -and
        $isTargetProcess -and
        $qtMainClass -and
        $hasMainFrameStyle
    )
    $internalHelperCandidate = $effectiveVisible -and $isTargetProcess -and (
        $class -like "*ThemeChangeObserverWindow*" -or
        $class -like "*ScreenChangeObserverWindow*" -or
        $class -like "*TrayIconMessageWindowClass*" -or
        $class -eq "_q_titlebar"
    )
    $systemShadow = [System.String]::Equals($class, "SysShadow", [System.StringComparison]::OrdinalIgnoreCase)
    $blankCandidate = -not $systemShadow -and $effectiveVisible -and [string]::IsNullOrWhiteSpace($title) -and $Window.width -gt 40 -and $Window.height -gt 20 -and $Window.width -lt 700 -and $Window.height -lt 500
    $shellCandidate = -not $systemShadow -and $effectiveVisible -and -not $isMainCandidate -and $Window.width -gt 40 -and $Window.height -gt 20 -and $Window.width -lt 700 -and $Window.height -lt 500 -and (
        [string]::IsNullOrWhiteSpace($title) -or
        [System.String]::Equals($title, "Tesseract", [System.StringComparison]::OrdinalIgnoreCase)
    ) -and (
        $class -like "Qt*" -or
        [System.String]::Equals($title, "Tesseract", [System.StringComparison]::OrdinalIgnoreCase)
    )
    $suspiciousCandidate = $blankCandidate -or $shellCandidate -or $internalHelperCandidate

    return [pscustomobject]@{
        hwnd = $Window.hwnd
        hwnd_int = $Window.hwnd_int
        pid = $Window.pid
        visible = $visible
        class = $class
        title = $title
        rect = $Window.rect
        width = $Window.width
        height = $Window.height
        baseline_window = $isBaseline
        target_process = $isTargetProcess
        main_candidate = $isMainCandidate
        internal_helper_candidate = $internalHelperCandidate
        system_shadow = $systemShadow
        blank_candidate = $blankCandidate
        shell_candidate = $shellCandidate
        suspicious_candidate = $suspiciousCandidate
        same_pid_suspicious_candidate = $isTargetProcess -and $suspiciousCandidate
    }
}

$qtAuditPath = Join-Path $OutputDir "qt-startup-windows.json"
$win32AuditPath = Join-Path $OutputDir "win32-startup-windows.json"
$win32EventAuditPath = Join-Path $OutputDir "win32-window-events.json"
$summaryPath = Join-Path $OutputDir "summary.txt"
$script:ProcessStdoutPath = Join-Path $OutputDir "process.stdout.log"
$script:ProcessStderrPath = Join-Path $OutputDir "process.stderr.log"
Remove-Item -LiteralPath @(
    $qtAuditPath,
    $win32AuditPath,
    $win32EventAuditPath,
    $summaryPath,
    $script:ProcessStdoutPath,
    $script:ProcessStderrPath,
    (Join-Path $OutputDir "qt-startup-final-window.png"),
    (Join-Path $OutputDir "startup-final-window.png")
) -Force -ErrorAction SilentlyContinue

$baselineHwnds = @(
    foreach ($window in [StartupWindowAudit2]::GetTopLevelWindows()) {
        [int64]$window.Hwnd
    }
)

if ($NoQtAudit) {
    $process = Start-AuditedProcess
} else {
    $oldAuditPath = $env:TESSERACT_STARTUP_AUDIT_PATH
    $oldAuditDuration = $env:TESSERACT_STARTUP_AUDIT_DURATION_MS
    $oldAuditInterval = $env:TESSERACT_STARTUP_AUDIT_INTERVAL_MS
    $oldAuditQuit = $env:TESSERACT_STARTUP_AUDIT_QUIT
    $oldAuditAction = $env:TESSERACT_STARTUP_AUDIT_ACTION

    # Leave a deterministic publication margin before the Win32 sampling loop
    # finishes and starts process cleanup.
    $qtAuditPublishMarginMs = [Math]::Max(500, 2 * [Math]::Max(5, $IntervalMs))
    $qtAuditDurationMs = [Math]::Max(100, $DurationMs - $qtAuditPublishMarginMs)
    $env:TESSERACT_STARTUP_AUDIT_PATH = $qtAuditPath
    $env:TESSERACT_STARTUP_AUDIT_DURATION_MS = [string]$qtAuditDurationMs
    $env:TESSERACT_STARTUP_AUDIT_INTERVAL_MS = [string]$IntervalMs
    $env:TESSERACT_STARTUP_AUDIT_QUIT = "1"
    $env:TESSERACT_STARTUP_AUDIT_ACTION = if ($ExerciseStartAction) {
        "start-services"
    } elseif ($ExerciseMotionAction) {
        "ui-motion"
    } else {
        $null
    }

    try {
        $process = Start-AuditedProcess
    } finally {
        $env:TESSERACT_STARTUP_AUDIT_PATH = $oldAuditPath
        $env:TESSERACT_STARTUP_AUDIT_DURATION_MS = $oldAuditDuration
        $env:TESSERACT_STARTUP_AUDIT_INTERVAL_MS = $oldAuditInterval
        $env:TESSERACT_STARTUP_AUDIT_QUIT = $oldAuditQuit
        $env:TESSERACT_STARTUP_AUDIT_ACTION = $oldAuditAction
    }
}

$blankSeen = $false
$suspiciousSeen = $false
$transientSuspiciousSeen = $false
$samePidSuspiciousSeen = $false
$samePidSuspiciousSampleCount = 0
$nativeSuspiciousShowSeen = $false
$nativeSuspiciousShowCount = 0
$nativeInternalHelperShowCount = 0
$nativeLegalQtPopupShowCount = 0
$nativeMotionComboPopupShowCount = 0
$nativeSystemShadowShowCount = 0
$nativeUnmatchedQtPopupShowCount = 0
$nativeMetadataLostTargetShowCount = 0
$nativeUnclassifiedTargetShowCount = 0
$persistentSuspiciousSeen = $false
$maxSuspiciousObservedSpanMs = 0
$suspiciousScreenshots = New-Object System.Collections.ArrayList
$screenshotHandles = @{}
$suspiciousLifetimes = @{}
$IntervalMs = [Math]::Max(5, $IntervalMs)
$nativeQtCompletionTailMs = [Math]::Max(500, 2 * $IntervalMs)
$nativeQtAuditPath = if ($NoQtAudit) { "" } else { $qtAuditPath }
$samples = @([StartupWindowAudit2]::SampleWindows(
    $process.Id,
    $ExePath,
    [int64[]]$baselineHwnds,
    $DurationMs,
    $IntervalMs,
    [bool]$IncludeAllNewWindows,
    $nativeQtAuditPath,
    $QtPublishTimeoutMs,
    $nativeQtCompletionTailMs
))
$nativeWindowEvents = @([StartupWindowAudit2]::LastWindowEvents)
$nativeWindowEventHookActive = [bool][StartupWindowAudit2]::LastWindowEventHookActive
$nativeWindowEventHookRequestedCount = [int][StartupWindowAudit2]::LastWindowEventHookRequestedCount
$nativeWindowEventHookInstalledCount = [int][StartupWindowAudit2]::LastWindowEventHookInstalledCount
$nativeWindowEventHookError = [string][StartupWindowAudit2]::LastWindowEventHookError
$nativeAuditElapsedMs = [int][StartupWindowAudit2]::LastNativeAuditElapsedMs
$nativeAuditHardDeadlineMs = [int][StartupWindowAudit2]::LastNativeAuditHardDeadlineMs
$nativeQtCompletionTailMs = [int][StartupWindowAudit2]::LastNativeQtCompletionTailMs
$nativeQtEvidenceObserved = [bool][StartupWindowAudit2]::LastNativeQtEvidenceObserved
$nativeQtEvidenceObservedElapsedMs = [int][StartupWindowAudit2]::LastNativeQtEvidenceObservedElapsedMs
$nativeAuditStopReason = [string][StartupWindowAudit2]::LastNativeAuditStopReason
$nativeQtCoverageComplete = [bool](
    $NoQtAudit -or
    (
        $nativeWindowEventHookActive -and
        $nativeQtEvidenceObserved -and
        $nativeAuditStopReason -eq "qt_evidence_published" -and
        $nativeAuditElapsedMs -ge (
            $nativeQtEvidenceObservedElapsedMs + $nativeQtCompletionTailMs
        )
    )
)
$nativeLastWindowEventElapsedMs = $null
if ($nativeWindowEvents.Count -gt 0) {
    $nativeLastWindowEventElapsedMs = [int](
        $nativeWindowEvents |
            Measure-Object -Property elapsed_ms -Maximum
    ).Maximum
}

$qtPublishWaitMs = 0
$earlyQtSamples = @()
if (-not $NoQtAudit) {
    # The Qt clock begins after the source window is fully constructed, while
    # Win32 sampling begins at process launch. On a cold source start the
    # nominal publication margin can therefore expire before the Qt evidence
    # file is complete. Poll for a parseable, flat sample set so native popup
    # HWNDs can be correlated instead of being misclassified as suspicious.
    $qtPublishWaitStarted = [System.Diagnostics.Stopwatch]::StartNew()
    # SampleWindows has already held both the WinEvent hook and the Win32
    # sampler until the atomically published final path appeared. This is only
    # a short parse/visibility grace, not a second unobserved startup window.
    $qtPublishGraceMs = if ($nativeQtEvidenceObserved) { 2000 } else { 1000 }
    $qtPublishDeadline = [DateTime]::UtcNow.AddMilliseconds($qtPublishGraceMs)
    while ([DateTime]::UtcNow -lt $qtPublishDeadline) {
        if (Test-Path -LiteralPath $qtAuditPath) {
            try {
                $parsedEarlyQtSamples = (
                    Get-Content -LiteralPath $qtAuditPath -Raw -Encoding UTF8 |
                        ConvertFrom-Json
                )
                $candidateQtSamples = @()
                foreach ($sample in $parsedEarlyQtSamples) {
                    $candidateQtSamples += $sample
                }
                $invalidQtSamples = @(
                    $candidateQtSamples | Where-Object {
                        $null -eq $_.elapsed_ms -or
                        $_.elapsed_ms -is [System.Array]
                    }
                )
                if (
                    $candidateQtSamples.Count -gt 0 -and
                    $invalidQtSamples.Count -eq 0
                ) {
                    $earlyQtSamples = $candidateQtSamples
                    break
                }
            } catch {
                # The publisher may have created the file but not completed
                # its single JSON write yet. Retry until the bounded deadline.
            }
        } elseif ($process.HasExited) {
            break
        }
        Start-Sleep -Milliseconds 100
    }
    $qtPublishWaitStarted.Stop()
    $qtPublishWaitMs = [int]$qtPublishWaitStarted.ElapsedMilliseconds
}

$qtMainWindowHwnds = New-Object 'System.Collections.Generic.HashSet[long]'
foreach ($qtSample in @($earlyQtSamples)) {
    foreach ($qtTopLevel in @($qtSample.top_levels)) {
        if (
            [bool]$qtTopLevel.is_main_window -and
            [int64]$qtTopLevel.hwnd_int -ne 0
        ) {
            [void]$qtMainWindowHwnds.Add([int64]$qtTopLevel.hwnd_int)
        }
    }
}
$hasExactQtMainWindow = $qtMainWindowHwnds.Count -gt 0
$qtToWin32AuditOffsetMs = Get-QtToWin32AuditOffsetMs -Win32Samples $samples -QtSamples $earlyQtSamples
# Both samplers use IntervalMs. One and a half intervals permits their sampling
# phases plus the main-window alignment quantization, without exempting an HWND
# through a later inactive sample. At the default 10 ms cadence this is 15 ms.
$qtPopupMatchWindowMs = [Math]::Max(
    8,
    [int][Math]::Ceiling(1.5 * [double]$IntervalMs)
)
$legalQtPopupSampleCount = 0
$legalQtPopupHwndsObserved = @()

for ($sampleIndex = 0; $sampleIndex -lt $samples.Count; $sampleIndex += 1) {
    $win32Sample = $samples[$sampleIndex]
    foreach ($record in @($win32Sample.windows)) {
        $nativeRoleMainCandidate = [bool]$record.main_candidate
        $isExactQtMainWindow = (
            $hasExactQtMainWindow -and
            $qtMainWindowHwnds.Contains([int64]$record.hwnd_int)
        )
        $unexpectedFramedQtTopLevel = (
            $hasExactQtMainWindow -and
            $nativeRoleMainCandidate -and
            -not $isExactQtMainWindow -and
            [bool]$record.target_process
        )
        $record | Add-Member -NotePropertyName native_role_main_candidate -NotePropertyValue $nativeRoleMainCandidate -Force
        $record | Add-Member -NotePropertyName qt_main_window -NotePropertyValue $isExactQtMainWindow -Force
        $record | Add-Member -NotePropertyName unexpected_framed_qt_top_level -NotePropertyValue $unexpectedFramedQtTopLevel -Force
        if ($hasExactQtMainWindow) {
            $record.main_candidate = $isExactQtMainWindow
        }
        if ($unexpectedFramedQtTopLevel) {
            $record.suspicious_candidate = $true
            $record.same_pid_suspicious_candidate = $true
        }
        $popupMatch = Get-QtPopupSampleMatch `
            -Win32Sample $win32Sample `
            -PopupHwnd ([int64]$record.hwnd_int) `
            -QtSamples $earlyQtSamples `
            -QtToWin32OffsetMs $qtToWin32AuditOffsetMs `
            -MaxDeltaMs $qtPopupMatchWindowMs
        $isLegalQtPopup = [bool]$popupMatch.legal
        $record | Add-Member -NotePropertyName legal_qt_active_popup -NotePropertyValue $isLegalQtPopup -Force
        $record | Add-Member -NotePropertyName qt_popup_match_delta_ms -NotePropertyValue $popupMatch.delta_ms -Force
        $record | Add-Member -NotePropertyName qt_popup_nearest_elapsed_ms -NotePropertyValue $popupMatch.qt_elapsed_ms -Force
        if ($isLegalQtPopup) {
            $legalQtPopupSampleCount += 1
            $legalQtPopupHwndsObserved += [int64]$record.hwnd_int
            continue
        }
        if ($record.blank_candidate) {
            $blankSeen = $true
        }
        if ($record.suspicious_candidate) {
            $transientSuspiciousSeen = $true
            if ($record.same_pid_suspicious_candidate) {
                $samePidSuspiciousSeen = $true
                $samePidSuspiciousSampleCount += 1
            }
            if (-not $suspiciousLifetimes.ContainsKey($record.hwnd)) {
                $suspiciousLifetimes[$record.hwnd] = [ordered]@{
                    first_ms = [int]$samples[$sampleIndex].elapsed_ms
                    last_ms = [int]$samples[$sampleIndex].elapsed_ms
                    sample_count = 0
                }
            }
            $lifetime = $suspiciousLifetimes[$record.hwnd]
            $lifetime.last_ms = [int]$samples[$sampleIndex].elapsed_ms
            $lifetime.sample_count = [int]$lifetime.sample_count + 1
            if (-not $screenshotHandles.ContainsKey($record.hwnd)) {
                $safeHwnd = $record.hwnd.Replace("0x", "")
                $path = Join-Path $OutputDir ("suspicious-{0:D4}-{1}.png" -f $sampleIndex, $safeHwnd)
                if (Save-WindowScreenshot $record $path) {
                    [void]$suspiciousScreenshots.Add($path)
                }
                $screenshotHandles[$record.hwnd] = $true
            }
        }
    }
}

# WinEvent hooks close the blind interval between EnumWindows polling samples.
# A Qt popup SHOW is exempt when either the in-process Qt sampler observed the
# exact same HWND as QApplication.activePopupWidget(), or the deterministic
# motion audit recorded that exact combo-popup HWND and its bounded synchronous
# show request. The latter closes the same-UI-thread sampling gap without
# exempting a reused HWND outside the request window. Internal observer/helper
# HWNDs are never legal popups. SysShadow is an OS-owned shadow surface, never
# an application blank window.
$qtPopupEventMatchWindowMs = [Math]::Max(
    16,
    [int][Math]::Ceiling(2.5 * [double]$IntervalMs)
)
$nativeShowEvents = @(
    $nativeWindowEvents | Where-Object {
        $_.event_name -eq "SHOW" -or $_.event_name -eq "UNCLOAKED"
    }
)
$motionComboPopupCalibration = Get-MotionComboPopupEventAssignments `
    -NativeShowEvents $nativeShowEvents `
    -QtSamples $earlyQtSamples `
    -ToleranceMs $qtPopupEventMatchWindowMs
$motionComboPopupMatchesByEventIndex = @{}
foreach ($assignment in @($motionComboPopupCalibration.assignments)) {
    $motionComboPopupMatchesByEventIndex[[int]$assignment.event_index] = $assignment
}
$motionComboDeclaredPopupHwnds = New-Object 'System.Collections.Generic.HashSet[long]'
foreach ($declaredPopupHwnd in @($motionComboPopupCalibration.declared_popup_hwnds)) {
    [void]$motionComboDeclaredPopupHwnds.Add([int64]$declaredPopupHwnd)
}
for (
    $nativeShowEventIndex = 0;
    $nativeShowEventIndex -lt $nativeShowEvents.Count;
    $nativeShowEventIndex += 1
) {
    $nativeEvent = $nativeShowEvents[$nativeShowEventIndex]
    $record = $nativeEvent.window
    $targetMetadataLostShow = [bool](
        [bool]$record.target_process -and
        [bool]$record.metadata_lost
    )
    if ($targetMetadataLostShow) {
        $nativeMetadataLostTargetShowCount += 1
    }
    $nativeEvent | Add-Member -NotePropertyName metadata_lost_target_show -NotePropertyValue $targetMetadataLostShow -Force
    $nativeEvent | Add-Member -NotePropertyName metadata_lost_explained_by_exact_motion_action -NotePropertyValue $false -Force
    $nativeEvent | Add-Member -NotePropertyName unclassified_target_show -NotePropertyValue $false -Force
    $nativeRoleMainCandidate = [bool]$record.main_candidate
    $isExactQtMainWindow = (
        $hasExactQtMainWindow -and
        $qtMainWindowHwnds.Contains([int64]$record.hwnd_int)
    )
    $unexpectedFramedQtTopLevel = (
        $hasExactQtMainWindow -and
        $nativeRoleMainCandidate -and
        -not $isExactQtMainWindow -and
        [bool]$record.target_process
    )
    $record | Add-Member -NotePropertyName native_role_main_candidate -NotePropertyValue $nativeRoleMainCandidate -Force
    $record | Add-Member -NotePropertyName qt_main_window -NotePropertyValue $isExactQtMainWindow -Force
    $record | Add-Member -NotePropertyName unexpected_framed_qt_top_level -NotePropertyValue $unexpectedFramedQtTopLevel -Force
    if ($hasExactQtMainWindow) {
        $record.main_candidate = $isExactQtMainWindow
    }
    if ($unexpectedFramedQtTopLevel) {
        $record.suspicious_candidate = $true
        $record.same_pid_suspicious_candidate = $true
    }
    if ([bool]$record.system_shadow) {
        $nativeEvent | Add-Member -NotePropertyName legal_system_shadow -NotePropertyValue $true -Force
        $nativeSystemShadowShowCount += 1
        continue
    }
    $samePidQtPopupSurface = [bool](
        [bool]$record.target_process -and
        -not [bool]$record.main_candidate -and
        [string]$record.class_name -like "Qt*QWindowPopup*"
    )
    $motionComboEventRequiresAssignment = [bool](
        (
            $samePidQtPopupSurface -or
            (
                $targetMetadataLostShow -and
                $motionComboDeclaredPopupHwnds.Contains([int64]$record.hwnd_int)
            )
        ) -and
        $motionComboPopupCalibration.action_count -ge 2 -and
        $motionComboDeclaredPopupHwnds.Contains([int64]$record.hwnd_int)
    )
    $activePopupMatch = if ([bool]$record.internal_helper_candidate) {
        [pscustomobject]@{
            legal = $false
            delta_ms = $null
            qt_elapsed_ms = $null
        }
    } else {
        Get-QtPopupEventMatch `
            -Win32ElapsedMs ([int]$nativeEvent.elapsed_ms) `
            -PopupHwnd ([int64]$record.hwnd_int) `
            -QtSamples $earlyQtSamples `
            -QtToWin32OffsetMs $qtToWin32AuditOffsetMs `
            -MaxDeltaMs $qtPopupEventMatchWindowMs
    }
    $motionComboMatch = if (
        -not [bool]$record.internal_helper_candidate -and
        $motionComboPopupMatchesByEventIndex.ContainsKey($nativeShowEventIndex)
    ) {
        $motionComboPopupMatchesByEventIndex[$nativeShowEventIndex]
    } else {
        [pscustomobject]@{
            legal = $false
            phase = $null
            kind = $null
            mapped_qt_elapsed_ms = $null
            request_elapsed_ms = $null
            duration_ms = $null
            calibrated_offset_ms = $null
            pair_offset_ms = $null
            offset_deviation_ms = $null
            window_start_ms = $null
            window_end_ms = $null
        }
    }
    # Once a HWND is part of the declared motion sequence, its SHOW events must
    # consume distinct declared actions. activePopupWidget remains a fallback
    # for all other popup HWNDs, but cannot hide a duplicate/late motion SHOW.
    $isLegalQtActivePopup = [bool](
        [bool]$activePopupMatch.legal -and
        -not $targetMetadataLostShow -and
        -not $motionComboEventRequiresAssignment
    )
    $isLegalMotionComboPopup = [bool](
        $null -ne $motionComboMatch.event_index -and
        $null -ne $motionComboMatch.popup_hwnd_int -and
        [int64]$motionComboMatch.popup_hwnd_int -eq [int64]$record.hwnd_int
    )
    $metadataLostAssessment = Get-NativeMetadataLostShowAssessment `
        -NativeEvent $nativeEvent `
        -NativeShowEventIndex $nativeShowEventIndex `
        -MotionComboMatch $motionComboMatch
    $unclassifiedTargetShow = [bool]$metadataLostAssessment.unclassified_target_show
    $nativeEvent.metadata_lost_explained_by_exact_motion_action = [bool]$metadataLostAssessment.exact_motion_action_explained
    $nativeEvent.unclassified_target_show = $unclassifiedTargetShow
    if ($unclassifiedTargetShow) {
        $nativeUnclassifiedTargetShowCount += 1
        $record.suspicious_candidate = $true
        $record.same_pid_suspicious_candidate = $true
    }
    $isLegalQtPopup = $isLegalQtActivePopup -or $isLegalMotionComboPopup
    $nativeEvent | Add-Member -NotePropertyName legal_qt_active_popup -NotePropertyValue $isLegalQtActivePopup -Force
    $nativeEvent | Add-Member -NotePropertyName legal_motion_combo_popup -NotePropertyValue $isLegalMotionComboPopup -Force
    $nativeEvent | Add-Member -NotePropertyName motion_combo_assignment_required -NotePropertyValue $motionComboEventRequiresAssignment -Force
    $nativeEvent | Add-Member -NotePropertyName legal_qt_popup -NotePropertyValue $isLegalQtPopup -Force
    $nativeEvent | Add-Member -NotePropertyName qt_popup_match_delta_ms -NotePropertyValue $activePopupMatch.delta_ms -Force
    $nativeEvent | Add-Member -NotePropertyName qt_popup_nearest_elapsed_ms -NotePropertyValue $activePopupMatch.qt_elapsed_ms -Force
    $nativeEvent | Add-Member -NotePropertyName motion_combo_popup_phase -NotePropertyValue $motionComboMatch.phase -Force
    $nativeEvent | Add-Member -NotePropertyName motion_combo_popup_kind -NotePropertyValue $motionComboMatch.kind -Force
    $nativeEvent | Add-Member -NotePropertyName motion_combo_popup_request_elapsed_ms -NotePropertyValue $motionComboMatch.request_elapsed_ms -Force
    $nativeEvent | Add-Member -NotePropertyName motion_combo_popup_duration_ms -NotePropertyValue $motionComboMatch.duration_ms -Force
    $nativeEvent | Add-Member -NotePropertyName motion_combo_popup_calibrated_offset_ms -NotePropertyValue $motionComboMatch.calibrated_offset_ms -Force
    $nativeEvent | Add-Member -NotePropertyName motion_combo_popup_pair_offset_ms -NotePropertyValue $motionComboMatch.pair_offset_ms -Force
    $nativeEvent | Add-Member -NotePropertyName motion_combo_popup_offset_deviation_ms -NotePropertyValue $motionComboMatch.offset_deviation_ms -Force
    $nativeEvent | Add-Member -NotePropertyName motion_combo_popup_mapped_qt_elapsed_ms -NotePropertyValue $motionComboMatch.mapped_qt_elapsed_ms -Force
    $nativeEvent | Add-Member -NotePropertyName motion_combo_popup_window_start_ms -NotePropertyValue $motionComboMatch.window_start_ms -Force
    $nativeEvent | Add-Member -NotePropertyName motion_combo_popup_window_end_ms -NotePropertyValue $motionComboMatch.window_end_ms -Force
    if ($isLegalQtPopup) {
        $nativeLegalQtPopupShowCount += 1
        if ($isLegalMotionComboPopup) {
            $nativeMotionComboPopupShowCount += 1
        }
        continue
    }
    # Popup HWNDs are reusable and their geometry can grow beyond the generic
    # small-window heuristic (the cipher list is taller than 1000 px). A SHOW
    # outside both exact Qt evidence windows must therefore fail closed based
    # on its native popup role, regardless of current size.
    $unmatchedSamePidQtPopupShow = $samePidQtPopupSurface
    $nativeEvent | Add-Member -NotePropertyName unmatched_same_pid_qt_popup_show -NotePropertyValue $unmatchedSamePidQtPopupShow -Force
    if ($unmatchedSamePidQtPopupShow) {
        $nativeUnmatchedQtPopupShowCount += 1
        $record.suspicious_candidate = $true
        $record.same_pid_suspicious_candidate = $true
    }
    if ([bool]$record.blank_candidate) {
        $blankSeen = $true
    }
    if ([bool]$record.internal_helper_candidate) {
        $nativeInternalHelperShowCount += 1
    }
    if ([bool]$record.suspicious_candidate) {
        $nativeSuspiciousShowSeen = $true
        $nativeSuspiciousShowCount += 1
        $transientSuspiciousSeen = $true
        if ([bool]$record.same_pid_suspicious_candidate) {
            $samePidSuspiciousSeen = $true
        }
    }
}

$SuspiciousPersistenceMs = [Math]::Max(1, $SuspiciousPersistenceMs)
foreach ($lifetime in $suspiciousLifetimes.Values) {
    $observedSpan = [Math]::Max(0, [int]$lifetime.last_ms - [int]$lifetime.first_ms)
    $maxSuspiciousObservedSpanMs = [Math]::Max($maxSuspiciousObservedSpanMs, $observedSpan)
    if ($observedSpan -ge $SuspiciousPersistenceMs) {
        $persistentSuspiciousSeen = $true
    }
}
$suspiciousSeen = (
    $samePidSuspiciousSeen -or
    $persistentSuspiciousSeen -or
    $nativeSuspiciousShowSeen
)
$legalQtPopupHandleCount = @(
    $legalQtPopupHwndsObserved | Sort-Object -Unique
).Count

if ($CaptureScreenshots) {
    $lastWindows = @()
    if ($samples.Count -gt 0) {
        $lastWindows = @($samples[$samples.Count - 1].windows)
    }
    $main = $lastWindows | Where-Object { $_.main_candidate } | Select-Object -First 1
    if ($main) {
        [void](Save-WindowScreenshot $main (Join-Path $OutputDir "startup-final-window.png"))
    }
}

$samples | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $win32AuditPath -Encoding UTF8
ConvertTo-Json -InputObject @($nativeWindowEvents) -Depth 8 |
    Set-Content -LiteralPath $win32EventAuditPath -Encoding UTF8

if (-not $process.HasExited) {
    # Source-mode construction is materially slower than the packaged binary
    # on some Windows hosts. Preserve the Qt evidence publisher instead of
    # killing a healthy audit between window construction and its final sample.
    $process.WaitForExit(15000) | Out-Null
}
if (-not $process.HasExited) {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
}
if (-not $RootPidOnlyCleanup) {
    Stop-TargetAppProcesses $ExePath
}

$processStderrText = ""
if (Test-Path -LiteralPath $script:ProcessStderrPath) {
    $processStderrText = Get-Content -LiteralPath $script:ProcessStderrPath -Raw -ErrorAction SilentlyContinue
}
$deletedWidgetErrorSeen = [bool](
    $processStderrText -match (
        "Internal C\+\+ object.*already deleted|" +
        "wrapped C/C\+\+ object.*deleted|" +
        "Shiboken.*deleted"
    )
)

$parsedQtSamples = $null
if (-not $NoQtAudit) {
    if (-not (Test-Path -LiteralPath $qtAuditPath)) {
        throw "Qt startup audit did not publish its evidence file: $qtAuditPath"
    }
    try {
        $parsedQtSamples = Get-Content -LiteralPath $qtAuditPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        throw "Qt startup audit evidence is not valid JSON: $qtAuditPath. $($_.Exception.Message)"
    }
}

$feedbackWithin250Ms = $null
$startupActionRequestedElapsedMs = $null
$startupFeedbackLatencyMs = $null
$phasePersisted = $null
$falseNoSourceSeen = $false
$motionSequenceSeen = $null
$motionSettled = $null
$motionAuditSteps = @()
$motionSettledAfterMs = $null
$motionPolicyEnabled = $null
$busyVisualSeen = $null
$busySpinnerSeen = $null
$sidebarMotionFrameCount = 0
$sidebarIntegratedWithTarget = $null
$sidebarIntermediateSeen = $null
$sidebarReducedSnapped = $null
if ($ExerciseStartAction -and -not $NoQtAudit) {
    $qtSamples = @()
    foreach ($sample in $parsedQtSamples) {
        $qtSamples += $sample
    }
    $actionSamples = @(
        $qtSamples | Where-Object {
            $null -ne $_.startup.action_requested_elapsed_ms
        }
    )
    if ($actionSamples.Count -gt 0) {
        $startupActionRequestedElapsedMs = [int](
            $actionSamples[0].startup.action_requested_elapsed_ms
        )
        $feedbackSamples = @(
            $qtSamples | Where-Object {
                [int]$_.elapsed_ms -ge $startupActionRequestedElapsedMs -and
                $_.startup.notice_visible -and
                -not [string]::IsNullOrWhiteSpace([string]$_.startup.notice_text)
            }
        )
        if ($feedbackSamples.Count -gt 0) {
            $startupFeedbackLatencyMs = [int]$feedbackSamples[0].elapsed_ms -
                $startupActionRequestedElapsedMs
            $feedbackWithin250Ms = $startupFeedbackLatencyMs -le 250
        } else {
            $feedbackWithin250Ms = $false
        }
    } else {
        $feedbackWithin250Ms = $false
    }
    $settledSamples = @($qtSamples | Where-Object { $_.elapsed_ms -ge 1000 })
    $phasePersisted = @(
        $settledSamples | Where-Object {
            ($_.startup.phase -eq "collecting" -or $_.startup.phase -eq "core_standby" -or $_.startup.phase -eq "running") -and
            ($_.startup.phase -eq "running" -or $_.startup.notice_visible)
        }
    ).Count -gt 0
    $falseNoSourceSeen = @(
        $qtSamples | Where-Object {
            # Regex escapes for the zh_CN notice: Windows PowerShell 5.1 reads a
            # BOM-less script in the system code page, so this file stays ASCII.
            [string]$_.startup.notice_text -match "\u6ca1\u6709\u53ef\u7528\u6765\u6e90|No available sources"
        }
    ).Count -gt 0
}

if ($ExerciseMotionAction -and -not $NoQtAudit) {
    $qtSamples = @()
    foreach ($sample in $parsedQtSamples) {
        $qtSamples += $sample
    }
    $visitedPages = @(
        $qtSamples |
            ForEach-Object {
                [string]$_.motion.page
                foreach ($page in @($_.motion.visited_pages)) {
                    [string]$page
                }
            } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            Sort-Object -Unique
    )
    $motionAuditSteps = @(
        $qtSamples |
            ForEach-Object {
                foreach ($step in @($_.motion.audit_steps)) {
                    [string]$step
                }
            } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            Sort-Object -Unique
    )
    $requiredMotionSteps = @(
        "sidebar-selection",
        "layer-detail",
        "layer-browse",
        "gateway-detail",
        "gateway-browse",
        "button-hover-press",
        "combo-popup-small",
        "combo-popup-ss-cipher",
        "log-clear-undo",
        "busy-visual"
    )
    $motionPolicyEnabled = @(
        $qtSamples | Where-Object { [bool]$_.motion.motion_enabled }
    ).Count -gt 0
    $sidebarMotionFrames = @(
        $qtSamples |
            ForEach-Object {
                foreach ($frame in @($_.motion.sidebar_motion_frames)) {
                    $frame
                }
            } |
            Where-Object {
                $null -ne $_ -and
                @($_.geometry).Count -eq 4
            }
    )
    # The explicit in-app checkpoints are useful for reduced-motion mode, but
    # a busy Windows event loop can deliver a checkpoint before the property
    # animation advances. Include the audit sampler's live indicator state so
    # an observed intermediate frame remains the source of truth.
    $sidebarLiveFrames = @(
        $qtSamples |
            ForEach-Object { $_.motion.sidebar_motion } |
            Where-Object {
                $null -ne $_ -and
                @($_.geometry).Count -eq 4
            }
    )
    $sidebarObservedFrames = @($sidebarMotionFrames) + @($sidebarLiveFrames)
    $sidebarIntegratedFrames = @(
        $sidebarObservedFrames |
            Where-Object {
                [int]$_.geometry[2] -gt 0 -and
                [int]$_.geometry[3] -gt 0 -and
                [int]$_.target_geometry[2] -gt 0 -and
                [int]$_.target_geometry[3] -gt 0
            }
    )
    $sidebarMotionFrameCount = $sidebarIntegratedFrames.Count
    $sidebarIntegratedWithTarget = (
        $sidebarMotionFrameCount -gt 0 -and
        @(
            $sidebarIntegratedFrames |
                Where-Object { -not [bool]$_.integrated_with_target }
        ).Count -eq 0
    )
    $sidebarIntermediateSeen = @(
        $sidebarObservedFrames |
            Where-Object {
                [double]$_.reveal_progress -gt 0.0 -and
                [double]$_.reveal_progress -lt 1.0 -and
                [bool]$_.animation_running
            }
    ).Count -gt 0
    $sidebarReducedSnapped = @(
        $sidebarObservedFrames |
            Where-Object {
                [double]$_.reveal_progress -lt 0.999 -or
                [bool]$_.animation_running
            }
    ).Count -eq 0
    if (
        $sidebarIntegratedWithTarget -and
        (
            ($motionPolicyEnabled -and $sidebarIntermediateSeen) -or
            (-not $motionPolicyEnabled -and $sidebarReducedSnapped)
        ) -and
        $motionAuditSteps -notcontains "sidebar-selection"
    ) {
        $motionAuditSteps += "sidebar-selection"
    }
    $busyVisualSeen = @(
        $qtSamples | Where-Object {
            [int]$_.motion.busy_visual_count -gt 0
        }
    ).Count -gt 0
    $busySpinnerSeen = @(
        $qtSamples | Where-Object {
            [int]$_.motion.busy_spinner_count -gt 0
        }
    ).Count -gt 0
    $motionSequenceSeen = (
        $visitedPages.Count -ge 3 -and
        @($requiredMotionSteps | Where-Object {
            $motionAuditSteps -notcontains $_
        }).Count -eq 0 -and
        $busyVisualSeen -and
        (-not $motionPolicyEnabled -or $busySpinnerSeen) -and
        $sidebarIntegratedWithTarget -and
        (
            ($motionPolicyEnabled -and $sidebarIntermediateSeen) -or
            (-not $motionPolicyEnabled -and $sidebarReducedSnapped)
        )
    )
    $motionSettledAfterMs = [Math]::Max(2000, $qtAuditDurationMs - 300)
    $settledMotionSamples = @(
        $qtSamples | Where-Object {
            $_.elapsed_ms -ge $motionSettledAfterMs
        }
    )
    $dirtySettledMotionSamples = @(
        $settledMotionSamples | Where-Object {
            [int]$_.motion.active_animation_count -ne 0 -or
            [int]$_.motion.busy_spinner_count -ne 0 -or
            @($_.motion.graphics_effect_targets).Count -ne 0
        }
    )
    $motionSettled = (
        $settledMotionSamples.Count -gt 0 -and
        $dirtySettledMotionSamples.Count -eq 0
    )
}

@(
    "exe=$ExePath"
    "arguments=$($ArgumentList -join ' ')"
    "pid=$($process.Id)"
    "duration_ms=$DurationMs"
    "qt_duration_ms=$(if ($NoQtAudit) { '' } else { $qtAuditDurationMs })"
    "qt_publish_wait_ms=$qtPublishWaitMs"
    "qt_publish_timeout_ms=$QtPublishTimeoutMs"
    "interval_ms=$IntervalMs"
    "exact_qt_main_window_handles=$($qtMainWindowHwnds.Count)"
    "native_audit_elapsed_ms=$nativeAuditElapsedMs"
    "native_audit_hard_deadline_ms=$nativeAuditHardDeadlineMs"
    "native_audit_stop_reason=$nativeAuditStopReason"
    "native_qt_evidence_observed=$nativeQtEvidenceObserved"
    "native_qt_evidence_observed_elapsed_ms=$nativeQtEvidenceObservedElapsedMs"
    "native_qt_completion_tail_ms=$nativeQtCompletionTailMs"
    "native_qt_coverage_complete=$nativeQtCoverageComplete"
    "native_last_window_event_elapsed_ms=$nativeLastWindowEventElapsedMs"
    "native_event_hook_active=$nativeWindowEventHookActive"
    "native_event_hook_requested_count=$nativeWindowEventHookRequestedCount"
    "native_event_hook_installed_count=$nativeWindowEventHookInstalledCount"
    "native_event_hook_error=$nativeWindowEventHookError"
    "native_window_event_count=$($nativeWindowEvents.Count)"
    "native_show_event_count=$($nativeShowEvents.Count)"
    "native_suspicious_show_seen=$nativeSuspiciousShowSeen"
    "native_suspicious_show_count=$nativeSuspiciousShowCount"
    "native_internal_helper_show_count=$nativeInternalHelperShowCount"
    "native_legal_qt_popup_show_count=$nativeLegalQtPopupShowCount"
    "native_motion_combo_popup_show_count=$nativeMotionComboPopupShowCount"
    "native_system_shadow_show_count=$nativeSystemShadowShowCount"
    "native_unmatched_qt_popup_show_count=$nativeUnmatchedQtPopupShowCount"
    "native_metadata_lost_target_show_count=$nativeMetadataLostTargetShowCount"
    "native_unclassified_target_show_count=$nativeUnclassifiedTargetShowCount"
    "motion_combo_clock_calibrated=$([bool]$motionComboPopupCalibration.calibrated)"
    "motion_combo_clock_offset_ms=$($motionComboPopupCalibration.calibrated_offset_ms)"
    "motion_combo_clock_action_count=$($motionComboPopupCalibration.action_count)"
    "motion_combo_clock_declared_handle_count=$($motionComboDeclaredPopupHwnds.Count)"
    "motion_combo_clock_eligible_event_count=$($motionComboPopupCalibration.eligible_event_count)"
    "motion_combo_clock_consensus_match_count=$($motionComboPopupCalibration.consensus_match_count)"
    "motion_combo_clock_max_offset_deviation_ms=$($motionComboPopupCalibration.max_offset_deviation_ms)"
    "qt_popup_event_match_window_ms=$qtPopupEventMatchWindowMs"
    "blank_candidate_seen=$blankSeen"
    "suspicious_candidate_seen=$suspiciousSeen"
    "transient_suspicious_candidate_seen=$transientSuspiciousSeen"
    "same_pid_suspicious_candidate_seen=$samePidSuspiciousSeen"
    "same_pid_suspicious_sample_count=$samePidSuspiciousSampleCount"
    "legal_qt_popup_handles=$legalQtPopupHandleCount"
    "legal_qt_popup_samples=$legalQtPopupSampleCount"
    "qt_popup_clock_offset_ms=$qtToWin32AuditOffsetMs"
    "qt_popup_match_window_ms=$qtPopupMatchWindowMs"
    "persistent_suspicious_candidate_seen=$persistentSuspiciousSeen"
    "suspicious_persistence_ms=$SuspiciousPersistenceMs"
    "max_suspicious_observed_span_ms=$maxSuspiciousObservedSpanMs"
    "suspicious_screenshots=$($suspiciousScreenshots.Count)"
    "no_qt_audit=$([bool]$NoQtAudit)"
    "include_all_new_windows=$([bool]$IncludeAllNewWindows)"
    "exercise_start_action=$([bool]$ExerciseStartAction)"
    "exercise_motion_action=$([bool]$ExerciseMotionAction)"
    "skip_motion_contract=$([bool]$SkipMotionContract)"
    "feedback_within_250ms=$feedbackWithin250Ms"
    "startup_action_requested_elapsed_ms=$startupActionRequestedElapsedMs"
    "startup_feedback_latency_ms=$startupFeedbackLatencyMs"
    "startup_phase_persisted=$phasePersisted"
    "false_no_source_seen=$falseNoSourceSeen"
    "motion_sequence_seen=$motionSequenceSeen"
    "motion_audit_steps=$($motionAuditSteps -join ',')"
    "motion_policy_enabled=$motionPolicyEnabled"
    "busy_visual_seen=$busyVisualSeen"
    "busy_spinner_seen=$busySpinnerSeen"
    "sidebar_motion_frame_count=$sidebarMotionFrameCount"
    "sidebar_integrated_with_target=$sidebarIntegratedWithTarget"
    "sidebar_intermediate_seen=$sidebarIntermediateSeen"
    "sidebar_reduced_snapped=$sidebarReducedSnapped"
    "motion_settled_after_ms=$motionSettledAfterMs"
    "motion_settled=$motionSettled"
    "deleted_widget_error_seen=$deletedWidgetErrorSeen"
    "process_stdout=$script:ProcessStdoutPath"
    "process_stderr=$script:ProcessStderrPath"
    "qt_audit=$qtAuditPath"
    "win32_audit=$win32AuditPath"
    "win32_event_audit=$win32EventAuditPath"
) | Set-Content -LiteralPath $summaryPath -Encoding UTF8

Get-Content -LiteralPath $summaryPath

$strictWindowAudit = [bool](
    $FailOnBlankCandidate -or
    $FailOnSuspiciousCandidate -or
    $ExerciseStartAction -or
    $ExerciseMotionAction
)
if ($strictWindowAudit -and -not $nativeWindowEventHookActive) {
    exit 10
}
if ($strictWindowAudit -and -not $nativeQtCoverageComplete) {
    exit 11
}
if ($FailOnBlankCandidate -and $blankSeen) {
    exit 2
}
if (
    $FailOnSuspiciousCandidate -and
    ($suspiciousSeen -or $nativeUnclassifiedTargetShowCount -gt 0)
) {
    exit 3
}
if ($ExerciseStartAction -and $feedbackWithin250Ms -ne $true) {
    exit 4
}
if ($ExerciseStartAction -and $phasePersisted -ne $true) {
    exit 5
}
if ($ExerciseStartAction -and $falseNoSourceSeen) {
    exit 6
}
if ($ExerciseMotionAction -and -not $SkipMotionContract -and $motionSequenceSeen -ne $true) {
    exit 7
}
if ($ExerciseMotionAction -and -not $SkipMotionContract -and $motionSettled -ne $true) {
    exit 8
}
if ($ExerciseMotionAction -and $deletedWidgetErrorSeen) {
    exit 9
}
