"""Cut lines that live on the model's surface — "Edit on Surface".

A cut line is stored as what was drawn: an **ordered ring of anchors sitting
on the model**, kept in the model's own local space so the line rides with it,
with the path between consecutive anchors walked *across the surface*
(`walker`). There is no plane and no height field anywhere in it.

That matters because the line has to hold a contour. Stored the other way —
as a height over a fitted plane — a line passes through its anchors and sags
towards the plane between them, and a contour wrapping round a limb is not a
function over any plane at all, so where the field could not express what was
drawn the line doubled back on itself. Walked across the surface it is on the
model everywhere by construction, it cannot sag, and a shortest path cannot
double back.

The cut's own object still has a transform: its connectors are parented to it,
and the "cut right through" mode grows a half-space out of it. That frame is
fitted *to* the finished line rather than the line being expressed in it, so
it constrains nothing.
"""

import bmesh
import bpy
from mathutils import Matrix, Vector

from . import core, walker

# How many points the cap preview and the cut object's own display mesh are
# built from. The line itself is as dense as the model's faces make it, which
# is far more than a preview needs.
PREVIEW_RING = 96


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
# Section lines: where a plane or a drawn stroke meets the model surface
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
    # run and shift the anchors around it.
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


# ----------------------------------------------------------------------
# Polyline and polygon maths
# ----------------------------------------------------------------------

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


def loop_length(points):
    count = len(points)
    return sum((points[(i + 1) % count] - points[i]).length
               for i in range(count))


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


def loop_inset(u, v, polys):
    """How far inside the nearest cut line a point is (negative = outside)."""
    best = -float('inf')
    for poly in polys:
        distance = _distance_to_loop(u, v, poly)
        signed = distance if _in_polygon(u, v, poly) else -distance
        best = max(best, signed)
    return best


# ----------------------------------------------------------------------
# Anchors: the cut line as it is stored
# ----------------------------------------------------------------------

# Anchors used to be kept in the cut object's own space, which moved whenever
# the cut's plane was re-fitted. They are in the model's space now, so a cut
# saved by an older version has to be brought across once. `pp_anchor_space`
# says which it is.
MODEL_SPACE = 1


def migrate(cut, target):
    """Bring a cut saved before anchors moved into the model's space."""
    if cut.pp_anchor_space == MODEL_SPACE or not len(cut.pp_points):
        return False
    to_model = target.matrix_world.inverted()
    matrix = cut.matrix_world
    for point in cut.pp_points:
        world = matrix @ Vector(point.co)
        landed, face = walker.place(target, to_model @ world)
        point.co = landed
        point.face = face
    cut.pp_anchor_space = MODEL_SPACE
    return True


def anchor_loops(cut):
    """The anchors grouped into their rings, in the model's local space."""
    loops = {}
    for p in cut.pp_points:
        loops.setdefault(p.loop, []).append(Vector(p.co))
    return [loops[k] for k in sorted(loops)]


def anchor_faces(cut):
    """The face each anchor sits on, grouped like `anchor_loops`."""
    loops = {}
    for p in cut.pp_points:
        loops.setdefault(p.loop, []).append(p.face)
    return [loops[k] for k in sorted(loops)]


def store_anchors(cut, points, loop_ids, faces=None):
    """Replace a cut's anchors. Points are in the model's local space."""
    cut.pp_points.clear()
    for index, (co, loop_id) in enumerate(zip(points, loop_ids)):
        item = cut.pp_points.add()
        item.co = co
        item.loop = loop_id
        item.face = -1 if faces is None else faces[index]
    cut.pp_anchor_space = MODEL_SPACE


def world_anchors(cut, target):
    """The anchors in world space, ring by ring."""
    matrix = target.matrix_world
    return [[matrix @ p for p in loop] for loop in anchor_loops(cut)]


def usable_loop_indices(cut):
    """Which of a cut's lines have enough anchors to be a line at all.

    Every one of them is cut. There is no plane to share any more, so no line
    is ever set aside for facing the wrong way.
    """
    return [i for i, loop in enumerate(anchor_loops(cut)) if len(loop) >= 3]


# ----------------------------------------------------------------------
# The line itself
# ----------------------------------------------------------------------

# Where a cut's line could not be walked across the surface, per cut name.
# Written whenever the line is built. Transient.
BROKEN_AT = {}


