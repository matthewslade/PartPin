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

# How far the cap's rim steps out through the surface, as a fraction of the
# model's size. Enough to start outside the model even in a crease.
CAP_STEP_OUT = 1.5e-2

# Samples per span between control points. One value for the line you see and
# for the boundary of the surface that cuts, so the two cannot drift apart.
LINE_SAMPLES_PER_SPAN = 8


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


def line_rings(cut, target):
    """(rings, normals, cut normal, settle) for a cut, or four Nones.

    The single entry point the cutter takes its lines from.
    """
    from . import mesh_cut  # local import: mesh_cut builds on this module
    return mesh_cut.line_rings(cut, target)


def failure_reason(spots, trouble=None):
    """Why a cut did not separate anything, given what went wrong and where.

    Three different things stop a cut, and they want completely different
    things of you, so they are never described with the same sentence.
    """
    from . import mesh_cut  # local import: mesh_cut builds on this module

    if trouble == mesh_cut.BUSY:
        return ("the cut could not be run just then — Blender was busy with "
                "something else. Click Create Parts again; nothing about the "
                "line needs changing")
    if trouble == mesh_cut.UNENCLOSED:
        return ("nothing came away — this line does not ring-fence a piece of "
                "the model. It has to go right round the part you want "
                "removed, and come back to where it started")
    if trouble == mesh_cut.UNCAPPED:
        return ("nothing came away — the line cut the surface cleanly but the "
                "two halves would not close up over it, which happens where a "
                "line doubles back sharply on itself. Smooth out the tightest "
                "turns in it, or take it a wider way round")
    if spots:
        return (f"nothing came away — the line could not be cut into the "
                f"model's surface at {len(spots)} spot(s) along it, and "
                "raising the cut around them did not free it either. Open "
                "Edit Cut on Surface: they are marked in red. Move the line "
                "off the crease there, or take it a shorter way round")
    return ("nothing came away — the line could not be cut into this model's "
            "surface, and nothing along it stands out as the reason. Try "
            "moving the line onto a smoother stretch")


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
    bm = None
    if is_local(cut):
        usable, problem, _warning = loop_quality(cut, min_alignment=0.0)
        if problem is None and usable:
            bm, _issue = cap_sheet(cut, target, usable, ring=56, relax=10,
                                   cuts=1, settle_rim=False)
    if bm is None:
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


def _closest_on_loop(u, v, poly):
    """Nearest point on a closed 2D polygon, and the distance to it."""
    best, best_distance = poly[0], float('inf')
    count = len(poly)
    for i in range(count):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % count]
        dx, dy = x2 - x1, y2 - y1
        length = dx * dx + dy * dy
        t = 0.0 if length < 1e-18 else max(
            0.0, min(1.0, ((u - x1) * dx + (v - y1) * dy) / length))
        px, py = x1 + dx * t, y1 + dy * t
        distance = ((u - px) ** 2 + (v - py) ** 2) ** 0.5
        if distance < best_distance:
            best, best_distance = (px, py), distance
    return best, best_distance


def loop_inset(u, v, polys):
    """How far inside the nearest cut line a point is (negative = outside)."""
    best = -float('inf')
    for poly in polys:
        distance = _distance_to_loop(u, v, poly)
        signed = distance if _in_polygon(u, v, poly) else -distance
        best = max(best, signed)
    return best


# ----------------------------------------------------------------------
# Diagnosing a cut line
# ----------------------------------------------------------------------


def get_settings_safe():
    try:
        return bpy.context.scene.part_pin
    except AttributeError:
        return None


