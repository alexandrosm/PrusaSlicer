# One authoritative policy for the private fast-build SDK, not release assets.
# This file is fingerprinted by both whole-prefix and package caches.
function Test-FastPrunedDependencyPath {
    param([Parameter(Mandatory)][string] $RelativePath)
    $relative = $RelativePath.Replace('\', '/').ToLowerInvariant()
    if ($relative -in @(
        'bin/cjpeg.exe', 'bin/djpeg.exe', 'bin/jpegtran.exe',
        'bin/rdjpgcom.exe', 'bin/wrjpgcom.exe', 'bin/tjbench.exe', 'bin/vdb_print.exe',
        'include/turbojpeg.h', 'lib/turbojpeg-static.lib', 'lib/pkgconfig/libturbojpeg.pc',
        'include/boost-1_83/boost/json.hpp', 'include/boost-1_83/boost/program_options.hpp',
        'include/boost-1_83/boost/progress.hpp', 'include/boost-1_83/boost/timer.hpp',
        'include/boost-1_83/boost/url.hpp',
        'share/man/man1/cjpeg.1', 'share/man/man1/djpeg.1', 'share/man/man1/jpegtran.1',
        'share/man/man1/rdjpgcom.1', 'share/man/man1/wrjpgcom.1'
    )) { return $true }
    $components = '(json|poly_collection|program_options|timer|type_erasure|url)'
    if ($relative -match "^lib/libboost_${components}-[^/]+[.]lib$" -or
        $relative -match "^lib/cmake/boost_${components}-1[.]83[.]0/" -or
        $relative -match "^include/boost-1_83/boost/${components}/") { return $true }
    if ($relative.StartsWith('share/doc/libjpeg-turbo/') -and
        (Split-Path -Leaf $relative) -ne 'license.md') { return $true }
    return $false
}
