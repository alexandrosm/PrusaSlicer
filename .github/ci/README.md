# Fork CI

The separate [full-feature release experiment](RELEASE.md) exercises a real
dependency build, fresh-runner cache restore, application build and packaging.
It requires an explicit trigger and does not run on every quick-check push.

The `Streamlining tooling` workflow validates the integration branch on a
disposable GitHub-hosted Windows 2022 runner. It needs no repository secrets or
Prusa's private release infrastructure. Official actions are pinned to commit
identities; Python tooling is version-pinned and installed from binary wheels.
Both CMake matrix lanes select the wheel's RECORD-verified native CMake/CTest
executables, not pip's generated launchers. Their compact bootstrap reports
live in `out/ci-bootstrap/`. Pure fake-wheel and cache-handoff regressions guard
this selection; the release lane separately proves a real fresh-runner handoff
before starting dependency compilation.

Coverage includes PowerShell 7 and 5.1 build-wrapper checks, dependency-package
cache and CMake policy fixtures, command quoting, a freshly compiled native STL
decoder, standalone ImGui atlas-lifetime tests, and selected Python regressions.
Six bundled wrappers are configured with explicit fixture providers; only the
dependency-free `miniz_static` and `glu-libtess` targets are built from that tree.
Their archive sizes/hashes and guarded build logs are retained. Placeholder
Boost/Eigen providers are never used to claim a successful application build.
The Python gate records exact test identities and rejects unexpected skips or
missing coverage. Only the documented Windows symlink-privilege skip is allowed.
Every `test_*.py` module discovered recursively under `tools/tests`,
`tests/build_tools`, and `.github/ci` must belong to exactly one lane in
`test-inventory.json`. New unassigned files, stale entries and duplicate names
fail before tests start. This inventory covers test modules, not deletion of
individual test methods; review still must protect the quality of assertions.

The separate geometry lane installs `.github/ci/requirements-geometry.txt`,
which includes the existing pinned geometry-experiment requirements. It runs
the numerical, spatial-index and geometry-certificate tests with real NumPy,
SciPy, trimesh, Rtree, PyMeshLab and fast-simplification. All geometry skips are
failures. No full production bed corpus is transformed, and geometry fixture
success is not proof of GUI/rendering or complete application parity.

Native fixtures and the Python suite run through the production resource guard
with one CPU, a 2 GiB sampled process-tree memory budget, 2 GiB free physical and
commit reserves, and a 240-second command timeout. These are CI-only settings;
the desktop build wrapper retains its larger safety reserves. Logs and test
results are retained as workflow artifacts for seven days.
Every invoked command now streams its complete merged stdout/stderr to a
dedicated file and records command arguments, exit status and elapsed time as
JSON. Compact output is under `reports/`: command logs, guard telemetry, exact
test identities, and `summary.json`/`summary.md`. The same concise summary is
appended to `GITHUB_STEP_SUMMARY`. Nested guard timings are included in their
parent command timings and must not be added twice.

Upload only `out/ci-streamlining/reports/**` and `out/ci-geometry/reports/**` for
normal success; optionally retain the complete corresponding output directory
on failure for scratch CMake trees. This reduces CI artifact transfer/storage,
not application download size. Python/CMake/Ninja and dependency versions are
captured in logs; the hosted runner, Visual Studio, Python patch version and
7-Zip remain platform inputs rather than a fully hermetic toolchain.

This is **not a full PrusaSlicer build**. It does not establish application or
installer parity, geometry-kernel correctness, GUI behavior, startup/RAM gains,
or release size. Geometry orchestration tests use fake-engine fixtures. Those
additional checks remain separate requirements before upstream submission.

For reproduction on a disposable Windows machine with Visual Studio x64 C++
tools, 7-Zip, PowerShell 7, and Python 3.12:

```powershell
python -m pip install --only-binary=:all: -r .github/ci/requirements-windows-tools.txt
$ciPython = (Get-Command python -CommandType Application | Select-Object -First 1).Source
& .github/ci/run-windows-tools.ps1 -Python $ciPython
```

The output path `out/ci-streamlining` must not already exist. Both runners accept
`-OutputDirectory` for a fresh alternate location (for example a CMake matrix).
The newer CMake lane exercises the reviewed bundled-wrapper configure contracts;
the full application/dependency build retains its separate supported-toolchain
requirements. The base tooling requirements remain on CMake 3.31.6.
The integration branch excludes itself from the inherited unfiltered release
push workflows; those workflows otherwise retain their existing behavior.

For the real geometry lane, use a separate Python 3.12 environment:

```powershell
python -m pip install --only-binary=:all: -r .github/ci/requirements-geometry.txt
$ciPython = (Get-Command python -CommandType Application | Select-Object -First 1).Source
& .github/ci/run-geometry-tests.ps1 -Python $ciPython
```

The inventory and its policy tests can be checked without numerical packages,
compilers, child process launches or downloads:

```text
python -B .github/ci/run_tool_tests.py --check-inventory
python -B -m unittest discover -s .github/ci -p test_ci_test_policy.py
```
