# The cut line lives on the surface — done, and what is left

**Status:** built and measured. A cut line is no longer a height field over a
fitted plane; it is a ring of anchors on the model with the path between
consecutive anchors **walked across the surface**. Everything the old
representation could not hold — the contour, the hairpins, the 45° limit, the
knobs — went with it.

Written by the agent who did the rework, for whoever comes next. §5 is the
measurements, §6 is what is still open, §7 is what has already been tried and
rejected — **read §7 before writing code**, several of those were measured
wrong twice.

---

## 1. What it was, and what it measured

A cut line used to be a handful of control points in the cut object's own
space, with a Wendland-RBF height field `h(u, v)` over that object's XY plane
interpolating them. The line you saw and the line that cut were both a walk of
that field's control polygon *in the plane*, projected onto the model.

On the user's model (a 441,616-face sculpt, 156.36 across):

| file | control points | line strays from the polyline through its own points |
| --- | --- | --- |
| freshly drawn | 25 | **0.55 = 0.353% of the model** |
| after a failed cut | 40 | **1.40 = 0.894% of the model** |

The line passed through its control points exactly and sagged towards the
fitted plane between them, because that is what a height field does. A contour
wrapping round a limb is not a function over any plane, so where the field
could not express what was drawn, the projected line doubled back on itself —
373 and 3,648 hairpin pairs on those two files, **on lines that had only just
been drawn**. A hairpin is the one shape of perimeter that cannot be cut.

## 2. What it is now

**`part_pin/walker.py`** — the one piece of new machinery. Everything else in
the rework is subtraction.

- **Anchors** are stored in the *model's* local space (`pp_points`, with the
  face each one sits on as a hint for the walker), so the line rides with the
  model. A cut saved by an older version is brought across on first read —
  `surface.migrate`, keyed on `pp_anchor_space`.
- **Spans**: between anchor *i* and *i+1*, the shortest path across the
  surface. Cached per anchor pair, so dragging one anchor re-walks the two
  spans either side of it and nothing else.
- **The ring handed to the cutter** is the anchors with each span's walked
  points spliced in — `surface.line_rings`, and `mesh_cut.line_rings` above it
  is still the single seam between the line and the cutter.

### How a span is walked

1. Both ends are put onto a triangle (`Surface.locate`).
2. A corridor is found by a shortest-path search **over the edges the path
   would cross**, not over the triangles' middles, kept inside an ellipse with
   the anchors as its foci and leaning towards the far anchor (A*).
3. The corridor is unfolded flat and the **funnel algorithm** draws the
   straight line across it.
4. What comes back is where that line crosses each edge — points exactly on
   the surface, with every stretch between two of them inside one triangle.
5. The whole thing is repeated with the path just found as the guide, until a
   pass finds nothing shorter. Most spans settle on the second pass.

If a span cannot be walked, it says so (`surface.BROKEN_AT`, marked blue in the
editor). It never falls back to a chord through space.

### Five things in there that do not fail loudly

Every one of these was a bug that produced a plausible-looking line running
somewhere else entirely. They are commented in place; this is the index.

- **The funnel's signed area is the other way round from the obvious one**
  (`_turn` vs `_area2`). Backwards, the funnel turns at every gate and the
  "straightened" path comes out 2.5× the length of the line it was asked for.
- **The unfolding's fold direction, and the gates' left/right, are read off
  each triangle's own winding**, not off where the neighbouring corner landed.
  A model that has been through a boolean carries triangles with no area, their
  far corner lands *on* the shared edge, and "the other side from that" is a
  coin toss.
- **The funnel's ends are drawn a hair towards the middle of their triangle**
  (`_in_flat`). An anchor sits exactly on one of the model's edges more often
  than not — every point of a line round an extruded limb does — and an end of
  the funnel sitting on a gate leaves it spanning half a turn.
- **The corridor's cost includes the last leg to the far anchor.** Without it,
  a corridor arriving through a gate at the far end of a long triangle looks
  cheaper than one arriving beside the anchor.
- **Two parallel segments have no single closest point**, so
  `_closest_on_segment` takes the middle of the overlap. An end instead puts
  the search's guess a whole edge away — and a line along a limb runs parallel
  to every edge of it.

## 3. What went, and what replaced it

