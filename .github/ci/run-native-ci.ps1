# Bootstrap the verified prebuilt and VS environment for tiny hosted fixtures.
[CmdletBinding()]
param([Parameter(Mandatory)][string] $Python)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted') {
    throw 'Native CI bootstrap is restricted to disposable hosted runners'
}
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$bootstrap = Join-Path $repo 'out/ci-native-bootstrap'
if (Test-Path -LiteralPath $bootstrap) { throw 'Native bootstrap output must be fresh' }
New-Item -ItemType Directory -Path $bootstrap | Out-Null
$archive = Join-Path $bootstrap 'sccache.zip'
Invoke-WebRequest 'https://github.com/mozilla/sccache/releases/download/v0.17.0/sccache-v0.17.0-x86_64-pc-windows-msvc.zip' `
    -OutFile $archive -TimeoutSec 120
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne
    'e94cfc5b58cbe439302f586c1d1bd7980c2cd371d47bdf385ade657411e6f3ac') {
    throw 'Official sccache prebuilt failed SHA-256 verification'
}
Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $bootstrap 'sccache')
$sccache = @(Get-ChildItem -LiteralPath (Join-Path $bootstrap 'sccache') -Filter sccache.exe -Recurse -File)
if ($sccache.Count -ne 1) { throw 'Unexpected sccache archive layout' }
$vswhere = Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'Microsoft Visual Studio/Installer/vswhere.exe'
$vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if ($LASTEXITCODE -ne 0 -or -not $vs) { throw 'x64 MSVC is required' }
$vsDevCmd = Join-Path ($vs | Select-Object -First 1) 'Common7/Tools/VsDevCmd.bat'
. (Join-Path $repo 'build_fast_resources.ps1')
$command = Join-Path $bootstrap 'mechanisms.cmd'
$pwsh = (Get-Command pwsh -CommandType Application | Select-Object -First 1).Source
New-FastWindowsCommandFile -Path $command -VsDevCmd $vsDevCmd -Executable $pwsh -ArgumentList @(
    '-NoLogo', '-NoProfile', '-NonInteractive', '-File', (Join-Path $PSScriptRoot 'run-native-mechanisms.ps1'),
    '-Python', $Python, '-Sccache', $sccache[0].FullName, '-Output', (Join-Path $repo 'out/ci-mechanisms'))
& $env:ComSpec /d /c $command
if ($LASTEXITCODE -ne 0) { throw 'Native mechanism proofs failed' }
