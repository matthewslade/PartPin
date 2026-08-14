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

# What went wrong, when a cut will not go through. Kept apart because they
# want completely different things of the user, and one message covering all
# three tells two thirds of them the wrong thing.
APART = 'APART'            # the seam came apart at the spots reported
UNENCLOSED = 'UNENCLOSED'  # the line does not ring-fence anything
UNCAPPED = 'UNCAPPED'      # it parted, but the halves would not close up
BUSY = 'BUSY'              # Blender would not run the cut at all just then

# Marks the band's faces so they can be told from the model's after the
# intersect has rebuilt the mesh around them.
BAND_TAG = "pp_band"

# How much taller the band is made where a seam came apart, and how far along
# the line that reaches, in multiples of the spacing between samples. A seam
# only ever fails locally — at one crease the band could not bridge — so the
# repair is local too: raising the whole band instead is what makes it reach
# through a thin fin somewhere else entirely.
REPAIR_BOOST = 8.0
REPAIR_REACH = 5.0
# Rounds of repair. Each one works from where the *last* attempt came apart,
# not where the first did, and reaches further and stands taller than the one
# before — a crease that shrugged off one go may still give way to three.
REPAIR_ROUNDS = 3

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


def _work_object(obj, rings, normals, heights, scene):
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

    # A part cut once already carries this tag, and asking for a second layer
    # of the same name gets one under a different name — leaving the stale one
    # to be read instead, every face reading 0, the band never told apart from
    # the model, and the cut quietly doing nothing. Clear it out first.
    stale = bm.faces.layers.int.get(BAND_TAG)
    if stale is not None:
        bm.faces.layers.int.remove(stale)
    layer = bm.faces.layers.int.new(BAND_TAG)
    for face in bm.faces:
        face[layer] = 0

    # One band per line: a cut can have several, and every one of them has to
    # be cut for the piece they ring-fence between them to come away.
    for ring, along, tall in zip(rings, normals, heights):
        outer = [bm.verts.new(p + n * h)
                 for p, n, h in zip(ring, along, tall)]
        inner = [bm.verts.new(p - n * h)
                 for p, n, h in zip(ring, along, tall)]
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


class CutterBusy(RuntimeError):
    """Blender would not let the cut run — a context problem, not a geometry
    one. Worth telling apart: the line is not what needs changing."""


def _uniform(rings, height):
    return [[height] * len(ring) for ring in rings]


def _repaired(rings, heights, spots, round_no=0):
    """The same band, stood taller where the seam came apart.

    Returns None if nothing needed raising.
    """
    if not spots:
        return None
    boost = REPAIR_BOOST * (round_no + 1)
    raised, touched = [], False
    for ring, tall in zip(rings, heights):
        spacing = surface.loop_length(ring) / max(len(ring), 1)
        reach = spacing * REPAIR_REACH * (round_no + 1)
        along = []
        for point, height in zip(ring, tall):
            near = min((point - spot).length for spot in spots)
            if near >= reach:
                along.append(height)
                continue
            # Tallest right at the trouble, tapering back to the band's own
            # height, so the join between the two is not a step.
            lean = 1.0 - near / reach
            along.append(height * (1.0 + (boost - 1.0) * lean))
            touched = True
        raised.append(along)
    return raised if touched else None


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


def _cut_surface(obj, rings, normals, heights, scene):
    """Cut the object's faces along the lines.

    Returns ((bmesh, layer, seam), loose ends). The first is None unless the
    bands left closed seams parting the faces in two or more — which is the
    whole test, and it is exact: a seam either closes or it does not. The
    loose ends are where it did not, in world space, which is exactly where
    the line needs moving.
    """
    work = _work_object(obj, rings, normals, heights, scene)
    view_layer = bpy.context.view_layer

    # Whatever was active has to be in object mode before anything else is
    # made active, or the mode switch below refuses and the cut comes back
    # looking like a geometry failure. Drawing a cut leaves the cut object
    # active, which is how pressing Create Parts straight afterwards could
    # fail and then work on a second press.
    active = view_layer.objects.active
    if active is not None and active.mode != 'OBJECT':
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except RuntimeError:
            pass
    for other in list(bpy.context.selected_objects):
        other.select_set(False)
    work.select_set(True)
    view_layer.objects.active = work
    bpy.context.tool_settings.mesh_select_mode = (False, False, True)

    try:
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.intersect(mode='SELECT_UNSELECT', separate_mode='NONE',
                               solver='EXACT')
    except RuntimeError as exc:
        core.remove_object(work)
        raise CutterBusy(str(exc))
    finally:
        if work.name in bpy.data.objects and work.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except RuntimeError:
                pass

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
    loose = [vert.co.copy() for vert, count in degree.items() if count != 2]
    if not seam or loose:
        bm.free()
        return None, loose  # the seam is in pieces: the band missed a crease

    if len(_regions(bm, layer, seam)) < 2:
        bm.free()
        return None, []  # the line does not ring-fence anything on this part
    return (bm, layer, seam), []


