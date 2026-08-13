"""Geometry engine for PartPin.

Everything here is UI-free so it can run headless (tests, batch scripts).

Concepts
--------
Cut        A draft object the user can edit freely: a wire plane (straight
           cut) or a 2D curve with extrusion preview (curved cut).
Cutter     A closed volume built from a cut at apply time. Splitting is
           part ∩ cutter / part − cutter with the exact boolean solver, so
           both halves come out closed and capped.
Connector  A draft pin object sitting on the cut, spanning the seam
           symmetrically along its local Z. At apply time the pin is
           unioned into one part and a clearance-fattened copy is
           subtracted from the other, producing the socket.
"""

import bmesh
import bpy
from mathutils import Matrix, Quaternion, Vector
from mathutils.geometry import interpolate_bezier

DRAFT_COLLECTION = "PartPin Drafts"

ROLE_CUT = 'CUT'
ROLE_CONNECTOR = 'CONNECTOR'
ROLE_PART = 'PART'

PIN_COLOR = (1.0, 0.55, 0.1, 1.0)
PIN_COLOR_FLIPPED = (0.2, 0.55, 1.0, 1.0)


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------

def get_settings(context):
    return context.scene.part_pin


def world_bbox(obj):
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    lo = Vector(tuple(min(c[i] for c in corners) for i in range(3)))
    hi = Vector(tuple(max(c[i] for c in corners) for i in range(3)))
    return lo, hi


def bbox_diagonal(obj):
    lo, hi = world_bbox(obj)
    return max((hi - lo).length, 1e-6)


def ensure_collection(scene, name, unique=False):
    if not unique:
        coll = bpy.data.collections.get(name)
        if coll is not None:
            if name not in scene.collection.children:
                try:
                    scene.collection.children.link(coll)
                except RuntimeError:
                    pass
            return coll
    coll = bpy.data.collections.new(name)
    scene.collection.children.link(coll)
    return coll


def link_only(obj, coll):
    for c in list(obj.users_collection):
        c.objects.unlink(obj)
    coll.objects.link(obj)


def new_mesh_object(name, bm, coll, matrix=None):
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    if matrix is not None:
        obj.matrix_world = matrix
    coll.objects.link(obj)
    return obj


def duplicate_object(obj, name, coll):
    dup = obj.copy()
    dup.data = obj.data.copy()
    dup.name = name
    dup.data.name = name
    coll.objects.link(dup)
    return dup


def remove_object(obj):
    data = obj.data
    kind = obj.type
    bpy.data.objects.remove(obj)
    if data is not None and data.users == 0:
        if kind == 'MESH':
            bpy.data.meshes.remove(data)
        elif kind == 'CURVE':
            bpy.data.curves.remove(data)


def scene_cuts(scene):
    cuts = [o for o in scene.objects if o.pp_role == ROLE_CUT]
    cuts.sort(key=lambda o: (o.pp_index, o.name))
    return cuts


def cut_connectors(scene, cut):
    return [
        o for o in scene.objects
        if o.pp_role == ROLE_CONNECTOR and o.parent == cut
    ]


# ----------------------------------------------------------------------
# Mesh validation
# ----------------------------------------------------------------------

def mesh_issues(obj):
    """Return (non_manifold_edges, boundary_edges) of the evaluated mesh."""
    dg = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(obj.evaluated_get(dg))
    bm = bmesh.new()
    bm.from_mesh(me)
    non_manifold = sum(1 for e in bm.edges if not e.is_manifold)
    boundary = sum(1 for e in bm.edges if e.is_boundary)
    bm.free()
    bpy.data.meshes.remove(me)
    return non_manifold, boundary


def is_mesh_closed(obj):
    non_manifold, boundary = mesh_issues(obj)
    return non_manifold == 0 and boundary == 0


# ----------------------------------------------------------------------
# Booleans
# ----------------------------------------------------------------------

def boolean_apply(obj, other, operation):
    """Apply an exact boolean of `other` onto `obj` in place.

    Returns True if the result has geometry.
    """
    mod = obj.modifiers.new("PartPin", 'BOOLEAN')
    mod.object = other
    mod.operation = operation
    mod.solver = 'EXACT'
    mod.use_hole_tolerant = True

    dg = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(obj.evaluated_get(dg))

    obj.modifiers.remove(mod)
    old = obj.data
    obj.data = me
    me.name = old.name
    if old.users == 0:
        bpy.data.meshes.remove(old)
    return len(me.polygons) > 0


