from pathlib import Path
import struct
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).parents[1]))
import stage_windows_release as staging


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_rejects_unsafe_archive_paths_and_collisions(self):
        invalid = (
            "../escape", "/absolute", "other-root/file", f"{staging.OFFICIAL_ROOT}/../escape",
            f"{staging.OFFICIAL_ROOT}/file:stream", f"{staging.OFFICIAL_ROOT}/file.",
            f"{staging.OFFICIAL_ROOT}\\file",
        )
        for name in invalid:
            with self.subTest(name=name), self.assertRaises(ValueError):
                staging.validate_members([zipfile.ZipInfo(name)])
        for names in (("A", "a"), ("file", "file/child")):
            with self.subTest(names=names), self.assertRaises(ValueError):
                staging.validate_members([zipfile.ZipInfo(f"{staging.OFFICIAL_ROOT}/{name}") for name in names])
        link = zipfile.ZipInfo(f"{staging.OFFICIAL_ROOT}/link")
        link.external_attr = 0o120777 << 16
        with self.assertRaisesRegex(ValueError, "link"):
            staging.validate_members([link])

    def test_accepts_normal_root_directories_and_unicode(self):
        names = (f"{staging.OFFICIAL_ROOT}/", f"{staging.OFFICIAL_ROOT}/resources/",
                 f"{staging.OFFICIAL_ROOT}/resources/日本語.txt")
        staging.validate_members([zipfile.ZipInfo(name) for name in names])

    def test_refuses_wrong_baseline_without_creating_output(self):
        baseline = self.root / "wrong.zip"
        baseline.write_bytes(b"not the official archive")
        output = self.root / "output"
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            staging.extract_baseline(baseline, output)
        self.assertFalse(output.exists())

    def test_output_and_report_are_never_overwritten(self):
        output = self.root / "existing"
        output.mkdir()
        sentinel = output / "sentinel"
        sentinel.write_text("keep")
        with self.assertRaisesRegex(ValueError, "overwrite"):
            staging.reserve_output(output)
        self.assertEqual(sentinel.read_text(), "keep")
        report = self.root / "new.json"
        report.write_text("keep report")
        with self.assertRaisesRegex(ValueError, "overwrite"):
            staging.reserve_output(self.root / "new")
        self.assertEqual(report.read_text(), "keep report")

    def test_resource_plan_keeps_fonts_profiles_and_new_runtime_assets(self):
        source = {name: {} for name in ("fonts/CJK.ttc", "profiles/vendor.ini", "icons/new.svg",
                  "localization/PrusaSlicer.pot", "localization/en/PrusaSlicer.po", "icons/PrusaSlicer.ico")}
        baseline = {"resources/" + name: {} for name in ("fonts/CJK.ttc", "profiles/vendor.ini", "localization/PrusaSlicer.pot")}
        baseline[staging.REMOVED_RESOURCE] = {}
        replace, add, exclude = staging.resource_plan(source, baseline)
        self.assertIn("resources/fonts/CJK.ttc", replace)
        self.assertIn("resources/profiles/vendor.ini", replace)
        self.assertIn("resources/localization/PrusaSlicer.pot", replace)
        self.assertEqual(add, ["resources/icons/new.svg"])
        self.assertEqual(len(exclude), 2)
        baseline["resources/unknown-runtime.bin"] = {}
        with self.assertRaisesRegex(ValueError, "explicit review"):
            staging.resource_plan(source, baseline)

    def fixture(self):
        source, build = self.root / "source", self.root / "build"
        (source / "resources/fonts").mkdir(parents=True)
        (source / "resources/localization").mkdir()
        (source / "resources/fonts/CJK.ttc").write_bytes(b"complete font")
        (source / "resources/localization/app.mo").write_bytes(b"compiled translation")
        (source / "resources/localization/app.po").write_bytes(b"translation source")
        (source / "resources/new-runtime.txt").write_text("new asset")
        (source / "LICENSE").write_text("license notice")
        (build / "src").mkdir(parents=True)
        pe = bytearray(70)
        pe[:2] = b"MZ"
        struct.pack_into("<I", pe, 0x3c, 64)
        pe[64:68] = b"PE\0\0"
        struct.pack_into("<H", pe, 68, 0x8664)
        for name in staging.APP_BINARIES + staging.DEPENDENCY_BINARIES:
            (build / "src" / name).write_bytes(pe + name.encode())
        (build / "CMakeCache.txt").write_text(
            f"CMAKE_HOME_DIRECTORY:INTERNAL={source}\nCMAKE_BUILD_TYPE:STRING=Release\n"
            "SLIC3R_GUI:BOOL=ON\nSLIC3R_ENABLE_FORMAT_STEP:BOOL=ON\nSLIC3R_STATIC:BOOL=ON\n")
        baseline = self.root / "fixture.zip"
        with zipfile.ZipFile(baseline, "w") as archive:
            for name in staging.APP_BINARIES + staging.DEPENDENCY_BINARIES:
                archive.writestr(f"{staging.OFFICIAL_ROOT}/{name}", b"old binary")
            for name, data in {"mesa/opengl32.dll": b"software renderer", "vcruntime140.dll": b"runtime",
                               "resources/fonts/CJK.ttc": b"old font", "resources/localization/app.mo": b"old catalog",
                               staging.REMOVED_RESOURCE: b"duplicate"}.items():
                archive.writestr(f"{staging.OFFICIAL_ROOT}/{name}", data)
        return source, build, baseline

    def test_rejects_headless_or_step_disabled_build(self):
        source, build, _ = self.fixture()
        cache = build / "CMakeCache.txt"
        valid = cache.read_text()
        for option in ("SLIC3R_GUI", "SLIC3R_ENABLE_FORMAT_STEP", "SLIC3R_STATIC"):
            cache.write_text(valid.replace(f"{option}:BOOL=ON", f"{option}:BOOL=OFF"))
            with self.subTest(option=option), self.assertRaisesRegex(ValueError, option):
                staging.read_cache(build, source)

    def test_rejects_output_within_source_or_binary_inputs(self):
        source, build = self.root / "source", self.root / "build"
        for output in (source / "resources/candidate", build / "src/candidate"):
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, "outside staging inputs"):
                staging.check_output_location(output, source, build)
            self.assertFalse(output.exists())
        staging.check_output_location(self.root / "out/staged", source, build)

    def test_records_untracked_production_header(self):
        source = self.root / "source"
        header = source / "src/new.hpp"
        header.parent.mkdir(parents=True)
        header.write_text("// new implementation")
        with patch.object(staging.subprocess, "check_output", return_value=b"src/new.hpp\0"):
            result = staging.untracked_production_inputs(source)
        self.assertEqual(result["src/new.hpp"]["sha256"], staging.sha256(header))

    def test_build_provenance_allows_missing_optional_ninja_and_include(self):
        self.assertEqual(staging.build_file_provenance(self.root, self.root, {}), {})

    def test_build_provenance_hashes_ninja_and_relative_project_include(self):
        source, build = self.root / "source", self.root / "build"
        source.mkdir()
        build.mkdir()
        ninja = build / "build.ninja"
        overlay = source / "fastlink-overlay.cmake"
        ninja.write_text("LINK_FLAGS = /DEBUG:FASTLINK\n")
        overlay.write_text("# build-local link override\n")
        variable = "CMAKE_PROJECT_PrusaSlicer_INCLUDE"
        result = staging.build_file_provenance(build, source, {variable: overlay.name})
        self.assertEqual(result["build.ninja"]["sha256"], staging.sha256(ninja))
        self.assertEqual(result[variable]["files"][0]["path"], str(overlay))
        self.assertEqual(result[variable]["files"][0]["sha256"], staging.sha256(overlay))

    def test_build_provenance_rejects_missing_or_nonregular_include(self):
        variable = "CMAKE_PROJECT_PrusaSlicer_INCLUDE"
        for path, expected in ((self.root / "missing.cmake", "Missing"), (self.root, "regular file")):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, expected):
                staging.build_file_provenance(self.root, self.root, {variable: str(path)})

    def test_build_provenance_rejects_reparse_files_and_parent_directories(self):
        target = self.root / "overlay.cmake"
        target.write_text("# input")
        original_lstat = Path.lstat
        for reparse in (target, self.root):
            def lstat(path, *args, **kwargs):
                if path == reparse:
                    return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
                return original_lstat(path, *args, **kwargs)
            with self.subTest(reparse=reparse), patch.object(Path, "lstat", lstat), \
                    self.assertRaisesRegex(ValueError, "link/reparse"):
                staging.fingerprint_build_file(target)

    def test_complete_stage_replaces_every_binary_and_retains_offline_extras(self):
        source, build, baseline = self.fixture()
        overlay = source / "fastlink-overlay.cmake"
        overlay.write_text("# build-local link override\n")
        (build / "build.ninja").write_text("LINK_FLAGS = /DEBUG:FASTLINK\n")
        with (build / "CMakeCache.txt").open("a") as cache:
            cache.write(f"CMAKE_PROJECT_PrusaSlicer_INCLUDE:FILEPATH={overlay}\n")
        output = self.root / "candidate"
        # Synthetic corpus exercises staging; production CLI never bypasses the pinned digest.
        with patch.object(staging, "open_baseline", side_effect=lambda path: zipfile.ZipFile(path)), \
                patch.object(staging.subprocess, "check_output", side_effect=["commit-id\n", b"diff", b""]):
            report = staging.stage_release(baseline, output, source, build)
        payload = output / staging.OFFICIAL_ROOT
        for name in staging.APP_BINARIES + staging.DEPENDENCY_BINARIES:
            self.assertEqual((payload / name).read_bytes(), (build / "src" / name).read_bytes())
        self.assertEqual((payload / "mesa/opengl32.dll").read_bytes(), b"software renderer")
        self.assertEqual((payload / "vcruntime140.dll").read_bytes(), b"runtime")
        self.assertEqual((payload / "resources/fonts/CJK.ttc").read_bytes(), b"complete font")
        self.assertEqual((payload / "resources/localization/app.mo").read_bytes(), b"compiled translation")
        self.assertTrue((payload / "resources/new-runtime.txt").is_file())
        self.assertTrue((payload / "LICENSE").is_file())
        self.assertFalse((payload / staging.REMOVED_RESOURCE).exists())
        self.assertFalse((payload / "resources/localization/app.po").exists())
        self.assertIn("mesa/opengl32.dll", report["inherited_baseline_files"])
        self.assertEqual(report["build_files"]["build.ninja"]["sha256"], staging.sha256(build / "build.ninja"))
        self.assertEqual(report["build_files"]["CMAKE_PROJECT_PrusaSlicer_INCLUDE"]["files"][0]["sha256"], staging.sha256(overlay))
        self.assertTrue(output.with_name("candidate.json").is_file())


if __name__ == "__main__":
    unittest.main()