def line_samples(cut, target, indices=None,
                 per_span=LINE_SAMPLES_PER_SPAN, lift=0.0):
    """The cut line as it lies on the model: one dense ring per cut line.

    The single definition of where the line runs. Both the line you see and
    the boundary of the surface that cuts are built from this, so they agree
    exactly — building them separately left the two disagreeing by a corner's
    width, which is visible as the highlight not quite meeting the line.
    """
    loops = control_loops(cut)
    if indices is None:
        indices = usable_loop_indices(cut) or list(range(len(loops)))
    field = field_for(cut, indices)
    matrix = cut.matrix_world
    model = evaluated(target)
    to_model = model.matrix_world.inverted()
    normal_matrix = model.matrix_world.to_3x3()

    rings = []
    for index in indices:
        loop = loops[index]
        if len(loop) < 3:
            continue
        # Spaced evenly along the whole ring, but always breaking at the
        # control points, so corners stay corners and no stretch is sampled
        # coarsely just because it is long.
        spans = [(loop[(i + 1) % len(loop)] - a).length
                 for i, a in enumerate(loop)]
        spacing = max(sum(spans) / (len(loop) * max(int(per_span), 1)), 1e-9)

        ring = []
        for i, a in enumerate(loop):
            b = loop[(i + 1) % len(loop)]
            steps = max(1, int(round(spans[i] / spacing)))
            for k in range(steps):
                t = k / steps
                u = a.x + (b.x - a.x) * t
                v = a.y + (b.y - a.y) * t
                world = matrix @ Vector((u, v, field.eval(u, v)))
                ok, near, normal, _index = model.closest_point_on_mesh(
                    to_model @ world)
                if ok:
                    world = model.matrix_world @ near
                    outward = normal_matrix @ normal
                    if lift > 0.0 and outward.length > 1e-9:
                        world = world + outward.normalized() * lift
                ring.append(world)
        if ring:
            rings.append(ring)
    return rings


