[CmdletBinding()]
param([Parameter(Mandatory)][string] $CMake, [Parameter(Mandatory)][string] $Ninja)
if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'Package-cache integration tests require PowerShell 7 (long-path-safe cmdlets)' }
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $repo 'build_fast_cache.ps1')
$scratch = Join-Path $PSScriptRoot ('.tmp-package-cache-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $scratch | Out-Null
$checks = 0
function Assert-True($Condition, [string] $Message) {
    if (-not $Condition) { throw $Message }
    $script:checks++
}
function Invoke-Fixture([string[]] $Arguments) {
    & $CMake @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "CMake fixture failed: $Arguments" }
}
try {
    $deps = Join-Path $scratch 'deps'
    $build = Join-Path $deps 'build'
    $prefix = Join-Path $build 'destdir/usr/local'
    foreach ($directory in @($deps, $build, (Join-Path $scratch 'cmake/modules'))) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    Write-FastUtf8File (Join-Path $deps 'CMakeLists.txt') '# fixture shared build input'
    Copy-Item -LiteralPath (Join-Path $repo 'deps/PackageCache.cmake') -Destination (Join-Path $deps 'PackageCache.cmake')
    foreach ($file in 'build_fast_cache.ps1', 'build_fast_package_cache.ps1', 'build_fast_prune_policy.ps1') {
        Copy-Item -LiteralPath (Join-Path $repo $file) -Destination $scratch
    }
    foreach ($name in @('A', 'B', 'C', 'Unused')) {
        $directory = Join-Path $deps ('+' + $name)
        New-Item -ItemType Directory -Path $directory | Out-Null
        Write-FastUtf8File (Join-Path $directory ($name + '.cmake')) "# recipe $name"
    }
    $selection = [pscustomobject]@{ packages = @(
        [pscustomobject]@{ name = 'A'; system = $false; depends = @() },
        [pscustomobject]@{ name = 'B'; system = $false; depends = @('A') },
        [pscustomobject]@{ name = 'C'; system = $false; depends = @() }) }
    $inputData = [ordered]@{ effectiveCMake = [ordered]@{ CMAKE_BUILD_TYPE = 'Release'; DEP_DEBUG = 'OFF' }
        tools = [ordered]@{}; environment = [ordered]@{}; platform = 'windows'; targetArchitecture = 'x64'
        prefixPolicy = 'fixture'; dependencyBuildDirectory = ConvertTo-FastNormalizedPath $build
        sourceDirectory = ConvertTo-FastNormalizedPath $scratch }
    $context = [pscustomobject]@{ InstallPrefix = $prefix; CanonicalInstallPrefix = ConvertTo-FastNormalizedPath $prefix
        DependencyBuildDirectory = $build; PackageSelectionPath = Join-Path $build '.fast-package-selection.json'
        PackageContexts = @{} }
    function Refresh-Keys {
        $context.PackageContexts = New-FastPackageContexts -Context $context -BuildInput $inputData -Selection $selection `
            -SourceDirectory $scratch -DependencyDirectory $deps
    }
    Refresh-Keys
    $originalKeys = $context.PackageContexts
    # Exercise the production context constructor with a compiler-free cache
    # fixture. Real CMake/Ninja/Git --version and file hashing are not mocked;
    # the fake compiler input is fingerprinted but is never executed.
    $contextBuild = Join-Path $deps 'context-build'
    $compilerDir = Join-Path $contextBuild 'CMakeFiles/fixture'
    New-Item -ItemType Directory -Path $compilerDir -Force | Out-Null
    $fakeCompiler = Join-Path $compilerDir 'cl.exe'
    $fakeDevCmd = Join-Path $compilerDir 'VsDevCmd.bat'
    Write-FastUtf8File $fakeCompiler 'not an executable; fingerprint-only fixture'
    Write-FastUtf8File $fakeDevCmd 'not invoked; fingerprint-only fixture'
    Write-FastUtf8File (Join-Path $compilerDir 'CMakeCXXCompiler.cmake') '# identification fixture'
    $git = (Get-Command git -CommandType Application | Select-Object -First 1).Source
    $fixtureCache = @(
        "PrusaSlicer_deps_DEP_INSTALL_PREFIX:PATH=$contextBuild/destdir/usr/local",
        'CMAKE_BUILD_TYPE:STRING=Release', 'DEP_DEBUG:BOOL=OFF',
        'PrusaSlicer_deps_ROOTS:STRING=A;B;C', "GIT_EXECUTABLE:FILEPATH=$git",
        "CMAKE_CXX_COMPILER:FILEPATH=$fakeCompiler", 'PrusaSlicer_deps_CACHE_SELECTION:FILEPATH='
    ) -join "`n"
    Write-FastUtf8File (Join-Path $contextBuild 'CMakeCache.txt') $fixtureCache
    $selectionDocument = [ordered]@{ schema = 1; packages = $selection.packages }
    Write-FastUtf8File (Join-Path $contextBuild 'dependency-selection.json') (ConvertTo-FastCanonicalJson $selectionDocument)
    $constructorArguments = @{ SourceDirectory = $scratch; DependencyDirectory = $deps
        DependencyBuildDirectory = $contextBuild; Profile = 'cli'; Preset = 'fixture'
        CMake = $CMake; Ninja = $Ninja; VsDevCmd = $fakeDevCmd }
    $productionContext = New-FastDependencyCacheContext @constructorArguments
    Assert-True ($productionContext.PackageContexts.Count -eq 3) 'Production context did not generate package contexts'
    Assert-True ($productionContext.CanonicalInstallPrefix -eq (ConvertTo-FastNormalizedPath "$contextBuild/destdir/usr/local")) 'Production context changed prefix identity'
    Write-FastUtf8File (Join-Path $contextBuild 'CMakeCache.txt') ($fixtureCache + "`nLibBGCode_SOURCE_DIR:PATH=$scratch/local-libbgcode`n")
    $localOverrideRejected = $false
    try { New-FastDependencyCacheContext @constructorArguments | Out-Null }
    catch { $localOverrideRejected = $_.Exception.Message -match 'Dependency caches disabled: LibBGCode_SOURCE_DIR' }
    Assert-True $localOverrideRejected 'Local-source override obtained a publishable/restorable cache context'
    Write-FastUtf8File (Join-Path $contextBuild 'CMakeCache.txt') $fixtureCache
    $originalClosure = Get-FastFileSetMetadata -Root $scratch -Files @(Get-FastDependencyRecipeFiles $scratch $deps -Packages A,B,C)
    Write-FastUtf8File (Join-Path $deps '+Unused/Unused.cmake') '# unrelated recipe edit'
    $newClosure = Get-FastFileSetMetadata -Root $scratch -Files @(Get-FastDependencyRecipeFiles $scratch $deps -Packages A,B,C)
    Assert-True ($originalClosure.Sha256 -eq $newClosure.Sha256) 'Unselected recipe invalidated closure'
    $afterUnselected = New-FastDependencyCacheContext @constructorArguments
    Assert-True ($productionContext.Fingerprint -eq $afterUnselected.Fingerprint) 'Unselected recipe invalidated production whole-prefix key'
    # Changing the selected set changes the bundle, not unchanged package keys.
    $selectionDocument.packages = @($selection.packages) + @([pscustomobject]@{ name = 'Unused'; system = $false; depends = @() })
    Write-FastUtf8File (Join-Path $contextBuild 'dependency-selection.json') (ConvertTo-FastCanonicalJson $selectionDocument)
    $expandedContext = New-FastDependencyCacheContext @constructorArguments
    Assert-True ($expandedContext.Fingerprint -ne $productionContext.Fingerprint) 'Selected graph did not invalidate bundle'
    Assert-True ($expandedContext.PackageContexts.A.Fingerprint -eq $productionContext.PackageContexts.A.Fingerprint) 'Selected set invalidated unchanged package A'
    Write-FastUtf8File (Join-Path $deps 'DependencyGraph.cmake') '# unrelated edge change outside selected graph'
    Refresh-Keys
    Assert-True ($context.PackageContexts.A.Fingerprint -eq $originalKeys.A.Fingerprint) 'Global graph bytes invalidated package key'
    $fixture = Join-Path $PSScriptRoot 'package_cache_fixture'
    $module = Join-Path $repo 'deps/PackageCache.cmake'
    $firstMarkers = Join-Path $scratch 'first-markers'
    New-Item -ItemType Directory -Path $firstMarkers | Out-Null
    Invoke-Fixture @('-S', $fixture, '-B', $build, '-G', 'Ninja', "-DCMAKE_MAKE_PROGRAM=$Ninja",
        "-DPrusaSlicer_deps_DEP_INSTALL_PREFIX=$prefix", "-DCACHE_MODULE=$module", "-DBUILD_MARKER=$firstMarkers")
    Invoke-Fixture @('--build', $build, '--target', 'deps', '--parallel', '1')
    Assert-True (@(Get-ChildItem -LiteralPath $firstMarkers -File).Count -eq 3) 'Initial fake packages did not all build'
    Optimize-FastDependencyPrefix -Prefix $prefix -AllowedRoot $scratch
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $prefix 'bin/cjpeg.exe'))) 'Known SDK CLI was not pruned'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $prefix 'include/boost-1_83/boost/json/unused.hpp'))) 'Known unused Boost header was not pruned'
    Assert-True (Test-Path -LiteralPath (Join-Path $prefix 'share/doc/libjpeg-turbo/LICENSE.md')) 'Pruning removed license'
    $fixtureA = Join-Path $prefix 'lib/libA.lib'
    $fixtureAContents = Get-Content -LiteralPath $fixtureA -Raw
    Assert-FastPathWithin -Path $fixtureA -Root $scratch
    Remove-Item -LiteralPath $fixtureA
    Publish-FastPackageArtifacts $context
    Assert-True (-not (Test-Path -LiteralPath $context.PackageContexts.A.Entry)) 'Unexpected missing install file was silently accepted'
    Write-FastUtf8File $fixtureA $fixtureAContents
    Publish-FastPackageArtifacts $context
    foreach ($name in 'A', 'B', 'C') {
        Assert-True ($null -ne (Get-FastPackageManifest $context $context.PackageContexts[$name])) "Missing artifact $name"
    }
    Assert-True (@((Get-FastPackageManifest $context $context.PackageContexts.C).files).Count -eq 2) 'Prune-policy ownership did not retain exactly library and license'
    Write-FastUtf8File (Join-Path $deps '+C/C.cmake') '# changed independent recipe'
    Refresh-Keys
    Assert-True ($context.PackageContexts.A.Fingerprint -eq $originalKeys.A.Fingerprint) 'Independent A invalidated'
    Assert-True ($context.PackageContexts.B.Fingerprint -eq $originalKeys.B.Fingerprint) 'Independent B invalidated'
    Assert-True ($context.PackageContexts.C.Fingerprint -ne $originalKeys.C.Fingerprint) 'Changed C not invalidated'
    # This is a disposable fixture build, not the application's build directory.
    Remove-FastTemporaryDirectory -Path $build -AllowedRoot $scratch
    New-Item -ItemType Directory -Path $build | Out-Null
    Assert-True (Restore-FastPackageArtifacts $context) 'Partial restore failed'
    Assert-True (Test-Path -LiteralPath (Join-Path $prefix 'lib/libA.lib')) 'A payload absent'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $prefix 'lib/libC.lib'))) 'Changed C restored incorrectly'
    $secondMarkers = Join-Path $scratch 'second-markers'
    New-Item -ItemType Directory -Path $secondMarkers | Out-Null
    Invoke-Fixture @('-S', $fixture, '-B', $build, '-G', 'Ninja', "-DCMAKE_MAKE_PROGRAM=$Ninja",
        "-DPrusaSlicer_deps_DEP_INSTALL_PREFIX=$prefix", "-DCACHE_MODULE=$module", "-DBUILD_MARKER=$secondMarkers",
        "-DPrusaSlicer_deps_CACHE_SELECTION=$($context.PackageSelectionPath)")
    Invoke-Fixture @('--build', $build, '--target', 'deps', '--parallel', '1')
    Assert-True ((@(Get-ChildItem -LiteralPath $secondMarkers -File).Name -join ',') -eq 'C.built') 'Cached A/B rebuilt or changed C did not rebuild'
    & $CMake -S $fixture -B $build "-DLibBGCode_SOURCE_DIR=$scratch/local-libbgcode" *> $null
    Assert-True ($LASTEXITCODE -ne 0) 'Direct CMake cache selection accepted local-source override'
    Invoke-Fixture @('-S', $fixture, '-B', $build, '-DLibBGCode_SOURCE_DIR=')
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $build 'builds/A/install_manifest.txt'))) 'Cached target fabricated an install manifest'
    Assert-True (-not (Restore-FastPackageArtifacts $context)) 'Restore overwrote nonempty prefix'
    $installedA = Join-Path $prefix 'lib/libA.lib'
    Write-FastUtf8File $installedA 'modified installed package'
    & $CMake --build $build --target dep_A --parallel 1 *> $null
    Assert-True ($LASTEXITCODE -ne 0) 'Build target accepted modified installed payload'
    Copy-Item -LiteralPath (Join-Path $originalKeys.A.Entry 'payload/lib/libA.lib') -Destination $installedA
    # Reconfigure B as a cache hit with A explicitly source-built. A's own cache
    # target must not accidentally mask a failure in B's transitive validation.
    $sourceChildSelection = Get-Content -LiteralPath $context.PackageSelectionPath -Raw | ConvertFrom-Json
    $sourceChildSelection.hits.PSObject.Properties.Remove('A')
    Write-FastUtf8File $context.PackageSelectionPath (ConvertTo-FastCanonicalJson $sourceChildSelection)
    Invoke-Fixture @('-S', $fixture, '-B', $build)
    Assert-True (Test-Path -LiteralPath (Join-Path $build 'dep_A-prefix/tmp/dep_A-cfgcmd.txt')) 'Non-hit child did not get a real ExternalProject'
    $beforeChildEdit = $context.PackageContexts
    Write-FastUtf8File (Join-Path $deps '+A/A.cmake') '# changed transitive dependency'
    Refresh-Keys
    Assert-True ($context.PackageContexts.A.Fingerprint -ne $beforeChildEdit.A.Fingerprint) 'A edit not invalidated'
    Assert-True ($context.PackageContexts.B.Fingerprint -ne $beforeChildEdit.B.Fingerprint) 'A edit did not invalidate dependent B'
    Assert-True ($context.PackageContexts.C.Fingerprint -eq $beforeChildEdit.C.Fingerprint) 'A edit invalidated independent C'
    & $CMake -S $fixture -B $build *> $null
    Assert-True ($LASTEXITCODE -ne 0) 'Cached B accepted edited source-built child A during configure'
    & $CMake "-DPrusaSlicer_deps_DEP_INSTALL_PREFIX=$prefix" "-DPRUSASLICER_CACHE_SELECTION=$($context.PackageSelectionPath)" `
        -DPRUSASLICER_CACHE_PACKAGE=B -DPRUSASLICER_CACHE_BUILD_CHECK=ON -P $module *> $null
    Assert-True ($LASTEXITCODE -ne 0) 'Cached B accepted edited source-built child A during build validation'
    $beforeAbiEdit = $context.PackageContexts.C.Fingerprint
    $inputData.effectiveCMake['CMAKE_CXX_FLAGS_RELEASE'] = '/O1'
    Refresh-Keys
    Assert-True ($context.PackageContexts.C.Fingerprint -ne $beforeAbiEdit) 'ABI/options change reused artifact'
    $oldPrefix = $context.CanonicalInstallPrefix
    $beforePrefixEdit = $context.PackageContexts.C.Fingerprint
    $context.CanonicalInstallPrefix = $oldPrefix + '-different'
    Refresh-Keys
    Assert-True ($context.PackageContexts.C.Fingerprint -ne $beforePrefixEdit) 'Different prefix reused artifact'
    $context.CanonicalInstallPrefix = $oldPrefix
    $inputData.effectiveCMake['BUILD_SHARED_LIBS'] = 'ON'
    Refresh-Keys
    Assert-True ($context.PackageContexts.Count -eq 0) 'Unsupported shared-library build received package artifacts'
    $inputData.effectiveCMake.Remove('BUILD_SHARED_LIBS')
    Refresh-Keys
    $beforePolicyEdit = $context.PackageContexts.C.Fingerprint
    Write-FastUtf8File (Join-Path $scratch 'build_fast_prune_policy.ps1') '# changed prune policy fixture'
    Refresh-Keys
    Assert-True ($context.PackageContexts.C.Fingerprint -ne $beforePolicyEdit) 'Prune policy change did not invalidate package'
    $artifact = $originalKeys.C
    $payload = Join-Path $artifact.Entry 'payload/lib/libC.lib'
    Write-FastUtf8File $payload 'corrupt'
    Assert-True ($null -eq (Get-FastPackageManifest $context $artifact)) 'Corrupt artifact accepted'
    Assert-True ((Get-Content -LiteralPath (Join-Path $prefix 'lib/libC.lib') -Raw) -match 'fixture library') 'Artifact mutation affected installed prefix'
    foreach ($system in 'Windows', 'Linux') {
        Invoke-Fixture @('-S', (Join-Path $PSScriptRoot 'dependency_graph_fixture'), '-B', (Join-Path $scratch ('graph-' + $system)),
            '-G', 'Ninja', "-DCMAKE_MAKE_PROGRAM=$Ninja", "-DGRAPH_SYSTEM=$system")
        Assert-True $true "$system dependency graph contract"
    }
    & $CMake -S (Join-Path $PSScriptRoot 'dependency_graph_fixture') -B (Join-Path $scratch 'graph-missing') `
        -G Ninja "-DCMAKE_MAKE_PROGRAM=$Ninja" -DFAIL_MISSING=ON *> $null
    Assert-True ($LASTEXITCODE -ne 0) 'Unresolved non-system graph edge was accepted'
    Write-Host "PASS: $checks package-cache checks; compiler-free ExternalProjects built A/B/C, then only C after partial reuse."
}
finally { Remove-FastTemporaryDirectory -Path $scratch -AllowedRoot $PSScriptRoot }