def point_inside(obj, world_point):
    """Ray-parity point-in-volume test against a closed mesh object."""
    obj = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    inv = obj.matrix_world.inverted()
    origin = inv @ world_point
    direction = Vector((0.4231, 0.5713, 0.7031)).normalized()
    step = bbox_diagonal(obj) * 1e-6 + 1e-9
    count = 0
    for _ in range(512):
        hit, loc, _normal, _idx = obj.ray_cast(origin, direction)
        if not hit:
            break
        count += 1
        origin = loc + direction * step
    return count % 2 == 1


# ----------------------------------------------------------------------
# Cutter volumes
# ----------------------------------------------------------------------

def make_halfspace_cutter(matrix_world, diag, scene):
    """Closed box occupying the negative side (local −Z) of a plane."""
    loc, rot, _scale = matrix_world.decompose()
    size = diag * 6.0
    normal = rot @ Vector((0.0, 0.0, 1.0))
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bm.transform(Matrix.LocRotScale(loc - normal * (size / 2.0), rot,
                                    Vector((size, size, size))))
    obj = new_mesh_object("PartPin_Cutter", bm, scene.collection)
    obj.hide_render = True
    return obj


def sample_cut_curve(curve_obj, resolution=16):
    """Sample the first spline of a cut curve to 2D points in local space."""
    data = curve_obj.data
    if not data.splines:
        return [], False
    spline = data.splines[0]
    pts = []
    if spline.type == 'BEZIER':
        bp = spline.bezier_points
        n = len(bp)
        if n < 2:
            return [], False
        segments = n if spline.use_cyclic_u else n - 1
        for i in range(segments):
            a = bp[i]
            b = bp[(i + 1) % n]
            seg = interpolate_bezier(a.co, a.handle_right, b.handle_left,
                                     b.co, resolution)
            pts.extend(seg if i == 0 else seg[1:])
    else:
        pts = [p.co.xyz for p in spline.points]
    pts2d = []
    for p in pts:
        q = Vector((p.x, p.y))
        if not pts2d or (q - pts2d[-1]).length > 1e-9:
            pts2d.append(q)
    return pts2d, spline.use_cyclic_u


def make_curve_cutter(curve_obj, target, scene):
    """Closed prism whose curved face follows the drawn cut curve.

    Built in the curve's local space (its XY plane holds the stroke, local Z
    is the sketch-view direction), then placed with the curve's matrix. The
    prism covers everything on one side of the stroke, extended well past the
    target's bounds.
    """
    pts, cyclic = sample_cut_curve(curve_obj)
    if len(pts) < 2:
        return None

    inv = curve_obj.matrix_world.inverted()
    corners = [inv @ (target.matrix_world @ Vector(c)) for c in target.bound_box]
    xs = [c.x for c in corners] + [p.x for p in pts]
    ys = [c.y for c in corners] + [p.y for p in pts]
    zs = [c.z for c in corners]
    extent = max(max(xs) - min(xs), max(ys) - min(ys),
                 max(zs) - min(zs), 1e-6)
    big = extent * 4.0

    if cyclic:
        polygon = pts
    else:
        t0 = pts[0] - pts[1]
        tn = pts[-1] - pts[-2]
        if t0.length < 1e-9 or tn.length < 1e-9:
            return None
        start = pts[0] + t0.normalized() * big
        end = pts[-1] + tn.normalized() * big
        floor_y = min(ys) - big
        polygon = ([start] + pts + [end,
                    Vector((end.x, floor_y)),
                    Vector((start.x, floor_y))])

    z_lo = min(zs) - big
    z_hi = max(zs) + big

    bm = bmesh.new()
    bottom = [bm.verts.new((p.x, p.y, z_lo)) for p in polygon]
    try:
        face = bm.faces.new(bottom)
    except ValueError:
        bm.free()
        return None
    ret = bmesh.ops.extrude_face_region(bm, geom=[face])
    top_verts = [g for g in ret['geom'] if isinstance(g, bmesh.types.BMVert)]
    bmesh.ops.translate(bm, verts=top_verts, vec=(0.0, 0.0, z_hi - z_lo))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bmesh.ops.triangulate(bm, faces=list(bm.faces))

    obj = new_mesh_object("PartPin_Cutter", bm, scene.collection,
                          matrix=curve_obj.matrix_world.copy())
    obj.hide_render = True
    return obj