def line_rings(cut, target, indices=None, lift=0.0):
    """The cut line as it lies on the model: one ring of points per line.

    The single definition of where the line runs. The line you see and the
    line that cuts are both this, so they cannot drift apart.

    Each ring is the anchors with the surface path between consecutive ones
    spliced in. A span that cannot be walked — anchors on shells that do not
    join — leaves its whole ring out and is remembered in `BROKEN_AT`, rather
    than being papered over with a straight line through space.
    """
    migrate(cut, target)
    loops = anchor_loops(cut)
    faces = anchor_faces(cut)
    if indices is None:
        indices = usable_loop_indices(cut)
    matrix = target.matrix_world
    model = evaluated(target)
    normal_matrix = model.matrix_world.to_3x3()
    to_model = model.matrix_world.inverted()

    rings, broken = [], []
    for index in indices:
        if index >= len(loops) or len(loops[index]) < 3:
            continue
        loop, hints = loops[index], faces[index]
        local, whole = [], True
        for i, anchor in enumerate(loop):
            j = (i + 1) % len(loop)
            local.append(anchor)
            walked = walker.between(target, anchor, loop[j],
                                    hints[i], hints[j])
            if walked is None:
                broken.append(matrix @ ((anchor + loop[j]) * 0.5))
                whole = False
                break
            local.extend(walked)
        if not whole:
            continue
        ring = [matrix @ p for p in local]
        if lift > 0.0:
            ring = [_lifted(model, to_model, normal_matrix, p, lift)
                    for p in ring]
        if len(ring) >= 3:
            rings.append(ring)

    if broken:
        BROKEN_AT[cut.name] = broken
    else:
        BROKEN_AT.pop(cut.name, None)
    return rings


def _lifted(model, to_model, normal_matrix, world, lift):
    """A point raised clear of the surface it lies on, for drawing."""
    ok, near, normal, _index = model.closest_point_on_mesh(to_model @ world)
    if not ok:
        return world
    outward = normal_matrix @ normal
    if outward.length < 1e-9:
        return model.matrix_world @ near
    return (model.matrix_world @ near) + outward.normalized() * lift


def ring_plane(ring):
    """The plane a finished ring lies closest to, as (origin, normal).

    Fitted to the *result*, so it constrains nothing about the line. It is
    what the cap's flat middle is drawn down to, what the connectors sit on,
    and what "cut right through" grows its half-space out of.
    """
    return fit_plane(ring)


def frame_to_line(cut, target):
    """Sit the cut object's own frame on the plane of its line.

    The line does not live in this frame — it is on the model — so this can
    neither move the line nor lose its shape. What it is for is the things
    hanging off the cut: its connectors, and the half-space the "cut right
    through" mode grows.
    """
    rings = line_rings(cut, target)
    if not rings:
        rings = [loop for loop in world_anchors(cut, target) if len(loop) >= 3]
    if not rings:
        return False
    main = None
    if 0 <= cut.pp_main_loop < len(rings):
        main = rings[cut.pp_main_loop]
    if main is None:
        main = max(rings, key=loop_length)
    origin, normal = ring_plane(main)
    frame = Matrix.LocRotScale(origin, normal.to_track_quat('Z', 'Y'),
                               Vector((1.0, 1.0, 1.0)))
    inverse = frame.inverted()

    connectors = [(c, c.matrix_world.copy())
                  for c in core.cut_connectors(bpy.context.scene, cut)]
    cut.matrix_world = frame
    for conn, world_matrix in connectors:
        conn.matrix_parent_inverse = inverse
        conn.matrix_world = world_matrix
    return True


def cut_line_problem(cut, target=None):
    """Why this cut cannot be made at all, or None if it can."""
    loops = anchor_loops(cut)
    if not loops or not usable_loop_indices(cut):
        return ("this cut has no cut line to work from — draw one right "
                "round the part you want removed")
    if target is not None:
        rings = line_rings(cut, target)
        if not rings:
            return ("this cut line cannot be followed across the model's "
                    "surface. Its points have to sit on one connected piece "
                    "of it — move the marked ones back onto the model")
    return None


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


# ----------------------------------------------------------------------
# Localized cuts: sever only the region ring-fenced by the cut line
# ----------------------------------------------------------------------

def is_local(cut):
    return (cut.pp_cut_kind == 'SURFACE' and cut.pp_local
            and len(cut.pp_points) >= 3)


