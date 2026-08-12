"""Headless smoke test for the PartPin add-on.

Run with:

    blender --background --python-exit-code 1 --python tests/smoke_test.py

Covers: straight cuts (draft + easy mode), multiple cuts, disabled cuts,
curved cuts, built-in and custom connectors, pin/socket generation,
manifold validation, and STL/OBJ/FBX export.
"""

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
