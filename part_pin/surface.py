"""Free-form cut surfaces — the geometry behind "Edit on Surface".

A surface cut is stored as a **height field over its own local XY plane**:
the cut object's transform defines the base frame (local Z is the cut
normal) and a handful of control points, kept in local space, pull the
surface up and down. The surface interpolates those control points
exactly (Wendland RBF) and flattens back to the base plane away from
them.

Storing the cut as a height field h(u, v) — rather than an arbitrary
deformable sheet — is what keeps the result reliable: a graph over a
plane can never fold back on itself, so extruding it always yields a
closed cutter volume that splits the model in exactly two, just like the
plane half-space it grew out of.

Control points are placed on the model's surface, along the line where
the cut meets it, which is what makes them draggable "on the model".
"""

import bmesh
import bpy
from mathutils import Matrix, Vector

from . import core

# Grid used for the viewport preview mesh; the cutter uses a finer one.
DISPLAY_RES = 24

# Thickness of the severing slab for a localized cut, as a fraction of the
# model's size. This much material is removed at the seam — 0.02 mm on a
# 200 mm model, far below anything a printer resolves.
SEAM_FACTOR = 1e-4


def evaluated(obj):
    """The object with modifiers applied — what actually gets cut."""
    return obj.evaluated_get(bpy.context.evaluated_depsgraph_get())


def project_to_surface(target, world_point):
    """Nearest point on the model's surface, in world space."""
    obj = evaluated(target)
    ok, location, _normal, _index = obj.closest_point_on_mesh(
        obj.matrix_world.inverted() @ world_point)
    return obj.matrix_world @ location if ok else world_point.copy()


# ----------------------------------------------------------------------
# Section lines: where a cut meets the model surface
# ----------------------------------------------------------------------

def order_wire_loops(bm, min_points=3):
    """Order loose wire edges into vertex chains. Returns [(verts, cyclic)]."""
    adj = {}
    for e in bm.edges:
        adj.setdefault(e.verts[0], []).append(e)
        adj.setdefault(e.verts[1], []).append(e)
    used = set()

    def walk(start):
        chain = [start]
        v = start
        prev = None
        while True:
            nxt = None
            for e in adj.get(v, ()):
                if e not in used and e is not prev:
                    nxt = e
                    break
            if nxt is None:
                return chain, False
            used.add(nxt)
            v = nxt.other_vert(v)
            prev = nxt
            if v is start:
                return chain, True
            chain.append(v)

    # Walk in coordinate order: bmesh elements hash by address, so plain
    # dict order would start each loop at a different vertex from run to
    # run and shift the control points around it.
    ordered = sorted(adj, key=lambda v: (round(v.co.x, 9), round(v.co.y, 9),
                                         round(v.co.z, 9)))

    loops = []
    # Open chains first, so their ends are not consumed mid-walk.
    for v in ordered:
        if len(adj[v]) == 1 and any(e not in used for e in adj[v]):
            chain, cyclic = walk(v)
            if len(chain) >= min_points:
                loops.append((chain, cyclic))
    for v in ordered:
        if any(e not in used for e in adj[v]):
            chain, cyclic = walk(v)
            if len(chain) >= min_points:
                loops.append((chain, cyclic))
    return loops


def plane_section_loops(target, matrix_world):
    """World-space loops where a plane meets the model surface."""
    loc, rot, _scale = matrix_world.decompose()
    normal = rot @ Vector((0.0, 0.0, 1.0))

    dg = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(target.evaluated_get(dg))
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.transform(target.matrix_world)
    bmesh.ops.bisect_plane(
        bm,
        geom=list(bm.verts) + list(bm.edges) + list(bm.faces),
        dist=1e-6,
        plane_co=loc,
        plane_no=normal,
        clear_inner=True,
        clear_outer=True,
    )
    loops = [[v.co.copy() for v in chain]
             for chain, _cyclic in order_wire_loops(bm)]
    bm.free()
    bpy.data.meshes.remove(me)
    loops.sort(key=len, reverse=True)
    return loops


def curve_section_loops(target, cut, samples=48):
    """Where a drawn (extruded) cut meets the model surface.

    Rays are fired along the extrusion direction at points across the
    stroke; the front hits and the reversed back hits form one loop.
    """
    pts, _cyclic = core.sample_cut_curve(cut, resolution=24)
    if len(pts) < 2:
        return []
    m = cut.matrix_world
    stroke = resample_loop([Vector((p.x, p.y, 0.0)) for p in pts],
                           samples, cyclic=False)
    extrude = (m.to_quaternion() @ Vector((0.0, 0.0, 1.0))).normalized()
    far = core.bbox_diagonal(target) * 2.0

    obj = evaluated(target)
    inv = obj.matrix_world.inverted()
    inv3 = inv.to_3x3()
    front, back = [], []
    for p in stroke:
        world = m @ p
        for direction, bucket in ((extrude, front), (-extrude, back)):
            origin = world - direction * far
            hit, loc, _n, _i = obj.ray_cast(inv @ origin, inv3 @ direction)
            if hit:
                bucket.append(obj.matrix_world @ loc)
    loop = front + list(reversed(back))
    return [loop] if len(loop) >= 4 else []


def resample_loop(points, count, cyclic=True):
    """Resample a polyline to `count` evenly arc-length-spaced points."""
    if len(points) < 2 or count < 2:
        return [p.copy() for p in points[:max(count, 1)]]
    pts = list(points) + ([points[0]] if cyclic else [])
    segs = [(pts[i + 1] - pts[i]).length for i in range(len(pts) - 1)]
    total = sum(segs)
    if total <= 1e-12:
        return [points[0].copy() for _ in range(count)]
    step = total / count if cyclic else total / (count - 1)
    out = []
    i, acc = 0, 0.0
    for k in range(count):
        want = step * k
        while i < len(segs) - 1 and acc + segs[i] < want:
            acc += segs[i]
            i += 1
        t = (want - acc) / segs[i] if segs[i] > 1e-12 else 0.0
        out.append(pts[i].lerp(pts[i + 1], min(max(t, 0.0), 1.0)))
    return out


