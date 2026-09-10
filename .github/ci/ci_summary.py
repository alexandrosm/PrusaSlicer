"""Summarize compact CI reports; do not confuse fixture timing with app benchmarks."""

import argparse
import json
import os
from pathlib import Path


def summarize(output, lane, status):
    reports = Path(output) / 'reports'
    commands = [json.loads(path.read_text(encoding='utf-8')) for path in sorted((reports / 'commands').glob('*.json'))]
    guards = [json.loads(path.read_text(encoding='utf-8')) for path in sorted((reports / 'guards').glob('*.json'))]
    tests_path = reports / 'tests.json'
    tests = json.loads(tests_path.read_text(encoding='utf-8')) if tests_path.is_file() else None
    passed = (status == 'passed' and bool(commands) and bool(guards) and bool(tests) and tests.get('passed')
              and all(item.get('status') == 'passed' for item in commands + guards))
    report = {'lane': lane, 'passed': bool(passed), 'source_commit': os.environ.get('GITHUB_SHA'),
              'scope': 'CI fixtures, not an application build/performance benchmark',
              'command_seconds': round(sum(item.get('seconds', 0) for item in commands), 3),
              'commands': commands, 'guards': guards, 'tests': tests}
    lines = [f'## {lane.capitalize()} CI: {"passed" if passed else "failed"}', '',
             'Fixture measurements only; not a full PrusaSlicer build benchmark.', '',
             '| Command | Status | Seconds |', '| --- | --- | ---: |']
    for item in commands:
        safe_label = str(item['label']).replace('|', '/').replace('\n', ' ')
        lines.append(f"| {safe_label} | {item['status']} | {item.get('seconds', 0):.3f} |")
    if tests:
        lines += ['', f"Python tests: {tests['passes']} passed; {len(tests['skips'])} skips; {tests['tests_run']} run."]
    if guards:
        peak = max(item.get('sampled_peak_tree_rss_bytes', 0) for item in guards)
        lines += ['', f'Maximum sampled process-tree RSS across guarded commands: {peak / 1048576:.3f} MiB.',
                  'Nested guard seconds are already inside command timings; do not add them again.']
    return report, '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--lane', required=True)
    parser.add_argument('--status', choices=('passed', 'failed'), required=True)
    args = parser.parse_args()
    report, markdown = summarize(args.output, args.lane, args.status)
    with (args.output / 'reports/summary.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
    with (args.output / 'reports/summary.md').open('x', encoding='utf-8') as stream:
        stream.write(markdown)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
            stream.write(markdown)
    print(markdown)
    return 0 if report['passed'] or args.status == 'failed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