def loop_polygons(cut, target):
    """The cut's lines flattened onto the cut's own frame, as 2D polygons.

    Only used for deciding what is *inside* a line — where a connector may
    go. The line itself is never expressed this way.
    """
    inverse = cut.matrix_world.inverted()
    return [[((inverse @ p).x, (inverse @ p).y) for p in ring]
            for ring in line_rings(cut, target)]


def cap_geometry(cut, target, indices=None, ring=PREVIEW_RING):
    """The lid the cutter will build, as (world points, triangles) per line.

    The same plan the cutter fills its rims with, so what is previewed is
    what is cut rather than something synthesised alongside it.
    """
    from . import mesh_cut  # local import: mesh_cut builds on this module

    plans = []
    for line in line_rings(cut, target, indices):
        # Kept as it is unless it is longer than the preview needs. Resampling
        # a line evenly by length puts no point on a corner, and a lid that
        # misses the corners of a cube waist stops short of its own boundary.
        rim = (list(line) if len(line) <= ring
               else resample_loop(line, ring, cyclic=True))
        if len(rim) < 3:
            continue
        _origin, normal = ring_plane(rim)
        extra, tris = mesh_cut._cap_plan(rim, normal)
        if tris is None:
            continue
        plans.append((rim + extra, tris))
    return plans


def cap_preview_tris(cut, target, ring=PREVIEW_RING, lift=0.0):
    """World-space triangles of the lid, for showing what the cut will be.

    `lift` raises the rim by the same amount the cut line is drawn above the
    surface, so on screen the lid meets the line instead of stopping a hair
    short of it.
    """
    model = evaluated(target)
    to_model = model.matrix_world.inverted()
    normal_matrix = model.matrix_world.to_3x3()
    tris = []
    for points, faces in cap_geometry(cut, target, ring=ring):
        if lift > 0.0:
            points = [_lifted(model, to_model, normal_matrix, p, lift)
                      for p in points]
        for a, b, c in faces:
            tris.extend((points[a], points[b], points[c]))
    return tris


def _plane_sheet(bm, cut, target):
    """A quad across the model on the cut's own plane, in the cut's space.

    What "cut right through" will do, drawn as what it is: one flat plane
    carrying on past the line and splitting everything it meets.
    """
    inverse = cut.matrix_world.inverted()
    corners = [inverse @ (target.matrix_world @ Vector(c))
               for c in target.bound_box]
    us = [p.x for p in corners]
    vs = [p.y for p in corners]
    pad = max(max(us) - min(us), max(vs) - min(vs), 1e-6) * 0.08
    u0, u1 = min(us) - pad, max(us) + pad
    v0, v1 = min(vs) - pad, max(vs) + pad
    verts = [bm.verts.new((u, v, 0.0))
             for u, v in ((u0, v0), (u1, v0), (u1, v1), (u0, v1))]
    bm.faces.new(verts)


def build_display_mesh(cut, target):
    """Replace the cut's mesh with the surface it would cut with.

    Shown as a wire, so what the cut object looks like in the viewport is the
    shape that will actually come out of it: the lid that spans the line, or
    the plane that carries on past it when the cut is not held to its line.
    """
    bm = bmesh.new()
    inverse = cut.matrix_world.inverted()
    if is_local(cut):
        for points, faces in cap_geometry(cut, target):
            verts = [bm.verts.new(inverse @ p) for p in points]
            for a, b, c in faces:
                try:
                    bm.faces.new((verts[a], verts[b], verts[c]))
                except ValueError:
                    pass  # that triangle is already there
    if not bm.faces:
        _plane_sheet(bm, cut, target)
    if bm.faces:
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)

    mesh = bpy.data.meshes.new("PartPin_CutSurface")
    bm.to_mesh(mesh)
    bm.free()
    old = cut.data
    cut.data = mesh
    if old is not None and old.users == 0:
        bpy.data.meshes.remove(old)
    return mesh


def build_surface_cutter(cut, target, scene=None):
    """Closed solid filling everything below the cut's plane.

    The "untick Cut Inside Line Only" fallback: it splits everything it
    meets, which is all it ever really did. The plane is the one fitted to
    the finished line.
    """
    scene = scene or bpy.context.scene
    frame_to_line(cut, target)
    return core.make_halfspace_cutter(cut.matrix_world,
                                      core.bbox_diagonal(target), scene)


