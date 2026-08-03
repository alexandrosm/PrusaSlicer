# Windows-only compiled dependency-prefix cache used by build_fast.ps1.
#
# Cache entries are immutable and bound to the exact absolute install prefix.
# This deliberately trades cross-checkout portability for safety: generated
# package metadata (and, on Unix, wx-config) may contain absolute paths.

function ConvertTo-FastNormalizedPath {
    param([Parameter(Mandatory)] [string] $Path)

    return [IO.Path]::GetFullPath($Path).TrimEnd('\', '/').Replace('\', '/').ToLowerInvariant()
}

function Get-FastBytesSha256 {
    param([Parameter(Mandatory)] [byte[]] $Bytes)

    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha256.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
    }
}

function Get-FastTextSha256 {
    param([Parameter(Mandatory)] [string] $Text)

    $encoding = New-Object Text.UTF8Encoding($false)
    return Get-FastBytesSha256 -Bytes $encoding.GetBytes($Text)
}

function Get-FastFileSha256 {
    param([Parameter(Mandatory)] [string] $Path)

    $stream = [IO.File]::OpenRead($Path)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha256.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
}

function ConvertTo-FastCanonicalJson {
    param([Parameter(Mandatory)] $Value)

    $json = $Value | ConvertTo-Json -Depth 20 -Compress
    return $json.Replace("`r`n", "`n").TrimEnd() + "`n"
}

function Write-FastUtf8File {
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [string] $Content
    )

    $encoding = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Assert-FastPathWithin {
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [string] $Root
    )

    $fullRoot = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $fullPath = [IO.Path]::GetFullPath($Path)
    $rootPrefix = $fullRoot + [IO.Path]::DirectorySeparatorChar
    if (-not $fullPath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing cache operation outside '$fullRoot': $fullPath"
    }
}

function Remove-FastTemporaryDirectory {
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [string] $AllowedRoot
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    Assert-FastPathWithin -Path $Path -Root $AllowedRoot
    Remove-Item -LiteralPath $Path -Recurse -Force
}

function Get-FastRelativePath {
    param(
        [Parameter(Mandatory)] [string] $Root,
        [Parameter(Mandatory)] [string] $Path
    )

    $fullRoot = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $fullPath = [IO.Path]::GetFullPath($Path)
    $rootPrefix = $fullRoot + [IO.Path]::DirectorySeparatorChar
    if (-not $fullPath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "'$fullPath' is not below '$fullRoot'"
    }
    return $fullPath.Substring($rootPrefix.Length).Replace('\', '/')
}

function Get-FastFileSetMetadata {
    param(
        [Parameter(Mandatory)] [string] $Root,
        [Parameter(Mandatory)] [object[]] $Files
    )

    $records = New-Object 'Collections.Generic.List[string]'
    $caseInsensitivePaths = @{}
    [long] $totalBytes = 0

    foreach ($file in $Files) {
        $fileInfo = if ($file -is [IO.FileInfo]) { $file } else { Get-Item -LiteralPath ([string] $file) }
        if (-not $fileInfo.Exists) {
            throw "Fingerprint input disappeared: $($fileInfo.FullName)"
        }
        if (($fileInfo.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Reparse points are not supported in cached inputs: $($fileInfo.FullName)"
        }

        $relativePath = Get-FastRelativePath -Root $Root -Path $fileInfo.FullName
        $collisionKey = $relativePath.ToLowerInvariant()
        if ($caseInsensitivePaths.ContainsKey($collisionKey)) {
            throw "Case-insensitive path collision in cache input: $relativePath"
        }
        $caseInsensitivePaths[$collisionKey] = $true

        $fileHash = Get-FastFileSha256 -Path $fileInfo.FullName
        $totalBytes += $fileInfo.Length
        [void] $records.Add("$relativePath`0$($fileInfo.Length)`0$fileHash`n")
    }

    $recordArray = $records.ToArray()
    [Array]::Sort($recordArray, [StringComparer]::Ordinal)
    $payload = [string]::Concat($recordArray)
    return [pscustomobject]@{
        Sha256    = Get-FastTextSha256 -Text $payload
        FileCount = $recordArray.Length
        TotalBytes = $totalBytes
    }
}

function Get-FastDirectoryMetadata {
    param([Parameter(Mandatory)] [string] $Root)

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        throw "Dependency prefix does not exist: $Root"
    }

    $items = @(Get-Item -LiteralPath $Root) + @(Get-ChildItem -LiteralPath $Root -Recurse -Force)
    foreach ($item in $items) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Reparse points are not supported in a cached prefix: $($item.FullName)"
        }
    }

    $files = @($items | Where-Object { -not $_.PSIsContainer })
    if ($files.Count -eq 0) {
        throw "Dependency prefix is empty: $Root"
    }
    return Get-FastFileSetMetadata -Root $Root -Files $files
}

function Read-FastCMakeCache {
    param([Parameter(Mandatory)] [string] $Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "CMake cache was not generated: $Path"
    }

    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if (-not $line -or $line[0] -eq '#' -or $line.StartsWith('//')) {
            continue
        }
        $colon = $line.IndexOf(':')
        $equals = if ($colon -ge 0) { $line.IndexOf('=', $colon + 1) } else { -1 }
        if ($colon -le 0 -or $equals -lt 0) {
            continue
        }
        $values[$line.Substring(0, $colon)] = $line.Substring($equals + 1)
    }
    return $values
}

