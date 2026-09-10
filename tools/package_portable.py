#!/usr/bin/env python3
"""Repack a staged Windows release with bounded, lossless solid compression.

Requires Python 3.9+ and 7-Zip. Never alters or filters the input tree. The
archive is extracted and its entire file manifest compared before publication.
This checks packaging integrity, not the feature set of the supplied binaries.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inventory(root):
    """Reject links/junctions so a staged release cannot pull in a build tree."""
    files = {}
    directories = []

    def visit(path):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Stage contains a link or reparse point: {path}")
        relative = path.relative_to(root).as_posix()
        if stat.S_ISDIR(info.st_mode):
            if path != root:
                directories.append(relative)
            for child in sorted(path.iterdir(), key=lambda p: p.name):
                visit(child)
        elif stat.S_ISREG(info.st_mode):
            files[relative] = {"bytes": info.st_size, "sha256": sha256(path)}
        else:
            raise ValueError(f"Stage contains a non-regular file: {path}")

    visit(root)
    if not files:
        raise ValueError("Stage contains no files")
    return {"files": files, "directories": sorted(directories)}


def find_seven_zip(explicit=None):
    candidates = [explicit] if explicit else [shutil.which("7z"), shutil.which("7zz")]
    if not explicit:
        candidates.append(str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "7-Zip/7z.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise ValueError("7-Zip was not found; pass --seven-zip with its executable path")


def run_seven_zip(executable, arguments, cwd=None):
    options = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
    result = subprocess.run(
        [executable, *arguments], cwd=cwd, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", **options,
    )
    # Even 7-Zip's warning status can mean a locked/missing input file.
    if result.returncode != 0:
        raise RuntimeError(f"7-Zip exited with {result.returncode}:\n{result.stdout}")
    return result.stdout


def package(source, output, seven_zip, dictionary_mib=32, compression_level=7):
    started = time.monotonic()
    source = Path(os.path.abspath(source))
    output = Path(os.path.abspath(output))
    if not source.is_dir():
        raise ValueError(f"Stage directory does not exist: {source}")
    if output.suffix.lower() != ".7z":
        raise ValueError("Output must have a .7z extension")
    # Resolve the parents as well: an output junction pointing into the stage
    # must not make the archive include itself while it is being written.
    if output.resolve().is_relative_to(source.resolve()):
        raise ValueError("Output must be outside the stage directory")
    report_path = output.with_suffix(output.suffix + ".json")
    if output.exists() or report_path.exists():
        raise ValueError(f"Refusing to overwrite an existing archive/report: {output}")
    if dictionary_mib not in (16, 32, 64):
        raise ValueError("Dictionary size must be 16, 32 or 64 MiB")
    if type(compression_level) is not int or compression_level not in (7, 9):
        raise ValueError("Compression level must be the integer 7 or 9")
    original = inventory(source)
    seven_zip = find_seven_zip(seven_zip)
    version = run_seven_zip(seven_zip, ["i", "-sccUTF-8"]).strip().splitlines()[0]
    output.parent.mkdir(parents=True, exist_ok=True)
    # The temporary directory is created by this invocation, beside the output.
    # It is cleaned up automatically; publication failures also roll back this
    # invocation's outputs below. The input is always read-only.
    with tempfile.TemporaryDirectory(prefix=".prusaslicer-package-", dir=output.parent) as temp:
        temporary = Path(temp)
        archive = temporary / "payload.7z"
        arguments = [
            "a", "-t7z", f"-mx={compression_level}", "-m0=LZMA2", f"-md={dictionary_mib}m",
            "-ms=256m", "-mqs=on", "-mmt=1", "-sccUTF-8", "-bb0", "-bd",
            str(archive), "--", "./" + source.name,
        ]
        print(f"Compressing at level {compression_level} with one thread and a {dictionary_mib} MiB dictionary...", flush=True)
        compression_started = time.monotonic()
        run_seven_zip(seven_zip, arguments, cwd=source.parent)
        compression_seconds = time.monotonic() - compression_started
        print("Verifying extracted file hashes and directory layout...", flush=True)
        extraction = temporary / "verify"
        run_seven_zip(seven_zip, ["x", str(archive), f"-o{extraction}", "-mmt=1", "-sccUTF-8", "-bb0", "-bd"])
        if sorted(p.name for p in extraction.iterdir()) != [source.name]:
            raise RuntimeError("Archive root differs from the supplied stage")
        if inventory(extraction / source.name) != original:
            raise RuntimeError("Extracted payload differs from the supplied stage")
        if inventory(source) != original:
            raise RuntimeError("Source changed during packaging; retry with an immutable stage")
        report = {
            "schema": 1,
            "verification": "all file SHA-256 hashes and directories match the input stage",
            "feature_validation": "not performed; depends on the supplied release binaries",
            "source": str(source),
            "archive": output.name,
            "archive_bytes": archive.stat().st_size,
            "archive_sha256": sha256(archive),
            "uncompressed_bytes": sum(f["bytes"] for f in original["files"].values()),
            "file_count": len(original["files"]),
            "seven_zip": version,
            "compression": {"codec": "LZMA2", "level": compression_level, "dictionary_mib": dictionary_mib,
                            "solid_block_mib": 256, "threads": 1, "sort_by_type": True},
            "compression_seconds": round(compression_seconds, 3),
            "payload": original,
        }
        # Exclusive creation also prevents overwriting files created by another
        # invocation since the initial check. Publish only verified bytes.
        output_created = False
        report_created = False
        try:
            with output.open("xb") as dest:
                output_created = True
                with archive.open("rb") as stream:
                    shutil.copyfileobj(stream, dest)
            report["seconds_through_archive_publication"] = round(time.monotonic() - started, 3)
            with report_path.open("x", encoding="utf-8") as stream:
                report_created = True
                json.dump(report, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
        except BaseException:
            # Roll back only files this invocation exclusively created, never
            # a pre-existing archive or a concurrent invocation's report.
            if report_created:
                report_path.unlink()
            if output_created:
                output.unlink()
            raise
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Complete, immutable release directory")
    parser.add_argument("--output", required=True, type=Path, help="New .7z file outside the source directory")
    parser.add_argument("--seven-zip", help="7z/7zz executable (auto-detected by default)")
    parser.add_argument("--dictionary-mib", type=int, choices=(16, 32, 64), default=32)
    parser.add_argument("--compression-level", type=int, choices=(7, 9), default=7,
                        help="LZMA2 effort level; 9 is an opt-in compression experiment (default: 7)")
    args = parser.parse_args()
    try:
        result = package(args.source, args.output, args.seven_zip, args.dictionary_mib, args.compression_level)
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(1, f"Packaging failed: {error}\n")
    print(f"Verified {result['file_count']} files: {result['archive_bytes']:,} bytes "
          f"({result['archive_bytes'] / 1048576:.2f} MiB). Report: {args.output}.json")


if __name__ == "__main__":
    main()
