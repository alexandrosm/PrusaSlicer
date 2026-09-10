# Upstream dependency experiments (2026-09-09)

These results distinguish application build improvements from isolated dependency
experiments. No new full-feature PrusaSlicer DLL, installer, or downloadable
release was produced. Registry replacements remain outside production builds.

## Results

Both initializers/registries in each A/B pair use identical compiler flags from
the pinned dependency build. Probe object files, supporting archives, linker
optimization and compression settings are identical within each pair.

| Experiment | Uncompressed probe, before → after | Solid LZMA2 probe, before → after | Validation |
| --- | ---: | ---: | --- |
| OpenVDB FloatGrid-only registration | 8,152,064 → 6,417,408 bytes | 1,489,559 → 1,312,883 bytes | Actual PrusaSlicer OpenVDBUtils geometry/sample output matches exactly |
| Z3: omit the `subpaving` testing-tactic registration | 14,659,072 → 14,362,624 bytes | 3,963,482 → 3,889,594 bytes | Default-solver semantic smoke checks pass in both variants |
| Z3: omit all named tactic/probe/simplifier registration | 14,658,560 → 13,724,672 bytes | 3,963,622 → 3,746,155 bytes | Empty named-tactic list verified; same solver smoke checks pass |
| Remove external Half from the OpenVDB probe link | 8,152,064 → 8,152,064 bytes | Not recompressed | Byte-identical executable and matching runtime output |

The two Z3 rows are alternatives, not additive savings. Their baseline probe
sources differ slightly to check the broader registry expectation. Raw DEFLATE
savings were respectively 363,911, 140,443 and 407,601 bytes for the first three
rows. LZMA2 used one thread, level 7 and a 32 MiB dictionary.

The OpenVDB change saves 1.65 MiB uncompressed but only 172.5 KiB under LZMA2;
the broader Z3 experiment saves 912 KiB uncompressed and 212.4 KiB under LZMA2.
These are **probe-only** savings, not reductions in the application DLL or its
installer. An application may retain the same code through additional references,
and solid compression changes the marginal contribution of each file. The
50 MiB full-feature offline download remains an unproven target.

## OpenVDB: what was actually removed

PrusaSlicer's current OpenVDB adapter uses FloatGrid and typed geometry APIs.
OpenVDB's default initialization also registers nine other ordinary grid types,
point-index grids/metadata and the point-data subsystem.

The experimental replacement preserves all 17 ordinary metadata registrations,
eight transform/map registrations, delayed-load metadata, logging, Blosc and
the original mutex/atomic initialization lifecycle. Only unrelated grid/point
registration is removed. Integer/Boolean temporary grids used inside geometry
algorithms remain available; removing a registration is not removing their code.

The probe uses existing PrusaSlicer static libraries, not rewritten algorithms.
Its cases cover cube/sphere/transformed/multipart meshes, signed distances,
metadata, accessor resets, zero and nonzero dilation, both redistance overloads,
adaptive and offset meshing, union/difference/intersection, rescaling and
cancellation. Mesh and sample hashes, geometry counts and volumes match exactly.
Independent link maps resolve initialization to the intended override object;
point-subsystem map entries disappear in the selective variant.

This is not a full SLA/organic-support corpus. FloatGrid's virtual serialization
methods still retain I/O; selective registration does not sever that whole branch.
The initialization objects themselves shrink from 81,752,785 to 8,696,429 bytes,
but that object-file reduction must not be mistaken for a download saving.

## Z3: preserve the solver, change registration

The narrow cut removes one generated registration for Z3's testing-only
`subpaving` tactic. The broader arm replaces `install_tactics(tactic_manager&)`
with an empty implementation. Neither changes the default solver factories or
AST/theory registration.

The call-site audit found no use in PrusaSlicer production sources of named
tactic/probe/simplifier APIs, SMT-LIB input parsing or a `default_tactic` override.
Default solver/fallback paths construct their strategies directly. However, an
empty registry disables those named APIs for other Z3 consumers: this is **not**
a general-purpose substitute for Z3.

The shared probe checks exact rational models, Boolean constraints, SAT/UNSAT
with empty and nonempty assumptions, temporary assumptions, push/pop, three
distinct disjunctive rectangle layouts, reversed ordering, collision rejection
and recovery. It also verifies the expected registry difference. Real PrusaSlicer
arrangement-quality, collision and timeout regression testing remains necessary
before enabling either cut in production.

## Enabled improvement: remove the accidental OpenEXR dependency

The pinned OpenVDB already uses embedded Half. PrusaSlicer's older finder still
unconditionally required external IlmBase/Half. The finder now reads the OpenVDB
feature header and requests the correct external provider only when necessary;
legacy IlmBase and modern Imath configurations remain supported.

Eight configure-only cases passed, including the installed OpenVDB prefix and
expected failures when a genuinely required external provider is absent. Removing
`Half-2_5.lib` from the baseline probe link produces an executable with the same
SHA-256, and no external-Half archive entries appear in its map.

The selected GUI/CLI/full-release dependency closures now contain 23/20/25
packages, down from 24/21/26. OpenEXR accounts for 134 cached compilation steps
and 19,806,472 bytes (18.89 MiB) of dependency archives. Its recipe remains
available to explicit callers; the legacy all-recipes profile still builds it.
No existing cache or installed file was deleted, and no release-byte savings
are attributed to this cleanup.

## Resource controls and reproducibility

No WSL, full rebuild, dependency download or concurrent compiler jobs were used.
Child processes ran at below-normal priority with one logical CPU affinity;
TBB in the geometry probe was also limited to one worker. The experiment runner
sampled total process-tree working set and stopped work above 3 GiB. This is a
sampling guard, not a hard kernel-enforced limit.

The largest successful probe link sampled about 2.90 GiB; initializer compilation
sampled below 0.5 GiB, and the Z3 links below 0.8 GiB. Very short processes can
finish before the first sample, so a recorded zero is not zero memory use.
Full application DLL links exceeded the ceiling and were stopped, including a
retry with single-thread LTCG and no link map. No full-DLL savings are reported.

Reproducible scripts and limitations are in
[tools/experiments/README.md](../tools/experiments/README.md). Local reports:

- `out/upstream-experiments/openvdb-run4/report.json`
- `out/upstream-experiments/openvdb-no-half/report.json`
- `out/upstream-experiments/z3-run2/report.json`
- `out/upstream-experiments/z3-empty-run1/report.json`

These output directories are ignored generated artifacts. Earlier run directories
record preliminary/control failures and are not the reported A/B results.
