"""Tiny optional numerical fixtures; no repository meshes or native builds."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

AVAILABLE = all(importlib.util.find_spec(x) for x in ('numpy', 'scipy', 'trimesh', 'rtree'))
if AVAILABLE:
    sys.path.insert(0, str(Path(__file__).parents[1]))
    import numpy as np
    import bed_mesh_geometry as geometry
    import bed_mesh_certificates as exact


@unittest.skipUnless(AVAILABLE, 'Optional geometry dependencies')
class CertificateTests(unittest.TestCase):
    def fan(self, z=0):
        return geometry.Mesh([[0, 0, 0], [3, 0, 0], [0, 3, 0], [1, 1, z]],
                             [[3, 0, 1], [3, 1, 2], [3, 2, 0]])

    def test_exact_fan_preserves_boundary_and_surface(self):
        original = self.fan()
        candidate, proof = geometry.simplify_exact_planar(original)
        self.assertEqual(proof['proved_patches'], 1)
        self.assertEqual(len(candidate.faces), 1)
        self.assertEqual(geometry.mesh_stats(candidate)['surface_area_mm2'], 4.5)
        report = geometry.validate_candidate(original, candidate, sample_count=64, enhanced=True)
        self.assertTrue(report['accepted'], report['reasons'])
        self.assertEqual(report['self_intersection']['status'], 'unproven')

    def test_nearly_planar_is_not_claimed_exact(self):
        original = self.fan(2**-30)
        candidate, proof = geometry.simplify_exact_planar(original)
        self.assertEqual(proof['proved_patches'], 0)
        np.testing.assert_array_equal(candidate.faces, original.faces)

    def test_outside_point_winding_and_existing_outer_face_are_rejected(self):
        first = self.fan()
        outside = first.vertices.copy()
        outside[3] = [4, 4, 0]
        for vertices, faces in ((outside, first.faces), (first.vertices, first.faces[[0, 1]]),
                               (first.vertices, np.vstack([first.faces, [0, 2, 1]]))):
            with self.subTest(faces=faces.tolist()):
                out, report = exact.planar_convex_fans(vertices, faces)
                self.assertEqual(report['proved_patches'], 0)
                np.testing.assert_array_equal(out, faces)

    def test_exact_planar_budget_and_cancel(self):
        mesh = self.fan()
        _, report = exact.planar_convex_fans(mesh.vertices, mesh.faces, max_candidates=0)
        self.assertTrue(report['budget_exhausted'])
        def cancel():
            raise TimeoutError('stop')
        with self.assertRaises(TimeoutError):
            geometry.simplify_exact_planar(mesh, check_cancel=cancel)

    def test_convex_six_fan_exact_coverage_and_unchanged_boundary(self):
        ring = np.array([[2, 0, 0], [1, 2, 0], [-1, 2, 0], [-2, 0, 0], [-1, -2, 0], [1, -2, 0]])
        original = geometry.Mesh(np.vstack([ring, [0, 0, 0]]), [[6, i, (i+1) % 6] for i in range(6)])
        candidate, proof = geometry.simplify_exact_planar(original)
        self.assertEqual(proof['proved_patches'], 1)
        self.assertEqual(proof['removed_triangles'], 2)
        self.assertEqual(len(candidate.faces), 4)
        report = geometry.validate_candidate(original, candidate, sample_count=64, enhanced=True)
        self.assertTrue(report['accepted'], report['reasons'])
        before, after = geometry.mesh_stats(original), geometry.mesh_stats(candidate)
        self.assertEqual(before['surface_area_mm2'], after['surface_area_mm2'])
        self.assertEqual(before['boundary_edges'], after['boundary_edges'])

    def test_concave_or_self_crossing_fan_is_not_certified(self):
        ring = np.array([[2, 0, 0], [1, 2, 0], [-1, 2, 0], [-2, 0, 0], [-1, -2, 0], [1, -2, 0]])
        concave = ring.copy()
        concave[1] = [0, 0, 0]
        for vertices, order in ((concave, range(6)), (ring, [0, 2, 4, 1, 3, 5])):
            order = list(order)
            vertices = np.vstack([vertices, [0.25, 0.25, 0]])
            faces = np.array([[6, order[i], order[(i+1) % 6]] for i in range(6)])
            _, proof = exact.planar_convex_fans(vertices, faces)
            self.assertEqual(proof['proved_patches'], 0)

    def test_exact_intersection_and_close_nonintersection(self):
        horizontal = [[0, 0, 0], [2, 0, 0], [0, 2, 0]]
        crossing = [[0.5, 0.5, -1], [0.5, 0.5, 1], [1, 0.5, 0]]
        self.assertTrue(exact.triangles_intersect_exact(horizontal, crossing))
        self.assertFalse(exact.triangles_intersect_exact(horizontal, np.asarray(horizontal)+[0, 0, 2**-30]))
        self.assertTrue(exact.triangles_intersect_exact(horizontal, np.asarray(horizontal)+[0.25, 0.25, 0]))
        self.assertFalse(exact.triangles_intersect_exact(horizontal, np.asarray(horizontal)+[3, 0, 0]))

    def test_self_intersection_probe_finds_crossing_but_never_certifies_absence(self):
        mesh = geometry.Mesh([[0, 0, 0], [2, 0, 0], [0, 2, 0],
                              [0.5, 0.5, -1], [0.5, 0.5, 1], [1, 0.5, 0]], [[0, 1, 2], [3, 4, 5]])
        report = exact.intersection_probe(mesh)
        self.assertEqual(report['status'], 'intersection-found')
        self.assertFalse(report['global_absence_certified'])
        report = exact.intersection_probe(self.fan())
        self.assertEqual(report['status'], 'unproven')
        self.assertFalse(report['global_absence_certified'])

    def test_thin_opposing_components_cannot_swap_orientation(self):
        vertices = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=float)
        original = geometry.Mesh(np.vstack([vertices, vertices+[0, 0, 1e-5]]), [[0, 1, 2], [3, 5, 4]])
        candidate = geometry.Mesh(original.vertices, original.faces[:, ::-1])
        # Global nearby oriented sheets alone would allow exchanging roles.
        report = geometry.validate_candidate(original, candidate, sample_count=64, enhanced=True)
        self.assertFalse(report['accepted'])
        self.assertIn('sampled surface winding reversed', report['reasons'])

    def test_identity_with_thin_sheets_passes_component_aware_check(self):
        vertices = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=float)
        original = geometry.Mesh(np.vstack([vertices, vertices+[0, 0, 1e-5]]), [[0, 1, 2], [3, 5, 4]])
        report = geometry.validate_candidate(original, original, sample_count=64, enhanced=True)
        self.assertTrue(report['accepted'], report['reasons'])

    def test_feature_check_detects_lost_boundary_or_crease(self):
        import trimesh
        cube = trimesh.creation.box()
        closed = geometry.Mesh(cube.vertices, cube.faces)
        open_surface = geometry.Mesh(cube.vertices, cube.faces[:-1])
        result = geometry._feature_distance(closed, open_surface)
        self.assertGreater(result['maximum_distance_mm'], 0.05)

    def test_feature_check_handles_more_than_old_edge_cutoff_with_same_samples(self):
        # Repeated finite segments exercise the bounded numerical work, without
        # constructing a large asset or weakening the distance/sampling test.
        segments = np.tile(np.array([[[0, 0, 0], [1, 0, 0]]], dtype=float), (32769, 1, 1))
        with patch.object(geometry, '_feature_segments', return_value=segments):
            result = geometry._feature_distance(self.fan(), self.fan())
        self.assertEqual(result['status'], 'sampled')
        self.assertEqual(result['completed_sample_points'], 192)
        self.assertEqual(result['segment_comparisons'], 192*32769)
        self.assertEqual(result['maximum_distance_mm'], 0)

    def test_feature_work_budget_and_deadline_remain_explicitly_unproven(self):
        result = geometry._feature_distance(self.fan(), self.fan(), max_comparisons=1)
        self.assertEqual(result['status'], 'unproven-budget')
        self.assertFalse(result['global_coverage_certified'])
        with self.assertRaises(TimeoutError):
            geometry._feature_distance(self.fan(), self.fan(), check_cancel=lambda:
                (_ for _ in ()).throw(TimeoutError('fixture deadline')))

    def test_feature_rejection_happens_before_expensive_surface_proximity(self):
        with patch.object(geometry, '_feature_distance', return_value={
                'status': 'sampled', 'maximum_distance_mm': 1.0}), \
             patch.object(geometry, '_surface_distance') as surface:
            report = geometry.validate_candidate(self.fan(), self.fan(), sample_count=64, enhanced=True)
        surface.assert_not_called()
        self.assertFalse(report['accepted'])
        self.assertEqual(report['last_phase'], 'feature-distance')
        self.assertIn('feature-distance', report['timings_seconds'])

    def test_validation_timeout_carries_partial_metrics_and_substage_timing(self):
        with patch.object(geometry, '_surface_distance', side_effect=TimeoutError('fixture deadline')):
            with self.assertRaises(TimeoutError) as raised:
                geometry.validate_candidate(self.fan(), self.fan(), sample_count=64, enhanced=True)
        report = raised.exception.validation_report
        self.assertFalse(report['accepted'])
        self.assertIn('feature_check', report['metrics'])
        self.assertIn('surface-original-to-candidate', report['timings_seconds'])


if __name__ == '__main__':
    unittest.main()
