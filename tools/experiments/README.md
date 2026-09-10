# Upstream dependency experiments

These tools are isolated investigations, not release configuration switches.
They read existing Windows/MSVC Ninja objects and dependency archives and write
only to a new experiment output directory. They do not invoke Ninja/CMake builds,
download dependencies, start WSL, or replace installed application binaries.

Requirements: an already built fast-GUI tree, an x64 Visual Studio developer
shell, Python 3.9+, `psutil`, and 7-Zip. Scripts constrain child processes to one
logical CPU at below-normal priority. They sample the process-tree working set
and terminate their own work if it exceeds 3 GiB or a step lasts 180 seconds;
they refuse to start a step with less than 6 GiB RAM available. Sampling is a
safety guard, not a hard kernel memory limit. Full application links can exceed
this budget, even when no compilation is needed.

## OpenVDB registration

`openvdb_float_init.cpp` derives from the pinned OpenVDB 8.2
[`openvdb/openvdb/openvdb.cc`](https://github.com/prusa3d/openvdb/blob/339ee88230da33e3fefb133d8c1a9e16bef09144/openvdb/openvdb/openvdb.cc).
Copyright Contributors to the OpenVDB Project; SPDX-License-Identifier:
MPL-2.0. The complete license is included in
[LICENSE-MPL-2.0.txt](LICENSE-MPL-2.0.txt), copied without abridgment from that
commit's [`LICENSE`](https://github.com/prusa3d/openvdb/blob/339ee88230da33e3fefb133d8c1a9e16bef09144/LICENSE)
in the locally cached source archive. Its archive SHA-256 is
`098c67620a3884b7c09775e5819e88ff09e6c69b09c07695a4301f77f9382664`,
matching `deps/+OpenVDB/OpenVDB.cmake`; the source file's copyright and SPDX
notices remain intact.

```text
python -B tools/experiments/openvdb_registration.py --output out/openvdb-experiment
```

Both the unchanged upstream initializer and a FloatGrid-only registration
initializer are compiled with the same flags from the pinned dependency's Ninja
edge. The replacement preserves ordinary metadata types, maps, delayed-load
metadata, logging, Blosc and initialization synchronization. It omits registration
of other grid types, PointIndex metadata and the point-data subsystem; it does
not remove their geometry implementations from the dependency archive.

Both variants link the same test object and existing PrusaSlicer static
libraries. The probe calls actual OpenVDBUtils APIs for mesh/volume conversion,
transforms, multipart meshes, distance samples, dilation, redistancing, CSG,
metadata, adaptive meshing and cancellation. Their output must match exactly.
This is a small deterministic corpus, not complete SLA/organic-support validation.

The output includes link maps, flags, logs, hashes, byte counts, compressed probes
and a JSON report. `--with-dll` attempts additional application DLL links only
after probe parity, under the same memory ceiling. The cached fast-GUI objects
omit STEP; even a successful DLL link is not full-release validation. Never
substitute these binaries into a release installer.

`--reuse-initializers-from` can reuse completed initializer objects from a prior
attempt after matching the recorded source hashes and compiler flags. Dependency
headers/libraries must remain unchanged between attempts; use a fresh run if they
have changed.

To test the separate external-Half dependency fix without compiling again:

```text
python -B tools/experiments/openvdb_without_half.py --reference out/openvdb-experiment --output out/openvdb-no-half
```

This verifies the reference probe's hash and removes exactly its explicit
`Half-2_5.lib` linker argument before relinking and comparing runtime output.

## Z3 testing-tactic registration

```text
python -B tools/experiments/z3_registration.py --output out/z3-experiment
```

This removes exactly the generated `subpaving` testing-tactic registration from
an experimental copy. It does not remove general solver theories, change the
default solver, or edit the cached source. Both registries use the same original
compiler flags. The probe verifies the expected registration difference and
checks exact rational models, SAT/UNSAT assumptions, push/pop and small
disjunctive layout constraints. This is not an arrangement-quality/timeout corpus.

An additional `--empty-registry` arm overrides only `install_tactics()` with an
empty implementation. It keeps the AST/theory registration and default solver
factories intact, but deliberately makes named tactic/probe/simplifier APIs
unavailable. The current PrusaSlicer call-site audit found no use of those APIs;
this must not be used as a general-purpose replacement for Z3. The probe checks
that the named tactic list is empty and repeats the same default-solver tests.

Executable and compressed-probe savings are not automatically savings in the
PrusaSlicer DLL or its installer. A larger application may retain the same code
through other references, and solid compression can change marginal savings.
