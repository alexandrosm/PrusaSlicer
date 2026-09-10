"""No compiler launch: benchmark orchestration and limits are mocked."""
import importlib.util
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
try:
    spec = importlib.util.spec_from_file_location('benchmark_pch_under_test', TOOLS / 'benchmark_pch.py')
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
finally:
    sys.path.pop(0)


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / 'new-run'
        self.calls = []
        self.code = 0
        self.launch = patch.object(benchmark, 'run_guarded', side_effect=self.mock_run)
        self.launch.start()
        self.addCleanup(self.launch.stop)
        discovery = patch.object(benchmark.shutil, 'which', side_effect=lambda name: 'C:/mock/' + name + '.exe')
        discovery.start()
        self.addCleanup(discovery.stop)

    def mock_run(self, command, directory, label, **options):
        self.calls.append((command, label, options))
        directory.mkdir(exist_ok=True)
        (directory / (label + '.json')).write_text(json.dumps(
            {'status': 'passed' if not self.code else 'guard-stopped', 'seconds': 0.1,
             'sampled_peak_tree_rss_bytes': 1234}), encoding='utf-8')
        if not self.code and not label.endswith('configure') and '-warm' not in label:
            build = Path(command[command.index('--build') + 1])
            target = build / 'CMakeFiles/pch_probe.dir'
            target.mkdir(parents=True, exist_ok=True)
            outputs = ['a.cpp.obj', 'b.cpp.obj']
            if label.endswith('clean') or label.startswith('full-'):
                outputs.insert(0, 'cmake_pch.cxx.obj')
                (target / 'cmake_pch.cxx.pch').write_bytes(label.encode())
            journal = build / '.ninja_log'
            with journal.open('a', encoding='utf-8') as stream:
                for name in outputs:
                    (target / name).write_bytes((label + name).encode())
                    stream.write(f'0\t1\t2\tCMakeFiles/pch_probe.dir/{name}\t0000\n')
        return self.code

    def test_all_scenarios_guarded_and_source_unchanged(self):
        original = (benchmark.FIXTURE / 'project.hpp').read_bytes()
        with patch.dict(os.environ, {'CMAKE_BUILD_PARALLEL_LEVEL': '99'}):
            report = benchmark.benchmark(self.output)
            self.assertEqual(os.environ['CMAKE_BUILD_PARALLEL_LEVEL'], '99')
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(len(self.calls), 10)
        for command, label, limits in self.calls:
            self.assertEqual(limits['memory_gib'], 2)
            self.assertEqual(limits['cpu_count'], 1)
            self.assertEqual(limits['minimum_free_gib'], 6)
            self.assertGreater(limits['timeout_seconds'], 0)
            self.assertLessEqual(limits['timeout_seconds'], 120)
            if 'configure' in label:
                self.assertIn('-DSLIC3R_COMPILER_CACHE=off', command)
            else:
                self.assertEqual(command[command.index('--target') + 1], 'pch_probe')
        self.assertEqual((benchmark.FIXTURE / 'project.hpp').read_bytes(), original)
        self.assertIn('= 2', (self.output / 'stable/source/project.hpp').read_text())
        proofs = [step['mechanism'] for step in report['steps'] if 'mechanism' in step]
        self.assertEqual(len(proofs), 8)
        self.assertTrue(all(proof['passed'] for proof in proofs))
        edit = next(step for step in report['steps'] if step['mode'] == 'stable' and step['scenario'] == 'header-edit')
        self.assertEqual(len(edit['mechanism']['executed_edges']), 2)
        self.assertNotEqual(edit['header_before']['sha256'], edit['header_after']['sha256'])

    def test_failure_preserves_report_and_restores_environment(self):
        self.code = 2
        with patch.dict(os.environ, {'CMAKE_BUILD_PARALLEL_LEVEL': '7'}):
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                benchmark.benchmark(self.output)
            self.assertEqual(os.environ['CMAKE_BUILD_PARALLEL_LEVEL'], '7')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(json.loads((self.output / 'benchmark.json').read_text())['status'], 'failed')

    def test_existing_output_not_overwritten(self):
        self.output.mkdir()
        with self.assertRaises(FileExistsError):
            benchmark.benchmark(self.output)
        self.assertFalse(self.calls)

    def test_budget_validation_precedes_any_output(self):
        for budget in (0, -1, float('nan'), 301):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                benchmark.benchmark(self.output, max_seconds=budget)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.calls)

    def test_total_budget_exhaustion_does_not_launch(self):
        with patch.object(benchmark.time, 'monotonic', side_effect=[0, 121, 122]):
            with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
                benchmark.benchmark(self.output)
        self.assertFalse(self.calls)

    def test_explicit_ci_reserves_do_not_change_defaults(self):
        report = benchmark.benchmark(self.output, minimum_free_gib=2, minimum_commit_gib=2)
        self.assertEqual(report['minimum_free_gib'], 2)
        self.assertTrue(all(call[2]['minimum_free_gib'] == 2 for call in self.calls))

    def test_invalid_reserve_rejected_before_output(self):
        for reserve in (0, -1, float('nan'), 65):
            with self.subTest(reserve=reserve), self.assertRaises(ValueError):
                benchmark.benchmark(self.output, minimum_free_gib=reserve)
        self.assertFalse(self.output.exists())