def build_cutter(cut_obj, target, scene):
    if cut_obj.pp_cut_kind == 'SURFACE':
        # Imported here: surface.py builds on this module's helpers.
        from . import surface
        resolution = get_settings(bpy.context).surface_resolution
        return surface.build_surface_cutter(cut_obj, target, resolution,
                                            scene)
    if cut_obj.pp_cut_kind == 'CURVE':
        return make_curve_cutter(cut_obj, target, scene)
    return make_halfspace_cutter(cut_obj.matrix_world, bbox_diagonal(target),
                                 scene)


# ----------------------------------------------------------------------
# Connector geometry
# ----------------------------------------------------------------------

def _lathe(bm, profile, segments):
    """Spin an XZ profile (list of (radius, z)) around Z into a closed solid."""
    verts = [bm.verts.new((r, 0.0, z)) for r, z in profile]
    edges = [bm.edges.new((verts[i], verts[i + 1]))
             for i in range(len(verts) - 1)]
    bmesh.ops.spin(
        bm,
        geom=verts + edges,
        cent=(0.0, 0.0, 0.0),
        axis=(0.0, 0.0, 1.0),
        dvec=(0.0, 0.0, 0.0),
        angle=6.283185307179586,
        steps=segments,
        use_merge=True,
    )
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-7)
    bmesh.ops.dissolve_degenerate(bm, dist=1e-9, edges=bm.edges)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)


def build_pin_mesh(shape, size, length, segments=32):
    """Closed pin solid spanning local Z ∈ [−length, +length]."""
    bm = bmesh.new()
    r = max(size / 2.0, 1e-6)
    length = max(length, 1e-6)
    if shape == 'BOX':
        bmesh.ops.create_cube(bm, size=1.0)
        bm.transform(Matrix.Diagonal((size, size, 2.0 * length, 1.0)))
    elif shape == 'TAPER':
        tip = r * 0.45
        _lathe(bm, [(0.0, -length), (tip, -length), (r, 0.0),
                    (tip, length), (0.0, length)], segments)
    else:  # CYLINDER (default)
        ch = min(0.25 * length, 0.6 * r)
        _lathe(bm, [(0.0, -length), (r - ch, -length), (r, -length + ch),
                    (r, length - ch), (r - ch, length), (0.0, length)],
               segments)
    mesh = bpy.data.meshes.new("PartPin_Pin")
    bm.to_mesh(mesh)
    bm.free()
    return mesh


def realize_world_copy(obj, scene, offset=0.0):
    """World-space snapshot of a mesh object, optionally fattened.

    `offset` moves every vertex along its normal — used to grow the pin
    into the socket cavity by the clearance.
    """
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.transform(obj.matrix_world)
    if offset > 0.0:
        bm.normal_update()
        deltas = [v.normal.copy() * offset for v in bm.verts]
        for v, d in zip(bm.verts, deltas):
            v.co += d
    return new_mesh_object("PartPin_Solid", bm, scene.collection)


def connector_probe_points(conn):
    """World points just inside the pin-side / socket-side parts."""
    z_max = max((abs(v[2]) for v in conn.bound_box), default=1.0)
    probe = z_max * 0.5
    if conn.pp_pin_flip:
        probe = -probe
    pin_pt = conn.matrix_world @ Vector((0.0, 0.0, probe))
    sock_pt = conn.matrix_world @ Vector((0.0, 0.0, -probe))
    return pin_pt, sock_pt


def make_connector_object(context, cut, matrix, name="Pin"):
    """Create a draft connector object parented to `cut`."""
    s = get_settings(context)
    scene = context.scene
    draft = ensure_collection(scene, DRAFT_COLLECTION)

    if s.shape == 'CUSTOM' and s.custom_object is not None:
        mesh = s.custom_object.data.copy()
        mesh.name = "PartPin_Pin"
    else:
        shape = s.shape if s.shape != 'CUSTOM' else 'CYLINDER'
        mesh = build_pin_mesh(shape, s.size, s.length)

    conn = bpy.data.objects.new(name, mesh)
    draft.objects.link(conn)
    conn.matrix_world = matrix
    conn.parent = cut
    conn.matrix_parent_inverse = cut.matrix_world.inverted()
    conn.pp_role = ROLE_CONNECTOR
    conn.pp_shape = s.shape
    conn.pp_clearance = s.clearance
    conn.show_in_front = True
    conn.hide_render = True
    conn.color = PIN_COLOR
    conn.display.show_shadows = False
    return conn


