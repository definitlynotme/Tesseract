[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')]
    [string] $Repository,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-rc\.(?:0|[1-9]\d*))?$')]
    [string] $Tag,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string] $ExpectedTagObjectSha,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string] $ExpectedCommit,

    [string] $ApiBaseUrl = $env:GITHUB_API_URL
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($ApiBaseUrl)) {
    throw 'The GitHub API base URL is required.'
}
if ([string]::IsNullOrWhiteSpace($env:GH_TOKEN)) {
    throw 'GH_TOKEN is required to revalidate the release tag.'
}

$expectedTagObject = $ExpectedTagObjectSha.ToLowerInvariant()
$expectedCommitSha = $ExpectedCommit.ToLowerInvariant()
$expectedRef = "refs/tags/$Tag"
$apiRoot = $ApiBaseUrl.TrimEnd('/')
$headers = @{
    Accept = 'application/vnd.github+json'
    Authorization = "Bearer $env:GH_TOKEN"
    'X-GitHub-Api-Version' = '2022-11-28'
}

$escapedTag = [Uri]::EscapeDataString($Tag)
$refUri = "$apiRoot/repos/$Repository/git/ref/tags/$escapedTag"
$tagRef = Invoke-RestMethod -Method Get -Uri $refUri -Headers $headers
if ([string] $tagRef.ref -cne $expectedRef) {
    throw 'GitHub returned a different release tag reference.'
}
if ([string] $tagRef.object.type -cne 'tag') {
    throw 'The release tag is no longer an annotated tag object.'
}
$currentTagObjectText = [string] $tagRef.object.sha
if ($currentTagObjectText -notmatch '^[0-9a-fA-F]{40}$') {
    throw 'GitHub returned an invalid release tag-object SHA.'
}
$currentTagObject = $currentTagObjectText.ToLowerInvariant()
if ($currentTagObject -cne $expectedTagObject) {
    throw 'The release tag object changed after the build gate.'
}

$tagObjectUri = "$apiRoot/repos/$Repository/git/tags/$expectedTagObject"
$tagObject = Invoke-RestMethod -Method Get -Uri $tagObjectUri -Headers $headers
$tagObjectShaText = [string] $tagObject.sha
if (
    $tagObjectShaText -notmatch '^[0-9a-fA-F]{40}$' -or
    $tagObjectShaText.ToLowerInvariant() -cne $expectedTagObject
) {
    throw 'GitHub returned a different annotated tag object.'
}
if ([string] $tagObject.tag -cne $Tag) {
    throw 'The annotated tag name changed after the build gate.'
}
if ([string] $tagObject.object.type -cne 'commit') {
    throw 'The annotated release tag no longer directly targets a commit.'
}
$tagTargetText = [string] $tagObject.object.sha
if (
    $tagTargetText -notmatch '^[0-9a-fA-F]{40}$' -or
    $tagTargetText.ToLowerInvariant() -cne $expectedCommitSha
) {
    throw 'The annotated release tag target changed after the build gate.'
}
if (
    $tagObject.verification.verified -ne $true -or
    [string] $tagObject.verification.reason -cne 'valid'
) {
    throw 'GitHub no longer verifies the annotated release tag signature as valid.'
}
if (
    [string]::IsNullOrWhiteSpace([string] $tagObject.verification.signature) -or
    [string]::IsNullOrWhiteSpace([string] $tagObject.verification.payload)
) {
    throw 'GitHub returned incomplete annotated-tag signature evidence.'
}

function Invoke-ReleaseGitCapture {
    param(
        [Parameter(Mandatory = $true)]
        [string[]] $Arguments
    )

    $capturedExit = -1
    $capturedLines = @()
    $savedErrorActionPreference = $ErrorActionPreference
    try {
        # Windows PowerShell 5 promotes native stderr to an ErrorRecord under
        # Stop. Preserve the native exit code before restoring the strict mode.
        $ErrorActionPreference = 'Continue'
        $capturedLines = @(& git @Arguments 2>$null)
        $capturedExit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $savedErrorActionPreference
    }
    return [PSCustomObject] @{
        ExitCode = [int] $capturedExit
        Lines = [object[]] @($capturedLines)
    }
}

$localTagResult = Invoke-ReleaseGitCapture -Arguments @(
    'rev-parse',
    '--verify',
    $expectedRef
)
$localTagLines = @($localTagResult.Lines)
if (
    $localTagResult.ExitCode -ne 0 -or
    $localTagLines.Count -ne 1 -or
    [string] $localTagLines[0] -notmatch '^[0-9a-fA-F]{40}$'
) {
    throw 'Could not resolve the exact local release tag object.'
}
$localTagObject = ([string] $localTagLines[0]).ToLowerInvariant()
if ($localTagObject -cne $expectedTagObject) {
    throw 'The checked-out release tag object changed after the build gate.'
}

$localTypeResult = Invoke-ReleaseGitCapture -Arguments @(
    'cat-file',
    '-t',
    $localTagObject
)
$localTypeLines = @($localTypeResult.Lines)
if (
    $localTypeResult.ExitCode -ne 0 -or
    $localTypeLines.Count -ne 1 -or
    [string] $localTypeLines[0] -cne 'tag'
) {
    throw 'The checked-out release ref is not an annotated tag object.'
}

$localPayloadResult = Invoke-ReleaseGitCapture -Arguments @(
    'cat-file',
    '-p',
    $localTagObject
)
$localPayload = @($localPayloadResult.Lines)
if ($localPayloadResult.ExitCode -ne 0 -or $localPayload.Count -lt 4) {
    throw 'Could not read the checked-out annotated tag payload.'
}
if ([string] $localPayload[0] -cne "object $expectedCommitSha") {
    throw 'The checked-out annotated tag target changed after the build gate.'
}
if ([string] $localPayload[1] -cne 'type commit') {
    throw 'The checked-out annotated tag no longer directly targets a commit.'
}
if ([string] $localPayload[2] -cne "tag $Tag") {
    throw 'The checked-out annotated tag name changed after the build gate.'
}

$headResult = Invoke-ReleaseGitCapture -Arguments @(
    'rev-parse',
    '--verify',
    'HEAD^{commit}'
)
$headLines = @($headResult.Lines)
if (
    $headResult.ExitCode -ne 0 -or
    $headLines.Count -ne 1 -or
    [string] $headLines[0] -notmatch '^[0-9a-fA-F]{40}$'
) {
    throw 'Could not resolve the checked-out release commit.'
}
$localHead = ([string] $headLines[0]).ToLowerInvariant()
if ($localHead -cne $expectedCommitSha) {
    throw 'The checked-out release commit changed after the build gate.'
}

# Sandwich the local checks between two authenticated reads. A replacement
# that races the first API read must not be accepted merely because checkout
# still contains the original object.
$finalTagRef = Invoke-RestMethod -Method Get -Uri $refUri -Headers $headers
$finalTagObjectText = [string] $finalTagRef.object.sha
if (
    [string] $finalTagRef.ref -cne $expectedRef -or
    [string] $finalTagRef.object.type -cne 'tag' -or
    $finalTagObjectText -notmatch '^[0-9a-fA-F]{40}$' -or
    $finalTagObjectText.ToLowerInvariant() -cne $expectedTagObject
) {
    throw 'The release tag changed during identity revalidation.'
}

Write-Output "RELEASE_TAG_IDENTITY_OK tag=$Tag object=$expectedTagObject commit=$expectedCommitSha"
