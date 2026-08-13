"""Interactive on-surface cut editing.

Draws the line where the cut meets the model and lets you drag points
along the model's surface to reshape it. The cut object itself is hidden
while this runs, so the only thing on screen is the cut line.
"""

import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

from . import core, surface

LINE_COLOR = (1.0, 0.62, 0.16, 1.0)
LINE_COLOR_HIDDEN = (1.0, 0.62, 0.16, 0.22)
POINT_COLOR = (0.96, 0.96, 0.96, 1.0)

# Trouble spots found along the cut line, by kind.
PROBE_COLORS = {
    surface.PROBE_BRIDGE: (1.0, 0.15, 0.15, 1.0),  # red: holds the piece on
    surface.PROBE_MARGIN: (1.0, 0.78, 0.0, 1.0),   # amber: needs more reach
    surface.PROBE_STUCK: (0.65, 0.35, 1.0, 1.0),   # violet: line off material
}
JOINED_COLOR = (1.0, 0.15, 0.15, 1.0)     # still joined: material wraps round
HOLE_COLOR = (0.55, 0.4, 1.0, 1.0)        # the cut leaves the model here
CAP_COLOR = (1.0, 0.62, 0.16, 0.16)       # the lid that will do the cutting
CAP_COLOR_EDGE = (1.0, 0.72, 0.30, 0.5)
POINT_HOVER = (1.0, 0.95, 0.35, 1.0)
POINT_ACTIVE = (0.35, 1.0, 0.45, 1.0)

HIT_RADIUS_PX = 14
SEGMENT_SUBDIV = 8

STATUS = ("Drag points — the shaded surface is the cut    "
          "Ctrl+Click: add point    X: remove point    "
          "Alt+X: remove whole line    Ctrl+Wheel: falloff    "
          "Enter: confirm    Esc: cancel")


def _draw_lines(points, color, width, depth_test):
    if len(points) < 2:
        return
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    gpu.state.depth_test_set(depth_test)
    gpu.state.blend_set('ALPHA')
    shader.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
    shader.uniform_float("lineWidth", width)
    shader.uniform_float("color", color)
    batch_for_shader(shader, 'LINE_STRIP', {"pos": points}).draw(shader)


def _draw_tris(tris, color):
    if len(tris) < 3:
        return
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    # Seen through the model: the cut surface is inside it, and the point of
    # showing it is to see where it lands.
    gpu.state.depth_test_set('NONE')
    gpu.state.blend_set('ALPHA')
    gpu.state.face_culling_set('NONE')
    shader.uniform_float("color", color)
    batch_for_shader(shader, 'TRIS', {"pos": tris}).draw(shader)


def _draw_points(points, color, size):
    if not points:
        return
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.depth_test_set('NONE')
    gpu.state.blend_set('ALPHA')
    gpu.state.point_size_set(size)
    shader.uniform_float("color", color)
    batch_for_shader(shader, 'POINTS', {"pos": points}).draw(shader)


