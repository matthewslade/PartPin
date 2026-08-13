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
    return obj.type == 'MESH' and not obj.pp_role


def _custom_poll(self, obj):
    return obj.type == 'MESH' and not obj.pp_role


def _mark_sized(self, context):
    # Once the user (or Auto Size) touches the dimensions, stop
    # auto-deriving them when connectors are added.
    if not self.sized:
        self.sized = True


class PartPinControlPoint(bpy.types.PropertyGroup):
    """One draggable point on a surface cut, in the cut's local space."""

    co: FloatVectorProperty(size=3, subtype='XYZ')
    loop: IntProperty(default=0)


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
            "How many draggable points to place around each cut line when "
            "surface editing starts"
        ),
        default=16,
        min=3,
        max=96,
    )
    surface_resolution: IntProperty(
        name="Surface Detail",
        description=(
            "Grid resolution of a reshaped cut surface. Higher follows the "
            "dragged points more closely but cuts more slowly"
        ),
        default=48,
        min=8,
        max=160,
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

    # Surface-cut shape data (registered after PartPinControlPoint exists).
    bpy.types.Object.pp_points = CollectionProperty(type=PartPinControlPoint)
    bpy.types.Object.pp_main_loop = IntProperty(
        name="Main Cut Line",
        description=("Which cut line sets this cut's plane — the last one "
                     "edited. -1 picks the longest"),
        default=-1,
        options={'HIDDEN'},
    )
    bpy.types.Object.pp_local = BoolProperty(
        name="Cut Inside Line Only",
        description=(
            "Cut only the region ring-fenced by this cut's line, leaving "
            "the rest of the model whole. Turn off to let the cut surface "
            "carry on and split everything it meets"
        ),
        default=True,
    )
    bpy.types.Object.pp_margin = FloatProperty(
        name="Edge Margin",
        description=(
            "How far past the cut line the cut reaches, as a fraction of "
            "the line's size. Just enough to break through the surface — "
            "raise it if the cut fails to separate the region"
        ),
        default=0.05,
        min=0.001,
        max=0.5,
    )
    bpy.types.Object.pp_undercut = FloatProperty(
        name="Undercut",
        description=(
            "How far the cut may reach into the model around the line, as a "
            "fraction of the line's size. Raise it to free a piece that is "
            "recessed into the model — an arm buried under a shoulder — "
            "which cannot come away without cutting a little of what holds "
            "it. Leave at 0 to keep the cut to the line"
        ),
        default=0.0,
        min=0.0,
        max=0.5,
    )
    bpy.types.Object.pp_falloff = FloatProperty(
        name="Falloff",
        description=(
            "How far a dragged point's influence spreads across the cut, "
            "in multiples of the spacing between points. Low is local and "
            "sharp, high is broad and smooth"
        ),
        default=1.5,
        min=0.2,
        max=8.0,
    )


def unregister():
    del bpy.types.Object.pp_points
    del bpy.types.Object.pp_falloff
    del bpy.types.Scene.part_pin
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    for attr in (
        "pp_role", "pp_cut_kind", "pp_index", "pp_enabled",
        "pp_shape", "pp_clearance", "pp_pin_flip",
        "pp_local", "pp_margin", "pp_main_loop", "pp_undercut",
    ):
        delattr(bpy.types.Object, attr)
