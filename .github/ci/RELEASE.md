# Full-feature hosted release experiment

`streamlining-release.yml` is deliberately separate from quick PR checks.
On the integration branch, push a commit whose message includes `[ci release]`
to request a run. Ordinary pushes do not start full builds. A manual dispatch
is also declared, but GitHub requires the workflow on the default branch before
dispatch events are available. No default-branch change is required for the
commit-marker path.

The three disposable `windows-2022` jobs use the same checkout layout (`source`),
Python/CMake/Ninja versions, profile, two build workers, and production cache
constructor. This is a same-path Windows x64 Release experiment, **not SDK
relocation support**. Moving checkouts or changing the toolchain remains a cache
miss, by design.

1. A cheap identity job configures the actual graph without building dependency
   sources, then publishes only a tiny marker and its full production inputs.
   The dependency job independently configures on another runner, restores this
   proof, and checks every input and payload hash **before source compilation**.
   Differences are reported by field. This preflight tests identity/transport,
   not SDK correctness, and has no rebuild-on-miss fallback.
2. The dependency producer computes
   its production fingerprint, and optionally restores an exact matching cache.
   Missing dependencies build from source. It must publish a hash-verified
   whole-prefix artifact before saving that exact entry to GitHub's cache.
3. A new runner independently configures and computes the fingerprint, checks
   equality before cache lookup, restores
   the producer's exact entry, and requires a verified `hit`. No source-build
   fallback is permitted for this proof. Transport restoration alone is not a
   successful dependency-cache test.
4. It builds the `lean-release` application default target: GUI, STEP, tests,
   all launchers and PDB generation remain enabled. Compiler caching is off in
   this first full-build experiment; the separate tiny native CI lane proves
   the sccache mechanism. Three warm application builds are timed afterward.
5. CTest runs the full registered suites with 600-second per-suite timeouts and
   a 30-minute outer guard. A Catch2 suite contains many individual cases; this
   is not a per-case timeout. A failure remains a failed workflow.
6. Independently of test-suite success, if compilation succeeded, the package
   phase verifies generated main-app symbols, compares CLI slicing/STEP outputs
   against the SHA-256-pinned official 2.9.6 ZIP, and packages the original
   geometry. Package staging records inherited runtime extras from the official
   ZIP; it is not a from-source rebuild of every bundled runtime component.

The producer can be a **cache hit**, so its elapsed time must not automatically
be labelled a cold build. Inspect the cache action outcome and dependency log.
The first empty-cache run measures source compilation including downloads,
pruning, hashing and publication. The consumer's restore report measures
verified prefix extraction separately from GitHub cache transport.

Production source/cache hashes are still the correctness gate. GitHub cache
keys are only a transport lookup. Only trusted pushes/dispatches run this
workflow and save cache entries; there is no `pull_request_target`, secret,
signing, upstream publication or deployment step. Future PR consumers should
restore only, with the same validation. Dependency caches contain no credentials.

After installing the pinned CMake wheel, every job prepends its native
`cmake/data/bin` directory to subsequent steps' PATH. The bootstrap locates files
through distribution metadata (without importing a workspace `cmake.py`) and
checks CMake/CTest against the wheel's SHA-256 RECORD entries. RECORD is local
wheel integrity evidence, not an independent publisher signature. Production
cache inputs still include the native executable's exact path, bytes and
version; the handoff proof also checks the configured `CMAKE_COMMAND`.

This avoids fingerprinting pip's generated launcher, whose bytes differed
between the producer and consumer in run `34536948353` despite matching CMake
versions. That run's old cache does not record the native binary hash, so it is
not relabelled or reused under the corrected identity. The first corrected run
requires a new dependency build; subsequent exact matches may reuse it. No
version-only fallback or excluded hash field is introduced.

The identity job has a 15-minute timeout; each build job has 180 minutes.
Heavy children use two CPUs, an 11 GiB
sampled process-tree RSS threshold, and 2 GiB physical/commit reserves. These
settings apply **only to disposable hosted runners**. The scripts reject local
execution and leave desktop build defaults unchanged. Sampling is not a hard
OS memory quota. A RAM/link failure is evidence to investigate, not permission
to disable symbols silently.

Compact logs and JSON measurements are retained for seven days. A successful
run also retains the unsigned portable archive as a CI artifact, not a GitHub
Release. Source builds, SDK caches, objects, PDBs and test inputs are not shipped
inside that archive. The STEP input is OCCT 7.6.1's `data/step/screw.step`, pinned
to commit `d2abb6d844231cb8f29be6894440874a4700e4a5` and SHA-256, downloaded into
the test workspace only; OCCT's LGPL-2.1/exception terms remain upstream.

Reported: current-revision build/restore timings, warm-build median, sampled
build memory, actual compressed/uncompressed package bytes, archive hashes,
extraction time, and CLI/STEP parity. This is not an upstream default-build A/B.
Extraction time is not installer time. Interactive GUI startup, steady-state
application RAM, signed installation and update-delta size remain unmeasured;
they need a separate controlled application/installer benchmark.

Legacy external dependency recipes still use CMake 3.31.6 in this experiment.
The quick lane additionally tests the modernized bundled wrappers and tiny
native builds with CMake 4.4.3. Passing that lane does not claim every downloaded
third-party project has been ported to CMake 4.