def _boundary_loops(bm):
    """The open rims of a mesh, each as its vertices in the order they run.

    Only rims that come back round to where they started are returned. A real
    model often arrives with a nick in it somewhere — a stray open edge, a
    vertex where three faces meet — and those open edges have nothing to do
    with the cut. Giving up on the whole mesh because of one of them left the
    seam itself uncapped and both halves open, over a flaw a hundred times
    further away than the cut. They are stepped over instead, and whatever
    damage came in is left exactly as it was found.
    """
    open_edges = [edge for edge in bm.edges if len(edge.link_faces) == 1]
    along = {}
    for edge in open_edges:
        for vert in edge.verts:
            along.setdefault(vert, []).append(edge)
    # A rim only runs cleanly through a vertex with exactly two open edges;
    # anywhere else is damage, and no rim is walked through it.
    clean = {vert for vert, edges in along.items() if len(edges) == 2}

    # Walk in coordinate order. bmesh elements hash by their address, so plain
    # dict order starts each rim at a different vertex from run to run, and
    # the same cut then comes out capped one way and not the next.
    ordered = sorted(clean, key=lambda v: (round(v.co.x, 9), round(v.co.y, 9),
                                           round(v.co.z, 9)))
    loops, seen = [], set()
    for start in ordered:
        if start in seen:
            continue
        loop, vert, came_from = [], start, None
        while vert in clean and vert not in seen:
            seen.add(vert)
            loop.append(vert)
            step = next((e for e in along[vert] if e is not came_from), None)
            if step is None:
                break
            came_from, vert = step, step.other_vert(vert)
        if len(loop) >= 3 and vert is loop[0]:
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

    def side(a, b):
        return ((flat[a][0] - flat[b][0]) ** 2
                + (flat[a][1] - flat[b][1]) ** 2) ** 0.5

    tris = []
    while len(order) > 3:
        size = len(order)
        # Only a corner that turns the wrong way can sit inside an ear, so
        # those are the only ones worth testing against — and on a line drawn
        # round a limb there are usually none at all.
        reflex = {order[k] for k in range(size)
                  if cross(order[k - 1], order[k],
                           order[(k + 1) % size]) < -tiny}
        best, best_score = None, -1.0
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
            # Take the roundest ear going, not the first one found. Always
            # taking the first walks steadily round the loop and hangs every
            # triangle off whichever corner it started at — a fan, which on a
            # rim that is not flat is a cone with its point in the wrong place.
            sides = side(a, b) * side(b, c) * side(c, a)
            score = cross(a, b, c) / sides if sides > 1e-30 else 0.0
            if score > best_score:
                best, best_score = k, score
        if best is None:
            return None  # no ear anywhere: the rim crosses itself
        a, b, c = (order[best - 1], order[best],
                   order[(best + 1) % size])
        tris.append((a, b, c))
        order.pop(best)
    tris.append(tuple(order))
    return tris


# The cap is not filled straight across. Rings of points are stepped inwards
# from the line, each one evened out, drawn in, and settled closer to the
# line's own mean plane than the last. What that gives is a bevel following
# the model round the edge of the cut and a flat floor in the middle of it —
# instead of one surface stretched from the line to a single point on it.
CAP_RINGS = 5          # steps inwards before the middle is filled in
CAP_STEP = 0.16        # how far in each step goes, of the cut's own radius
CAP_THINNING = 0.78    # of its points each step keeps
CAP_SETTLE = 0.45      # of the way onto the cut's own surface each step goes
CAP_EVENING = 2        # evening-out passes per step
CAP_MIN_RING = 12
CAP_BACKOFF = 5        # halvings of a step before the rings stop


def _cap_frame(normal):
    """(across, other, axis) — the cut's own plane, as three directions."""
    axis = normal.normalized() if normal.length > 1e-9 else Vector((0, 0, 1))
    across = axis.orthogonal().normalized()
    return across, axis.cross(across).normalized(), axis


def _flat_area(ring):
    """Area the ring encloses, seen down the cut's normal."""
    count = len(ring)
    return abs(sum(ring[i].x * ring[(i + 1) % count].y
                   - ring[(i + 1) % count].x * ring[i].y
                   for i in range(count)) * 0.5)


