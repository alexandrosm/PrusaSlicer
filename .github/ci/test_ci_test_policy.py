"""Tiny tests of the CI coverage gate itself; no external processes."""

import io
import unittest

import run_tool_tests as ci


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


if __name__ == '__main__':
    unittest.main()
