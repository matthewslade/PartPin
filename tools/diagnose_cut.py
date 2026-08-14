"""Open a saved .blend and say exactly why a cut will not go through.

    ./.venv-bpy/bin/python tools/diagnose_cut.py <file.blend> [cut name]

Reports the model, the line, and then every band the cutter would try, with
what each one left behind — so a cut that fails says which of the three ways
it failed and where, rather than only that it did.

Nothing is written back: the file is opened read-only as far as this is
concerned, and the cut is tried on a copy.
"""

import os
import sys

import bpy  # noqa: F401  (must precede bmesh with the wheel)
import bmesh
from mathutils import Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import part_pin  # noqa: E402
from part_pin import core, mesh_cut, surface  # noqa: E402


def mesh_report(obj, label):
    non_manifold, boundary = core.mesh_issues(obj)
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    tiny = sum(1 for f in bm.faces if f.calc_area() < 1e-14)
    faces = len(bm.faces)
    bm.free()
    lo, hi = core.world_bbox(obj)
    scale = tuple(round(v, 4) for v in obj.matrix_world.to_scale())
    print(f"{label}: {obj.name}")
    print(f"    {faces} faces, size {tuple(round(v, 3) for v in (hi - lo))}, "
          f"diagonal {core.bbox_diagonal(obj):.4f}, object scale {scale}")
    print(f"    {non_manifold} non-manifold edge(s), {boundary} open edge(s), "
          f"{tiny} face(s) of no area")
    if non_manifold or boundary:
        print("    *** NOT a closed manifold mesh — this alone will stop a "
              "cut. Repair it first (3D-Print Toolbox, Remesh).")
    if tiny:
        print("    *** faces of no area confuse the exact solver; a Merge By "
              "Distance would clear them.")


def line_report(cut, target):
    print(f"\ncut: {cut.name}")
    print(f"    kind {cut.pp_cut_kind}, local {bool(cut.pp_local)}, "
          f"{len(cut.pp_points)} control point(s), "
          f"{len(surface.control_loops(cut))} line(s)")
    if not surface.is_local(cut):
        print("    *** not a localized surface cut, so the new cutter is not "
              "what runs for it. 'Cut Inside Line Only' has to be on.")
    problem = surface.cut_line_problem(cut)
    if problem:
        print(f"    *** the line itself is unusable: {problem}")
        return None

    rings, normals, normal, settle = surface.line_rings(cut, target)
    if rings is None:
        print("    *** no line to cut along")
        return None
    diagonal = core.bbox_diagonal(target)
    for i, ring in enumerate(rings):
        gaps = [(ring[(k + 1) % len(ring)] - p).length
                for k, p in enumerate(ring)]
        off = max(surface.surface_gap(target, p) for p in ring)
        print(f"    line {i}: {len(ring)} samples, length "
               f"{surface.loop_length(ring):.4f}, spacing "
               f"{min(gaps):.5f}..{max(gaps):.5f}, "
               f"furthest off the surface {off / diagonal:.4%} of the model")
    return rings, normals, normal, settle


