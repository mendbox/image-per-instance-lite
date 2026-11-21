# SPDX-License-Identifier: GPL-3.0-or-later
"""Image Per Instance: pack a folder of images into one atlas and give every object (or
Geometry Nodes instance) its own image through ONE material and a per-object index."""
import math
import os
import random
import re
import textwrap
import tomllib

import bpy
import numpy as np
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty,
                       PointerProperty, StringProperty)
from bpy.types import Operator, Panel, PropertyGroup

def _version():
    try:
        with open(os.path.join(os.path.dirname(__file__), "blender_manifest.toml"), "rb") as f:
            return tomllib.load(f).get("version", "?")
    except Exception:
        return "?"


VERSION = _version()
ATLAS = "IPIL_Atlas"
MAT = "IPIL_Material"
GN = "IPI_Assign_Index"
ATTR = "img_index"
EXT = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".exr", ".tga")
MAX_ATLAS = 16384
MAX_IMAGES = 20
FULL_URL = "https://mendbox.gumroad.com/l/image-per-instance"
FULL_TEXT = ("Full version: unlimited images, Geometry Nodes instances, fit modes, glowing screens, "
             "flipbooks and random variation.")
ATLAS_ALPHA = False






class IPIL_Props(PropertyGroup):
    folder: StringProperty(name="Image Folder", subtype="DIR_PATH")
    tile: EnumProperty(name="Tile Size", items=[(str(s), f"{s} px", "") for s in (128, 256, 512, 1024, 2048)], default="512")
    pad: IntProperty(name="Padding", description="Edge padding in pixels, hides bleeding between tiles", default=4, min=0, max=32)
    order: EnumProperty(name="Order", items=[("NAME", "By file name", "")], default="NAME")
    seed: IntProperty(name="Seed", default=1)
    count: IntProperty(name="Images", default=0)
    cols: IntProperty(default=1)
    rows: IntProperty(default=1)


def _p(context):
    return context.scene.ipi_lite


