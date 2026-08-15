"""Headless smoke test for the PartPin add-on.

Run with:

    blender --background --python-exit-code 1 --python tests/smoke_test.py

Covers: the cut line and how it is walked across the model, cutting along
it, straight cuts (draft + easy mode), multiple cuts, disabled cuts, built-in
and custom connectors, pin/socket generation, manifold validation, and
STL/OBJ/FBX export.
"""

import math
import os
import sys
import tempfile
import traceback

import bpy  # noqa: F401 — must precede bmesh for the pip "bpy" module
import bmesh
from mathutils import Vector

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


def scenario_the_walker(core):
    """The one piece of new machinery: the shortest path across the surface.

    Measured against a sphere, where the answer is known exactly — the great
    circle. A path that is on the surface but not straightened comes out
    noticeably longer, and a path that is straightened in space rather than
    across the surface comes out shorter than the great circle and off the
    model, so the length alone separates all three.
    """
    print("Scenario: walking a span across the surface")
    reset_scene()
    from part_pin import walker

    model = make_sphere(radius=1.0)
    bpy.context.view_layer.update()

    for degrees in (22.5, 45.0, 90.0):
        lat = math.radians(20.0)
        a = Vector((math.cos(lat), 0.0, math.sin(lat)))
        b = Vector((math.cos(lat) * math.cos(math.radians(degrees)),
                    math.cos(lat) * math.sin(math.radians(degrees)),
                    math.sin(lat)))
        start, face_a = walker.place(model, a)
        end, face_b = walker.place(model, b)
        walked = walker.between(model, start, end, face_a, face_b)
        check(f"the span at {degrees}° is walked", walked is not None)
        if walked is None:
            continue
        path = [start] + walked + [end]
        length = sum((path[i + 1] - path[i]).length
                     for i in range(len(path) - 1))
        arc = math.acos(max(-1.0, min(1.0, start.normalized()
                                      .dot(end.normalized()))))
        check(f"it is the geodesic at {degrees}°, not a zig-zag",
              abs(length / arc - 1.0) < 0.02,
              f"walked {length:.5f} against a great circle of {arc:.5f}")
        worst = max(surface_distance(model, model.matrix_world @ p)
                    for p in path)
        check(f"and every point of it is on the model at {degrees}°",
              worst < 1e-6, f"furthest {worst:.2e}")
        # The samples are the edge crossings, so they cannot crowd or gap.
        gaps = [(path[i + 1] - path[i]).length for i in range(len(path) - 1)]
        check(f"walked at an even spacing at {degrees}°",
              max(gaps) < 4.0 * (sum(gaps) / len(gaps)),
              f"{min(gaps):.4f}..{max(gaps):.4f}")

    # A hint that has gone stale is checked rather than trusted.
    point = Vector((0.0, 0.0, 1.0))
    landed, face = walker.place(model, point)
    same, again = walker.place(model, landed, hint=face)
    check("a good face hint is taken", again == face and
          (same - landed).length < 1e-9)
    moved, elsewhere = walker.place(model, Vector((1.0, 0.0, 0.0)), hint=face)
    check("a stale one is not", elsewhere != face
          and surface_distance(model, model.matrix_world @ moved) < 1e-6)

    # Two shells that do not join have no path between them, and that is
    # said plainly rather than papered over with a line through space.
    reset_scene()
    pair = make_two_spheres(core, gap=4.0)
    bpy.context.view_layer.update()
    here, face_a = walker.place(pair, Vector((0.0, 0.0, 1.0)))
    there, face_b = walker.place(pair, Vector((4.0, 0.0, 1.0)))
    check("a span between two loose shells fails, rather than cutting "
          "through space",
          walker.between(pair, here, there, face_a, face_b) is None)


def scenario_the_line_holds_its_contour(core):
    """The whole point of the rework: the line is on the model everywhere,
    it does not sag away from the anchors between them, and it never doubles
    back on itself.

    Stored as a height field over a fitted plane, a line passed through its
    anchors exactly and strayed up to 0.894% of the model between them, with
    hundreds of hairpins on a line that had only just been drawn.
    """
    print("Scenario: the line holds the contour it was drawn on")
    from part_pin import mesh_cut, surface

    def measure(label, model, cut, allow_off_anchors):
        rings = surface.line_rings(cut, model)
        check(f"{label}: the line is built", bool(rings))
        if not rings:
            return None
        found = surface.line_quality(cut, model)
        diagonal = found['diagonal']
        check(f"{label}: every sample is on the model",
              found['off_surface'] < 1e-4,
              f"furthest {found['off_surface']:.2e} "
              f"= {found['off_surface'] / diagonal:.5%} of the model")
        check(f"{label}: no span could not be walked", not found['broken'])
        check(f"{label}: it holds its contour between the anchors",
              found['off_anchors'] < diagonal * allow_off_anchors,
              f"{found['off_anchors'] / diagonal:.4%} of the model, "
              f"allowed {allow_off_anchors:.2%}")
        check(f"{label}: no hairpins on a freshly drawn line",
              found['hairpins'] == 0, f"{found['hairpins']} pairs")
        # Every anchor is *on* the line, not near it.
        worst = 0.0
        for ring, anchors in zip(rings, surface.world_anchors(cut, model)):
            for anchor in anchors:
                worst = max(worst, min((anchor - p).length for p in ring))
        check(f"{label}: the line runs through every anchor", worst < 1e-9,
              f"furthest anchor {worst:.2e} from the line")
        return found

    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    measure("collar on a limb", model, collar_cut(core, surface, model), 0.01)

    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_cube_model(core)
    s.target = model
    cut, _error = surface.cut_from_stroke(bpy.context, model,
                                          waist_stroke(surface, model),
                                          per_loop=16)
    if cut is not None:
        measure("waist on a cube", model, cut, 0.01)

    reset_scene()
    s = bpy.context.scene.part_pin
    model, base, axis = make_shoulder_arm(core)
    s.target = model
    stroke = collar_stroke(surface, model, base + axis * 0.55, axis)
    cut, _error = surface.cut_from_stroke(bpy.context, model, stroke,
                                          per_loop=18)
    if cut is not None:
        measure("collar in an armpit", model, cut, 0.01)

    # And it holds it better the more anchors there are. The line only ever
    # leaves the polyline through its anchors by as much as the surface bends
    # between them, so putting them closer together closes the gap — sharply,
    # because that is a chord against an arc. A line sagging towards a fitted
    # plane instead would keep on sagging however many points it was given.
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    bpy.ops.partpin.add_plane_cut()
    cut = bpy.context.view_layer.objects.active
    cut, _error = surface.convert_to_surface(bpy.context, cut, model,
                                             per_loop=16)
    strayed = {}
    for count in (8, 16, 32):
        surface.store_anchors(cut, collar_points(surface, model, count=count),
                              [0] * count)
        strayed[count] = surface.line_quality(cut, model)['off_anchors']
    check("more anchors hold the contour more closely",
          strayed[8] > strayed[16] > strayed[32],
          ", ".join(f"{k}: {v:.5f}" for k, v in sorted(strayed.items())))
    check("and it closes on them as a chord closes on an arc",
          strayed[8] > strayed[32] * 8.0,
          ", ".join(f"{k}: {v:.5f}" for k, v in sorted(strayed.items())))

    # Whatever the cutter is handed keeps the same properties.
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    cut = collar_cut(core, surface, model)
    rings, _normals = mesh_cut.line_rings(cut, model)
    worst = max(surface_distance(model, p) for ring in rings for p in ring)
    check("the ring the cutter is handed is on the model too", worst < 1e-4,
          f"furthest {worst:.2e}")


def scenario_dragging_stays_local(core):
    """Dragging one anchor re-walks the two spans either side of it, and
    nothing else. That is what keeps it usable on a dense sculpt."""
    print("Scenario: dragging is local, and quick on a dense model")
    reset_scene()
    import time
    from part_pin import surface, walker
    s = bpy.context.scene.part_pin

    model = link(bpy.data.objects.new("Dense", make_dense_mesh()))
    s.target = model
    # The size the bar is set at: the user's sculpt is 441,616 faces and
    # 156 across, and dragging a point on it has to stay interactive.
    check("the fixture is as dense as the model this is for",
          len(model.data.polygons) > 400000,
          f"{len(model.data.polygons)} faces")

    walker.forget()
    started = time.time()
    cut, _error = surface.cut_from_stroke(
        bpy.context, model,
        collar_stroke(surface, model, Vector((60.0, 0.0, 0.0)),
                      Vector((0.0, 1.0, 0.0)), count=200, reach=60.0),
        per_loop=25)
    check("a collar is drawn on it", cut is not None)
    if cut is None:
        return
    print(f"  (first build, including reading the model: "
          f"{time.time() - started:.3f}s)")

    before = [list(ring) for ring in surface.line_rings(cut, model)]
    moved = 3
    anchor = cut.pp_points[moved]
    landed, face = walker.place(
        model, Vector(anchor.co) + Vector((0.0, 0.0, 6.0)))
    anchor.co = landed
    anchor.face = face

    started = time.time()
    after = [list(ring) for ring in surface.line_rings(cut, model)]
    elapsed = time.time() - started
    check("re-walking after a drag is interactive", elapsed < 0.1,
          f"{elapsed * 1000:.1f} ms")

    # Everything more than two spans away from the anchor that moved is the
    # very same point it was, not merely close to it.
    was = {(round(q.x, 9), round(q.y, 9), round(q.z, 9)) for q in before[0]}
    kept = sum(1 for p in after[0]
               if (round(p.x, 9), round(p.y, 9), round(p.z, 9)) in was)
    check("most of the line is untouched by the drag",
          kept > len(after[0]) * 0.7,
          f"{kept} of {len(after[0])} samples are unchanged")
    check("and the line still lies on the model",
          surface.line_quality(cut, model)['off_surface'] < 1e-4)
    walker.forget()


