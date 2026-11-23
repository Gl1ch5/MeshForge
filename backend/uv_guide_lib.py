"""
uv_guide_lib.py - Pure Blender (`bpy`/`bmesh`/`mathutils`) helper library.
Implements deterministic 2x2 Semantic Planar UV Atlas packing:
  - Top-Left  (U 0.02..0.48, V 0.52..0.98): FRONT (-Y) & SCREEN/FACE parts
  - Top-Right (U 0.52..0.98, V 0.52..0.98): TOP (+Z) & KEYBOARD/PANEL parts
  - Bot-Left  (U 0.02..0.48, V 0.02..0.48): LEFT (-X) & RIGHT (+X) side silhouettes
  - Bot-Right (U 0.52..0.98, V 0.02..0.48): BACK (+Y) & BOTTOM (-Z) parts
Zero third-party imports (works inside any Steam Blender Python).
"""
import json
from pathlib import Path

import bpy
import bmesh
from mathutils import Vector

REGIONS = [
    {"id": 0, "name": "FRONT", "color_name": "BLUE", "rgb": [58, 134, 240], "uv_box": [0.03, 0.53, 0.47, 0.97], "quadrant": "TOP-LEFT"},
    {"id": 1, "name": "TOP_PANEL", "color_name": "GREEN", "rgb": [54, 196, 114], "uv_box": [0.53, 0.53, 0.97, 0.97], "quadrant": "TOP-RIGHT"},
    {"id": 2, "name": "SIDES", "color_name": "ORANGE", "rgb": [238, 128, 46], "uv_box": [0.03, 0.03, 0.47, 0.47], "quadrant": "BOTTOM-LEFT"},
    {"id": 3, "name": "BACK_BASE", "color_name": "PURPLE", "rgb": [166, 86, 232], "uv_box": [0.53, 0.03, 0.97, 0.47], "quadrant": "BOTTOM-RIGHT"},
]


def setup_clean_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _classify_face(face, mat_names: list[str]) -> int:
    """Assign each polygon to one of the 4 deterministic semantic UV cells."""
    n = face.normal
    mat_name = mat_names[face.material_index].upper() if 0 <= face.material_index < len(mat_names) else ""

    # Strong semantic overrides from material names
    if any(k in mat_name for k in ("SCREEN", "CRT", "DISPLAY", "MONITOR_FACE", "FRONT")):
        if n.y < 0.35:
            return 0
    if any(k in mat_name for k in ("KEY", "BOARD", "TOP", "ROOF", "LID", "CONSOLE")):
        if n.z > 0.25:
            return 1
    if any(k in mat_name for k in ("BACK", "REAR")):
        if n.y > -0.35:
            return 3

    ax, ay, az = abs(n.x), abs(n.y), abs(n.z)
    if ay >= ax and ay >= az:
        return 0 if n.y <= 0 else 3
    if az >= ax and az >= ay:
        return 1 if n.z >= 0 else 3
    return 2


def ensure_joined_mesh_and_uv(objects: list | None = None) -> bpy.types.Object:
    mesh_objs = [o for o in (objects or bpy.context.scene.objects) if o.type == "MESH"]
    if not mesh_objs:
        raise RuntimeError("No MESH objects found in scene")

    bpy.ops.object.select_all(action="DESELECT")
    for o in mesh_objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = mesh_objs[0]

    for o in list(mesh_objs):
        bpy.context.view_layer.objects.active = o
        try:
            bpy.ops.object.convert(target="MESH")
        except Exception:
            pass

    if len(mesh_objs) > 1:
        bpy.context.view_layer.objects.active = mesh_objs[0]
        bpy.ops.object.join()

    obj = bpy.context.view_layer.objects.active
    obj.name = "GeneratedModel"
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)

    # Normalize: centered at X=0, Y=0, bottom at Z=0, max dimension = 2.0
    bbox = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    min_v = Vector((min(v.x for v in bbox), min(v.y for v in bbox), min(v.z for v in bbox)))
    max_v = Vector((max(v.x for v in bbox), max(v.y for v in bbox), max(v.z for v in bbox)))
    span = max_v - min_v
    max_dim = max(span.x, span.y, span.z, 0.001)
    scale_factor = 2.0 / max_dim
    cx = (min_v.x + max_v.x) * 0.5
    cy = (min_v.y + max_v.y) * 0.5
    for v in obj.data.vertices:
        v.co.x = (v.co.x - cx) * scale_factor
        v.co.y = (v.co.y - cy) * scale_factor
        v.co.z = (v.co.z - min_v.z) * scale_factor
    obj.location = Vector((0.0, 0.0, 0.0))
    obj.data.update()

    return obj


