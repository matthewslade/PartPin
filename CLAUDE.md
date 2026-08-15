# Notes for working on PartPin

The README is for whoever is using the add-on. This is for whoever is changing
it. Everything here was paid for once already.

## What the thing is

A Blender add-on that cuts a closed mesh into printable parts along a line the
user draws on the model, and fits pin/socket connectors across the seam.

Three files carry the geometry. Each opens with why it is built the way it is,
and nearly every constant in them has the measurement that set it written
beside it — read those before changing a number.

| File | What it owns |
| --- | --- |
| `part_pin/walker.py` | The shortest path across the model's surface between two points |
| `part_pin/surface.py` | The cut line: anchors on the model, walked between; and what hangs off a cut |
| `part_pin/mesh_cut.py` | The cutter: band, intersect, seam, part, cap |
| `part_pin/core.py` | Booleans, connectors, and the apply pipeline |
| `part_pin/ops.py`, `ui.py`, `shape_edit.py`, `draw_cut.py`, `props.py` | Operators, panels, the two modal tools, settings |

The cut line is an **ordered ring of anchors in the model's own local space**,
with the path between consecutive anchors **walked across the surface**. There
is no plane and no height field anywhere in it. `surface.line_rings` is the one
definition of where the line runs — the line drawn on screen and the line that
cuts are both that.

## Five things in the walker that do not fail loudly

Every one of these was a bug that produced a plausible-looking line running
somewhere else entirely. They are commented in place; this is the index.

1. **The funnel's signed area runs the opposite way to the obvious one**
   (`_turn` against `_area2`). Backwards, the funnel turns at every gate and
   the "straightened" path comes out 2.5× the length it was asked for.
2. **The unfolding's fold direction, and the gates' left/right, come from each
   triangle's own winding** — not from where the neighbouring corner landed. A
   model that has been through a boolean carries triangles with no area, and
   their far corner lands *on* the shared edge, so "the other side from that"
   is a coin toss.
3. **The funnel's ends are drawn a hair towards the middle of their triangle**
   (`_in_flat`). An anchor sits exactly on one of the model's edges more often
   than not, and an end of the funnel sitting on a gate leaves it spanning half
   a turn.
4. **The corridor's cost includes the last leg to the far anchor.** Without it,
   a corridor arriving through a gate at the far end of a long triangle looks
   cheaper than one arriving beside the anchor.
5. **Two parallel segments have no single closest point**, so
   `_closest_on_segment` takes the middle of the overlap. Taking an end puts
   the search's guess a whole edge away — and a line along a limb runs parallel
   to every edge of it.

## Things that have been tried and do not work

Each one looks obviously right and costs a release to disprove.

| Attempt | What happened |
| --- | --- |
| Storing the line as a height field over a fitted plane | Cannot hold a contour that wraps round anything. It sagged up to 0.9% of the model away from its own points and doubled back on itself where the field could not express what was drawn — 3,648 hairpin pairs on a freshly drawn line |
| Searching for the path over the triangles' middles, the textbook way | Comes apart on a real model: the side of an extruded limb is one triangle six units long and its middle is nowhere near the line. Search over the edges the path would cross |
| Plain Dijkstra for the corridor | Correct but 11× slower than the same search leaned towards the far anchor. A drag went from 175 ms to 16 ms |
| Capping both halves from separate triangulations | Two triangulations of a non-planar loop span different surfaces: the parts stop mating and 2% of the model goes missing. **One shared triangulation, always** |
| Measuring a tolerance against each part rather than across the whole cut | A body and a hand are different sizes, so the two halves welded to different rims (936 points against 989), the rims stopped matching, and neither half got capped |
| Holding the cap's steps to exact areas, or hairpins to a count of samples | Both are fine on a coarse model and nonsense on a dense one, where the line carries a point per face. Measure against the model or against the line, never against a count of samples |
| Requiring parts to come out perfectly manifold | A model with one nick in it could then never be cut. Judge on leaving it **no worse than it arrived** |
| Tuning one band height to suit every model | Every value failed some fixture. The cutter tries a ladder and **verifies** each rung by carrying it through to two closed parts. Keep that shape |
| Capping the band's height *everywhere* by the line's turning radius, or by how close the line comes back to itself | Both regress `make_limb_with_fin`, which needs a band taller than the fin's edges are round. Cutting it back only at the corners where the ribbon would fold is a different thing and is what `_unfolded` does |
| Flagging hairpins by proximity alone | Fires on a line across a thin fin, where the two sides are legitimately that close. Marks on a cut that works are the one thing that must never ship |
| Cutting doubled-back stretches out automatically | Not safe: the excursion can be the part the user wanted. Marker only |
| Fitting the cap's plane to the rim by least squares | Leans over towards an excursion in the line, and seen down *that* the loop can cross itself. Use the area-weighted (Newell) normal |
| Nudging the band's line further than a millionth of the model to clear the mesh's own edges | Closes a regular tube and breaks a real sculpt. See the open item below |
| Blaming a failed cut on the model when it reports non-manifold edges | Measured on a 409,717-face model with 3 bad edges: they were 1.7 units from the line and had nothing to do with it. The seam was breaking at a 62° corner the user had drawn. **Find where the loose ends are before believing any theory about why** |

## The band must never fold over itself

