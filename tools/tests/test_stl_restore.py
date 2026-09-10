import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stl_codec as codec
import stage_stl_codec
from test_stl_codec import fixture


@unittest.skipUnless(os.name == "nt" and os.environ.get("PRUSA_STL_RESTORE_TEST_EXE"),
                     "Supply PRUSA_STL_RESTORE_TEST_EXE for native decoder tests")
class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="prusa-stl-native-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.decoder = Path(os.environ["PRUSA_STL_RESTORE_TEST_EXE"]).resolve()

    def run_decoder(self, *arguments):
        return subprocess.run([str(self.decoder), *map(str, arguments)], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)

    def test_all_modes_and_empty_stl(self):
        for mode in codec.MODES:
            for count in (0, 1, 200):
                source = self.root / f"{mode}-{count}.pstlc"
                output = self.root / f"{mode}-{count}.stl"
                original = fixture(count)
                source.write_bytes(codec.encode(original, mode))
                result = self.run_decoder("--decode", source, output)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(output.read_bytes(), original)
                self.assertTrue(source.exists())

    def test_corruption_does_not_publish(self):
        source, output = self.root / "bad.pstlc", self.root / "bad.stl"
        data = bytearray(codec.encode(fixture()))
        data[-1] ^= 1
        source.write_bytes(data)
        self.assertNotEqual(self.run_decoder("--decode", source, output).returncode, 0)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.iterdir()), [source])

    def test_refuse_existing_different_output(self):
        source, output = self.root / "bed.pstlc", self.root / "bed.stl"
        source.write_bytes(codec.encode(fixture()))
        output.write_bytes(b"user edit")
        self.assertNotEqual(self.run_decoder("--decode", source, output).returncode, 0)
        self.assertEqual(output.read_bytes(), b"user edit")

    def test_offline_restore_and_idempotent_rerun(self):
        folder = self.root / "resources" / "日本語 space"
        folder.mkdir(parents=True)
        source, output = folder / "bed.stl.pstlc", folder / "bed.stl"
        original = fixture()
        source.write_bytes(codec.encode(original))
        result = self.run_decoder("--root", self.root, "--remove-encoded")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(output.read_bytes(), original)
        self.assertFalse(source.exists())
        self.assertEqual(self.run_decoder("--root", self.root, "--remove-encoded").returncode, 0)

    def test_malformed_length_and_mode_rejected(self):
        good = codec.encode(fixture())
        for i, data in enumerate((good[:10], good + b"x", good[:8] + b"\xff" + good[9:])):
            source, output = self.root / f"{i}.pstlc", self.root / f"{i}.stl"
            source.write_bytes(data)
            self.assertNotEqual(self.run_decoder("--decode", source, output).returncode, 0)
            self.assertFalse(output.exists())

    def test_stage_immutable_round_trip(self):
        source = self.root / "original"
        source.mkdir()
        (source / "resources").mkdir()
        (source / "resources" / "empty").mkdir()
        (source / "resources" / "bed.stl").write_bytes(fixture(500))
        (source / "other.txt").write_bytes(b"retained original")
        before = stage_stl_codec.inventory(source)
        result = stage_stl_codec.stage(source, self.root / "candidate", self.decoder,
                                      minimum_proxy_saving=0)
        self.assertEqual(stage_stl_codec.inventory(source), before)
        self.assertEqual(result["source_payload"], before)
        self.assertEqual(result["transformed_files"], 1)

    def test_stage_rejects_overwrite_overlap_and_reserved_name(self):
        source = self.root / "original"
        source.mkdir()
        (source / "file").write_bytes(b"x")
        with self.assertRaisesRegex(ValueError, "overlap"):
            stage_stl_codec.stage(source, source / "nested", self.decoder)
        (source / "PREPARE.cmd").write_bytes(b"keep")
        with self.assertRaisesRegex(ValueError, "collide"):
            stage_stl_codec.stage(source, self.root / "candidate", self.decoder)

    def test_stage_rejects_decoder_change_during_run(self):
        source = self.root / "original"
        source.mkdir()
        (source / "bed.stl").write_bytes(fixture(50))
        decoder_copy = self.root / "decoder.exe"
        shutil.copyfile(self.decoder, decoder_copy)
        restore = stage_stl_codec.native_restore
        def changed_after_restore(executable, root):
            result = restore(executable, root)
            with decoder_copy.open("ab") as stream:
                stream.write(b"changed")
            return result
        with patch.object(stage_stl_codec, "native_restore", side_effect=changed_after_restore):
            with self.assertRaisesRegex(RuntimeError, "decoder changed"):
                stage_stl_codec.stage(source, self.root / "candidate", decoder_copy)
        self.assertFalse((self.root / "candidate").exists())
        self.assertFalse((self.root / "candidate.json").exists())


if __name__ == "__main__":
    unittest.main()