def newell_normal(points):
    """Area-weighted normal of a (near-planar) closed polygon."""
    n = Vector((0.0, 0.0, 0.0))
    for i, a in enumerate(points):
        b = points[(i + 1) % len(points)]
        n.x += (a.y - b.y) * (a.z + b.z)
        n.y += (a.z - b.z) * (a.x + b.x)
        n.z += (a.x - b.x) * (a.y + b.y)
    return n


def polygon_area(uvs):
    """Signed shoelace area of a closed 2D polygon."""
    total = 0.0
    count = len(uvs)
    for i in range(count):
        x1, y1 = uvs[i]
        x2, y2 = uvs[(i + 1) % count]
        total += x1 * y2 - x2 * y1
    return total * 0.5


def polygon_perimeter(uvs):
    count = len(uvs)
    return sum(((uvs[(i + 1) % count][0] - uvs[i][0]) ** 2
                + (uvs[(i + 1) % count][1] - uvs[i][1]) ** 2) ** 0.5
               for i in range(count))


def polygon_roundness(uvs):
    """4πA/P²: 1 for a circle, ~0 for a sliver. Measures whether a cut line
    still encloses a usable area once flattened onto the cut's plane."""
    perimeter = polygon_perimeter(uvs)
    if perimeter <= 1e-12:
        return 0.0
    return 4.0 * 3.141592653589793 * abs(polygon_area(uvs)) / (perimeter ** 2)


def loop_length(points):
    count = len(points)
    return sum((points[(i + 1) % count] - points[i]).length
               for i in range(count))


def fit_plane(points):
    """Best-fit plane through world points, as (origin, unit normal)."""
    origin = sum(points, Vector()) / len(points)
    normal = None
    try:
        import numpy as np
        rows = np.array([[p.x - origin.x, p.y - origin.y, p.z - origin.z]
                         for p in points])
        # Smallest singular vector of the centred points = plane normal.
        _u, _s, vh = np.linalg.svd(rows, full_matrices=False)
        normal = Vector(tuple(float(x) for x in vh[-1]))
    except Exception:
        normal = None
    if normal is None or normal.length < 1e-12:
        normal = newell_normal(points)
    if normal.length < 1e-12:
        normal = Vector((0.0, 0.0, 1.0))
    normal.normalize()
    if normal.dot(newell_normal(points)) < 0.0:
        normal = -normal
    return origin, normal


def refit_frame(cut):
    """Re-align a surface cut's base plane to its current cut line.

    A cut is stored as a height field over its own plane, so once the line
    has been dragged well away from the plane it started on — a collar
    round a limb, say, which ends up nearly perpendicular to the original
    cut plane — that plane can no longer describe it and the enclosed
    region flattens to nothing. Re-fitting the plane to the line keeps the
    representation able to express whatever the user drew.

    World positions of the control points and connectors are preserved.
    """
    loops = control_loops(cut)
    matrix = cut.matrix_world
    world = [[matrix @ p for p in loop] for loop in loops]
    if not world or max(len(loop) for loop in world) < 3:
        return False

    # Fit to one line rather than to all of them at once: a cut has a single
    # plane, and averaging lines lying in different planes gives a
    # compromise that suits none of them. The line to follow is the one the
    # user last edited, falling back to the longest.
    main = None
    if 0 <= cut.pp_main_loop < len(world) \
            and len(world[cut.pp_main_loop]) >= 3:
        main = world[cut.pp_main_loop]
    if main is None:
        main = max(world, key=loop_length)
    origin, normal = fit_plane(main)
    frame = Matrix.LocRotScale(origin, normal.to_track_quat('Z', 'Y'),
                               Vector((1.0, 1.0, 1.0)))
    inverse = frame.inverted()

    connectors = [(c, c.matrix_world.copy())
                  for c in core.cut_connectors(bpy.context.scene, cut)]
    cut.matrix_world = frame
    for conn, world_matrix in connectors:
        conn.matrix_parent_inverse = inverse
        conn.matrix_world = world_matrix

    points, ids = [], []
    for index, loop in enumerate(world):
        for point in loop:
            points.append(inverse @ point)
            ids.append(index)
    store_control_points(cut, points, ids)
    return True


def loop_quality(cut, minimum_roundness=0.02, min_alignment=0.7):
    """Sort a cut's lines into usable and not.

    Returns (usable polygons, problem, warning). A cut has one plane, so
    lines that no longer share it — the leftovers on other features after
    one line has been dragged round a limb — cannot be cut alongside it.
    Those are skipped with a warning rather than failing the whole cut. A
    line qualifies when it encloses an area, does not cross itself, and its
    own plane is within ~45° of the cut's.
    """
    loops = control_loops(cut)
    if not loops:
        return [], "this cut has no cut line to work from", None

    usable = []
    for index, loop in enumerate(loops):
        poly = [(p.x, p.y) for p in loop]
        if polygon_roundness(poly) < minimum_roundness \
                or polygon_self_intersects(poly):
            continue
        if len(loop) >= 3:
            _origin, normal = fit_plane(loop)
            if abs(normal.z) < min_alignment:  # local Z is the cut normal
                continue
        usable.append(index)
    if not usable:
        if len(loops) == 1:
            return [], ("this cut line does not enclose an area to cut off. "
                        "Drag its points into a loop that goes right round "
                        "the part you want removed"), None
        return [], ("none of this cut's lines enclose an area to cut off"), None

    warning = None
    skipped = len(loops) - len(usable)
    if skipped:
        warning = (f"{skipped} of {len(loops)} cut lines were ignored — they "
                   "do not share a plane with the rest. Hover each one and "
                   "press Alt+X to remove it, or use a separate cut per region")
    return usable, None, warning


