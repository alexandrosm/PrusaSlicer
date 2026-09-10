# Real tiny geometry fixtures, not a full mesh corpus transformation.
[CmdletBinding()]
param([Parameter(Mandatory)][string] $Python, [string] $OutputDirectory = '')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$output = if ($OutputDirectory) { [IO.Path]::GetFullPath($OutputDirectory) } else { Join-Path $repo 'out/ci-geometry' }
. (Join-Path $PSScriptRoot 'ci_commands.ps1')
Initialize-CiReporting -Python $Python -Repository $repo -OutputDirectory $output
$succeeded = $false
$previousThreads = @{}
foreach ($name in 'OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS') {
    $previousThreads[$name] = [Environment]::GetEnvironmentVariable($name)
    [Environment]::SetEnvironmentVariable($name, '1')
}
Push-Location -LiteralPath $repo
try {
    Invoke-Checked $Python @('-B', '.github/ci/run_tool_tests.py', '--check-inventory') 'inventory'
    Invoke-Checked $Python @('--version') 'python-version'
    Invoke-Checked $Python @('-m', 'pip', 'freeze', '--disable-pip-version-check') 'installed-packages'
    Invoke-Guarded 'geometry-regressions' @($Python, '-B', '.github/ci/run_tool_tests.py', '--lane', 'geometry',
        '--report', (Join-Path $output 'reports/tests.json'))
    $succeeded = $true
}
finally {
    foreach ($name in $previousThreads.Keys) { [Environment]::SetEnvironmentVariable($name, $previousThreads[$name]) }
    Pop-Location
    Complete-CiReporting 'geometry' $succeeded
}