class PARTPIN_OT_edit_cut_surface(bpy.types.Operator):
    bl_idname = "partpin.edit_cut_surface"
    bl_label = "Edit Cut on Surface"
    bl_description = (
        "Show the line where the cut meets the model and drag points "
        "along the surface to reshape it. Converts the cut into a "
        "free-form cut surface that passes through those points"
    )
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        s = core.get_settings(context)
        if s.target is None or context.area is None \
                or context.area.type != 'VIEW_3D':
            return False
        return bool([c for c in core.scene_cuts(context.scene)
                     if c.pp_enabled])

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    def invoke(self, context, event):
        s = core.get_settings(context)
        self.target = s.target
        cut = self._pick_cut(context)
        if cut is None:
            self.report({'ERROR'}, "Select a cut to edit")
            return {'CANCELLED'}
        if cut.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        # One undo step for "entered surface editing", so Ctrl+Z after the
        # modal rolls back the conversion as well as the edits.
        bpy.ops.ed.undo_push(message="Edit Cut on Surface")

        cut, error = surface.convert_to_surface(
            context, cut, self.target, per_loop=s.handle_points)
        if cut is None:
            self.report({'ERROR'}, error)
            return {'CANCELLED'}

        self.cut = cut
        # Points are stored in the cut's own space and re-fitting moves that
        # space, so the snapshot has to remember the frame it belongs to.
        self.snapshot = [(Vector(p.co), p.loop) for p in cut.pp_points]
        self.snapshot_matrix = cut.matrix_world.copy()
        self.falloff_start = cut.pp_falloff
        self.was_hidden = cut.hide_get()
        cut.hide_set(True)  # only the cut line should be visible

        # Resolve the viewport's WINDOW region explicitly: when the operator
        # is launched from the sidebar button, context.region is that panel,
        # which would throw off every mouse-to-3D conversion.
        self.area = context.area
        self.region = next((r for r in context.area.regions
                            if r.type == 'WINDOW'), None)
        self.rv3d = context.space_data.region_3d
        if self.region is None or self.rv3d is None:
            self.report({'ERROR'}, "Run this from a 3D viewport")
            return {'CANCELLED'}
        self.hover = -1
        self.dragging = -1
        self.moved = False
        self._cache = None
        self._cap_tris = []
        self._joined = []
        self._holes = []
        self._probe_points = {}
        self.probe_summary = ""
        self._rebuild_cache()
        self._rebuild_cap()
        self._rebuild_hints()

        self._handle = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_VIEW')
        context.workspace.status_text_set(STATUS)
        context.window.cursor_modal_set('CROSSHAIR')
        context.window_manager.modal_handler_add(self)
        self.area.tag_redraw()
        self.report({'INFO'}, "Drag the points on the model to shape the cut")
        return {'RUNNING_MODAL'}

    def _update_status(self, context):
        """Keep the footer honest: say so as soon as the line stops
        enclosing a cuttable region, rather than at Create Parts time."""
        problem = None
        if self.cut.pp_local:
            problem = surface.cut_line_problem(self.cut)
        if problem:
            context.workspace.status_text_set(
                "CANNOT CUT: " + problem.split(' — ')[0] + "    " + STATUS)
        elif self._joined or self._holes:
            context.workspace.status_text_set(
                f"WILL NOT SEPARATE: red = cut still buried "
                f"({len(self._joined)}), violet = cut leaves the model "
                f"({len(self._holes)})    " + STATUS)
        else:
            context.workspace.status_text_set(STATUS)

    def _run_probe(self, context, announce=False):
        """Test the line against the model and mark the spots that won't cut."""
        self._probe_points = {}
        self.probe_summary = ""
        if not self.cut.pp_local:
            return
        try:
            probes = surface.probe_cut_line(self.cut, self.target)
        except Exception:
            return
        bad, _suggested, summary = surface.probe_summary(probes, self.cut)
        for probe in bad:
            self._probe_points.setdefault(probe['status'], []).append(
                probe['position'])
        self.probe_summary = summary if bad else ""
        if announce:
            self.report({'INFO'} if not bad else {'WARNING'}, summary)
        self._update_status(context)
        if self.area:
            self.area.tag_redraw()

    def _pick_cut(self, context):
        active = context.view_layer.objects.active
        if active is not None:
            if active.pp_role == core.ROLE_CUT:
                return active
            if active.pp_role == core.ROLE_CONNECTOR and active.parent:
                return active.parent
        cuts = [c for c in core.scene_cuts(context.scene) if c.pp_enabled]
        return cuts[-1] if cuts else None

    def _finish(self, context):
        if getattr(self, "_handle", None) is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'WINDOW')
            self._handle = None
        context.workspace.status_text_set(None)
        context.window.cursor_modal_restore()
        if self.area is not None:
            self.area.tag_redraw()

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------

    def _rebuild_hints(self, context=None):
        """Where the model would stay joined, marked on it.

        Costs a moment because it asks the model itself, so it is worked out
        when editing starts and after each drag, not while dragging.
        """
        self._joined = []
        self._holes = []
        if not self.cut.pp_local:
            return
        try:
            self._joined, self._holes = surface.find_join_hints(self.cut,
                                                               self.target)
        except Exception:
            return
        if context is not None and (self._joined or self._holes):
            self.report(
                {'WARNING'},
                f"{len(self._joined)} spots where the cut stays buried, "
                f"{len(self._holes)} where it leaves the model — this cut "
                "would not separate yet")

    def _rebuild_cap(self):
        """The lid spanning the line, as it will be cut — rebuilt whenever the
        line moves, so it is always the surface the cut would use."""
        self._cap_tris = []
        if not self.cut.pp_local:
            return
        try:
            self._cap_tris = surface.cap_preview_tris(self.cut, self.target)
        except Exception:
            self._cap_tris = []

    def _rebuild_cache(self):
        """Recompute the drawable cut line and the world control points."""
        cut = self.cut
        field = surface.field_for(cut)
        matrix = cut.matrix_world
        model = surface.evaluated(self.target)
        inv_target = model.matrix_world.inverted()

        loops = surface.control_loops(cut)
        spacing = 0.0
        counted = 0
        for loop in loops:
            for i, p in enumerate(loop):
                spacing += (loop[(i + 1) % len(loop)] - p).length
                counted += 1
        spacing = spacing / counted if counted else 1.0
        limit = spacing * 0.35

        polylines = []
        for loop in loops:
            line = []
            for i, a in enumerate(loop):
                b = loop[(i + 1) % len(loop)]
                for k in range(SEGMENT_SUBDIV):
                    t = k / SEGMENT_SUBDIV
                    u = a.x + (b.x - a.x) * t
                    v = a.y + (b.y - a.y) * t
                    on_cut = matrix @ Vector((u, v, field.eval(u, v)))
                    # Hug the model: snap onto its surface when that is a
                    # small correction, otherwise keep the surface point.
                    ok, near, _n, _i = model.closest_point_on_mesh(
                        inv_target @ on_cut)
                    if ok:
                        near_world = model.matrix_world @ near
                        if (near_world - on_cut).length <= limit:
                            on_cut = near_world
                    line.append(on_cut)
            if line:
                line.append(line[0])
            polylines.append(line)

        self._cache = {
            'field': field,
            'polylines': polylines,
            'world': [matrix @ Vector(p.co) for p in cut.pp_points],
        }

    def _surface_hit(self, context, mouse):
        """World point on the model under the mouse, or None."""
        origin = view3d_utils.region_2d_to_origin_3d(
            self.region, self.rv3d, mouse)
        direction = view3d_utils.region_2d_to_vector_3d(
            self.region, self.rv3d, mouse)
        model = surface.evaluated(self.target)
        inv = model.matrix_world.inverted()
        hit, location, _normal, _index = model.ray_cast(
            inv @ origin, inv.to_3x3() @ direction)
        if not hit:
            return None
        return model.matrix_world @ location

    def _nearest_point(self, mouse):
        best, best_dist = -1, HIT_RADIUS_PX
        for i, world in enumerate(self._cache['world']):
            screen = view3d_utils.location_3d_to_region_2d(
                self.region, self.rv3d, world)
            if screen is None:
                continue
            dist = (Vector(mouse) - screen).length
            if dist < best_dist:
                best, best_dist = i, dist
        return best

    def _insert_point(self, context, mouse):
        """Add a control point on the surface, inside the nearest segment."""
        world = self._surface_hit(context, mouse)
        if world is None:
            self.report({'WARNING'}, "Ctrl+Click on the model's surface")
            return
        items = [(Vector(p.co), p.loop) for p in self.cut.pp_points]
        if len(items) < 2:
            return
        # The new point keeps the clicked position, so it lands on the
        # model's surface just like a dragged point.
        clicked = self.cut.matrix_world.inverted() @ world

        by_loop = {}
        for index, (co, loop_id) in enumerate(items):
            by_loop.setdefault(loop_id, []).append((index, co))

        after, best = None, float('inf')
        for entries in by_loop.values():
            count = len(entries)
            for k in range(count):
                index_a, a = entries[k]
                _index_b, b = entries[(k + 1) % count]  # includes the wrap
                seg = b - a
                if seg.length_squared < 1e-18:
                    continue
                t = max(0.0, min(1.0,
                                 (clicked - a).dot(seg) / seg.length_squared))
                dist = (clicked - (a + seg * t)).length
                if dist < best:
                    after, best = index_a, dist
        if after is None:
            return

        items.insert(after + 1, (clicked, items[after][1]))
        surface.store_control_points(self.cut, [c for c, _l in items],
                                     [l for _c, l in items])
        self.cut.pp_main_loop = items[after][1]
        self.moved = True
        self._rebuild_cache()
        self._rebuild_cap()

    def _delete_point(self, index):
        items = [(Vector(p.co), p.loop) for p in self.cut.pp_points]
        loop_id = items[index][1]
        if sum(1 for _c, l in items if l == loop_id) <= 3:
            self.report({'WARNING'},
                        "A cut line needs at least 3 points")
            return
        items.pop(index)
        surface.store_control_points(self.cut, [c for c, _l in items],
                                     [l for _c, l in items])
        self.hover = -1
        self.moved = True
        self._rebuild_cache()
        self._rebuild_cap()

    def _delete_loop(self, index):
        """Drop a whole cut line — the region it fences stops being cut."""
        items = [(Vector(p.co), p.loop) for p in self.cut.pp_points]
        if index < 0 or index >= len(items):
            self.report({'WARNING'}, "Hover a point on the line to remove it")
            return
        doomed = items[index][1]
        kept = [(co, loop) for co, loop in items if loop != doomed]
        if not kept:
            self.report({'WARNING'}, "A cut needs at least one line")
            return
        order = {loop: i for i, loop
                 in enumerate(sorted({loop for _co, loop in kept}))}
        surface.store_control_points(self.cut, [co for co, _l in kept],
                                     [order[l] for _co, l in kept])
        # Loop ids shift down, so follow the main line across or forget it.
        self.cut.pp_main_loop = order.get(self.cut.pp_main_loop, -1)
        self.hover = -1
        self.moved = True
        self._rebuild_cache()
        self._rebuild_cap()
        self.report({'INFO'}, "Cut line removed — that region stays whole")

    # ------------------------------------------------------------------
    # Modal
    # ------------------------------------------------------------------

    def modal(self, context, event):
        if self.area is not None:
            self.area.tag_redraw()
        # Absolute coords minus the region origin: event.mouse_region_* is
        # relative to whichever region the pointer is over.
        mouse = (event.mouse_x - self.region.x, event.mouse_y - self.region.y)

        if event.type in {'MIDDLEMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM',
                          'MOUSEROTATE', 'MOUSESMARTZOOM'} \
                or event.type.startswith('NUMPAD_'):
            return {'PASS_THROUGH'}

        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            if not event.ctrl:
                return {'PASS_THROUGH'}
            step = 1.15 if event.type == 'WHEELUPMOUSE' else 1.0 / 1.15
            self.cut.pp_falloff = max(0.2, min(12.0,
                                               self.cut.pp_falloff * step))
            self.moved = True
            self._rebuild_cache()
            self._rebuild_cap()
            context.workspace.status_text_set(
                f"Falloff {self.cut.pp_falloff:.2f}    " + STATUS)
            return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE':
            if self.dragging >= 0:
                world = self._surface_hit(context, mouse)
                if world is not None:
                    # The raw hit is used as-is: the point stays exactly on
                    # the model's surface and the cut surface is re-fitted
                    # to pass through it.
                    self.cut.pp_points[self.dragging].co = (
                        self.cut.matrix_world.inverted() @ world)
                    self.moved = True
                    self._rebuild_cache()
                    self._rebuild_cap()
            else:
                self.hover = self._nearest_point(mouse)
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE':
            if event.value == 'PRESS':
                if event.ctrl:
                    self._insert_point(context, mouse)
                    return {'RUNNING_MODAL'}
                index = self._nearest_point(mouse)
                if index >= 0:
                    self.dragging = index
                return {'RUNNING_MODAL'}
            if event.value == 'RELEASE':
                if self.dragging >= 0:
                    # Keep the cut's plane aligned to the line as it moves,
                    # so dragging it right round a limb still describes a
                    # region that can be cut. The line being edited is the
                    # one the plane should follow.
                    self.cut.pp_main_loop = \
                        self.cut.pp_points[self.dragging].loop
                    surface.refit_frame(self.cut)
                    self._rebuild_cache()
                    self._rebuild_cap()
                    self._rebuild_hints()
                    self._update_status(context)
                self.dragging = -1
                return {'RUNNING_MODAL'}

        if event.type in {'X', 'DEL'} and event.value == 'PRESS':
            if event.alt:
                self._delete_loop(self.hover)
            elif self.hover >= 0:
                self._delete_point(self.hover)
            return {'RUNNING_MODAL'}

        if event.type in {'RET', 'NUMPAD_ENTER', 'SPACE'} \
                and event.value == 'PRESS':
            return self._confirm(context)

        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            return self._cancel(context)

        return {'RUNNING_MODAL'}

    def _confirm(self, context):
        self._finish(context)
        cut = self.cut
        surface.refit_frame(cut)
        surface.build_display_mesh(cut, self.target)
        cut.hide_set(self.was_hidden)

        s = core.get_settings(context)
        snapped = 0
        if s.auto_snap_connectors:
            snapped = surface.snap_connectors(cut)
        bpy.ops.ed.undo_push(message="Edit Cut on Surface")

        message = f"Cut shape updated ({len(cut.pp_points)} points)"
        if snapped:
            message += f", {snapped} connector(s) snapped to it"
        self.report({'INFO'}, message)
        problem = surface.cut_line_problem(cut) if cut.pp_local else None
        if problem:
            self.report({'ERROR'}, f"This cut cannot be made yet: {problem}")
        return {'FINISHED'}

    def _cancel(self, context):
        self._finish(context)
        connectors = [(c, c.matrix_world.copy())
                      for c in core.cut_connectors(context.scene, self.cut)]
        self.cut.matrix_world = self.snapshot_matrix
        for conn, world in connectors:
            conn.matrix_parent_inverse = self.snapshot_matrix.inverted()
            conn.matrix_world = world
        surface.store_control_points(
            self.cut, [co for co, _l in self.snapshot],
            [l for _co, l in self.snapshot])
        self.cut.pp_falloff = self.falloff_start
        surface.build_display_mesh(self.cut, self.target)
        self.cut.hide_set(self.was_hidden)
        self.report({'INFO'}, "Cut shape reverted")
        return {'CANCELLED'}

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _draw(self, context):
        if self._cache is None:
            return
        try:
            _draw_tris(self._cap_tris, CAP_COLOR)
            _draw_points(self._holes, HOLE_COLOR, 8.0)
            _draw_points(self._joined, JOINED_COLOR, 11.0)

            for line in self._cache['polylines']:
                _draw_lines(line, LINE_COLOR_HIDDEN, 2.0, 'NONE')
                _draw_lines(line, LINE_COLOR, 3.0, 'LESS_EQUAL')

            # Where the cut cannot break through, marked on the line itself.
            for status, points in (self._probe_points or {}).items():
                colour = PROBE_COLORS.get(status)
                if not points or colour is None:
                    continue
                _draw_points(points, (colour[0], colour[1], colour[2], 0.35),
                             15.0)
                _draw_points(points, colour, 9.0)

            world = self._cache['world']
            plain = [w for i, w in enumerate(world)
                     if i != self.hover and i != self.dragging]
            _draw_points(plain, POINT_COLOR, 9.0)
            if 0 <= self.hover < len(world) and self.hover != self.dragging:
                _draw_points([world[self.hover]], POINT_HOVER, 13.0)
            if 0 <= self.dragging < len(world):
                _draw_points([world[self.dragging]], POINT_ACTIVE, 13.0)
        except Exception:  # never let a draw glitch wedge the modal
            pass
        finally:
            gpu.state.depth_test_set('LESS_EQUAL')
            gpu.state.blend_set('NONE')
            gpu.state.face_culling_set('BACK')
            gpu.state.point_size_set(1.0)


