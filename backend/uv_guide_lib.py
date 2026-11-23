"""
uv_guide_lib.py - Pure Blender (`bpy`/`bmesh`/`mathutils`) helper library.
Implements overlap-free, aspect-preserving 2x2 Semantic Planar UV Atlas packing:
  - Top-Left  (U 0.02..0.48, V 0.52..0.98): FRONT (-Y) & front details (headlights, grille, screen)
  - Top-Right (U 0.52..0.98, V 0.52..0.98): TOP (+Z) & upper details (hood, roof, canopy, panel)
  - Bot-Left  (U 0.02..0.48, V 0.02..0.48): SIDES (-X / +X) & side details (doors, wheels, fins)
  - Bot-Right (U 0.52..0.98, V 0.02..0.48): BACK (+Y) & BOTTOM (-Z) parts (taillights, exhaust, base)
Zero third-party imports (works inside any Steam Blender Python).
"""
import json
from collections import deque
from pathlib import Path

import bpy
import bmesh
from mathutils import Vector

REGIONS = [
    {"id": 0, "name": "FRONT", "color_name": "BLUE", "rgb": [58, 134, 240], "uv_box": [0.025, 0.525, 0.475, 0.942], "header_box": [0.025, 0.945, 0.475, 0.975], "quadrant": "TOP-LEFT"},
    {"id": 1, "name": "TOP_PANEL", "color_name": "GREEN", "rgb": [54, 196, 114], "uv_box": [0.525, 0.525, 0.975, 0.942], "header_box": [0.525, 0.945, 0.975, 0.975], "quadrant": "TOP-RIGHT"},
    {"id": 2, "name": "SIDES", "color_name": "ORANGE", "rgb": [238, 128, 46], "uv_box": [0.025, 0.025, 0.475, 0.442], "header_box": [0.025, 0.445, 0.475, 0.475], "quadrant": "BOTTOM-LEFT"},
    {"id": 3, "name": "BACK_BASE", "color_name": "PURPLE", "rgb": [166, 86, 232], "uv_box": [0.525, 0.025, 0.975, 0.442], "header_box": [0.525, 0.445, 0.975, 0.475], "quadrant": "BOTTOM-RIGHT"},
]

SPECIAL_ISOLATE_KEYWORDS = (
    "GLASS", "WINDSHIELD", "WINDOW", "CANOPY", "COCKPIT",
    "WHEEL", "TIRE", "TYRE", "RIM", "HUB",
    "HEADLIGHT", "TAILLIGHT", "LIGHT", "LAMP", "EMISSIVE",
    "SCREEN", "CRT", "DISPLAY", "MONITOR_FACE",
    "GRILLE", "EXHAUST", "PIPE", "GEAR", "ORDNANCE", "MISSILE",
    "SPOILER", "MIRROR", "BUMPER", "CHROME", "LOGO", "BADGE", "LABEL",
)


def setup_clean_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _is_special_material(mat_name: str) -> bool:
    up = (mat_name or "").upper()
    return any(k in up for k in SPECIAL_ISOLATE_KEYWORDS)


def _dominant_axis(n: Vector) -> str:
    ax, ay, az = abs(n.x), abs(n.y), abs(n.z)
    if ay >= ax and ay >= az:
        return "-Y" if n.y <= 0 else "+Y"
    if az >= ax and az >= ay:
        return "+Z" if n.z >= 0 else "-Z"
    return "-X" if n.x <= 0 else "+X"


def _classify_face(face, mat_names: list[str]) -> tuple[int, str]:
    """Assign each polygon to (quadrant_id, orthographic_projection_axis)."""
    n = face.normal
    mat_name = mat_names[face.material_index].upper() if 0 <= face.material_index < len(mat_names) else ""
    axis = _dominant_axis(n)

    # Strong semantic overrides from material names
    if any(k in mat_name for k in ("SCREEN", "CRT", "DISPLAY", "MONITOR_FACE", "HEADLIGHT", "GRILLE", "FRONT")):
        if n.y < 0.35:
            return 0, "-Y"
    if any(k in mat_name for k in ("WINDSHIELD",)):
        return (1 if n.z > 0.72 else 0), ("+Z" if n.z > 0.72 else "-Y")
    if any(k in mat_name for k in ("KEY", "BOARD", "ROOF", "CANOPY", "HOOD", "CONSOLE")):
        if n.z > 0.25:
            return 1, "+Z"
    if any(k in mat_name for k in ("WHEEL", "TIRE", "TYRE", "RIM")):
        if abs(n.x) >= 0.25:
            return 2, ("-X" if n.x <= 0 else "+X")
    if any(k in mat_name for k in ("TAILLIGHT", "EXHAUST", "BACK", "REAR")):
        if n.y > -0.35:
            return 3, "+Y"

    if axis == "-Y":
        return 0, axis
    if axis == "+Z":
        return 1, axis
    if axis in ("-X", "+X"):
        return 2, axis
    return 3, axis


