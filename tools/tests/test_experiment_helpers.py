import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.name == 'nt', 'MSVC experiment helpers require Windows')
class ExperimentHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import psutil  # noqa: F401
        except ImportError:
            raise unittest.SkipTest('psutil is not installed')
        path = Path(__file__).parents[1] / 'experiments/openvdb_registration.py'
        spec = importlib.util.spec_from_file_location('registration', path)
        cls.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.helper)

    def test_windows_arguments_round_trip(self):
        arguments = ['plain', '/IC:\\Program Files\\headers', 'C:\\name with spaces\\', '/DVALUE=a b', '']
        self.assertEqual(self.helper.tokens(subprocess.list2cmdline(arguments)), arguments)

    def test_ninja_paths_preserve_escaped_spaces(self):
        self.assertEqual(self.helper.ninja_paths('C$:\\a$ b\\test.obj dir\\other.obj'),
                         ['C:\\a b\\test.obj', 'dir\\other.obj'])

    def test_ninja_block_is_specific(self):
        content = 'build one: RULE a\n  FLAGS = one\n\nbuild two: RULE b\n  FLAGS = two\n'
        header, variables = self.helper.block(content, 'build two:')
        self.assertEqual(header, 'build two: RULE b')
        self.assertEqual(variables, {'FLAGS': 'two'})
        with self.assertRaises(ValueError):
            self.helper.block(content, 'build absent:')

    def test_refuses_low_available_memory_before_launch(self):
        with patch.object(self.helper.psutil, 'virtual_memory', return_value=SimpleNamespace(available=1024)), \
             patch.object(self.helper.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, '6 GiB'):
                self.helper.run('no_launch', ['unused'], Path.cwd(), Path.cwd(), [])
            launch.assert_not_called()

    def test_process_exit_during_child_sampling_is_normal(self):
        helper = self.helper
        process = SimpleNamespace(pid=999999, returncode=0)
        polls = iter([None, 0, 0])
        process.poll = lambda: next(polls, 0)
        process.wait = lambda: 0
        parent = SimpleNamespace(cpu_affinity=lambda *args: [0])

        def disappeared(**kwargs):
            raise helper.psutil.NoSuchProcess(process.pid)

        parent.children = disappeared
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(helper.psutil, 'virtual_memory', return_value=SimpleNamespace(available=8 * 1024**3)), \
             patch.object(helper.subprocess, 'Popen', return_value=process), \
             patch.object(helper.psutil, 'Process', return_value=parent), \
             patch.object(helper.time, 'sleep'):
            records = []
            self.assertEqual(helper.run('exit_race', ['unused'], Path(temp), Path(temp), records), '')
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]['step'], 'exit_race')

    def test_memory_guard_terminates_its_process_and_records_failure(self):
        helper = self.helper
        process = SimpleNamespace(pid=999999, returncode=None)
        process.poll = lambda: process.returncode
        process.wait = lambda: process.returncode
        process.kill = lambda: setattr(process, 'returncode', -9)
        parent = SimpleNamespace(cpu_affinity=lambda *args: [0], children=lambda **kwargs: [],
                                 memory_info=lambda: SimpleNamespace(rss=4 * 1024**3))
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(helper.psutil, 'virtual_memory', return_value=SimpleNamespace(available=8 * 1024**3)), \
             patch.object(helper.subprocess, 'Popen', return_value=process), \
             patch.object(helper.psutil, 'Process', return_value=parent):
            records = []
            with self.assertRaisesRegex(RuntimeError, '3 GiB'):
                helper.run('memory_guard', ['unused'], Path(temp), Path(temp), records)
            self.assertEqual(process.returncode, -9)
            self.assertEqual(records[0]['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
