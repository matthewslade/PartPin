"""The localized cut: sever the model along the line drawn on it.

Nothing here synthesises a surface to cut with. The model's own faces are cut
along the drawn line, the two sides of that cut are parted, and each is capped
with the polygon the line describes. Both halves are therefore built from
geometry that already lay on the model, which is what makes the seam land
exactly on the line rather than near it.

How it works:

1. The line, as a dense ring of points on the surface — `surface.line_samples`,
   the one definition of where the line runs.
2. A **band**: a thin ribbon standing along the ring, straddling the surface.
3. `bpy.ops.mesh.intersect` cuts the model's faces where the band crosses them.
   This writes the line into the model's own topology; the vertices it creates
   sit on the line exactly, so there is no tolerance anywhere below.
4. The **seam** is read off without measuring anything: an edge with a band
   face on one side and a model face on the other is on the cut.
5. The seam is checked — it has to be one closed loop that parts the model's
   faces in two — then the two sides are split apart and each is capped.

The one judgement call is how far the band should stand off the surface. Too
thin and it fails to cross a crease, leaving the seam in pieces; too thick and
it reaches through to something else and cuts that too. No single height suits
every model, so this does not try to find one: it works up a ladder of heights
and keeps the first that is *demonstrated* to have worked — carried right
through to two closed parts. The height is therefore only ever a question of
how many tries it takes, never of whether the answer is right.
"""

import bmesh
import bpy
from mathutils import Vector

from . import core, surface

# Band heights to try, as a share of the model's size. A thin band disturbs
# least, so it is tried first; each rung up bridges a coarser crease at the
# cost of reaching further into the model.
BAND_LADDER = (0.0015, 0.003, 0.006, 0.012, 0.024)

# Marks the band's faces so they can be told from the model's after the
# intersect has rebuilt the mesh around them.
BAND_TAG = "pp_band"

# Passes of averaging along the ring's normals. At a crease the two sides
# disagree by the whole angle of it, and a band built on the raw normals folds
# there instead of bending; averaged, it leans along the bisector and crosses
# the crease cleanly.
NORMAL_SMOOTHING = 2


def ring_normals(ring, target, smoothing=NORMAL_SMOOTHING):
    """The model's outward normal at each point of one ring, evened out."""
    obj = surface.evaluated(target)
    to_model = obj.matrix_world.inverted()
    rotation = obj.matrix_world.to_3x3()
    up = Vector((0.0, 0.0, 1.0))

    normals = []
    for point in ring:
        ok, _near, normal, _index = obj.closest_point_on_mesh(
            to_model @ point)
        normal = (rotation @ normal) if ok else up
        normals.append(normal.normalized() if normal.length > 1e-9 else up)

    count = len(normals)
    for _pass in range(smoothing):
        evened = []
        for i, normal in enumerate(normals):
            total = normals[i - 1] + normal * 2.0 + normals[(i + 1) % count]
            evened.append(total.normalized() if total.length > 1e-9
                          else normal)
        normals = evened
    return normals


