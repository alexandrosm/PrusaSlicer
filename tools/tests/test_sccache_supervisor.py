"""All processes and sockets mocked; no cache server or compiler starts."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
try:
    spec = importlib.util.spec_from_file_location('sccache_supervisor_under_test', TOOLS / 'sccache_supervisor.py')
    supervisor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(supervisor)
finally:
    sys.path.pop(0)


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.tool = self.root / 'sccache.exe'
        self.tool.touch()
        self.server = Mock(pid=111)
        self.server.poll.return_value = None
        self.child = Mock(pid=222)
        self.child.poll.return_value = 0
        self.child.wait.return_value = 0
        self.server_tree = Mock(root=Mock(pid=111))
        self.server_tree.root.create_time.return_value = 123.0
        self.server_tree.root.cpu_affinity.return_value = [7]
        self.server_tree.stop.return_value = []
        self.child_tree = Mock()
        self.child_tree.stop.return_value = []
        self.launch = self.mock(supervisor.subprocess, 'Popen', side_effect=[self.server, self.child])
        self.stats = self.mock(supervisor.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=b'{"cache_hits":1}'))
        self.mock(supervisor, 'ProcessTree', side_effect=[self.server_tree, self.child_tree])
        self.mock(supervisor.psutil, 'Process', return_value=Mock(cpu_affinity=Mock(return_value=[7])))
        self.listener = self.mock(supervisor, 'owns_listener', return_value=True)
        socket = self.mock(supervisor.socket, 'socket')
        socket.return_value.__enter__.return_value.getsockname.return_value = ('127.0.0.1', 12345)
        self.mock(supervisor.time, 'sleep')
        self.mock(supervisor, 'print')

    def mock(self, owner, name, **arguments):
        item = patch.object(owner, name, **arguments)
        result = item.start()
        self.addCleanup(item.stop)
        return result

    def run_fixture(self):
        return supervisor.supervise(self.tool, ['mock-build'], self.root / 'session', self.root / 'cache')

    def test_private_environment_removes_ambient_cache_and_remote_settings(self):
        parent = {'PATH': 'compiler-path', '_MSPDBSRV_ENDPOINT_': 'private', 'SCCACHE_S3_BUCKET': 'secret',
                  'SCCACHE_SERVER_PORT': '4226', 'SCCACHE_START_SERVER': '1', 'SCCACHE_CONF': 'user.toml'}
        original = parent.copy()
        environment = supervisor.private_environment(parent, self.root, self.root / 'cache', 12345)
        self.assertEqual(parent, original)
        self.assertNotIn('SCCACHE_S3_BUCKET', environment)
        self.assertNotIn('SCCACHE_START_SERVER', environment)
        self.assertEqual(environment['SCCACHE_SERVER_PORT'], '12345')
        self.assertEqual(environment['PATH'], 'compiler-path')
        self.assertEqual(environment['_MSPDBSRV_ENDPOINT_'], 'private')

    def test_verified_foreground_server_build_and_cleanup(self):
        self.assertEqual(self.run_fixture(), 0)
        server_options = self.launch.call_args_list[0].kwargs
        client_options = self.launch.call_args_list[1].kwargs
        self.assertEqual(server_options['env']['SCCACHE_START_SERVER'], '1')
        self.assertEqual(server_options['env']['SCCACHE_NO_DAEMON'], '1')
        self.assertNotIn('SCCACHE_START_SERVER', client_options['env'])
        self.assertEqual(server_options['env']['SCCACHE_DIR'], client_options['env']['SCCACHE_DIR'])
        self.server_tree.stop.assert_called_once()
        self.child_tree.stop.assert_called_once()
        self.server_tree.root.cpu_affinity.assert_any_call([7])
        report = json.loads((self.root / 'session/server.json').read_text())
        self.assertTrue(report['listener_verified'])
        self.assertEqual(report['server_pid'], 111)
        self.assertEqual(report['stats']['cache_hits'], 1)

    def test_unverified_listener_cannot_start_build(self):
        self.listener.return_value = False
        self.server.poll.return_value = 1
        with self.assertRaisesRegex(RuntimeError, 'before binding'):
            self.run_fixture()
        self.assertEqual(self.launch.call_count, 1)
        self.server_tree.stop.assert_called_once()

    def test_listener_query_failure_fails_closed(self):
        self.listener.side_effect = OSError('denied')
        with self.assertRaisesRegex(OSError, 'denied'):
            self.run_fixture()
        self.assertEqual(self.launch.call_count, 1)
        self.server_tree.stop.assert_called_once()

    def test_server_failure_during_build_stops_both_trees(self):
        self.child.poll.return_value = None
        self.server.poll.return_value = 1
        with self.assertRaisesRegex(RuntimeError, 'while the build'):
            self.run_fixture()
        self.child_tree.stop.assert_called_once()
        self.server_tree.stop.assert_called_once()

    def test_cleanup_failure_still_stops_server(self):
        self.child_tree.stop.side_effect = RuntimeError('cleanup failed')
        self.assertEqual(self.run_fixture(), 1)
        self.server_tree.stop.assert_called_once()
        self.assertEqual(json.loads((self.root / 'session/server.json').read_text())['status'], 'cleanup-failed')

    def test_preexisting_session_is_not_overwritten(self):
        (self.root / 'session').mkdir()
        with self.assertRaises(FileExistsError):
            self.run_fixture()
        self.launch.assert_not_called()