def _step_in(ring, distance):
    """Every point moved in along the ring's own inward normal.

    Not a scale about the middle. A cut whose line runs close in on one side
    and far out on another — a collar with a fin standing off it — shrinks
    unevenly under a scale, and the smaller copy crosses back out through the
    larger one where the line dips in. Stepping in along the normals keeps the
    ring the same distance inside all the way round, whatever shape it is.
    """
    count = len(ring)
    twice_area = sum(ring[i].x * ring[(i + 1) % count].y
                     - ring[(i + 1) % count].x * ring[i].y
                     for i in range(count))
    facing = 1.0 if twice_area > 0.0 else -1.0

    def inward_of(a, b):
        span = Vector((b.x - a.x, b.y - a.y))
        if span.length < 1e-12:
            return None
        span.normalize()
        return Vector((-span.y, span.x)) * facing

    stepped = []
    for i, point in enumerate(ring):
        before = inward_of(ring[i - 1], point)
        after = inward_of(point, ring[(i + 1) % count])
        if before is None or after is None:
            stepped.append(point.copy())
            continue
        # Move to where the two neighbouring stretches end up once both have
        # come in, not straight in from the point itself. On a corner those
        # are different places: coming straight in leaves the corner short of
        # where its own edges went, and the ring doubles back over itself
        # there. Very sharp corners are held back rather than sent flying.
        bisector = before + after
        if bisector.length < 1e-9:
            stepped.append(point.copy())
            continue
        bisector.normalize()
        lean = max(bisector.dot(before), 0.25)
        stepped.append(Vector((point.x + bisector.x * distance / lean,
                               point.y + bisector.y * distance / lean,
                               point.z)))
    return stepped


def _next_ring(current, distance, evening):
    """One step in from `current`: thinned, evened out, stepped in, settled
    flatter. None if it will not hold at that distance."""
    count = int(len(current) * CAP_THINNING)
    if count < CAP_MIN_RING:
        return None
    evened = surface.resample_loop(current, min(count, len(current)),
                                   cyclic=True)
    for _pass in range(evening):
        evened = [(evened[i - 1] + p * 2.0 + evened[(i + 1) % len(evened)])
                  * 0.25 for i, p in enumerate(evened)]
    ring = _step_in(evened, distance)
    if surface.polygon_self_intersects([(p.x, p.y) for p in ring]):
        return None
    return ring


def _bridge(outer, outer_ids, inner, inner_ids):
    """Triangles filling the band between two rings, matched round them.

    Matched by where each point sits along the **outer** ring. The inner one
    was laid out by walking the outer at even steps, so that is where its
    points belong; going by the inner's own spacing instead pairs a stretch of
    one with a stretch of the other that is nowhere near it — and where the
    line has a spur, as a collar round a fin does, the band folds over itself.
    """
    def along(points):
        steps = [(points[(i + 1) % len(points)] - p).length
                 for i, p in enumerate(points)]
        total = sum(steps) or 1.0
        marks, walked = [], 0.0
        for step in steps:
            marks.append(walked / total)
            walked += step
        return marks

    n, m = len(outer), len(inner)
    out_at = along(outer)
    in_at = [k / m for k in range(m)]
    tris, i, j = [], 0, 0
    while i < n or j < m:
        next_out = out_at[i + 1] if i + 1 < n else 1.0
        next_in = in_at[j + 1] if j + 1 < m else 1.0
        if j >= m or (i < n and next_out <= next_in):
            tris.append((outer_ids[i], outer_ids[(i + 1) % n],
                         inner_ids[j % m]))
            i += 1
        else:
            tris.append((outer_ids[i % n], inner_ids[(j + 1) % m],
                         inner_ids[j % m]))
            j += 1
    return tris


def _band_tiles(outer, inner, band, points):
    """Whether a band covers the space between two rings once and once only.

    Both rings lie over the cut's plane, so the band between them has to come
    to exactly the area between them: any less and there is a hole, any more
    and two of its triangles overlap, which is a cap doubling back on itself.
    Checked band by band, so a line that only allows two rings keeps those two
    instead of losing the lot.
    """
    want = _flat_area(outer) - _flat_area(inner)
    if want <= 0.0:
        return False
    covered = 0.0
    for a, b, c in band:
        pa, pb, pc = points[a], points[b], points[c]
        covered += abs((pb.x - pa.x) * (pc.y - pa.y)
                       - (pb.y - pa.y) * (pc.x - pa.x))
    return abs(covered * 0.5 - want) <= want * 1e-6