def _work_object(obj, rings, normals, height, scene):
    """The object's mesh with a band standing along each ring, ready to cut.

    The model's faces are left selected and the band's are not, because
    `mesh.intersect` cuts the selected geometry against the unselected. The
    selection has to be set here, on the mesh in object mode: assignments made
    through `bmesh.from_edit_mesh` do not reach the operator.
    """
    source = surface.evaluated(obj)
    bm = bmesh.new()
    mesh = bpy.data.meshes.new_from_object(source)
    bm.from_mesh(mesh)
    bm.transform(source.matrix_world)
    bpy.data.meshes.remove(mesh)

    layer = bm.faces.layers.int.new(BAND_TAG)
    for face in bm.faces:
        face[layer] = 0

    # One band per line: a cut can have several, and every one of them has to
    # be cut for the piece they ring-fence between them to come away.
    for ring, along in zip(rings, normals):
        outer = [bm.verts.new(p + n * height) for p, n in zip(ring, along)]
        inner = [bm.verts.new(p - n * height) for p, n in zip(ring, along)]
        for i in range(len(ring)):
            j = (i + 1) % len(ring)
            # Triangles, not quads: a quad spanning a bend in the line is not
            # planar, and the solver has to guess how to split it.
            for corners in ((inner[i], inner[j], outer[j]),
                            (inner[i], outer[j], outer[i])):
                try:
                    face = bm.faces.new(corners)
                except ValueError:
                    continue  # a repeated point; the band skips it
                face[layer] = 1

    data = bpy.data.meshes.new("PartPin_Work")
    bm.to_mesh(data)
    bm.free()

    tags = data.attributes.get(BAND_TAG)
    for poly in data.polygons:
        poly.select = not tags.data[poly.index].value
    for vert in data.vertices:
        vert.select = False
    for poly in data.polygons:
        if poly.select:
            for index in poly.vertices:
                data.vertices[index].select = True
    for edge in data.edges:
        edge.select = all(data.vertices[i].select for i in edge.vertices)

    work = bpy.data.objects.new("PartPin_Work", data)
    scene.collection.objects.link(work)
    work.hide_render = True
    return work


def _regions(bm, layer, seam):
    """The model's faces grouped into what the seam separates them into."""
    seen, groups = set(), []
    for face in bm.faces:
        if face[layer] or face in seen:
            continue
        stack, group = [face], []
        seen.add(face)
        while stack:
            current = stack.pop()
            group.append(current)
            for edge in current.edges:
                if edge in seam:
                    continue
                for other in edge.link_faces:
                    if other[layer] or other in seen:
                        continue
                    seen.add(other)
                    stack.append(other)
        groups.append(group)
    return groups


def _cut_surface(obj, rings, normals, height, scene):
    """Cut the object's faces along the lines. Returns (bmesh, layer, seam).

    None if the bands did not leave closed seams parting the faces in two or
    more — which is the whole test, and it is exact: a seam either closes or
    it does not.
    """
    work = _work_object(obj, rings, normals, height, scene)
    view_layer = bpy.context.view_layer
    for other in list(bpy.context.selected_objects):
        other.select_set(False)
    work.select_set(True)
    view_layer.objects.active = work
    bpy.context.tool_settings.mesh_select_mode = (False, False, True)

    bpy.ops.object.mode_set(mode='EDIT')
    try:
        bpy.ops.mesh.intersect(mode='SELECT_UNSELECT', separate_mode='NONE',
                               solver='EXACT')
    except RuntimeError:
        return None
    finally:
        if work.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

    bm = bmesh.new()
    bm.from_mesh(work.data)
    core.remove_object(work)

    layer = bm.faces.layers.int[BAND_TAG]
    seam = {edge for edge in bm.edges
            if any(face[layer] for face in edge.link_faces)
            and any(not face[layer] for face in edge.link_faces)}

    degree = {}
    for edge in seam:
        for vert in edge.verts:
            degree[vert] = degree.get(vert, 0) + 1
    if not seam or any(count != 2 for count in degree.values()):
        bm.free()
        return None  # the seam is in pieces: the band missed a crease

    if len(_regions(bm, layer, seam)) < 2:
        bm.free()
        return None  # the line does not ring-fence anything on this part
    return bm, layer, seam


def _boundary_loops(bm):
    """The open rims of a mesh, each as its vertices in the order they run."""
    open_edges = [edge for edge in bm.edges if len(edge.link_faces) == 1]
    along = {}
    for edge in open_edges:
        for vert in edge.verts:
            along.setdefault(vert, []).append(edge)
    if any(len(edges) != 2 for edges in along.values()):
        return None  # a rim that branches is not a rim we can fill

    # Walk in coordinate order. bmesh elements hash by their address, so
    # plain dict order starts each rim at a different vertex from run to run,
    # and the same cut then comes out capped one way and not the next.
    ordered = sorted(along, key=lambda v: (round(v.co.x, 9), round(v.co.y, 9),
                                           round(v.co.z, 9)))
    loops, seen = [], set()
    for start in ordered:
        if start in seen:
            continue
        loop, vert, came_from = [], start, None
        while vert is not None and vert not in seen:
            seen.add(vert)
            loop.append(vert)
            step = next((e for e in along[vert] if e is not came_from), None)
            if step is None:
                break
            came_from, vert = step, step.other_vert(vert)
        if len(loop) >= 3:
            loops.append(loop)
    return loops


