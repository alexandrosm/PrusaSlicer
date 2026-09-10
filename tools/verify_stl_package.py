#!/usr/bin/env python3
"""Extract and prepare our report-verified experimental STL package offline.

Requires the archive's packaging report and the original codec stage report.
Checks the full transported and reconstructed manifests. Retains the prepared
tree for application smoke tests; never overwrites an existing tree/report.
Timing is a warm local extraction/preparation measurement, not installer/startup.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import time

from package_portable import find_seven_zip, inventory, run_seven_zip, sha256
from stage_stl_codec import HELPER_FILES, native_restore, reject_links


def verify(archive, stage_report, output, seven_zip=None):
    archive, stage_report, output = (Path(os.path.abspath(p)) for p in (archive, stage_report, output))
    archive_report = archive.with_suffix(archive.suffix + ".json")
    report_path = output.with_name(output.name + ".json")
    for path in (archive, archive_report, stage_report, output, report_path):
        reject_links(path)
    if output.exists() or report_path.exists():
        raise ValueError("Refusing to overwrite output/report")
    if any(path.is_relative_to(output) for path in (archive, archive_report, stage_report)):
        raise ValueError("Verification output overlaps an input")
    package_bytes, stage_bytes = archive_report.read_bytes(), stage_report.read_bytes()
    package = json.loads(package_bytes)
    stage = json.loads(stage_bytes)
    package_report_hash = hashlib.sha256(package_bytes).hexdigest()
    stage_report_hash = hashlib.sha256(stage_bytes).hexdigest()
    if (stage["operation"] != "experimental-byte-reversible-stl-preconditioner" or
            package["payload"] != stage["payload"] or
            archive.stat().st_size != package["archive_bytes"] or sha256(archive) != package["archive_sha256"]):
        raise ValueError("Archive or report identity mismatch")
    root_name = Path(package["source"]).name
    if not root_name or root_name in (".", ".."):
        raise ValueError("Invalid package root name")
    tool = find_seven_zip(seven_zip)
    output.mkdir(parents=True)
    extraction = output / "extracted"
    started = time.monotonic()
    run_seven_zip(tool, ["x", str(archive), f"-o{extraction}", "-mmt=1", "-sccUTF-8", "-bb0", "-bd"])
    extracted_seconds = time.monotonic() - started
    if sorted(path.name for path in extraction.iterdir()) != [root_name]:
        raise RuntimeError("Unexpected extracted root")
    root = extraction / root_name
    if inventory(root) != package["payload"]:
        raise RuntimeError("Extracted transport payload differs")
    native = native_restore(root / HELPER_FILES[0], root)
    prepared = inventory(root)
    source_only = {"files": dict(prepared["files"]), "directories": prepared["directories"]}
    for name in HELPER_FILES:
        if source_only["files"].pop(name) != package["payload"]["files"][name]:
            raise RuntimeError("Preparation helper changed")
    if source_only != stage["source_payload"]:
        raise RuntimeError("Prepared source files/layout differ")
    if (sha256(archive) != package["archive_sha256"] or sha256(stage_report) != stage_report_hash or
            sha256(archive_report) != package_report_hash):
        raise RuntimeError("Archive or source reports changed during verification")
    report = {"schema": 1, "operation": "verified-offline-stl-package-preparation",
              "archive": str(archive), "archive_sha256": package["archive_sha256"],
              "stage_report_sha256": stage_report_hash, "archive_report_sha256": package_report_hash,
              "prepared_root": str(root), "source_file_count": len(source_only["files"]),
              "source_uncompressed_bytes": sum(record["bytes"] for record in source_only["files"].values()),
              "installed_bytes_including_helpers": sum(record["bytes"] for record in prepared["files"].values()),
              "extraction_seconds": round(extracted_seconds, 3), "native_preparation": native,
              "seconds_including_full_hash_verification": round(time.monotonic() - started, 3),
              "verification": "all transported hashes/layout and all reconstructed original hashes/layout match",
              "timing_note": "warm local extraction/preparation, not a cold installer or GUI startup benchmark"}
    with report_path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--stage-report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seven-zip")
    args = parser.parse_args()
    try:
        report = verify(args.archive, args.stage_report, args.output, args.seven_zip)
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        parser.exit(1, f"Offline preparation verification failed: {error}\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