# ----------------------------------------------------------------------
# Diagnosing a cut line
# ----------------------------------------------------------------------

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
# There used to be four more of these. They measured a lid that was
# synthesised to span the line — whether it folded, whether its rim broke out
# of the model, whether its middle ran through open space. No lid is built any
# more: the cut is made along the model's own faces, so it cannot fold, its
# rim is on the surface by construction, and what it spans is whatever the
# line encloses. Those could only ever fire on nothing, or worse, on a cut
# that works — which is how they lost the user's trust. They are gone.
ADRIFT = 'ADRIFT'        # an anchor has left the model's surface
STUCK = 'STUCK'          # the cut could not be carried through the surface
PINCHED = 'PINCHED'      # the line doubles back and nearly touches itself
BROKEN = 'BROKEN'        # the line cannot be walked across the surface here

TROUBLE = {
    ADRIFT: "the line has come off the model here — drag these points back on",
    STUCK: ("the cut could not be carried through the surface here — move the "
            "line off the crease, or take it a shorter way round"),
    PINCHED: ("the line doubles back on itself here, and there is no room to "
              "cut between the two sides — drag these points apart"),
    BROKEN: ("the line cannot get from one of these points to the next across "
             "the model — they are on pieces of it that do not join up"),
}


def inspect_cut(cut, target):
    """Everything measurably wrong with a cut, as places on the model.

    Read-only, and silent about a cut with nothing wrong with it. Whether the
    cut will separate is not guessed at here but answered by `trial_cut`,
    which makes the cut and looks.

    Returns {kind: [world positions]}.
    """
    from . import mesh_cut  # local import: mesh_cut builds on this module

    found = {ADRIFT: [], STUCK: list(STUCK_AT.get(cut.name, ())),
             PINCHED: [], BROKEN: []}
    if not usable_loop_indices(cut):
        return found

    diagonal = core.bbox_diagonal(target)
    rings = line_rings(cut, target)
    found[BROKEN].extend(BROKEN_AT.get(cut.name, ()))
    for ring in rings:
        if len(ring) < 3:
            continue
        found[PINCHED].extend(mesh_cut.hairpins(
            ring, mesh_cut.ring_normals(ring, target), diagonal))

    # Measured on the anchors as stored, not on the walked line, which is on
    # the surface by construction and so could never look adrift.
    matrix = target.matrix_world
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


def line_quality(cut, target):
    """How well the line holds what was drawn. Returns a dict of measurements.

    The four numbers the rework is judged on: how far the line strays from
    the model, how far it strays from the polyline through its own anchors,
    how evenly it is spaced, and how many hairpins it has.
    """
    from . import mesh_cut  # local import: mesh_cut builds on this module

    diagonal = core.bbox_diagonal(target)
    rings = line_rings(cut, target)
    anchors = world_anchors(cut, target)
    report = {'lines': len(rings), 'samples': sum(len(r) for r in rings),
              'diagonal': diagonal, 'off_surface': 0.0, 'off_anchors': 0.0,
              'spacing': (0.0, 0.0), 'hairpins': 0,
              'broken': len(BROKEN_AT.get(cut.name, ()))}
    if not rings:
        return report

    off_surface = max(surface_gap(target, p) for ring in rings for p in ring)
    gaps = [(ring[(i + 1) % len(ring)] - p).length
            for ring in rings for i, p in enumerate(ring)]
    off_anchors = 0.0
    for ring, loop in zip(rings, anchors):
        if len(loop) < 2:
            continue
        for point in ring:
            off_anchors = max(off_anchors, _distance_to_polyline(point, loop))
    hairpins = sum(len(mesh_cut.hairpins(ring,
                                         mesh_cut.ring_normals(ring, target),
                                         diagonal)) for ring in rings)
    report.update(off_surface=off_surface, off_anchors=off_anchors,
                  spacing=(min(gaps), max(gaps)), hairpins=hairpins)
    return report


def _distance_to_polyline(point, loop):
    """Distance from a point to the closed polyline through `loop`."""
    best = float('inf')
    count = len(loop)
    for i, a in enumerate(loop):
        b = loop[(i + 1) % count]
        span = b - a
        length = span.length_squared
        t = 0.0 if length < 1e-18 else max(
            0.0, min(1.0, (point - a).dot(span) / length))
        best = min(best, (a + span * t - point).length)
    return best


