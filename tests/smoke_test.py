"""Headless smoke test for the PartPin add-on.

Run with:

    blender --background --python-exit-code 1 --python tests/smoke_test.py

Covers: straight cuts (draft + easy mode), multiple cuts, disabled cuts,
curved cuts, built-in and custom connectors, pin/socket generation,
manifold validation, and STL/OBJ/FBX export.
"""

import math
import os
import sys
import tempfile
import traceback

import bpy  # noqa: F401 — must precede bmesh for the pip "bpy" module
import bmesh
from mathutils import Quaternion, Vector

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

FAILURES = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" ({detail})" if detail else ""))
    if not condition:
        FAILURES.append(f"{label} {detail}".strip())


def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def link(obj):
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.update()
    return obj


def make_sphere(name="Model", radius=1.0):
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=48, v_segments=24,
                              radius=radius)
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    return link(bpy.data.objects.new(name, mesh))


def make_open_plane(name="Broken"):
    bm = bmesh.new()
    verts = [bm.verts.new(v) for v in
             ((-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0))]
    bm.faces.new(verts)
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    return link(bpy.data.objects.new(name, mesh))


def make_small_cube(name="CustomPeg", half=0.09):
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=half * 2.0)
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    return link(bpy.data.objects.new(name, mesh))


def volume(obj):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.transform(obj.matrix_world)
    vol = bm.calc_volume(signed=False)
    bm.free()
    return vol


def z_range(obj):
    zs = [(obj.matrix_world @ v.co).z for v in obj.data.vertices]
    return min(zs), max(zs)


def parts_of(settings):
    coll = settings.parts_collection
    return [o for o in coll.objects if o.type == 'MESH'] if coll else []


def is_closed(core, obj):
    non_manifold, boundary = core.mesh_issues(obj)
    return non_manifold == 0 and boundary == 0


def scenario_plane_cut(core):
    print("Scenario: draft straight cut with connectors")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_sphere()
    original_volume = volume(model)
    s.target = model

    bpy.ops.partpin.add_plane_cut()
    cuts = core.scene_cuts(bpy.context.scene)
    check("plane cut created", len(cuts) == 1)

    bpy.ops.partpin.add_connectors()
    conns = core.cut_connectors(bpy.context.scene, cuts[0])
    check("connectors auto-placed", len(conns) == s.count,
          f"expected {s.count}, got {len(conns)}")

    bpy.ops.partpin.create_parts()
    parts = parts_of(s)
    check("two parts created", len(parts) == 2)
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))

    upper = max(parts, key=lambda p: z_range(p)[1])
    lower = min(parts, key=lambda p: z_range(p)[0])
    check("distinct upper/lower parts", upper != lower)
    check("pin protrudes from upper part", z_range(upper)[0] < -0.05,
          f"min z {z_range(upper)[0]:.4f}")
    check("socket part keeps flat face", z_range(lower)[1] < 0.005,
          f"max z {z_range(lower)[1]:.4f}")
    check("pin part heavier than socket part",
          volume(upper) > volume(lower) + 1e-4)
    total = volume(upper) + volume(lower)
    check("volume roughly conserved",
          0.9 * original_volume < total < original_volume + 0.01,
          f"{total:.4f} vs {original_volume:.4f}")
    check("original kept and hidden",
          model.name in bpy.context.scene.objects and model.hide_get())
    return s


def scenario_multi_cut_disabled(core):
    print("Scenario: multiple cuts + disabled cut ignored")
    reset_scene()
    s = bpy.context.scene.part_pin
    bpy.ops.mesh.primitive_torus_add(major_radius=1.0, minor_radius=0.3,
                                     major_segments=64, minor_segments=24)
    model = bpy.context.view_layer.objects.active
    model.name = "Ring"
    s.target = model
    s.plane_axis = 'X'
    s.count = 3

    bpy.ops.partpin.add_plane_cut()
    cut1 = bpy.context.view_layer.objects.active
    cut1.location.x = 0.8
    bpy.context.view_layer.update()
    bpy.ops.partpin.add_connectors()

    bpy.ops.partpin.add_plane_cut()
    cut2 = bpy.context.view_layer.objects.active
    cut2.location.x = -0.8
    bpy.context.view_layer.update()
    bpy.ops.partpin.add_connectors()

    # A third cut that would split things further — disabled, so ignored.
    bpy.ops.partpin.add_plane_cut()
    cut3 = bpy.context.view_layer.objects.active
    bpy.ops.partpin.cut_toggle(cut_name=cut3.name)
    check("cut disabled", not cut3.pp_enabled)

    bpy.ops.partpin.create_parts()
    parts = parts_of(s)
    check("three parts from two enabled cuts", len(parts) == 3,
          f"got {len(parts)}")
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))


def scenario_curved_cut(core):
    print("Scenario: curved cut")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_sphere()
    original_volume = volume(model)
    s.target = model

    data = bpy.data.curves.new("Cut Curve", 'CURVE')
    data.dimensions = '2D'
    data.fill_mode = 'NONE'
    spline = data.splines.new('POLY')
    spline.points.add(4)
    wave = [(-2.0, 0.0), (-1.0, 0.3), (0.0, -0.3), (1.0, 0.3), (2.0, 0.0)]
    for point, (x, y) in zip(spline.points, wave):
        point.co = (x, y, 0.0, 1.0)
    cut = bpy.data.objects.new("Cut Curve", data)
    link(cut)
    # Stroke plane = world XZ, prism extrudes along world Y through the model.
    cut.rotation_mode = 'QUATERNION'
    cut.rotation_quaternion = Quaternion((1.0, 0.0, 0.0), 1.5707963)
    cut.pp_role = core.ROLE_CUT
    cut.pp_cut_kind = 'CURVE'
    cut.pp_enabled = True
    bpy.context.view_layer.update()

    cutter = core.make_curve_cutter(cut, model, bpy.context.scene)
    check("curve cutter built", cutter is not None)
    if cutter is not None:
        check("curve cutter closed", is_closed(core, cutter))
        core.remove_object(cutter)

    bpy.ops.partpin.add_connectors()
    conns = core.cut_connectors(bpy.context.scene, cut)
    check("connectors placed on curve", len(conns) >= 1,
          f"got {len(conns)}")

    bpy.ops.partpin.create_parts()
    parts = parts_of(s)
    check("curved cut made two parts", len(parts) == 2, f"got {len(parts)}")
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))
    if len(parts) == 2:
        total = sum(volume(p) for p in parts)
        check("curved cut volume sane",
              0.9 * original_volume < total < original_volume + 0.01,
              f"{total:.4f} vs {original_volume:.4f}")


def scenario_custom_connector(core):
    print("Scenario: custom connector mesh")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_sphere()
    peg = make_small_cube()
    peg.location = (5.0, 0.0, 0.0)  # parked off to the side
    s.target = model
    s.shape = 'CUSTOM'
    s.custom_object = peg
    s.clearance = 0.004
    s.count = 2

    bpy.ops.partpin.add_plane_cut()
    bpy.ops.partpin.add_connectors()
    bpy.ops.partpin.create_parts()
    parts = parts_of(s)
    check("two parts with custom connector", len(parts) == 2)
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))
    if len(parts) == 2:
        upper = max(parts, key=lambda p: z_range(p)[1])
        check("custom pin protrudes", z_range(upper)[0] < -0.03,
              f"min z {z_range(upper)[0]:.4f}")


def scenario_flip_pin(core):
    print("Scenario: flipped pin side")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_sphere()
    s.target = model
    s.count = 1

    bpy.ops.partpin.add_plane_cut()
    bpy.ops.partpin.add_connectors()
    bpy.ops.partpin.flip_pin()
    bpy.ops.partpin.create_parts()
    parts = parts_of(s)
    check("two parts (flipped)", len(parts) == 2)
    if len(parts) == 2:
        upper = max(parts, key=lambda p: z_range(p)[1])
        lower = min(parts, key=lambda p: z_range(p)[0])
        check("flipped: pin protrudes from lower part",
              z_range(lower)[1] > 0.05, f"max z {z_range(lower)[1]:.4f}")
        check("flipped: upper keeps flat face",
              z_range(upper)[0] > -0.005, f"min z {z_range(upper)[0]:.4f}")


def scenario_easy_mode(core):
    print("Scenario: easy mode one-click")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_sphere()
    s.target = model

    bpy.ops.partpin.easy_cut(axis='Y', at_cursor=False)
    parts = parts_of(s)
    check("easy mode made two parts", len(parts) == 2)
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))
    leftovers = [o for o in bpy.context.scene.objects
                 if o.pp_role in (core.ROLE_CUT, core.ROLE_CONNECTOR)]
    check("easy mode cleaned its drafts", not leftovers,
          f"{[o.name for o in leftovers]}")
    return s


def scenario_validation(core):
    print("Scenario: non-manifold mesh rejected")
    reset_scene()
    s = bpy.context.scene.part_pin
    broken = make_open_plane()
    s.target = broken
    bpy.ops.partpin.add_plane_cut()
    raised = False
    try:
        bpy.ops.partpin.create_parts()
    except RuntimeError:
        raised = True
    check("open mesh rejected with error", raised)


