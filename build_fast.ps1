[CmdletBinding()]
param(
    [ValidateSet("gui", "cli", "lean-release")]
    [string] $Profile = "gui",

    [ValidateSet("all", "deps", "configure", "app")]
    [string] $Step = "all",

    [ValidateRange(0, 512)]
    [int] $Jobs = 0,

    [ValidateRange(0.1, 1048576)] [double] $MemoryGiB = 6,
    [ValidateRange(0.1, 1048576)] [double] $MinimumFreeGiB = 6,
    [ValidateRange(0.1, 1048576)] [double] $MinimumCommitGiB = 6,
    [ValidateRange(1, 512)] [int] $CpuCount = 2,
    [ValidateRange(0, 1048576)] [int] $LinkWorkingSetMiB = 0,
    [string] $Python = "",
    [string] $CompilerCache = 'auto',

    [switch] $Clean,
    [switch] $NoUnity,
    [switch] $NoDependencyCache
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sourceDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $sourceDirectory "build_fast_cache.ps1")
. (Join-Path $sourceDirectory "build_fast_resources.ps1")
$preset = if ($Profile -eq 'lean-release') { 'lean-release' } else { "fast-$Profile" }
$dependencyDirectory = Join-Path $sourceDirectory "deps"
$dependencyBuildDirectory = Join-Path $dependencyDirectory "build-$preset"
$applicationBuildDirectory = Join-Path $sourceDirectory "build-$preset"
$isWindowsHost = [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT

function Find-Executable {
    param(
        [Parameter(Mandatory)] [string] $Name,
        [string[]] $Candidates = @()
    )

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    return $null
}

$pythonExecutable = if ($Python) { Find-Executable -Name $Python } else { Find-Executable -Name 'python' }
if (-not $pythonExecutable) {
    throw 'Python with psutil is required for resource-guarded builds. Supply -Python with its executable path.'
}
$cacheExecutable = $null
if ($CompilerCache -eq 'auto') {
    $cacheExecutable = Find-Executable -Name 'sccache'
    if (-not $cacheExecutable) { $cacheExecutable = Find-Executable -Name 'ccache' }
}
elseif ($CompilerCache -notin @('off', 'none', '')) {
    $cacheExecutable = Find-Executable -Name $CompilerCache
    if (-not $cacheExecutable) { throw "Requested compiler cache was not found: $CompilerCache" }
}
if ($cacheExecutable -and [IO.Path]::GetFileNameWithoutExtension($cacheExecutable) -notin @('sccache', 'ccache')) {
    throw 'The guarded wrapper supports only known sccache/ccache launchers; an arbitrary launcher could escape process-tree protection.'
}
$cacheMode = if ($cacheExecutable) { $cacheExecutable } else { 'off' }
# Clear stale/custom launchers before our known launcher is selected by CMake.
$cacheConfigureArguments = @("-DSLIC3R_COMPILER_CACHE=$cacheMode", '-DCMAKE_C_COMPILER_LAUNCHER=', '-DCMAKE_CXX_COMPILER_LAUNCHER=')
$privateSccache = $cacheExecutable -and [IO.Path]::GetFileNameWithoutExtension($cacheExecutable) -eq 'sccache'
$hostMemory = Get-FastHostMemory
if (-not $isWindowsHost) {
    # PowerShell's native memory helper is Windows-only. Use the same psutil
    # provider as the guard on other native hosts; never start WSL to query it.
    $memoryJson = & $pythonExecutable -c 'import json,psutil; m=psutil.virtual_memory(); print(json.dumps(dict(TotalBytes=m.total, AvailableBytes=m.available)))'
    if ($LASTEXITCODE -ne 0) { throw 'Python/psutil physical-memory preflight failed.' }
    $hostMemory = $memoryJson | ConvertFrom-Json
}
$buildJobs = Resolve-FastBuildJobs -Jobs $Jobs -TotalMemoryBytes $hostMemory.TotalBytes `
    -AvailableMemoryBytes $hostMemory.AvailableBytes `
    -MinimumFreeMemoryBytes ([long] ($MinimumFreeGiB * 1GB)) -BuildMemoryBytes ([long] ($MemoryGiB * 1GB))
Write-Host "[fast-build] Using $buildJobs jobs; guard: $MemoryGiB GiB tree RSS, $MinimumFreeGiB GiB free RAM, $CpuCount CPUs."
if ($isWindowsHost) { Write-Host "[fast-build] Windows commit reserve: $MinimumCommitGiB GiB; private PDB service and below-normal priority." }

function Remove-BuildDirectory {
    param([Parameter(Mandatory)] [string] $Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    $workspace = [IO.Path]::GetFullPath($sourceDirectory).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $workspacePrefix = $workspace + [IO.Path]::DirectorySeparatorChar
    if (-not $resolved.StartsWith($workspacePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a build directory outside the workspace: $resolved"
    }

    Write-Host "[fast-build] Removing $resolved"
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

$visualStudioDirectory = $null
$vsDevCmd = $null
$cmakeCandidates = @()
$ninjaCandidates = @()

if ($isWindowsHost) {
    $programFilesX86 = [Environment]::GetFolderPath("ProgramFilesX86")
    $programFiles = [Environment]::GetFolderPath("ProgramFiles")
    $vswhere = Find-Executable -Name "vswhere.exe" -Candidates @(
        (Join-Path $programFilesX86 "Microsoft Visual Studio\Installer\vswhere.exe"),
        (Join-Path $programFiles "Microsoft Visual Studio\Installer\vswhere.exe")
    )
    if (-not $vswhere) {
        throw "Visual Studio Installer (vswhere.exe) was not found. Install Visual Studio Build Tools with the C++ workload."
    }

    $visualStudioDirectory = (& $vswhere -latest -products * `
        -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
        -property installationPath | Select-Object -First 1)
    if (-not $visualStudioDirectory) {
        throw "A Visual Studio installation with the C++ toolchain was not found."
    }

    $vsDevCmd = Join-Path $visualStudioDirectory "Common7\Tools\VsDevCmd.bat"
    $cmakeCandidates += Join-Path $visualStudioDirectory "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
    $ninjaCandidates += Join-Path $visualStudioDirectory "Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe"
}

$cmake = Find-Executable -Name "cmake" -Candidates $cmakeCandidates
$ninja = Find-Executable -Name "ninja" -Candidates $ninjaCandidates
if (-not $cmake) {
    throw "CMake was not found. Install CMake or the Visual Studio CMake component."
}
if (-not $ninja) {
    throw "Ninja was not found. Install Ninja or the Visual Studio CMake component."
}

function Invoke-CMake {
    param(
        [Parameter(Mandatory)] [string] $WorkingDirectory,
        [Parameter(Mandatory)] [string[]] $ArgumentList,
        [Parameter(Mandatory)] [string] $Label
    )

    Write-Host "[fast-build] $Label"
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    $previousParallelLevel = [Environment]::GetEnvironmentVariable('CMAKE_BUILD_PARALLEL_LEVEL')
    Push-Location -LiteralPath $WorkingDirectory
    try {
        # Also bound install steps and other nested cmake --build commands that
        # do not carry an explicit job count. Restore the caller's setting below.
        $env:CMAKE_BUILD_PARALLEL_LEVEL = [string] $buildJobs
        $guardLabel = $preset + '-' + [Guid]::NewGuid().ToString('N')
        $guardDirectory = Join-Path $sourceDirectory 'out/build-guard'
        if ($isWindowsHost) {
            [IO.Directory]::CreateDirectory($guardDirectory) | Out-Null
            $commandFile = Join-Path $guardDirectory ($guardLabel + '.cmd')
            New-FastWindowsCommandFile -Path $commandFile -VsDevCmd $vsDevCmd -Executable $cmake -ArgumentList $ArgumentList
            $childArguments = @($env:ComSpec, '/d', '/c', $commandFile)
        }
        else {
            $childArguments = @($cmake) + $ArgumentList
        }
        if ($privateSccache) {
            $childArguments = @($pythonExecutable, '-B', (Join-Path $sourceDirectory 'tools/sccache_supervisor.py'),
                '--sccache', $cacheExecutable, '--session-dir', (Join-Path $guardDirectory ($guardLabel + '.sccache')),
                '--cache-dir', (Join-Path $sourceDirectory 'out/compiler-cache/sccache'), '--') + $childArguments
        }

        $guardArguments = @('-B', (Join-Path $sourceDirectory 'tools/run_guarded.py'),
            '--log-dir', $guardDirectory,
            '--label', $guardLabel,
            '--memory-gib', $MemoryGiB.ToString([Globalization.CultureInfo]::InvariantCulture),
            '--minimum-free-gib', $MinimumFreeGiB.ToString([Globalization.CultureInfo]::InvariantCulture),
            '--launch-headroom-gib', ([Math]::Min(2, $MemoryGiB)).ToString([Globalization.CultureInfo]::InvariantCulture),
            '--cpu-count', [string] $CpuCount)
        if ($isWindowsHost) {
            $guardArguments += @('--minimum-commit-gib', $MinimumCommitGiB.ToString([Globalization.CultureInfo]::InvariantCulture))
            if ($LinkWorkingSetMiB -gt 0) { $guardArguments += @('--link-working-set-mib', [string] $LinkWorkingSetMiB) }
        }
        elseif ($LinkWorkingSetMiB -gt 0) { throw '-LinkWorkingSetMiB is Windows-only.' }
        & $pythonExecutable @guardArguments -- @childArguments

        if ($LASTEXITCODE -ne 0) {
            throw "$Label failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        [Environment]::SetEnvironmentVariable('CMAKE_BUILD_PARALLEL_LEVEL', $previousParallelLevel)
        Pop-Location
        $stopwatch.Stop()
        Write-Host ("[fast-build] {0}: {1:c}" -f $Label, $stopwatch.Elapsed)
    }
}

if ($Clean) {
    if ($Step -in @("all", "deps")) {
        Remove-BuildDirectory -Path $dependencyBuildDirectory
    }
    if ($Step -in @("all", "configure", "app")) {
        Remove-BuildDirectory -Path $applicationBuildDirectory
    }
}

if ($Step -in @("all", "deps")) {
    $dependencyConfigureArguments = @("--preset", $preset, "-DCMAKE_MAKE_PROGRAM=$ninja", "-DDEP_MAX_THREADS=$buildJobs", '-DPrusaSlicer_deps_CACHE_SELECTION=') + $cacheConfigureArguments
    Invoke-CMake -WorkingDirectory $dependencyDirectory `
        -ArgumentList $dependencyConfigureArguments `
        -Label "Configure $Profile dependencies"

    $dependencyCacheContext = $null
    $dependencyCacheHit = $false
    if ($isWindowsHost -and -not $NoDependencyCache) {
        try {
            $dependencyCacheContext = New-FastDependencyCacheContext `
                -SourceDirectory $sourceDirectory `
                -DependencyDirectory $dependencyDirectory `
                -DependencyBuildDirectory $dependencyBuildDirectory `
                -Profile $Profile `
                -Preset $preset `
                -CMake $cmake `
                -Ninja $ninja `
                -VsDevCmd $vsDevCmd
            $dependencyCacheState = Restore-FastDependencyCache `
                -Context $dependencyCacheContext `
                -CMake $cmake
            if ($dependencyCacheState -eq 'rebuild') {
                Remove-BuildDirectory -Path $dependencyBuildDirectory
                Invoke-CMake -WorkingDirectory $dependencyDirectory `
                    -ArgumentList $dependencyConfigureArguments `
                    -Label "Reconfigure $Profile dependencies after cache invalidation"
                $dependencyCacheContext = New-FastDependencyCacheContext `
                    -SourceDirectory $sourceDirectory `
                    -DependencyDirectory $dependencyDirectory `
                    -DependencyBuildDirectory $dependencyBuildDirectory `
                    -Profile $Profile `
                    -Preset $preset `
                    -CMake $cmake `
                    -Ninja $ninja `
                    -VsDevCmd $vsDevCmd
                $dependencyCacheState = Restore-FastDependencyCache `
                    -Context $dependencyCacheContext `
                    -CMake $cmake
                if ($dependencyCacheState -eq 'rebuild') {
                    throw 'Dependency cache remained stale after a clean reconfigure'
                }
            }
            $dependencyCacheHit = $dependencyCacheState -in @('hit', 'publish')
            if ($dependencyCacheState -eq 'partial') {
                Invoke-CMake -WorkingDirectory $dependencyDirectory `
                    -ArgumentList ($dependencyConfigureArguments + "-DPrusaSlicer_deps_CACHE_SELECTION=$($dependencyCacheContext.PackageSelectionPath)") `
                    -Label "Configure $Profile verified partial dependency cache"
            }
            if ($dependencyCacheState -eq 'publish') {
                try {
                    Publish-FastDependencyCache `
                        -Context $dependencyCacheContext `
                        -CMake $cmake
                }
                catch {
                    Write-Warning "Unable to republish the compiled dependency cache: $($_.Exception.Message)"
                }
            }
        }
        catch {
            Write-Warning "Compiled dependency cache unavailable; continuing with a source build: $($_.Exception.Message)"
            $dependencyCacheContext = $null
        }
    }

    if (-not $dependencyCacheHit) {
        # Ninja's console pool serializes the resource-heavy ExternalProject
        # build/install steps while downloads and configuration may overlap.
        # Build directly so a build preset's environment cannot override the
        # selected CMAKE_BUILD_PARALLEL_LEVEL for nested install commands.
        $dependencyBuildArguments = @("--build", $dependencyBuildDirectory, "--target", "deps", "--parallel", "$buildJobs")
        Invoke-CMake -WorkingDirectory $dependencyDirectory `
            -ArgumentList $dependencyBuildArguments `
            -Label "Build $Profile dependencies"

        if ($isWindowsHost) {
            Optimize-FastDependencyPrefix `
                -Prefix (Join-Path $dependencyBuildDirectory 'destdir\usr\local') `
                -AllowedRoot $dependencyBuildDirectory
        }

        if ($dependencyCacheContext) {
            try {
                Publish-FastDependencyCache `
                    -Context $dependencyCacheContext `
                    -CMake $cmake
            }
            catch {
                Write-Warning "Unable to publish the compiled dependency cache: $($_.Exception.Message)"
            }
        }
    }
}

$applicationCacheExists = Test-Path -LiteralPath (Join-Path $applicationBuildDirectory 'CMakeCache.txt') -PathType Leaf
$cacheSelectionChanged = $false
if ($applicationCacheExists) {
    $applicationSettings = Read-FastCMakeCache -Path (Join-Path $applicationBuildDirectory 'CMakeCache.txt')
    foreach ($language in @('C', 'CXX')) {
        $configuredLauncher = ([string] $applicationSettings["CMAKE_${language}_COMPILER_LAUNCHER"]).Replace('\', '/')
        if ($configuredLauncher -ne ([string] $cacheExecutable).Replace('\', '/')) {
            $cacheSelectionChanged = $true
        }
    }
}
if ($Step -in @("all", "configure") -or
    ($Step -eq "app" -and ($Clean -or $NoUnity -or $cacheSelectionChanged -or -not $applicationCacheExists))) {
    $configureArguments = @("--preset", $preset, "-DCMAKE_MAKE_PROGRAM=$ninja") + $cacheConfigureArguments
    if ($NoUnity) {
        $configureArguments += "-DSLIC3R_UNITY_BUILD=OFF"
    }
    Invoke-CMake -WorkingDirectory $sourceDirectory `
        -ArgumentList $configureArguments `
        -Label "Configure $Profile application"
}

if ($Step -in @("all", "app")) {
    # The full release includes every wrapper and configured test target. The
    # development profiles intentionally build only their runnable entry point.
    $target = if ($Profile -eq 'lean-release') { 'all' } else { 'PrusaSlicer_fast' }
    $buildArguments = @("--build", $applicationBuildDirectory, "--parallel", "$buildJobs")
    if ($Profile -ne 'lean-release') { $buildArguments += @('--target', $target) }
    Invoke-CMake -WorkingDirectory $sourceDirectory `
        -ArgumentList $buildArguments `
        -Label "Build $target"
}

Write-Host "[fast-build] Done. Dependency downloads and compiled prefixes remain cached under deps/.pkg_cache and deps/.compiled_cache."
