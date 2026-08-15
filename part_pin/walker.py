"""Walking a cut line across the model's own surface.

A cut line is an ordered ring of anchors sitting on the model, and what runs
between two consecutive anchors is the shortest path *across the surface*
from one to the other. Nothing here leaves the model: every point that comes
back lies exactly on a triangle, and every stretch between two of them lies
inside a single triangle, so the line is on the surface everywhere by
construction rather than by projection.

How one span is walked:

1. Both ends are put onto a triangle (`Surface.locate`).
2. A corridor of triangles is found between them — a shortest-path search
   over the edges the path would cross, kept inside an ellipse with the two
   anchors as its foci. A sculpt has hundreds of thousands of faces and a
   span crosses a handful of them; without that window, and without leaning
   the search towards the far anchor, every drag would search the model.
3. The corridor is unfolded flat and the funnel algorithm draws the straight
   line across it. Without that the path zig-zags from one edge midpoint to
   the next, which crowds the samples the cutting band is built on.
4. What comes back is where that straight line crosses each triangle edge.

If a span cannot be walked — the two anchors are on shells that do not join,
or the search runs out of room — this says so by returning None. It never
falls back to a straight line through space: that is the thing being removed.
"""

import heapq
import math

import bpy
import numpy as np
from mathutils import Vector
from mathutils.geometry import barycentric_transform, closest_point_on_tri

# How far the search may wander, as a multiple of the straight-line distance
# between the two anchors: an edge is looked at only while the sum of its
# distances to both anchors stays inside this — an ellipse with the anchors
# as its foci. The first is enough for an ordinary span across a flattish
# stretch, and starting there is what keeps a drag quick; the wider ones are
# for a span that has to climb over something, and cost a search each.
WINDOWS = (1.15, 1.6, 3.0, 8.0, 24.0)

# How much shorter a pass has to come out than the one before it to be worth
# another. Below this the corridor has settled, and the passes cost more than
# the fraction of a per cent they are chasing.
SETTLED = 0.002

# When a walked span is within this of the straight line between its anchors
# there is no point looking for a shorter one: no path across the surface can
# be shorter than through it, so what has been found is within this of the
# best there is.
GOOD_ENOUGH = 0.01

# A backstop on the widest search, so a span between two anchors that simply
# cannot be joined gives up rather than walking a whole sculpt.
FACE_BUDGET = 120000

# How many times a span may be walked again around the path the last pass
# found. Most settle on the second, which then costs nothing but the search
# that proves it.
PASSES = 4

# The node standing for "the far anchor has been reached".
ARRIVED = -1

# Two points this close together, as a share of the model's size, are the
# same point. Used to drop the crossings that land on top of an anchor.
SAME_POINT = 1e-9

# How far a crossing is kept from either end of the edge it lands on, as a
# share of the model's size. A geodesic turns *at* the model's own vertices,
# so left alone the line runs exactly through them — and the band standing on
# it then cuts the mesh where it already has a vertex, leaving pairs of them
# in the same place along the seam. Measured against the model rather than
# against the edge, so a long edge is not nudged along proportionally.
EDGE_MARGIN = 1e-5


def _area2(a, b, c):
    """Twice the signed area of a 2D triangle: > 0 when it turns left."""
    return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x)


def _turn(a, b, c):
    """Which side of a→b the point c falls on, the way the funnel counts it.

    The funnel widens on one side and narrows on the other, so it reads this
    the other way round from `_area2`. Written out rather than negating one
    with the other, because getting the sense of it backwards does not fail:
    the funnel simply turns at every gate and the "straightened" path comes
    out twice as long as the line it was meant to be.
    """
    return (c.x - a.x) * (b.y - a.y) - (c.y - a.y) * (b.x - a.x)


def _cross(a, b):
    return a.x * b.y - a.y * b.x


