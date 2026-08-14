# Next job: store the cut line on the surface, not over a plane

**Status:** the cutter is sound. The **line** is not. Everything fiddly about
the tool traces back to one decision — a cut line is stored as a *height field
over a fitted plane* — and no amount of patching the symptoms has fixed it. Nine
releases (1.12 → 1.21) went on symptoms. This document says what to build
instead.

Written by the agent who built the mesh-surgery cutter, at the user's request,
for whoever does the rework. **Read §4 before writing code** — a lot of
plausible fixes have already been measured and rejected, several of them twice.

---

## 1. What is wrong, measured

A cut line is a handful of control points kept in the cut object's own local
space, with a Wendland-RBF height field `h(u, v)` over that object's XY plane
interpolating them (`surface.HeightField`, `surface.field_for`). The line you
see and the line that cuts are both `surface.line_samples`: walk the control
polygon **in the plane**, evaluate the field, then project each sample onto the
model.

That representation cannot hold a contour. On the user's model (a 441,616-face
sculpt, 156.36 across, closed and manifold):

| file | control points | line strays from the polyline through its own points |
| --- | --- | --- |
| freshly drawn | 25 | **0.55 = 0.353% of the model** |
| after a failed cut | 40 | **1.40 = 0.894% of the model** |

The line passes through its control points exactly (measured: 0.0000) and every
control point is on the surface (0.0000). Between them it sags toward the fitted
plane, because that is what a height field does. In the user's words the lines
"pulled away from their respective points which ruins the contour I was trying
to follow". That is not a bug to patch; it is the representation declining to
hold the shape.

**The hairpins come from the same place.** A contour wrapping round a limb is
not a function over any plane, so where the field cannot express what was drawn,
the projected line doubles back on itself. `mesh_cut.hairpins` finds them:

| line | close pairs | facing alike (hairpin) | opposed (thin feature) |
| --- | --- | --- | --- |
| freshly drawn, will not cut | 380 | **373**, 1–23° | 0 |
| after a failed cut | 4107 | **3648**, 1–7° | 0 |
| collar across a 0.04 fin | 64 | 0 | 61, all 180° |
| plain collar on a limb | 0 | 0 | 0 |

A hairpin is the one shape of perimeter that cannot be cut: there is no room to
cut between its two sides. It is present **on a freshly drawn line, before any
cut is attempted**, which rules out the cutter as the cause.

Everything else the user complains about is downstream of the plane:

- *Cut Detail*, *Points*, falloff, `refit_frame` — all exist to manage the
  field, and all are knobs the user should never have seen.
- "One cut has one plane. Lines facing more than about 45° away from the one you
  last edited are skipped with a warning" — `surface.loop_quality`'s
  `min_alignment`. Purely an artefact of the plane.
- `refit_frame` silently rewrites the cut's frame *and* its stored points on
  every cut attempt, which is why a failed cut leaves the line different from
  how it was drawn.

## 2. What to build

**Store the line as what was drawn: an ordered ring of anchor points on the
model's surface, with the path between consecutive anchors walked across the
surface.** No plane, no field, no projection of anything through space.

- **Anchors** in the model's local space (so the line rides with the model if it
  is moved), one ring per cut line. Keep the face index each anchor sits on as a
  starting hint for the walker.
- **Spans**: between anchor *i* and *i+1*, the shortest path *across the
  surface*. Cache the walked points per span; a drag only invalidates the two
  spans either side of the anchor it moved.
- **The ring handed to the cutter** is the anchors with each span's walked points
  spliced in — which is exactly the shape `mesh_cut` already consumes.

### Why this fixes it rather than patching it

- The line is on the surface everywhere by construction, so it cannot stray from
  the contour and cannot sag.
- **Hairpins become impossible between anchors**: a shortest path does not
  double back. A hairpin can then only come from anchors the user has genuinely
  dragged into one, which is visible, local, and their choice.
- Dragging is local and predictable: no falloff, no field, no re-fitting.
- Lines at any angle work. The 45° limitation goes.

### The walker

This is the one real piece of new machinery. Recommended shape:

1. Find the start and end faces (`closest_point_on_mesh` gives a face index).
2. **Window** the search: only faces within an ellipse whose foci are the two
   anchors, padded by ~1.5× their straight-line distance. Consecutive anchors
   are ~0.2 apart on a 156-unit model, so this is a few hundred faces out of
   441,616 — the windowing is what makes it interactive, and it must not be left
   out.
3. Dijkstra over the mesh vertices/edges inside the window to get a corridor.
4. **Straighten** the corridor with the funnel algorithm over the triangle strip
   it crosses. Without this the path zig-zags along mesh edges, which reintroduces
   the crowded-and-coincident sample problem `_thinned`/`_filled` exist to clean
   up. With it, the path is a proper geodesic within the strip.