def cap_sheet(cut, target, usable, ring=96, relax=18, cuts=2,
              settle_rim=True, stuck=None):
    """The lid that spans a cut's line, in the cut's own space.

    Laid out inside the line, evened out, then lifted onto the cut's smooth
    surface — a height over a plane, so it can never fold back on itself. A
    plain fan of triangles in space can fold on a line with a spur in it, and
    a folded lid cuts nothing.

    With `settle_rim`, the rim is put on the model's surface and stepped out
    until clear of it, which is what a cutting lid needs; without, the lid is
    left exactly on the line, which is what a preview should show.

    Returns (bmesh, problem).
    """
    loops = control_loops(cut)
    field = field_for(cut, usable)
    matrix = cut.matrix_world
    model = evaluated(target)
    to_model = model.matrix_world.inverted()
    normal_matrix = model.matrix_world.to_3x3()
    diagonal = core.bbox_diagonal(target)
    step_out = diagonal * CAP_STEP_OUT
    # How far the rim may keep stepping out to get clear. Undercut buys more,
    # for a piece buried deeply enough that the rim starts inside another part
    # of the model.
    limit_out = diagonal * (CAP_STEP_OUT * 4.0 + cut.pp_undercut * 0.5)

    bm = bmesh.new()
    perimeters = []
    for index in usable:
        local = list(loops[index])
        if len(local) < 3:
            continue
        # Follow the line as it is drawn: sampled between the points and put
        # onto the model, the way the visible line is built. Sampling straight
        # across the cut's plane instead cuts the corners between points, and
        # on a rounded line those chords dip inside the model — leaving the
        # lid short of its own boundary, and the cut unable to separate.
        dense = []
        for i, a in enumerate(resample_loop(local, max(ring, len(local)),
                                            cyclic=True)):
            world = matrix @ Vector((a.x, a.y, field.eval(a.x, a.y)))
            ok, near, _normal, _index = model.closest_point_on_mesh(
                to_model @ world)
            if ok:
                world = model.matrix_world @ near
            dense.append(matrix.inverted() @ world)
        flat = [(p.x, p.y) for p in dense]
        centre = (sum(u for u, _v in flat) / len(flat),
                  sum(v for _u, v in flat) / len(flat))
        verts = [bm.verts.new((u, v, height))
                 for (u, v), height in zip(flat, [p.z for p in dense])]
        hub = bm.verts.new((centre[0], centre[1], 0.0))
        for i, vert in enumerate(verts):
            try:
                bm.faces.new((vert, verts[(i + 1) % len(verts)], hub))
            except ValueError:
                pass
        perimeters.append(loop_length([Vector((u, v, 0.0)) for u, v in flat]))

    if not bm.faces:
        bm.free()
        return None, ("the cut line does not describe a loop to span — "
                      "redraw it")

    if cuts > 0:
        bmesh.ops.subdivide_edges(bm, edges=list(bm.edges), cuts=cuts,
                                  use_grid_fill=True)
    # Subdividing rebuilds the mesh, so the line is found again afterwards:
    # the lid is an open sheet, and its only boundary is the line itself.
    rim = {v for v in bm.verts if v.is_boundary}

    # Even the lid out inside the line. Flat, so it cannot tangle: only where
    # each point sits within the line moves, never the line itself.
    for _pass in range(max(int(relax), 0)):
        moved = {}
        for vert in bm.verts:
            if vert in rim:
                continue
            neighbours = [edge.other_vert(vert) for edge in vert.link_edges]
            if neighbours:
                average = sum((n.co for n in neighbours), Vector()) \
                    / len(neighbours)
                moved[vert] = Vector((average.x, average.y, vert.co.z))
        for vert, target_co in moved.items():
            vert.co = vert.co.lerp(target_co, 0.5)

    area = sum(face.calc_area() for face in bm.faces)
    perimeter = sum(perimeters) or 1.0
    if area < 0.02 * perimeter * perimeter / (4.0 * 3.141592653589793):
        bm.free()
        return None, ("this cut line does not enclose an area to cut off. "
                      "Draw it right round the part you want removed")

    for vert in bm.verts:
        if vert in rim:
            continue  # already on the line, where it was drawn
        u, v = vert.co.x, vert.co.y
        vert.co = Vector((u, v, field.eval(u, v)))

    if settle_rim:
        # Work out where each point of the rim should sit, then even those
        # positions out along the rim before applying them. In a crease,
        # neighbouring points can find opposite faces of it and be sent
        # opposite ways, which folds the lid over itself.
        targets = {}
        for vert in rim:
            world = matrix @ vert.co
            ok, location, normal, _index = model.closest_point_on_mesh(
                to_model @ world)
            if not ok or normal.length < 1e-9:
                continue
            # Put the rim on the surface first: between the points the user
            # placed, the cut's own surface wanders a little off the model, and
            # stepping out from there can leave the rim buried.
            on_surface = model.matrix_world @ location
            outward = (normal_matrix @ normal).normalized()
            travelled = step_out
            clear = False
            while travelled <= limit_out:
                if not core.point_inside(target,
                                         on_surface + outward * travelled):
                    clear = True
                    break
                travelled += step_out
            if not clear and stuck is not None:
                # The rim is still buried here, so material wraps round it and
                # holds the two sides together. This is the spot to move the
                # line away from.
                stuck.append(on_surface.copy())
            targets[vert] = matrix.inverted() @ (on_surface
                                                 + outward * travelled)
        for _pass in range(4):
            smoothed = {}
            for vert, position in targets.items():
                neighbours = [edge.other_vert(vert) for edge in vert.link_edges
                              if edge.is_boundary]
                nearby = [targets[n] for n in neighbours if n in targets]
                if nearby:
                    average = sum(nearby, Vector()) / len(nearby)
                    smoothed[vert] = position.lerp(average, 0.5)
            targets.update(smoothed)
        for vert, position in targets.items():
            vert.co = position

    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return bm, None


# Where a cut was last found to be uncuttable, per cut name, so the editor can
# mark it. Written whenever a cut is actually tried — by T in the editor, and
# by Create Parts when it fails — so the marks always describe the last real
# attempt rather than a guess. Transient.
STUCK_AT = {}


# Why each cut last failed, alongside where. Transient, like STUCK_AT.
WHY = {}


