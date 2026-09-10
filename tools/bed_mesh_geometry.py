"""Conservative, build-time-only STL simplification and geometry checks.

No automatic input repairs, tolerance welding, invalid-face cleanup or winding
fixes are performed. Only exactly equal vertex coordinates are welded. Validation uses
the float32 coordinates that binary STL will actually store. Surface distances
are deterministic *samples*, NOT a Hausdorff bound or a self-intersection proof.
The caller must retain the original whenever ``accepted`` is false.

Pinned dependencies are in mesh-stage-requirements.txt; the optional comparison
backend is separate in mesh-experiment-requirements.txt. These are packaging
tools, not runtime dependencies.
"""

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
import re
import struct
import threading
import time

import numpy as np


_FACET_DTYPE = np.dtype([("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)),
                         ("attribute", "<u2")])
_SIMPLIFY_LOCK = threading.Lock()  # The upstream wrapper uses shared C++ state.
_DEFAULT_HEADER = b"PrusaSlicer experimental validated bed mesh; binary STL".ljust(80, b" ")


@dataclass(frozen=True, eq=False)
class Mesh:
    vertices: np.ndarray
    faces: np.ndarray
    source_format: str = "memory"
    uniform_attribute: int | None = 0  # None means mixed per-face metadata: audit only.
    binary_header: bytes | None = None

    def __post_init__(self):
        vertices = np.array(self.vertices, dtype=np.float64, order="C", copy=True)
        raw_faces = np.asarray(self.faces)
        if raw_faces.dtype.kind not in "iu":
            raise ValueError("Face indices must be integers, not rounded floats")
        faces = np.array(raw_faces, dtype=np.int64, order="C", copy=True)
        if vertices.ndim != 2 or vertices.shape[1:] != (3,):
            raise ValueError("Vertices must have shape (n, 3)")
        if faces.ndim != 2 or faces.shape[1:] != (3,):
            raise ValueError("Faces must have shape (n, 3)")
        if self.uniform_attribute is not None:
            if (isinstance(self.uniform_attribute, bool)
                    or not isinstance(self.uniform_attribute, (int, np.integer))
                    or not 0 <= self.uniform_attribute <= 65535):
                raise ValueError("Uniform STL attribute must be a uint16 or None for mixed values")
            object.__setattr__(self, "uniform_attribute", int(self.uniform_attribute))
        if self.binary_header is not None and (not isinstance(self.binary_header, bytes)
                                               or len(self.binary_header) != 80):
            raise ValueError("Binary STL header must be exactly 80 bytes")
        vertices.setflags(write=False)
        faces.setflags(write=False)
        object.__setattr__(self, "vertices", vertices)
        object.__setattr__(self, "faces", faces)

    @property
    def attributes_present(self):
        return self.uniform_attribute != 0

    @cached_property
    def _analysis(self):
        return _analyze(self)

    @cached_property
    def _spatial_query(self):
        from bed_mesh_spatial import SpatialQuery
        return SpatialQuery(self.vertices, self.faces)

    @cached_property
    def _features_45(self):
        features = _compute_feature_segments(self, 45)
        features.setflags(write=False)
        return features


def _check_indices(mesh):
    if not len(mesh.vertices) or not len(mesh.faces):
        raise ValueError("Empty meshes are not candidates")
    if not np.isfinite(mesh.vertices).all():
        raise ValueError("Non-finite vertex coordinate")
    if mesh.faces.min() < 0 or mesh.faces.max() >= len(mesh.vertices):
        raise ValueError("Face index outside vertex array")


def _weld(vertices, faces, source_format="memory", uniform_attribute=0, binary_header=None):
    mesh = Mesh(vertices, faces, source_format, uniform_attribute, binary_header)
    _check_indices(mesh)
    unique, inverse = np.unique(mesh.vertices, axis=0, return_inverse=True)
    # np.unique reorders storage, never face order or triangle winding.
    return Mesh(unique, inverse[mesh.faces], source_format, uniform_attribute, binary_header)


def quantize_stl(mesh):
    """Exactly model STL float32 serialization, including resulting welds."""
    if mesh.uniform_attribute is None:
        raise ValueError("Mixed per-face STL attributes may only be audited, not rewritten")
    _check_indices(mesh)
    with np.errstate(over="ignore", invalid="ignore"):
        vertices = mesh.vertices.astype(np.float32).astype(np.float64)
    if not np.isfinite(vertices).all():
        raise ValueError("Coordinates overflow binary STL float32")
    return _weld(vertices, mesh.faces, "binary-stl-candidate", mesh.uniform_attribute, mesh.binary_header)


def _read_ascii(data):
    try:
        lines = [line.strip() for line in data.decode("ascii").splitlines() if line.strip()]
    except UnicodeError as error:
        raise ValueError("Not a valid binary or ASCII STL") from error
    triangles = []
    position = 0

    def vector(prefix):
        nonlocal position
        if position >= len(lines):
            raise ValueError("Truncated ASCII STL")
        fields = lines[position].split()
        position += 1
        if [part.lower() for part in fields[:len(prefix)]] != prefix or len(fields) != len(prefix) + 3:
            raise ValueError("Unexpected ASCII STL vector record")
        try:
            values = [float(value) for value in fields[len(prefix):]]
        except ValueError as error:
            raise ValueError("Invalid ASCII STL number") from error
        if not np.isfinite(values).all():
            raise ValueError("Non-finite ASCII STL vector")
        return values

    def keyword(expected):
        nonlocal position
        if position >= len(lines) or lines[position].lower() != expected:
            raise ValueError(f"Expected ASCII STL '{expected}'")
        position += 1

    # Multiple solid blocks are valid; preserve every facet in source order.
    while position < len(lines):
        if not re.fullmatch(r"solid(?:\s+.*)?", lines[position], re.I):
            raise ValueError("Expected ASCII STL solid header")
        position += 1
        while position < len(lines) and not re.fullmatch(r"endsolid(?:\s+.*)?", lines[position], re.I):
            vector(["facet", "normal"])  # Validate, but do not repair geometry from normals.
            keyword("outer loop")
            triangles.append([vector(["vertex"]) for _ in range(3)])
            keyword("endloop")
            keyword("endfacet")
        if position >= len(lines):
            raise ValueError("Missing ASCII STL endsolid")
        position += 1
    if not triangles:
        raise ValueError("Empty ASCII STL")
    return np.asarray(triangles, dtype=np.float64)


def _read_stl(path, max_file_bytes, allow_attributes_for_audit):
    path = Path(path)
    if path.stat().st_size > max_file_bytes:
        raise ValueError("STL exceeds bounded reader size limit")
    data = path.read_bytes()
    uniform_attribute = 0
    binary_header = None
    count = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else None
    if count is not None and len(data) == 84 + 50 * count:
        if not count:
            raise ValueError("Empty binary STL")
        records = np.frombuffer(data, dtype=_FACET_DTYPE, count=count, offset=84)
        attributes_present = bool(np.any(records["attribute"] != 0))
        if attributes_present and not allow_attributes_for_audit:
            raise ValueError("Nonzero binary STL attributes require an explicit metadata-aware read")
        first_attribute = int(records["attribute"][0])
        uniform_attribute = first_attribute if np.all(records["attribute"] == first_attribute) else None
        binary_header = data[:80]
        if not np.isfinite(records["normal"]).all():
            raise ValueError("Non-finite binary STL normal")
        triangles = records["vertices"].astype(np.float64)
        source_format = "binary-stl"
    else:
        triangles = _read_ascii(data)
        source_format = "ascii-stl"
    faces = np.arange(triangles.shape[0] * 3, dtype=np.int64).reshape(-1, 3)
    return _weld(triangles.reshape(-1, 3), faces, source_format, uniform_attribute, binary_header)


def read_stl(path, max_file_bytes=256 * 1024 * 1024, allow_attributes_for_audit=False):
    """Strict STL reader with explicit opt-in for nonzero attribute words.

    Opt-in retains a uniform uint16 attribute and the complete binary header,
    which simplification/serialization preserve exactly. Mixed per-face words
    remain audit-only: statistics are allowed, but simplification/write reject
    them. The same opt-in is needed to re-read uniform-attribute candidates.
    """
    try:
        return _read_stl(path, max_file_bytes, allow_attributes_for_audit)
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from error


def _analyze(mesh):
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    _check_indices(mesh)
    triangles = mesh.vertices[mesh.faces]
    cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    double_area = np.linalg.norm(cross, axis=1)
    if not np.isfinite(double_area).all():
        raise ValueError("Non-finite triangle area")
    areas = double_area * 0.5
    normals = np.divide(cross, double_area[:, None], out=np.zeros_like(cross),
                        where=double_area[:, None] > 0)
    edges = np.concatenate([mesh.faces[:, [0, 1]], mesh.faces[:, [1, 2]], mesh.faces[:, [2, 0]]])
    unique_edges, inverse, incidence = np.unique(np.sort(edges, axis=1), axis=0,
                                                return_inverse=True, return_counts=True)
    winding_sums = np.bincount(inverse, weights=np.where(edges[:, 0] < edges[:, 1], 1, -1))
    conflict = (incidence == 2) & (winding_sums != 0)
    graph = coo_matrix((np.ones(len(unique_edges), dtype=np.uint8),
                        (unique_edges[:, 0], unique_edges[:, 1])),
                       shape=(len(mesh.vertices), len(mesh.vertices))).tocsr()
    component_count, labels = connected_components(graph, directed=False)
    face_labels = labels[mesh.faces[:, 0]]
    edge_labels = labels[unique_edges[:, 0]]
    boundary = unique_edges[incidence == 1]
    boundary_loops = np.zeros(component_count, dtype=np.int64)
    boundary_branch_vertices = 0
    if len(boundary):
        boundary_vertices, boundary_inverse = np.unique(boundary, return_inverse=True)
        boundary_edges = boundary_inverse.reshape(-1, 2)
        degrees = np.bincount(boundary_inverse.ravel())
        boundary_branch_vertices = int(np.count_nonzero(degrees != 2))
        boundary_graph = coo_matrix((np.ones(len(boundary_edges), dtype=np.uint8),
                                    (boundary_edges[:, 0], boundary_edges[:, 1])),
                                   shape=(len(boundary_vertices), len(boundary_vertices))).tocsr()
        _, boundary_labels = connected_components(boundary_graph, directed=False)
        _, first = np.unique(boundary_labels, return_index=True)
        boundary_loops = np.bincount(labels[boundary_vertices[first]], minlength=component_count)
    vertex_counts = np.bincount(labels, minlength=component_count)
    face_counts = np.bincount(face_labels, minlength=component_count)
    edge_counts = np.bincount(edge_labels, minlength=component_count)
    boundary_counts = np.bincount(edge_labels[incidence == 1], minlength=component_count)
    nonmanifold_counts = np.bincount(edge_labels[incidence > 2], minlength=component_count)
    conflict_counts = np.bincount(edge_labels[conflict], minlength=component_count)
    minimum = np.full((component_count, 3), np.inf)
    maximum = np.full((component_count, 3), -np.inf)
    np.minimum.at(minimum, labels, mesh.vertices)
    np.maximum.at(maximum, labels, mesh.vertices)
    origins = (minimum + maximum) * 0.5
    local = triangles - origins[face_labels, None, :]
    signed_contributions = np.einsum("ij,ij->i", local[:, 0], np.cross(local[:, 1], local[:, 2])) / 6.0
    volumes = np.bincount(face_labels, weights=signed_contributions, minlength=component_count)
    component_areas = np.bincount(face_labels, weights=areas, minlength=component_count)
    components = []
    for index in range(component_count):
        closed = not (boundary_counts[index] or nonmanifold_counts[index] or conflict_counts[index])
        volume_floor = max(1e-12, float(np.prod(maximum[index] - minimum[index])) * 1e-12)
        components.append({"vertices": int(vertex_counts[index]), "triangles": int(face_counts[index]),
                           "edges": int(edge_counts[index]),
                           "euler_characteristic": int(vertex_counts[index] - edge_counts[index] + face_counts[index]),
                           "boundary_edges": int(boundary_counts[index]),
                           "boundary_loops": int(boundary_loops[index]),
                           "watertight_consistent": bool(closed),
                           "volume_check_suitable": bool(closed and abs(volumes[index]) > volume_floor),
                           "signed_volume_mm3": float(volumes[index]),
                           "surface_area_mm2": float(component_areas[index]),
                           "bounds_mm": [minimum[index].tolist(), maximum[index].tolist()]})
    duplicate_faces = len(mesh.faces) - len(np.unique(np.sort(mesh.faces, axis=1), axis=0))
    unused_vertices = len(mesh.vertices) - len(np.unique(mesh.faces))
    stats = {"triangles": len(mesh.faces), "vertices": len(mesh.vertices),
             "attributes_present": bool(mesh.attributes_present),
             "uniform_attribute": mesh.uniform_attribute,
             "mixed_attributes": mesh.uniform_attribute is None,
             "surface_area_mm2": float(areas.sum()),
             "bounds_mm": [mesh.vertices.min(axis=0).tolist(), mesh.vertices.max(axis=0).tolist()],
             "degenerate_faces": int(np.count_nonzero(double_area == 0)),
             "duplicate_faces": int(duplicate_faces), "unused_vertices": int(unused_vertices),
             "component_count": int(component_count), "boundary_edges": int(len(boundary)),
             "boundary_branch_vertices": boundary_branch_vertices,
             "nonmanifold_edges": int(np.count_nonzero(incidence > 2)),
             "winding_conflicts": int(np.count_nonzero(conflict)), "components": components}
    return stats, areas, normals, face_labels


def mesh_stats(mesh):
    """Return JSON-compatible geometric/topological stats without modifying mesh."""
    import copy
    return copy.deepcopy(mesh._analysis[0])


def _invalid_reasons(stats):
    return [name for name in ("mixed_attributes", "degenerate_faces", "duplicate_faces", "unused_vertices",
                              "nonmanifold_edges", "winding_conflicts", "boundary_branch_vertices")
            if stats[name]]


def simplify(mesh, target_faces, backend="fast-simplification"):
    """Deterministic QEM proposal only: ALWAYS validate before accepting it."""
    if isinstance(target_faces, bool) or not isinstance(target_faces, (int, np.integer)) or target_faces < 1:
        raise ValueError("target_faces must be a positive integer")
    if backend not in ("fast-simplification", "pymeshlab"):
        raise ValueError("Unknown simplification backend: " + str(backend))
    invalid = _invalid_reasons(mesh._analysis[0])
    if invalid:
        raise ValueError("Original mesh is unsuitable: " + ", ".join(invalid))
    if target_faces >= len(mesh.faces):
        return quantize_stl(mesh)
    # Fixed input order/options, no random preprocessing or automatic repair.
    with _SIMPLIFY_LOCK:
        if backend == "fast-simplification":
            import fast_simplification
            vertices, faces = fast_simplification.simplify(
                mesh.vertices.copy(), mesh.faces.copy(), target_count=int(target_faces),
                agg=7.0, verbose=False)
        else:
            import pymeshlab
            try:
                mesh_set = pymeshlab.MeshSet()
                mesh_set.add_mesh(pymeshlab.Mesh(vertex_matrix=mesh.vertices.copy(),
                                                 face_matrix=mesh.faces.astype(np.int32)))
                mesh_set.meshing_decimation_quadric_edge_collapse(
                    targetfacenum=int(target_faces), targetperc=0.0,
                    preservetopology=True, preserveboundary=True, preservenormal=True,
                    optimalplacement=False, autoclean=False)
                reduced = mesh_set.current_mesh()
                # Compact deleted storage slots after edge collapses. This is NOT
                # MeshLab's AutoClean, which also deletes/repairs unrelated facets.
                reduced.compact()
                vertices, faces = reduced.vertex_matrix(), reduced.face_matrix()
            except pymeshlab.PyMeshLabException as error:
                raise ValueError("PyMeshLab proposal failed: " + str(error)) from error
    return quantize_stl(Mesh(vertices, faces, uniform_attribute=mesh.uniform_attribute,
                             binary_header=mesh.binary_header))


def simplify_exact_planar(mesh, max_candidates=256, check_cancel=lambda: None):
    from bed_mesh_certificates import planar_convex_fans
    invalid = _invalid_reasons(mesh._analysis[0])
    if invalid:
        raise ValueError("Original mesh is unsuitable: " + ", ".join(invalid))
    faces, proof = planar_convex_fans(mesh.vertices, mesh.faces, max_candidates, check_cancel)
    if not proof["proved_patches"]:
        return mesh, proof
    used, inverse = np.unique(faces, return_inverse=True)
    result = Mesh(mesh.vertices[used], inverse.reshape(-1, 3), uniform_attribute=mesh.uniform_attribute,
                  binary_header=mesh.binary_header)
    return quantize_stl(result), proof


def _sample_surface(mesh, count, return_components=False):
    """Vertex/face-order strata + per-component centroids + area-weighted points."""
    if isinstance(count, bool) or not isinstance(count, (int, np.integer)) or count < 16:
        raise ValueError("sample_count must be an integer of at least 16")
    stats, areas, normals, face_labels = mesh._analysis
    _, representative_faces = np.unique(face_labels, return_index=True)
    if len(representative_faces) > count // 4:
        raise ValueError("Too many components for this bounded sample budget")
    vertex_budget = min(len(mesh.vertices), count // 4)
    vertex_ids = np.linspace(0, len(mesh.vertices) - 1, vertex_budget, dtype=np.int64)
    # Include extremal coordinates even if ordering strata would miss them.
    vertex_ids = np.unique(np.concatenate([vertex_ids, mesh.vertices.argmin(axis=0), mesh.vertices.argmax(axis=0)]))
    face_budget = min(len(mesh.faces), count // 4)
    face_ids = np.unique(np.concatenate([np.linspace(0, len(mesh.faces) - 1, face_budget, dtype=np.int64),
                                         representative_faces]))
    random_count = max(1, count - len(vertex_ids) - len(face_ids))
    rng = np.random.Generator(np.random.PCG64(0))
    # Stratified area quantiles, shuffled barycentric coordinates with a fixed seed.
    area_positions = (np.arange(random_count) + 0.5) / random_count * areas.sum()
    area_ids = np.searchsorted(np.cumsum(areas), area_positions, side="left")
    area_ids = np.minimum(area_ids, len(mesh.faces) - 1)
    u = np.sqrt(rng.random(random_count))
    v = rng.random(random_count)
    barycentric = np.column_stack([1 - u, u * (1 - v), u * v])
    triangles = mesh.vertices[mesh.faces]
    points = np.concatenate([mesh.vertices[vertex_ids], triangles[face_ids].mean(axis=1),
                             np.einsum("ij,ijk->ik", barycentric, triangles[area_ids])])
    source_normals = np.concatenate([np.zeros((len(vertex_ids), 3)), normals[face_ids], normals[area_ids]])
    interior = np.arange(len(points)) >= len(vertex_ids)
    if return_components:
        vertex_labels = np.empty(len(mesh.vertices), dtype=np.int64)
        vertex_labels[mesh.faces.ravel()] = np.repeat(face_labels, 3)
        components = np.concatenate([vertex_labels[vertex_ids], face_labels[face_ids], face_labels[area_ids]])
        return points, source_normals, interior, components
    return points, source_normals, interior


def _aligned_match_distance(point, normal, target_mesh, target_normals, tolerance_mm,
                            target_labels=None, required_component=None, check_cancel=lambda: None):
    """Find an exact same-facing surface match, with bounded query batches.

    Return a confirmed distance <= tolerance, not necessarily the global
    minimum. AABB overlap alone never establishes a surface-distance match.
    """
    from itertools import islice
    import trimesh

    candidates = target_mesh.triangles_tree.intersection(
        np.concatenate([point - tolerance_mm, point + tolerance_mm]))
    while True:
        check_cancel()
        face_ids = np.fromiter(islice(candidates, 1024), dtype=np.int64)
        if not len(face_ids):
            return None
        if required_component is not None:
            face_ids = face_ids[target_labels[face_ids] == required_component]
        if normal is not None:
            face_ids = face_ids[np.einsum("ij,j->i", target_normals[face_ids], normal) > 0]
        if not len(face_ids):
            continue
        triangles = target_mesh.triangles[face_ids]
        closest = trimesh.triangles.closest_point(triangles, np.broadcast_to(point, (len(face_ids), 3)))
        distances = np.linalg.norm(closest - point, axis=1)
        if not np.isfinite(distances).all():
            raise ValueError("Oriented surface proximity returned invalid results")
        minimum = float(distances.min())
        if minimum <= tolerance_mm:
            return minimum


def _surface_distance(source, target, sample_count, tolerance_mm=0.05,
                      component_map=None, check_cancel=lambda: None):
    import trimesh

    points, normals, interior, components = _sample_surface(source, sample_count, return_components=True)
    query = target._spatial_query
    target_mesh = query.mesh
    target_normals = target._analysis[2]
    maximum = 0.0
    normal_conflicts = 0
    resolved_ambiguities = 0
    unresolved_oriented = 0
    maximum_aligned_distance = 0.0
    minimum_normal_dot = 1.0
    component_conflicts = unresolved_components = 0
    # Small batches bound proximity candidate-array memory on dense meshes.
    for start in range(0, len(points), 32):
        check_cancel()
        stop = start + 32
        _, distances, face_ids = query.closest_point(points[start:stop], check_cancel=check_cancel)
        if not np.isfinite(distances).all() or np.any(face_ids < 0):
            raise ValueError("Surface proximity returned invalid results")
        maximum = max(maximum, float(distances.max()))
        dots = np.einsum("ij,ij->i", normals[start:stop], target_normals[face_ids])
        relevant = dots[interior[start:stop]]
        if len(relevant):
            minimum_normal_dot = min(minimum_normal_dot, float(relevant.min()))
            normal_conflicts += int(np.count_nonzero(relevant < -1e-7))
        # A nearest face alone cannot establish inversion on nearby or coincident
        # opposite-facing sheets. Confirm whether the original orientation still
        # has a matching surface inside the same geometric tolerance.
        opposed = interior[start:stop] & (dots < -1e-7)
        wrong_component = np.zeros(len(face_ids), dtype=bool)
        if component_map is not None:
            wanted_components = component_map[components[start:stop]]
            wrong_component = target._analysis[3][face_ids] != wanted_components
            component_conflicts += int(np.count_nonzero(wrong_component))
        for local_index in np.flatnonzero(opposed | wrong_component):
            index = start + local_index
            aligned = _aligned_match_distance(points[index], normals[index] if interior[index] else None,
                target_mesh, target_normals, tolerance_mm, target._analysis[3],
                None if component_map is None else int(component_map[components[index]]), check_cancel)
            if aligned is None:
                unresolved_oriented += int(opposed[local_index])
                unresolved_components += int(wrong_component[local_index])
            else:
                resolved_ambiguities += int(opposed[local_index])
                maximum_aligned_distance = max(maximum_aligned_distance, aligned)
    return {"sample_count": len(points), "maximum_distance_mm": maximum,
            "opposed_sample_normals": normal_conflicts, "minimum_sample_normal_dot": minimum_normal_dot,
            "resolved_nearest_normal_ambiguities": resolved_ambiguities,
            "unresolved_oriented_samples": unresolved_oriented,
            "nearest_component_mismatches": component_conflicts,
            "unresolved_component_samples": unresolved_components,
            "maximum_confirmed_aligned_distance_mm": maximum_aligned_distance}


def _compute_feature_segments(mesh, angle_degrees=45):
    edges = np.concatenate([mesh.faces[:, [0, 1]], mesh.faces[:, [1, 2]], mesh.faces[:, [2, 0]]])
    unique, first, inverse, counts = np.unique(np.sort(edges, axis=1), axis=0,
                                               return_index=True, return_inverse=True, return_counts=True)
    last = np.zeros(len(unique), dtype=np.int64)
    np.maximum.at(last, inverse, np.arange(len(edges)))
    normals = mesh._analysis[2]
    dots = np.einsum("ij,ij->i", normals[first % len(mesh.faces)], normals[last % len(mesh.faces)])
    chosen = (counts == 1) | ((counts == 2) & (dots <= np.cos(np.deg2rad(angle_degrees))))
    return mesh.vertices[unique[chosen]]


def _feature_segments(mesh, angle_degrees=45):
    return mesh._features_45 if angle_degrees == 45 else _compute_feature_segments(mesh, angle_degrees)


def _feature_distance(source, target, sample_budget=96, check_cancel=lambda: None,
                      max_comparisons=20000000):
    """Bounded symmetric boundary/45-degree-crease proximity, not full coverage."""
    if type(max_comparisons) is not int or not 1 <= max_comparisons <= 100000000:
        raise ValueError("Feature comparison budget must be in [1,100000000]")
    check_cancel()
    first, second = _feature_segments(source), _feature_segments(target)
    report = {"status": "sampled", "crease_angle_degrees": 45, "source_edges": len(first),
              "target_edges": len(second), "global_coverage_certified": False, "maximum_distance_mm": 0.0,
              "segment_comparisons": 0, "comparison_budget": max_comparisons, "completed_sample_points": 0}
    for src, dst in ((first, second), (second, first)):
        check_cancel()
        if not len(src):
            continue
        if not len(dst):
            report.update(status="missing-corresponding-feature")
            return report
        ids = np.unique(np.linspace(0, len(src)-1, min(len(src), sample_budget//3), dtype=np.int64))
        points = np.vstack([src[ids, 0], src[ids, 1], src[ids].mean(axis=1)])
        for point in points:
            minimum = np.inf
            for offset in range(0, len(dst), 2048):
                check_cancel()
                segments = dst[offset:offset+2048]
                if report["segment_comparisons"] + len(segments) > max_comparisons:
                    report.update(status="unproven-budget")
                    return report
                report["segment_comparisons"] += len(segments)
                direction = segments[:, 1] - segments[:, 0]
                lengths = np.einsum("ij,ij->i", direction, direction)
                if np.any(lengths <= 0):
                    raise ValueError("Degenerate feature edge")
                t = np.clip(np.einsum("ij,ij->i", point-segments[:, 0], direction)/lengths, 0, 1)
                distances = np.linalg.norm(point-segments[:, 0]-t[:, None]*direction, axis=1)
                minimum = min(minimum, float(distances.min()))
            report["maximum_distance_mm"] = max(report["maximum_distance_mm"], minimum)
            report["completed_sample_points"] += 1
    return report


def validate_candidate(original, candidate, tolerance_mm=0.05, relative_limit=0.005, sample_count=4096,
                       enhanced=False, check_cancel=lambda: None, progress=lambda phase: None):
    """Fail closed on geometry/topology errors; sampling is not a Hausdorff proof."""
    if not np.isfinite(tolerance_mm) or tolerance_mm <= 0 or not np.isfinite(relative_limit) or relative_limit <= 0:
        raise ValueError("Validation tolerances must be finite and positive")
    report = {"accepted": False, "reasons": [], "metrics": {},
              "limits": {"absolute_mm": tolerance_mm, "relative_area_volume": relative_limit,
                         "requested_samples_per_direction": sample_count},
              "coordinate_policy": "candidate is float32-quantized and exact-welded before validation",
              "surface_check": "deterministic bidirectional sampled deviation; NOT a Hausdorff bound or self-intersection proof",
              "orientation_check": "opposed nearest normals require an exact same-facing surface match within absolute tolerance",
              "self_intersection": {"status": "unproven", "global_absence_certified": False},
              "timings_seconds": {}}
    active_phase, phase_started = None, time.monotonic()
    def phase(name):
        nonlocal active_phase, phase_started
        now = time.monotonic()
        if active_phase is not None:
            report["timings_seconds"][active_phase] = round(now-phase_started, 6)
        active_phase, phase_started = name, now
        if name is not None:
            report["last_phase"] = name
            progress(name)
    try:
        phase("quantize-and-analyze")
        check_cancel()
        candidate = quantize_stl(candidate)
        before, after = original._analysis[0], candidate._analysis[0]
        report["original"] = mesh_stats(original)
        report["candidate"] = mesh_stats(candidate)
        attribute_matches = original.uniform_attribute == candidate.uniform_attribute
        header_matches = original.binary_header is None or original.binary_header == candidate.binary_header
        report["metrics"].update(uniform_attribute_preserved=attribute_matches,
                                  original_binary_header_preserved=header_matches)
        if not attribute_matches:
            report["reasons"].append("uniform STL attribute changed or was discarded")
        if not header_matches:
            report["reasons"].append("original binary STL header changed or was discarded")
        for label, stats in (("original", before), ("candidate", after)):
            report["reasons"].extend(label + ": " + reason for reason in _invalid_reasons(stats))
        if report["reasons"]:
            return report
        phase("topology-area-volume")
        if before["component_count"] != after["component_count"]:
            report["reasons"].append("component count changed")
        def topology(stats):
            return sorted((item["euler_characteristic"], item["boundary_edges"], item["boundary_loops"],
                           item["watertight_consistent"]) for item in stats["components"])
        if topology(before) != topology(after):
            report["reasons"].append("component topology/boundary signature changed")
        bounds_error = float(np.max(np.abs(np.array(before["bounds_mm"]) - np.array(after["bounds_mm"]))))
        area_error = abs(after["surface_area_mm2"] / before["surface_area_mm2"] - 1)
        report["metrics"].update(bounds_max_error_mm=bounds_error, relative_surface_area_change=area_error)
        if bounds_error > tolerance_mm:
            report["reasons"].append("bounds exceed absolute tolerance")
        if area_error > relative_limit:
            report["reasons"].append("surface area exceeds relative tolerance")
        volume_suitable = any(item["volume_check_suitable"] for item in before["components"])
        volume_checks = []
        report["metrics"].update(volume_check_applicable=volume_suitable,
                                  volume_components_checked=0, volume_component_checks=volume_checks)
        component_map = reverse_map = None
        if (volume_suitable or enhanced) and before["component_count"] == after["component_count"]:
            from scipy.optimize import linear_sum_assignment
            old_bounds = np.asarray([item["bounds_mm"] for item in before["components"]]).reshape(-1, 6)
            new_bounds = np.asarray([item["bounds_mm"] for item in after["components"]]).reshape(-1, 6)
            if len(old_bounds) > 1024:
                raise ValueError("Too many components for bounded volume matching")
            costs = np.max(np.abs(old_bounds[:, None, :] - new_bounds[None, :, :]), axis=2)
            if enhanced:
                signatures = lambda stats: [(x["euler_characteristic"], x["boundary_loops"],
                                             x["watertight_consistent"]) for x in stats["components"]]
                for row, signature in enumerate(signatures(before)):
                    check_cancel()
                    costs[row, [signature != other for other in signatures(after)]] = np.inf
            rows, columns = linear_sum_assignment(costs)
            component_map = np.empty(len(rows), dtype=np.int64)
            reverse_map = np.empty(len(rows), dtype=np.int64)
            component_map[rows], reverse_map[columns] = columns, rows
            errors = []
            for row, column in zip(rows, columns):
                # Open or zero-volume parts must not disable checks on the other
                # closed components, and must never be used as divisors here.
                if not before["components"][row]["volume_check_suitable"]:
                    continue
                old_volume = before["components"][row]["signed_volume_mm3"]
                new_volume = after["components"][column]["signed_volume_mm3"]
                relative_change = abs(new_volume / old_volume - 1)
                errors.append(relative_change)
                volume_checks.append({"original_component": int(row), "candidate_component": int(column),
                                      "original_signed_volume_mm3": old_volume,
                                      "candidate_signed_volume_mm3": new_volume,
                                      "relative_volume_change": relative_change,
                                      "within_limit": bool(relative_change <= relative_limit)})
            volume_error = max(errors, default=0.0)
            report["metrics"]["volume_components_checked"] = len(volume_checks)
            report["metrics"]["maximum_component_relative_volume_change"] = volume_error
            if volume_error > relative_limit:
                report["reasons"].append("component volume/orientation exceeds relative tolerance")
        if report["reasons"]:
            return report  # Do not spend proximity work on an already rejected mesh.
        if enhanced:
            from bed_mesh_certificates import intersection_probe
            phase("feature-distance")
            feature = _feature_distance(original, candidate, check_cancel=check_cancel)
            report["metrics"]["feature_check"] = feature
            if feature["status"] != "sampled" or feature["maximum_distance_mm"] > tolerance_mm:
                report["reasons"].append("boundary/crease feature check failed or is unproven")
                return report
            phase("intersection-probe")
            report["self_intersection"] = intersection_probe(candidate, check_cancel=check_cancel)
            if report["self_intersection"]["status"] == "intersection-found":
                report["reasons"].append("exact predicate found nonadjacent triangle intersection")
                return report
        phase("surface-original-to-candidate")
        forward = _surface_distance(original, candidate, sample_count, tolerance_mm,
                                     component_map if enhanced else None, check_cancel)
        report["metrics"]["original_to_candidate"] = forward
        phase("surface-candidate-to-original")
        reverse = _surface_distance(candidate, original, sample_count, tolerance_mm,
                                     reverse_map if enhanced else None, check_cancel)
        report["metrics"].update(original_to_candidate=forward, candidate_to_original=reverse)
        if max(forward["maximum_distance_mm"], reverse["maximum_distance_mm"]) > tolerance_mm:
            report["reasons"].append("sampled surface deviation exceeds absolute tolerance")
        if forward["unresolved_oriented_samples"] or reverse["unresolved_oriented_samples"]:
            report["reasons"].append("sampled surface winding reversed")
        if forward["unresolved_component_samples"] or reverse["unresolved_component_samples"]:
            report["reasons"].append("sampled component surface correspondence missing")
        report["accepted"] = not report["reasons"]
    except (ValueError, IndexError, FloatingPointError) as error:
        report["reasons"].append(str(error))
    except TimeoutError as error:
        report["reasons"].append("Validation incomplete: " + str(error))
        error.validation_report = report
        raise
    finally:
        phase(None)
    return report


def write_stl(mesh, path):
    """Write a NEW STL, preserving uniform attributes/header; never overwrite."""
    mesh = quantize_stl(mesh)
    stats, _, normals, _ = mesh._analysis
    if stats["degenerate_faces"]:
        raise ValueError("Refusing to serialize degenerate triangles")
    records = np.zeros(len(mesh.faces), dtype=_FACET_DTYPE)
    records["normal"] = normals
    records["vertices"] = mesh.vertices[mesh.faces]
    records["attribute"] = mesh.uniform_attribute
    header = mesh.binary_header if mesh.binary_header is not None else _DEFAULT_HEADER
    with Path(path).open("xb") as stream:
        stream.write(header)
        stream.write(struct.pack("<I", len(mesh.faces)))
        stream.write(records.tobytes())
