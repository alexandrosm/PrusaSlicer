#!/usr/bin/env python3
"""Measure removal of Z3's subpaving testing-tactic registration, in isolation.

Run in an x64 VS developer shell; needs the existing fast-GUI dependency build,
Python 3.9+, psutil, and 7-Zip. No production/dependency files are overwritten.
Uses the same resource limits as openvdb_registration.py. This is semantic smoke
coverage, not a complete arrangement benchmark or a release-size measurement.
"""
import argparse
import json
from pathlib import Path
import re
import shutil
import zlib

from openvdb_registration import HERE, ROOT, block, digest, ninja_paths, response, run, tokens, unescape


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path, help='New experiment output directory')
    parser.add_argument('--seven-zip', default='C:/Program Files/7-Zip/7z.exe')
    parser.add_argument('--empty-registry', action='store_true', help='Broader experimental arm: remove all named tactic/probe/simplifier registration, not solver algorithms')
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() or output.is_relative_to(ROOT / 'deps') or output.is_relative_to(ROOT / 'build-fast-gui'):
        parser.error('Use a new output directory outside existing build/dependency trees')
    compiler, linker = shutil.which('cl'), shutil.which('link')
    if not compiler or not linker:
        parser.error('Run from an x64 Visual Studio developer shell')
    prefix = ROOT / 'deps/build-fast-gui/destdir/usr/local'
    build = ROOT / 'deps/build-fast-gui/builds/z3'
    ninja_path = build / 'build.ninja'
    header, variables = block(ninja_path.read_text(encoding='utf-8'),
        'build src\\api\\dll\\CMakeFiles\\api_dll.dir\\install_tactic.cpp.obj:')
    source = Path(ninja_paths(re.split(r': CXX_COMPILER\S* ', header, maxsplit=1)[1].split(' ||')[0])[0])
    original = source.read_text(encoding='utf-8')
    cut_lines = [line for line in original.splitlines(keepends=True)
                 if line.lstrip().startswith('ADD_TACTIC_CMD("subpaving",')]
    if len(cut_lines) != 1 or 'mk_subpaving_tactic' not in cut_lines[0]:
        raise ValueError('Pinned generated registry changed; expected exactly one testing-tactic entry')
    flags = tokens(unescape(variables['DEFINES'] + ' ' + variables['FLAGS'] + ' ' + variables['INCLUDES']))
    output.mkdir(parents=True)
    # Generated experimental source; the original generated registry is read-only.
    cut_source = output / 'install_tactic_without_subpaving.cpp'
    cut_text = ('#include "tactic/tactic.h"\nclass tactic_manager;\nvoid install_tactics(tactic_manager&) {}\n'
                if args.empty_registry else original.replace(cut_lines[0], ''))
    cut_source.write_text(cut_text, encoding='utf-8')
    report = {'status': 'incomplete', 'scope': 'Z3 semantic probe, not PrusaSlicer DLL/installer',
              'source': str(source), 'source_sha256': digest(source), 'cut_source_sha256': digest(cut_source),
              'probe_sha256': digest(HERE / 'z3_probe.cpp'), 'compiler': compiler, 'linker': linker,
              'initializer_flags': flags, 'registry_mode': 'empty' if args.empty_registry else 'without-subpaving',
              'removed_line': None if args.empty_registry else cut_lines[0].strip(), 'steps': [], 'variants': {}}
    records, variants = report['steps'], report['variants']
    try:
        probe = output / 'probe.obj'
        run('compile_probe', [compiler, response(output / 'probe.rsp',
            ['/nologo', '/c', '/std:c++20', '/MD', '/EHsc', '/O2', '/DNDEBUG', '/I' + str(prefix / 'include'),
             '/Fo' + str(probe), '/Fd' + str(output / 'probe.pdb'), str(HERE / 'z3_probe.cpp')])], build, output, records)
        for name, path, expectation in [('baseline', source, 'present'),
                                        ('selective', cut_source, 'none' if args.empty_registry else 'absent')]:
            directory = output / name
            directory.mkdir()
            obj = directory / 'install_tactic.obj'
            run('compile_registry_' + name, [compiler, response(directory / 'compile.rsp',
                flags + ['/nologo', '/c', '/Fo' + str(obj), '/Fd' + str(directory / 'registry.pdb'), str(path)])], build, output, records)
            exe = directory / 'probe.exe'
            run('link_' + name, [linker, response(directory / 'link.rsp',
                ['/nologo', '/MACHINE:X64', '/INCREMENTAL:NO', '/OPT:REF', '/OPT:ICF', '/Brepro', '/CGTHREADS:1',
                 '/OUT:' + str(exe), '/MAP:' + str(directory / 'probe.map'), str(probe), str(obj),
                 str(prefix / 'lib/libz3.lib'), 'kernel32.lib', 'user32.lib', 'advapi32.lib', 'psapi.lib', 'shell32.lib', 'ws2_32.lib'])], build, output, records)
            stdout = run('run_' + name, [str(exe), expectation], directory, output, records)
            archive = directory / 'probe.7z'
            run('compress_' + name, [args.seven_zip, 'a', '-t7z', '-mx=7', '-m0=LZMA2', '-md=32m',
                '-mmt=1', '-bd', str(archive), exe.name], directory, output, records)
            compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
            compressed_bytes = 0
            with exe.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1048576), b''):
                    compressed_bytes += len(compressor.compress(chunk))
            compressed_bytes += len(compressor.flush())
            variants[name] = {'probe_bytes': exe.stat().st_size, 'probe_7z_bytes': archive.stat().st_size,
                              'probe_deflate_bytes': compressed_bytes, 'probe_sha256': digest(exe), 'stdout': stdout}
        if variants['baseline']['stdout'] != variants['selective']['stdout']:
            raise RuntimeError('Z3 semantic probe outputs differ')
        report['status'] = 'semantic-smoke-parity-passed'
        report['savings'] = {key: variants['baseline'][key] - variants['selective'][key]
                             for key in ('probe_bytes', 'probe_7z_bytes', 'probe_deflate_bytes')}
        print(json.dumps(report['savings'], indent=2), flush=True)
    finally:
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
