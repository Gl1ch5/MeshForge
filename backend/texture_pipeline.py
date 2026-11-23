"""
texture_pipeline.py - Mode 2 (Standalone Seamless Texture + PBR Maps + Enhancer)
& Mode 3 (Texture Pack Planner + Batch Generator) + Unified Job History.
"""
import base64
import json
import re
import time
import uuid
from pathlib import Path
from typing import Callable

from PIL import Image

from . import llm_client, pbr_tools
from .config import OUTPUTS_DIR, STYLE_PRESETS

ENHANCER_SYSTEM = """You are a senior game material / PBR texture technical artist.
Given a user's short texture description, requested style, and seamless flag:
1. Write a clean, detailed English image-generation prompt for a flat orthographic top-down 2D texture tile (no perspective, no 3D spheres, even neutral lighting, zero text/watermarks).
2. Choose optimal PBR map generation parameters tailored to this physical material.

Return ONLY valid JSON:
{
  "enhanced_prompt": "...",
  "pbr": {
    "normal_strength": 2.4,
    "roughness_base": 0.65,
    "roughness_contrast": 1.2,
    "invert_roughness": false,
    "ao_strength": 0.75,
    "height_contrast": 1.2,
    "metallic_level": 0.0
  }
}"""

PACK_PLANNER_SYSTEM = """You are a game environment material director.
The user wants a cohesive pack of game textures (e.g. for a road, industrial corridor, forest, sci-fi base).
Analyze the user's request and break it down into {count_hint} distinct, practical surface textures.
For EACH texture, provide a slug (ascii snake_case), Russian display title, flat orthographic top-down English image prompt, seamless bool, and tailored PBR map parameters.

Return ONLY valid JSON:
{{
  "pack_title": "Short title",
  "items": [
    {{
      "slug": "asphalt_main",
      "title": "Асфальт основной",
      "prompt": "...",
      "seamless": true,
      "pbr": {{
        "normal_strength": 2.8,
        "roughness_base": 0.78,
        "roughness_contrast": 1.3,
        "invert_roughness": false,
        "ao_strength": 0.8,
        "height_contrast": 1.25,
        "metallic_level": 0.0
      }}
    }}
  ]
}}"""


def _enhance_texture_prompt(cfg: dict, user_prompt: str, style: str, seamless: bool) -> tuple[str, dict]:
    preset = STYLE_PRESETS.get(style, STYLE_PRESETS["realistic"])
    msg = (
        f"User prompt: {user_prompt}\n"
        f"Target style: {preset['label']} ({preset['tex_prompt']})\n"
        f"Seamless tileable: {'YES' if seamless else 'NO'}\n"
        "Return JSON only."
    )
    raw = llm_client.call_text_llm(
        cfg,
        [{"role": "system", "content": ENHANCER_SYSTEM}, {"role": "user", "content": msg}],
        temperature=0.3,
    )
    data = llm_client.extract_json_object(raw)
    ep = str(data.get("enhanced_prompt", "")).strip() or user_prompt
    pbr = data.get("pbr") if isinstance(data.get("pbr"), dict) else {}
    return ep, pbr


