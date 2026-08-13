# PartPin — split models into printable parts

A free Blender add-on for cutting a model into 3D-printable parts. You draw
the perimeter of the cut straight onto the model, adjust it, and cut. Matching
**pin / socket connectors** hold the parts together, and the finished parts
export ready for slicing.

Works in **Blender 4.2+** on Windows, macOS and Linux. Needs a **closed,
manifold mesh** (as does every boolean-based cutter).

## Install

1. Download `part_pin-<version>.zip` from the
   [Releases page](https://github.com/matthewslade/PartPin/releases), or build
   it yourself:

   ```sh
   ./build.sh   # writes dist/part_pin-<version>.zip
   ```

2. In Blender: **Edit ▸ Preferences ▸ Add-ons ▸ ⌄ ▸ Install from Disk…** and
   pick the zip. **Restart Blender** if you are updating — Python keeps the old
   copy loaded otherwise.

3. The panel is in the 3D Viewport sidebar: press **N ▸ PartPin**.

## The short version

**Pick the model → Draw Cut on Model → adjust the line → press T → Create
Parts → Export.**

## 1. Pick the model

Set **Model** in the panel. **Check Mesh** confirms it is closed and manifold;
if it is not, repair it first (Mesh ▸ Clean Up, Remesh, or the 3D-Print
Toolbox add-on that ships with Blender).

## 2. Draw the cut on the model

Hit **Draw Cut on Model** and draw the perimeter of the cut onto the surface.
Every position is ray-cast onto the model, so the line goes exactly where you
put it.

| Action | Result |
| --- | --- |
| Hold left mouse and draw | Lay the line onto the model |
| Let go, orbit, draw again | Carry on round the far side — the line joins up across the model, following its faces |
| Close at the green dot | Finish the loop and go straight to adjusting it |
| Enter | Close the loop from wherever you are |
| Backspace | Undo the last stretch |
| Esc | Cancel |

The green dot marks where you started, and turns yellow when you are close
enough to close on it. Corners are kept: a point lands on each corner you
draw, so a line round a boxy shape keeps its corners instead of being rounded
off.

**Or start from a shape** if that suits the model better — a **Straight** cut
plane you move and rotate like any object, or **Draw Across Model** for a
single stroke that cuts along your view direction. Either can then be edited on
the surface exactly like a drawn line.

## 3. Adjust the line on the model

Closing a drawn loop drops you straight in here; otherwise hit **Edit Cut on
Surface**. The cut object disappears and you get the line where the cut meets
your model, with draggable points on it, and the **shaded surface that will do
the cutting** drawn through the model.

| Action | Result |
| --- | --- |
| Drag a point | It slides along the surface; the line and the cut surface follow it |
| Ctrl+Click | Add a point |
| X | Remove the point under the cursor |
| Alt+X | Remove a whole cut line |
| Ctrl+Wheel | Widen or tighten the falloff — how far each point's pull spreads |
| **T** | **Try the cut** on a copy and say whether it separates |
| Enter | Confirm |
| Esc | Revert |

Middle-mouse and the wheel still orbit and zoom, so you can spin the model
around while editing.

### The warnings

After every drag, the cut is measured against your model and anything wrong
with it is marked where it is wrong. A cut with nothing wrong is marked nothing
at all.

| Mark | Meaning | What to do |
| --- | --- | --- |
| **Red** | The cut surface folds onto itself there. The solver cannot use a folded surface and returns nothing, which is the commonest reason a cut does nothing at all | Spread those points apart, or take the line a shorter way round |
| **Amber** | The cut cannot break out of the model there, so material wraps round it and holds the piece on | Move the line to where the piece is clear of what holds it, or raise *Undercut* |
| **Yellow** | The line has come off the model there | Drag those points back onto it |
| **Violet** | The cut runs through open space well inside the line, so the line encloses more than solid material | Bring the line in closer round the piece |

Press **T** at any point for a straight answer: *"This cut separates into 2
parts"*, or what is stopping it.

## 4. Connectors

With a cut active, **Add Connectors** places pins along it. They are ordinary
objects: move, rotate, scale, duplicate (Shift+D) or delete them.

- **Shape** — Cylinder, Tapered (self-centering, the default), Box
  (anti-rotation), or **Custom Mesh** (apply scale on your object first; its
  mesh is used as-is).
- **Clearance** — how much bigger the socket is than the pin. FDM printers
  usually want 0.1–0.3 mm. Print one and check before committing.
- **Flip Pin** — swap which part gets the pin and which the socket. Flipped
  pins turn blue (enable *Viewport Shading ▸ Color: Object* to see it).
- **Auto Size** — derive pin sizes from the model; it also happens by itself
  the first time you add connectors.
- **Snap Connectors** re-seats pins after you reshape a cut.

## 5. Create Parts, and export

**Create Parts** applies every enabled cut, unions the pins into their parts
and subtracts clearance-fattened sockets from the mating parts. The parts land
in a new `<Model> Parts` collection; the original is kept hidden unless you
untick *Keep Original*. *Part Gap* moves the finished parts apart so you can
look at the seams.

**Export** writes STL, OBJ or FBX, one file per part or a single file. If you
model true-to-scale in metres, set *Scale* to 1000 so slicers — which read STL
units as millimetres — get the size right. If you already work at 1 unit =
1 mm, leave it at 1.

**Easy mode**, at the bottom of the panel, does cut → connectors → parts in one
click for simple models: pick an axis, then **Cut at Center** or **At Cursor**.

## What the cut actually does

Your perimeter is spanned by a lid — a surface drawn inwards from the line —
and that lid, thickened to a hair, is what cuts. So **the perimeter decides
everything**: there is no plane to flatten your line onto, and nothing reaching
sideways into the model.

Only the material inside the line is cut. Draw round an arm at the armpit and
the arm comes away with the body left whole, even though the cut's plane would
carry on through it. About 0.01% of the model's volume is spent at the seam,
far below what a printer resolves.

The lid's rim steps out through the model's surface to break the piece free —
following the surface's own outward normal, so at a crease it steps out of the
crease rather than into the body. *Undercut* lets it reach further, for a piece
that starts buried inside another part of the model.

## Settings worth knowing

| Setting | What it does |
| --- | --- |
| **Points** | How many draggable points a new line gets. Corners always get one, so the count is a guide rather than a rule |
| **Cut Detail** | How finely the cut surface is built. Higher follows the line more closely; lower is steadier on an awkward line, where a fine surface is likelier to fold |
| **Line Lift** | How far above the surface the line is drawn, so the surface cannot swallow it. Drawing only — the cut does not move |
| **Undercut** | How far the cut may reach into the model around the line, to free a recessed piece. 0 by default |
| **Cut Inside Line Only** | On by default. Off lets the cut surface carry on and split everything it meets |

## Testing

The whole geometry side runs headless — no UI needed:

```sh
blender --background --python-exit-code 1 --python tests/smoke_test.py
```

45 scenarios covering cuts, connectors, drawing, the warnings and all three
exporters, checking that every part comes out closed and manifold. On macOS the
binary is usually `/Applications/Blender.app/Contents/MacOS/Blender`.

## Limits worth knowing

- **The cut surface can fold onto itself** on an awkward line, and the exact
  solver then returns nothing rather than a cut. It is marked in red where that
  happens, and lowering *Cut Detail* or spreading the points usually clears it.
  This is the roughest edge in the tool.
- A cut only separates a piece attached **inside** the line. If something
  crosses the line — a strap, a fin, a spike — the piece cannot come away
  without cutting that too, and the cut says so rather than doing it.
- Boolean cutting is exact but not instant: multi-million-poly meshes take a
  while. Cut before subdividing where you can.
- One cut has one plane. Lines facing more than about 45° away from the one you
  last edited are skipped with a warning — give each such region its own cut.
- Drawing and dragging follow the visible surface, so orbit first to reach the
  far side.
- Modifiers on the model are baked into the parts.

## License

MIT — see [LICENSE](LICENSE).