def auto_size_defaults(settings, target):
    """Derive sensible connector defaults from the model size."""
    diag = bbox_diagonal(target)
    settings.size = diag * 0.045
    settings.length = settings.size * 1.1
    settings.clearance = settings.size * 0.035
    settings.sized = True


# ----------------------------------------------------------------------
# Connector auto-placement
# ----------------------------------------------------------------------

def plane_section_frame(target, matrix_world):
    """Cross-section of `target` with a cut plane, in the plane's 2D frame.

    Returns (centroid_uv, (u_min, u_max), (v_min, v_max)) or None if the
    plane misses the model.
    """
    loc, rot, _scale = matrix_world.decompose()
    normal = rot @ Vector((0.0, 0.0, 1.0))

    dg = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(target.evaluated_get(dg))
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.transform(target.matrix_world)
    ret = bmesh.ops.bisect_plane(
        bm,
        geom=list(bm.verts) + list(bm.edges) + list(bm.faces),
        dist=1e-6,
        plane_co=loc,
        plane_no=normal,
        clear_inner=True,
        clear_outer=True,
    )
    cut_verts = [g for g in ret['geom_cut']
                 if isinstance(g, bmesh.types.BMVert)]
    inv_rot = rot.inverted()
    uvs = []
    for v in cut_verts:
        local = inv_rot @ (v.co - loc)
        uvs.append((local.x, local.y))
    bm.free()
    bpy.data.meshes.remove(me)

    if not uvs:
        return None
    us = [p[0] for p in uvs]
    vs = [p[1] for p in uvs]
    centroid = (sum(us) / len(us), sum(vs) / len(vs))
    return centroid, (min(us), max(us)), (min(vs), max(vs))


def _pick_spread(candidates, count):
    """Pick `count` items spread evenly across a list, without repeats."""
    picked = []
    for j in range(count):
        t = (j + 1) / (count + 1)
        idx = min(int(t * len(candidates)), len(candidates) - 1)
        if idx not in picked:
            picked.append(idx)
    return [candidates[i] for i in picked]


def plane_connector_matrices(target, cut, count):
    """Evenly spaced connector transforms across a straight cut.

    Candidate spots along the section's major axis are kept only when they
    are genuinely inside the model, so cuts through hollow cross-sections
    (e.g. a ring) still get usable pins.
    """
    frame = plane_section_frame(target, cut.matrix_world)
    if frame is None:
        return []
    centroid, (u0, u1), (v0, v1) = frame
    loc, rot, _scale = cut.matrix_world.decompose()
    along_u = (u1 - u0) >= (v1 - v0)
    lo, hi = (u0, u1) if along_u else (v0, v1)
    other = centroid[1] if along_u else centroid[0]

    n_candidates = max(count * 8, 48)
    good = []
    for i in range(n_candidates):
        t = (i + 1) / (n_candidates + 1)
        a = lo + (hi - lo) * t
        uv = (a, other) if along_u else (other, a)
        world = loc + rot @ Vector((uv[0], uv[1], 0.0))
        if point_inside(target, world):
            good.append(world)
    return [Matrix.LocRotScale(w, rot, Vector((1, 1, 1)))
            for w in _pick_spread(good, count)] if good else []


def curve_connector_matrices(target, cut, count):
    """Connector transforms along a drawn cut curve, pins normal to the
    cut surface (tangent × sketch-view direction). Only spots genuinely
    inside the model are used."""
    pts, cyclic = sample_cut_curve(cut, resolution=24)
    if len(pts) < 2:
        return []
    m = cut.matrix_world

    dense = []
    pairs = list(zip(pts, pts[1:] + ([pts[0]] if cyclic else [])))
    for a, b in pairs:
        for i in range(8):
            dense.append(a.lerp(b, i / 8.0))
    dense.append(pts[0] if cyclic else pts[-1])

    world_pts = [m @ Vector((p.x, p.y, 0.0)) for p in dense]
    inside = [i for i, w in enumerate(world_pts) if point_inside(target, w)]
    if not inside:
        return []

    extrude_dir = (m.to_quaternion() @ Vector((0.0, 0.0, 1.0))).normalized()
    matrices = []
    for idx in _pick_spread(inside, count):
        prev_i = max(idx - 1, 0)
        next_i = min(idx + 1, len(world_pts) - 1)
        tangent = (world_pts[next_i] - world_pts[prev_i])
        if tangent.length < 1e-9:
            continue
        tangent.normalize()
        pin_axis = tangent.cross(extrude_dir)
        if pin_axis.length < 1e-9:
            continue
        pin_axis.normalize()
        y_axis = pin_axis.cross(tangent)
        basis = Matrix((
            (tangent.x, y_axis.x, pin_axis.x, world_pts[idx].x),
            (tangent.y, y_axis.y, pin_axis.y, world_pts[idx].y),
            (tangent.z, y_axis.z, pin_axis.z, world_pts[idx].z),
            (0.0, 0.0, 0.0, 1.0),
        ))
        matrices.append(basis)
    return matrices