| Gone | Replaced by |
| --- | --- |
| `HeightField`, `field_for`, `pp_falloff` | nothing — the line is on the model |
| `refit_frame` (rewrote the stored points on every cut attempt) | `frame_to_line`, which moves only the cut object's own frame |
| `loop_quality`'s plane and 45° alignment test | nothing; every line is cut |
| `line_samples`' in-plane chord walk | `line_rings`, walked across the surface |
| `cap_sheet`, `build_cap_slab`, `find_join_hints`, `CAP_STEP_OUT`, `SEAM_FACTOR` | `mesh_cut._cap_plan`, which is what the cutter actually fills rims with |
| `surface_resolution` (*Cut Detail*), `pp_undercut` | nothing |
| `build_surface_cutter`'s height grid | a half-space on the plane fitted to the finished ring |
| `_height_grid`, `_full_grid_bm`, `frame_extent`, `_rim_rings`, `_segments_touch`, `polygon_roundness`, and five dead helpers in `core` | deleted |

Two changes inside the cutter, both forced by the line being walked rather than
sampled at a fixed count:

- **`REPAIR_REACH` is a share of the line's length**, not a count of samples.
  How many samples a line has is now a fact about the model's density, and
  "five samples" covered a hand's breadth of one model and half of another.
- **`hairpins` measures how far apart two stretches are *along the line***, not
  in samples, and buckets by position instead of comparing every pair. In
  samples it flagged **12,036 pairs on a clean line** on a dense model. It also
  reads 30° rather than 60° as "facing alike": the root of a fin comes in at
  about 40° on a cut that works, and the real hairpins came in at 1–23°.

`_thinned` and `_filled` are still there and still earn their place — the
walker's crossings crowd where the path passes near a vertex.

## 4. The one thing added to the cutter

`_weld_rim`: rim vertices sitting exactly on top of one another are merged
before the cap is planned. Cutting a line into the model's faces leaves the odd
coincident pair — where the line runs along an edge the model already has, the
solver produces the crossing it was asked for *and* keeps the vertex that was
there. Those are zero-length edges in the rim, the triangles spanning them
cannot be built, and the part comes out with a few holes in it.

**This also cleared the one failing scenario in the suite** (§7 of the old
notes: "connectors on a reshaped cut leave one part with a few tiny holes at
the sockets"). It was not the pin's boolean.

## 5. Measured

The suite: **362 checks, all passing, three runs identical**, 13 seconds.

```sh
/Applications/Blender.app/Contents/MacOS/Blender --background \
    --python-exit-code 1 --python tests/smoke_test.py
```

On a 441,800-face model 270 across (`tests/smoke_test.make_dense_mesh`):

| | before | now |
| --- | --- | --- |
| line off the model's surface | up to 0.894% of it | **8.5e-6 = 0.000003%** |
| hairpins on a freshly drawn line | 373 and 3,648 | **0** |
| re-walking two spans after a drag | — | **4–12 ms** (bar: 100 ms) |
| reading the model, once per model | — | 0.7 s |
| building a whole line, 33 spans | — | 0.09 s |

The line still leaves the polyline through its own anchors, and should: a
geodesic bulges where the surface does. What matters is that it closes on them
as the anchors get closer together — 0.031 → 0.008 → 0.003 for 8, 16 and 32
anchors round the same collar, which is a chord closing on an arc. The suite
asserts that.

Against a sphere, where the answer is known exactly, the walked span is within
**0.5% of the great circle** at 22.5°, 45° and 90° of arc, and its greatest
distance from the chord equals the sagitta to four figures.

## 6. What is still open

- **The editor's preview is now the cutter's own cap plan**
  (`surface.cap_geometry` → `mesh_cut._cap_plan`), and the cut object's display
  mesh is that same lid — or a plain plane when "Cut Inside Line Only" is off,
  which is what that mode does. That closes the old §7 item.
- **`bpy.ops.mesh.intersect` still only sees a selection set on the mesh, in
  object mode.** Assignments through `bmesh.from_edit_mesh` do not reach it.
- **Anything walking bmesh elements must walk in coordinate order.** bmesh
  hashes by address; dict order varies between runs. This has bitten twice.
- The walker's `WINDOWS` ladder gives up after the widest search. No fixture
  reaches it, so the failure path is exercised only by the two-shells test.
- `line_quality`'s spacing spread reads alarmingly (the raw ring has pairs a
  hair apart where the path passes a vertex); the ring the cutter is handed,
  reported on the next line of `diagnose_cut`, is evenly spaced. Worth tidying
  if it ever misleads a diagnosis.

