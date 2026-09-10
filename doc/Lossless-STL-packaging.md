# Experimental byte-reversible STL packaging

This build-time experiment reduces STL redundancy without changing geometry or
any other original STL byte. It is separate from the geometry-changing bed
adjustment stage. All resources remain available offline.

The resulting archive is **not a ready-to-run portable release**: after extracting
all files, run `PREPARE.cmd` once. The bundled, statically linked Windows
`RestoreSTL.exe` reconstructs and SHA-256-verifies the original meshes. No Python,
network, administrator rights, or geometry dependency is needed on the target
machine. This is not yet integrated into PrusaSlicer's installer or launcher.

## Measurements (2026-09-10)

All archive comparisons use one-thread LZMA2 level 9, a 64 MiB dictionary, and
the same base release and application binaries as the earlier staged build;
the STL treatment differs as shown in the table.

| Package | Download bytes | Mesh geometry |
| --- | ---: | --- |
| Previous retained-resource package | 65,161,761 | Original |
| Previous mean-density bed adjustment | 62,612,052 | Simplified, sampled QA |
| First reversible transform | 62,851,404 | Original, byte-exact reconstruction |
| Reversible transform with delta indices | 62,502,563 | Original, byte-exact reconstruction |
| Adaptive meshes plus reversible transform | 61,110,683 | Simplified; reconstruction is exact only relative to that simplified input |

The original-geometry delta-index row saves 2,659,198 compressed bytes (4.08%) against the first row.
It is 109,489 bytes smaller than the previous geometry-changing experiment;
the savings of the two techniques must not be added. The final row is a separately
measured combination with the newer, stronger-QA adaptive stage. It remains a
geometry-changing experiment without full GUI/picking validation. See
[Review-improvements.md](Review-improvements.md) for its complete measurements.

The delta-index package transforms 103 of 147 STLs and leaves the others intact.
Its transported payload is 191,240,151 bytes; after preparation, all 1,119
original files and 72 directories match the original 214,039,683-byte payload.
Three preparation helpers add 299,720 installed bytes. The preparation step
removes only the verified encoded intermediates, never original source files.

Actual-archive verification measured 6.250 seconds for extraction and 2.703
seconds for native preparation. The full hash-verification harness took 23.453
seconds and the sampled process-tree peak was about 0.11 GiB. These are warm
local measurements, not a cold installer/startup benchmark. Packaging took
111.110 seconds of compression / 129.671 seconds through guarded completion,
with a sampled peak of 757,428,224 bytes. These trials used one logical CPU,
below-normal priority, a 2 GiB sampled RSS ceiling, and 6 GiB physical-RAM and
commit reserves. No WSL was used.

Archive: `out/review-improvements/PrusaSlicer-2.9.6-lossless-stl-delta.7z`

SHA-256: `b3db5592bbe0cc0215290c5edf6d1fb1dcc36bdf651289ecfcfa014fc83d6681`

Reports are adjacent to each archive/stage; the actual-archive preparation
report is `out/review-improvements/prepared-original-delta.json`. The application
binaries are reused from the earlier complete build; this experiment does not
measure a new full application build, GUI startup, or runtime memory use.

## Format and safeguards

`tools/stl_codec.py` uses only Python's standard library. It accepts binary STLs
only when the 84-byte prefix and 50-byte facet records exactly consume the file.
ASCII STLs and unsupported/trailing-data files stay unchanged. Files over
256 MiB are outside this bounded experiment.

The versioned `PSSTL1` envelope stores mode, original length, original SHA-256,
and original STL prefix. Four transforms are compared:

1. Byte-plane transposition of the 50-byte facet records.
2. Exact 12-byte vertex/normal dictionaries and byte-plane index streams.
3. The dictionaries with XOR-predicted coordinate bits and index streams.
4. XOR-predicted dictionary coordinates with zigzag delta-coded indices.

Dictionary identity is by raw bytes, not floating-point equality. Facet order,
winding, normal bits, signed zeros, NaN payloads, the 80-byte header, and the
16-bit attributes are all preserved. This does not recompute normals, quantize,
weld by tolerance, reorder geometry, or simplify any mesh.

Per-file DEFLATE estimates choose candidates; they are not claimed as LZMA2
download savings. The full archive is compressed, extracted, and hash-verified.
The native decoder also verifies SHA-256 before publishing any reconstructed
STL. Existing different files are never overwritten. Matching files permit
safe reruns after interruption. Link/reparse paths and malformed lengths/index
ranges are rejected. Only exclusively created temporary files are rolled back.

The stage records hashes of its tools and decoder, verifies the copied decoder,
and refuses publication if those inputs change while it runs. Earlier measured
stage reports predate this provenance hardening; their full round-trip and
actual-archive checks passed, but use current tooling for new reproductions.

## Reproduction

From an x64 Visual Studio developer environment, build the tiny decoder into a
new directory using `tools/build_stl_restore.cmd out/stl-native`. It links the
CRT statically and uses Windows' BCrypt SHA-256 implementation.

Run these commands under `tools/run_guarded.py`, choosing new output/log paths:

```text
python -B tools/stage_stl_codec.py --source out/full-release/staged/PrusaSlicer-2.9.6 --output out/new-codec --decoder out/stl-native/RestoreSTL.exe
python -B tools/package_portable.py --source out/new-codec/PrusaSlicer-2.9.6 --output out/new-codec.7z --compression-level 9 --dictionary-mib 64
python -B tools/verify_stl_package.py --archive out/new-codec.7z --stage-report out/new-codec.json --output out/new-prepared
```

For native tests, set `PRUSA_STL_RESTORE_TEST_EXE` to that decoder. Tests exercise
all modes, exceptional float bit patterns, malformed headers/counts/indices,
corruption, Unicode paths, no-overwrite behavior, interrupted-work reruns, and
real archive extraction/preparation.
