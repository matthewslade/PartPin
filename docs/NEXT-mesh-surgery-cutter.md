# Next job: cut the model's own surface instead of synthesising a lid

**Status:** the localized cut ("Cut Inside Line Only", the default) is not
reliable on dense sculpts. This document says why, what to build instead, what
has already been tried and failed, and how to prove the new thing works.

Written at the end of a long session by the agent who built the current cutter.
The diagnosis is well evidenced; the current implementation is not sound. Read
"What has already been tried" before writing code — several plausible fixes have
already been measured and rejected.

---

## 1. Where things stand

The add-on lives at `github.com/matthewslade/PartPin`, shipped through
**v1.11.1**. Everything except the localized cutter works and the user is happy
with it:

- **Drawing** (`part_pin/draw_cut.py`) — draw the cut perimeter onto the model,
  ray-cast per mouse position, drawing in several stretches with the gaps walked
  across the surface, corner-preserving resampling. The user's words: *"the
  drawing tool works very well"*. **Do not disturb this.**
- **Line editing** (`part_pin/shape_edit.py`) — drag points on the surface, live
  preview of the cut surface, add/remove points, remove a whole line.
- **Connectors, Create Parts, export** (`part_pin/core.py`, `ops.py`) — solid.
- **Full-extent cut** (`pp_local = False` → `surface.build_surface_cutter`) —
  **reliable**, because the cutter is a height field over a plane extruded into a
  closed solid, which cannot fold or have holes. It cuts through the whole model,
  so it takes more than the line asks for. This is the current fallback and the
  README points users at it.

The broken part is `surface.cap_sheet` / `surface.build_cap_slab`: the localized
cutter.

## 2. Why the current construction fails

It **synthesises** a lid spanning the drawn line: fan the line's interior in the
cut's plane, subdivide, relax, lift onto the height field, then displace the rim
onto the model's surface and step it out through it, then `bmesh.ops.solidify`
into a thin slab, then subtract.

Every failure comes from that synthesis:

- The rim displacement is an arbitrary 3D move. In a crease, neighbouring rim
  points find opposite faces and go opposite ways, and the lid **folds through
  itself**. Blender's exact boolean answers a self-intersecting cutter with an
  **empty mesh**, which surfaces as "nothing came away".
- On the user's sculpt the lid also comes out **with holes** (more than one
  boundary ring), which sheds fragments — visible in their screenshot as a trail
  of small disc shells instead of two parts.

Measured evidence (all on `tests/smoke_test.py` fixtures, headless):

| Case | Crossing faces in the lid | Result |
| --- | --- | --- |
| `make_limb` + collar | 0 | 2 clean parts |
| `make_cube_model` + waist | 0 | 2 clean parts |
| `make_shoulder_arm` + collar | 0 | 2 clean parts |
| `make_limb_with_fin` + collar | **69** | boolean returns nothing |

And density trades fit against tangling — raising the lid's sampling closes the
gap between the drawn line and the cut surface (0.74% → 0.16% of model size on a
cube) but makes folding likelier: at the densest setting, cases that cut at the
current setting stop cutting.

## 3. What to build

Stop synthesising. **Cut the model's own surface along the line, and cap each
half with the section that cut produces.** The cap is then built from real
geometry that lies on the model, so it cannot fold and cannot have holes.

### Sketch

1. **Get the line** as a dense ring of world points on the surface:
   `surface.line_samples(cut, target)` already returns exactly this, one ring per
   cut line, every sample projected onto the model. It is the single definition
   of where the line lies — the visible line and the current lid both use it.

2. **Build a knife surface** through the ring. It only has to *cross* the model
   cleanly near the ring; its shape away from the ring does not matter, so make
   it a graph over the cut's fitted plane (`surface.field_for`, which cannot
   fold) extended a little past the ring in every direction. `surface._height_grid`
   and `_full_grid_bm` already build this; `build_surface_cutter` shows the
   extrusion into a closed solid.

3. **Cut the model's surface with it.** Options, in order of preference:
   - `bpy.ops.mesh.intersect(mode='SELECT_UNSELECT', separate_mode='CUT')` in
     edit mode on a copy of the model joined with the knife surface. This writes
     the intersection curve into the model's own topology, which is the whole
     point.
   - Failing that, `bmesh.ops.bisect_plane` per-face is not enough (planes only),
     and an exact boolean against the extruded solid gives the section but also
     cuts through the whole model, which is what we are trying to avoid.