def usable_loop_indices(cut):
    indices, _problem, _warning = loop_quality(cut)
    return indices


def cut_line_problem(cut, minimum_roundness=0.02):
    """Why this cut cannot be made at all, or None if it can."""
    _usable, problem, _warning = loop_quality(cut, minimum_roundness)
    return problem


def polygon_self_intersects(uvs):
    """True if the closed 2D polygon crosses itself (O(n²), n is small)."""
    n = len(uvs)

    def side(a, b, c):
        return ((b[0] - a[0]) * (c[1] - a[1])
                - (b[1] - a[1]) * (c[0] - a[0]))

    def crosses(p1, p2, p3, p4):
        d1, d2 = side(p3, p4, p1), side(p3, p4, p2)
        d3, d4 = side(p1, p2, p3), side(p1, p2, p4)
        return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))

    for i in range(n):
        a, b = uvs[i], uvs[(i + 1) % n]
        for j in range(i + 1, n):
            if j == i or (j + 1) % n == i or (i + 1) % n == j:
                continue
            if crosses(a, b, uvs[j], uvs[(j + 1) % n]):
                return True
    return False


# ----------------------------------------------------------------------
# The height field
# ----------------------------------------------------------------------

class HeightField:
    """Smooth h(u, v) passing exactly through the control points.

    Gaussian RBF interpolation: exact at every control point and decaying
    back to the base plane (h = 0) further than `falloff` spacings away,
    which is what makes a dragged point feel like a local pull.
    """

    def __init__(self, points, falloff=2.0):
        self.nodes = [(p.x, p.y) for p in points]
        self.heights = [p.z for p in points]
        self.radius = self._radius(falloff)
        # Control points projected onto the model land a hair off the base
        # plane, so "flat" needs a tolerance — testing for non-zero floats
        # would treat every unedited cut as deformed.
        span = 0.0
        if self.nodes:
            span = max(max(u for u, _v in self.nodes)
                       - min(u for u, _v in self.nodes),
                       max(v for _u, v in self.nodes)
                       - min(v for _u, v in self.nodes))
        tolerance = max(span, 1e-12) * 1e-7
        self._flat = not self.nodes or all(abs(h) <= tolerance
                                          for h in self.heights)
        self.weights = self._solve()

    def _radius(self, falloff):
        n = len(self.nodes)
        if n < 2:
            return 1.0
        spacing = []
        for i, (u, v) in enumerate(self.nodes):
            best = min((((u - a) ** 2 + (v - b) ** 2) ** 0.5
                        for j, (a, b) in enumerate(self.nodes) if j != i),
                       default=0.0)
            if best > 0.0:
                spacing.append(best)
        mean = sum(spacing) / len(spacing) if spacing else 1.0
        return max(mean * max(falloff, 0.05), 1e-9)

    def _kernel(self, distances):
        """Wendland C² radial basis, evaluated on a numpy array.

        Compactly supported and far better conditioned than a Gaussian: a
        Gaussian fit through closely spaced points rings badly, and a
        single dragged point could balloon the surface right out of the
        model. This one stays local and overshoot-free.
        """
        q = distances / self.radius
        inside = q < 1.0
        out = (1.0 - q) ** 4 * (4.0 * q + 1.0)
        return out * inside

    def _solve(self):
        if self._flat:
            return None  # flat: nothing to solve, eval() short-circuits
        try:
            import numpy as np
        except ImportError:
            return None
        P = np.asarray(self.nodes, dtype=float)
        h = np.asarray(self.heights, dtype=float)
        d = np.linalg.norm(P[:, None, :] - P[None, :, :], axis=-1)
        A = self._kernel(d)
        A += np.eye(len(P)) * 1e-6  # ridge: tolerate coincident points
        try:
            return np.linalg.solve(A, h)
        except Exception:
            return np.linalg.lstsq(A, h, rcond=None)[0]

    @property
    def is_flat(self):
        return self._flat

    def eval_many(self, uvs):
        """Heights for a list of (u, v) pairs."""
        if self.is_flat:
            return [0.0] * len(uvs)
        if self.weights is None:
            return [self._shepard(u, v) for u, v in uvs]
        import numpy as np
        Q = np.asarray(uvs, dtype=float)
        P = np.asarray(self.nodes, dtype=float)
        d = np.linalg.norm(Q[:, None, :] - P[None, :, :], axis=-1)
        return (self._kernel(d) @ self.weights).tolist()

    def eval(self, u, v):
        return self.eval_many([(u, v)])[0]

    def _shepard(self, u, v):
        """numpy-free fallback: inverse-distance weighting (exact at nodes)."""
        num = den = 0.0
        for (a, b), h in zip(self.nodes, self.heights):
            d2 = (u - a) ** 2 + (v - b) ** 2
            if d2 < 1e-18:
                return h
            w = 1.0 / (d2 ** 1.5)
            num += w * h
            den += w
        return num / den if den else 0.0

    def normal(self, u, v):
        """Local-space unit normal of the surface at (u, v)."""
        eps = self.radius * 0.02 + 1e-9
        hs = self.eval_many([(u - eps, v), (u + eps, v),
                             (u, v - eps), (u, v + eps)])
        du = (hs[1] - hs[0]) / (2.0 * eps)
        dv = (hs[3] - hs[2]) / (2.0 * eps)
        return Vector((-du, -dv, 1.0)).normalized()


def control_points(cut):
    """Control points of a surface cut, in the cut's local space."""
    return [Vector(p.co) for p in cut.pp_points]


