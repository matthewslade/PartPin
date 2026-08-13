"""PartPin — split models into printable parts with pin connectors.

Cuts a closed mesh into parts with straight or drawn curved cuts, adds
pin/socket connectors with printable clearance, and exports the parts
for 3D printing.
"""

bl_info = {
    "name": "PartPin",
    "author": "PartPin contributors",
    "version": (1, 13, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport ▸ Sidebar (N) ▸ PartPin",
    "description": ("Split models into printable parts with matching "
                    "pin/socket connectors and export them for 3D printing"),
    "category": "Object",
    "doc_url": "",
}

if "props" in locals():
    import importlib

    importlib.reload(core)  # noqa: F821
    importlib.reload(surface)  # noqa: F821
    importlib.reload(props)  # noqa: F821
    importlib.reload(ops)  # noqa: F821
    importlib.reload(shape_edit)  # noqa: F821
    importlib.reload(draw_cut)  # noqa: F821
    importlib.reload(ui)  # noqa: F821
else:
    from . import core, draw_cut, ops, props, shape_edit, surface, ui


def register():
    props.register()
    ops.register()
    shape_edit.register()
    draw_cut.register()
    ui.register()


def unregister():
    ui.unregister()
    draw_cut.unregister()
    shape_edit.unregister()
    ops.unregister()
    props.unregister()


if __name__ == "__main__":
    register()
