"""Small, bounded exact-predicate checks; no general remeshing or repair.

Coordinates are interpreted as their exact stored binary floating-point values.
The planar operation proves only disjoint strictly convex interior fan replacements.
The intersection probe examines a bounded subset, never a global certificate.
"""

from fractions import Fraction

import numpy as np


def _point(value):
    return tuple(Fraction(float(x)) for x in value)


def _sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _orient(a, b, c):
    return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])


def _project(points, normal):
    axis = max(range(3), key=lambda i: abs(normal[i]))
    return [tuple(x for i, x in enumerate(point) if i != axis) for point in points]


def planar_convex_fans(vertices, faces, max_candidates=256, check_cancel=lambda: None):
    """Remove centers of exactly planar, strictly convex closed 3--12-fans.

No new coordinates, changed boundary edges, overlapping edited fans, snapping,
or approximate plane tests. Exact global half-plane tests prove a simple convex
outer ring and a strictly interior center: both triangulations cover that same
polygon exactly once with the same orientation. New diagonals join existing
boundary vertices. Concave, collinear, open, and non-coplanar fans are skipped.
"""
    report = {"method": "exact-dyadic-convex-interior-fan", "proved_patches": 0,
              "examined_vertices": 0, "scope": "disjoint strictly convex closed 3--12-fans only",
              "surface_preservation_proved": True, "budget_exhausted": False}
    if not 0 <= max_candidates <= 4096:
        raise ValueError("Exact planar candidate budget must be in [0,4096]")
    check_cancel()
    if len(faces) > 300000 or not np.array_equal(vertices.astype(np.float32).astype(np.float64), vertices):
        report["skipped"] = "requires <=300000 faces and exactly float32-representable coordinates"
        return faces.copy(), report
    counts = np.bincount(faces.ravel(), minlength=len(vertices))
    eligible = np.flatnonzero((counts >= 3) & (counts <= 12))
    report["budget_exhausted"] = len(eligible) > max_candidates
    selected = eligible[:max_candidates]
    incidence = {int(v): [] for v in selected}
    hits = np.flatnonzero(np.any(np.isin(faces, selected), axis=1))
    for index in hits:
        if int(index) % 128 == 0:
            check_cancel()
        for vertex in faces[index]:
            if int(vertex) in incidence:
                incidence[int(vertex)].append(int(index))
    face_keys = {tuple(sorted(map(int, face))) for face in faces}
    removed, blocked, replacements = set(), set(), []
    for vertex in selected:
        check_cancel()
        vertex = int(vertex)
        report["examined_vertices"] += 1
        neighbors, ring_faces = {}, incidence[vertex]
        if not 3 <= len(ring_faces) <= 12 or vertex in blocked:
            continue
        valid = True
        for index in ring_faces:
            tri = list(map(int, faces[index]))
            if len(set(tri)) != 3 or index in removed:
                valid = False
                break
            at = tri.index(vertex)
            a, b = tri[(at+1) % 3], tri[(at+2) % 3]
            if a in neighbors:
                valid = False
                break
            neighbors[a] = b
        if not valid or len(neighbors) != len(ring_faces):
            continue
        ring = [min(neighbors)]
        for _ in range(len(neighbors)-1):
            following = neighbors.get(ring[-1])
            if following is None or following in ring:
                break
            ring.append(following)
        if len(ring) != len(neighbors) or neighbors.get(ring[-1]) != ring[0]:
            continue
        new_faces = [(ring[0], ring[i], ring[i+1]) for i in range(1, len(ring)-1)]
        if any(v in blocked for v in ring) or any(tuple(sorted(f)) in face_keys for f in new_faces):
            continue
        p, *boundary = map(_point, vertices[[vertex, *ring]])
        normal = _cross(_sub(boundary[1], boundary[0]), _sub(boundary[2], boundary[0]))
        if not any(normal) or any(_dot(_sub(q, boundary[0]), normal) != 0 for q in [p, *boundary[3:]]):
            continue
        pp, *projected = _project([p, *boundary], normal)
        orientation = _orient(*projected[:3])
        # Global half-plane tests reject self-crossing star orderings too.
        if any(_orient(projected[i], projected[(i+1) % len(ring)], q)*orientation <= 0
               for i in range(len(ring))
               for q in [pp, *(v for j, v in enumerate(projected) if j not in (i, (i+1) % len(ring)))]):
            continue
        removed.update(ring_faces)
        blocked.update((vertex, *ring))
        replacements.extend(new_faces)
        report["proved_patches"] += 1
    check_cancel()
    keep = np.ones(len(faces), dtype=bool)
    if removed:
        keep[list(removed)] = False
        result = np.vstack([faces[keep], np.asarray(replacements, dtype=np.int64)])
    else:
        result = faces.copy()
    report["removed_triangles"] = len(faces) - len(result)
    return result, report


