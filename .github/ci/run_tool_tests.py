"""Run the lightweight Windows CI surface; never silently skip native coverage.

Geometry kernels, full app builds, slicing and GUI parity are NOT tested here.
The bed-stage orchestrator uses its existing fake geometry engine fixtures.
"""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULES = (
    'test_adjust_bed_meshes', 'test_benchmark_pch', 'test_experiment_helpers',
    'test_package_portable', 'test_run_guarded', 'test_sccache_supervisor',
    'test_stage_windows_release', 'test_stl_codec', 'test_stl_restore',
    'test_verify_stl_package', 'test_verify_release_smoke', 'test_ci_test_policy',
)
SYMLINK_TEST = 'test_package_portable.PackageTests.test_symlink_rejected'
SYMLINK_REASON = 'Creating symlinks requires Windows developer mode or privilege'


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed_ids = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed_ids.append(test.id())


def test_ids(suite):
    for entry in suite:
        if isinstance(entry, unittest.TestSuite):
            yield from test_ids(entry)
        else:
            yield entry.id()


def evaluate(result, selected_ids):
    """Fail closed on missing tests, unexpected skips, or expected failures."""
    skips = [{'test': test.id(), 'reason': reason} for test, reason in result.skipped]
    unexpected_skips = [item for item in skips if item != {'test': SYMLINK_TEST, 'reason': SYMLINK_REASON}]
    selected = Counter(selected_ids)
    finished = Counter(result.passed_ids)
    finished.update(item['test'] for item in skips)
    okay = (bool(selected) and result.wasSuccessful() and not unexpected_skips
            and not result.expectedFailures and selected == finished
            and result.testsRun == len(selected_ids))
    return {
        'passed': okay, 'tests_run': result.testsRun, 'passes': len(result.passed_ids),
        'skips': skips, 'unexpected_skips': unexpected_skips,
        'failures': [{'test': test.id(), 'traceback': detail} for test, detail in result.failures],
        'errors': [{'test': test.id(), 'traceback': detail} for test, detail in result.errors],
        'expected_failures': [test.id() for test, _ in result.expectedFailures],
        'unexpected_successes': [test.id() for test in result.unexpectedSuccesses],
        'selected_tests': selected_ids, 'passed_tests': result.passed_ids,
        'module_counts': dict(sorted(Counter(item.split('.')[0] for item in selected_ids).items())),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('This CI surface requires native Windows; do not skip native tests')
    decoder = Path(os.environ.get('PRUSA_STL_RESTORE_TEST_EXE', ''))
    if not decoder.is_file():
        parser.error('PRUSA_STL_RESTORE_TEST_EXE must identify the freshly built native decoder')
    if args.report.exists():
        parser.error('Refusing to overwrite an existing test report')
    for folder in (ROOT / 'tools/tests', ROOT / 'tests/build_tools', Path(__file__).parent, ROOT / 'tools'):
        sys.path.insert(0, str(folder))
    import package_portable
    package_portable.find_seven_zip()  # Required: real archive tests must run, not skip.
    suite = unittest.TestSuite()
    for module in MODULES:
        tests = unittest.defaultTestLoader.loadTestsFromName(module)
        if tests.countTestCases() == 0:
            raise RuntimeError(f'CI module contains no tests: {module}')
        suite.addTests(tests)
    selected_ids = list(test_ids(suite))
    start = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    report = evaluate(result, selected_ids)
    report.update({'seconds': round(time.monotonic() - start, 3),
                   'scope': 'Windows tooling and tiny native/7-Zip fixtures; not full app or geometry validation'})
    with args.report.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
    print(f"CI policy: {report['passes']} passed, {len(report['skips'])} permitted skips; passed={report['passed']}")
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
