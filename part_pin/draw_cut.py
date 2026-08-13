"""Drawing a cut perimeter straight onto the model.

Hold the left mouse button and draw on the model; every mouse position is
ray-cast onto its surface, so the line goes exactly where you put it. Let go
to orbit, then carry on drawing — that is how a perimeter gets round the far
side of a limb. Close the loop back at the dot you started from and the
stroke becomes an ordinary editable cut, ready to nudge point by point.
"""

import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

from . import core, surface

STROKE_COLOR = (1.0, 0.62, 0.16, 1.0)
STROKE_COLOR_HIDDEN = (1.0, 0.62, 0.16, 0.25)
START_COLOR = (0.35, 1.0, 0.45, 1.0)
START_COLOR_READY = (1.0, 0.95, 0.35, 1.0)
GAP_COLOR = (0.55, 0.55, 0.6, 1.0)

# How close to the first point counts as closing the loop.
CLOSE_RADIUS_PX = 22
# Minimum spacing between recorded points, as a fraction of the model size.
MIN_STEP = 0.004

STATUS = ("Draw the perimeter on the model    "
          "Release to orbit, then carry on    "
          "Close at the green dot, or Enter    "
          "Backspace: undo stroke    Esc: cancel")


def _draw_polyline(points, color, width, depth_test):
    if len(points) < 2:
        return
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    gpu.state.depth_test_set(depth_test)
    gpu.state.blend_set('ALPHA')
    shader.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
    shader.uniform_float("lineWidth", width)
    shader.uniform_float("color", color)
    batch_for_shader(shader, 'LINE_STRIP', {"pos": points}).draw(shader)


def _draw_dots(points, color, size):
    if not points:
        return
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.depth_test_set('NONE')
    gpu.state.blend_set('ALPHA')
    gpu.state.point_size_set(size)
    shader.uniform_float("color", color)
    batch_for_shader(shader, 'POINTS', {"pos": points}).draw(shader)


