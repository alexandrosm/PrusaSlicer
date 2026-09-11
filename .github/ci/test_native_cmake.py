"""Fake installed-wheel metadata only: no installation, downloads or compiles."""
import base64
import csv
import hashlib
from importlib import metadata, util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SPEC = util.spec_from_file_location('select_native_cmake', Path(__file__).with_name('select-native-cmake.py'))
native = util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)


class NativeCMakeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='native-cmake-wheel-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.info = self.root / 'cmake-3.31.6.dist-info'
        self.info.mkdir()
        (self.info / 'METADATA').write_text('Metadata-Version: 2.1\nName: cmake\nVersion: 3.31.6\n', encoding='utf-8')
        self.files = ['cmake/data/bin/cmake.exe', 'cmake/data/bin/ctest.exe', 'Scripts/cmake.exe']
        for relative in self.files:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('fixture native ' + relative).encode())
        self.write_record()
        self.distribution = metadata.PathDistribution(self.info)

    def write_record(self, entries=None, no_hash=(), algorithm='sha256'):
        output = io.StringIO(newline='')
        writer = csv.writer(output, lineterminator='\n')
        for relative in self.files if entries is None else entries:
            data = (self.root / relative).read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')
            writer.writerow((relative, '' if relative in no_hash else algorithm + '=' + digest, str(len(data))))
        (self.info / 'RECORD').write_text(output.getvalue(), encoding='utf-8')

    def select(self):
        return native.select_native(self.distribution, windows=True)

    def test_selects_exact_native_cmake_and_ctest_not_pip_launcher(self):
        result = self.select()
        self.assertEqual(result['distribution_version'], '3.31.6')
        self.assertEqual(result['native_bin'], str(self.root / 'cmake/data/bin'))
        self.assertEqual(set(result['binaries']), {'cmake', 'ctest'})
        for name in ('cmake', 'ctest'):
            self.assertEqual(result['binaries'][name]['path'], str(self.root / f'cmake/data/bin/{name}.exe'))
            self.assertEqual(len(result['binaries'][name]['sha256']), 64)

    def test_launcher_timestamp_and_content_do_not_change_native_identity(self):
        before = self.select()
        launcher = self.root / 'Scripts/cmake.exe'
        launcher.write_bytes(b'regenerated timestamp-dependent pip launcher')
        os.utime(launcher, (1, 1))
        self.write_record()
        after = self.select()
        self.assertEqual(before['identity_sha256'], after['identity_sha256'])
        self.assertEqual(before['binaries'], after['binaries'])

    def test_modified_native_binary_fails_record_even_if_size_unchanged(self):
        for name in ('cmake', 'ctest'):
            path = self.root / f'cmake/data/bin/{name}.exe'
            original = path.read_bytes()
            path.write_bytes(b'X' + original[1:])
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'does not match'):
                self.select()
            path.write_bytes(original)

    def test_missing_selected_hash_fails_but_launcher_hash_is_not_needed(self):
        self.write_record(no_hash=('Scripts/cmake.exe',))
        self.select()
        self.write_record(no_hash=('cmake/data/bin/ctest.exe',))
        with self.assertRaisesRegex(ValueError, 'requires a SHA256'):
            self.select()

    def test_wrong_record_hash_algorithm_rejected(self):
        self.write_record(algorithm='sha512')
        with self.assertRaisesRegex(ValueError, 'requires a SHA256'):
            self.select()

    def test_missing_record_or_native_entry_rejected(self):
        self.write_record(entries=['cmake/data/bin/cmake.exe', 'Scripts/cmake.exe'])
        with self.assertRaisesRegex(ValueError, 'Missing or ambiguous'):
            self.select()
        (self.info / 'RECORD').unlink()
        with self.assertRaisesRegex(ValueError, 'no wheel RECORD'):
            self.select()

    def test_duplicate_native_record_is_ambiguous(self):
        self.write_record(entries=self.files + ['cmake/data/bin/cmake.exe'])
        with self.assertRaisesRegex(ValueError, 'Missing or ambiguous'):
            self.select()

    def test_missing_native_file_rejected(self):
        (self.root / 'cmake/data/bin/ctest.exe').unlink()
        github_path, report = self.root / 'github-path', self.root / 'native.json'
        # Some importlib.metadata versions filter nonexistent RECORD paths;
        # others leave them for lstat to reject. Both must fail before execution
        # or publishing a usable PATH/report, without weakening the selector.
        with patch.object(native.subprocess, 'run') as version, \
                self.assertRaisesRegex((ValueError, FileNotFoundError), 'ctest'):
            native.publish(github_path, report, self.distribution, windows=True)
        version.assert_not_called()
        self.assertFalse(report.exists())
        self.assertFalse(github_path.exists())

    def test_nonregular_native_file_rejected(self):
        path = self.root / 'cmake/data/bin/ctest.exe'
        path.unlink()
        path.mkdir()
        with self.assertRaisesRegex(ValueError, 'regular non-reparse'):
            self.select()

    def test_metadata_lookup_never_imports_workspace_cmake_module(self):
        (self.root / 'cmake.py').write_text('raise AssertionError("Workspace module must not be imported")\n')
        with patch.object(native.metadata, 'distribution', return_value=self.distribution) as locate, \
                patch.object(sys, 'path', [str(self.root)] + sys.path):
            result = native.select_native(windows=True)
        locate.assert_called_once_with('cmake')
        self.assertEqual(result['distribution_version'], '3.31.6')

    def test_publishes_verified_native_path_and_byte_identity(self):
        github_path, report = self.root / 'github-path', self.root / 'reports/native.json'
        github_path.write_text('existing-entry\n', encoding='utf-8')
        response = subprocess.CompletedProcess([], 0, 'cmake version 3.31.6\n', '')
        with patch.object(native.subprocess, 'run', return_value=response) as version:
            result = native.publish(github_path, report, self.distribution, windows=True)
        version.assert_called_once_with([str(self.root / 'cmake/data/bin/cmake.exe'), '--version'],
                                        check=True, capture_output=True, text=True, timeout=10)
        self.assertEqual(result['native_version'], '3.31.6')
        self.assertEqual(json.loads(report.read_text())['identity_sha256'], self.select()['identity_sha256'])
        self.assertEqual(github_path.read_text(), 'existing-entry\n' + str(self.root / 'cmake/data/bin') + '\n')

    def test_existing_report_is_not_overwritten_and_path_unchanged(self):
        github_path, report = self.root / 'github-path', self.root / 'native.json'
        github_path.write_text('unchanged\n')
        report.write_text('previous evidence')
        with patch.object(native.subprocess, 'run') as version, self.assertRaises(FileExistsError):
            native.publish(github_path, report, self.distribution, windows=True)
        version.assert_not_called()
        self.assertEqual(report.read_text(), 'previous evidence')
        self.assertEqual(github_path.read_text(), 'unchanged\n')

    def test_invalid_native_bytes_do_not_execute_or_publish(self):
        github_path, report = self.root / 'github-path', self.root / 'native.json'
        (self.root / 'cmake/data/bin/cmake.exe').write_bytes(b'tampered')
        with patch.object(native.subprocess, 'run') as version, self.assertRaises(ValueError):
            native.publish(github_path, report, self.distribution, windows=True)
        version.assert_not_called()
        self.assertFalse(report.exists())
        self.assertFalse(github_path.exists())

    def test_report_cannot_alias_github_path(self):
        with self.assertRaisesRegex(ValueError, 'separate files'):
            native.publish(self.root / 'same', self.root / 'same', self.distribution, windows=True)


if __name__ == '__main__':
    unittest.main()
