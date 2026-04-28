"""USB plugin REST endpoints (mounted at /api/usb)."""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from core.compression import ALLOWED_BFRAMES, ALLOWED_X264_PRESETS, load_quality_presets
from core.helpers import (
    PLUGINS_DIR,
    REPO_DIR,
    SNAP_DIR,
    api_post,
    is_valid_name,
    systemctl,
)

from .render import cameras_yml_path, default_encode

ALLOWED_FORMATS = {"MJPG", "YUYV", "H264"}
ALLOWED_ENCODES = {"h264", "mjpeg", "copy"}

USB_DETECT_BIN = PLUGINS_DIR / "usb" / "detect.py"
SNAP_BIN = REPO_DIR / "bin" / "snap"


def _config_dir(ctx) -> Path:
    return ctx.plugin.config_dir


def _profiles_doc():
    p = REPO_DIR / "etc" / "profiles.yml"
    return yaml.safe_load(p.read_text()) or {} if p.exists() else {}


def _detect_cameras() -> dict:
    p = subprocess.run(
        ["python3", str(USB_DETECT_BIN)],
        capture_output=True, text=True, timeout=15,
    )
    if p.returncode != 0:
        return {"cameras": []}
    return json.loads(p.stdout)


def _render_config() -> tuple[bool, str]:
    p = subprocess.run(
        ["python3", "-m", "core.renderer"],
        cwd=str(REPO_DIR),
        capture_output=True, text=True, timeout=15,
    )
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def _reload_mediamtx() -> str:
    code, _ = systemctl("restart", "usb-rtsp")
    return "restart" if code == 0 else "failed"