def triangles_intersect_exact(first, second):
    """Exact intersection, including touching, for two nondegenerate triangles."""
    a, b = tuple(map(_point, first)), tuple(map(_point, second))
    normals = [_cross(_sub(t[1], t[0]), _sub(t[2], t[0])) for t in (a, b)]
    if not all(any(n) for n in normals):
        raise ValueError("Intersection predicate requires nondegenerate triangles")
    distances = [[_dot(_sub(p, t[0]), n) for p in s]
                 for s, t, n in ((a, b, normals[1]), (b, a, normals[0]))]
    if any(all(d > 0 for d in ds) or all(d < 0 for d in ds) for ds in distances):
        return False
    if all(d == 0 for d in distances[0]):
        aa = _project(a, normals[0])
        bb = _project(b, normals[0])
        def inside(p, tri):
            orientation = _orient(*tri)
            return all(_orient(tri[i], tri[(i+1) % 3], p)*orientation >= 0 for i in range(3))
        if any(inside(p, bb) for p in aa) or any(inside(p, aa) for p in bb):
            return True
        for i in range(3):
            p, q = aa[i], aa[(i+1) % 3]
            for j in range(3):
                r, s = bb[j], bb[(j+1) % 3]
                if (_orient(p, q, r)*_orient(p, q, s) <= 0
                        and _orient(r, s, p)*_orient(r, s, q) <= 0
                        and all(max(min(p[k], q[k]), min(r[k], s[k])) <=
                                min(max(p[k], q[k]), max(r[k], s[k])) for k in range(2))):
                    return True
        return False
    for src, dst, normal, ds in ((a, b, normals[1], distances[0]), (b, a, normals[0], distances[1])):
        for i in range(3):
            p, q, d, e = src[i], src[(i+1) % 3], ds[i], ds[(i+1) % 3]
            points = [p] if d == 0 else []
            if d*e < 0:
                weight = d / (d-e)
                points.append(tuple(x + weight*(y-x) for x, y in zip(p, q)))
            for point in points:
                if all(_dot(_cross(_sub(dst[(j+1) % 3], dst[j]), _sub(point, dst[j])), normal) >= 0
                       for j in range(3)):
                    return True
    return False


def intersection_probe(mesh, max_faces=64, max_pairs=256, check_cancel=lambda: None):
    """Reject demonstrated nonadjacent intersections; never certify absence."""
    if not 1 <= max_faces <= 256 or not 1 <= max_pairs <= 4096:
        raise ValueError("Intersection probe budget is outside supported limits")
    target = mesh._spatial_query.mesh
    selected = np.unique(np.linspace(0, len(mesh.faces)-1, min(max_faces, len(mesh.faces)), dtype=np.int64))
    result = {"status": "unproven", "global_absence_certified": False, "tested_pairs": 0,
              "selected_faces": len(selected), "pair_budget": max_pairs,
              "limitation": "bounded nonadjacent-face probe; shared-vertex pairs and unsampled faces are not certified"}
    seen = set()
    for index in selected:
        check_cancel()
        tri = target.triangles[index]
        for other in target.triangles_tree.intersection(np.concatenate([tri.min(axis=0), tri.max(axis=0)])):
            check_cancel()
            if other == index:
                continue
            pair = tuple(sorted((int(index), int(other))))
            if pair in seen:
                continue
            seen.add(pair)
            # Bound bookkeeping as well as exact predicate work.
            if len(seen) > max_pairs*8 or result["tested_pairs"] >= max_pairs:
                result["budget_exhausted"] = True
                return result
            if set(map(int, mesh.faces[index])) & set(map(int, mesh.faces[other])):
                continue
            result["tested_pairs"] += 1
            if triangles_intersect_exact(tri, target.triangles[other]):
                result.update(status="intersection-found", face_pair=list(pair))
                return result
    result["budget_exhausted"] = False
    return result