function Get-FastCommandVersion {
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [string[]] $Arguments
    )

    $output = & $Path @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to query tool version: $Path"
    }
    return (($output | ForEach-Object { $_.ToString() }) -join "`n").Trim()
}

function Get-FastToolRecord {
    param(
        [Parameter(Mandatory)] [string] $Path,
        [string] $Version = ''
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Fingerprint tool/input does not exist: $Path"
    }
    $item = Get-Item -LiteralPath $Path
    return [ordered]@{
        path        = ConvertTo-FastNormalizedPath -Path $item.FullName
        sha256      = Get-FastFileSha256 -Path $item.FullName
        fileVersion = [string] $item.VersionInfo.FileVersion
        version     = $Version
    }
}

function Get-FastDependencyRecipeFiles {
    param(
        [Parameter(Mandatory)] [string] $SourceDirectory,
        [Parameter(Mandatory)] [string] $DependencyDirectory
    )

    $filesByPath = @{}
    $explicitFiles = @(
        (Join-Path $DependencyDirectory 'CMakeLists.txt'),
        (Join-Path $DependencyDirectory 'CMakePresets.json'),
        (Join-Path $DependencyDirectory 'DependencyGraph.cmake')
    )
    foreach ($path in $explicitFiles) {
        $item = Get-Item -LiteralPath $path
        $filesByPath[$item.FullName.ToLowerInvariant()] = $item
    }

    foreach ($packageDirectory in Get-ChildItem -LiteralPath $DependencyDirectory -Directory -Filter '+*') {
        foreach ($item in Get-ChildItem -LiteralPath $packageDirectory.FullName -Recurse -File -Force) {
            $filesByPath[$item.FullName.ToLowerInvariant()] = $item
        }
    }

    $moduleDirectory = Join-Path $SourceDirectory 'cmake\modules'
    foreach ($item in Get-ChildItem -LiteralPath $moduleDirectory -File -Filter '*.cmake' -Force) {
        $filesByPath[$item.FullName.ToLowerInvariant()] = $item
    }

    return @($filesByPath.Values)
}

function Add-FastOptionalToolRecord {
    param(
        [Parameter(Mandatory)] [Collections.IDictionary] $Destination,
        [Parameter(Mandatory)] [string] $Name,
        [string] $Path,
        [string] $Version = ''
    )

    if ($Path -and (Test-Path -LiteralPath $Path -PathType Leaf)) {
        $Destination[$Name] = Get-FastToolRecord -Path $Path -Version $Version
    }
    else {
        $Destination[$Name] = [ordered]@{ missing = $true }
    }
}

