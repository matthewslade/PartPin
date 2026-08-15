"""The localized cut: sever the model along the line drawn on it.

Nothing here synthesises a surface to cut with. The model's own faces are cut
along the drawn line, the two sides of that cut are parted, and each is capped
with the polygon the line describes. Both halves are therefore built from
geometry that already lay on the model, which is what makes the seam land
exactly on the line rather than near it.

How it works:

1. The line, as a dense ring of points on the surface — `surface.line_rings`,
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
# the line that reaches, as a share of the line's own length. A seam only ever
# fails locally — at one crease the band could not bridge — so the repair is
# local too: raising the whole band instead is what makes it reach through a
# thin fin somewhere else entirely.
#
# Measured against the line rather than against the spacing between its
# samples: the line is walked across the model's own faces, so how many
# samples it has is a fact about the model's density, and a repair that
# reaches "five samples" covers a hand's breadth of one model and most of the
# way round another.
REPAIR_BOOST = 8.0
REPAIR_REACH = 0.04
# Rounds of repair. Each one works from where the *last* attempt came apart,
# not where the first did, and reaches further and stands taller than the one
# before — a crease that shrugged off one go may still give way to three.
REPAIR_ROUNDS = 3

# The most bands a cut can ever try: the plain ladder, then a round of it per
# round of repair. Only used to say how far along a cut is.
MOST_ATTEMPTS = len(BAND_LADDER) * (1 + REPAIR_ROUNDS)

# How close two samples may be before one of them is dropped, as a share of
# the average spacing along the line. Two samples in the same place make a
# band quad with no area, and no intersection can be found against nothing —
# the seam simply breaks there, at every height, which is a red mark that will
# not shift however much the band is raised.
BAND_MIN_GAP = 0.15

# How far to one side the band's copy of the line is moved, as a share of the
# model's size, to keep it off the model's own edges. See `_off_the_edges`.
EDGE_CLEAR = 1e-6

# And how much longer than the rest a gap may be before it is filled in.
# Projecting onto a dense model can leave two neighbouring samples far apart —
# across a crease, or over a fold — and a band quad spanning that cannot
# follow the surface between them, so the seam breaks there too. Both ends of
# this are the same complaint: a band only works on an evenly walked line.
BAND_MAX_GAP = 1.6

# What counts as the line doubling back on itself: two stretches of it closer
# than this share of the model's size, this far apart along the line, with the
# surface facing the same way at both.
#
# That last condition is what makes this usable. Proximity alone flags a line
# drawn across a thin fin, where the two sides are legitimately a fin's
# thickness apart — and marking a cut that works is worse than marking
# nothing. On a fin the two stretches sit on opposite faces and the surface
# faces opposite ways there (180 degrees apart, measured). On a hairpin both
# stretches lie on the same patch and it faces the same way (1 to 23 degrees,
# measured on two models that would not cut).
#
# The line is walked across the model's own faces now, so it cannot double
# back between one anchor and the next at all: a hairpin can only come from
# anchors dragged into one, where the two sides lie on the same patch and
# face within a few degrees of each other. What is left in between is the
# root of a fin, where a line coming round it reads about 40 degrees on a
# cut that works — so PINCH_ALIKE sits between that and the 23 the real ones
# came in at, rather than halfway to the fin's own 180.
#
# PINCH_APART is how far apart two stretches have to be *along the line*
# before being close in space says anything, as a multiple of that closeness.
# Counted in samples instead it said nothing at all on a dense model: a
# 441,616-face sculpt gives a ring of two thousand samples, six of them apart
# is a fraction of a millimetre, and every neighbour within a hundredth of the
# model reads as a doubling back — twelve thousand of them on a line that cuts
# perfectly well.
PINCH_NEAR = 0.01
PINCH_APART = 3.0
PINCH_ALIKE = 30.0


def hairpins(ring, along, diagonal):
    """Where the line doubles back on itself, as world positions.

    Bucketed by position rather than compared pair by pair: every sample
    against every other is four million comparisons on a dense model, and
    this runs after every drag.
    """
    count = len(ring)
    if count < 8:
        return []
    near = diagonal * PINCH_NEAR
    steps = [(ring[(i + 1) % count] - p).length for i, p in enumerate(ring)]
    total = sum(steps)
    apart = near * PINCH_APART
    if total <= apart * 2.0:
        return []
    walked, acc = [0.0] * count, 0.0
    for i in range(count - 1):
        acc += steps[i]
        walked[i + 1] = acc

    cell = max(near, 1e-12)
    buckets = {}
    for i, point in enumerate(ring):
        buckets.setdefault((int(point.x // cell), int(point.y // cell),
                            int(point.z // cell)), []).append(i)

    found = []
    for i, point in enumerate(ring):
        home = (int(point.x // cell), int(point.y // cell),
                int(point.z // cell))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for j in buckets.get((home[0] + dx, home[1] + dy,
                                          home[2] + dz), ()):
                        if j <= i:
                            continue
                        gap = abs(walked[j] - walked[i])
                        if min(gap, total - gap) <= apart:
                            continue
                        if (point - ring[j]).length >= near:
                            continue
                        if along[i].angle(along[j], 0.0) * 57.29578 \
                                < PINCH_ALIKE:
                            found.append((point + ring[j]) * 0.5)
    return found


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


def _thinned(ring):
    """The ring with samples that crowd their neighbour dropped.

    A dense model projects two nearby samples onto the same feature, and they
    come back as the same point or near enough. Dropping one moves the line by
    a fraction of the space between samples — nothing — and it is the
    difference between a seam that closes and one that does not.
    """
    if len(ring) < 4:
        return ring
    least = surface.loop_length(ring) / len(ring) * BAND_MIN_GAP
    kept = [ring[0]]
    for point in ring[1:]:
        if (point - kept[-1]).length >= least:
            kept.append(point)
    # The ring closes, so the last one has to clear the first as well.
    while len(kept) > 3 and (kept[-1] - kept[0]).length < least:
        kept.pop()
    return kept if len(kept) >= 3 else ring


def _filled(ring, target, rounds=4):
    """The ring with extra samples put into gaps far longer than the rest."""
    for _round in range(rounds):
        gaps = sorted((ring[(i + 1) % len(ring)] - p).length
                      for i, p in enumerate(ring))
        if not gaps:
            return ring
        allowed = gaps[len(gaps) // 2] * BAND_MAX_GAP
        if allowed <= 0.0 or gaps[-1] <= allowed:
            return ring
        grown = []
        for i, point in enumerate(ring):
            grown.append(point)
            after = ring[(i + 1) % len(ring)]
            if (after - point).length > allowed:
                grown.append(surface.project_to_surface(
                    target, (point + after) * 0.5))
        ring = grown
    return ring


def _select_model_not_band(data):
    """Select the model's faces and leave the band's unselected.

    Walked element by element in Python this is most of what a cut costs: a
    441,616-face model is that many round trips through RNA, three times over,
    for every band the ladder tries. Read and written in bulk it is a handful
    of array operations.
    """
    import numpy as np

    faces = len(data.polygons)
    tags = np.empty(faces, dtype=np.int32)
    data.attributes[BAND_TAG].data.foreach_get("value", tags)
    on_model = tags == 0
    data.polygons.foreach_set("select", on_model)

    # A vertex counts as selected when a model face uses it, and an edge when
    # both of its vertices are — which is what walking the polygons and then
    # the edges came to.
    totals = np.empty(faces, dtype=np.int32)
    data.polygons.foreach_get("loop_total", totals)
    corners = np.empty(len(data.loops), dtype=np.int32)
    data.loops.foreach_get("vertex_index", corners)
    chosen = np.zeros(len(data.vertices), dtype=bool)
    chosen[corners[np.repeat(on_model, totals)]] = True
    data.vertices.foreach_set("select", chosen)

    ends = np.empty(len(data.edges) * 2, dtype=np.int32)
    data.edges.foreach_get("vertices", ends)
    ends = ends.reshape(-1, 2)
    data.edges.foreach_set("select", chosen[ends[:, 0]] & chosen[ends[:, 1]])


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
    _select_model_not_band(data)

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
        reach = surface.loop_length(ring) * REPAIR_REACH * (round_no + 1)
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
    """How many of the model's faces are in each piece the seam makes.

    Counted rather than collected: on a 441,616-face model the faces
    themselves are half a million Python references built for the sake of
    asking how many groups there are.
    """
    seen, sizes = set(), []
    for face in bm.faces:
        if face[layer] or face in seen:
            continue
        stack, size = [face], 0
        seen.add(face)
        while stack:
            current = stack.pop()
            size += 1
            for edge in current.edges:
                if edge in seam:
                    continue
                for other in edge.link_faces:
                    if other[layer] or other in seen:
                        continue
                    seen.add(other)
                    stack.append(other)
        sizes.append(size)
    return sizes


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
CAP_SETTLE = 0.45      # of the way onto the line's own plane each step goes
CAP_EVENING = 2        # evening-out passes per step
CAP_MIN_RING = 12
CAP_BACKOFF = 5        # halvings of a step before the rings stop

# The most points a ring inside the rim is given. The rim itself is as dense
# as the model's faces make it — a thousand points on a sculpt — and the
# middle of a cap has no use for that: every step inwards was being offered,
# checked for crossing itself and thrown away at that density, which is an
# O(n²) test seventy-five times over and took half a minute on one cut. It is
# also why none of the steps held: a ring carrying every wobble of the rim
# crosses itself the moment it is drawn in.
CAP_MAX_RING = 96

# How far a band's triangles may come from covering the area between its two
# rings before the band is called folded, as a share of that area.
BAND_SLACK = 1e-2


def cap_normal(rim):
    """Which way to look at a rim to fill it in.

    The area-weighted normal of the loop, not the plane of best fit through
    it. They agree on an even ring and part company on one with an excursion
    in it — a line taken up over a lump and back — where the plane of best
    fit leans over towards the excursion, and seen down *that* the loop can
    cross itself. What has to hold is that the loop is simple seen down this
    direction, which is what the area-weighted normal is for.
    """
    normal = surface.newell_normal(rim)
    if normal.length < 1e-12:
        _origin, normal = surface.fit_plane(rim)
    return normal.normalized()


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
    count = min(int(len(current) * CAP_THINNING), CAP_MAX_RING)
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
    to about the area between them: much less and there is a hole, much more
    and its triangles overlap, which is a cap doubling back on itself.
    Checked band by band, so a line that only allows two rings keeps those two
    instead of losing the lot.

    "About" and not "exactly", because the rim is as dense as the model's own
    faces: a thousand points of a sculpt wobble against their own plane, and
    the area they enclose and the area of the triangles filling them differ in
    the fourth decimal place however sound the band is. Held to the exact
    figure, every band on a real model was thrown away and the cap came out
    flat — while a band that has really folded is out by whole percent.
    """
    want = _flat_area(outer) - _flat_area(inner)
    if want <= 0.0:
        return False
    covered = 0.0
    for a, b, c in band:
        pa, pb, pc = points[a], points[b], points[c]
        covered += abs((pb.x - pa.x) * (pc.y - pa.y)
                       - (pb.y - pa.y) * (pc.x - pa.x))
    return abs(covered * 0.5 - want) <= want * BAND_SLACK