def scenario_no_line_is_ever_skipped(core):
    """A cut has no single plane any more, so no line is set aside for
    facing the wrong way, and nothing warns about one."""
    print("Scenario: no line is skipped for facing the wrong way")
    reset_scene()
    import inspect as inspect_module
    from part_pin import surface
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model

    bpy.ops.partpin.add_plane_cut()
    cut = bpy.context.view_layer.objects.active
    cut, _error = surface.convert_to_surface(bpy.context, cut, model,
                                             per_loop=16)
    # A collar round the limb and a line lying lengthwise along it: two
    # lines whose planes are at right angles to one another.
    collar = collar_points(surface, model, cut)
    lengthwise = []
    for i in range(10):
        angle = 2.0 * math.pi * i / 10
        lengthwise.append(model.matrix_world.inverted()
                          @ surface.project_to_surface(
                              model, Vector((-1.0 + 0.8 * math.cos(angle),
                                             0.42 * math.sin(angle), 0.0))))
    surface.store_anchors(cut, collar + lengthwise, [0] * 16 + [1] * 10)

    usable = surface.usable_loop_indices(cut)
    check("both lines are usable, whatever way they face", usable == [0, 1],
          f"usable lines {usable}")
    check("and nothing is wrong with the cut",
          surface.cut_line_problem(cut, model) is None,
          str(surface.cut_line_problem(cut, model)))
    rings = surface.line_rings(cut, model)
    check("both lines are walked", len(rings) == 2, f"got {len(rings)}")

    source = "".join(inspect_module.getsource(m) for m in (surface,))
    check("no alignment test is left in the code",
          "min_alignment" not in source and "do not share a plane" not in source)


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

    on_surface = max(surface_distance(model, model.matrix_world @ Vector(p.co))
                     for p in cut.pp_points)
    check("anchors sit on the model surface", on_surface < 1e-3,
          f"max {on_surface:.2e}")
    check("every anchor knows the face it sits on",
          all(p.face >= 0 for p in cut.pp_points))
    check("anchors are kept in the model's space",
          cut.pp_anchor_space == surface.MODEL_SPACE)

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
    start = model.matrix_world @ Vector(point.co)
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
    point.co = model.matrix_world.inverted() @ dragged
    surface.build_display_mesh(cut, model)

    line = surface.line_rings(cut, model)[0]
    check("the cut line runs through the dragged point",
          min((p - dragged).length for p in line) < 1e-9,
          f"nearest sample {min((p - dragged).length for p in line):.2e}")

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
    # cut face, which both parts share. The cut is made along the model's own
    # faces now, so the point is *on* the seam rather than near it.
    diagonal = core.bbox_diagonal(model)
    worst = max(surface_distance(p, dragged) for p in parts)
    check("both parts touch the dragged point", worst < diagonal * 1e-3,
          f"max distance {worst:.6f} = {worst / diagonal:.4%} of the model")

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
    # Drag one anchor up the sphere, so the line is no longer flat and the
    # plane fitted to it is no longer the plane it started on.
    point = cut.pp_points[0]
    point.co = model.matrix_world.inverted() @ surface.project_to_surface(
        model, model.matrix_world @ Vector(point.co) + Vector((0, 0, 0.5)))
    surface.build_display_mesh(cut, model)

    bpy.context.view_layer.objects.active = cut
    bpy.ops.partpin.add_connectors()
    conns = core.cut_connectors(bpy.context.scene, cut)
    check("connectors placed on the surface cut", len(conns) == 2,
          f"got {len(conns)}")

    def off_the_plane(conn):
        return abs((cut.matrix_world.inverted()
                    @ conn.matrix_world.translation).z)

    off = max(off_the_plane(c) for c in conns)
    check("connectors sit on the cut's own plane", off < 1e-4,
          f"max {off:.2e}")

    # Nudge one off the plane, then snap it back.
    conns[0].matrix_world.translation += Vector((0.0, 0.0, 0.2))
    bpy.context.view_layer.update()
    moved = surface.snap_connectors(cut)
    check("snap moved the connectors", moved == 2)
    check("nudged connector snapped back onto the plane",
          off_the_plane(conns[0]) < 1e-4, f"{off_the_plane(conns[0]):.2e}")

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
    keep = [(Vector(p.co), p.face) for p in cut.pp_points
            if p.loop == kept_loop]
    surface.store_anchors(cut, [c for c, _f in keep], [0] * len(keep),
                          [f for _c, f in keep])
    check("localized cutting is on by default", cut.pp_local)
    # Which sphere the surviving line encircles (x≈0 or x≈4): the line's
    # centroid, not any single point of it.
    centre = sum((model.matrix_world @ co for co, _f in keep),
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
    keep = [(Vector(p.co), p.face) for p in cut.pp_points if p.loop == 0]
    surface.store_anchors(cut, [c for c, _f in keep], [0] * len(keep),
                          [f for _c, f in keep])
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
    polys = surface.loop_polygons(cut, model)
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
    start = model.matrix_world @ Vector(point.co)
    azimuth = math.atan2(start.y, start.x)
    lat = math.radians(35.0)
    dragged = surface.project_to_surface(
        model, Vector((math.cos(lat) * math.cos(azimuth),
                       math.cos(lat) * math.sin(azimuth),
                       math.sin(lat))))
    point.co = model.matrix_world.inverted() @ dragged
    surface.build_display_mesh(cut, model)

    bpy.context.view_layer.objects.active = cut
    bpy.ops.partpin.add_connectors()
    conns = core.cut_connectors(bpy.context.scene, cut)
    check("connectors placed inside the cut line", len(conns) == 2,
          f"got {len(conns)}")
    polys = surface.loop_polygons(cut, model)
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
    # The two halves are parted along the model's own faces and capped with
    # one and the same polygon, so between them they are still the model —
    # nothing is spent at the seam at all.
    check("no material lost at the seam",
          abs(lost) < original_volume * 1e-6,
          f"lost {lost:.5f} of {original_volume:.3f}")
    diagonal = core.bbox_diagonal(model)
    worst = max(surface_distance(p, dragged) for p in parts)
    check("seam still runs through the dragged point", worst < diagonal * 1e-3,
          f"max distance {worst:.6f} = {worst / diagonal:.4%} of the model")

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
    surface.store_anchors(cut, collar_points(surface, model), [0] * 16)
    cut.pp_main_loop = 0
    surface.frame_to_line(cut, model)
    surface.build_display_mesh(cut, model)
    bpy.context.view_layer.update()  # refresh the cached bounding box
    lo, hi = core.world_bbox(cut)
    local_size = max(hi - lo)
    check("localized preview hugs the collar", local_size < 2.5,
          f"preview spans {local_size:.3f}")

    # With localization off the cut is a plane carrying on past the line, and
    # the preview says so rather than showing a lid that will not be used.
    cut.pp_local = False
    surface.build_display_mesh(cut, model)
    bpy.context.view_layer.update()
    lo2, hi2 = core.world_bbox(cut)
    full_size = max(hi2 - lo2)
    check("full-extent preview spans the whole model",
          full_size > local_size * 1.5, f"{full_size:.3f} vs {local_size:.3f}")
    check("and it is one flat plane", len(cut.data.polygons) == 1,
          f"{len(cut.data.polygons)} faces")


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
    for name in ('_rebuild_cache', '_rebuild_cap', '_delete_loop', '_store'):
        setattr(op, name, getattr(cls, name).__get__(op))
    op.cut, op.target = cut, model
    op.hover, op.dragging, op.moved, op._cache = -1, -1, False, None
    op._cap_tris = []
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


def collar_points(surface_mod, model, cut=None, x=2.0, radius=0.55, count=16):  # noqa: E501
    """A cut line dragged right round the limb, as anchors in model space —
    a loop lying nearly perpendicular to the plane the cut started on."""
    points = []
    inverse = model.matrix_world.inverted()
    for i in range(count):
        angle = 2.0 * math.pi * i / count
        world = surface_mod.project_to_surface(
            model, Vector((x, radius * math.cos(angle),
                           radius * math.sin(angle))))
        points.append(inverse @ world)
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

    # Drag the line right round the limb. The anchors are on the model, so
    # the line is a collar the moment they are stored — there is no plane to
    # re-fit, and nothing about the cut it grew out of is left in it.
    surface.store_anchors(cut, collar_points(surface, model), [0] * 16)
    check("no problem reported for the collar",
          surface.cut_line_problem(cut, model) is None,
          str(surface.cut_line_problem(cut, model)))
    ring = surface.line_rings(cut, model)[0]
    check("the collar is a ring right round the limb",
          max(p.x for p in ring) - min(p.x for p in ring) < 0.4
          and max(p.z for p in ring) - min(p.z for p in ring) > 0.7,
          f"spans x {max(p.x for p in ring) - min(p.x for p in ring):.2f}, "
          f"z {max(p.z for p in ring) - min(p.z for p in ring):.2f}")

    # The cut's own frame follows it, which is what its connectors and the
    # "cut right through" mode hang off.
    surface.frame_to_line(cut, model)
    after = cut.matrix_world.to_quaternion() @ Vector((0.0, 0.0, 1.0))
    check("the cut's frame now faces along the limb", abs(after.x) > 0.99,
          f"normal {tuple(round(v, 2) for v in after)}")

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
    """The same line with localization off cuts through on the line's own
    plane, not the plane the cut happened to start on."""
    print("Scenario: a full-extent cut follows the line's own plane")
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    from part_pin import surface

    bpy.ops.partpin.add_plane_cut()
    cut = bpy.context.view_layer.objects.active
    cut, _error = surface.convert_to_surface(bpy.context, cut, model,
                                            per_loop=16)
    surface.store_anchors(cut, collar_points(surface, model), [0] * 16)
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
    into a collar used to leave the others in a different plane, and they were
    dropped with a warning. There is no shared plane now, so every line is
    cut — and a line the user does not want is theirs to remove."""
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
    # Line 0 is the collar; line 1 is a second collar further down the limb,
    # lying in a plane at a good angle to the first.
    collar = collar_points(surface, model, x=2.0)
    second = collar_points(surface, model, x=-1.0, radius=0.5)
    surface.store_anchors(cut, collar + second, [0] * 16 + [1] * 16)
    cut.pp_main_loop = 0  # the collar is what the user was editing

    usable = surface.usable_loop_indices(cut)
    check("both lines are kept, whatever plane they lie in", usable == [0, 1],
          f"usable lines {usable}")
    check("no hard failure", surface.cut_line_problem(cut, model) is None,
          str(surface.cut_line_problem(cut, model)))

    failures = []
    parts, _applied, warns = core.create_parts(
        bpy.context, model, [cut], keep_original=True, failures=failures)
    check("both lines cut, giving three parts", len(parts) == 3,
          f"got {len(parts)}: {failures}")
    check("and nothing is warned about", not warns, str(warns))
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
    """A collar drawn round a limb, ray-cast onto it like the drawing tool."""
    stroke = collar_stroke(surface_mod, model, Vector((x, 0.0, 0.0)),
                           Vector((1.0, 0.0, 0.0)))
    cut, _error = surface_mod.cut_from_stroke(bpy.context, model, stroke,
                                             per_loop=per_loop)
    if cut is None:
        return None
    cut.pp_main_loop = 0
    bpy.context.view_layer.objects.active = cut
    return cut


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


def collar_stroke(surface_mod, model, centre, axis, count=90, reach=4.0):
    """A collar drawn round a limb: rays fired inward at its axis, which is
    what drawing round it in the viewport produces."""
    up = Vector((0, 1, 0)) if abs(axis.y) < 0.9 else Vector((1, 0, 0))
    across = axis.cross(up).normalized()
    other = axis.cross(across).normalized()
    obj = surface_mod.evaluated(model)
    inverse = obj.matrix_world.inverted()
    points = []
    for i in range(count):
        angle = 2.0 * math.pi * i / count
        radial = (math.cos(angle) * across
                  + math.sin(angle) * other).normalized()
        hit, location, _n, _i = obj.ray_cast(
            inverse @ (centre + radial * reach),
            inverse.to_3x3() @ (-radial))
        if hit:
            points.append(obj.matrix_world @ location)
    return points


def make_dense_mesh(around=940, round_it=470, major=60.0, minor=20.0):
    """A closed, manifold model with as many faces as the user's sculpt.

    Built straight into the mesh from numpy arrays: asking bmesh for a sphere
    this dense takes over a minute, and this takes a fifth of a second.
    """
    import numpy as np
    theta = np.arange(around) * (2.0 * np.pi / around)
    phi = np.arange(round_it) * (2.0 * np.pi / round_it)
    T, P = np.meshgrid(theta, phi, indexing='ij')
    co = np.stack([(major + minor * np.cos(P)) * np.cos(T),
                   (major + minor * np.cos(P)) * np.sin(T),
                   minor * np.sin(P)], axis=-1).reshape(-1, 3)
    grid = np.arange(around * round_it).reshape(around, round_it)
    quads = np.stack([grid, np.roll(grid, -1, axis=0),
                      np.roll(np.roll(grid, -1, axis=0), -1, axis=1),
                      np.roll(grid, -1, axis=1)], axis=-1).reshape(-1, 4)
    mesh = bpy.data.meshes.new("Dense")
    mesh.vertices.add(len(co))
    mesh.vertices.foreach_set("co", co.ravel())
    mesh.loops.add(len(quads) * 4)
    mesh.polygons.add(len(quads))
    mesh.loops.foreach_set("vertex_index", quads.ravel().astype(np.int32))
    mesh.polygons.foreach_set("loop_start",
                              (np.arange(len(quads)) * 4).astype(np.int32))
    mesh.update()
    return mesh


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

    # The cut spans the line and nothing beyond it: measured as the lid's
    # extent against the line's own.
    tris = surface.cap_preview_tris(cut, model)
    line = [model.matrix_world @ Vector(p.co) for p in cut.pp_points]
    for axis in range(3):
        reach = max(p[axis] for p in tris) - min(p[axis] for p in tris)
        drawn = max(p[axis] for p in line) - min(p[axis] for p in line)
        check(f"the cut stays within the line on axis {axis}",
              reach <= drawn + core.bbox_diagonal(model) * 0.05,
              f"cut spans {reach:.2f}, line spans {drawn:.2f}")

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
    # Drawn round the arm clear of the overhang, which crosses the model
    # nearby and must come through untouched.
    cut = collar_cut(core, surface, model, radius=0.6, x=1.9)
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
          lo.x > 1.8 and hi.z < 0.6,
          f"x from {lo.x:.2f}, max z {hi.z:.2f}")
    body = max(parts, key=volume)
    lo2, hi2 = core.world_bbox(body)
    check("the overhang stayed whole on the body", hi2.x > 1.4,
          f"body reaches x {hi2.x:.2f}, overhang tip is at 1.5")


def scenario_line_across_a_fin_cuts_it(core):
    """A fin crossing the line is cut where the line crosses it.

    The line is drawn onto the model, so where it runs over a fin it is asking
    for the fin to be cut there too. The cut follows it and says nothing:
    there is no failure here to report.
    """
    print("Scenario: a fin the line runs across is cut with it")
    reset_scene()
    from part_pin import surface
    s = bpy.context.scene.part_pin
    model = make_limb_with_fin(core)
    s.target = model
    before = volume(model)
    cut = collar_cut(core, surface, model)

    failures = []
    parts, _applied, _warns = core.create_parts(
        bpy.context, model, [cut], keep_original=True, failures=failures)
    check("the fin does not stop the cut", len(parts) == 2,
          f"got {len(parts)}: {failures}")
    check("nothing is reported, because nothing went wrong",
          not failures, str(failures))
    if len(parts) != 2:
        return
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p))
    check("the model is all still there",
          abs(sum(volume(p) for p in parts) - before) < before * 1e-6,
          f"{sum(volume(p) for p in parts):.4f} vs {before:.4f}")
    # The fin stands well clear of the limb, so each side keeping some of it
    # is what tells us the fin was cut rather than carried off whole.
    fin_high = [p for p in parts if core.world_bbox(p)[1].z > 0.5]
    check("the fin was cut, not carried off whole", len(fin_high) == 2,
          f"{[round(core.world_bbox(p)[1].z, 2) for p in parts]}")


def scenario_check_line_operator(core):
    print("Scenario: Check Line reports on the span")
    reset_scene()
    from part_pin import surface
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    cut = collar_cut(core, surface, model)
    check("operator is registered",
          hasattr(bpy.ops.partpin, "check_cut_line"))
    before = ([tuple(p.co) for p in cut.pp_points],
              cut.matrix_world.copy())
    bpy.ops.partpin.check_cut_line()
    after = ([tuple(p.co) for p in cut.pp_points], cut.matrix_world.copy())
    check("checking changes nothing about the line",
          after[0] == before[0] and after[1] == before[1],
          f"{len(before[0])} points before, {len(after[0])} after")
    failures = []
    parts, _applied, _warns = core.create_parts(
        bpy.context, model, [cut], keep_original=True, failures=failures)
    check("and the cut works", len(parts) == 2, f"got {len(parts)}: {failures}")


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
    inverse = model.matrix_world.inverted()
    for i in range(12):
        t = i / 11.0 if i < 6 else (11 - i) / 11.0
        world = surface.project_to_surface(
            model, Vector((-2.5 + 5.0 * t, 0.0, 0.45)))
        points.append(inverse @ world)
    surface.store_anchors(cut, points, [0] * 12)

    # Nothing is guessed at up front: whether a line encloses anything is a
    # question about the model, and it is answered by making the cut.
    check("the line itself is not called unusable in advance",
          surface.cut_line_problem(cut, model) is None,
          str(surface.cut_line_problem(cut, model)))

    failures = []
    parts, _applied, _warnings = core.create_parts(
        bpy.context, model, [cut], keep_original=True, failures=failures)
    check("create_parts surfaces the reason", len(failures) == 1,
          str(failures))
    check("and the reason says what to do about it",
          failures and ("ring-fence" in failures[0]
                        or "shorter way round" in failures[0]),
          str(failures))
    # Nothing is made at all, rather than a "parts" collection holding one
    # copy of the model: the cut has to stay put and stay editable, and having
    # to undo a failure to get back to it is worse than the failure.
    check("nothing is made", len(parts) == 0, f"got {len(parts)}")
    check("the model is left alone and visible",
          model.name in bpy.context.scene.objects and not model.hide_get())
    check("no parts collection is left behind",
          not [c for c in bpy.data.collections if c.name.endswith("Parts")],
          str([c.name for c in bpy.data.collections]))
    check("the cut is still there to fix",
          cut.name in bpy.context.scene.objects and not cut.hide_get())


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


def make_cube_model(core, size=2.0):
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=size)
    mesh = bpy.data.meshes.new("Cube")
    bm.to_mesh(mesh)
    bm.free()
    return link(bpy.data.objects.new("Cube", mesh))


def waist_stroke(surface_mod, model, count=157, phase=0.37):
    """A line drawn round a cube's waist: it crosses four sharp corners, and
    the count is chosen so no sample lands neatly on one."""
    obj = surface_mod.evaluated(model)
    inverse = obj.matrix_world.inverted()
    points = []
    for i in range(count):
        angle = 2.0 * math.pi * i / count + phase
        radial = Vector((math.cos(angle), math.sin(angle), 0.0))
        hit, location, _n, _i = obj.ray_cast(inverse @ (radial * 5.0),
                                            inverse.to_3x3() @ (-radial))
        if hit:
            points.append(obj.matrix_world @ location)
    return points


def scenario_corners_are_kept(core):
    """A line drawn over a corner must keep a point on it. Sampling by length
    alone puts points at even spacing and none on the corners, and the cut
    then crosses them — on a cube that stopped it separating at all."""
    print("Scenario: corners of a drawn line are kept")
    reset_scene()
    from part_pin import surface
    s = bpy.context.scene.part_pin
    model = make_cube_model(core)
    s.target = model
    stroke = waist_stroke(surface, model)
    corners = [Vector((x, y, 0.0)) for x in (-1, 1) for y in (-1, 1)]
    check("the stroke runs round the cube", len(stroke) > 100,
          f"{len(stroke)} points")

    for per_loop in (13, 16, 24):
        loop = surface.stroke_to_loop(model, stroke, per_loop=per_loop)
        worst = max(min((p - corner).length for p in loop)
                    for corner in corners)
        check(f"a point lands on each corner ({per_loop} asked for)",
              worst < 0.06, f"worst corner {worst:.3f} from any point")

        cut, error = surface.cut_from_stroke(bpy.context, model, stroke,
                                           per_loop=per_loop)
        check(f"the cut is created ({per_loop})", cut is not None, str(error))
        if cut is None:
            continue
        tris = surface.cap_preview_tris(cut, model)
        missed = max(min((Vector(q) - corner).length for q in tris)
                     for corner in corners)
        check(f"the cut surface reaches the corners ({per_loop})",
              missed < 0.1, f"missed by {missed:.3f} of a 1.0 half-width")
        pieces, spots = surface.trial_cut(cut, model)
        check(f"and the cube separates ({per_loop})", pieces == 2,
              f"{pieces} pieces, {len(spots)} stuck")
        core.remove_object(cut)


def scenario_gap_is_bridged_along_surface(core):
    """Letting go to orbit and drawing again leaves a gap. It is carried across
    the model, not straight through it."""
    print("Scenario: a gap in the drawing is carried across the surface")
    reset_scene()
    from part_pin import draw_cut
    s = bpy.context.scene.part_pin
    model = make_cube_model(core)
    s.target = model
    step = core.bbox_diagonal(model) * 0.004

    # A gap spanning a corner: mid-face on one side to mid-face on the next.
    start, end = Vector((1.0, -0.6, 0.0)), Vector((0.6, 1.0, 0.0))
    bridge = draw_cut.bridge_points(model, start, end, step)
    check("the gap is filled in", len(bridge) > 20, f"{len(bridge)} points")
    check("every point of it is on the model",
          max(surface_distance(model, p) for p in bridge) < 1e-4,
          f"worst {max(surface_distance(model, p) for p in bridge):.2e}")
    check("it goes round the corner rather than through it",
          any(abs(p.x - 1.0) < 1e-4 for p in bridge)
          and any(abs(p.y - 1.0) < 1e-4 for p in bridge))
    walked = sum((bridge[i + 1] - bridge[i]).length
                 for i in range(len(bridge) - 1))
    check("so it is longer than the straight line", walked > (end - start).length,
          f"{walked:.3f} vs {(end - start).length:.3f}")

    check("a gap too small to matter is left alone",
          not draw_cut.bridge_points(model, start, start + Vector((step, 0, 0)),
                                     step))


def scenario_a_join_never_doubles_back(core):
    """The join across a gap must go one way, not out and back.

    Putting a straight line onto the surface is not the same as walking along
    it: nearest-point projection is not monotone in general, so a step can
    land back behind the one before it and the join doubles back on itself.
    That is a hairpin, and a hairpin cannot be cut — there is no room between
    its two sides.

    Held as an invariant rather than a reproduction: no fixture here provokes
    a reversal out of the old code, so this guards the property rather than
    proving the bug. Whatever provokes it wants adding here when it is found.
    """
    print("Scenario: a join across a gap never doubles back")
    reset_scene()
    from part_pin import draw_cut, surface
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    step = core.bbox_diagonal(model) * 0.01

    # Two ends on opposite sides of the limb: the straight line between them
    # runs clean through it, which is the case that used to fold.
    for gap in (Vector((2.0, 0.0, 0.45)), Vector((1.0, 0.42, 0.0)),
                Vector((2.6, 0.0, -0.42))):
        start = surface.project_to_surface(model, Vector((2.0, 0.0, 0.45)))
        end = surface.project_to_surface(model, -gap + Vector((0.0, 0.0, 0.0))
                                         if gap.z else
                                         Vector((gap.x, -gap.y, gap.z)))
        joined = draw_cut.bridge_points(model, start, end, step)
        if len(joined) < 2:
            continue
        walk = [start] + joined + [end]
        span = (end - start)
        if span.length < 1e-9:
            continue
        direction = span / span.length
        along = [(p - start).dot(direction) for p in walk]
        back = [along[i + 1] - along[i] for i in range(len(along) - 1)]
        worst = min(back)
        check(f"the join to {tuple(round(v, 1) for v in end)} only goes "
              "forward", worst >= -1e-9, f"worst step back {worst:.4f}")
        check("and every point of it is on the model",
              max(surface_distance(model, p) for p in joined) < step,
              f"{max(surface_distance(model, p) for p in joined):.4f}")


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
    # Not exactly the number asked for: corners are kept, so a shape with more
    # of them gets more points, and a plain curve fewer.
    check("stroke reduces to a handful of draggable points",
          8 <= len(loop) <= 32, f"got {len(loop)}")
    check("control points sit on the model",
          max(surface_distance(model, p) for p in loop) < 1e-3,
          f"{max(surface_distance(model, p) for p in loop):.2e}")
    spacing = [(loop[(i + 1) % len(loop)] - loop[i]).length
               for i in range(len(loop))]
    # Spacing is not uniform on purpose — points gather at corners — but no
    # stretch may be left so long that dragging cannot shape it.
    perimeter = sum(spacing)
    check("no stretch of the line is left too long",
          max(spacing) < perimeter * 0.25,
          f"longest {max(spacing):.3f} of {perimeter:.3f}")

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
    check("the gap is bridged into a closed ring", 8 <= len(loop) <= 36,
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
    for name in ('_rebuild_cache', '_rebuild_cap'):
        setattr(op, name, getattr(cls, name).__get__(op))
    op.cut, op.target = cut, model
    op.hover, op.dragging, op.moved, op._cache = -1, -1, False, None
    op._cap_tris = []
    op.report = lambda *a, **k: None
    op._rebuild_cache()
    check("the drawn line draws as one loop",
          len(op._cache['polylines']) == 1,
          f"{len(op._cache['polylines'])} loops")

    # Nudge one point along the limb, as dragging it would. Away from the
    # head, so it stays on the surface — dragging in the viewport ray-casts
    # onto visible surface and cannot bury a point inside the model.
    point = cut.pp_points[0]
    world = model.matrix_world @ Vector(point.co)
    moved = surface.project_to_surface(model, world + Vector((-0.25, 0, 0)))
    check("the nudged point is on the model surface",
          surface_distance(model, moved) < 1e-3,
          f"{surface_distance(model, moved):.2e}")
    point.co = model.matrix_world.inverted() @ moved
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
    # It is not turned away at the door — whether a line encloses anything is
    # a question about the model, not about how the loop looks flattened onto
    # some plane, and guessing at it is what used to refuse lines that worked.
    # The cut is made, and it is the cut that says so.
    along = [surface.project_to_surface(model, Vector((x, 0.0, 0.45)))
             for x in [-2.0 + 0.1 * i for i in range(40)]]
    along += list(reversed(along))
    cut, error = surface.cut_from_stroke(bpy.context, model, along,
                                        per_loop=16)
    check("a stroke that doubles back is taken as drawn", cut is not None,
          str(error))
    if cut is not None:
        failures = []
        parts, _applied, _warns = core.create_parts(
            bpy.context, model, [cut], keep_original=True, failures=failures)
        check("and the cut is what reports it cannot be made",
              not parts and len(failures) == 1,
              f"{len(parts)} parts: {failures}")


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


def scenario_lid_reaches_the_line(core):
    """The surface that cuts must reach the line all the way round.

    It used to be laid out straight across the cut's plane between the points,
    which on a rounded line cuts the corners: the lid stopped short of its own
    boundary and those chords dipped inside the model, so nothing separated.
    """
    print("Scenario: the cut surface reaches the drawn line")
    reset_scene()
    from part_pin import surface
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    # Few points around a round limb: the worst case for cutting corners.
    cut = collar_cut(core, surface, model, per_loop=10)
    diagonal = core.bbox_diagonal(model)

    line = [p for ring in surface.line_rings(cut, model) for p in ring]
    tris = surface.cap_preview_tris(cut, model)
    check("the lid is built", len(tris) >= 3, f"{len(tris) // 3} triangles")
    corners = [Vector(p) for p in tris]
    worst = max(min((p - q).length for q in corners) for p in line)
    check("every point of the line is met by the lid",
          worst < diagonal * 0.01,
          f"furthest {worst:.4f} = {worst / diagonal * 100:.2f}% of the model")

    # Which is what lets it cut with points this sparse.
    pieces, spots = surface.trial_cut(cut, model)
    check("and it separates", pieces == 2,
          f"{pieces} pieces, {len(spots)} stuck")


def scenario_trial_cut(core):
    """Whether a cut separates is answered by trying it, and reasons are only
    shown when the answer is no — a cut surface often passes outside the model
    harmlessly, and marking that on a cut that works is noise."""
    print("Scenario: try the cut, and only then explain")
    reset_scene()
    from part_pin import surface
    s = bpy.context.scene.part_pin

    model = make_limb(core)
    s.target = model
    cut = collar_cut(core, surface, model)
    before = volume(model)
    pieces, spots = surface.trial_cut(cut, model)
    check("a good line is reported as separating", pieces == 2, f"{pieces}")
    check("and carries no marks", not spots, f"{len(spots)} stuck")
    check("the model itself is untouched",
          abs(volume(model) - before) < 1e-9 and model.name
          in bpy.context.scene.objects)
    check("nothing is left behind",
          not [o for o in bpy.context.scene.objects
               if "Trial" in o.name or "Cap" in o.name],
          str([o.name for o in bpy.context.scene.objects]))

    # The answer has to be the same as the cut's, on a harder model than the
    # first: a fin crossing the line, which the trial must neither flinch at
    # nor mark up if the cut goes through with it.
    reset_scene()
    s = bpy.context.scene.part_pin
    finned = make_limb_with_fin(core)
    s.target = finned
    across = collar_cut(core, surface, finned)
    pieces, spots = surface.trial_cut(across, finned)
    failures = []
    parts, _applied, _warns = core.create_parts(
        bpy.context, finned, [across], keep_original=True, failures=failures)
    check("the trial matches what the cut does", len(parts) == pieces,
          f"trial {pieces}, cut {len(parts)}: {failures}")
    check("a line across a fin still separates", pieces == 2, f"{pieces}")
    check("and carries no marks either", not spots, f"{len(spots)} stuck")


def scenario_seam_lands_on_the_line(core):
    """The seam has to be the drawn line, not a surface fitted near it.

    A point on the line ends up on the seam exactly when it lies on the skin
    of *both* halves: anywhere the cut wandered off the line, the point would
    be buried inside one of them instead. Measured on a limb, where the line
    curves, and on a cube, where it turns corners.
    """
    print("Scenario: the seam lands on the drawn line")
    from part_pin import surface

    def measure(label, model, cut, allow):
        rings = surface.line_rings(cut, model)
        check(f"{label}: the line is there to check", bool(rings))
        if not rings:
            return
        failures = []
        parts, _applied, _warns = core.create_parts(
            bpy.context, model, [cut], keep_original=True, failures=failures)
        check(f"{label}: two parts", len(parts) == 2,
              f"got {len(parts)}: {failures}")
        if len(parts) != 2:
            return
        for p in parts:
            check(f"{label}: part closed: {p.name}", is_closed(core, p))
        diagonal = core.bbox_diagonal(model)
        worst = max(max(surface_distance(p, point) for p in parts)
                    for ring in rings for point in ring)
        check(f"{label}: every point of the line is on the seam",
              worst < diagonal * allow,
              f"worst {worst / diagonal:.4%} of the model, allowed {allow:.2%}")

    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    measure("limb", model, collar_cut(core, surface, model), 0.002)

    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_cube_model(core)
    s.target = model
    cut, _error = surface.cut_from_stroke(bpy.context, model,
                                          waist_stroke(surface, model),
                                          per_loop=16)
    if cut is not None:
        measure("cube", model, cut, 0.002)

    # And the cut leaves nothing of its own behind.
    strays = [o.name for o in bpy.context.scene.objects
              if o.name.startswith("PartPin_")]
    check("no working objects are left in the scene", not strays, str(strays))


def scenario_parts_can_be_cut_again(core):
    """A part is a model. Cutting one again is how a big model gets down to
    a printable size, and the Model list has to offer them to allow it."""
    print("Scenario: a part can be picked as the model and cut again")
    reset_scene()
    from part_pin import props, surface
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    cut = collar_cut(core, surface, model)
    bpy.ops.partpin.create_parts()
    parts = parts_of(s)
    check("two parts to start with", len(parts) == 2, f"got {len(parts)}")
    if len(parts) != 2:
        return

    check("a part is offered in the Model list",
          all(props._target_poll(s, p) for p in parts),
          str([(p.name, p.pp_role) for p in parts]))
    check("a cut is still not offered", not props._target_poll(s, cut))

    # And picking one really does cut it again.
    # The shaft, not the head: a collar wants a straight stretch to sit on.
    def length_of(part):
        lo, hi = core.world_bbox(part)
        return hi.x - lo.x

    shaft = max(parts, key=length_of)
    before = volume(shaft)
    s.target = shaft
    check("the part can actually be set as the model", s.target == shaft)
    lo, hi = core.world_bbox(shaft)
    again = collar_cut(core, surface, shaft, x=(lo.x + hi.x) * 0.5)
    if again is None:
        check("a second cut is drawn on the part", False)
        return
    failures = []
    made, _applied, _warns = core.create_parts(
        bpy.context, shaft, [again], keep_original=True, failures=failures)
    check("the part splits in two", len(made) == 2,
          f"got {len(made)}: {failures}")
    for p in made:
        check(f"part closed: {p.name}", is_closed(core, p))
    if len(made) == 2:
        check("and it is still all there",
              abs(sum(volume(p) for p in made) - before) < before * 1e-6,
              f"{sum(volume(p) for p in made):.4f} vs {before:.4f}")


def scenario_a_nicked_model_still_cuts(core):
    """A stray hole somewhere else must not stop the cut.

    Real sculpts arrive with the odd flaw in them. One open edge a long way
    from the line used to leave the seam of *both* halves uncapped, because
    capping gave up on a whole mesh that had any open edge in it at all — so
    a four-edge flaw in 400,000 faces failed the cut and blamed the line.
    """
    print("Scenario: a model with a nick in it still cuts")
    reset_scene()
    from part_pin import surface
    s = bpy.context.scene.part_pin
    model = make_limb(core)

    # Knock a hole in it, well away from where the collar will go.
    import bmesh as bm_mod
    bm = bm_mod.new()
    bm.from_mesh(model.data)
    far = max(bm.faces, key=lambda f: f.calc_center_median().x)
    bm_mod.ops.delete(bm, geom=[far], context='FACES_ONLY')
    bm.to_mesh(model.data)
    bm.free()
    model.data.update()
    damage = core.mesh_issues(model)
    check("the model is damaged to start with", damage[1] > 0, str(damage))

    s.target = model
    cut = collar_cut(core, surface, model)
    before = volume(model)
    failures = []
    parts, _applied, _warns = core.create_parts(
        bpy.context, model, [cut], keep_original=True, failures=failures)
    check("it still cuts in two", len(parts) == 2,
          f"got {len(parts)}: {failures}")
    if len(parts) != 2:
        return
    # No worse than it came in: the seam is capped on both halves, and the
    # hole that was there is still exactly one hole.
    after = [sum(counts) for counts in
             zip(*(core.mesh_issues(p) for p in parts))]
    check("and is no worse than it came in",
          after[0] <= damage[0] and after[1] <= damage[1],
          f"{after} against {list(damage)}")
    # A plain hole is a rim like any other, so it gets filled along with the
    # seam and the part comes out better than the model did. Nothing here
    # relies on that, but it must never go the other way.
    for p in parts:
        check(f"part is not left open: {p.name}",
              core.mesh_issues(p)[1] <= damage[1],
              str(core.mesh_issues(p)))
    filled = sum(volume(p) for p in parts)
    check("volume is conserved, bar whatever the hole let in",
          abs(filled - before) < before * 0.01,
          f"{filled:.4f} vs {before:.4f}")


def scenario_a_seam_repairs_itself(core):
    """A seam that comes apart is mended where it came apart.

    A band only ever fails locally, at one crease it could not bridge. Raising
    the whole band to cover that is what makes it reach through a thin fin
    somewhere else entirely, so the repair stands it taller only around the
    loose ends and leaves the rest alone.
    """
    print("Scenario: a seam that comes apart is mended where it broke")
    from part_pin import mesh_cut, surface

    def mend(label, model, cut):
        rings, normals = mesh_cut.line_rings(cut, model)
        thin = core.bbox_diagonal(model) * mesh_cut.BAND_LADDER[0]
        flat = mesh_cut._uniform(rings, thin)
        work = core.duplicate_object(model, "Probe",
                                     bpy.context.scene.collection)
        found, loose = mesh_cut._cut_surface(work, rings, normals, flat,
                                             bpy.context.scene)
        core.remove_object(work)
        if found is not None:
            # Nothing to mend. Since the line started being walked across the
            # model's own faces rather than projected onto it, the thinnest
            # band clears some creases that used to break it — so what is
            # checked here is that a repair is not invented for a seam that
            # closed.
            found[0].free()
            check(f"{label}: a seam that closed is left alone",
                  mesh_cut._repaired(rings, flat, []) is None)
            return
        check(f"{label}: a band this thin comes apart", len(loose) > 0,
              f"{len(loose)} loose ends")

        mended = mesh_cut._repaired(rings, flat, loose)
        check(f"{label}: the repair raises the band", mended is not None)
        if mended is None:
            return
        # Only around the trouble: most of the line is left as it was.
        raised = sum(1 for ring in mended for h in ring if h > thin * 1.01)
        total = sum(len(ring) for ring in mended)
        check(f"{label}: and only around the trouble", raised < total * 0.5,
              f"{raised} of {total} samples raised")

        work = core.duplicate_object(model, "Probe",
                                     bpy.context.scene.collection)
        found, still = mesh_cut._cut_surface(work, rings, normals, mended,
                                             bpy.context.scene)
        core.remove_object(work)
        check(f"{label}: and the seam closes", found is not None,
              f"{len(still)} loose ends left")
        if found is not None:
            found[0].free()

    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_cube_model(core)
    s.target = model
    cut, _error = surface.cut_from_stroke(bpy.context, model,
                                          waist_stroke(surface, model),
                                          per_loop=16)
    if cut is not None:
        mend("cube corners", model, cut)

    reset_scene()
    s = bpy.context.scene.part_pin
    model, base, axis = make_shoulder_arm(core)
    s.target = model
    stroke = collar_stroke(surface, model, base + axis * 0.55, axis)
    cut, _error = surface.cut_from_stroke(bpy.context, model, stroke,
                                          per_loop=18)
    if cut is not None:
        mend("armpit crease", model, cut)


def scenario_a_failed_cut_stays_put(core):
    """A cut that fails has to still be there, with the trouble marked.

    Tearing the scene down on failure — hiding the cut, hiding the model, and
    leaving a collection holding one uncut copy — meant undoing to get back to
    a line that needed a nudge, with nothing to show where to nudge it.
    """
    print("Scenario: a cut that fails stays put, and says where")
    reset_scene()
    from part_pin import surface
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    cut = collar_cut(core, surface, model)

    # The plumbing that carries "where it got stuck" from the cutter to the
    # marks on the model, driven directly since every fixture here does cut.
    spots = [Vector((2.0, 0.4, 0.0)), Vector((2.0, -0.4, 0.0))]
    surface.remember_stuck(cut, spots)
    found = surface.inspect_cut(cut, model)
    check("the stuck spots are marked", len(found.get(surface.STUCK, [])) == 2,
          str({k: len(v) for k, v in found.items()}))
    check("the mark has advice to give", surface.STUCK in surface.TROUBLE)
    reason = surface.failure_reason(spots)
    check("the reason counts them", "2 spot" in reason, reason)
    check("and says where to look", "Edit Cut on Surface" in reason, reason)

    # Three things stop a cut and they want different things of the user, so
    # they must never be described with the same sentence.
    from part_pin import mesh_cut
    said = {
        "came apart": surface.failure_reason(spots, mesh_cut.APART),
        "encloses nothing": surface.failure_reason([], mesh_cut.UNENCLOSED),
        "would not close up": surface.failure_reason([], mesh_cut.UNCAPPED),
    }
    check("each way of failing reads differently",
          len(set(said.values())) == 3, str(list(said)))
    check("only the one about spots counts spots",
          "spot(s)" not in said["encloses nothing"]
          and "spot(s)" not in said["would not close up"],
          str(said))
    check("the one about enclosing says to go right round",
          "right round" in said["encloses nothing"], said["encloses nothing"])
    check("the one about closing up blames the turns, not the line's place",
          "turns" in said["would not close up"], said["would not close up"])

    # A cut that works clears them: marks left over from a failure would sit
    # on the model claiming a working cut is broken.
    pieces, left = surface.trial_cut(cut, model)
    check("the collar does cut", pieces == 2, f"{pieces}")
    check("trying it clears the old marks", not left, f"{len(left)} stuck")
    check("and nothing is marked any more",
          not any(surface.inspect_cut(cut, model).values()))


def scenario_line_hugs_surface(core):
    """The drawn line must lie on the model wherever it runs, and be lifted
    clear of it by a controllable amount so the surface does not swallow it."""
    print("Scenario: the cut line hugs the surface and is lifted clear")
    reset_scene()
    from part_pin import shape_edit, surface
    import types
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    cut = collar_cut(core, surface, model)

    # Pull a point well away, so the cut's own surface wanders off the model
    # between the points — where the line used to leave the surface.
    point = cut.pp_points[0]
    world = model.matrix_world @ Vector(point.co)
    point.co = model.matrix_world.inverted() @ surface.project_to_surface(
        model, world + Vector((-0.5, 0.0, 0.0)))

    cls = shape_edit.PARTPIN_OT_edit_cut_surface
    op = types.SimpleNamespace()
    setattr(op, '_rebuild_cache', cls._rebuild_cache.__get__(op))
    op.cut, op.target = cut, model
    op.hover, op.dragging, op.moved, op._cache = -1, -1, False, None
    op.report = lambda *a, **k: None

    diagonal = core.bbox_diagonal(model)
    s.line_lift = 0.0
    op._rebuild_cache()
    samples = [p for line in op._cache['polylines'] for p in line]
    check("the line is drawn in detail", len(samples) > 60,
          f"{len(samples)} samples")
    worst = max(surface_distance(model, p) for p in samples)
    check("every sample sits on the surface", worst < diagonal * 1e-4,
          f"furthest {worst:.6f} of {diagonal:.2f}")

    s.line_lift = 0.004
    op._rebuild_cache()
    lifted = [p for line in op._cache['polylines'] for p in line]
    offsets = [surface_distance(model, p) for p in lifted]
    expected = diagonal * 0.004
    check("the lift raises the whole line evenly",
          abs(min(offsets) - expected) < expected * 0.1
          and abs(max(offsets) - expected) < expected * 0.1,
          f"{min(offsets):.4f}..{max(offsets):.4f}, expected {expected:.4f}")

    check("the cut itself does not move with the lift",
          max(surface_distance(model, model.matrix_world @ Vector(p.co))
              for p in cut.pp_points) < diagonal * 1e-4,
          "anchors left the surface")

    # The cut still works, and lands where the line is — not where it is drawn.
    s.line_lift = 0.0015
    failures = []
    parts, _applied, _warns = core.create_parts(
        bpy.context, model, [cut], keep_original=True, failures=failures)
    check("the lifted display does not affect the cut", len(parts) == 2,
          f"got {len(parts)}: {failures}")


def scenario_cap_preview(core):
    """The surface spanning the line is shown as it is edited, and it is the
    same surface that does the cutting."""
    print("Scenario: live preview of the cut surface")
    reset_scene()
    from part_pin import surface
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    cut = collar_cut(core, surface, model)

    tris = surface.cap_preview_tris(cut, model)
    check("a surface is produced", len(tris) >= 3 * 8,
          f"{len(tris) // 3} triangles")
    check("its corners come in threes", len(tris) % 3 == 0)

    def bounds(points):
        return ([min(p[i] for p in points) for i in range(3)],
                [max(p[i] for p in points) for i in range(3)])

    line = [model.matrix_world @ Vector(p.co) for p in cut.pp_points]
    cap_lo, cap_hi = bounds(tris)
    line_lo, line_hi = bounds(line)
    span = max(line_hi[i] - line_lo[i] for i in range(3))
    check("it spans the line and no further",
          all(abs(cap_lo[i] - line_lo[i]) < span * 0.08
              and abs(cap_hi[i] - line_hi[i]) < span * 0.08 for i in range(3)),
          f"cap {[round(x, 2) for x in cap_lo]}..{[round(x, 2) for x in cap_hi]}"
          f" vs line {[round(x, 2) for x in line_lo]}.."
          f"{[round(x, 2) for x in line_hi]}")

    # It follows the line: move a point and the surface moves with it.
    point = cut.pp_points[0]
    world = model.matrix_world @ Vector(point.co)
    moved = surface.project_to_surface(model, world + Vector((-0.4, 0, 0)))
    point.co = model.matrix_world.inverted() @ moved
    after = surface.cap_preview_tris(cut, model)
    check("the surface follows the point",
          bounds(after)[0][0] < cap_lo[0] - 0.1,
          f"reached x {bounds(after)[0][0]:.2f}, was {cap_lo[0]:.2f}")

    # And it is the very surface that cuts: the preview is the cutter's own
    # cap plan, laid over the line, rather than something built alongside it.
    from part_pin import mesh_cut
    plans = surface.cap_geometry(cut, model)
    check("the preview is one plan per line", len(plans) == 1,
          f"{len(plans)} plans")
    points, tris_of = plans[0]
    line = surface.line_rings(cut, model)[0]
    rim = points[:len(line)]
    check("its rim is the line itself, point for point",
          len(line) <= surface.PREVIEW_RING
          and all((p - q).length < 1e-9 for p, q in zip(rim, line)),
          f"{len(line)} samples against a rim of {len(rim)}")
    extra, plan = mesh_cut._cap_plan(rim, surface.ring_plane(rim)[1])
    check("and it is the cutter's own plan, not a copy of one",
          plan == tris_of and len(points) == len(rim) + len(extra),
          f"{len(plan)} triangles against {len(tris_of)}")


def scenario_cut_object_shows_the_lid(core):
    print("Scenario: the cut object itself shows the spanning surface")
    reset_scene()
    from part_pin import surface
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    cut = collar_cut(core, surface, model)
    surface.build_display_mesh(cut, model)
    bpy.context.view_layer.update()

    check("the cut object has a surface", len(cut.data.polygons) > 8,
          f"{len(cut.data.polygons)} faces")
    lo, hi = core.world_bbox(cut)
    line = [model.matrix_world @ Vector(p.co) for p in cut.pp_points]
    line_span = max(max(p[i] for p in line) - min(p[i] for p in line)
                    for i in range(3))
    check("it hugs the line rather than spanning the model",
          max(hi - lo) < line_span * 1.3,
          f"{max(hi - lo):.2f} vs line span {line_span:.2f}")


def scenario_inspection_marks(core):
    """Everything measurably wrong with a cut is reported as places on the
    model, and a cut with nothing wrong reports nothing."""
    print("Scenario: inspecting a cut for trouble")
    from part_pin import surface

    for label, maker, kwargs in (("limb", make_limb, {}),
                                 ("cube", make_cube_model, {})):
        reset_scene()
        s = bpy.context.scene.part_pin
        model = maker(core, **kwargs)
        s.target = model
        if maker is make_cube_model:
            cut, _error = surface.cut_from_stroke(
                bpy.context, model, waist_stroke(surface, model), per_loop=16)
        else:
            cut = collar_cut(core, surface, model)
        found = surface.inspect_cut(cut, model)
        check(f"a cut that works reports nothing wrong ({label})",
              not any(found.values()),
              ", ".join(f"{k}={len(v)}" for k, v in found.items() if v))
        pieces, _spots = surface.trial_cut(cut, model)
        check(f"and it does cut ({label})", pieces == 2, f"{pieces} pieces")

    # The awkward one. A fin crossing the line used to be marked in red as a
    # surface folding onto itself, and the cut used to fail. It cuts now, so
    # there must be nothing on it: marks on a working cut are the thing that
    # makes the rest of them worthless.
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_limb_with_fin(core)
    s.target = model
    cut = collar_cut(core, surface, model)
    found = surface.inspect_cut(cut, model)
    pieces, _spots = surface.trial_cut(cut, model)
    check("a line running across a fin cuts", pieces == 2, f"{pieces} pieces")
    check("and is marked with nothing at all", not any(found.values()),
          ", ".join(f"{k}={len(v)}" for k, v in found.items() if v))
    check("every mark there is has advice to give",
          all(kind in surface.TROUBLE for kind in found),
          str(sorted(surface.TROUBLE)))

    # A line dragged off the model is called out as such.
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    cut = collar_cut(core, surface, model)
    for point in cut.pp_points:
        world = model.matrix_world @ Vector(point.co)
        point.co = model.matrix_world.inverted() @ (world * 1.6)
    found = surface.inspect_cut(cut, model)
    check("a line off the model is marked as adrift",
          len(found[surface.ADRIFT]) > 0,
          ", ".join(f"{k}={len(v)}" for k, v in found.items()))


def surface_gap_to(model, point):
    from part_pin import surface
    return surface.surface_gap(model, point)


def scenario_settings_are_all_used(core):
    """Every setting the panel shows must still do something — a dial that
    changes nothing is worse than no dial."""
    print("Scenario: no leftover settings")
    import inspect as inspect_module
    from part_pin import core as core_module
    from part_pin import draw_cut, ops, shape_edit, surface, ui

    panel_source = inspect_module.getsource(ui)
    code = "".join(inspect_module.getsource(module) for module in
                   (core_module, surface, ops, shape_edit, draw_cut))

    import re
    shown = set(re.findall(r'\.prop\((?:s|cut), "(\w+)"', panel_source))
    check("the panel shows some settings", len(shown) > 5, str(sorted(shown)))
    unused = [name for name in shown
              if code.count(name) == 0]
    check("every setting the panel shows is used", not unused, str(unused))


def scenario_operator_wiring_audit(core):
    """Every self.helper() an operator calls, and every self.thing it reads,
    must exist.

    Renaming a helper and missing a call site fails only in the running app,
    on whichever click reaches it — a modal's invoke cannot be exercised
    headlessly, so this reads the code instead of running it.
    """
    print("Scenario: operator wiring audit")
    import ast
    import inspect
    from part_pin import draw_cut, ops, shape_edit

    # Provided by Blender on the instance rather than the class, so not
    # visible on the class itself.
    from_blender = {'report', 'layout', 'properties', 'options', 'has_reports',
                    'bl_rna', 'as_keywords', 'poll_message_set'}
    missing_calls, missing_reads = [], []
    for module in (shape_edit, draw_cut, ops):
        tree = ast.parse(inspect.getsource(module))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            cls = getattr(module, node.name, None)
            if cls is None:
                continue
            known = (set(dir(cls)) | from_blender
                     | set(getattr(cls, '__annotations__', {})))
            assigned = {
                target.attr
                for sub in ast.walk(node)
                if isinstance(sub, (ast.Assign, ast.AugAssign, ast.AnnAssign))
                for target in (sub.targets if isinstance(sub, ast.Assign)
                               else [sub.target])
                if isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == 'self'
            }
            for sub in ast.walk(node):
                if not (isinstance(sub, ast.Attribute)
                        and isinstance(sub.value, ast.Name)
                        and sub.value.id == 'self'):
                    continue
                if sub.attr in known or sub.attr in assigned:
                    continue
                if isinstance(sub.ctx, ast.Load):
                    missing_reads.append(f"{module.__name__}.{node.name}"
                                         f".{sub.attr}")

    for module in (shape_edit, draw_cut, ops):
        tree = ast.parse(inspect.getsource(module))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            cls = getattr(module, node.name, None)
            if cls is None:
                continue
            known = (set(dir(cls)) | from_blender
                     | set(getattr(cls, '__annotations__', {})))
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id == 'self'
                        and sub.func.attr not in known):
                    missing_calls.append(f"{module.__name__}.{node.name}"
                                         f".{sub.func.attr}()")

    check("every self.helper() call resolves to a method",
          not missing_calls, str(sorted(set(missing_calls))))
    check("every self.attribute read is set somewhere",
          not missing_reads, str(sorted(set(missing_reads))))


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
    for name in ('_rebuild_cache', '_rebuild_cap', '_insert_point',
                 '_delete_point', '_store',
                 '_nearest_point', '_surface_hit'):
        setattr(op, name, getattr(cls, name).__get__(op))
    op.cut, op.target = cut, model
    op.hover, op.dragging, op.moved, op._cache = -1, -1, False, None
    op.report = lambda *a, **k: None

    op._rebuild_cache()
    check("cache holds one cut line", len(op._cache['polylines']) == 1)
    line = op._cache['polylines'][0]
    check("cut line is walked out and closed",
          len(line) > 12 and (line[0] - line[-1]).length < 1e-9,
          f"{len(line)} points")
    # Drawn lifted clear of the surface, so it is not swallowed by it.
    lift = core.bbox_diagonal(model) * s.line_lift
    off = max(surface_distance(model, p) for p in line)
    check("drawn cut line hugs the model surface, at the lift it is drawn at",
          abs(off - lift) < max(lift, 1e-6) * 0.05 + 1e-6,
          f"max {off:.5f}, lift {lift:.5f}")
    check("one world position per anchor", len(op._cache['world']) == 12)

    # Ctrl+click insert: the new point must land on the surface, in a loop.
    hit = surface.project_to_surface(model, Vector((1.0, 0.02, 0.0)))
    op._surface_hit = lambda context, mouse: (hit, -1)
    op._insert_point(bpy.context, (0, 0))
    check("insert added an anchor", len(cut.pp_points) == 13,
          f"got {len(cut.pp_points)}")
    worst = max(surface_distance(model, model.matrix_world @ Vector(p.co))
                for p in cut.pp_points)
    check("all anchors still on the model surface", worst < 1e-3,
          f"max {worst:.2e}")
    check("inserted point kept the loop id",
          {p.loop for p in cut.pp_points} == {0})
    inserted = min(range(len(cut.pp_points)),
                   key=lambda i: ((model.matrix_world
                                   @ Vector(cut.pp_points[i].co)) - hit).length)
    check("inserted point is adjacent to its neighbours in storage order",
          0 < inserted < 13)

    op.hover = 0
    op._delete_point(0)
    check("delete removed an anchor", len(cut.pp_points) == 12,
          f"got {len(cut.pp_points)}")

    # Guard: never shrink a loop below 3 points.
    keep = [(Vector(p.co), p.loop, p.face) for p in cut.pp_points][:3]
    op._store(keep)
    op._delete_point(0)
    check("refuses to delete below 3 points", len(cut.pp_points) == 3,
          f"got {len(cut.pp_points)}")


def make_dense_sphere(segments=900, rings=250, radius=78.0):
    """A closed sculpt-sized model: a sphere with its vertices nudged about.

    The nudging matters. A perfectly regular sphere has every one of its edges
    running along a line of latitude or longitude, and a cut drawn round it
    lands exactly on them — which is a real case, but not the one a sculpt
    gives, and testing only that would leave the ordinary case untested.
    """
    import numpy as np
    lat = (np.arange(1, rings) / rings) * np.pi
    lon = np.arange(segments) * (2.0 * np.pi / segments)
    LAT, LON = np.meshgrid(lat, lon, indexing='ij')
    # A wobble worked out from the angles themselves, so it is the same every
    # run without asking for a random number.
    wobble = 1.0 + 0.004 * np.sin(LAT * 37.0) * np.cos(LON * 23.0)
    body = np.stack([radius * wobble * np.sin(LAT) * np.cos(LON),
                     radius * wobble * np.sin(LAT) * np.sin(LON),
                     radius * wobble * np.cos(LAT)], axis=-1).reshape(-1, 3)
    co = np.concatenate([body, [[0.0, 0.0, radius], [0.0, 0.0, -radius]]])
    top, bottom = len(co) - 2, len(co) - 1

    grid = np.arange((rings - 1) * segments).reshape(rings - 1, segments)
    quads = np.stack([grid[:-1], np.roll(grid[:-1], -1, axis=1),
                      np.roll(grid[1:], -1, axis=1), grid[1:]],
                     axis=-1).reshape(-1, 4)
    caps = [(top, int(grid[0][(i + 1) % segments]), int(grid[0][i]))
            for i in range(segments)]
    caps += [(bottom, int(grid[-1][i]), int(grid[-1][(i + 1) % segments]))
             for i in range(segments)]

    mesh = bpy.data.meshes.new("DenseSphere")
    mesh.vertices.add(len(co))
    mesh.vertices.foreach_set("co", co.ravel())
    corners = list(quads.ravel()) + [i for face in caps for i in face]
    starts, at = [], 0
    for total in [4] * len(quads) + [3] * len(caps):
        starts.append(at)
        at += total
    mesh.loops.add(len(corners))
    mesh.polygons.add(len(quads) + len(caps))
    mesh.loops.foreach_set("vertex_index", corners)
    mesh.polygons.foreach_set("loop_start", starts)
    mesh.update()
    return mesh


def scenario_a_dense_model_cuts(core):
    """A cut on a model as dense as a real sculpt.

    Everything about the cut scales with the model's own faces: the line is
    walked across them, so its ring carries a point per face it crosses, and
    the rim the cap has to fill carries one per face along the seam. Held to
    the same measures as a coarse rim, that cap was rejected step by step
    until it gave up and filled the whole thing flat — half a minute of
    ear-clipping a thousand-point polygon, a part with holes in it, and then
    nineteen more bands tried against a cut that had already worked.
    """
    print("Scenario: a cut on a model as dense as a sculpt")
    reset_scene()
    import time
    from part_pin import mesh_cut, surface, walker
    s = bpy.context.scene.part_pin

    model = link(bpy.data.objects.new("Sculpt", make_dense_sphere()))
    s.target = model
    bpy.context.view_layer.update()
    check("the fixture is as dense as a sculpt",
          len(model.data.polygons) > 200000 and is_closed(core, model),
          f"{len(model.data.polygons)} faces, {core.mesh_issues(model)}")
    before = volume(model)

    walker.forget()
    cut, error = surface.cut_from_stroke(
        bpy.context, model,
        collar_stroke(surface, model, Vector((0.0, 0.0, 30.0)),
                      Vector((0.0, 0.0, 1.0)), count=180, reach=120.0),
        per_loop=24)
    check("a collar is drawn round it", cut is not None, str(error))
    if cut is None:
        return
    rings, _normals = mesh_cut.line_rings(cut, model)
    check("its ring is as dense as the model", len(rings[0]) > 400,
          f"{len(rings[0])} samples")

    started = time.time()
    failures = []
    parts, _applied, warns = core.create_parts(
        bpy.context, model, [cut], keep_original=True, failures=failures)
    elapsed = time.time() - started
    check("it cuts", len(parts) == 2, f"got {len(parts)}: {failures}")
    if len(parts) != 2:
        return
    for p in parts:
        check(f"part closed: {p.name}", is_closed(core, p),
              str(core.mesh_issues(p)))
    check("nothing is warned about", not warns, str(warns))
    check("the model is all still there",
          abs(sum(volume(p) for p in parts) - before) < before * 1e-6,
          f"{sum(volume(p) for p in parts):.3f} vs {before:.3f}")
    # The first band should do it. Grinding through the ladder on a model
    # this size is a minute of work and, on one machine, a crash.
    check("and it does not grind through the ladder to get there",
          elapsed < 30.0, f"took {elapsed:.1f}s")
    print(f"  (the whole cut took {elapsed:.1f}s)")

    # The cap has to step inwards on a rim this dense, not give up and fill
    # it flat: filling flat is the slow, hole-leaving path.
    rim = surface.resample_loop(rings[0], min(len(rings[0]), 900),
                               cyclic=True)
    started = time.time()
    extra, tris = mesh_cut._cap_plan(rim, mesh_cut.cap_normal(rim))
    check("the cap steps inwards over a dense rim", tris and len(extra) > 20,
          f"{len(extra)} points inside a {len(rim)}-point rim")
    check("and planning it is quick", time.time() - started < 2.0,
          f"{time.time() - started:.1f}s")
    walker.forget()


def scenario_a_cut_says_how_far_along_it_is(core):
    """A cut takes seconds per attempt and there can be twenty of them, so it
    is handed out a step at a time: something to show, and something to stop."""
    print("Scenario: a cut can be watched, and stopped")
    reset_scene()
    from part_pin import ops, surface
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    before = volume(model)
    cut = collar_cut(core, surface, model)

    seen = []
    steps = core.create_parts_steps(bpy.context, model, [cut],
                                    keep_original=True)
    while True:
        try:
            seen.append(next(steps))
        except StopIteration as done:
            parts, _applied, _warnings = done.value
            break
    check("it reports its way through the cut", len(seen) >= 2,
          f"{len(seen)} steps")
    check("every step says how far along it is",
          all(0.0 <= fraction <= 1.0 and doing for fraction, doing in seen),
          str(seen[:3]))
    check("and the steps run forwards",
          all(b[0] >= a[0] for a, b in zip(seen, seen[1:])),
          str([round(f, 2) for f, _d in seen]))
    check("the cut still comes out in two", len(parts) == 2,
          f"got {len(parts)}")
    check("a bar is drawn for it",
          ops.progress_bar(0.0).startswith("░")
          and ops.progress_bar(1.0).startswith("█")
          and len(ops.progress_bar(0.5)) == len(ops.progress_bar(1.0)))

    # Stopping it part way has to leave the scene exactly as it was found.
    reset_scene()
    s = bpy.context.scene.part_pin
    model = make_limb(core)
    s.target = model
    cut = collar_cut(core, surface, model)
    collections = {c.name for c in bpy.data.collections}
    objects = {o.name for o in bpy.context.scene.objects}
    steps = core.create_parts_steps(bpy.context, model, [cut],
                                    keep_original=True)
    next(steps)
    next(steps)
    steps.close()
    check("stopping puts back everything it had made",
          {c.name for c in bpy.data.collections} == collections
          and {o.name for o in bpy.context.scene.objects} == objects,
          str(sorted({o.name for o in bpy.context.scene.objects} - objects)))
    check("the model is still there, still visible",
          model.name in bpy.context.scene.objects and not model.hide_get()
          and abs(volume(model) - before) < 1e-9)
    check("and so is the cut", cut.name in bpy.context.scene.objects)


def scenario_the_version_is_shown_and_agrees(core):
    """The version is on screen, and it is the one on the release.

    It lives in two files — the manifest Blender installs from, and `bl_info`
    — and a release where those disagree is one where a bug report cannot be
    tied to a build.
    """
    print("Scenario: the version is shown, and says the same everywhere")
    import re
    from part_pin import ui

    shown = ui.version()
    check("the panel has a version to show", re.fullmatch(r"\d+\.\d+\.\d+",
                                                          shown), shown)
    manifest = os.path.join(REPO, "part_pin", "blender_manifest.toml")
    with open(manifest) as handle:
        written = re.search(r'^version = "(.+)"', handle.read(),
                            re.MULTILINE).group(1)
    check("and it is the version Blender installs", shown == written,
          f"panel says {shown}, manifest says {written}")
    check("it is drawn at the bottom, under everything else",
          ui.CLASSES[-1] is ui.PARTPIN_PT_version
          and ui.PARTPIN_PT_version.bl_parent_id == "PARTPIN_PT_main",
          str([c.__name__ for c in ui.CLASSES]))


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
    scenario_custom_connector(core)
    scenario_flip_pin(core)
    scenario_validation(core)
    scenario_the_walker(core)
    scenario_the_line_holds_its_contour(core)
    scenario_dragging_stays_local(core)
    scenario_no_line_is_ever_skipped(core)
    scenario_surface_convert(core)
    scenario_surface_drag(core)
    scenario_surface_connectors(core)
    scenario_surface_multi_loop(core)
    scenario_local_leaves_rest_whole(core)
    scenario_local_vs_full(core)
    scenario_local_wide_piece(core)
    scenario_local_seam(core)
    scenario_local_display(core)
    scenario_delete_loop(core)
    scenario_collar_cut(core)
    scenario_collar_full_extent(core)
    scenario_collar_plus_leftover_line(core)
    scenario_arm_at_shoulder(core)
    scenario_overhang_outside_line_survives(core)
    scenario_line_across_a_fin_cuts_it(core)
    scenario_check_line_operator(core)
    scenario_unusable_line_reports(core)
    scenario_corners_are_kept(core)
    scenario_gap_is_bridged_along_surface(core)
    scenario_a_join_never_doubles_back(core)
    scenario_draw_cut(core)
    scenario_draw_cut_in_pieces(core)
    scenario_draw_then_adjust(core)
    scenario_draw_cut_rejections(core)
    scenario_draw_operator_registered(core)
    scenario_lid_reaches_the_line(core)
    scenario_trial_cut(core)
    scenario_seam_lands_on_the_line(core)
    scenario_parts_can_be_cut_again(core)
    scenario_a_nicked_model_still_cuts(core)
    scenario_a_seam_repairs_itself(core)
    scenario_a_failed_cut_stays_put(core)
    scenario_line_hugs_surface(core)
    scenario_cap_preview(core)
    scenario_cut_object_shows_the_lid(core)
    scenario_inspection_marks(core)
    scenario_settings_are_all_used(core)
    scenario_operator_wiring_audit(core)
    scenario_modal_helpers(core)
    scenario_a_dense_model_cuts(core)
    scenario_a_cut_says_how_far_along_it_is(core)
    scenario_the_version_is_shown_and_agrees(core)
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