function New-FastDependencyCacheContext {
    param(
        [Parameter(Mandatory)] [string] $SourceDirectory,
        [Parameter(Mandatory)] [string] $DependencyDirectory,
        [Parameter(Mandatory)] [string] $DependencyBuildDirectory,
        [Parameter(Mandatory)] [string] $Profile,
        [Parameter(Mandatory)] [string] $Preset,
        [Parameter(Mandatory)] [string] $CMake,
        [Parameter(Mandatory)] [string] $Ninja,
        [Parameter(Mandatory)] [string] $VsDevCmd
    )

    $cmakeCache = Read-FastCMakeCache -Path (Join-Path $DependencyBuildDirectory 'CMakeCache.txt')
    $expectedPrefix = Join-Path $DependencyBuildDirectory 'destdir\usr\local'
    $configuredPrefix = $cmakeCache['PrusaSlicer_deps_DEP_INSTALL_PREFIX']
    if (-not $configuredPrefix) {
        throw 'PrusaSlicer_deps_DEP_INSTALL_PREFIX is missing from CMakeCache.txt'
    }
    if ((ConvertTo-FastNormalizedPath -Path $configuredPrefix) -ne (ConvertTo-FastNormalizedPath -Path $expectedPrefix)) {
        throw "The dependency prefix is not the expected same-path cache location: $configuredPrefix"
    }

    $effectiveNames = @(
        'BUILD_SHARED_LIBS',
        'CMAKE_BUILD_TYPE',
        'CMAKE_C_FLAGS',
        'CMAKE_C_FLAGS_RELEASE',
        'CMAKE_CXX_FLAGS',
        'CMAKE_CXX_FLAGS_RELEASE',
        'CMAKE_DEBUG_POSTFIX',
        'CMAKE_EXE_LINKER_FLAGS',
        'CMAKE_EXE_LINKER_FLAGS_RELEASE',
        'CMAKE_GENERATOR',
        'CMAKE_GENERATOR_PLATFORM',
        'CMAKE_GENERATOR_TOOLSET',
        'CMAKE_MODULE_LINKER_FLAGS',
        'CMAKE_MODULE_LINKER_FLAGS_RELEASE',
        'CMAKE_MSVC_RUNTIME_LIBRARY',
        'CMAKE_RC_FLAGS',
        'CMAKE_RC_FLAGS_RELEASE',
        'CMAKE_SHARED_LINKER_FLAGS',
        'CMAKE_SHARED_LINKER_FLAGS_RELEASE',
        'CMAKE_STATIC_LINKER_FLAGS',
        'CMAKE_STATIC_LINKER_FLAGS_RELEASE',
        'CMAKE_SYSTEM_NAME',
        'CMAKE_SYSTEM_PROCESSOR',
        'CMAKE_SYSTEM_VERSION',
        'CMAKE_TOOLCHAIN_FILE',
        'CMAKE_C_COMPILER_LAUNCHER',
        'CMAKE_CXX_COMPILER_LAUNCHER',
        'SLIC3R_COMPILER_CACHE'
    )
    $ignoredDependencyVariables = @(
        'DEP_DOWNLOAD_DIR',
        'DEP_MAX_THREADS',
        'DEP_MESSAGES_WRITTEN',
        'PrusaSlicer_deps_BINARY_DIR',
        'PrusaSlicer_deps_SOURCE_DIR',
        'PrusaSlicer_deps_IS_TOP_LEVEL',
        'PrusaSlicer_deps_DEP_DOWNLOAD_DIR',
        'PrusaSlicer_deps_DEP_INSTALL_PREFIX',
        'PrusaSlicer_deps_DEP_BUILD_VERBOSE'
    )

    $effectiveValues = @{}
    foreach ($name in $cmakeCache.Keys) {
        $include = $effectiveNames -contains $name
        if ($name.StartsWith('DEP_', [StringComparison]::Ordinal) -or
            $name.StartsWith('PrusaSlicer_deps_', [StringComparison]::Ordinal)) {
            $include = $true
        }
        if ($ignoredDependencyVariables -contains $name -or
            $name -match '^DEP_.+_MAX_THREADS$') {
            $include = $false
        }
        if ($include) {
            $effectiveValues[$name] = [string] $cmakeCache[$name]
        }
    }
    if (-not $effectiveValues.ContainsKey('CMAKE_MSVC_RUNTIME_LIBRARY') -or
        -not $effectiveValues['CMAKE_MSVC_RUNTIME_LIBRARY']) {
        $effectiveValues['CMAKE_MSVC_RUNTIME_LIBRARY'] = 'MultiThreadedDLL (CMake default for Release)'
    }

    $effectiveKeys = [string[]] $effectiveValues.Keys
    [Array]::Sort($effectiveKeys, [StringComparer]::Ordinal)
    $orderedEffectiveValues = [ordered]@{}
    foreach ($name in $effectiveKeys) {
        $orderedEffectiveValues[$name] = $effectiveValues[$name]
    }

    $recipeFiles = Get-FastDependencyRecipeFiles -SourceDirectory $SourceDirectory -DependencyDirectory $DependencyDirectory
    $recipeMetadata = Get-FastFileSetMetadata -Root $SourceDirectory -Files $recipeFiles

    $cmakeVersion = Get-FastCommandVersion -Path $CMake -Arguments @('--version')
    $ninjaVersion = Get-FastCommandVersion -Path $Ninja -Arguments @('--version')
    $gitPath = $cmakeCache['GIT_EXECUTABLE']
    $gitVersion = Get-FastCommandVersion -Path $gitPath -Arguments @('--version')

    $tools = [ordered]@{}
    $tools['cmake'] = Get-FastToolRecord -Path $CMake -Version $cmakeVersion
    $tools['ninja'] = Get-FastToolRecord -Path $Ninja -Version $ninjaVersion
    $tools['git'] = Get-FastToolRecord -Path $gitPath -Version $gitVersion
    $tools['vsDevCmd'] = Get-FastToolRecord -Path $VsDevCmd
    Add-FastOptionalToolRecord -Destination $tools -Name 'cCompiler' -Path $cmakeCache['CMAKE_C_COMPILER']
    Add-FastOptionalToolRecord -Destination $tools -Name 'cxxCompiler' -Path $cmakeCache['CMAKE_CXX_COMPILER']
    Add-FastOptionalToolRecord -Destination $tools -Name 'librarian' -Path $cmakeCache['CMAKE_AR']
    Add-FastOptionalToolRecord -Destination $tools -Name 'linker' -Path $cmakeCache['CMAKE_LINKER']
    Add-FastOptionalToolRecord -Destination $tools -Name 'resourceCompiler' -Path $cmakeCache['CMAKE_RC_COMPILER']
    Add-FastOptionalToolRecord -Destination $tools -Name 'manifestTool' -Path $cmakeCache['CMAKE_MT']

    foreach ($launcherName in @('CMAKE_C_COMPILER_LAUNCHER', 'CMAKE_CXX_COMPILER_LAUNCHER')) {
        $launcherPath = $cmakeCache[$launcherName]
        if ($launcherPath) {
            Add-FastOptionalToolRecord -Destination $tools -Name $launcherName -Path $launcherPath
        }
    }
    if ($cmakeCache['CMAKE_TOOLCHAIN_FILE']) {
        Add-FastOptionalToolRecord -Destination $tools -Name 'cmakeToolchainFile' `
            -Path $cmakeCache['CMAKE_TOOLCHAIN_FILE']
    }

    $compilerConfiguration = Get-ChildItem -LiteralPath (Join-Path $DependencyBuildDirectory 'CMakeFiles') `
        -Recurse -File -Filter 'CMakeCXXCompiler.cmake' | Sort-Object FullName | Select-Object -First 1
    if (-not $compilerConfiguration) {
        throw 'CMake compiler identification file was not generated'
    }
    $tools['cmakeCompilerConfiguration'] = Get-FastToolRecord -Path $compilerConfiguration.FullName

    $compilerPath = $cmakeCache['CMAKE_CXX_COMPILER']
    $compilerDirectory = Split-Path -Parent $compilerPath
    $vcToolsRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $compilerDirectory))
    $vcToolsVersion = Split-Path -Leaf $vcToolsRoot
    foreach ($libraryName in @('libcmt.lib', 'msvcrt.lib', 'vcruntime.lib')) {
        Add-FastOptionalToolRecord -Destination $tools -Name "vc-$libraryName" `
            -Path (Join-Path $vcToolsRoot "lib\x64\$libraryName")
    }

    $resourceCompiler = $cmakeCache['CMAKE_RC_COMPILER']
    $sdkVersion = ''
    if ($resourceCompiler) {
        $resourceDirectory = Split-Path -Parent $resourceCompiler
        $sdkVersionDirectory = Split-Path -Parent $resourceDirectory
        $sdkVersion = Split-Path -Leaf $sdkVersionDirectory
        $sdkRoot = Split-Path -Parent (Split-Path -Parent $sdkVersionDirectory)
        Add-FastOptionalToolRecord -Destination $tools -Name 'sdk-ucrt.lib' `
            -Path (Join-Path $sdkRoot "Lib\$sdkVersion\ucrt\x64\ucrt.lib")
        Add-FastOptionalToolRecord -Destination $tools -Name 'sdk-kernel32.lib' `
            -Path (Join-Path $sdkRoot "Lib\$sdkVersion\um\x64\kernel32.lib")
        Add-FastOptionalToolRecord -Destination $tools -Name 'sdk-corecrt.h' `
            -Path (Join-Path $sdkRoot "Include\$sdkVersion\ucrt\corecrt.h")
        Add-FastOptionalToolRecord -Destination $tools -Name 'sdk-Windows.h' `
            -Path (Join-Path $sdkRoot "Include\$sdkVersion\um\Windows.h")
    }

    $environment = [ordered]@{}
    foreach ($name in @('CC', 'CXX', 'CL', '_CL_', 'CFLAGS', 'CXXFLAGS', 'CPPFLAGS', 'LDFLAGS',
                         'CMAKE_PREFIX_PATH', 'CMAKE_TOOLCHAIN_FILE', 'INCLUDE', 'LIB', 'LIBPATH',
                         'SOURCE_DATE_EPOCH')) {
        $environment[$name] = [string] [Environment]::GetEnvironmentVariable($name)
    }

    $input = [ordered]@{
        schema                   = 1
        platform                 = 'windows'
        hostArchitecture         = 'x64'
        targetArchitecture       = 'x64'
        prefixPolicy             = 'lean-v1'
        profile                  = $Profile
        preset                   = $Preset
        sourceDirectory          = ConvertTo-FastNormalizedPath -Path $SourceDirectory
        dependencyBuildDirectory = ConvertTo-FastNormalizedPath -Path $DependencyBuildDirectory
        installPrefix            = ConvertTo-FastNormalizedPath -Path $expectedPrefix
        vcToolsVersion           = $vcToolsVersion
        windowsSdkVersion        = $sdkVersion
        effectiveCMake           = $orderedEffectiveValues
        environment              = $environment
        recipeTree               = [ordered]@{
            sha256     = $recipeMetadata.Sha256
            fileCount  = $recipeMetadata.FileCount
            totalBytes = $recipeMetadata.TotalBytes
        }
        tools                     = $tools
    }
    $inputJson = ConvertTo-FastCanonicalJson -Value $input
    $fingerprint = Get-FastTextSha256 -Text $inputJson
    $cacheRoot = Join-Path $DependencyDirectory '.compiled_cache\v1'

    return [pscustomobject]@{
        Schema                   = 1
        Profile                  = $Profile
        Preset                   = $Preset
        Fingerprint              = $fingerprint
        InputJson                = $inputJson
        CacheRoot                = $cacheRoot
        CacheEntryDirectory      = Join-Path $cacheRoot $fingerprint
        DependencyBuildDirectory = [IO.Path]::GetFullPath($DependencyBuildDirectory)
        InstallPrefix            = [IO.Path]::GetFullPath($expectedPrefix)
        CanonicalInstallPrefix   = ConvertTo-FastNormalizedPath -Path $expectedPrefix
        PrefixMarker             = Join-Path $DependencyBuildDirectory '.fast-dependency-cache.json'
    }
}

function Assert-FastDependencyPrefix {
    param(
        [Parameter(Mandatory)] [string] $Prefix,
        [Parameter(Mandatory)] [string] $Profile
    )

    $includeDirectory = Join-Path $Prefix 'include'
    $libraryDirectory = Join-Path $Prefix 'lib'
    if (-not (Test-Path -LiteralPath $includeDirectory -PathType Container) -or
        -not (Get-ChildItem -LiteralPath $includeDirectory -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1)) {
        throw 'Cached dependency prefix contains no headers'
    }
    if (-not (Test-Path -LiteralPath $libraryDirectory -PathType Container) -or
        -not (Get-ChildItem -LiteralPath $libraryDirectory -Recurse -File -Filter '*.lib' -ErrorAction SilentlyContinue | Select-Object -First 1)) {
        throw 'Cached dependency prefix contains no static/import libraries'
    }
    if ($Profile -eq 'gui') {
        $wxHeader = Get-ChildItem -LiteralPath (Join-Path $Prefix 'include') `
            -Recurse -File -Filter 'wx.h' -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '[\\/]wx[\\/]wx\.h$' } |
            Select-Object -First 1
        if (-not $wxHeader) {
            throw 'GUI dependency prefix is missing wxWidgets headers'
        }
        if (-not (Test-Path -LiteralPath (Join-Path $Prefix 'bin\WebView2Loader.dll') -PathType Leaf)) {
            throw 'GUI dependency prefix is missing WebView2Loader.dll'
        }
    }
}

