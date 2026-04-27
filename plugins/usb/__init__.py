"""USB plugin entry point."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from core.helpers import PLUGINS_DIR, REPO_DIR

from .api import make_router
from .render import load_cameras, render_paths  # re-export for renderer

__all__ = ["register", "render_paths"]


PLUGIN_DIR = Path(__file__).resolve().parent
DETECT_BIN = PLUGIN_DIR / "detect.py"


def _detect_cameras() -> dict:
    p = subprocess.run(
        ["python3", str(DETECT_BIN)],
        capture_output=True, text=True, timeout=15,
    )
    if p.returncode != 0:
        return {"cameras": []}
    return json.loads(p.stdout)


def _build_card_data(ctx) -> dict:
    """Pre-compute the per-card data the section template needs.

    Mirrors the dashboard() context computation that previously lived in
    admin/app.py before the refactor — finds sizes for the current format,
    fps for the current resolution, etc.
    """
    cams_doc = load_cameras(ctx.plugin.config_dir)
    detected = _detect_cameras()
    detected_by_id = {c["by_id"]: c for c in detected.get("cameras", [])}
    cards = []
    for cam in cams_doc.get("cameras", []):
        det = detected_by_id.get(cam["by_id"], {})
        formats = det.get("formats", [])
        cur_fmt_rec = next((f for f in formats if f["format"] == cam["format"]), None)
        cur_sizes = cur_fmt_rec["sizes"] if cur_fmt_rec else []
        cur_size_rec = next(
            (s for s in cur_sizes
             if s["width"] == cam["width"] and s["height"] == cam["height"]),
            None,
        )
        cur_fps_options = cur_size_rec["fps"] if cur_size_rec else [cam["fps"]]
        cards.append({
            "config": cam,
            "card_name": det.get("card", "(camera unplugged?)"),
            "available": formats,
            "current_sizes": cur_sizes,
            "current_fps_options": sorted(set(cur_fps_options + [cam["fps"]])),
            "is_present": cam["by_id"] in detected_by_id,
        })
    configured_ids = {c["by_id"] for c in cams_doc.get("cameras", [])}
    new_cams = [c for c in detected.get("cameras", []) if c["by_id"] not in configured_ids]

    profiles_yml = REPO_DIR / "etc" / "profiles.yml"
    profiles = (yaml.safe_load(profiles_yml.read_text()) or {}) if profiles_yml.exists() else {}
    qpresets_yml = REPO_DIR / "etc" / "quality-presets.yml"
    qpresets = (yaml.safe_load(qpresets_yml.read_text()) or {}) if qpresets_yml.exists() else {}

    return {
        "cards": cards,
        "new_cams": new_cams,
        "profiles": list(profiles.keys()),
        "qualities": list(qpresets.keys()) or ["low", "medium", "high"],
        "x264_presets": list(("ultrafast", "superfast", "veryfast", "faster", "fast")),
    }


def section_context(ctx, request) -> dict:
    """Plugin-supplied context merged into the dashboard template render.

    Called by admin/app.py during dashboard() so the section.html template
    has everything it needs to draw the camera cards.
    """
    return _build_card_data(ctx)


def register(app, ctx) -> None:
    """Mount this plugin's API routes + static files."""
    app.include_router(make_router(ctx))
    static_dir = PLUGIN_DIR / "static"
    if static_dir.is_dir():
        app.mount("/static/usb", StaticFiles(directory=str(static_dir)), name="static-usb")
