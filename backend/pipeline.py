"""
pipeline.py - Facade re-exporting modular MeshForge pipeline modules.
"""
from .config import (
    ASSETS_DIR,
    OUTPUTS_DIR,
    ROOT,
    SETTINGS_PATH,
    STYLE_PRESETS,
    find_steam_blender,
    load_settings,
    save_settings,
    scan_blender_installations,
)
from .llm_client import fetch_models, fetch_models_detailed, is_image_gen_model
from .blender_bridge import run_bpy_stage, build_uv_guide_and_previews
from .model_pipeline import run_full_generation, run_stage2_texture
from .texture_pipeline import run_texture_generation, run_texture_pack, list_jobs

__all__ = [
    "ASSETS_DIR",
    "OUTPUTS_DIR",
    "ROOT",
    "SETTINGS_PATH",
    "STYLE_PRESETS",
    "find_steam_blender",
    "load_settings",
    "save_settings",
    "scan_blender_installations",
    "fetch_models",
    "fetch_models_detailed",
    "is_image_gen_model",
    "run_bpy_stage",
    "build_uv_guide_and_previews",
    "run_full_generation",
    "run_stage2_texture",
    "run_texture_generation",
    "run_texture_pack",
    "list_jobs",
]