## 7. Already tried, measured, and rejected. Do not repeat these.

Everything in §4 of `NEXT-mesh-surgery-cutter.md` still applies. On top of it:

| Attempt | Outcome |
| --- | --- |
| Tuning the band's height — fixed, spacing-derived, measured surface bulge, room to the next surface, crease-following resampling, and combinations | Every one failed on at least one fixture. The cutter tries a ladder and **verifies**; keep that shape |
| Capping the band's height by the line's turning radius | Regressed `make_limb_with_fin`, which needs a band taller than the fin's edges are round |
| Capping it by how close the line comes back to itself | Same regression, and did not close the gap it was written for |
| Repairing a failed seam by raising the band locally around the loose ends | Kept, but it is what makes a hairpin dramatically worse (6 spots became 10, 20, 148, 926) |
| Flagging hairpins by proximity alone | Fires on a collar across a thin fin, where the two sides are legitimately that close |
| Cutting doubled-back stretches out automatically | Not safe: the excursion can be the part the user wanted. Marker only |
| Capping both halves from separate triangulations | Two triangulations of a non-planar loop span different surfaces: parts stop mating and 2% of the model goes missing. **One shared triangulation, always** |
| Requiring parts to come out perfectly manifold | A model with one nick in it can never pass. Judge on leaving it **no worse than it arrived** |
| Searching the corridor over the triangles' middles (the textbook way) | Comes apart on a real model: the side of an extruded limb is one triangle six units long. Search over the edges the path would cross |
| Plain Dijkstra for the corridor | Correct but 11× slower than the same search leaned towards the far anchor. A drag went from 175 ms to 16 ms |
| Fitting the cap's plane to the rim by least squares | Leans over towards an excursion in the line, and seen down *that* the loop can cross itself. Use the area-weighted normal |

## 8. Setting up, and the tools

There is **Blender 5.2 on the dev machine now**, which is the quickest way to
run anything:

```sh
/Applications/Blender.app/Contents/MacOS/Blender --background \
    --python-exit-code 1 --python tests/smoke_test.py
```

The `bpy` PyPI wheel also works and needs CPython 3.13 (`brew install
python@3.13`), which a fresh workspace will not have:

```sh
/opt/homebrew/bin/python3.13 -m venv .venv-bpy && .venv-bpy/bin/pip install bpy
.venv-bpy/bin/python tests/smoke_test.py
```

`import bpy` must come **before** `import bmesh`.

**`tools/diagnose_cut.py` is still the most valuable thing in the repo for
this work.** It opens a saved .blend, reports the model, the line's quality —
how far off the surface, how far from its own anchors, the spacing spread, the
hairpin count — and then walks every band the cutter would try.

```sh
blender --background --python tools/diagnose_cut.py -- <file.blend> [cut name]
```

Ask the user to re-attach their saved files — they live in `.context/`, which
is per-workspace and gitignored, so a new workspace will not have them. The
three that matter: a cut that works, a freshly drawn cut full of hairpins, and
a cut after a failed attempt. **The two hairpin files are the ones this rework
has never been run against**, because they were not in this workspace: every
number in §5 is from fixtures, not from them.

## 9. Working with this user

- **Ask for the .blend.** Three files settled three bugs in one run each, after
  whole releases of guessing wrong from descriptions. Never diagnose from a
  description when a file is available.
- **They read the tool better than the instrumentation does.** "It worked when
  I retried" found a first-run/second-run context bug. "Hairpins come from
  stop-start drawing" was correct about the mechanism and pointed straight at
  `draw_cut.bridge_points`.
- **Say plainly what is not fixed.** Never let a release imply more than was
  measured.
- **Ship every change.** They install from the Releases page to try it. Bump
  both `part_pin/blender_manifest.toml` and `part_pin/__init__.py`,
  `./build.sh`, push, `gh release create` with `--notes-file` (never `--notes`:
  backticks get executed by the shell). **Never mention any other product in
  anything public.**
