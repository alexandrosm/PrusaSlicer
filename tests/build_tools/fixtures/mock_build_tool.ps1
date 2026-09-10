# Used as both vswhere and the native command runner by test_fast_build.ps1.
# No native process, compiler, configure step, or build is launched.
if ($args[0] -eq '-latest') {
    $global:LASTEXITCODE = 0
    return $global:PrusaFastBuildTestState.Root
}

[void] $global:PrusaFastBuildTestState.Calls.Add([pscustomobject]@{
    Command = Get-Content -LiteralPath $args[-1] -Raw
    GuardArguments = @($args)
    ParallelLevel = [Environment]::GetEnvironmentVariable('CMAKE_BUILD_PARALLEL_LEVEL')
})
$global:LASTEXITCODE = if ($global:PrusaFastBuildTestState.Fail) { 7 } else { 0 }
