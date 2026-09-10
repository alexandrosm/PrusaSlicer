"""No compiler launch: benchmark orchestration and limits are mocked."""
import importlib.util
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