def _settle_ring(ring):
    """Move a ring part of the way onto the line's own plane.

    Heights here are measured along that plane's normal from a point on it,
    so drawing them towards zero *is* settling onto it.
    """
    for point in ring:
        point.z *= 1.0 - CAP_SETTLE


def _cap_plan(rim_world, normal):
    """How to fill one rim. Returns (extra world points, triangles).

    Triangles index the rim's own points first, then the extra ones.

    Each ring inwards is settled closer to the plane fitted to the rim, so
    the cap leaves the line following the model round the edge of the cut and
    arrives flat in the middle — which is what a spanning surface wants to be,
    and the surface the connectors were placed on. Fitting a plane to the
    finished rim constrains nothing about the line itself.
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
                    _settle_ring(ring)
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
    # is the same point; there is nothing else it could be. Measured on the
    # average gap, not the smallest: a rim carries the odd pair of points
    # almost on top of each other, and going by the smallest leaves a
    # tolerance finer than the two halves agree to — which reads as the two
    # rims being different rims, and neither half gets capped at all.
    spacing = sum((positions[i] - positions[i - 1]).length
                  for i in range(count)) / count
    close = max(spacing * 0.25, 1e-12) ** 2

    start = min(range(count),
                key=lambda i: (loop[i].co - positions[0]).length_squared)
    for step in (1, -1):
        candidate = [loop[(start + step * i) % count] for i in range(count)]
        if all((v.co - p).length_squared < close
               for v, p in zip(candidate, positions)):
            return candidate
    return None


# How close two rim vertices have to be to count as the same one, as a share
# of the model's size.
WELD = 1e-7


def _weld_rim(bm, distance):
    """Merge rim vertices sitting exactly on top of one another.

    Cutting the line into the model's faces leaves the odd pair of vertices
    in the same place — where the line runs along an edge the model already
    has, the solver produces the crossing it was asked for *and* keeps the
    vertex that was there. In the rim those read as edges of no length, and a
    triangle spanning one has two corners the same, so it cannot be built:
    the cap comes out with a handful of holes in it and the part will not
    print. They are the same point by any measure, so merging them takes
    nothing away.

    `distance` is handed in rather than worked out from what is in front of
    it, and this matters: the two halves are the same rim, but one of them is
    the whole body and the other a hand. Measured against each piece's own
    size the two weld differently — 936 vertices against 989 on the model
    this was found on — and rims that no longer hold the same points cannot
    be capped with the same polygon, so both halves came out open.
    """
    rim = [v for v in bm.verts if v.is_boundary]
    if rim:
        bmesh.ops.remove_doubles(bm, verts=rim, dist=distance)


def _cap_all(pieces):
    """Fill the holes the cut left. True if every piece came out closed.

    Both halves are filled from **one** triangulation of the rim. Filling them
    separately is the obvious thing and it is wrong: the rim is a loop in
    space, not a flat one, and two triangulations of the same loop span
    different surfaces — so the parts no longer meet, and between them they no
    longer add up to the model.

    Each rim is filled over the plane fitted to *it*, not to some frame the
    cut carries about with it. A cut with several lines round different
    features has a different plane per line, and one shared frame could only
    ever suit one of them.
    """
    # One distance for every piece, measured on all of them together: they
    # were one model a moment ago and their rims have to stay the same rim.
    corners = [corner for piece in pieces for corner in piece.bound_box]
    span = max(max(max(c[i] for c in corners) - min(c[i] for c in corners)
                   for i in range(3)), 1e-9)

    opened = []
    for piece in pieces:
        bm = bmesh.new()
        bm.from_mesh(piece.data)
        _weld_rim(bm, span * WELD)
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
                extra, tris = _cap_plan(rim, cap_normal(rim))
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


def _part_and_cap(bm, layer, seam, scene, collection):
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
    _cap_all(pieces)
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


def cut_object(obj, rings, normals, scene, collection=None):
    """Sever `obj` along the ring. Returns (pieces, problem, spots).

    The blocking form of `cut_object_steps`, for scripts and tests.
    """
    steps = cut_object_steps(obj, rings, normals, scene, collection)
    while True:
        try:
            next(steps)
        except StopIteration as done:
            return done.value


def cut_object_steps(obj, rings, normals, scene, collection=None):
    """Sever `obj` along the ring, a band at a time.

    Yields (band tried, bands there could be) before each attempt and returns
    (pieces, problem, spots). `pieces` is what it fell into when it worked and
    None when it did not; the object itself is left untouched either way.
    `spots` are the places along the line the cut could not be carried
    through — world positions, for marking on the model.

    Handed out an attempt at a time because on a real model each one takes a
    couple of seconds and there can be twenty of them: whoever is driving this
    gets to say how far along it is, and to stop it.
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

    for number, heights in enumerate(attempts(), start=1):
        yield number, MOST_ATTEMPTS
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
            pieces = _part_and_cap(bm, layer, seam, scene, collection)
        except Exception:
            trouble = UNCAPPED
            continue
        # A rung is only accepted on the evidence that it worked: the model
        # in pieces, and between them all but a few of the edges the seam is
        # made of closed up.
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


