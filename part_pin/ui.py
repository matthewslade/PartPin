"""Sidebar panels for PartPin (3D Viewport ▸ N-panel ▸ PartPin)."""

import os
import re
import sys

import bpy

from . import core, ops


class PARTPIN_PT_base:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PartPin"


def version():
    """The add-on's version, as it is written on the release.

    Read from the manifest, which is the one file Blender itself installs
    from. `bl_info` is not there to read: installed as an extension — which
    is how Blender 4.2 and later install this — the package is loaded as
    `bl_ext.user_default.part_pin` and Blender takes `bl_info` away, so
    reaching for it threw on every redraw of the panel. It is still the
    answer for a legacy add-on install, so it is the fallback.
    """
    global _VERSION
    if _VERSION is not None:
        return _VERSION
    found = None
    manifest = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "blender_manifest.toml")
    try:
        with open(manifest, encoding="utf-8") as handle:
            written = re.search(r'^version\s*=\s*"([^"]+)"', handle.read(),
                                re.MULTILINE)
        found = written.group(1) if written else None
    except OSError:
        found = None
    if found is None:
        legacy = getattr(sys.modules.get(__package__), "bl_info", None)
        if legacy:
            found = ".".join(str(part) for part in legacy.get("version", ()))
    _VERSION = found or "?"
    return _VERSION


# Worked out once: the manifest cannot change under a running Blender.
_VERSION = None


class PARTPIN_PT_main(PARTPIN_PT_base, bpy.types.Panel):
    bl_idname = "PARTPIN_PT_main"
    bl_label = "PartPin"

    def draw(self, context):
        layout = self.layout
        s = core.get_settings(context)
        layout.prop(s, "target")
        row = layout.row(align=True)
        row.operator("partpin.check_mesh", icon='CHECKMARK')
        layout.label(text="A closed, manifold mesh cuts cleanest",
                     icon='INFO')


class PARTPIN_PT_cuts(PARTPIN_PT_base, bpy.types.Panel):
    bl_idname = "PARTPIN_PT_cuts"
    bl_parent_id = "PARTPIN_PT_main"
    bl_label = "Cuts"

    def draw(self, context):
        layout = self.layout
        s = core.get_settings(context)
        scene = context.scene

        active = context.view_layer.objects.active
        col = layout.column()
        col.scale_y = 1.5
        col.operator("partpin.draw_cut_line", icon='GREASEPENCIL',
                     text="Draw Cut on Model")
        layout.label(text="Draw the perimeter where you want the cut",
                     icon='INFO')

        box = layout.box()
        box.label(text="Or cut straight across", icon='MESH_PLANE')
        row = box.row(align=True)
        row.operator("partpin.add_plane_cut", icon='MESH_PLANE',
                     text="Straight")
        row.prop(s, "plane_axis", text="")

        cuts = core.scene_cuts(scene)
        if cuts:
            box = layout.box()
            for cut in cuts:
                row = box.row(align=True)
                icon = 'CHECKBOX_HLT' if cut.pp_enabled else 'CHECKBOX_DEHLT'
                row.operator("partpin.cut_toggle", text="", icon=icon,
                             emboss=False).cut_name = cut.name
                kind_icon = ('SURFACE_NSURFACE'
                             if cut.pp_cut_kind == 'SURFACE'
                             else 'MESH_PLANE')
                row.operator("partpin.cut_select", text=cut.name,
                             icon=kind_icon,
                             depress=(cut == active)).cut_name = cut.name
                row.operator("partpin.cut_remove", text="", icon='X',
                             emboss=False).cut_name = cut.name
            layout.operator("partpin.clear_drafts", icon='TRASH')
        else:
            layout.label(text="No cuts yet", icon='DOT')


class PARTPIN_PT_shape(PARTPIN_PT_base, bpy.types.Panel):
    bl_idname = "PARTPIN_PT_shape"
    bl_parent_id = "PARTPIN_PT_cuts"
    bl_label = "Fine-Tune on Surface"

    def draw(self, context):
        layout = self.layout
        s = core.get_settings(context)
        active = context.view_layer.objects.active
        cut = None
        if active is not None:
            if active.pp_role == core.ROLE_CUT:
                cut = active
            elif active.pp_role == core.ROLE_CONNECTOR:
                cut = active.parent

        col = layout.column()
        col.scale_y = 1.4
        col.operator("partpin.edit_cut_surface", icon='MOD_MESHDEFORM')
        layout.prop(s, "handle_points")

        if cut is not None and cut.pp_cut_kind == 'SURFACE':
            loops = len({p.loop for p in cut.pp_points})
            box = layout.box()
            box.label(text=f"{len(cut.pp_points)} points, "
                           f"{loops} line{'s' if loops != 1 else ''}",
                      icon='SURFACE_NSURFACE')
            box.prop(cut, "pp_local")
            if not cut.pp_local:
                box.label(text="Cuts straight through on the line's own "
                               "plane", icon='CHECKMARK')
            else:
                box.label(text="Untick if the cut will not separate",
                          icon='INFO')
                box.operator("partpin.check_cut_line", text="Try This Cut",
                             icon='ZOOM_ALL')
            row = box.row(align=True)
            row.operator("partpin.snap_connectors", text="Snap Connectors")
            row.operator("partpin.reset_cut_shape", text="Flatten")
            if loops > 1 and cut.pp_local:
                box.label(text="Alt+X while editing drops a line",
                          icon='INFO')
        else:
            layout.label(text="Drag the cut line right on the model",
                         icon='INFO')
        layout.prop(s, "auto_snap_connectors")