def _surface_cut(context):
    """The surface cut the user is working on, via the active object."""
    active = context.view_layer.objects.active
    if active is None:
        return None
    cut = (active if active.pp_role == core.ROLE_CUT
           else active.parent if active.pp_role == core.ROLE_CONNECTOR
           else None)
    if cut is not None and cut.pp_cut_kind == 'SURFACE':
        return cut
    return None


class PARTPIN_OT_check_cut_line(bpy.types.Operator):
    bl_idname = "partpin.check_cut_line"
    bl_label = "Check Cut Line"
    bl_description = (
        "Check that this cut's line closes into a loop on the model that can "
        "be spanned and cut"
    )
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        cut = _surface_cut(context)
        return cut is not None and core.get_settings(context).target is not None

    def execute(self, context):
        cut = _surface_cut(context)
        target = core.get_settings(context).target
        problem = surface.cut_line_problem(cut) if cut.pp_local else None
        if problem:
            self.report({'ERROR'}, f"Cut '{cut.name}': {problem}")
            return {'CANCELLED'}

        cap, problem, warning = surface.build_cap_slab(cut, target)
        if warning:
            self.report({'WARNING'}, warning)
        if cap is None:
            self.report({'ERROR'}, f"Cut '{cut.name}': {problem}")
            return {'CANCELLED'}
        faces = len(cap.data.polygons)
        core.remove_object(cap)
        self.report({'INFO'},
                    f"Cut line spans a surface of {faces} faces — ready to "
                    "cut. If nothing comes away, the piece is joined to the "
                    "model outside the line: slide the line along it")
        return {'FINISHED'}