def remember_stuck(cut, spots):
    """Record where a cut got stuck, for the editor to mark."""
    if spots:
        STUCK_AT[cut.name] = list(spots)
    else:
        STUCK_AT.pop(cut.name, None)


# What an inspection can find wrong with a cut, and what to do about it.
#
# There used to be four of these. Three of them measured a lid that was
# synthesised to span the line — whether it folded, whether its rim broke out
# of the model, whether its middle ran through open space. No lid is built any
# more: the cut is made along the model's own faces, so it cannot fold, its
# rim is on the surface by construction, and what it spans is whatever the
# line encloses. Those three could only ever fire on nothing, or worse, on a
# cut that works — which is how they lost the user's trust. They are gone.
ADRIFT = 'ADRIFT'        # the line has left the model's surface
STUCK = 'STUCK'          # the cut could not be carried through the surface
PINCHED = 'PINCHED'      # the line doubles back and nearly touches itself

TROUBLE = {
    ADRIFT: "the line has come off the model here — drag these points back on",
    STUCK: ("the cut could not be carried through the surface here — move the "
            "line off the crease, or take it a shorter way round"),
    PINCHED: ("the line doubles back on itself here, and there is no room to "
              "cut between the two sides — drag these points apart"),
}


def inspect_cut(cut, target):
    """Everything measurably wrong with a cut, as places on the model.

    Read-only, and silent about a cut with nothing wrong with it. The one
    thing that can still be wrong with a line is that it has come off the
    model; whether the cut will separate is not guessed at here but answered
    by `trial_cut`, which makes the cut and looks.

    Returns {kind: [world positions]}.
    """
    from . import mesh_cut  # local import: mesh_cut builds on this module

    found = {ADRIFT: [], STUCK: list(STUCK_AT.get(cut.name, ())), PINCHED: []}
    usable, problem, _warning = loop_quality(cut, min_alignment=0.0)
    if problem is not None:
        return found

    diagonal = core.bbox_diagonal(target)
    for ring in line_samples(cut, target, usable):
        if len(ring) < 3:
            continue
        found[PINCHED].extend(mesh_cut.hairpins(
            ring, mesh_cut.ring_normals(ring, target), diagonal))

    matrix = cut.matrix_world
    diagonal = core.bbox_diagonal(target)
    # Measured on the points as stored, not on the drawn line, which is put
    # onto the surface as it is built and so could never look adrift.
    for point in cut.pp_points:
        world = matrix @ Vector(point.co)
        if surface_gap(target, world) > diagonal * 2e-3:
            found[ADRIFT].append(world)
    return {kind: _thin(places) for kind, places in found.items()}


def surface_gap(target, world_point):
    """How far a point sits from the model's surface."""
    model = evaluated(target)
    ok, near, _normal, _index = model.closest_point_on_mesh(
        model.matrix_world.inverted() @ world_point)
    if not ok:
        return float('inf')
    return ((model.matrix_world @ near) - world_point).length


def _rim_rings(bm):
    """Every boundary of the lid, each in the order it runs round.

    There can be more than one: a cut with several lines has one per line, and
    a lid that failed to close over some triangle has a hole in it. Walking
    only the first and treating the rest as part of it compares stretches of
    one boundary against stretches of another, which reads as folds all over
    the model — marks with nothing to do with anything.
    """
    rings = []
    seen = set()
    for start in bm.verts:
        if not start.is_boundary or start in seen:
            continue
        ring, vert = [], start
        while vert is not None and vert not in seen:
            seen.add(vert)
            ring.append(vert)
            vert = next((edge.other_vert(vert) for edge in vert.link_edges
                         if edge.is_boundary
                         and edge.other_vert(vert) not in seen), None)
        if len(ring) >= 4:
            rings.append(ring)
    return rings