function Optimize-FastDependencyPrefix {
    param(
        [Parameter(Mandatory)] [string] $Prefix,
        [Parameter(Mandatory)] [string] $AllowedRoot
    )

    if (-not (Test-Path -LiteralPath $Prefix -PathType Container)) {
        return
    }
    Assert-FastPathWithin -Path $Prefix -Root $AllowedRoot

    # None of these developer-prefix artifacts are consumed by PrusaSlicer.
    # Several are also stale when an existing prefix is incrementally rebuilt
    # after disabling TurboJPEG and vdb_print in their source projects.
    $relativeFiles = @(
        'bin\cjpeg.exe',
        'bin\djpeg.exe',
        'bin\jpegtran.exe',
        'bin\rdjpgcom.exe',
        'bin\wrjpgcom.exe',
        'bin\tjbench.exe',
        'bin\vdb_print.exe',
        'include\turbojpeg.h',
        'lib\turbojpeg-static.lib',
        'lib\pkgconfig\libturbojpeg.pc',
        'include\boost-1_83\boost\json.hpp',
        'include\boost-1_83\boost\program_options.hpp',
        'include\boost-1_83\boost\progress.hpp',
        'include\boost-1_83\boost\timer.hpp',
        'include\boost-1_83\boost\url.hpp',
        'share\man\man1\cjpeg.1',
        'share\man\man1\djpeg.1',
        'share\man\man1\jpegtran.1',
        'share\man\man1\rdjpgcom.1',
        'share\man\man1\wrjpgcom.1'
    )

    [long] $removedBytes = 0
    [int] $removedFiles = 0
    foreach ($relativePath in $relativeFiles) {
        $path = Join-Path $Prefix $relativePath
        Assert-FastPathWithin -Path $path -Root $AllowedRoot
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $removedBytes += (Get-Item -LiteralPath $path).Length
            Remove-Item -LiteralPath $path -Force
            ++$removedFiles
        }
    }

    $leanBoostComponents = @('json', 'poly_collection', 'program_options', 'timer', 'type_erasure', 'url')
    $boostLibraryDirectory = Join-Path $Prefix 'lib'
    foreach ($component in $leanBoostComponents) {
        foreach ($library in Get-ChildItem -LiteralPath $boostLibraryDirectory -File `
                -Filter "libboost_${component}-*.lib" -ErrorAction SilentlyContinue) {
            Assert-FastPathWithin -Path $library.FullName -Root $AllowedRoot
            $removedBytes += $library.Length
            Remove-Item -LiteralPath $library.FullName -Force
            ++$removedFiles
        }
    }

    $relativeDirectories = @(
        $leanBoostComponents | ForEach-Object { "lib\cmake\boost_${_}-1.83.0" }
        $leanBoostComponents | ForEach-Object { "include\boost-1_83\boost\${_}" }
    )
    foreach ($relativePath in $relativeDirectories) {
        $path = Join-Path $Prefix $relativePath
        if (Test-Path -LiteralPath $path -PathType Container) {
            Assert-FastPathWithin -Path $path -Root $AllowedRoot
            $files = @(Get-ChildItem -LiteralPath $path -Recurse -File -Force)
            $removedBytes += ($files | Measure-Object Length -Sum).Sum
            $removedFiles += $files.Count
            Remove-Item -LiteralPath $path -Recurse -Force
        }
    }

    # Keep the package license but omit installed end-user/tool documentation
    # from this private build SDK.
    $jpegDocs = Join-Path $Prefix 'share\doc\libjpeg-turbo'
    if (Test-Path -LiteralPath $jpegDocs -PathType Container) {
        Assert-FastPathWithin -Path $jpegDocs -Root $AllowedRoot
        foreach ($file in Get-ChildItem -LiteralPath $jpegDocs -Recurse -File -Force) {
            if ($file.Name -ne 'LICENSE.md') {
                $removedBytes += $file.Length
                Remove-Item -LiteralPath $file.FullName -Force
                ++$removedFiles
            }
        }
        Get-ChildItem -LiteralPath $jpegDocs -Recurse -Directory -Force |
            Sort-Object FullName -Descending |
            Where-Object { @(Get-ChildItem -LiteralPath $_.FullName -Force).Count -eq 0 } |
            Remove-Item -Force
    }

    if ($removedFiles -gt 0) {
        Write-Host ("[fast-build] Pruned {0} unused dependency artifacts ({1:N1} MiB)." -f `
            $removedFiles, ($removedBytes / 1MB))
    }
}