The band is a ribbon standing along the line. Where the line turns a corner,
the rail on the inside of the turn runs back over itself — it overlaps by the
height times the tangent of half the turn — and a ribbon that crosses itself is
not something any solver can cut with. The seam breaks there at every rung of
the ladder, and taller bands make it worse rather than better, which reads
exactly like a crease the band cannot bridge and is the opposite problem.

`_unfolded` cuts the height back at those corners and leaves the rest alone.
That is not the same as capping the band by the line's turning radius, which
*was* tried and rejected: that shortens the whole band and leaves it unable to
bridge a crease elsewhere. Where the line runs straight the cap is thousands of
times any height ever asked for.

## Three bugs that will bite again if this is rewritten carelessly

- `bpy.ops.mesh.intersect` only sees a selection set **on the mesh, in object
  mode**. Assignments through `bmesh.from_edit_mesh` do not reach it.
- Anything walking bmesh elements must walk in **coordinate order**. bmesh
  hashes by address, so dict order varies between runs and the same cut then
  works one run and not the next. This has bitten twice, in two places.
- **This installs as an extension, and an extension has no `bl_info`.** Blender
  4.2+ loads it as `bl_ext.user_default.part_pin` and takes `bl_info` away; the
  manifest is the only metadata there is at runtime. The suite imports
  `part_pin` as a plain package, which *keeps* `bl_info` — so anything reading
  packaging metadata passes the tests and throws in the product. It did: the
  version footer raised `ImportError` on every redraw of the panel. Test that
  path with `bl_info` deleted from the package module, the way
  `scenario_the_version_is_shown_and_agrees` does.

## Everything scales with the model

The line is walked across the model's own faces, so on a 441,616-face sculpt
its ring carries ~2,000 points and the rim the cap fills carries ~950, where a
test cube gives thirty. Anything measured per sample, or done per element in
Python, falls over there. What that has already cost:

- Selection set by walking every polygon three times: 3.2 s a band. In bulk:
  0.46 s.
- `mesh_issues` edge by edge: asked of the model and both halves on every band.
- `_ear_clip` on a 936-point rim: **31 seconds**, because the cap's steps
  inwards had been rejected one by one first.
- Hairpins compared pair by pair: four million comparisons after every drag.

Bulk array work through `foreach_get`/`foreach_set` is the fix in every case.

## Known open

- **A line round a perfectly regular tube.** Where the walked line runs *along*
  the model's own edges — whole stretches of an extruded cylinder — the band
  contains those edges, the solver is asked to cut a mesh along an edge it
  already has, and the seam breaks at every doubled vertex. `EDGE_CLEAR` moves
  the band's copy of the line a millionth of the model sideways, which took a
  441,800-face tube from 90 loose ends to 4. Not 0. Larger nudges close the
  tube and break a real sculpt, so this wants moving only the stretches that
  lie along an edge, rather than the whole line.
- The walker's `WINDOWS` ladder gives up after the widest search. No fixture
  reaches it, so that path is exercised only by the two-shells test.

## Licensing

GPL-3.0-or-later, which is what a Blender add-on belongs under: it runs inside
Blender and against its Python API, and those are GPL. `LICENSE` is the
verbatim FSF text, the manifest declares `SPDX:GPL-3.0-or-later`, and every
source file carries an SPDX notice at the top. A scenario in the suite checks
all three agree, so a file added without a notice fails the tests.

## Testing

```sh
/Applications/Blender.app/Contents/MacOS/Blender --background \
    --python-exit-code 1 --python tests/smoke_test.py
```

389 checks. **Run it three times** — a bmesh-ordering bug has twice made
results vary between runs. It takes about 25 seconds; the dense fixtures are
built from numpy arrays straight into the mesh, because asking bmesh for a
441,800-face sphere takes over a minute on its own.

The `bpy` PyPI wheel also works and needs CPython 3.13:

```sh
/opt/homebrew/bin/python3.13 -m venv .venv-bpy && .venv-bpy/bin/pip install bpy
.venv-bpy/bin/python tests/smoke_test.py
```

`import bpy` must come **before** `import bmesh`.

**`tools/diagnose_cut.py` is the most valuable thing in the repo for this
work.** It opens a saved .blend, reports the model, the line's quality — how
far off the surface, how far from its own anchors, the spacing spread, the
hairpin count — and then walks every band the cutter would try, saying which of
the three ways it failed and where.

```sh
blender --background --python tools/diagnose_cut.py -- <file.blend> [cut name]
```

## Working with this user

- **Ask for the .blend.** Every real diagnosis in this project came from one.
  A day of measuring fixtures that all passed was settled in an afternoon by a
  single file: the rim density that broke the cap, the weld that stopped the
  halves mating, and a crash, all in one sitting.
- **They read the tool better than the instrumentation does.** "It worked when
  I retried" found a first-run context bug. "Hairpins come from stop-start
  drawing" was right about the mechanism and pointed straight at the code.
  Reproduce what they describe before theorising.
- **Say plainly what is not fixed**, and never let release notes imply more
  than was measured.
- **Ship every change.** They install from the Releases page, so work sitting
  unreleased is work they cannot use. Bump both
  `part_pin/blender_manifest.toml` and `part_pin/__init__.py` (a test checks
  they agree, and the version shows at the bottom of the panel), `./build.sh`,
  push, then `gh release create` with `--notes-file` — never `--notes`, the
  shell executes backticks in it. **Never mention any other product in
  anything public.**
