"""Compiler-free patch checks, including a fresh Windows-style Git checkout.

Run normally with Python/unittest. To additionally verify the exact pinned JPEG
source without downloading anything:
  python -B tests/build_tools/test_dependency_patches.py --jpeg-archive PATH
Only three CMake files are extracted into a disposable fixture directory.
"""

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[2]
JPEG_PATCH = Path('deps/+JPEG/0001-Add-lean-build-options.patch')
JPEG_FILES = ('CMakeLists.txt', 'sharedlib/CMakeLists.txt',
              'cmakescripts/BuildPackages.cmake')


def git(directory, *args, check=True):
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith('GIT_'):
            del env[key]
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0')
    result = subprocess.run(
        ['git', '-c', 'safe.directory=' + Path(directory).resolve().as_posix(),
         '-c', 'core.autocrlf=true', '-c', 'core.safecrlf=false', *args],
        cwd=directory, env=env, capture_output=True, text=True, timeout=20,
    )
    if check and result.returncode:
        raise AssertionError(f'git {args!r} failed: {result.stdout}{result.stderr}')
    return result


def apply_and_reverse_check(directory, patch):
    options = ('--ignore-space-change', '--whitespace=fix')
    git(directory, 'apply', '--check', *options, str(patch))
    git(directory, 'apply', *options, str(patch))
    git(directory, 'apply', '--reverse', '--check', *options, str(patch))


def jpeg_before_images(patch):
    """Reconstruct hunk context, not an independent copy of upstream source.

    This catches framing/count regressions offline; the optional archive check
    below independently verifies actual pinned source contents and applicability.
    """
    files = {}
    current = None
    lines = patch.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith('--- a/'):
            current = files.setdefault(line[6:], {})
        elif line.startswith('@@ '):
            match = re.match(r'@@ -(\d+),(\d+) \+(\d+),(\d+) @@', line)
            if match is None or current is None:
                raise AssertionError(f'Unsupported JPEG hunk: {line!r}')
            old_start, old_count, _, new_count = map(int, match.groups())
            old_seen = new_seen = 0
            index += 1
            while index < len(lines) and not lines[index].startswith(('@@ ', '--- a/')):
                body = lines[index]
                if not body or body[0] not in ' +-':
                    raise AssertionError('Every JPEG context line requires its diff prefix')
                if body[0] != '+':
                    position = old_start - 1 + old_seen
                    if position in current and current[position] != body[1:]:
                        raise AssertionError('Overlapping JPEG hunk contexts disagree')
                    current[position] = body[1:]
                    old_seen += 1
                if body[0] != '-':
                    new_seen += 1
                index += 1
            if (old_seen, new_seen) != (old_count, new_count):
                raise AssertionError(f'Incorrect JPEG hunk counts: {line!r}')
            continue
        index += 1
    if set(files) != set(JPEG_FILES):
        raise AssertionError(f'Unexpected JPEG patch targets: {sorted(files)}')
    return {name: ''.join(entries.get(i, '# Untouched fixture line') + '\n'
                         for i in range(max(entries) + 1))
            for name, entries in files.items()}


def check_jpeg_archive(archive):
    """Check the recipe's pinned archive, without extracting or building it."""
    recipe = (ROOT / 'deps/+JPEG/JPEG.cmake').read_text(encoding='utf-8')
    expected = re.search(r'URL_HASH SHA256=([0-9a-f]{64})', recipe).group(1)
    digest = hashlib.sha256()
    with archive.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise AssertionError('JPEG archive does not match the pinned recipe SHA256')
    with tempfile.TemporaryDirectory(prefix='jpeg-patch-archive-') as temporary:
        scratch = Path(temporary)
        with zipfile.ZipFile(archive) as source:
            for name in JPEG_FILES:
                member = source.getinfo('libjpeg-turbo-3.0.1/' + name)
                if member.file_size > 1024 * 1024:
                    raise AssertionError('Unexpectedly large JPEG CMake fixture')
                target = scratch / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read(member))
        apply_and_reverse_check(scratch, ROOT / JPEG_PATCH)
    print('PASS: JPEG patch applies to all three SHA256-verified pinned source files')


class DependencyPatchTests(unittest.TestCase):
    def test_all_dependency_patches_survive_clean_windows_checkout(self):
        paths = git(ROOT, 'ls-files', '--', 'deps/*.patch', 'deps/**/*.patch').stdout.splitlines()
        self.assertIn(JPEG_PATCH.as_posix(), paths)
        with tempfile.TemporaryDirectory(prefix='dependency-patch-checkout-') as temporary:
            scratch = Path(temporary)
            repository, checkout = scratch / 'repository', scratch / 'checkout'
            repository.mkdir()
            checkout.mkdir()
            shutil.copyfile(ROOT / '.gitattributes', repository / '.gitattributes')
            for name in paths:
                target = repository / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / name).read_bytes())
            # This is a disposable index; the workspace Git index is never changed.
            git(repository, 'init', '--quiet', '--template=')
            git(repository, 'add', '--all')
            git(repository, 'checkout-index', '--all', '--prefix=' + checkout.as_posix() + '/')
            for name in paths:
                with self.subTest(patch=name):
                    patch = checkout / name
                    self.assertNotIn(b'\r\n', patch.read_bytes())
                    self.assertTrue(git(checkout, 'apply', '--numstat', str(patch)).stdout.strip())

    def test_jpeg_hunks_apply_and_reverse_without_compiling(self):
        patch = ROOT / JPEG_PATCH
        with tempfile.TemporaryDirectory(prefix='jpeg-patch-context-') as temporary:
            scratch = Path(temporary)
            for name, text in jpeg_before_images(patch.read_text(encoding='utf-8')).items():
                target = scratch / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(text.encode('utf-8'))
            apply_and_reverse_check(scratch, patch)

    def test_jpeg_hunk_count_regression_is_rejected(self):
        text = (ROOT / JPEG_PATCH).read_text(encoding='utf-8')
        broken = text.replace('@@ -149,15 +149,17 @@', '@@ -149,16 +149,18 @@')
        self.assertNotEqual(text, broken)
        with self.assertRaisesRegex(AssertionError, 'Incorrect JPEG hunk counts'):
            jpeg_before_images(broken)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--jpeg-archive', type=Path)
    args, remaining = parser.parse_known_args()
    if args.jpeg_archive:
        check_jpeg_archive(args.jpeg_archive.resolve(strict=True))
    unittest.main(argv=[__file__, *remaining])
