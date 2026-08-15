"""Property definitions for PartPin."""

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

CONNECTOR_SHAPES = [
    ('CYLINDER', "Cylinder", "Straight pin with chamfered ends"),
    ('TAPER', "Tapered", "Double-tapered pin — self-centering, easiest to insert"),
    ('BOX', "Box", "Rectangular key — stops parts rotating against each other"),
    ('CUSTOM', "Custom Mesh", "Use your own object as the connector shape"),
]

AXIS_ITEMS = [
    ('X', "X", "Cut across the X axis (plane normal = X)"),
    ('Y', "Y", "Cut across the Y axis (plane normal = Y)"),
    ('Z', "Z", "Cut across the Z axis (plane normal = Z)"),
]

EXPORT_FORMATS = [
    ('STL', "STL", "Export parts as STL (most slicers)"),
    ('OBJ', "OBJ", "Export parts as Wavefront OBJ"),
    ('FBX', "FBX", "Export parts as FBX"),
]


def _target_poll(self, obj):
    # Parts count as models. Cutting a part again is the ordinary way to get
    # a model down to a printable size, and leaving them out of the list left
    # the only way of doing it being to clear the role by hand.
    return obj.type == 'MESH' and obj.pp_role in ("", 'PART')


def _custom_poll(self, obj):
    return obj.type == 'MESH' and not obj.pp_role


def _mark_sized(self, context):
    # Once the user (or Auto Size) touches the dimensions, stop
    # auto-deriving them when connectors are added.
    if not self.sized:
        self.sized = True


class PartPinControlPoint(bpy.types.PropertyGroup):
    """One anchor of a cut line, in the *model's* local space.

    `face` is the model face it last sat on — a starting hint for the walker
    that saves looking the anchor up from scratch on every rebuild.
    """

    co: FloatVectorProperty(size=3, subtype='XYZ')
    loop: IntProperty(default=0)
    face: IntProperty(default=-1)


class PartPinSettings(bpy.types.PropertyGroup):
    target: PointerProperty(
        name="Model",
        description="Mesh to split into printable parts",
        type=bpy.types.Object,
        poll=_target_poll,
    )

    plane_axis: EnumProperty(
        name="Axis",
        description="Normal of the new straight cut plane",
        items=AXIS_ITEMS,
        default='Z',
    )

    # ------------------------------------------------------------------
    # Connector defaults (applied to newly added connectors)
    # ------------------------------------------------------------------
    shape: EnumProperty(
        name="Shape",
        description="Connector shape used for new connectors",
        items=CONNECTOR_SHAPES,
        default='TAPER',
    )
    custom_object: PointerProperty(
        name="Custom",
        description="Object used as the connector when shape is Custom Mesh",
        type=bpy.types.Object,
        poll=_custom_poll,
    )
    size: FloatProperty(
        name="Width",
        description="Connector width / diameter",
        default=0.0,
        min=0.0,
        subtype='DISTANCE',
        precision=4,
        update=_mark_sized,
    )
    length: FloatProperty(
        name="Length",
        description="How far the pin reaches into each part from the cut face",
        default=0.0,
        min=0.0,
        subtype='DISTANCE',
        precision=4,
        update=_mark_sized,
    )
    clearance: FloatProperty(
        name="Clearance",
        description=(
            "Gap between pin and socket. The socket is offset outwards by "
            "this amount so printed parts actually fit (FDM typically needs "
            "0.1–0.3 mm)"
        ),
        default=0.0,
        min=0.0,
        subtype='DISTANCE',
        precision=5,
    )
    count: IntProperty(
        name="Count",
        description="Number of connectors to place along the cut",
        default=2,
        min=1,
        max=32,
    )
    sized: BoolProperty(default=False, options={'HIDDEN'})

    # ------------------------------------------------------------------
    # On-surface cut editing
    # ------------------------------------------------------------------
    handle_points: IntProperty(
        name="Points",
        description=(
            "How many draggable points to place around each cut line. Corners "
            "always get one, so this is a guide rather than a rule — more of "
            "them follow a drawn line more closely and give finer control"
        ),
        default=32,
        min=3,
        max=96,
    )
    line_lift: FloatProperty(
        name="Line Lift",
        description=(
            "How far above the model's surface the cut line and its points "
            "are drawn, as a fraction of the model's size. Enough to keep the "
            "line clear of the surface it lies on, which would otherwise hide "
            "parts of it. Drawing only — the cut itself does not move"
        ),
        default=0.0015,
        min=0.0,
        max=0.02,
        precision=4,
    )
    auto_snap_connectors: BoolProperty(
        name="Snap Connectors",
        description=(
            "After reshaping a cut, move its connectors back onto the new "
            "surface and align them to it"
        ),
        default=True,
    )

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------
    keep_original: BoolProperty(
        name="Keep Original",
        description="Hide the original model instead of deleting it",
        default=True,
    )
    part_gap: FloatProperty(
        name="Part Gap",
        description="Move final parts apart by this distance so they are easy to inspect",
        default=0.0,
        min=0.0,
        subtype='DISTANCE',
    )
    ignore_validation: BoolProperty(
        name="Skip Mesh Check",
        description="Attempt to cut even if the mesh is not closed/manifold (results may be broken)",
        default=False,
    )

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    parts_collection: PointerProperty(
        name="Parts",
        description="Collection holding the final parts to export",
        type=bpy.types.Collection,
    )
    export_format: EnumProperty(
        name="Format",
        items=EXPORT_FORMATS,
        default='STL',
    )
    export_batch: BoolProperty(
        name="One File per Part",
        description="Write each part to its own file instead of a single file",
        default=True,
    )
    export_scale: FloatProperty(
        name="Scale",
        description=(
            "Global export scale. If you model at 1 unit = 1 m with real-world "
            "sizes, use 1000 so slicers (which read STL as mm) get the right size"
        ),
        default=1.0,
        min=0.0001,
        max=100000.0,
    )
    export_dir: StringProperty(
        name="Folder",
        description="Directory the part files are written to",
        subtype='DIR_PATH',
        default="//parts/",
    )


