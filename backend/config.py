"""
config.py - Settings persistence, style presets, and targeted Blender discovery.
"""
import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path

BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
APP_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else BUNDLE_ROOT
ROOT = BUNDLE_ROOT
OUTPUTS_DIR = APP_ROOT / "outputs"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
ASSETS_DIR = BUNDLE_ROOT / "assets"
SETTINGS_PATH = APP_ROOT / "settings.json"

STYLE_PRESETS = {
    "ps1": {
        "label": "PS1 (Low-Poly)",
        "pixel_crisp": True,
        "quantize_colors": 128,
        "mesh_rules": (
            "Create an authentic PlayStation 1 (PS1) low-poly retro 3D asset (60 to 450 triangles). "
            "Use crisp blocky silhouettes, 6-10 sided cylinders, flat/faceted shading, and chunky readable proportions."
        ),
        "tex_prompt": (
            "Authentic PlayStation 1 (PS1) low-res retro pixel-art game texture, weathered matte surface, "
            "subtle baked ambient occlusion and grit, sharp pixelated details, no glossy PBR."
        ),
    },
    "stylized": {
        "label": "Стилизованный",
        "pixel_crisp": False,
        "quantize_colors": 0,
        "mesh_rules": (
            "Create a clean stylized mid-poly game asset (300 to 1400 triangles) with bold silhouettes, "
            "clean chamfers, and strong readable proportions."
        ),
        "tex_prompt": (
            "Stylized hand-painted game texture, clean brush strokes, rich surface color variation, "
            "subtle stylized shading, crisp material separation, flat albedo-oriented lighting."
        ),
    },
    "realistic": {
        "label": "Реализм (PBR)",
        "pixel_crisp": False,
        "quantize_colors": 0,
        "mesh_rules": (
            "Create a detailed game-ready realistic mid-poly 3D asset (600 to 3000 triangles) with crisp bevels, "
            "recessed seams, extruded hardware/trim, and accurate real-world proportions."
        ),
        "tex_prompt": (
            "Photorealistic game-ready PBR albedo texture, authentic surface micro-detail, natural wear, "
            "scratches, grime, flat uniform lighting without harsh directional cast shadows."
        ),
    },
    "voxel": {
        "label": "Воксель / Пиксель",
        "pixel_crisp": True,
        "quantize_colors": 96,
        "mesh_rules": (
            "Create a chunky voxel/pixel-art 3D asset built from crisp orthogonal cuboids and stepped blocks "
            "(80 to 600 triangles), flat shaded, zero smooth curves."
        ),
        "tex_prompt": (
            "Crisp voxel pixel-art game texture, clean blocky pixel clusters, sharp edges, cohesive limited palette, "
            "flat lighting."
        ),
    },
    "horror": {
        "label": "Хоррор / Индастриал",
        "pixel_crisp": True,
        "quantize_colors": 96,
        "mesh_rules": (
            "Create a gritty PSX industrial horror 3D asset (120 to 700 triangles) like Mouthwashing / Silent Hill, "
            "with sturdy industrial geometry, panels, pipes, bolts, and flat/gritty silhouettes."
        ),
        "tex_prompt": (
            "Gritty PSX industrial horror game texture, heavy rust streaks, stained concrete/metal, "
            "high-contrast weathered grime, subtle dithering feel, dark moody palette."
        ),
    },
}