def _segments_touch(a1, a2, b1, b2, tolerance):
    """Whether two segments come within `tolerance` of each other."""
    if tolerance <= 0.0:
        return False
    # Cheap reject on their bounding boxes first.
    for axis in range(3):
        if (min(a1[axis], a2[axis]) - tolerance
                > max(b1[axis], b2[axis])
                or min(b1[axis], b2[axis]) - tolerance
                > max(a1[axis], a2[axis])):
            return False
    best = float('inf')
    for steps in ((a1, a2, b1, b2), (b1, b2, a1, a2)):
        first, second, other_first, other_second = steps
        span = second - first
        length = span.length_squared
        for point in (other_first, other_second, (other_first
                                                  + other_second) * 0.5):
            t = 0.0 if length < 1e-18 else max(
                0.0, min(1.0, (point - first).dot(span) / length))
            best = min(best, (first + span * t - point).length)
    return best < tolerance


def find_join_hints(cut, target, ring=96):
    """Where the model would stay joined if this cut were made.

    Two things keep a cut from separating, and both are found here rather than
    guessed at: the rim of the cut still buried in the model, so material wraps
    around it, and the cut's surface leaving the model in the middle, so the
    two sides join through the gap.

    Returns (joined_at, left_model), both lists of world positions.
    """
    usable, problem, _warning = loop_quality(cut, min_alignment=0.0)
    if problem is not None:
        return [], []
    stuck = []
    bm, problem = cap_sheet(cut, target, usable, ring=ring, stuck=stuck)
    if bm is None:
        return stuck, []

    matrix = cut.matrix_world
    diagonal = core.bbox_diagonal(target)
    reach = diagonal * CAP_STEP_OUT * 2.0

    # Material carrying on past the rim: the cut stops at the line, so
    # anything solid just beyond it joins the two sides around the cut. This
    # is the usual reason a line that looks right will not separate.
    for vert in bm.verts:
        if not vert.is_boundary:
            continue
        along = [edge.other_vert(vert) for edge in vert.link_edges
                 if edge.is_boundary]
        inward = [edge.other_vert(vert) for edge in vert.link_edges
                  if not edge.is_boundary]
        if len(along) < 2 or not inward:
            continue
        tangent = (along[1].co - along[0].co)
        middle = sum((v.co for v in inward), Vector()) / len(inward)
        outward = (vert.co - middle)
        if tangent.length > 1e-9:
            tangent.normalize()
            outward = outward - tangent * outward.dot(tangent)
        if outward.length < 1e-12:
            continue
        outward.normalize()
        world = matrix @ vert.co
        direction = (matrix.to_3x3() @ outward).normalized()
        if core.point_inside(target, world + direction * reach):
            stuck.append(world)

    # The band along the rim is stepped out of the model deliberately, so only
    # faces well inside the lid can be said to have left it.
    edge_verts = {v for v in bm.verts if v.is_boundary}
    for _ring in range(2):
        edge_verts |= {edge.other_vert(v) for v in set(edge_verts)
                       for edge in v.link_edges}
    holes = []
    for face in bm.faces:
        if any(vert in edge_verts for vert in face.verts):
            continue
        centre = matrix @ face.calc_center_median()
        if not core.point_inside(target, centre):
            holes.append(centre)
    bm.free()
    return _thin(stuck), _thin(holes)


def _thin(points, most=60):
    """Keep a readable spread of markers rather than a solid mass of them."""
    if len(points) <= most:
        return points
    step = len(points) / most
    return [points[int(i * step)] for i in range(most)]


