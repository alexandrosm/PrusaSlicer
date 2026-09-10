"""CI-only bounded-output command capture; resource guards remain explicit children.

Output is streamed to disk and console, never accumulated in memory. This helper
does not claim a resource limit: use run_guarded.py for native/geometry work.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time


def capture(command, report_path, label):
    report_path = Path(report_path)
    log_path = report_path.with_suffix('.log')
    if not command or report_path.exists() or log_path.exists():
        raise ValueError('A command and fresh report/log paths are required')
    report = {'label': label, 'command': command, 'cwd': str(Path.cwd()), 'status': 'running',
              'started_utc': datetime.now(timezone.utc).isoformat(), 'log': log_path.name}
    # Reserve both outputs before starting anything; never overwrite a prior run.
    with report_path.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
    start = time.monotonic()
    process = None
    try:
        with log_path.open('xb') as log:
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
                while True:
                    chunk = process.stdout.read1(65536)
                    if not chunk:
                        break
                    log.write(chunk)
                    log.flush()
                    # stdout can be a text fixture stream in unit tests.
                    if hasattr(sys.stdout, 'buffer'):
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
                    else:
                        sys.stdout.write(chunk.decode('utf-8', errors='replace'))
                code = process.wait()
        report.update(status='passed' if code == 0 else 'failed', returncode=code)
    except (OSError, subprocess.SubprocessError) as error:
        report.update(status='failed', returncode=1, error=f'{type(error).__name__}: {error}')
    finally:
        report['seconds'] = round(time.monotonic() - start, 3)
        report['finished_utc'] = datetime.now(timezone.utc).isoformat()
        # This is the exact report exclusively reserved by this invocation.
        with report_path.open('w', encoding='utf-8') as stream:
            json.dump(report, stream, indent=2)
            stream.write('\n')
    return report['returncode']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    return capture(command, args.report, args.label)


if __name__ == '__main__':
    sys.exit(main())