def _project_semantic_2x2_uvs(obj: bpy.types.Object) -> tuple[list[dict], list[int]]:
    """
    Build clean planar-projected UVs into a 2x2 semantic atlas so each side is
    upright, contiguous, and never fragments into tiny confetti shards.
    """
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    bm.normal_update()

    if not bm.loops.layers.uv:
        bm.loops.layers.uv.new("UVMap")
    uv_lay = bm.loops.layers.uv.active

    mat_names = [m.name if m else "" for m in obj.data.materials]

    xs = [v.co.x for v in bm.verts]
    ys = [v.co.y for v in bm.verts]
    zs = [v.co.z for v in bm.verts]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    z0, z1 = min(zs), max(zs)
    dx = max(x1 - x0, 0.001)
    dy = max(y1 - y0, 0.001)
    dz = max(z1 - z0, 0.001)

    face_regions: list[int] = [0] * len(bm.faces)
    region_polys: dict[int, list[list[list[float]]]] = {0: [], 1: [], 2: [], 3: []}
    region_parts: dict[int, set[str]] = {0: set(), 1: set(), 2: set(), 3: set()}

    for f in bm.faces:
        rid = _classify_face(f, mat_names)
        face_regions[f.index] = rid
        if 0 <= f.material_index < len(mat_names) and mat_names[f.material_index]:
            region_parts[rid].add(mat_names[f.material_index])

        u_lo, v_lo, u_hi, v_hi = REGIONS[rid]["uv_box"]
        uw = u_hi - u_lo
        vh = v_hi - v_lo

        poly_uvs = []
        for l in f.loops:
            co = l.vert.co
            nx_norm = (co.x - x0) / dx
            ny_norm = (co.y - y0) / dy
            nz_norm = (co.z - z0) / dz

            if rid == 0:
                # FRONT (-Y): U = X (left->right), V = Z (bottom->top)
                lu, lv = nx_norm, nz_norm
            elif rid == 1:
                # TOP (+Z): U = X (left->right), V = Y (front->back)
                lu, lv = nx_norm, ny_norm
            elif rid == 2:
                # SIDES: LEFT (-X) in left half of cell, RIGHT (+X) in right half
                if f.normal.x <= 0:
                    lu = ny_norm * 0.46
                else:
                    lu = 0.54 + (1.0 - ny_norm) * 0.46
                lv = nz_norm
            else:
                # BACK (+Y) in upper half, BOTTOM (-Z) in lower half
                if f.normal.y >= 0 and abs(f.normal.y) >= abs(f.normal.z):
                    lu = 1.0 - nx_norm
                    lv = 0.54 + nz_norm * 0.46
                else:
                    lu = nx_norm
                    lv = ny_norm * 0.46

            u = u_lo + max(0.0, min(1.0, lu)) * uw
            v = v_lo + max(0.0, min(1.0, lv)) * vh
            l[uv_lay].uv = (u, v)
            poly_uvs.append([round(u, 5), round(v, 5)])

        region_polys[rid].append(poly_uvs)

    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()

    islands_meta = []
    for reg in REGIONS:
        rid = reg["id"]
        if not region_polys[rid]:
            continue
        islands_meta.append({
            "index": rid,
            "name": reg["name"],
            "quadrant": reg["quadrant"],
            "color_name": reg["color_name"],
            "rgb": reg["rgb"],
            "bbox_uv": reg["uv_box"],
            "parts": sorted(region_parts[rid])[:6],
            "polys": region_polys[rid],
        })
    return islands_meta, face_regions