class PARTPIN_OT_draw_cut_line(bpy.types.Operator):
    bl_idname = "partpin.draw_cut_line"
    bl_label = "Draw Cut on Model"
    bl_description = (
        "Draw the cut perimeter directly onto the model. Hold left mouse and "
        "draw, let go to orbit and carry on, then close the loop where you "
        "started. The stroke becomes an editable cut you can fine-tune before "
        "cutting"
    )
    bl_options = {'REGISTER'}

    then_edit: bpy.props.BoolProperty(
        name="Adjust When Closed",
        description="Go straight into point editing once the loop is closed",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        s = core.get_settings(context)
        return (s.target is not None and context.area is not None
                and context.area.type == 'VIEW_3D')

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    def invoke(self, context, event):
        s = core.get_settings(context)
        self.target = s.target
        if self.target.name not in context.scene.objects:
            self.report({'ERROR'}, "Pick a model to cut first")
            return {'CANCELLED'}
        if self.target.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        self.area = context.area
        self.region = next((r for r in context.area.regions
                            if r.type == 'WINDOW'), None)
        self.rv3d = context.space_data.region_3d
        if self.region is None or self.rv3d is None:
            self.report({'ERROR'}, "Run this from a 3D viewport")
            return {'CANCELLED'}

        self.strokes = [[]]       # world points, one list per drawn stretch
        self.drawing = False
        self.near_start = False
        self.min_step = core.bbox_diagonal(self.target) * MIN_STEP

        self._handle = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_VIEW')
        context.workspace.status_text_set(STATUS)
        context.window.cursor_modal_set('PAINT_CROSS')
        context.window_manager.modal_handler_add(self)
        self.area.tag_redraw()
        self.report({'INFO'},
                    "Draw the perimeter on the model, then close the loop")
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        if getattr(self, "_handle", None) is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'WINDOW')
            self._handle = None
        context.workspace.status_text_set(None)
        context.window.cursor_modal_restore()
        if self.area is not None:
            self.area.tag_redraw()

    # ------------------------------------------------------------------
    # Stroke building
    # ------------------------------------------------------------------

    def _points(self):
        return [p for stroke in self.strokes for p in stroke]

    def _surface_hit(self, mouse):
        origin = view3d_utils.region_2d_to_origin_3d(
            self.region, self.rv3d, mouse)
        direction = view3d_utils.region_2d_to_vector_3d(
            self.region, self.rv3d, mouse)
        model = surface.evaluated(self.target)
        inverse = model.matrix_world.inverted()
        hit, location, _normal, _index = model.ray_cast(
            inverse @ origin, inverse.to_3x3() @ direction)
        if not hit:
            return None
        return model.matrix_world @ location

    def _screen_of(self, world):
        return view3d_utils.location_3d_to_region_2d(
            self.region, self.rv3d, world)

    def _add(self, mouse):
        world = self._surface_hit(mouse)
        if world is None:
            return False
        stroke = self.strokes[-1]
        if stroke and (world - stroke[-1]).length < self.min_step:
            return False
        stroke.append(world)
        return True

    def _close_ready(self, mouse):
        """True when the pointer is back at the start and there is enough
        drawn to make a loop."""
        points = self._points()
        if len(points) < 8:
            return False
        start = self._screen_of(points[0])
        if start is None:
            return False
        dx, dy = start[0] - mouse[0], start[1] - mouse[1]
        return (dx * dx + dy * dy) ** 0.5 <= CLOSE_RADIUS_PX

    # ------------------------------------------------------------------
    # Modal
    # ------------------------------------------------------------------

    def modal(self, context, event):
        if self.area is not None:
            self.area.tag_redraw()
        mouse = (event.mouse_x - self.region.x, event.mouse_y - self.region.y)

        if event.type in {'MIDDLEMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM',
                          'MOUSEROTATE', 'MOUSESMARTZOOM', 'WHEELUPMOUSE',
                          'WHEELDOWNMOUSE'} \
                or event.type.startswith('NUMPAD_'):
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            self.near_start = self._close_ready(mouse)
            if self.drawing:
                self._add(mouse)
                if self.near_start:
                    return self._complete(context)
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE':
            if event.value == 'PRESS':
                if self.near_start:
                    return self._complete(context)
                self.drawing = True
                if self.strokes[-1]:
                    self.strokes.append([])
                self._add(mouse)
                return {'RUNNING_MODAL'}
            if event.value == 'RELEASE':
                self.drawing = False
                if not self.strokes[-1]:
                    self.strokes.pop()
                    if not self.strokes:
                        self.strokes = [[]]
                return {'RUNNING_MODAL'}

        if event.type in {'BACK_SPACE', 'DEL'} and event.value == 'PRESS':
            if len(self.strokes) > 1:
                self.strokes.pop()
            else:
                self.strokes = [[]]
            self.report({'INFO'}, "Stroke removed")
            return {'RUNNING_MODAL'}

        if event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            return self._complete(context)

        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self._finish(context)
            self.report({'INFO'}, "Drawing cancelled")
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    def _complete(self, context):
        points = self._points()
        if len(points) < 8:
            self.report({'WARNING'},
                        "Draw more of the perimeter before closing it")
            return {'RUNNING_MODAL'}

        self._finish(context)
        bpy.ops.ed.undo_push(message="Draw Cut on Model")
        cut, error = surface.cut_from_stroke(
            context, self.target, points,
            per_loop=core.get_settings(context).handle_points)
        if cut is None:
            self.report({'ERROR'}, error)
            return {'CANCELLED'}

        for other in list(context.selected_objects):
            other.select_set(False)
        cut.select_set(True)
        context.view_layer.objects.active = cut
        self.report({'INFO'},
                    f"Cut drawn with {len(cut.pp_points)} points — adjust it, "
                    "then Create Parts")
        if self.then_edit:
            # Straight into point editing, which is what the drawing was for.
            bpy.ops.partpin.edit_cut_surface('INVOKE_DEFAULT')
        return {'FINISHED'}

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _draw(self, context):
        try:
            for stroke in self.strokes:
                if len(stroke) < 2:
                    continue
                _draw_polyline(stroke, STROKE_COLOR_HIDDEN, 2.0, 'NONE')
                _draw_polyline(stroke, STROKE_COLOR, 3.0, 'LESS_EQUAL')

            # Dashes across the gaps, so a perimeter drawn in stretches
            # still reads as one line.
            filled = [s for s in self.strokes if s]
            for before, after in zip(filled, filled[1:]):
                _draw_polyline([before[-1], after[0]], GAP_COLOR, 1.5, 'NONE')

            points = self._points()
            if points:
                colour = START_COLOR_READY if self.near_start else START_COLOR
                _draw_dots([points[0]], colour,
                           16.0 if self.near_start else 12.0)
                if len(points) > 1:
                    _draw_dots([points[-1]], STROKE_COLOR, 8.0)
        except Exception:  # never let a draw glitch wedge the modal
            pass
        finally:
            gpu.state.depth_test_set('LESS_EQUAL')
            gpu.state.blend_set('NONE')
            gpu.state.point_size_set(1.0)


CLASSES = (PARTPIN_OT_draw_cut_line,)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
