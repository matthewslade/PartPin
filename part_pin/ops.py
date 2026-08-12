"""Operators for PartPin."""

import math
import os
import re

import bmesh
import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from mathutils import Matrix, Quaternion, Vector

from . import core
from .props import AXIS_ITEMS

AXIS_ROTATIONS = {
    'X': Quaternion((0.0, 1.0, 0.0), math.radians(90.0)),
    'Y': Quaternion((1.0, 0.0, 0.0), math.radians(-90.0)),
    'Z': Quaternion(),
}


def _ensure_object_mode(context):
    obj = context.view_layer.objects.active
    if obj is not None and obj.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')


def _select_only(context, objects):
    context.view_layer.update()
    for o in context.view_layer.objects:
        if o is not None:
            o.select_set(False)
    for o in objects:
        try:
            o.select_set(True)
        except RuntimeError:
            pass
    if objects:
        context.view_layer.objects.active = objects[0]


def _validated_target(op, context, settings):
    target = settings.target
    if target is None or target.name not in context.scene.objects:
        op.report({'ERROR'}, "Pick a model to split first")
        return None
    if not settings.ignore_validation:
        non_manifold, boundary = core.mesh_issues(target)
        if non_manifold or boundary:
            op.report(
                {'ERROR'},
                f"'{target.name}' is not a closed manifold mesh "
                f"({non_manifold} non-manifold, {boundary} boundary edges). "
                "Repair it (3D-Print Toolbox, Remesh) or enable "
                "'Skip Mesh Check'",
            )
            return None
    return target


def _new_plane_cut(context, location, rotation, half_size):
    scene = context.scene
    draft = core.ensure_collection(scene, core.DRAFT_COLLECTION)
    bm = bmesh.new()
    verts = [
        bm.verts.new((-half_size, -half_size, 0.0)),
        bm.verts.new((half_size, -half_size, 0.0)),
        bm.verts.new((half_size, half_size, 0.0)),
        bm.verts.new((-half_size, half_size, 0.0)),
    ]
    bm.faces.new(verts)
    cut = core.new_mesh_object(
        "Cut Plane", bm, draft,
        matrix=Matrix.LocRotScale(location, rotation, Vector((1, 1, 1))))
    cut.pp_role = core.ROLE_CUT
    cut.pp_cut_kind = 'PLANE'
    cut.pp_enabled = True
    cut.pp_index = len(core.scene_cuts(scene)) - 1
    cut.display_type = 'WIRE'
    cut.show_in_front = True
    cut.hide_render = True
    return cut


def _cut_location(target, cursor):
    lo, hi = core.world_bbox(target)
    center = (lo + hi) / 2.0
    if all(lo[i] - 1e-9 <= cursor[i] <= hi[i] + 1e-9 for i in range(3)):
        return Vector(cursor)
    return center


# ----------------------------------------------------------------------
# Setup / validation
# ----------------------------------------------------------------------

class PARTPIN_OT_check_mesh(bpy.types.Operator):
    bl_idname = "partpin.check_mesh"
    bl_label = "Check Mesh"
    bl_description = "Verify the model is a closed manifold mesh, ready for cutting"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return core.get_settings(context).target is not None

    def execute(self, context):
        target = core.get_settings(context).target
        non_manifold, boundary = core.mesh_issues(target)
        if non_manifold == 0 and boundary == 0:
            self.report({'INFO'}, f"'{target.name}' is closed and manifold — ready to cut")
        else:
            self.report(
                {'WARNING'},
                f"'{target.name}' has {non_manifold} non-manifold and "
                f"{boundary} boundary edges — repair before cutting "
                "(Mesh > Clean Up, Remesh, or the 3D-Print Toolbox add-on)",
            )
        return {'FINISHED'}


class PARTPIN_OT_auto_size(bpy.types.Operator):
    bl_idname = "partpin.auto_size"
    bl_label = "Auto Size"
    bl_description = "Derive connector width, length and clearance from the model size"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return core.get_settings(context).target is not None

    def execute(self, context):
        s = core.get_settings(context)
        core.auto_size_defaults(s, s.target)
        return {'FINISHED'}


# ----------------------------------------------------------------------
# Cuts
# ----------------------------------------------------------------------

