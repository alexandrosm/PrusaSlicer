# Immutable, package-granular cache. Dot-sourced by build_fast_cache.ps1.
# Same absolute prefix/toolchain only. Missing ownership evidence => no artifact.

function Get-FastInputRecords {
    param([object[]] $Files)
    $records = @($Files | ForEach-Object {
        $item = if ($_ -is [IO.FileInfo]) { $_ } else { Get-Item -LiteralPath $_ }
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked package input is unsupported' }
        [ordered]@{ path = $item.FullName.Replace('\', '/'); sha256 = Get-FastFileSha256 $item.FullName }
    } | Sort-Object { $_.path })
    return $records
}

function New-FastPackageContexts {
    param($Context, $BuildInput, $Selection, [string] $SourceDirectory, [string] $DependencyDirectory)
    $result = @{}
    # Windows PowerShell 5.1 cmdlets can copy a short staging path and then fail
    # to read the same files after its immutable 64-character key rename. Keep
    # its existing whole-prefix cache behavior, but never publish partial entries.
    if ($PSVersionTable.PSVersion.Major -lt 7) { return $result }
    # External/system-provided dependencies need their own ABI fingerprint; do
    # not guess one. Release-only Windows wrapper is the supported first version.
    if ($BuildInput.effectiveCMake['CMAKE_BUILD_TYPE'] -ne 'Release' -or
        $BuildInput.effectiveCMake['DEP_DEBUG'] -match '^(ON|TRUE|1)$' -or
        $BuildInput.effectiveCMake['BUILD_SHARED_LIBS'] -match '^(ON|TRUE|1)$' -or
        @($Selection.packages | Where-Object { $_.system }).Count) { return $result }
    $effective = [ordered]@{}
    foreach ($name in $BuildInput.effectiveCMake.Keys) {
        if ($name -match '^PrusaSlicer_deps_(ROOTS|SELECT_.*|PACKAGE_EXCLUDES|PLATFORM_PACKAGES)$') { continue }
        $effective[$name] = $BuildInput.effectiveCMake[$name]
    }
    $commonFiles = @(Get-FastDependencyRecipeFiles -SourceDirectory $SourceDirectory `
        -DependencyDirectory $DependencyDirectory -CommonOnly)
    $commonInputs = @(Get-FastInputRecords $commonFiles)
    # Tool versions/options remain in each key, and file hashes are also checked
    # by CMake when it consumes a restored package selection.
    foreach ($tool in $BuildInput.tools.Values) {
        if ($tool.Contains('path') -and $tool.Contains('sha256')) {
            $commonInputs += [ordered]@{ path = $tool.path; sha256 = $tool.sha256 }
        }
    }
    $pending = @($Selection.packages | Sort-Object name)
    while ($pending.Count) {
        $next = @()
        foreach ($package in $pending) {
            if ($package.name -notmatch '^[A-Za-z0-9_]+$') { throw 'Invalid package name in selection' }
            $children = [ordered]@{}
            $ready = $true
            foreach ($child in @($package.depends | Sort-Object)) {
                if (-not $result.ContainsKey([string] $child)) { $ready = $false; break }
                $children[[string] $child] = $result[[string] $child].Fingerprint
            }
            if (-not $ready) { $next += $package; continue }
            # A child may be source-built rather than restored. Its fingerprint
            # in a static selection is not proof that its current recipe still
            # matches: retain transitive provenance for independent validation.
            $dependencyInputMap = @{}
            $dependencyEdges = @{}
            foreach ($child in $children.Keys) {
                $childInput = $result[[string] $child].InputJson | ConvertFrom-Json
                foreach ($record in @($childInput.recipeInputs) + @($childInput.dependencyInputs)) {
                    $dependencyInputMap[[string] $record.path] = $record
                }
                $dependencyEdges[[string] $child] = @($childInput.dependencies.PSObject.Properties | ForEach-Object Name)
                foreach ($edge in $childInput.dependencyEdges.PSObject.Properties) {
                    $dependencyEdges[$edge.Name] = @($edge.Value)
                }
            }
            $orderedDependencyEdges = [ordered]@{}
            foreach ($child in @($dependencyEdges.Keys | Sort-Object)) { $orderedDependencyEdges[$child] = $dependencyEdges[$child] }
            $recipeDirectory = Join-Path $DependencyDirectory ('+' + $package.name)
            $recipeFiles = @(Get-ChildItem -LiteralPath $recipeDirectory -Recurse -File -Force)
            $keyInput = [ordered]@{
                schema = 1; kind = 'package'; package = [string] $package.name
                installPrefix = $Context.CanonicalInstallPrefix
                dependencyBuildDirectory = $BuildInput.dependencyBuildDirectory
                sourceDirectory = $BuildInput.sourceDirectory
                platform = $BuildInput.platform; targetArchitecture = $BuildInput.targetArchitecture
                prefixPolicy = $BuildInput.prefixPolicy; effectiveCMake = $effective
                environment = $BuildInput.environment; tools = $BuildInput.tools
                commonInputs = @($commonInputs); recipeInputs = @(Get-FastInputRecords $recipeFiles)
                dependencies = $children
                dependencyInputs = @($dependencyInputMap.Values | Sort-Object path)
                dependencyEdges = $orderedDependencyEdges
            }
            $json = ConvertTo-FastCanonicalJson $keyInput
            $key = Get-FastTextSha256 $json
            $result[[string] $package.name] = [pscustomobject]@{
                Name = [string] $package.name; Fingerprint = $key; InputJson = $json
                Entry = Join-Path $DependencyDirectory ('.compiled_cache\packages-v1\' + $key)
            }
        }
        if ($next.Count -eq $pending.Count) { throw 'Dependency cycle or unresolved package in cache graph' }
        $pending = $next
    }
    return $result
}

function Assert-FastPackagePath {
    param([string] $Relative)
    if (-not $Relative -or $Relative -match '(^/|\\|:|(^|/)\.\.?(/|$)|//|[\r\n\t])') {
        throw "Unsafe package payload path: $Relative"
    }
}

function Assert-FastPackageInputs {
    param($Package)
    $inputs = $Package.InputJson | ConvertFrom-Json
    foreach ($record in @($inputs.commonInputs) + @($inputs.recipeInputs) + @($inputs.dependencyInputs)) {
        if ((Get-FastFileSha256 $record.path) -ne $record.sha256) {
            throw "Package inputs changed during build: $($record.path)"
        }
    }
}

function Get-FastPackageManifest {
    param($Context, $Package)
    if (-not (Test-Path -LiteralPath $Package.Entry -PathType Container)) { return $null }
    try {
        $inputPath = Join-Path $Package.Entry 'input.json'
        $manifestPath = Join-Path $Package.Entry 'manifest.json'
        $completePath = Join-Path $Package.Entry 'COMPLETE'
        foreach ($path in @($inputPath, $manifestPath, $completePath)) {
            Assert-FastPathWithin -Path $path -Root $Package.Entry
        }
        if ((Get-FastFileSha256 $inputPath) -ne $Package.Fingerprint -or
            (Get-Content -LiteralPath $completePath -Raw).Trim() -ne (Get-FastFileSha256 $manifestPath)) {
            throw 'Package cache fingerprint/completion mismatch'
        }
        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        if ($manifest.schema -ne 1 -or $manifest.package -cne $Package.Name -or
            $manifest.fingerprint -ne $Package.Fingerprint -or
            $manifest.installPrefix -ne $Context.CanonicalInstallPrefix -or -not @($manifest.files).Count) {
            throw 'Package manifest identity mismatch'
        }
        $seen = @{}
        $payload = Join-Path $Package.Entry 'payload'
        foreach ($file in $manifest.files) {
            Assert-FastPackagePath $file.path
            if ($seen.ContainsKey($file.path)) { throw 'Duplicate package payload path' }
            $seen[$file.path] = $true
            $path = Join-Path $payload $file.path
            Assert-FastPathWithin -Path $path -Root $Package.Entry
            if ((Get-Item -LiteralPath $path).Length -ne $file.bytes -or
                (Get-FastFileSha256 $path) -ne $file.sha256) { throw 'Package payload hash/size mismatch' }
        }
        # Extra/unrecorded payload files are not allowed into the shared prefix.
        $actualFiles = @(Get-ChildItem -LiteralPath $payload -Recurse -File -Force)
        if ($actualFiles.Count -ne $seen.Count) { throw 'Unrecorded package payload file' }
        return $manifest
    }
    catch { Write-Warning "Rejecting package cache $($Package.Name): $($_.Exception.Message)"; return $null }
}

function Get-FastOwnedPackagePaths {
    param($Context, $Package)
    $manifestFile = Join-Path $Context.DependencyBuildDirectory ('builds\' + $Package.Name + '\install_manifest.txt')
    $relative = @()
    if (Test-Path -LiteralPath $manifestFile -PathType Leaf) {
        foreach ($path in Get-Content -LiteralPath $manifestFile) {
            if ($path) { $relative += Get-FastRelativePath -Root $Context.InstallPrefix -Path $path }
        }
        if ($Package.Name -eq 'wxWidgets') { $relative += 'bin/WebView2Loader.dll' }
    }
    elseif ($Package.Name -eq 'GMP') {
        $relative = @('include/gmp.h', 'lib/libgmp-10.lib', 'bin/libgmp-10.dll')
    }
    elseif ($Package.Name -eq 'MPFR') {
        $relative = @('include/mpfr.h', 'include/mpf2mpfr.h', 'lib/libmpfr-4.lib', 'bin/libmpfr-4.dll')
    }
    else {
        # Restored packages have no forged install manifests/build stamps. Their
        # original artifact is already immutable and needs no republication.
        return @()
    }
    # Do not silently forgive arbitrary missing installed files. Only the exact
    # shared SDK prune policy is excluded, whether or not pruning has run yet.
    return @($relative | Sort-Object -Unique | Where-Object { -not (Test-FastPrunedDependencyPath $_) })
}

function Publish-FastPackageArtifacts {
    param($Context)
    foreach ($name in @($Context.PackageContexts.Keys | Sort-Object)) {
        $package = $Context.PackageContexts[$name]
        if (Test-Path -LiteralPath $package.Entry) { continue } # Immutable, including corrupt entries.
        $temporary = $null
        try {
            Assert-FastPackageInputs $package
            $owned = @(Get-FastOwnedPackagePaths -Context $Context -Package $package)
            if (-not $owned.Count) { continue }
            $files = @()
            foreach ($relative in $owned) {
                Assert-FastPackagePath $relative
                $path = Join-Path $Context.InstallPrefix $relative
                Assert-FastPathWithin -Path $path -Root $Context.DependencyBuildDirectory
                # The explicit prune-policy entries were filtered above. Any
                # other missing manifest entry means incomplete ownership.
                $item = Get-Item -LiteralPath $path -ErrorAction Stop
                $files += [ordered]@{ path = $relative; bytes = $item.Length; sha256 = Get-FastFileSha256 $path }
            }
            $parent = Split-Path -Parent $package.Entry
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
            $temporary = Join-Path $parent ('.tmp-package-' + [Guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Path $temporary | Out-Null
            foreach ($file in $files) {
                $destination = Join-Path (Join-Path $temporary 'payload') $file.path
                New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
                Copy-Item -LiteralPath (Join-Path $Context.InstallPrefix $file.path) -Destination $destination
                if ((Get-FastFileSha256 $destination) -ne $file.sha256) { throw 'Package changed while publishing' }
            }
            Write-FastUtf8File -Path (Join-Path $temporary 'input.json') -Content $package.InputJson
            $manifest = [ordered]@{ schema = 1; package = $package.Name; fingerprint = $package.Fingerprint
                installPrefix = $Context.CanonicalInstallPrefix; files = $files }
            $manifestPath = Join-Path $temporary 'manifest.json'
            Write-FastUtf8File -Path $manifestPath -Content (ConvertTo-FastCanonicalJson $manifest)
            Write-FastUtf8File -Path (Join-Path $temporary 'COMPLETE') -Content ((Get-FastFileSha256 $manifestPath) + "`n")
            # Directory.Move is atomic within this volume and refuses an existing
            # destination; PowerShell Move-Item could nest inside a concurrent entry.
            [IO.Directory]::Move($temporary, $package.Entry)
            $temporary = $null
            Write-Host "[fast-build] Published package cache: $name"
        }
        catch { Write-Warning "Package $name remains source-built: $($_.Exception.Message)" }
        finally {
            if ($temporary) { Remove-FastTemporaryDirectory -Path $temporary -AllowedRoot (Split-Path -Parent $package.Entry) }
        }
    }
}

function Restore-FastPackageArtifacts {
    param($Context)
    if (Test-Path -LiteralPath $Context.InstallPrefix) {
        if (@(Get-ChildItem -LiteralPath $Context.InstallPrefix -Force).Count) { return $false }
    }
    $valid = [ordered]@{}
    foreach ($name in @($Context.PackageContexts.Keys | Sort-Object)) {
        $manifest = Get-FastPackageManifest -Context $Context -Package $Context.PackageContexts[$name]
        if ($manifest) { $valid[$name] = $manifest }
    }
    if (-not $valid.Count) { return $false }
    if (Test-Path -LiteralPath $Context.PackageSelectionPath) { throw 'Existing package selection must not be overwritten' }
    $temporary = Join-Path $Context.DependencyBuildDirectory ('.package-restore-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temporary | Out-Null
    try {
        $payload = Join-Path $temporary 'prefix'
        $hits = [ordered]@{}
        foreach ($name in $valid.Keys) {
            $package = $Context.PackageContexts[$name]
            foreach ($file in $valid[$name].files) {
                $destination = Join-Path $payload $file.path
                if (Test-Path -LiteralPath $destination) {
                    if ((Get-FastFileSha256 $destination) -ne $file.sha256) { throw "Conflicting package ownership: $($file.path)" }
                }
                else {
                    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
                    Copy-Item -LiteralPath (Join-Path (Join-Path $package.Entry 'payload') $file.path) -Destination $destination
                }
                if ((Get-FastFileSha256 $destination) -ne $file.sha256) { throw 'Package changed during restore' }
            }
            $manifestPath = Join-Path $package.Entry 'manifest.json'
            $hits[$name] = [ordered]@{ manifest = $manifestPath.Replace('\', '/'); sha256 = Get-FastFileSha256 $manifestPath }
        }
        $fingerprints = [ordered]@{}
        foreach ($name in @($Context.PackageContexts.Keys | Sort-Object)) { $fingerprints[$name] = $Context.PackageContexts[$name].Fingerprint }
        $selection = [ordered]@{ schema = 1; installPrefix = $Context.CanonicalInstallPrefix; hits = $hits; fingerprints = $fingerprints }
        $selectionJson = ConvertTo-FastCanonicalJson $selection
        Assert-FastPathWithin -Path $Context.InstallPrefix -Root $Context.DependencyBuildDirectory
        if (Test-Path -LiteralPath $Context.InstallPrefix) {
            if (@(Get-ChildItem -LiteralPath $Context.InstallPrefix -Force).Count) { throw 'Prefix became nonempty during package restore' }
            [IO.Directory]::Delete($Context.InstallPrefix) # Empty only, never recursive.
        }
        New-Item -ItemType Directory -Path (Split-Path -Parent $Context.InstallPrefix) -Force | Out-Null
        [IO.Directory]::Move($payload, $Context.InstallPrefix)
        $stream = [IO.File]::Open($Context.PackageSelectionPath, [IO.FileMode]::CreateNew)
        $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($selectionJson)
        try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
        Write-Host "[fast-build] Restored $($valid.Count) independent packages; remaining dependencies will build normally."
        return $true
    }
    finally { Remove-FastTemporaryDirectory -Path $temporary -AllowedRoot $Context.DependencyBuildDirectory }
}
