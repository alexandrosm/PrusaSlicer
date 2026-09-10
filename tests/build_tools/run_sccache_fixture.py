"""Tiny native compiler-cache proof; invoke only inside the resource guard."""
import argparse
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--cmake', required=True)
parser.add_argument('--ninja', required=True)
parser.add_argument('--sccache', required=True)
parser.add_argument('--build', required=True, type=Path)
args = parser.parse_args()
repository = Path(__file__).resolve().parents[2]
build = args.build.resolve()
subprocess.run([args.cmake, '-S', str(repository / 'tests/build_tools/sccache_fixture'), '-B', str(build),
                '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release', '-DPRUSA_SOURCE=' + str(repository),
                '-DCMAKE_MAKE_PROGRAM=' + args.ninja, '-DSLIC3R_COMPILER_CACHE=' + args.sccache], check=True)
# Force one compile edge on the second invocation, without a recursive clean or
# changing source/flags. Each independent supervisor uses the same object path.
object_file = build / 'CMakeFiles/cache_probe.dir/cache_probe.cpp.obj'
if object_file.exists():
    object_file.unlink()
subprocess.run([args.cmake, '--build', str(build), '--target', 'cache_probe', '--parallel', '1', '--verbose'], check=True)
if not object_file.is_file():
    raise RuntimeError('Tiny cache fixture did not produce its expected object')
