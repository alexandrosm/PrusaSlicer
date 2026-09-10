#!/usr/bin/env python3
"""Stage an offline Windows 2.9.6 release against the pinned official ZIP.

No downloads, builds, compression, or installation. Both commands require new
output directories. Reports live beside the output, outside the package tree.
"""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import subprocess
import zipfile

from package_portable import inventory, sha256


OFFICIAL_ROOT = "PrusaSlicer-2.9.6"
OFFICIAL_BYTES = 106598059
OFFICIAL_SHA256 = "5aaf22e42f95accecfa122d23a835911f289ecc2ff606db3e83d637ddcc0a209"
OFFICIAL_URL = "https://github.com/prusa3d/PrusaSlicer/releases/download/version_2.9.6/PrusaSlicer-2.9.6.zip"
APP_BINARIES = ("PrusaSlicer.dll", "OCCTWrapper.dll", "prusa-slicer.exe",
                "prusa-slicer-console.exe", "prusa-gcodeviewer.exe")
DEPENDENCY_BINARIES = ("libgmp-10.dll", "libmpfr-4.dll", "WebView2Loader.dll")
REMOVED_RESOURCE = "resources/icons/splashscreen-gcodepreview.jpg"
SOURCE_ONLY_ICONS = {
    "resources/icons/PrusaSlicer.icns": "macOS bundle icon; absent from the official Windows ZIP",
    "resources/icons/PrusaSlicer.ico": "Windows executable build input; absent from the official ZIP",
    "resources/icons/PrusaSlicer_128px.png": "platform icon; absent from the official Windows ZIP",
}


def validate_members(entries):
    """Validate all names before creating any output (including Windows aliases)."""
    seen = set()
    files = set()
    for entry in entries:
        name = entry.filename.rstrip("/")
        parts = name.split("/")
        if (entry.orig_filename != entry.filename or not name or "\\" in name or ":" in name or parts[0] != OFFICIAL_ROOT
                or any(part in ("", ".", "..") or part.endswith((".", " ")) for part in parts)):
            raise ValueError(f"Unsafe or unexpected ZIP path: {entry.filename}")
        key = name.casefold()
        if key in seen:
            raise ValueError(f"Duplicate/case-colliding ZIP path: {name}")
        seen.add(key)
        mode = entry.external_attr >> 16
        if stat.S_ISLNK(mode) or (entry.external_attr & 0x400):
            raise ValueError(f"ZIP link/reparse entry: {name}")
        if entry.flag_bits & 1:
            raise ValueError(f"Encrypted ZIP entry: {name}")
        if not entry.is_dir():
            files.add(key)
    for name in seen:
        parent = PurePosixPath(name).parent
        while str(parent) != ".":
            if str(parent) in files:
                raise ValueError(f"ZIP file/directory conflict: {name}")
            parent = parent.parent


def open_baseline(path):
    path = Path(path)
    if path.stat().st_size != OFFICIAL_BYTES or sha256(path) != OFFICIAL_SHA256:
        raise ValueError("Baseline must match the pinned official PrusaSlicer 2.9.6 ZIP SHA-256 and size")
    archive = zipfile.ZipFile(path)
    try:
        validate_members(archive.infolist())
    except Exception:
        archive.close()
        raise
    return archive


