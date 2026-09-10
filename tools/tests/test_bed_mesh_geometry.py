"""Run with the isolated mesh-stage interpreter; optional elsewhere."""
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

try:
    import numpy as np
    import scipy  # noqa: F401
    import trimesh
    import rtree  # noqa: F401
    OPTIONAL_AVAILABLE = True
except ImportError:
    OPTIONAL_AVAILABLE = False

if OPTIONAL_AVAILABLE:
    sys.path.insert(0, str(Path(__file__).parents[1]))
    SPEC = importlib.util.spec_from_file_location("bed_mesh_geometry", Path(__file__).parents[1] / "bed_mesh_geometry.py")
    geometry = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = geometry
    SPEC.loader.exec_module(geometry)


@unittest.skipUnless(OPTIONAL_AVAILABLE, "Optional isolated mesh geometry packages are not installed")
class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bed-mesh-unit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def tetrahedron(self, offset=0):
        return geometry.Mesh(np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=float) + offset,
                             [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]])

    def triangle(self):
        return geometry.Mesh([[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]])

    def ascii_stl(self, vertices, normal="0 0 1"):
        text = "solid test\nfacet normal " + normal + "\nouter loop\n"
        text += "".join("vertex " + " ".join(str(value) for value in vertex) + "\n" for vertex in vertices)
        return (text + "endloop\nendfacet\nendsolid test\n").encode("ascii")

    def binary_stl(self, attributes=0, header=b"solid misleading binary header", normal=(0, 0, 1)):
        return header.ljust(80, b" ") + struct.pack("<I12fH", 1, *normal, 0, 0, 0, 1, 0, 0, 0, 1, 0, attributes)

    def test_ascii_and_binary_preserve_geometry(self):
        ascii_file, binary_file = self.root / "ascii.stl", self.root / "binary.stl"
        ascii_file.write_bytes(self.ascii_stl([[0, 0, 0], [1, 0, 0], [0, 1, 0]]))
        binary_file.write_bytes(self.binary_stl())
        first, second = geometry.read_stl(ascii_file), geometry.read_stl(binary_file)
        np.testing.assert_array_equal(first.vertices, second.vertices)
        np.testing.assert_array_equal(first.faces, second.faces)
        self.assertEqual(first.source_format, "ascii-stl")
        self.assertEqual(second.source_format, "binary-stl")

    def test_attributes_nonfinite_normals_and_malformed_stl_are_rejected(self):
        for data in (self.binary_stl(attributes=1), self.binary_stl(normal=(float("nan"), 0, 1)),
                     self.binary_stl()[:-1], self.binary_stl() + b"extra", b"solid empty\nendsolid empty\n",
                     self.ascii_stl([[0, 0, 0], [1, 0, 0], [0, 1, 0]], normal="nan 0 1"),
                     self.ascii_stl([[0, 0, 0], [1, 0, 0]]),
                     self.ascii_stl([[0, 0, 0], [1, 0, 0], [0, 1, 0]]) + b"garbage"):
            path = self.root / "invalid.stl"
            path.write_bytes(data)
            with self.subTest(data=data[:50]), self.assertRaises(ValueError):
                geometry.read_stl(path)

    def test_ascii_multiple_solids_and_exact_only_welding(self):
        first = [[0, 0, 0], [1, 0, 0], [0, 1, 0]]
        second = [[0, 0, 0], [1, 0, 0], [0, 1.0000000001, 0]]
        path = self.root / "two.stl"
        path.write_bytes(self.ascii_stl(first) + self.ascii_stl(second))
        mesh = geometry.read_stl(path)
        self.assertEqual(len(mesh.vertices), 4)  # No tolerance welding.
        self.assertEqual(len(mesh.faces), 2)
        np.testing.assert_array_equal(mesh.vertices[mesh.faces], [first, second])

    def test_uniform_attribute_and_header_are_preserved_exactly(self):
        path = self.root / "colored.stl"
        header = (b"COLOR=\x01\x02\x03\x04 arbitrary exporter metadata\x00" + bytes(range(30))).ljust(80, b"\xff")
        path.write_bytes(self.binary_stl(attributes=123, header=header))
        original_bytes = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "colored.stl"):
            geometry.read_stl(path)
        mesh = geometry.read_stl(path, allow_attributes_for_audit=True)
        self.assertTrue(mesh.attributes_present)
        stats = geometry.mesh_stats(mesh)
        self.assertTrue(stats["attributes_present"])
        self.assertEqual(stats["uniform_attribute"], 123)
        self.assertFalse(stats["mixed_attributes"])
        self.assertEqual(stats["surface_area_mm2"], 0.5)
        simplified = geometry.simplify(mesh, 1)
        self.assertEqual(simplified.uniform_attribute, 123)
        self.assertEqual(simplified.binary_header, header)
        output = self.root / "preserved.stl"
        geometry.write_stl(simplified, output)
        self.assertEqual(output.read_bytes()[:80], header)
        self.assertEqual(struct.unpack_from("<H", output.read_bytes(), 132)[0], 123)
        reopened = geometry.read_stl(output, allow_attributes_for_audit=True)
        report = geometry.validate_candidate(mesh, reopened, sample_count=32)
        self.assertTrue(report["accepted"], report["reasons"])
        self.assertTrue(report["metrics"]["uniform_attribute_preserved"])
        self.assertTrue(report["metrics"]["original_binary_header_preserved"])
        self.assertEqual(path.read_bytes(), original_bytes)

    def test_mixed_attribute_audit_never_allows_simplifying_or_rewriting(self):
        path = self.root / "mixed.stl"
        first = self.binary_stl(attributes=123)
        second = self.binary_stl(attributes=456)
        path.write_bytes(first[:80] + struct.pack("<I", 2) + first[84:] + second[84:])
        original_bytes = path.read_bytes()
        mesh = geometry.read_stl(path, allow_attributes_for_audit=True)
        self.assertTrue(mesh.attributes_present)
        self.assertIsNone(mesh.uniform_attribute)
        stats = geometry.mesh_stats(mesh)
        self.assertTrue(stats["mixed_attributes"])
        self.assertEqual(stats["surface_area_mm2"], 1.0)
        json.dumps(stats, allow_nan=False)
        with self.assertRaises(ValueError):
            geometry.simplify(mesh, 1)
        output = self.root / "must-not-exist.stl"
        with self.assertRaises(ValueError):
            geometry.write_stl(mesh, output)
        self.assertFalse(output.exists())
        self.assertFalse(geometry.validate_candidate(mesh, self.triangle())["accepted"])
        self.assertEqual(path.read_bytes(), original_bytes)

    def test_metadata_loss_or_changes_are_rejected(self):
        mesh = self.triangle()
        original = geometry.Mesh(mesh.vertices, mesh.faces, uniform_attribute=20083, binary_header=b"x" * 80)
        for attribute, header in ((0, b"x" * 80), (20083, b"y" * 80), (20083, None)):
            with self.subTest(attribute=attribute, header=header):
                changed = geometry.Mesh(mesh.vertices, mesh.faces, uniform_attribute=attribute, binary_header=header)
                report = geometry.validate_candidate(original, changed, sample_count=32)
                self.assertFalse(report["accepted"])
                self.assertTrue(any("STL" in reason for reason in report["reasons"]))

    @unittest.skipUnless(importlib.util.find_spec("fast_simplification"), "Optional comparison backend")
    def test_actual_qem_preserves_uniform_metadata(self):
        sphere = trimesh.creation.icosphere(subdivisions=1)
        original = geometry.Mesh(sphere.vertices, sphere.faces, uniform_attribute=65535, binary_header=b"z" * 80)
        candidate = geometry.simplify(original, 40)
        self.assertLess(len(candidate.faces), len(original.faces))
        self.assertEqual(candidate.uniform_attribute, 65535)
        self.assertEqual(candidate.binary_header, b"z" * 80)
        output = self.root / "simplified.stl"
        geometry.write_stl(candidate, output)
        data = output.read_bytes()
        self.assertEqual(data[:80], b"z" * 80)
        self.assertTrue(all(struct.unpack_from("<H", data, offset)[0] == 65535
                            for offset in range(132, len(data), 50)))

    def test_statistics_are_finite_and_original_degenerate_faces_not_removed(self):
        mesh = geometry.Mesh([[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2], [0, 0, 0]])
        stats = geometry.mesh_stats(mesh)
        self.assertEqual(stats["triangles"], 2)
        self.assertEqual(stats["degenerate_faces"], 1)
        self.assertEqual(stats["surface_area_mm2"], 0.5)
        json.dumps(stats, allow_nan=False)
        with self.assertRaises(ValueError):
            geometry.simplify(mesh, 1)
        report = geometry.validate_candidate(mesh, self.triangle(), sample_count=32)
        self.assertFalse(report["accepted"])
        self.assertIn("original: degenerate_faces", report["reasons"])

    def test_closed_topology_area_and_volume(self):
        stats = geometry.mesh_stats(self.tetrahedron())
        self.assertEqual(stats["component_count"], 1)
        self.assertEqual(stats["boundary_edges"], 0)
        self.assertEqual(stats["winding_conflicts"], 0)
        self.assertEqual(stats["components"][0]["euler_characteristic"], 2)
        self.assertAlmostEqual(stats["surface_area_mm2"], 1.5 + np.sqrt(3) / 2)
        self.assertAlmostEqual(stats["components"][0]["signed_volume_mm3"], 1 / 6)
        self.assertTrue(stats["components"][0]["volume_check_suitable"])

    def test_identical_mesh_passes_bidirectional_surface_checks(self):
        original = self.tetrahedron()
        report = geometry.validate_candidate(original, original, sample_count=64)
        self.assertTrue(report["accepted"], report["reasons"])
        self.assertLess(report["metrics"]["original_to_candidate"]["maximum_distance_mm"], 1e-12)
        self.assertLess(report["metrics"]["candidate_to_original"]["maximum_distance_mm"], 1e-12)
        self.assertIn("NOT a Hausdorff", report["surface_check"])
        json.dumps(report, allow_nan=False)

    def test_finite_bounds_indices_degenerate_and_duplicate_validation(self):
        original = self.tetrahedron()
        cases = [geometry.Mesh([[float("nan"), 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]]),
                 geometry.Mesh(original.vertices, [[0, 1, 99]]),
                 geometry.Mesh(original.vertices, [[-1, 1, 2]]),
                 geometry.Mesh(original.vertices, [[0, 0, 2]]),
                 geometry.Mesh(original.vertices, np.vstack([original.faces, original.faces[0]]))]
        for candidate in cases:
            with self.subTest(candidate=candidate):
                report = geometry.validate_candidate(original, candidate, sample_count=32)
                self.assertFalse(report["accepted"])
                json.dumps(report, allow_nan=False)
        with self.assertRaises(ValueError):
            geometry.Mesh(original.vertices, [[0.5, 1, 2]])

    def test_components_and_boundary_topology_are_preserved(self):
        first, second = self.tetrahedron(), self.tetrahedron(offset=3)
        original = geometry.Mesh(np.vstack([first.vertices, second.vertices]),
                                 np.vstack([first.faces, second.faces + len(first.vertices)]))
        report = geometry.validate_candidate(original, first, sample_count=64)
        self.assertIn("component count changed", report["reasons"])
        hole = geometry.Mesh(first.vertices, first.faces[:-1])
        report = geometry.validate_candidate(first, hole, sample_count=64)
        self.assertIn("component topology/boundary signature changed", report["reasons"])

    def test_closed_and_open_winding_reversal_is_rejected(self):
        closed = self.tetrahedron()
        reversed_closed = geometry.Mesh(closed.vertices, closed.faces[:, ::-1])
        report = geometry.validate_candidate(closed, reversed_closed, sample_count=64)
        self.assertIn("component volume/orientation exceeds relative tolerance", report["reasons"])
        opened = self.triangle()
        reversed_open = geometry.Mesh(opened.vertices, opened.faces[:, ::-1])
        report = geometry.validate_candidate(opened, reversed_open, sample_count=64)
        self.assertIn("sampled surface winding reversed", report["reasons"])
        self.assertGreater(report["metrics"]["original_to_candidate"]["unresolved_oriented_samples"], 0)

    def test_close_opposite_sheets_identity_and_ambiguous_nearest_faces(self):
        first = self.triangle()
        mesh = geometry.Mesh(np.vstack([first.vertices, first.vertices + [0, 0, 1e-5]]),
                             np.vstack([first.faces, first.faces[:, ::-1] + 3]))
        report = geometry.validate_candidate(mesh, mesh, sample_count=64)
        self.assertTrue(report["accepted"], report["reasons"])
        from bed_mesh_spatial import SpatialQuery
        real_closest = SpatialQuery.closest_point

        def choose_opposite_nearby_face(query, points, check_cancel=lambda: None):
            closest, _, face_ids = real_closest(query, points, check_cancel=check_cancel)
            other = 1 - face_ids
            closest[:, 2] = query.mesh.triangles[other, 0, 2]
            return closest, np.linalg.norm(closest - points, axis=1), other

        # Reproduce the ambiguous nearest-face choice independently of library
        # ordering/tie tolerances; the returned point is really on that near sheet.
        with mock.patch.object(SpatialQuery, "closest_point", new=choose_opposite_nearby_face):
            report = geometry.validate_candidate(mesh, mesh, sample_count=64)
        self.assertTrue(report["accepted"], report["reasons"])
        for direction in ("original_to_candidate", "candidate_to_original"):
            result = report["metrics"][direction]
            self.assertGreater(result["opposed_sample_normals"], 0)
            self.assertEqual(result["resolved_nearest_normal_ambiguities"], result["opposed_sample_normals"])
            self.assertEqual(result["unresolved_oriented_samples"], 0)

    def test_oriented_match_requires_exact_distance_not_only_aabb_overlap(self):
        mesh = self.triangle()
        target = trimesh.Trimesh(vertices=mesh.vertices, faces=mesh.faces, process=False, validate=False)
        normals = mesh._analysis[2]
        self.assertIsNone(geometry._aligned_match_distance(np.array([0.9, 0.9, 0.0]),
                                                           np.array([0, 0, 1]), target, normals, 0.05))
        self.assertIsNone(geometry._aligned_match_distance(np.array([0.2, 0.2, 0.06]),
                                                           np.array([0, 0, 1]), target, normals, 0.05))
        self.assertIsNone(geometry._aligned_match_distance(np.array([0.2, 0.2, 0.0]),
                                                           np.array([0, 0, -1]), target, normals, 0.05))
        self.assertAlmostEqual(geometry._aligned_match_distance(np.array([0.2, 0.2, 0.04]),
                                                                 np.array([0, 0, 1]), target, normals, 0.05), 0.04)

    def test_bounds_area_and_volume_have_independent_limits(self):
        original = self.tetrahedron()
        moved = geometry.Mesh(original.vertices + [0.06, 0, 0], original.faces)
        self.assertIn("bounds exceed absolute tolerance",
                      geometry.validate_candidate(original, moved)["reasons"])
        scaled = geometry.Mesh(original.vertices * 1.01, original.faces)
        self.assertIn("surface area exceeds relative tolerance",
                      geometry.validate_candidate(original, scaled)["reasons"])
        slightly_scaled = geometry.Mesh(original.vertices * 1.002, original.faces)
        report = geometry.validate_candidate(original, slightly_scaled)
        self.assertLess(report["metrics"]["relative_surface_area_change"], 0.005)
        self.assertIn("component volume/orientation exceeds relative tolerance", report["reasons"])

    def test_open_component_does_not_disable_closed_component_volume_check(self):
        tetrahedron, triangle = self.tetrahedron(), self.triangle()
        vertices = np.vstack([tetrahedron.vertices, triangle.vertices + [3, 0, 0]])
        faces = np.vstack([tetrahedron.faces, triangle.faces + 4])
        original = geometry.Mesh(vertices, faces)
        unchanged = geometry.validate_candidate(original, original, sample_count=64)
        self.assertTrue(unchanged["accepted"], unchanged["reasons"])
        self.assertTrue(unchanged["metrics"]["volume_check_applicable"])
        self.assertEqual(unchanged["metrics"]["volume_components_checked"], 1)
        vertices[:4] *= 1.002
        candidate = geometry.Mesh(vertices, faces)
        report = geometry.validate_candidate(original, candidate, sample_count=64)
        self.assertFalse(report["accepted"])
        metrics = report["metrics"]
        self.assertLess(metrics["relative_surface_area_change"], 0.005)
        self.assertLess(metrics["bounds_max_error_mm"], 0.05)
        self.assertTrue(metrics["volume_check_applicable"])
        self.assertEqual(metrics["volume_components_checked"], 1)
        self.assertEqual(len(metrics["volume_component_checks"]), 1)
        checked = metrics["volume_component_checks"][0]
        self.assertAlmostEqual(checked["relative_volume_change"], 1.002 ** 3 - 1, places=6)
        self.assertFalse(checked["within_limit"])
        self.assertAlmostEqual(checked["original_signed_volume_mm3"], 1 / 6)
        self.assertIn("component volume/orientation exceeds relative tolerance", report["reasons"])
        json.dumps(report, allow_nan=False)

    def test_zero_volume_component_does_not_disable_other_volume_checks(self):
        tetrahedron = self.tetrahedron()
        flat_vertices = np.array([[3, 0, 0], [4, 0, 0], [3, 1, 0], [4, 1, 0]])
        vertices = np.vstack([tetrahedron.vertices, flat_vertices])
        faces = np.vstack([tetrahedron.faces, tetrahedron.faces + 4])
        original = geometry.Mesh(vertices, faces)
        stats = geometry.mesh_stats(original)
        self.assertEqual(sum(item["volume_check_suitable"] for item in stats["components"]), 1)
        vertices[:4] *= 1.002
        report = geometry.validate_candidate(original, geometry.Mesh(vertices, faces), sample_count=64)
        self.assertFalse(report["accepted"])
        self.assertTrue(report["metrics"]["volume_check_applicable"])
        self.assertEqual(report["metrics"]["volume_components_checked"], 1)
        self.assertIn("component volume/orientation exceeds relative tolerance", report["reasons"])
        json.dumps(report, allow_nan=False)

    def test_float32_serialization_not_just_double_geometry_is_validated(self):
        original = geometry.Mesh(self.tetrahedron().vertices + [1e8, 0, 0], self.tetrahedron().faces)
        report = geometry.validate_candidate(original, original, sample_count=64)
        self.assertFalse(report["accepted"])
        self.assertIn("candidate: degenerate_faces", report["reasons"])

    def test_sampled_deviation_finds_interior_dent_without_bounds_change(self):
        box = trimesh.creation.box()
        vertices, faces = trimesh.remesh.subdivide(box.vertices, box.faces)
        original = geometry.Mesh(vertices, faces)
        altered = vertices.copy()
        # A face-edge midpoint shared by two coplanar triangles, not box bounds.
        face_center = np.where(np.all(np.isclose(vertices, [0, 0, 0.5]), axis=1))[0][0]
        altered[face_center, 2] -= 0.1
        candidate = geometry.Mesh(altered, faces)
        report = geometry.validate_candidate(original, candidate, relative_limit=1.0, sample_count=256)
        self.assertEqual(report["metrics"]["bounds_max_error_mm"], 0)
        self.assertIn("sampled surface deviation exceeds absolute tolerance", report["reasons"])

    @unittest.skipUnless(importlib.util.find_spec("fast_simplification"), "Optional comparison backend")
    def test_sampling_and_simplification_are_repeatable(self):
        sphere = trimesh.creation.icosphere(subdivisions=1)
        original = geometry.Mesh(sphere.vertices, sphere.faces)
        original_vertices, original_faces = original.vertices.copy(), original.faces.copy()
        first = geometry.simplify(original, 40)
        second = geometry.simplify(original, 40)
        self.assertLess(len(first.faces), len(original.faces))
        np.testing.assert_array_equal(first.vertices, second.vertices)
        np.testing.assert_array_equal(first.faces, second.faces)
        np.testing.assert_array_equal(original.vertices, original_vertices)
        np.testing.assert_array_equal(original.faces, original_faces)
        for a, b in zip(geometry._sample_surface(original, 64), geometry._sample_surface(original, 64)):
            np.testing.assert_array_equal(a, b)

    @unittest.skipUnless(importlib.util.find_spec("pymeshlab"), "Optional PyMeshLab backend is not installed")
    def test_pymeshlab_preserves_geometry_metadata_and_is_repeatable(self):
        box = trimesh.creation.box()
        vertices, faces = trimesh.remesh.subdivide(box.vertices, box.faces)
        original = geometry.Mesh(vertices, faces, uniform_attribute=20083, binary_header=b"p" * 80)
        first = geometry.simplify(original, 12, backend="pymeshlab")
        second = geometry.simplify(original, 12, backend="pymeshlab")
        self.assertLess(len(first.faces), len(original.faces))
        np.testing.assert_array_equal(first.vertices, second.vertices)
        np.testing.assert_array_equal(first.faces, second.faces)
        np.testing.assert_array_equal(original.vertices, vertices)
        np.testing.assert_array_equal(original.faces, faces)
        self.assertEqual(first.uniform_attribute, 20083)
        self.assertEqual(first.binary_header, b"p" * 80)
        report = geometry.validate_candidate(original, first, sample_count=64)
        self.assertTrue(report["accepted"], report["reasons"])

    def test_binary_write_is_exclusive_and_roundtrip_matches_quantized_geometry(self):
        mesh = self.tetrahedron()
        output = self.root / "new.stl"
        geometry.write_stl(mesh, output)
        self.assertEqual(output.stat().st_size, 84 + 50 * len(mesh.faces))
        reopened = geometry.read_stl(output)
        expected = geometry.quantize_stl(mesh)
        np.testing.assert_array_equal(reopened.vertices[reopened.faces], expected.vertices[expected.faces])
        original_bytes = output.read_bytes()
        with self.assertRaises(FileExistsError):
            geometry.write_stl(mesh, output)
        self.assertEqual(output.read_bytes(), original_bytes)

    def test_limits_and_input_memory_are_bounded(self):
        path = self.root / "triangle.stl"
        path.write_bytes(self.binary_stl())
        with self.assertRaises(ValueError):
            geometry.read_stl(path, max_file_bytes=20)
        for count in (0, -1, 2.5, True):
            with self.assertRaises(ValueError):
                geometry.simplify(self.tetrahedron(), count)
        with self.assertRaises(ValueError):
            geometry.simplify(self.tetrahedron(), 4, backend="unknown")
        for tolerance in (0, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                geometry.validate_candidate(self.tetrahedron(), self.tetrahedron(), tolerance_mm=tolerance)


if __name__ == "__main__":
    unittest.main()