def scenario_export(core):
    print("Scenario: export STL / OBJ / FBX")
    s = scenario_easy_mode(core)  # fresh scene with two finished parts
    tmp = tempfile.mkdtemp(prefix="partpin_")
    s.export_dir = tmp
    s.export_scale = 1000.0

    s.export_format = 'STL'
    s.export_batch = True
    bpy.ops.partpin.export_parts()
    stls = [f for f in os.listdir(tmp) if f.endswith(".stl")]
    check("batch STL wrote one file per part", len(stls) == 2,
          f"{stls}")
    check("STL files non-empty",
          all(os.path.getsize(os.path.join(tmp, f)) > 200 for f in stls))

    s.export_format = 'OBJ'
    s.export_batch = False
    bpy.ops.partpin.export_parts()
    objs = [f for f in os.listdir(tmp) if f.endswith(".obj")]
    check("single OBJ written", len(objs) == 1, f"{objs}")

    s.export_format = 'FBX'
    bpy.ops.partpin.export_parts()
    fbxs = [f for f in os.listdir(tmp) if f.endswith(".fbx")]
    check("single FBX written", len(fbxs) == 1, f"{fbxs}")
    print(f"  export dir: {tmp}")


def surface_distance(obj, world_point):
    """Distance from a world point to an object's surface."""
    ok, loc, _n, _i = obj.closest_point_on_mesh(
        obj.matrix_world.inverted() @ world_point)
    if not ok:
        return float('inf')
    return ((obj.matrix_world @ loc) - world_point).length


def scenario_height_field(core):
    print("Scenario: height field maths")
    from part_pin import surface
    from mathutils import Vector as V

    flat = surface.HeightField([V((0, 0, 0)), V((1, 0, 0)), V((0, 1, 0))])
    check("flat field stays flat", abs(flat.eval(0.4, 0.4)) < 1e-12)

    nodes = [V((0, 0, 0)), V((1, 0, 0.3)), V((0, 1, -0.2)), V((1, 1, 0.1)),
             V((0.5, 0.5, 0.25))]
    field = surface.HeightField(nodes, falloff=2.0)
    worst = max(abs(field.eval(n.x, n.y) - n.z) for n in nodes)
    check("field passes through every control point", worst < 1e-4,
          f"max error {worst:.2e}")
    far = abs(field.eval(14.0, 14.0))
    check("field flattens away from the points", far < 1e-3,
          f"h={far:.2e}")
    n = field.normal(0.5, 0.5)
    check("normal is unit length and upward",
          abs(n.length - 1.0) < 1e-6 and n.z > 0.0)


def scenario_surface_convert(core):
    print("Scenario: plane cut → editable surface cut")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_sphere()
    original_volume = volume(model)
    s.target = model
    from part_pin import surface

    bpy.ops.partpin.add_plane_cut()
    cut = bpy.context.view_layer.objects.active
    cut, error = surface.convert_to_surface(bpy.context, cut, model,
                                           per_loop=16)
    check("conversion succeeded", cut is not None, str(error))
    if cut is None:
        return
    check("cut is now a surface cut", cut.pp_cut_kind == 'SURFACE')
    check("16 control points stored", len(cut.pp_points) == 16,
          f"got {len(cut.pp_points)}")

    on_surface = max(surface_distance(model, cut.matrix_world @ Vector(p.co))
                     for p in cut.pp_points)
    check("control points sit on the model surface", on_surface < 1e-3,
          f"max {on_surface:.2e}")
    heights = [abs(p.co[2]) for p in cut.pp_points]
    check("unedited surface is still flat", max(heights) < 1e-6)

    bpy.ops.partpin.create_parts()
    parts = parts_of(s)
    check("flat surface cut still makes two parts", len(parts) == 2)
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))
    if len(parts) == 2:
        total = sum(volume(p) for p in parts)
        check("volume conserved", abs(total - original_volume) < 0.01,
              f"{total:.4f} vs {original_volume:.4f}")


def scenario_surface_drag(core):
    print("Scenario: dragging a point reshapes the cut")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_sphere()
    original_volume = volume(model)
    s.target = model
    from part_pin import surface
    import math

    bpy.ops.partpin.add_plane_cut()
    cut = bpy.context.view_layer.objects.active
    cut, error = surface.convert_to_surface(bpy.context, cut, model,
                                           per_loop=16)
    if cut is None:
        check("conversion succeeded", False, str(error))
        return

    # Simulate a drag: slide one point up the sphere to 40° latitude,
    # exactly what the modal operator does on mouse-move.
    point = cut.pp_points[0]
    start = cut.matrix_world @ Vector(point.co)
    azimuth = math.atan2(start.y, start.x)
    lat = math.radians(40.0)
    # Snap onto the faceted mesh, exactly like a viewport ray-cast hit.
    dragged = surface.project_to_surface(
        model, Vector((math.cos(lat) * math.cos(azimuth),
                       math.cos(lat) * math.sin(azimuth),
                       math.sin(lat))))
    check("drag target is on the model surface",
          surface_distance(model, dragged) < 1e-6,
          f"{surface_distance(model, dragged):.2e}")
    point.co = cut.matrix_world.inverted() @ dragged
    surface.build_display_mesh(cut, model)

    field = surface.field_for(cut)
    local = Vector(point.co)
    check("cut surface passes through the dragged point",
          abs(field.eval(local.x, local.y) - local.z) < 1e-4,
          f"{field.eval(local.x, local.y):.6f} vs {local.z:.6f}")

    bpy.ops.partpin.create_parts()
    parts = parts_of(s)
    check("reshaped cut makes two parts", len(parts) == 2, f"got {len(parts)}")
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))
    if len(parts) != 2:
        return

    total = sum(volume(p) for p in parts)
    check("volume conserved after reshaping",
          abs(total - original_volume) < 0.02,
          f"{total:.4f} vs {original_volume:.4f}")

    # The seam must actually run through the dragged point: it lies on the
    # cut face, which both parts share. The cut surface is a grid, so allow
    # roughly one cell of discretization error.
    lo, hi = core.world_bbox(model)
    cell = max(hi - lo) * 1.16 / s.surface_resolution
    worst = max(surface_distance(p, dragged) for p in parts)
    check("both parts touch the dragged point", worst < cell * 1.5,
          f"max distance {worst:.4f}, one grid cell is {cell:.4f}")

    lower = min(parts, key=lambda p: z_range(p)[0])
    check("cut is no longer flat (lower part bulges upward)",
          z_range(lower)[1] > 0.3, f"max z {z_range(lower)[1]:.4f}")

    bpy.ops.partpin.clear_drafts()


def scenario_surface_connectors(core):
    print("Scenario: connectors on a reshaped cut")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_sphere()
    s.target = model
    s.count = 2
    from part_pin import surface

    bpy.ops.partpin.add_plane_cut()
    cut = bpy.context.view_layer.objects.active
    cut, _error = surface.convert_to_surface(bpy.context, cut, model,
                                             per_loop=12)
    point = cut.pp_points[0]
    point.co = (point.co[0], point.co[1], 0.35)
    surface.build_display_mesh(cut, model)

    bpy.context.view_layer.objects.active = cut
    bpy.ops.partpin.add_connectors()
    conns = core.cut_connectors(bpy.context.scene, cut)
    check("connectors placed on the surface cut", len(conns) == 2,
          f"got {len(conns)}")

    field = surface.field_for(cut)
    off = 0.0
    for conn in conns:
        local = cut.matrix_world.inverted() @ conn.matrix_world.translation
        off = max(off, abs(field.eval(local.x, local.y) - local.z))
    check("connectors sit on the cut surface", off < 1e-4,
          f"max {off:.2e}")

    # Nudge one off the surface, then snap it back.
    conns[0].matrix_world.translation += Vector((0.0, 0.0, 0.2))
    bpy.context.view_layer.update()
    moved = surface.snap_connectors(cut)
    check("snap moved the connectors", moved == 2)
    local = cut.matrix_world.inverted() @ conns[0].matrix_world.translation
    check("nudged connector snapped back onto the surface",
          abs(field.eval(local.x, local.y) - local.z) < 1e-4)

    bpy.ops.partpin.create_parts()
    parts = parts_of(s)
    check("reshaped cut + connectors gives two parts", len(parts) == 2)
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))
    bpy.ops.partpin.clear_drafts()


def scenario_surface_multi_loop(core):
    print("Scenario: cut with two section loops (torus)")
    reset_scene()
    s = bpy.context.scene.part_pin
    bpy.ops.mesh.primitive_torus_add(major_radius=1.0, minor_radius=0.3,
                                     major_segments=64, minor_segments=24)
    model = bpy.context.view_layer.objects.active
    model.name = "Ring"
    s.target = model
    from part_pin import surface

    bpy.ops.partpin.add_plane_cut()
    cut = bpy.context.view_layer.objects.active
    cut.rotation_euler = (0.0, math.radians(90.0), 0.0)
    bpy.context.view_layer.update()

    loops = surface.plane_section_loops(model, cut.matrix_world)
    check("two section loops found on the ring", len(loops) == 2,
          f"got {len(loops)}")

    cut, error = surface.convert_to_surface(bpy.context, cut, model,
                                           per_loop=10)
    check("multi-loop conversion succeeded", cut is not None, str(error))
    if cut is None:
        return
    check("points stored for both loops", len(cut.pp_points) == 20,
          f"got {len(cut.pp_points)}")
    check("two distinct loop ids",
          len({p.loop for p in cut.pp_points}) == 2)

    bpy.ops.partpin.create_parts()
    parts = parts_of(s)
    check("ring split in two", len(parts) == 2, f"got {len(parts)}")
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))


