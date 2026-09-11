# Pure scratch fixtures; no CMake/compiler/process/network invocation.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'test-cmake-handoff.ps1')
$scratch = Join-Path $PSScriptRoot ('.tmp-cmake-handoff-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $scratch | Out-Null
$checks = 0
function Check($Condition, [string] $Message) { if (-not $Condition) { throw $Message }; $script:checks++ }
function Reject([scriptblock] $Action, [string] $Pattern) {
    $rejected = $false
    try { & $Action | Out-Null } catch { $rejected = $_.Exception.Message -match $Pattern }
    Check $rejected "Expected refusal: $Pattern"
}
try {
    $build = Join-Path $scratch 'configured'
    New-Item -ItemType Directory -Path $build | Out-Null
    $native = Join-Path $scratch 'native-cmake.exe'
    Write-FastUtf8File $native 'not executable; hash-only fixture, never invoked'
    Write-FastUtf8File (Join-Path $build 'CMakeCache.txt') "CMAKE_COMMAND:INTERNAL=$native`n"
    $inputData = [ordered]@{ schema = 1; profile = 'lean-release'; preset = 'lean-release'
        dependencyBuildDirectory = ConvertTo-FastNormalizedPath $build
        effectiveCMake = [ordered]@{ TEST_ABI = 'same' }
        tools = [ordered]@{ cmake = Get-FastToolRecord -Path $native -Version 'fixture version' } }
    $prepared = Join-Path $scratch 'prepared.json'
    $canonical = ConvertTo-FastCanonicalJson $inputData
    # Prepare's Set-Content adds an extra newline; canonical hashing must match.
    Write-FastUtf8File $prepared ($canonical + "`n")
    $key = 'release-prefix-v1-' + (Get-FastTextSha256 $canonical)
    $proof = Join-Path $scratch 'proof'
    $arguments = @{ PrepareReport = $prepared; ProductionKey = $key; ProofDirectory = $proof }
    $published = Invoke-CMakeHandoff @arguments -Operation Publish -OutputReport (Join-Path $scratch 'publish.json')
    Check ($published.status -eq 'passed') 'Publish failed'
    Check (@(Get-ChildItem -LiteralPath $proof -File).Count -eq 3) 'Proof must contain exactly three tiny files'
    $verified = Invoke-CMakeHandoff @arguments -Operation Verify -OutputReport (Join-Path $scratch 'verify.json')
    Check ($verified.status -eq 'passed' -and $verified.changed_fields.Count -eq 0) 'Identical independent inputs rejected'
    Check ($published.marker_sha256 -eq $verified.marker_sha256) 'Marker did not survive transport proof'
    $unexpected = Join-Path $proof 'unexpected.txt'
    Write-FastUtf8File $unexpected 'must not be transported'
    Reject { Invoke-CMakeHandoff @arguments -Operation Verify -OutputReport (Join-Path $scratch 'extra.json') } 'Unexpected transported'
    Remove-Item -LiteralPath $unexpected
    $transportedInput = Join-Path $proof 'input.json'
    Write-FastUtf8File $transportedInput ($canonical + "`n")
    Reject { Invoke-CMakeHandoff @arguments -Operation Verify -OutputReport (Join-Path $scratch 'input-corrupt.json') } 'canonical input hash changed'
    Write-FastUtf8File $transportedInput $canonical
    Reject { Invoke-CMakeHandoff @arguments -Operation Publish -OutputReport (Join-Path $scratch 'duplicate.json') } 'overwrite existing handoff proof'
    Reject { Invoke-CMakeHandoff @arguments -Operation Verify -OutputReport (Join-Path $scratch 'verify.json') } 'overwrite handoff report'
    Reject { Invoke-CMakeHandoff @arguments -Operation Verify -OutputReport (Join-Path $proof 'diagnostic.json') } 'outside the transported'
    $marker = Join-Path $proof 'marker.bin'
    $originalMarker = [IO.File]::ReadAllBytes($marker)
    Write-FastUtf8File $marker 'corruption'
    Reject { Invoke-CMakeHandoff @arguments -Operation Verify -OutputReport (Join-Path $scratch 'corrupt.json') } 'payload hash/length'
    [IO.File]::WriteAllBytes($marker, $originalMarker)
    $inputData.effectiveCMake.TEST_ABI = 'different'
    $different = ConvertTo-FastCanonicalJson $inputData
    Write-FastUtf8File $prepared $different
    $arguments.ProductionKey = 'release-prefix-v1-' + (Get-FastTextSha256 $different)
    Reject { Invoke-CMakeHandoff @arguments -Operation Verify -OutputReport (Join-Path $scratch 'different.json') } 'production inputs differ'
    $difference = Read-HandoffJson (Join-Path $scratch 'different.json')
    Check ($difference.changed_fields.Count -eq 1 -and $difference.changed_fields[0].field -eq '$.effectiveCMake.TEST_ABI') 'Changed input field was not reported exactly'
    $arguments.ProductionKey = $key
    Reject { Invoke-CMakeHandoff @arguments -Operation Verify -OutputReport (Join-Path $scratch 'wrong-key.json') } 'key does not match'
    Write-FastUtf8File $prepared $canonical
    Write-FastUtf8File (Join-Path $build 'CMakeCache.txt') "CMAKE_COMMAND:INTERNAL=$scratch/other-cmake.exe`n"
    Reject { Invoke-CMakeHandoff @arguments -Operation Verify -OutputReport (Join-Path $scratch 'shim.json') } 'not configured CMAKE_COMMAND'
    Write-FastUtf8File (Join-Path $build 'CMakeCache.txt') "CMAKE_COMMAND:INTERNAL=$native`n"
    Write-FastUtf8File $native 'changed binary'
    Reject { Invoke-CMakeHandoff @arguments -Operation Verify -OutputReport (Join-Path $scratch 'changed-tool.json') } 'CMake changed since Prepare'
    $arrayChanges = @(Compare-HandoffInputs ([ordered]@{ item = @(1, 2); empty = @{} }) ([ordered]@{ item = @(1); empty = @() }))
    Check ($arrayChanges.Count -ge 3) 'Array/type/missing fields were not reported'
    Write-Host "PASS: $checks handoff proof checks; no compiler/process/network work"
}
finally { Remove-FastTemporaryDirectory -Path $scratch -AllowedRoot $PSScriptRoot }