def control_loops(cut):
    """Control points grouped into their section loops (local space)."""
    loops = {}
    for p in cut.pp_points:
        loops.setdefault(p.loop, []).append(Vector(p.co))
    return [loops[k] for k in sorted(loops)]


def store_control_points(cut, points, loop_ids):
    cut.pp_points.clear()
    for co, loop_id in zip(points, loop_ids):
        item = cut.pp_points.add()
        item.co = co
        item.loop = loop_id


def field_for(cut, indices=None):
    """The cut's surface. Lines that cannot be cut in this cut's plane are
    left out, so a stray off-plane line does not warp the whole surface."""
    loops = control_loops(cut)
    if not loops:
        return HeightField([], falloff=cut.pp_falloff)
    if indices is None:
        indices = usable_loop_indices(cut) or list(range(len(loops)))
    points = [p for i in indices for p in loops[i]]
    return HeightField(points or control_points(cut), falloff=cut.pp_falloff)


def world_polylines(cut):
    """Section polylines through the control points, in world space."""
    m = cut.matrix_world
    return [[m @ p for p in loop] for loop in control_loops(cut)]


# ----------------------------------------------------------------------
# Surface meshes
# ----------------------------------------------------------------------

def frame_extent(cut, target, margin=0.08):
    """(u0, u1, v0, v1, z_min, z_max) of the model in the cut's frame."""
    inv = cut.matrix_world.inverted()
    pts = [inv @ (target.matrix_world @ Vector(c)) for c in target.bound_box]
    pts += control_points(cut)
    us = [p.x for p in pts]
    vs = [p.y for p in pts]
    zs = [p.z for p in pts]
    pad = max(max(us) - min(us), max(vs) - min(vs), 1e-6) * margin
    return (min(us) - pad, max(us) + pad,
            min(vs) - pad, max(vs) + pad,
            min(zs) - pad, max(zs) + pad)


def _height_grid(cut, target, resolution):
    u0, u1, v0, v1, z_min, z_max = frame_extent(cut, target)
    n = max(int(resolution), 2)
    us = [u0 + (u1 - u0) * i / (n - 1) for i in range(n)]
    vs = [v0 + (v1 - v0) * j / (n - 1) for j in range(n)]
    field = field_for(cut)
    flat = field.eval_many([(u, v) for u in us for v in vs])
    heights = [flat[i * n:(i + 1) * n] for i in range(n)]
    return us, vs, heights, z_min, z_max


def _full_grid_bm(cut, target, resolution):
    us, vs, heights, _z0, _z1 = _height_grid(cut, target, resolution)
    bm = bmesh.new()
    grid = [[bm.verts.new((us[i], vs[j], heights[i][j]))
             for j in range(len(vs))] for i in range(len(us))]
    for i in range(len(us) - 1):
        for j in range(len(vs) - 1):
            bm.faces.new((grid[i][j], grid[i + 1][j],
                          grid[i + 1][j + 1], grid[i][j + 1]))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return bm


def build_display_mesh(cut, target, resolution=DISPLAY_RES):
    """Replace the cut's mesh with the (open) preview surface.

    A localized cut previews only the patch inside its cut line, so the
    viewport shows what will actually be cut.
    """
    patch = None
    if is_local(cut):
        usable, _problem, _warning = loop_quality(cut)
        if usable:
            patch = patch_grid(cut, target, resolution, indices=usable)
    if patch is not None:
        bm = _patch_bm(patch, thickness=0.0)
    else:
        bm = _full_grid_bm(cut, target, resolution)

    mesh = bpy.data.meshes.new("PartPin_CutSurface")
    bm.to_mesh(mesh)
    bm.free()
    old = cut.data
    cut.data = mesh
    if old is not None and old.users == 0:
        bpy.data.meshes.remove(old)
    return mesh


# ----------------------------------------------------------------------
# Localized cuts: sever only the region ring-fenced by the cut line
# ----------------------------------------------------------------------

def is_local(cut):
    return (cut.pp_cut_kind == 'SURFACE' and cut.pp_local
            and len(cut.pp_points) >= 3)


def loop_polygons(cut):
    """The cut lines as 2D polygons in the cut's own plane."""
    return [[(p.x, p.y) for p in loop] for loop in control_loops(cut)]


def _in_polygon(u, v, poly):
    """Ray-crossing point-in-polygon test."""
    inside = False
    count = len(poly)
    for i in range(count):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % count]
        if (y1 > v) != (y2 > v):
            crossing = x1 + (v - y1) * (x2 - x1) / (y2 - y1)
            if u < crossing:
                inside = not inside
    return inside


def _distance_to_loop(u, v, poly):
    best = float('inf')
    count = len(poly)
    for i in range(count):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % count]
        dx, dy = x2 - x1, y2 - y1
        length = dx * dx + dy * dy
        t = 0.0 if length < 1e-18 else max(
            0.0, min(1.0, ((u - x1) * dx + (v - y1) * dy) / length))
        best = min(best, ((u - x1 - dx * t) ** 2
                          + (v - y1 - dy * t) ** 2) ** 0.5)
    return best


def inside_loops(u, v, polys, margin=0.0):
    """Inside any cut line, or within `margin` of one."""
    for poly in polys:
        if _in_polygon(u, v, poly):
            return True
        if margin > 0.0 and _distance_to_loop(u, v, poly) <= margin:
            return True
    return False


def loop_inset(u, v, polys):
    """How far inside the nearest cut line a point is (negative = outside)."""
    best = -float('inf')
    for poly in polys:
        distance = _distance_to_loop(u, v, poly)
        signed = distance if _in_polygon(u, v, poly) else -distance
        best = max(best, signed)
    return best