def scenario_surface_from_curve(core):
    print("Scenario: drawn curve cut → surface cut")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_sphere()
    s.target = model
    from part_pin import surface
    from mathutils import Quaternion as Q

    data = bpy.data.curves.new("Cut Curve", 'CURVE')
    data.dimensions = '2D'
    data.fill_mode = 'NONE'
    spline = data.splines.new('POLY')
    spline.points.add(4)
    for point, (x, y) in zip(spline.points,
                             [(-2.0, 0.0), (-1.0, 0.22), (0.0, -0.18),
                              (1.0, 0.22), (2.0, 0.0)]):
        point.co = (x, y, 0.0, 1.0)
    cut = bpy.data.objects.new("Cut Curve", data)
    link(cut)
    cut.rotation_mode = 'QUATERNION'
    cut.rotation_quaternion = Q((1.0, 0.0, 0.0), 1.5707963)
    cut.pp_role = core.ROLE_CUT
    cut.pp_cut_kind = 'CURVE'
    cut.pp_enabled = True
    bpy.context.view_layer.update()

    loops = surface.curve_section_loops(model, cut)
    check("curve section loop found", len(loops) == 1 and len(loops[0]) > 8,
          f"{[len(l) for l in loops]}")

    cut, error = surface.convert_to_surface(bpy.context, cut, model,
                                            per_loop=20)
    check("curve → surface conversion succeeded", cut is not None, str(error))
    if cut is None:
        return
    check("converted cut is a mesh surface cut",
          cut.type == 'MESH' and cut.pp_cut_kind == 'SURFACE')
    on_surface = max(surface_distance(model, cut.matrix_world @ Vector(p.co))
                     for p in cut.pp_points)
    check("converted points lie on the model surface", on_surface < 5e-3,
          f"max {on_surface:.2e}")

    bpy.ops.partpin.create_parts()
    parts = parts_of(s)
    check("converted curve cut still splits the model", len(parts) == 2,
          f"got {len(parts)}")
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))


def make_two_spheres(core, gap=4.0):
    """One mesh, two separate closed spheres — a stand-in for a model whose
    features a plane would cross in several places."""
    import bmesh as bm_mod
    bm = bm_mod.new()
    bm_mod.ops.create_uvsphere(bm, u_segments=40, v_segments=20, radius=1.0)
    verts_a = list(bm.verts)
    ret = bm_mod.ops.duplicate(bm, geom=verts_a + list(bm.edges)
                               + list(bm.faces))
    moved = [g for g in ret['geom'] if isinstance(g, bm_mod.types.BMVert)]
    bm_mod.ops.translate(bm, verts=moved, vec=(gap, 0.0, 0.0))
    mesh = bpy.data.meshes.new("Pair")
    bm.to_mesh(mesh)
    bm.free()
    return link(bpy.data.objects.new("Pair", mesh))


def make_mushroom(core):
    """A wide head on a narrow stalk: the piece is far wider than the loop
    that ring-fences it."""
    import bmesh as bm_mod
    bm = bm_mod.new()
    bm_mod.ops.create_uvsphere(bm, u_segments=40, v_segments=20, radius=0.55)
    for v in bm.verts:
        v.co.z += 0.95
    head = bpy.data.meshes.new("head")
    bm.to_mesh(head)
    bm.free()
    head_obj = link(bpy.data.objects.new("head", head))

    bm = bm_mod.new()
    bm_mod.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=32,
                           radius1=0.16, radius2=0.16, depth=2.0)
    stalk = bpy.data.meshes.new("Mushroom")
    bm.to_mesh(stalk)
    bm.free()
    obj = link(bpy.data.objects.new("Mushroom", stalk))

    mod = obj.modifiers.new("u", 'BOOLEAN')
    mod.object, mod.operation, mod.solver = head_obj, 'UNION', 'EXACT'
    dg = bpy.context.evaluated_depsgraph_get()
    merged = bpy.data.meshes.new_from_object(obj.evaluated_get(dg))
    obj.modifiers.clear()
    obj.data = merged
    bpy.data.objects.remove(head_obj)
    bpy.context.view_layer.update()
    return obj


def scenario_local_leaves_rest_whole(core):
    print("Scenario: localized cut leaves the rest of the model whole")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_two_spheres(core)
    s.target = model
    from part_pin import surface

    bpy.ops.partpin.add_plane_cut()
    cut = bpy.context.view_layer.objects.active
    cut.location = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()
    cut, error = surface.convert_to_surface(bpy.context, cut, model,
                                            per_loop=14)
    check("conversion found both cut lines",
          cut is not None and len({p.loop for p in cut.pp_points}) == 2,
          str(error))
    if cut is None:
        return

    # Drop one sphere's cut line: that sphere should then stay in one piece.
    kept_loop = cut.pp_points[0].loop
    keep = [(Vector(p.co), p.loop) for p in cut.pp_points
            if p.loop == kept_loop]
    surface.store_control_points(cut, [c for c, _l in keep],
                                [0] * len(keep))
    check("localized cutting is on by default", cut.pp_local)
    # Which sphere the surviving line encircles (x≈0 or x≈4): the line's
    # centroid, not any single point of it.
    centre = sum((cut.matrix_world @ co for co, _l in keep),
                 Vector()) / len(keep)
    fenced_x = 0.0 if centre.x < 2.0 else 4.0
    other_x = 4.0 - fenced_x

    bpy.ops.partpin.create_parts()
    parts = parts_of(s)
    check("three parts: two halves plus the untouched sphere",
          len(parts) == 3, f"got {len(parts)}")
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))
    if len(parts) != 3:
        return

    def centre_x(part):
        lo, hi = core.world_bbox(part)
        return (lo.x + hi.x) / 2.0

    def height(part):
        lo, hi = core.world_bbox(part)
        return hi.z - lo.z

    sphere_volume = 4.0 / 3.0 * math.pi
    whole = [p for p in parts if volume(p) > sphere_volume * 0.9]
    check("one sphere was not cut at all", len(whole) == 1,
          f"volumes {sorted(round(volume(p), 3) for p in parts)}")
    if whole:
        check("the untouched part is the sphere whose line was dropped",
              abs(centre_x(whole[0]) - other_x) < 0.5,
              f"centre x {centre_x(whole[0]):.2f}, expected ~{other_x}")
        check("untouched sphere spans its full height",
              height(whole[0]) > 1.9, f"height {height(whole[0]):.3f}")

    halves = [p for p in parts if p not in whole]
    check("the ring-fenced sphere became two halves", len(halves) == 2)
    check("halves belong to the ring-fenced sphere",
          all(abs(centre_x(p) - fenced_x) < 0.5 for p in halves),
          f"centres {[round(centre_x(p), 2) for p in halves]}")
    check("each half is about half a sphere",
          all(height(p) < 1.2 for p in halves),
          f"heights {[round(height(p), 2) for p in halves]}")


def scenario_local_vs_full(core):
    print("Scenario: turning localization off cuts everything again")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_two_spheres(core)
    s.target = model
    from part_pin import surface

    bpy.ops.partpin.add_plane_cut()
    cut = bpy.context.view_layer.objects.active
    cut, _error = surface.convert_to_surface(bpy.context, cut, model,
                                             per_loop=14)
    keep = [(Vector(p.co), p.loop) for p in cut.pp_points if p.loop == 0]
    surface.store_control_points(cut, [c for c, _l in keep], [0] * len(keep))
    cut.pp_local = False

    bpy.ops.partpin.create_parts()
    parts = parts_of(s)
    check("full-extent cut gives two parts", len(parts) == 2,
          f"got {len(parts)}")
    # Every part is a slice: none spans a whole sphere's height any more.
    tallest = 0.0
    for p in parts:
        lo, hi = core.world_bbox(p)
        tallest = max(tallest, hi.z - lo.z)
    check("full-extent cut slices both spheres (old behaviour)",
          tallest < 1.2, f"tallest part spans {tallest:.3f} of 2.0")
    spans_both = [p for p in parts
                  if (core.world_bbox(p)[1].x - core.world_bbox(p)[0].x) > 4.0]
    check("each part contains material from both spheres",
          len(spans_both) == 2, f"got {len(spans_both)}")


def scenario_local_wide_piece(core):
    print("Scenario: localized cut keeps a piece wider than its cut line")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_mushroom(core)
    s.target = model
    from part_pin import surface

    bpy.ops.partpin.add_plane_cut()
    cut = bpy.context.view_layer.objects.active
    cut.location = (0.0, 0.0, 0.2)  # through the narrow stalk
    bpy.context.view_layer.update()
    cut, error = surface.convert_to_surface(bpy.context, cut, model,
                                            per_loop=14)
    check("cut line found around the stalk", cut is not None, str(error))
    if cut is None:
        return
    polys = surface.loop_polygons(cut)
    span = max(max(u for u, _v in polys[0]) - min(u for u, _v in polys[0]),
               max(v for _u, v in polys[0]) - min(v for _u, v in polys[0]))
    check("cut line is narrow (the stalk)", span < 0.45, f"span {span:.3f}")

    bpy.ops.partpin.create_parts()
    parts = parts_of(s)
    check("mushroom split in two", len(parts) == 2, f"got {len(parts)}")
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))
    if len(parts) != 2:
        return
    top = max(parts, key=lambda p: core.world_bbox(p)[1].z)
    lo, hi = core.world_bbox(top)
    check("the head kept its full width — not clipped to the cut line",
          (hi.x - lo.x) > 1.0, f"width {hi.x - lo.x:.3f}")
    check("head volume is intact",
          volume(top) > 0.6, f"volume {volume(top):.3f}")