# ----------------------------------------------------------------------
# Apply pipeline
# ----------------------------------------------------------------------

def split_parts(parts, cutter, parts_coll):
    """Split every part that the cutter volume actually intersects.

    Returns (parts, split_any) — a cutter legitimately misses parts that
    earlier cuts already separated, so that alone is not a problem.
    """
    result = []
    split_any = False
    for part in parts:
        outside = duplicate_object(part, part.name, parts_coll)
        inside = duplicate_object(part, part.name, parts_coll)
        ok_out = boolean_apply(outside, cutter, 'DIFFERENCE')
        ok_in = boolean_apply(inside, cutter, 'INTERSECT')
        if ok_out and ok_in:
            remove_object(part)
            result.extend((outside, inside))
            split_any = True
        else:
            remove_object(outside)
            remove_object(inside)
            result.append(part)
    return result, split_any


def split_loose(obj):
    """Split an object's disconnected shells into separate objects."""
    context = bpy.context
    view_layer = context.view_layer
    if obj.name not in view_layer.objects:
        return [obj]
    previous = view_layer.objects.active
    for other in list(context.selected_objects):
        other.select_set(False)
    obj.select_set(True)
    view_layer.objects.active = obj
    try:
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.separate(type='LOOSE')
    except RuntimeError:
        return [obj]
    finally:
        if obj.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except RuntimeError:
                pass
        view_layer.objects.active = previous

    pieces = [o for o in context.selected_objects if o.type == 'MESH']
    if obj not in pieces:
        pieces.append(obj)
    return pieces


def mesh_volume(obj):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.transform(obj.matrix_world)
    volume = bm.calc_volume(signed=False)
    bm.free()
    return volume


def drop_debris(pieces, share=1e-4):
    """Discard numerical crumbs left by a boolean.

    A cut that grazes along the surface can shed slivers of no volume. They
    are not parts anyone would print, and passing them on as parts means
    stray specks in the collection and in the exported files.
    """
    if len(pieces) < 2:
        return pieces, 0
    volumes = [(piece, mesh_volume(piece)) for piece in pieces]
    biggest = max(volume for _piece, volume in volumes)
    if biggest <= 0.0:
        return pieces, 0
    kept, dropped = [], 0
    for piece, volume in volumes:
        if volume > biggest * share:
            kept.append(piece)
        else:
            remove_object(piece)
            dropped += 1
    return (kept or pieces), dropped


def split_parts_surgery(parts, rings, normals, normal, scene, parts_coll,
                        settle=None):
    """Cut every part the drawn line runs across, and leave the rest whole.

    A line only ever crosses one of the parts on the table, and it may cross
    none of them if an earlier cut already took that piece away, so a part the
    line misses is not a failure — it is passed through untouched.
    """
    from . import mesh_cut  # local import: mesh_cut builds on this module

    result = []
    split_any = False
    for part in parts:
        pieces, _problem = mesh_cut.cut_object(part, rings, normals, normal,
                                               scene, parts_coll, settle)
        if pieces is None:
            result.append(part)
            continue
        remove_object(part)
        for piece in pieces:
            piece.pp_role = ROLE_PART
        result.extend(pieces)
        split_any = True
    return result, split_any


def split_parts_local(parts, slab, parts_coll, debris=None, tangled=None):
    """Sever parts with a thin slab and keep the resulting pieces.

    Unlike a plane cutter this only separates what the slab actually
    spans, so material elsewhere in the model is left whole.
    """
    result = []
    split_any = False
    if debris is None:
        debris = [0]
    for part in parts:
        work = duplicate_object(part, part.name, parts_coll)
        if not boolean_apply(work, slab, 'DIFFERENCE'):
            # An empty result means the cutter tangled: the solver cannot make
            # sense of a surface that crosses itself, and returns nothing. That
            # is a different problem from the piece being joined on, and worth
            # saying so rather than blaming the line.
            if tangled is not None:
                tangled[0] = True
            remove_object(work)
            result.append(part)
            continue
        pieces, crumbs = drop_debris(split_loose(work))
        if crumbs:
            debris[0] += crumbs
        if len(pieces) < 2:
            for piece in pieces:
                remove_object(piece)
            result.append(part)
            continue
        remove_object(part)
        for piece in pieces:
            piece.pp_role = ROLE_PART
        result.extend(pieces)
        split_any = True
    return result, split_any


