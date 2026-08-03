[CmdletBinding()]
param(
    [ValidateSet("gui", "cli")]
    [string] $Profile = "gui",

    [ValidateSet("all", "deps", "configure", "app")]
    [string] $Step = "all",

    [ValidateRange(0, 512)]
    [int] $Jobs = 0,

    [switch] $Clean,
    [switch] $NoUnity,
    [switch] $NoDependencyCache
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sourceDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $sourceDirectory "build_fast_cache.ps1")
$preset = "fast-$Profile"
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
    Push-Location -LiteralPath $WorkingDirectory
    try {
        if ($isWindowsHost) {
            foreach ($argument in $ArgumentList) {
                if ($argument.Contains('"')) {
                    throw "A CMake argument contains an unsupported quote: $argument"
                }
            }
            $quotedArguments = ($ArgumentList | ForEach-Object { '"' + $_ + '"' }) -join " "
            $commandLine = "call `"$vsDevCmd`" -no_logo -arch=x64 -host_arch=x64 >nul && `"$cmake`" $quotedArguments"
            & $env:ComSpec /d /c $commandLine
        }
        else {
            & $cmake @ArgumentList
        }

        if ($LASTEXITCODE -ne 0) {
            throw "$Label failed with exit code $LASTEXITCODE"
        }
    }
    finally {
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
    $dependencyConfigureArguments = @("--preset", $preset, "-DCMAKE_MAKE_PROGRAM=$ninja")
    if ($Jobs -gt 0) {
        $dependencyConfigureArguments += "-DDEP_MAX_THREADS=$Jobs"
    }
    else {
        # Do not retain a cap from an earlier invocation of this build tree.
        $dependencyConfigureArguments += "-UDEP_MAX_THREADS"
    }
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
        $dependencyBuildArguments = @("--build", "--preset", $preset)
        if ($Jobs -gt 0) {
            $dependencyBuildArguments += @("--parallel", "$Jobs")
        }
        else {
            $dependencyBuildArguments += "--parallel"
        }
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

if ($Step -in @("all", "configure") -or ($Step -eq "app" -and $Clean)) {
    $configureArguments = @("--preset", $preset, "-DCMAKE_MAKE_PROGRAM=$ninja")
    if ($NoUnity) {
        $configureArguments += "-DSLIC3R_UNITY_BUILD=OFF"
    }
    Invoke-CMake -WorkingDirectory $sourceDirectory `
        -ArgumentList $configureArguments `
        -Label "Configure $Profile application"
}

if ($Step -in @("all", "app")) {
    $target = "PrusaSlicer_fast"
    $buildArguments = @("--build", $applicationBuildDirectory, "--target", $target)
    if ($Jobs -gt 0) {
        $buildArguments += @("--parallel", "$Jobs")
    }
    else {
        $buildArguments += "--parallel"
    }
    Invoke-CMake -WorkingDirectory $sourceDirectory `
        -ArgumentList $buildArguments `
        -Label "Build $target"
}

Write-Host "[fast-build] Done. Dependency downloads and compiled prefixes remain cached under deps/.pkg_cache and deps/.compiled_cache."