def _point_on_segment(p1, p2, point):
    """The point of segment p1p2 closest to a point."""
    span = p2 - p1
    length = span.length_squared
    if length < 1e-18:
        return p1.copy()
    return p1 + span * min(max((point - p1).dot(span) / length, 0.0), 1.0)


def _closest_on_segment(p1, p2, q1, q2):
    """The point of segment p1p2 closest to segment q1q2."""
    span = p2 - p1
    length = span.length_squared
    if length < 1e-18:
        return p1.copy()
    other = q2 - q1
    across = other.length_squared
    if across < 1e-18:
        t = (q1 - p1).dot(span) / length
        return p1 + span * min(max(t, 0.0), 1.0)
    between = span.dot(other)
    denominator = length * across - between * between
    start = (q1 - p1).dot(span)
    if abs(denominator) <= length * across * 1e-12:
        # Parallel: every point of the overlap is as close as every other,
        # so the middle of it is the honest answer. Taking an end instead
        # puts the answer a whole edge away from where the path really runs
        # — and a line drawn along a limb runs parallel to its every edge.
        t = ((q1 + q2) * 0.5 - p1).dot(span) / length
    else:
        t = (start * across - (q1 - p1).dot(other) * between) / denominator
    t = min(max(t, 0.0), 1.0)
    # Pin the other segment too, then come back: with either end clamped the
    # first guess is not the nearest point any more.
    u = min(max(((p1 + span * t) - q1).dot(other) / across, 0.0), 1.0)
    t = min(max(((q1 + other * u) - p1).dot(span) / length, 0.0), 1.0)
    return p1 + span * t


GUIDE_POINTS = 6


def _thinned_guide(path, most=GUIDE_POINTS):
    """A path cut down to a handful of points, evenly along it."""
    if len(path) <= most:
        return list(path)
    step = (len(path) - 1) / (most - 1)
    kept = [path[min(int(round(i * step)), len(path) - 1)]
            for i in range(most)]
    return kept