def _settle_ring(ring, settle, centre, across, other, axis):
    """Move a ring part of the way onto the cut's own surface."""
    if settle is None:
        for point in ring:
            point.z *= 1.0 - CAP_SETTLE
        return
    landed = settle([centre + across * p.x + other * p.y + axis * p.z
                     for p in ring])
    for point, target in zip(ring, landed):
        point.z += ((target - centre).dot(axis) - point.z) * CAP_SETTLE


def _cap_plan(rim_world, normal, settle=None):
    """How to fill one rim. Returns (extra world points, triangles).

    Triangles index the rim's own points first, then the extra ones.

    `settle` puts world points onto the cut's own surface. Each ring inwards
    goes part of the way there, so the cap leaves the line following the model
    and arrives at the surface the cut is *for* — flat, unless the surface has
    been shaped, and either way the surface the connectors were placed on.
    """
    across, other, axis = _cap_frame(normal)
    centre = sum(rim_world, Vector()) / len(rim_world)
    rim = [Vector(((p - centre).dot(across), (p - centre).dot(other),
                   (p - centre).dot(axis))) for p in rim_world]

    reach = (_flat_area(rim) / 3.141592653589793) ** 0.5 * CAP_STEP
    tris, points = [], list(rim)
    outer, outer_ids = rim, list(range(len(rim)))
    for _step in range(CAP_RINGS):
        # How far in a step can go depends on the line: a corner cannot be
        # brought in further than it is round without turning inside out, and
        # a line with a spur in it folds the band over itself long before
        # that. So each step is offered and checked, shortened until it
        # holds, and failing that evened out less — a line with a spur needs
        # the evening left off, because that alone moves it enough sideways
        # to fold the band. If none of it holds, the rings stop where they
        # are and the rest is filled straight across.
        made = None
        for evening in range(CAP_EVENING, -1, -1):
            distance = reach
            for _try in range(CAP_BACKOFF):
                ring = _next_ring(outer, distance, evening)
                if ring is not None:
                    _settle_ring(ring, settle, centre, across, other, axis)
                    ring_ids = [len(points) + k for k in range(len(ring))]
                    band = _bridge(outer, outer_ids, ring, ring_ids)
                    if _band_tiles(outer, ring, band, points + ring):
                        made = (ring, ring_ids, band)
                        break
                distance *= 0.5
            if made is not None:
                break
        if made is None:
            break
        ring, ring_ids, band = made
        tris.extend(band)
        points.extend(ring)
        outer, outer_ids = ring, ring_ids

    middle = _ear_clip([(p.x, p.y) for p in outer])
    if middle is None:
        return None, None
    tris.extend(tuple(outer_ids[k] for k in tri) for tri in middle)

    extra = [centre + across * p.x + other * p.y + axis * p.z
             for p in points[len(rim):]]
    return extra, tris


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


def _cap_all(pieces, normal, settle=None):
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
    opened = []
    for piece in pieces:
        bm = bmesh.new()
        bm.from_mesh(piece.data)
        opened.append((piece, bm, _boundary_loops(bm)))

    plans, ok = [], True
    for _piece, bm, loops in opened:
        for loop in loops:
            centre = sum((v.co for v in loop), Vector()) / len(loop)
            spread = max((v.co - centre).length for v in loop)
            plan = next((p for p in plans
                         if len(p[0]) == len(loop)
                         and (p[3] - centre).length < spread * 1e-4), None)
            if plan is None:
                rim = [v.co.copy() for v in loop]
                extra, tris = _cap_plan(rim, normal, settle)
                if tris is None:
                    ok = False
                    break
                plans.append((rim, extra, tris, centre))
                ordered = loop
            else:
                rim, extra, tris = plan[0], plan[1], plan[2]
                ordered = _aligned(loop, rim)
                if ordered is None:
                    ok = False
                    break
            # The rings are worked out once and built again here from the same
            # positions, so both halves get the very same cap and still meet.
            made = list(ordered) + [bm.verts.new(p) for p in extra]
            for a, b, c in tris:
                try:
                    bm.faces.new((made[a], made[b], made[c]))
                except ValueError:
                    pass  # that triangle is already there
        if not ok:
            break

    if ok:
        # Every rim that *could* be filled has to have been. What is left open
        # is damage the model came in with, which is not this cut's to mend.
        ok = all(not _boundary_loops(bm) for _p, bm, _l in opened)
    if ok:
        for piece, bm, _loops in opened:
            bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
            bm.to_mesh(piece.data)
    for _p, bm, _l in opened:
        bm.free()
    return ok


