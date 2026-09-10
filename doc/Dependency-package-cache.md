# Dependency cache and graph contracts

`deps/DependencyGraph.cmake` is the sole dependency-edge declaration. Package
recipes define build steps, not a second copy of the graph. Dependency configure
writes `dependency-selection.json`; cache code consumes this resolved selection.
Missing non-system dependency targets fail configure. Linux libpng and the
Release-provided packages in nested MSVC Debug configurations are explicit
platform-provided dependencies.

## Two cache levels

The existing Windows whole-prefix `.compiled_cache/v1` archive remains the fast
path. Its recipe fingerprint now includes selected package trees rather than
every recipe. Resolved selected edges, effective options and shared build modules
remain inputs; editing an unselected package recipe does not invalidate it.

The additional `.compiled_cache/packages-v1` entries contain one package's
installed files, `input.json`, `manifest.json` and a hashed `COMPLETE` marker.
They are published through a new temporary directory and atomic, no-overwrite
directory rename. Payloads are copied, never hard-linked to mutable install trees.
Existing entries are immutable; corrupt entries are rejected, not overwritten.
These first-version artifacts are uncompressed directories: additional local
disk space buys independent reuse. They are not downloadable runtime packages.

Each package key includes its recipe/patch files, actual resolved child package
fingerprints, effective build options, toolchain/tool hashes, caller environment,
shared CMake/cache code and the exact absolute source/build/install paths.
It does not include unrelated recipes, raw graph-file contents, raw presets,
the full selected package set, or root/selection/exclusion variables. Shared build
logic/toolchain changes intentionally invalidate all affected artifacts.

Package reuse currently requires PowerShell 7 and supports native Windows x64
Release-only dependency builds without system-provided packages. Windows
PowerShell 5.1 retains whole-prefix caching; package caching is disabled because
its filesystem cmdlets cannot reliably access longer immutable payload paths.
No cross-checkout or cross-prefix ABI relocation is attempted.
`BUILD_SHARED_LIBS=ON`, Debug/Release mixing, additional
system-library ABI fingerprinting and cross-profile relocation need separate
validation before broadening support.

A nonempty `LibBGCode_SOURCE_DIR` disables **both** cache layers. Its editable
local source and `BUILD_ALWAYS` recipe semantics must take precedence over
pinned package artifacts; local-source binaries are not published either.
Direct CMake use of a package selection with this override also fails closed.

## Ownership and partial reuse

Ownership comes from the package's generated CMake `install_manifest.txt`.
Windows GMP/MPFR have explicit custom-install file lists; wxWidgets additionally
owns its manually copied WebView2 loader. The fingerprinted
`build_fast_prune_policy.ps1` predicate is the single authority both for deleting
known SDK waste and omitting those exact installed paths from package ownership.
It retains JPEG licenses. A missing manifest, any other missing listed file, or
unrecognized custom install prevents publication of that package. No filename
heuristics guess which library owns a file. Old restored prefixes with no build/install manifests remain usable through
the whole-prefix cache but cannot retroactively seed arbitrary package artifacts.

Read-only audit of this checkout's existing builds: `build-lean-release` has
complete OCCT (7,737 files) and Catch2 (225 files) install manifests; neither has
missing entries. The reused `build-fast-gui` prefix has no generated manifests,
and the 19 `build-fast-cli/builds` package directories have none either. Those
legacy Boost/JPEG builds cannot be retroactively published per package. Future
real install manifests can survive the explicit prune policy; GMP/MPFR remain
covered by their small recipe-specific lists.

On a stale managed whole-prefix cache, the existing wrapper discards only the
validated generated dependency tree and configures it freshly. Matching package
artifacts are verified and composed in a temporary directory, then moved into
the empty install prefix. Shared paths are allowed only when their hashes match;
conflicting ownership aborts partial reuse. Nonempty/unmanaged prefixes are not
overwritten. Incomplete packages build normally.

`Restore-FastDependencyCache` returns `partial` and sets
`Context.PackageSelectionPath` when independent packages were restored. The
wrapper reconfigures with `PrusaSlicer_deps_CACHE_SELECTION=<that JSON>`, then
builds `deps`. Reused packages become file-validation targets, not fake completed
ExternalProjects. CMake rechecks metadata, source/tool inputs, options, dependency
edges and restored files; the build target rechecks input and payload hashes.
Cached parents also carry transitive recipe/edge provenance, including children
that are source-built rather than cache hits. Editing such a child's recipe
invalidates the cached parent during both configure and build validation.
Initial wrapper configure clears any previous cache selection. Directly reusing
a stale selection fails closed and asks for the wrapper to resolve fresh keys.

Source/install trees must remain stable during building/publication. Package
publication rechecks recorded recipe/tool hashes and copied payload hashes.
This is a local optimization, not a signed supply-chain provenance mechanism.

## Lightweight tests

```powershell
./tests/build_tools/test_package_cache.ps1 -CMake <cmake.exe> -Ninja <ninja.exe>
```

The test builds compiler-free CMake ExternalProjects A, B depending on A, and C,
publishes real install-manifest artifacts, changes C and proves only C rebuilds
after A/B restoration. It also checks the production context constructor,
post-prune install ownership and license retention, unselected recipe/graph isolation,
transitive invalidation, options/path invalidation, corruption, stale selections,
nonempty-prefix preservation and Windows/Linux graph contracts. All operations
are confined to a fresh fixture scratch directory. No application build, network
access or WSL is involved.