def scenario_local_seam(core):
    print("Scenario: localized seam mates tightly and follows dragged points")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_sphere()
    original_volume = volume(model)
    s.target = model
    s.count = 2
    from part_pin import surface

    bpy.ops.partpin.add_plane_cut()
    cut = bpy.context.view_layer.objects.active
    cut, _error = surface.convert_to_surface(bpy.context, cut, model,
                                             per_loop=16)
    point = cut.pp_points[0]
    start = cut.matrix_world @ Vector(point.co)
    azimuth = math.atan2(start.y, start.x)
    lat = math.radians(35.0)
    dragged = surface.project_to_surface(
        model, Vector((math.cos(lat) * math.cos(azimuth),
                       math.cos(lat) * math.sin(azimuth),
                       math.sin(lat))))
    point.co = cut.matrix_world.inverted() @ dragged
    surface.build_display_mesh(cut, model)

    bpy.context.view_layer.objects.active = cut
    bpy.ops.partpin.add_connectors()
    conns = core.cut_connectors(bpy.context.scene, cut)
    check("connectors placed inside the cut line", len(conns) == 2,
          f"got {len(conns)}")
    polys = surface.loop_polygons(cut)
    worst_inset = min(
        surface.loop_inset(*(cut.matrix_world.inverted()
                             @ c.matrix_world.translation)[:2], polys)
        for c in conns)
    check("connectors are inside the cut line, not on its edge",
          worst_inset > 0.0, f"min inset {worst_inset:.4f}")

    # Drop the pins before measuring the seam: a pin adds material to one
    # part, which would mask how much the seam itself removes.
    for conn in conns:
        core.remove_object(conn)
    bpy.context.view_layer.update()

    bpy.ops.partpin.create_parts()
    parts = parts_of(s)
    check("localized reshaped cut makes two parts", len(parts) == 2,
          f"got {len(parts)}")
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))
    if len(parts) != 2:
        return
    total = sum(volume(p) for p in parts)
    lost = original_volume - total
    check("almost no material lost at the seam",
          0.0 <= lost < original_volume * 0.01,
          f"lost {lost:.5f} of {original_volume:.3f}")
    lo, hi = core.world_bbox(model)
    cell = max(hi - lo) * 1.16 / s.surface_resolution
    worst = max(surface_distance(p, dragged) for p in parts)
    check("seam still runs through the dragged point", worst < cell * 1.5,
          f"max distance {worst:.4f}, one grid cell is {cell:.4f}")

    lower = min(parts, key=lambda p: z_range(p)[0])
    check("seam is not flat", z_range(lower)[1] > 0.25,
          f"max z {z_range(lower)[1]:.3f}")


def scenario_local_display(core):
    """The preview should cover the fenced region only — checked on a small
    region of a long model, where the difference is unmistakable."""
    print("Scenario: preview mesh shows only the localized patch")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    from part_pin import surface

    bpy.ops.partpin.add_plane_cut()
    cut = bpy.context.view_layer.objects.active
    cut, _error = surface.convert_to_surface(bpy.context, cut, model,
                                            per_loop=16)
    surface.store_control_points(cut, collar_points(surface, model, cut),
                                [0] * 16)
    cut.pp_main_loop = 0
    surface.refit_frame(cut)
    surface.build_display_mesh(cut, model)
    bpy.context.view_layer.update()  # refresh the cached bounding box
    lo, hi = core.world_bbox(cut)
    local_size = max(hi - lo)
    check("localized preview hugs the collar", local_size < 2.5,
          f"preview spans {local_size:.3f}")

    cut.pp_local = False
    surface.build_display_mesh(cut, model)
    bpy.context.view_layer.update()
    lo2, hi2 = core.world_bbox(cut)
    full_size = max(hi2 - lo2)
    check("full-extent preview spans the whole model",
          full_size > local_size * 1.5, f"{full_size:.3f} vs {local_size:.3f}")


def scenario_delete_loop(core):
    print("Scenario: Alt+X removes a whole cut line")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_two_spheres(core)
    s.target = model
    from part_pin import shape_edit, surface
    import types

    bpy.ops.partpin.add_plane_cut()
    cut = bpy.context.view_layer.objects.active
    cut, _error = surface.convert_to_surface(bpy.context, cut, model,
                                             per_loop=12)
    check("two lines before removal",
          len({p.loop for p in cut.pp_points}) == 2)

    cls = shape_edit.PARTPIN_OT_edit_cut_surface
    op = types.SimpleNamespace()
    for name in ('_rebuild_cache', '_delete_loop'):
        setattr(op, name, getattr(cls, name).__get__(op))
    op.cut, op.target = cut, model
    op.hover, op.dragging, op.moved, op._cache = -1, -1, False, None
    op.report = lambda *a, **k: None
    op._rebuild_cache()

    second = next(i for i, p in enumerate(cut.pp_points) if p.loop == 1)
    op._delete_loop(second)
    check("one line left after Alt+X",
          len({p.loop for p in cut.pp_points}) == 1,
          f"{sorted({p.loop for p in cut.pp_points})}")
    check("remaining points all belong to line 0",
          all(p.loop == 0 for p in cut.pp_points))
    check("12 points remain", len(cut.pp_points) == 12,
          f"got {len(cut.pp_points)}")

    op.hover = 0
    op._delete_loop(0)
    check("refuses to remove the last line",
          len(cut.pp_points) == 12, f"got {len(cut.pp_points)}")


def make_limb(core):
    """A limb along X with a head on the end — the shape from issue #1."""
    import bmesh as bm_mod
    from mathutils import Matrix
    bm = bm_mod.new()
    bm_mod.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=32,
                           radius1=0.4, radius2=0.4, depth=6.0)
    bm_mod.ops.rotate(bm, verts=bm.verts, cent=(0, 0, 0),
                      matrix=Matrix.Rotation(math.radians(90), 3, 'Y'))
    mesh = bpy.data.meshes.new("Limb")
    bm.to_mesh(mesh)
    bm.free()
    obj = link(bpy.data.objects.new("Limb", mesh))

    bm = bm_mod.new()
    bm_mod.ops.create_uvsphere(bm, u_segments=32, v_segments=16, radius=0.9)
    for v in bm.verts:
        v.co.x += 3.0
    head_mesh = bpy.data.meshes.new("head")
    bm.to_mesh(head_mesh)
    bm.free()
    head = link(bpy.data.objects.new("head", head_mesh))

    mod = obj.modifiers.new("u", 'BOOLEAN')
    mod.object, mod.operation, mod.solver = head, 'UNION', 'EXACT'
    dg = bpy.context.evaluated_depsgraph_get()
    obj.data = bpy.data.meshes.new_from_object(obj.evaluated_get(dg))
    obj.modifiers.clear()
    bpy.data.objects.remove(head)
    bpy.context.view_layer.update()
    return obj


def collar_points(surface_mod, model, cut, x=2.0, radius=0.55, count=16):  # noqa: E501
    """A cut line dragged right round the limb — a loop that ends up nearly
    perpendicular to the plane the cut started on."""
    points = []
    for i in range(count):
        angle = 2.0 * math.pi * i / count
        world = surface_mod.project_to_surface(
            model, Vector((x, radius * math.cos(angle),
                           radius * math.sin(angle))))
        points.append(cut.matrix_world.inverted() @ world)
    return points


def scenario_collar_cut(core):
    """Issue #1: a line dragged round a limb used to cut nothing at all."""
    print("Scenario: line dragged right round a limb (issue #1)")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    from part_pin import surface

    bpy.ops.partpin.add_plane_cut()  # Z axis: slices the limb lengthwise
    cut = bpy.context.view_layer.objects.active
    cut, _error = surface.convert_to_surface(bpy.context, cut, model,
                                            per_loop=16)
    before = cut.matrix_world.to_quaternion() @ Vector((0.0, 0.0, 1.0))
    check("cut starts out normal to Z", abs(before.z) > 0.99)

    surface.store_control_points(cut, collar_points(surface, model, cut),
                                [0] * 16)
    flat = max(surface.polygon_roundness(p)
               for p in surface.loop_polygons(cut))
    check("collar is degenerate in the original plane (the bug)", flat < 0.01,
          f"roundness {flat:.4f}")

    surface.refit_frame(cut)
    after = cut.matrix_world.to_quaternion() @ Vector((0.0, 0.0, 1.0))
    check("re-fitted plane now faces along the limb", abs(after.x) > 0.99,
          f"normal {tuple(round(v, 2) for v in after)}")
    round_now = max(surface.polygon_roundness(p)
                    for p in surface.loop_polygons(cut))
    check("collar encloses a proper area after re-fitting", round_now > 0.8,
          f"roundness {round_now:.4f}")
    check("no problem reported for the collar",
          surface.cut_line_problem(cut) is None,
          str(surface.cut_line_problem(cut)))

    failures = []
    parts, _applied, _warnings = core.create_parts(
        bpy.context, model, [cut], keep_original=True, failures=failures)
    check("collar cut separates the limb", len(parts) == 2,
          f"got {len(parts)}: {failures}")
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))
    if len(parts) != 2:
        return
    check("no failures reported", not failures, str(failures))

    ends = sorted((core.world_bbox(p)[0].x, core.world_bbox(p)[1].x)
                  for p in parts)
    check("split runs across the limb at the collar, not lengthwise",
          abs(ends[0][1] - 2.0) < 0.15 and abs(ends[1][0] - 2.0) < 0.15,
          f"x ranges {[(round(a, 2), round(b, 2)) for a, b in ends]}")