def _thin(points, most=60):
    """Keep a readable spread of markers rather than a solid mass of them."""
    if len(points) <= most:
        return points
    step = len(points) / most
    return [points[int(i * step)] for i in range(most)]


def trial_cut(cut, target, scene=None):
    """Actually try the cut on a copy and see what happens.

    Whether a cut separates is a question about the model, not something to
    infer from the shape of the cut, so this makes the cut, counts what falls
    out, throws the copy away, and only looks for reasons when the answer is
    that nothing came away.

    Returns (pieces, spots) — how many pieces it fell into, and where the cut
    could not be carried through if it did not.
    """
    from . import mesh_cut  # local import: mesh_cut builds on this module

    scene = scene or bpy.context.scene
    rings, normals = mesh_cut.line_rings(cut, target)
    if rings is None:
        STUCK_AT.pop(cut.name, None)
        return 0, []

    trial = core.duplicate_object(target, "PartPin_Trial", scene.collection)
    trial.hide_render = True
    pieces, spots = 1, []
    try:
        cut_pieces, trouble, spots = mesh_cut.cut_object(
            trial, rings, normals, scene)
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


# ----------------------------------------------------------------------
# Conversion: plane / drawn cut → editable surface cut
# ----------------------------------------------------------------------

def _anchors_on_surface(target, loops, per_loop):
    """World loops resampled and put on the model, as model-local anchors."""
    to_model = target.matrix_world.inverted()
    out = []
    for loop in loops:
        anchors, faces = [], []
        for point in resample_loop(loop, per_loop):
            landed, face = walker.place(target, to_model @ point)
            anchors.append(landed)
            faces.append(face)
        out.append((anchors, faces))
    return out


def convert_to_surface(context, cut, target, per_loop=16):
    """Turn a plane or drawn cut into an editable surface cut.

    Returns (cut_object, error_message). The object may be a *new* one
    when the original was a curve, since a curve object cannot become a
    mesh in place; connectors are re-parented in that case.
    """
    scene = context.scene
    if cut.pp_cut_kind == 'SURFACE':
        if len(cut.pp_points) >= 3:
            migrate(cut, target)
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

    placed = _anchors_on_surface(target, loops, per_loop)
    points, faces, loop_ids = [], [], []
    for index, (anchors, on_faces) in enumerate(placed):
        points.extend(anchors)
        faces.extend(on_faces)
        loop_ids.extend([index] * len(anchors))

    if cut.pp_cut_kind == 'CURVE':
        # A curve object cannot become a mesh in place, so the cut is
        # rebuilt as a mesh object and its connectors move across without
        # shifting in world space.
        matrix = cut.matrix_world.copy()
        new_cut = bpy.data.objects.new(
            cut.name, bpy.data.meshes.new("PartPin_CutSurface"))
        for coll in cut.users_collection:
            coll.objects.link(new_cut)
        new_cut.pp_role = core.ROLE_CUT
        new_cut.pp_enabled = cut.pp_enabled
        new_cut.pp_index = cut.pp_index
        new_cut.matrix_world = matrix
        for conn in core.cut_connectors(scene, cut):
            world = conn.matrix_world.copy()
            conn.parent = new_cut
            conn.matrix_parent_inverse = matrix.inverted()
            conn.matrix_world = world
        core.remove_object(cut)
        cut = new_cut

    cut.pp_cut_kind = 'SURFACE'
    store_anchors(cut, points, loop_ids, faces)
    frame_to_line(cut, target)
    build_display_mesh(cut, target)
    cut.display_type = 'WIRE'
    cut.show_in_front = False
    cut.hide_render = True
    return cut, None


