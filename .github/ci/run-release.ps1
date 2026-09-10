# Explicit hosted-runner experiment. Never starts a build on a developer machine.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Prepare', 'Dependencies', 'Restore', 'Application', 'Tests', 'Package', 'Report')][string] $Stage,
    [Parameter(Mandatory)][string] $Python
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted') {
    throw 'Release experiments are restricted to disposable GitHub-hosted runners'
}
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$output = Join-Path $repo 'out/ci-release'
New-Item -ItemType Directory -Path $output -Force | Out-Null
$reports = Join-Path $output 'reports'
New-Item -ItemType Directory -Path $reports -Force | Out-Null
$cmake = (Get-Command cmake -CommandType Application | Select-Object -First 1).Source
$ctest = Join-Path (Split-Path -Parent $cmake) 'ctest.exe'
$ninja = (Get-Command ninja -CommandType Application | Select-Object -First 1).Source
$vswhere = Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'Microsoft Visual Studio/Installer/vswhere.exe'
$vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if ($LASTEXITCODE -ne 0 -or -not $vs) { throw 'x64 MSVC is required' }
$vsDevCmd = Join-Path ($vs | Select-Object -First 1) 'Common7/Tools/VsDevCmd.bat'
. (Join-Path $repo 'build_fast_resources.ps1')
. (Join-Path $repo 'build_fast_cache.ps1')
$dependencyDirectory = Join-Path $repo 'deps'
$dependencyBuild = Join-Path $dependencyDirectory 'build-lean-release'
$applicationBuild = Join-Path $repo 'build-lean-release'

