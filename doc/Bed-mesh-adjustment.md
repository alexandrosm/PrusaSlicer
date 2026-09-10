# Optional bed-mesh adjustment stage

The full-release packaging pipeline can now run:

`build -> exact release stage -> adjusted release clone -> verified archive`

The adjustment is **off by default** and changes geometry. It is an experiment
for reducing offline release size, not a demonstrated lossless optimization.
It does not modify source resources, the original staged release, normal CMake
builds, or application binaries. All profiles and files remain available offline.

## Run after the full Release build

Use the STEP-enabled `lean-release` build and `stage_windows_release.py` described
in [Windows-package-size.md](Windows-package-size.md). The `build_fast.ps1`
GUI/CLI helper is a different, reduced development configuration.

Create an isolated Python 3.11 environment for the optional build-time tools:

```powershell
python -m venv out/mesh-stage-venv
& out/mesh-stage-venv/Scripts/python.exe -m pip install --only-binary=:all: -r tools/mesh-stage-requirements.txt
```

The pinned packages never enter the release. The geometry backend uses
[PyMeshLab's topology-preserving quadric simplifier](https://pymeshlab.readthedocs.io/en/latest/filter_list.html#meshing-decimation-quadric-edge-collapse)
and [trimesh proximity queries](https://trimesh.org/nearest.html) for sampled
point-to-surface checks. Original-vertex placement is used, with automatic
repair disabled. The unused fast-simplification comparison backend is now
separate in `tools/mesh-experiment-requirements.txt`; the normal optional stage
does not install it. Its first Cosmos proposal failed independent topology checks.

Run the adjustment and existing verified packager as one pipeline invocation.
The interpreter running `run_guarded.py` must have `psutil` installed; the child
uses the isolated environment. Use fresh output and log names for every run:

```powershell
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
$meshStagePython = (Resolve-Path out/mesh-stage-venv/Scripts/python.exe).Path
python -B tools/run_guarded.py --log-dir out/review-improvements/logs --label bed-adaptive-cold-v2 --memory-gib 1 --minimum-free-gib 6 --minimum-commit-gib 6 --cpu-count 2 --timeout-seconds 1200 -- $meshStagePython -B tools/adjust_bed_meshes.py --source out/full-release/staged/PrusaSlicer-2.9.6 --source-report out/full-release/staged.json --output out/review-improvements/bed-adaptive-cold-v2 --asset-cache out/review-improvements/mesh-cache --time-budget-seconds 900 --asset-seconds 180 --seed-divisor 8 --require-complete
```

The command performs only adjustment; use `--archive path.7z` to package afterward,
or run the verified packager separately so geometry and compression have separate
resource measurements. The output preserves the
inner `PrusaSlicer-2.9.6` root name. The adjustment report is the sibling
`bed-adaptive-cold-v2.json`; an archive has its own `.7z.json` extraction-verification
report. If final publication fails, an output without its completed report is
not a verified stage and must not be treated as one.

## Selection and checks

- Audit each shipped `resources/profiles/**/*.stl` once. Gantries, examples,
  shape-gallery assets and other resources are outside the adjustment scope.
- Adaptive search is the default **within this opt-in tool**, not a new normal
  release-build step. Every bed with at least 64 triangles is eligible. Start at
  one eighth of that asset's original triangle count, then make at most six QEM proposals
  by refining the pass/fail interval. Collection mean/median are reported but do
  not influence eligibility, seed, acceptance, or cache identity. Adding an
  unrelated printer does not invalidate an existing asset's result.
  `--seed-divisor 2..64` changes only the source-count seed (default 8), not QA.
- Every proposal uses the same error policy. Keep independently validated
  candidates while searching, not merely the first successful backoff. The
  original is always an option. The search is bounded and heuristic; QEM/QA need
  not behave monotonically, and finishing the schedule does not prove optimality.
- Rank accepted smaller candidates with a cheap standalone LZMA2 proxy (preset 1,
  1 MiB dictionary). Keep a top-three metadata shortlist and select only a result
  that improves this proxy over the original. Repeated identical candidate bytes
  reuse that asset's completed QA. The proxy is not an estimate of the final solid
  archive delta: compare complete verified archives before accepting a package.
- An additional exact planar pass considers at most 256 vertices in meshes up to
  300,000 faces. It replaces disjoint, strictly convex closed 3–12-triangle fans,
  removing two faces per proven patch. Exact rational predicates over the stored
  float32 coordinates prove coplanarity, global convexity, a strictly interior
  center, identical covered surface, boundary edges, and winding. It creates no
  new coordinates and rejects duplicate replacement faces. Concave fans, holes,
  collinear rings, approximate planes, and non-float32-exact inputs are skipped.
  The serialized result must still pass all independent QA. This local proof
  does not certify the input's global validity or make QEM candidates lossless.
- Check the serialized float32 STL, not only the pre-export candidate. The
  default tolerance is 0.05 mm for bounds and sampled bidirectional surface
  deviation, with 0.5% relative area/volume limits and topology/winding checks.
- Adaptive QA also checks sample-to-corresponding-component proximity, including
  nearby opposing thin sheets, and bidirectional samples of boundaries and
  creases of at least 45 degrees. Feature samples must find an actual segment
  within tolerance, not just another nearby face. The same 96 samples per
  direction compare against all target feature segments in chunks of 2,048.
  A 20-million-comparison budget and deadline bound the work; exhausting either
  is unproven. A dense original is no longer rejected solely for exceeding a
  fixed 32,768-edge cutoff. Feature and intersection checks run before expensive
  surface proximity, without changing acceptance thresholds or omitting checks.
- A bounded exact-predicate probe rejects demonstrated nonadjacent triangle
  intersections. It always labels absence `unproven`: shared-vertex pairs and
  unexamined faces are not globally certified. It is not a global self-intersection
  solver, and surface/feature samples remain samples.
- A rejected candidate is never shipped: a later acceptable backoff is used,
  or the exact original is retained. Parse/I/O/engine failures abort the stage.
- Preserve uniform binary STL facet attributes and the original binary header.
  Mixed facet attributes cannot be mapped safely through simplification and
  cause the original file to be retained. No metadata is silently discarded.
- Re-inventory source and clone. Every unchanged file, path and empty directory
  must match; only explicitly accepted bed meshes may differ. Record source
  report, tool and resource hashes, dependency versions, settings and attempts.
- Compress with one thread, level 9 and a 64 MiB dictionary by default, matching
  the previous best full-release measurement. Extract and verify every hash.

## Work budgets, cache and reproducibility

Default work limits are 240 seconds overall and 12 seconds per asset; the example
allows 900/180 seconds for a full measurement. Python loops check deadlines, and
candidate count, exact predicates, proximity batches and reader sizes are bounded.
Native QEM/NumPy calls cannot be interrupted mid-call, so these soft budgets can
overshoot. Use the external process guard for a hard timeout, RAM ceiling and CPU
affinity. Original inventory verification and final clone/hash verification are
not skipped to meet a time target. No speedup for a full run is promised here.

In-memory spatial contexts also reuse one nearest-vertex cKDTree and triangle
Rtree per immutable mesh, including the original across successive trials.
`bed_mesh_spatial.py` executes the pinned trimesh 4.12.2 closest-point and
face-normal tie-resolution code in a private namespace, substituting only the
cached candidate collector; it does not monkeypatch the library. Candidate
ordering and distance arithmetic stay unchanged. At most 250,000 candidate
point/triangle pairs are materialized per 32-point batch; exceeding the bound
is an explicit indeterminate rejection. Sharp-feature segments are cached too.
Parity fixtures compare all closest coordinates, distances and face IDs with
upstream, including coincident and opposing sheets. The helper includes the
upstream MIT notice and remains a build-time-only dependency adapter.

`--asset-cache` enables immutable, hash-checked local audit and completed-result
entries. Keys include original file bytes/hash, implementation and requirements
hashes, dependency versions, and the asset policy. It never trusts cached path
names to select an output. The cache and reports must be outside the source and
output trees; corrupted entries are ignored but never overwritten or deleted.
It is a trusted local cache, not a defense against someone rewriting both its
payload and checksums. Changing QA or tools invalidates results. Operational
wall-clock limits are not policy: increasing `--asset-seconds` can finish a slow
remaining asset without discarding completed results from other assets.

Audits and results are separate. An unfinished asset result is never cached as
final. `--require-complete` refuses to publish if any search times out, but already
completed cache entries remain reusable: run the same command again with the same
cache to skip them. If one asset repeatedly exceeds its individual budget, raise
that budget (maximum 300 seconds), reduce the explicitly recorded trial/sample
policy, or keep the original full release. A complete cache-hit run should produce
the same payload manifest as its complete cold run. Timings and cache counters in
reports necessarily differ. Without `--require-complete`, an explicitly incomplete
experimental stage may retain the last validated winner, and a later run can
improve it; do not present that as a reproducible completed measurement.

Every attempt records generation, serialization, serialized-read, QA and proxy
times. QA records analysis/topology, feature, intersection and each surface
direction separately, and emits its active phase and rejection reasons promptly.
Timeout exceptions carry partial QA metrics, not an empty success/failure label.
On incomplete-search rejection, an exclusive sibling
`<output>.incomplete-<unique-id>.json` persists the attempts, timings and provenance.
It explicitly has `verified_stage: false` and no output payload/package root;
temporary candidates are not published. Retrying never overwrites that diagnostic.

The previous density policy remains available as `--search density-backoff`:
mean (or `--statistic median`) sets eligibility and initial target, followed by
up to `--backoffs 3` halfway steps toward the original count. It uses the previous
QA and first-success choice for historical reproduction, not the stronger
adaptive checks. Its native operations require the external guard as well.

## Limits on preservation claims

The surface-distance test is sampled, not a proven global Hausdorff bound.
Topology and aggregate area/volume checks do not establish exact rendered
appearance, preservation of every sharp detail, or absence of self-intersections.

Bed STLs also feed `MeshRaycaster` picking and bounds used for camera framing
and multiple-bed spacing (`src/slic3r/GUI/3DBed.cpp`). Printable build volume is
configured separately by `bed_shape`, which this stage does not change. GUI
verification must cover rendering, picking through holes/around edges, camera
framing and multiple-bed layout, using an isolated data directory so downloaded
vendor assets cannot mask the candidate. CLI slicing checks do not cover this.

Do not label a generated candidate a fully validated no-functionality-loss
release until that work is complete. Raw STL savings and compressed archive
savings are separate measurements; report the actual repack result, not a
subtraction of raw savings from an older download.

## Focused validator diagnosis (2026-09-10)

A guarded read-only probe reused the same 125,748-triangle Cosmos proposal, with
the same original, tolerance and 4,096 samples per surface direction. Both
directional metric dictionaries match exactly before and after spatial caching,
including the 745 resolved opposite-normal nearest-face ambiguities.

| Local probe timing | Before | After |
| --- | ---: | ---: |
| Original-to-candidate surface query | 14.125 s | 4.672 s |
| Candidate-to-original surface query | 18.468 s | 6.406 s |
| Whole validator probe including reads | 40.547 s | 17.141 s |

These are individual runs, not a repeated controlled benchmark. Both used one
CPU and a 1 GiB guard. The earlier version rejected solely because its hard
feature-count cutoff also excluded the original's 51,948 feature edges. The
new version actually completed 192 feature samples and 9,752,352 segment
comparisons, with a maximum sampled feature deviation of 0.0287942 mm, below the
unchanged 0.05 mm tolerance. It also ran 256 exact intersection-pair checks;
global absence remained explicitly unproven. The candidate passed the current
QA. This does not establish GUI parity or a new archive-size result.

Logs are under `out/review-improvements/logs/`:

- `cosmos-existing-qem0-validator.log` and its `.json` guard report.
- `cosmos-existing-qem0-cached-validator.log` and its `.json` guard report.
- `mesh-query-timing-unit-v2.log`: 98 focused tests passed, including query parity,
  feature work bounds, partial timing diagnostics and cache/staging safety.

## Previous mean-density result (2026-09-10)

All sizes below use decimal MB. This is the **mean-density, tolerance-constrained
experiment**, not a replacement for the unmodified-resource baseline.
These numbers do not measure the newer adaptive policy, exact fan pass, cache,
or enhanced feature checks. See the later [review measurements](Review-improvements.md)
for the separate 75-file adaptive stage, its byte-identical warm-cache repeat,
and the measured contribution (or lack of contribution) of each technique.

| Measurement | Before adjustment | After adjustment | Reduction |
| --- | ---: | ---: | ---: |
| Verified portable 7z | 65,161,761 bytes | 62,612,052 bytes | 2,549,709 bytes (3.91%) |
| Uncompressed complete payload | 214,039,683 bytes | 198,572,983 bytes | 15,466,700 bytes (7.23%) |
| All 121 bed STLs | 35,285,807 bytes | 19,819,107 bytes | 15,466,700 bytes (43.83%) |
| Bed triangles | 668,891 | 359,557 | 309,334 |

The new download is **62.61 MB / 59.71 MiB**: 41.26% below the original official
106,598,059-byte ZIP, but still 12,612,052 bytes above 50 MB (10,183,252 bytes above
50 MiB). Same existing level-9/64 MiB-dictionary/one-thread packaging settings.
It is an unsigned portable archive, not an installer. No on-demand downloads,
profiles, fonts, binaries, or other resource paths were removed.

The stage adjusted 16 files in 32 candidate attempts. Six accepted their initial
mean-density proposal; seven accepted the first backoff; the three TriLAB models
needed the third backoff. The 19,270,050-byte arithmetic budget from the earlier
audit was not fully achievable under these checks. All affected files were
binary STL already, so these results include no ASCII-conversion savings.

Cosmos II changed from 251,496 to 20,866 triangles and from 12,574,884 to
1,043,384 bytes. It accounts for 11,531,500 bytes of the raw saving. All accepted
models retained exactly the original bounding box. Across the 16 models, the
largest bidirectional sampled distance was 0.0376307 mm, largest relative total
area change 0.0085182%, and largest checked component-volume change 0.430322%.
Every accepted original had suitable closed components, so all their component
volumes were checked; subsequent mixed-open/closed-component hardening does not
change those decisions. These measurements still do not prove GUI/picking parity.

The adjustment took 102.219 seconds, with a sampled peak process-tree RSS of
469,061,632 bytes (0.437 GiB), and at least 13.15 GiB available RAM. Compression
took 94.750 seconds; packaging through archive publication took 108.515 seconds.
These are individual local runs, not controlled speed benchmarks. No C++ rebuild
or WSL was involved.

Verification:

- The source release remained unchanged; only the 16 accepted STL paths differ.
- All 1,119 files and directory layout passed archive extraction/hash comparison.
- All six paired CLI STEP-export/cube-slicing/STEP-slicing commands passed with
  identical normalized results. This does not exercise bed GUI rendering/picking.
- The tooling suite ran 128 tests: 103 passed and 25 skipped in ordinary Python;
  the 24 optional geometry tests then all passed in the isolated environment.
  Net: 127 distinct tests passed, with one Windows symlink-privilege skip.
- A fresh run of the final pipeline, including the independent per-component
  volume hardening, reproduced the complete payload manifest byte-for-byte.
  It took 121.234 seconds with 469,991,424 bytes sampled peak RSS. The report is
  `bed-adjusted-mean-reproduced.json`; it fingerprints the final tool versions.

Artifacts under `out/full-release/`:

- `bed-adjusted-mean.json`: input provenance, every candidate attempt and payload.
- `PrusaSlicer-2.9.6-bed-adjusted-mean.7z` and its `.7z.json`: verified archive and
  full manifest. SHA-256:
  `e794f4bf4d4a5b8bd73a0b843e5dfa8d8aae59c318e1fa7a61457f28b26f1841`.
- `bed-adjusted-mean-smoke/report.json`: paired application smoke comparisons.
- `logs/bed-adjust-mean-v2.json`, `logs/bed-adjusted-mean-package.json`, and
  `logs/bed-stage-*-tests.json`: resource measurements and tests.

The first attempt stopped before output because it encountered facet attributes;
the completed implementation preserves uniform metadata. The initial Fast-QEM
Cosmos probe was rejected for duplicate/nonmanifold faces. An initial normals
test also rejected original-vs-original Cosmos because nearest opposite-facing
surfaces can be ambiguous. The corrected check requires an actual nearby
same-facing match, retains genuine reversed-surface rejection, and is covered by
regression tests; it is not an unconditional exemption for opposing normals.

## Tests

```powershell
& out/mesh-stage-venv/Scripts/python.exe -B -m unittest discover -s tools/tests -p test_bed_mesh_geometry.py
& out/mesh-stage-venv/Scripts/python.exe -B -m unittest discover -s tools/tests -p test_bed_mesh_certificates.py
python -B -m unittest discover -s tools/tests -p test_adjust_bed_meshes.py
```

The adaptive additions are covered by tiny fixtures for thin-sheet/component
orientation, missing features, exact convex six-fans, almost-planar/concave/star
rejections, demonstrated intersections with unproven absence, bounded refinement,
compression ranking, repeated-candidate QA reuse, incomplete-run retry, cache
corruption and policy invalidation, equal cold/warm payload manifests, cached
spatial-query parity, larger streamed feature sets, and persistent timing diagnostics.
