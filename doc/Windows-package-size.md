# Windows package-size measurements

This document records controlled size experiments that preserve the Windows
Release feature set. Build-tree and dependency-prefix reductions are documented
separately in [Fast-build.md](Fast-build.md).

## Restoring Release linker elimination

PrusaSlicer generates PDBs for MSVC Release builds with `/Zi` and `/DEBUG`.
`/DEBUG` changes the linker's defaults to `/OPT:NOREF` and `/OPT:NOICF`, which
retains unreachable and duplicate COMDATs in the shipped PE image. Release
targets now request `/OPT:REF` and `/OPT:ICF` explicitly while retaining PDB
generation.

A controlled relink used the same GUI objects and libraries for both variants;
only the linker options changed. Both variants retained `/DEBUG`.

| Measurement | `/OPT:NOREF /OPT:NOICF` | `/OPT:REF /OPT:ICF` | Reduction |
| --- | ---: | ---: | ---: |
| `PrusaSlicer.dll` | 97,600,512 bytes | 49,126,912 bytes | 48,473,600 bytes (49.7%) |
| Raw DEFLATE stream, level 9 | 33,469,949 bytes | 20,059,174 bytes | 13,410,775 bytes (40.1%) |
| PE mapped-image size | 98,222,080 bytes | 49,741,824 bytes | 48,480,256 bytes (49.4%) |

The official 2.9.6 portable ZIP is 106,598,059 bytes. Its central directory
reports a 96,501,256-byte `PrusaSlicer.dll` occupying 32,220,379 compressed
bytes, closely matching the controlled no-elimination variant. A full release
package A/B is still required, but the controlled result projects roughly
12--13 MiB off the ZIP without removing code paths, features, or the separate
PDB artifact.

Fifteen alternating warm `--info` launches over the same STL produced identical
output. Median elapsed time was statistically unchanged (228.08 ms without
elimination and 229.62 ms with elimination). Median sampled peak working set was
51.64 versus 51.08 MiB, while sampled virtual size fell from 5,569.93 to
5,525.22 MiB. This short command does not measure cold I/O or full interactive
GUI startup; those require a packaged full-feature benchmark.

## Exact duplicate splash image

`resources/icons/splashscreen.jpg` and
`resources/icons/splashscreen-gcodepreview.jpg` were byte-identical (SHA-256
`19EBB4192070F302FF1226201EEC3C8D20B089EF3ECE6E9A8ACDF141F2E593F3`). Both
application modes now use the shared file. This removes 275,085 installed bytes
and 274,617 bytes from the official 2.9.6 ZIP with identical rendered pixels.

## Promising work not yet integrated

- A direct-document OCCT importer prototype reduced `OCCTWrapper.dll` from
  15,401,472 to 12,213,760 bytes. It disables metadata that the wrapper API does
  not expose, but it needs a diverse STEP equivalence corpus before it can be
  considered functionality-preserving.
- The Windows resource bundle contains about 0.9 MiB of translation sources and
  platform-specific files that are not used at runtime. The authoritative
  installer workflow is external to this repository, so filtering belongs in
  that pinned package manifest.
- Exact duplicate resource groups account for about 3.29 MiB before packaging.
  Alias compatibility and solid-archive behavior make this a lower-priority
  packaging change.
- Unused wxWidgets archives can be omitted from the dependency prefix, but they
  are not linked or shipped and therefore do not reduce the download.
- Z3 is statically linked and its parser code reaches the GUI binary. No safe
  component removal or measured final-binary reduction has been demonstrated.

Before release, run the complete test suite and exercise GUI/editor/viewer,
localization, WebView, software rendering, and STEP import using the exact
packager configuration.
