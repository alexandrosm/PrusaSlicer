# Native Windows smoke: VS environment setup + cmake -E echo, never compilation.
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $VsDevCmd,
    [Parameter(Mandatory)] [string] $CMake,
    [string] $Python = 'python'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repository = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $repository 'build_fast_resources.ps1')
$scratch = Join-Path $repository ('out/cmd quote smoke ' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($scratch) | Out-Null
$script = Join-Path $scratch 'command with spaces.cmd'
# Non-ASCII text is built from code points so the test itself also works when
# Windows PowerShell 5.1 reads this UTF-8-without-BOM source in an ANSI locale.
$literal = 'literal %PATH% & ^ ! ' + [char]0x03a9 + ' ' + [char]0x6e2c + [char]0x8a66
New-FastWindowsCommandFile -Path $script -VsDevCmd $VsDevCmd -Executable $CMake -ArgumentList @('-E', 'echo', $literal)
& $Python -B (Join-Path $repository 'tools/run_guarded.py') --log-dir $scratch --label quoting `
    --memory-gib 1 --minimum-free-gib 6 --minimum-commit-gib 6 --launch-headroom-gib 1 `
    --cpu-count 1 --timeout-seconds 30 -- $env:ComSpec /d /c $script
if ($LASTEXITCODE -ne 0) { throw 'Native guarded command smoke failed' }
$output = Get-Content -LiteralPath (Join-Path $scratch 'quoting.log') -Raw -Encoding UTF8
if (-not $output.Contains($literal)) { throw "Windows argument quoting changed the literal payload; see $scratch" }
Write-Host "PASS: real guarded cmd/VS environment with spaces, percent, shell metacharacters and Unicode; artifacts: $scratch"
