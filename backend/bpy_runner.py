"""
bpy_runner.py - Isolated subprocess runner for Blender (`bpy` or Steam `blender.exe --background`).
Uses zero third-party packages inside Blender's Python.
"""
import argparse
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import uv_guide_lib


def run_stage1(script_path: Path, out_dir: Path, tex_size: int) -> int:
    uv_guide_lib.setup_clean_scene()
    code = script_path.read_text(encoding="utf-8")
    ns = {"__name__": "__main__", "__file__": str(script_path), "uv_guide_lib": uv_guide_lib}
    exec(compile(code, str(script_path), "exec"), ns)

    obj = uv_guide_lib.ensure_joined_mesh_and_uv()
    manifest = uv_guide_lib.export_stage1_uv_guide(obj, out_dir, tex_size=tex_size)
    print(json.dumps({
        "ok": True,
        "stage": 1,
        "islands": len(manifest["islands"]),
        "poly_count": manifest.get("poly_count", 0),
        "vert_count": manifest.get("vert_count", 0),
    }, ensure_ascii=False))
    return 0


def run_stage2(out_dir: Path, tex_path: Path, ps1_mode: bool, normal_path: str = "", roughness_path: str = "") -> int:
    res = uv_guide_lib.apply_texture_and_export(
        out_dir,
        tex_path,
        ps1_mode=ps1_mode,
        normal_path=normal_path or None,
        roughness_path=roughness_path or None,
    )
    print(json.dumps({"ok": True, "stage": 2, **res}, ensure_ascii=False))
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("stage1")
    p1.add_argument("--script", required=True)
    p1.add_argument("--out-dir", required=True)
    p1.add_argument("--tex-size", type=int, default=512)

    p2 = sub.add_parser("stage2")
    p2.add_argument("--out-dir", required=True)
    p2.add_argument("--tex", required=True)
    p2.add_argument("--ps1", type=int, default=1)
    p2.add_argument("--normal", default="")
    p2.add_argument("--roughness", default="")

    args = p.parse_args(argv)
    try:
        if args.cmd == "stage1":
            return run_stage1(Path(args.script), Path(args.out_dir), args.tex_size)
        elif args.cmd == "stage2":
            return run_stage2(Path(args.out_dir), Path(args.tex), bool(args.ps1), args.normal, args.roughness)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
