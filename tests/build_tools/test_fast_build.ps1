# Run directly in PowerShell; no Pester, compiler, WSL, or build is required.
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$testDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$repositoryDirectory = Split-Path -Parent (Split-Path -Parent $testDirectory)
. (Join-Path $repositoryDirectory 'build_fast_resources.ps1')

function Assert-Equal {
    param($Actual, $Expected, [string] $Message)
    if ($Actual -cne $Expected) {
        throw "$Message`: expected '$Expected', got '$Actual'"
    }
}

$cases = @(
    @{ Name = 'large workstation'; Cpu = 24; Total = 64GB; Available = 48GB; Expected = 2 },
    @{ Name = 'half the CPU'; Cpu = 4; Total = 64GB; Available = 48GB; Expected = 2 },
    @{ Name = 'single CPU'; Cpu = 1; Total = 64GB; Available = 48GB; Expected = 1 },
    @{ Name = 'memory pressure'; Cpu = 24; Total = 16GB; Available = 10GB; Expected = 1 }
)
foreach ($case in $cases) {
    $actual = Resolve-FastBuildJobs -ProcessorCount $case.Cpu `
        -TotalMemoryBytes $case.Total -AvailableMemoryBytes $case.Available
    Assert-Equal $actual $case.Expected $case.Name
}
Assert-Equal (Resolve-FastBuildJobs -Jobs 12 -ProcessorCount 1 -TotalMemoryBytes 64GB -AvailableMemoryBytes 48GB) 12 'explicit concurrency override'
Assert-Equal (Resolve-FastBuildJobs -Jobs 1 -TotalMemoryBytes 64GB -AvailableMemoryBytes 48GB) 1 'explicit serial build'
foreach ($unsafe in @(@{Total=8GB;Free=4GB}, @{Total=64GB;Free=1GB}, @{Total=0;Free=0}, @{Total=4GB;Free=64GB})) {
    foreach ($requestedJobs in @(0, 12)) {
        $rejected = $false
        try { Resolve-FastBuildJobs -Jobs $requestedJobs -TotalMemoryBytes $unsafe.Total -AvailableMemoryBytes $unsafe.Free }
        catch { $rejected = $_.Exception.Message -match 'refusing to start' }
        Assert-Equal $rejected $true 'unsafe resource budget rejected even with explicit jobs'
    }
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    Write-Host 'PASS: 14 resource-budget cases; Windows wrapper tests skipped on this host.'
    return
}

$memory = Get-FastHostMemory
if ($memory.TotalBytes -le 0 -or $memory.AvailableBytes -gt $memory.TotalBytes) {
    throw 'Windows physical-memory query did not return valid data'
}

$temporaryDirectory = Join-Path $testDirectory ('.tmp-fast-build-' + [Guid]::NewGuid().ToString('N'))
$previousComSpec = [Environment]::GetEnvironmentVariable('ComSpec')
$previousParallelLevel = [Environment]::GetEnvironmentVariable('CMAKE_BUILD_PARALLEL_LEVEL')
$previousState = Get-Variable -Name PrusaFastBuildTestState -Scope Global -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
try {
    foreach ($name in @('build_fast.ps1')) {
        Copy-Item -LiteralPath (Join-Path $repositoryDirectory $name) -Destination $temporaryDirectory
    }
    Copy-Item -LiteralPath (Join-Path $testDirectory 'fixtures/mock_build_cache.ps1') `
        -Destination (Join-Path $temporaryDirectory 'build_fast_cache.ps1')
    Copy-Item -LiteralPath (Join-Path $testDirectory 'fixtures/mock_build_resources.ps1') `
        -Destination (Join-Path $temporaryDirectory 'build_fast_resources.ps1')
    New-Item -ItemType Directory -Path (Join-Path $temporaryDirectory 'deps') | Out-Null
    $wrapper = Join-Path $temporaryDirectory 'build_fast.ps1'
    $mockTool = Join-Path $testDirectory 'fixtures/mock_build_tool.ps1'
    $global:PrusaFastBuildTestState = [pscustomobject]@{
        Root = $temporaryDirectory
        Tool = $mockTool
        Resources = Join-Path $repositoryDirectory 'build_fast_resources.ps1'
        Memory = [pscustomobject]@{TotalBytes=64GB; AvailableBytes=48GB}
        Settings = @{}
        Calls = New-Object 'Collections.Generic.List[object]'
        Fail = $false
    }

    # Child script command discovery resolves these mocks before any native tool.
    function Get-Command {
        param([string] $Name, $ErrorAction)
        switch ($Name) {
            'vswhere.exe' { return [pscustomobject]@{ Source = $global:PrusaFastBuildTestState.Tool } }
            'cmake' { return [pscustomobject]@{ Source = 'C:\mock\cmake.exe' } }
            'ninja' { return [pscustomobject]@{ Source = 'C:\mock\ninja.exe' } }
            'python' { return [pscustomobject]@{ Source = $global:PrusaFastBuildTestState.Tool } }
            'sccache' { return $null }
            'ccache' { return $null }
            'C:\mock\sccache.exe' { return [pscustomobject]@{ Source = 'C:\mock\sccache.exe' } }
            default { throw "Unexpected executable lookup in wrapper test: $Name" }
        }
    }

    $env:ComSpec = $mockTool
    $env:CMAKE_BUILD_PARALLEL_LEVEL = '99'
    & $wrapper -Jobs 3 -NoDependencyCache
    $calls = $global:PrusaFastBuildTestState.Calls
    Assert-Equal $calls.Count 4 'all step configure/build command count'
    if ($calls[0].Command -notmatch '"-DDEP_MAX_THREADS=3"') {
        throw 'The dependency configure did not receive the explicit worker count'
    }
    foreach ($index in @(1, 3)) {
        if ($calls[$index].Command -notmatch '"--parallel" "3"') {
            throw 'An outer build did not receive an explicit worker count'
        }
        if ($calls[$index].Command -match '"--preset"') {
            throw 'Build preset environment could override the selected nested worker count'
        }
    }
    foreach ($call in $calls) {
        Assert-Equal $call.ParallelLevel '3' 'nested build environment cap'
        foreach ($flag in @('--memory-gib', '--minimum-free-gib', '--minimum-commit-gib', '--cpu-count')) {
            if ($flag -notin $call.GuardArguments) { throw "Missing mandatory safety flag $flag" }
        }
        if ($call.GuardArguments[1] -notmatch 'tools[\\/]run_guarded.py$') { throw 'CMake command bypassed guard' }
    }
    Assert-Equal $env:CMAKE_BUILD_PARALLEL_LEVEL '99' 'caller environment after success'

    $calls.Clear()
    & $wrapper -Step all -Jobs 0 -NoDependencyCache
    $automaticJobs = [int] $calls[0].ParallelLevel
    if ($automaticJobs -lt 1 -or $automaticJobs -gt 4) {
        throw "Automatic worker count is outside its safe bounds: $automaticJobs"
    }
    if ($calls[0].Command -notmatch ('"-DDEP_MAX_THREADS=' + $automaticJobs + '"')) {
        throw 'The dependency configure did not receive the automatic worker count'
    }
    foreach ($index in @(1, 3)) {
        if ($calls[$index].Command -notmatch ('"--parallel" "' + $automaticJobs + '"')) {
            throw 'An automatic build has an inconsistent or unbounded worker count'
        }
    }
    foreach ($call in $calls) {
        Assert-Equal ([int] $call.ParallelLevel) $automaticJobs 'automatic nested build environment cap'
    }

    $calls.Clear()
    & $wrapper -Step app -Jobs 2 -NoDependencyCache
    Assert-Equal $calls.Count 2 'missing application tree configures before build'

    $applicationDirectory = Join-Path $temporaryDirectory 'build-fast-gui'
    New-Item -ItemType Directory -Path $applicationDirectory | Out-Null
    New-Item -ItemType File -Path (Join-Path $applicationDirectory 'CMakeCache.txt') | Out-Null
    $calls.Clear()
    & $wrapper -Step app -Jobs 2 -NoDependencyCache
    Assert-Equal $calls.Count 1 'configured application builds without reconfigure'

    $calls.Clear()
    & $wrapper -Step app -Jobs 2 -NoUnity -NoDependencyCache
    Assert-Equal $calls.Count 2 'NoUnity on app step reconfigures'
    if ($calls[0].Command -notmatch '"-DSLIC3R_UNITY_BUILD=OFF"') {
        throw 'NoUnity was ignored on the application build step'
    }

    $calls.Clear()
    & $wrapper -Profile lean-release -Step all -Jobs 3 -NoDependencyCache -MemoryGiB 8 -MinimumFreeGiB 7 -MinimumCommitGiB 9 -CpuCount 2 -LinkWorkingSetMiB 4096
    Assert-Equal $calls.Count 4 'release configure/build count'
    if ($calls[0].Command -notmatch '"lean-release"' -or $calls[2].Command -notmatch '"lean-release"') {
        throw 'Release profile did not select the paired release presets'
    }
    if ($calls[3].Command -match '"--target"|"PrusaSlicer_fast"') { throw 'Release must build the default all target including tests' }
    foreach ($call in $calls) {
        $guardArgs = $call.GuardArguments
        foreach ($entry in @(@('--memory-gib','8'), @('--minimum-free-gib','7'), @('--minimum-commit-gib','9'), @('--cpu-count','2'), @('--link-working-set-mib','4096'))) {
            $position = [Array]::IndexOf($guardArgs, $entry[0])
            Assert-Equal $guardArgs[$position+1] $entry[1] 'explicit resource limit forwarded'
        }
    }

    $calls.Clear()
    $global:PrusaFastBuildTestState.Memory.AvailableBytes = 1GB
    $rejected = $false
    try { & $wrapper -Step all -Jobs 12 -Clean -NoDependencyCache }
    catch { $rejected = $_.Exception.Message -match 'refusing to start' }
    Assert-Equal $rejected $true 'low memory wrapper preflight'
    Assert-Equal $calls.Count 0 'no child command launched on low memory'
    Assert-Equal (Test-Path -LiteralPath $applicationDirectory) $true 'preflight precedes Clean'
    $global:PrusaFastBuildTestState.Memory.AvailableBytes = 48GB

    $calls.Clear()
    & $wrapper -Step app -Jobs 2 -CompilerCache 'C:\mock\sccache.exe' -NoDependencyCache
    Assert-Equal $calls.Count 2 'cache selection change reconfigures existing app tree'
    foreach ($call in $calls) {
        $guardArgs = $call.GuardArguments
        if (-not ($guardArgs | Where-Object { $_ -match 'tools[\\/]sccache_supervisor.py$' })) {
            throw 'sccache-enabled build did not use the private foreground supervisor'
        }
        if ('--cache-dir' -notin $guardArgs -or '--session-dir' -notin $guardArgs) {
            throw 'Private server session or persistent artifact cache missing'
        }
    }
    if ($calls[0].Command -notmatch '-DSLIC3R_COMPILER_CACHE=C:\\mock\\sccache.exe') {
        throw 'Selected cache was not forwarded to CMake'
    }
    $global:PrusaFastBuildTestState.Settings = @{
        CMAKE_C_COMPILER_LAUNCHER='C:/mock/sccache.exe'; CMAKE_CXX_COMPILER_LAUNCHER='C:/mock/sccache.exe'
    }
    $calls.Clear()
    & $wrapper -Step app -Jobs 2 -CompilerCache 'C:\mock\sccache.exe' -NoDependencyCache
    Assert-Equal $calls.Count 1 'same launcher path spelling does not force reconfigure'
    $calls.Clear()
    & $wrapper -Step app -Jobs 2 -CompilerCache off -NoDependencyCache
    Assert-Equal $calls.Count 2 'disabling previously configured launcher reconfigures'
    $global:PrusaFastBuildTestState.Settings = @{}

    $calls.Clear()
    & $wrapper -Step deps -Jobs 2
    Assert-Equal $calls.Count 3 'partial cache configure then normal build'
    if ($calls[0].Command -notmatch '"-DPrusaSlicer_deps_CACHE_SELECTION="') {
        throw 'Initial dependency configure did not clear a stale package selection'
    }
    if ($calls[1].Command -notmatch 'verified-packages.json' -or $calls[2].Command -notmatch '"--build"') {
        throw 'Partial cache must select verified packages and still build remaining dependencies'
    }

    $global:PrusaFastBuildTestState.Fail = $true
    $failed = $false
    try {
        & $wrapper -Step configure -Jobs 2 -NoDependencyCache
    }
    catch {
        $failed = $_.Exception.Message -match 'failed with exit code 7'
    }
    Assert-Equal $failed $true 'native build failure propagates'
    Assert-Equal $env:CMAKE_BUILD_PARALLEL_LEVEL '99' 'caller environment after failure'
    Write-Host 'PASS: 14 resource-budget cases, Windows memory query, and 12 mocked wrapper scenarios.'
}
finally {
    [Environment]::SetEnvironmentVariable('ComSpec', $previousComSpec)
    [Environment]::SetEnvironmentVariable('CMAKE_BUILD_PARALLEL_LEVEL', $previousParallelLevel)
    if ($previousState) {
        $global:PrusaFastBuildTestState = $previousState.Value
    }
    else {
        Remove-Variable -Name PrusaFastBuildTestState -Scope Global -ErrorAction SilentlyContinue
    }
    $resolvedTemporaryDirectory = (Resolve-Path -LiteralPath $temporaryDirectory).Path
    $testPrefix = [IO.Path]::GetFullPath($testDirectory).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if (-not $resolvedTemporaryDirectory.StartsWith($testPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a test directory outside '$testDirectory': $resolvedTemporaryDirectory"
    }
    Remove-Item -LiteralPath $resolvedTemporaryDirectory -Recurse -Force
}