function Write-FastDependencyPrefixMarker {
    param(
        [Parameter(Mandatory)] $Context,
        [Parameter(Mandatory)] $TreeMetadata
    )

    $marker = [ordered]@{
        schema        = $Context.Schema
        fingerprint   = $Context.Fingerprint
        installPrefix = $Context.CanonicalInstallPrefix
        prefix        = [ordered]@{
            sha256     = $TreeMetadata.Sha256
            fileCount  = $TreeMetadata.FileCount
            totalBytes = $TreeMetadata.TotalBytes
        }
    }
    $temporaryMarker = "$($Context.PrefixMarker).tmp-$PID-$([Guid]::NewGuid().ToString('N'))"
    Write-FastUtf8File -Path $temporaryMarker -Content (ConvertTo-FastCanonicalJson -Value $marker)
    Move-Item -LiteralPath $temporaryMarker -Destination $Context.PrefixMarker -Force
}

function Test-FastInstalledDependencyPrefix {
    param([Parameter(Mandatory)] $Context)

    if (-not (Test-Path -LiteralPath $Context.PrefixMarker -PathType Leaf) -or
        -not (Test-Path -LiteralPath $Context.InstallPrefix -PathType Container)) {
        return $false
    }

    try {
        $marker = Get-Content -LiteralPath $Context.PrefixMarker -Raw | ConvertFrom-Json
        if ([int] $marker.schema -ne $Context.Schema -or
            [string] $marker.fingerprint -ne $Context.Fingerprint -or
            [string] $marker.installPrefix -ne $Context.CanonicalInstallPrefix) {
            return $false
        }
        Assert-FastDependencyPrefix -Prefix $Context.InstallPrefix -Profile $Context.Profile
        $tree = Get-FastDirectoryMetadata -Root $Context.InstallPrefix
        if ($tree.Sha256 -ne [string] $marker.prefix.sha256 -or
            $tree.FileCount -ne [int] $marker.prefix.fileCount -or
            $tree.TotalBytes -ne [long] $marker.prefix.totalBytes) {
            Write-Warning 'Installed dependency prefix no longer matches its cache marker; rebuilding it.'
            return $false
        }
        Write-Host "[fast-build] Compiled dependency prefix is current ($($Context.Fingerprint.Substring(0, 12)))."
        return $true
    }
    catch {
        Write-Warning "Unable to validate the installed dependency prefix: $($_.Exception.Message)"
        return $false
    }
}