def _project_point_2d(co: Vector, axis: str) -> tuple[float, float, float]:
    """Return upright (u, v, depth) in true world units for the given orthographic axis."""
    if axis == "-Y":
        return co.x, co.z, -co.y
    if axis == "+Y":
        return -co.x, co.z, co.y
    if axis == "+Z":
        return co.x, co.y, co.z
    if axis == "-Z":
        return co.x, -co.y, -co.z
    if axis == "-X":
        return co.y, co.z, -co.x
    return -co.y, co.z, co.x  # +X


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
    bpy.context.view_layer.update()

    # Ensure consistent outward normals so top/front/side classification never inverts
    try:
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.normals_make_consistent(inside=False)
        bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass

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
    bpy.context.view_layer.update()

    return obj


def _split_overlapping_faces(faces: list, axis: str) -> list[list]:
    """Split a face group into non-overlapping 2D layers using an interior-sample occupancy grid."""
    if len(faces) <= 1:
        return [faces] if faces else []

    proj: dict[int, tuple[list[tuple[float, float]], float, float]] = {}
    all_u: list[float] = []
    all_v: list[float] = []
    for f in faces:
        pts = []
        depths = []
        signed_area = 0.0
        for l in f.loops:
            u, v, d = _project_point_2d(l.vert.co, axis)
            pts.append((u, v))
            depths.append(d)
            all_u.append(u)
            all_v.append(v)
        for i in range(len(pts)):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % len(pts)]
            signed_area += x1 * y2 - x2 * y1
        avg_d = sum(depths) / max(len(depths), 1)
        proj[f.index] = (pts, avg_d, signed_area)

    u_min, u_max = min(all_u), max(all_u)
    v_min, v_max = min(all_v), max(all_v)
    du = max(u_max - u_min, 1e-4)
    dv = max(v_max - v_min, 1e-4)

    grid_n = 72
    # Process outermost faces first (highest depth)
    ordered = sorted(faces, key=lambda f: proj[f.index][1], reverse=True)
    layers: list[list] = []

    remaining = ordered
    for _ in range(4):
        if not remaining:
            break
        occupied: set[tuple[int, int]] = set()
        layer_faces = []
        next_remaining = []

        for f in remaining:
            pts, _, signed_area = proj[f.index]
            if signed_area < -1e-4 and len(remaining) > 1:
                # Back-folded face relative to projection axis
                next_remaining.append(f)
                continue

            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            # Sample strictly inside polygon so shared edges don't collide
            samples = [(cx, cy)]
            for px, py in pts:
                samples.append((0.72 * cx + 0.28 * px, 0.72 * cy + 0.28 * py))

            cells = [
                (
                    min(grid_n - 1, max(0, int((sx - u_min) / du * (grid_n - 1)))),
                    min(grid_n - 1, max(0, int((sy - v_min) / dv * (grid_n - 1)))),
                )
                for sx, sy in samples
            ]
            hits = sum(1 for c in cells[1:] if c in occupied)
            if cells[0] in occupied and hits >= 1:
                next_remaining.append(f)
            else:
                layer_faces.append(f)
                occupied.update(cells)

        if layer_faces:
            layers.append(layer_faces)
            remaining = next_remaining
        else:
            # Avoid infinite loop if all remaining faces were negative area
            layers.append(remaining)
            remaining = []

    if remaining:
        layers.append(remaining)
    return layers