def reserve_output(output):
    output = Path(output).absolute()
    report = output.with_name(output.name + ".json")
    if output.exists() or output.is_symlink() or report.exists() or report.is_symlink():
        raise ValueError(f"Refusing to overwrite output or report: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()  # Exclusive directory creation also handles a concurrent writer.
    return output, report


def copy_stream(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)


def extract_entries(archive, output, skip=()):
    for entry in archive.infolist():
        relative = PurePosixPath(entry.filename).relative_to(OFFICIAL_ROOT).as_posix()
        if relative in skip:
            continue
        destination = output.joinpath(*PurePosixPath(entry.filename).parts)
        if entry.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            with archive.open(entry) as source:
                copy_stream(source, destination)


def baseline_report(archive):
    return {
        "url": OFFICIAL_URL, "archive_bytes": OFFICIAL_BYTES, "archive_sha256": OFFICIAL_SHA256,
        "root": OFFICIAL_ROOT,
        "zip_files": {
            PurePosixPath(e.filename).relative_to(OFFICIAL_ROOT).as_posix(): {
                "bytes": e.file_size, "compressed_bytes": e.compress_size, "crc32": f"{e.CRC:08x}"}
            for e in archive.infolist() if not e.is_dir()
        },
    }


def write_report(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write("\n")


def extract_baseline(baseline, output):
    with open_baseline(baseline) as archive:
        output, report_path = reserve_output(output)
        # On failure keep this new directory for inspection; never remove inputs.
        extract_entries(archive, output)
        payload = inventory(output / OFFICIAL_ROOT)
        report = {"schema": 1, "operation": "extract-official-baseline", "baseline": baseline_report(archive),
                  "payload": payload, "uncompressed_bytes": sum(e["bytes"] for e in payload["files"].values())}
        write_report(report_path, report)
        return report


def read_cache(build_root, source_root):
    cache_path = build_root / "CMakeCache.txt"
    cache = {}
    for line in cache_path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith(("//", "#")) and "=" in line:
            key, value = line.split("=", 1)
            cache[key.split(":", 1)[0]] = value
    for key in ("SLIC3R_GUI", "SLIC3R_ENABLE_FORMAT_STEP", "SLIC3R_STATIC"):
        if cache.get(key, "").upper() not in ("ON", "TRUE", "1", "YES"):
            raise ValueError(f"Full offline staging requires {key}=ON in the build cache")
    if cache.get("CMAKE_BUILD_TYPE", "").lower() != "release":
        raise ValueError("Staging requires a single-configuration Release build")
    if Path(cache.get("CMAKE_HOME_DIRECTORY", "")).resolve() != source_root.resolve():
        raise ValueError("Build cache source directory does not match --source-root")
    return cache, sha256(cache_path)


def verify_x64_pe(path):
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) < 64 or header[:2] != b"MZ":
            raise ValueError(f"Not a PE executable: {path}")
        stream.seek(struct.unpack_from("<I", header, 0x3c)[0])
        signature = stream.read(6)
        if len(signature) != 6 or signature[:4] != b"PE\0\0" or struct.unpack_from("<H", signature, 4)[0] != 0x8664:
            raise ValueError(f"Not an x64 PE executable: {path}")


def resource_plan(source_files, baseline_files):
    replacements, additions, exclusions = [], [], {}
    for relative in sorted(source_files):
        name = "resources/" + relative
        if name in baseline_files:
            replacements.append(name)
        elif name.startswith("resources/localization/") and name.endswith(".po"):
            exclusions[name] = "gettext source catalog; runtime uses the included compiled .mo catalogs"
        elif name in SOURCE_ONLY_ICONS:
            exclusions[name] = SOURCE_ONLY_ICONS[name]
        else:
            additions.append(name)  # Include unknown/new source assets conservatively.
    missing = sorted(name for name in baseline_files if name.startswith("resources/")
                     and name.removeprefix("resources/") not in source_files and name != REMOVED_RESOURCE)
    if missing:
        raise ValueError(f"Baseline runtime resources missing from source (requires explicit review): {missing}")
    if REMOVED_RESOURCE in baseline_files and REMOVED_RESOURCE.removeprefix("resources/") in source_files:
        raise ValueError("Source still contains the supposedly removed duplicate splash")
    return replacements, additions, exclusions


def copy_verified(path, destination, expected):
    with path.open("rb") as source:
        copy_stream(source, destination)
    if destination.stat().st_size != expected["bytes"] or sha256(destination) != expected["sha256"]:
        raise RuntimeError(f"Source changed while staging: {path}")


def check_output_location(output, source_root, build_root):
    output = Path(output).absolute()
    for candidate in (output, output.with_name(output.name + ".json")):
        for input_root in (source_root / "resources", build_root / "src"):
            if candidate.resolve().is_relative_to(input_root.resolve()):
                raise ValueError(f"Output/report must be outside staging inputs: {candidate}")


def untracked_production_inputs(source_root):
    paths = subprocess.check_output(["git", "-C", str(source_root), "ls-files", "--others",
                                     "--exclude-standard", "-z", "--", "src", "bundled_deps", "cmake"])
    result = {}
    for name in paths.decode("utf-8").split("\0"):
        if not name:
            continue
        path = source_root / name
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Untracked production input is a link: {path}")
        result[name] = {"bytes": info.st_size, "sha256": sha256(path)}
    return result


def fingerprint_build_file(path):
    path = Path(path).absolute()
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise ValueError(f"Missing build provenance file: {path}") from error
    for component in (path, *path.parents):
        component_info = info if component == path else component.lstat()
        if stat.S_ISLNK(component_info.st_mode) or getattr(component_info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Build provenance path contains a link/reparse point: {component}")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Build provenance input must be a regular file: {path}")
    return {"path": str(path), "bytes": info.st_size, "sha256": sha256(path)}


def build_file_provenance(build_root, source_root, cache):
    result = {}
    ninja = build_root / "build.ninja"
    # lstat notices dangling links too, unlike exists(). Non-Ninja generators
    # and small fixture builds need not have this generated file.
    try:
        ninja.lstat()
    except FileNotFoundError:
        pass
    else:
        result["build.ninja"] = fingerprint_build_file(ninja)
    variable = "CMAKE_PROJECT_PrusaSlicer_INCLUDE"
    configured = cache.get(variable, "")
    if configured:
        includes = []
        for name in configured.split(";"):
            if not name:
                continue
            path = Path(name)
            if not path.is_absolute():
                path = source_root / path
            includes.append(fingerprint_build_file(path))
        if not includes:
            raise ValueError(f"No file paths in configured {variable}")
        result[variable] = {"configured_value": configured, "files": includes}
    return result


def stage_release(baseline, output, source_root, build_root):
    source_root, build_root = Path(source_root).absolute(), Path(build_root).absolute()
    check_output_location(output, source_root, build_root)
    cache, cache_hash = read_cache(build_root, source_root)
    build_files = build_file_provenance(build_root, source_root, cache)
    source_resources = inventory(source_root / "resources")
    binaries = {}
    for name in APP_BINARIES + DEPENDENCY_BINARIES:
        path = build_root / "src" / name
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Build binary is a link: {path}")
        verify_x64_pe(path)
        binaries[name] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
    license_path = source_root / "LICENSE"
    license_info = {"bytes": license_path.stat().st_size, "sha256": sha256(license_path)}
    with open_baseline(baseline) as archive:
        official = baseline_report(archive)
        replace, add, exclude = resource_plan(source_resources["files"], official["zip_files"])
        output, report_path = reserve_output(output)
        package_root = output / OFFICIAL_ROOT
        skip = set(replace + list(binaries) + [REMOVED_RESOURCE, "LICENSE"])
        extract_entries(archive, output, skip)
        for name in replace + add:
            relative = name.removeprefix("resources/")
            copy_verified(source_root / name, package_root / name, source_resources["files"][relative])
        for directory in source_resources["directories"]:
            (package_root / "resources" / directory).mkdir(parents=True, exist_ok=True)
        for name, details in binaries.items():
            copy_verified(build_root / "src" / name, package_root / name, details)
        copy_verified(license_path, package_root / "LICENSE", license_info)
        if inventory(source_root / "resources") != source_resources:
            raise RuntimeError("Resource tree changed while staging")
        if (sha256(build_root / "CMakeCache.txt") != cache_hash or
                build_file_provenance(build_root, source_root, cache) != build_files):
            raise RuntimeError("Build configuration/provenance changed while staging")
        payload = inventory(package_root)
        revision = subprocess.check_output(["git", "-C", str(source_root), "rev-parse", "HEAD"], text=True).strip()
        diff = subprocess.check_output(["git", "-C", str(source_root), "diff", "--binary", "HEAD"])
        report = {
            "schema": 1, "operation": "stage-full-windows-release", "baseline": official,
            "source_root": str(source_root), "source_revision": revision,
            "tracked_source_diff_sha256": hashlib.sha256(diff).hexdigest(),
            "untracked_production_inputs": untracked_production_inputs(source_root),
            "build_root": str(build_root), "build_cache_sha256": cache_hash,
            "build_files": build_files,
            "build_configuration": {key: cache.get(key) for key in ("CMAKE_BUILD_TYPE", "CMAKE_CXX_COMPILER",
                "SLIC3R_GUI", "SLIC3R_STATIC", "SLIC3R_ENABLE_FORMAT_STEP", "SLIC3R_MSVC_DEBUG_SYMBOLS")},
            "built_app_binaries": list(APP_BINARIES), "built_dependency_binaries": list(DEPENDENCY_BINARIES),
            "replaced_resources": replace, "added_resources": add, "excluded_source_only_files": exclude,
            "removed_baseline_files": {REMOVED_RESOURCE: "identical splash now shared by both app modes"},
            "added_license": {"path": "LICENSE", **license_info},
            "inherited_baseline_files": sorted(set(official["zip_files"]) - skip),
            "payload": payload, "uncompressed_bytes": sum(e["bytes"] for e in payload["files"].values()),
            "feature_validation": "build-cache guards and payload checks only; run packaged CLI/GUI/STEP tests",
        }
        write_report(report_path, report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("extract", "stage"))
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="new parent directory; official package root is retained inside")
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--build-root", type=Path)
    args = parser.parse_args()
    if args.operation == "stage" and args.build_root is None:
        parser.error("stage requires --build-root")
    report = (extract_baseline(args.baseline, args.output) if args.operation == "extract" else
              stage_release(args.baseline, args.output, args.source_root, args.build_root))
    print(json.dumps({"operation": report["operation"], "files": len(report["payload"]["files"]),
                      "uncompressed_bytes": report["uncompressed_bytes"], "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
