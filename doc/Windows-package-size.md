# Windows package-size measurements

This document records controlled size experiments and their validation limits.
Build-tree and dependency-prefix reductions are documented
separately in [Fast-build.md](Fast-build.md).

## Current status (2026-09-10)

The target remains an approximately 50 MiB Windows download with all features
available offline. The completed full-application build's best verified
ready-to-run, original-geometry portable archive is **65,161,761 bytes:
65.16 MB / 62.14 MiB**, 38.87% below the official
ZIP. It does not meet either a 50 MB or 50 MiB target. No installer was produced.
The main DLL's developer PDB was omitted to complete linking within resource
reserves; application features and offline resources were retained. Earlier
conversation totals were projections assembled from separate experiments;
they must not be presented as measurements of a finished release.

A subsequent opt-in [bed-mesh adjustment experiment](Bed-mesh-adjustment.md)
measured **62,612,052 bytes: 62.61 MB / 59.71 MiB**, an additional 2,549,709-byte
download reduction. It changes 16 bed geometries subject to reported checks;
GUI/picking parity remains unverified. It is separate from the original-resource
baseline above and still does not meet either 50-unit target.

The newer [byte-reversible STL transport experiment](Lossless-STL-packaging.md)
measured **62,502,563 bytes: 62.50 MB / 59.61 MiB** using the original meshes.
All original staged files are restored byte-for-byte, but this archive requires
an offline `PREPARE.cmd` step after extraction; it is not ready-to-run portable
or an integrated installer. This is 2,659,198 bytes below the original-geometry
7z baseline, not an additive saving on top of the mesh experiment. The latest
[review report](Review-improvements.md) separates implemented build changes,
measured transport savings, and still-unmeasured runtime/build performance.

A separately verified combination of the revised adaptive meshes and reversible
STL transport is **61,110,683 bytes: 61.11 MB / 58.28 MiB**, 42.67% below the
official ZIP. It also needs offline preparation and retains the simplified
geometry experiment's unverified GUI/picking behavior. It must not be described
as a byte-exact original-geometry release; neither variant reaches 50 MB.

In particular, the local Windows-font coverage check and translation-only font
subsets do not establish arbitrary-text coverage on every supported Windows
version/edition. No DirectWrite replacement has been implemented or validated.
Bundled CJK coverage, Mesa, STEP support, and all profiles must stay available
offline until replacements demonstrate parity.

The subsequent [upstream dependency experiments](Upstream-experiments.md) measured
smaller OpenVDB/Z3 probe executables, not a new application package. Their savings
must not be subtracted from the portable archive below.

## Restoring Release linker elimination

PrusaSlicer generates PDBs for MSVC Release builds with `/Zi` and `/DEBUG`.
`/DEBUG` changes the linker's defaults to `/OPT:NOREF` and `/OPT:NOICF`, which
retains unreachable and duplicate COMDATs in the shipped PE image. Release
targets now request `/OPT:REF` and `/OPT:ICF` explicitly while retaining PDB
generation.

A controlled relink used the same GUI objects and libraries for both variants;
only the linker options changed. Both variants retained `/DEBUG`.
The existing fast-GUI build disables STEP at compile time. Its optimized DLL
must not be substituted into an official package and described as a verified
full-feature build merely because `OCCTWrapper.dll` is also present. Use the new
`lean-release` presets for the full-feature build/validation path.

| Measurement | `/OPT:NOREF /OPT:NOICF` | `/OPT:REF /OPT:ICF` | Reduction |
| --- | ---: | ---: | ---: |
| `PrusaSlicer.dll` | 97,600,512 bytes | 49,126,912 bytes | 48,473,600 bytes (49.7%) |
| Raw DEFLATE stream, level 9 | 33,469,949 bytes | 20,059,174 bytes | 13,410,775 bytes (40.1%) |
| PE mapped-image size | 98,222,080 bytes | 49,741,824 bytes | 48,480,256 bytes (49.4%) |

The official 2.9.6 portable ZIP is 106,598,059 bytes. Its central directory
reports a 96,501,256-byte `PrusaSlicer.dll` occupying 32,220,379 compressed
bytes, closely matching the controlled no-elimination variant. The isolated
result projected roughly 12--13 MiB off a ZIP. That is not a measurement of the
finished package: the full-build LZMA2 result below is smaller by only 6.89 MiB
than the unchanged official payload repacked with the same LZMA2 settings.
Compression can already encode much of the duplicate code compactly; raw PE,
ZIP and solid LZMA2 savings must not be added together.