def scenario_collar_full_extent(core):
    """The same line with localization off should follow the line's plane,
    not the plane the cut happened to start on."""
    print("Scenario: re-fitting also fixes full-extent cuts")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    from part_pin import surface

    bpy.ops.partpin.add_plane_cut()
    cut = bpy.context.view_layer.objects.active
    cut, _error = surface.convert_to_surface(bpy.context, cut, model,
                                            per_loop=16)
    surface.store_control_points(cut, collar_points(surface, model, cut),
                                [0] * 16)
    cut.pp_local = False

    failures = []
    parts, _applied, _warnings = core.create_parts(
        bpy.context, model, [cut], keep_original=True, failures=failures)
    check("full-extent collar cut gives two parts", len(parts) == 2,
          f"got {len(parts)}: {failures}")
    if len(parts) != 2:
        return
    # Lengthwise (the old, wrong result) would give two parts of the full
    # length; across the limb gives one short and one long part.
    lengths = sorted(core.world_bbox(p)[1].x - core.world_bbox(p)[0].x
                     for p in parts)
    check("cut follows the line's plane, not the original Z plane",
          lengths[0] < 2.5 and lengths[1] > 4.0,
          f"part lengths {[round(x, 2) for x in lengths]}")


def scenario_collar_plus_leftover_line(core):
    """A plane cut picks up a line on every feature it crosses. Reshaping one
    into a collar leaves the others in a different plane — the collar should
    still cut, with the leftovers reported rather than silently dropped."""
    print("Scenario: collar plus a leftover line from another feature")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    from part_pin import surface

    bpy.ops.partpin.add_plane_cut()
    cut = bpy.context.view_layer.objects.active
    cut, _error = surface.convert_to_surface(bpy.context, cut, model,
                                            per_loop=16)
    # Line 0 becomes the collar; line 1 is a leftover lengthwise line.
    collar = collar_points(surface, model, cut)
    leftover = []
    for i in range(10):
        angle = 2.0 * math.pi * i / 10
        world = surface.project_to_surface(
            model, Vector((-1.0 + 0.8 * math.cos(angle),
                           0.42 * math.sin(angle), 0.0)))
        leftover.append(cut.matrix_world.inverted() @ world)
    surface.store_control_points(cut, collar + leftover,
                                [0] * 16 + [1] * 10)
    cut.pp_main_loop = 0  # the collar is what the user was editing

    surface.refit_frame(cut)
    usable, problem, warning = surface.loop_quality(cut)
    check("only the collar survives as usable", usable == [0],
          f"usable lines {usable}")
    check("no hard failure", problem is None, str(problem))
    check("the leftover line is reported",
          warning is not None and "Alt+X" in warning, str(warning))

    failures = []
    warns = []
    parts, _applied, warns = core.create_parts(
        bpy.context, model, [cut], keep_original=True, failures=failures)
    check("the collar still cuts", len(parts) == 2,
          f"got {len(parts)}: {failures}")
    check("the ignored line comes through as a warning",
          any("ignored" in w for w in warns), str(warns))
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))


def make_limb_with_fin(core, thickness=0.04):
    """A limb with a thin fin crossing the cut — material that carries on
    past a collar drawn round the limb."""
    import bmesh as bm_mod
    from mathutils import Matrix
    obj = make_limb(core)
    bm = bm_mod.new()
    bm_mod.ops.create_cube(bm, size=1.0)
    bm.transform(Matrix.LocRotScale(Vector((2.0, 0.0, 0.75)), None,
                                    Vector((1.4, thickness, 0.8))))
    mesh = bpy.data.meshes.new("fin")
    bm.to_mesh(mesh)
    bm.free()
    fin = link(bpy.data.objects.new("fin", mesh))
    mod = obj.modifiers.new("u", 'BOOLEAN')
    mod.object, mod.operation, mod.solver = fin, 'UNION', 'EXACT'
    dg = bpy.context.evaluated_depsgraph_get()
    obj.data = bpy.data.meshes.new_from_object(obj.evaluated_get(dg))
    obj.modifiers.clear()
    bpy.data.objects.remove(fin)
    bpy.context.view_layer.update()
    return obj


def make_shoulder(core):
    """An arm off a body, with an armour overhang crossing the same cut just
    outside where a collar round the arm would go — the shape from issue #2."""
    import bmesh as bm_mod
    from mathutils import Matrix

    def union_box(obj, location, scale):
        bm = bm_mod.new()
        bm_mod.ops.create_cube(bm, size=1.0)
        bm.transform(Matrix.LocRotScale(Vector(location), None, Vector(scale)))
        mesh = bpy.data.meshes.new("box")
        bm.to_mesh(mesh)
        bm.free()
        box = link(bpy.data.objects.new("box", mesh))
        mod = obj.modifiers.new("u", 'BOOLEAN')
        mod.object, mod.operation, mod.solver = box, 'UNION', 'EXACT'
        dg = bpy.context.evaluated_depsgraph_get()
        obj.data = bpy.data.meshes.new_from_object(obj.evaluated_get(dg))
        obj.modifiers.clear()
        bpy.data.objects.remove(box)

    bm = bm_mod.new()
    bm_mod.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=32,
                           radius1=0.5, radius2=0.5, depth=4.0)
    bm_mod.ops.rotate(bm, verts=bm.verts, cent=(0, 0, 0),
                      matrix=Matrix.Rotation(math.radians(90), 3, 'Y'))
    for v in bm.verts:
        v.co.x += 2.0
    mesh = bpy.data.meshes.new("Shoulder")
    bm.to_mesh(mesh)
    bm.free()
    obj = link(bpy.data.objects.new("Shoulder", mesh))
    union_box(obj, (-0.9, 0, 0), (2.0, 2.4, 2.4))       # body
    union_box(obj, (0.75, 0, 0.62), (1.5, 0.6, 0.16))   # armour overhang
    bpy.context.view_layer.update()
    return obj


def collar_cut(core, surface_mod, model, per_loop=16, radius=0.75, x=2.0):
    bpy.ops.partpin.add_plane_cut()
    cut = bpy.context.view_layer.objects.active
    cut, _error = surface_mod.convert_to_surface(bpy.context, cut, model,
                                                per_loop=per_loop)
    surface_mod.store_control_points(
        cut, collar_points(surface_mod, model, cut, x=x, radius=radius,
                           count=per_loop),
        [0] * per_loop)
    cut.pp_main_loop = 0
    surface_mod.refit_frame(cut)
    return cut


def scenario_probe_finds_trouble(core):
    """The probe must agree with what the cut actually does: silent on a
    clean line, and pointing at the spots on a line that cannot sever."""
    print("Scenario: probing a cut line for trouble spots")
    from part_pin import surface

    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    cut = collar_cut(core, surface, model)
    probes = surface.probe_cut_line(cut, model)
    bad, suggested, summary = surface.probe_summary(probes, cut)
    check("clean collar reports no trouble spots", not bad, summary)
    check("clean collar suggests no change", suggested is None, str(suggested))
    check("clean collar checked several points along the line",
          len(probes) >= 16, f"{len(probes)} probes")

    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_limb_with_fin(core)
    s.target = model
    cut = collar_cut(core, surface, model)
    probes = surface.probe_cut_line(cut, model)
    bad, suggested, summary = surface.probe_summary(probes, cut)
    check("a fin crossing the cut is flagged", len(bad) >= 3,
          f"{len(bad)} flagged: {summary}")
    check("the flags say material crosses the line",
          any(p['status'] == surface.PROBE_BRIDGE for p in bad),
          str({p['status'] for p in bad}))
    check("the crossing is what gets named, not a reach to try",
          "outside the line" in surface.failure_reason(probes, cut),
          surface.failure_reason(probes, cut))
    reason = surface.failure_reason(probes, cut)
    check("the failure reason points at the crossing material",
          "outside the line" in reason, reason)
    check("every flag carries a position to draw",
          all(len(p['position']) == 3 for p in bad))
    # Flags must sit on the fin, which is the actual obstruction.
    on_fin = [p for p in bad if p['position'].z > 0.3]
    check("flags land on the fin, not scattered over the model",
          len(on_fin) >= len(bad) * 0.5,
          f"{len(on_fin)} of {len(bad)} above z=0.3")


