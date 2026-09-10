# Proposed upstream PR split

Status: preparation only, based on the local review evidence recorded on
2026-09-10. **No upstream PrusaSlicer PR has been created as part of this work.**
The validation below is local evidence, not a claim that GitHub CI has passed.

The reviewed modifications are collected on
[`streamlining/all-improvements`](https://github.com/alexandrosm/PrusaSlicer/tree/streamlining/all-improvements).
Next extract small, independently testable commits for future upstream proposals. Publishing
the integration branch does not mean every experiment is recommended for merge.
Use [Review-improvements.md](Review-improvements.md) for current measurements and
limitations; this document describes scope and sequencing, not projected savings.

## Starting point and history

The comparison base is `b028299`, the upstream 2.9.6 version bump. Existing local
commits are useful provenance, but not all are suitable PR boundaries:

| Existing commit | Treatment when splitting |
| --- | --- |
| `3efdac2` — retain MSVC Release linker optimization with PDBs | A narrow first PR candidate; extract its linker-policy change without unrelated profile work. |
| `4dc90b5` — reuse the identical splash image | An independent resource-deduplication PR candidate. |
| `4c7a985` — streamlined development profiles | A broad 46-file integration change, including profiles, dependency recipes, caching, unity/PCH work and Windows source changes. Split by purpose; do not submit it wholesale as an uncontested optimization. |
| `3e2f70f` — package-size measurements | Historical evidence; refresh relevant results in each PR rather than treating documentation as implementation. |

The newer work is organized into the following integration commits. These groups
keep experiments separate, but the larger build/cache groups still need finer
extraction before proposing independent upstream PRs.

| Integration commit | Scope |
| --- | --- |
| `091bad7` | Exclude local investigation artifacts; legitimate OBJ mesh resources remain trackable. |
| `7c3f20f` | Explicit-only helpers, headless launcher selection and redundant runtime-copy cleanup. |
| `f7b3f91` | Dependency graph consolidation, unused component trimming and Half-provider correction. |
| `891b149` | Font-atlas lifetime change and standalone regression tests. |
| `c76e615` | Guarded build profiles, compiler cache, dependency caches and their fixtures. |
| `ff1f16f` | Verified release staging and reversible offline STL transport. |
| `b3e7194` | Opt-in mesh search, QA and incremental asset caching. |
| `2c85453` | Isolated dependency-registration probes, provenance and third-party license. |
| `7c4f39f` | Measured results and initial upstream roadmap. |

Fork CI and publication follow-up changes are separate commits. Generated
compiler object files, binaries, build trees, compiled caches, downloaded tool
archives, `out/`, and `.agents/` remain local; they are not source changes to
publish. Curate or redact any attached logs that contain local paths.

## Track A: small production changes to propose first

These are low-risk candidates with explicit verification requirements, not a
claim that upstream acceptance or full platform validation is already assured.
Keep normal GUI/release functionality intact and keep independent changes in
separate PRs.

### A1. Preserve normal Release elimination when generating MSVC PDBs

Scope: the explicit Release `/OPT:REF` and `/OPT:ICF` linker policy in
`CMakeLists.txt`, retaining symbol generation. Do not combine it with disabled
symbols, FASTLINK experiments, compiler-cache installation or changed features.

Evidence: `3efdac2` records a controlled same-object relink. The later full
Windows package also has the expected smaller main DLL, but its package-to-package
comparison contains other compiler/dependency/signing differences. Neither is a
clean default-build speed benchmark.

Before submission: verify generated Release and Debug flags, complete a
symbol-producing Release link, and check debugger/stack-symbol behavior. The
earlier full-package build used a build-local `/DEBUG:NONE` workaround after
resource-limited PDB link attempts; that override is not the proposed change and
does not demonstrate a complete developer-symbol build.

### A2. Reuse the byte-identical splash resource

Scope: `GUI_App.cpp` uses `splashscreen.jpg` in both modes; remove only the duplicate
`splashscreen-gcodepreview.jpg`. The deleted input was 275,085 bytes and identical
to the retained JPEG. No image recompression or replacement is involved.

Before submission: verify editor and standalone G-code viewer resource lookup on
each packaged platform, and check install/package manifests for stale references.
Raw file removal is not an independently measured compressed-archive delta.

### A3. Keep developer helper executables outside normal builds

Scope: make `hintsToPot` and the standalone `avrdude-slic3r` executable explicit
targets rather than default build work. Keep translation generation available;
firmware flashing still links the avrdude library. These two small target changes
can be reviewed separately if different maintainers own their workflows.

Evidence: generated build-target checks and the local workflow fixtures cover
target selection. Before submission, build both explicit helpers and verify the
translation/update and firmware-flashing workflows. `EXCLUDE_FROM_ALL` must not
silently remove a maintainer's required packaging prerequisite.

### A4. Resolve OpenVDB's actual Half provider

Scope: make `FindOpenVDB.cmake` respect embedded Half, legacy IlmBase and modern
Imath configurations; remove the unnecessary OpenEXR edge from the selected
OpenVDB closure. Keep the OpenEXR recipe available to explicit users. This does
not replace OpenVDB initialization or remove registered grid types.

Evidence: eight finder cases include the installed prefix and expected failures
for genuinely missing external providers. Removing external Half from an
isolated probe link produced a byte-identical executable. The dependency closure
shrinks, but no release-download saving is attributed to this no-op link input.

Before submission: run supported Linux/macOS finder configurations and test
static/shared external-provider combinations. Land this correction before the
larger dependency-graph/cache work that consumes the corrected closure.

### A5. Release ImGui font-atlas build data after successful upload

Scope: `ImGuiWrapper.cpp`, the small `ImGuiFontAtlas.hpp` lifetime helper and
`tests/imgui`. Keep all fonts, glyphs, packed custom rectangles, cursor/line IDs
and the GPU texture. Failed uploads retain build input; later rebuilds reload it
through the existing full rebuild path. A bare `ClearInputData()` is insufficient
because this ImGui version also clears custom rectangles.

Evidence: the allocator-instrumented standalone test covers five rebuild
configurations, glyph/icon/cursor coordinates and rendering lifecycle. The
changed wrapper compiled against the existing Release dependencies/PCH. Tests
measure freed allocations, not application working-set or startup improvements.

Before submission: exercise real GUI DPI changes, localization/CJK text,
missing-glyph rebuilds, icons, cursors and upload-failure/retry handling on the
supported rendering platforms. This change does not shrink the font download.

### A6. Reduce unused dependency targets and improve headless selection

Split into reviewable, feature-specific proposals:

- Disable unused dependency executables, such as the curl CLI, without changing
  the linked library. Keep JPEG/Boost lean options explicitly scoped where their
  SDK output differs from a general-purpose dependency build.
- Disable unused wxWidgets components only with a documented application call-site
  audit and GUI build/runtime checks; this is broader than deleting a duplicate
  resource and should not be presented as universally risk-free.
- Gate GUI-only discovery, sources and Windows GUI/viewer launchers behind the
  existing GUI option. Keep the console target and core slicing/file-format
  dependencies. Preserve the complete default GUI/release configuration.

Fresh GUI+STEP and CLI configurations passed locally. They do not substitute for
new full application links and GUI smoke tests after the latest changes. Keep
optional STEP/test removal in development presets, not in a full-release PR.

The Windows ATL-to-WRL source change from `4c7a985` also deserves a separate
platform-maintenance review and its own supported-toolchain/COM-path checks;
do not hide it inside a cache or dependency-target PR.

## Track B: Windows development workflow and cache infrastructure

This is a larger maintainability and correctness project. Propose it only after
the small target/finder changes stabilize, in this order:

1. **Resource-safe wrapper and diagnostics.** Introduce the opt-in workflow,
   conservative job selection and process guard, with owned-process cleanup,
   physical-RAM/commit reserves and reproducible logs. Sampled stopping limits
   are not hard OS allocation quotas. Python/psutil are workflow requirements,
   not application runtime dependencies. Keep direct CMake builds usable.
2. **Compiler-cache-compatible symbols and PCH policy.** Separate the `/Z7`
   object-symbol handling, preserved linker-PDB policy, private sccache lifecycle,
   and explicit release/development PCH choices. Retain a no-cache path and older
   supported CMake behavior. Native fixtures prove cache reuse and stable-PCH
   reuse after a first-party-header edit; no full-project speed A/B exists for
   these newest changes. Keep unity compilation opt-in with its compatibility
   fixes and isolated sources reviewed together, rather than enabling it globally.
3. **One dependency graph and shared pruning/ownership rules.** Recipes must not
   redefine edges. Test Linux system libpng, Apple system dependencies, explicit
   platform packages, and nested MSVC Debug/Release handling. The pruning policy
   applies to the private fast-build SDK, not user installs or release resources.
4. **Verified dependency reuse.** Add selected-closure whole-prefix keys, then
   immutable package artifacts and partial restore. Require actual install-file
   ownership, hash verification, transitive source/tool/edge identity, and safe
   fallback. Do not forge ExternalProject completion stamps or skip required
   custom install side effects such as the WebView2 loader copy.

Current package reuse is limited to PowerShell 7, native Windows x64 Release,
unchanged absolute source/build/install locations, and no system-provided
dependency ABI assumptions. PowerShell 5.1 retains whole-prefix reuse. Local
`LibBGCode_SOURCE_DIR` disables both caches to preserve editable-source
`BUILD_ALWAYS` behavior. Cached parents validate source-built descendants too.
Cross-prefix relocation, other platforms, shared builds and mixed configurations
need separate support work; do not broaden the advertised contract implicitly.

See [Dependency-package-cache.md](Dependency-package-cache.md) and
[Fast-build.md](Fast-build.md). Before proposing this track, add CI coverage for
clean, incremental, interrupted, corrupt-cache and no-cache workflows, then
measure a production dependency build/restore using actual package artifacts.

## Track C: keep packaging and dependency experiments separate

These tools can live on the integration/research branch without becoming normal
PrusaSlicer release behavior.

| Experiment | Current evidence | Required before proposing production integration |
| --- | --- | --- |
| Verified Windows staging and portable compression | Full manifests and extracted hashes; fonts, profiles, runtime extras and notices retained | Coordinate with upstream release/install rules, supported tools, signing and maintenance ownership; do not substitute a reduced development build. |
| Reversible STL transport and native restorer | Actual original-geometry archive is 62,502,563 bytes; all 1,119 original files reconstruct byte-for-byte offline | Integrate preparation with an installer or launcher, review decoder/update/failure handling and licenses, and exercise real GUI launch. Current `PREPARE.cmd` is a manual extra step, not a production installer. |
| Adaptive bed simplification, mesh cache and spatial-query optimization | Complete cold/warm payload identity, sampled geometry QA and verified repacks | Geometry-changing and opt-in. Obtain rendering, picking, holes, camera and multibed parity evidence. No global surface-distance/self-intersection certificate is claimed. Do not merge this as a no-functionality-loss resource update. |
| OpenVDB registration and Z3 registry cuts | Isolated linked probes and bounded semantic/geometry tests | Run real application corpus/timeout/quality tests and full-feature links; retain general dependency API contracts or negotiate explicit specialized builds. Probe bytes are not app-download savings. |

The restricted exact-planar pass is implemented and tested, but selected no final
winner in the measured adaptive package, so it contributes no measured shipped
saving. Other dependency/OCCT exploration without an included, validated
production change remains research, not a PR promise. Neither font removal nor
system-font substitution is part of this plan.

Use [Lossless-STL-packaging.md](Lossless-STL-packaging.md),
[Bed-mesh-adjustment.md](Bed-mesh-adjustment.md) and
[Upstream-experiments.md](Upstream-experiments.md) for separate reproduction
contracts. The reversible codec preserves its input; when combined with simplified
meshes it cannot restore the upstream geometry discarded earlier. Download
savings from different arms are not additive, and a complete 50 MB download has
not been achieved.

## Current validation matrix

All entries are local results, with the qualification in the last column.
Generated report paths below are repository-relative evidence locations, not
files that must be committed.

| Scope | Evidence already available | Remaining gap |
| --- | --- | --- |
| Earlier full Windows application/package | Default application/test targets built; paired STEP/slicing smoke tests; 56 selected Catch2 cases / 531 assertions; [Windows-package-size.md](Windows-package-size.md) | Predates the newest workflow changes; main-DLL link used a recorded build-local `/DEBUG:NONE` override. Not a latest-source, full-symbol release build. |
| Latest GUI+STEP and CLI configuration | `out/review-improvements/logs/cache-review-configure-gui-v2.log` and `cache-review-configure-cli.log` passed | Configure checks, not fresh full compilation/linking or interactive GUI coverage. |
| ImGui lifetime and OpenVDB finder | Standalone native atlas regression and wrapper-object compile; eight finder configurations | Real GUI failure/rebuild coverage and native Linux/macOS validation still required. |
| Dependency cache/graph | 43 focused checks, including real compiler-free ExternalProject install/partial-restore fixtures and stale-descendant/local-source cases | Not a timed complete production dependency rebuild or cross-platform cache implementation. |
| Compiler cache/PCH | Separate native sccache miss/write then genuine hit; two-TU PCH fixture | No latest whole-application clean/edit-build benchmark or complete symbol-set verification. |
| Tooling and geometry regressions | Broad suite plus pinned 98-test mesh suite: 212 distinct local passing tests, one Windows symlink-privilege skip; `out/review-improvements/logs/final-tools-regression-v2.log` | Suites overlap: do not sum their raw totals. These are not GitHub CI results. |
| Experimental packages | Actual archive extraction/reconstruction and paired CLI/STEP outputs match their recorded input; see [Review-improvements.md](Review-improvements.md) | No signed installer, cold startup, runtime-memory, update-delta or full GUI parity benchmark. |

The first adaptive cold run's outer guard reported cleanup failure after a
successful stage exit. That failure remains recorded. Subsequent cleanup
regressions and the warm run passed; do not relabel the original guard result.

## Publication and upstream submission checklist

- Freeze the integration snapshot, include its new helpers/tests/license notices,
  and record exactly which revision each local report validates. Exclude generated
  outputs and private machine configuration.
- Run the integration branch's configured CI and record its actual outcome.
  Until that completes, describe CI as pending or unverified, never passed.
- Rebase or extract each proposed PR against the intended upstream base; its
  description should state one change, affected platforms, preserved behavior,
  local evidence and outstanding validation. Do not cherry-pick the broad profile
  commit wholesale when only one fix is intended.
- Submit independent Track A fixes first. Keep finder/graph changes ahead of the
  cache code that relies on them; keep resource controls ahead of heavier cache
  experiments. Track C requires separate release/geometry maintainer decisions.
- Before calling any resulting branch release-ready, complete the latest full
  GUI+STEP build with the intended symbol policy, relevant cross-platform CI,
  interactive GUI and packaging smoke tests, and signing/install validation.
  Mechanism tests and smaller archives are not substitutes for that work.
