"""Immutable, bounded spatial queries for the optional build-time mesh QA tools.

The closest-point arithmetic and face-normal tie resolution are executed from
the installed, pinned trimesh 4.12.2 function itself. A private function/global
namespace substitutes only its candidate collector; no module is monkeypatched.
The nearest-vertex/AABB collector follows trimesh.proximity.nearby_faces, with
one cached cKDTree and Rtree per immutable mesh instead of rebuilding the former
for every batch. Accepted queries retain upstream candidate ordering and math.

Source: https://github.com/mikedh/trimesh/blob/4.12.2/trimesh/proximity.py
Trimesh is MIT licensed, Copyright (c) 2023 Michael Dawson-Haggerty:

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from dataclasses import dataclass
from itertools import islice
from types import FunctionType

import numpy as np
from scipy.spatial import cKDTree
import trimesh
from trimesh.constants import tol


def _immutable_array(value, dtype):
    """Bytes-backed arrays cannot be made writable again with setflags."""
    array = np.asarray(value, dtype=dtype, order="C")
    return np.frombuffer(array.tobytes(order="C"), dtype=dtype).reshape(array.shape)


@dataclass(frozen=True, eq=False)
class _IntersectionTree:
    _index: object

    def intersection(self, bounds):
        return self._index.intersection(bounds)


@dataclass(frozen=True, eq=False)
class _MeshView:
    vertices: np.ndarray
    faces: np.ndarray
    triangles: np.ndarray
    face_normals: np.ndarray
    triangles_tree: _IntersectionTree


@dataclass(frozen=True, eq=False, init=False)
class SpatialQuery:
    """A read-only mesh snapshot and reusable trees, without repairs or welding.

    ``max_candidates`` bounds total candidate pairs within each at-most-32-point
    arithmetic batch. Exceeding it raises ValueError (indeterminate QA), never
    silently discards faces or approximates a distance. The bound is not a hard
    process-memory quota: tree storage and upstream native allocations remain
    subject to the caller's process resource guard.
    """

    mesh: _MeshView
    _kdtree: object
    max_candidates: int
    batch_size: int

    def __init__(self, vertices, faces, *, max_candidates=250_000, batch_size=32):
        if trimesh.__version__ != "4.12.2":
            raise ValueError("Spatial QA requires pinned trimesh==4.12.2 for exact tie-resolution parity")
        if (type(max_candidates) is not int or max_candidates < 1
                or type(batch_size) is not int or not 1 <= batch_size <= 32):
            raise ValueError("Candidate budget must be positive and batch_size must be between 1 and 32")
        vertices = _immutable_array(vertices, np.float64)
        raw_faces = np.asarray(faces)
        if raw_faces.dtype.kind not in "iu":
            raise ValueError("Face indices must be integers")
        faces = _immutable_array(raw_faces, np.int64)
        if (vertices.ndim != 2 or vertices.shape[1:] != (3,) or not len(vertices)
                or faces.ndim != 2 or faces.shape[1:] != (3,) or not len(faces)
                or not np.isfinite(vertices).all()
                or faces.min() < 0 or faces.max() >= len(vertices)):
            raise ValueError("Spatial QA requires finite vertices and valid nonempty triangle indices")
        # process=False / validate=False preserve face order, duplicate vertices,
        # winding and disconnected/coincident sheets exactly as provided.
        source = trimesh.Trimesh(vertices=vertices, faces=faces, process=False, validate=False)
        tree = source.triangles_tree
        nearest_vertices = source.vertices[source.referenced_vertices]
        # cKDTree must own stable storage, never borrow mutable caller arrays.
        kdtree = cKDTree(nearest_vertices, copy_data=True)
        kdtree.data.setflags(write=False)
        view = _MeshView(vertices, faces,
                         _immutable_array(source.triangles, np.float64),
                         _immutable_array(source.face_normals, np.float64),
                         _IntersectionTree(tree))
        object.__setattr__(self, "mesh", view)
        object.__setattr__(self, "_kdtree", kdtree)
        object.__setattr__(self, "max_candidates", max_candidates)
        object.__setattr__(self, "batch_size", batch_size)

    def _nearby_faces(self, points, check_cancel):
        check_cancel()
        # Same float64 query, tol.merge expansion and AABB as upstream.
        distance_vertex = self._kdtree.query(points)[0].reshape((-1, 1))
        distance_vertex += tol.merge
        bounds = np.column_stack((points - distance_vertex, points + distance_vertex))
        candidates = []
        total = 0
        for bound in bounds:
            check_cancel()
            iterator = iter(self.mesh.triangles_tree.intersection(bound))
            faces = []
            while True:
                check_cancel()
                # Never materialize an unbounded Rtree intersection. Fetch at
                # most one item beyond the explicit limit to prove exhaustion.
                chunk = list(islice(iterator, min(1024, self.max_candidates - total + 1)))
                total += len(chunk)
                if total > self.max_candidates:
                    raise ValueError("Spatial proximity indeterminate: candidate-pair budget exceeded")
                faces.extend(chunk)
                if not chunk:
                    break
            candidates.append(faces)
        return candidates

    def closest_point(self, points, check_cancel=lambda: None):
        """Return upstream closest coordinates, distances and face IDs exactly."""
        check_cancel()
        points = np.asanyarray(points, dtype=np.float64)
        if points.ndim != 2 or points.shape[1:] != (3,) or not len(points) or not np.isfinite(points).all():
            raise ValueError("Query points must be a nonempty finite (n, 3) array")
        upstream = trimesh.proximity.closest_point
        # Executing the original code object avoids a divergent copied version
        # of the subtle distance-sort/normal tie logic. Globals belong only to
        # this call; concurrent query cancellation cannot leak between callers.
        private_globals = dict(upstream.__globals__)
        private_globals["nearby_faces"] = lambda mesh, values: self._nearby_faces(values, check_cancel)
        closest = FunctionType(upstream.__code__, private_globals, upstream.__name__,
                               upstream.__defaults__, upstream.__closure__)
        outputs = []
        for start in range(0, len(points), self.batch_size):
            check_cancel()
            outputs.append(closest(self.mesh, points[start:start + self.batch_size]))
            check_cancel()
        return tuple(np.concatenate([part[index] for part in outputs]) for index in range(3))