function Get-FastArchiveEntries {
    param(
        [Parameter(Mandatory)] [string] $CMake,
        [Parameter(Mandatory)] [string] $ArchivePath
    )

    $output = & $CMake -E tar tzf $ArchivePath 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to list compiled dependency cache archive'
    }
    $entries = @($output | ForEach-Object { $_.ToString().Trim() } | Where-Object { $_ })
    if ($entries.Count -eq 0) {
        throw 'Compiled dependency cache archive is empty'
    }

    $seen = @{}
    foreach ($entry in $entries) {
        $normalized = $entry.Replace('\', '/')
        if ($normalized.StartsWith('/', [StringComparison]::Ordinal) -or
            $normalized -match '^[A-Za-z]:' -or
            $normalized.Contains(':') -or
            $normalized.Contains('//')) {
            throw "Unsafe path in compiled dependency cache archive: $entry"
        }
        $segments = $normalized.Split('/')
        if ($segments -contains '..' -or $segments -contains '.') {
            throw "Unsafe traversal path in compiled dependency cache archive: $entry"
        }
        if ($normalized -ne 'usr/local' -and
            -not $normalized.StartsWith('usr/local/', [StringComparison]::Ordinal)) {
            throw "Archive entry is outside usr/local: $entry"
        }
        $collisionKey = $normalized.ToLowerInvariant()
        if ($seen.ContainsKey($collisionKey)) {
            throw "Duplicate case-insensitive path in cache archive: $entry"
        }
        $seen[$collisionKey] = $true
    }
    return $entries
}