def _natural(name):
    """Sort key: img2 before img10."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name.lower())]


def _list_images(folder):
    """Image files directly inside `folder` (nothing when the field is empty or not a folder)."""
    if not folder or not folder.strip():
        return []
    folder = os.path.normpath(bpy.path.abspath(folder))
    if not os.path.isdir(folder):
        return []
    return [os.path.join(folder, f) for f in sorted(os.listdir(folder), key=_natural) if f.lower().endswith(EXT)]


def _pixels(im, w, h, linear):
    """Pixels of a Blender image as an h x w RGBA float array in display (sRGB) values, straight alpha.
    Float images (16-bit PNG, EXR, HDR) come in linear light with premultiplied alpha and are converted."""
    buf = np.empty(w * h * 4, np.float32)
    im.pixels.foreach_get(buf)
    buf = buf.reshape(h, w, 4)
    if linear:
        a = np.clip(buf[..., 3:4], 0.0, 1.0)
        rgb = np.clip(np.divide(buf[..., :3], a, out=buf[..., :3].copy(), where=a > 1e-6), 0.0, 1.0)
        buf[..., :3] = np.where(rgb <= 0.0031308, rgb * 12.92, 1.055 * np.power(rgb, 1 / 2.4) - 0.055)
        buf[..., 3:4] = a
    return buf


def _load_tile(path, w, h, fit="STRETCH", bar=(0.0, 0.0, 0.0, 1.0)):
    """Load an image file as a w x h RGBA float array (resampled; stretched, fitted with bars or cropped to fill)."""
    im = bpy.data.images.load(path, check_existing=False)
    crop = None
    try:
        iw, ih = im.size
        if iw == 0 or ih == 0:
            raise ValueError("empty image")
        linear = im.is_float
        if fit == "STRETCH":
            tw, th = w, h
        else:
            k = min(w / iw, h / ih) if fit == "FIT" else max(w / iw, h / ih)
            tw, th = max(1, round(iw * k)), max(1, round(ih * k))
        if tw * th > 16 * w * h:  # very long strip that must fill the tile: crop first, the scaled copy would be huge
            cw, ch = max(1, min(iw, round(ih * w / h))), max(1, min(ih, round(iw * h / w)))
            y0, x0 = (ih - ch) // 2, (iw - cw) // 2
            part = np.ascontiguousarray(_pixels(im, iw, ih, linear)[y0:y0 + ch, x0:x0 + cw])
            crop = bpy.data.images.new("IPI_crop", cw, ch, alpha=True)
            crop.pixels.foreach_set(part.ravel())
            crop.scale(w, h)
            return _pixels(crop, w, h, False)
        im.scale(tw, th)
        buf = _pixels(im, tw, th, linear)
        if (tw, th) == (w, h):
            return buf
        out = np.empty((h, w, 4), np.float32)
        out[:] = bar
        if fit == "FIT":
            y0, x0 = (h - th) // 2, (w - tw) // 2
            out[y0:y0 + th, x0:x0 + tw] = buf
        else:
            y0, x0 = (th - h) // 2, (tw - w) // 2
            out[:] = buf[y0:y0 + h, x0:x0 + w]
        return out
    finally:
        bpy.data.images.remove(im)
        if crop:
            bpy.data.images.remove(crop)


def _set_index(ob, i):
    """Store the image index on the object and tell Blender to redraw it (a plain property write does not)."""
    ob[ATTR] = float(i)
    ob.update_tag()


def _redraw(context):
    """Redraw every 3D view now, not on the next mouse move."""
    for area in (context.screen.areas if context.screen else ()):
        if area.type == "VIEW_3D":
            area.tag_redraw()


def _put_material(ob, mat):
    """The image goes on material slot 1; other slots (frame, edges...) stay as they are."""
    mats = ob.data.materials
    if len(mats):
        mats[0] = mat
    else:
        mats.append(mat)


def _build_material(cols, rows, atlas, w, h, pad):
    mat = bpy.data.materials.get(MAT) or bpy.data.materials.new(MAT)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    N = nt.nodes.new
    out = N("ShaderNodeOutputMaterial")
    bsdf = N("ShaderNodeBsdfPrincipled")
    tex = N("ShaderNodeTexImage")
    tex.image = atlas
    tex.interpolation = "Linear"
    tex.extension = "EXTEND"
    uv = N("ShaderNodeTexCoord")
    attr = N("ShaderNodeAttribute")
    attr.attribute_type = "INSTANCER"  # falls back to the object's own property of the same name
    attr.attribute_name = ATTR
    sep, comb = N("ShaderNodeSeparateXYZ"), N("ShaderNodeCombineXYZ")

    def m(op, a=None, b=None):
        n = N("ShaderNodeMath")
        n.operation = op
        if a is not None:
            n.inputs[0].default_value = a
        if b is not None:
            n.inputs[1].default_value = b
        return n

    col = m("FLOORED_MODULO", b=cols)
    rowf = m("FLOOR")
    rowd = m("DIVIDE", b=cols)
    us, uo = m("MULTIPLY", b=(w - 2 * pad) / w), m("ADD", b=pad / w)  # inset so linear filtering never reads the neighbour tile
    vs, vo = m("MULTIPLY", b=(h - 2 * pad) / h), m("ADD", b=pad / h)
    ux = m("ADD")
    ud = m("DIVIDE", b=cols)
    vx = m("ADD")
    vd = m("DIVIDE", b=rows)
    L = nt.links.new
    L(attr.outputs["Fac"], col.inputs[0])
    L(attr.outputs["Fac"], rowd.inputs[0])
    L(rowd.outputs[0], rowf.inputs[0])
    L(uv.outputs["UV"], sep.inputs[0])
    L(sep.outputs["X"], us.inputs[0])
    L(us.outputs[0], uo.inputs[0])
    L(uo.outputs[0], ux.inputs[0])
    L(col.outputs[0], ux.inputs[1])
    L(ux.outputs[0], ud.inputs[0])
    L(sep.outputs["Y"], vs.inputs[0])
    L(vs.outputs[0], vo.inputs[0])
    L(vo.outputs[0], vx.inputs[0])
    L(rowf.outputs[0], vx.inputs[1])
    L(vx.outputs[0], vd.inputs[0])
    L(ud.outputs[0], comb.inputs["X"])
    L(vd.outputs[0], comb.inputs["Y"])
    L(comb.outputs[0], tex.inputs["Vector"])
    L(tex.outputs["Color"], bsdf.inputs["Base Color"])
    L(bsdf.outputs[0], out.inputs[0])
    for i, n in enumerate((out, bsdf, tex, uv, attr, sep, comb, col, rowf, rowd, us, uo, vs, vo, ux, ud, vx, vd)):
        n.location = (i * 160 - 1000, 0)
    return mat


def _tile_size(p):
    s = int(p.tile)
    return s, s


def _fit_args(p):
    return "STRETCH", (0.0, 0.0, 0.0, 1.0)


def _store_names(p, files):
    pass


def _cell(p, atlas):
    """Size of one atlas cell in pixels as it was built (the Tile Size field may have changed since)."""
    return atlas.size[0] // max(1, p.cols), atlas.size[1] // max(1, p.rows)


def _make_material(p, atlas):
    w, h = _cell(p, atlas)
    return _build_material(p.cols, p.rows, atlas, w, h, p.pad)


def _sock(node, name):
    return next(s for s in node.inputs if s.name == name and s.enabled)








class IPIL_OT_build(Operator):
    bl_idname = "ipil.build_atlas"
    bl_label = "Build Atlas"
    bl_description = "Pack all images from the folder into one atlas and create the shared material"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        p = _p(context)
        files = _list_images(p.folder)
        if not files:
            if not p.folder.strip():
                self.report({"ERROR"}, "Choose an image folder first (folder icon next to Image Folder)")
            else:
                self.report({"ERROR"}, f"No images (png, jpg, tif, exr...) found in: {os.path.normpath(bpy.path.abspath(p.folder))}")
            return {"CANCELLED"}
        # the atlas is always in file-name order, so Image Number N is always the N-th file
        tw, th = _tile_size(p)
        fit, bar = _fit_args(p)
        pad = p.pad
        n = len(files)
        if MAX_IMAGES and n > MAX_IMAGES:
            self.report({"ERROR"}, f"This free version supports up to {MAX_IMAGES} images ({n} found)")
            return {"CANCELLED"}
        cols = max(1, math.ceil(math.sqrt(n * th / tw)))
        rows = math.ceil(n / cols)
        W, H = cols * tw, rows * th
        if W > MAX_ATLAS or H > MAX_ATLAS:
            self.report({"ERROR"}, f"Atlas would be {W}x{H} px (limit {MAX_ATLAS}). Use a smaller tile size or fewer images")
            return {"CANCELLED"}
        A = np.zeros((H, W, 4), np.float32)
        A[..., 3] = 1
        for i, f in enumerate(files):
            try:
                t = _load_tile(f, tw, th, fit, bar)
            except Exception as e:
                self.report({"WARNING"}, f"Skipped {os.path.basename(f)}: {e}")
                continue
            r, c = divmod(i, cols)
            A[r * th:(r + 1) * th, c * tw:(c + 1) * tw] = t
        old = bpy.data.images.get(ATLAS)
        if old:
            bpy.data.images.remove(old)
        atlas = bpy.data.images.new(ATLAS, W, H, alpha=ATLAS_ALPHA)
        atlas.pixels.foreach_set(A.ravel())
        atlas.pack()
        p.count, p.cols, p.rows = n, cols, rows
        _store_names(p, files)
        for o in bpy.data.objects:  # fewer images than before: no object may point past the end of the atlas
            if o.get(ATTR, 0) >= n:
                _set_index(o, int(o[ATTR]) % n)
        _make_material(p, atlas)
        self.report({"INFO"}, f"Atlas {W}x{H} px, {n} images")
        _redraw(context)
        return {"FINISHED"}


class IPIL_OT_apply(Operator):
    bl_idname = "ipil.apply_objects"
    bl_label = "Apply To Selected Objects"
    bl_description = "Give each selected mesh object its own image (shared material, no duplicates). Free version: up to 20 images"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        p = _p(context)
        mat = bpy.data.materials.get(MAT)
        if not mat or p.count < 1:
            self.report({"ERROR"}, "Build the atlas first")
            return {"CANCELLED"}
        obs = [o for o in context.selected_objects if o.type == "MESH"]
        if not obs:
            self.report({"ERROR"}, "Select mesh objects")
            return {"CANCELLED"}
        obs.sort(key=lambda o: _natural(o.name))
        order = list(range(len(obs)))
        for k, o in zip(order, obs):
            _set_index(o, k % p.count)
            _put_material(o, mat)
        self.report({"INFO"}, f"{len(obs)} objects, {p.count} images")
        _redraw(context)
        return {"FINISHED"}








class IPIL_PT_panel(Panel):
    bl_label = "Image Per Instance Lite"
    bl_idname = "IPIL_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Image Per Instance Lite"

    def draw(self, context):
        p = _p(context)
        col = self.layout.column(align=True)
        col.prop(p, "folder")
        col.prop(p, "tile")
        col.prop(p, "pad")
        col.prop(p, "order")
        col.operator("ipil.build_atlas", icon="IMAGE_DATA")
        col.separator()
        col.label(text=f"Atlas: {p.count} images" if p.count else "Atlas: not built")
        col.operator("ipil.apply_objects", icon="OBJECT_DATA")
        box = self.layout.box()
        for n, line in enumerate(textwrap.wrap(FULL_TEXT, max(16, int((context.region.width - 70) / (7.5 * context.preferences.system.ui_scale))))):
            box.label(text=line, icon="INFO" if n == 0 else "BLANK1")
        box.operator("wm.url_open", text="Get the full version", icon="URL").url = FULL_URL
        self.layout.label(text=f"Image Per Instance Lite v{VERSION}")


classes = (IPIL_Props, IPIL_OT_build, IPIL_OT_apply, IPIL_PT_panel)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.ipi_lite = PointerProperty(type=IPIL_Props)


def unregister():
    del bpy.types.Scene.ipi_lite
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