def _label_regions(mask, n):
    """Label 4-connected True cells; returns (labels, count)."""
    labels = [[-1] * n for _ in range(n)]
    count = 0
    for si in range(n):
        for sj in range(n):
            if not mask[si][sj] or labels[si][sj] >= 0:
                continue
            stack = [(si, sj)]
            labels[si][sj] = count
            while stack:
                i, j = stack.pop()
                for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    a, b = i + di, j + dj
                    if 0 <= a < n and 0 <= b < n and mask[a][b] \
                            and labels[a][b] < 0:
                        labels[a][b] = count
                        stack.append((a, b))
            count += 1
    return labels, count


def _dilate(mask, n, steps):
    for _ in range(max(int(steps), 0)):
        grown = [row[:] for row in mask]
        for i in range(n):
            for j in range(n):
                if not mask[i][j]:
                    continue
                for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    a, b = i + di, j + dj
                    if 0 <= a < n and 0 <= b < n:
                        grown[a][b] = True
        mask = grown
    return mask


def patch_grid(cut, target, resolution, margin=None, indices=None):
    """Grid over the cut, with a mask of the cells to actually cut.

    The mask is *where the cut surface runs through the model*, restricted
    to the region(s) enclosed by this cut's lines, then grown a little so
    the patch breaks out through the surface all the way round. Deriving
    it from real inside/outside tests rather than from the cut line's
    outline keeps it correct however far the reshaped surface has moved.
    """
    loops = control_loops(cut)
    if indices is None:
        indices = usable_loop_indices(cut) or list(range(len(loops)))
    polys = [[(p.x, p.y) for p in loops[i]] for i in indices]
    if not polys:
        return None
    field = field_for(cut, indices)
    us = [u for poly in polys for u, _v in poly]
    vs = [v for poly in polys for _u, v in poly]
    span = max(max(us) - min(us), max(vs) - min(vs), 1e-9)
    factor = cut.pp_margin if margin is None else margin
    grow = span * max(factor, 1e-4)
    pad = max(grow * 3.0, field.radius if not field.is_flat else grow)
    u0, u1 = min(us) - pad, max(us) + pad
    v0, v1 = min(vs) - pad, max(vs) + pad

    n = max(int(resolution), 8)
    du, dv = (u1 - u0) / n, (v1 - v0) / n
    centres = [(u0 + du * (i + 0.5), v0 + dv * (j + 0.5))
               for i in range(n) for j in range(n)]
    centre_h = field.eval_many(centres)
    matrix = cut.matrix_world

    inside = [[False] * n for _ in range(n)]
    for index, (u, v) in enumerate(centres):
        point = matrix @ Vector((u, v, centre_h[index]))
        if core.point_inside(target, point):
            inside[index // n][index % n] = True

    # Sweep each unmarked cell with a cross of short rays along the cut
    # surface. Thin geometry — a strap, a fin, an armour plate — can be
    # narrower than a cell and sit between sample points entirely, which
    # leaves the pieces joined by it; whether that happened came down to how
    # the grid happened to land. A wall crossing the cell is hit by the
    # cross whatever the alignment.
    model = evaluated(target)
    to_local = model.matrix_world.inverted()
    rotation = matrix.to_quaternion()
    axis_u = to_local.to_3x3() @ (rotation @ Vector((1.0, 0.0, 0.0)))
    axis_v = to_local.to_3x3() @ (rotation @ Vector((0.0, 1.0, 0.0)))
    for index, (u, v) in enumerate(centres):
        i, j = index // n, index % n
        if inside[i][j]:
            continue
        centre = to_local @ (matrix @ Vector((u, v, centre_h[index])))
        for axis, reach in ((axis_u, du), (axis_v, dv)):
            step = axis.normalized() * (reach * 0.5)
            hit, _loc, _nor, _idx = model.ray_cast(centre - step, axis,
                                                   distance=reach)
            if hit:
                inside[i][j] = True
                break

    # Finally test the cell corners, taking a cell as material if any sample
    # in it is.
    corners = [(u0 + du * i, v0 + dv * j)
               for i in range(n + 1) for j in range(n + 1)]
    corner_h = field.eval_many(corners)
    corner_inside = [[False] * (n + 1) for _ in range(n + 1)]
    for index, (u, v) in enumerate(corners):
        i, j = index // (n + 1), index % (n + 1)
        if inside[min(i, n - 1)][min(j, n - 1)]:
            continue  # cell already counted; skip the ray cast
        point = matrix @ Vector((u, v, corner_h[index]))
        corner_inside[i][j] = core.point_inside(target, point)
    for i in range(n):
        for j in range(n):
            if inside[i][j]:
                continue
            if (corner_inside[i][j] or corner_inside[i + 1][j]
                    or corner_inside[i][j + 1] or corner_inside[i + 1][j + 1]):
                inside[i][j] = True

    keep = [[False] * n for _ in range(n)]
    labels, count = _label_regions(inside, n)
    if count:
        # Keep the region(s) this cut's lines enclose. A cell seeds a region
        # if it is inside the model and either within the line or hugging
        # it: cells just inside the line are in the enclosed region, while
        # cells just outside it are outside the model and cannot seed. That
        # holds even when the flattened line is an awkward shape, which a
        # point-in-polygon test alone does not.
        band = max(grow * 2.0, max(du, dv) * 1.5)
        wanted = set()
        for i in range(n):
            for j in range(n):
                if labels[i][j] < 0:
                    continue
                u, v = centres[i * n + j]
                near = any(_distance_to_loop(u, v, poly) <= band
                           for poly in polys)
                if near or inside_loops(u, v, polys):
                    wanted.add(labels[i][j])
        if wanted:
            for i in range(n):
                for j in range(n):
                    if labels[i][j] in wanted:
                        keep[i][j] = True

    if not any(any(row) for row in keep):
        # The surface never enters the model inside its own cut lines;
        # fall back to the outline so the user still gets a cut attempt.
        keep = [[inside_loops(*centres[i * n + j], polys, grow)
                 for j in range(n)] for i in range(n)]
        if not any(any(row) for row in keep):
            return None

    cell = max(min(du, dv), 1e-12)
    keep = _dilate(keep, n, max(1, int(grow / cell + 0.5)))

    nodes = [(u0 + du * i, v0 + dv * j)
             for i in range(n + 1) for j in range(n + 1)]
    flat = field.eval_many(nodes)
    heights = [[flat[i * (n + 1) + j] for j in range(n + 1)]
               for i in range(n + 1)]
    thickness = max(core.bbox_diagonal(target) * SEAM_FACTOR, 1e-9)
    return {'u0': u0, 'v0': v0, 'du': du, 'dv': dv, 'n': n,
            'keep': keep, 'material': inside, 'heights': heights,
            'thickness': thickness}


def _patch_bm(patch, thickness=None):
    """Mesh the masked patch: a closed slab, or an open sheet if thickness=0."""
    u0, v0 = patch['u0'], patch['v0']
    du, dv, n = patch['du'], patch['dv'], patch['n']
    keep, heights = patch['keep'], patch['heights']
    if thickness is None:
        thickness = patch['thickness']
    half = thickness * 0.5

    bm = bmesh.new()
    top, bottom = {}, {}

    def vert(cache, i, j, offset):
        if (i, j) not in cache:
            cache[(i, j)] = bm.verts.new(
                (u0 + du * i, v0 + dv * j, heights[i][j] + offset))
        return cache[(i, j)]

    def kept(i, j):
        return 0 <= i < n and 0 <= j < n and keep[i][j]

    for i in range(n):
        for j in range(n):
            if not keep[i][j]:
                continue
            bm.faces.new((vert(top, i, j, half), vert(top, i + 1, j, half),
                          vert(top, i + 1, j + 1, half),
                          vert(top, i, j + 1, half)))
            if half <= 0.0:
                continue
            bm.faces.new((vert(bottom, i, j, -half),
                          vert(bottom, i, j + 1, -half),
                          vert(bottom, i + 1, j + 1, -half),
                          vert(bottom, i + 1, j, -half)))
            # Walls wherever the neighbouring cell is not part of the patch.
            if not kept(i - 1, j):
                bm.faces.new((vert(top, i, j, half),
                              vert(top, i, j + 1, half),
                              vert(bottom, i, j + 1, -half),
                              vert(bottom, i, j, -half)))
            if not kept(i + 1, j):
                bm.faces.new((vert(top, i + 1, j, half),
                              vert(bottom, i + 1, j, -half),
                              vert(bottom, i + 1, j + 1, -half),
                              vert(top, i + 1, j + 1, half)))
            if not kept(i, j - 1):
                bm.faces.new((vert(top, i, j, half),
                              vert(bottom, i, j, -half),
                              vert(bottom, i + 1, j, -half),
                              vert(top, i + 1, j, half)))
            if not kept(i, j + 1):
                bm.faces.new((vert(top, i, j + 1, half),
                              vert(top, i + 1, j + 1, half),
                              vert(bottom, i + 1, j + 1, -half),
                              vert(bottom, i, j + 1, -half)))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return bm


# ----------------------------------------------------------------------
# Diagnosing a cut line
# ----------------------------------------------------------------------

# What a probed spot along the cut line found.
PROBE_OK = 'OK'          # the cut breaks out through the surface here
PROBE_MARGIN = 'MARGIN'  # more material just past the line: needs more reach
PROBE_STUCK = 'STUCK'    # material keeps going as far as the probe can see

PROBE_ADVICE = {
    PROBE_MARGIN: "have more material just outside the line (raise Edge Margin)",
    PROBE_STUCK: ("have material running well past the line — take the line "
                  "around it, or cut there instead"),
}


def _outward(poly, index):
    """Unit uv direction pointing out of the polygon at a vertex."""
    count = len(poly)
    ax, ay = poly[(index - 1) % count]
    bx, by = poly[(index + 1) % count]
    tx, ty = bx - ax, by - ay
    length = (tx * tx + ty * ty) ** 0.5
    if length < 1e-12:
        return None
    normal = (ty / length, -tx / length)
    u, v = poly[index]
    step = length * 0.01
    if _in_polygon(u + normal[0] * step, v + normal[1] * step, poly):
        normal = (-normal[0], -normal[1])
    return normal


def probe_cut_line(cut, target, resolution=None, per_loop=48):
    """Find the spots where this cut will fail to separate the model.

    This tests the mechanism itself rather than guessing: it builds the
    patch the cut would use and looks for

      * material still being cut at the very edge of the cut's reach — the
        piece carries on past the line (a strap, a fin, a spike) and the
        cut stops mid-way through it, leaving the parts joined;
      * stretches of the line sitting on nothing the cut will touch.

    Returns dicts of world position, status and the reach (as a fraction of
    the line's size) that spot would need.
    """
    indices, problem, _warning = loop_quality(cut)
    if problem is not None or not indices:
        return []
    settings = get_settings_safe()
    if resolution is None:
        resolution = settings.surface_resolution if settings else 48
    patch = patch_grid(cut, target, resolution, indices=indices)
    if patch is None:
        return []

    loops = control_loops(cut)
    field = field_for(cut, indices)
    matrix = cut.matrix_world
    u0, v0 = patch['u0'], patch['v0']
    du, dv, n = patch['du'], patch['dv'], patch['n']
    keep = patch['keep']
    material = patch.get('material', keep)

    def world_of(u, v):
        return matrix @ Vector((u, v, field.eval(u, v)))

    def cell_of(u, v):
        return (int((u - u0) / du), int((v - v0) / dv))

    results = []
    # Cells kept right on the outer ring of the grid. The patch always grows
    # at least one cell past the material it found, so material reaching the
    # very edge means the cut was clipped there and stops mid-way through it.
    for i in range(n):
        for j in range(n):
            if not material[i][j]:
                continue
            if 0 < i < n - 1 and 0 < j < n - 1:
                continue
            results.append({
                'position': world_of(u0 + du * (i + 0.5),
                                     v0 + dv * (j + 0.5)),
                'status': PROBE_MARGIN,
                'needed': cut.pp_margin * 2.5,
            })

    # Stretches of the line the cut will not touch at all.
    for loop_index in indices:
        poly = [(p.x, p.y) for p in loops[loop_index]]
        dense = resample_loop([Vector((u, v, 0.0)) for u, v in poly],
                              max(per_loop, len(poly)))
        for point in dense:
            i, j = cell_of(point.x, point.y)
            covered = (0 <= i < n and 0 <= j < n and keep[i][j])
            results.append({
                'position': world_of(point.x, point.y),
                'status': PROBE_OK if covered else PROBE_STUCK,
                'needed': 0.0 if covered else cut.pp_margin * 2.5,
            })
    return results


def get_settings_safe():
    try:
        return bpy.context.scene.part_pin
    except AttributeError:
        return None


def probe_summary(probes, cut):
    """(bad probe list, suggested margin, one-line summary)."""
    if not probes:
        return [], None, "No cut line to check"
    bad = [p for p in probes if p['status'] != PROBE_OK]
    line_points = sum(1 for p in probes
                      if p['status'] in (PROBE_OK, PROBE_STUCK))
    if not bad:
        return [], None, (f"Cut line looks good — it closes round the region "
                          f"and the cut reaches clear of it "
                          f"({line_points} points checked)")

    needed = max(p['needed'] for p in probes)
    suggested = min(round(max(needed, cut.pp_margin * 2.5) + 0.005, 3), 0.5)
    kinds = {}
    for probe in bad:
        kinds[probe['status']] = kinds.get(probe['status'], 0) + 1
    described = [f"{count} spots {PROBE_ADVICE.get(status, status)}"
                 for status, count in sorted(kinds.items(),
                                             key=lambda kv: -kv[1])]
    summary = "This cut will not separate: " + "; ".join(described)
    if PROBE_MARGIN in kinds:
        summary += (f". Edge Margin {cut.pp_margin:.3f} → try "
                    f"{suggested:.3f}")
    return bad, suggested, summary


def build_local_slab(cut, target, resolution=48, scene=None):
    """Thin closed slab spanning only the cut's ring-fenced region.

    Subtracting this severs the model along the cut line and nowhere else;
    the pieces then fall out as separate connected components. Returns
    (object, problem, warning).
    """
    scene = scene or bpy.context.scene
    # The cut line may have been dragged away from the plane the cut began
    # on; re-fit before measuring anything in that plane.
    refit_frame(cut)
    usable, problem, warning = loop_quality(cut)
    if problem is not None:
        return None, problem, warning

    patch = patch_grid(cut, target, resolution, indices=usable)
    if patch is None:
        return None, ("this cut's surface never passes through the model "
                      "inside its cut line — check the line lies on the "
                      "model"), warning
    bm = _patch_bm(patch)
    if not bm.faces:
        bm.free()
        return None, "this cut covers no area of the model", warning
    obj = core.new_mesh_object("PartPin_Slab", bm, scene.collection,
                               matrix=cut.matrix_world.copy())
    obj.hide_render = True
    return obj, None, warning


def build_surface_cutter(cut, target, resolution=48, scene=None):
    """Closed solid filling everything below the cut surface (local −Z)."""
    scene = scene or bpy.context.scene
    us, vs, heights, z_min, _z_max = _height_grid(cut, target, resolution)
    n, m = len(us), len(vs)
    span = max(u_v for u_v in (us[-1] - us[0], vs[-1] - vs[0]))
    z_bottom = z_min - span - 1.0

    bm = bmesh.new()
    top = [[bm.verts.new((us[i], vs[j], heights[i][j])) for j in range(m)]
           for i in range(n)]
    bottom = [[bm.verts.new((us[i], vs[j], z_bottom)) for j in range(m)]
              for i in range(n)]
    for i in range(n - 1):
        for j in range(m - 1):
            bm.faces.new((top[i][j], top[i + 1][j],
                          top[i + 1][j + 1], top[i][j + 1]))
            bm.faces.new((bottom[i][j], bottom[i][j + 1],
                          bottom[i + 1][j + 1], bottom[i + 1][j]))
    for i in range(n - 1):
        bm.faces.new((top[i][0], bottom[i][0],
                      bottom[i + 1][0], top[i + 1][0]))
        bm.faces.new((top[i][m - 1], top[i + 1][m - 1],
                      bottom[i + 1][m - 1], bottom[i][m - 1]))
    for j in range(m - 1):
        bm.faces.new((top[0][j], top[0][j + 1],
                      bottom[0][j + 1], bottom[0][j]))
        bm.faces.new((top[n - 1][j], bottom[n - 1][j],
                      bottom[n - 1][j + 1], top[n - 1][j + 1]))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)

    obj = core.new_mesh_object("PartPin_Cutter", bm, scene.collection,
                               matrix=cut.matrix_world.copy())
    obj.hide_render = True
    return obj


