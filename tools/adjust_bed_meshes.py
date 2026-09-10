#!/usr/bin/env python3
"""Opt-in build-time bed simplification: immutable full stage -> adjusted clone.

This is a geometry-changing experiment, not a lossless transformation. Failed
checks retain original bytes. Reports are outside the payload. No dependencies
are added to the application. Run under run_guarded.py, with one BLAS thread.
"""

import argparse
import hashlib
import importlib.metadata
import json
import lzma
import math
import os
from pathlib import Path
import shutil
import stat
import statistics
import tempfile
import time
import uuid

from package_portable import inventory, package, sha256
from bed_mesh_cache import AssetCache


class BudgetExceeded(TimeoutError):
    pass


def deadline_check(deadline):
    if time.monotonic() >= deadline:
        raise BudgetExceeded("Mesh work budget exhausted; unfinished validation is unproven")


def compression_proxy(path, check_cancel=lambda: None):
    """Bounded standalone ranking only; never an estimate of solid archive delta."""
    encoder = lzma.LZMACompressor(format=lzma.FORMAT_RAW,
        filters=[{"id": lzma.FILTER_LZMA2, "preset": 1, "dict_size": 1024*1024}])
    size = 0
    with Path(path).open("rb") as stream:
        while True:
            check_cancel()
            block = stream.read(65536)
            if not block:
                break
            size += len(encoder.compress(block))
    size += len(encoder.flush())
    check_cancel()
    return size


