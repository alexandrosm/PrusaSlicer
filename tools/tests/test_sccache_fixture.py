"""Compiler-free negative tests for the hosted native cache proof assertions."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('native_sccache_fixture', ROOT / 'tests/build_tools/run_sccache_fixture.py')
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


def stats(**changes):
    values = dict.fromkeys(('compile_requests', 'compilations', 'cache_writes', 'compile_fails',
                            'requests_not_cacheable', 'requests_unsupported_compiler',
                            'non_cacheable_compilations', 'cache_write_errors', 'cache_read_errors'), 0)
    for name in ('cache_hits', 'cache_misses', 'cache_errors'):
        values[name] = {'counts': {}, 'adv_counts': {}}
    for name, value in changes.items():
        if isinstance(values[name], dict):
            values[name] = {'counts': {'C/C++': value}, 'adv_counts': {'c++ [msvc]': value}}
        else:
            values[name] = value
    return {'stats': values}


def coff(symbols=True):
    header = struct.pack('<HHLLLHH', 0x8664, 1, 0, 0, 0, 0, 0)
    section = (b'.debug$S' if symbols else b'.text\0\0\0') + b'\0' * 32
    return header + section


class CacheCounterTests(unittest.TestCase):
    def test_cold_requires_one_miss_write_and_compile(self):
        proof = fixture.prove_counters('cold', stats(), stats(compile_requests=1, cache_misses=1, cache_writes=1, compilations=1))
        self.assertEqual(proof['cache_misses'], 1)  # Advanced counts must not double count.

    def test_warm_requires_hit_without_compiler_execution(self):
        proof = fixture.prove_counters('warm', stats(), stats(compile_requests=1, cache_hits=1))
        self.assertEqual(proof['cache_hits'], 1)

    def test_exit_zero_or_noop_stats_are_not_a_hit(self):
        with self.assertRaisesRegex(RuntimeError, 'no genuine'):
            fixture.prove_counters('warm', stats(), stats())

    def test_noncacheable_compile_or_fallback_cannot_pass(self):
        for changes in ({'compile_requests': 1, 'requests_not_cacheable': 1, 'compilations': 1},
                        {'compile_requests': 1, 'cache_hits': 1, 'compilations': 1},
                        {'compile_requests': 1, 'cache_hits': 1, 'cache_errors': 1},
                        {'compile_requests': 1, 'cache_hits': 2}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                fixture.prove_counters('warm', stats(), stats(**changes))

    def test_delta_not_lifetime_totals(self):
        old = stats(compile_requests=1, cache_misses=1, cache_writes=1, compilations=1)
        new = stats(compile_requests=2, cache_misses=1, cache_writes=1, compilations=1, cache_hits=1)
        self.assertEqual(fixture.prove_counters('warm', old, new)['compilations'], 0)

    def test_invalid_counter_types_rejected(self):
        for value in (True, -1, '1', 1.0):
            document = stats()
            document['stats']['compile_requests'] = value
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                fixture.counters(document)

    def test_missing_stats_are_not_silently_zero(self):
        with self.assertRaises(KeyError):
            fixture.counters({'stats': {}})

    def test_actual_coff_debug_section_required(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.obj'
            for data, expected in ((coff(), True), (coff(False), False), (b'.debug$S', False),
                                   (coff()[:-1], False), (b'\x4c\x01' + coff()[2:], False)):
                path.write_bytes(data)
                self.assertEqual(fixture.has_embedded_symbols(path), expected)


@unittest.skipUnless(os.name == 'nt', 'This fixture requires Windows MSVC')
class NativeOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.sccache = self.output / 'sccache.exe'
        self.sccache.write_bytes(b'not executed')
        self.build = self.output / 'build'
        self.object = self.build / 'CMakeFiles/cache_probe.dir/cache_probe.cpp.obj'
        self.args = SimpleNamespace(cmake='cmake', ninja='ninja', sccache=str(self.sccache),
                                    build=self.build, mode='cold', report=self.output / 'cold.json', previous_report=None)
        self.stat_values = [stats(), stats(compile_requests=1, cache_misses=1, cache_writes=1, compilations=1)]
        self.commands = []
        self.launcher_output = 'sccache.exe cl.exe /Z7 /c cache_probe.cpp\n'
        self.env = patch.dict(os.environ, {'SCCACHE_SERVER_PORT': '12345', 'SCCACHE_CONF': 'private.toml'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.launcher = patch.object(fixture.subprocess, 'run', side_effect=self.mock_run)
        self.launcher.start()
        self.addCleanup(self.launcher.stop)
        self.statistics = patch.object(fixture, 'statistics', side_effect=lambda *_: self.stat_values.pop(0))
        self.statistics.start()
        self.addCleanup(self.statistics.stop)

    def mock_run(self, command, **kwargs):
        self.commands.append(command)
        self.object.parent.mkdir(parents=True, exist_ok=True)
        if '--build' in command:
            self.object.write_bytes(coff())
        return subprocess.CompletedProcess(command, 0, self.launcher_output)

    def warm(self):
        self.args.previous_report = self.args.report
        self.args.report = self.output / 'warm.json'
        self.args.mode = 'warm'
        self.stat_values = [stats(), stats(compile_requests=1, cache_hits=1)]

    def test_cold_warm_same_object_and_embedded_symbols(self):
        cold = fixture.run(self.args)
        self.warm()
        warm = fixture.run(self.args)
        self.assertEqual(cold['object'], warm['object'])
        self.assertTrue(warm['forced_compile_object_removed'])
        self.assertTrue(warm['embedded_codeview_symbols'])
        self.assertEqual(len(self.commands), 3)  # Warm does not reconfigure.

    def test_warm_rejects_changed_object_without_deleting_it(self):
        fixture.run(self.args)
        self.warm()
        self.object.write_bytes(b'changed output')
        with self.assertRaisesRegex(RuntimeError, 'do not match'):
            fixture.run(self.args)
        self.assertEqual(self.object.read_bytes(), b'changed output')

    def test_cold_rejects_existing_tree_without_launch(self):
        self.build.mkdir()
        with self.assertRaises(FileExistsError):
            fixture.run(self.args)
        self.assertFalse(self.commands)

    def test_shared_pdb_compile_fails_and_saves_diagnostics(self):
        self.launcher_output = 'sccache.exe cl.exe /Zi /c cache_probe.cpp\n'
        with self.assertRaisesRegex(RuntimeError, 'must use sccache and /Z7'):
            fixture.run(self.args)
        report = json.loads(self.args.report.read_text())
        self.assertEqual(report['status'], 'failed')
        self.assertIn('/Zi', report['build_output'])

    def test_missing_private_endpoint_never_launches(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(RuntimeError, 'private endpoint'):
            fixture.run(self.args)
        self.assertFalse(self.commands)