# ----------------------------------------------------------------------
# Conversion: plane / drawn cut → editable surface cut
# ----------------------------------------------------------------------

def _base_frame_from_loops(loops):
    """Best-fit plane of the section loops as (origin, rotation)."""
    every = [p for loop in loops for p in loop]
    origin = sum(every, Vector()) / len(every)
    normal = Vector((0.0, 0.0, 0.0))
    for loop in loops:
        normal += newell_normal(loop)
    if normal.length < 1e-9:
        normal = Vector((0.0, 0.0, 1.0))
    normal.normalize()
    return origin, normal.to_track_quat('Z', 'Y')


def convert_to_surface(context, cut, target, per_loop=16):
    """Turn a plane or drawn cut into an editable surface cut.

    Returns (cut_object, error_message). The object may be a *new* one
    when the original was a curve, since a curve object cannot become a
    mesh in place; connectors are re-parented in that case.
    """
    scene = context.scene
    if cut.pp_cut_kind == 'SURFACE':
        if len(cut.pp_points) >= 3:
            return cut, None
        loops = plane_section_loops(target, cut.matrix_world)
    elif cut.pp_cut_kind == 'CURVE':
        loops = curve_section_loops(target, cut)
    else:
        loops = plane_section_loops(target, cut.matrix_world)

    loops = [loop for loop in loops if len(loop) >= 3]
    if not loops:
        return None, ("The cut does not intersect the model — "
                      "move it so it passes through, then try again")

    # A plane cut already has a valid base frame; anything else gets the
    # best-fit plane of its section.
    if cut.pp_cut_kind == 'PLANE':
        matrix = cut.matrix_world.copy()
    else:
        origin, rotation = _base_frame_from_loops(loops)
        matrix = Matrix.LocRotScale(origin, rotation, Vector((1, 1, 1)))

    # Resample in world space and pull each point back onto the model, so
    # every control point starts exactly on the surface it will be dragged
    # along (arc-length resampling otherwise cuts chords across curvature,
    # and across the silhouette gap on drawn cuts).
    inv = matrix.inverted()
    local_loops = [[inv @ project_to_surface(target, p)
                    for p in resample_loop(loop, per_loop)]
                   for loop in loops]

    # The height-field representation needs each loop to stay a simple
    # polygon when viewed down the cut normal.
    for loop in local_loops:
        if polygon_self_intersects([(p.x, p.y) for p in loop]):
            return None, ("This cut is too strongly curved to fine-tune on "
                          "the surface — edit its drawn stroke instead")

    points, loop_ids = [], []
    for index, loop in enumerate(local_loops):
        points.extend(loop)
        loop_ids.extend([index] * len(loop))

    if cut.pp_cut_kind == 'CURVE':
        # A curve object cannot become a mesh in place, so the cut is
        # rebuilt as a mesh object and its connectors move across without
        # shifting in world space.
        new_cut = bpy.data.objects.new(cut.name,
                                       bpy.data.meshes.new("PartPin_CutSurface"))
        for coll in cut.users_collection:
            coll.objects.link(new_cut)
        new_cut.pp_role = core.ROLE_CUT
        new_cut.pp_enabled = cut.pp_enabled
        new_cut.pp_index = cut.pp_index
        new_cut.pp_falloff = cut.pp_falloff
        new_cut.matrix_world = matrix
        for conn in core.cut_connectors(scene, cut):
            world = conn.matrix_world.copy()
            conn.parent = new_cut
            conn.matrix_parent_inverse = matrix.inverted()
            conn.matrix_world = world
        core.remove_object(cut)
        cut = new_cut

    cut.matrix_world = matrix
    cut.pp_cut_kind = 'SURFACE'
    store_control_points(cut, points, loop_ids)
    build_display_mesh(cut, target)
    cut.display_type = 'WIRE'
    cut.show_in_front = False
    cut.hide_render = True
    return cut, None


