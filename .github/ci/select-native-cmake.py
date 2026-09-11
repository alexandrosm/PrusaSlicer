"""Select wheel-verified native CMake, never pip's generated launcher.

Only importlib.metadata is used to locate the installed distribution: importing
``cmake`` could import an unrelated workspace cmake.py. The installed wheel's
RECORD is integrity/provenance evidence, not an independent publisher signature.
The workflow remains responsible for pinning the wheel installation.
"""
import argparse
import base64
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import stat
import subprocess


def regular_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
        raise ValueError(f'Expected a regular non-reparse file: {path}')
    return info


def select_native(distribution=None, windows=None):
    distribution = metadata.distribution('cmake') if distribution is None else distribution
    windows = os.name == 'nt' if windows is None else windows
    entries = distribution.files
    if entries is None:
        raise ValueError('Installed CMake distribution has no wheel RECORD')
    result = {'distribution': 'cmake', 'distribution_version': distribution.version, 'binaries': {}}
    for program in ('cmake', 'ctest'):
        relative = 'cmake/data/bin/' + program + ('.exe' if windows else '')
        candidates = [entry for entry in entries if str(entry).replace('\\', '/').casefold() == relative.casefold()]
        if len(candidates) != 1:
            raise ValueError(f'Missing or ambiguous native binary RECORD entry: {relative}')
        entry = candidates[0]
        if entry.hash is None or entry.hash.mode != 'sha256':
            raise ValueError(f'Native binary requires a SHA256 wheel RECORD hash: {relative}')
        path = Path(distribution.locate_file(entry)).absolute()
        info = regular_file(path)
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        encoded = base64.urlsafe_b64encode(digest.digest()).decode('ascii').rstrip('=')
        if encoded != entry.hash.value.rstrip('=') or (entry.size is not None and info.st_size != entry.size):
            raise ValueError(f'Native binary does not match its wheel RECORD: {relative}')
        result['binaries'][program] = {'path': str(path), 'record_path': relative,
                                       'sha256': digest.hexdigest(), 'bytes': info.st_size}
    directories = {str(Path(binary['path']).parent) for binary in result['binaries'].values()}
    if len(directories) != 1:
        raise ValueError('CMake and CTest must share one native bin directory')
    result['native_bin'] = directories.pop()
    if '\n' in result['native_bin'] or '\r' in result['native_bin']:
        raise ValueError('Native bin path cannot contain a newline')
    # Deliberately exclude installer-generated launchers, timestamps and local
    # installation paths. Actual build fingerprints still hash selected tools.
    identity = {'distribution_version': result['distribution_version'],
                'binaries': {name: {key: value for key, value in binary.items() if key != 'path'}
                             for name, binary in result['binaries'].items()}}
    result['identity_sha256'] = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return result


def publish(github_path, report_path, distribution=None, windows=None):
    github_path, report_path = Path(github_path).absolute(), Path(report_path).absolute()
    if github_path.resolve() == report_path.resolve():
        raise ValueError('Report and GITHUB_PATH must be separate files')
    if report_path.exists() or report_path.is_symlink():
        raise FileExistsError(f'Refusing to overwrite native CMake report: {report_path}')
    if github_path.exists() or github_path.is_symlink():
        regular_file(github_path)
    result = select_native(distribution, windows)
    command = [result['binaries']['cmake']['path'], '--version']
    version = subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)
    match = re.match(r'^cmake version (\S+)', version.stdout)
    if match is None:
        raise ValueError('Verified native binary did not report a CMake version')
    result.update(native_version=match.group(1), status='verified',
                  github_path=str(github_path), version_command=command,
                  record_trust='Installed pinned wheel RECORD, not a publisher signature')
    payload = json.dumps(result, indent=2) + '\n'
    report_path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive report creation precedes PATH publication: reruns never silently
    # overwrite provenance or append a second selection on a report collision.
    with report_path.open('x', encoding='utf-8') as stream:
        stream.write(payload)
    with github_path.open('a', encoding='utf-8', newline='\n') as stream:
        stream.write(result['native_bin'] + '\n')
    print(f"Verified native CMake {result['native_version']}: {result['native_bin']}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--github-path', type=Path, default=os.environ.get('GITHUB_PATH'))
    parser.add_argument('--report', type=Path, required=True, help='Fresh JSON report, never overwritten')
    args = parser.parse_args()
    if not args.github_path:
        parser.error('--github-path or GITHUB_PATH is required')
    publish(args.github_path, args.report)


if __name__ == '__main__':
    main()
