#!/usr/bin/env python3
"""Isolated Windows/MSVC OpenVDB registration A/B using an existing Ninja build.

Run in an x64 VS developer shell. Requires Python 3.9+, psutil and 7-Zip.
Never invokes a build system or writes to existing application/dependency trees.
The existing build must already contain all object files and static libraries.
Outputs are experimental, not release artifacts. The fast-GUI build lacks STEP.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import zlib

import psutil


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def tokens(value):
    # Use Windows parsing, including quotes embedded in /I and -D tokens.
    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(ctypes.c_wchar_p)
    count = ctypes.c_int()
    values = parse('unused.exe ' + value, ctypes.byref(count))
    if not values:
        raise ctypes.WinError()
    try:
        return list(values[:count.value])[1:]
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(values)


def block(ninja, prefix):
    for section in ninja.split('\n\n'):
        if any(line.startswith(prefix) for line in section.splitlines()):
            header = next(line for line in section.splitlines() if line.startswith(prefix))
            variables = dict(re.findall(r'^  (\w+) = (.*)$', section, re.M))
            return header, variables
    raise ValueError(f'No Ninja edge for {prefix}')


def unescape(value):
    return value.replace('$:', ':').replace('$ ', ' ').replace('$$', '$')


def ninja_paths(value):
    # Ninja escapes spaces with '$ ', rather than Windows double quotes.
    return [unescape(p) for p in re.findall(r'(?:\$[ :$]|[^\s])+', value)]


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def run(label, command, cwd, output, records, timeout=180):
    if psutil.virtual_memory().available < 6 * 1024**3:
        raise RuntimeError('Less than 6 GiB RAM available; refusing to start another process')
    print(label, flush=True)
    started = time.monotonic()
    log = output / (label + '.log')
    peak = 0
    with log.open('wb') as stream:
        process = subprocess.Popen(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW)
        try:
            parent = psutil.Process(process.pid)
        except psutil.NoSuchProcess:
            parent = None
        try:
            # A single logical CPU is inherited by compiler/linker children.
            try:
                if parent is not None:
                    parent.cpu_affinity([parent.cpu_affinity()[-1]])
            except psutil.NoSuchProcess:
                pass  # A tiny command can finish before affinity is applied.
            while process.poll() is None:
                try:
                    members = [parent, *parent.children(recursive=True)] if parent is not None else []
                except psutil.NoSuchProcess:
                    members = []  # Process exited between poll() and sampling.
                rss = 0
                for member in members:
                    try:
                        rss += member.memory_info().rss
                    except psutil.NoSuchProcess:
                        pass
                peak = max(peak, rss)
                if rss > 3 * 1024**3 or time.monotonic() - started > timeout:
                    raise RuntimeError(f'{label} exceeded the 3 GiB sampled working-set or {timeout}s limit')
                time.sleep(0.2)
            process.wait()
            if process.returncode:
                raise RuntimeError(f'{label} failed ({process.returncode}):\n{log.read_text(errors="replace")[-6000:]}')
        except BaseException as error:
            records.append({'step': label, 'status': 'failed', 'error': str(error),
                            'seconds': round(time.monotonic() - started, 3),
                            'sampled_peak_tree_rss_bytes': peak})
            raise
        finally:
            if process.poll() is None:
                try:
                    children = parent.children(recursive=True) if parent is not None else []
                except psutil.NoSuchProcess:
                    children = []
                for member in reversed(children):
                    try:
                        member.kill()
                    except psutil.NoSuchProcess:
                        pass
                process.kill()
            process.wait()
    records.append({'step': label, 'seconds': round(time.monotonic() - started, 3),
                    'sampled_peak_tree_rss_bytes': peak})
    print(f'  {records[-1]["seconds"]} s; sampled peak {peak / 1048576:.1f} MiB', flush=True)
    return log.read_text(errors='replace')


def response(path, arguments):
    # Generated experiment artifact, with Windows quoting for paths with spaces.
    path.write_text(subprocess.list2cmdline(arguments), encoding='utf-8')
    return '@' + str(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, default=ROOT / 'build-fast-gui')
    parser.add_argument('--output', type=Path, required=True, help='New output directory, outside the build/deps trees')
    parser.add_argument('--seven-zip', default='C:/Program Files/7-Zip/7z.exe')
    parser.add_argument('--with-dll', action='store_true', help='Also attempt full DLL links after probe parity (same resource ceiling)')
    parser.add_argument('--reuse-initializers-from', type=Path, help='Reuse prior initializer objects after checking source hashes and flags')
    args = parser.parse_args()
    build = args.build_dir.resolve()
    output = args.output.resolve()
    if output.is_relative_to(build) or output.is_relative_to(ROOT / 'deps'):
        parser.error('Output must not be in an application/dependency build tree')
    if output.exists():
        parser.error('Output already exists; use a fresh directory')
    compiler, linker = shutil.which('cl'), shutil.which('link')
    if not compiler or not linker:
        parser.error('Run from an x64 Visual Studio developer shell')
    if not Path(args.seven_zip).is_file():
        parser.error('7-Zip executable not found')
    ninja_path = build / 'build.ninja'
    ninja = ninja_path.read_text(encoding='utf-8')
    link_header, link_vars = block(ninja, 'build src\\PrusaSlicer.dll ')
    object_part = re.split(r': CXX_SHARED_LIBRARY_LINKER\S* ', link_header, maxsplit=1)[1].split(' |')[0]
    objects = ninja_paths(object_part)
    libraries = tokens(unescape(link_vars['LINK_LIBRARIES']))
    # Read flags without running Ninja: no CMake regeneration or incidental builds.
    _, compile_vars = block(ninja, 'build src\\libslic3r\\CMakeFiles\\libslic3r.dir\\Unity\\unity_libslic3r_core_21_cxx.cxx.obj:')
    prefix = Path(next(p for p in libraries if Path(p).name.lower() == 'libopenvdb.lib')).parent.parent
    compile_flags = tokens(unescape(compile_vars['DEFINES'] + ' ' + compile_vars['INCLUDES']))
    compile_flags += ['/nologo', '/c', '/std:c++17', '/EHsc', '/MD', '/O2', '/Ob2', '/DNDEBUG', '/bigobj', '/Gy', '/Gw', '/wd4146']
    vdb_build = prefix.parents[2] / 'builds/OpenVDB'
    init_header, init_vars = block((vdb_build / 'build.ninja').read_text(encoding='utf-8'),
        'build openvdb\\openvdb\\CMakeFiles\\openvdb_static.dir\\openvdb.cc.obj:')
    init_source = Path(ninja_paths(re.split(r': CXX_COMPILER\S* ', init_header, maxsplit=1)[1].split(' ||')[0])[0])
    init_flags = tokens(unescape(init_vars['DEFINES'] + ' ' + init_vars['FLAGS'] + ' ' + init_vars['INCLUDES']))
    init_flags += ['/nologo', '/c']
    for item in objects:
        if not (build / item).is_file():
            raise ValueError(f'Existing application object is missing: {item}')
    output.mkdir(parents=True)
    records = []
    report = {'status': 'incomplete', 'build': str(build), 'ninja_sha256': digest(ninja_path),
              'compiler': compiler, 'linker': linker, 'dependency_prefix': str(prefix),
              'limits': {'cpu_affinity_count': 1, 'process_priority': 'below normal',
                         'sampled_tree_rss_limit_bytes': 3 * 1024**3, 'step_timeout_seconds': 180},
              'scope': 'Same existing app objects/libraries; only OpenVDB registration is replaced. Not release validation.',
              'initializer_flags': init_flags,
              'source_hashes': {str(p): digest(p) for p in (init_source, HERE / 'openvdb_float_init.cpp', HERE / 'openvdb_probe.cpp')},
              'steps': records}
    try:
        init_obj = output / 'float_init.obj'
        full_init_obj = output / 'full_init.obj'
        probe_obj = output / 'probe.obj'
        # Compile BOTH initializers with identical original dependency flags,
        # so language mode and function/data section packaging cannot confound A/B.
        if args.reuse_initializers_from:
            previous = args.reuse_initializers_from.resolve()
            previous_report = json.loads((previous / 'report.json').read_text(encoding='utf-8'))
            if previous_report['source_hashes'] != report['source_hashes'] or previous_report['initializer_flags'] != init_flags:
                raise ValueError('Cannot reuse initializers: source hashes or compiler flags differ')
            full_init_obj, init_obj = previous / 'full_init.obj', previous / 'float_init.obj'
            report['reused_initializers'] = {str(p): digest(p) for p in (full_init_obj, init_obj)}
        else:
            for label, source, obj in [('full', init_source, full_init_obj),
                                       ('float', HERE / 'openvdb_float_init.cpp', init_obj)]:
                run('compile_' + label + '_init', [compiler, response(output / (label + '_init.rsp'),
                    init_flags + ['/Fo' + str(obj), '/Fd' + str(output / (label + '.pdb')), str(source)])], vdb_build, output, records)
        run('compile_probe', [compiler, response(output / 'probe.rsp',
            compile_flags + ['/Fo' + str(probe_obj), '/Fd' + str(output / 'probe.pdb'), str(HERE / 'openvdb_probe.cpp')])], build, output, records)
        variants = {}
        extras = {'baseline': [str(full_init_obj)], 'selective': [str(init_obj)]}
        report['variants'] = variants
        for name, extra in extras.items():
            directory = output / name
            directory.mkdir()
            exe = directory / 'probe.exe'
            # Some existing dependencies use /GL. Constrain LTCG's internal
            # code-generation workers, not just the outer build concurrency.
            common = ['/nologo', '/MACHINE:X64', '/INCREMENTAL:NO', '/OPT:REF', '/OPT:ICF', '/Brepro', '/LTCG', '/CGTHREADS:1']
            run('link_probe_' + name, [linker, response(directory / 'probe_link.rsp',
                common + ['/OUT:' + str(exe), '/MAP:' + str(directory / 'probe.map')]
                + extra + [str(probe_obj)] + libraries)], build, output, records)
            # Runtime dependencies are found through a child-local PATH.
            old_path = os.environ['PATH']
            try:
                os.environ['PATH'] = str(build / 'src') + os.pathsep + str(prefix / 'bin') + os.pathsep + old_path
                stdout = run('run_probe_' + name, [str(exe)], build, output, records)
            finally:
                os.environ['PATH'] = old_path
            variants[name] = {'probe_bytes': exe.stat().st_size, 'probe_sha256': digest(exe), 'probe_stdout': stdout}
        if variants['baseline']['probe_stdout'] != variants['selective']['probe_stdout']:
            raise RuntimeError('Baseline/selective geometry probe outputs differ')
        report['status'] = 'probe-parity-passed'
        # Size experiments happen only after both independent probes have passed.
        kinds = ['probe'] + (['dll'] if args.with_dll else [])
        for kind in kinds:
            for name, extra in extras.items():
                directory = output / name
                payload = directory / ('probe.exe' if kind == 'probe' else 'PrusaSlicer.dll')
                if kind == 'dll':
                    # Do not request a public-symbol map for the full app: it
                    # increases linker memory. The small probes retain maps.
                    run('link_dll_' + name, [linker, response(directory / 'dll_link.rsp',
                        common + ['/DLL', '/VERSION:0.0', '/OUT:' + str(payload),
                                  '/IMPLIB:' + str(directory / 'PrusaSlicer.lib')]
                        + extra + objects + libraries)], build, output, records)
                archive = directory / (kind + '.7z')
                run('compress_' + kind + '_' + name, [args.seven_zip, 'a', '-t7z', '-mx=7', '-m0=LZMA2', '-md=32m',
                    '-ms=256m', '-mqs=on', '-mmt=1', '-bd', str(archive), payload.name], directory, output, records)
                compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
                compressed_bytes = 0
                with payload.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        compressed_bytes += len(compressor.compress(chunk))
                compressed_bytes += len(compressor.flush())
                variants[name].update({kind + '_bytes': payload.stat().st_size, kind + '_sha256': digest(payload),
                    kind + '_deflate_bytes': compressed_bytes, kind + '_7z_bytes': archive.stat().st_size})
            report['savings'] = {key: variants['baseline'][key] - variants['selective'][key]
                                 for key in variants['baseline'] if key.endswith('_bytes') and key in variants['selective']}
            (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report['savings'], indent=2), flush=True)
    except Exception as error:
        report['error'] = str(error)
        raise
    finally:
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
