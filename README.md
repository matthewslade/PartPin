# PartPin — split models into printable parts

A free Blender add-on that splits a model into 3D-printable parts,
adds matching **pin / socket connectors** with printable clearance,
and exports the parts ready for slicing.

Works in **Blender 4.2+** on Windows, macOS and Linux. Requires a
**closed, manifold mesh** (like every boolean-based cutter).

## Install

1. Download `part_pin-<version>.zip` from the
   [Releases page](https://github.com/matthewslade/PartPin/releases),
   or build it from source:

   ```sh
   ./build.sh   # writes dist/part_pin-<version>.zip
   ```

2. In Blender: **Edit ▸ Preferences ▸ Add-ons ▸ (v) dropdown ▸ Install from
   Disk…** and pick the zip. Enable **PartPin** if it isn't already.

3. The panel lives in the 3D Viewport sidebar: press **N ▸ PartPin** tab.

## Workflow (Draft mode — non-destructive)

1. **Pick the model** in the PartPin panel, optionally hit **Check Mesh**
   (it must be closed/manifold — repair with Remesh or the bundled
   3D-Print Toolbox add-on if not).
2. **Add cuts:**
   - **Straight** — adds a wire cut plane through the 3D cursor (or model
     center). Move / rotate / scale it like any object. Pick the axis in
     the dropdown next to the button.
   - **Draw Curved Cut** — orient your view first, then draw one freehand
     stroke across the model (start and end outside the silhouette) and
     click **Finish Drawing**. The stroke becomes a cut surface extruded
     along your view direction. A closed loop stroke cuts out a plug.
   - Cuts are listed in the panel — click to select, tick to
     enable/disable, ✕ to delete. Nothing touches the model until you
     confirm.
3. **Fine-tune the cut on the model** (optional, and the nicest way to
   work) — hit **Edit Cut on Surface**. The cut plane itself disappears
   and you get the actual line where the cut meets your model, with
   draggable points sitting on its surface:

   | Action | Result |
   | --- | --- |
   | Drag a point | It slides along the model's surface; the cut reshapes to pass through it |
   | Ctrl+Click | Add a point on the surface |
   | X | Remove the point under the cursor |
   | Alt+X | Remove a whole cut line (that region stops being cut) |
   | C | Re-check the line and mark what would stop it cutting |
   | Ctrl+Wheel | Widen / tighten the falloff (how far each point's pull spreads) |
   | Enter | Confirm |
   | Esc | Revert |

   Middle-mouse and the scroll wheel still orbit and zoom, so you can
   spin the model around while editing.

   Because a plane is defined by only three points, dragging points
   turns the cut into a **free-form cut surface**: a smooth surface that
   passes exactly through every point you place. It is stored as a
   height field over the original plane, which means it can never fold
   back on itself — the cut always yields clean, closed parts.
   **Flatten** returns a reshaped cut to a plane, and **Snap
   Connectors** re-seats existing pins onto the new surface.

   **The cut stops at the line** (*Cut Inside Line Only*, on by default).
   Only the region ring-fenced by the cut line is severed — the cut does
   not carry on as an endless plane through the rest of the model. Draw a
   line round a head, an arm or a knob and only that comes off; anything
   else the old plane happened to pass through is left whole. The piece
   may be far wider than the line that fences it (a mushroom head on a
   thin stalk comes off intact), because the region is found by following
   the model, not by clipping to the line's outline.

   Drag the line as far as you like — right round a limb, even to a
   plane at right angles to where the cut started. The cut's plane
   re-fits itself to the line you are editing, so the region it fences
   is always one it can actually cut.

   **What gets cut:** the material continuous with what lies inside your
   line, and nothing else. A fin or spike welded across the line is part
   of the piece, so it is cut through; a separate feature that merely
   passes close by — an armour overhang beside a shoulder — is left
   whole. If two features come closer together than one grid cell they
   can read as one, so the cut refines the grid by itself when the region
   reaches suspiciously far past the line; raise *Surface Detail* if a
   piece you did not ring still comes off.

   **When a cut won't separate, the line shows you why.** Trouble spots
   are marked on the line as you work — amber where material carries on
   just outside the line so the cut cannot reach clear of it, red where
   material runs well past it (a strap, a fin, a spike bridging the two
   halves). Press **C** to re-check, or use **Check Line** in the panel.
   **Fix Margin** sets Edge Margin to the reach the line needs. The cut
   never widens its own reach into neighbouring material to force a
   separation — it reports instead, because quietly taking a bite out of
   the feature next door is worse than saying it could not do it.

   A cut picks up a line on **every** feature it crosses, and each one
   cuts. Hover a line and press **Alt+X** to drop the ones you don't
   want. Lines that end up in a different plane from the one you are
   editing cannot be cut alongside it: those are skipped and reported,
   so use a separate cut for each region facing a different way. Untick
   *Cut Inside Line Only* for the old behaviour of splitting everything
   the surface meets.

4. **Add connectors** — with a cut active, **Add Connectors** places
   `Count` pins spaced along the cut cross-section. They are ordinary
   draft objects: move, rotate, scale, duplicate (Shift+D) or delete
   them. Shapes: **Cylinder**, **Tapered** (self-centering, the default),
   **Box** (anti-rotation), or **Custom Mesh** (apply scale on your
   custom object first — its mesh is used as-is).
   - **Clearance** is how much bigger the socket is than the pin.
     FDM printers typically want 0.1–0.3 mm — print a test first.
   - **Flip Pin** swaps which side gets the pin vs. the socket
     (flipped connectors turn blue — enable *Viewport Shading ▸ Color:
     Object* to see it).
   - **Auto Size** derives sensible pin dimensions from the model; it
     also happens automatically the first time you add connectors.
5. **Create Parts** — applies every enabled cut, unions the pins into
   their parts and subtracts clearance-fattened sockets from the mating
   parts. Final parts land in a new `<Model> Parts` collection; the
   original is kept hidden (untick *Keep Original* to discard it).
   *Part Gap* moves the finished parts apart so you can inspect the
   seams.
6. **Export** — STL, OBJ or FBX; one file per part or a single file.
   If you model true-to-scale in meters, set *Scale* to 1000 so slicers
   (which read STL units as mm) get the size right. If you already work
   "1 unit = 1 mm", leave it at 1.

## Easy mode

For simple models: pick the axis, then **Cut at Center** (or **At
Cursor**). One click does cut → auto connectors → final parts, no draft
step.

## Features

- Draft mode: multiple editable cuts — enable, disable or remove them
  before anything is applied
- Easy mode: one-click cut for simple models
- Straight cuts with a movable / rotatable / scalable plane
- Drawn curved cuts (freehand stroke in the viewport)
- On-surface fine-tuning: drag the cut line's points along the model to
  reshape the cut, with add/remove points and adjustable falloff
- Trouble spots marked on the cut line when a cut cannot separate
- Neighbouring features are never cut: only material continuous with the
  region inside the line is taken
- Localized cuts: only the region ring-fenced by the cut line is severed,
  leaving the rest of the model whole
- Built-in connector shapes plus custom connector meshes
- Connector position / rotation / scale editing and pin-side flip
- Adjustable pin/socket clearance, per connector
- Final parts land in a new collection; the original model stays untouched
- STL / OBJ / FBX export, one file per part or a single file

## Testing

Headless smoke test (no UI needed) — runs cuts, connectors, surface
reshaping, easy mode, validation and all three exporters, and checks
every part comes out closed and manifold:

```sh
blender --background --python-exit-code 1 --python tests/smoke_test.py
```

On macOS the binary is usually
`/Applications/Blender.app/Contents/MacOS/Blender`.

## Notes & limitations

- Boolean cutting is exact but not instant: multi-million-poly meshes
  take a while. Cut before subdividing when you can.
- Curved cuts assume one stroke drawn across the model (or a closed
  loop). Strongly self-intersecting scribbles won't produce a valid cut
  volume.
- On-surface editing needs the cut line to stay a simple shape when
  viewed down the cut's normal. A drawn cut that doubles back on itself
  that severely is refused with a message rather than cut badly — edit
  its stroke instead.
- Dragged points follow the visible surface, so to move one round the
  back of the model, orbit the view first.
- Surface cuts are graded by *Surface Detail*: raise it if a reshaped cut
  looks faceted, lower it if cutting gets slow.
- A localized cut removes a hair of material at the seam (0.01% of the
  model — 0.02 mm on a 200 mm print), so the two faces mate with a gap far
  below what a printer resolves.
- A localized cut needs its line to close around a region. If the line
  runs off the model, doubles back so it fences nothing, or *Edge Margin*
  is too small to break through the surface, the cut says so instead of
  silently doing nothing.
- Features closer together than one grid cell can be read as one. The cut
  refines the grid automatically when it suspects this, but on very fine
  detail raise *Surface Detail* if the wrong piece comes off.
- One cut has one plane. Lines facing more than about 45° away from the
  line you last edited are skipped with a warning — give each such region
  its own cut.
- Modifiers on the model are baked into the parts.
- Work on one model at a time; *Clear All Cuts* resets the drafts.

## License

MIT — see [LICENSE](LICENSE).
