# Hosted-only, tiny native acceleration proofs. Invoke after VsDevCmd x64 setup.
# Does not configure/build PrusaSlicer, its dependencies, or a final linker PDB.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string] $Python,
    [Parameter(Mandatory)][string] $Sccache,
    [Parameter(Mandatory)][string] $Output,
    [string] $CMake = 'cmake',
    [string] $Ninja = 'ninja'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_OS -ne 'Windows') {
    throw 'Hosted Windows CI only: these explicit VM reserves are not desktop defaults'
}
if ($env:VSCMD_ARG_TGT_ARCH -ne 'x64') { throw 'Set up the native x64 VS developer environment first' }
if (-not [IO.Path]::IsPathRooted($Python) -or -not [IO.Path]::IsPathRooted($Sccache)) {
    throw 'Python and the checksum-verified sccache executable must have absolute paths'
}
foreach ($executable in @($Python, $Sccache)) {
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw "Missing executable: $executable" }
}
$CMake = (Get-Command $CMake -CommandType Application | Select-Object -First 1).Source
$Ninja = (Get-Command $Ninja -CommandType Application | Select-Object -First 1).Source
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Output = [IO.Path]::GetFullPath($Output)
if (Test-Path -LiteralPath $Output) { throw 'Mechanism output must be fresh; refusing to overwrite' }
New-Item -ItemType Directory -Path $Output | Out-Null
$reports = Join-Path $Output 'reports'
New-Item -ItemType Directory -Path $reports | Out-Null
$cache = Join-Path $Output 'private-cache'
New-Item -ItemType Directory -Path $cache | Out-Null
$summary = [ordered]@{
    status = 'running'
    scope = 'One MSVC cache object and two-TU PCH fixtures; no application build or whole-project speed claim'
    sccache_sha256 = (Get-FileHash -LiteralPath $Sccache -Algorithm SHA256).Hash.ToLowerInvariant()
    phases = @()
}
$savedParallel = [Environment]::GetEnvironmentVariable('CMAKE_BUILD_PARALLEL_LEVEL')
$savedThreads = [Environment]::GetEnvironmentVariable('OMP_NUM_THREADS')
$watch = [Diagnostics.Stopwatch]::StartNew()
try {
    $env:CMAKE_BUILD_PARALLEL_LEVEL = '1'
    $env:OMP_NUM_THREADS = '1'
    $version = & $Sccache --version
    if ($LASTEXITCODE -ne 0 -or $version -ne 'sccache 0.17.0') { throw 'Expected the pinned official sccache 0.17.0' }
    $summary.sccache_version = $version
    foreach ($mode in @('cold', 'warm')) {
        $fixtureReport = Join-Path $reports "sccache-$mode-fixture.json"
        $session = Join-Path $Output "sessions/$mode"
        $command = @('-B', (Join-Path $repo 'tools/run_guarded.py'), '--log-dir', $reports,
            '--label', "sccache-$mode-guard", '--memory-gib', '2', '--minimum-free-gib', '2',
            '--minimum-commit-gib', '2', '--launch-headroom-gib', '0.5', '--cpu-count', '1',
            '--timeout-seconds', '180', '--', $Python, '-B',
            (Join-Path $repo 'tools/sccache_supervisor.py'), '--sccache', $Sccache,
            '--session-dir', $session, '--cache-dir', $cache, '--', $Python, '-B',
            (Join-Path $repo 'tests/build_tools/run_sccache_fixture.py'), '--cmake', $CMake,
            '--ninja', $Ninja, '--sccache', $Sccache, '--build', (Join-Path $Output 'cache-build'),
            '--mode', $mode, '--report', $fixtureReport)
        if ($mode -eq 'warm') { $command += @('--previous-report', (Join-Path $reports 'sccache-cold-fixture.json')) }
        Write-Host "[native mechanism] sccache $mode (one worker, private supervised server)"
        & $Python @command
        $code = $LASTEXITCODE
        $serverReport = Join-Path $session 'server.json'
        if (Test-Path -LiteralPath $serverReport) {
            Copy-Item -LiteralPath $serverReport -Destination (Join-Path $reports "sccache-$mode-server.json")
        }
        if ($code -ne 0) { throw "Guarded sccache $mode failed ($code)" }
        $server = Get-Content -LiteralPath $serverReport -Raw | ConvertFrom-Json
        $fixture = Get-Content -LiteralPath $fixtureReport -Raw | ConvertFrom-Json
        if ($server.status -ne 'passed' -or -not $server.listener_verified -or $fixture.status -ne 'passed') {
            throw "Missing private-listener/cleanup or cache-mechanism proof for $mode"
        }
        $summary.phases += [ordered]@{ name = "sccache-$mode"; status = 'passed'; fixture_report = "sccache-$mode-fixture.json" }
    }
    Write-Host '[native mechanism] full versus stable PCH (real header edit and no-op proofs)'
    $pchOutput = Join-Path $Output 'pch'
    & $Python -B (Join-Path $repo 'tools/benchmark_pch.py') --output $pchOutput --cmake $CMake --ninja $Ninja `
        --max-seconds 180 --minimum-free-gib 2 --minimum-commit-gib 2
    $code = $LASTEXITCODE
    $pchReport = Join-Path $pchOutput 'benchmark.json'
    if (Test-Path -LiteralPath $pchReport) {
        Copy-Item -LiteralPath $pchReport -Destination (Join-Path $reports 'pch-mechanisms.json')
    }
    # Include bounded command logs, never the large PCH/object/cache artifacts.
    $pchLogs = Join-Path $pchOutput 'logs'
    if (Test-Path -LiteralPath $pchLogs) {
        Copy-Item -LiteralPath $pchLogs -Destination (Join-Path $reports 'pch-logs') -Recurse
    }
    if ($code -ne 0) { throw "PCH mechanism benchmark failed ($code)" }
    $pch = Get-Content -LiteralPath $pchReport -Raw | ConvertFrom-Json
    $proofs = @($pch.steps | Where-Object { $_.PSObject.Properties.Name -contains 'mechanism' })
    if ($pch.status -ne 'passed' -or $proofs.Count -ne 8 -or @($proofs | Where-Object { -not $_.mechanism.passed }).Count) {
        throw 'Expected all eight clean/edit/no-op PCH mechanism proofs'
    }
    $summary.phases += [ordered]@{ name = 'pch'; status = 'passed'; fixture_report = 'pch-mechanisms.json' }
    $summary.status = 'passed'
    Write-Host 'PASS: genuine MSVC cache miss/write and hit, embedded object symbols, full-PCH rebuild, stable-PCH reuse and four no-op builds.'
}
catch {
    $summary.status = 'failed'
    $summary.error = $_.Exception.Message
    throw
}
finally {
    [Environment]::SetEnvironmentVariable('CMAKE_BUILD_PARALLEL_LEVEL', $savedParallel)
    [Environment]::SetEnvironmentVariable('OMP_NUM_THREADS', $savedThreads)
    # Preserve private-server diagnostics even when startup/cleanup fails. Do
    # not upload cache files, compiler objects, PCHs or the downloaded tool.
    foreach ($mode in @('cold', 'warm')) {
        foreach ($extension in @('json', 'log')) {
            $sourceLog = Join-Path $Output "sessions/$mode/server.$extension"
            if (Test-Path -LiteralPath $sourceLog -PathType Leaf) {
                Copy-Item -LiteralPath $sourceLog -Destination (Join-Path $reports "sccache-$mode-server.$extension") -Force
            }
        }
    }
    $summary.seconds = [Math]::Round($watch.Elapsed.TotalSeconds, 3)
    $summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $reports 'native-mechanisms.json') -Encoding UTF8
    @(
        '# Native acceleration mechanism fixtures',
        '',
        "Status: $($summary.status)",
        '',
        'This lane compiles only one cache-probe object and two small PCH translation units.',
        'It requires a cold miss/write, a genuine cache hit with zero compiler executions,',
        'byte-identical restored objects with embedded symbols, full-PCH invalidation,',
        'stable-PCH reuse after a real copied-header edit, and four unchanged no-op builds.',
        '',
        'See native-mechanisms.json and the separate fixture/guard reports for actual outcomes.',
        'No full application build, final linker PDB, GUI parity or whole-project speedup is proved.'
    ) | Set-Content -LiteralPath (Join-Path $reports 'summary.md') -Encoding UTF8
}