Fifteen alternating warm `--info` launches over the same STL produced identical
output. Median elapsed time was statistically unchanged (228.08 ms without
elimination and 229.62 ms with elimination). Median sampled peak working set was
51.64 versus 51.08 MiB, while sampled virtual size fell from 5,569.93 to
5,525.22 MiB. This short command does not measure cold I/O or full interactive
GUI startup; those require a packaged full-feature benchmark.

## Exact duplicate splash image

`resources/icons/splashscreen.jpg` and
`resources/icons/splashscreen-gcodepreview.jpg` were byte-identical (SHA-256
`19EBB4192070F302FF1226201EEC3C8D20B089EF3ECE6E9A8ACDF141F2E593F3`). Both
application modes now use the shared file. This removes 275,085 installed bytes
and 274,617 bytes from the official 2.9.6 ZIP with identical rendered pixels.

## Promising work not yet integrated

An opt-in [bed-mesh adjustment stage](Bed-mesh-adjustment.md) now fits between
the exact full-release stage and portable packaging. It generates a separate
clone with tolerance-checked simplified bed assets and leaves default resources
unchanged. This geometry-changing experiment is not yet a demonstrated
no-functionality-loss release; its measured results must be kept separate from
the immutable baseline above.

- A direct-document OCCT importer prototype reduced `OCCTWrapper.dll` from
  15,401,472 to 12,213,760 bytes. It disables metadata that the wrapper API does
  not expose, but it needs a diverse STEP equivalence corpus before it can be
  considered functionality-preserving.
- The Windows resource bundle contains about 0.9 MiB of translation sources and
  platform-specific files that are not used at runtime. The authoritative
  installer workflow is external to this repository, so filtering belongs in
  that pinned package manifest.
- Exact duplicate resource groups account for about 3.29 MiB before packaging.
  Alias compatibility and solid-archive behavior make this a lower-priority
  packaging change.
- Unused wxWidgets archives can be omitted from the dependency prefix, but they
  are not linked or shipped and therefore do not reduce the download.
- Z3 is statically linked and its parser code reaches the GUI binary. Isolated
  named-registration cuts now pass solver smoke tests, but no full application
  binary reduction or arrangement-corpus validation has been demonstrated.

Before release, run the complete test suite and exercise GUI/editor/viewer,
localization, WebView, software rendering, and STEP import using the exact
packager configuration.

## Repeatable lossless portable packaging

`tools/package_portable.py` packages an existing, fully staged Windows release
as a solid LZMA2 `.7z`. It keeps every input file, including fonts, Mesa, OCCT,
and profile aliases. It extracts the result and compares every file's SHA-256
and the complete directory layout before publishing the archive and JSON report.
It also verifies that the source stayed unchanged during packaging, refuses
overwriting an archive/report, and rejects symlinks/junctions in the stage.

```powershell
python tools/package_portable.py --source C:/staging/PrusaSlicer --output out/PrusaSlicer.7z
python -B -m unittest discover -s tools/tests -p test_package_portable.py -v
```

Requires Python 3.9+ and 7-Zip. Compression uses one thread at below-normal
priority on Windows, a 32 MiB dictionary, and 256 MiB solid blocks. The encoder
needs several times the dictionary size in RAM; the dictionary is not a process
memory limit. `--dictionary-mib 16` reduces encoder memory, while `64` is an
opt-in compression experiment. `--compression-level 9` increases compression
effort without changing the payload; the default remains level 7. Validation temporarily needs disk space for an
extracted copy of the input. No build or network access is involved.

This is a portable archive, not an installer; extraction requires a compatible
archive tool. It preserves file bytes and directory layout, not Windows ACLs or
alternate data streams. A source tree's completeness still depends on its
release staging/validation. Do not package a fast development build for release.

A local test used the installed **2.9.5** directory, which was the available
complete corpus. All 1,125 files and directory entries survived extraction with
matching hashes:

| Measurement | Result |
| --- | ---: |
| Input file bytes | 262,275,448 (250.13 MiB) |
| Verified `.7z` | 73,096,132 (69.71 MiB) |
| Compression time, one thread | 120.23 s |
| 7-Zip | 26.02 x64 |