5. If the walk fails (the anchors are on disconnected shells, or the window is
   exhausted), say so plainly on that span rather than falling back to a chord
   through space. A chord through space is the bug being removed.

Straightest-geodesic marching (step within a face toward the target, cross the
edge you hit, repeat) is tempting because it is ~30 lines. It gets stuck on
concave regions and on a sculpt of this density it will get stuck often. If it
is tried, hold it to the same bar in §5 and measure the failure rate before
trusting it.

## 3. What stays

**`part_pin/mesh_cut.py` is sound and measured. Do not rewrite it.** It takes a
ring of world points on the surface and produces two closed parts, with the seam
landing on the ring to 0.0001% of the model's size and volume conserved exactly.
It has been beaten on for nine releases and the remaining problems in it are all
about the *line* it is given.

What it needs from the rework:

- `mesh_cut.line_rings(cut, target)` is the single seam between line and cutter.
  Reimplement it on the new representation; everything below it is unchanged.
- It returns a `settle` callable, used to draw the middle of the cap down onto
  "the cut's own surface". With no height field there is no such surface —
  **fit a plane to the finished ring and settle onto that.** That is legitimate:
  the cap is a spanning surface and wants to be flat-ish in the middle; fitting
  a plane to the *result* does not constrain the line. `_cap_plan` also takes a
  `normal` for its frame — the same fitted plane's normal.
- `_thinned` and `_filled` can probably go once the walker produces evenly
  spaced points, but **measure before deleting**: they fixed a real bug where
  coincident samples made band quads with no area, which is a red mark that
  never shifts at any band height.

## 4. Already tried, measured, and rejected. Do not repeat these.

Everything in the old §4 of `NEXT-mesh-surgery-cutter.md` still applies. On top
of it, from the nine releases since:

| Attempt | Outcome |
| --- | --- |
| Tuning the band's height — fixed, spacing-derived, measured surface bulge, room to the next surface, crease-following resampling, and combinations | Every one failed on at least one fixture. The cutter now tries a ladder and **verifies**; keep that shape |
| Capping the band's height by the line's turning radius | Regressed `make_limb_with_fin`, which needs a band taller than the fin's edges are round |
| Capping it by how close the line comes back to itself | Same regression, and did not close the gap it was written for |
| Repairing a failed seam by raising the band locally around the loose ends | Helps a cube's corners and an armpit (8× too thin still cuts). **Makes a hairpin dramatically worse** — 6 spots became 10, 20, 148, 926 — because raising the band is what does the damage there |
| Flagging hairpins by proximity alone | Fires on a collar across a thin fin, where the two sides are legitimately that close. Marks on a working cut are the one thing that must never ship |
| Cutting doubled-back stretches out automatically | Not safe as written: the excursion can be the part the user wanted. Kept as a marker only |
| Capping both halves from separate triangulations | Two triangulations of a non-planar loop span different surfaces: parts stop mating and 2% of the model goes missing. **One shared triangulation, always** |
| Requiring parts to come out perfectly manifold | A model with one nick in it can never pass. Judge on leaving it **no worse than it arrived** |

Two bugs that will bite again if the code is rewritten carelessly:

- `bpy.ops.mesh.intersect` only sees a selection set **on the mesh, in object
  mode**. Assignments through `bmesh.from_edit_mesh` do not reach it.
- Anything walking bmesh elements must walk in **coordinate order**. bmesh
  hashes by address, so dict order varies between runs and the same cut then
  works one run and not the next. This has bitten twice, in two different
  places.

## 5. The bar

Existing scenarios must all still pass (`tests/smoke_test.py`, currently 1
known failure — see §7). On top of them:

1. **The line is on the surface.** Every sample within 1e-4 of the model, on
   every fixture. Currently 0.0000 at the anchors and up to 0.894% between them.
2. **The line holds its contour.** Max deviation from the polyline through its
   own anchors under ~0.05% of the model on the user's files, against 0.353% and
   0.894% now. (It will not be zero — a geodesic bulges where the surface does,
   which is correct.)
3. **No hairpins from drawing.** `mesh_cut.hairpins` returns empty on both of
   the user's saved files. Currently 373 and 3648 pairs.
4. **All eight geometry fixtures still cut** into closed parts with volume
   conserved: limb, limb+fin, shoulder, shoulder-arm (armpit), cube waist at 13
   /16/24 points, mushroom. Plus the sphere cases in the suite.
5. **The seam still lands on the line** to 0.2% of the model, which
   `scenario_seam_lands_on_the_line` already asserts (it measures 0.0001%).
