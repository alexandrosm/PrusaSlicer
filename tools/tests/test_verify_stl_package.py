import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import package_portable
import stage_stl_codec
import verify_stl_package
from test_stl_codec import fixture


@unittest.skipUnless(os.name == "nt" and os.environ.get("PRUSA_STL_RESTORE_TEST_EXE"),
                     "Supply PRUSA_STL_RESTORE_TEST_EXE for native offline packaging test")
class OfflinePackageTests(unittest.TestCase):
    def test_actual_archive_prepare_and_refuse_overwrite(self):
        try:
            seven_zip = package_portable.find_seven_zip()
        except ValueError:
            self.skipTest("7-Zip unavailable")
        with tempfile.TemporaryDirectory(prefix="prusa-offline-package-") as temporary:
            root = Path(temporary)
            source = root / "original"
            source.mkdir()
            (source / "resources").mkdir()
            (source / "resources" / "empty").mkdir()
            (source / "resources" / "bed.stl").write_bytes(fixture(500))
            (source / "other.txt").write_bytes(b"kept")
            stage = stage_stl_codec.stage(source, root / "stage", os.environ["PRUSA_STL_RESTORE_TEST_EXE"],
                                          minimum_proxy_saving=0)
            archive = root / "candidate.7z"
            package_portable.package(Path(stage["package_root"]), archive, seven_zip, 16)
            result = verify_stl_package.verify(archive, root / "stage.json", root / "restored", seven_zip)
            self.assertEqual(result["source_file_count"], 2)
            self.assertEqual((Path(result["prepared_root"]) / "resources" / "bed.stl").read_bytes(), fixture(500))
            with self.assertRaisesRegex(ValueError, "overwrite"):
                verify_stl_package.verify(archive, root / "stage.json", root / "restored", seven_zip)
            with archive.open("ab") as stream:
                stream.write(b"corrupt")
            with self.assertRaisesRegex(ValueError, "identity"):
                verify_stl_package.verify(archive, root / "stage.json", root / "bad", seven_zip)
            self.assertFalse((root / "bad").exists())


if __name__ == "__main__":
    unittest.main()