def scan_blender_installations() -> dict:
    """Fast, targeted search for Blender (PATH, Registry, Steam VDF, standard dirs) and Python bpy."""
    found: list[dict] = []
    seen_paths: set[str] = set()

    def add_candidate(p_str: str, source: str) -> None:
        if not p_str:
            return
        try:
            p = Path(p_str).resolve()
            key = str(p).lower()
            if p.is_file() and key not in seen_paths:
                seen_paths.add(key)
                found.append({"path": str(p), "source": source})
        except Exception:
            pass

    add_candidate(shutil.which("blender") or "", "PATH")

    if os.name == "nt":
        try:
            import winreg
            for hive, subkey in (
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\blender.exe"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\blender.exe"),
            ):
                try:
                    with winreg.OpenKey(hive, subkey) as k:
                        val, _ = winreg.QueryValueEx(k, "")
                        add_candidate(str(val), "Реестр Windows")
                except OSError:
                    pass
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for base_sub in (
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                    r"SOFTWARE\WOW6432Node\Microsoft\Microsoft\Windows\CurrentVersion\Uninstall",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
                ):
                    try:
                        with winreg.OpenKey(hive, base_sub) as uk:
                            for i in range(winreg.QueryInfoKey(uk)[0]):
                                name = winreg.EnumKey(uk, i)
                                if "blender" in name.lower():
                                    try:
                                        with winreg.OpenKey(uk, name) as sk:
                                            loc, _ = winreg.QueryValueEx(sk, "InstallLocation")
                                            add_candidate(str(Path(loc) / "blender.exe"), "Установленные программы")
                                    except OSError:
                                        pass
                    except OSError:
                        pass
        except Exception:
            pass

    for vdf in (
        Path("C:/Program Files (x86)/Steam/steamapps/libraryfolders.vdf"),
        Path("C:/Program Files/Steam/steamapps/libraryfolders.vdf"),
    ):
        if vdf.is_file():
            try:
                txt = vdf.read_text(encoding="utf-8", errors="ignore")
                for m in re.finditer(r'"path"\s+"([^"]+)"', txt):
                    lib = Path(m.group(1).replace("\\\\", "/"))
                    add_candidate(str(lib / "steamapps/common/Blender/blender.exe"), "Steam")
            except Exception:
                pass

    for sr in (
        Path("C:/Program Files (x86)/Steam"),
        Path("C:/Program Files/Steam"),
        Path("D:/Steam"),
        Path("D:/SteamLibrary"),
        Path("E:/SteamLibrary"),
        Path("F:/SteamLibrary"),
    ):
        add_candidate(str(sr / "steamapps/common/Blender/blender.exe"), "Steam")

    for base_dir in (
        Path("C:/Program Files/Blender Foundation"),
        Path("D:/Program Files/Blender Foundation"),
    ):
        if base_dir.is_dir():
            for exe in sorted(base_dir.glob("*/blender.exe"), reverse=True):
                add_candidate(str(exe), "Blender Foundation")

    winget_dir = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    if winget_dir.is_dir():
        for exe in winget_dir.glob("*Blender*/**/blender.exe"):
            add_candidate(str(exe), "WinGet")

    has_bpy = importlib.util.find_spec("bpy") is not None
    return {
        "found_blenders": found,
        "has_bpy": has_bpy,
        "recommended": found[0]["path"] if found else "",
    }


def find_steam_blender() -> str:
    return scan_blender_installations()["recommended"]


def normalize_proxy_url(raw: str, ptype: str = "") -> str:
    s = (raw or "").strip()
    if not s or ptype == "none":
        return ""
    if "://" in s:
        return s
    scheme = "socks5h" if ptype == "socks5" else ("http" if ptype in ("http", "") else ptype)
    return f"{scheme}://{s}"


def load_settings() -> dict:
    defaults = {
        "provider_mode": "gemini_cookies",
        "proxy_url": "",
        "gemini_accounts": [],
        "text_base_url": "https://api.openai.com/v1",
        "text_api_key": "",
        "text_model": "gemini-авто",
        "image_base_url": "https://api.openai.com/v1",
        "image_api_key": "",
        "image_model": "gemini-image",
        "blender_path": "",
        "use_bpy": False,
        "setup_done": False,
        "style": "ps1",
        "tex_size": 512,
        "max_retries": 3,
    }
    if SETTINGS_PATH.exists():
        try:
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            defaults.update(saved)
        except Exception:
            pass
    return defaults


def save_settings(cfg: dict) -> dict:
    cur = load_settings()
    cur.update(cfg)
    SETTINGS_PATH.write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")
    return cur
