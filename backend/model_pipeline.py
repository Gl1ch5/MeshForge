"""
model_pipeline.py - Mode 1: 2-Step 3D Model Generator.
Step 1: Text LLM outputs BOTH `bpy` geometry code AND a structured per-zone texture guide.
        Blender builds mesh -> 2x2 semantic UV atlas -> immediate `stage1_preview.glb` + 3 UV maps.
Step 2: Image AI receives all 3 UV maps + LLM's per-zone guide -> generates texture ->
        optional PBR normal/roughness maps -> exports `model.glb` + 4-side render.
"""
import base64
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import blender_bridge, llm_client, pbr_tools
from .config import OUTPUTS_DIR, STYLE_PRESETS

SYSTEM_BPY_PROMPT = """You are an expert 3D technical artist writing standalone Blender (`bpy` / `bmesh`) Python scripts AND a texture specification.
Your task: build a clean 3D mesh matching the user's request and reference images (if provided), and describe how its UV zones should be textured.

CRITICAL OUTPUT FORMAT — return TWO blocks in order:
1. A ```python ... ``` block with the complete executable Blender script.
2. A ```json ... ``` block with a texture guide describing what materials/details belong in each semantic UV zone:
```json
{{
  "overall_palette": "colors, material finish, wear level",
  "zones": {{
    "FRONT": "exact visual details for front faces (-Y)",
    "TOP_PANEL": "exact visual details for top faces (+Z)",
    "SIDES": "exact visual details for left/right side faces (-X/+X)",
    "BACK_BASE": "exact visual details for back (+Y) and bottom (-Z)"
  }}
}}
```

BLENDER SCRIPT RULES:
1. Assume an empty Blender scene (`bpy` and `bmesh` are available; do NOT call `bpy.ops.wm.read_factory_settings`).
2. Coordinate convention: `-Y` = FRONT (facing viewer), `+Y` = BACK, `-X` = LEFT, `+X` = RIGHT, `+Z` = UP. Build upright with bottom near `Z = 0` and centered at `X = 0, Y = 0`.
3. Topology & UV-Friendly Modeling: {style_rules}
   - Build distinct sub-parts cleanly (separate meshes or disconnected shells for wheels/tires, windshields/glass/canopy, headlights/taillights, spoilers, mirrors, doors, handles) and call `bmesh.ops.recalc_face_normals(bm, faces=bm.faces)` on every generated `bmesh`.
4. Create distinct semantic material slots matching each part's role (`BODY`, `WINDSHIELD`/`GLASS`/`CANOPY`, `WHEEL`/`TIRE`, `HEADLIGHT`, `TAILLIGHT`, `GRILLE`, `EXHAUST`, `SPOILER`, `TRIM`/`METAL`, `SCREEN`, `LABEL`, `FRONT`, `BACK`, `TOP`, `BOTTOM`, etc.) and assign every polygon to its matching slot before joining. This lets the deterministic UV packer isolate glass, wheels, lights, and body panels into clean, non-overlapping islands!
5. Do NOT save files, render, or pack final UVs yourself (the runner normalizes scale, packs the overlap-free 2x2 semantic atlas, and saves `stage1.blend`).
6. Use only valid Blender 4/5 `bpy` / `bmesh` APIs.
"""


def _build_uv_texture_prompt(user_prompt: str, style: str, manifest: dict, llm_guide: dict | None = None) -> str:
    preset = STYLE_PRESETS.get(style, STYLE_PRESETS["ps1"])
    guide_zones = (llm_guide or {}).get("zones", {})
    palette_hint = (llm_guide or {}).get("overall_palette", "")

    legend_lines = []
    for isl in manifest.get("islands", []):
        name = isl["name"]
        parts_str = ", ".join(isl.get("parts") or [name])
        extra = guide_zones.get(name, "")
        sub_descs = []
        for sub in (isl.get("sub_islands") or [])[:10]:
            sp = "/".join(sub.get("parts") or [])
            ax = sub.get("axis", "")
            if sp:
                sub_descs.append(f"{sp}({ax})" if ax else sp)
        sub_str = "; ".join(sub_descs)
        desc = f"{extra} (parts: {parts_str})" if extra else f"surface for: {parts_str}"
        if sub_str:
            desc += f" [separated islands: {sub_str}]"
        legend_lines.append(
            f"- {isl['quadrant']} quadrant ({name}, shown in {isl['color_name']} on Image 2) -> {desc}"
        )
    legend = "\n".join(legend_lines)

    palette_line = f"Palette & finish: {palette_hint}\n" if palette_hint else ""
    return (
        f"Create a clean 2D UV texture atlas image for this 3D asset: '{user_prompt}'.\n"
        f"{palette_line}"
        f"You are given 3 reference UV layout images:\n"
        f"- Image 1 (uv_guide.png): labeled color-coded UV map showing which quadrant is FRONT, TOP_PANEL, SIDES, and BACK_BASE.\n"
        f"- Image 2 (uv_3d_preview.png): 4-view 3D render showing how those exact colors wrap onto the 3D object.\n"
        f"- Image 3 (uv_template_clean.png): clean neutral grey UV polygon silhouettes (no text) showing the exact target layout to paint over.\n\n"
        f"QUADRANT MAPPING (MATCH IMAGE 3 EXACT 2D POSITIONS & SILHOUETTES):\n"
        f"{legend}\n\n"
        f"CRITICAL RULES:\n"
        f"1. Paint realistic 2D surface materials filling 100% of EVERY silhouette shape from Image 3 all the way to its outer edges (including back frames, trim, legs, and corners) in its EXACT 2D position and orientation (top stays up, bottom stays down).\n"
        f"2. DO NOT leave ANY unpainted grey template fill or wireframe lines anywhere, DO NOT copy blue/green/orange/purple guide colors from Images 1-2, and DO NOT render any guide words or labels.\n"
        f"3. Style: {preset['tex_prompt']}"
    )