def _assign_color_materials_for_preview(obj: bpy.types.Object, face_regions: list[int]) -> bpy.types.Object:
    dup = obj.copy()
    dup.data = obj.data.copy()
    bpy.context.collection.objects.link(dup)

    dup.data.materials.clear()
    for reg in REGIONS:
        rgb = reg["rgb"]
        mat = bpy.data.materials.new(name=f"Region_{reg['name']}")
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf:
            bsdf.inputs["Base Color"].default_value = (rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0, 1.0)
            bsdf.inputs["Roughness"].default_value = 0.85
        dup.data.materials.append(mat)

    for fi, rid in enumerate(face_regions):
        if fi < len(dup.data.polygons):
            dup.data.polygons[fi].material_index = rid
    return dup


def _render_4_views(target_obj: bpy.types.Object, out_dir: Path, prefix: str, bg_rgb=(0.08, 0.085, 0.095)) -> list[str]:
    scene = bpy.context.scene
    for eng in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "BLENDER_WORKBENCH"):
        try:
            scene.render.engine = eng
            break
        except Exception:
            continue

    scene.render.resolution_x = 512
    scene.render.resolution_y = 512
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"

    world = bpy.data.worlds.new("PreviewWorld")
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (bg_rgb[0], bg_rgb[1], bg_rgb[2], 1.0)
        bg.inputs[1].default_value = 1.0

    lights = []
    for l_name, loc, energy in [
        ("KeyLight", (3.2, -3.5, 4.0), 320.0),
        ("FillLight", (-3.5, -2.5, 2.5), 160.0),
        ("RimLight", (0.5, 4.0, 3.0), 220.0),
    ]:
        ldata = bpy.data.lights.new(name=l_name, type="POINT")
        ldata.energy = energy
        lobj = bpy.data.objects.new(name=l_name, object_data=ldata)
        lobj.location = loc
        bpy.context.collection.objects.link(lobj)
        lights.append(lobj)

    cam_data = bpy.data.cameras.new("PreviewCam")
    cam_data.lens = 38.0
    cam_obj = bpy.data.objects.new("PreviewCam", cam_data)
    bpy.context.collection.objects.link(cam_obj)
    scene.camera = cam_obj

    bbox = [target_obj.matrix_world @ Vector(c) for c in target_obj.bound_box]
    center = Vector((
        sum(v.x for v in bbox) / 8.0,
        sum(v.y for v in bbox) / 8.0,
        sum(v.z for v in bbox) / 8.0,
    ))
    radius = max((v - center).length for v in bbox)
    dist = max(2.2, radius * 2.75)

    views = [
        ("FRONT", Vector((0.35, -1.0, 0.42)).normalized()),
        ("RIGHT", Vector((1.0, -0.25, 0.35)).normalized()),
        ("BACK", Vector((-0.35, 1.0, 0.42)).normalized()),
        ("LEFT", Vector((-1.0, 0.25, 0.35)).normalized()),
    ]

    out_files = []
    for label, dvec in views:
        cam_obj.location = center + dvec * dist
        direction = (center - cam_obj.location).normalized()
        cam_obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
        tmp_png = out_dir / f"{prefix}_{label}.png"
        scene.render.filepath = str(tmp_png)
        bpy.ops.render.render(write_still=True)
        out_files.append(str(tmp_png))

    for obj_rm in lights + [cam_obj]:
        bpy.data.objects.remove(obj_rm, do_unlink=True)
    return out_files