def try_every_band(target, cut, rings, normals, normal, settle):
    """Walk the same bands the cutter would, reporting each."""
    diagonal = core.bbox_diagonal(target)
    was = core.mesh_issues(target)
    if any(was):
        print(f"\n(the model comes in with {was[0]} non-manifold and {was[1]} "
              "open edge(s); a cut is judged on leaving it no worse than that)")
    scene = bpy.context.scene
    spots = []
    plan = [("rung", h) for h in mesh_cut.BAND_LADDER]
    print("\nbands the cutter would try:")

    for round_no in range(-1, mesh_cut.REPAIR_ROUNDS):
        for _label, height in plan:
            base = mesh_cut._uniform(rings, diagonal * height)
            if round_no < 0:
                heights, what = base, f"uniform {height}"
            else:
                heights = mesh_cut._repaired(rings, base, spots, round_no)
                if heights is None:
                    continue
                raised = sum(1 for r in heights for h in r
                             if h > diagonal * height * 1.01)
                total = sum(len(r) for r in heights)
                what = (f"mended round {round_no + 1} at {height} "
                        f"({raised}/{total} samples raised)")

            work = core.duplicate_object(target, "PartPin_Probe",
                                         scene.collection)
            try:
                found, loose = mesh_cut._cut_surface(work, rings, normals,
                                                     heights, scene)
            except Exception as exc:
                found, loose = None, []
                print(f"  {what}: raised {type(exc).__name__}: {exc}")
            if work.name in bpy.data.objects:
                core.remove_object(work)

            if found is None:
                if loose:
                    print(f"  {what}: SEAM CAME APART at {len(loose)} spot(s)")
                    if not spots or len(loose) < len(spots):
                        spots = loose
                else:
                    print(f"  {what}: no seam, or it enclosed nothing")
                continue

            bm, layer, seam = found
            regions = [len(g) for g in mesh_cut._regions(bm, layer, seam)]
            print(f"  {what}: seam closed ({len(seam)} edges), "
                  f"regions {sorted(regions, reverse=True)[:4]}")
            try:
                pieces = mesh_cut._part_and_cap(bm, layer, seam, normal, scene,
                                                scene.collection, settle)
            except Exception as exc:
                print(f"      *** could not part and cap it: "
                      f"{type(exc).__name__}: {exc}")
                continue
            issues = [core.mesh_issues(p) for p in pieces]
            now = [sum(counts) for counts in zip(*issues)]
            print(f"      -> {len(pieces)} piece(s), issues {issues}")
            for piece in pieces:
                core.remove_object(piece)
            if len(pieces) >= 2 and now[0] <= was[0] and now[1] <= was[1]:
                print(f"\nVERDICT: this cut goes through, on '{what}'.")
                return
            print(f"      *** worse than it started ({now} against "
                  f"{list(was)}), so this band is rejected")
    if spots:
        print(f"\nVERDICT: the seam comes apart, best case {len(spots)} "
              "spot(s). Where they are, in world space:")
        middle = sum(spots, Vector()) / len(spots)
        for point in spots[:20]:
            print(f"    {tuple(round(v, 4) for v in point)}")
        if len(spots) > 20:
            print(f"    ... and {len(spots) - 20} more")
        print(f"    they centre on {tuple(round(v, 4) for v in middle)} and "
              f"spread over "
              f"{max((p - middle).length for p in spots) / diagonal:.2%} "
              "of the model")
    else:
        print("\nVERDICT: no band produced a seam that enclosed anything. "
              "The line may not close, or may not lie on this model.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    wanted = sys.argv[2] if len(sys.argv) > 2 else None

    part_pin.register()
    bpy.ops.wm.open_mainfile(filepath=path)

    settings = getattr(bpy.context.scene, "part_pin", None)
    target = getattr(settings, "target", None)
    cuts = core.scene_cuts(bpy.context.scene)
    print(f"file: {path}")
    print(f"    {len(bpy.context.scene.objects)} object(s), "
          f"{len(cuts)} PartPin cut(s), model set to "
          f"{target.name if target else 'nothing'}")
    if target is None:
        meshes = [o for o in bpy.context.scene.objects
                  if o.type == 'MESH' and not o.pp_role]
        print("    no Model set; guessing from the mesh objects: "
              f"{[o.name for o in meshes]}")
        if not meshes:
            return 1
        target = max(meshes, key=lambda o: len(o.data.polygons))
    if not cuts:
        print("    *** no cuts in the file — save it with the cut still there")
        return 1

    mesh_report(target, "\nmodel")
    for cut in cuts:
        if wanted and cut.name != wanted:
            continue
        found = line_report(cut, target)
        if found is None:
            continue
        try_every_band(target, cut, *found)
    return 0


if __name__ == "__main__":
    sys.exit(main())