class MechanismProofTests(unittest.TestCase):
    def setUp(self):
        self.before = {'journal': ['# ninja log v6'], 'artifacts': {
            'cmake_pch.cxx.pch': {'sha256': 'pch1', 'bytes': 5, 'mtime_ns': 1},
            'a.cpp.obj': {'sha256': 'a1', 'bytes': 5, 'mtime_ns': 1},
            'b.cpp.obj': {'sha256': 'b1', 'bytes': 5, 'mtime_ns': 1}}}

    def edited(self, pch=False):
        after = copy.deepcopy(self.before)
        names = ['a.cpp.obj', 'b.cpp.obj']
        if pch:
            names.append('cmake_pch.cxx.obj')
            after['artifacts']['cmake_pch.cxx.pch']['sha256'] = 'pch2'
        for name in names:
            after['journal'].append(f'0\t1\t2\tCMakeFiles/pch_probe.dir/{name}\tx')
            if name in after['artifacts']:
                after['artifacts'][name]['sha256'] += '-edited'
        return after

    def test_clean_requires_real_pch_and_both_objects(self):
        for missing in ('cmake_pch.cxx.pch', 'a.cpp.obj'):
            after = self.edited(pch=True)
            del after['artifacts'][missing]
            with self.subTest(missing=missing), self.assertRaises(RuntimeError):
                benchmark.prove_step('full', 'clean', {'journal': [], 'artifacts': {}}, after)

    def test_full_edit_must_rebuild_pch(self):
        with self.assertRaisesRegex(RuntimeError, 'incorrect PCH'):
            benchmark.prove_step('full', 'header-edit', self.before, self.edited())

    def test_stable_edit_must_not_rebuild_pch(self):
        with self.assertRaisesRegex(RuntimeError, 'incorrect PCH'):
            benchmark.prove_step('stable', 'header-edit', self.before, self.edited(pch=True))

    def test_stable_edit_must_preserve_pch_bytes_and_timestamp(self):
        after = self.edited()
        after['artifacts']['cmake_pch.cxx.pch']['mtime_ns'] = 2
        with self.assertRaisesRegex(RuntimeError, 'Stable PCH changed'):
            benchmark.prove_step('stable', 'header-edit', self.before, after)

    def test_edit_must_change_both_objects(self):
        after = self.edited()
        after['artifacts']['b.cpp.obj'] = self.before['artifacts']['b.cpp.obj']
        with self.assertRaisesRegex(RuntimeError, 'constant did not change'):
            benchmark.prove_step('stable', 'header-edit', self.before, after)

    def test_warm_requires_no_edges_and_identical_artifacts(self):
        for after in (self.edited(), copy.deepcopy(self.before)):
            after['artifacts']['a.cpp.obj']['mtime_ns'] = 2
            with self.assertRaisesRegex(RuntimeError, 'Warm no-op'):
                benchmark.prove_step('full', 'warm', self.before, after)

    def test_replaced_journal_rejected(self):
        after = copy.deepcopy(self.before)
        after['journal'] = ['# replaced']
        with self.assertRaisesRegex(RuntimeError, 'journal was replaced'):
            benchmark.prove_step('stable', 'warm', self.before, after)

    def test_duplicate_or_unexpected_edges_rejected(self):
        for edge in ('a.cpp.obj', 'unrelated.cpp.obj'):
            after = self.edited()
            after['journal'].append(f'0\t1\t2\tCMakeFiles/pch_probe.dir/{edge}\tx')
            with self.subTest(edge=edge), self.assertRaises(RuntimeError):
                benchmark.prove_step('stable', 'header-edit', self.before, after)