function Get-FastValidatedCacheManifest {
    param(
        [Parameter(Mandatory)] $Context,
        [Parameter(Mandatory)] [string] $CMake
    )

    $entry = $Context.CacheEntryDirectory
    if (-not (Test-Path -LiteralPath $entry -PathType Container)) {
        return $null
    }

    try {
        $inputPath = Join-Path $entry 'input.json'
        $manifestPath = Join-Path $entry 'manifest.json'
        $archivePath = Join-Path $entry 'prefix.tar.gz'
        $completePath = Join-Path $entry 'COMPLETE'
        foreach ($path in @($inputPath, $manifestPath, $archivePath, $completePath)) {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
                throw "Cache entry is incomplete: $path"
            }
        }

        if ((Get-FastFileSha256 -Path $inputPath) -ne $Context.Fingerprint) {
            throw 'input.json does not match the cache fingerprint'
        }
        $manifestHash = Get-FastFileSha256 -Path $manifestPath
        if ((Get-Content -LiteralPath $completePath -Raw).Trim() -ne $manifestHash) {
            throw 'COMPLETE does not match manifest.json'
        }

        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        if ([int] $manifest.schema -ne $Context.Schema -or
            [string] $manifest.fingerprint -ne $Context.Fingerprint -or
            [string] $manifest.platform -ne 'windows' -or
            [string] $manifest.targetArchitecture -ne 'x64' -or
            [string] $manifest.profile -ne $Context.Profile -or
            [string] $manifest.preset -ne $Context.Preset -or
            [string] $manifest.installPrefix -ne $Context.CanonicalInstallPrefix) {
            throw 'Cache manifest does not match the requested dependency ABI/prefix'
        }
        if ([string] $manifest.archive.fileName -ne 'prefix.tar.gz' -or
            [int] $manifest.prefix.fileCount -le 0 -or
            [long] $manifest.prefix.totalBytes -le 0) {
            throw 'Cache manifest contains invalid archive/prefix metadata'
        }
        $archiveItem = Get-Item -LiteralPath $archivePath
        if ($archiveItem.Length -ne [long] $manifest.archive.size -or
            (Get-FastFileSha256 -Path $archivePath) -ne [string] $manifest.archive.sha256) {
            throw 'Compiled dependency archive hash/size mismatch'
        }
        [void] (Get-FastArchiveEntries -CMake $CMake -ArchivePath $archivePath)
        return $manifest
    }
    catch {
        Write-Warning "Rejecting compiled dependency cache entry '$entry': $($_.Exception.Message)"
        return $null
    }
}

function Restore-FastDependencyCache {
    param(
        [Parameter(Mandatory)] $Context,
        [Parameter(Mandatory)] [string] $CMake
    )

    $hadManagedPrefix = Test-Path -LiteralPath $Context.PrefixMarker -PathType Leaf
    if (Test-FastInstalledDependencyPrefix -Context $Context) {
        if (Get-FastValidatedCacheManifest -Context $Context -CMake $CMake) {
            return 'hit'
        }
        Write-Warning 'The installed dependency prefix is current, but its compiled cache entry is missing; republishing it.'
        return 'publish'
    }

    $manifest = Get-FastValidatedCacheManifest -Context $Context -CMake $CMake

    if (Test-Path -LiteralPath $Context.InstallPrefix -PathType Container) {
        $existingItems = @(Get-ChildItem -LiteralPath $Context.InstallPrefix -Force)
        if ($existingItems.Count -gt 0 -and -not $hadManagedPrefix) {
            Write-Host '[fast-build] Existing unmarked dependency prefix retained; using the normal incremental build.'
            return 'miss'
        }
    }

    if ($hadManagedPrefix -and -not $manifest) {
        # Removing only the prefix would leave ExternalProject completion stamps
        # behind, allowing a false no-op build. The caller must discard and
        # reconfigure the generated dependency tree before source-building.
        Write-Warning 'The managed dependency prefix is stale and no matching compiled cache exists; a full dependency rebuild is required.'
        return 'rebuild'
    }

    if (-not $manifest) {
        Write-Host "[fast-build] Compiled dependency cache miss ($($Context.Fingerprint.Substring(0, 12)))."
        return 'miss'
    }

    if ($hadManagedPrefix) {
        Assert-FastPathWithin -Path $Context.PrefixMarker -Root $Context.DependencyBuildDirectory
        Remove-Item -LiteralPath $Context.PrefixMarker -Force
        if (Test-Path -LiteralPath $Context.InstallPrefix) {
            Assert-FastPathWithin -Path $Context.InstallPrefix -Root $Context.DependencyBuildDirectory
            Remove-Item -LiteralPath $Context.InstallPrefix -Recurse -Force
        }
        Write-Host '[fast-build] Removed a stale or modified cache-managed dependency prefix.'
    }

    $stagingDirectory = Join-Path $Context.DependencyBuildDirectory ".cache-restore-$PID-$([Guid]::NewGuid().ToString('N').Substring(0, 8))"
    Assert-FastPathWithin -Path $stagingDirectory -Root $Context.DependencyBuildDirectory
    New-Item -ItemType Directory -Path $stagingDirectory | Out-Null
    try {
        Push-Location -LiteralPath $stagingDirectory
        try {
            & $CMake -E tar xzf (Join-Path $Context.CacheEntryDirectory 'prefix.tar.gz')
            if ($LASTEXITCODE -ne 0) {
                throw 'Unable to extract compiled dependency cache archive'
            }
        }
        finally {
            Pop-Location
        }

        $stagedPrefix = Join-Path $stagingDirectory 'usr\local'
        Assert-FastDependencyPrefix -Prefix $stagedPrefix -Profile $Context.Profile
        $tree = Get-FastDirectoryMetadata -Root $stagedPrefix
        if ($tree.Sha256 -ne [string] $manifest.prefix.sha256 -or
            $tree.FileCount -ne [int] $manifest.prefix.fileCount -or
            $tree.TotalBytes -ne [long] $manifest.prefix.totalBytes) {
            throw 'Extracted dependency prefix tree does not match manifest.json'
        }

        if (Test-Path -LiteralPath $Context.InstallPrefix) {
            if (@(Get-ChildItem -LiteralPath $Context.InstallPrefix -Force).Count -ne 0) {
                throw 'Dependency prefix became non-empty during cache restore'
            }
            Remove-Item -LiteralPath $Context.InstallPrefix -Force
        }
        $prefixParent = Split-Path -Parent $Context.InstallPrefix
        New-Item -ItemType Directory -Path $prefixParent -Force | Out-Null
        Move-Item -LiteralPath $stagedPrefix -Destination $Context.InstallPrefix
        Write-FastDependencyPrefixMarker -Context $Context -TreeMetadata $tree
        Write-Host "[fast-build] Restored compiled dependencies ($($Context.Fingerprint.Substring(0, 12)))."
        return 'hit'
    }
    finally {
        Remove-FastTemporaryDirectory -Path $stagingDirectory -AllowedRoot $Context.DependencyBuildDirectory
    }
}