6. **Dragging stays interactive.** Re-walking two spans on the 441,616-face
   sculpt under ~100 ms. Measure it; this is what the windowing in §2 is for.
7. **No plane-alignment warning exists any more**, and no cut is ever skipped
   for facing the wrong way.
8. **Silent when it works.** Zero marks of any colour on a cut that cuts. This
   has destroyed the user's trust in the marks twice; the suite asserts it.

Run the suite **three times** — a bmesh-ordering bug has twice made results
vary between runs.

## 6. Setting up, and the tools

There is **no Blender on the dev machine**; the tests run against the `bpy`
PyPI wheel, and a fresh workspace has no venv:

```sh
/opt/homebrew/bin/python3.13 -m venv .venv-bpy && .venv-bpy/bin/pip install bpy
.venv-bpy/bin/python tests/smoke_test.py
```

`import bpy` must come **before** `import bmesh`. The wheel needs CPython 3.13.

**`tools/diagnose_cut.py` is the most valuable thing in the repo for this
work.** It opens a saved .blend, reports the model and the line, then walks
every band the cutter would try and says which of the three failures it ended
on and where. Every real diagnosis in this session came from it; every diagnosis
made without it was wrong.

```sh
.venv-bpy/bin/python tools/diagnose_cut.py <file.blend>
```

Ask the user to re-attach their saved files — they live in `.context/`, which is
per-workspace and gitignored, so a new workspace will not have them. The three
that matter: a cut that works, a freshly drawn cut full of hairpins, and a cut
after a failed attempt. **Add a "line quality" section to the tool** as part of
this work: on-surface deviation, deviation from the anchor polyline, spacing
spread, and hairpin count. Those four numbers are the whole bar in §5.

## 7. Known problems, unrelated to the line

- **Connectors on a reshaped cut leave one part with a few tiny holes at the
  sockets** — three edges per pin beyond the first, reproduced on a clean
  441,616-face model. The seam is sound before the pin goes in; it is the pin's
  boolean. Ruled out: cap ring count, step, settle strength, ear-selection, and
  `use_hole_tolerant`. It happens with *any* interior cap geometry at all. This
  is the one failing scenario in the suite. The untried idea is reordering —
  union the pins into the model and cut afterwards, rather than cutting first.
- **The editor still previews the old synthesised lid** (`shape_edit._rebuild_cap`
  → `surface.cap_preview_tris` → `cap_sheet`), which is not what the cutter
  does. It should show the cap the cutter actually builds
  (`mesh_cut._cap_plan`). Blocks deleting `cap_sheet`, `build_cap_slab`,
  `find_join_hints`, `CAP_STEP_OUT`, `SEAM_FACTOR`, and the `pp_undercut`
  setting, which does nothing to this cutter.
- **The full-extent cutter** (`pp_local = False`, `build_surface_cutter`) is
  built on the height field. It is the "untick Cut Inside Line Only" fallback
  and it works. Removing the field breaks it, so give it a plane fitted to the
  ring — which is all it ever really was.

## 8. What to delete once the walker works

`HeightField`, `field_for`, `refit_frame`, `loop_quality`'s plane and alignment
checks, `line_samples`' in-plane chord walk, `pp_falloff`, `surface_resolution`
(*Cut Detail*) if nothing else needs it, `frame_extent`/`_height_grid`/
`_full_grid_bm` if the full-extent cutter stops needing them, and
`convert_to_surface`'s plane fitting.

**Dead code is what let the last crash through** — a helper renamed inside code
nothing exercised. `surface.py` was 1984 lines with 687 unreachable. Delete
aggressively and re-run the suite to prove it. Keep the two guards that have
caught real bugs: the operator wiring audit, and "every setting the panel shows
is used somewhere".

## 9. Working with this user

- **Ask for the .blend.** Three files settled three bugs in one run each, after
  I had spent whole releases guessing wrong from descriptions. Never diagnose
  from a description when a file is available.
- **They read the tool better than the instrumentation does.** "It worked when I
  retried" found a first-run/second-run context bug I had not looked for.
  "Hairpins come from stop-start drawing" was correct about the mechanism and
  pointed straight at `draw_cut.bridge_points`. Take their reading seriously and
  reproduce it before theorising.
- **Say plainly what is not fixed.** Several releases here claimed a probable
  fix for something unreproduced; that was right to ship but only because the
  notes said so. Never let a release imply more than was measured.
- **Ship every change.** They install from the Releases page to try it; work
  sitting unreleased is work they cannot use. Bump both
  `part_pin/blender_manifest.toml` and `part_pin/__init__.py`, `./build.sh`,
  push, `gh release create` with `--notes-file` (never `--notes`: backticks get
  executed by the shell). A known failing test does not block a release, but
  say so in the notes. **Never mention any other product in anything public.**
