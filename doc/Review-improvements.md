# Build and packaging review: implemented changes

Measured on Windows on 2026-09-10. Archive sizes use decimal MB; memory limits
use binary GiB. The application binaries in these package experiments come from
the earlier complete build. No new full application build, cold startup, GUI
memory, installer, or binary-update delta benchmark was performed in this review.

## Implemented

| Area | Change | Verification and limits |
| --- | --- | --- |
| Dependency reuse | Selected-closure whole-prefix keys, plus immutable per-package artifacts keyed by recursive dependencies | 43 focused checks, including real compiler-free ExternalProject install/restore/rebuild fixtures; not a timed production dependency rebuild |
| Dependency correctness | One authoritative dependency graph; shared prune/ownership policy; local source overrides disable caches; cached parents validate source-built descendants | Fresh actual GUI+STEP and CLI configurations passed in 35.6 s and 20.4 s, about 0.10 GiB peak each |
| Compiler cache | MSVC embedded object symbols (`/Z7`) and isolated, guarded sccache with persistent local artifacts | Native test produced a miss/write, then a genuine hit and zero compilations in a separate server; final linker symbol policy retained |
| Resource safety | Guarded dependency/application configure and build, including `lean-release`; conservative job selection and launch preflight | Default 6 GiB sampled tree RSS ceiling, 6 GiB free physical RAM and commit reserves, two logical CPUs and below-normal priority; sampled limits are not hard OS quotas |
| Edit/build policy | Full release PCH, stable-only development PCH; explicit-only translation helper; headless-only launcher trimming | Native two-TU fixture proved stable PCH reuse after first-party-header editing; not a whole-application speed benchmark |
| Mesh adjustment | Bounded source-specific adaptive search, compression-proxy ranking, exact restricted planar fan pass, stronger feature/component checks | Experimental geometry changes remain separate from the lossless package; sampled QA does not prove GUI/picking equivalence |
| Asset reuse | Content-addressed audit/result cache, source/tool/dependency/policy fingerprints, incomplete-search diagnostics and safe retry | Only complete bounded searches are reusable as final results; original mesh always remains an eligible candidate |
| STL transport | Four byte-reversible layouts, tiny statically linked native restorer, complete offline reconstruction and archive verification | No quantization, removed meshes, or on-demand resources; requires a one-time `PREPARE.cmd` after extraction |

Package caching currently requires PowerShell 7 and native Windows x64 Release
dependencies at the same absolute source/build/install paths. PowerShell 5.1
retains whole-prefix caching. Legacy prefixes without install manifests cannot
retroactively populate arbitrary package artifacts. Shared/toolchain changes
intentionally invalidate affected cache entries. See
[Dependency-package-cache.md](Dependency-package-cache.md).

The sccache prebuilt is local to `out/review-improvements/tools`; nothing was
installed system-wide or added to PATH. The wrapper clears ambient remote cache
configuration only in its own children, verifies its private server identity,
and tears down only owned processes. No WSL command was used.

## Measured package results

All our rows use the same one-thread, level-9 LZMA2, 64 MiB dictionary settings.
The official baseline uses ZIP, so the overall percentage is a package-to-package
comparison, not a claim that source changes alone eliminated that fraction.

| Package | Download bytes | Uncompressed transported payload | Original geometry? |
| --- | ---: | ---: | --- |
| Official 2.9.6 ZIP | 106,598,059 | See original package audit | Yes |
| Previous retained-resource 7z | 65,161,761 | 214,039,683 | Yes |
| Previous mean-density mesh experiment | 62,612,052 | 198,572,983 | No: simplified with sampled QA |
| New adaptive mesh experiment | 62,696,029 | 197,047,246 | No: simplified with stronger sampled QA |
| New reversible delta-index STL package | 62,502,563 | 191,240,151 | Yes: byte-exact after preparation |
| Adaptive meshes + reversible STL transport | 61,110,683 | 180,046,294 | No: reconstructs the simplified meshes exactly |

The new original-geometry package is **62.50 MB / 59.61 MiB**: 2,659,198 bytes
(4.08%) smaller than our retained-resource package, and 41.37% smaller than the
official ZIP. It is 109,489 bytes smaller than the earlier simplified-mesh
experiment. These are alternatives, not additive savings. The remaining gap to
50 decimal MB is **12,502,563 bytes**.

After preparation, all 1,119 original files and all 72 directories match the
214,039,683-byte input payload. Three helper files add 299,720 bytes, making the
prepared installation 214,339,403 bytes. Thus this technique reduces transport
size, not the application's installed resource size or runtime memory.