function Publish-FastDependencyCache {
    param(
        [Parameter(Mandatory)] $Context,
        [Parameter(Mandatory)] [string] $CMake
    )

    Assert-FastDependencyPrefix -Prefix $Context.InstallPrefix -Profile $Context.Profile
    $tree = Get-FastDirectoryMetadata -Root $Context.InstallPrefix
    New-Item -ItemType Directory -Path $Context.CacheRoot -Force | Out-Null
    if (Test-Path -LiteralPath $Context.CacheEntryDirectory -PathType Container) {
        if (Get-FastValidatedCacheManifest -Context $Context -CMake $CMake) {
            Write-FastDependencyPrefixMarker -Context $Context -TreeMetadata $tree
            Write-Host "[fast-build] Compiled dependency cache already populated ($($Context.Fingerprint.Substring(0, 12)))."
            return
        }
        $quarantineName = ".invalid-$($Context.Fingerprint)-$([DateTime]::UtcNow.ToString('yyyyMMddHHmmss'))-$([Guid]::NewGuid().ToString('N').Substring(0, 8))"
        $quarantinePath = Join-Path $Context.CacheRoot $quarantineName
        Move-Item -LiteralPath $Context.CacheEntryDirectory -Destination $quarantinePath
        Write-Warning "Moved the invalid cache entry to '$quarantinePath'."
    }

    $temporaryEntry = Join-Path $Context.CacheRoot ".tmp-$PID-$([Guid]::NewGuid().ToString('N').Substring(0, 8))"
    Assert-FastPathWithin -Path $temporaryEntry -Root $Context.CacheRoot
    New-Item -ItemType Directory -Path $temporaryEntry | Out-Null
    try {
        $inputPath = Join-Path $temporaryEntry 'input.json'
        $archivePath = Join-Path $temporaryEntry 'prefix.tar.gz'
        $manifestPath = Join-Path $temporaryEntry 'manifest.json'
        $completePath = Join-Path $temporaryEntry 'COMPLETE'
        Write-FastUtf8File -Path $inputPath -Content $Context.InputJson

        $destdir = Join-Path $Context.DependencyBuildDirectory 'destdir'
        Push-Location -LiteralPath $destdir
        try {
            & $CMake -E tar czf $archivePath 'usr/local'
            if ($LASTEXITCODE -ne 0) {
                throw 'Unable to create compiled dependency cache archive'
            }
        }
        finally {
            Pop-Location
        }

        $archiveItem = Get-Item -LiteralPath $archivePath
        $manifest = [ordered]@{
            schema        = $Context.Schema
            fingerprint   = $Context.Fingerprint
            createdUtc    = [DateTime]::UtcNow.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
            platform      = 'windows'
            targetArchitecture = 'x64'
            profile       = $Context.Profile
            preset        = $Context.Preset
            installPrefix = $Context.CanonicalInstallPrefix
            archive       = [ordered]@{
                fileName = 'prefix.tar.gz'
                sha256   = Get-FastFileSha256 -Path $archivePath
                size     = $archiveItem.Length
            }
            prefix        = [ordered]@{
                sha256     = $tree.Sha256
                fileCount  = $tree.FileCount
                totalBytes = $tree.TotalBytes
            }
        }
        Write-FastUtf8File -Path $manifestPath -Content (ConvertTo-FastCanonicalJson -Value $manifest)
        Write-FastUtf8File -Path $completePath -Content ((Get-FastFileSha256 -Path $manifestPath) + "`n")

        if (Test-Path -LiteralPath $Context.CacheEntryDirectory) {
            if (-not (Get-FastValidatedCacheManifest -Context $Context -CMake $CMake)) {
                throw 'A concurrently published dependency cache entry failed validation'
            }
            Write-FastDependencyPrefixMarker -Context $Context -TreeMetadata $tree
            Write-Host '[fast-build] Another process populated the compiled dependency cache first.'
            return
        }
        Move-Item -LiteralPath $temporaryEntry -Destination $Context.CacheEntryDirectory
        Write-FastDependencyPrefixMarker -Context $Context -TreeMetadata $tree
        Write-Host "[fast-build] Published compiled dependency cache ($($Context.Fingerprint.Substring(0, 12)))."
        $temporaryEntry = $null
    }
    finally {
        if ($temporaryEntry) {
            Remove-FastTemporaryDirectory -Path $temporaryEntry -AllowedRoot $Context.CacheRoot
        }
    }
}
