# Fork CI

The `Streamlining tooling` workflow validates the integration branch on a
disposable GitHub-hosted Windows 2022 runner. It needs no repository secrets or
Prusa's private release infrastructure. Official actions are pinned to commit
identities; Python tooling is version-pinned and installed from binary wheels.

Coverage includes PowerShell 7 and 5.1 build-wrapper checks, dependency-package
cache and CMake policy fixtures, command quoting, a freshly compiled native STL
decoder, standalone ImGui atlas-lifetime tests, and selected Python regressions.
The Python gate records exact test identities and rejects unexpected skips or
missing coverage. Only the documented Windows symlink-privilege skip is allowed.

Native fixtures and the Python suite run through the production resource guard
with one CPU, a 2 GiB sampled process-tree memory budget, 2 GiB free physical and
commit reserves, and a 240-second command timeout. These are CI-only settings;
the desktop build wrapper retains its larger safety reserves. Logs and test
results are retained as workflow artifacts for seven days.

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

The output path `out/ci-streamlining` must not already exist. CMake stays on
3.31.6 because bundled legacy projects predate CMake 4's compatibility changes.
The integration branch excludes itself from the inherited unfiltered release
push workflows; those workflows otherwise retain their existing behavior.