def _ear_clip(flat):
    """Triangulate a simple polygon given as (u, v) pairs. Indices, or None.

    Written out rather than handed to a fill operator because the rim has to
    be closed completely or not at all: a fill that gives up half way leaves a
    hole that only shows up as a part that will not print.
    """
    count = len(flat)
    if count < 3:
        return None
    span = max(max(u for u, _v in flat) - min(u for u, _v in flat),
               max(v for _u, v in flat) - min(v for _u, v in flat), 1e-12)
    # Cross products here are twice an area, so the tolerance is squared too.
    tiny = span * span * 1e-9

    twice_area = sum(
        flat[i][0] * flat[(i + 1) % count][1]
        - flat[(i + 1) % count][0] * flat[i][1] for i in range(count))
    order = list(range(count))
    if twice_area < 0.0:
        order.reverse()

    def cross(o, a, b):
        return ((flat[a][0] - flat[o][0]) * (flat[b][1] - flat[o][1])
                - (flat[a][1] - flat[o][1]) * (flat[b][0] - flat[o][0]))

    tris = []
    while len(order) > 3:
        size = len(order)
        # Only a corner that turns the wrong way can sit inside an ear, so
        # those are the only ones worth testing against — and on a line drawn
        # round a limb there are usually none at all.
        reflex = {order[k] for k in range(size)
                  if cross(order[k - 1], order[k],
                           order[(k + 1) % size]) < -tiny}
        for k in range(size):
            a, b, c = order[k - 1], order[k], order[(k + 1) % size]
            if b in reflex:
                continue
            # A stretch of the line that runs straight gives corners with no
            # turn in them. They are clipped all the same, as slivers lying
            # flat in the cap: dropping them instead would leave the rim edge
            # beside them with nothing on the other side, and the part open.
            if any(p not in (a, b, c)
                   and cross(a, b, p) > tiny and cross(b, c, p) > tiny
                   and cross(c, a, p) > tiny for p in reflex):
                continue
            tris.append((a, b, c))
            order.pop(k)
            break
        else:
            return None  # no ear anywhere: the rim crosses itself
    tris.append(tuple(order))
    return tris


def _aligned(loop, positions):
    """`loop` rotated (and reversed if need be) to sit against `positions`.

    The two rims hold the very same points — they were made by parting one
    edge from itself — so this only has to find where one starts in the other
    and which way round it runs.
    """
    count = len(loop)
    if count != len(positions):
        return None
    # Anything closer than a fraction of the gap between neighbouring points
    # is the same point; there is nothing else it could be.
    spacing = min((positions[i] - positions[i - 1]).length
                  for i in range(count))
    close = max(spacing * 0.25, 1e-12) ** 2

    start = min(range(count),
                key=lambda i: (loop[i].co - positions[0]).length_squared)
    for step in (1, -1):
        candidate = [loop[(start + step * i) % count] for i in range(count)]
        if all((v.co - p).length_squared < close
               for v, p in zip(candidate, positions)):
            return candidate
    return None


