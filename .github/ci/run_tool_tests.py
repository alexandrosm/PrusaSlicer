"""Run the lightweight Windows CI surface; never silently skip native coverage.

Geometry kernels, full app builds, slicing and GUI parity are NOT tested here.
The bed-stage orchestrator uses its existing fake geometry engine fixtures.
"""

import argparse
from collections import Counter
import importlib.metadata
import importlib
import json
import os
from pathlib import Path
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
TEST_ROOTS = ('tools/tests', 'tests/build_tools', '.github/ci')
SYMLINK_TEST = 'test_package_portable.PackageTests.test_symlink_rejected'
SYMLINK_REASON = 'Creating symlinks requires Windows developer mode or privilege'


def validate_inventory(root=ROOT, inventory=None):
    """Every Python test module in these roots must belong to exactly one lane."""
    if inventory is None:
        inventory = json.loads((root / '.github/ci/test-inventory.json').read_text(encoding='utf-8'))
    if inventory.get('schema') != 1 or set(inventory.get('lanes', {})) != {'tooling', 'geometry'}:
        raise ValueError('CI inventory must assign tooling and geometry lanes with schema 1')
    assigned = []
    for lane, paths in inventory['lanes'].items():
        if not isinstance(paths, list) or not paths or not all(isinstance(item, str) for item in paths):
            raise ValueError(f'Invalid or empty test inventory lane: {lane}')
        assigned.extend(paths)
    if len(set(assigned)) != len(assigned):
        raise ValueError('A test module is assigned more than once')
    discovered = set()
    for directory in TEST_ROOTS:
        folder = root / directory
        if not folder.is_dir():
            raise ValueError(f'Missing test discovery directory: {directory}')
        discovered.update(path.relative_to(root).as_posix() for path in folder.rglob('test_*.py'))
    unknown, stale = discovered - set(assigned), set(assigned) - discovered
    if unknown or stale:
        raise ValueError(f'CI test inventory mismatch: unassigned={sorted(unknown)}; missing={sorted(stale)}')
    stems = [Path(path).stem for path in assigned]
    if len(set(stems)) != len(stems):
        raise ValueError('Ambiguous duplicate Python test module names')
    return inventory['lanes']


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


def evaluate(result, selected_ids, lane='tooling'):
    """Fail closed on missing tests, unexpected skips, or expected failures."""
    skips = [{'test': test.id(), 'reason': reason} for test, reason in result.skipped]
    unexpected_skips = [item for item in skips if lane != 'tooling' or
                        item != {'test': SYMLINK_TEST, 'reason': SYMLINK_REASON}]
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
    parser.add_argument('--report', type=Path)
    parser.add_argument('--lane', choices=('tooling', 'geometry'), default='tooling')
    parser.add_argument('--check-inventory', action='store_true', help='Classify all test files without importing/running tests')
    args = parser.parse_args()
    lanes = validate_inventory()
    if args.check_inventory:
        print(json.dumps({lane: len(paths) for lane, paths in lanes.items()}, sort_keys=True))
        return 0
    if args.report is None:
        parser.error('--report is required when running tests')
    if os.name != 'nt':
        parser.error('This CI surface requires native Windows; do not skip native tests')
    if args.lane == 'tooling':
        decoder = Path(os.environ.get('PRUSA_STL_RESTORE_TEST_EXE', ''))
        if not decoder.is_file():
            parser.error('PRUSA_STL_RESTORE_TEST_EXE must identify the freshly built native decoder')
    if args.report.exists():
        parser.error('Refusing to overwrite an existing test report')
    for folder in (ROOT / 'tools/tests', ROOT / 'tests/build_tools', Path(__file__).parent, ROOT / 'tools'):
        sys.path.insert(0, str(folder))
    versions = {'Python': sys.version, 'psutil': importlib.metadata.version('psutil')}
    if args.lane == 'tooling':
        import package_portable
        package_portable.find_seven_zip()  # Required: real archive tests must run, not skip.
    else:
        for package, module in (('numpy', 'numpy'), ('scipy', 'scipy'), ('trimesh', 'trimesh'),
                                ('rtree', 'rtree'), ('pymeshlab', 'pymeshlab'),
                                ('fast-simplification', 'fast_simplification')):
            importlib.import_module(module)  # Installed metadata alone is insufficient.
            versions[package] = importlib.metadata.version(package)
    suite = unittest.TestSuite()
    for relative in lanes[args.lane]:
        sys.path.insert(0, str((ROOT / relative).parent))
        module = Path(relative).stem
        tests = unittest.defaultTestLoader.loadTestsFromName(module)
        if tests.countTestCases() == 0:
            raise RuntimeError(f'CI module contains no tests: {module}')
        suite.addTests(tests)
    selected_ids = list(test_ids(suite))
    start = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    report = evaluate(result, selected_ids, args.lane)
    report.update({'seconds': round(time.monotonic() - start, 3),
                   'lane': args.lane, 'inventory': lanes, 'versions': versions,
                   'source_commit': os.environ.get('GITHUB_SHA'),
                   'scope': ('Real pinned geometry-kernel fixtures; not full bed corpus or GUI parity'
                             if args.lane == 'geometry' else
                             'Windows tooling and tiny native/7-Zip fixtures; not full app or geometry validation')})
    with args.report.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
    print(f"CI policy: {report['passes']} passed, {len(report['skips'])} permitted skips; passed={report['passed']}")
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
