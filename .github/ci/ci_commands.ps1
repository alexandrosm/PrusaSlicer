# Shared CI-only command capture. Dot-source, then initialize with a fresh output.
function Initialize-CiReporting {
    param([string] $Python, [string] $Repository, [string] $OutputDirectory)
    $script:ciPython = $Python
    $script:ciRepository = $Repository
    $script:ciOutput = [IO.Path]::GetFullPath($OutputDirectory)
    $script:ciReportRoot = Join-Path $script:ciOutput 'reports'
    $script:ciCommandIndex = 0
    if (Test-Path -LiteralPath $script:ciOutput) { throw 'CI output must be fresh; refusing to overwrite' }
    New-Item -ItemType Directory -Path (Join-Path $script:ciReportRoot 'commands') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $script:ciReportRoot 'guards') | Out-Null
}

function Invoke-Checked([string] $Executable, [string[]] $Arguments, [string] $Label = '') {
    $script:ciCommandIndex++
    if (-not $Label) { $Label = [IO.Path]::GetFileNameWithoutExtension($Executable) }
    $name = '{0:d2}-{1}' -f $script:ciCommandIndex, $Label
    Write-Host "[CI fixture] $name`: $Executable $($Arguments -join ' ')"
    & $script:ciPython -B (Join-Path $script:ciRepository '.github/ci/capture_command.py') `
        --report (Join-Path $script:ciReportRoot "commands/$name.json") --label $name -- $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Fixture failed ($LASTEXITCODE): $name" }
}

function Invoke-Guarded([string] $Label, [string[]] $Command) {
    # Explicit reserves for ephemeral CI, not the user's desktop. This remains
    # a sampled stopping threshold, not a hard allocation or CPU-time quota.
    Invoke-Checked $script:ciPython (@('-B', (Join-Path $script:ciRepository 'tools/run_guarded.py'),
        '--log-dir', (Join-Path $script:ciReportRoot 'guards'), '--label', $Label,
        '--memory-gib', '2', '--minimum-free-gib', '2', '--minimum-commit-gib', '2',
        '--launch-headroom-gib', '0.5', '--cpu-count', '1', '--timeout-seconds', '240', '--') + $Command) $Label
}

function Complete-CiReporting([string] $Lane, [bool] $Succeeded) {
    $status = if ($Succeeded) { 'passed' } else { 'failed' }
    & $script:ciPython -B (Join-Path $script:ciRepository '.github/ci/ci_summary.py') `
        --output $script:ciOutput --lane $Lane --status $status
    if ($LASTEXITCODE -ne 0) { throw 'CI summary generation failed' }
}
