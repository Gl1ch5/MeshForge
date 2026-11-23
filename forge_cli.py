#!/usr/bin/env python3
"""
forge_cli.py - Agent-friendly CLI for MeshForge (3D models, seamless PBR textures, and texture packs).
Outputs structured JSON on stdout for easy consumption by AI coding agents and automation scripts.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import pipeline


def _apply_api_overrides(cfg: dict, args) -> dict:
    if getattr(args, "text_url", None):
        cfg["text_base_url"] = args.text_url
    if getattr(args, "text_key", None):
        cfg["text_api_key"] = args.text_key
    if getattr(args, "text_model", None):
        cfg["text_model"] = args.text_model
    if getattr(args, "image_url", None):
        cfg["image_base_url"] = args.image_url
    if getattr(args, "image_key", None):
        cfg["image_api_key"] = args.image_key
    if getattr(args, "image_model", None):
        cfg["image_model"] = args.image_model
    if getattr(args, "blender", None):
        cfg["blender_path"] = args.blender
    elif not cfg.get("blender_path"):
        cfg["blender_path"] = pipeline.find_steam_blender()
    return cfg


def _copy_to_out(src_dir: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        if item.name.startswith("_"):
            continue
        dst = out_dir / item.name
        if item.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(item, dst)
        elif item.is_file():
            shutil.copy2(item, dst)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    p = argparse.ArgumentParser(description="MeshForge Agent CLI (3D models, PBR textures, texture packs)")
    p.add_argument("--text-url")
    p.add_argument("--text-key")
    p.add_argument("--text-model")
    p.add_argument("--image-url")
    p.add_argument("--image-key")
    p.add_argument("--image-model")
    p.add_argument("--blender")
    p.add_argument("--quiet", action="store_true", help="Suppress progress logs on stderr")

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("models", help="List models from configured Text and Image APIs")

    pm = sub.add_parser("model", help="Generate a 3D GLB model with UV guide & texture")
    pm.add_argument("prompt")
    pm.add_argument("--style", default="ps1", choices=list(pipeline.STYLE_PRESETS.keys()))
    pm.add_argument("--size", type=int, default=512, choices=[256, 512, 1024])
    pm.add_argument("--out", help="Optional directory to copy exported files into")

    pt = sub.add_parser("texture", help="Generate a single texture + optional PBR maps")
    pt.add_argument("prompt")
    pt.add_argument("--style", default="realistic", choices=list(pipeline.STYLE_PRESETS.keys()))
    pt.add_argument("--size", type=int, default=512, choices=[256, 512, 1024])
    pt.add_argument("--seamless", action="store_true")
    pt.add_argument("--enhance", action="store_true")
    pt.add_argument("--maps", default="normal,roughness,ao,height", help="Comma-separated PBR maps or 'none'")
    pt.add_argument("--out", help="Optional directory to copy exported files into")

    pp = sub.add_parser("pack", help="Plan and generate a multi-texture pack + PBR maps")
    pp.add_argument("prompt")
    pp.add_argument("--count", type=int, default=4)
    pp.add_argument("--style", default="realistic", choices=list(pipeline.STYLE_PRESETS.keys()))
    pp.add_argument("--size", type=int, default=512, choices=[256, 512, 1024])
    pp.add_argument("--seamless", action="store_true")
    pp.add_argument("--maps", default="normal,roughness,ao,height", help="Comma-separated PBR maps or 'none'")
    pp.add_argument("--out", help="Optional directory to copy pack files into")

    args = p.parse_args()
    cfg = _apply_api_overrides(pipeline.load_settings(), args)

    def log_err(msg: str) -> None:
        if not args.quiet:
            print(f"[MeshForge] {msg}", file=sys.stderr, flush=True)

    if args.cmd == "models":
        res = {
            "ok": True,
            "text_models": pipeline.fetch_models(cfg["text_base_url"], cfg.get("text_api_key", "")),
            "image_models": pipeline.fetch_models(cfg["image_base_url"], cfg.get("image_api_key", "")),
        }
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "model":
        cfg["style"] = args.style
        cfg["tex_size"] = args.size
        res = pipeline.run_full_generation(args.prompt, cfg, log_cb=log_err)
        job_dir = pipeline.OUTPUTS_DIR / res["job_id"]
        if args.out:
            _copy_to_out(job_dir, Path(args.out))
        out_payload = {
            "ok": True,
            "job_id": res["job_id"],
            "dir": str(Path(args.out).resolve() if args.out else job_dir),
            "glb": str((Path(args.out) if args.out else job_dir) / "model.glb"),
            "texture": str((Path(args.out) if args.out else job_dir) / "texture.png"),
            "preview_4side": str((Path(args.out) if args.out else job_dir) / "preview_4side.png"),
            "poly_count": res["meta"].get("poly_count", 0),
        }
        print(json.dumps(out_payload, indent=2, ensure_ascii=False))
        return 0

    maps = [] if args.maps.strip().lower() == "none" else [m.strip() for m in args.maps.split(",") if m.strip()]

    if args.cmd == "texture":
        res = pipeline.run_texture_generation(
            args.prompt,
            cfg,
            options={
                "style": args.style,
                "tex_size": args.size,
                "seamless": args.seamless,
                "enhance": args.enhance,
                "maps": maps,
            },
            log_cb=log_err,
        )
        job_dir = pipeline.OUTPUTS_DIR / res["job_id"]
        if args.out:
            _copy_to_out(job_dir, Path(args.out))
        target_dir = Path(args.out).resolve() if args.out else job_dir
        print(json.dumps({
            "ok": True,
            "job_id": res["job_id"],
            "dir": str(target_dir),
            "enhanced_prompt": res["meta"].get("enhanced_prompt", ""),
            "pbr_params": res["meta"].get("pbr_params", {}),
            "files": {k: str(target_dir / Path(v).name) for k, v in res["urls"].items()},
        }, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "pack":
        res = pipeline.run_texture_pack(
            args.prompt,
            cfg,
            options={
                "count": args.count,
                "style": args.style,
                "tex_size": args.size,
                "seamless": args.seamless,
                "maps": maps,
            },
            log_cb=log_err,
        )
        job_dir = pipeline.OUTPUTS_DIR / res["job_id"]
        if args.out:
            _copy_to_out(job_dir, Path(args.out))
        target_dir = Path(args.out).resolve() if args.out else job_dir
        print(json.dumps({
            "ok": True,
            "job_id": res["job_id"],
            "pack_title": res["meta"].get("pack_title", ""),
            "dir": str(target_dir),
            "items": res["items"],
        }, indent=2, ensure_ascii=False))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
