"""Pure release-orchestration contracts: no builds, applications or downloads."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("release_validation", Path(__file__).with_name("validate-release.py"))
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
REPOSITORY = Path(__file__).resolve().parents[2]


class ReleaseCiTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="release-ci-contract-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)

    def put(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_collect_keeps_smoke_logs_but_excludes_release_payloads(self):
        output = self.root / "out/ci-release"
        self.put("out/ci-release/reports/application-warm-1.json", '{"seconds":3,"exit_code":0}')
        self.put("out/ci-release/reports/application-warm-2.json", '{"seconds":2,"exit_code":0}')
        self.put("out/ci-release/reports/application-warm-3.json", '{"seconds":4,"exit_code":0}')
        self.put("out/build-guard/guard.json", '{"seconds":1,"status":"passed"}')
        self.put("out/build-guard/guard.log", "guard evidence")
        self.put("out/ci-release/validation/smoke/report.json", '{"executions_ok":false}')
        self.put("out/ci-release/validation/smoke/baseline/cube.stdout.log", "baseline diagnostics")
        self.put("out/ci-release/validation/smoke/candidate/step.stderr.log", "candidate diagnostics")
        for name in ("baseline", "candidate", "extraction"):
            self.put(f"out/ci-release/validation/{name}/resources/private.json", "not report JSON")
        with patch.object(release, "ROOT", self.root), patch.dict(release.os.environ, {}, clear=True):
            result = release.collect(output)
        self.assertEqual(result["warm_wrapper_median_seconds"], 3)
        reports = output / "reports"
        self.assertTrue((reports / "build-guard/guard.json").is_file())
        self.assertEqual((reports / "validation/smoke/baseline/cube.stdout.log").read_text(), "baseline diagnostics")
        self.assertEqual((reports / "validation/smoke/candidate/step.stderr.log").read_text(), "candidate diagnostics")
        for name in ("baseline", "candidate", "extraction"):
            self.assertFalse((reports / "validation" / name).exists())
        with patch.object(release, "ROOT", self.root), patch.dict(release.os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                release.collect(output)

    def validate_fake(self, returncode):
        output = self.root / f"validate-{returncode}"
        self.put("build-lean-release/CMakeCache.txt", "SLIC3R_MSVC_DEBUG_SYMBOLS:BOOL=TRUE\n")
        self.put("build-lean-release/src/PrusaSlicer.pdb", "nonempty fixture PDB")

        def package_stub(source, archive, **kwargs):
            archive.write_bytes(b"fake independently verified archive")
            return {"archive_bytes": archive.stat().st_size, "archive_sha256": "fixture-hash",
                    "uncompressed_bytes": 1234}

        with patch.object(release, "ROOT", self.root), \
                patch.object(release, "extract_baseline"), patch.object(release, "stage_release"), \
                patch.object(release.subprocess, "run", return_value=SimpleNamespace(returncode=returncode)) as smoke, \
                patch.object(release, "package", side_effect=package_stub) as packager, \
                patch.object(release, "find_seven_zip", return_value="not-invoked"), \
                patch.object(release, "run_seven_zip"), patch.object(release, "inventory", return_value={}):
            if returncode:
                with self.assertRaisesRegex(RuntimeError, f"code {returncode}"):
                    release.validate(self.root / "baseline.zip", self.root / "fixture.step", output)
            else:
                release.validate(self.root / "baseline.zip", self.root / "fixture.step", output)
            packager.assert_called_once()
            self.assertFalse(smoke.call_args.kwargs["check"])
        report = json.loads((output / "release-result.json").read_text(encoding="utf-8"))
        self.assertEqual(report["smoke_exit_code"], returncode)
        self.assertEqual(report["uncompressed_bytes"], 1234)
        self.assertTrue((output / "PrusaSlicer-current-original-geometry.7z").is_file())

    def test_smoke_failure_is_not_hidden_by_successful_packaging(self):
        self.validate_fake(2)

    def test_successful_smoke_and_packaging_record_separate_evidence(self):
        self.validate_fake(0)

    def test_missing_pdb_rejects_symbol_claim_before_staging(self):
        self.put("build-lean-release/CMakeCache.txt", "SLIC3R_MSVC_DEBUG_SYMBOLS:BOOL=TRUE\n")
        with patch.object(release, "ROOT", self.root), patch.object(release, "stage_release") as stage:
            with self.assertRaisesRegex(RuntimeError, "PDB"):
                release.validate(self.root / "baseline.zip", self.root / "fixture.step", self.root / "no-pdb")
            stage.assert_not_called()

    def test_disabled_symbols_reject_even_when_a_pdb_exists(self):
        self.put("build-lean-release/CMakeCache.txt", "SLIC3R_MSVC_DEBUG_SYMBOLS:BOOL=OFF\n")
        self.put("build-lean-release/src/PrusaSlicer.pdb", "stale fixture PDB")
        with patch.object(release, "ROOT", self.root), patch.object(release, "stage_release") as stage:
            with self.assertRaisesRegex(RuntimeError, "symbol-generation option"):
                release.validate(self.root / "baseline.zip", self.root / "fixture.step", self.root / "symbols-off")
            stage.assert_not_called()

    def test_lean_release_preset_retains_full_feature_contract(self):
        presets = json.loads((REPOSITORY / "CMakePresets.json").read_text(encoding="utf-8"))
        by_name = {item["name"]: item for item in presets["configurePresets"]}

        def resolve(name):
            entry = by_name[name]
            inherited = entry.get("inherits", [])
            if isinstance(inherited, str):
                inherited = [inherited]
            result = {}
            for parent in reversed(inherited):
                result.update(resolve(parent))
            result.update(entry.get("cacheVariables", {}))
            return result

        effective = resolve("lean-release")
        for option in ("SLIC3R_GUI", "SLIC3R_ENABLE_FORMAT_STEP", "SLIC3R_BUILD_TESTS",
                       "SLIC3R_MSVC_DEBUG_SYMBOLS", "SLIC3R_STATIC"):
            self.assertIn(str(effective[option]).upper(), ("TRUE", "ON", "1", "YES"), option)
        self.assertEqual(effective["CMAKE_BUILD_TYPE"], "Release")


if __name__ == "__main__":
    unittest.main()