def trial_cut(cut, target, scene=None):
    """Actually try the cut on a copy and see what happens.

    Whether a cut separates is a question about the model, not something to
    infer from the shape of the cut: parts of the cut surface may well lie
    outside the model without doing any harm. So this makes the cut, counts
    what falls out, throws the copy away, and only looks for reasons when the
    answer is that nothing came away.

    Returns (pieces, spots) — how many pieces it fell into, and where the cut
    could not be carried through if it did not.
    """
    from . import mesh_cut  # local import: mesh_cut builds on this module

    scene = scene or bpy.context.scene
    rings, normals, normal, settle = mesh_cut.line_rings(cut, target)
    if rings is None:
        STUCK_AT.pop(cut.name, None)
        return 0, []

    trial = core.duplicate_object(target, "PartPin_Trial", scene.collection)
    trial.hide_render = True
    pieces, spots = 1, []
    try:
        cut_pieces, trouble, spots = mesh_cut.cut_object(
            trial, rings, normals, normal, scene, settle=settle)
        if cut_pieces is not None:
            pieces = len(cut_pieces)
            for part in cut_pieces:
                core.remove_object(part)
    except Exception:
        pieces = 0
    finally:
        core.remove_object(trial)

    if pieces >= 2:
        spots, trouble = [], None
    remember_stuck(cut, spots)
    WHY[cut.name] = trouble
    return pieces, spots


def cap_preview_tris(cut, target, ring=56, relax=10, cuts=1, lift=0.0):
    """World-space triangles of the lid, for showing what the cut will be.

    `lift` raises the rim by the same amount the cut line is drawn above the
    surface, so on screen the lid meets the line instead of stopping a hair
    short of it.
    """
    usable, problem, _warning = loop_quality(cut, min_alignment=0.0)
    if problem is not None:
        return []
    bm, problem = cap_sheet(cut, target, usable, ring=ring, relax=relax,
                            cuts=cuts, settle_rim=False)
    if bm is None:
        return []
    matrix = cut.matrix_world
    if lift > 0.0:
        model = evaluated(target)
        to_model = model.matrix_world.inverted()
        normal_matrix = model.matrix_world.to_3x3()
        inverse_rotation = matrix.inverted().to_3x3()
        for vert in bm.verts:
            if not vert.is_boundary:
                continue
            ok, _location, normal, _index = model.closest_point_on_mesh(
                to_model @ (matrix @ vert.co))
            outward = normal_matrix @ normal if ok else None
            if outward and outward.length > 1e-9:
                vert.co = vert.co + (inverse_rotation
                                     @ outward.normalized()) * lift
    tris = []
    for face in bm.faces:
        corners = [matrix @ v.co for v in face.verts]
        for i in range(1, len(corners) - 1):
            tris.extend((corners[0], corners[i], corners[i + 1]))
    bm.free()
    return tris


