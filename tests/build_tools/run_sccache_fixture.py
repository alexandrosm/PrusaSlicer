"""Assert a tiny MSVC cache miss/hit under the guarded private supervisor.

Cold and warm runs use the same fresh build/cache but separate private servers.
The warm run deletes ONLY the proven cold fixture object, then requires a real
cache hit, zero compiler executions, retained embedded symbols and identical
object bytes. Exit zero and elapsed time alone are not evidence of a cache hit.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import time


REPOSITORY = Path(__file__).resolve().parents[2]
FIXTURE = REPOSITORY / 'tests/build_tools/sccache_fixture'


def identity(path):
    return {'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def counters(document):
    """Pinned sccache JSON has aggregate counts and duplicate advanced counts."""
    stats = document['stats']
    result = {}
    for name in ('compile_requests', 'compilations', 'cache_writes', 'compile_fails',
                 'requests_not_cacheable', 'requests_unsupported_compiler',
                 'non_cacheable_compilations', 'cache_write_errors', 'cache_read_errors'):
        value = stats[name]
        if type(value) is not int or value < 0:
            raise RuntimeError(f'Invalid sccache counter {name}')
        result[name] = value
    for name in ('cache_hits', 'cache_misses', 'cache_errors'):
        counts = stats[name]['counts']
        if not isinstance(counts, dict) or any(type(value) is not int or value < 0 for value in counts.values()):
            raise RuntimeError(f'Invalid sccache aggregate {name}')
        result[name] = sum(counts.values())
    return result


def prove_counters(mode, before, after):
    old, new = counters(before), counters(after)
    delta = {key: new[key] - old[key] for key in old}
    expected = dict.fromkeys(delta, 0)
    expected['compile_requests'] = 1
    if mode == 'cold':
        expected.update(cache_misses=1, cache_writes=1, compilations=1)
    else:
        expected['cache_hits'] = 1
    if delta != expected:
        raise RuntimeError(f'{mode}: no genuine expected cache mechanism: {delta}; expected {expected}')
    return delta


def has_embedded_symbols(path):
    """The tiny standard x64 COFF object must retain a CodeView debug section."""
    data = path.read_bytes()
    if len(data) < 20:
        return False
    machine, count = struct.unpack_from('<HH', data)
    optional = struct.unpack_from('<H', data, 16)[0]
    offset = 20 + optional
    if machine != 0x8664 or not 0 < count <= 96 or len(data) < offset + 40 * count:
        return False
    return any(data[offset + 40 * index:offset + 40 * index + 8].rstrip(b'\0') == b'.debug$S'
               for index in range(count))


def statistics(sccache):
    result = subprocess.run([str(sccache), '--show-stats', '--stats-format=json'],
                            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    return json.loads(result.stdout)


def run(args):
    if os.name != 'nt':
        raise RuntimeError('This mechanism fixture requires native Windows MSVC')
    if not os.environ.get('SCCACHE_SERVER_PORT') or not os.environ.get('SCCACHE_CONF'):
        raise RuntimeError('Run only below sccache_supervisor.py with its private endpoint')
    build, report_path = args.build.resolve(), args.report.resolve()
    if report_path.exists():
        raise FileExistsError('Refusing to overwrite a fixture report')
    report_path.parent.mkdir(parents=True, exist_ok=True)
    object_file = build / 'CMakeFiles/cache_probe.dir/cache_probe.cpp.obj'
    report = {'status': 'running', 'mode': args.mode, 'build': str(build),
              'scope': 'one MSVC object with embedded symbols; no application build or final PDB link',
              'fixture_sha256': {path.name: identity(path)['sha256'] for path in sorted(FIXTURE.iterdir())},
              'sccache': identity(Path(args.sccache))}
    started = time.monotonic()
    try:
        if args.mode == 'cold':
            if build.exists():
                raise FileExistsError('Cold fixture requires a fresh build directory')
            subprocess.run([args.cmake, '-S', str(FIXTURE), '-B', str(build), '-G', 'Ninja',
                            '-DCMAKE_BUILD_TYPE=Release', '-DPRUSA_SOURCE=' + str(REPOSITORY),
                            '-DCMAKE_MAKE_PROGRAM=' + args.ninja,
                            '-DSLIC3R_COMPILER_CACHE=' + args.sccache], check=True)
        else:
            if not args.previous_report:
                raise ValueError('Warm fixture requires the cold report')
            previous = json.loads(args.previous_report.read_text(encoding='utf-8'))
            if (previous.get('status') != 'passed' or previous.get('mode') != 'cold'
                    or previous.get('build') != str(build)
                    or previous.get('fixture_sha256') != report['fixture_sha256']
                    or previous.get('sccache') != report['sccache']
                    or not object_file.is_file() or object_file.is_symlink()
                    or previous.get('object') != identity(object_file)):
                raise RuntimeError('Warm inputs do not match the independently proven cold run')
            report['cold_report'] = str(args.previous_report.resolve())
            report['cold_object'] = previous['object']
            object_file.unlink()  # Exact private fixture output; never recursive clean.
            report['forced_compile_object_removed'] = not object_file.exists()
        report['stats_before'] = statistics(args.sccache)
        command = [args.cmake, '--build', str(build), '--target', 'cache_probe', '--parallel', '1', '--verbose']
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace')
        print(result.stdout, end='', flush=True)
        report['build_output'] = result.stdout
        result.check_returncode()
        if (not re.search(r'(?<!\S)[-/]Z7(?=\s|$)', result.stdout)
                or re.search(r'(?<!\S)[-/]Z[iI](?=\s|$)', result.stdout)
                or 'sccache' not in result.stdout.lower()):
            raise RuntimeError('The executed compile must use sccache and /Z7, not shared compiler PDBs')
        if not object_file.is_file() or not has_embedded_symbols(object_file):
            raise RuntimeError('Expected native x64 object with embedded CodeView symbols')
        report['object'] = identity(object_file)
        report['embedded_codeview_symbols'] = True
        if args.mode == 'warm' and report['object'] != report['cold_object']:
            raise RuntimeError('Cache hit did not reproduce the cold object byte-for-byte')
        report['stats_after'] = statistics(args.sccache)
        report['counter_delta'] = prove_counters(args.mode, report['stats_before'], report['stats_after'])
        report['status'] = 'passed'
    except Exception as error:
        report.update(status='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        report['seconds'] = round(time.monotonic() - started, 3)
        with report_path.open('x', encoding='utf-8') as stream:
            json.dump(report, stream, indent=2)
            stream.write('\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cmake', required=True)
    parser.add_argument('--ninja', required=True)
    parser.add_argument('--sccache', required=True)
    parser.add_argument('--build', required=True, type=Path)
    parser.add_argument('--mode', required=True, choices=('cold', 'warm'))
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--previous-report', type=Path)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
