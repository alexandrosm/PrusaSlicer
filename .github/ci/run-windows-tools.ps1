# Disposable hosted-runner fixtures only; not a full application build/release.
[CmdletBinding()]
param([Parameter(Mandatory)][string] $Python)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$output = Join-Path $repo 'out/ci-streamlining'
if (Test-Path -LiteralPath $output) { throw 'CI output must be fresh; refusing to overwrite' }
New-Item -ItemType Directory -Path $output | Out-Null
Start-Transcript -LiteralPath (Join-Path $output 'fixtures.log') | Out-Null

function Invoke-Checked([string] $Executable, [string[]] $Arguments) {
    Write-Host "[CI fixture] $Executable $($Arguments -join ' ')"
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Fixture failed ($LASTEXITCODE): $Executable $($Arguments -join ' ')" }
}

function Invoke-Guarded([string] $Label, [string[]] $Command) {
    # These reserves are for an ephemeral CI VM, not the user's desktop. Keep
    # the normal build wrapper's larger safety defaults unchanged.
    Invoke-Checked $Python (@('-B', (Join-Path $repo 'tools/run_guarded.py'),
        '--log-dir', $output, '--label', $Label, '--memory-gib', '2',
        '--minimum-free-gib', '2', '--minimum-commit-gib', '2',
        '--launch-headroom-gib', '0.5', '--cpu-count', '1',
        '--timeout-seconds', '240', '--') + $Command)
}

$previousDecoder = [Environment]::GetEnvironmentVariable('PRUSA_STL_RESTORE_TEST_EXE')
Push-Location -LiteralPath $repo
try {
    $cmake = (Get-Command cmake -CommandType Application | Select-Object -First 1).Source
    $ctest = Join-Path (Split-Path -Parent $cmake) 'ctest.exe'
    $ninja = (Get-Command ninja -CommandType Application | Select-Object -First 1).Source
    $pwsh = (Get-Command pwsh -CommandType Application | Select-Object -First 1).Source
    $windowsPowerShell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
    $vswhere = Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'Microsoft Visual Studio/Installer/vswhere.exe'
    $vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if ($LASTEXITCODE -ne 0 -or -not $vs) { throw 'Native x64 MSVC prerequisites are missing' }
    $vsDevCmd = Join-Path ($vs | Select-Object -First 1) 'Common7/Tools/VsDevCmd.bat'
    . (Join-Path $repo 'build_fast_resources.ps1')
    Invoke-Checked $Python @('--version')
    Invoke-Checked $cmake @('--version')
    Invoke-Checked $ninja @('--version')
    Invoke-Checked $Python @('-c', 'import sys; sys.path.insert(0,"tools"); import psutil,package_portable; print("psutil",psutil.__version__); print("7-Zip",package_portable.find_seven_zip())')
    foreach ($shell in @($pwsh, $windowsPowerShell)) {
        Invoke-Checked $shell @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', 'tests/build_tools/test_fast_build.ps1')
    }
    Invoke-Checked $pwsh @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', 'tests/build_tools/test_package_cache.ps1', '-CMake', $cmake, '-Ninja', $ninja)
    Invoke-Checked $cmake @('-P', 'tests/build_tools/test_compiler_cache.cmake')
    Invoke-Checked $cmake @("-DTEST_BINARY_DIR=$output/openvdb-finder", '-DTEST_GENERATOR=Ninja', "-DTEST_MAKE_PROGRAM=$ninja", '-P', 'tests/build_tools/test_openvdb_finder.cmake')

    # Exercise the production cmd quoting/VS setup path with real Unicode and
    # shell metacharacters, without relying on cmd's incompatible argv quoting.
    $literal = 'literal %PATH% & ^ ! ' + [char]0x03a9 + ' ' + [char]0x6e2c + [char]0x8a66
    $quoteCommand = Join-Path $output 'command with spaces.cmd'
    New-FastWindowsCommandFile -Path $quoteCommand -VsDevCmd $vsDevCmd -Executable $cmake -ArgumentList @('-E', 'echo', $literal)
    Invoke-Guarded 'native-quoting' @($env:ComSpec, '/d', '/c', $quoteCommand)
    $quoteLog = Get-Content -LiteralPath (Join-Path $output 'native-quoting.log') -Raw -Encoding UTF8
    if (-not $quoteLog.Contains($literal)) { throw 'Native command quoting changed the payload' }

    # Native compilation stays limited to the small codec and standalone atlas
    # fixture; never configure the application or its dependency bundle.
    $decoderOutput = Join-Path $output 'native-codec'
    $compileCommand = Join-Path $output 'compile-codec.cmd'
    New-FastWindowsCommandFile -Path $compileCommand -VsDevCmd $vsDevCmd `
        -Executable (Join-Path $repo 'tools/build_stl_restore.cmd') -ArgumentList @($decoderOutput)
    Invoke-Guarded 'native-codec' @($env:ComSpec, '/d', '/c', $compileCommand)
    $env:PRUSA_STL_RESTORE_TEST_EXE = Join-Path $decoderOutput 'RestoreSTL.exe'
    if (-not (Test-Path -LiteralPath $env:PRUSA_STL_RESTORE_TEST_EXE -PathType Leaf)) { throw 'Native decoder was not produced' }
    $atlasBuild = Join-Path $output 'imgui-atlas'
    $atlasCommands = @(
        @{ Label = 'atlas-configure'; Arguments = @('-S', (Join-Path $repo 'tests/imgui'), '-B', $atlasBuild,
            '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release', "-DCMAKE_MAKE_PROGRAM=$ninja") },
        @{ Label = 'atlas-build'; Arguments = @('--build', $atlasBuild, '--parallel', '1') }
    )
    foreach ($atlas in $atlasCommands) {
        $commandFile = Join-Path $output ($atlas.Label + '.cmd')
        New-FastWindowsCommandFile -Path $commandFile -VsDevCmd $vsDevCmd -Executable $cmake -ArgumentList $atlas.Arguments
        Invoke-Guarded $atlas.Label @($env:ComSpec, '/d', '/c', $commandFile)
    }
    Invoke-Guarded 'atlas-regression' @($ctest, '--test-dir', $atlasBuild, '--output-on-failure', '--no-tests=error', '--timeout', '60', '-j', '1')
    Invoke-Guarded 'python-regressions' @($Python, '-B', '.github/ci/run_tool_tests.py', '--report', (Join-Path $output 'tests.json'))
    Write-Host 'PASS: tooling, native codec and atlas-lifetime fixtures. No application build, slicing/full-GUI parity, or geometry-kernel validation was performed.'
}
finally {
    [Environment]::SetEnvironmentVariable('PRUSA_STL_RESTORE_TEST_EXE', $previousDecoder)
    Pop-Location
    Stop-Transcript | Out-Null
}
