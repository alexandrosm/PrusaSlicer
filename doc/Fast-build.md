# Fast development builds

The normal build remains feature-complete and suitable for release validation. The
fast profiles deliberately trade optional features and debug artifacts for a much
shorter edit/build/run loop.

## Windows quick start

From PowerShell:

```powershell
.\build_fast.ps1
```

PowerShell 7 (`pwsh`) is recommended for independent-package cache reuse;
Windows PowerShell 5.1 remains supported by the wrapper and whole-prefix cache,
but its path-length limitations disable the finer-grained package cache.

The script requires Python with `psutil` (`-Python` can select an existing
interpreter), discovers Visual Studio Build Tools, CMake, and Ninja; builds a
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

The wrapper now refuses to start with unknown/inconsistent memory information
or less than the desktop reserve plus one worker's headroom. Automatic concurrency
is normally one or two workers under the default 6 GiB build budget; a larger
explicit memory budget permits up to four. It also leaves a quarter of installed
RAM free when estimating concurrency on large workstations.

Every configure/build invocation runs below normal priority through the process-tree
guard: 6 GiB sampled aggregate RSS ceiling, at least 6 GiB available physical RAM,
and on Windows at least 6 GiB system commit headroom. Launch requires an additional
2 GiB free RAM/commit above those reserves. Two allowed logical CPUs from distinct
physical cores are selected where topology is available. A private Windows PDB
endpoint isolates this build's compiler service from unrelated projects. Commands,
logs, PIDs, limits and measurements are retained in `out/build-guard`; Windows
`.cmd` sidecars avoid the incompatible Python/cmd.exe embedded-quote conventions.

These are sampled stopping thresholds, **not hard allocation quotas**: short
spikes can overshoot between samples, and RSS can count shared pages twice. A large
full-PDB link can stop safely without completing. No full build has a default
timeout, and no WSL commands are used. Deliberately tune `-MemoryGiB`,
`-MinimumFreeGiB`, `-MinimumCommitGiB`, or `-CpuCount` for a known machine budget;
`-LinkWorkingSetMiB` remains opt-in because a resident-memory cap may increase
paging without reducing committed memory or total work.

Pass `-Jobs N` to override concurrency, not resource safety. The same count applies to
dependency compilation, the application build, and nested CMake install steps.
Ninja's console pool serializes the resource-heavy ExternalProject steps.
Changing the worker count does not invalidate the compiled dependency cache.

`-Step app` now configures a missing application tree automatically, and
`-Step app -NoUnity` reconfigures before building so the switch takes effect.

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
| `lean-release` | yes | yes | yes | Release only | yes |

