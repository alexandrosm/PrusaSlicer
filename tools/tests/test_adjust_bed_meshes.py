"""Bed-stage workflow tests using a tiny, deterministic fake geometry engine.

No optional geometry package, native compiler, or compressor is invoked.
"""

import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


TOOLS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('adjust_bed_meshes_under_test', TOOLS / 'adjust_bed_meshes.py')
adjuster = importlib.util.module_from_spec(SPEC)
with patch.object(sys, 'path', [str(TOOLS), *sys.path]):
    SPEC.loader.exec_module(adjuster)


class FakeMesh:
    def __init__(self, count, area, name, loaded_from=None):
        self.faces = range(count)
        self.area = area
        self.name = name
        self.loaded_from = loaded_from


def fixture_mesh_bytes(count, area=100.0, marker=b'O'):
    # Binary-STL-sized bytes with a private fake header. Geometry is never
    # inferred from these bytes by anything except the fake engine below.
    return struct.pack('<Id', count, area).ljust(84, b' ') + marker * (50 * count)


class AdjustmentTargetTests(unittest.TestCase):
    def test_floor_then_halfway_backoff_arithmetic(self):
        self.assertEqual(adjuster.adjustment_targets(120, 0.529, 100, 3), [52, 86, 103, 112])

    def test_minimum_four_and_no_duplicate_or_original_targets(self):
        self.assertEqual(adjuster.adjustment_targets(5, 0, 100, 8), [4])
        self.assertEqual(adjuster.adjustment_targets(120, 0, 100, 0), [4])
        self.assertEqual(adjuster.adjustment_targets(3, 0, 100, 8), [])

    def test_below_or_equal_target_is_unchanged(self):
        self.assertEqual(adjuster.adjustment_targets(24, 0.24, 100, 3), [])
        self.assertEqual(adjuster.adjustment_targets(12, 0.24, 100, 3), [])


class AdjustmentWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='prusaslicer-adjust-tests-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'PrusaSlicer portable'
        self.source.mkdir()
        self.output = self.root / 'adjusted'
        for name, data in {
            'prusa-slicer.exe': b'MZ GUI fixture',
            'prusa-slicer-console.exe': b'MZ console fixture',
            'PrusaSlicer.dll': b'MZ application fixture',
            'tbb12.dll': b'MZ dependency fixture',
            'LICENSE': b'Keep licensing notices',
            'resources/fonts/NotoSansCJK-Regular.ttc': b'complete CJK font fixture',
            'resources/profiles/vendor.ini': b'printer_model = fixture\n',
            'resources/localization/日本語.mo': b'compiled Unicode translation',
            'resources/shapes/keep.stl': b'not a bed: never send to the engine',
        }.items():
            destination = self.source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        (self.source / 'resources/empty/空').mkdir(parents=True)
        self.beds = {}
        for name, count in (('low.stl', 12), ('middle.stl', 24), ('dense.stl', 120)):
            self.add_bed(name, count)
        self.geometry = ModuleType('bed_mesh_geometry')
        self.geometry.__file__ = __file__  # A real, small file for tool provenance hashing.
        self.geometry.read_stl = Mock(side_effect=self.read_mesh)
        self.geometry.mesh_stats = Mock(side_effect=lambda mesh: {
            'triangles': len(mesh.faces), 'surface_area_mm2': mesh.area})
        self.geometry.simplify = Mock(side_effect=self.simplify_mesh)
        self.geometry.write_stl = Mock(side_effect=self.write_mesh)
        self.geometry.validate_candidate = Mock(return_value={'accepted': True, 'reasons': []})
        context = patch.dict(sys.modules, {'bed_mesh_geometry': self.geometry})
        context.start()
        self.addCleanup(context.stop)
        context = patch.object(adjuster.importlib.metadata, 'version', return_value='fixture-version')
        self.versions = context.start()
        self.addCleanup(context.stop)
        context = patch.object(adjuster, 'print')
        context.start()
        self.addCleanup(context.stop)

    def add_bed(self, name, count, area=100):
        relative = 'resources/profiles/Vendor/' + name
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fixture_mesh_bytes(count, area))
        self.beds[name] = relative

    def read_mesh(self, path, allow_attributes_for_audit=False):
        count, area = struct.unpack_from('<Id', Path(path).read_bytes())
        return FakeMesh(count, area, Path(path).name, Path(path))

    def write_mesh(self, mesh, path):
        Path(path).write_bytes(fixture_mesh_bytes(len(mesh.faces), mesh.area, b'C'))

    def simplify_mesh(self, mesh, wanted, *, backend):
        # Required keyword plus assertion makes an omitted or unintended
        # backend an orchestration failure, never an accidental fake success.
        self.assertEqual(backend, 'pymeshlab')
        return FakeMesh(wanted, mesh.area, mesh.name)

    def run_adjustment(self, **kwargs):
        kwargs.setdefault('search', 'density-backoff')  # Retain regression coverage of the historical strategy.
        return adjuster.adjust(self.source, self.output, **kwargs)

    def assert_not_published(self):
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.with_name(self.output.name + '.json').exists())
        self.assertFalse(list(self.output.parent.glob('.prusaslicer-bed-adjust-*')))

    def write_source_report(self):
        report = self.root / 'source-report.json'
        report.write_text(json.dumps({'payload': adjuster.inventory(self.source)}), encoding='utf-8')
        return report

    def test_complete_clone_changes_only_accepted_bed_and_accounts_exact_bytes(self):
        before = adjuster.inventory(self.source)
        report_path = self.write_source_report()
        report = self.run_adjustment(source_report=report_path)
        clone = self.output / self.source.name
        self.assertEqual(report['settings']['target_density'], 0.52)
        self.assertEqual(report['changed_files'], [self.beds['dense.stl']])
        self.assertEqual(report['bed_count'], 3)
        self.assertEqual(report['raw_bytes_saved'], (120 - 52) * 50)
        self.assertEqual(report['source_uncompressed_bytes'], sum(f['bytes'] for f in before['files'].values()))
        self.assertEqual(report['uncompressed_bytes'], report['source_uncompressed_bytes'] - 3400)
        self.assertEqual(report['source_report']['sha256'], adjuster.sha256(report_path))
        self.assertEqual(report['payload'], adjuster.inventory(clone))
        self.assertEqual(report['source_payload'], before)
        self.assertEqual(Path(report['package_root']), clone)
        self.assertEqual(adjuster.inventory(self.source), before)
        self.assertEqual(report['payload']['directories'], before['directories'])
        self.assertTrue((clone / 'resources/empty/空').is_dir())
        for name in before['files']:
            if name != self.beds['dense.stl']:
                self.assertEqual((clone / name).read_bytes(), (self.source / name).read_bytes(), name)
        published = json.loads(self.output.with_name(self.output.name + '.json').read_text(encoding='utf-8'))
        self.assertEqual(published, report)
        self.assertEqual(set(report['dependencies'].values()), {'fixture-version'})
        self.assertEqual(set(report['dependencies']), {
            'numpy', 'scipy', 'pymeshlab', 'trimesh', 'rtree'})
        self.assertEqual(set(report['tool_sha256']), {
            'adjust_bed_meshes.py', Path(__file__).name, 'mesh-stage-requirements.txt',
            'bed_mesh_cache.py', 'bed_mesh_certificates.py', 'bed_mesh_spatial.py'})

    def test_mean_and_median_use_original_per_file_density(self):
        expected = (('mean', 0.52, 52), ('median', 0.24, 24))
        for statistic, density, count in expected:
            with self.subTest(statistic=statistic):
                self.output = self.root / statistic
                report = self.run_adjustment(statistic=statistic)
                row = next(r for r in report['beds'] if r['path'] == self.beds['dense.stl'])
                self.assertEqual(report['settings']['target_density'], density)
                self.assertEqual(row['target_triangles'], count)
                self.assertEqual(row['after_triangles'], count)

    def test_below_target_files_keep_original_bytes_without_simplification(self):
        report = self.run_adjustment()
        below = [row for row in report['beds'] if row['status'] == 'below-target']
        self.assertEqual({row['path'] for row in below}, {self.beds['low.stl'], self.beds['middle.stl']})
        self.assertTrue(all(row['before'] == row['after'] and not row['attempts'] for row in below))
        self.assertEqual(self.geometry.simplify.call_count, 1)

    def test_validation_rejection_backs_off_until_accepted(self):
        self.geometry.validate_candidate.side_effect = [
            {'accepted': False, 'reasons': ['distance']},
            {'accepted': False, 'reasons': ['boundary']},
            {'accepted': True, 'reasons': []},
        ]
        report = self.run_adjustment()
        row = next(row for row in report['beds'] if row['status'] == 'adjusted')
        self.assertEqual([call.args[1] for call in self.geometry.simplify.call_args_list], [52, 86, 103])
        self.assertTrue(all(call.kwargs == {'backend': 'pymeshlab'}
                            for call in self.geometry.simplify.call_args_list))
        self.assertEqual([a['requested_triangles'] for a in row['attempts']], [52, 86, 103])
        self.assertEqual(row['after_triangles'], 103)
        self.assertEqual(report['raw_bytes_saved'], 850)

    def test_unsuitable_original_value_errors_retain_original_with_reasons(self):
        before = adjuster.inventory(self.source)
        self.geometry.simplify.side_effect = ValueError('unsupported original topology')
        report = self.run_adjustment()
        row = next(row for row in report['beds'] if row['status'] == 'retained-original')
        self.assertEqual([a['requested_triangles'] for a in row['attempts']], [52, 86, 103, 112])
        self.assertTrue(all(a['validation'] == {
            'accepted': False, 'reasons': ['unsupported original topology']} for a in row['attempts']))
        self.assertEqual(report['payload'], before)
        self.assertEqual(report['raw_bytes_saved'], 0)
        self.geometry.validate_candidate.assert_not_called()

    def test_candidate_write_value_error_backs_off_without_publishing_failed_proposal(self):
        def write(mesh, path):
            if len(mesh.faces) == 52:
                raise ValueError('degenerate proposal')
            self.write_mesh(mesh, path)
        self.geometry.write_stl.side_effect = write
        report = self.run_adjustment()
        row = next(row for row in report['beds'] if row['status'] == 'adjusted')
        self.assertEqual([a['requested_triangles'] for a in row['attempts']], [52, 86])
        self.assertEqual(row['attempts'][0]['validation']['reasons'], ['degenerate proposal'])
        self.assertEqual(row['after_triangles'], 86)
        self.assertEqual(report['raw_bytes_saved'], (120 - 86) * 50)

    def test_serialized_proposal_value_error_backs_off_and_validates_next_proposal(self):
        def read(path, allow_attributes_for_audit=False):
            mesh = self.read_mesh(path, allow_attributes_for_audit=allow_attributes_for_audit)
            if 'replacements' in Path(path).parts and len(mesh.faces) == 52:
                raise ValueError('float32 serialization collapsed triangle')
            return mesh
        self.geometry.read_stl.side_effect = read
        report = self.run_adjustment()
        row = next(row for row in report['beds'] if row['status'] == 'adjusted')
        self.assertEqual(row['attempts'][0]['validation']['reasons'], ['float32 serialization collapsed triangle'])
        self.assertEqual(row['after_triangles'], 86)
        self.geometry.validate_candidate.assert_called_once()

    def test_all_validation_rejections_preserve_the_complete_original_payload(self):
        before = adjuster.inventory(self.source)
        self.geometry.validate_candidate.return_value = {'accepted': False, 'reasons': ['distance']}
        report = self.run_adjustment()
        self.assertEqual(report['changed_files'], [])
        self.assertEqual(report['raw_bytes_saved'], 0)
        self.assertEqual(report['payload'], before)
        row = next(row for row in report['beds'] if row['status'] == 'retained-original')
        self.assertEqual([a['requested_triangles'] for a in row['attempts']], [52, 86, 103, 112])

    def test_only_accepted_mesh_changes_when_another_candidate_is_rejected(self):
        self.add_bed('other-dense.stl', 80)
        self.geometry.validate_candidate.side_effect = lambda original, candidate, **kwargs: {
            'accepted': original.name == 'dense.stl', 'reasons': []}
        report = self.run_adjustment(backoffs=0)
        self.assertEqual(report['changed_files'], [self.beds['dense.stl']])
        other = self.beds['other-dense.stl']
        self.assertEqual(report['payload']['files'][other], report['source_payload']['files'][other])

    def test_validation_receives_serialized_candidate_and_requested_tolerances(self):
        self.run_adjustment(tolerance_mm=0.125, relative_limit=0.01, sample_count=64)
        original, candidate = self.geometry.validate_candidate.call_args.args
        self.assertEqual(original.loaded_from, self.source / self.beds['dense.stl'])
        self.assertIsNotNone(candidate.loaded_from)
        self.assertIn('replacements', candidate.loaded_from.parts)
        self.assertEqual(self.geometry.validate_candidate.call_args.kwargs,
                         {'tolerance_mm': 0.125, 'relative_limit': 0.01, 'sample_count': 64})
        for call in self.geometry.read_stl.call_args_list:
            self.assertEqual(call.kwargs, {'allow_attributes_for_audit': True})

    def test_valid_but_not_smaller_candidate_is_not_published_as_a_change(self):
        self.geometry.simplify.side_effect = lambda mesh, wanted, *, backend: FakeMesh(len(mesh.faces), mesh.area, mesh.name)
        report = self.run_adjustment(backoffs=0)
        self.assertEqual(report['changed_files'], [])
        self.assertEqual(report['raw_bytes_saved'], 0)
        row = next(row for row in report['beds'] if row['attempts'])
        self.assertFalse(row['attempts'][0]['smaller'])
        self.assertEqual(row['after'], row['before'])

    def test_mixed_attribute_rejection_retains_original_metadata_bytes(self):
        before = adjuster.inventory(self.source)
        original_bytes = (self.source / self.beds['dense.stl']).read_bytes()
        self.geometry.simplify.side_effect = ValueError('Mixed per-face attributes cannot be preserved')
        report = self.run_adjustment(backoffs=1)
        self.assertEqual(report['changed_files'], [])
        self.assertEqual(report['payload'], before)
        self.assertEqual((self.output / self.source.name / self.beds['dense.stl']).read_bytes(), original_bytes)
        row = next(row for row in report['beds'] if row['attempts'])
        self.assertEqual(len(row['attempts']), 2)
        self.assertTrue(all(attempt['validation']['accepted'] is False for attempt in row['attempts']))
        self.assertTrue(all('attributes' in attempt['validation']['reasons'][0] for attempt in row['attempts']))
        self.geometry.write_stl.assert_not_called()

    def test_existing_output_and_report_are_never_overwritten(self):
        for report_only in (False, True):
            with self.subTest(report_only=report_only):
                self.output = self.root / ('existing-report' if report_only else 'existing-output')
                marker = self.output.with_name(self.output.name + '.json') if report_only else self.output / 'keep'
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_bytes(b'preserve original')
                with self.assertRaisesRegex(ValueError, 'overwrite'):
                    self.run_adjustment()
                self.assertEqual(marker.read_bytes(), b'preserve original')
        self.geometry.read_stl.assert_not_called()

    def test_overlapping_source_and_output_are_rejected(self):
        for output in (self.source, self.source / 'nested', self.root):
            with self.subTest(output=output):
                with self.assertRaisesRegex(ValueError, 'overlap'):
                    adjuster.adjust(self.source, output)
        self.geometry.read_stl.assert_not_called()

    def test_reparse_ancestor_is_rejected_before_geometry_or_publication(self):
        real_lstat = Path.lstat

        def flagged(path, *args, **kwargs):
            if path == self.source:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return real_lstat(path, *args, **kwargs)

        with patch.object(Path, 'lstat', flagged):
            with self.assertRaisesRegex(ValueError, 'reparse'):
                self.run_adjustment()
        self.geometry.read_stl.assert_not_called()
        self.assert_not_published()

    def test_source_report_mismatch_is_rejected_before_creating_work_or_importing_engine(self):
        report = self.write_source_report()
        report.write_text(json.dumps({'payload': {'files': {}, 'directories': []}}), encoding='utf-8')
        self.output = self.root / 'not-created-parent' / 'adjusted'
        with patch.object(adjuster.tempfile, 'TemporaryDirectory') as workspace:
            with self.assertRaisesRegex(ValueError, 'does not match'):
                self.run_adjustment(source_report=report)
            workspace.assert_not_called()
        self.geometry.read_stl.assert_not_called()
        self.versions.assert_not_called()
        self.assertFalse(self.output.parent.exists())

    def test_source_mutation_aborts_without_publication(self):
        def mutate(original, candidate, **kwargs):
            (self.source / self.beds['dense.stl']).write_bytes(b'changed by another actor')
            return {'accepted': True, 'reasons': []}
        self.geometry.validate_candidate.side_effect = mutate
        with self.assertRaisesRegex(RuntimeError, 'Source changed'):
            self.run_adjustment()
        self.assert_not_published()

    def test_source_report_mutation_aborts_without_publication(self):
        report = self.write_source_report()

        def mutate(original, candidate, **kwargs):
            report.write_text('modified provenance', encoding='utf-8')
            return {'accepted': True, 'reasons': []}
        self.geometry.validate_candidate.side_effect = mutate
        with self.assertRaisesRegex(RuntimeError, 'Source report changed'):
            self.run_adjustment(source_report=report)
        self.assert_not_published()

    def test_unexpected_engine_failure_aborts_without_publication(self):
        self.geometry.simplify.side_effect = RuntimeError('engine failure')
        with self.assertRaisesRegex(RuntimeError, 'engine failure'):
            self.run_adjustment()
        self.assert_not_published()

    def test_nonfinite_metrics_fail_before_publication(self):
        self.geometry.validate_candidate.return_value = {'accepted': True, 'reasons': [], 'distance': float('nan')}
        with self.assertRaises(ValueError):
            self.run_adjustment()
        self.assert_not_published()

    def test_concurrent_report_is_preserved_without_publishing_our_clone(self):
        report_path = self.output.with_name(self.output.name + '.json')

        def concurrent_writer(original, candidate, **kwargs):
            report_path.write_bytes(b'another invocation owns this report')
            return {'accepted': True, 'reasons': []}
        self.geometry.validate_candidate.side_effect = concurrent_writer
        with self.assertRaises(FileExistsError):
            self.run_adjustment()
        self.assertEqual(report_path.read_bytes(), b'another invocation owns this report')
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob('.prusaslicer-bed-adjust-*')))

    def test_concurrent_output_is_preserved_and_our_reserved_report_is_removed(self):
        marker = self.output / 'another-invocation'

        def concurrent_writer(original, candidate, **kwargs):
            self.output.mkdir()
            marker.write_bytes(b'preserve unrelated output')
            return {'accepted': True, 'reasons': []}
        self.geometry.validate_candidate.side_effect = concurrent_writer
        with self.assertRaises(FileExistsError):
            self.run_adjustment()
        self.assertEqual(marker.read_bytes(), b'preserve unrelated output')
        self.assertEqual(list(self.output.iterdir()), [marker])
        self.assertFalse(self.output.with_name(self.output.name + '.json').exists())

    def test_rename_failure_removes_our_reserved_report(self):
        real_rename = Path.rename

        def failed_rename(path, target):
            if Path(target) == self.output / self.source.name:
                self.assertTrue(self.output.with_name(self.output.name + '.json').is_file())
                raise OSError('mock rename failure')
            return real_rename(path, target)
        with patch.object(Path, 'rename', failed_rename):
            with self.assertRaisesRegex(OSError, 'mock rename failure'):
                self.run_adjustment()
        self.assertFalse(self.output.with_name(self.output.name + '.json').exists())
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertFalse(list(self.root.glob('.prusaslicer-bed-adjust-*')))

    def test_report_write_failure_removes_incomplete_report_and_retains_clone_for_inspection(self):
        report_path = self.output.with_name(self.output.name + '.json')
        real_open = Path.open

        class FailingReport:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def write(self, text):
                self.stream.write(text[:16])
                raise OSError('mock disk full')

        def failing_open(path, *args, **kwargs):
            stream = real_open(path, *args, **kwargs)
            if path == report_path and args and args[0] == 'x':
                return FailingReport(stream)
            return stream
        before = adjuster.inventory(self.source)
        with patch.object(Path, 'open', failing_open):
            with self.assertRaisesRegex(OSError, 'mock disk full'):
                self.run_adjustment()
        self.assertFalse(report_path.exists())
        self.assertTrue((self.output / self.source.name / 'PrusaSlicer.dll').is_file())
        self.assertEqual(adjuster.inventory(self.source), before)

    def test_archive_uses_normalized_package_root_for_dot_and_parent_sources(self):
        nested = self.source / 'working-subdirectory'
        nested.mkdir()
        previous_directory = Path.cwd()
        for source_arg, cwd, label in (('.', self.source, 'dot'), ('..', nested, 'parent')):
            with self.subTest(source=source_arg):
                self.output = self.root / ('cli-' + label)
                archive = self.root / (label + '.7z')
                argv = ['adjust_bed_meshes.py', '--source', source_arg, '--output', str(self.output),
                        '--archive', str(archive), '--dictionary-mib', '32', '--compression-level', '7',
                        '--search', 'density-backoff']
                try:
                    os.chdir(cwd)
                    with patch.object(sys, 'argv', argv), \
                         patch.object(adjuster, 'package', return_value={'archive_bytes': 123}) as package:
                        adjuster.main()
                        package.assert_called_once_with(self.output / self.source.name, archive, None, 32, 7)
                finally:
                    os.chdir(previous_directory)
                report = json.loads(self.output.with_name(self.output.name + '.json').read_text(encoding='utf-8'))
                self.assertEqual(Path(report['package_root']), self.output / self.source.name)
                self.assertTrue(Path(report['package_root']).is_dir())

    def test_archive_destinations_must_be_outside_both_stages(self):
        for archive in (self.source / 'bad.7z', self.output / 'bad.7z', self.output / self.source.name / 'bad.7z'):
            with self.subTest(archive=archive), self.assertRaisesRegex(ValueError, 'outside'):
                adjuster.archive_destination(self.source, self.output, archive)
        good = self.root / 'good.7z'
        self.assertEqual(adjuster.archive_destination(self.source, self.output, good), good)
        self.assert_not_published()

    def test_archive_rejects_existing_archive_report_bad_extension_and_provenance_conflict(self):
        for report_only in (False, True):
            archive = self.root / ('report-only.7z' if report_only else 'existing.7z')
            marker = archive.with_suffix('.7z.json') if report_only else archive
            marker.write_bytes(b'preserve archive or report')
            with self.assertRaisesRegex(ValueError, 'overwrite'):
                adjuster.archive_destination(self.source, self.output, archive)
            self.assertEqual(marker.read_bytes(), b'preserve archive or report')
        with self.assertRaisesRegex(ValueError, 'extension'):
            adjuster.archive_destination(self.source, self.output, self.root / 'bad.zip')
        archive = self.root / 'provenance.7z'
        with self.assertRaisesRegex(ValueError, 'provenance report'):
            adjuster.archive_destination(self.source, self.output, archive, archive.with_suffix('.7z.json'))

    def test_archive_reparse_ancestor_is_rejected(self):
        folder = self.root / 'archive-folder'
        folder.mkdir()
        real_lstat = Path.lstat

        def flagged(path, *args, **kwargs):
            if path == folder:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return real_lstat(path, *args, **kwargs)
        with patch.object(Path, 'lstat', flagged):
            with self.assertRaisesRegex(ValueError, 'reparse'):
                adjuster.archive_destination(self.source, self.output, folder / 'bad.7z')

    def test_main_invalid_archive_is_rejected_before_adjustment_or_packaging(self):
        for archive in (self.source / 'bad.7z', self.output / 'bad.7z', self.root / 'bad.zip'):
            with self.subTest(archive=archive):
                argv = ['adjust_bed_meshes.py', '--source', str(self.source), '--output', str(self.output),
                        '--archive', str(archive)]
                with patch.object(sys, 'argv', argv), patch.object(sys, 'stderr', io.StringIO()), \
                     patch.object(adjuster, 'adjust') as adjustment, patch.object(adjuster, 'package') as package:
                    with self.assertRaises(SystemExit) as failure:
                        adjuster.main()
                    self.assertEqual(failure.exception.code, 1)
                    adjustment.assert_not_called()
                    package.assert_not_called()
        self.assert_not_published()

    def test_deterministic_payload_and_report_except_elapsed_time(self):
        first = self.run_adjustment()
        self.output = self.root / 'second-run'
        second = self.run_adjustment()
        first.pop('seconds_before_publication')
        second.pop('seconds_before_publication')
        first_root = Path(first.pop('package_root'))
        second_root = Path(second.pop('package_root'))
        self.assertEqual(first_root.name, self.source.name)
        self.assertEqual(second_root, self.output / self.source.name)
        self.assertNotEqual(first_root, second_root)
        self.assertEqual(first, second)

    def test_invalid_options_are_rejected_before_geometry_or_writes(self):
        options = ({'statistic': 'weighted'}, {'tolerance_mm': 0}, {'tolerance_mm': float('nan')},
                   {'relative_limit': 1}, {'relative_limit': float('inf')}, {'sample_count': 31},
                   {'sample_count': True}, {'backoffs': 9}, {'backoffs': True})
        for settings in options:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.run_adjustment(**settings)
        self.geometry.read_stl.assert_not_called()
        self.assert_not_published()

    def adaptive(self, **kwargs):
        kwargs.update(search='adaptive', exact_planar=False)
        kwargs.setdefault('seed_divisor', 2)  # Stable historical bracket fixture.
        with patch.object(adjuster, 'compression_proxy', side_effect=lambda p, check: Path(p).stat().st_size):
            return self.run_adjustment(**kwargs)

    def test_adaptive_density_is_not_eligibility_gate(self):
        self.add_bed('large-low-density.stl', 200, area=100000)
        report = self.adaptive(max_attempts_per_asset=1)
        row = next(r for r in report['beds'] if r['path'] == self.beds['large-low-density.stl'])
        self.assertLess(row['density'], report['settings']['target_density'])
        self.assertEqual(row['target_triangles'], 100)
        self.assertEqual(row['status'], 'adjusted')

    def test_default_seed_is_one_eighth_of_this_asset_not_collection_density(self):
        with patch.object(adjuster, 'compression_proxy', side_effect=lambda p, check: Path(p).stat().st_size):
            report = self.run_adjustment(search='adaptive', exact_planar=False, max_attempts_per_asset=1)
        row = next(r for r in report['beds'] if r['path'] == self.beds['dense.stl'])
        self.assertEqual(row['target_triangles'], 15)
        self.assertEqual(report['asset_policy']['seed_divisor'], 8)

    def test_adaptive_refines_first_success_without_relaxing_qa(self):
        self.geometry.validate_candidate.side_effect = lambda a, b, **kw: {
            'accepted': len(b.faces) >= 48, 'reasons': [] if len(b.faces) >= 48 else ['distance']}
        report = self.adaptive()
        row = next(r for r in report['beds'] if r['status'] == 'adjusted')
        self.assertEqual(row['after_triangles'], 48)
        self.assertEqual([a['requested_triangles'] for a in row['attempts']], [60, 30, 45, 52, 48, 46])
        self.assertTrue(all(c.kwargs['enhanced'] for c in self.geometry.validate_candidate.call_args_list))
        self.assertTrue(all(c.kwargs['tolerance_mm'] == 0.05 for c in self.geometry.validate_candidate.call_args_list))

    def test_compression_ranking_can_prefer_more_triangles(self):
        def proxy(path, check):
            count = self.read_mesh(path).faces
            return 1 if len(count) == 60 else 100 + len(count)
        with patch.object(adjuster, 'compression_proxy', side_effect=proxy):
            report = self.run_adjustment(search='adaptive', exact_planar=False, seed_divisor=2)
        row = next(r for r in report['beds'] if r['status'] == 'adjusted')
        self.assertEqual(row['after_triangles'], 60)
        self.assertGreater(len(row['attempts']), 1)
        self.assertLessEqual(len(row['shortlist']), 3)

    def test_proxy_regression_retains_original_even_when_raw_smaller(self):
        def proxy(path, check):
            return 1 if Path(path).is_relative_to(self.source) else 100
        with patch.object(adjuster, 'compression_proxy', side_effect=proxy):
            report = self.run_adjustment(search='adaptive', exact_planar=False)
        self.assertEqual(report['changed_files'], [])
        self.assertTrue(any(r['shortlist'] for r in report['beds']))

    def test_identical_candidate_bytes_reuse_only_same_asset_validated_qa(self):
        self.geometry.simplify.side_effect = lambda mesh, wanted, backend: FakeMesh(50, mesh.area, mesh.name)
        report = self.adaptive()
        row = next(r for r in report['beds'] if r['status'] == 'adjusted')
        self.assertEqual(self.geometry.validate_candidate.call_count, 1)
        self.assertEqual(len(row['shortlist']), 1)
        self.assertTrue(all(a.get('duplicate_candidate_qa_reused') for a in row['attempts'][1:]))

    def test_budget_exhaustion_keeps_last_independently_validated_winner(self):
        calls = 0
        def proposal(mesh, wanted, *, backend):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise adjuster.BudgetExceeded('fixture budget exhausted')
            return FakeMesh(wanted, mesh.area, mesh.name)
        self.geometry.simplify.side_effect = proposal
        report = self.adaptive()
        row = next(r for r in report['beds'] if r['status'] == 'adjusted')
        self.assertEqual(row['after_triangles'], 60)
        self.assertFalse(row['search_complete'])
        self.assertIn('budget', row['budget_exhausted'])
        self.assertFalse(report['search_complete'])
        self.assertEqual(report['incomplete_assets'], [self.beds['dense.stl']])

    def test_require_complete_rejects_publication_and_reuses_completed_cache_on_retry(self):
        cache = self.root / 'cache'
        self.geometry.simplify.side_effect = adjuster.BudgetExceeded('fixture stop')
        with self.assertRaisesRegex(RuntimeError, 'Incomplete bounded searches'):
            self.adaptive(asset_cache=cache, require_complete=True)
        self.assert_not_published()
        self.assertTrue(list(cache.glob('*/record.json')))
        diagnostics = list(self.output.parent.glob('adjusted.incomplete-*.json'))
        self.assertEqual(len(diagnostics), 1)
        diagnostic = json.loads(diagnostics[0].read_text(encoding='utf-8'))
        self.assertFalse(diagnostic['verified_stage'])
        self.assertFalse(diagnostic['search_complete'])
        self.assertNotIn('payload', diagnostic)
        self.assertNotIn('package_root', diagnostic)
        row = next(r for r in diagnostic['beds'] if r['path'] == self.beds['dense.stl'])
        self.assertEqual(row['attempts'][0]['phase'], 'generation')
        self.assertIn('generation', row['attempts'][0]['timings_seconds'])
        self.geometry.simplify.side_effect = self.simplify_mesh
        result = self.adaptive(asset_cache=cache, require_complete=True, asset_seconds=30)
        self.assertTrue(result['search_complete'])
        self.assertTrue(all(r.get('cache_hit') for r in result['beds'] if r['original']['triangles'] < 64))
        self.assertEqual(json.loads(diagnostics[0].read_text(encoding='utf-8')), diagnostic)

    def test_attempt_timings_cover_generation_serialized_geometry_qa_and_proxy(self):
        report = self.adaptive(max_attempts_per_asset=1, asset_seconds=300)
        row = next(r for r in report['beds'] if r['status'] == 'adjusted')
        attempt = row['attempts'][0]
        self.assertEqual(attempt['phase'], 'complete')
        self.assertEqual(set(attempt['timings_seconds']),
            {'generation', 'serialization', 'serialized-read', 'validation', 'compression-proxy'})
        self.assertTrue(all(value >= 0 for value in attempt['timings_seconds'].values()))

    def test_adding_unrelated_bed_does_not_invalidate_completed_asset_results(self):
        cache = self.root / 'cache'
        first = self.adaptive(asset_cache=cache)
        old_names = {r['path'] for r in first['beds']}
        self.add_bed('new-low-density.stl', 12, area=100000)
        self.output = self.root / 'new-bed'
        second = self.adaptive(asset_cache=cache, asset_seconds=30)
        self.assertTrue(all(r.get('cache_hit') for r in second['beds'] if r['path'] in old_names))
        for name in old_names:
            self.assertEqual(first['payload']['files'][name], second['payload']['files'][name])

    def test_asset_cache_hits_skip_audit_and_simplification(self):
        cache = self.root / 'asset-cache'
        first = self.adaptive(asset_cache=cache)
        self.output = self.root / 'cache-hit'
        self.geometry.read_stl.reset_mock()
        self.geometry.simplify.reset_mock()
        second = self.adaptive(asset_cache=cache)
        self.geometry.read_stl.assert_not_called()
        self.geometry.simplify.assert_not_called()
        self.assertEqual(first['payload'], second['payload'])
        self.assertGreater(second['cache']['hits'], 0)

    def test_cache_payload_corruption_recomputes_without_overwriting_cache(self):
        cache = self.root / 'asset-cache'
        first = self.adaptive(asset_cache=cache)
        payload = next(cache.glob('*/candidate.stl'))
        payload.write_bytes(b'corrupt-cache')
        self.output = self.root / 'after-corruption'
        second = self.adaptive(asset_cache=cache)
        self.assertEqual(first['payload'], second['payload'])
        self.assertGreater(second['cache']['invalid'], 0)
        self.assertEqual(payload.read_bytes(), b'corrupt-cache')

    def test_cache_policy_change_invalidates_result_but_can_reuse_audit(self):
        cache = self.root / 'asset-cache'
        self.adaptive(asset_cache=cache)
        self.output = self.root / 'new-policy'
        self.geometry.simplify.reset_mock()
        report = self.adaptive(asset_cache=cache, tolerance_mm=0.01)
        self.assertTrue(self.geometry.simplify.called)
        self.assertGreater(report['cache']['hits'], 0)

    def test_cache_record_semantic_mismatch_is_rejected_even_with_matching_checksum(self):
        cache = self.root / 'cache'
        first = self.adaptive(asset_cache=cache)
        record = next(p for p in cache.glob('*/record.json')
                      if json.loads(p.read_bytes())['body'].get('selected_payload'))
        data = json.loads(record.read_bytes())
        data['body']['row']['search_complete'] = False
        data['body_sha256'] = adjuster.hashlib.sha256(json.dumps(data['body'], sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        record.write_text(json.dumps(data))
        self.output = self.root / 'semantic-mismatch'
        second = self.adaptive(asset_cache=cache)
        self.assertEqual(first['payload'], second['payload'])
        self.assertGreater(second['cache']['invalid'], 0)

    def test_cache_root_existing_file_is_rejected_before_work(self):
        cache = self.root / 'not-a-directory'
        cache.write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, 'must be a directory'):
            self.adaptive(asset_cache=cache)
        self.geometry.read_stl.assert_not_called()
        self.assertEqual(cache.read_bytes(), b'keep')

    def test_cache_overlap_is_rejected_before_work(self):
        for cache in (self.source, self.source / 'cache', self.output, self.root):
            with self.subTest(cache=cache), self.assertRaisesRegex(ValueError, 'cache must not overlap'):
                self.adaptive(asset_cache=cache)
        self.geometry.read_stl.assert_not_called()

    def test_all_adaptive_limits_validate_before_source_work(self):
        for kwargs in ({'search': 'unknown'}, {'max_attempts_per_asset': 13},
                       {'asset_seconds': float('inf')}, {'asset_seconds': 301}, {'seed_divisor': 1},
                       {'seed_divisor': True}, {'time_budget_seconds': 0}, {'minimum_triangles': 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_adjustment(**kwargs)
        self.geometry.read_stl.assert_not_called()


if __name__ == '__main__':
    unittest.main()