def _off_the_edges(ring, along, diagonal):
    """The ring shifted a hair sideways, across the surface.

    The line is walked across the model's own faces, and a line drawn round
    anything extruded — a limb, a handle, a printed part — runs *along* the
    model's own edges for whole stretches of it. The band standing on it then
    contains those edges exactly, the solver is asked to cut a mesh along an
    edge it already has, and what comes back is pairs of vertices in the same
    place: the seam breaks at every one of them. Measured on a 441,800-face
    tube, 86 loose ends the length of the collar.

    Moved a hundred-thousandth of the model to one side, the band crosses the
    faces cleanly and the seam closes exactly. That is a thousandth of the
    tolerance the seam is held to, and a fiftieth of the size of one face on
    a model that dense — the line the user sees does not move at all, only
    the copy the band is built on.
    """
    count = len(ring)
    shifted = []
    for i, point in enumerate(ring):
        tangent = ring[(i + 1) % count] - ring[i - 1]
        sideways = tangent.cross(along[i]) if tangent.length > 1e-12 else None
        if sideways is None or sideways.length < 1e-12:
            shifted.append(point.copy())
            continue
        shifted.append(point + sideways.normalized() * diagonal * EDGE_CLEAR)
    return shifted


def line_rings(cut, target):
    """What the cutter needs of a cut's lines, or two Nones.

    Returns (rings, their normals). The single seam between the line and the
    cutter: everything below this reads a ring of world points on the surface
    and nothing else about how the line is stored.
    """
    # Even the line out before a band is stood on it: drop samples that crowd
    # each other, then fill the gaps that are left far longer than the rest.
    rings = [_filled(_thinned(ring), target)
             for ring in surface.line_rings(cut, target)
             if len(ring) >= 3]
    rings = [ring for ring in rings if len(ring) >= 3]
    if not rings:
        return None, None
    normals = [ring_normals(ring, target) for ring in rings]
    diagonal = core.bbox_diagonal(target)
    return ([_off_the_edges(ring, along, diagonal)
             for ring, along in zip(rings, normals)], normals)
