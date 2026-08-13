"""Free-form cut surfaces — the geometry behind "Edit on Surface".

A surface cut is stored as a **height field over its own local XY plane**:
the cut object's transform defines the base frame (local Z is the cut
normal) and a handful of control points, kept in local space, pull the
surface up and down. The surface interpolates those control points
exactly (Gaussian RBF) and flattens back to the base plane away from
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

    loops = []
    # Open chains first, so their ends are not consumed mid-walk.
    for v, edges in adj.items():
        if len(edges) == 1 and any(e not in used for e in edges):
            chain, cyclic = walk(v)
            if len(chain) >= min_points:
                loops.append((chain, cyclic))
    for v, edges in adj.items():
        if any(e not in used for e in edges):
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

    def _solve(self):
        if not self.nodes or not any(self.heights):
            return None  # flat: nothing to solve, eval() short-circuits
        try:
            import numpy as np
        except ImportError:
            return None
        P = np.asarray(self.nodes, dtype=float)
        h = np.asarray(self.heights, dtype=float)
        d = np.linalg.norm(P[:, None, :] - P[None, :, :], axis=-1)
        A = np.exp(-(d / self.radius) ** 2)
        A += np.eye(len(P)) * 1e-6  # ridge: tolerate coincident points
        try:
            return np.linalg.solve(A, h)
        except Exception:
            return np.linalg.lstsq(A, h, rcond=None)[0]

    @property
    def is_flat(self):
        return not self.nodes or not any(self.heights)

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
        return (np.exp(-(d / self.radius) ** 2) @ self.weights).tolist()

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


def field_for(cut):
    return HeightField(control_points(cut), falloff=cut.pp_falloff)


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


def build_display_mesh(cut, target, resolution=DISPLAY_RES):
    """Replace the cut's mesh with the (open) preview surface."""
    us, vs, heights, _z0, _z1 = _height_grid(cut, target, resolution)
    bm = bmesh.new()
    grid = [[bm.verts.new((us[i], vs[j], heights[i][j]))
             for j in range(len(vs))] for i in range(len(us))]
    for i in range(len(us) - 1):
        for j in range(len(vs) - 1):
            bm.faces.new((grid[i][j], grid[i + 1][j],
                          grid[i + 1][j + 1], grid[i][j + 1]))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)

    mesh = bpy.data.meshes.new("PartPin_CutSurface")
    bm.to_mesh(mesh)
    bm.free()
    old = cut.data
    cut.data = mesh
    if old is not None and old.users == 0:
        bpy.data.meshes.remove(old)
    return mesh


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


def surface_connector_matrices(target, cut, count, samples=22):
    """Connector transforms spread over the part of the surface that is
    inside the model, each aligned to the surface normal."""
    field = field_for(cut)
    pts = control_points(cut)
    if not pts:
        return []
    us = [p.x for p in pts]
    vs = [p.y for p in pts]
    u0, u1, v0, v1 = min(us), max(us), min(vs), max(vs)
    n = max(int(samples), 3)

    candidates = []
    for i in range(n):
        for j in range(n):
            u = u0 + (u1 - u0) * (i + 0.5) / n
            v = v0 + (v1 - v0) * (j + 0.5) / n
            local = Vector((u, v, field.eval(u, v)))
            if core.point_inside(target, cut.matrix_world @ local):
                candidates.append((u, v, local))
    if not candidates:
        return []

    matrices = []
    for u, v, local in core._pick_spread(candidates, count):
        normal = field.normal(u, v)
        basis = normal.to_track_quat('Z', 'Y').to_matrix().to_4x4()
        basis.translation = local
        matrices.append(cut.matrix_world @ basis)
    return matrices