4. **Choose the region.** The intersection curve divides the model's surface.
   Flood-fill faces from a seed inside the drawn line, blocked by the curve. Seed
   from a point known to be inside: the centroid of the ring projected onto the
   surface, or any face whose centre is inside the ring's own footprint.

5. **Separate and cap.** Split the filled region off (`bpy.ops.mesh.separate`
   after selecting it, or `bmesh.ops.split`), then cap both halves with the
   section polygon — one copy per half, wound opposite ways, welded to the split
   rim (the vertices are shared, since the knife created them). Both halves come
   out closed by construction.

6. **Connectors and the rest are unchanged.** `core.apply_connector` works on
   whatever parts exist; `core.drop_debris` still guards against crumbs.

### Why this is worth the work

- No rim displacement, so nothing to fold.
- No synthesised interior, so no holes.
- The cut lands exactly on the drawn line, which is what the user has asked for
  repeatedly and what the current construction only approximates (~0.15% of model
  size out, and worse at corners).
- "Cut inside the line only" becomes true by construction rather than by
  clipping heuristics — the region is chosen by walking the model's surface, not
  by testing points against a flattened polygon.

## 4. What has already been tried and rejected

Do not repeat these. Each was implemented and measured.

| Attempt | Outcome |
| --- | --- |
| Grid mask + half-space, region by connectivity | Sliced the whole model wherever the piece joins it in the cut plane (issue #3: an arm at the shoulder took the torso with it) |
| Same, region clipped to the flattened line | Nothing separated: the line is a ring of chords sitting inside the true section curve, leaving a hair of material that holds the parts together |
| Sideways growth in the cut's plane to break out | Ate into whatever lay beside the piece (issue #2: chipped an armour overhang) |
| Escalating Edge Margin on failure | Reached far outside the line — the direct cause of issues #2 and #3 |
| Per-vertex easing of folded rim points | Broke the armpit case and produced 15-piece garbage on the fin case |
| Global back-off ladder (flatten the rim until clean) | Mechanically fine, but needs a fold test that is both correct and bounded — see below |
| BVH self-overlap as the fold test | Exact, 0.02s when clean, **>400s** when badly folded, i.e. useless exactly when needed |
| 2D proxy (is the rim simple seen down the cut normal?) | Milliseconds, but false-positives on oblique loops — flattened cuts that did not need it |
| Raising lid density to close the gap to the line | Closes the gap, increases folding; net worse |

If the mesh-surgery route stalls and you fall back to repairing the lid, the
missing piece is a **budgeted 3D fold test**: restrict the overlap search to
faces adjacent to the rim (every fold starts there — the interior is a height
field and cannot fold) and stop at the first genuine crossing, so cost is bounded
whether the lid is clean or a mess.

## 5. Code map

| File | Lines | What matters |
| --- | --- | --- |
| `part_pin/surface.py` | 1565 | `cap_sheet`, `build_cap_slab` (**replace these**), `line_samples`, `field_for`, `refit_frame`, `loop_quality`, `inspect_cut`, `trial_cut`, `build_surface_cutter` (the reliable full-extent cutter) |
| `part_pin/core.py` | 804 | `create_parts` (calls the cutter; localized path around line 710), `split_parts_local`, `split_loose`, `drop_debris`, `boolean_apply`, `point_inside` |
| `part_pin/draw_cut.py` | 352 | drawing; leave alone |
| `part_pin/shape_edit.py` | 710 | the editor, live preview (`_rebuild_cap`), the marks (`_inspect`), `T` = `_try_cut` |
| `part_pin/ops.py`, `props.py`, `ui.py` | 686/333/243 | operators, settings, panel |
| `tests/smoke_test.py` | 2323 | 45 scenarios, 269 checks |

Useful existing helpers: `core.point_inside`, `core.mesh_issues` (closed/manifold
check), `core.split_loose`, `surface.surface_gap`, `surface.evaluated`.

## 6. How to test — this matters

Everything geometric runs headless. **There is no Blender install on the dev
machine**; the tests run against the `bpy` PyPI wheel:

```sh
# already set up in this workspace
./.venv-bpy/bin/python tests/smoke_test.py

# on a machine with Blender
blender --background --python-exit-code 1 --python tests/smoke_test.py
```

`import bpy` must come **before** `import bmesh` with the wheel. The wheel needs
a matching CPython (5.1+ → cp313; installed via `brew install python@3.13`).

### Fixtures already available

`make_limb`, `make_limb_with_fin`, `make_shoulder`, `make_shoulder_arm`,
`make_cube_model`, `make_mushroom`, `make_two_spheres`, `make_sphere`, plus
stroke generators `collar_stroke` (ray-cast, like the drawing tool),
`waist_stroke`, `hand_stroke`, and `collar_cut` (stroke → cut).

**Use ray-cast strokes, not `project_to_surface`.** Three separate test bugs in
this session came from nearest-point projection landing the line on a different
feature than a user's click would; the real tool always ray-casts.

### The bar for the new cutter

Every one of these must pass before shipping. The first four are existing
scenarios; the last three are new.

1. `make_limb` + collar → 2 closed parts, volume conserved to 1%.
2. `make_cube_model` + waist stroke → 2 closed parts **at 13, 16 and 24 points**
   (corners must not be cut across).
3. `make_shoulder_arm` + collar under the armpit → 2 closed parts, arm ~15% of
   the volume, **body still spanning its full height** (issue #3).
4. `make_shoulder` + collar clear of the overhang → 2 closed parts, **no chip**
   off the overhang (issue #2).
5. `make_limb_with_fin` + collar → either 2 closed parts, or a clear report; it
   must **never** return an empty boolean or fragments. This is the case that
   currently produces 69 crossing faces.
6. The cut lands on the line: every sample of `line_samples` within ~0.2% of the
   model's size of the cut seam.
7. Nothing left behind: no `PartPin_Trial`/`PartPin_Cap` objects in the scene,
   model untouched, `core.mesh_issues` == (0, 0) on every part.

Run the suite **three times** — a bmesh-ordering bug once made results vary
between runs (fixed by walking in coordinate order in `order_wire_loops`; keep
that property).

Also keep these two guards, which caught real bugs:

- **operator wiring audit** — every `self.helper()` an operator calls and every
  `self.attribute` it reads must exist. A modal's `invoke` cannot run headlessly,
  and a renamed helper crashed the user on the click that closes a drawn loop.
- **no leftover settings** — every setting the panel shows must be used
  somewhere.

## 7. Diagnostics: keep them honest

`surface.inspect_cut` marks four things on the model (red = the cut surface folds,
amber = it cannot break out, yellow = the line is off the model, violet = the cut
runs through open space). Two rules learned the hard way:

- **Silent when the cut works.** Marks shown on a working cut destroyed the
  user's trust in them twice. Verify the negative case as carefully as the
  positive: the limb, cube and shoulder cuts must produce **zero** marks.
- **Marks must be where the problem is.** A fold test that walked one boundary
  and compared it against another put marks all over the model, nowhere near the
  line. If the new construction cannot fold, delete the red category rather than
  leave it firing on nothing.

`trial_cut` (bound to **T** in the editor) makes the cut on a copy and reports
what happens. Keep it: it is the only honest answer to "will this work".

## 8. What to delete once the new cutter works

- `cap_sheet`, `build_cap_slab`, `cap_preview_tris`'s dependence on them,
  `find_join_hints`, `CAP_STEP_OUT`, `pp_undercut` (the undercut only exists to
  push a synthesised rim further out).
- The `FOLDED` mark, if folds become impossible.
- Whatever else stops being reachable. **Dead code is what let the last crash
  through** — a helper renamed inside code nothing exercised. `surface.py` was
  1984 lines with 687 of them unreachable; delete aggressively and re-run the
  suite to prove it.

## 9. Release process

```sh
./build.sh                       # writes dist/part_pin-<version>.zip
gh release create vX.Y.Z dist/part_pin-X.Y.Z.zip --repo matthewslade/PartPin \
    --target main --title "..." --notes-file /tmp/notes.md
```

Bump the version in **both** `part_pin/blender_manifest.toml` and
`part_pin/__init__.py`. Use `--notes-file`, not `--notes`: backticks in release
notes get executed by the shell. Push with `git push partpin HEAD:main` (the
`partpin` remote; the local branch name differs deliberately).

**Never mention CatPin or any other product in anything public** — repo, code,
commits, release notes. That is an explicit standing instruction from the user.

## 10. How to work with this user

- They read renders better than my instrumentation did. Twice they diagnosed a
  bug correctly from a screenshot when my measurements had it wrong. Take their
  reading seriously and reproduce it before theorising.
- Ask for the model. Every diagnosis in this session came from screenshots, and
  each was partly right and partly wrong. A cropped or decimated copy of the
  shoulder region would have saved most of the churn.
- Say plainly when something is not sound. The session went badly when I kept
  adding features on top of an unreliable core instead of stopping to say so.