Actual archive extraction took 6.250 s; native reconstruction took 2.703 s.
These are single warm local runs, not cold installation benchmarks. The complete
extraction/preparation/verification harness took 23.453 s, with about 0.11 GiB
sampled process-tree peak. All six CLI/STEP smoke commands passed; the three
paired normalized outputs match the original staged build. No GUI runtime
performance claim follows from those checks.

Compression took 111.110 s (129.671 s through guarded completion) at a sampled
peak of 757,428,224 bytes. The archive is experimental and unsigned, and the
preparation hook is **not integrated into an installer or application launcher**.
See [Lossless-STL-packaging.md](Lossless-STL-packaging.md) for the exact artifact,
SHA-256, format, decoder tests and reproduction commands.

## Mesh validation performance finding

The first strengthened adaptive run exhausted its 600-second work budget and
correctly refused to publish an incomplete stage. Investigation found that the
new fixed feature-edge budget unnecessarily rejected dense models, and that
upstream proximity queries rebuilt the nearest-vertex tree for every 32-point
batch. The implementation now streams exact feature-distance comparisons within
an explicit pair budget and reuses immutable per-mesh spatial indexes.

On one saved Cosmos candidate, validation fell from 40.547 s to 17.141 s. The
surface-distance portion fell from 32.593 s to 11.078 s, with identical distance,
normal-ambiguity and component metrics. The new run also completed all 192
feature samples / 9,752,352 segment comparisons, which the former edge-count
guard rejected. The query adapter preserves pinned trimesh arithmetic and face
tie resolution without global monkeypatching. This is one local before/after
probe, not a repeated controlled benchmark.

The revised geometry/cache/spatial-query suite passed all 98 tests. In particular,
thin sheets, coincident surfaces, duplicate/unreferenced vertices, immutable
input snapshots, exact upstream closest-point/face-ID parity, budget exhaustion,
cache invalidation and incomplete-run retry have dedicated fixtures. A bounded
intersection probe still does not prove the absence of all self-intersections.

The complete revised stage subsequently finished all 121 bounded asset searches
in 554.110 s, with no incomplete results. It changed 75 bed files and retained
46 originals (including 15 below the minimum-size policy). Total bed triangles
fell from 668,891 to 364,721; the complete payload became 197,047,246 bytes,
saving 16,992,437 raw bytes. Cosmos retained 58,944 triangles rather than the
old experiment's 20,866 under the stronger checks. Every selected replacement
was a QEM candidate; the restricted exact-planar pass supplied no final winner.
Raw savings must not be interpreted as compressed download savings.

This payload is 1,525,737 raw bytes smaller than the old mean-density stage
despite retaining 5,164 more triangles. Nineteen ASCII beds were converted to
binary STL: their format-only arithmetic saving is 1,783,937 bytes, offset by
258,200 bytes for the extra triangles. The improvement is not evidence of more
aggressive overall geometric reduction. The exact pass proved 172 local fans
across 12 accepted proposals, but no exact-pass candidate won the final ranking,
so its contribution to this payload's saving is zero.

An independent report audit confirmed all 75 selected candidates have accepted
QA and matching selected-file hashes. Maximum selected bidirectional sampled
surface deviation was 0.048510203 mm; maximum feature-sample deviation was
0.049031023 mm; all bounds were exact. Maximum relative total area change was
0.017439916% and maximum checked component-volume change was 0.4540714%.
There were no unresolved orientation/component samples. All 75 intersection
absence results remain explicitly **unproven**. Ten identical input pairs reused
work within this cold run, explaining its 20 combined audit/result cache hits.

The cold guard recorded 555.485 s and 456,314,880 bytes sampled peak RSS, but
reported a cleanup error after the stage exited successfully with code zero.
The named process and both recorded launcher/runner processes were already gone
on inspection. The stage's completed source/clone verification report is valid;
the guard failure is retained in the log, not relabeled as a passed run.
Cleanup was subsequently hardened with fresh PID/creation-time checks before
actions and after errors, treating only confirmed exit/PID reuse as benign.
Live/inaccessible identities still fail closed. Structured events now preserve
phase and exception type; all 56 focused guard tests passed, including 11 new
cleanup-identity regressions. The old generic error cannot identify its exact
exception class retrospectively.

Stage/report: `out/review-improvements/bed-adaptive-cold-v2[.json]`.

The warm repeat reproduced the complete payload manifest byte-for-byte in
20.578 s (21.563 s through the guard), with 242 cache hits, zero misses and zero
invalid entries. Sampled peak RSS was 71,823,360 bytes, and guard cleanup passed.
That is about 26.9x faster than the cold stage in this single local comparison;
it is an asset-stage cache measurement, not a compiler or full-build speedup.
Warm report: `out/review-improvements/bed-adaptive-warm-v2.json`.

