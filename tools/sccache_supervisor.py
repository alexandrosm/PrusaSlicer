"""Run a build with a private foreground sccache server UNDER run_guarded.py.

The storage directory is reusable, but the server/config/port are private to
this invocation. Never attach a guarded build to an ambient shared server:
server-side compilers would otherwise escape its process tree and CPU affinity.
Official sccache supports SCCACHE_START_SERVER=1 + SCCACHE_NO_DAEMON=1:
https://github.com/mozilla/sccache#debugging
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import psutil
from run_guarded import ProcessTree


def private_environment(parent, directory, cache, port):
    # Do not inherit another server, distributed-compilation settings, remote
    # credentials or a user configuration capable of offloading compilation.
    # Only this supervisor's children see these changes. The local cache remains
    # reusable and the random port is not part of compiler/dependency cache keys.
    environment = {key: value for key, value in parent.items() if not key.upper().startswith('SCCACHE_')}
    environment.update(SCCACHE_SERVER_PORT=str(port), SCCACHE_DIR=str(cache),
                       SCCACHE_CACHE_SIZE='2G', SCCACHE_IDLE_TIMEOUT='0',
                       SCCACHE_CONF=str(directory / 'private.toml'),
                       SCCACHE_CACHED_CONF=str(directory / 'private-state.toml'))
    return environment


def owns_listener(process, port):
    if not process.is_running():
        return False
    return any(connection.status == psutil.CONN_LISTEN and connection.laddr.port == port
               and connection.laddr.ip == '127.0.0.1'
               for connection in process.net_connections(kind='tcp'))


def supervise(executable, command, directory, cache):
    if not command or any(not isinstance(arg, str) for arg in command):
        raise ValueError('Supply a nonempty build argument array')
    executable = str(Path(executable).resolve(strict=True))
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    cache = Path(cache).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    (directory / 'private.toml').write_text('', encoding='utf-8')
    # Port selection alone is not proof: another process could win the bind
    # race. Verify the listener belongs to our exact foreground child below.
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    environment = private_environment(os.environ, directory, cache, port)
    server_environment = {**environment, 'SCCACHE_START_SERVER': '1', 'SCCACHE_NO_DAEMON': '1'}
    options = {'creationflags': subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    report = {'status': 'starting', 'server_port': port, 'supervisor_pid': os.getpid(),
              'cache_directory': str(cache), 'command': command, 'executable': executable}
    started = time.monotonic()
    server = child = server_tree = child_tree = None
    result = 1
    def save():
        (directory / 'server.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    try:
        with (directory / 'server.log').open('xb') as log:
            server = subprocess.Popen([executable], env=server_environment, stdout=log, stderr=subprocess.STDOUT, **options)
            server_tree = ProcessTree(server.pid)
            process = server_tree.root
            if process is None:
                raise RuntimeError('Foreground sccache exited before identity verification')
            process.cpu_affinity(psutil.Process().cpu_affinity())
            if os.name != 'nt':
                process.nice(10)
            report.update(server_pid=process.pid, server_created=process.create_time())
            save()
            while not owns_listener(process, port):
                if server.poll() is not None:
                    raise RuntimeError('Foreground sccache exited before binding its private port')
                if time.monotonic() - started > 15:
                    raise RuntimeError('Cannot verify private foreground sccache listener within 15s')
                time.sleep(0.05)
            report['listener_verified'] = True
            report['listener_verified_seconds'] = round(time.monotonic() - started, 3)
            report['cpu_affinity'] = process.cpu_affinity()
            report['status'] = 'running'
            save()
            print(f'[sccache] Private foreground server PID {server.pid}, port {port}; report: {directory / "server.json"}', flush=True)
            child = subprocess.Popen(command, env=environment, stdout=sys.stdout, stderr=subprocess.STDOUT, **options)
            child_tree = ProcessTree(child.pid)
            while child.poll() is None:
                server_tree.collect()
                child_tree.collect()
                if server.poll() is not None:
                    raise RuntimeError('Private sccache server exited while the build was running')
                time.sleep(0.1)
            result = child.wait()
            report['build_finished_seconds'] = round(time.monotonic() - started, 3)
            # Read-only statistics from the verified private endpoint. No global
            # --zero-stats or --stop-server operation is ever issued.
            if server.poll() is None and owns_listener(process, port):
                stats = subprocess.run([executable, '--show-stats', '--stats-format=json'], env=environment,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, **options)
                if stats.returncode == 0:
                    report['stats'] = json.loads(stats.stdout)
                else:
                    report['stats_error'] = 'Private server statistics command failed'
            report['status'] = 'passed' if result == 0 else 'command-failed'
    except BaseException as error:
        report['status'] = 'failed'
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        errors = []
        for tree in (child_tree, server_tree):
            if tree is not None:
                try:
                    errors.extend(tree.stop())
                except Exception as error:
                    errors.append(f'{type(error).__name__}: {error}')
        for process in (child, server):
            if process is not None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                except Exception as error:
                    errors.append(f'{type(error).__name__}: {error}')
        if errors:
            report['cleanup_errors'] = errors
            report['status'] = 'cleanup-failed'
            result = 1
        report['seconds'] = round(time.monotonic() - started, 3)
        save()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sccache', required=True)
    parser.add_argument('--session-dir', required=True, type=Path)
    parser.add_argument('--cache-dir', required=True, type=Path)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    return supervise(args.sccache, command, args.session_dir, args.cache_dir)


if __name__ == '__main__':
    raise SystemExit(main())
