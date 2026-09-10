"""Resource-guard regression tests. No native child process is ever launched."""

import importlib.util
import ctypes
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location(
    'run_guarded_under_test', Path(__file__).resolve().parents[1] / 'run_guarded.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class FakeChild:
    pid = 424242

    def __init__(self, code=0):
        self.code = code
        self.returncode = None
        self.polls = 0

    def poll(self):
        self.polls += 1
        if self.polls > 1:
            self.returncode = self.code
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = self.code
        return self.code

    def kill(self):
        self.returncode = -1


class FakeRoot:
    def __init__(self):
        self.cpus = [4, 5, 6, 7]

    def cpu_affinity(self, selected=None):
        if selected is not None:
            self.cpus = selected
        return self.cpus

    def nice(self, value):
        pass


class FakeTree:
    def __init__(self, rss=guard.GIB):
        self.root = FakeRoot()
        self.rss = rss
        self.stopped = False

    def sample_rss(self):
        return self.rss

    def stop(self):
        self.stopped = True
        return []


class GuardedRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='prusaslicer-guard-tests-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.child = FakeChild()
        self.tree = FakeTree()

        # Popen is patched for EVERY test, including error cases, so a change to
        # the runner cannot accidentally execute a compiler during this suite.
        self.launch = self.start_patch(guard.subprocess, 'Popen', return_value=self.child)
        self.start_patch(guard, 'ProcessTree', return_value=self.tree)
        self.start_patch(guard, 'windows_physical_core_masks', return_value=[0x30, 0xc0])
        self.commit = self.start_patch(guard, 'windows_commit_available', return_value=20 * guard.GIB)
        self.start_patch(guard, 'windows_cap_working_set', side_effect=AssertionError('Native cap must be mocked'))
        self.memory = self.start_patch(
            guard.psutil, 'virtual_memory', return_value=SimpleNamespace(available=20 * guard.GIB))
        self.start_patch(guard.time, 'sleep')
        self.start_patch(guard, 'print')
        environment = patch.dict(os.environ, {'CMAKE_BUILD_PARALLEL_LEVEL': '1'})
        environment.start()
        self.addCleanup(environment.stop)

    def start_patch(self, owner, name, **kwargs):
        replacement = patch.object(owner, name, **kwargs)
        mocked = replacement.start()
        self.addCleanup(replacement.stop)
        return mocked

    def execute(self, label, **options):
        result = guard.run_guarded(
            ['mock-cmake', '--build', 'fixture', '--parallel', '1'], self.directory, label, **options)
        report = json.loads((self.directory / (label + '.json')).read_text(encoding='utf-8'))
        return result, report

    def mock_platform(self, name):
        # Replace only the runner's os reference, not os.name globally: pathlib
        # must keep using the real host's path implementation in these tests.
        self.start_patch(guard, 'os', new=SimpleNamespace(
            name=name, environ=os.environ, getpid=os.getpid))
        if name == 'nt':
            self.start_patch(guard.subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', new=0x4000, create=True)
            self.start_patch(guard.subprocess, 'CREATE_NO_WINDOW', new=0x08000000, create=True)

    def test_success_preserves_environment_and_records_identity(self):
        result, report = self.execute('success')
        self.assertEqual(result, 0)
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['command_returncode'], 0)
        self.assertEqual(report['build_parallel_level'], '1')
        self.assertEqual(report['cpu_affinity'], [5, 7] if os.name == 'nt' else [6, 7])
        self.assertEqual(report['pid'], FakeChild.pid)
        self.assertEqual(report['runner_pid'], os.getpid())
        self.assertEqual(report['sampled_peak_tree_rss_bytes'], guard.GIB)
        self.assertIsNone(report['limits']['timeout_seconds'])
        self.assertTrue(self.tree.stopped)
        self.assertEqual(self.launch.call_args.args[0], report['command'])
        self.assertNotIn('shell', self.launch.call_args.kwargs)
        if os.name == 'nt':
            self.assertEqual(self.launch.call_args.kwargs['env'], {
                **os.environ, '_MSPDBSRV_ENDPOINT_': report['mspdbsrv_endpoint']})
        else:
            self.assertNotIn('env', self.launch.call_args.kwargs)

    def test_windows_uses_fresh_endpoint_for_each_run(self):
        self.mock_platform('nt')
        _, first_report = self.execute('first-endpoint')
        first_endpoint = self.launch.call_args.kwargs['env']['_MSPDBSRV_ENDPOINT_']
        _, second_report = self.execute('second-endpoint')
        second_endpoint = self.launch.call_args.kwargs['env']['_MSPDBSRV_ENDPOINT_']
        self.assertRegex(first_endpoint, r'^prusaslicer_[0-9a-f]{32}$')
        self.assertRegex(second_endpoint, r'^prusaslicer_[0-9a-f]{32}$')
        self.assertNotEqual(first_endpoint, second_endpoint)
        self.assertEqual(first_report['mspdbsrv_endpoint'], first_endpoint)
        self.assertEqual(second_report['mspdbsrv_endpoint'], second_endpoint)

    def test_windows_preserves_parent_and_all_other_child_environment(self):
        self.mock_platform('nt')
        with patch.dict(os.environ, {'GUARDED_TEST_SENTINEL': 'spaces;unicode-δ=ok'}):
            parent_environment = os.environ.copy()
            _, report = self.execute('private-environment')
            child_environment = self.launch.call_args.kwargs['env']
            self.assertIsNot(child_environment, os.environ)
            self.assertEqual(os.environ.copy(), parent_environment)
            self.assertEqual(child_environment, {
                **parent_environment, '_MSPDBSRV_ENDPOINT_': report['mspdbsrv_endpoint']})

    def test_windows_replaces_caller_endpoint_in_child_only(self):
        self.mock_platform('nt')
        with patch.dict(os.environ, {'_MSPDBSRV_ENDPOINT_': 'caller-shared-endpoint'}):
            _, report = self.execute('replace-caller-endpoint')
            child_endpoint = self.launch.call_args.kwargs['env']['_MSPDBSRV_ENDPOINT_']
            self.assertNotEqual(child_endpoint, 'caller-shared-endpoint')
            self.assertEqual(report['mspdbsrv_endpoint'], child_endpoint)
            self.assertEqual(os.environ['_MSPDBSRV_ENDPOINT_'], 'caller-shared-endpoint')

    def test_non_windows_inheritance_is_unchanged(self):
        self.mock_platform('posix')
        with patch.dict(os.environ, {'_MSPDBSRV_ENDPOINT_': 'caller-endpoint'}):
            _, report = self.execute('non-windows-environment')
            self.assertNotIn('env', self.launch.call_args.kwargs)
            self.assertNotIn('creationflags', self.launch.call_args.kwargs)
            self.assertNotIn('mspdbsrv_endpoint', report)
            self.assertEqual(os.environ['_MSPDBSRV_ENDPOINT_'], 'caller-endpoint')

    def test_windows_guard_records_physical_core_selection(self):
        self.mock_platform('nt')
        result, report = self.execute('distinct-cores')
        self.assertEqual(result, 0)
        self.assertEqual(self.tree.root.cpus, [5, 7])
        self.assertEqual(report['cpu_affinity'], [5, 7])
        self.assertEqual(report['cpu_affinity_selection'], {
            'method': 'distinct-physical-cores-first', 'allowed': [4, 5, 6, 7],
            'requested_count': 2, 'selected': [5, 7],
            'physical_core_masks': ['0x30', '0xc0'], 'selected_physical_core_count': 2})

    def test_windows_guard_records_topology_failure_and_keeps_cpu_bound(self):
        self.mock_platform('nt')
        guard.windows_physical_core_masks.side_effect = OSError('mock topology unavailable')
        result, report = self.execute('topology-fallback')
        self.assertEqual(result, 0)
        self.assertEqual(self.tree.root.cpus, [6, 7])
        self.assertEqual(report['cpu_affinity'], [6, 7])
        self.assertEqual(report['cpu_affinity_selection']['method'], 'last-allowed')
        self.assertIn('mock topology unavailable', report['cpu_affinity_selection']['fallback_reason'])

    def test_optional_working_set_caps_precede_rss_sampling_and_are_recorded(self):
        self.mock_platform('nt')
        events = []

        def cap(tree, maximum, endpoint, records):
            events.append('cap')
            self.assertIs(tree, self.tree)
            self.assertEqual(maximum, 4 * guard.GIB)
            self.assertRegex(endpoint, r'^prusaslicer_[0-9a-f]{32}$')
            records.append({'pid': 123, 'created': 1.0, 'readback_maximum_bytes': maximum})

        def sample():
            events.append('sample')
            return guard.GIB

        self.start_patch(guard, 'cap_owned_link_processes', side_effect=cap)
        self.start_patch(self.tree, 'sample_rss', side_effect=sample)
        result, report = self.execute('working-set-opt-in', link_working_set_mib=4096)
        self.assertEqual(result, 0)
        self.assertEqual(events, ['cap', 'sample'])
        self.assertEqual(report['limits']['link_process_working_set_bytes'], 4 * guard.GIB)
        self.assertEqual(report['working_set_caps'][0]['readback_maximum_bytes'], 4 * guard.GIB)

    def test_working_set_api_failure_stops_owned_run(self):
        self.mock_platform('nt')
        self.start_patch(guard, 'cap_owned_link_processes', side_effect=OSError('quota refused'))
        result, report = self.execute('working-set-error', link_working_set_mib=4096)
        self.assertEqual(result, 1)
        self.assertEqual(report['status'], 'runner-failed')
        self.assertIn('quota refused', report['error'])
        self.assertTrue(self.tree.stopped)

    def test_commit_guard_prevents_launch_with_low_headroom(self):
        self.mock_platform('nt')
        self.commit.return_value = 5 * guard.GIB
        result, report = self.execute('commit-low-before', minimum_commit_gib=6)
        self.assertEqual(result, 2)
        self.assertEqual(report['status'], 'guard-stopped')
        self.assertEqual(report['sampled_minimum_available_system_commit_bytes'], 5 * guard.GIB)
        self.launch.assert_not_called()

    def test_commit_guard_stops_when_headroom_falls(self):
        self.mock_platform('nt')
        self.commit.side_effect = [20 * guard.GIB, 5 * guard.GIB]
        result, report = self.execute('commit-low-during', minimum_commit_gib=6)
        self.assertEqual(result, 2)
        self.assertEqual(report['limits']['minimum_available_system_commit_bytes'], 6 * guard.GIB)
        self.assertEqual(report['sampled_minimum_available_system_commit_bytes'], 5 * guard.GIB)
        self.assertTrue(self.tree.stopped)

    def test_commit_query_failure_before_or_after_launch_fails_closed(self):
        self.mock_platform('nt')
        for label, responses in (
                ('commit-error-before', [OSError('commit query failed')]),
                ('commit-error-during', [20 * guard.GIB, OSError('commit query failed')])):
            with self.subTest(label=label):
                self.child.polls = 0
                self.child.returncode = None
                self.launch.reset_mock()
                self.commit.side_effect = responses
                result, report = self.execute(label, minimum_commit_gib=6)
                self.assertEqual(result, 1)
                self.assertEqual(report['status'], 'runner-failed')
                self.assertIn('commit query failed', report['error'])
                if label.endswith('before'):
                    self.launch.assert_not_called()
                else:
                    self.assertTrue(self.tree.stopped)

    def test_default_guard_does_not_query_commit_or_cap_processes(self):
        with patch.object(guard, 'cap_owned_link_processes') as cap:
            _, report = self.execute('unchanged-defaults')
            cap.assert_not_called()
        self.commit.assert_not_called()
        self.assertNotIn('working_set_caps', report)
        self.assertNotIn('sampled_minimum_available_system_commit_bytes', report)

    def test_optional_windows_limits_are_rejected_on_other_platforms(self):
        self.mock_platform('posix')
        for options in ({'link_working_set_mib': 4096}, {'minimum_commit_gib': 6}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, 'Windows-only'):
                self.execute('non-windows-limits', **options)
        self.launch.assert_not_called()

    def test_nonzero_command_exit_is_reported_and_propagated(self):
        self.child.code = 17
        result, report = self.execute('native-error')
        self.assertEqual(result, 17)
        self.assertEqual(report['status'], 'command-failed')
        self.assertEqual(report['command_returncode'], 17)
        self.assertIn('17', report['error'])
        self.assertTrue(self.tree.stopped)

    def test_process_tree_memory_limit_stops_owned_work(self):
        self.tree.rss = 7 * guard.GIB
        result, report = self.execute('rss-limit')
        self.assertEqual(result, 2)
        self.assertEqual(report['status'], 'guard-stopped')
        self.assertEqual(report['sampled_peak_tree_rss_bytes'], 7 * guard.GIB)
        self.assertIn('above 6', report['error'])
        self.assertTrue(self.tree.stopped)

    def test_low_system_memory_during_run_stops_owned_work(self):
        self.memory.side_effect = [
            SimpleNamespace(available=20 * guard.GIB),
            SimpleNamespace(available=5 * guard.GIB),
        ]
        result, report = self.execute('low-free-memory')
        self.assertEqual(result, 2)
        self.assertEqual(report['status'], 'guard-stopped')
        self.assertEqual(report['sampled_minimum_system_available_bytes'], 5 * guard.GIB)
        self.assertTrue(self.tree.stopped)

    def test_low_system_memory_before_launch_never_starts_command(self):
        self.memory.return_value = SimpleNamespace(available=5 * guard.GIB)
        result, report = self.execute('low-free-before-launch')
        self.assertEqual(result, 2)
        self.assertEqual(report['status'], 'guard-stopped')
        self.assertNotIn('pid', report)
        self.launch.assert_not_called()

    def test_launch_headroom_does_not_lower_runtime_reserves(self):
        self.memory.side_effect = [SimpleNamespace(available=8 * guard.GIB), SimpleNamespace(available=7 * guard.GIB)]
        result, report = self.execute('headroom-runtime', launch_headroom_gib=2)
        self.assertEqual(result, 0)
        self.assertEqual(report['limits']['minimum_available_system_bytes'], 6 * guard.GIB)
        self.assertEqual(report['limits']['launch_headroom_bytes'], 2 * guard.GIB)

    def test_launch_requires_ram_reserve_plus_worker_headroom(self):
        self.memory.return_value = SimpleNamespace(available=7 * guard.GIB)
        result, report = self.execute('headroom-ram', launch_headroom_gib=2)
        self.assertEqual(result, 2)
        self.assertEqual(report['status'], 'guard-stopped')
        self.launch.assert_not_called()

    def test_launch_requires_commit_reserve_plus_worker_headroom(self):
        self.mock_platform('nt')
        self.commit.return_value = 7 * guard.GIB
        result, report = self.execute('headroom-commit', launch_headroom_gib=2, minimum_commit_gib=6)
        self.assertEqual(result, 2)
        self.assertEqual(report['status'], 'guard-stopped')
        self.launch.assert_not_called()

    def test_optional_deadline_stops_only_owned_work(self):
        self.start_patch(guard.time, 'monotonic', side_effect=range(100))
        result, report = self.execute('deadline', timeout_seconds=1)
        self.assertEqual(result, 2)
        self.assertIn('timeout', report['error'])
        self.assertEqual(report['limits']['timeout_seconds'], 1)
        self.assertTrue(self.tree.stopped)

    def test_programmatic_invalid_limits_never_launch(self):
        for options in ({'memory_gib': float('nan')}, {'minimum_free_gib': -1},
                        {'cpu_count': 0}, {'cpu_count': True}, {'timeout_seconds': 0},
                        {'launch_headroom_gib': -1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.execute('invalid-api', **options)
        self.launch.assert_not_called()

    def test_launch_error_still_produces_report(self):
        self.launch.side_effect = OSError('mock launch failure')
        result, report = self.execute('launch-error')
        self.assertEqual(result, 1)
        self.assertEqual(report['status'], 'runner-failed')
        self.assertIn('mock launch failure', report['error'])
        self.assertNotIn('command_returncode', report)

    def test_existing_label_does_not_overwrite_prior_files(self):
        self.execute('duplicate')
        log = self.directory / 'duplicate.log'
        report = self.directory / 'duplicate.json'
        original_log, original_report = log.read_bytes(), report.read_bytes()
        self.launch.reset_mock()
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.execute('duplicate')
        self.assertEqual(log.read_bytes(), original_log)
        self.assertEqual(report.read_bytes(), original_report)
        self.launch.assert_not_called()

    def test_cleanup_exception_is_recorded(self):
        self.start_patch(self.tree, 'stop', side_effect=RuntimeError('mock cleanup failure'))
        result, report = self.execute('cleanup-error')
        self.assertEqual(result, 1)
        self.assertEqual(report['status'], 'cleanup-failed')
        self.assertIn('mock cleanup failure', report['cleanup_errors'][0])
        self.assertEqual(report['command_returncode'], 0)

    def test_keyboard_interrupt_cleans_up_and_reports_cancellation(self):
        self.start_patch(self.tree, 'sample_rss', side_effect=KeyboardInterrupt)
        result, report = self.execute('interrupt')
        self.assertEqual(result, 130)
        self.assertEqual(report['status'], 'interrupted')
        self.assertEqual(report['error'], 'Interrupted by caller')
        self.assertTrue(self.tree.stopped)


class ProcessExitRaceTests(unittest.TestCase):
    def test_root_disappearing_before_tracking_is_harmless(self):
        with patch.object(guard.subprocess, 'Popen') as launch, \
             patch.object(guard.psutil, 'Process', side_effect=guard.psutil.NoSuchProcess(1)), \
             patch.object(guard.psutil, 'wait_procs', return_value=([], [])):
            tree = guard.ProcessTree(1)
            self.assertIsNone(tree.root)
            self.assertEqual(tree.collect(), [])
            self.assertEqual(tree.sample_rss(), 0)
            self.assertEqual(tree.stop(), [])
            launch.assert_not_called()


class ProcessCleanupIdentityTests(unittest.TestCase):
    """Race injection only: no native processes are queried or manipulated."""

    def setUp(self):
        self.key = (142404, 1700000000.125)
        self.process = Mock(pid=self.key[0])
        self.tree = guard.ProcessTree.__new__(guard.ProcessTree)
        self.tree.root = self.process
        self.tree.members = {self.key: self.process}
        self.current = Mock()
        self.current.create_time.return_value = self.key[1]
        self.current.is_running.return_value = True
        self.factory = self.start_patch(guard.psutil, 'Process', return_value=self.current)
        self.exists = self.start_patch(guard.psutil, 'pid_exists', return_value=True)
        self.collect = self.start_patch(self.tree, 'collect', side_effect=lambda: list(self.tree.members.values()))
        self.wait = self.start_patch(guard.psutil, 'wait_procs', return_value=([], []))
        self.start_patch(guard.subprocess, 'Popen', side_effect=AssertionError('No child launches in cleanup tests'))

    def start_patch(self, owner, name, **options):
        context = patch.object(owner, name, **options)
        value = context.start()
        self.addCleanup(context.stop)
        return value

    def test_confirmed_gone_before_suspend_is_not_modified(self):
        self.factory.side_effect = guard.psutil.NoSuchProcess(self.key[0])
        self.assertEqual(self.tree.stop(), [])
        self.process.suspend.assert_not_called()
        self.process.kill.assert_not_called()
        self.assertEqual(self.tree.cleanup_events[0]['identity'], 'gone')

    def test_reused_pid_before_suspend_is_not_modified(self):
        self.current.create_time.return_value = self.key[1] + 1
        self.assertEqual(self.tree.stop(), [])
        self.process.suspend.assert_not_called()
        self.process.kill.assert_not_called()
        self.assertEqual(self.tree.cleanup_events[0]['identity'], 'reused')

    def test_suspend_access_denied_then_confirmed_exit_is_benign(self):
        self.process.suspend.side_effect = guard.psutil.AccessDenied(self.key[0])
        self.factory.side_effect = [self.current, guard.psutil.NoSuchProcess(self.key[0])]
        self.assertEqual(self.tree.stop(), [])
        self.process.kill.assert_not_called()
        self.assertEqual(self.tree.cleanup_events[0], {
            'phase': 'suspend', 'pid': self.key[0], 'created': self.key[1], 'identity': 'gone',
            'error_type': 'AccessDenied', 'error': str(guard.psutil.AccessDenied(self.key[0]))})

    def test_suspend_access_denied_then_reused_pid_is_benign(self):
        self.process.suspend.side_effect = guard.psutil.AccessDenied(self.key[0])
        replacement = Mock()
        replacement.create_time.return_value = self.key[1] + 1
        self.factory.side_effect = [self.current, replacement]
        self.assertEqual(self.tree.stop(), [])
        self.process.kill.assert_not_called()
        self.assertEqual(self.tree.cleanup_events[0]['identity'], 'reused')

    def test_still_owned_access_denied_remains_a_failure(self):
        self.process.suspend.side_effect = guard.psutil.AccessDenied(self.key[0])
        errors = self.tree.stop()
        self.assertEqual(len(errors), 1)
        for detail in ('suspend', 'AccessDenied', 'pid=142404', 'created=1700000000.125', 'identity=owned'):
            self.assertIn(detail, errors[0])
        self.process.kill.assert_called_once()

    def test_inaccessible_identity_is_not_assumed_gone(self):
        self.factory.side_effect = guard.psutil.AccessDenied(self.key[0])
        errors = self.tree.stop()
        self.assertTrue(errors)
        self.assertIn('identity=unknown', errors[0])
        self.process.suspend.assert_not_called()
        self.process.kill.assert_not_called()

    def test_inaccessible_but_independently_absent_pid_is_benign(self):
        self.factory.side_effect = guard.psutil.AccessDenied(self.key[0])
        self.exists.return_value = False
        self.assertEqual(self.tree.stop(), [])
        self.process.suspend.assert_not_called()
        self.process.kill.assert_not_called()

    def test_kill_access_denied_then_exit_is_benign(self):
        self.process.kill.side_effect = guard.psutil.AccessDenied(self.key[0])
        self.factory.side_effect = [self.current, self.current, guard.psutil.NoSuchProcess(self.key[0])]
        self.assertEqual(self.tree.stop(), [])
        self.assertEqual(self.tree.cleanup_events[0]['phase'], 'kill')
        self.assertEqual(self.tree.cleanup_events[0]['identity'], 'gone')

    def test_collect_access_denied_after_exit_is_benign(self):
        self.collect.side_effect = guard.psutil.AccessDenied(self.key[0])
        self.factory.side_effect = [self.current, guard.psutil.NoSuchProcess(self.key[0])]
        self.assertEqual(self.tree.stop(), [])
        self.process.kill.assert_not_called()
        self.assertEqual(self.tree.cleanup_events[0]['phase'], 'collect')

    def test_wait_does_not_confuse_reused_pid_with_owned_survivor(self):
        self.wait.return_value = ([], [self.process])
        replacement = Mock()
        replacement.create_time.return_value = self.key[1] + 1
        self.factory.side_effect = [self.current, self.current, replacement]
        self.assertEqual(self.tree.stop(), [])
        self.assertEqual(self.tree.cleanup_events[0]['identity'], 'reused')

    def test_owned_survivor_is_reported_as_failure(self):
        self.wait.return_value = ([], [self.process])
        errors = self.tree.stop()
        self.assertEqual(len(errors), 1)
        self.assertIn('wait: RuntimeError pid=142404', errors[0])
        self.assertIn('identity=owned', errors[0])


class CpuSelectionTests(unittest.TestCase):
    def select(self, allowed, count, masks):
        with patch.object(guard, 'os', SimpleNamespace(name='nt')), \
             patch.object(guard, 'windows_physical_core_masks', return_value=masks), \
             patch.object(guard.subprocess, 'Popen') as launch:
            result = guard.select_cpu_affinity(allowed, count)
            launch.assert_not_called()
            return result

    def test_prefers_distinct_later_cores_independent_of_api_record_order(self):
        selected, detail = self.select(list(range(8)), 2, [0xc0, 0x03, 0x30, 0x0c])
        self.assertEqual(selected, [5, 7])
        self.assertEqual(detail['selected_physical_core_count'], 2)

    def test_restricted_allowed_set_never_adds_unavailable_cpus(self):
        selected, detail = self.select([0, 2, 3, 6], 3, [0x03, 0x0c, 0x30, 0xc0])
        self.assertEqual(selected, [0, 3, 6])
        self.assertEqual(detail['selected_physical_core_count'], 3)

    def test_adds_siblings_only_after_all_allowed_cores_are_represented(self):
        selected, detail = self.select(list(range(8)), 6, [0x03, 0x0c, 0x30, 0xc0])
        self.assertEqual(selected, [1, 3, 4, 5, 6, 7])
        self.assertEqual(detail['selected_physical_core_count'], 4)

    def test_single_allowed_core_and_excess_request_remain_bounded(self):
        selected, detail = self.select([6, 7], 5, [0x03, 0x0c, 0x30, 0xc0])
        self.assertEqual(selected, [6, 7])
        self.assertEqual(detail['selected_physical_core_count'], 1)

    def test_incomplete_or_overlapping_topology_falls_back(self):
        for masks in ([], [0x03], [0x03, 0x0e], [0x00, 0x0f]):
            with self.subTest(masks=masks):
                selected, detail = self.select([0, 1, 2, 3], 2, masks)
                self.assertEqual(selected, [2, 3])
                self.assertEqual(detail['method'], 'last-allowed')
                self.assertIn('fallback_reason', detail)

    def test_empty_allowed_set_never_returns_reset_affinity(self):
        with self.assertRaisesRegex(ValueError, 'allowed CPUs'):
            self.select([], 2, [0x03])

    def test_non_windows_keeps_last_allowed_policy_without_query(self):
        with patch.object(guard, 'os', SimpleNamespace(name='posix')), \
             patch.object(guard, 'windows_physical_core_masks') as query:
            selected, detail = guard.select_cpu_affinity([0, 1, 2, 3], 2)
            self.assertEqual(selected, [2, 3])
            self.assertEqual(detail['method'], 'last-allowed')
            query.assert_not_called()


class LinkWorkingSetTests(unittest.TestCase):
    def process(self, pid, name='link.exe', endpoint='private'):
        return SimpleNamespace(pid=pid, create_time=Mock(return_value=float(pid)),
                               is_running=Mock(return_value=True), name=Mock(return_value=name),
                               environ=Mock(return_value={'_MSPDBSRV_ENDPOINT_': endpoint}))

    def test_only_private_linker_and_pdb_processes_are_capped_once(self):
        processes = [self.process(1), self.process(2, 'mspdbsrv.exe'), self.process(3, 'cmake.exe')]
        tree = SimpleNamespace(collect=lambda: processes)
        records = []
        with patch.object(guard, 'windows_cap_working_set',
                          side_effect=lambda process, maximum: {'pid': process.pid, 'created': float(process.pid)}) as cap:
            guard.cap_owned_link_processes(tree, 4 * guard.GIB, 'private', records)
            guard.cap_owned_link_processes(tree, 4 * guard.GIB, 'private', records)
            self.assertEqual(cap.call_count, 2)
        self.assertEqual([record['pid'] for record in records], [1, 2])
        self.assertTrue(all(record['private_endpoint_verified'] for record in records))

    def test_foreign_endpoint_is_never_modified(self):
        process = self.process(1, endpoint='another-build')
        with patch.object(guard, 'windows_cap_working_set') as cap:
            with self.assertRaisesRegex(guard.GuardLimit, 'endpoint mismatch'):
                guard.cap_owned_link_processes(SimpleNamespace(collect=lambda: [process]),
                                               4 * guard.GIB, 'private', [])
            cap.assert_not_called()

    def test_disappearing_process_is_skipped(self):
        process = self.process(1)
        with patch.object(guard, 'windows_cap_working_set', side_effect=guard.psutil.NoSuchProcess(1)):
            records = []
            guard.cap_owned_link_processes(SimpleNamespace(collect=lambda: [process]),
                                           4 * guard.GIB, 'private', records)
            self.assertEqual(records, [])


class WindowsMemoryApiTests(unittest.TestCase):
    """Native calls are mocked, including the quota setter; never cap a process."""

    def setUp(self):
        self.creation = 1600000000.0
        self.process = SimpleNamespace(pid=123, create_time=Mock(return_value=self.creation),
                                       is_running=Mock(return_value=True))
        self.kernel = SimpleNamespace(OpenProcess=Mock(return_value=321), CloseHandle=Mock(return_value=True),
                                      GetProcessTimes=Mock(side_effect=self.get_times),
                                      SetProcessWorkingSetSizeEx=Mock(return_value=True),
                                      GetProcessWorkingSetSizeEx=Mock(side_effect=self.get_limit))
        for name, replacement in (
                ('WinDLL', Mock(return_value=self.kernel)),
                ('get_last_error', Mock(return_value=5)),
                ('WinError', lambda number: OSError(number, 'mock Windows error'))):
            context = patch.object(ctypes, name, replacement, create=True)
            context.start()
            self.addCleanup(context.stop)

    def get_times(self, handle, creation, *_):
        ticks = int((self.creation + 11644473600.0) * 10000000)
        creation._obj.dwHighDateTime = ticks >> 32
        creation._obj.dwLowDateTime = ticks & 0xffffffff
        return True

    def get_limit(self, handle, minimum, maximum, flags):
        minimum._obj.value = 16 * 1024 ** 2
        maximum._obj.value = 4 * guard.GIB
        flags._obj.value = 6
        return True

    def test_identity_held_set_and_readback_are_verified(self):
        record = guard.windows_cap_working_set(self.process, 4 * guard.GIB)
        self.kernel.SetProcessWorkingSetSizeEx.assert_called_once_with(321, 16 * 1024 ** 2, 4 * guard.GIB, 6)
        self.assertEqual(record['pid'], 123)
        self.assertEqual(record['created'], self.creation)
        self.assertEqual(record['readback_maximum_bytes'], 4 * guard.GIB)
        self.assertEqual(record['readback_flags'], 6)
        self.kernel.CloseHandle.assert_called_once_with(321)

    def test_reused_pid_is_never_modified(self):
        self.process.create_time.return_value = self.creation - 0.0005
        with self.assertRaises(guard.psutil.NoSuchProcess):
            guard.windows_cap_working_set(self.process, 4 * guard.GIB)
        self.kernel.SetProcessWorkingSetSizeEx.assert_not_called()
        self.kernel.CloseHandle.assert_called_once_with(321)

    def test_set_failure_closes_handle_and_raises(self):
        self.kernel.SetProcessWorkingSetSizeEx.return_value = False
        with self.assertRaises(OSError):
            guard.windows_cap_working_set(self.process, 4 * guard.GIB)
        self.kernel.GetProcessWorkingSetSizeEx.assert_not_called()
        self.kernel.CloseHandle.assert_called_once_with(321)

    def test_missing_hard_maximum_readback_fails_closed(self):
        def soft_limit(handle, minimum, maximum, flags):
            self.get_limit(handle, minimum, maximum, flags)
            flags._obj.value = 8
            return True
        self.kernel.GetProcessWorkingSetSizeEx.side_effect = soft_limit
        with self.assertRaisesRegex(OSError, 'hard working-set'):
            guard.windows_cap_working_set(self.process, 4 * guard.GIB)
        self.kernel.CloseHandle.assert_called_once_with(321)

    def test_conflicting_hard_limit_flags_fail_closed(self):
        for returned_flags in (4 | 8, 4 | 1):
            with self.subTest(flags=returned_flags):
                def conflicting_limit(handle, minimum, maximum, flags):
                    self.get_limit(handle, minimum, maximum, flags)
                    flags._obj.value = returned_flags
                    return True
                self.kernel.GetProcessWorkingSetSizeEx.side_effect = conflicting_limit
                with self.assertRaisesRegex(OSError, 'hard working-set'):
                    guard.windows_cap_working_set(self.process, 4 * guard.GIB)

    def test_commit_query_uses_commit_pages_and_page_size(self):
        def query(pointer, size):
            pointer._obj.CommitTotal = 1000
            pointer._obj.CommitLimit = 2500
            pointer._obj.PageSize = 4096
            return True
        self.kernel.GetPerformanceInfo = Mock(side_effect=query)
        self.assertEqual(guard.windows_commit_available(), 1500 * 4096)

    def test_commit_native_failure_raises(self):
        self.kernel.GetPerformanceInfo = Mock(return_value=False)
        with self.assertRaises(OSError):
            guard.windows_commit_available()


if __name__ == '__main__':
    unittest.main()