This input includes installed uninstaller files and USB drivers. The result is
a packaging experiment, not a new release installer. It does not incorporate
the new binary changes, and is not an A/B comparison with the earlier 2.9.6 ZIP.
The archive SHA-256 is
`5728207b569c0b98afde28dcd39f177557ea0c15ac075482c4b89878e679f2b2`.

### Same-version official 2.9.6 control

The subsequent full-build run downloaded the pinned official 2.9.6 portable
ZIP and verified its SHA-256
`5aaf22e42f95accecfa122d23a835911f289ecc2ff606db3e83d637ddcc0a209`.
Repacking its complete, unchanged payload gives a same-version control:

| Measurement | Official 2.9.6 control |
| --- | ---: |
| Original portable ZIP | 106,598,059 bytes (101.66 MiB) |
| Uncompressed files | 261,683,165 bytes (249.56 MiB) |
| Verified solid LZMA2 `.7z` | 73,306,233 bytes (69.91 MiB) |
| Files retained and hash-verified | 1,119 |

The `.7z` SHA-256 is
`f3e39bc185aa400ad21eab1a757e1152aceb6ebf87b9448221a0cead538535fd`.
The report is `out/full-release/PrusaSlicer-2.9.6-official-repacked.7z.json`.
Compression took 234.156 seconds while sharing two logical CPUs with the
dependency build; this is not an uncontended compression-speed benchmark.
Two isolated CLI runs of STEP export, cube slicing and STEP slicing matched
after excluding only generated timestamps/version headers and the binary STL
description header (`out/full-release/baseline-self-smoke/report.json`).
This control still contains the official binaries, not the optimized build.

`tools/stage_windows_release.py` extracts the pinned baseline and stages the
new full-feature build without relying on a developer build's runtime junction.
It requires GUI, STEP, static dependencies and Release configuration; replaces
all five app binaries plus GMP, MPFR and WebView2Loader; preserves Mesa, VC/UCRT
runtimes and every runtime resource; adds the source license; and records file
hashes and source/build provenance. Its only baseline resource removal is the
known duplicate splash. Source-only PO catalogs and three platform/build icons
are excluded exactly as in the official Windows package.

`tools/verify_release_smoke.py` performs the paired offline CLI checks using
separate scratch working directories and `--datadir` paths. It records all
raw hashes and rejects unexpected output formats. Any differences beyond its
narrow header normalization remain visible; these smoke tests are not full
GUI, printer, localization or compatibility validation.

### Full application build and verified package

The native Windows build completed all default application and test targets.
The GUI, STEP importer, printer-host integrations, Mesa, bundled fonts, profiles
and examples remain included. The staged release contains 1,119 files; every
retained resource matches its official-release hash. The sole removed resource
is the identical second splash, and the source license was added.

| Measurement | Official 2.9.6 | New full-feature configuration |
| --- | ---: | ---: |
| Main DLL | 96,501,256 bytes (92.03 MiB) | 49,183,744 bytes (46.91 MiB) |
| Uncompressed payload | 261,683,165 bytes (249.56 MiB) | 214,039,683 bytes (204.12 MiB) |
| Same LZMA2 settings: level 7, 32 MiB dictionary | 73,306,233 bytes (69.91 MiB) | 66,084,778 bytes (63.02 MiB) |
| Stronger candidate compression: level 9, 64 MiB dictionary | Not measured with these settings | 65,161,761 bytes (62.14 MiB) |

The verified candidate is **38.01% smaller than the original portable ZIP**,
**9.85% smaller than the unchanged same-version LZMA2 control**, and **18.21%
smaller installed**. This is 66.08 decimal MB, not a 50 MB download. The full
package measurement supersedes estimates assembled from separate experiments.
The same-compression comparison includes compiler/dependency and signing
differences, not just an isolated linker-option change.

Artifact: `out/full-release/PrusaSlicer-2.9.6-lean.7z`; SHA-256:
`41e23f7c0b00e2c9480598e88cb96970b78e27042b483382c341fe320e78295f`.
Its adjacent JSON records compression settings and all 1,119 extracted hashes.
Compression took 262.578 seconds with one encoder thread; extraction, hash
verification and publication completed at 285.094 seconds. Some selected tests
ran concurrently on the same two CPU affinity slots, so this is not a controlled
compression-speed benchmark.