class Surface:
    """A model's triangles, with what walking across them needs.

    Built once per model and kept, because building it reads the whole mesh
    and a drag re-walks two spans thirty times a second.
    """

    def __init__(self, obj, key):
        self.key = key
        self.name = obj.name
        dg = bpy.context.evaluated_depsgraph_get()
        mesh = bpy.data.meshes.new_from_object(obj.evaluated_get(dg))
        try:
            mesh.calc_loop_triangles()
            count = len(mesh.loop_triangles)
            flat = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
            mesh.vertices.foreach_get("co", flat)
            self.verts = flat.reshape(-1, 3)
            tris = np.empty(count * 3, dtype=np.int64)
            mesh.loop_triangles.foreach_get("vertices", tris)
            self.tris = tris.reshape(-1, 3)
            polys = np.empty(count, dtype=np.int64)
            mesh.loop_triangles.foreach_get("polygon_index", polys)
            self.poly_count = len(mesh.polygons)
        finally:
            bpy.data.meshes.remove(mesh)

        self._build_adjacency()
        self._build_poly_index(polys)
        lo = self.verts.min(axis=0) if len(self.verts) else np.zeros(3)
        hi = self.verts.max(axis=0) if len(self.verts) else np.zeros(3)
        self.diagonal = max(float(np.linalg.norm(hi - lo)), 1e-9)

    # ------------------------------------------------------------------
    # Building
    # ------------------------------------------------------------------

    def _build_adjacency(self):
        """Which triangles share an edge, and which edge they share.

        Kept as flat arrays with a start index per triangle rather than a
        list per triangle: on a 441,616-face model the second is hundreds of
        megabytes of Python objects for something read a few hundred entries
        at a time.
        """
        count = len(self.tris)
        if not count:
            self.adj_start = np.zeros(1, dtype=np.int64)
            self.adj_face = np.zeros(0, dtype=np.int64)
            self.adj_edge = np.zeros((0, 2), dtype=np.int64)
            return
        edges = np.concatenate([self.tris[:, [0, 1]], self.tris[:, [1, 2]],
                                self.tris[:, [2, 0]]])
        edges = np.sort(edges, axis=1)
        owner = np.tile(np.arange(count, dtype=np.int64), 3)
        order = np.lexsort((edges[:, 1], edges[:, 0]))
        edges, owner = edges[order], owner[order]
        # Neighbours are the pairs of identical edges lying next to each
        # other once sorted. An edge shared by three faces — a model with a
        # fin welded onto it — yields two pairs, which is right: all three
        # are reachable from one another.
        same = np.nonzero(np.all(edges[:-1] == edges[1:], axis=1))[0]
        first, second = owner[same], owner[same + 1]
        shared = edges[same]

        src = np.concatenate([first, second])
        dst = np.concatenate([second, first])
        via = np.concatenate([shared, shared])
        order = np.argsort(src, kind='stable')
        src, self.adj_face, self.adj_edge = src[order], dst[order], via[order]
        self.adj_start = np.searchsorted(src, np.arange(count + 1))

    def _build_poly_index(self, polys):
        """Which triangles each of the mesh's faces was cut into."""
        order = np.argsort(polys, kind='stable')
        self.poly_tris = order
        self.poly_start = np.searchsorted(polys[order],
                                          np.arange(self.poly_count + 1))

    # ------------------------------------------------------------------
    # Finding a point on the surface
    # ------------------------------------------------------------------

    def _model(self):
        obj = bpy.data.objects.get(self.name)
        if obj is None:
            return None
        return obj.evaluated_get(bpy.context.evaluated_depsgraph_get())

    def triangle(self, index):
        a, b, c = self.tris[index]
        return (Vector(self.verts[a]), Vector(self.verts[b]),
                Vector(self.verts[c]))

    def _nearest_in_poly(self, point, poly):
        """(distance, triangle, point on it) for one of the mesh's faces."""
        if poly < 0 or poly >= self.poly_count:
            return None
        best = None
        for k in range(int(self.poly_start[poly]),
                       int(self.poly_start[poly + 1])):
            index = int(self.poly_tris[k])
            a, b, c = self.triangle(index)
            near = closest_point_on_tri(point, a, b, c)
            gap = (near - point).length
            if best is None or gap < best[0]:
                best = (gap, index, near)
        return best

    def locate(self, point, hint=-1):
        """(the point put onto the surface, its triangle, its face).

        `hint` is the face the anchor last sat on. It is checked rather than
        trusted — an anchor that has been dragged is somewhere else — but
        when it holds, which is every time the line is rebuilt without being
        edited, this costs nothing.
        """
        close = self.diagonal * 1e-7
        found = self._nearest_in_poly(point, hint)
        if found is not None and found[0] <= close:
            return found[2], found[1], hint
        model = self._model()
        if model is None:
            return None, -1, -1
        ok, near, _normal, poly = model.closest_point_on_mesh(point)
        if not ok:
            return None, -1, -1
        found = self._nearest_in_poly(near, poly)
        if found is None:
            return None, -1, -1
        return found[2], found[1], int(poly)

    # ------------------------------------------------------------------
    # The corridor
    # ------------------------------------------------------------------

    def _crossing_spot(self, k, guide, spots):
        """Where the path would cross this edge, near enough to search on.

        The point of the edge closest to `guide` — the best guess at the path
        so far, the straight line between the anchors to begin with. Searching
        on the triangles' middles instead is what a textbook does, and it
        comes apart on a real model: the side of an extruded limb is one
        triangle six units long, its middle is nowhere near the line, and two
        neighbouring ones are a hair apart by that measure however far the
        path across them really is. Measured here, crossing a long edge a long
        way from the line costs what it actually costs, and the corridor comes
        out hugging the line.
        """
        found = spots.get(k)
        if found is None:
            edge = self.adj_edge[k]
            first = Vector(self.verts[int(edge[0])])
            second = Vector(self.verts[int(edge[1])])
            if len(guide) == 2:
                found = _closest_on_segment(first, second, guide[0], guide[1])
            else:
                # Which stretch of the guide this edge belongs to is settled
                # on the edge's middle, which is a few sums; only the winner
                # is worth the full segment-to-segment answer. Doing it in
                # full against every stretch is what made a drag crawl.
                middle = (first + second) * 0.5
                at, nearest = 0, math.inf
                for i in range(len(guide) - 1):
                    gap = (_point_on_segment(guide[i], guide[i + 1], middle)
                           - middle).length_squared
                    if gap < nearest:
                        at, nearest = i, gap
                found = _closest_on_segment(first, second,
                                            guide[at], guide[at + 1])
            spots[k] = found
        return found

    def _corridor(self, start, goal, a, b, reach, guide):
        """Triangles from `start` to `goal` inside the window, or None.

        Returns (triangles, the edge each consecutive pair shares). Walked
        over the edges the path would cross rather than over the triangles
        themselves, so the cost of a step is the distance it really covers.
        """
        if start == goal:
            return [start], []
        spots = {}
        best, came = {}, {}
        heap = []
        looked = 0

        def offer(k, walked, from_slot, ahead=0.0):
            """Take this way to an edge if it is the shortest one so far.

            Ordered by what has been walked *plus* what is left as the crow
            flies, which is never more than what is left across the surface —
            so the answer is the same as plain Dijkstra's, reached after
            looking at a fraction of the edges. On a dense model that is the
            difference between a drag that keeps up and one that does not.
            """
            if walked < best.get(k, math.inf):
                best[k] = walked
                came[k] = from_slot
                heapq.heappush(heap, (walked + ahead, walked, k))

        def within(spot, face):
            return (face == goal
                    or math.dist(spot, a) + math.dist(spot, b) <= reach)

        for k in range(int(self.adj_start[start]),
                       int(self.adj_start[start + 1])):
            spot = self._crossing_spot(k, guide, spots)
            if within(spot, int(self.adj_face[k])):
                offer(k, (spot - a).length, None, (spot - b).length)

        arrived = None
        while heap:
            _ahead, walked, k = heapq.heappop(heap)
            if k == ARRIVED:
                arrived = came[ARRIVED]
                break
            if walked > best.get(k, math.inf):
                continue
            face = int(self.adj_face[k])
            if face == goal:
                # Getting into the goal's triangle is not getting there: the
                # last leg across it counts too. Left out, a corridor that
                # arrives through a gate at the far end of a long triangle
                # looks cheaper than one that arrives beside the anchor, and
                # the line comes back the length of that triangle and out
                # again.
                offer(ARRIVED, walked + (self._crossing_spot(k, guide, spots)
                                         - b).length, k)
                continue
            looked += 1
            if looked > FACE_BUDGET:
                return None
            here = self._crossing_spot(k, guide, spots)
            came_by = self.adj_edge[k]
            for j in range(int(self.adj_start[face]),
                           int(self.adj_start[face + 1])):
                edge = self.adj_edge[j]
                if edge[0] == came_by[0] and edge[1] == came_by[1]:
                    continue  # straight back out the way it came in
                spot = self._crossing_spot(j, guide, spots)
                if not within(spot, int(self.adj_face[j])):
                    continue
                offer(j, walked + (spot - here).length, k, (spot - b).length)
        if arrived is None:
            return None

        chain = []
        k = arrived
        while k is not None:
            chain.append(k)
            k = came[k]
        chain.reverse()
        faces = [start] + [int(self.adj_face[k]) for k in chain]
        edges = [self.adj_edge[k] for k in chain]
        return faces, edges

    # ------------------------------------------------------------------
    # Unfolding and the funnel
    # ------------------------------------------------------------------

    def _unfold(self, faces, edges):
        """Each corridor triangle's corners, laid out flat side by side.

        Returns a list of {vertex: 2D position}, one per triangle, or None if
        the corridor has a triangle with no area in it.
        """
        first = self.tris[faces[0]]
        p0, p1, p2 = self.triangle(faces[0])
        along = (p1 - p0).length
        if along < 1e-12:
            return None
        x = (p2 - p0).dot(p1 - p0) / along
        y = math.sqrt(max((p2 - p0).length_squared - x * x, 0.0))
        flat = [{int(first[0]): Vector((0.0, 0.0)),
                 int(first[1]): Vector((along, 0.0)),
                 int(first[2]): Vector((x, y))}]

        for step, face in enumerate(faces[1:]):
            previous = flat[-1]
            u, w = int(edges[step][0]), int(edges[step][1])
            if u not in previous or w not in previous:
                return None
            corners = [int(i) for i in self.tris[face]]
            new = next((i for i in corners if i != u and i != w), None)
            if new is None:
                return None
            U, W = previous[u], previous[w]
            span = W - U
            length = span.length
            if length < 1e-12:
                return None
            du = (Vector(self.verts[new]) - Vector(self.verts[u])).length
            dw = (Vector(self.verts[new]) - Vector(self.verts[w])).length
            ex = span / length
            ey = Vector((-ex.y, ex.x))
            across = (du * du - dw * dw + length * length) / (2.0 * length)
            up = math.sqrt(max(du * du - across * across, 0.0))
            # Which side of the shared edge the new corner goes on is read
            # off this triangle's own winding, not off where the last one's
            # far corner ended up. A model that has been through a boolean
            # carries the odd triangle with no area in it, and the corner
            # opposite lands *on* the shared edge — so "the other side from
            # that" is a coin toss, and the toss going the wrong way folds
            # the strip back over itself. The line then comes out running
            # somewhere else entirely on the model.
            forward = any((corners[i], corners[(i + 1) % 3]) == (u, w)
                          for i in range(3))
            placed = U + ex * across + ey * (up if forward else -up)
            flat.append({u: U, w: W, new: placed})
        return flat

    def _portals(self, faces, edges, flat):
        """Each shared edge as (left, right), seen walking the corridor.

        Which of the two ends is on the left is read off the winding of the
        triangle being left, for the same reason the unfolding is: taking it
        from where that triangle's far corner sits gives no answer at all on
        one with no area in it, and a funnel handed its left and right the
        wrong way round hugs the far side of every gate — a path twice as
        long as the one it was asked for, running somewhere else entirely.
        """
        gates = []
        for step in range(len(edges)):
            u, w = int(edges[step][0]), int(edges[step][1])
            here = flat[step]
            if u not in here or w not in here:
                return None
            corners = [int(i) for i in self.tris[faces[step]]]
            forward = any((corners[i], corners[(i + 1) % 3]) == (u, w)
                          for i in range(3))
            if forward:  # the inside of the triangle is left of u → w
                gates.append((here[w], here[u], w, u))
            else:
                gates.append((here[u], here[w], u, w))
        return gates

    def _funnel(self, gates, start, end):
        """The straight line across the unfolded corridor.

        Returns the corners it turns at, each with the gate it turned on.
        """
        stops = [(start, start)] + [(g[0], g[1]) for g in gates] + [(end, end)]
        apex, left, right = stops[0][0], stops[0][0], stops[0][1]
        apex_at = left_at = right_at = 0
        corners = [(apex, 0)]
        i = 1
        rounds = 0
        limit = len(stops) * 4 + 16
        while i < len(stops):
            rounds += 1
            if rounds > limit:
                return None  # not converging; the caller falls back
            pl, pr = stops[i]
            if _turn(apex, right, pr) <= 0.0:
                if (right - apex).length < 1e-12 \
                        or _turn(apex, left, pr) > 0.0:
                    right, right_at = pr, i
                else:
                    corners.append((left, left_at))
                    apex, apex_at = left, left_at
                    left = right = apex
                    left_at = right_at = apex_at
                    i = apex_at + 1
                    continue
            if _turn(apex, left, pl) >= 0.0:
                if (left - apex).length < 1e-12 \
                        or _turn(apex, right, pl) < 0.0:
                    left, left_at = pl, i
                else:
                    corners.append((right, right_at))
                    apex, apex_at = right, right_at
                    left = right = apex
                    left_at = right_at = apex_at
                    i = apex_at + 1
                    continue
            i += 1
        corners.append((end, len(stops) - 1))
        return corners

    def _crossings(self, gates, corners):
        """Where the straight line crosses each gate, as points in space."""
        count = len(gates)
        share = [None] * count
        for j in range(len(corners) - 1):
            here, at_here = corners[j]
            there, at_there = corners[j + 1]
            step = there - here
            for k in range(at_here + 1, min(at_there, count) + 1):
                left, right = gates[k - 1][0], gates[k - 1][1]
                edge = right - left
                turn = _cross(step, edge)
                if abs(turn) < 1e-18:
                    length = edge.length_squared
                    t = 0.0 if length < 1e-18 else \
                        (here - left).dot(edge) / length
                else:
                    t = _cross(step, here - left) / turn
                share[k - 1] = min(max(t, 0.0), 1.0)

        points = []
        for k, gate in enumerate(gates):
            t = share[k]
            if t is None:
                t = 0.5  # the funnel skipped it; the middle is still on the
                # surface, which is all this promises
            a = Vector(self.verts[gate[2]])
            b = Vector(self.verts[gate[3]])
            keep = min((b - a).length and
                       self.diagonal * EDGE_MARGIN / (b - a).length, 0.25)
            points.append(a.lerp(b, min(max(t, keep), 1.0 - keep)))
        return points

    # ------------------------------------------------------------------
    # The walk
    # ------------------------------------------------------------------

    def _in_flat(self, point, face, flat):
        """Where a point of a triangle lands once the corridor is unfolded.

        Drawn a hair towards the middle of its triangle. An anchor often sits
        exactly on one of the model's own edges — every point of a line drawn
        round an extruded limb does — and an end of the funnel sitting on the
        gate it has to pass through leaves the funnel spanning half a turn,
        where which side of it a gate falls on comes down to the last digit.
        It reads one wrong and puts a corner in the line that is not there.
        """
        corners = [Vector((p.x, p.y, 0.0))
                   for p in (flat[int(i)] for i in self.tris[face])]
        a, b, c = self.triangle(face)
        landed = barycentric_transform(point, a, b, c, *corners)
        middle = (corners[0] + corners[1] + corners[2]) / 3.0
        return Vector(landed.lerp(middle, 1e-3).xy)

    def _straighten(self, faces, edges, a, b):
        """The straight line across an unfolded corridor, as points on it."""
        flat = self._unfold(faces, edges)
        gates = self._portals(faces, edges, flat) if flat else None
        if gates:
            start = self._in_flat(a, faces[0], flat[0])
            end = self._in_flat(b, faces[-1], flat[-1])
            turns = self._funnel(gates, start, end)
            if turns is not None:
                return self._crossings(gates, turns)
        # The corridor is sound but could not be straightened. Its own edge
        # midpoints are still a path along the surface, which is worth far
        # more than giving up on the span.
        return [Vector(self.verts[int(e[0])]).lerp(
            Vector(self.verts[int(e[1])]), 0.5) for e in edges]

    def walk(self, a, tri_a, b, tri_b):
        """The surface path strictly between two points, or None.

        Both ends are left out: the caller already has them, and they are the
        anchors the user placed.

        Walked more than once. The first pass searches around the straight
        line between the anchors, which is the right guess where the surface
        is flattish and a poor one where it is not — round a sphere the line
        runs well inside the model. Each pass afterwards searches around the
        path the last one found, so the corridor closes in on where the
        geodesic really goes. It stops as soon as a pass finds nothing
        shorter, which on most spans is the second one.
        """
        if tri_a < 0 or tri_b < 0:
            return None
        if tri_a == tri_b:
            return []  # one triangle: the straight line across it is on it
        straight = (b - a).length
        if straight < self.diagonal * SAME_POINT:
            return []

        guide = [a, b]
        best, shortest = None, math.inf
        for _pass in range(PASSES):
            span = sum((guide[i + 1] - guide[i]).length
                       for i in range(len(guide) - 1))
            found = None
            for window in WINDOWS:
                found = self._corridor(
                    tri_a, tri_b, a, b,
                    max(straight * window, span * 1.25)
                    + self.diagonal * 1e-9, guide)
                if found is not None:
                    break
            if found is None:
                break
            faces, edges = found
            if len(faces) < 2:
                return []
            points = self._straighten(faces, edges, a, b)
            path = [a] + points + [b]
            length = sum((path[i + 1] - path[i]).length
                         for i in range(len(path) - 1))
            if length >= shortest * (1.0 - SETTLED):
                break  # no better than the pass before: this is the answer
            best, shortest = points, length
            if length <= straight * (1.0 + GOOD_ENOUGH):
                break  # as good as anything across the surface could be
            # A handful of points is guide enough, and every one of them is
            # measured against every edge the next search looks at.
            guide = _thinned_guide([a] + points + [b])
        if best is None:
            return None

        least = self.diagonal * SAME_POINT
        walked = []
        for point in best:
            if (point - a).length <= least or (point - b).length <= least:
                continue
            if walked and (point - walked[-1]).length <= least:
                continue
            walked.append(point)
        return walked