class PARTPIN_PT_connectors(PARTPIN_PT_base, bpy.types.Panel):
    bl_idname = "PARTPIN_PT_connectors"
    bl_parent_id = "PARTPIN_PT_main"
    bl_label = "Connectors"

    def draw(self, context):
        layout = self.layout
        s = core.get_settings(context)

        col = layout.column(align=True)
        col.prop(s, "shape")
        if s.shape == 'CUSTOM':
            col.prop(s, "custom_object")
        else:
            col.prop(s, "size")
            col.prop(s, "length")
        col.prop(s, "clearance")
        row = col.row(align=True)
        row.prop(s, "count")
        row.operator("partpin.auto_size", text="", icon='SHADERFX')

        layout.operator("partpin.add_connectors", icon='SNAP_MIDPOINT')
        row = layout.row(align=True)
        row.operator("partpin.update_connectors", text="Apply to Selected")
        row.operator("partpin.flip_pin", text="Flip Pin")

        active = context.view_layer.objects.active
        if active is not None and active.pp_role == core.ROLE_CONNECTOR:
            box = layout.box()
            box.label(text=f"Active: {active.name}", icon='EMPTY_SINGLE_ARROW')
            side = "−Z side" if active.pp_pin_flip else "+Z side"
            box.label(text=f"Pin goes into the part on its {side}")
            box.prop(active, "pp_clearance")
            box.prop(active, "pp_pin_flip", toggle=True)


class PARTPIN_PT_finalize(PARTPIN_PT_base, bpy.types.Panel):
    bl_idname = "PARTPIN_PT_finalize"
    bl_parent_id = "PARTPIN_PT_main"
    bl_label = "Create Parts"

    def draw(self, context):
        layout = self.layout
        s = core.get_settings(context)

        running = ops.RUNNING
        if running is not None:
            fraction, doing = running
            box = layout.box()
            box.label(text=f"Cutting — {int(fraction * 100)}%",
                      icon='MOD_BOOLEAN')
            box.label(text=ops.progress_bar(fraction, 18))
            box.label(text=doing)
            box.label(text="Esc stops it and puts everything back",
                      icon='INFO')
            return

        col = layout.column(align=True)
        col.prop(s, "keep_original")
        col.prop(s, "part_gap")
        col.prop(s, "ignore_validation")

        big = layout.column()
        big.scale_y = 1.6
        big.operator("partpin.create_parts", icon='MOD_BOOLEAN')

        box = layout.box()
        box.label(text="Easy Mode (one click)", icon='SOLO_ON')
        row = box.row(align=True)
        op = row.operator("partpin.easy_cut", text="Cut at Center")
        op.axis = s.plane_axis
        op.at_cursor = False
        op = row.operator("partpin.easy_cut", text="At Cursor")
        op.axis = s.plane_axis
        op.at_cursor = True


class PARTPIN_PT_export(PARTPIN_PT_base, bpy.types.Panel):
    bl_idname = "PARTPIN_PT_export"
    bl_parent_id = "PARTPIN_PT_main"
    bl_label = "Export"

    def draw(self, context):
        layout = self.layout
        s = core.get_settings(context)
        col = layout.column(align=True)
        col.prop(s, "parts_collection")
        col.prop(s, "export_format")
        col.prop(s, "export_scale")
        col.prop(s, "export_batch")
        col.prop(s, "export_dir")
        layout.operator("partpin.export_parts", icon='EXPORT')


class PARTPIN_PT_version(PARTPIN_PT_base, bpy.types.Panel):
    """The version, at the bottom of the panel and nowhere else.

    No header, so it reads as a footer rather than as another thing to open,
    and registered last so it stays at the bottom. Worth having on screen:
    the first thing to establish about a bug is which version it is in, and
    Blender's own add-on list is three clicks away.
    """

    bl_idname = "PARTPIN_PT_version"
    bl_parent_id = "PARTPIN_PT_main"
    bl_label = "Version"
    bl_options = {'HIDE_HEADER'}

    def draw(self, context):
        row = self.layout.row()
        row.alignment = 'RIGHT'
        row.label(text=f"PartPin {version()}")


CLASSES = (
    PARTPIN_PT_main,
    PARTPIN_PT_cuts,
    PARTPIN_PT_shape,
    PARTPIN_PT_connectors,
    PARTPIN_PT_finalize,
    PARTPIN_PT_export,
    PARTPIN_PT_version,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
