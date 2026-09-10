#!/usr/bin/env python3
"""Run one command with a sampled memory guard and bounded CPU affinity.

Example (inside the desired compiler/developer environment):
  python tools/run_guarded.py --log-dir out/full-build --label configure -- cmake --preset lean-release

The command runs without a shell and inherits the caller's environment, except
that Windows runs receive a fresh, child-only _MSPDBSRV_ENDPOINT_. This normal
MSVC tool convention isolates the PDB service from unrelated builds before
process-tree cleanup; it is not a security boundary. A caller-supplied endpoint
is intentionally replaced without changing the parent environment.

Windows affinity prefers one allowed logical CPU per physical core, starting
with later cores, before adding SMT siblings. If topology is unavailable or
ambiguous, it falls back to the last allowed CPUs and records the reason.

Optional Windows-only --link-working-set-mib limits resident memory of private
link.exe and mspdbsrv.exe descendants, not their committed memory. Paging may
increase substantially. --minimum-commit-gib separately guards system commit
headroom. These sampled protections are not a whole-machine resource quota.

This does not set build parallelism: set CMAKE_BUILD_PARALLEL_LEVEL and/or pass
the build tool's explicit worker count separately. There is no build time limit.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import psutil


GIB = 1024 ** 3


class GuardLimit(RuntimeError):
    pass


def positive_float(value):
    number = float(value)
    if not 0 < number < float('inf'):
        raise argparse.ArgumentTypeError('Expected a finite positive number')
    return number


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('Expected a positive integer')
    return number


def label_name(value):
    if not value or not value[0].isalnum() or any(
            not (character.isalnum() or character in '._-') for character in value):
        raise argparse.ArgumentTypeError('Label must start with a letter/digit and contain only letters, digits, . _ -')
    return value


def windows_physical_core_masks():
    """Read single-group Windows core masks without WMI or external processes."""
    import ctypes
    from ctypes import wintypes

    class ProcessorInformation(ctypes.Structure):
        # The native union is 16 bytes with ULONGLONG alignment. Only the mask
        # and RelationProcessorCore (0) discriminator are needed here.
        _fields_ = [('mask', ctypes.c_size_t), ('relationship', ctypes.c_int),
                    ('reserved', ctypes.c_ulonglong * 2)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    group_count = kernel.GetActiveProcessorGroupCount
    group_count.argtypes = []
    group_count.restype = wintypes.WORD
    if group_count() != 1:
        # This API returns only the calling thread's group; do not confuse its
        # bit indices with a child's allowed CPUs on a multi-group machine.
        raise OSError('Physical-core selection requires a single processor group')
    query = kernel.GetLogicalProcessorInformation
    query.argtypes = [ctypes.POINTER(ProcessorInformation), ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.BOOL
    size = wintypes.DWORD()
    record_size = ctypes.sizeof(ProcessorInformation)
    buffer = None
    # Retry a bounded number of times if topology changes between sizing and
    # reading; the modest allocation ceiling also rejects malformed lengths.
    for _ in range(3):
        if query(buffer, ctypes.byref(size)):
            if (buffer is None or not size.value or size.value > ctypes.sizeof(buffer)
                    or size.value % record_size):
                raise OSError('Invalid Windows processor topology length')
            return [int(record.mask) for record in buffer[:size.value // record_size]
                    if record.relationship == 0]
        error = ctypes.get_last_error()
        if error != 122:  # ERROR_INSUFFICIENT_BUFFER
            raise ctypes.WinError(error)
        if not 0 < size.value <= 1024 * 1024 or size.value % record_size:
            raise OSError('Invalid Windows processor topology allocation size')
        buffer = (ProcessorInformation * (size.value // record_size))()
    raise OSError('Windows processor topology changed repeatedly during query')


def select_cpu_affinity(allowed, cpu_count):
    """Return a bounded allowed subset and reportable selection evidence."""
    allowed = list(allowed)
    if not allowed or cpu_count < 1:
        # Passing [] to psutil may reset affinity to all CPUs; never do that.
        raise ValueError('CPU affinity requires allowed CPUs and a positive count')
    count = min(cpu_count, len(allowed))
    fallback = allowed[-count:]
    detail = {'method': 'last-allowed', 'allowed': allowed,
              'requested_count': cpu_count, 'selected': fallback}
    if os.name != 'nt':
        return fallback, detail
    try:
        masks = windows_physical_core_masks()
        detail['physical_core_masks'] = [hex(mask) for mask in masks]
        cpu_to_core = {}
        covered = 0
        for mask in masks:
            if mask <= 0 or mask & covered:
                raise ValueError('Physical-core masks are empty or overlapping')
            covered |= mask
            for cpu in allowed:
                if cpu < 0:
                    raise ValueError('Invalid allowed logical CPU index')
                if mask & (1 << cpu):
                    cpu_to_core[cpu] = mask
        if len(cpu_to_core) != len(allowed):
            raise ValueError('Physical-core masks do not cover every allowed CPU')
        selected, used_cores = [], set()
        later_first = sorted(allowed, reverse=True)
        for cpu in later_first:
            core = cpu_to_core[cpu]
            if core not in used_cores:
                selected.append(cpu)
                used_cores.add(core)
                if len(selected) == count:
                    break
        if len(selected) < count:
            selected.extend(cpu for cpu in later_first if cpu not in selected)
        selected = sorted(selected[:count])
        detail.update(method='distinct-physical-cores-first', selected=selected,
                      selected_physical_core_count=len({cpu_to_core[cpu] for cpu in selected}))
        return selected, detail
    except Exception as error:
        detail['fallback_reason'] = f'{type(error).__name__}: {error}'
        return fallback, detail


def windows_commit_available():
    """Return current system commit headroom, not free physical RAM."""
    import ctypes
    from ctypes import wintypes

    class PerformanceInformation(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD)] + [
            (field, ctypes.c_size_t) for field in (
                'CommitTotal', 'CommitLimit', 'CommitPeak', 'PhysicalTotal',
                'PhysicalAvailable', 'SystemCache', 'KernelTotal', 'KernelPaged',
                'KernelNonpaged', 'PageSize')] + [
            ('HandleCount', wintypes.DWORD), ('ProcessCount', wintypes.DWORD),
            ('ThreadCount', wintypes.DWORD)]

    query = ctypes.WinDLL('psapi', use_last_error=True).GetPerformanceInfo
    query.argtypes = [ctypes.POINTER(PerformanceInformation), wintypes.DWORD]
    query.restype = wintypes.BOOL
    info = PerformanceInformation()
    info.cb = ctypes.sizeof(info)
    if not query(ctypes.byref(info), info.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    if not info.PageSize or not info.CommitLimit:
        raise OSError('Invalid Windows system commit information')
    return max(0, int(info.CommitLimit) - int(info.CommitTotal)) * int(info.PageSize)


def windows_cap_working_set(process, maximum_bytes):
    """Set and verify a hard resident-memory cap through an identity-held handle.

    The caller must first verify the tracked process's name and private endpoint.
    Holding the handle prevents a PID race from redirecting a quota change.
    """
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    open_process = kernel.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    close_handle = kernel.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    get_times = kernel.GetProcessTimes
    get_times.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    get_times.restype = wintypes.BOOL
    set_limit = kernel.SetProcessWorkingSetSizeEx
    set_limit.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
    set_limit.restype = wintypes.BOOL
    get_limit = kernel.GetProcessWorkingSetSizeEx
    get_limit.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t),
                          ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(wintypes.DWORD)]
    get_limit.restype = wintypes.BOOL
    expected_creation = process.create_time()
    handle = open_process(0x0100 | 0x1000, False, process.pid)  # SET_QUOTA | QUERY_LIMITED_INFORMATION
    if not handle:
        if not process.is_running():
            raise psutil.NoSuchProcess(process.pid)
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not get_times(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                         ctypes.byref(kernel_time), ctypes.byref(user_time)):
            raise ctypes.WinError(ctypes.get_last_error())
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        actual_creation = ticks / 10000000.0 - 11644473600.0
        if abs(actual_creation - expected_creation) > 0.00001:
            raise psutil.NoSuchProcess(process.pid, msg='PID identity changed before working-set cap')
        # HARDWS_MAX_ENABLE | HARDWS_MIN_DISABLE: cap residency without pinning
        # a minimum allocation away from other applications.
        flags = 0x00000004 | 0x00000002
        minimum_bytes = min(16 * 1024 ** 2, maximum_bytes)
        if not set_limit(handle, minimum_bytes, maximum_bytes, flags):
            raise ctypes.WinError(ctypes.get_last_error())
        actual_minimum, actual_maximum, actual_flags = ctypes.c_size_t(), ctypes.c_size_t(), wintypes.DWORD()
        if not get_limit(handle, ctypes.byref(actual_minimum), ctypes.byref(actual_maximum),
                         ctypes.byref(actual_flags)):
            raise ctypes.WinError(ctypes.get_last_error())
        if (not actual_flags.value & 0x4 or actual_flags.value & (0x8 | 0x1)
                or not 0 < actual_maximum.value <= maximum_bytes):
            raise OSError('Windows did not confirm the requested hard working-set maximum')
        return {'pid': process.pid, 'created': expected_creation,
                'requested_maximum_bytes': maximum_bytes,
                'readback_minimum_bytes': actual_minimum.value,
                'readback_maximum_bytes': actual_maximum.value,
                'readback_flags': actual_flags.value}
    except OSError:
        if not process.is_running():
            raise psutil.NoSuchProcess(process.pid)
        raise
    finally:
        close_handle(handle)


def cap_owned_link_processes(tree, maximum_bytes, endpoint, records):
    """Cap newly observed private linker/PDB processes before sampling their RSS."""
    applied = {(record['pid'], record['created']) for record in records}
    for process in tree.collect():
        try:
            identity = (process.pid, process.create_time())
            if identity in applied or not process.is_running():
                continue
            name = process.name().lower()
            if name not in ('link.exe', 'mspdbsrv.exe'):
                continue
            if process.environ().get('_MSPDBSRV_ENDPOINT_') != endpoint:
                raise GuardLimit(f'Refusing working-set change for PID {process.pid}: private endpoint mismatch')
            record = windows_cap_working_set(process, maximum_bytes)
            record.update(name=name, private_endpoint_verified=True)
            records.append(record)
            applied.add(identity)
        except psutil.NoSuchProcess:
            pass  # An exited process no longer needs its working set bounded.


class ProcessTree:
    """Track descendants while they still have a live parent.

    psutil Process identity checks protect against killing a reused PID. Retain
    previously observed descendants so cleanup still works if the root exits.
    """

    def __init__(self, pid):
        self.members = {}
        try:
            root = psutil.Process(pid)
            self.members[(root.pid, root.create_time())] = root
            self.root = root
        except psutil.NoSuchProcess:
            self.root = None

    def collect(self):
        for process in list(self.members.values()):
            try:
                if process.is_running():
                    for child in process.children(recursive=True):
                        self.members[(child.pid, child.create_time())] = child
            except psutil.NoSuchProcess:
                pass
        alive = []
        for key, process in list(self.members.items()):
            try:
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    alive.append(process)
                else:
                    self.members.pop(key, None)
            except psutil.NoSuchProcess:
                self.members.pop(key, None)
        return alive

    def sample_rss(self):
        rss = 0
        for process in self.collect():
            try:
                rss += process.memory_info().rss
            except psutil.NoSuchProcess:
                pass
        return rss

    def stop(self):
        errors = []
        self.cleanup_events = []

        def identity(key):
            # Use a fresh Process, not its cached create_time: Windows may deny
            # access to a just-exited process before psutil reports NoSuchProcess.
            # A PID that now belongs to another process is never ours to modify.
            try:
                current = psutil.Process(key[0])
                if current.create_time() != key[1]:
                    return 'reused', None
                if not current.is_running():
                    return 'gone', None
                return 'owned', None
            except psutil.NoSuchProcess:
                return 'gone', None
            except psutil.Error as error:
                # pid_exists is a separate existence check, not evidence of
                # ownership. Only a negative result resolves an inaccessible PID.
                try:
                    if not psutil.pid_exists(key[0]):
                        return 'gone', None
                except psutil.Error:
                    pass
                return 'unknown', error

        def record(phase, key, error, state, query_error=None):
            event = {'phase': phase, 'pid': None if key is None else key[0],
                     'created': None if key is None else key[1], 'identity': state,
                     'error_type': type(error).__name__ if error is not None else None,
                     'error': str(error) if error is not None else None}
            if query_error is not None:
                event['identity_error_type'] = type(query_error).__name__
                event['identity_error'] = str(query_error)
            self.cleanup_events.append(event)
            if state not in ('gone', 'reused'):
                errors.append(f"{phase}: {event['error_type']} pid={event['pid']} "
                              f"created={event['created']} identity={state}: {event['error']}"
                              + (f"; identity query {type(query_error).__name__}: {query_error}" if query_error else ''))
            elif key is not None:
                self.members.pop(key, None)

        def owned(key, phase):
            state, error = identity(key)
            if state == 'owned':
                return True
            record(phase + '-identity', key, error, state)
            return False

        def handle_error(phase, key, error):
            state, query_error = identity(key) if key is not None else ('unknown', None)
            record(phase, key, error, state, query_error)

        # Suspend observed parents before collecting/killing descendants to
        # reduce the chance of starting another compiler during cancellation.
        for key, process in list(self.members.items()):
            if not owned(key, 'suspend'):
                continue
            try:
                process.suspend()
            except psutil.NoSuchProcess:
                record('suspend', key, None, 'gone')
            except psutil.Error as error:
                handle_error('suspend', key, error)
        try:
            members = self.collect()
        except psutil.Error as error:
            key = next((key for key in self.members if key[0] == getattr(error, 'pid', None)), None)
            handle_error('collect', key, error)
            members = list(self.members.values())
        waiting = []
        for process in reversed(members):
            key = next((key for key, member in self.members.items() if member is process), None)
            if key is None or not owned(key, 'kill'):
                continue
            try:
                process.kill()
                waiting.append((key, process))
            except psutil.NoSuchProcess:
                record('kill', key, None, 'gone')
            except psutil.Error as error:
                handle_error('kill', key, error)
        try:
            _, alive = psutil.wait_procs([process for _, process in waiting], timeout=5)
        except psutil.Error as error:
            key = next((key for key, _ in waiting if key[0] == getattr(error, 'pid', None)), None)
            handle_error('wait', key, error)
            alive = [process for _, process in waiting]
        if alive:
            for process in alive:
                key = next(key for key, member in waiting if member is process)
                state, error = identity(key)
                record('wait', key, error or RuntimeError('Process still present after cleanup'), state)
        return errors


def run_guarded(command, log_directory, label, memory_gib=6, minimum_free_gib=6, cpu_count=2,
                link_working_set_mib=None, minimum_commit_gib=None,
                launch_headroom_gib=0, timeout_seconds=None):
    # Programmatic callers receive the same fail-closed validation as the CLI.
    label_name(label)
    for number in (memory_gib, minimum_free_gib):
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not 0 < number < float('inf'):
            raise ValueError('Memory limits must be finite positive numbers')
    if type(cpu_count) is not int or cpu_count < 1:
        raise ValueError('CPU count must be a positive integer')
    if not isinstance(command, (list, tuple)) or not command or any(not isinstance(arg, str) for arg in command):
        raise ValueError('Command must be a nonempty array of string arguments')
    if not 0 <= launch_headroom_gib < float('inf'):
        raise ValueError('Launch headroom must be finite and nonnegative')
    if timeout_seconds is not None and not 0 < timeout_seconds < float('inf'):
        raise ValueError('Timeout must be finite and positive')
    if link_working_set_mib is not None:
        if type(link_working_set_mib) is not int or link_working_set_mib < 1:
            raise ValueError('Link working-set MiB must be a positive integer')
    if minimum_commit_gib is not None:
        if not 0 < minimum_commit_gib < float('inf'):
            raise ValueError('Minimum commit GiB must be finite and positive')
    if os.name != 'nt' and (link_working_set_mib is not None or minimum_commit_gib is not None):
        raise ValueError('Link working-set and commit limits are Windows-only')
    log_directory = Path(log_directory).resolve()
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / (label + '.log')
    report_path = log_directory / (label + '.json')
    if log_path.exists() or report_path.exists():
        raise ValueError(f'Log/report already exists for {label}; choose a fresh label')

    started = time.monotonic()
    report = {
        'status': 'starting', 'label': label, 'command': command,
        'cwd': str(Path.cwd()), 'started_utc': datetime.now(timezone.utc).isoformat(),
        'log': str(log_path),
        'limits': {'sampled_tree_rss_bytes': int(memory_gib * GIB),
                   'minimum_available_system_bytes': int(minimum_free_gib * GIB),
                   'requested_cpu_count': cpu_count, 'sample_interval_seconds': 0.2,
                   'progress_interval_seconds': 20, 'timeout_seconds': timeout_seconds,
                   'launch_headroom_bytes': int(launch_headroom_gib * GIB)},
        'build_parallel_level': os.environ.get('CMAKE_BUILD_PARALLEL_LEVEL'),
        'sampled_peak_tree_rss_bytes': 0,
        'sampled_minimum_system_available_bytes': None,
        'measurement_note': 'Sampled RSS sum can count shared pages more than once; this is not a hard OS memory quota.',
    }
    process = None
    tree = None
    result = 1
    if link_working_set_mib is not None:
        report['limits']['link_process_working_set_bytes'] = link_working_set_mib * 1024 ** 2
        report['working_set_caps'] = []
        report['working_set_note'] = ('Per-process resident-memory caps may increase paging; '
                                      'they do not cap private commit or guarantee system responsiveness.')
    if minimum_commit_gib is not None:
        report['limits']['minimum_available_system_commit_bytes'] = int(minimum_commit_gib * GIB)
        report['sampled_minimum_available_system_commit_bytes'] = None
    # Exclusive handles protect earlier logs/reports, including concurrent runs.
    with report_path.open('x', encoding='utf-8') as report_stream:
        def save_report():
            report['seconds'] = round(time.monotonic() - started, 3)
            report_stream.seek(0)
            json.dump(report, report_stream, indent=2)
            report_stream.write('\n')
            report_stream.truncate()
            report_stream.flush()

        save_report()
        try:
            with log_path.open('xb') as log_stream:
                available = psutil.virtual_memory().available
                report['sampled_minimum_system_available_bytes'] = available
                if available < report['limits']['minimum_available_system_bytes'] + report['limits']['launch_headroom_bytes']:
                    raise GuardLimit(f'Only {available / GIB:.2f} GiB system RAM available before launch')
                if minimum_commit_gib is not None:
                    commit_available = windows_commit_available()
                    report['sampled_minimum_available_system_commit_bytes'] = commit_available
                    if commit_available < report['limits']['minimum_available_system_commit_bytes'] + report['limits']['launch_headroom_bytes']:
                        raise GuardLimit(f'Only {commit_available / GIB:.2f} GiB system commit headroom before launch')
                options = {}
                if os.name == 'nt':
                    options['creationflags'] = subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
                    # MSVC can otherwise share a surviving mspdbsrv.exe with
                    # unrelated builds. Keep this endpoint private to this run,
                    # whose observed process tree may be killed during cleanup.
                    child_environment = os.environ.copy()
                    endpoint = 'prusaslicer_' + uuid.uuid4().hex
                    child_environment['_MSPDBSRV_ENDPOINT_'] = endpoint
                    options['env'] = child_environment
                    report['mspdbsrv_endpoint'] = endpoint
                print(f'[{label}] Starting with {cpu_count} CPUs, {memory_gib:g} GiB sampled RSS ceiling; log: {log_path}', flush=True)
                process = subprocess.Popen(command, stdout=log_stream, stderr=subprocess.STDOUT, **options)
                report['pid'] = process.pid
                report['runner_pid'] = os.getpid()
                save_report()
                print(f'[{label}] Child PID {process.pid}; runner PID {os.getpid()}', flush=True)
                tree = ProcessTree(process.pid)
                try:
                    if tree.root is not None:
                        if os.name != 'nt':
                            tree.root.nice(10)
                        allowed = tree.root.cpu_affinity()
                        chosen, selection = select_cpu_affinity(allowed, cpu_count)
                        report['cpu_affinity_selection'] = selection
                        tree.root.cpu_affinity(chosen)
                        report['cpu_affinity'] = chosen
                except psutil.NoSuchProcess:
                    pass  # A very short command may already have exited.
                report['status'] = 'running'
                save_report()
                next_progress = time.monotonic() + 20
                while process.poll() is None:
                    if timeout_seconds is not None and time.monotonic() - started >= timeout_seconds:
                        raise GuardLimit(f'Guarded command exceeded {timeout_seconds:g}s timeout')
                    if link_working_set_mib is not None:
                        previous_caps = len(report['working_set_caps'])
                        cap_owned_link_processes(tree, report['limits']['link_process_working_set_bytes'],
                                                 report['mspdbsrv_endpoint'], report['working_set_caps'])
                        if len(report['working_set_caps']) != previous_caps:
                            save_report()
                    rss = tree.sample_rss()
                    available = psutil.virtual_memory().available
                    report['sampled_peak_tree_rss_bytes'] = max(report['sampled_peak_tree_rss_bytes'], rss)
                    report['sampled_minimum_system_available_bytes'] = min(
                        report['sampled_minimum_system_available_bytes'], available)
                    if rss > report['limits']['sampled_tree_rss_bytes']:
                        raise GuardLimit(f'Process tree reached {rss / GIB:.2f} GiB RSS, above {memory_gib:g} GiB')
                    if available < report['limits']['minimum_available_system_bytes']:
                        raise GuardLimit(f'Available system RAM fell to {available / GIB:.2f} GiB, below {minimum_free_gib:g} GiB')
                    if minimum_commit_gib is not None:
                        commit_available = windows_commit_available()
                        report['sampled_minimum_available_system_commit_bytes'] = min(
                            report['sampled_minimum_available_system_commit_bytes'], commit_available)
                        if commit_available < report['limits']['minimum_available_system_commit_bytes']:
                            raise GuardLimit(f'System commit headroom fell to {commit_available / GIB:.2f} GiB, '
                                             f'below {minimum_commit_gib:g} GiB')
                    if time.monotonic() >= next_progress:
                        save_report()
                        print(f'[{label}] {report["seconds"]:.0f}s; RSS {rss / GIB:.2f} GiB, '
                              f'peak {report["sampled_peak_tree_rss_bytes"] / GIB:.2f} GiB; '
                              f'free {available / GIB:.2f} GiB', flush=True)
                        next_progress = time.monotonic() + 20
                    time.sleep(0.2)
                result = process.wait()
                report['status'] = 'passed' if result == 0 else 'command-failed'
                if result != 0:
                    report['error'] = f'Command exited with code {result}; see {log_path}'
        except KeyboardInterrupt:
            report['status'] = 'interrupted'
            report['error'] = 'Interrupted by caller'
            result = 130
        except GuardLimit as error:
            report['status'] = 'guard-stopped'
            report['error'] = str(error)
            result = 2
        except Exception as error:
            report['status'] = 'runner-failed'
            report['error'] = f'{type(error).__name__}: {error}'
            result = 1
        finally:
            if tree is not None:
                try:
                    cleanup_errors = tree.stop()
                except Exception as error:
                    cleanup_errors = [f'{type(error).__name__}: {error}']
                if cleanup_errors:
                    report['cleanup_errors'] = cleanup_errors
                    if result == 0:
                        report['status'] = 'cleanup-failed'
                        result = 1
                if getattr(tree, 'cleanup_events', None):
                    report['cleanup_events'] = tree.cleanup_events
            if process is not None:
                try:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                    report['command_returncode'] = process.returncode
                except Exception as error:
                    report.setdefault('cleanup_errors', []).append(f'{type(error).__name__}: {error}')
                    if result == 0:
                        report['status'] = 'cleanup-failed'
                        result = 1
            report['returncode'] = result
            report['finished_utc'] = datetime.now(timezone.utc).isoformat()
            save_report()
    print(f'[{label}] {report["status"]} after {report["seconds"]:.1f}s; '
          f'peak {report["sampled_peak_tree_rss_bytes"] / GIB:.2f} GiB; report: {report_path}', flush=True)
    if 'error' in report:
        print(f'[{label}] {report["error"]}', file=sys.stderr, flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log-dir', type=Path, required=True)
    parser.add_argument('--label', type=label_name, required=True)
    parser.add_argument('--memory-gib', type=positive_float, default=6)
    parser.add_argument('--minimum-free-gib', type=positive_float, default=6)
    parser.add_argument('--cpu-count', type=positive_int, default=2)
    parser.add_argument('--link-working-set-mib', type=positive_int,
                        help='Windows-only hard resident-memory cap per private linker/PDB process (opt-in)')
    parser.add_argument('--minimum-commit-gib', type=positive_float,
                        help='Windows-only minimum system commit headroom (opt-in)')
    parser.add_argument('--launch-headroom-gib', type=positive_float, default=0,
                        help='Additional free RAM/commit required before launch (runtime reserves remain unchanged)')
    parser.add_argument('--timeout-seconds', type=positive_float,
                        help='Optional per-command deadline; full builds have no default timeout')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('Supply the executable and arguments after --')
    try:
        return run_guarded(command, args.log_dir, args.label,
                           args.memory_gib, args.minimum_free_gib, args.cpu_count,
                           args.link_working_set_mib, args.minimum_commit_gib,
                           args.launch_headroom_gib, args.timeout_seconds)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Unable to create guarded run: {error}\n')


if __name__ == '__main__':
    sys.exit(main())
