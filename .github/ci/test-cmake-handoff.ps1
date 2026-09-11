# Cheap cross-runner identity/transport proof. Configure with run-release Prepare
# first. This helper never configures, compiles, downloads, or restores a real SDK.
[CmdletBinding()]
param(
    [ValidateSet('Publish', 'Verify')][string] $Operation,
    [string] $PrepareReport,
    [string] $ProductionKey,
    [string] $ProofDirectory,
    [string] $OutputReport
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$handoffRepository = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $handoffRepository 'build_fast_cache.ps1')

function Write-HandoffNewFile([string] $Path, [byte[]] $Bytes) {
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try { $stream.Write($Bytes, 0, $Bytes.Length) }
    finally { $stream.Dispose() }
}

function Read-HandoffJson([string] $Path) {
    Assert-FastPathWithin -Path $Path -Root $handoffRepository
    $file = Get-Item -LiteralPath $Path
    if ($file.PSIsContainer -or $file.Length -gt 2MB) { throw "Invalid/oversized handoff JSON: $Path" }
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable)
}

function Add-HandoffLeaves($Value, [string] $Path, [Collections.IDictionary] $Leaves) {
    if ($Value -is [Collections.IDictionary]) {
        $Leaves[$Path + ':type'] = 'object'
        foreach ($key in $Value.Keys) { Add-HandoffLeaves $Value[$key] ($Path + '.' + $key) $Leaves }
    }
    elseif ($Value -is [array]) {
        $Leaves[$Path + ':type'] = 'array'
        $Leaves[$Path + ':length'] = $Value.Count
        for ($index = 0; $index -lt $Value.Count; $index++) {
            Add-HandoffLeaves $Value[$index] ($Path + '[' + $index + ']') $Leaves
        }
    }
    else { $Leaves[$Path] = ConvertTo-Json -InputObject $Value -Depth 20 -Compress }
}

function Compare-HandoffInputs($Producer, $Consumer) {
    $before = @{}; $after = @{}
    Add-HandoffLeaves $Producer '$' $before
    Add-HandoffLeaves $Consumer '$' $after
    $keys = @(@($before.Keys) + @($after.Keys) | Sort-Object -Unique)
    foreach ($key in $keys) {
        if (-not $before.ContainsKey($key) -or -not $after.ContainsKey($key) -or $before[$key] -cne $after[$key]) {
            [ordered]@{ field = $key; producer_exists = $before.ContainsKey($key); consumer_exists = $after.ContainsKey($key)
                producer = $before[$key]; consumer = $after[$key] }
        }
    }
}

function Get-HandoffPreparedIdentity([string] $Path, [string] $Key) {
    $inputData = Read-HandoffJson $Path
    if ($inputData.schema -ne 1 -or $inputData.profile -ne 'lean-release' -or $inputData.preset -ne 'lean-release') {
        throw 'Handoff requires a schema-1 production lean-release Prepare input'
    }
    $canonical = ConvertTo-FastCanonicalJson $inputData
    $fingerprint = Get-FastTextSha256 $canonical
    if ($Key -cne ('release-prefix-v1-' + $fingerprint)) {
        throw 'Prepare output key does not match canonical production inputs'
    }
    $cachePath = Join-Path $inputData.dependencyBuildDirectory 'CMakeCache.txt'
    Assert-FastPathWithin -Path $cachePath -Root $handoffRepository
    $cache = Read-FastCMakeCache $cachePath
    $configuredCommand = [string] $cache['CMAKE_COMMAND']
    if (-not $configuredCommand -or (ConvertTo-FastNormalizedPath $configuredCommand) -cne $inputData.tools.cmake.path) {
        throw 'Production CMake identity is not configured CMAKE_COMMAND; bootstrap the native bin, not a Python launcher'
    }
    $actual = Get-FastToolRecord -Path $configuredCommand -Version $inputData.tools.cmake.version
    if ((ConvertTo-FastCanonicalJson $actual) -cne (ConvertTo-FastCanonicalJson $inputData.tools.cmake)) {
        throw 'Configured native CMake changed since Prepare; refuse stale tool identity'
    }
    return [pscustomobject]@{ Input = $inputData; Canonical = $canonical; Fingerprint = $fingerprint; Key = $Key; NativeCMake = $actual }
}

