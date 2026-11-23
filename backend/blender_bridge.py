"""
blender_bridge.py - Runs Blender CLI / bpy subprocesses and renders UV guide overlays & 4-view grids.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

from .config import APP_ROOT, BUNDLE_ROOT, STYLE_PRESETS, find_steam_blender


def detect_bpy_command(blender_path: str = "") -> list[str]:
    bp = (blender_path or "").strip() or find_steam_blender()
    runner_path = str(BUNDLE_ROOT / "backend" / "bpy_runner.py")
    if bp and Path(bp).is_file():
        return [bp, "--background", "--factory-startup", "--python", runner_path, "--"]
    if not getattr(sys, "frozen", False) and importlib.util.find_spec("bpy") is not None:
        return [sys.executable, runner_path]
    py_exe = shutil.which("python")
    if py_exe:
        chk = subprocess.run([py_exe, "-c", "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('bpy') else 1)"], capture_output=True)
        if chk.returncode == 0:
            return [py_exe, runner_path]
    raise RuntimeError(
        "Не найден blender.exe и модуль bpy. Откройте Мастер Blender в шапке программы."
    )


def run_bpy_stage(blender_path: str, args: list[str], timeout: int = 120) -> tuple[bool, str]:
    cmd = detect_bpy_command(blender_path) + args
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=creationflags)
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return proc.returncode == 0, out.strip()


def _load_font(size: int):
    for p in (
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/consolab.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
    ):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def stitch_4_views(out_dir: Path, prefix: str, dest_name: str) -> Path:
    views = ["FRONT", "RIGHT", "BACK", "LEFT"]
    tiles = []
    font = _load_font(22)
    for lbl in views:
        vp = out_dir / f"{prefix}_{lbl}.png"
        if vp.is_file():
            im = Image.open(vp).convert("RGB").resize((512, 512), Image.Resampling.BILINEAR)
            try:
                vp.unlink()
            except Exception:
                pass
        else:
            im = Image.new("RGB", (512, 512), (20, 22, 26))
        draw = ImageDraw.Draw(im)
        draw.rectangle([10, 10, 118, 42], fill=(17, 18, 20))
        draw.text((20, 15), lbl, fill=(235, 238, 242), font=font)
        tiles.append(im)

    grid = Image.new("RGB", (1024, 1024), (17, 18, 20))
    grid.paste(tiles[0], (0, 0))
    grid.paste(tiles[1], (512, 0))
    grid.paste(tiles[2], (0, 512))
    grid.paste(tiles[3], (512, 512))
    dest = out_dir / dest_name
    grid.save(dest)
    return dest


def build_uv_guide_and_previews(job_dir: Path) -> dict:
    manifest = json.loads((job_dir / "uv_manifest.json").read_text(encoding="utf-8"))
    guide_size = 1024

    img_ui = Image.new("RGB", (guide_size, guide_size), (18, 19, 22))
    draw_ui = ImageDraw.Draw(img_ui)

    img_ai = Image.new("RGB", (guide_size, guide_size), (20, 22, 26))
    draw_ai = ImageDraw.Draw(img_ai)

    mask = Image.new("L", (guide_size, guide_size), 0)
    mdraw = ImageDraw.Draw(mask)

    font_big = _load_font(24)
    compact_islands = []

    for isl in manifest.get("islands", []):
        rgb = tuple(isl["rgb"])
        for poly in isl.get("polys", []):
            pts = [(u * (guide_size - 1), (1.0 - v) * (guide_size - 1)) for (u, v) in poly]
            if len(pts) >= 3:
                draw_ui.polygon(pts, fill=rgb, outline=(240, 242, 246))
                draw_ai.polygon(pts, fill=(84, 88, 96), outline=(108, 114, 124))
                mdraw.polygon(pts, fill=255)

        u0, v0, u1, v1 = isl["bbox_uv"]
        x0 = int(u0 * (guide_size - 1))
        x1 = int(u1 * (guide_size - 1))
        y0 = int((1.0 - v1) * (guide_size - 1))
        y1 = int((1.0 - v0) * (guide_size - 1))
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2

        draw_ui.rectangle([x0, y0, x1, y1], outline=(245, 246, 248), width=2)
        lbl = f"{isl['name']} ({isl['color_name']})"
        draw_ui.rectangle([cx - 125, cy - 18, cx + 125, cy + 18], fill=(14, 15, 18))
        draw_ui.text((cx, cy), lbl[:22], fill=(248, 248, 250), font=font_big, anchor="mm")

        compact_islands.append({
            "index": isl["index"],
            "name": isl["name"],
            "quadrant": isl["quadrant"],
            "color_name": isl["color_name"],
            "rgb": isl["rgb"],
            "bbox_uv": isl["bbox_uv"],
            "parts": isl.get("parts", []),
        })

    draw_ai.line([(guide_size // 2, 0), (guide_size // 2, guide_size)], fill=(45, 48, 56), width=2)
    draw_ai.line([(0, guide_size // 2), (guide_size, guide_size // 2)], fill=(45, 48, 56), width=2)

    img_ui.save(job_dir / "uv_guide.png")
    img_ai.save(job_dir / "uv_template_clean.png")
    mask.save(job_dir / "uv_mask.png")
    stitch_4_views(job_dir, "_uv3d", "uv_3d_preview.png")

    manifest["islands"] = compact_islands
    (job_dir / "uv_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def bleed_and_prepare_texture(
    raw_tex_path: Path,
    uv_mask_path: Path,
    out_tex_path: Path,
    target_size: int = 512,
    style: str = "ps1",
) -> Path:
    preset = STYLE_PRESETS.get(style, STYLE_PRESETS["ps1"])
    pixel_crisp = bool(preset.get("pixel_crisp", True))
    q_colors = int(preset.get("quantize_colors", 0))

    tex = Image.open(raw_tex_path).convert("RGB")
    resamp = Image.Resampling.NEAREST if pixel_crisp else Image.Resampling.LANCZOS
    tex = tex.resize((target_size, target_size), resamp)

    if uv_mask_path.exists():
        mask = Image.open(uv_mask_path).convert("L").resize((target_size, target_size), Image.Resampling.NEAREST)
        px = tex.load()
        mpx = mask.load()

        dark_edge = []
        for y in range(target_size):
            for x in range(target_size):
                if mpx[x, y] < 128:
                    continue
                is_edge = any(
                    not (0 <= x + dx < target_size and 0 <= y + dy < target_size) or mpx[x + dx, y + dy] < 128
                    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1))
                )
                if is_edge:
                    r, g, b = px[x, y]
                    if r < 34 and g < 34 and b < 38:
                        dark_edge.append((x, y))
        for x, y in dark_edge:
            mpx[x, y] = 0

        for _ in range(6):
            updates = []
            for y in range(target_size):
                for x in range(target_size):
                    if mpx[x, y] >= 128:
                        continue
                    r_sum = g_sum = b_sum = cnt = 0
                    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                        nx, ny = x + dx, y + dy
                        if 0 <= nx < target_size and 0 <= ny < target_size and mpx[nx, ny] >= 128:
                            r, g, b = px[nx, ny]
                            r_sum += r
                            g_sum += g
                            b_sum += b
                            cnt += 1
                    if cnt > 0:
                        updates.append((x, y, (r_sum // cnt, g_sum // cnt, b_sum // cnt)))
            for x, y, col in updates:
                px[x, y] = col
                mpx[x, y] = 255

    if q_colors > 0:
        tex = tex.quantize(colors=q_colors, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")

    tex.save(out_tex_path)
    return out_tex_path
