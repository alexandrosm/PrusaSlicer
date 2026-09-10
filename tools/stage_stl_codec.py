#!/usr/bin/env python3
"""Build an experimental offline-preparation stage using a reversible STL transform.

Input is immutable; transformed files are decoded and hash-checked before use.
The package includes a small native RestoreSTL.exe and PREPARE.cmd; consumers
must prepare once after extracting. This is NOT an ordinary ready-to-run portable
package. Whole-archive comparison, not per-file proxy estimates, decides success.
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
import zlib

from package_portable import inventory, sha256
from stl_codec import MODES, MAX_SOURCE_BYTES, decode, encode


HELPER_FILES = ("RestoreSTL.exe", "PREPARE.cmd", "README-OFFLINE-PREPARATION.txt")
PREPARE = b'@echo off\r\n"%~dp0RestoreSTL.exe" --root "%~dp0." --remove-encoded\r\nif errorlevel 1 exit /b 1\r\necho Offline preparation complete. You can now run prusa-slicer.exe.\r\n'
README = ("EXPERIMENTAL OFFLINE PREPARATION PACKAGE\r\n\r\n"
          "After extracting ALL files, run PREPARE.cmd once before PrusaSlicer.\r\n"
          "This reconstructs exact STL bytes and verifies SHA-256. It uses no network,\r\n"
          "Python, administrator rights, or separately installed geometry library.\r\n"
          "Only verified .pstlc intermediates are removed. Existing differing STLs\r\n"
          "are never overwritten. If interrupted, run PREPARE.cmd again.\r\n"
          "The geometry and all other application files are unchanged from the input\r\n"
          "stage. If that input contains simplified beds, this does NOT undo them.\r\n").encode("utf-8")


def reject_links(path):
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Link/reparse path rejected: {item}")


def native_restore(executable, root):
    options = {"creationflags": subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    started = time.monotonic()
    result = subprocess.run([str(executable), "--root", str(root), "--remove-encoded"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120, **options)
    if result.returncode:
        raise ValueError(f"Native decoder failed: {result.stdout}")
    return {"seconds": round(time.monotonic() - started, 3), "output": result.stdout.strip()}


def stage(source, output, decoder, modes=tuple(MODES), minimum_proxy_saving=256):
    started = time.monotonic()
    source, output, decoder = (Path(os.path.abspath(p)) for p in (source, output, decoder))
    report_path = output.with_name(output.name + ".json")
    for path in (source, output, decoder, report_path):
        reject_links(path)
    if source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError("Input and output must not overlap")
    if output.exists() or report_path.exists():
        raise ValueError("Refusing to overwrite output/report")
    if not decoder.is_file():
        raise ValueError("Build and supply the native decoder first")
    if not modes or any(mode not in MODES for mode in modes):
        raise ValueError("Unknown or empty modes")
    if type(minimum_proxy_saving) is not int or minimum_proxy_saving < 0:
        raise ValueError("Minimum proxy saving must be nonnegative")
    tool_paths = (Path(__file__), Path(__file__).with_name("stl_codec.py"),
                  Path(__file__).with_name("stl_restore.cpp"), Path(__file__).with_name("package_portable.py"))
    tool_hashes = {path.name: sha256(path) for path in tool_paths}
    decoder_hash = sha256(decoder)
    original = inventory(source)
    reserved = HELPER_FILES
    keys = {name.casefold() for name in original["files"]} | {name.casefold() for name in original["directories"]}
    if any(name.casefold() in keys for name in reserved):
        raise ValueError("Preparation filenames collide with the original stage")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".prusaslicer-codec-", dir=output.parent) as work_name:
        work = Path(work_name)
        clone = work / "payload" / source.name
        clone.mkdir(parents=True)
        for directory in original["directories"]:
            (clone / directory).mkdir(parents=True, exist_ok=True)
        rows = []
        for name, details in original["files"].items():
            origin, destination = source / name, clone / name
            if name.lower().endswith(".stl") and details["bytes"] <= MAX_SOURCE_BYTES:
                row = {"path": name, "original": details, "status": "retained", "candidates": []}
                data = origin.read_bytes()
                baseline = len(zlib.compress(data, 9))
                row["baseline_deflate_bytes"] = baseline
                best, best_size, best_mode = None, baseline - minimum_proxy_saving, None
                try:
                    for mode in modes:
                        transformed = encode(data, mode)
                        if decode(transformed) != data:
                            raise RuntimeError("Python decoder round trip differs")
                        compressed = len(zlib.compress(transformed, 9))
                        row["candidates"].append({"mode": mode, "bytes": len(transformed), "deflate_bytes": compressed})
                        if compressed < best_size:
                            best, best_size, best_mode = transformed, compressed, mode
                except ValueError as error:
                    row["reason"] = str(error)
                    best = None
                if best is not None:
                    transformed_name = name + ".pstlc"
                    if transformed_name.casefold() in keys:
                        raise ValueError("Transformed filename collision")
                    with (clone / transformed_name).open("xb") as stream:
                        stream.write(best)
                    row.update({"status": "transformed", "mode": best_mode, "transformed_bytes": len(best),
                                "transformed_sha256": hashlib.sha256(best).hexdigest(),
                                "deflate_bytes": best_size})
                else:
                    shutil.copyfile(origin, destination)
                rows.append(row)
                print(f"{row['status']}: {name}; proxy {baseline:,} -> {row.get('deflate_bytes', baseline):,}", flush=True)
            else:
                shutil.copyfile(origin, destination)
        shutil.copyfile(decoder, clone / reserved[0])
        (clone / reserved[1]).write_bytes(PREPARE)
        (clone / reserved[2]).write_bytes(README)
        transformed_inventory = inventory(clone)
        if transformed_inventory["files"][reserved[0]]["sha256"] != decoder_hash:
            raise RuntimeError("Decoder changed before being copied into the stage")
        # Verify the actual native, no-Python installation path on an isolated copy.
        verify = work / "verify" / source.name
        shutil.copytree(clone, verify)
        verification = native_restore(verify / reserved[0], verify)
        restored = inventory(verify)
        for name in reserved:
            if restored["files"].pop(name) != transformed_inventory["files"][name]:
                raise RuntimeError("Preparation helper changed during native verification")
        if restored != original:
            raise RuntimeError("Native reconstruction differs from source files/layout")
        if inventory(source) != original:
            raise RuntimeError("Source changed during transformation")
        if inventory(clone) != transformed_inventory:
            raise RuntimeError("Transformed stage changed during verification")
        if sha256(decoder) != decoder_hash or {path.name: sha256(path) for path in tool_paths} != tool_hashes:
            raise RuntimeError("Codec tool or decoder changed during transformation")
        output.mkdir()
        published = False
        report_created = False
        try:
            shutil.move(str(clone), str(output / source.name))
            report = {"schema": 1, "operation": "experimental-byte-reversible-stl-preconditioner",
                      "source": str(source), "package_root": str(output / source.name),
                      "source_payload": original, "payload": transformed_inventory,
                      "original_bytes": sum(f["bytes"] for f in original["files"].values()),
                      "transport_payload_bytes": sum(f["bytes"] for f in transformed_inventory["files"].values()),
                      "installed_bytes_including_helpers": sum(f["bytes"] for f in original["files"].values()) +
                                                           sum(transformed_inventory["files"][name]["bytes"] for name in reserved),
                      "transformed_files": sum(row["status"] == "transformed" for row in rows), "meshes": rows,
                      "verification": "all original files and directories match after native offline preparation",
                      "native_preparation": verification, "decoder_sha256": decoder_hash,
                      "tool_sha256": tool_hashes,
                      "ranking": "per-file DEFLATE proxy only; whole LZMA2 archive must be measured",
                      "limitations": ["Requires PREPARE.cmd after extraction; not ready-to-run portable",
                                      "Restores input geometry exactly, including any prior simplification"],
                      "seconds": round(time.monotonic() - started, 3)}
            with report_path.open("x", encoding="utf-8") as stream:
                report_created = True
                json.dump(report, stream, indent=2)
                stream.write("\n")
            published = True
        finally:
            if not published:
                # This invocation exclusively created this exact output directory.
                shutil.rmtree(output)
                if report_created:
                    report_path.unlink()
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--decoder", required=True, type=Path)
    parser.add_argument("--modes", nargs="+", choices=tuple(MODES), default=list(MODES))
    parser.add_argument("--minimum-proxy-saving", type=int, default=256)
    args = parser.parse_args()
    try:
        result = stage(args.source, args.output, args.decoder, tuple(args.modes), args.minimum_proxy_saving)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"STL transform failed: {error}\n")
    print(f"Verified native restoration of {result['transformed_files']} transformed STLs in {result['seconds']}s")


if __name__ == "__main__":
    main()