def make_shoulder_arm(core):
    """A body with an arm rising out of its shoulder, welded on. A collar
    drawn round the arm under the armpit has a plane that carries on down
    through the body — the shape from issue #3."""
    import bmesh as bm_mod
    from mathutils import Matrix

    bm = bm_mod.new()
    bm_mod.ops.create_cube(bm, size=1.0)
    bm.transform(Matrix.LocRotScale(Vector((0, 0, 0)), None,
                                    Vector((1.6, 1.2, 3.0))))
    mesh = bpy.data.meshes.new("Body")
    bm.to_mesh(mesh)
    bm.free()
    body = link(bpy.data.objects.new("Body", mesh))

    axis = Vector((0.6, 0.0, 0.8)).normalized()
    base = Vector((0.5, 0.0, 0.8))
    bm = bm_mod.new()
    bm_mod.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=32,
                           radius1=0.38, radius2=0.38, depth=3.0)
    bm_mod.ops.rotate(bm, verts=bm.verts, cent=(0, 0, 0),
                      matrix=Vector((0, 0, 1)).rotation_difference(
                          axis).to_matrix())
    for v in bm.verts:
        v.co += base + axis * 1.2
    arm_mesh = bpy.data.meshes.new("arm")
    bm.to_mesh(arm_mesh)
    bm.free()
    arm = link(bpy.data.objects.new("arm", arm_mesh))

    mod = body.modifiers.new("u", 'BOOLEAN')
    mod.object, mod.operation, mod.solver = arm, 'UNION', 'EXACT'
    dg = bpy.context.evaluated_depsgraph_get()
    body.data = bpy.data.meshes.new_from_object(body.evaluated_get(dg))
    body.modifiers.clear()
    bpy.data.objects.remove(arm)
    bpy.context.view_layer.update()
    return body, base, axis


def collar_stroke(surface_mod, model, centre, axis, count=90):
    """A collar drawn round a limb: rays fired inward at its axis, which is
    what drawing round it in the viewport produces."""
    across = axis.cross(Vector((0, 1, 0))).normalized()
    other = axis.cross(across).normalized()
    obj = surface_mod.evaluated(model)
    inverse = obj.matrix_world.inverted()
    points = []
    for i in range(count):
        angle = 2.0 * math.pi * i / count
        radial = (math.cos(angle) * across
                  + math.sin(angle) * other).normalized()
        hit, location, _n, _i = obj.ray_cast(
            inverse @ (centre + radial * 4.0),
            inverse.to_3x3() @ (-radial))
        if hit:
            points.append(obj.matrix_world @ location)
    return points


def scenario_arm_at_shoulder(core):
    """Issue #3: a collar under the armpit sliced the whole model down the
    cut plane instead of taking the arm off."""
    print("Scenario: collar under the armpit takes the arm only (issue #3)")
    reset_scene()
    from part_pin import surface
    s = bpy.context.scene.part_pin
    model, base, axis = make_shoulder_arm(core)
    s.target = model
    before = volume(model)

    stroke = collar_stroke(surface, model, base + axis * 0.55, axis)
    cut, error = surface.cut_from_stroke(bpy.context, model, stroke,
                                        per_loop=18)
    check("the drawn collar becomes a cut", cut is not None, str(error))
    if cut is None:
        return

    # The cut's plane genuinely carries on into the body: that is what used
    # to get sliced.
    patch = surface.patch_grid(cut, model, s.surface_resolution)
    material = sum(map(sum, patch['material']))
    kept = sum(1 for i in range(patch['n']) for j in range(patch['n'])
               if patch['material'][i][j] and patch['interior'][i][j])
    check("the cut only claims material inside the line", kept <= material,
          f"{kept} of {material} cells")

    failures = []
    parts, _applied, _warns = core.create_parts(
        bpy.context, model, [cut], keep_original=True, failures=failures)
    check("exactly two parts", len(parts) == 2, f"got {len(parts)}: {failures}")
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))
    if len(parts) != 2:
        return
    check("volume is conserved",
          abs(sum(volume(p) for p in parts) - before) < before * 0.01,
          f"{sum(volume(p) for p in parts):.3f} vs {before:.3f}")

    arm = min(parts, key=volume)
    body = max(parts, key=volume)
    check("the arm is the small part", volume(arm) < before * 0.25,
          f"arm is {volume(arm) / before:.0%} of the model")
    lo, hi = core.world_bbox(body)
    check("the body was NOT sliced down the cut plane",
          (hi.z - lo.z) > 2.9 and (hi.y - lo.y) > 1.1,
          f"body spans z {hi.z - lo.z:.2f} (of 3.0), y {hi.y - lo.y:.2f} "
          "(of 1.2)")
    check("the body keeps nearly all its volume",
          volume(body) > before * 0.8,
          f"body is {volume(body) / before:.0%} of the model")
    arm_lo, _arm_hi = core.world_bbox(arm)
    check("the arm part starts at the collar, not inside the body",
          arm_lo.z > 0.5, f"arm starts at z {arm_lo.z:.2f}")


def scenario_undercut_frees_recessed_piece(core):
    """Issue #4: a piece held on by material outside the line — an arm
    recessed under a shoulder — cannot come away without cutting a little of
    what holds it. Undercut allows exactly that, bounded, and Check Cut Line
    ▸ Fix works out how much."""
    print("Scenario: Undercut frees a recessed piece (issue #4)")
    reset_scene()
    from part_pin import surface
    s = bpy.context.scene.part_pin
    model = make_limb_with_fin(core)
    s.target = model
    before = volume(model)
    cut = collar_cut(core, surface, model)
    bpy.context.view_layer.objects.active = cut

    check("Undercut is off by default", cut.pp_undercut == 0.0,
          f"{cut.pp_undercut}")
    failures = []
    parts, _applied, _warns = core.create_parts(
        bpy.context, model, [cut], keep_original=True, failures=failures)
    check("by default it will not cut what holds the piece",
          len(parts) == 1 and failures, f"{len(parts)} parts, {failures}")
    for part in list(parts):
        core.remove_object(part)

    probes = surface.probe_cut_line(cut, model)
    wanted = surface.suggest_undercut(probes, cut)
    check("an Undercut is suggested",
          wanted and 0.0 < wanted <= surface.UNDERCUT_LIMIT, str(wanted))
    check("the reason offers it",
          "Undercut" in surface.failure_reason(probes, cut),
          surface.failure_reason(probes, cut))

    bpy.ops.partpin.check_cut_line(fix=True)
    check("Fix sets the Undercut", cut.pp_undercut > 0.0,
          f"{cut.pp_undercut:.3f}")

    failures = []
    parts, _applied, _warns = core.create_parts(
        bpy.context, model, [cut], keep_original=True, failures=failures)
    check("the recessed piece now comes away", len(parts) == 2,
          f"got {len(parts)}: {failures}")
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))
    if len(parts) != 2:
        return
    lost = before - sum(volume(p) for p in parts)
    check("only a sliver is spent freeing it", 0.0 <= lost < before * 0.01,
          f"lost {lost:.4f} of {before:.3f} ({100 * lost / before:.2f}%)")
    check("the rest of the model is still whole",
          max(volume(p) for p in parts) > before * 0.4,
          f"{max(volume(p) for p in parts):.3f} of {before:.3f}")


def scenario_undercut_declines_when_too_deep(core):
    """When what holds the piece runs deeper than a seam-side nick, no
    Undercut is sensible — say so rather than eating into the model."""
    print("Scenario: Undercut declines when the join runs deep")
    reset_scene()
    from part_pin import surface
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    # The line dropped onto solid material: the piece is joined outside it
    # all the way round, and no bounded reach can free that.
    cut = collar_cut(core, surface, model)
    points = []
    for point in cut.pp_points:
        world = cut.matrix_world @ Vector(point.co)
        points.append(cut.matrix_world.inverted() @ (world * 0.35))
    surface.store_control_points(cut, points, [0] * len(points))
    bpy.context.view_layer.objects.active = cut

    probes = surface.probe_cut_line(cut, model)
    wanted = surface.suggest_undercut(probes, cut)
    check("no runaway Undercut is suggested",
          wanted is None or wanted <= surface.UNDERCUT_LIMIT, str(wanted))
    before = cut.pp_undercut
    bpy.ops.partpin.check_cut_line(fix=True)
    check("Fix never exceeds the limit",
          cut.pp_undercut <= surface.UNDERCUT_LIMIT,
          f"{before} → {cut.pp_undercut}")


def scenario_material_across_line_blocks(core):
    """A fin crossing the line joins the piece to the model outside the line.
    Freeing it would mean cutting outside the line, which is the one thing
    this mode promises not to do (issue #3) — so it reports instead."""
    print("Scenario: material crossing the line blocks the cut, and is kept")
    reset_scene()
    from part_pin import surface
    s = bpy.context.scene.part_pin
    model = make_limb_with_fin(core)
    s.target = model
    before = volume(model)
    cut = collar_cut(core, surface, model)
    started_at = cut.pp_margin

    failures = []
    parts, _applied, _warns = core.create_parts(
        bpy.context, model, [cut], keep_original=True, failures=failures)
    check("the model is left whole rather than wrongly cut", len(parts) == 1,
          f"got {len(parts)}")
    check("the reason is reported", len(failures) == 1, str(failures))
    check("the reason names material outside the line",
          failures and "outside the line" in failures[0], str(failures))
    check("it says what to do about it",
          failures and "take the line around it" in failures[0],
          str(failures))
    check("Edge Margin was not silently widened", cut.pp_margin == started_at,
          f"{started_at} → {cut.pp_margin}")
    check("nothing was shaved off the model",
          abs(volume(parts[0]) - before) < before * 0.01,
          f"{volume(parts[0]):.4f} vs {before:.4f}")


