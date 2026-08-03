# Fast development builds

The normal build remains feature-complete and suitable for release validation. The
fast profiles deliberately trade optional features and debug artifacts for a much
shorter edit/build/run loop.

## Windows quick start

From PowerShell:

```powershell
.\build_fast.ps1
```

The script discovers Visual Studio Build Tools, CMake, and Ninja; builds a
Release-only dependency closure; configures the application with PCH and a
conservative core-library unity build; and builds only the runnable GUI target. Dependency downloads are kept in
`deps/.pkg_cache`. On Windows, completed dependency prefixes are also cached in
`deps/.compiled_cache` and restored after a clean build when the dependency
recipes, compiler, SDK, flags, profile, and absolute install prefix all match.

Subsequent source edits need only the application step:

```powershell
.\build_fast.ps1 -Step app
```

Useful variants:

```powershell
# Headless command-line slicer with GUI dependencies removed.
.\build_fast.ps1 -Profile cli

# Reconfigure after changing CMake options without rebuilding dependencies.
.\build_fast.ps1 -Step configure

# Diagnose a source that is incompatible with unity builds.
.\build_fast.ps1 -Step configure -NoUnity

# Discard generated outputs while retaining downloaded sources and compiled prefixes.
.\build_fast.ps1 -Clean

# Diagnose or force a dependency source build without reading/writing the compiled cache.
.\build_fast.ps1 -Clean -NoDependencyCache
```

Pass `-Jobs N` to cap both dependency-graph and application compile parallelism.
Without a cap, Ninja overlaps dependency downloads and configuration; its console
pool serializes the resource-heavy ExternalProject build and install steps.

Compiled-prefix cache entries are immutable and content-addressed. They are
restored through a staging directory only after the fingerprint, archive hash,
archive paths, and extracted file-tree hash validate. The initial implementation
is deliberately Windows-only and bound to the same absolute prefix, avoiding
relocation hazards in generated package metadata. A rejected entry falls back to
the normal source build and is quarantined when a replacement is published.

## What the profiles remove

| Profile | GUI | Tests | STEP/OCCT | Dependency configs | MSVC Release symbols |
| --- | --- | --- | --- | --- | --- |
| `default` | yes | yes | yes | Release + Debug on Windows | yes |
| `fast-gui` | yes | no | no | Release only | no |
| `fast-cli` | no | no | no | Release only | no |

The fast GUI dependency schedule contains 24 package targets instead of the
default Windows schedule of 47 (26 Release plus 21 Debug variants). The CLI
closure contains 21. The exact wall-time reduction depends on CPU, storage, and
whether the download/compiler caches are warm.

The CLI profile retains core file-format and slicing dependencies such as EXPAT,
PNG, JPEG, NanoSVG, OpenVDB, CGAL, and Z3. It removes only packages proven to be
GUI-, test-, or STEP-specific.

The Windows application sources also use the Windows Runtime Library COM pointer
instead of ATL. The optional Visual Studio ATL/MFC component is therefore not a
fast-build prerequisite.

## Measured Windows results

These measurements were taken on an AMD Ryzen AI 9 HX 370 (12 cores / 24 logical
processors), 63.3 GiB RAM, MSVC 19.44.35224, CMake 3.31.6, and Ninja 1.12.1.
Builds used `-Jobs 12`; no `sccache` or `ccache` executable was available.

| Measurement | Result |
| --- | ---: |
| Default Windows dependency schedule | 47 package/configuration targets |
| Fast GUI dependency schedule | 24 targets (49% fewer) |
| Fast CLI dependency schedule | 21 targets (55% fewer) |
| Fast CLI dependency source build | 14m 38s |
| Clean CLI compiled-prefix restore, including integrity checks | 3m 31s (76% less time, 4.15x faster) |
| Fast GUI dependency source build | 17m 25s, including first-time downloads |
| Clean fast GUI application configure + compile + link + stage | 9m 30s |
| Ninja portion of that clean GUI application build | 9m 17s |
| Warm GUI no-op application build | 2.3s median of three runs |
| Warm CLI no-op application build | 1.9s |

The build graph and artifact reductions are deterministic rather than
machine-dependent:

| Area | Before | Fast build | Reduction |
| --- | ---: | ---: | ---: |
| Core `libslic3r` compiler units | 226 source files | 28 unity groups + 8 isolated files | 84% |
| Core stable-PCH generation | 35.6s / 1,156,907,008 bytes | 15.8s / 881,590,272 bytes | 56% time / 24% size |
| libjpeg-turbo object edges | 134 | 100 | 25% |
| Boost compiled objects | 253 | 172 | 32% |
| Boost compiled libraries | 23 | 18 | 22% |
| Boost installed payload | 249.4 MiB | 229.0 MiB | 20.4 MiB |
| CLI dependency prefix after stale-artifact pruning | 930,164,088 bytes | 898,593,651 bytes | 30.1 MiB |

The dependency schedule comparison is structural: the complete default
47-target dependency graph was configured and counted, but not compiled to
produce a default wall-time baseline. The measured fast-profile times should
therefore not be presented as a wall-time speedup over an unmeasured default
build. The compiled-prefix restore comparison uses the same CLI fast profile and
includes archive, path, archive-hash, and extracted-tree validation.

## Direct CMake use

Developers already working in a compiler environment can invoke the presets
without the wrapper:

```text
cd deps
cmake --preset fast-gui
cmake --build --preset fast-gui --parallel
cd ..
cmake --preset fast-gui
cmake --build --preset fast-gui
```

Use `fast-cli` in both projects for the headless build. A configured `sccache` or
`ccache` executable is detected automatically and forwarded into CMake-based
dependency builds. Set `-DSLIC3R_COMPILER_CACHE=off` to disable detection, or set
it to an explicit executable name/path to require a cache.

## Validation builds

Do not use the fast profiles as release substitutes: they intentionally omit
tests, STEP import, Debug dependency variants, and distributable MSVC symbols.
Before merging or packaging, configure the normal profile and run the complete
test suite.