def _finalize_texture_files(
    raw_path: Path,
    out_dir: Path,
    tex_size: int,
    style: str,
    seamless: bool,
    maps: list[str],
    pbr_params: dict,
) -> tuple[Path, dict[str, str], str]:
    preset = STYLE_PRESETS.get(style, STYLE_PRESETS["realistic"])
    im = Image.open(raw_path).convert("RGB")
    resamp = Image.Resampling.NEAREST if preset["pixel_crisp"] else Image.Resampling.LANCZOS
    im = im.resize((tex_size, tex_size), resamp)

    if seamless:
        im = pbr_tools.make_seamless_toroidal(im, blend_ratio=0.16)

    q_colors = int(preset.get("quantize_colors", 0))
    if q_colors > 0:
        im = im.quantize(colors=q_colors, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")

    albedo_path = out_dir / "albedo.png"
    im.save(albedo_path)

    map_files = {}
    if maps:
        map_files = pbr_tools.generate_pbr_maps(albedo_path, out_dir, maps, params=pbr_params, seamless=seamless)

    tiled_name = ""
    if seamless:
        pbr_tools.build_tiled_preview(albedo_path, out_dir / "tiled_2x2.png", tiles=2)
        tiled_name = "tiled_2x2.png"

    return albedo_path, map_files, tiled_name


def run_texture_generation(
    prompt: str,
    cfg: dict,
    options: dict | None = None,
    ref_images: list[dict] | None = None,
    log_cb: Callable[[str], None] | None = None,
    stage_cb: Callable[[dict], None] | None = None,
) -> dict:
    def log(msg: str) -> None:
        if log_cb:
            log_cb(msg)

    opts = options or {}
    style = opts.get("style", cfg.get("style", "realistic"))
    preset = STYLE_PRESETS.get(style, STYLE_PRESETS["realistic"])
    tex_size = int(opts.get("tex_size", cfg.get("tex_size", 512)))
    seamless = bool(opts.get("seamless", True))
    enhance = bool(opts.get("enhance", True))
    maps = list(opts.get("maps", ["normal", "roughness", "ao", "height"]))
    pbr_params = dict(opts.get("pbr_params") or {})

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

    final_prompt = prompt
    if enhance:
        log("Улучшение промпта и расчёт параметров PBR-карт...")
        try:
            enhanced, ai_pbr = _enhance_texture_prompt(cfg, prompt, style, seamless)
            final_prompt = enhanced
            merged = {**ai_pbr, **pbr_params} if opts.get("override_pbr") else {**pbr_params, **ai_pbr}
            pbr_params = merged
        except Exception as e:
            log(f"Улучшайзер пропущен ({e}), использую базовый промпт...")

    tile_hint = "Seamless tileable edge-to-edge repeating pattern. " if seamless else ""
    img_prompt = (
        f"Flat orthographic top-down 2D game texture surface, zero perspective distortion, even neutral lighting, no text: "
        f"{final_prompt}. {tile_hint}Style: {preset['tex_prompt']}"
    )
    (job_dir / "texture_prompt.txt").write_text(img_prompt, encoding="utf-8")

    log("Генерация Albedo текстуры...")
    raw_path = job_dir / "albedo_raw.png"
    llm_client.call_image_ai(cfg, img_prompt, raw_path, ref_paths=user_ref_paths[:2])

    log("Бесшовная обработка и генерация PBR-карт...")
    _, map_files, tiled_name = _finalize_texture_files(
        raw_path, job_dir, tex_size, style, seamless, maps, pbr_params
    )

    urls = {"albedo": f"/outputs/{job_id}/albedo.png"}
    for k, fn in map_files.items():
        urls[k] = f"/outputs/{job_id}/{fn}"
    if tiled_name:
        urls["tiled"] = f"/outputs/{job_id}/{tiled_name}"

    meta = {
        "kind": "texture",
        "job_id": job_id,
        "prompt": prompt,
        "enhanced_prompt": final_prompt,
        "style": style,
        "tex_size": tex_size,
        "seamless": seamless,
        "enhance": enhance,
        "maps": maps,
        "pbr_params": pbr_params,
        "urls": urls,
        "status": "done",
        "created_at": time.time(),
    }
    (job_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    log("Готово.")
    return {"ok": True, "kind": "texture", "job_id": job_id, "meta": meta, "urls": urls}


def run_texture_pack(
    prompt: str,
    cfg: dict,
    options: dict | None = None,
    ref_images: list[dict] | None = None,
    log_cb: Callable[[str], None] | None = None,
    stage_cb: Callable[[dict], None] | None = None,
) -> dict:
    def log(msg: str) -> None:
        if log_cb:
            log_cb(msg)

    opts = options or {}
    style = opts.get("style", cfg.get("style", "realistic"))
    preset = STYLE_PRESETS.get(style, STYLE_PRESETS["realistic"])
    tex_size = int(opts.get("tex_size", cfg.get("tex_size", 512)))
    seamless = bool(opts.get("seamless", True))
    maps = list(opts.get("maps", ["normal", "roughness", "ao", "height"]))
    count = int(opts.get("count", 4))

    job_id = time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    job_dir = OUTPUTS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    log("Анализ запроса и планирование пака текстур...")
    count_hint = f"exact items requested by the user (or around {count} items if unspecified)"
    sys_p = PACK_PLANNER_SYSTEM.format(count_hint=count_hint)
    user_p = (
        f"Pack request: {prompt}\n"
        f"Style: {preset['label']} ({preset['tex_prompt']})\n"
        f"Default seamless: {seamless}\n"
        f"Target count: {count}"
    )
    raw_plan = llm_client.call_text_llm(
        cfg,
        [{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}],
        temperature=0.35,
    )
    plan = llm_client.extract_json_object(raw_plan)
    items = plan.get("items") or []
    if not items:
        raise RuntimeError("Не удалось спланировать список текстур для пака")

    pack_title = str(plan.get("pack_title", prompt[:40]))
    results_items = []

    for idx, it in enumerate(items, start=1):
        raw_slug = str(it.get("slug") or f"tex_{idx}")
        slug = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_slug).strip("_") or f"tex_{idx}"
        title = str(it.get("title") or slug)
        it_prompt = str(it.get("prompt") or prompt)
        it_seamless = bool(it.get("seamless", seamless))
        it_pbr = it.get("pbr") if isinstance(it.get("pbr"), dict) else {}

        item_dir = job_dir / slug
        item_dir.mkdir(parents=True, exist_ok=True)
        log(f"[{idx}/{len(items)}] Генерация: {title}...")

        tile_hint = "Seamless tileable edge-to-edge repeating pattern. " if it_seamless else ""
        full_img_prompt = (
            f"Flat orthographic top-down 2D game texture surface, zero perspective distortion, even neutral lighting, no text: "
            f"{it_prompt}. {tile_hint}Style: {preset['tex_prompt']}"
        )
        raw_path = item_dir / "albedo_raw.png"
        llm_client.call_image_ai(cfg, full_img_prompt, raw_path)

        _, map_files, tiled_name = _finalize_texture_files(
            raw_path, item_dir, tex_size, style, it_seamless, maps, it_pbr
        )

        urls = {"albedo": f"/outputs/{job_id}/{slug}/albedo.png"}
        for k, fn in map_files.items():
            urls[k] = f"/outputs/{job_id}/{slug}/{fn}"
        if tiled_name:
            urls["tiled"] = f"/outputs/{job_id}/{slug}/{tiled_name}"

        entry = {
            "slug": slug,
            "title": title,
            "prompt": it_prompt,
            "seamless": it_seamless,
            "pbr_params": it_pbr,
            "urls": urls,
        }
        results_items.append(entry)

        if stage_cb:
            stage_cb({
                "stage": "pack_progress",
                "job_id": job_id,
                "pack_title": pack_title,
                "done": idx,
                "total": len(items),
                "items": results_items,
            })

    meta = {
        "kind": "pack",
        "job_id": job_id,
        "prompt": prompt,
        "pack_title": pack_title,
        "style": style,
        "tex_size": tex_size,
        "seamless": seamless,
        "maps": maps,
        "items": results_items,
        "status": "done",
        "created_at": time.time(),
    }
    (job_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Пак готов ({len(results_items)} текстур).")
    return {"ok": True, "kind": "pack", "job_id": job_id, "meta": meta, "items": results_items}


def list_jobs() -> list[dict]:
    items = []
    for d in sorted(OUTPUTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        mp = d / "meta.json"
        if not mp.exists():
            continue
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
            m.setdefault("kind", "model")
            if m["kind"] == "model":
                m["has_glb"] = (d / "model.glb").exists()
                m["has_preview"] = (d / "preview_4side.png").exists()
            items.append(m)
        except Exception:
            pass
    return items[:60]
