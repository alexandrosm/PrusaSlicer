"""Bounded, isolated PCH mechanism benchmark (not a PrusaSlicer build-speed claim).

Run in a native compiler developer environment. Two small translation units
compare a full PCH against a stable-only PCH: clean build, warm no-op, editing a
first-party header, then a second warm no-op. No application/dependency build
directory or source header is modified. Compiler caching is OFF to isolate PCH.
Every child is below-normal priority, one CPU, <=2 GiB sampled tree RSS, with
6 GiB free RAM and (on Windows) 6 GiB commit reserves. An overall child-time
budget defaults to 120 seconds; each launch receives only the remaining time.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

from run_guarded import run_guarded


REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURE = REPOSITORY / 'tests/build_tools/pch_fixture'


def file_identity(path):
    return {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'bytes': path.stat().st_size, 'mtime_ns': path.stat().st_mtime_ns}


def snapshot(build):
    """Capture Ninja's executed-edge journal and the actual PCH/object outputs."""
    log = build / '.ninja_log'
    lines = log.read_text(encoding='utf-8').splitlines() if log.exists() else []
    artifacts = {}
    target = build / 'CMakeFiles/pch_probe.dir'
    if target.exists():
        for path in sorted(target.iterdir()):
            if path.is_file() and path.suffix in ('.obj', '.o', '.pch', '.gch'):
                artifacts[path.name] = file_identity(path)
    return {'journal': lines, 'artifacts': artifacts}


def prove_step(mode, scenario, before, after):
    """Fail closed: timings or an exit-zero build alone prove no acceleration."""
    old, new = before['journal'], after['journal']
    # These fresh tiny journals never need Ninja's thousand-entry compaction.
    if new[:len(old)] != old:
        raise RuntimeError('Ninja journal was replaced; cannot prove executed edges')
    edges = []
    for line in new[len(old):]:
        if line.startswith('#'):
            continue
        fields = line.split('\t')
        if len(fields) != 5:
            raise RuntimeError('Malformed Ninja journal entry')
        edges.append(fields[3].replace('\\', '/'))
    pch_edges = [edge for edge in edges if '/cmake_pch.' in edge]
    tu_edges = [edge for edge in edges if edge.endswith(('/a.cpp.obj', '/b.cpp.obj', '/a.cpp.o', '/b.cpp.o'))]
    if len(edges) != len(pch_edges) + len(tu_edges):
        raise RuntimeError(f'Unexpected fixture build edges: {edges}')
    pch_files = {name: value for name, value in after['artifacts'].items() if name.endswith(('.pch', '.gch'))}
    objects = {name: value for name, value in after['artifacts'].items() if name.startswith(('a.cpp.', 'b.cpp.'))}
    if len(pch_files) != 1 or len(objects) != 2:
        raise RuntimeError('Expected one real PCH and two compiled translation-unit objects')
    old_pch = {name: before['artifacts'].get(name) for name in pch_files}
    if scenario.startswith('warm'):
        if edges or before['artifacts'] != after['artifacts']:
            raise RuntimeError('Warm no-op unexpectedly rebuilt or modified an artifact')
        assertion = 'no compile edges and unchanged PCH/objects'
    else:
        if len(tu_edges) != 2 or len(set(tu_edges)) != 2:
            raise RuntimeError('Both translation units must actually compile')
        expect_pch = scenario == 'clean' or mode == 'full'
        if bool(pch_edges) != expect_pch:
            raise RuntimeError(f'{mode}/{scenario}: incorrect PCH rebuild behavior')
        if scenario == 'header-edit':
            if not expect_pch and old_pch != pch_files:
                raise RuntimeError('Stable PCH changed after editing a first-party header')
            if expect_pch and old_pch == pch_files:
                raise RuntimeError('Full PCH compile edge did not replace its artifact')
            for name, identity in objects.items():
                previous = before['artifacts'].get(name, {})
                if identity['sha256'] == previous.get('sha256'):
                    raise RuntimeError('Edited constant did not change both compiled objects')
        assertion = 'PCH rebuilt and both TUs compiled' if expect_pch else 'unchanged stable PCH reused; both TUs recompiled'
    return {'passed': True, 'assertion': assertion, 'executed_edges': edges,
            'artifacts_before': before['artifacts'], 'artifacts_after': after['artifacts']}


