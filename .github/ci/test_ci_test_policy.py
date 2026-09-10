"""Tiny tests of the CI coverage gate itself; no external processes."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import run_tool_tests as ci
import capture_command
import ci_summary


class NamedTest(unittest.TestCase):
    def __init__(self, identity):
        super().__init__()
        self.identity = identity

    def id(self):
        return self.identity


class CoveragePolicyTests(unittest.TestCase):
    def result(self):
        return ci.RecordingResult(unittest.runner._WritelnDecorator(io.StringIO()), True, 0)

    def test_all_selected_tests_must_finish(self):
        result = self.result()
        test = NamedTest('test_guard.Guard.test_one')
        result.startTest(test)
        result.addSuccess(test)
        self.assertTrue(ci.evaluate(result, [test.id()])['passed'])
        self.assertFalse(ci.evaluate(result, [test.id(), 'test_missing.case'])['passed'])

    def test_empty_selection_fails(self):
        self.assertFalse(ci.evaluate(self.result(), [])['passed'])

    def test_symlink_privilege_is_only_permitted_skip(self):
        result = self.result()
        test = NamedTest(ci.SYMLINK_TEST)
        result.startTest(test)
        result.addSkip(test, ci.SYMLINK_REASON)
        self.assertTrue(ci.evaluate(result, [test.id()])['passed'])

    def test_native_or_archive_skip_fails(self):
        result = self.result()
        test = NamedTest('test_stl_restore.NativeTests.test_all_modes_and_empty_stl')
        result.startTest(test)
        result.addSkip(test, 'Decoder missing')
        self.assertFalse(ci.evaluate(result, [test.id()])['passed'])

    def test_changed_symlink_skip_reason_fails(self):
        result = self.result()
        test = NamedTest(ci.SYMLINK_TEST)
        result.startTest(test)
        result.addSkip(test, '7-Zip unavailable')
        self.assertFalse(ci.evaluate(result, [test.id()])['passed'])

    def test_expected_failure_is_not_accepted_as_coverage(self):
        result = self.result()
        test = NamedTest('test_required.case')
        result.startTest(test)
        result.expectedFailures.append((test, 'known bug'))
        self.assertFalse(ci.evaluate(result, [test.id()])['passed'])

    def test_geometry_lane_permits_no_skips(self):
        result = self.result()
        test = NamedTest(ci.SYMLINK_TEST)
        result.startTest(test)
        result.addSkip(test, ci.SYMLINK_REASON)
        self.assertFalse(ci.evaluate(result, [test.id()], lane='geometry')['passed'])


class InventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ci-inventory-policy-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for directory in ci.TEST_ROOTS:
            (self.root / directory).mkdir(parents=True)
        (self.root / 'tools/tests/test_one.py').touch()
        (self.root / 'tools/tests/test_geometry.py').touch()
        self.inventory = {'schema': 1, 'lanes': {'tooling': ['tools/tests/test_one.py'],
                                                'geometry': ['tools/tests/test_geometry.py']}}

    def test_complete_assignment_passes(self):
        self.assertEqual(ci.validate_inventory(self.root, self.inventory), self.inventory['lanes'])

    def test_new_unassigned_module_fails(self):
        (self.root / 'tests/build_tools/test_new.py').touch()
        with self.assertRaisesRegex(ValueError, 'unassigned=.*test_new'):
            ci.validate_inventory(self.root, self.inventory)

    def test_nested_unassigned_module_fails(self):
        nested = self.root / '.github/ci/nested'
        nested.mkdir()
        (nested / 'test_new.py').touch()
        with self.assertRaisesRegex(ValueError, 'unassigned=.*nested/test_new'):
            ci.validate_inventory(self.root, self.inventory)

    def test_stale_assignment_fails(self):
        (self.root / 'tools/tests/test_one.py').unlink()
        with self.assertRaisesRegex(ValueError, 'missing=.*test_one'):
            ci.validate_inventory(self.root, self.inventory)

    def test_duplicate_lane_assignment_fails(self):
        self.inventory['lanes']['geometry'].append('tools/tests/test_one.py')
        with self.assertRaisesRegex(ValueError, 'more than once'):
            ci.validate_inventory(self.root, self.inventory)

    def test_wrong_or_empty_lane_fails(self):
        self.inventory['lanes']['tooling'] = []
        with self.assertRaisesRegex(ValueError, 'empty'):
            ci.validate_inventory(self.root, self.inventory)
        self.inventory['lanes']['new-lane'] = ['new.py']
        with self.assertRaisesRegex(ValueError, 'schema 1'):
            ci.validate_inventory(self.root, self.inventory)

    def test_duplicate_import_names_fail(self):
        path = self.root / 'tests/build_tools/test_one.py'
        path.touch()
        self.inventory['lanes']['tooling'].append('tests/build_tools/test_one.py')
        with self.assertRaisesRegex(ValueError, 'duplicate Python test module'):
            ci.validate_inventory(self.root, self.inventory)


class CommandCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ci-command-policy-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.report = self.root / 'command.json'

    def run_fake(self, data, code):
        process = MagicMock()
        process.__enter__.return_value = process
        process.stdout = io.BytesIO(data)
        process.wait.return_value = code
        with patch.object(capture_command.subprocess, 'Popen', return_value=process) as launch, \
             patch.object(capture_command.sys, 'stdout', io.StringIO()):
            status = capture_command.capture(['fake.exe', 'literal argument'], self.report, 'fixture')
        self.assertEqual(launch.call_args.args[0], ['fake.exe', 'literal argument'])
        return status, json.loads(self.report.read_text())

    def test_preserves_all_native_output_bytes_and_timing(self):
        data = b'output\r\n' + 'Unicode \u65e5\u672c\u8a9e'.encode('utf-8') + b'\xff' + b'x' * 150000
        status, report = self.run_fake(data, 0)
        self.assertEqual(status, 0)
        self.assertEqual(self.report.with_suffix('.log').read_bytes(), data)
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['returncode'], 0)
        self.assertGreaterEqual(report['seconds'], 0)

    def test_nonzero_exit_remains_failure(self):
        status, report = self.run_fake(b'actual diagnostic\n', 9)
        self.assertEqual(status, 9)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['returncode'], 9)

    def test_launch_failure_records_exception(self):
        with patch.object(capture_command.subprocess, 'Popen', side_effect=OSError('missing compiler')):
            self.assertEqual(capture_command.capture(['missing.exe'], self.report, 'missing'), 1)
        report = json.loads(self.report.read_text())
        self.assertIn('OSError: missing compiler', report['error'])
        self.assertEqual(report['status'], 'failed')

    def test_existing_log_or_report_never_overwritten(self):
        for path in (self.report, self.report.with_suffix('.log')):
            path.write_bytes(b'keep')
            with patch.object(capture_command.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(ValueError, 'fresh'):
                    capture_command.capture(['fake.exe'], self.report, 'test')
                launch.assert_not_called()
            self.assertEqual(path.read_bytes(), b'keep')
            path.unlink()


class SummaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ci-summary-policy-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for directory in ('commands', 'guards'):
            (self.root / 'reports' / directory).mkdir(parents=True)
        self.command = {'label': 'fixture', 'status': 'passed', 'seconds': 3}
        self.guard = {'status': 'passed', 'seconds': 2, 'sampled_peak_tree_rss_bytes': 1048576}
        self.tests = {'passed': True, 'passes': 7, 'tests_run': 7, 'skips': []}
        for filename, record in (('commands/one.json', self.command), ('guards/one.json', self.guard), ('tests.json', self.tests)):
            (self.root / 'reports' / filename).write_text(json.dumps(record))

    def test_nested_guard_time_not_counted_twice(self):
        report, markdown = ci_summary.summarize(self.root, 'tooling', 'passed')
        self.assertTrue(report['passed'])
        self.assertEqual(report['command_seconds'], 3)
        self.assertIn('7 passed', markdown)
        self.assertIn('not a full PrusaSlicer build benchmark', markdown)
        self.assertIn('1.000 MiB', markdown)

    def test_prior_failure_or_missing_tests_is_not_success(self):
        self.assertFalse(ci_summary.summarize(self.root, 'tooling', 'failed')[0]['passed'])
        (self.root / 'reports/tests.json').unlink()
        self.assertFalse(ci_summary.summarize(self.root, 'tooling', 'passed')[0]['passed'])

    def test_guard_cleanup_failure_remains_failure(self):
        self.guard['status'] = 'cleanup-failed'
        (self.root / 'reports/guards/one.json').write_text(json.dumps(self.guard))
        self.assertFalse(ci_summary.summarize(self.root, 'tooling', 'passed')[0]['passed'])

    def test_missing_guard_report_is_not_success(self):
        (self.root / 'reports/guards/one.json').unlink()
        self.assertFalse(ci_summary.summarize(self.root, 'tooling', 'passed')[0]['passed'])


if __name__ == '__main__':
    unittest.main()
