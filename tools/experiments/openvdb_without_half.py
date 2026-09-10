#!/usr/bin/env python3
"""Relink a passing OpenVDB probe after removing its explicit Half library input.

Run in an x64 VS developer shell. Reuses the reference response file and existing
objects; never compiles, builds dependencies, or changes production files. This
checks the exercised probe paths, not complete application or download savings.
"""

import argparse
import json
import os
from pathlib import Path

import openvdb_registration as common


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--reference', type=Path,
        default=common.ROOT / 'out/upstream-experiments/openvdb-run4',
        help='Passing registration experiment containing baseline/probe.exe',
    )
    parser.add_argument('--output', type=Path, required=True, help='Fresh experiment directory')
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('This experiment requires Windows/MSVC')

    reference = args.reference.resolve()
    reference_report_path = reference / 'report.json'
    reference_report = json.loads(reference_report_path.read_text(encoding='utf-8'))
    if reference_report.get('status') != 'probe-parity-passed':
        parser.error('Reference registration experiment did not pass probe parity')
    baseline = reference_report['variants']['baseline']
    expected_stdout = baseline['probe_stdout']
    if not isinstance(expected_stdout, str) or not expected_stdout.rstrip().endswith('\nPASS'):
        parser.error('Reference baseline probe stdout does not end with a PASS line')
    baseline_exe = reference / 'baseline/probe.exe'
    baseline_hash = common.digest(baseline_exe)
    if baseline_hash != baseline['probe_sha256']:
        parser.error('Reference baseline executable does not match its recorded SHA-256')
    if baseline_exe.stat().st_size != baseline['probe_bytes']:
        parser.error('Reference baseline executable size does not match its report')

    build = Path(reference_report['build']).resolve()
    prefix = Path(reference_report['dependency_prefix']).resolve()
    linker = Path(reference_report['linker'])
    if not linker.is_file():
        parser.error('The reference linker is unavailable; use the original MSVC toolchain')
    output = args.output.resolve()
    for forbidden in (build, prefix, (common.ROOT / 'deps').resolve(), reference):
        if output.is_relative_to(forbidden):
            parser.error('Output must be outside the reference and application/dependency trees')
    if output.exists():
        parser.error('Output already exists; use a fresh directory')

    reference_rsp = reference / 'baseline/probe_link.rsp'
    original_arguments = common.tokens(reference_rsp.read_text(encoding='utf-8'))
    half_indices = [i for i, value in enumerate(original_arguments)
                    if Path(value).name.lower() == 'half-2_5.lib']
    out_indices = [i for i, value in enumerate(original_arguments) if value.lower().startswith('/out:')]
    map_indices = [i for i, value in enumerate(original_arguments) if value.lower().startswith('/map:')]
    if len(half_indices) != 1 or len(out_indices) != 1 or len(map_indices) != 1:
        parser.error('Expected exactly one Half-2_5.lib input, /OUT, and /MAP in the reference response')
    if Path(original_arguments[out_indices[0]][5:]).resolve() != baseline_exe.resolve():
        parser.error('Reference response /OUT does not identify the verified baseline executable')

    executable = output / 'probe.exe'
    map_path = output / 'probe.map'
    arguments = []
    for i, value in enumerate(original_arguments):
        if i == half_indices[0]:
            continue
        if i == out_indices[0]:
            value = '/OUT:' + str(executable)
        elif i == map_indices[0]:
            value = '/MAP:' + str(map_path)
        arguments.append(value)

    # Only the selected archive and output locations differ. Do not add linker
    # flags, rewrite relative library paths, or remove implied/default libraries.
    unchanged_original = [v for i, v in enumerate(original_arguments)
                          if i not in half_indices + out_indices + map_indices]
    unchanged_experiment = [v for v in arguments
                            if not v.lower().startswith(('/out:', '/map:'))]
    if unchanged_original != unchanged_experiment:
        raise RuntimeError('Unexpected reference linker argument change')

    output.mkdir(parents=True)
    records = []
    report = {
        'status': 'incomplete',
        'scope': 'One explicit Half-2_5.lib input removed from a passing baseline probe link; '
                 'no production, full-application, or compressed-download claim.',
        'reference': str(reference),
        'reference_report_sha256': common.digest(reference_report_path),
        'reference_response_sha256': common.digest(reference_rsp),
        'reference_probe_sha256': baseline_hash,
        'reference_probe_bytes': baseline['probe_bytes'],
        'removed_library_argument': original_arguments[half_indices[0]],
        'build': str(build),
        'dependency_prefix': str(prefix),
        'linker': str(linker),
        'limits': {
            'cpu_affinity_count': 1,
            'process_priority': 'below normal',
            'sampled_tree_rss_limit_bytes': 3 * 1024**3,
            'step_timeout_seconds': 180,
        },
        'steps': records,
    }
    try:
        rsp = output / 'probe_link.rsp'
        common.run('link_probe_without_half',
                   [str(linker), common.response(rsp, arguments)], build, output, records)
        report['response_sha256'] = common.digest(rsp)
        report['probe_bytes'] = executable.stat().st_size
        report['probe_sha256'] = common.digest(executable)
        report['probe_identical_to_reference'] = report['probe_sha256'] == baseline_hash
        report['probe_byte_difference'] = baseline['probe_bytes'] - report['probe_bytes']
        # An archive could still be pulled in by a /DEFAULTLIB directive. Keep
        # that distinct from removing its explicit response-file argument.
        report['half_archive_map_mentions'] = sum(
            'half-2_5:' in line.lower()
            for line in map_path.read_text(errors='replace').splitlines()
        )
        old_path = os.environ.get('PATH')
        try:
            os.environ['PATH'] = os.pathsep.join((str(build / 'src'), str(prefix / 'bin'), old_path or ''))
            stdout = common.run('run_probe_without_half', [str(executable)], build, output, records)
        finally:
            if old_path is None:
                os.environ.pop('PATH', None)
            else:
                os.environ['PATH'] = old_path
        report['probe_stdout'] = stdout
        if stdout != expected_stdout:
            raise RuntimeError('Without-Half probe output differs from the passing reference baseline')
        if common.digest(baseline_exe) != baseline_hash:
            raise RuntimeError('Reference executable changed during the experiment')
        report['status'] = 'explicit-half-removal-probe-parity-passed'
        print(f'Probe parity passed: {report["probe_bytes"]:,} bytes; '
              f'byte difference {report["probe_byte_difference"]:+,}. No download-size claim.', flush=True)
    except Exception as error:
        report['error'] = str(error)
        raise
    finally:
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