The stronger compression pass preserved the exact same payload and verified all
1,119 files after extraction. It saved another 923,017 bytes (0.88 MiB), giving
a **65.16 MB / 62.14 MiB** archive and **38.87%** reduction from the official ZIP.
The remaining gap is 15,161,761 bytes to a decimal 50 MB target, or 12.14 MiB to
a 50 MiB target. Neither target has been achieved.

Best ready-to-run original-geometry artifact:
`out/full-release/PrusaSlicer-2.9.6-lean-max.7z`; SHA-256:
`fb635763693f943c4b715d142480f6de708494d29c0067e39bb80de3da15a490`.
Compression took 154.438 seconds and verification/publication finished at
180.312 seconds; the encoder used one thread and the guarded tree peaked at
758,018,048 bytes (0.71 GiB) sampled RSS. The faster observed time than level 7
does not establish a speed advantage: cache state and competing workloads were
not controlled between these runs.

Validation passed: paired offline STEP export, cube slicing and STEP slicing
matched the official release after narrow header normalization; 56 selected
Catch2 cases / 531 assertions and five ImGui atlas rebuild configurations passed.
The tooling tests passed 79 cases with one Windows symlink-permission skip.
One inherited Config test references an absent malformed-INI fixture and has no
assertions; it is not meaningful malformed-input coverage. Full interactive
GUI, localization, WebView, software-rendering and printer compatibility QA,
installer generation, signing, startup and installed-memory benchmarks remain
outstanding. This artifact is an **unsigned portable archive**, not an installer.

There is also a build-artifact qualification: MSVC's main-DLL PDB link exhausted
the reserved memory headroom. Full-PDB and FASTLINK attempts were stopped by the
guard. A build-local CMake project include selected `/DEBUG:NONE` for the main
DLL only, retaining the `/Zi` compiler objects and explicit Release elimination.
PDB files are not part of either download, but this run did **not** produce a
complete standalone developer-symbol set. The interrupted PDB was preserved as
`out/full-release/incomplete-symbols/PrusaSlicer.partial.pdb`, not shipped.
Project defaults were not changed; the local override and generated Ninja file
are fingerprinted in `out/full-release/staged.json`.

The application build took 5,376.002 seconds of active guarded attempts,
including stopped links. It reused compatible dependency libraries and newly
built OCCT/Catch2, so this is not a clean default-build speed comparison.
Compilation used two workers; final links used one worker on two CPU affinity
slots, below-normal priority. The successful final invocation peaked at 4.08 GiB
summed RSS using an opt-in 4 GiB per-linker working-set cap; it retained at least
10.90 GiB sampled available physical RAM and 4.10 GiB commit headroom. Earlier
stopped attempts reached 9.10 GiB summed RSS. These paging-based resource limits
do not establish an algorithmic memory improvement or continuous hard bounds
for the whole process tree. Reproduction commands and all attempts are recorded
in `out/full-release/BUILD-NOTES.md`.

## Font atlas memory cleanup

After a successful texture upload, the GUI now releases the source font buffers
and both CPU atlas pixel buffers. It retains glyphs, icon rectangles, cursor/line
packing IDs, and the OpenGL texture. Failed uploads keep their source data.

The bundled ImGui's `ClearInputData()` also clears custom rectangles, so calling
it directly would break icon rendering. The new helper preserves this output
while discarding build-only data. DPI, language, and missing-glyph changes retain
the existing full atlas rebuild path.

The allocator-instrumented regression test confirms release of 455,188 source
bytes for Noto Sans alone, or **20,787,580 bytes (19.82 MiB)** when CJK is merged,
plus **5 × atlas width × atlas height** bytes for the alpha/RGBA CPU copies.
It checks actual ImGui frame rendering, glyph/icon/cursor UVs, reconstruction,
and final allocation cleanup. The standalone MSVC Debug test passed, and the
modified `ImGuiWrapper.cpp` compiled successfully against the existing Release
PCH and dependencies. Actual GUI working-set, GPU memory, and startup-time
changes remain unmeasured. This change
retains the font files and does not reduce the download size.

Run the standalone lifetime regression without the application dependencies:

```text
cmake -S tests/imgui -B out/imgui-tests
cmake --build out/imgui-tests --config Debug --parallel 1
ctest --test-dir out/imgui-tests -C Debug --output-on-failure
```