def run_full_generation(
    prompt: str,
    cfg: dict,
    ref_images: list[dict] | None = None,
    log_cb: Callable[[str], None] | None = None,
    stage_cb: Callable[[dict], None] | None = None,
) -> dict:
    def log(msg: str) -> None:
        if log_cb:
            log_cb(msg)

    job_id = time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    job_dir = OUTPUTS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    user_ref_paths: list[Path] = []
    for i, r in enumerate(ref_images or []):
        data_url = r.get("data_url", "")
        if "," in data_url:
            header, b64 = data_url.split(",", 1)
            ext = "jpg" if "jpeg" in header or "jpg" in header else ("webp" if "webp" in header else "png")
            rp = job_dir / f"ref_{i}.{ext}"
            rp.write_bytes(base64.b64decode(b64))
            user_ref_paths.append(rp)

    style = cfg.get("style", "ps1")
    preset = STYLE_PRESETS.get(style, STYLE_PRESETS["ps1"])
    tex_size = int(cfg.get("tex_size", 512))
    max_retries = max(1, min(int(cfg.get("max_retries", 3)), 5))
    blender_path = cfg.get("blender_path", "")

    log("Шаг 1/2: Генерация bpy-кода геометрии и текстурного гайда...")
    sys_msg = SYSTEM_BPY_PROMPT.format(style_rules=preset["mesh_rules"])

    user_content: list[dict[str, Any]] = [
        {"type": "text", "text": f"Build a 3D model for: {prompt}"}
    ]
    for rp in user_ref_paths:
        user_content.append({"type": "image_url", "image_url": {"url": llm_client.file_to_data_url(rp)}})

    messages = [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": user_content if user_ref_paths else f"Build a 3D model for: {prompt}"},
    ]

    script_path = job_dir / "generated_model.py"
    last_bpy_err = ""
    stage1_ok = False
    llm_guide: dict = {}
    stage1_stats: dict = {}

    for attempt in range(1, max_retries + 1):
        raw_reply = llm_client.call_text_llm(cfg, messages)
        code, parsed_guide = llm_client.extract_python_and_guide(raw_reply)
        if parsed_guide:
            llm_guide = parsed_guide
        script_path.write_text(code, encoding="utf-8")
        messages.append({"role": "assistant", "content": raw_reply})

        log(f"Запуск Blender CLI (попытка {attempt}/{max_retries})...")
        ok, out = blender_bridge.run_bpy_stage(
            blender_path,
            ["stage1", "--script", str(script_path), "--out-dir", str(job_dir), "--tex-size", str(tex_size)],
        )
        if ok and (job_dir / "uv_manifest.json").exists() and (job_dir / "stage1.blend").exists():
            blender_bridge.build_uv_guide_and_previews(job_dir)
            for line in reversed(out.splitlines()):
                s = line.strip()
                if s.startswith("{") and s.endswith("}"):
                    try:
                        stage1_stats = json.loads(s)
                        break
                    except Exception:
                        pass
            stage1_ok = True
            log("Геометрия, гайд и UV готовы.")
            break

        last_bpy_err = out[-1800:]
        log(f"Ошибка Blender на попытке {attempt}, отправляю на исправление...")
        messages.append({
            "role": "user",
            "content": (
                f"Your Blender bpy script failed with this error:\n```\n{last_bpy_err}\n```\n"
                "Fix the script and return the corrected ```python ... ``` code and ```json ... ``` texture guide."
            ),
        })

    if not stage1_ok:
        raise RuntimeError(f"Не удалось собрать геометрию за {max_retries} попыток:\n{last_bpy_err}")

    if llm_guide:
        (job_dir / "llm_texture_guide.json").write_text(json.dumps(llm_guide, indent=2, ensure_ascii=False), encoding="utf-8")

    meta = {
        "kind": "model",
        "job_id": job_id,
        "prompt": prompt,
        "style": style,
        "tex_size": tex_size,
        "text_model": cfg.get("text_model"),
        "image_model": cfg.get("image_model"),
        "poly_count": stage1_stats.get("poly_count", 0),
        "vert_count": stage1_stats.get("vert_count", 0),
        "created_at": time.time(),
    }
    (job_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    if stage_cb:
        stage_cb({
            "stage": "geometry_ready",
            "job_id": job_id,
            "meta": meta,
            "preview_glb_url": f"/outputs/{job_id}/stage1_preview.glb" if (job_dir / "stage1_preview.glb").exists() else "",
            "uv_guide_url": f"/outputs/{job_id}/uv_guide.png",
            "uv_3d_preview_url": f"/outputs/{job_id}/uv_3d_preview.png",
            "uv_clean_url": f"/outputs/{job_id}/uv_template_clean.png",
            "code": script_path.read_text(encoding="utf-8"),
        })

    return run_stage2_texture(job_id, cfg, log_cb=log_cb, stage_cb=stage_cb)


def run_stage2_texture(
    job_id: str,
    cfg: dict,
    custom_tex_prompt: str | None = None,
    log_cb: Callable[[str], None] | None = None,
    stage_cb: Callable[[dict], None] | None = None,
) -> dict:
    def log(msg: str) -> None:
        if log_cb:
            log_cb(msg)

    job_dir = OUTPUTS_DIR / job_id
    meta_path = job_dir / "meta.json"
    if not meta_path.exists():
        raise RuntimeError(f"Задача {job_id} не найдена")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    prompt = custom_tex_prompt or meta["prompt"]
    style = cfg.get("style", meta.get("style", "ps1"))
    preset = STYLE_PRESETS.get(style, STYLE_PRESETS["ps1"])
    tex_size = int(cfg.get("tex_size", meta.get("tex_size", 512)))
    blender_path = cfg.get("blender_path", "")

    manifest = json.loads((job_dir / "uv_manifest.json").read_text(encoding="utf-8"))
    llm_guide = None
    guide_file = job_dir / "llm_texture_guide.json"
    if guide_file.is_file():
        try:
            llm_guide = json.loads(guide_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    uv_prompt = _build_uv_texture_prompt(prompt, style, manifest, llm_guide)
    (job_dir / "texture_prompt.txt").write_text(uv_prompt, encoding="utf-8")

    user_ref_paths = sorted(job_dir.glob("ref_*.*"))
    log("Шаг 2/2: Генерация UV-текстуры по 3 картам...")
    raw_tex_path = job_dir / "raw_texture.png"
    llm_client.call_image_ai(
        cfg,
        uv_prompt,
        raw_tex_path,
        ref_paths=[
            job_dir / "uv_guide.png",
            job_dir / "uv_3d_preview.png",
            job_dir / "uv_template_clean.png",
            *user_ref_paths[:2],
        ],
    )

    final_tex_path = blender_bridge.bleed_and_prepare_texture(
        raw_tex_path,
        job_dir / "uv_mask.png",
        job_dir / "texture.png",
        target_size=tex_size,
        style=style,
    )

    # For realistic/stylized models, also bake subtle normal/roughness maps
    normal_arg = ""
    rough_arg = ""
    if style in ("realistic", "stylized"):
        pbr_out = pbr_tools.generate_pbr_maps(
            final_tex_path,
            job_dir,
            ["normal", "roughness"],
            params={"normal_strength": 1.6, "roughness_base": 0.65, "roughness_contrast": 1.0},
            seamless=False,
        )
        if pbr_out.get("normal"):
            normal_arg = str(job_dir / pbr_out["normal"])
        if pbr_out.get("roughness"):
            rough_arg = str(job_dir / pbr_out["roughness"])

    log("Наложение текстуры, экспорт GLB и рендер 4 сторон...")
    stage2_args = [
        "stage2",
        "--out-dir", str(job_dir),
        "--tex", str(final_tex_path),
        "--ps1", "1" if preset["pixel_crisp"] else "0",
    ]
    if normal_arg:
        stage2_args += ["--normal", normal_arg]
    if rough_arg:
        stage2_args += ["--roughness", rough_arg]

    ok, out = blender_bridge.run_bpy_stage(blender_path, stage2_args)
    if not ok or not (job_dir / "model.glb").exists():
        raise RuntimeError(f"Ошибка экспорта GLB / рендера:\n{out[-1500:]}")

    blender_bridge.stitch_4_views(job_dir, "_tex3d", "preview_4side.png")

    stats = {}
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                stats = json.loads(line)
                break
            except Exception:
                pass

    meta["poly_count"] = stats.get("poly_count", meta.get("poly_count", 0))
    meta["vert_count"] = stats.get("vert_count", meta.get("vert_count", 0))
    meta["status"] = "done"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    log("Готово.")
    return {
        "ok": True,
        "kind": "model",
        "job_id": job_id,
        "meta": meta,
        "glb_url": f"/outputs/{job_id}/model.glb",
        "preview_4side_url": f"/outputs/{job_id}/preview_4side.png",
        "uv_guide_url": f"/outputs/{job_id}/uv_guide.png",
        "uv_3d_preview_url": f"/outputs/{job_id}/uv_3d_preview.png",
        "uv_clean_url": f"/outputs/{job_id}/uv_template_clean.png",
        "texture_url": f"/outputs/{job_id}/texture.png",
        "code": (job_dir / "generated_model.py").read_text(encoding="utf-8"),
    }
