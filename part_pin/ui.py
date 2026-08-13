"""Sidebar panels for PartPin (3D Viewport ▸ N-panel ▸ PartPin)."""

import bpy

from . import core


class PARTPIN_PT_base:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PartPin"


class PARTPIN_PT_main(PARTPIN_PT_base, bpy.types.Panel):
    bl_idname = "PARTPIN_PT_main"
    bl_label = "PartPin"

    def draw(self, context):
        layout = self.layout
        s = core.get_settings(context)
        layout.prop(s, "target")
        row = layout.row(align=True)
        row.operator("partpin.check_mesh", icon='CHECKMARK')
        layout.label(text="Needs a closed, manifold mesh", icon='INFO')


class PARTPIN_PT_cuts(PARTPIN_PT_base, bpy.types.Panel):
    bl_idname = "PARTPIN_PT_cuts"
    bl_parent_id = "PARTPIN_PT_main"
    bl_label = "Cuts"

    def draw(self, context):
        layout = self.layout
        s = core.get_settings(context)
        scene = context.scene

        active = context.view_layer.objects.active
        drawing = (active is not None and active.type == 'CURVE'
                   and active.pp_role == core.ROLE_CUT
                   and active.mode == 'EDIT')
        if drawing:
            col = layout.column()
            col.scale_y = 1.6
            col.operator("partpin.finish_curve_cut", icon='CHECKMARK')
            layout.label(text="Draw one stroke across the model", icon='GREASEPENCIL')
            return

        col = layout.column()
        col.scale_y = 1.5
        col.operator("partpin.draw_cut_line", icon='GREASEPENCIL',
                     text="Draw Cut on Model")
        layout.label(text="Draw the perimeter where you want the cut",
                     icon='INFO')

        box = layout.box()
        box.label(text="Or start from a shape", icon='MESH_PLANE')
        row = box.row(align=True)
        row.operator("partpin.add_plane_cut", icon='MESH_PLANE',
                     text="Straight")
        row.prop(s, "plane_axis", text="")
        box.operator("partpin.add_curve_cut", icon='CURVE_BEZCURVE',
                     text="Draw Across Model")

        cuts = core.scene_cuts(scene)
        if cuts:
            box = layout.box()
            for cut in cuts:
                row = box.row(align=True)
                icon = 'CHECKBOX_HLT' if cut.pp_enabled else 'CHECKBOX_DEHLT'
                row.operator("partpin.cut_toggle", text="", icon=icon,
                             emboss=False).cut_name = cut.name
                kind_icon = {
                    'CURVE': 'CURVE_BEZCURVE',
                    'SURFACE': 'SURFACE_NSURFACE',
                }.get(cut.pp_cut_kind, 'MESH_PLANE')
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
        layout.prop(s, "line_lift")

        if cut is not None and cut.pp_cut_kind == 'SURFACE':
            loops = len({p.loop for p in cut.pp_points})
            box = layout.box()
            box.label(text=f"{len(cut.pp_points)} points, "
                           f"{loops} line{'s' if loops != 1 else ''}",
                      icon='SURFACE_NSURFACE')
            box.prop(cut, "pp_local")
            sub = box.column()
            sub.enabled = cut.pp_local
            sub.prop(cut, "pp_undercut")
            box.prop(cut, "pp_falloff")
            box.prop(s, "surface_resolution")
            if cut.pp_local:
                box.label(text="Marks show what would stop the cut",
                          icon='INFO')
            if cut.pp_local:
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


CLASSES = (
    PARTPIN_PT_main,
    PARTPIN_PT_cuts,
    PARTPIN_PT_shape,
    PARTPIN_PT_connectors,
    PARTPIN_PT_finalize,
    PARTPIN_PT_export,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
