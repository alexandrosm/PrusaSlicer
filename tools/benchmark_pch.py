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


def benchmark(output, cmake='cmake', ninja='ninja', max_seconds=120):
    if not isinstance(max_seconds, (int, float)) or not 0 < max_seconds <= 300:
        raise ValueError('Benchmark budget must be positive and at most 300 seconds')
    cmake, ninja = shutil.which(cmake), shutil.which(ninja)
    if not cmake or not ninja:
        raise ValueError('CMake and Ninja must be available in the native compiler environment')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    report = {'status': 'running', 'scope': 'synthetic two-TU PCH rebuild mechanism only',
              'not_measured': ['whole PrusaSlicer speedup', 'release payload', 'compiler cache hit rate'],
              'budget_seconds': max_seconds, 'compiler_cache': 'off', 'steps': [],
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
                    header.write_text('#pragma once\ninline constexpr int edited_value = 2;\n', encoding='utf-8')
                label = mode + '-' + scenario
                options = {'memory_gib': 2, 'minimum_free_gib': 6, 'cpu_count': 1,
                           'launch_headroom_gib': 1, 'timeout_seconds': remaining}
                if os.name == 'nt':
                    options['minimum_commit_gib'] = 6
                code = run_guarded(command, output / 'logs', label, **options)
                telemetry = output / 'logs' / (label + '.json')
                step = json.loads(telemetry.read_text(encoding='utf-8'))
                report['steps'].append({'mode': mode, 'scenario': scenario, 'telemetry': str(telemetry),
                                        'seconds': step['seconds'], 'status': step['status'],
                                        'sampled_peak_tree_rss_bytes': step['sampled_peak_tree_rss_bytes']})
                if code:
                    raise RuntimeError(f'{label} failed; see {telemetry}')
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
    args = parser.parse_args()
    benchmark(args.output, args.cmake, args.ninja, args.max_seconds)


if __name__ == '__main__':
    main()
