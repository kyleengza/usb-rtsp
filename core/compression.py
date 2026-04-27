"""Compression knobs shared by any plugin that re-encodes via ffmpeg/libx264.

Lives in core because relay-plus-overlay or inference-annotation plugins
may eventually need the same bitrate ladder + quality presets. The USB
plugin uses these today.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .helpers import REPO_DIR

QUALITY_PRESETS_FILE = REPO_DIR / "etc" / "quality-presets.yml"

ALLOWED_X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast")
ALLOWED_BFRAMES = (0, 1, 2, 3)


def h264_bitrate_kbps(width: int, height: int, factor: float = 1.0) -> int:
    """Baseline kbps target for libx264 ultrafast at a given resolution,
    scaled by a quality preset's `bitrate_factor`. Tuned to leave headroom
    for WiFi-tethered phones with 'ultrafast'."""
    pixels = width * height
    if pixels >= 1920 * 1080: base = 2500
    elif pixels >= 1280 * 720: base = 1500
    elif pixels >= 640 * 480:  base = 800
    else:                      base = 500
    return max(100, int(round(base * float(factor))))


def load_quality_presets(path: Path | None = None) -> dict:
    p = path or QUALITY_PRESETS_FILE
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}
