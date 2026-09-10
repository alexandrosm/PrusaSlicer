"""Hosted full-feature release evidence; no geometry changes or signing claims."""
import argparse
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
from package_portable import find_seven_zip, inventory, package, run_seven_zip
from stage_windows_release import extract_baseline, stage_release, OFFICIAL_ROOT


def write_json(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def collect(output):
    """Retain compact logs/reports only, never build trees or package payloads."""
    reports = output / 'reports'
    sources = (ROOT / 'out/build-guard', output / 'validation')
    for source in sources:
        if not source.is_dir():
            continue
        for path in source.rglob('*'):
            if path.is_file() and path.suffix in ('.json', '.log'):
                # Validation trees include copied app resources; retain only
                # their root reports plus the smoke report/logs.
                if source.name == 'validation' and path.relative_to(source).parts[0] in (
                        'baseline', 'candidate', 'extraction'):
                    continue
                target = reports / source.name / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise RuntimeError(f'Refusing to overwrite evidence: {target}')
                shutil.copyfile(path, target)
    stages = []
    for path in sorted(reports.rglob('*.json')):
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if isinstance(value, dict) and 'seconds' in value:
            stages.append({'report': str(path.relative_to(reports)),
                           **{key: value[key] for key in ('seconds', 'status', 'exit_code',
                              'sampled_peak_tree_rss_bytes') if key in value}})
    warm = [item['seconds'] for item in stages if item['report'].startswith('application-warm-')]
    report = {'git_sha': os.environ.get('GITHUB_SHA'), 'runner_image': os.environ.get('ImageVersion'),
              'stages': stages, 'warm_wrapper_median_seconds': statistics.median(warm) if warm else None,
              'scope': 'full-feature lean-release, current revision; not an upstream clean-build A/B',
              'not_measured': ['interactive GUI startup and steady-state memory', 'signed installer and install time',
                               'update delta size', 'upstream default clean-build speedup']}
    write_json(reports / 'summary.json', report)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as stream:
            stream.write('## Release experiment\n\n')
            stream.write(f"Revision: `{report['git_sha']}`\n\n")
            stream.write('| Phase/report | Seconds | Result | Sampled peak MiB |\n|---|---:|---|---:|\n')
            for item in stages:
                peak = item.get('sampled_peak_tree_rss_bytes')
                stream.write(f"| {item['report']} | {item['seconds']:.3f} | "
                             f"{item.get('status', item.get('exit_code', 'see report'))} | "
                             f"{peak / 1048576 if peak is not None else '—'} |\n")
            stream.write('\nBuild success, test parity and package verification have separate results. '
                         'See the job outcome and reports; a summary is not a pass gate.\n')
            stream.write('\nNot measured: ' + '; '.join(report['not_measured']) + '.\n')
    return report


def validate(baseline_zip, step, output):
    output.mkdir(parents=True, exist_ok=False)
    build = ROOT / 'build-lean-release'
    cache = (build / 'CMakeCache.txt').read_text(encoding='utf-8')
    if 'SLIC3R_MSVC_DEBUG_SYMBOLS:BOOL=TRUE' not in cache.upper() and 'SLIC3R_MSVC_DEBUG_SYMBOLS:BOOL=ON' not in cache.upper():
        raise RuntimeError('Release symbol-generation option is not enabled')
    pdb = build / 'src/PrusaSlicer.pdb'
    if not pdb.is_file() or pdb.stat().st_size == 0:
        raise RuntimeError('Main application PDB was not generated; refusing a symbol-retention claim')
    baseline = output / 'baseline'
    candidate = output / 'candidate'
    extract_baseline(baseline_zip, baseline)
    stage_release(baseline_zip, candidate, ROOT, ROOT / 'build-lean-release')
    package_root = candidate / OFFICIAL_ROOT
    # Smoke failures do not discard independently useful size/transport evidence.
    smoke = subprocess.run([sys.executable, '-B', str(ROOT / 'tools/verify_release_smoke.py'),
                            str(baseline / OFFICIAL_ROOT), str(package_root), '--source-root', str(ROOT),
                            '--step', str(step), '--output', str(output / 'smoke'), '--timeout', '120'], check=False)
    archive = output / 'PrusaSlicer-current-original-geometry.7z'
    result = package(package_root, archive, dictionary_mib=32, compression_level=9)
    extraction = output / 'extraction'
    started = time.monotonic()
    run_seven_zip(find_seven_zip(), ['x', str(archive), '-o' + str(extraction), '-mmt=1', '-bb0', '-bd'])
    extract_seconds = time.monotonic() - started
    if inventory(extraction / OFFICIAL_ROOT) != inventory(package_root):
        raise RuntimeError('Timed extracted package differs from staged release')
    report = {'git_sha': os.environ.get('GITHUB_SHA'), 'archive_bytes': result['archive_bytes'],
              'archive_sha256': result['archive_sha256'], 'uncompressed_bytes': result['uncompressed_bytes'],
              'archive_extract_seconds': extract_seconds, 'smoke_exit_code': smoke.returncode,
              'geometry': 'original source meshes; no simplification or on-demand resources',
              'packaging': 'unsigned offline portable archive; inherited runtime extras are recorded in candidate.json',
              'symbol_policy': 'lean-release retains PDB generation; PDBs are not runtime payload',
              'main_pdb_bytes': pdb.stat().st_size,
              'not_measured': 'extraction is not installer time; no GUI startup/memory or update-delta claim'}
    write_json(output / 'release-result.json', report)
    if smoke.returncode:
        raise RuntimeError(f'CLI/STEP smoke/parity failed with code {smoke.returncode}; inspect smoke/report.json')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--step', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--collect', type=Path)
    args = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        parser.error('Restricted to disposable GitHub-hosted runners')
    if args.collect:
        collect(args.collect)
    elif args.baseline and args.step and args.output:
        validate(args.baseline, args.step, args.output)
    else:
        parser.error('Specify --collect, or --baseline/--step/--output')


if __name__ == '__main__':
    main()