function Invoke-CMakeHandoff {
    param(
        [Parameter(Mandatory)][ValidateSet('Publish', 'Verify')][string] $Operation,
        [Parameter(Mandatory)][string] $PrepareReport,
        [Parameter(Mandatory)][string] $ProductionKey,
        [Parameter(Mandatory)][string] $ProofDirectory,
        [Parameter(Mandatory)][string] $OutputReport
    )
    if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'Handoff proof requires PowerShell 7' }
    foreach ($path in $PrepareReport, $ProofDirectory, $OutputReport) {
        Assert-FastPathWithin -Path $path -Root $handoffRepository
    }
    $proofRoot = [IO.Path]::GetFullPath($ProofDirectory).TrimEnd('\', '/')
    $reportPath = [IO.Path]::GetFullPath($OutputReport)
    if ($reportPath.StartsWith($proofRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Diagnostic report must be outside the transported proof directory'
    }
    if (Test-Path -LiteralPath $reportPath) { throw 'Refusing to overwrite handoff report' }
    $encoding = New-Object Text.UTF8Encoding($false)
    Write-HandoffNewFile $reportPath $encoding.GetBytes('{}')
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $result = [ordered]@{ schema = 1; operation = $Operation; status = 'failed'; production_key = $ProductionKey
        source_commit = $env:GITHUB_SHA; runner_image = $env:ImageVersion
        scope = 'Tiny cache identity/transport preflight; no SDK build or restore proof'; changed_fields = @() }
    try {
        $local = Get-HandoffPreparedIdentity $PrepareReport $ProductionKey
        $result['consumer_fingerprint'] = $local.Fingerprint
        $result['native_cmake'] = $local.NativeCMake
        if ($Operation -eq 'Publish') {
            if (Test-Path -LiteralPath $proofRoot) { throw 'Refusing to overwrite existing handoff proof' }
            [IO.Directory]::CreateDirectory($proofRoot) | Out-Null
            $marker = $encoding.GetBytes("PrusaSlicer tiny cross-runner cache handoff`n$($local.Key)`n")
            Write-HandoffNewFile (Join-Path $proofRoot 'input.json') $encoding.GetBytes($local.Canonical)
            Write-HandoffNewFile (Join-Path $proofRoot 'marker.bin') $marker
            $proof = [ordered]@{ schema = 1; key = $local.Key; fingerprint = $local.Fingerprint
                native_cmake = $local.NativeCMake; marker_bytes = $marker.Length; marker_sha256 = Get-FastBytesSha256 $marker }
            Write-HandoffNewFile (Join-Path $proofRoot 'proof.json') $encoding.GetBytes((ConvertTo-FastCanonicalJson $proof))
        }
        else {
            if (-not (Test-Path -LiteralPath $proofRoot -PathType Container)) { throw 'Transported proof is missing; no fallback compilation is allowed' }
            $entries = @(Get-ChildItem -LiteralPath $proofRoot -Force | Select-Object -First 4)
            if (($entries.Name | Sort-Object) -join ',' -cne 'input.json,marker.bin,proof.json' -or @($entries | Where-Object PSIsContainer).Count) {
                throw 'Unexpected transported proof contents'
            }
            foreach ($entry in $entries) { Assert-FastPathWithin -Path $entry.FullName -Root $proofRoot }
            $proof = Read-HandoffJson (Join-Path $proofRoot 'proof.json')
            if ($proof.schema -ne 1 -or $proof.fingerprint -cnotmatch '^[0-9a-f]{64}$' -or
                $proof.key -cne ('release-prefix-v1-' + $proof.fingerprint)) { throw 'Invalid producer proof identity' }
            $producerPath = Join-Path $proofRoot 'input.json'
            $producer = Read-HandoffJson $producerPath # Bound input size before hashing.
            if ((Get-FastFileSha256 $producerPath) -cne $proof.fingerprint) { throw 'Transported canonical input hash changed' }
            if ((ConvertTo-FastCanonicalJson $producer.tools.cmake) -cne (ConvertTo-FastCanonicalJson $proof.native_cmake)) {
                throw 'Producer native CMake identity contradicts its fingerprint inputs'
            }
            $result['producer_fingerprint'] = $proof.fingerprint
            $result['producer_native_cmake'] = $proof.native_cmake
            $result.changed_fields = @(Compare-HandoffInputs $producer $local.Input)
            $markerPath = Join-Path $proofRoot 'marker.bin'
            $expectedMarker = $encoding.GetBytes("PrusaSlicer tiny cross-runner cache handoff`n$($proof.key)`n")
            $actualMarker = Get-Item -LiteralPath $markerPath
            if ($proof.marker_bytes -ne $expectedMarker.Length -or $actualMarker.Length -ne $expectedMarker.Length -or
                $proof.marker_sha256 -cne (Get-FastBytesSha256 $expectedMarker) -or
                (Get-FastFileSha256 $markerPath) -cne $proof.marker_sha256) { throw 'Transported marker payload hash/length changed' }
            if ($proof.fingerprint -cne $local.Fingerprint -or $result.changed_fields.Count -ne 0) {
                throw 'Producer/consumer production inputs differ; see changed_fields (no identity fields are ignored)'
            }
        }
        $result['marker_sha256'] = $proof.marker_sha256
        $result['marker_bytes'] = $proof.marker_bytes
        $result.status = 'passed'
        Write-Host "PASS: $Operation tiny handoff proof ($($local.Fingerprint)); native CMake $($local.NativeCMake.path)"
    }
    catch { $result['error'] = $_.Exception.Message; throw }
    finally {
        $timer.Stop()
        $result['seconds'] = $timer.Elapsed.TotalSeconds
        # This exact file was reserved exclusively before any proof operation.
        Write-FastUtf8File $reportPath (ConvertTo-FastCanonicalJson $result)
    }
    return [pscustomobject] $result
}

if ($MyInvocation.InvocationName -ne '.') {
    $null = Invoke-CMakeHandoff -Operation $Operation -PrepareReport $PrepareReport -ProductionKey $ProductionKey `
        -ProofDirectory $ProofDirectory -OutputReport $OutputReport
}