def _part_and_cap(bm, layer, seam, normal, scene, collection, settle=None):
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
    _cap_all(pieces, normal, settle)
    _drop_tag(pieces)
    return pieces


def _drop_tag(pieces):
    """Take the band's marking off the finished parts.

    A part is a model in its own right and can be cut again; leaving the tag
    on it would have the next cut read this one's markings.
    """
    for piece in pieces:
        marking = piece.data.attributes.get(BAND_TAG)
        if marking is not None:
            piece.data.attributes.remove(marking)


def cut_object(obj, rings, normals, normal, scene, collection=None,
               settle=None):
    """Sever `obj` along the ring. Returns (pieces, problem).

    Returns (pieces, problem, spots). `pieces` is what it fell into when it
    worked and None when it did not; the object itself is left untouched
    either way. `spots` are the places along the line the cut could not be
    carried through — world positions, for marking on the model.
    """
    collection = collection or scene.collection
    # Everything below reads the *evaluated* mesh, and an object made a moment
    # ago has no evaluated form until the depsgraph catches up. Without this,
    # the first cut after drawing one can be measured against nothing.
    bpy.context.view_layer.update()
    diagonal = core.bbox_diagonal(obj)
    spots, trouble = [], APART
    # What the model arrives with. A cut is judged on leaving it no worse, not
    # on making it perfect: demanding perfect means a model with one nick in it
    # a long way from the line can never be cut at all.
    was = core.mesh_issues(obj)

    def attempts():
        """Bands to try: the plain ladder first, then rounds of it mended
        wherever the attempts so far came apart."""
        for height in BAND_LADDER:
            yield _uniform(rings, diagonal * height)
        for round_no in range(REPAIR_ROUNDS):
            mended_any = False
            for height in BAND_LADDER:
                mended = _repaired(rings, _uniform(rings, diagonal * height),
                                   spots, round_no)
                if mended is not None:
                    mended_any = True
                    yield mended
            if not mended_any:
                return  # nothing came apart, so there is nothing to mend

    for heights in attempts():
        try:
            found, loose = _cut_surface(obj, rings, normals, heights, scene)
        except CutterBusy:
            # Nothing about the line will fix this, and trying every rung
            # against a context that will not have it wastes a minute.
            return None, BUSY, []
        except Exception:
            found, loose = None, []
        if found is None:
            # Keep the closest thing to a working cut anything managed, since
            # that is the most useful account of where the trouble is, and
            # what the next round of repair stands the band taller around.
            if loose and (not spots or len(loose) < len(spots)):
                spots, trouble = loose, APART
            elif not loose and not spots:
                trouble = UNENCLOSED
            continue
        bm, layer, seam = found
        try:
            pieces = _part_and_cap(bm, layer, seam, normal, scene,
                                   collection, settle)
        except Exception:
            trouble = UNCAPPED
            continue
        # A rung is only accepted on the evidence that it worked: the model
        # in pieces, and between them no more open or non-manifold edges than
        # it had to begin with. Anything else and the band gets another go.
        now = [sum(counts) for counts in
               zip(*(core.mesh_issues(p) for p in pieces))]
        if len(pieces) >= 2 and now[0] <= was[0] and now[1] <= was[1]:
            return pieces, None, []
        # It parted but would not close up: that is the cap's doing, not the
        # line's, and saying "your line encloses nothing" would be a lie.
        trouble = UNCAPPED
        for piece in pieces:
            core.remove_object(piece)
    return None, trouble, spots


def line_rings(cut, target):
    """What the cutter needs of a cut's lines, or four Nones.

    Returns (rings, their normals, the cut normal, settle), where `settle`
    puts world points onto the cut's own surface — the one the connectors sit
    on, and the one the middle of the cap is drawn down to.
    """
    surface.refit_frame(cut)
    usable, problem, _warning = surface.loop_quality(cut, min_alignment=0.0)
    if problem is not None:
        return None, None, None, None
    rings = [ring for ring in surface.line_samples(cut, target, usable)
             if len(ring) >= 3]
    if not rings:
        return None, None, None, None
    normal = cut.matrix_world.to_quaternion() @ Vector((0.0, 0.0, 1.0))

    field = surface.field_for(cut, usable)
    matrix = cut.matrix_world
    inverse = matrix.inverted()

    def settle(points):
        local = [inverse @ p for p in points]
        heights = field.eval_many([(p.x, p.y) for p in local])
        return [matrix @ Vector((p.x, p.y, h))
                for p, h in zip(local, heights)]

    return (rings, [ring_normals(ring, target) for ring in rings],
            normal, settle)
