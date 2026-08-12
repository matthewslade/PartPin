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
3. **Add connectors** — with a cut active, **Add Connectors** places
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
4. **Create Parts** — applies every enabled cut, unions the pins into
   their parts and subtracts clearance-fattened sockets from the mating
   parts. Final parts land in a new `<Model> Parts` collection; the
   original is kept hidden (untick *Keep Original* to discard it).
   *Part Gap* moves the finished parts apart so you can inspect the
   seams.
5. **Export** — STL, OBJ or FBX; one file per part or a single file.
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
- Built-in connector shapes plus custom connector meshes
- Connector position / rotation / scale editing and pin-side flip
- Adjustable pin/socket clearance, per connector
- Final parts land in a new collection; the original model stays untouched
- STL / OBJ / FBX export, one file per part or a single file

## Testing

Headless smoke test (no UI needed) — runs cuts, connectors, easy mode,
validation and all three exporters, and checks every part comes out
closed and manifold:

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
- Modifiers on the model are baked into the parts.
- Work on one model at a time; *Clear All Cuts* resets the drafts.

## License

MIT — see [LICENSE](LICENSE).