class PARTPIN_OT_snap_connectors(bpy.types.Operator):
    bl_idname = "partpin.snap_connectors"
    bl_label = "Snap Connectors to Cut"
    bl_description = (
        "Move the active cut's connectors back onto its surface and "
        "align them to it — useful after reshaping the cut"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        active = context.view_layer.objects.active
        if active is None:
            return False
        cut = (active if active.pp_role == core.ROLE_CUT
               else active.parent if active.pp_role == core.ROLE_CONNECTOR
               else None)
        return cut is not None and cut.pp_cut_kind == 'SURFACE'

    def execute(self, context):
        active = context.view_layer.objects.active
        cut = (active if active.pp_role == core.ROLE_CUT else active.parent)
        moved = surface.snap_connectors(cut)
        self.report({'INFO'}, f"Snapped {moved} connector(s) to the cut")
        return {'FINISHED'}


class PARTPIN_OT_reset_cut_shape(bpy.types.Operator):
    bl_idname = "partpin.reset_cut_shape"
    bl_label = "Flatten Cut"
    bl_description = "Reset the active surface cut back to a flat plane"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        active = context.view_layer.objects.active
        return (active is not None and active.pp_role == core.ROLE_CUT
                and active.pp_cut_kind == 'SURFACE')

    def execute(self, context):
        cut = context.view_layer.objects.active
        target = core.get_settings(context).target
        for point in cut.pp_points:
            point.co = (point.co[0], point.co[1], 0.0)
        if target is not None:
            surface.build_display_mesh(cut, target)
        self.report({'INFO'}, "Cut flattened")
        return {'FINISHED'}


CLASSES = (
    PARTPIN_OT_edit_cut_surface,
    PARTPIN_OT_check_cut_line,
    PARTPIN_OT_snap_connectors,
    PARTPIN_OT_reset_cut_shape,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
