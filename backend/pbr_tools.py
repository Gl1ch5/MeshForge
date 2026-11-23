"""
pbr_tools.py - Vectorized seamless tiling & PBR map generator (Normal, Roughness, Height, AO, Metallic).
Uses NumPy + Pillow only.
"""
from pathlib import Path
import numpy as np
from PIL import Image


def make_seamless_toroidal(img: Image.Image, blend_ratio: float = 0.18) -> Image.Image:
    """
    Create a genuinely tileable image by shifting half-width/half-height and
    crossfading the central seam bands with cosine weights.
    """
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
    h, w, _ = arr.shape
    bw = max(8, int(w * blend_ratio))
    bh = max(8, int(h * blend_ratio))

    # Horizontal seam crossfade: roll by w//2, blend center seam band
    rolled_x = np.roll(arr, w // 2, axis=1)
    wx = np.zeros((1, w, 1), dtype=np.float32)
    cx = w // 2
    x_idx = np.arange(w)
    dist_x = np.abs(x_idx - cx)
    mask_x = dist_x < bw
    wx[0, mask_x, 0] = 0.5 * (1.0 + np.cos(np.pi * dist_x[mask_x] / bw))
    # Keep original center intact; take rolled pixels only near outer edges of rolled image?
    # Wait: the seam of `rolled_x` IS at cx (where x=0 and x=w-1 met)!
    # So at cx (dist_x < bw), we blend `arr` (which is continuous at cx!) over `rolled_x`,
    # then roll back by -(w // 2)!
    blended_x = rolled_x * (1.0 - wx) + arr * wx
    step1 = np.roll(blended_x, -(w // 2), axis=1)

    # Vertical seam crossfade
    rolled_y = np.roll(step1, h // 2, axis=0)
    wy = np.zeros((h, 1, 1), dtype=np.float32)
    cy = h // 2
    y_idx = np.arange(h)
    dist_y = np.abs(y_idx - cy)
    mask_y = dist_y < bh
    wy[mask_y, 0, 0] = 0.5 * (1.0 + np.cos(np.pi * dist_y[mask_y] / bh))
    blended_y = rolled_y * (1.0 - wy) + step1 * wy
    out = np.roll(blended_y, -(h // 2), axis=0)

    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), mode="RGB")


def _wrap_box_blur(gray: np.ndarray, radius: int) -> np.ndarray:
    """Fast separable tile-wrapped box blur via cumulative sums."""
    if radius <= 0:
        return gray
    k = 2 * radius + 1
    # Horizontal wrap pad
    pad_x = np.pad(gray, ((0, 0), (radius, radius)), mode="wrap")
    csum_x = np.cumsum(np.pad(pad_x, ((0, 0), (1, 0)), mode="constant"), axis=1)
    blur_x = (csum_x[:, k:] - csum_x[:, :-k]) / float(k)

    # Vertical wrap pad
    pad_y = np.pad(blur_x, ((radius, radius), (0, 0)), mode="wrap")
    csum_y = np.cumsum(np.pad(pad_y, ((1, 0), (0, 0)), mode="constant"), axis=0)
    blur_xy = (csum_y[k:, :] - csum_y[:-k, :]) / float(k)
    return blur_xy


def generate_pbr_maps(
    albedo_path: Path,
    out_dir: Path,
    maps: list[str],
    params: dict | None = None,
    seamless: bool = True,
) -> dict[str, str]:
    """
    Generate PBR texture maps (`normal`, `roughness`, `height`, `ao`, `metallic`)
    from an albedo image using wrap-aware Sobel and multi-scale cavity filters.
    """
    p = params or {}
    normal_strength = float(p.get("normal_strength", 2.4))
    roughness_base = float(p.get("roughness_base", 0.65))
    roughness_contrast = float(p.get("roughness_contrast", 1.2))
    invert_roughness = bool(p.get("invert_roughness", False))
    ao_strength = float(p.get("ao_strength", 0.75))
    height_contrast = float(p.get("height_contrast", 1.15))
    metallic_level = float(p.get("metallic_level", 0.0))

    rgb = np.asarray(Image.open(albedo_path).convert("RGB"), dtype=np.float32) / 255.0
    lum = 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]

    # Normalize & shape height field
    lo, hi = np.percentile(lum, [2.0, 98.0])
    span = max(float(hi - lo), 1e-4)
    h_norm = np.clip((lum - lo) / span, 0.0, 1.0)
    h_shaped = np.clip((h_norm - 0.5) * height_contrast + 0.5, 0.0, 1.0)

    out_paths: dict[str, str] = {}
    req_maps = {m.strip().lower() for m in maps}

    if "height" in req_maps:
        hp = out_dir / "height.png"
        Image.fromarray((h_shaped * 255.0).astype(np.uint8), mode="L").save(hp)
        out_paths["height"] = hp.name

    if "normal" in req_maps:
        pad_mode = "wrap" if seamless else "edge"
        hp = np.pad(h_shaped, 1, mode=pad_mode)
        # Sobel kernels
        gx = (
            (hp[:-2, 2:] + 2.0 * hp[1:-1, 2:] + hp[2:, 2:])
            - (hp[:-2, :-2] + 2.0 * hp[1:-1, :-2] + hp[2:, :-2])
        )
        gy = (
            (hp[2:, :-2] + 2.0 * hp[2:, 1:-1] + hp[2:, 2:])
            - (hp[:-2, :-2] + 2.0 * hp[:-2, 1:-1] + hp[:-2, 2:])
        )
        nx = -gx * normal_strength
        ny = gy * normal_strength
        nz = np.ones_like(nx)
        norm = np.sqrt(nx * nx + ny * ny + nz * nz) + 1e-8
        n_rgb = np.stack([(nx / norm) * 0.5 + 0.5, (ny / norm) * 0.5 + 0.5, (nz / norm) * 0.5 + 0.5], axis=-1)
        np_path = out_dir / "normal.png"
        Image.fromarray(np.clip(n_rgb * 255.0, 0, 255).astype(np.uint8), mode="RGB").save(np_path)
        out_paths["normal"] = np_path.name

    if "ao" in req_maps:
        b1 = _wrap_box_blur(h_shaped, 3)
        b2 = _wrap_box_blur(h_shaped, 10)
        b3 = _wrap_box_blur(h_shaped, 24)
        avg_surround = 0.45 * b1 + 0.35 * b2 + 0.20 * b3
        cavity = np.clip(avg_surround - h_shaped, 0.0, 1.0)
        ao = np.clip(1.0 - cavity * (2.2 * ao_strength), 0.12, 1.0)
        ao_path = out_dir / "ao.png"
        Image.fromarray((ao * 255.0).astype(np.uint8), mode="L").save(ao_path)
        out_paths["ao"] = ao_path.name

    if "roughness" in req_maps:
        micro_detail = np.abs(h_shaped - _wrap_box_blur(h_shaped, 2))
        base_signal = h_shaped if invert_roughness else (1.0 - h_shaped)
        r_map = roughness_base + (base_signal - 0.5) * 0.45 * roughness_contrast + micro_detail * 0.35
        r_map = np.clip(r_map, 0.04, 0.99)
        r_path = out_dir / "roughness.png"
        Image.fromarray((r_map * 255.0).astype(np.uint8), mode="L").save(r_path)
        out_paths["roughness"] = r_path.name

    if "metallic" in req_maps:
        sat = (np.max(rgb, axis=2) - np.min(rgb, axis=2)) / (np.max(rgb, axis=2) + 1e-5)
        m_val = np.clip(metallic_level * (1.0 - sat * 0.65) * (0.65 + 0.55 * h_shaped), 0.0, 1.0)
        m_path = out_dir / "metallic.png"
        Image.fromarray((m_val * 255.0).astype(np.uint8), mode="L").save(m_path)
        out_paths["metallic"] = m_path.name

    return out_paths


def build_tiled_preview(albedo_path: Path, out_path: Path, tiles: int = 2) -> Path:
    im = Image.open(albedo_path).convert("RGB")
    sz = 256 if tiles >= 3 else 384
    tile = im.resize((sz, sz), Image.Resampling.NEAREST if im.width <= 256 else Image.Resampling.BILINEAR)
    canvas = Image.new("RGB", (sz * tiles, sz * tiles))
    for y in range(tiles):
        for x in range(tiles):
            canvas.paste(tile, (x * sz, y * sz))
    canvas.save(out_path)
    return out_path