def snap_connectors(cut, field=None):
    """Move each connector back onto the (possibly reshaped) cut surface."""
    field = field or field_for(cut)
    moved = 0
    for conn in core.cut_connectors(bpy.context.scene, cut):
        local = cut.matrix_world.inverted() @ conn.matrix_world.translation
        height = field.eval(local.x, local.y)
        normal = field.normal(local.x, local.y)
        basis = normal.to_track_quat('Z', 'Y').to_matrix().to_4x4()
        basis.translation = Vector((local.x, local.y, height))
        scale = conn.matrix_world.to_scale()
        conn.matrix_world = (cut.matrix_world @ basis
                             @ Matrix.Diagonal(scale.to_4d()))
        moved += 1
    return moved


def surface_connector_matrices(target, cut, count, inset=0.0, samples=22):
    """Connector transforms spread over the seam, aligned to the surface.

    Candidates must sit inside the model *and* inside the cut line — the
    seam only exists there — and `inset` keeps them clear of its edge so a
    pin is not left half-hanging off the cut.
    """
    field = field_for(cut)
    pts = control_points(cut)
    if not pts:
        return []
    us = [p.x for p in pts]
    vs = [p.y for p in pts]
    u0, u1, v0, v1 = min(us), max(us), min(vs), max(vs)
    n = max(int(samples), 3)
    polys = loop_polygons(cut)

    def gather(required_inset):
        found = []
        for i in range(n):
            for j in range(n):
                u = u0 + (u1 - u0) * (i + 0.5) / n
                v = v0 + (v1 - v0) * (j + 0.5) / n
                if loop_inset(u, v, polys) < required_inset:
                    continue
                local = Vector((u, v, field.eval(u, v)))
                if core.point_inside(target, cut.matrix_world @ local):
                    found.append((u, v, local))
        return found

    candidates = gather(inset)
    if not candidates:  # thin region: allow pins closer to the edge
        candidates = gather(0.0)
    if not candidates:
        return []

    matrices = []
    for u, v, local in core._pick_spread(candidates, count):
        normal = field.normal(u, v)
        basis = normal.to_track_quat('Z', 'Y').to_matrix().to_4x4()
        basis.translation = local
        matrices.append(cut.matrix_world @ basis)
    return matrices