# ----------------------------------------------------------------------
# What is kept between calls
# ----------------------------------------------------------------------

# One Surface per model. Rebuilding it reads the whole mesh, and a drag
# re-walks a span on every mouse move.
_SURFACES = {}
_SURFACE_LIMIT = 4

# Walked spans, keyed by the two anchors they run between, so moving one
# anchor only re-walks the two spans either side of it. Everything in here
# can be worked out again from the model, so it is thrown away freely.
_SPANS = {}
_SPAN_LIMIT = 20000


def forget(obj=None):
    """Drop what is remembered — after the model itself has changed."""
    if obj is None:
        _SURFACES.clear()
        _SPANS.clear()
        return
    for key in [k for k in _SURFACES if k[0] == obj.name]:
        del _SURFACES[key]
    _SPANS.clear()


def _key(obj):
    dg = bpy.context.evaluated_depsgraph_get()
    mesh = obj.evaluated_get(dg).data
    return (obj.name, getattr(obj.data, "session_uid", obj.data.name),
            len(mesh.vertices), len(mesh.polygons))


def surface_for(obj):
    """The walking surface of a model, built once and kept."""
    key = _key(obj)
    found = _SURFACES.get(key)
    if found is None:
        if len(_SURFACES) >= _SURFACE_LIMIT:
            _SURFACES.clear()
        found = Surface(obj, key)
        _SURFACES[key] = found
    return found


def place(obj, point, hint=-1):
    """Put a point onto the model's surface. Returns (point, face)."""
    surf = surface_for(obj)
    landed, _tri, face = surf.locate(Vector(point), hint)
    if landed is None:
        return Vector(point), -1
    return landed, face


def _rounded(point):
    return (round(point.x, 9), round(point.y, 9), round(point.z, 9))


def between(obj, a, b, hint_a=-1, hint_b=-1):
    """The surface path between two anchors, or None if there is not one.

    Anchors are in the model's own local space, and so is what comes back.
    """
    surf = surface_for(obj)
    key = (surf.key, _rounded(a), _rounded(b))
    if key in _SPANS:
        return _SPANS[key]
    start, tri_a, _face = surf.locate(Vector(a), hint_a)
    end, tri_b, _face = surf.locate(Vector(b), hint_b)
    walked = None
    if start is not None and end is not None:
        walked = surf.walk(start, tri_a, end, tri_b)
    if len(_SPANS) >= _SPAN_LIMIT:
        _SPANS.clear()
    _SPANS[key] = walked
    return walked