The fast GUI dependency schedule now contains 23 package targets instead of the
default Windows schedule of 47 (26 Release plus 21 Debug variants). The CLI
closure contains 20. The exact wall-time reduction depends on CPU, storage, and
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
Builds used `-Jobs 12`; no `sccache` or `ccache` executable was available. These
historical timings predate the resource-aware defaults and further OpenEXR,
wxWidgets and curl reductions below; they have not been rerun for the new graph.

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
cmake --build --preset fast-gui
cd ..
cmake --preset fast-gui
cmake --build --preset fast-gui
```

Use `fast-cli` in both projects for the headless build. A configured `sccache` or
`ccache` executable is detected automatically and forwarded into CMake-based
dependency builds. Set `-DSLIC3R_COMPILER_CACHE=off` to disable detection, or set
it to an explicit executable name/path to require a cache.

Direct fast/lean build presets use two workers, including nested CMake builds;
their dependency configure presets also set `DEP_MAX_THREADS=2`. Use the wrapper
for automatic memory budgeting and RAM/commit enforcement. Direct CMake
invocations do not automatically gain the wrapper's process guard. When tuning direct invocations, change both
`DEP_MAX_THREADS` at dependency configure time and the outer build parallelism.

## Full-feature Release build

The paired `lean-release` presets retain GUI, STEP import, tests, and Release
PDBs. They remove duplicate Debug dependency builds and use Ninja with compiler
caching support and a full PCH. The development presets retain stable-only PCH;
the release preset no longer inherits that edit-loop tradeoff. Unity compilation defaults off for this release path;
it remains an opt-in change requiring release validation.

The resource-guarded Windows entry point is now:

```powershell
.\build_fast.ps1 -Profile lean-release -Jobs 2
```

It builds the entire configured default target, including all GUI/console/G-code
launchers and test executables. It does not run the test suite or package a release.
Alternatively, direct CMake commands below require an x64 Visual Studio developer
shell on Windows (or a native compiler environment elsewhere), and do **not**
provide runtime resource enforcement:

```text
cmake --preset lean-release -S deps
cmake --build deps/build-lean-release --target deps --parallel 2
cmake --preset lean-release
cmake --build --preset lean-release
ctest --preset lean-release
```

The Windows dependency closure now contains 25 Release package targets,
including OCCT and Catch2, versus the default 47 Release/Debug targets. The
original 26-target preset was configured successfully, and the subsequent
OpenEXR removal was checked at the dependency-closure level.

A full Windows application build and all configured test executables have now
completed using this dependency closure, with cached compatible dependencies
and newly built OCCT/Catch2. The main DLL's full-PDB link exceeded this machine's
resource reserves; the measured package therefore uses a build-local
`/DEBUG:NONE` override for that DLL only, retaining the compiled `/Zi` objects,
GUI, STEP and `/OPT:REF /OPT:ICF`. This is not validation of a finished portable
release-symbol set. The checked-in preset and linker defaults still retain PDBs.
Packaged STEP export and FFF cube/STEP slicing match the official 2.9.6 release
after narrow generated-header normalization. See
[Windows-package-size.md](Windows-package-size.md) for package measurements and
validation limits; this resource-capped run is not a default-build speed A/B.

Some existing native tests write diagnostic files outside their working
directory or modify source fixtures. Run the complete suite only in an isolated
environment; the measurement run uses audited, selected regression cases and
private scratch directories, not an unrestricted full test-suite invocation.

A build directory is not a complete Windows distribution: the existing external
release staging workflow must also collect Mesa, runtime DLLs, and resources.

## Compiler cache symbols and PCH checks

MSVC+sccache selects embedded object symbols (`/Z7`) using CMake's
[CMP0141 policy](https://cmake.org/cmake/help/latest/policy/CMP0141.html) on CMake
3.25+, with standalone `/Zi`/`/ZI` flag normalization for older CMake or stale
cache entries. Final linker PDB flags are retained; symbols are not discarded.
An explicit `CMAKE_MSVC_DEBUG_INFORMATION_FORMAT` remains respected, with a
warning if it conflicts with sccache. This follows the
[sccache MSVC guidance](https://github.com/mozilla/sccache#usage).
The policy must precede the first `project()`; dependency external projects
receive the corresponding configuration-specific format.

The guarded wrapper detects sccache/ccache or accepts `-CompilerCache path`;
`-CompilerCache off` disables and clears stale launchers. For sccache it starts a
private **foreground** server under the guarded process tree, verifies that the
listener belongs to that exact child before starting the build, and cleans up
only its own processes. Server/port/config are private; artifact storage persists
in `out/compiler-cache/sccache` with a 2 GiB disk limit. Ambient `SCCACHE_*` settings
are cleared only for child processes, and an empty private configuration prevents
accidental remote upload/distributed compilation. Unsupported custom launchers
are rejected by this wrapper instead of bypassing tree protection. Direct CMake
still permits user-managed launchers.

A tiny native MSVC test with the official sccache 0.17.0 Windows prebuilt verified
one cache miss/write followed by **one cache hit and zero actual compilations**
in a separate supervisor with a different private port and the same artifact
directory. The generated command retained `-Z7`. Both runs stayed below 0.18 GiB
sampled RSS; this proves reusable cache operation, not a whole-build speedup.
The 7,936,022-byte ZIP was verified against the official asset SHA-256 and kept
under `out/review-improvements/tools`; nothing was installed globally or added to
PATH. See `out/review-improvements/sccache-native-8fc4963870e34c628ef22c0c836f2d9e`
for command graphs, guards and private-server statistics.

For a bounded PCH mechanism check in a native compiler environment:

```text
python tools/benchmark_pch.py --output out/pch-probe-new --max-seconds 120
```

This uses an isolated, small two-translation-unit fixture, never the production
build tree: clean build, warm no-op, first-party-header edit, and another no-op,
for full and stable-only PCH. Compiler caching is off to isolate PCH behavior.
Each child has one CPU, a 2 GiB RSS stopping threshold, 6 GiB RAM/Windows commit
reserves, and the remaining deadline. Logs show whether PCH regeneration
occurred. This synthetic fixture is **not** a measured whole-application speedup;
the historical PCH-generation-only numbers above also cannot establish overall
clean or edit/build performance.

The isolated native fixture completed all ten configure/build scenarios in
36.047 seconds (outer guard 40.860 seconds, peak 149,614,592 bytes). Both warm
builds were genuine Ninja no-ops. Editing the first-party header regenerated
the full PCH, but reused the stable-only PCH, as intended. These are synthetic
mechanism checks, not a production timing comparison; artifacts are under
`out/review-improvements/pch-native-bf63005e55b3480b8f7e1a76ab1811d3`.

`hintsToPot` is now explicit-only (`EXCLUDE_FROM_ALL`) while translation generation
remains available. Headless Windows builds omit only the GUI/G-code-viewer shims;
their console shim is retained. Full GUI/release builds retain all three shims.

## Further dependency build reductions

The pinned wxWidgets build no longer compiles unused AUI, property grid, ribbon,
rich text, STC/Scintilla, and XRC components. The pinned curl build omits its
command-line tool. In isolated Ninja configurations of the same source versions:

| Dependency | Original object compile steps | Reduced steps | Reduction |
| --- | ---: | ---: | ---: |
| wxWidgets | 792 | 525 | 267 (33.7%) |
| curl | 199 | 151 | 48 (24.1%) |

Retained wx compilation commands matched after normalizing build paths. All
libcurl compilation commands and its generated configuration header matched.
An MSVC syntax check using the application's required wx APIs passed. These are
build-graph measurements, not measured clean-build wall-time improvements or
release-download savings: the omitted archives/tools were not shipped.

The standalone `avrdude-slic3r` executable is also excluded from normal builds;
its explicit target remains available. Firmware flashing still links the same
avrdude library with all existing backends.

The pinned OpenVDB already uses its embedded Half implementation, but the older
PrusaSlicer finder unconditionally required external IlmBase/Half. The finder
now follows the installed OpenVDB feature header, preserving legacy IlmBase and
modern Imath configurations while avoiding this dependency for embedded-Half
builds. The pinned recipe explicitly selects embedded Half.

This removes OpenEXR from the explicit-root profiles: GUI 24 to 23, CLI 21 to 20,
and full Release 26 to 25 packages. The cached OpenEXR graph contains 134 C/C++
compile steps and its seven installed archives total 19,806,472 bytes (18.89 MiB).
These are avoided dependency builds and artifacts, not measured download savings.
The all-recipes default still selects OpenEXR independently; its recipe remains
available. Eight configure-only tests cover embedded, old external, new external,
and missing-provider cases, including the installed OpenVDB prefix.
An isolated relink without external Half also produced a byte-identical geometry
probe executable. See [Upstream-experiments.md](Upstream-experiments.md).

```text
cmake -DTEST_BINARY_DIR=out/openvdb-finder-tests -P tests/build_tools/test_openvdb_finder.cmake
```

The wrapper can be checked without any compilation or downloads:

```powershell
.\tests\build_tools\test_fast_build.ps1
```

## Validation builds

Do not use the `fast-gui`/`fast-cli` profiles as release substitutes: they intentionally omit
tests, STEP import, Debug dependency variants, and distributable MSVC symbols.
Before merging or packaging, configure the normal profile and run the complete
test suite.
