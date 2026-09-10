import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("package_portable", Path(__file__).parents[1] / "package_portable.py")
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "PrusaSlicer release"
        self.source.mkdir()
        (self.source / "PrusaSlicer.dll").write_bytes(b"binary fixture\x00" * 100)
        (self.source / "resources").mkdir()
        (self.source / "resources" / "empty").mkdir()
        (self.source / "resources" / "日本語.txt").write_text("こんにちは", encoding="utf-8")
        self.output = self.root / "output.7z"

    def test_requires_output_outside_stage(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            packager.package(self.source, self.source / "archive.7z", None)

    def test_refuses_overwrite(self):
        self.output.write_bytes(b"keep me")
        with self.assertRaisesRegex(ValueError, "overwrite"):
            packager.package(self.source, self.output, None)
        self.assertEqual(self.output.read_bytes(), b"keep me")

    def test_refuses_overwrite_report(self):
        report = self.output.with_suffix(".7z.json")
        report.write_text("keep me", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "overwrite"):
            packager.package(self.source, self.output, None)
        self.assertFalse(self.output.exists())

    def test_empty_stage_rejected(self):
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(ValueError, "no files"):
            packager.inventory(empty)

    def test_invalid_compression_level_is_rejected_before_running_tools(self):
        with patch.object(packager, "run_seven_zip") as run:
            for level in (0, 1, 8, 10, "9", 9.0, None):
                with self.subTest(level=level):
                    with self.assertRaisesRegex(ValueError, "Compression level"):
                        packager.package(self.source, self.output, None, compression_level=level)
            run.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.with_suffix(".7z.json").exists())

    def test_symlink_rejected(self):
        link = self.source / "alias"
        try:
            link.symlink_to(self.source / "PrusaSlicer.dll")
        except OSError:
            self.skipTest("Creating symlinks requires Windows developer mode or privilege")
        with self.assertRaisesRegex(ValueError, "link or reparse"):
            packager.inventory(self.source)

    def test_round_trip_with_real_seven_zip(self):
        try:
            seven_zip = packager.find_seven_zip()
        except ValueError:
            self.skipTest("7-Zip is unavailable")
        before = packager.inventory(self.source)
        # The pre-existing fourth positional argument remains the dictionary.
        with patch.object(packager, "run_seven_zip", wraps=packager.run_seven_zip) as run:
            result = packager.package(self.source, self.output, seven_zip, 16)
        compression = next(call.args[1] for call in run.call_args_list if call.args[1][0] == "a")
        self.assertIn("-mx=7", compression)
        self.assertIn("-mmt=1", compression)
        self.assertEqual(result["compression"]["level"], 7)
        self.assertEqual(result["compression"]["dictionary_mib"], 16)
        self.assertEqual(result["payload"], before)
        self.assertEqual(result["file_count"], 2)
        self.assertEqual(result["archive_sha256"], packager.sha256(self.output))
        self.assertEqual(packager.inventory(self.source), before)
        self.assertTrue(self.output.with_suffix(".7z.json").is_file())

    def test_level_nine_round_trip_preserves_payload_and_records_selection(self):
        try:
            seven_zip = packager.find_seven_zip()
        except ValueError:
            self.skipTest("7-Zip is unavailable")
        before = packager.inventory(self.source)
        with patch.object(packager, "run_seven_zip", wraps=packager.run_seven_zip) as run:
            result = packager.package(self.source, self.output, seven_zip,
                                      dictionary_mib=16, compression_level=9)
        compression = next(call.args[1] for call in run.call_args_list if call.args[1][0] == "a")
        self.assertIn("-mx=9", compression)
        self.assertIn("-mmt=1", compression)
        self.assertIn("-md=16m", compression)
        self.assertEqual(result["compression"]["level"], 9)
        self.assertEqual(result["compression"]["threads"], 1)
        self.assertEqual(result["payload"], before)
        self.assertEqual(result["archive_sha256"], packager.sha256(self.output))
        self.assertEqual(packager.inventory(self.source), before)

    def test_report_failure_rolls_back_publication(self):
        try:
            seven_zip = packager.find_seven_zip()
        except ValueError:
            self.skipTest("7-Zip is unavailable")
        with patch.object(packager.json, "dump", side_effect=OSError("report write failed")):
            with self.assertRaisesRegex(OSError, "report write failed"):
                packager.package(self.source, self.output, seven_zip, dictionary_mib=16)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.with_suffix(".7z.json").exists())

    def test_source_change_fails_verification(self):
        try:
            seven_zip = packager.find_seven_zip()
        except ValueError:
            self.skipTest("7-Zip is unavailable")
        run = packager.run_seven_zip

        def mutate_after_compression(executable, arguments, cwd=None):
            result = run(executable, arguments, cwd)
            if arguments[0] == "a":
                (self.source / "PrusaSlicer.dll").write_bytes(b"changed during packaging")
            return result

        with patch.object(packager, "run_seven_zip", side_effect=mutate_after_compression):
            with self.assertRaisesRegex(RuntimeError, "Source changed"):
                packager.package(self.source, self.output, seven_zip, dictionary_mib=16)
        self.assertFalse(self.output.exists())

    def test_compressor_failure_does_not_publish(self):
        try:
            seven_zip = packager.find_seven_zip()
        except ValueError:
            self.skipTest("7-Zip is unavailable")
        with patch.object(packager, "run_seven_zip", side_effect=["7-Zip fixture", RuntimeError("compression failed")]):
            with self.assertRaisesRegex(RuntimeError, "compression failed"):
                packager.package(self.source, self.output, seven_zip)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