def _pack_rectangles_shelf(items: list[dict], box: list[float], pad_uv: float = 0.012) -> list[tuple[float, float, float]]:
    """
    Pack items [{w, h}] proportionally into box=[u_lo, v_lo, u_hi, v_hi] with zero overlap.
    Uses binary search to find the maximum uniform world-to-UV scale that fits cleanly.
    Returns [(u_origin, v_origin, scale), ...] in the same order as `items`.
    """
    u_lo, v_lo, u_hi, v_hi = box
    box_w = u_hi - u_lo
    box_h = v_hi - v_lo

    order = sorted(range(len(items)), key=lambda i: (max(items[i]["h"], 0.01), items[i]["w"]), reverse=True)

    def try_pack(scale: float) -> list[tuple[float, float]] | None:
        placed = [(0.0, 0.0)] * len(items)
        cur_x = u_lo
        cur_y = v_hi
        row_h = 0.0
        for idx in order:
            w = max(items[idx]["w"] * scale, 0.008)
            h = max(items[idx]["h"] * scale, 0.008)
            if w > box_w + 1e-6 or h > box_h + 1e-6:
                return None
            if cur_x > u_lo + 1e-6 and cur_x + w > u_hi + 1e-6:
                cur_x = u_lo
                cur_y -= row_h + pad_uv
                row_h = 0.0
            if cur_y - h < v_lo - 1e-6:
                return None
            placed[idx] = (cur_x, cur_y - h)
            cur_x += w + pad_uv
            if h > row_h:
                row_h = h
        return placed

    max_w = max((it["w"] for it in items), default=1.0)
    max_h = max((it["h"] for it in items), default=1.0)
    hi = min(box_w / max(max_w, 1e-4), box_h / max(max_h, 1e-4))
    lo = 0.0
    best_placed = None
    best_scale = 0.0

    for _ in range(28):
        mid = (lo + hi) * 0.5
        res = try_pack(mid)
        if res is not None:
            best_placed = res
            best_scale = mid
            lo = mid
        else:
            hi = mid

    if best_placed is None:
        # Ultra-safe grid fallback if many tiny islands exist
        n = len(items)
        cols = max(1, int(n ** 0.5 + 0.999))
        rows = max(1, (n + cols - 1) // cols)
        cw = (box_w - pad_uv * (cols - 1)) / cols
        ch = (box_h - pad_uv * (rows - 1)) / rows
        out = []
        for idx, it in enumerate(items):
            r = idx // cols
            c = idx % cols
            sc = min(cw / max(it["w"], 1e-4), ch / max(it["h"], 1e-4))
            ox = u_lo + c * (cw + pad_uv)
            oy = v_hi - r * (ch + pad_uv) - it["h"] * sc
            out.append((ox, oy, sc))
        return out

    return [(ox, oy, best_scale) for (ox, oy) in best_placed]


def _project_semantic_2x2_uvs(obj: bpy.types.Object) -> tuple[list[dict], list[int]]:
    """
    Build overlap-free, aspect-preserving orthographic UV islands packed inside the
    4 semantic quadrants so Image AI receives crisp, separated, recognizable silhouettes.
    """
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    bm.normal_update()

    if not bm.loops.layers.uv:
        bm.loops.layers.uv.new("UVMap")
    uv_lay = bm.loops.layers.uv.active

    mat_names = [m.name if m else "" for m in obj.data.materials]

    face_rid: list[int] = [0] * len(bm.faces)
    face_axis: list[str] = ["-Y"] * len(bm.faces)
    face_group_key: list[tuple] = []

    for f in bm.faces:
        rid, axis = _classify_face(f, mat_names)
        face_rid[f.index] = rid
        face_axis[f.index] = axis
        mname = mat_names[f.material_index] if 0 <= f.material_index < len(mat_names) else ""
        # Keep special semantic materials (glass, wheels, headlights, canopy, etc.) isolated
        mat_bucket = f.material_index if _is_special_material(mname) else -1
        face_group_key.append((rid, axis, mat_bucket))

    # Flood-fill connected components sharing (quadrant, projection axis, semantic bucket)
    # and smooth normal continuity (< 60 deg)
    visited = [False] * len(bm.faces)
    raw_islands: list[tuple[int, str, list]] = []

    for f in bm.faces:
        if visited[f.index]:
            continue
        key = face_group_key[f.index]
        comp = []
        q = deque([f])
        visited[f.index] = True
        while q:
            cur = q.popleft()
            comp.append(cur)
            for e in cur.edges:
                for nb in e.link_faces:
                    if visited[nb.index]:
                        continue
                    if face_group_key[nb.index] != key:
                        continue
                    if cur.normal.length > 0 and nb.normal.length > 0:
                        if cur.normal.dot(nb.normal) < 0.25:  # > 75 deg sharp corner -> split cleanly
                            continue
                    visited[nb.index] = True
                    q.append(nb)
        raw_islands.append((key[0], key[1], comp))

    # Split any self-overlapping layers inside an island
    quadrant_islands: dict[int, list[dict]] = {0: [], 1: [], 2: [], 3: []}
    for rid, axis, comp in raw_islands:
        layers = _split_overlapping_faces(comp, axis)
        for layer_faces in layers:
            us = []
            vs = []
            part_names = set()
            for f in layer_faces:
                if 0 <= f.material_index < len(mat_names) and mat_names[f.material_index]:
                    part_names.add(mat_names[f.material_index])
                for l in f.loops:
                    u, v, _ = _project_point_2d(l.vert.co, axis)
                    us.append(u)
                    vs.append(v)
            u0, u1 = min(us), max(us)
            v0, v1 = min(vs), max(vs)
            w = max(u1 - u0, 0.03)
            h = max(v1 - v0, 0.03)
            quadrant_islands[rid].append({
                "rid": rid,
                "axis": axis,
                "faces": layer_faces,
                "u0": u0,
                "v0": v0,
                "w": w,
                "h": h,
                "parts": sorted(part_names),
            })

    region_polys: dict[int, list[list[list[float]]]] = {0: [], 1: [], 2: [], 3: []}
    region_parts: dict[int, set[str]] = {0: set(), 1: set(), 2: set(), 3: set()}
    sub_islands_meta: dict[int, list[dict]] = {0: [], 1: [], 2: [], 3: []}

    for rid, reg in enumerate(REGIONS):
        q_items = quadrant_islands[rid]
        if not q_items:
            continue

        placements = _pack_rectangles_shelf(q_items, reg["uv_box"], pad_uv=0.014)
        for item, (ox, oy, scale) in zip(q_items, placements):
            axis = item["axis"]
            u0_local = item["u0"]
            v0_local = item["v0"]
            for p in item["parts"]:
                region_parts[rid].add(p)

            isl_polys = []
            min_u = 1.0
            min_v = 1.0
            max_u = 0.0
            max_v = 0.0

            for f in item["faces"]:
                poly_uvs = []
                for l in f.loops:
                    lu, lv, _ = _project_point_2d(l.vert.co, axis)
                    u = ox + (lu - u0_local) * scale
                    v = oy + (lv - v0_local) * scale
                    u = max(0.005, min(0.995, u))
                    v = max(0.005, min(0.995, v))
                    l[uv_lay].uv = (u, v)
                    poly_uvs.append([round(u, 5), round(v, 5)])
                    if u < min_u:
                        min_u = u
                    if u > max_u:
                        max_u = u
                    if v < min_v:
                        min_v = v
                    if v > max_v:
                        max_v = v
                isl_polys.append(poly_uvs)
                region_polys[rid].append(poly_uvs)

            sub_islands_meta[rid].append({
                "axis": axis,
                "parts": item["parts"][:4],
                "bbox_uv": [round(min_u, 4), round(min_v, 4), round(max_u, 4), round(max_v, 4)],
                "polys": isl_polys,
            })

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
            "bbox_uv": [reg["uv_box"][0], reg["uv_box"][1], reg["header_box"][2], reg["header_box"][3]],
            "parts": sorted(region_parts[rid])[:8],
            "sub_islands": sub_islands_meta[rid],
            "polys": region_polys[rid],
        })
    return islands_meta, face_rid


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
    cam_data.lens = 50.0
    cam_obj = bpy.data.objects.new("PreviewCam", cam_data)
    bpy.context.collection.objects.link(cam_obj)
    scene.camera = cam_obj

    bpy.context.view_layer.update()
    verts_w = [target_obj.matrix_world @ v.co for v in target_obj.data.vertices]
    if verts_w:
        min_v = Vector((min(v.x for v in verts_w), min(v.y for v in verts_w), min(v.z for v in verts_w)))
        max_v = Vector((max(v.x for v in verts_w), max(v.y for v in verts_w), max(v.z for v in verts_w)))
        center = (min_v + max_v) * 0.5
        radius = max((v - center).length for v in verts_w)
    else:
        center = Vector((0.0, 0.0, 0.5))
        radius = 1.0
    dist = max(2.2, radius * 2.95)

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