def apply_connector(parts, conn, scene, warnings):
    pin_pt, sock_pt = connector_probe_points(conn)
    pin_part = next((p for p in parts if point_inside(p, pin_pt)), None)
    sock_part = next((p for p in parts if point_inside(p, sock_pt)), None)
    if pin_part is None or sock_part is None or pin_part == sock_part:
        warnings.append(
            f"Connector '{conn.name}' does not bridge two parts — skipped")
        return False

    pin_solid = realize_world_copy(conn, scene)
    boolean_apply(pin_part, pin_solid, 'UNION')
    remove_object(pin_solid)

    sock_solid = realize_world_copy(conn, scene, offset=conn.pp_clearance)
    boolean_apply(sock_part, sock_solid, 'DIFFERENCE')
    remove_object(sock_solid)
    return True


def create_parts(context, target, cuts, keep_original=True, part_gap=0.0,
                 failures=None):
    """Run every enabled cut and connector; return (parts, applied, warnings).

    `failures` collects the reasons any cut could not be made, so the caller
    can report them as errors rather than letting a cut fail silently.
    """
    scene = context.scene
    warnings = []
    if failures is None:
        failures = []

    parts_coll = ensure_collection(scene, f"{target.name} Parts", unique=True)
    base = duplicate_object(target, f"{target.name}_part", parts_coll)
    base.pp_role = ROLE_PART
    base.hide_render = False
    parts = [base]

    from . import surface  # local import: surface.py builds on this module

    for cut in cuts:
        if surface.is_local(cut):
            rings, normals, normal, settle = surface.line_rings(cut, target)
            if rings is None:
                failures.append(
                    f"Cut '{cut.name}': "
                    f"{surface.cut_line_problem(cut) or 'no usable cut line'}")
                continue
            _usable, _problem, note = surface.loop_quality(cut,
                                                           min_alignment=0.0)
            if note:
                warnings.append(f"Cut '{cut.name}': {note}")
            parts, split_any = split_parts_surgery(parts, rings, normals,
                                                   normal, scene, parts_coll,
                                                   settle)
            if not split_any:
                # Say why, and leave it at that. Reaching further to force a
                # separation would cut material outside the line, which is
                # the one thing this mode promises not to do.
                failures.append(
                    f"Cut '{cut.name}': {surface.failure_reason(cut, target)}")
            continue

        if cut.pp_cut_kind == 'SURFACE':
            surface.refit_frame(cut)
        cutter = build_cutter(cut, target, scene)
        if cutter is None:
            warnings.append(f"Cut '{cut.name}' has no usable geometry — skipped")
            continue
        parts, split_any = split_parts(parts, cutter, parts_coll)
        if not split_any:
            warnings.append(f"Cut '{cut.name}' did not split anything")
        remove_object(cutter)

    context.view_layer.update()

    applied = 0
    if len(parts) > 1:
        for cut in cuts:
            for conn in cut_connectors(scene, cut):
                if apply_connector(parts, conn, scene, warnings):
                    applied += 1
                context.view_layer.update()
    elif any(cut_connectors(scene, cut) for cut in cuts):
        warnings.append("Nothing was split — connectors were not applied")

    for i, part in enumerate(parts, start=1):
        part.name = f"{target.name}_part_{i:02d}"
        part.data.name = part.name
        part.pp_role = ROLE_PART

    if part_gap > 0.0 and len(parts) > 1:
        centers = {}
        total = Vector()
        for part in parts:
            lo, hi = world_bbox(part)
            centers[part] = (lo + hi) / 2.0
            total += centers[part]
        overall = total / len(parts)
        for part in parts:
            direction = centers[part] - overall
            if direction.length > 1e-9:
                part.matrix_world.translation += (
                    direction.normalized() * part_gap)

    if keep_original:
        target.hide_set(True)
    else:
        remove_object(target)

    return parts, applied, warnings


def hide_drafts(scene, hide=True):
    for cut in scene_cuts(scene):
        for conn in cut_connectors(scene, cut):
            conn.hide_set(hide)
        cut.hide_set(hide)