function Invoke-Recorded([string] $Label, [string] $Executable, [string[]] $Arguments) {
    $log = Join-Path $reports ($Label + '.log')
    $json = Join-Path $reports ($Label + '.json')
    if ((Test-Path -LiteralPath $log) -or (Test-Path -LiteralPath $json)) { throw "Duplicate release phase: $Label" }
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $code = -1
    try {
        & $Executable @Arguments 2>&1 | Tee-Object -FilePath $log | ForEach-Object { Write-Host "$_" }
        $code = $LASTEXITCODE
        if ($code -ne 0) { throw "$Label failed: $code" }
    }
    finally {
        $timer.Stop()
        [ordered]@{ label = $Label; seconds = $timer.Elapsed.TotalSeconds; exit_code = $code;
            executable = $Executable; arguments = $Arguments; git_sha = $env:GITHUB_SHA;
            runner_image = $env:ImageVersion } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $json -Encoding utf8
    }
}
function Invoke-GuardedRelease([string] $Label, [string[]] $Command, [int] $Timeout = 1800) {
    Invoke-Recorded $Label $Python (@('-B', (Join-Path $repo 'tools/run_guarded.py'),
        '--log-dir', $reports, '--label', ($Label + '-guard'), '--memory-gib', '11',
        '--minimum-free-gib', '2', '--minimum-commit-gib', '2', '--launch-headroom-gib', '2',
        '--cpu-count', '2', '--timeout-seconds', [string]$Timeout, '--') + $Command)
}
function New-Context {
    New-FastDependencyCacheContext -SourceDirectory $repo -DependencyDirectory $dependencyDirectory `
        -DependencyBuildDirectory $dependencyBuild -Profile lean-release -Preset lean-release `
        -CMake $cmake -Ninja $ninja -VsDevCmd $vsDevCmd
}
function Get-PinnedDownload([string] $Url, [string] $Destination, [string] $Hash) {
    if (Test-Path -LiteralPath $Destination) { throw "Refusing to overwrite download: $Destination" }
    Invoke-WebRequest -Uri $Url -OutFile $Destination -TimeoutSec 180
    if ((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Hash) {
        throw "Downloaded input failed SHA-256 verification: $Url"
    }
}

Push-Location -LiteralPath $repo
try {
    $env:CMAKE_BUILD_PARALLEL_LEVEL = '2'
    $wrapperArguments = @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', (Join-Path $repo 'build_fast.ps1'),
        '-Profile', 'lean-release', '-Python', $Python, '-CompilerCache', 'off', '-Jobs', '2',
        '-MemoryGiB', '11', '-MinimumFreeGiB', '2', '-MinimumCommitGiB', '2', '-CpuCount', '2')
    $pwsh = (Get-Command pwsh -CommandType Application | Select-Object -First 1).Source
    switch ($Stage) {
        'Prepare' {
            $command = Join-Path $output 'configure-dependencies.cmd'
            New-FastWindowsCommandFile -Path $command -VsDevCmd $vsDevCmd -Executable $cmake -ArgumentList @(
                '--preset', 'lean-release', '-S', $dependencyDirectory, "-DCMAKE_MAKE_PROGRAM=$ninja", '-DDEP_MAX_THREADS=2',
                '-DPrusaSlicer_deps_CACHE_SELECTION=', '-DSLIC3R_COMPILER_CACHE=off',
                '-DCMAKE_C_COMPILER_LAUNCHER=', '-DCMAKE_CXX_COMPILER_LAUNCHER=')
            Invoke-GuardedRelease 'prepare' @($env:ComSpec, '/d', '/c', $command)
            $context = New-Context
            $context.InputJson | Set-Content -LiteralPath (Join-Path $reports 'dependency-input.json') -Encoding utf8
            "key=release-prefix-v1-$($context.Fingerprint)" | Add-Content -LiteralPath $env:GITHUB_OUTPUT
            "entry=$($context.CacheEntryDirectory)" | Add-Content -LiteralPath $env:GITHUB_OUTPUT
        }
        'Dependencies' {
            Invoke-Recorded 'dependencies' $pwsh ($wrapperArguments + @('-Step', 'deps'))
            $context = New-Context
            if (-not (Get-FastValidatedCacheManifest -Context $context -CMake $cmake)) {
                throw 'A dependency build is not a cache publication proof: verified entry is missing'
            }
        }
        'Restore' {
            $context = New-Context
            $timer = [Diagnostics.Stopwatch]::StartNew()
            $state = Restore-FastDependencyCache -Context $context -CMake $cmake
            $timer.Stop()
            [ordered]@{ state = $state; seconds = $timer.Elapsed.TotalSeconds; fingerprint = $context.Fingerprint;
                fresh_runner = $true; source_rebuild_allowed = $false } | ConvertTo-Json |
                Set-Content -LiteralPath (Join-Path $reports 'restore.json') -Encoding utf8
            if ($state -ne 'hit') { throw "Fresh-runner cache proof failed: $state; no fallback compilation is allowed" }
        }
        'Application' {
            Invoke-Recorded 'application-configure' $pwsh ($wrapperArguments + @('-Step', 'configure'))
            Invoke-Recorded 'application-clean' $pwsh ($wrapperArguments + @('-Step', 'app'))
            foreach ($attempt in 1..3) {
                Invoke-Recorded "application-warm-$attempt" $pwsh ($wrapperArguments + @('-Step', 'app'))
            }
        }
        'Tests' {
            Invoke-GuardedRelease 'application-tests' @($ctest, '--test-dir', $applicationBuild,
                '--output-on-failure', '--no-tests=error', '--timeout', '600', '-j', '2')
        }
        'Package' {
            $baseline = Join-Path $output 'official-2.9.6.zip'
            Get-PinnedDownload 'https://github.com/prusa3d/PrusaSlicer/releases/download/version_2.9.6/PrusaSlicer-2.9.6.zip' `
                $baseline '5aaf22e42f95accecfa122d23a835911f289ecc2ff606db3e83d637ddcc0a209'
            $step = Join-Path $output 'occt-screw.step'
            # OCCT 7.6.1 test data, LGPL-2.1 with OCCT exception; downloaded only
            # into the disposable test workspace, never included in a release.
            Get-PinnedDownload 'https://raw.githubusercontent.com/Open-Cascade-SAS/OCCT/d2abb6d844231cb8f29be6894440874a4700e4a5/data/step/screw.step' `
                $step '4b3649a4f5c4f05c7a06a402a91fe2fd7e3cba1615520fbd8c62a62610ad3e69'
            Invoke-GuardedRelease 'release-validation' @($Python, '-B', '.github/ci/validate-release.py',
                '--baseline', $baseline, '--step', $step, '--output', (Join-Path $output 'validation'))
        }
        'Report' {
            Invoke-Recorded 'collect-report' $Python @('-B', '.github/ci/validate-release.py', '--collect', $output)
        }
    }
}
finally { Pop-Location }