def _save_cameras(config_dir: Path, doc: dict) -> None:
    p = cameras_yml_path(config_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        backup = p.with_suffix(f".yml.bak.{int(datetime.now().timestamp())}")
        shutil.copy2(p, backup)
    p.write_text(yaml.safe_dump(doc, sort_keys=False))


class CamSettings(BaseModel):
    by_id: str
    format: str
    width: int = Field(ge=16, le=7680)
    height: int = Field(ge=16, le=4320)
    fps: int = Field(ge=1, le=240)
    encode: str = "h264"
    profile: str = "balanced"
    quality: str = "medium"
    on_demand: bool = True
    bitrate_kbps: int | None = Field(default=None, ge=100, le=20000)
    x264_preset: str | None = None
    gop_seconds: int | None = Field(default=None, ge=1, le=10)
    bframes: int | None = Field(default=None, ge=0, le=3)
    mjpeg_qv: int | None = Field(default=None, ge=1, le=31)


def _validate_cam_name(name: str) -> str:
    if not is_valid_name(name):
        raise HTTPException(400, f"invalid camera name: {name!r}")
    return name


def make_router(ctx) -> APIRouter:
    """Build the FastAPI APIRouter with closures over the plugin context."""
    cfg_dir = _config_dir(ctx)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    router = APIRouter(prefix="/api/usb", tags=["usb"])

    @router.post("/cam/{name}")
    def save_cam(name: str, body: CamSettings) -> JSONResponse:
        _validate_cam_name(name)
        if body.format not in ALLOWED_FORMATS:
            raise HTTPException(400, f"format must be one of {sorted(ALLOWED_FORMATS)}")
        if body.encode not in ALLOWED_ENCODES:
            raise HTTPException(400, f"encode must be one of {sorted(ALLOWED_ENCODES)}")
        profiles = _profiles_doc()
        if body.profile not in profiles:
            raise HTTPException(400, f"profile must be one of {list(profiles.keys())}")
        qpresets = load_quality_presets()
        if qpresets and body.quality not in qpresets:
            raise HTTPException(400, f"quality must be one of {list(qpresets.keys())}")
        if body.x264_preset is not None and body.x264_preset not in ALLOWED_X264_PRESETS:
            raise HTTPException(400, f"x264_preset must be one of {sorted(ALLOWED_X264_PRESETS)}")
        if body.bframes is not None and body.bframes not in ALLOWED_BFRAMES:
            raise HTTPException(400, f"bframes must be one of {sorted(ALLOWED_BFRAMES)}")

        from .render import load_cameras
        doc = load_cameras(cfg_dir)
        cams = doc.setdefault("cameras", [])
        new_entry = {
            "name": name, "by_id": body.by_id, "format": body.format,
            "width": body.width, "height": body.height, "fps": body.fps,
            "encode": body.encode, "profile": body.profile, "quality": body.quality,
            "on_demand": bool(body.on_demand),
        }
        for k in ("bitrate_kbps", "x264_preset", "gop_seconds", "bframes", "mjpeg_qv"):
            v = getattr(body, k)
            if v is not None:
                new_entry[k] = v
        for i, c in enumerate(cams):
            if c["name"] == name:
                cams[i] = new_entry
                break
        else:
            cams.append(new_entry)
        _save_cameras(cfg_dir, doc)
        ok, msg = _render_config()
        if not ok:
            raise HTTPException(500, f"render failed: {msg}")
        return JSONResponse({"saved": True, "reload": _reload_mediamtx()})

    @router.delete("/cam/{name}")
    def delete_cam(name: str) -> JSONResponse:
        _validate_cam_name(name)
        from .render import load_cameras
        doc = load_cameras(cfg_dir)
        before = len(doc.get("cameras", []))
        doc["cameras"] = [c for c in doc.get("cameras", []) if c["name"] != name]
        if len(doc["cameras"]) == before:
            raise HTTPException(404, "no such camera")
        _save_cameras(cfg_dir, doc)
        ok, msg = _render_config()
        if not ok:
            raise HTTPException(500, f"render failed: {msg}")
        return JSONResponse({"deleted": True, "reload": _reload_mediamtx()})

    @router.post("/cam/{name}/restart")
    def kick_cam(name: str) -> JSONResponse:
        _validate_cam_name(name)
        code, _ = api_post(f"/v3/paths/kick/{name}")
        return JSONResponse({"kicked": code in (200, 204), "code": code})

    @router.get("/cam/{name}/snap")
    def snap_cam(name: str):
        _validate_cam_name(name)
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        out = SNAP_DIR / f"{name}-panel-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jpg"
        p = subprocess.run(
            [str(SNAP_BIN), name, str(out)],
            capture_output=True, text=True, timeout=15,
        )
        if p.returncode != 0 or not out.exists():
            raise HTTPException(502, f"snap failed: {p.stderr.strip() or p.stdout.strip()}")
        return FileResponse(out, media_type="image/jpeg")

    @router.post("/cam/{name}/enable")
    def enable_cam(name: str) -> JSONResponse:
        return _set_cam_enabled(name, True)

    @router.post("/cam/{name}/disable")
    def disable_cam(name: str) -> JSONResponse:
        return _set_cam_enabled(name, False)

    def _set_cam_enabled(name: str, enable: bool) -> JSONResponse:
        _validate_cam_name(name)
        from .render import load_cameras
        doc = load_cameras(cfg_dir)
        found = False
        for c in doc.get("cameras", []):
            if c["name"] == name:
                c["enabled"] = enable
                found = True
                break
        if not found:
            raise HTTPException(404, "no such camera")
        _save_cameras(cfg_dir, doc)
        ok, msg = _render_config()
        if not ok:
            raise HTTPException(500, f"render failed: {msg}")
        return JSONResponse({
            "name": name,
            "enabled": enable,
            "reload": _reload_mediamtx(),
        })

    @router.post("/rescan")
    def rescan() -> JSONResponse:
        detected = _detect_cameras()
        from .render import load_cameras
        doc = load_cameras(cfg_dir)
        existing_ids = {c["by_id"] for c in doc.get("cameras", [])}
        next_idx = len(doc.get("cameras", []))
        added = []
        for c in detected.get("cameras", []):
            if c["by_id"] in existing_ids:
                continue
            d = c.get("default")
            if not d:
                continue
            encode = "copy" if d["format"] == "H264" else "h264"
            doc.setdefault("cameras", []).append({
                "name": f"cam{next_idx}",
                "by_id": c["by_id"],
                "format": d["format"],
                "width": d["width"],
                "height": d["height"],
                "fps": d["fps"],
                "encode": encode,
                "profile": "balanced",
                "quality": "medium",
            })
            added.append(f"cam{next_idx}")
            next_idx += 1
        if added:
            _save_cameras(cfg_dir, doc)
            ok, msg = _render_config()
            if not ok:
                raise HTTPException(500, f"render failed: {msg}")
            method = _reload_mediamtx()
        else:
            method = "noop"
        return JSONResponse({"added": added, "reload": method})

    return router