CLASSES = (PartPinControlPoint, PartPinSettings)


def register():
    # Per-object metadata. Registered RNA props do not clutter the custom
    # properties panel and survive save/load.
    bpy.types.Object.pp_role = StringProperty(default="", options={'HIDDEN'})
    bpy.types.Object.pp_cut_kind = StringProperty(default="", options={'HIDDEN'})
    bpy.types.Object.pp_index = IntProperty(default=0, options={'HIDDEN'})
    bpy.types.Object.pp_enabled = BoolProperty(
        name="Enabled",
        description="Disabled cuts are ignored when creating parts",
        default=True,
    )
    bpy.types.Object.pp_shape = StringProperty(default='TAPER', options={'HIDDEN'})
    bpy.types.Object.pp_clearance = FloatProperty(
        name="Clearance",
        description="Socket offset for this connector",
        default=0.0,
        min=0.0,
        subtype='DISTANCE',
        precision=5,
    )
    bpy.types.Object.pp_pin_flip = BoolProperty(
        name="Flip Pin Side",
        description="Put the pin on the opposite part",
        default=False,
    )

    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.part_pin = PointerProperty(type=PartPinSettings)

    # Cut-line data (registered after PartPinControlPoint exists).
    bpy.types.Object.pp_points = CollectionProperty(type=PartPinControlPoint)
    bpy.types.Object.pp_main_loop = IntProperty(
        name="Main Cut Line",
        description=("Which cut line the cut's own frame follows — the last "
                     "one edited. -1 picks the longest"),
        default=-1,
        options={'HIDDEN'},
    )
    # Anchors used to be kept in the cut object's space. A cut saved that way
    # is brought across the first time it is read; this says which it is.
    bpy.types.Object.pp_anchor_space = IntProperty(default=0,
                                                   options={'HIDDEN'})
    bpy.types.Object.pp_local = BoolProperty(
        name="Cut Inside Line Only",
        description=(
            "Cut only the region ring-fenced by this cut's line, leaving "
            "the rest of the model whole. Turn off to let a flat cut through "
            "the line's own plane carry on and split everything it meets"
        ),
        default=True,
    )


def unregister():
    del bpy.types.Object.pp_points
    del bpy.types.Scene.part_pin
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    for attr in (
        "pp_role", "pp_cut_kind", "pp_index", "pp_enabled",
        "pp_shape", "pp_clearance", "pp_pin_flip",
        "pp_local", "pp_main_loop", "pp_anchor_space",
    ):
        delattr(bpy.types.Object, attr)