class PARTPIN_OT_add_plane_cut(bpy.types.Operator):
    bl_idname = "partpin.add_plane_cut"
    bl_label = "Add Straight Cut"
    bl_description = (
        "Add an editable cut plane (placed at the 3D cursor when it is "
        "inside the model, otherwise at the model center). Move, rotate "
        "and scale it freely — nothing is applied until Create Parts"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return core.get_settings(context).target is not None

    def execute(self, context):
        _ensure_object_mode(context)
        s = core.get_settings(context)
        target = s.target
        rotation = AXIS_ROTATIONS[s.plane_axis].copy()
        location = _cut_location(target, context.scene.cursor.location)
        half = core.bbox_diagonal(target) * 0.55
        cut = _new_plane_cut(context, location, rotation, half)
        _select_only(context, [cut])
        self.report({'INFO'},
                    "Cut added — position it, add connectors, then Create Parts")
        return {'FINISHED'}


class PARTPIN_OT_add_curve_cut(bpy.types.Operator):
    bl_idname = "partpin.add_curve_cut"
    bl_label = "Draw Curved Cut"
    bl_description = (
        "Draw a freehand cut line across the model in the viewport. "
        "The cut goes through the model along your current view direction. "
        "Draw one stroke from outside one silhouette edge to outside the "
        "other, then press Finish Drawing"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (core.get_settings(context).target is not None
                and context.area is not None
                and context.area.type == 'VIEW_3D'
                and context.region_data is not None)

    def execute(self, context):
        _ensure_object_mode(context)
        s = core.get_settings(context)
        target = s.target
        scene = context.scene
        lo, hi = core.world_bbox(target)
        center = (lo + hi) / 2.0

        rotation = context.region_data.view_rotation.copy()
        data = bpy.data.curves.new("Cut Curve", 'CURVE')
        data.dimensions = '2D'
        data.fill_mode = 'NONE'
        cut = bpy.data.objects.new("Cut Curve", data)
        draft = core.ensure_collection(scene, core.DRAFT_COLLECTION)
        draft.objects.link(cut)
        cut.matrix_world = Matrix.LocRotScale(center, rotation,
                                              Vector((1, 1, 1)))
        cut.pp_role = core.ROLE_CUT
        cut.pp_cut_kind = 'CURVE'
        cut.pp_enabled = True
        cut.pp_index = len(core.scene_cuts(scene)) - 1
        cut.show_in_front = True
        cut.hide_render = True

        scene.cursor.location = center
        _select_only(context, [cut])
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.wm.tool_set_by_id(name="builtin.draw")
        paint = scene.tool_settings.curve_paint_settings
        paint.depth_mode = 'CURSOR'
        paint.curve_type = 'BEZIER'
        self.report({'INFO'},
                    "Draw one stroke across the model, then click Finish Drawing")
        return {'FINISHED'}


class PARTPIN_OT_finish_curve_cut(bpy.types.Operator):
    bl_idname = "partpin.finish_curve_cut"
    bl_label = "Finish Drawing"
    bl_description = "Confirm the drawn stroke and preview the cut surface"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.view_layer.objects.active
        return (obj is not None and obj.type == 'CURVE'
                and obj.pp_role == core.ROLE_CUT and obj.mode == 'EDIT')

    def execute(self, context):
        cut = context.view_layer.objects.active
        bpy.ops.object.mode_set(mode='OBJECT')
        s = core.get_settings(context)
        pts, _cyclic = core.sample_cut_curve(cut)
        if len(pts) < 2:
            core.remove_object(cut)
            self.report({'WARNING'}, "No stroke was drawn — cut removed")
            return {'CANCELLED'}
        if len(cut.data.splines) > 1:
            self.report({'WARNING'},
                        "Multiple strokes drawn — only the first is used")
        if s.target is not None:
            inv = cut.matrix_world.inverted()
            corners = [inv @ (s.target.matrix_world @ Vector(c))
                       for c in s.target.bound_box]
            cut.data.extrude = max(abs(c.z) for c in corners) * 1.3
        return {'FINISHED'}


class PARTPIN_OT_cut_select(bpy.types.Operator):
    bl_idname = "partpin.cut_select"
    bl_label = "Select Cut"
    bl_description = "Make this cut active so it can be moved and edited"
    bl_options = {'REGISTER', 'UNDO'}

    cut_name: StringProperty()

    def execute(self, context):
        cut = context.scene.objects.get(self.cut_name)
        if cut is None:
            return {'CANCELLED'}
        _ensure_object_mode(context)
        if cut.hide_get():
            cut.hide_set(False)
        _select_only(context, [cut])
        return {'FINISHED'}


class PARTPIN_OT_cut_toggle(bpy.types.Operator):
    bl_idname = "partpin.cut_toggle"
    bl_label = "Enable / Disable Cut"
    bl_description = "Disabled cuts stay in the scene but are ignored by Create Parts"
    bl_options = {'REGISTER', 'UNDO'}

    cut_name: StringProperty()

    def execute(self, context):
        scene = context.scene
        cut = scene.objects.get(self.cut_name)
        if cut is None:
            return {'CANCELLED'}
        cut.pp_enabled = not cut.pp_enabled
        hidden = not cut.pp_enabled
        cut.hide_set(hidden)
        for conn in core.cut_connectors(scene, cut):
            conn.hide_set(hidden)
        return {'FINISHED'}


class PARTPIN_OT_cut_remove(bpy.types.Operator):
    bl_idname = "partpin.cut_remove"
    bl_label = "Remove Cut"
    bl_description = "Delete this cut and its connectors"
    bl_options = {'REGISTER', 'UNDO'}

    cut_name: StringProperty()

    def execute(self, context):
        scene = context.scene
        cut = scene.objects.get(self.cut_name)
        if cut is None:
            return {'CANCELLED'}
        _ensure_object_mode(context)
        for conn in core.cut_connectors(scene, cut):
            core.remove_object(conn)
        core.remove_object(cut)
        return {'FINISHED'}


class PARTPIN_OT_clear_drafts(bpy.types.Operator):
    bl_idname = "partpin.clear_drafts"
    bl_label = "Clear All Cuts"
    bl_description = "Remove every draft cut and connector"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        _ensure_object_mode(context)
        for cut in core.scene_cuts(scene):
            for conn in core.cut_connectors(scene, cut):
                core.remove_object(conn)
            core.remove_object(cut)
        coll = bpy.data.collections.get(core.DRAFT_COLLECTION)
        if coll is not None and not coll.objects:
            bpy.data.collections.remove(coll)
        return {'FINISHED'}


# ----------------------------------------------------------------------
# Connectors
# ----------------------------------------------------------------------

def _active_cut(context):
    obj = context.view_layer.objects.active
    if obj is not None and obj.pp_role == core.ROLE_CUT:
        return obj
    if obj is not None and obj.pp_role == core.ROLE_CONNECTOR and obj.parent:
        return obj.parent
    cuts = [c for c in core.scene_cuts(context.scene) if c.pp_enabled]
    return cuts[-1] if cuts else None


class PARTPIN_OT_add_connectors(bpy.types.Operator):
    bl_idname = "partpin.add_connectors"
    bl_label = "Add Connectors"
    bl_description = (
        "Place connectors spaced along the active cut. They are draft "
        "objects — move, rotate, scale or duplicate them freely before "
        "Create Parts"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return core.get_settings(context).target is not None

    def execute(self, context):
        _ensure_object_mode(context)
        s = core.get_settings(context)
        target = s.target
        cut = _active_cut(context)
        if cut is None:
            self.report({'ERROR'}, "Add a cut first")
            return {'CANCELLED'}
        if s.shape == 'CUSTOM' and s.custom_object is None:
            self.report({'ERROR'}, "Pick a custom connector object first")
            return {'CANCELLED'}
        if not s.sized:
            core.auto_size_defaults(s, target)

        if cut.pp_cut_kind == 'CURVE':
            matrices = core.curve_connector_matrices(target, cut, s.count)
        else:
            matrices = core.plane_connector_matrices(target, cut, s.count)
        if not matrices:
            self.report({'ERROR'},
                        "The cut does not pass through the model — move it first")
            return {'CANCELLED'}

        conns = [core.make_connector_object(context, cut, m)
                 for m in matrices]
        _select_only(context, conns)
        self.report({'INFO'},
                    f"Added {len(conns)} connector(s) — adjust them, then Create Parts")
        return {'FINISHED'}


def _selected_connectors(context):
    conns = [o for o in context.selected_objects
             if o.pp_role == core.ROLE_CONNECTOR]
    active = context.view_layer.objects.active
    if not conns and active is not None \
            and active.pp_role == core.ROLE_CONNECTOR:
        conns = [active]
    return conns


class PARTPIN_OT_update_connectors(bpy.types.Operator):
    bl_idname = "partpin.update_connectors"
    bl_label = "Apply Settings to Selected"
    bl_description = "Rebuild the selected connectors with the current shape, size and clearance"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(_selected_connectors(context))

    def execute(self, context):
        s = core.get_settings(context)
        if s.shape == 'CUSTOM' and s.custom_object is None:
            self.report({'ERROR'}, "Pick a custom connector object first")
            return {'CANCELLED'}
        for conn in _selected_connectors(context):
            old = conn.data
            if s.shape == 'CUSTOM':
                conn.data = s.custom_object.data.copy()
            else:
                conn.data = core.build_pin_mesh(s.shape, s.size, s.length)
            conn.data.name = "PartPin_Pin"
            if old.users == 0:
                bpy.data.meshes.remove(old)
            conn.pp_shape = s.shape
            conn.pp_clearance = s.clearance
        return {'FINISHED'}


class PARTPIN_OT_flip_pin(bpy.types.Operator):
    bl_idname = "partpin.flip_pin"
    bl_label = "Flip Pin Side"
    bl_description = "Swap which part receives the pin and which the socket"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(_selected_connectors(context))

    def execute(self, context):
        for conn in _selected_connectors(context):
            conn.pp_pin_flip = not conn.pp_pin_flip
            conn.color = (core.PIN_COLOR_FLIPPED if conn.pp_pin_flip
                          else core.PIN_COLOR)
        return {'FINISHED'}


# ----------------------------------------------------------------------
# Finalize
# ----------------------------------------------------------------------

class PARTPIN_OT_create_parts(bpy.types.Operator):
    bl_idname = "partpin.create_parts"
    bl_label = "Create Parts"
    bl_description = (
        "Apply every enabled cut and connector and put the final parts in "
        "a new collection. The original model is kept (hidden) by default"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return core.get_settings(context).target is not None

    def execute(self, context):
        _ensure_object_mode(context)
        s = core.get_settings(context)
        scene = context.scene
        target = _validated_target(self, context, s)
        if target is None:
            return {'CANCELLED'}
        cuts = [c for c in core.scene_cuts(scene) if c.pp_enabled]
        if not cuts:
            self.report({'ERROR'}, "Add at least one cut first")
            return {'CANCELLED'}

        parts, applied, warnings = core.create_parts(
            context, target, cuts,
            keep_original=s.keep_original,
            part_gap=s.part_gap,
        )
        for w in warnings:
            self.report({'WARNING'}, w)

        if s.keep_original:
            core.hide_drafts(scene, True)
        else:
            for cut in core.scene_cuts(scene):
                for conn in core.cut_connectors(scene, cut):
                    core.remove_object(conn)
                core.remove_object(cut)

        if parts:
            s.parts_collection = parts[0].users_collection[0]
            _select_only(context, parts)
        self.report({'INFO'},
                    f"Created {len(parts)} part(s) with {applied} connector(s)")
        return {'FINISHED'}


class PARTPIN_OT_easy_cut(bpy.types.Operator):
    bl_idname = "partpin.easy_cut"
    bl_label = "Easy Cut"
    bl_description = (
        "One click for simple models: straight cut, auto-placed "
        "connectors, parts created immediately"
    )
    bl_options = {'REGISTER', 'UNDO'}

    axis: EnumProperty(name="Axis", items=AXIS_ITEMS, default='Z')
    at_cursor: BoolProperty(
        name="At 3D Cursor",
        description="Cut through the 3D cursor instead of the model center",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        return core.get_settings(context).target is not None

    def execute(self, context):
        _ensure_object_mode(context)
        s = core.get_settings(context)
        target = _validated_target(self, context, s)
        if target is None:
            return {'CANCELLED'}

        rotation = AXIS_ROTATIONS[self.axis].copy()
        lo, hi = core.world_bbox(target)
        location = (lo + hi) / 2.0
        if self.at_cursor:
            location = Vector(context.scene.cursor.location)
        half = core.bbox_diagonal(target) * 0.55
        cut = _new_plane_cut(context, location, rotation, half)

        if not s.sized:
            core.auto_size_defaults(s, target)
        matrices = core.plane_connector_matrices(target, cut, s.count)
        if not matrices:
            core.remove_object(cut)
            self.report({'ERROR'},
                        "The cut plane does not pass through the model")
            return {'CANCELLED'}
        conns = [core.make_connector_object(context, cut, m)
                 for m in matrices]

        parts, applied, warnings = core.create_parts(
            context, target, [cut],
            keep_original=s.keep_original,
            part_gap=s.part_gap,
        )
        for conn in conns:
            core.remove_object(conn)
        core.remove_object(cut)
        for w in warnings:
            self.report({'WARNING'}, w)

        if parts:
            s.parts_collection = parts[0].users_collection[0]
            _select_only(context, parts)
        self.report({'INFO'},
                    f"Created {len(parts)} part(s) with {applied} connector(s)")
        return {'FINISHED'}


# ----------------------------------------------------------------------
# Export
# ----------------------------------------------------------------------

def _safe_name(name):
    return re.sub(r"[^\w\-.]+", "_", name).strip("_") or "part"


def _export_file(fmt, filepath, scale):
    if fmt == 'STL':
        bpy.ops.wm.stl_export(filepath=filepath,
                              export_selected_objects=True,
                              global_scale=scale,
                              apply_modifiers=True)
    elif fmt == 'OBJ':
        bpy.ops.wm.obj_export(filepath=filepath,
                              export_selected_objects=True,
                              global_scale=scale,
                              export_materials=False)
    else:
        bpy.ops.export_scene.fbx(filepath=filepath,
                                 use_selection=True,
                                 global_scale=scale)


class PARTPIN_OT_export_parts(bpy.types.Operator):
    bl_idname = "partpin.export_parts"
    bl_label = "Export Parts"
    bl_description = "Write the final parts to STL / OBJ / FBX files"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return core.get_settings(context).parts_collection is not None

    def execute(self, context):
        s = core.get_settings(context)
        coll = s.parts_collection
        objs = [o for o in coll.objects if o.type == 'MESH']
        if not objs:
            self.report({'ERROR'}, f"No mesh parts in '{coll.name}'")
            return {'CANCELLED'}

        if s.export_dir.startswith("//") and not bpy.data.filepath:
            self.report({'ERROR'},
                        "Save the .blend file first, or pick an absolute export folder")
            return {'CANCELLED'}
        directory = bpy.path.abspath(s.export_dir)
        os.makedirs(directory, exist_ok=True)

        _ensure_object_mode(context)
        ext = s.export_format.lower()
        prev_selection = list(context.selected_objects)
        prev_active = context.view_layer.objects.active

        written = []
        try:
            if s.export_batch:
                for obj in objs:
                    _select_only(context, [obj])
                    path = os.path.join(directory,
                                        f"{_safe_name(obj.name)}.{ext}")
                    _export_file(s.export_format, path, s.export_scale)
                    written.append(path)
            else:
                _select_only(context, objs)
                path = os.path.join(directory,
                                    f"{_safe_name(coll.name)}.{ext}")
                _export_file(s.export_format, path, s.export_scale)
                written.append(path)
        finally:
            _select_only(context, prev_selection)
            context.view_layer.objects.active = prev_active

        self.report({'INFO'},
                    f"Exported {len(written)} {s.export_format} file(s) to {directory}")
        return {'FINISHED'}


CLASSES = (
    PARTPIN_OT_check_mesh,
    PARTPIN_OT_auto_size,
    PARTPIN_OT_add_plane_cut,
    PARTPIN_OT_add_curve_cut,
    PARTPIN_OT_finish_curve_cut,
    PARTPIN_OT_cut_select,
    PARTPIN_OT_cut_toggle,
    PARTPIN_OT_cut_remove,
    PARTPIN_OT_clear_drafts,
    PARTPIN_OT_add_connectors,
    PARTPIN_OT_update_connectors,
    PARTPIN_OT_flip_pin,
    PARTPIN_OT_create_parts,
    PARTPIN_OT_easy_cut,
    PARTPIN_OT_export_parts,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