The adaptive stage's complete verified repack is **62,696,029 bytes**. It saves
2,465,732 bytes against the original-geometry 7z baseline, but is 83,977 bytes
larger than the old mean-density experiment despite its smaller raw payload.
The stronger checks and broader search did not deliver a smaller download alone.
Compression took 128.969 s / 147.469 s through guarded completion, with
757,207,040 bytes sampled peak RSS and successful cleanup. All 1,119 extracted
files and directory layout matched the stage. Archive:
`out/review-improvements/PrusaSlicer-2.9.6-bed-adaptive-v2.7z`; SHA-256:
`e89886a48c1b974cbf560cd99f4a6d7480ee7ac24bd74adc38aaf6c16569485b`.

## Combined experimental result

Applying the reversible transform to the complete adaptive stage produced a
**61,110,683-byte archive: 61.11 MB / 58.28 MiB**. This is a separately measured
combination, not an addition of projected savings. It is 42.67% below the official
ZIP, 4,051,078 bytes (6.22%) below the original-geometry 7z baseline, and
1,501,369 bytes (2.40%) below the old simplified-mesh archive. The remaining
gap to 50 decimal MB is 11,110,683 bytes.

This remains a **geometry-changing experiment**, not a proven no-functionality-
loss release. The reversible codec restores its simplified input byte-for-byte;
it does not restore the upstream geometry discarded by simplification. It also
requires the same offline `PREPARE.cmd` step. The conservative original-geometry
option remains 62,502,563 bytes.

The combined transport contains 180,046,294 uncompressed bytes. After preparation,
all 1,119 adaptive-stage files and 72 directories match, and the three helpers
bring the installed total to 197,346,966 bytes. All 101 encoded meshes restored
successfully from the actual archive, not just the pre-packaging stage.

The transform stage took 67.172 s (67.860 s guarded; 117,968,896 bytes peak RSS).
Compression took 116.812 s (133.640 s guarded; 757,383,168 bytes peak RSS).
Actual archive extraction took 7.297 s and native preparation 2.312 s; the complete
verification harness took 24.969 s (25.391 s guarded; 113,373,184 bytes peak).
All three guards passed cleanup. These are single warm local measurements, not
installer, startup or runtime-memory benchmarks.

Archive: `out/review-improvements/PrusaSlicer-2.9.6-bed-adaptive-lossless-transport.7z`.
SHA-256: `319b1c7c3acb086a30608ab20a3b663068e350f0bcc62eda212fa9441e2c68b2`.
Stage report: `out/review-improvements/codec-adaptive-v2.json`.
Actual preparation report: `out/review-improvements/prepared-adaptive-v2.json`.

The actually extracted/prepared combined package also passed all six CLI
STEP-export/cube-slicing/STEP-slicing commands, with all three normalized paired
outputs equal to the original staged build. This does not exercise bed GUI
rendering or picking. Report: `out/review-improvements/codec-adaptive-smoke/report.json`.

## Final regression check

The broad tool suite discovered 213 tests: 165 passed and 48 were skipped in
the ordinary Python environment. The separately pinned geometry environment's
98-test mesh suite passed in full; these scopes overlap and must not be added
as distinct test counts. Matching test identities across both logs gives **212
distinct passing tests**, with only the Windows symlink-privilege test still
skipped. Native decoder and actual archive/preparation tests
were enabled in the broad run. The final guard passed without cleanup errors.
Log: `out/review-improvements/logs/final-tools-regression-v2.log`.

The first broad invocation resolved a different bare `python` executable and
failed three module imports because that interpreter lacked `psutil`. Rerunning
with the intended virtual-environment executable's absolute path resolved that
test-environment issue. The failed log is retained, not hidden or rewritten.

## What is not yet measured or shipped

- A production clean-build or incremental-build speed A/B for these newest
  cache/PCH changes. Cache hits and target reductions are verified mechanisms,
  not a license to extrapolate wall-time percentages.
- Cold startup, steady-state application memory, installer performance, or
  update/delta size. A smaller solid archive does not establish smaller updates.
- Full GUI rendering/picking/camera/multibed parity for simplified geometry.
- A 50 MB complete download. No fonts, profiles, Mesa fallback, or functionality
  were removed to claim that target.
- Production adoption of the exploratory Z3/OpenVDB/OCCT internal-shaking
  probes. Their estimates are not counted in these measured package totals.

Build entry point: `./build_fast.ps1 -Profile lean-release -Jobs 2`. Build and
cache details are in [Fast-build.md](Fast-build.md); mesh policy and experimental
limitations are in [Bed-mesh-adjustment.md](Bed-mesh-adjustment.md).