def adaptive_asset(geometry, source, replacements, row, seed, policy, deadline):
    """Bounded heuristic search: retain every independently valid best candidate.

    Fail/pass bracketing schedules trials; it does NOT assume QA monotonicity or
    certify an optimal triangle count. The original remains a candidate.
    """
    name, count = row["path"], row["original"]["triangles"]
    row.update(status="retained-original", after=row["before"], after_triangles=count,
               target_triangles=seed, shortlist=[], search_complete=False)
    if count < policy["minimum_triangles"]:
        row.update(status="policy-small-mesh", search_complete=True)
        return None
    check = lambda: deadline_check(deadline)
    winner = None
    try:
        check()
        mesh = geometry.read_stl(source / name, allow_attributes_for_audit=True)
        if sha256(source / name) != row["before"]["sha256"]:
            raise RuntimeError("Source changed before simplification")
        original_score = compression_proxy(source / name, check)
        best_rank = (original_score, row["before"]["bytes"], row["before"]["sha256"])
        row["original_proxy_bytes"] = original_score
        evaluated = {}

        def run_step(attempt, phase, operation):
            attempt["phase"] = phase
            began = time.monotonic()
            try:
                check()
                return operation()
            finally:
                attempt.setdefault("timings_seconds", {})[phase] = round(time.monotonic()-began, 6)

        def new_attempt(label, wanted):
            attempt = {"method": label, "requested_triangles": wanted, "timings_seconds": {}}
            row["attempts"].append(attempt)
            print(f"  {label}: generate target {wanted:,}", flush=True)
            return attempt

        def validation_phase(attempt, phase):
            attempt["validation_phase"] = phase
            print(f"    {attempt['method']}: QA {phase}", flush=True)

        def evaluate(candidate, attempt):
            nonlocal winner, best_rank
            label = attempt["method"]
            path = replacements / label / name
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                run_step(attempt, "serialization", lambda: geometry.write_stl(candidate, path))
                details = {"bytes": path.stat().st_size, "sha256": sha256(path)}
                attempt["candidate"] = details
                if details["sha256"] in evaluated:
                    validation, actual_count, score = evaluated[details["sha256"]]
                    attempt["duplicate_candidate_qa_reused"] = True
                else:
                    serialized = run_step(attempt, "serialized-read", lambda:
                        geometry.read_stl(path, allow_attributes_for_audit=True))
                    actual_count = len(serialized.faces)
                    attempt["actual_triangles"] = actual_count
                    validation = run_step(attempt, "validation", lambda: geometry.validate_candidate(mesh, serialized,
                        tolerance_mm=policy["tolerance_mm"], relative_limit=policy["relative_limit"],
                        sample_count=policy["sample_count"], enhanced=True, check_cancel=check,
                        progress=lambda phase: validation_phase(attempt, phase)))
                    score = None
                check()
            except ValueError as error:
                attempt["validation"] = {"accepted": False, "reasons": [str(error)]}
                print(f"    rejected during {attempt['phase']}: {error}", flush=True)
                return False
            attempt["validation"] = validation
            attempt.update(actual_triangles=actual_count, candidate=details)
            evaluated[details["sha256"]] = validation, actual_count, score
            if not validation["accepted"]:
                print(f"    rejected: {validation['reasons']}", flush=True)
                return False
            if details["bytes"] >= row["before"]["bytes"] or actual_count >= count:
                attempt["smaller"] = False
                return True
            if score is None:
                score = run_step(attempt, "compression-proxy", lambda: compression_proxy(path, check))
                evaluated[details["sha256"]] = validation, actual_count, score
            rank = (score, details["bytes"], details["sha256"])
            attempt.update(smaller=True, proxy_bytes=score, proxy_improves_original=score < original_score)
            if not any(r["sha256"] == details["sha256"] for r in row["shortlist"]):
                row["shortlist"].append({"method": label, "triangles": actual_count,
                                          "proxy_bytes": score, **details})
            row["shortlist"] = sorted(row["shortlist"], key=lambda r: (r["proxy_bytes"], r["bytes"], r["sha256"]))[:3]
            if score < original_score and rank < best_rank:
                best_rank, winner = rank, (path, details)
                row.update(status="adjusted", after=details, after_triangles=actual_count,
                           selected_method=label, selected_proxy_bytes=score)
            attempt["phase"] = "complete"
            print(f"    QA passed: {actual_count:,} triangles; proxy {score:,} bytes; "
                  f"selected={row.get('selected_method') == label}", flush=True)
            return True

        if policy["exact_planar"]:
            attempt = new_attempt("exact-planar", count)
            try:
                planar, proof = run_step(attempt, "generation", lambda:
                    geometry.simplify_exact_planar(mesh, max_candidates=256, check_cancel=check))
                row["exact_planar"] = proof
                attempt["planar_proof"] = proof
                if proof["proved_patches"]:
                    attempt["requested_triangles"] = len(planar.faces)
                    evaluate(planar, attempt)
                else:
                    attempt["phase"] = "skipped-no-proven-patches"
            except ValueError as error:
                row["exact_planar"] = {"skipped": str(error)}
                attempt["validation"] = {"accepted": False, "reasons": [str(error)]}
        wanted, tried, passed, failed = seed, set(), [], []
        for number in range(policy["max_attempts_per_asset"]):
            check()
            if wanted in tried or wanted >= count or wanted < 4:
                break
            tried.add(wanted)
            attempt = new_attempt(f"qem-{number}", wanted)
            try:
                candidate = run_step(attempt, "generation", lambda:
                    geometry.simplify(mesh, wanted, backend="pymeshlab"))
                valid = evaluate(candidate, attempt)
            except ValueError as error:
                attempt["validation"] = {"accepted": False, "reasons": [str(error)]}
                print(f"    rejected during {attempt['phase']}: {error}", flush=True)
                valid = False
                if "Original mesh is unsuitable" in str(error):
                    break
            (passed if valid else failed).append(wanted)
            upper = min(passed, default=count)
            lower = max((n for n in failed if n < upper), default=0)
            wanted = max(4, (upper+lower)//2)
        row["search_complete"] = True
    except BudgetExceeded as error:
        row["budget_exhausted"] = str(error)
        if row["attempts"] and "validation" not in row["attempts"][-1]:
            row["attempts"][-1]["validation"] = getattr(error, "validation_report",
                {"accepted": False, "reasons": [str(error)]})
        print(f"    INCOMPLETE: {error}; last phase "
              f"{row['attempts'][-1].get('phase') if row['attempts'] else 'input'}", flush=True)
    return winner


def cached_result_matches(body, row, seed):
    """Validate record shape and decisions, not just its accidental-corruption hash."""
    cached = body.get("row")
    if (not isinstance(cached, dict) or cached.get("search_complete") is not True
            or cached.get("before") != row["before"] or cached.get("original") != row["original"]
            or cached.get("target_triangles") != seed or not isinstance(cached.get("attempts"), list)):
        return False
    selected = body.get("selected_payload")
    if selected:
        return (cached.get("status") == "adjusted" and cached.get("after") == selected
                and 0 < cached.get("after_triangles", 0) < row["original"]["triangles"]
                and selected["bytes"] < row["before"]["bytes"]
                and cached.get("selected_proxy_bytes", math.inf) < cached.get("original_proxy_bytes", 0)
                and any(a.get("method") == cached.get("selected_method")
                        and a.get("candidate") == selected
                        and a.get("validation", {}).get("accepted") is True for a in cached["attempts"]))
    return (cached.get("status") in ("retained-original", "policy-small-mesh")
            and cached.get("after") == row["before"]
            and cached.get("after_triangles") == row["original"]["triangles"])


def reject_reparse_ancestors(path):
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Path contains a link or reparse point: {item}")


def output_paths(source, output):
    source, output = Path(os.path.abspath(source)), Path(os.path.abspath(output))
    report_path = output.with_name(output.name + ".json")
    for path in (source, output, report_path):
        reject_reparse_ancestors(path)
    if source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError("Input and output must not overlap")
    if output.exists() or report_path.exists():
        raise ValueError("Refusing to overwrite an existing output or report")
    return source, output, report_path


def adjustment_targets(original_count, density_target, area, backoffs):
    target = min(original_count, max(4, math.floor(density_target * area)))
    if target == original_count:
        return []
    targets = [target]
    for _ in range(backoffs):
        target = math.ceil((target + original_count) / 2)
        if target < original_count and target != targets[-1]:
            targets.append(target)
    return targets


def archive_destination(source, output, archive, source_report=None):
    """Preflight before adjustment: packaging must not mutate the original stage."""
    source, output, stage_report = output_paths(source, output)
    archive = Path(os.path.abspath(archive))
    report = archive.with_suffix(archive.suffix + ".json")
    if archive.suffix.lower() != ".7z":
        raise ValueError("Archive output must have a .7z extension")
    for path in (archive, report):
        reject_reparse_ancestors(path)
        if path.is_relative_to(source) or path.is_relative_to(output):
            raise ValueError("Archive and its report must be outside original and adjusted stages")
        if path == stage_report or (source_report is not None and path == Path(source_report).absolute()):
            raise ValueError("Archive output conflicts with a stage provenance report")
        if path.exists():
            raise ValueError("Refusing to overwrite an existing archive or report")
    return archive


def adjust(source, output, statistic="mean", tolerance_mm=0.05,
           relative_limit=0.005, sample_count=4096, backoffs=3, source_report=None, *,
           search="adaptive", asset_cache=None, time_budget_seconds=240,
           max_attempts_per_asset=6, asset_seconds=12, minimum_triangles=64, exact_planar=True,
           require_complete=False, seed_divisor=8):
    started = time.monotonic()
    source, output, report_path = output_paths(source, output)
    if statistic not in ("mean", "median"):
        raise ValueError("Statistic must be mean or median")
    if not math.isfinite(tolerance_mm) or tolerance_mm <= 0:
        raise ValueError("Tolerance must be finite and positive")
    if not math.isfinite(relative_limit) or not 0 < relative_limit < 1:
        raise ValueError("Relative limit must be between zero and one")
    if type(sample_count) is not int or sample_count < 32:
        raise ValueError("At least 32 validation samples are required")
    if type(backoffs) is not int or not 0 <= backoffs <= 8:
        raise ValueError("Backoffs must be between zero and eight")
    if search not in ("adaptive", "density-backoff"):
        raise ValueError("Unknown search policy")
    if not math.isfinite(time_budget_seconds) or not 1 <= time_budget_seconds <= 3600:
        raise ValueError("Time budget must be in [1,3600] seconds")
    if not math.isfinite(asset_seconds) or not 1 <= asset_seconds <= 300:
        raise ValueError("Per-asset budget must be in [1,300] seconds")
    if type(seed_divisor) is not int or not 2 <= seed_divisor <= 64:
        raise ValueError("Seed divisor must be an integer in [2,64]")
    if type(max_attempts_per_asset) is not int or not 1 <= max_attempts_per_asset <= 12:
        raise ValueError("Attempts per asset must be in [1,12]")
    if type(minimum_triangles) is not int or not 4 <= minimum_triangles <= 4096:
        raise ValueError("Minimum triangles must be in [4,4096]")
    cache = None
    if asset_cache is not None:
        asset_cache = Path(os.path.abspath(asset_cache))
        for target_path in (source, output, report_path):
            if asset_cache.is_relative_to(target_path) or target_path.is_relative_to(asset_cache):
                raise ValueError("Asset cache must not overlap input, output, or report")
        cache = AssetCache(asset_cache, reject_reparse_ancestors)
    deadline = started + time_budget_seconds
    original = inventory(source)
    provenance = None
    if source_report is not None:
        source_report = Path(source_report).absolute()
        reject_reparse_ancestors(source_report)
        report_bytes = source_report.read_bytes()
        digest = hashlib.sha256(report_bytes).hexdigest()
        parent_report = json.loads(report_bytes)
        if parent_report.get("payload") != original:
            raise ValueError("Source stage does not match the supplied source report")
        provenance = {"path": str(source_report), "sha256": digest}
    # Import only after path/option checks, keeping the normal build independent
    # of experimental geometry dependencies.
    import bed_mesh_geometry as geometry

    versions = {name: importlib.metadata.version(name) for name in
                ("numpy", "scipy", "pymeshlab", "trimesh", "rtree")}
    tool_paths = (Path(__file__), Path(geometry.__file__), Path(__file__).with_name("mesh-stage-requirements.txt"),
                 Path(__file__).with_name("bed_mesh_cache.py"), Path(__file__).with_name("bed_mesh_certificates.py"))
    tool_paths += (Path(__file__).with_name("bed_mesh_spatial.py"),)
    tool_hashes = {p.name: sha256(p) for p in tool_paths}
    policy = {"version": 2, "search": search, "tolerance_mm": tolerance_mm, "relative_limit": relative_limit,
              "sample_count": sample_count, "max_attempts_per_asset": max_attempts_per_asset,
              "minimum_triangles": minimum_triangles, "exact_planar": bool(exact_planar),
              "seed": ("original triangle count / seed_divisor; independent of other assets" if search == "adaptive"
                       else "collection density times original surface area"),
              "seed_divisor": seed_divisor,
              "proxy": "standalone LZMA2 preset1 dictionary1MiB; ranking only",
              "enhanced_qa": search == "adaptive"}
    names = sorted(name for name in original["files"]
                   if name.startswith("resources/profiles/") and name.lower().endswith(".stl"))
    if not names:
        raise ValueError("No profile bed STL files in the stage")
    rows = []
    for name in names:
        deadline_check(deadline)
        audit_inputs = {"kind": "audit-v2", "source": original["files"][name], "tools": tool_hashes,
                        "dependencies": versions}
        cached = cache.read(audit_inputs, 0, lambda body, payload:
            payload is None and isinstance(body.get("stats"), dict)
            and type(body["stats"].get("triangles")) is int and body["stats"]["triangles"] > 0
            and isinstance(body["stats"].get("surface_area_mm2"), (float, int))) if cache else None
        if cached:
            stats = cached[0]["stats"]
        else:
            mesh = geometry.read_stl(source / name, allow_attributes_for_audit=True)
            stats = geometry.mesh_stats(mesh)
            if sha256(source / name) != original["files"][name]["sha256"]:
                raise RuntimeError("Source changed during mesh audit")
            if cache:
                cache.write(audit_inputs, {"stats": stats})
        area = stats["surface_area_mm2"]
        if not math.isfinite(area) or area <= 0:
            raise ValueError(f"Invalid original mesh surface area: {name}")
        rows.append({"path": name, "original": stats,
                     "density": stats["triangles"] / area,
                     "before": original["files"][name], "attempts": []})
    target = getattr(statistics, statistic)(row["density"] for row in rows)
    print(f"Audited {len(rows)} beds; {statistic} surface density {target:.9g} triangles/mm2", flush=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".prusaslicer-bed-adjust-", dir=output.parent) as work:
        work = Path(work)
        replacements = work / "replacements"
        clone = work / source.name
        if clone == replacements:
            raise ValueError("Unsupported stage root name")
        changed = {}
        ordered = sorted(rows, key=lambda r: (-r["before"]["bytes"], r["path"])) if search == "adaptive" else rows
        for row in ordered:
            name = row["path"]
            count = row["original"]["triangles"]
            if search == "adaptive":
                seed = max(4, count//seed_divisor)
                inputs = {"kind": "validated-asset-v2", "source": row["before"], "tools": tool_hashes,
                          "dependencies": versions, "policy": policy, "seed": seed}
                cached = cache.read(inputs, row["before"]["bytes"],
                    lambda body, payload: cached_result_matches(body, row, seed)) if cache else None
                if cached:
                    cached_row, payload_path = cached[0]["row"], cached[1]
                    # Cache data never selects a destination path.
                    row.update(cached_row)
                    row["path"], row["cache_hit"] = name, True
                    if payload_path:
                        changed[name] = (payload_path, cached[0]["selected_payload"])
                    print(f"Cached {name}: {count:,} -> {row['after_triangles']:,} triangles", flush=True)
                    continue
                print(f"Searching {name}: {count:,} triangles; seed {seed:,}", flush=True)
                winner = adaptive_asset(geometry, source, replacements, row, seed, policy,
                                         min(deadline, time.monotonic()+asset_seconds))
                if winner:
                    changed[name] = winner
                if cache and row["search_complete"]:
                    body = {"row": {k: v for k, v in row.items() if k != "path"}}
                    if winner:
                        body["selected_payload"] = winner[1]
                    cache.write(inputs, body, winner[0] if winner else None)
                print(f"  {row['status']}: {row['after_triangles']:,} triangles; "
                      f"bounded search {'complete' if row['search_complete'] else 'INCOMPLETE'}", flush=True)
                continue
            deadline_check(deadline)
            row["search_complete"] = True
            targets = adjustment_targets(count, target, row["original"]["surface_area_mm2"], backoffs)
            row["target_triangles"] = targets[0] if targets else count
            row["status"] = "below-target" if not targets else "retained-original"
            if not targets:
                row["after"] = row["before"]
                row["after_triangles"] = count
                continue
            mesh = geometry.read_stl(source / name, allow_attributes_for_audit=True)
            print(f"Adjusting {name}: {count:,} -> target {targets[0]:,}", flush=True)
            for attempt_number, wanted in enumerate(targets):
                deadline_check(deadline)
                attempt = {"requested_triangles": wanted}
                candidate_path = replacements / str(attempt_number) / name
                candidate_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    candidate = geometry.simplify(mesh, wanted, backend="pymeshlab")
                    geometry.write_stl(candidate, candidate_path)
                    # Validate precisely the float32 geometry that will be shipped.
                    serialized = geometry.read_stl(candidate_path, allow_attributes_for_audit=True)
                except ValueError as error:
                    # Unsupported original topology or an invalid proposal is
                    # a rejection, not permission to repair the source mesh.
                    attempt["validation"] = {"accepted": False, "reasons": [str(error)]}
                    row["attempts"].append(attempt)
                    print(f"  retained/retrying: {error}", flush=True)
                    continue
                validation = geometry.validate_candidate(mesh, serialized, tolerance_mm=tolerance_mm,
                    relative_limit=relative_limit, sample_count=sample_count)
                details = {"bytes": candidate_path.stat().st_size, "sha256": sha256(candidate_path)}
                smaller = details["bytes"] < row["before"]["bytes"] and len(serialized.faces) < count
                attempt.update({"actual_triangles": len(serialized.faces), "validation": validation,
                                "smaller": smaller, "candidate": details})
                row["attempts"].append(attempt)
                if validation["accepted"] and smaller:
                    changed[name] = (candidate_path, details)
                    row.update({"status": "adjusted", "after": details,
                                "after_triangles": len(serialized.faces)})
                    print(f"  accepted {len(serialized.faces):,} triangles, saves {row['before']['bytes'] - details['bytes']:,} bytes", flush=True)
                    break
                print(f"  retained/retrying: {validation.get('reasons', [])}; smaller={smaller}", flush=True)
            if row["status"] != "adjusted":
                row["after"] = row["before"]
                row["after_triangles"] = count
        incomplete = sorted(row["path"] for row in rows if not row["search_complete"])
        if require_complete and incomplete:
            diagnostic_path = output.with_name(output.name + ".incomplete-" + uuid.uuid4().hex + ".json")
            reject_reparse_ancestors(diagnostic_path)
            diagnostic = {"schema": 1, "operation": "incomplete-bed-mesh-search-diagnostic",
                "verified_stage": False, "search_complete": False, "incomplete_assets": incomplete,
                "source": str(source), "source_report": provenance, "source_payload": original,
                "source_still_matches": inventory(source) == original,
                "tools_still_match": {p.name: sha256(p) for p in tool_paths} == tool_hashes,
                "asset_policy": policy, "dependencies": versions, "tool_sha256": tool_hashes,
                "work_budget_seconds": time_budget_seconds, "per_asset_budget_seconds": asset_seconds,
                "elapsed_seconds": round(time.monotonic()-started, 3), "beds": rows,
                "limitations": ["No stage or archive published; candidate files were temporary",
                                "Completed cache entries may be reused; incomplete results are not final"]}
            serialized_diagnostic = json.dumps(diagnostic, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
            with diagnostic_path.open("x", encoding="utf-8") as stream:
                stream.write(serialized_diagnostic)
            print(f"Incomplete search diagnostic: {diagnostic_path}", flush=True)
            raise RuntimeError(f"Incomplete bounded searches for {len(incomplete)} assets; no stage published. "
                f"Diagnostic: {diagnostic_path}. Reuse --asset-cache to skip completed assets; "
                "raise --asset-seconds for an individually timed-out asset.")
        clone.mkdir()
        for directory in original["directories"]:
            (clone / directory).mkdir(parents=True, exist_ok=True)
        expected = {"files": dict(original["files"]), "directories": original["directories"]}
        for name in original["files"]:
            origin = changed[name][0] if name in changed else source / name
            dest = clone / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with origin.open("rb") as src, dest.open("xb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            if name in changed:
                expected["files"][name] = changed[name][1]
        payload = inventory(clone)
        if payload != expected:
            raise RuntimeError("Adjusted clone differs outside accepted mesh replacements")
        if inventory(source) != original:
            raise RuntimeError("Source changed during mesh adjustment")
        if provenance and sha256(source_report) != provenance["sha256"]:
            raise RuntimeError("Source report changed during mesh adjustment")
        report = {
            "schema": 1, "operation": "experimental-bed-mesh-adjustment", "source": str(source),
            "package_root": str(output / source.name),
            "source_report": provenance, "source_payload": original, "payload": payload,
            "source_uncompressed_bytes": sum(f["bytes"] for f in original["files"].values()),
            "uncompressed_bytes": sum(f["bytes"] for f in payload["files"].values()),
            "changed_files": sorted(changed), "bed_count": len(rows), "beds": rows,
            "settings": {"statistic": statistic, "density_basis": "per-file original triangle surface area in mm2",
                         "simplifier": "pymeshlab topology-preserving QEM, original-vertex placement",
                         "target_density": target, "tolerance_mm": tolerance_mm,
                         "relative_limit": relative_limit, "sample_count": sample_count, "backoffs": backoffs},
            "dependencies": versions,
            "tool_sha256": tool_hashes, "asset_policy": policy,
            "work_budget_seconds": time_budget_seconds,
            "per_asset_budget_seconds": asset_seconds,
            "search_complete": not incomplete, "incomplete_assets": incomplete,
            "require_complete": bool(require_complete),
            "cache": None if cache is None else {"root": str(cache.root), "hits": cache.hits,
                                                   "misses": cache.misses, "invalid": cache.invalid},
            "validation": "unchanged nonselected bytes; accepted geometry checks and sampled surface distances",
            "limitations": ["Geometry-changing, not lossless; samples do not prove a global distance bound",
                            "Complete means the finite heuristic trial schedule finished, not a global size optimum",
                            "Standalone compression ranking does not predict whole solid-archive savings",
                            "Self-intersection absence and complete feature coverage remain unproven",
                            "No full GUI rendering, picking, camera or multibed parity established",
                            "No compressed saving measured until the adjusted payload is packaged"],
            "seconds_before_publication": round(time.monotonic() - started, 3),
        }
        report["raw_bytes_saved"] = report["source_uncompressed_bytes"] - report["uncompressed_bytes"]
        if {p.name: sha256(p) for p in tool_paths} != tool_hashes:
            raise RuntimeError("Mesh tools changed during adjustment")
        # Catch non-JSON/nonfinite engine metrics before publishing any output.
        serialized_report = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        # Reserve report ownership before moving the clone so a concurrent
        # report can never be associated with this invocation's payload.
        # A failed final publication may retain our clone for inspection, but
        # its incomplete report is removed; inputs and prior outputs stay intact.
        report_created = False
        try:
            with report_path.open("x", encoding="utf-8") as stream:
                report_created = True
                output.mkdir()
                clone.rename(output / source.name)
                stream.write(serialized_report)
        except BaseException:
            if report_created:
                report_path.unlink()
            raise
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Complete immutable portable root")
    parser.add_argument("--output", required=True, type=Path, help="New parent for the adjusted portable root")
    parser.add_argument("--source-report", type=Path, help="Verify input against this stage report's payload")
    parser.add_argument("--statistic", choices=("mean", "median"), default="mean")
    parser.add_argument("--tolerance-mm", type=float, default=0.05)
    parser.add_argument("--relative-limit", type=float, default=0.005)
    parser.add_argument("--sample-count", type=int, default=4096)
    parser.add_argument("--backoffs", type=int, default=3)
    parser.add_argument("--search", choices=("adaptive", "density-backoff"), default="adaptive")
    parser.add_argument("--asset-cache", type=Path, help="Optional trusted local content-addressed cache outside both stages")
    parser.add_argument("--time-budget-seconds", type=float, default=240)
    parser.add_argument("--max-attempts-per-asset", type=int, default=6)
    parser.add_argument("--asset-seconds", type=float, default=12)
    parser.add_argument("--seed-divisor", type=int, default=8,
                        help="Start QEM at original triangle count / divisor (2..64); not collection density")
    parser.add_argument("--minimum-triangles", type=int, default=64)
    parser.add_argument("--no-exact-planar", action="store_true")
    parser.add_argument("--require-complete", action="store_true",
                        help="Refuse stage publication on any timed-out search; completed cache entries remain reusable")
    parser.add_argument("--archive", type=Path, help="Optionally run verified packaging after adjustment")
    parser.add_argument("--seven-zip")
    parser.add_argument("--dictionary-mib", type=int, choices=(16, 32, 64), default=64)
    parser.add_argument("--compression-level", type=int, choices=(7, 9), default=9)
    args = parser.parse_args()
    try:
        if args.archive:
            args.archive = archive_destination(args.source, args.output, args.archive, args.source_report)
        result = adjust(args.source, args.output, args.statistic, args.tolerance_mm,
                        args.relative_limit, args.sample_count, args.backoffs, args.source_report,
                        search=args.search, asset_cache=args.asset_cache, time_budget_seconds=args.time_budget_seconds,
                        max_attempts_per_asset=args.max_attempts_per_asset, asset_seconds=args.asset_seconds,
                        minimum_triangles=args.minimum_triangles, exact_planar=not args.no_exact_planar,
                        require_complete=args.require_complete, seed_divisor=args.seed_divisor)
        print(f"Adjusted {len(result['changed_files'])}/{result['bed_count']} beds; "
              f"raw saving {result['raw_bytes_saved']:,} bytes. Report: {args.output}.json", flush=True)
        if args.archive:
            packaged = package(Path(result["package_root"]), args.archive, args.seven_zip,
                               args.dictionary_mib, args.compression_level)
            print(f"Verified adjusted archive: {packaged['archive_bytes']:,} bytes", flush=True)
    except (ValueError, RuntimeError, OSError, ImportError) as error:
        parser.exit(1, f"Mesh-adjustment stage failed: {error}\n")


if __name__ == "__main__":
    main()