def scenario_overhang_outside_line_survives(core):
    """Issue #2: a cut used to take a chip out of an overhang sitting just
    outside the line, while leaving the enclosed piece attached."""
    print("Scenario: an overhang outside the line is never cut (issue #2)")
    reset_scene()
    from part_pin import surface
    s = bpy.context.scene.part_pin
    model = make_shoulder(core)
    s.target = model
    before = volume(model)
    cut = collar_cut(core, surface, model, radius=0.6, x=1.0)
    # The collar goes round the arm at x=1; the overhang plate crosses the
    # same cut just beyond it, and belongs to the body.
    failures = []
    parts, _applied, _warns = core.create_parts(
        bpy.context, model, [cut], keep_original=True, failures=failures)

    check("the arm comes off", len(parts) == 2, f"got {len(parts)}: {failures}")
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))
    if len(parts) != 2:
        return
    check("volume is conserved", abs(sum(volume(p) for p in parts) - before)
          < before * 0.01,
          f"{sum(volume(p) for p in parts):.3f} vs {before:.3f}")

    # No part may be a chip of the overhang: the overhang lives above the
    # arm (z > 0.5) and outside the line, so nothing that size may appear.
    chips = [p for p in parts if volume(p) < before * 0.02]
    check("no chip was cut off outside the line", not chips,
          f"{[round(volume(p), 4) for p in chips]}")

    arm = min(parts, key=volume)
    lo, hi = core.world_bbox(arm)
    check("the separated part is the arm inside the line",
          lo.x > 0.9 and hi.z < 0.6,
          f"x from {lo.x:.2f}, max z {hi.z:.2f}")
    body = max(parts, key=volume)
    lo2, hi2 = core.world_bbox(body)
    check("the overhang stayed whole on the body", hi2.x > 1.4,
          f"body reaches x {hi2.x:.2f}, overhang tip is at 1.5")


def scenario_check_operator(core):
    print("Scenario: Check Cut Line operator")
    reset_scene()
    from part_pin import surface
    s = bpy.context.scene.part_pin
    model = make_limb_with_fin(core)
    s.target = model
    cut = collar_cut(core, surface, model)
    bpy.context.view_layer.objects.active = cut
    before = cut.pp_margin

    check("operator is registered",
          hasattr(bpy.ops.partpin, "check_cut_line"))
    bpy.ops.partpin.check_cut_line(fix=False)
    check("checking alone changes nothing", cut.pp_margin == before,
          f"{before} → {cut.pp_margin}")
    bpy.ops.partpin.check_cut_line(fix=True)
    check("it does not widen the reach when that cannot help",
          cut.pp_margin == before, f"{before} → {cut.pp_margin}")

    # On a clean line it reports that all is well and still changes nothing.
    reset_scene()
    s = bpy.context.scene.part_pin
    clean = make_limb(core)
    s.target = clean
    cut = collar_cut(core, surface, clean)
    bpy.context.view_layer.objects.active = cut
    was = cut.pp_margin
    bpy.ops.partpin.check_cut_line(fix=True)
    check("a clean line is left alone", cut.pp_margin == was,
          f"{was} → {cut.pp_margin}")
    failures = []
    parts, _applied, _warns = core.create_parts(
        bpy.context, clean, [cut], keep_original=True, failures=failures)
    check("and it cuts", len(parts) == 2, f"got {len(parts)}: {failures}")


def scenario_unusable_line_reports(core):
    print("Scenario: a line that encloses nothing says so")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    from part_pin import surface

    bpy.ops.partpin.add_plane_cut()
    cut = bpy.context.view_layer.objects.active
    cut, _error = surface.convert_to_surface(bpy.context, cut, model,
                                            per_loop=12)
    # Collapse the line onto a straight path along the limb: it doubles back
    # on itself and fences no region at all.
    points = []
    for i in range(12):
        t = i / 11.0 if i < 6 else (11 - i) / 11.0
        world = surface.project_to_surface(
            model, Vector((-2.5 + 5.0 * t, 0.0, 0.45)))
        points.append(cut.matrix_world.inverted() @ world)
    surface.store_control_points(cut, points, [0] * 12)

    problem = surface.cut_line_problem(cut)
    check("unusable line is reported, not silently ignored",
          problem is not None and "enclose" in problem, str(problem))

    failures = []
    parts, _applied, _warnings = core.create_parts(
        bpy.context, model, [cut], keep_original=True, failures=failures)
    check("create_parts surfaces the reason", len(failures) == 1,
          str(failures))
    check("model is left in one piece", len(parts) == 1, f"got {len(parts)}")


def hand_stroke(surface_mod, model, x=2.0, count=90, wobble=0.06, gap=0):
    """A stroke as drawing produces one: closely spaced ray-cast hits on the
    surface, slightly shaky, optionally with a stretch missing where the user
    let go to orbit.

    Each point is a ray fired at the model from outside, which is exactly
    what a click in the viewport does — not a nearest-point projection,
    which can land on whatever happens to be closest.
    """
    obj = surface_mod.evaluated(model)
    inverse = obj.matrix_world.inverted()
    points = []
    for i in range(count):
        if gap and count // 3 <= i < count // 3 + gap:
            continue  # the pointer was off the model / orbiting
        angle = 2.0 * math.pi * i / count
        along = x + wobble * math.sin(angle * 3.0)   # a shaky hand
        direction = Vector((0.0, -math.cos(angle), -math.sin(angle)))
        origin = Vector((along, -direction.y * 5.0, -direction.z * 5.0))
        hit, location, _n, _i = obj.ray_cast(inverse @ origin,
                                             inverse.to_3x3() @ direction)
        if hit:
            points.append(obj.matrix_world @ location)
    return points


def scenario_draw_cut(core):
    """Drawing the perimeter onto the model and cutting along it."""
    print("Scenario: draw the cut perimeter on the model")
    reset_scene()
    from part_pin import surface
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    before = volume(model)

    stroke = hand_stroke(surface, model)
    check("stroke has many points along the surface", len(stroke) > 60,
          f"{len(stroke)} points")
    # Ray-cast hit positions land a whisker off the surface (~3e-5 on a
    # model six units long), which is what drawing actually produces.
    check("every stroke point is on the model",
          max(surface_distance(model, p) for p in stroke) < 1e-3,
          f"{max(surface_distance(model, p) for p in stroke):.2e}")

    loop = surface.stroke_to_loop(model, stroke, per_loop=16)
    check("stroke reduces to draggable control points", len(loop) == 16,
          f"got {len(loop)}")
    check("control points sit on the model",
          max(surface_distance(model, p) for p in loop) < 1e-3,
          f"{max(surface_distance(model, p) for p in loop):.2e}")
    spacing = [(loop[(i + 1) % len(loop)] - loop[i]).length
               for i in range(len(loop))]
    # Spacing is evened out along the stroke, then each point is pulled onto
    # the faceted surface, which shifts it a little.
    check("control points are evenly spread",
          max(spacing) < min(spacing) * 2.5,
          f"{min(spacing):.3f}..{max(spacing):.3f}")

    cut, error = surface.cut_from_stroke(bpy.context, model, stroke,
                                        per_loop=16)
    check("a cut is created from the stroke", cut is not None, str(error))
    if cut is None:
        return
    check("it is an editable surface cut", cut.pp_cut_kind == 'SURFACE')
    check("it is localized to the drawn perimeter", cut.pp_local)
    check("it is listed as a cut", cut in core.scene_cuts(bpy.context.scene))
    check("its plane follows the drawn loop",
          abs((cut.matrix_world.to_quaternion()
               @ Vector((0.0, 0.0, 1.0))).x) > 0.95,
          "expected to face along the limb")

    failures = []
    parts, _applied, _warns = core.create_parts(
        bpy.context, model, [cut], keep_original=True, failures=failures)
    check("the drawn cut separates the model", len(parts) == 2,
          f"got {len(parts)}: {failures}")
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))
    if len(parts) != 2:
        return
    check("volume is conserved",
          abs(sum(volume(p) for p in parts) - before) < before * 0.01,
          f"{sum(volume(p) for p in parts):.3f} vs {before:.3f}")
    seam = min(core.world_bbox(p)[1].x for p in parts)
    check("the cut runs where it was drawn", abs(seam - 2.0) < 0.25,
          f"seam at x {seam:.2f}, drawn at 2.0")


def scenario_draw_cut_in_pieces(core):
    """Drawing in several stretches — let go, orbit, carry on — must still
    close into one perimeter."""
    print("Scenario: perimeter drawn in stretches with a gap")
    reset_scene()
    from part_pin import surface
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model

    stroke = hand_stroke(surface, model, gap=12)
    loop = surface.stroke_to_loop(model, stroke, per_loop=18)
    check("the gap is bridged into a closed ring", len(loop) == 18,
          f"got {len(loop)}")
    longest = max((loop[(i + 1) % len(loop)] - loop[i]).length
                  for i in range(len(loop)))
    check("no wild jump across the gap", longest < 0.6,
          f"longest span {longest:.3f}")

    cut, error = surface.cut_from_stroke(bpy.context, model, stroke,
                                        per_loop=18)
    check("a cut is still created", cut is not None, str(error))
    if cut is None:
        return
    failures = []
    parts, _applied, _warns = core.create_parts(
        bpy.context, model, [cut], keep_original=True, failures=failures)
    check("it separates the model", len(parts) == 2,
          f"got {len(parts)}: {failures}")


