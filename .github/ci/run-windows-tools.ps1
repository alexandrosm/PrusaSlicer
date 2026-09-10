# Disposable hosted-runner fixtures only; not a full application build/release.
[CmdletBinding()]
param([Parameter(Mandatory)][string] $Python, [string] $OutputDirectory = '')

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$output = if ($OutputDirectory) { [IO.Path]::GetFullPath($OutputDirectory) } else { Join-Path $repo 'out/ci-streamlining' }
. (Join-Path $PSScriptRoot 'ci_commands.ps1')
Initialize-CiReporting -Python $Python -Repository $repo -OutputDirectory $output
$succeeded = $false

$previousDecoder = [Environment]::GetEnvironmentVariable('PRUSA_STL_RESTORE_TEST_EXE')
Push-Location -LiteralPath $repo
try {
    Invoke-Checked $Python @('-B', '.github/ci/run_tool_tests.py', '--check-inventory') 'inventory'
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
    Invoke-Checked $Python @('--version') 'python-version'
    Invoke-Checked $cmake @('--version') 'cmake-version'
    Invoke-Checked $ninja @('--version') 'ninja-version'
    Invoke-Checked $Python @('-c', 'import sys; sys.path.insert(0,"tools"); import psutil,package_portable; print("psutil",psutil.__version__); z=package_portable.find_seven_zip(); print("7-Zip",z); print(package_portable.run_seven_zip(z,["i"]).splitlines()[0])') 'runtime-prerequisites'
    foreach ($shell in @($pwsh, $windowsPowerShell)) {
        Invoke-Checked $shell @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', 'tests/build_tools/test_fast_build.ps1') ('wrapper-' + [IO.Path]::GetFileNameWithoutExtension($shell))
    }
    Invoke-Checked $pwsh @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', 'tests/build_tools/test_package_cache.ps1', '-CMake', $cmake, '-Ninja', $ninja) 'package-cache'
    Invoke-Checked $cmake @('-P', 'tests/build_tools/test_compiler_cache.cmake') 'compiler-cache-policy'
    Invoke-Checked $cmake @("-DTEST_BINARY_DIR=$output/openvdb-finder", '-DTEST_GENERATOR=Ninja', "-DTEST_MAKE_PROGRAM=$ninja", '-P', 'tests/build_tools/test_openvdb_finder.cmake') 'openvdb-finder'

    # Configure-only language/policy contracts for all six bundled wrappers;
    # placeholder providers deliberately do not establish real dependency ABI.
    $wrapperCommand = Join-Path $output 'reports/commands/bundled-wrappers.cmd'
    $wrapperArguments = @("-DTEST_BINARY_DIR=$output/bundled-wrappers", '-DTEST_GENERATOR=Ninja',
        "-DTEST_MAKE_PROGRAM=$ninja", '-P', 'tests/build_tools/test_bundled_wrappers.cmake')
    New-FastWindowsCommandFile -Path $wrapperCommand -VsDevCmd $vsDevCmd -Executable $cmake -ArgumentList $wrapperArguments
    Invoke-Guarded 'bundled-wrappers' @($env:ComSpec, '/d', '/c', $wrapperCommand)
    foreach ($provider in 'bundled', 'system') {
        Copy-Item -LiteralPath (Join-Path $output "bundled-wrappers/$provider.log") `
            -Destination (Join-Path $output "reports/commands/bundled-wrappers-$provider.log")
    }
    # Only these two targets have no placeholder-dependent includes/libraries.
    # Never build ALL, Boost-dependent admesh, or avrdude against fixture stubs.
    $componentCommand = Join-Path $output 'reports/commands/bundled-components.cmd'
    New-FastWindowsCommandFile -Path $componentCommand -VsDevCmd $vsDevCmd -Executable $cmake `
        -ArgumentList @('--build', (Join-Path $output 'bundled-wrappers/bundled'),
            '--target', 'miniz_static', 'glu-libtess', '--parallel', '1')
    Invoke-Guarded 'bundled-components' @($env:ComSpec, '/d', '/c', $componentCommand)
    $componentArtifacts = foreach ($relative in 'miniz/miniz_static.lib', 'glu-libtess/glu-libtess.lib') {
        $artifact = Get-Item -LiteralPath (Join-Path $output "bundled-wrappers/bundled/$relative")
        if ($artifact.Length -le 0) { throw "Standalone component did not produce a nonempty archive: $relative" }
        [ordered]@{ relative_path = $relative; bytes = $artifact.Length;
            sha256 = (Get-FileHash -LiteralPath $artifact.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
    }
    $componentArtifacts | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $output 'reports/bundled-component-artifacts.json') -Encoding UTF8

    # Exercise the production cmd quoting/VS setup path with real Unicode and
    # shell metacharacters, without relying on cmd's incompatible argv quoting.
    $literal = 'literal %PATH% & ^ ! ' + [char]0x03a9 + ' ' + [char]0x6e2c + [char]0x8a66
    $quoteCommand = Join-Path $output 'reports/commands/command with spaces.cmd'
    New-FastWindowsCommandFile -Path $quoteCommand -VsDevCmd $vsDevCmd -Executable $cmake -ArgumentList @('-E', 'echo', $literal)
    Invoke-Guarded 'native-quoting' @($env:ComSpec, '/d', '/c', $quoteCommand)
    $quoteLog = Get-Content -LiteralPath (Join-Path $output 'reports/guards/native-quoting.log') -Raw -Encoding UTF8
    if (-not $quoteLog.Contains($literal)) { throw 'Native command quoting changed the payload' }

    # Native compilation stays limited to standalone components and fixtures;
    # never configure the application or its dependency bundle.
    $decoderOutput = Join-Path $output 'native-codec'
    $compileCommand = Join-Path $output 'reports/commands/compile-codec.cmd'
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
        $commandFile = Join-Path $output ('reports/commands/' + $atlas.Label + '.cmd')
        New-FastWindowsCommandFile -Path $commandFile -VsDevCmd $vsDevCmd -Executable $cmake -ArgumentList $atlas.Arguments
        Invoke-Guarded $atlas.Label @($env:ComSpec, '/d', '/c', $commandFile)
    }
    Invoke-Guarded 'atlas-regression' @($ctest, '--test-dir', $atlasBuild, '--output-on-failure', '--no-tests=error', '--timeout', '60', '-j', '1')
    Invoke-Guarded 'python-regressions' @($Python, '-B', '.github/ci/run_tool_tests.py', '--report', (Join-Path $output 'reports/tests.json'))
    $succeeded = $true
    Write-Host 'PASS: tooling, native codec and atlas-lifetime fixtures. No application build, slicing/full-GUI parity, or geometry-kernel validation was performed.'
}
finally {
    [Environment]::SetEnvironmentVariable('PRUSA_STL_RESTORE_TEST_EXE', $previousDecoder)
    Pop-Location
    Complete-CiReporting 'tooling' $succeeded
}
