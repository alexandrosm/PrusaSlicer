"""Small parity fixtures; optional outside the isolated mesh-stage environment."""
from dataclasses import FrozenInstanceError
from pathlib import Path
import sys
import unittest
from unittest import mock

try:
    import numpy as np
    import scipy.spatial
    import trimesh
    import rtree  # noqa: F401
    OPTIONAL_AVAILABLE = trimesh.__version__ == "4.12.2"
except ImportError:
    OPTIONAL_AVAILABLE = False

if OPTIONAL_AVAILABLE:
    sys.path.insert(0, str(Path(__file__).parents[1]))
    import bed_mesh_spatial as spatial


@unittest.skipUnless(OPTIONAL_AVAILABLE, "Pinned optional mesh-stage dependencies unavailable")
class SpatialQueryTests(unittest.TestCase):
    def tetrahedron(self):
        return np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=float), np.array(
            [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]])

    def assert_parity(self, vertices, faces, points):
        reference = trimesh.Trimesh(vertices=vertices, faces=faces, process=False, validate=False)
        query = spatial.SpatialQuery(vertices, faces)
        expected = trimesh.proximity.closest_point(reference, points)
        for actual, wanted in zip(query.closest_point(points), expected):
            np.testing.assert_array_equal(actual, wanted)
        for start in range(0, len(points), 7):
            for actual, wanted in zip(query.closest_point(points[start:start + 7]),
                                      trimesh.proximity.closest_point(reference, points[start:start + 7])):
                np.testing.assert_array_equal(actual, wanted)

    def test_tetrahedron_coordinates_distances_and_ids_across_batches(self):
        vertices, faces = self.tetrahedron()
        points = np.vstack([vertices, np.random.default_rng(123).uniform(-2, 2, (96, 3))])
        self.assert_parity(vertices, faces, points)

    def test_opposite_coincident_and_close_sheet_ties(self):
        vertices = np.array([[0, 0, 0], [2, 0, 0], [0, 2, 0],
                             [0, 0, 0.001], [2, 0, 0.001], [0, 2, 0.001]])
        faces = np.array([[0, 1, 2], [0, 2, 1], [3, 5, 4]])
        points = np.array([[x, y, z] for x, y in [(0.2, 0.3), (0, 0), (1, 1), (2, 2)]
                           for z in [-0.1, 0, 0.0005, 0.001, 0.1]])
        self.assert_parity(vertices, faces, points)

    def test_unreferenced_and_duplicate_vertices_keep_upstream_face_order(self):
        vertices, faces = self.tetrahedron()
        vertices = np.vstack([vertices, [50, 50, 50], vertices[0]])
        faces[1, 0] = 5
        self.assert_parity(vertices, faces, np.array([[0.1, 0.1, 0.2], [50, 50, 50], [0, 0, 0]]))

    def test_each_tree_constructed_once_and_upstream_not_monkeypatched(self):
        vertices, faces = self.tetrahedron()
        original_nearby = trimesh.proximity.nearby_faces
        with mock.patch.object(spatial, "cKDTree", wraps=scipy.spatial.cKDTree) as kd, mock.patch.object(
                trimesh.triangles, "bounds_tree", wraps=trimesh.triangles.bounds_tree) as rt:
            query = spatial.SpatialQuery(vertices, faces)
            for _ in range(8):
                query.closest_point([[0.1, 0.2, 0.3], [0, 0, 2]])
            self.assertEqual(kd.call_count, 1)
            self.assertEqual(rt.call_count, 1)
        self.assertIs(trimesh.proximity.nearby_faces, original_nearby)

    def test_context_geometry_is_independent_and_read_only(self):
        vertices, faces = self.tetrahedron()
        query = spatial.SpatialQuery(vertices, faces)
        before = query.closest_point([[0.1, 0.2, 0.3]])
        vertices[:] = 100
        faces[:] = 0
        for array in [query.mesh.vertices, query.mesh.faces, query.mesh.triangles, query.mesh.face_normals]:
            with self.assertRaises(ValueError):
                array.setflags(write=True)
        with self.assertRaises(FrozenInstanceError):
            query.mesh.vertices = np.zeros((3, 3))
        for actual, wanted in zip(query.closest_point([[0.1, 0.2, 0.3]]), before):
            np.testing.assert_array_equal(actual, wanted)

    def test_candidate_budget_never_silently_drops_faces(self):
        vertices, faces = self.tetrahedron()
        query = spatial.SpatialQuery(vertices, faces, max_candidates=1)
        with self.assertRaisesRegex(ValueError, "candidate-pair budget exceeded"):
            query.closest_point([[2, 2, 2]])

    def test_cancellation_during_candidate_collection(self):
        vertices, faces = self.tetrahedron()
        query = spatial.SpatialQuery(vertices, faces)
        calls = 0

        def cancel():
            nonlocal calls
            calls += 1
            if calls == 5:
                raise RuntimeError("cancel fixture")

        with self.assertRaisesRegex(RuntimeError, "cancel fixture"):
            query.closest_point([[2, 2, 2]], check_cancel=cancel)
        self.assertEqual(calls, 5)
        self.assertEqual(len(query.closest_point([[0, 0, 0]])[0]), 1)

    def test_invalid_inputs_and_version_fail_closed(self):
        vertices, faces = self.tetrahedron()
        for budget in [0, -1, True]:
            with self.assertRaises(ValueError):
                spatial.SpatialQuery(vertices, faces, max_candidates=budget)
        with mock.patch.object(trimesh, "__version__", "5.0.0"), self.assertRaises(ValueError):
            spatial.SpatialQuery(vertices, faces)
        with self.assertRaises(ValueError):
            spatial.SpatialQuery(vertices, [[0, 1, 99]])
        with self.assertRaises(ValueError):
            spatial.SpatialQuery(vertices, [[0.0, 1.0, 2.0]])
        query = spatial.SpatialQuery(vertices, faces)
        for points in [[], [[float("nan"), 0, 0]], [[0, 0]], [[float("inf"), 0, 0]]]:
            with self.assertRaises(ValueError):
                query.closest_point(points)


if __name__ == "__main__":
    unittest.main()