def build_cap_slab(cut, target, scene=None, ring=96, relax=18):
    """The cutter: the lid that spans the cut's line, thickened to a hair.

    Subtracting it severs exactly what the line encircles. The perimeter
    decides everything — no grid picks a side of the line, and nothing reaches
    sideways into the model to break out through its surface.

    Returns (object, problem, warning).
    """
    scene = scene or bpy.context.scene
    refit_frame(cut)
    usable, problem, warning = loop_quality(cut, min_alignment=0.0)
    if problem is not None:
        return None, problem, warning

    stuck = []
    bm, problem = cap_sheet(cut, target, usable, ring=ring, relax=relax,
                            stuck=stuck)
    if bm is None:
        return None, problem, warning

    thickness = max(core.bbox_diagonal(target) * SEAM_FACTOR, 1e-9)
    bmesh.ops.solidify(bm, geom=list(bm.faces), thickness=thickness)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    obj = core.new_mesh_object("PartPin_Cap", bm, scene.collection,
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


def simplify_ring(points, tolerance):
    """Thin a closed ring, keeping the corners and dropping what lies along a
    straight run (Douglas-Peucker). Sampling a drawn line by length alone puts
    points at even spacing and none on the corners, and a cut line that misses
    a corner cuts across it."""
    count = len(points)
    if count < 4:
        return list(points)

    def furthest(start, end):
        """Index between start and end furthest from the line joining them."""
        first, last = points[start], points[end]
        span = last - first
        length = span.length
        worst, worst_at = -1.0, -1
        for i in range(start + 1, end):
            offset = points[i] - first
            if length < 1e-12:
                distance = offset.length
            else:
                along = offset.dot(span) / (length * length)
                distance = (offset - span * along).length
            if distance > worst:
                worst, worst_at = distance, i
        return worst, worst_at

    def walk(start, end, keep):
        worst, worst_at = furthest(start, end)
        if worst_at < 0 or worst <= tolerance:
            return
        walk(start, worst_at, keep)
        keep.add(worst_at)
        walk(worst_at, end, keep)

    # Split the ring at two far-apart points so each half is an open chain.
    half = count // 2
    keep = {0, half}
    walk(0, half, keep)
    walk(half, count - 1, keep)
    keep.add(count - 1)
    return [points[i] for i in sorted(keep)]


def stroke_to_loop(target, stroke, per_loop=16):
    """Turn a drawn stroke into a closed ring of control points.

    The stroke arrives as world points already on the model (each one a
    ray-cast hit). Corners are kept — a point lands on each of them — and the
    straight runs between are filled in evenly, so the line can be dragged
    about without having lost the shape that was drawn.
    """
    points = []
    for point in stroke:
        if not points or (point - points[-1]).length > 1e-9:
            points.append(Vector(point))
    if len(points) < 3:
        return []
    if (points[0] - points[-1]).length < 1e-9:
        points.pop()
    if len(points) < 3:
        return []

    wanted = max(int(per_loop), 6)
    diagonal = core.bbox_diagonal(target)
    tolerance = diagonal * 0.004
    corners = simplify_ring(points, tolerance)
    # A scribble would otherwise keep every wobble as a corner.
    while len(corners) > wanted * 2 and tolerance < diagonal:
        tolerance *= 1.7
        corners = simplify_ring(points, tolerance)

    # Fill the straight runs so no span is much longer than the rest.
    span_target = max(loop_length(corners) / wanted, diagonal * 1e-4)
    filled = []
    for i, a in enumerate(corners):
        b = corners[(i + 1) % len(corners)]
        filled.append(a)
        steps = int((b - a).length / span_target)
        for k in range(1, steps):
            filled.append(a.lerp(b, k / steps))
    return [project_to_surface(target, p) for p in filled]


def cut_from_stroke(context, target, stroke, per_loop=16, name="Drawn Cut"):
    """Create a surface cut from a perimeter drawn on the model.

    Returns (cut_object, error). The cut is the same kind of object the
    on-surface editor already works with, so it can be adjusted point by
    point and cut exactly like one grown from a plane.
    """
    loop = stroke_to_loop(target, stroke, per_loop)
    if len(loop) < 6:
        return None, "That stroke is too short to make a cut from"

    origin, rotation = _base_frame_from_loops([loop])
    matrix = Matrix.LocRotScale(origin, rotation, Vector((1.0, 1.0, 1.0)))
    inverse = matrix.inverted()
    local = [inverse @ p for p in loop]

    flat = [(p.x, p.y) for p in local]
    if polygon_self_intersects(flat):
        return None, ("The drawn perimeter crosses itself when flattened, so "
                      "the region it encloses is ambiguous. Redraw it as a "
                      "single loop that does not double back")
    if polygon_roundness(flat) < 0.02:
        return None, ("The drawn perimeter does not enclose an area — draw it "
                      "right round the part you want to remove")

    scene = context.scene
    draft = core.ensure_collection(scene, core.DRAFT_COLLECTION)
    cut = bpy.data.objects.new(name,
                               bpy.data.meshes.new("PartPin_CutSurface"))
    draft.objects.link(cut)
    cut.matrix_world = matrix
    cut.pp_role = core.ROLE_CUT
    cut.pp_cut_kind = 'SURFACE'
    cut.pp_enabled = True
    cut.pp_local = True
    cut.pp_main_loop = 0
    cut.pp_index = len(core.scene_cuts(scene)) - 1
    cut.display_type = 'WIRE'
    cut.show_in_front = False
    cut.hide_render = True
    store_control_points(cut, local, [0] * len(local))
    build_display_mesh(cut, target)
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