def export_stage1_uv_guide(obj: bpy.types.Object, out_dir: str | Path, tex_size: int = 512) -> dict:
    out_p = Path(out_dir)
    out_p.mkdir(parents=True, exist_ok=True)

    islands_meta, face_regions = _project_semantic_2x2_uvs(obj)

    preview_obj = _assign_color_materials_for_preview(obj, face_regions)
    obj.hide_render = True
    _render_4_views(preview_obj, out_p, "_uv3d")
    obj.hide_render = False
    bpy.data.objects.remove(preview_obj, do_unlink=True)

    manifest = {
        "tex_size": tex_size,
        "islands": islands_meta,
    }
    (out_p / "uv_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    bpy.ops.wm.save_as_mainfile(filepath=str(out_p / "stage1.blend"))

    # Export clean untextured clay GLB immediately so UI can show 3D geometry while texture generates
    clay_mat = bpy.data.materials.new(name="Stage1Clay")
    clay_mat.use_nodes = True
    bsdf = clay_mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (0.45, 0.48, 0.53, 1.0)
        bsdf.inputs["Roughness"].default_value = 0.85
    obj.data.materials.clear()
    obj.data.materials.append(clay_mat)
    for p in obj.data.polygons:
        p.material_index = 0

    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.export_scene.gltf(
        filepath=str(out_p / "stage1_preview.glb"),
        export_format="GLB",
        use_selection=True,
        export_materials="EXPORT",
    )

    return {**manifest, "poly_count": len(obj.data.polygons), "vert_count": len(obj.data.vertices)}


def apply_texture_and_export(
    out_dir: str | Path,
    final_tex_path: str | Path,
    ps1_mode: bool = True,
    normal_path: str | Path | None = None,
    roughness_path: str | Path | None = None,
) -> dict:
    out_p = Path(out_dir)
    bpy.ops.wm.open_mainfile(filepath=str(out_p / "stage1.blend"))

    obj = bpy.data.objects.get("GeneratedModel") or next((o for o in bpy.context.scene.objects if o.type == "MESH"), None)
    if obj is None:
        raise RuntimeError("No GeneratedModel found in stage1.blend")

    mat = bpy.data.materials.new(name="MeshForge_Material")
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    tex_node = nt.nodes.new("ShaderNodeTexImage")
    img_data = bpy.data.images.load(str(Path(final_tex_path).resolve()))
    tex_node.image = img_data
    tex_node.interpolation = "Closest" if ps1_mode else "Linear"

    if bsdf:
        nt.links.new(tex_node.outputs["Color"], bsdf.inputs["Base Color"])
        bsdf.inputs["Roughness"].default_value = 0.92 if ps1_mode else 0.72
        if "Specular IOR Level" in bsdf.inputs:
            bsdf.inputs["Specular IOR Level"].default_value = 0.15 if ps1_mode else 0.45

        if roughness_path and Path(roughness_path).is_file():
            r_node = nt.nodes.new("ShaderNodeTexImage")
            r_img = bpy.data.images.load(str(Path(roughness_path).resolve()))
            r_img.colorspace_settings.name = "Non-Color"
            r_node.image = r_img
            r_node.interpolation = "Closest" if ps1_mode else "Linear"
            nt.links.new(r_node.outputs["Color"], bsdf.inputs["Roughness"])

        if normal_path and Path(normal_path).is_file():
            n_node = nt.nodes.new("ShaderNodeTexImage")
            n_img = bpy.data.images.load(str(Path(normal_path).resolve()))
            n_img.colorspace_settings.name = "Non-Color"
            n_node.image = n_img
            n_node.interpolation = "Closest" if ps1_mode else "Linear"
            nmap = nt.nodes.new("ShaderNodeNormalMap")
            nmap.inputs["Strength"].default_value = 0.85
            nt.links.new(n_node.outputs["Color"], nmap.inputs["Color"])
            nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

    obj.data.materials.clear()
    obj.data.materials.append(mat)
    for p in obj.data.polygons:
        p.material_index = 0
        p.use_smooth = not ps1_mode

    _render_4_views(obj, out_p, "_tex3d")

    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    glb_path = out_p / "model.glb"
    bpy.ops.export_scene.gltf(
        filepath=str(glb_path),
        export_format="GLB",
        use_selection=True,
        export_materials="EXPORT",
        export_image_format="AUTO",
    )

    return {
        "glb_path": str(glb_path),
        "poly_count": len(obj.data.polygons),
        "vert_count": len(obj.data.vertices),
    }