def _cap_all(pieces, normal):
    """Fill the holes the cut left. True if every piece came out closed.

    Both halves are filled from **one** triangulation of the rim. Filling them
    separately is the obvious thing and it is wrong: the rim is a loop in
    space, not a flat one, and two triangulations of the same loop span
    different surfaces — so the parts no longer meet, and between them they no
    longer add up to the model.

    The rim lies on the model along the drawn line, and that line is a simple
    loop seen down the cut's own normal, so flattening it that way and filling
    the polygon cannot produce a cap that folds.
    """
    axis = normal.normalized() if normal.length > 1e-9 else Vector((0, 0, 1))
    across = axis.orthogonal().normalized()
    other = axis.cross(across).normalized()

    opened = []
    for piece in pieces:
        bm = bmesh.new()
        bm.from_mesh(piece.data)
        loops = _boundary_loops(bm)
        if loops is None:
            bm.free()
            for _p, spare, _l in opened:
                spare.free()
            return False
        opened.append((piece, bm, loops))

    plans, ok = [], True
    for _piece, bm, loops in opened:
        for loop in loops:
            centre = sum((v.co for v in loop), Vector()) / len(loop)
            spread = max((v.co - centre).length for v in loop)
            plan = next((p for p in plans
                         if len(p[0]) == len(loop)
                         and (p[2] - centre).length < spread * 1e-4), None)
            if plan is None:
                flat = [(v.co.dot(across), v.co.dot(other)) for v in loop]
                tris = _ear_clip(flat)
                if tris is None:
                    ok = False
                    break
                plans.append(([v.co.copy() for v in loop], tris, centre))
                ordered = loop
            else:
                ordered = _aligned(loop, plan[0])
                tris = plan[1]
                if ordered is None:
                    ok = False
                    break
            for a, b, c in tris:
                try:
                    bm.faces.new((ordered[a], ordered[b], ordered[c]))
                except ValueError:
                    pass  # that triangle is already there
        if not ok:
            break

    if ok:
        ok = all(not any(len(e.link_faces) == 1 for e in bm.edges)
                 for _p, bm, _l in opened)
    if ok:
        for piece, bm, _loops in opened:
            bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
            bm.to_mesh(piece.data)
    for _p, bm, _l in opened:
        bm.free()
    return ok


def _part_and_cap(bm, layer, seam, normal, scene, collection):
    """Split the two sides apart and cap each. Returns the pieces."""
    bmesh.ops.delete(bm, geom=[f for f in bm.faces if f[layer]],
                     context='FACES')
    parting = [edge for edge in seam
               if edge.is_valid and len(edge.link_faces) == 2]
    bmesh.ops.split_edges(bm, edges=parting)

    data = bpy.data.meshes.new("PartPin_Cut")
    bm.to_mesh(data)
    bm.free()
    whole = bpy.data.objects.new("PartPin_Cut", data)
    collection.objects.link(whole)
    bpy.context.view_layer.update()

    pieces = core.split_loose(whole)
    _cap_all(pieces, normal)
    return pieces


def cut_object(obj, rings, normals, normal, scene, collection=None):
    """Sever `obj` along the ring. Returns (pieces, problem).

    `pieces` is what it fell into when it worked and None when it did not;
    the object itself is left untouched either way.
    """
    collection = collection or scene.collection
    for height in BAND_LADDER:
        try:
            found = _cut_surface(obj, rings, normals,
                                 core.bbox_diagonal(obj) * height, scene)
        except Exception:
            found = None
        if found is None:
            continue
        bm, layer, seam = found
        try:
            pieces = _part_and_cap(bm, layer, seam, normal, scene, collection)
        except Exception:
            continue
        # A rung is only accepted on the evidence that it worked: the model
        # in pieces, every one of them closed. Anything else and the band
        # gets another go, taller.
        if len(pieces) >= 2 and all(core.mesh_issues(p) == (0, 0)
                                    for p in pieces):
            return pieces, None
        for piece in pieces:
            core.remove_object(piece)
    return None, ("the line could not be cut cleanly into the model's "
                  "surface here")


def line_rings(cut, target):
    """(rings, normals, cut normal) for a cut's lines, or three Nones."""
    surface.refit_frame(cut)
    usable, problem, _warning = surface.loop_quality(cut, min_alignment=0.0)
    if problem is not None:
        return None, None, None
    rings = [ring for ring in surface.line_samples(cut, target, usable)
             if len(ring) >= 3]
    if not rings:
        return None, None, None
    normal = cut.matrix_world.to_quaternion() @ Vector((0.0, 0.0, 1.0))
    return rings, [ring_normals(ring, target) for ring in rings], normal
