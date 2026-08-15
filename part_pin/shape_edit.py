# SPDX-FileCopyrightText: 2026 PartPin contributors
# SPDX-License-Identifier: GPL-3.0-or-later
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

TROUBLE_COLORS = {
    surface.STUCK: (1.0, 0.15, 0.15, 1.0),    # red: the cut cannot get through
    surface.PINCHED: (1.0, 0.35, 0.85, 1.0),  # pink: the line doubles back
    surface.ADRIFT: (1.0, 0.95, 0.30, 1.0),   # yellow: line off the model
    surface.BROKEN: (0.35, 0.75, 1.0, 1.0),   # blue: the line cannot get across
}
TROUBLE_LABELS = {
    surface.STUCK: "red = the cut cannot get through here",
    surface.PINCHED: "pink = the line doubles back on itself",
    surface.ADRIFT: "yellow = off the model",
    surface.BROKEN: "blue = the line cannot get across the model here",
}
CAP_COLOR = (1.0, 0.62, 0.16, 0.16)       # the lid that will do the cutting
POINT_HOVER = (1.0, 0.95, 0.35, 1.0)
POINT_ACTIVE = (0.35, 1.0, 0.45, 1.0)

HIT_RADIUS_PX = 20

STATUS = ("Drag points — the shaded surface is the cut    "
          "Ctrl+Click: add point    X: remove point    "
          "Alt+X: remove whole line    "
          "T: try the cut    Enter: confirm    Esc: cancel")


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
        # Anchors are in the model's own space, so a snapshot of them is all
        # that is needed to put the line back exactly as it was found.
        self.snapshot = [(Vector(p.co), p.loop, p.face) for p in cut.pp_points]
        self.snapshot_matrix = cut.matrix_world.copy()
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
        self._trouble = {}
        self._verdict = ""
        self._rebuild_cache()
        self._rebuild_cap()
        self._inspect()

        self._handle = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_VIEW')
        self._update_status(context)
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
            problem = surface.cut_line_problem(self.cut, self.target)
        if problem:
            context.workspace.status_text_set(
                "CANNOT CUT: " + problem.split(' — ')[0] + "    " + STATUS)
        elif self._verdict:
            context.workspace.status_text_set(self._verdict + "    " + STATUS)
        else:
            context.workspace.status_text_set(STATUS)

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

    def _inspect(self, context=None):
        """Measure the cut against the model and mark whatever is wrong.

        Costs about a thirtieth of a second and never changes anything, so it
        runs when editing starts and after every drag — the point of it is to
        show trouble while there is still a hand on the line.
        """
        self._trouble = {}
        self._verdict = ""
        if not self.cut.pp_local:
            return
        try:
            found = surface.inspect_cut(self.cut, self.target)
        except Exception:
            return
        self._trouble = {kind: places for kind, places in found.items()
                         if places}
        if self._trouble:
            self._verdict = "WILL NOT CUT YET: " + "  ".join(
                TROUBLE_LABELS[kind] for kind in self._trouble)
        if context is not None:
            self._update_status(context)
            if self.area:
                self.area.tag_redraw()

    def _try_cut(self, context):
        """Make the cut on a copy and say whether it separates.

        Marks are only worth showing once the answer is no. Parts of a cut
        surface often lie outside the model without doing any harm, and
        pointing at those on a cut that works is just noise.
        """
        if not self.cut.pp_local:
            self.report({'INFO'}, "This cut is not limited to its line")
            return
        try:
            pieces, _spots = surface.trial_cut(self.cut, self.target)
        except Exception:
            self.report({'WARNING'}, "Could not try the cut")
            return
        self._inspect()
        if pieces >= 2:
            self._verdict = f"This cut separates into {pieces} parts"
            self.report({'INFO'}, self._verdict)
        elif self._trouble:
            self.report({'WARNING'}, "This cut would not separate — "
                        + "; ".join(f"{len(places)} spots where "
                                    + surface.TROUBLE[kind].split(' — ')[0]
                                    for kind, places in
                                    self._trouble.items()))
        else:
            self._verdict = ("This cut would not separate, and nothing on the "
                             "line looks wrong — try nudging a point")
            self.report({'WARNING'}, self._verdict)
        self._update_status(context)
        if self.area:
            self.area.tag_redraw()

    def _rebuild_cap(self):
        """The lid spanning the line, as it will be cut — rebuilt whenever the
        line moves, so it is always the surface the cut would use."""
        self._cap_tris = []
        if not self.cut.pp_local:
            return
        try:
            lift = (core.bbox_diagonal(self.target)
                    * core.get_settings(bpy.context).line_lift)
            self._cap_tris = surface.cap_preview_tris(self.cut, self.target,
                                                     lift=lift)
        except Exception:
            self._cap_tris = []

    def _rebuild_cache(self):
        """Recompute the drawable cut line and the world anchor positions.

        The line comes from the one definition of where it lies on the model,
        the same one the cut is made along, lifted clear so the surface it
        lies on cannot swallow it.
        """
        cut = self.cut
        matrix = self.target.matrix_world
        model = surface.evaluated(self.target)
        inv_target = model.matrix_world.inverted()
        normal_matrix = model.matrix_world.to_3x3()
        lift = (core.bbox_diagonal(self.target)
                * core.get_settings(bpy.context).line_lift)

        polylines = []
        for ring in surface.line_rings(cut, self.target, lift=lift):
            polylines.append(ring + [ring[0]])

        world = [matrix @ Vector(p.co) for p in cut.pp_points]
        self._cache = {
            'polylines': polylines,
            'world': world,
            'drawn': [surface._lifted(model, inv_target, normal_matrix,
                                      p, lift) for p in world],
        }

    def _surface_hit(self, context, mouse):
        """(world point, face) on the model under the mouse, or (None, -1)."""
        origin = view3d_utils.region_2d_to_origin_3d(
            self.region, self.rv3d, mouse)
        direction = view3d_utils.region_2d_to_vector_3d(
            self.region, self.rv3d, mouse)
        model = surface.evaluated(self.target)
        inv = model.matrix_world.inverted()
        hit, location, _normal, index = model.ray_cast(
            inv @ origin, inv.to_3x3() @ direction)
        if not hit:
            return None, -1
        return model.matrix_world @ location, index

    def _nearest_point(self, mouse):
        """The point under the mouse, picked where it is actually drawn.

        Two things used to make this feel like guesswork. It measured to the
        control points themselves while the dots on screen are drawn lifted
        clear of the surface, so the target sat a lift away from the dot — and
        which way, and how far in pixels, changed with the angle you were
        looking from. And a point round the back of the model competed on
        equal terms with one in front of it, so a hidden point could take the
        pick from the one being aimed at.
        """
        drawn = self._cache.get('drawn') or self._cache['world']
        model = surface.evaluated(self.target)
        inverse = model.matrix_world.inverted()
        best, best_dist, best_seen = -1, HIT_RADIUS_PX, False
        for i, world in enumerate(drawn):
            screen = view3d_utils.location_3d_to_region_2d(
                self.region, self.rv3d, world)
            if screen is None:
                continue
            dist = (Vector(mouse) - screen).length
            if dist >= HIT_RADIUS_PX:
                continue
            seen = not self._hidden(model, inverse, world)
            # Anything in view beats anything behind the model, and only then
            # does closeness to the cursor decide.
            if (seen, -dist) > (best_seen, -best_dist):
                best, best_dist, best_seen = i, dist, seen
        return best

    def _hidden(self, model, inverse, world):
        """Whether the model itself stands between the view and this point."""
        origin = view3d_utils.region_2d_to_origin_3d(
            self.region, self.rv3d,
            view3d_utils.location_3d_to_region_2d(self.region, self.rv3d,
                                                  world))
        if origin is None:
            return False
        span = world - origin
        reach = span.length
        if reach < 1e-9:
            return False
        hit, location, _normal, _index = model.ray_cast(
            inverse @ origin, inverse.to_3x3() @ (span / reach))
        if not hit:
            return False
        # Allowed to be a lift's worth in front of the surface it sits on.
        slack = core.bbox_diagonal(self.target) * max(
            core.get_settings(bpy.context).line_lift, 1e-4) * 3.0
        return ((model.matrix_world @ location) - origin).length < reach - slack

    def _insert_point(self, context, mouse):
        """Add an anchor on the surface, inside the nearest span."""
        world, face = self._surface_hit(context, mouse)
        if world is None:
            self.report({'WARNING'}, "Ctrl+Click on the model's surface")
            return
        items = [(Vector(p.co), p.loop, p.face) for p in self.cut.pp_points]
        if len(items) < 2:
            return
        # The new anchor keeps the clicked position, so it lands on the
        # model's surface just like a dragged one.
        clicked = self.target.matrix_world.inverted() @ world

        by_loop = {}
        for index, (co, loop_id, _face) in enumerate(items):
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

        items.insert(after + 1, (clicked, items[after][1], face))
        self._store(items)
        self.cut.pp_main_loop = items[after][1]
        self.moved = True
        self._rebuild_cache()
        self._rebuild_cap()

    def _store(self, items):
        surface.store_anchors(self.cut, [co for co, _l, _f in items],
                              [l for _co, l, _f in items],
                              [f for _co, _l, f in items])

    def _delete_point(self, index):
        items = [(Vector(p.co), p.loop, p.face) for p in self.cut.pp_points]
        loop_id = items[index][1]
        if sum(1 for _c, l, _f in items if l == loop_id) <= 3:
            self.report({'WARNING'},
                        "A cut line needs at least 3 points")
            return
        items.pop(index)
        self._store(items)
        self.hover = -1
        self.moved = True
        self._rebuild_cache()
        self._rebuild_cap()

    def _delete_loop(self, index):
        """Drop a whole cut line — the region it fences stops being cut."""
        items = [(Vector(p.co), p.loop, p.face) for p in self.cut.pp_points]
        if index < 0 or index >= len(items):
            self.report({'WARNING'}, "Hover a point on the line to remove it")
            return
        doomed = items[index][1]
        kept = [item for item in items if item[1] != doomed]
        if not kept:
            self.report({'WARNING'}, "A cut needs at least one line")
            return
        order = {loop: i for i, loop
                 in enumerate(sorted({l for _co, l, _f in kept}))}
        self._store([(co, order[l], f) for co, l, f in kept])
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
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            if self.dragging >= 0:
                world, face = self._surface_hit(context, mouse)
                if world is not None:
                    # The raw hit is used as-is: the anchor stays exactly on
                    # the model's surface, and only the two spans either side
                    # of it are walked again.
                    anchor = self.cut.pp_points[self.dragging]
                    anchor.co = self.target.matrix_world.inverted() @ world
                    anchor.face = face
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
                    # The line being edited is the one anything hanging off
                    # the cut should follow.
                    self.cut.pp_main_loop = \
                        self.cut.pp_points[self.dragging].loop
                    self._inspect(context)
                self.dragging = -1
                return {'RUNNING_MODAL'}

        if event.type == 'T' and event.value == 'PRESS':
            self._try_cut(context)
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
        surface.frame_to_line(cut, self.target)
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
        problem = (surface.cut_line_problem(cut, self.target)
                   if cut.pp_local else None)
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
        self._store(self.snapshot)
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
            for kind, places in (self._trouble or {}).items():
                colour = TROUBLE_COLORS.get(kind)
                if colour:
                    _draw_points(places, (colour[0], colour[1], colour[2],
                                          0.3), 16.0)
                    _draw_points(places, colour, 9.0)

            for line in self._cache['polylines']:
                _draw_lines(line, LINE_COLOR_HIDDEN, 2.0, 'NONE')
                _draw_lines(line, LINE_COLOR, 3.0, 'LESS_EQUAL')

            world = self._cache.get('drawn') or self._cache['world']
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
    bl_label = "Try This Cut"
    bl_description = (
        "Make this cut on a copy and say whether it separates. If it does "
        "not, the reasons are marked on the model in Edit Cut on Surface"
    )
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        cut = _surface_cut(context)
        return cut is not None and core.get_settings(context).target is not None

    def execute(self, context):
        cut = _surface_cut(context)
        target = core.get_settings(context).target
        problem = (surface.cut_line_problem(cut, target)
                   if cut.pp_local else None)
        if problem:
            self.report({'ERROR'}, f"Cut '{cut.name}': {problem}")
            return {'CANCELLED'}
        if not cut.pp_local:
            self.report({'INFO'}, "This cut is not limited to its line")
            return {'FINISHED'}

        pieces, spots = surface.trial_cut(cut, target)
        if pieces >= 2:
            self.report({'INFO'},
                        f"Cut '{cut.name}' separates into {pieces} parts")
            return {'FINISHED'}
        self.report({'WARNING'},
                    f"Cut '{cut.name}': "
                    f"{surface.failure_reason(spots, surface.WHY.get(cut.name))}")
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
    bl_description = (
        "Lay the cut line back down where a flat cut would meet the model — "
        "the flat plane closest to the line as it stands"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        active = context.view_layer.objects.active
        return (active is not None and active.pp_role == core.ROLE_CUT
                and active.pp_cut_kind == 'SURFACE')

    def execute(self, context):
        cut = context.view_layer.objects.active
        target = core.get_settings(context).target
        if target is None:
            self.report({'ERROR'}, "Pick a model first")
            return {'CANCELLED'}
        # The line is on the model, so flattening it is not a matter of
        # zeroing a height any more: it is asking where a flat cut through
        # the plane the line lies closest to would meet the model.
        if not surface.flatten_line(cut, target,
                                    core.get_settings(context).handle_points):
            self.report({'WARNING'},
                        "A flat cut there does not meet the model")
            return {'CANCELLED'}
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