def benchmark(output, cmake='cmake', ninja='ninja', max_seconds=120,
              minimum_free_gib=6, minimum_commit_gib=6):
    if not isinstance(max_seconds, (int, float)) or not 0 < max_seconds <= 300:
        raise ValueError('Benchmark budget must be positive and at most 300 seconds')
    for reserve in (minimum_free_gib, minimum_commit_gib):
        if not isinstance(reserve, (int, float)) or not 0 < reserve <= 64:
            raise ValueError('Explicit RAM/commit reserve must be positive and at most 64 GiB')
    cmake, ninja = shutil.which(cmake), shutil.which(ninja)
    if not cmake or not ninja:
        raise ValueError('CMake and Ninja must be available in the native compiler environment')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    report = {'status': 'running', 'scope': 'synthetic two-TU PCH rebuild mechanism only',
              'not_measured': ['whole PrusaSlicer speedup', 'release payload', 'compiler cache hit rate'],
              'budget_seconds': max_seconds, 'compiler_cache': 'off', 'steps': [],
              'minimum_free_gib': minimum_free_gib, 'minimum_commit_gib': minimum_commit_gib,
              'fixture_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in sorted(FIXTURE.iterdir()) if path.is_file()}}
    previous = os.environ.get('CMAKE_BUILD_PARALLEL_LEVEL')
    try:
        os.environ['CMAKE_BUILD_PARALLEL_LEVEL'] = '1'
        for mode in ('full', 'stable'):
            source = output / mode / 'source'
            shutil.copytree(FIXTURE, source)
            build = output / mode / 'build'
            commands = [('configure', [cmake, '-S', str(source), '-B', str(build), '-G', 'Ninja',
                         '-DCMAKE_BUILD_TYPE=Release', '-DSLIC3R_COMPILER_CACHE=off',
                         '-DPRUSA_SOURCE=' + str(REPOSITORY), '-DCMAKE_MAKE_PROGRAM=' + ninja,
                         '-DFULL_PCH=' + ('ON' if mode == 'full' else 'OFF')])]
            build_command = [cmake, '--build', str(build), '--target', 'pch_probe', '--parallel', '1', '--verbose']
            commands += [(scenario, build_command) for scenario in ('clean', 'warm', 'header-edit', 'warm-after-edit')]
            for scenario, command in commands:
                remaining = max_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise RuntimeError('PCH benchmark total time budget exhausted')
                if scenario == 'header-edit':
                    # Edit only our private fixture copy; content and mtime both
                    # change, so this is a real dependency invalidation.
                    header = source / 'project.hpp'
                    previous_header = file_identity(header)
                    header.write_text('#pragma once\ninline constexpr int edited_value = 2;\n', encoding='utf-8')
                    if file_identity(header)['sha256'] == previous_header['sha256']:
                        raise RuntimeError('Private first-party header edit did not change its content')
                before = snapshot(build) if scenario != 'configure' else None
                label = mode + '-' + scenario
                options = {'memory_gib': 2, 'minimum_free_gib': minimum_free_gib, 'cpu_count': 1,
                           'launch_headroom_gib': 1, 'timeout_seconds': remaining}
                if os.name == 'nt':
                    options['minimum_commit_gib'] = minimum_commit_gib
                code = run_guarded(command, output / 'logs', label, **options)
                telemetry = output / 'logs' / (label + '.json')
                step = json.loads(telemetry.read_text(encoding='utf-8'))
                report['steps'].append({'mode': mode, 'scenario': scenario, 'telemetry': str(telemetry),
                                        'seconds': step['seconds'], 'status': step['status'],
                                        'sampled_peak_tree_rss_bytes': step['sampled_peak_tree_rss_bytes']})
                if code:
                    raise RuntimeError(f'{label} failed; see {telemetry}')
                if scenario != 'configure':
                    report['steps'][-1]['mechanism'] = prove_step(mode, scenario, before, snapshot(build))
                if scenario == 'header-edit':
                    report['steps'][-1]['header_before'] = previous_header
                    report['steps'][-1]['header_after'] = file_identity(header)
        report['status'] = 'passed'
    except Exception as error:
        report['status'] = 'failed'
        report['error'] = str(error)
        raise
    finally:
        if previous is None:
            os.environ.pop('CMAKE_BUILD_PARALLEL_LEVEL', None)
        else:
            os.environ['CMAKE_BUILD_PARALLEL_LEVEL'] = previous
        report['seconds'] = round(time.monotonic() - started, 3)
        with (output / 'benchmark.json').open('x', encoding='utf-8') as stream:
            json.dump(report, stream, indent=2)
            stream.write('\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Fresh directory; never overwrites an earlier run')
    parser.add_argument('--cmake', default='cmake')
    parser.add_argument('--ninja', default='ninja')
    parser.add_argument('--max-seconds', type=float, default=120)
    parser.add_argument('--minimum-free-gib', type=float, default=6,
                        help='Explicit hosted-CI override; desktop default remains 6 GiB')
    parser.add_argument('--minimum-commit-gib', type=float, default=6,
                        help='Windows commit reserve; desktop default remains 6 GiB')
    args = parser.parse_args()
    benchmark(args.output, args.cmake, args.ninja, args.max_seconds,
              args.minimum_free_gib, args.minimum_commit_gib)


if __name__ == '__main__':
    main()