def flatten_line(cut, target, per_loop=16):
    """Lay a cut's line back down where a flat cut would meet the model.

    The line lives on the model, so there is no height to zero any more.
    What "flatten" means is the flat cut nearest to the line as it stands:
    the plane fitted to it, sectioned against the model.
    """
    if not frame_to_line(cut, target):
        return False
    loops = [loop for loop in plane_section_loops(target, cut.matrix_world)
             if len(loop) >= 3]
    if not loops:
        return False
    # A plane through a limb can meet the model in several places; keep the
    # ones the line was already near, so flattening a collar does not swap it
    # for a section on the far side of the model.
    rings = line_rings(cut, target) or world_anchors(cut, target)
    if rings:
        reach = core.bbox_diagonal(target) * 0.25
        near = [loop for loop in loops
                if min((p - q).length for p in loop for q in rings[0])
                < reach]
        loops = near or loops[:1]

    placed = _anchors_on_surface(target, loops, per_loop)
    points, faces, loop_ids = [], [], []
    for index, (anchors, on_faces) in enumerate(placed):
        points.extend(anchors)
        faces.extend(on_faces)
        loop_ids.extend([index] * len(anchors))
    store_anchors(cut, points, loop_ids, faces)
    cut.pp_main_loop = 0
    return True


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
    """Turn a drawn stroke into a closed ring of anchors.

    The stroke arrives as world points already on the model (each one a
    ray-cast hit). Corners are kept — an anchor lands on each of them — and
    the straight runs between are filled in evenly, so the line can be dragged
    about without having lost the shape that was drawn. What runs between the
    anchors is walked across the surface, so the fill is only about giving the
    user handles at a comfortable spacing.
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

    to_model = target.matrix_world.inverted()
    anchors, faces = [], []
    for point in loop:
        landed, face = walker.place(target, to_model @ point)
        anchors.append(landed)
        faces.append(face)

    scene = context.scene
    draft = core.ensure_collection(scene, core.DRAFT_COLLECTION)
    cut = bpy.data.objects.new(name,
                               bpy.data.meshes.new("PartPin_CutSurface"))
    draft.objects.link(cut)
    cut.pp_role = core.ROLE_CUT
    cut.pp_cut_kind = 'SURFACE'
    cut.pp_enabled = True
    cut.pp_local = True
    cut.pp_main_loop = 0
    cut.pp_index = len(core.scene_cuts(scene)) - 1
    cut.display_type = 'WIRE'
    cut.show_in_front = False
    cut.hide_render = True
    store_anchors(cut, anchors, [0] * len(anchors), faces)
    frame_to_line(cut, target)
    build_display_mesh(cut, target)
    return cut, None


# ----------------------------------------------------------------------
# Connectors
# ----------------------------------------------------------------------

def snap_connectors(cut, target=None):
    """Move each connector back onto the cut's own plane and align it.

    That plane is the one fitted to the finished line — the same surface the
    middle of the cap is drawn down to, so a pin placed on it meets the face
    it has to bridge.
    """
    if target is not None:
        frame_to_line(cut, target)
    matrix = cut.matrix_world
    inverse = matrix.inverted()
    moved = 0
    for conn in core.cut_connectors(bpy.context.scene, cut):
        local = inverse @ conn.matrix_world.translation
        basis = Matrix.Translation(Vector((local.x, local.y, 0.0)))
        scale = conn.matrix_world.to_scale()
        conn.matrix_world = matrix @ basis @ Matrix.Diagonal(scale.to_4d())
        moved += 1
    return moved


def surface_connector_matrices(target, cut, count, inset=0.0, samples=22):
    """Connector transforms spread over the seam, aligned to the cut's plane.

    Candidates must sit inside the model *and* inside the cut line — the
    seam only exists there — and `inset` keeps them clear of its edge so a
    pin is not left half-hanging off the cut.
    """
    frame_to_line(cut, target)
    polys = loop_polygons(cut, target)
    if not polys:
        return []
    flat = [p for poly in polys for p in poly]
    us = [u for u, _v in flat]
    vs = [v for _u, v in flat]
    u0, u1, v0, v1 = min(us), max(us), min(vs), max(vs)
    n = max(int(samples), 3)

    def gather(required_inset):
        found = []
        for i in range(n):
            for j in range(n):
                u = u0 + (u1 - u0) * (i + 0.5) / n
                v = v0 + (v1 - v0) * (j + 0.5) / n
                if loop_inset(u, v, polys) < required_inset:
                    continue
                local = Vector((u, v, 0.0))
                if core.point_inside(target, cut.matrix_world @ local):
                    found.append(local)
        return found

    candidates = gather(inset)
    if not candidates:  # thin region: allow pins closer to the edge
        candidates = gather(0.0)
    if not candidates:
        return []

    matrices = []
    for local in core._pick_spread(candidates, count):
        matrices.append(cut.matrix_world @ Matrix.Translation(local))
    return matrices
