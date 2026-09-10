# The argument tests disable the dependency cache. Do not touch real prefixes.
function Optimize-FastDependencyPrefix {
    param([string] $Prefix, [string] $AllowedRoot)
}
function Read-FastCMakeCache { param([string] $Path); return $global:PrusaFastBuildTestState.Settings }
function New-FastDependencyCacheContext {
    param($SourceDirectory, $DependencyDirectory, $DependencyBuildDirectory, $Profile, $Preset, $CMake, $Ninja, $VsDevCmd)
    return [pscustomobject]@{PackageSelectionPath=(Join-Path $DependencyBuildDirectory 'verified-packages.json')}
}
function Restore-FastDependencyCache { param($Context, $CMake); return 'partial' }
function Publish-FastDependencyCache { param($Context, $CMake) }