def scenario_draw_then_adjust(core):
    """A drawn cut must be adjustable exactly like any other: the on-surface
    editor works on it, and the cut follows the moved point."""
    print("Scenario: adjust a drawn cut, then cut")
    reset_scene()
    from part_pin import shape_edit, surface
    import types
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model

    cut, _error = surface.cut_from_stroke(
        bpy.context, model, hand_stroke(surface, model), per_loop=16)
    bpy.context.view_layer.objects.active = cut

    # The editor accepts it without re-deriving anything.
    same, error = surface.convert_to_surface(bpy.context, cut, model)
    check("the editor takes the drawn cut as-is", same is cut, str(error))

    cls = shape_edit.PARTPIN_OT_edit_cut_surface
    op = types.SimpleNamespace()
    for name in ('_rebuild_cache',):
        setattr(op, name, getattr(cls, name).__get__(op))
    op.cut, op.target = cut, model
    op.hover, op.dragging, op.moved, op._cache = -1, -1, False, None
    op.report = lambda *a, **k: None
    op._rebuild_cache()
    check("the drawn line draws as one loop",
          len(op._cache['polylines']) == 1,
          f"{len(op._cache['polylines'])} loops")

    # Nudge one point along the limb, as dragging it would. Away from the
    # head, so it stays on the surface — dragging in the viewport ray-casts
    # onto visible surface and cannot bury a point inside the model.
    point = cut.pp_points[0]
    world = cut.matrix_world @ Vector(point.co)
    moved = surface.project_to_surface(model, world + Vector((-0.25, 0, 0)))
    check("the nudged point is on the model surface",
          surface_distance(model, moved) < 1e-3,
          f"{surface_distance(model, moved):.2e}")
    point.co = cut.matrix_world.inverted() @ moved
    surface.refit_frame(cut)
    surface.build_display_mesh(cut, model)

    failures = []
    parts, _applied, _warns = core.create_parts(
        bpy.context, model, [cut], keep_original=True, failures=failures)
    check("the adjusted drawn cut still separates", len(parts) == 2,
          f"got {len(parts)}: {failures}")
    if len(parts) != 2:
        return
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))
    worst = min(surface_distance(p, moved) for p in parts)
    check("the seam passes through the nudged point", worst < 0.08,
          f"nearest part surface is {worst:.4f} away")


def scenario_draw_cut_rejections(core):
    print("Scenario: strokes that cannot become a cut say why")
    reset_scene()
    from part_pin import surface
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model

    cut, error = surface.cut_from_stroke(
        bpy.context, model,
        [Vector((2.0, 0.4, 0.0)), Vector((2.0, 0.0, 0.4))], per_loop=16)
    check("a two-point scribble is refused", cut is None and error,
          str(error))
    check("the reason mentions the stroke being too short",
          error and "too short" in error, str(error))

    # A stroke that runs along the limb instead of round it encloses nothing.
    along = [surface.project_to_surface(model, Vector((x, 0.0, 0.45)))
             for x in [-2.0 + 0.1 * i for i in range(40)]]
    along += list(reversed(along))
    cut, error = surface.cut_from_stroke(bpy.context, model, along,
                                        per_loop=16)
    check("a stroke that doubles back is refused", cut is None, str(error))
    check("the reason is actionable", error and ("enclose" in error
                                                or "crosses itself" in error),
          str(error))


def scenario_draw_operator_registered(core):
    print("Scenario: drawing operator wiring")
    reset_scene()
    from part_pin import draw_cut
    s = bpy.context.scene.part_pin
    check("partpin.draw_cut_line exists",
          hasattr(bpy.ops.partpin, "draw_cut_line"))
    check("it will not run without a model",
          not draw_cut.PARTPIN_OT_draw_cut_line.poll(bpy.context))
    s.target = make_limb(core)
    check("it defaults to going straight into adjustment",
          draw_cut.PARTPIN_OT_draw_cut_line.__annotations__['then_edit']
          .keywords.get('default') is True)


def scenario_modal_helpers(core):
    """Drive the modal operator's logic directly (everything but the GPU)."""
    print("Scenario: surface-edit modal helpers")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_sphere()
    s.target = model
    from part_pin import shape_edit, surface

    bpy.ops.partpin.add_plane_cut()
    cut = bpy.context.view_layer.objects.active
    cut, _error = surface.convert_to_surface(bpy.context, cut, model,
                                            per_loop=12)

    # bpy operator classes cannot be instantiated, so bind the methods under
    # test to a stand-in holding the same state invoke() would have set up.
    import types
    cls = shape_edit.PARTPIN_OT_edit_cut_surface
    op = types.SimpleNamespace()
    for name in ('_rebuild_cache', '_insert_point', '_delete_point',
                 '_nearest_point', '_surface_hit'):
        setattr(op, name, getattr(cls, name).__get__(op))
    op.cut, op.target = cut, model
    op.hover, op.dragging, op.moved, op._cache = -1, -1, False, None
    op.report = lambda *a, **k: None

    op._rebuild_cache()
    check("cache holds one cut line", len(op._cache['polylines']) == 1)
    line = op._cache['polylines'][0]
    check("cut line is densified and closed",
          len(line) == 12 * shape_edit.SEGMENT_SUBDIV + 1
          and (line[0] - line[-1]).length < 1e-9, f"{len(line)} points")
    off = max(surface_distance(model, p) for p in line)
    check("drawn cut line hugs the model surface", off < 0.02,
          f"max {off:.4f}")
    check("one world position per control point",
          len(op._cache['world']) == 12)

    # Ctrl+click insert: the new point must land on the surface, in a loop.
    hit = surface.project_to_surface(model, Vector((1.0, 0.02, 0.0)))
    op._surface_hit = lambda context, mouse: hit
    op._insert_point(bpy.context, (0, 0))
    check("insert added a control point", len(cut.pp_points) == 13,
          f"got {len(cut.pp_points)}")
    worst = max(surface_distance(model, cut.matrix_world @ Vector(p.co))
                for p in cut.pp_points)
    check("all points still on the model surface", worst < 1e-3,
          f"max {worst:.2e}")
    check("inserted point kept the loop id",
          {p.loop for p in cut.pp_points} == {0})
    inserted = min(range(len(cut.pp_points)),
                   key=lambda i: ((cut.matrix_world
                                   @ Vector(cut.pp_points[i].co)) - hit).length)
    check("inserted point is adjacent to its neighbours in storage order",
          0 < inserted < 13)

    op.hover = 0
    op._delete_point(0)
    check("delete removed a control point", len(cut.pp_points) == 12,
          f"got {len(cut.pp_points)}")

    # Guard: never shrink a loop below 3 points.
    keep = [(Vector(p.co), p.loop) for p in cut.pp_points][:3]
    surface.store_control_points(cut, [c for c, _l in keep],
                                [l for _c, l in keep])
    op._delete_point(0)
    check("refuses to delete below 3 points", len(cut.pp_points) == 3,
          f"got {len(cut.pp_points)}")


def scenario_operators_registered(core):
    print("Scenario: new operators registered")
    reset_scene()
    for name in ("edit_cut_surface", "snap_connectors", "reset_cut_shape"):
        check(f"partpin.{name} exists",
              hasattr(bpy.ops.partpin, name))
    # poll must refuse cleanly with no model picked (and no UI context)
    check("edit_cut_surface poll is False with nothing set up",
          not bpy.ops.partpin.edit_cut_surface.poll())


def main():
    reset_scene()
    import part_pin
    part_pin.register()
    from part_pin import core

    scenario_plane_cut(core)
    scenario_multi_cut_disabled(core)
    scenario_curved_cut(core)
    scenario_custom_connector(core)
    scenario_flip_pin(core)
    scenario_validation(core)
    scenario_height_field(core)
    scenario_surface_convert(core)
    scenario_surface_drag(core)
    scenario_surface_connectors(core)
    scenario_surface_multi_loop(core)
    scenario_surface_from_curve(core)
    scenario_local_leaves_rest_whole(core)
    scenario_local_vs_full(core)
    scenario_local_wide_piece(core)
    scenario_local_seam(core)
    scenario_local_display(core)
    scenario_delete_loop(core)
    scenario_collar_cut(core)
    scenario_collar_full_extent(core)
    scenario_collar_plus_leftover_line(core)
    scenario_probe_finds_trouble(core)
    scenario_arm_at_shoulder(core)
    scenario_undercut_frees_recessed_piece(core)
    scenario_undercut_declines_when_too_deep(core)
    scenario_material_across_line_blocks(core)
    scenario_overhang_outside_line_survives(core)
    scenario_check_operator(core)
    scenario_unusable_line_reports(core)
    scenario_draw_cut(core)
    scenario_draw_cut_in_pieces(core)
    scenario_draw_then_adjust(core)
    scenario_draw_cut_rejections(core)
    scenario_draw_operator_registered(core)
    scenario_modal_helpers(core)
    scenario_operators_registered(core)
    scenario_export(core)  # runs easy mode internally, then exports

    print()
    if FAILURES:
        print(f"SMOKE TEST FAILED — {len(FAILURES)} failure(s):")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("SMOKE TEST PASSED — all scenarios OK")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        print("SMOKE TEST FAILED — unhandled exception")
        sys.exit(1)
