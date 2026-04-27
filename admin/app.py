"""usb-rtsp admin panel — single-file FastAPI app.

Bound to 0.0.0.0:8080 by the systemd unit. Talks to mediamtx control API on
127.0.0.1:9997 and to systemd via `systemctl --user`. No DB, no auth — LAN only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ─── paths & constants ──────────────────────────────────────────────────────

REPO_DIR = Path(os.environ.get("USB_RTSP_REPO", Path(__file__).resolve().parent.parent))
CONFIG_DIR = Path(os.environ.get("USB_RTSP_CONFIG_DIR", Path.home() / ".config" / "usb-rtsp"))
CAMERAS_YML = CONFIG_DIR / "cameras.yml"
SNAP_DIR = CONFIG_DIR / "snapshots"
DETECT_BIN = REPO_DIR / "bin" / "usb-rtsp-detect"
RENDER_BIN = REPO_DIR / "bin" / "usb-rtsp-render"
SNAP_BIN = REPO_DIR / "bin" / "snap"
PROFILES_YML = REPO_DIR / "etc" / "profiles.yml"

MEDIAMTX_API = "http://127.0.0.1:9997"
ALLOWED_UNITS = {"usb-rtsp", "usb-rtsp-admin"}
CAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,15}$")
ALLOWED_FORMATS = {"MJPG", "YUYV", "H264", "H264-encode"}

app = FastAPI(title="usb-rtsp admin")
templates = Jinja2Templates(directory=str(REPO_DIR / "admin" / "templates"))
app.mount("/static", StaticFiles(directory=str(REPO_DIR / "admin" / "static")), name="static")


# ─── helpers ────────────────────────────────────────────────────────────────

def _api_get(path: str, timeout: float = 2.0) -> Any:
    try:
        with urllib.request.urlopen(f"{MEDIAMTX_API}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None


def _api_post(path: str, body: dict | None = None, timeout: float = 3.0) -> tuple[int, Any]:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        f"{MEDIAMTX_API}{path}",
        data=data,
        method="POST",
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body_bytes = r.read()
            try:
                return r.status, json.loads(body_bytes.decode()) if body_bytes else None
            except json.JSONDecodeError:
                return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, None


def _systemctl(action: str, unit: str) -> tuple[int, str]:
    if unit not in ALLOWED_UNITS:
        raise HTTPException(400, f"unit not allowed: {unit}")
    if action not in {"is-active", "restart", "status", "show"}:
        raise HTTPException(400, f"action not allowed: {action}")
    cmd = ["systemctl", "--user", action, unit]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return p.returncode, (p.stdout + p.stderr).strip()


def _journal(unit: str, lines: int) -> str:
    if unit not in ALLOWED_UNITS:
        raise HTTPException(400, f"unit not allowed: {unit}")
    n = max(1, min(int(lines), 500))
    p = subprocess.run(
        ["journalctl", "--user", "-u", unit, "-n", str(n), "--no-pager"],
        capture_output=True, text=True, timeout=10,
    )
    return p.stdout + (p.stderr if p.returncode else "")


def load_cameras() -> dict:
    if not CAMERAS_YML.exists():
        return {"cameras": []}
    return yaml.safe_load(CAMERAS_YML.read_text()) or {"cameras": []}


def save_cameras(doc: dict) -> None:
    CAMERAS_YML.parent.mkdir(parents=True, exist_ok=True)
    backup = CAMERAS_YML.with_suffix(f".yml.bak.{int(datetime.now().timestamp())}")
    if CAMERAS_YML.exists():
        shutil.copy2(CAMERAS_YML, backup)
    CAMERAS_YML.write_text(yaml.safe_dump(doc, sort_keys=False))


def load_profiles() -> dict:
    return yaml.safe_load(PROFILES_YML.read_text()) or {}


def detect_cameras() -> dict:
    p = subprocess.run([str(DETECT_BIN)], capture_output=True, text=True, timeout=15)
    if p.returncode != 0:
        return {"cameras": []}
    return json.loads(p.stdout)


def render_config() -> tuple[bool, str]:
    p = subprocess.run([str(RENDER_BIN)], capture_output=True, text=True, timeout=15)
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def reload_mediamtx() -> str:
    """Try hot-reload first; fall back to systemctl restart. Returns 'hot' or 'restart' or 'failed'."""
    # mediamtx v1.18 reads its config file once at startup. There is no in-place
    # full-config reload endpoint; per-path PATCH exists but doesn't restart the
    # ffmpeg `runOnInit` after format/resolution changes. So a service restart is
    # the only reliable way to apply width/height/fps/format changes.
    code, _ = _systemctl("restart", "usb-rtsp")
    return "restart" if code == 0 else "failed"


def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60: return f"{s}s"
    if s < 3600: return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


# ─── routes: dashboard ──────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    cams_doc = load_cameras()
    profiles = load_profiles()
    detected = detect_cameras()

    # match each cameras.yml entry to its detected capabilities (by by_id)
    detected_by_id = {c["by_id"]: c for c in detected.get("cameras", [])}
    cards = []
    for cam in cams_doc.get("cameras", []):
        det = detected_by_id.get(cam["by_id"], {})
        cards.append({
            "config": cam,
            "card_name": det.get("card", "(camera unplugged?)"),
            "available": det.get("formats", []),
            "is_present": cam["by_id"] in detected_by_id,
        })

    # also surface unconfigured detected cameras (so the user can add them)
    configured_ids = {c["by_id"] for c in cams_doc.get("cameras", [])}
    new_cams = [c for c in detected.get("cameras", []) if c["by_id"] not in configured_ids]

    return templates.TemplateResponse("index.html", {
        "request": request,
        "cards": cards,
        "new_cams": new_cams,
        "profiles": list(profiles.keys()),
        "host": request.headers.get("host", "").split(":")[0] or "pitato.local",
    })


# ─── routes: status / live state ────────────────────────────────────────────

@app.get("/api/status")
def api_status() -> JSONResponse:
    paths = _api_get("/v3/paths/list") or {"items": []}
    sessions = _api_get("/v3/rtspsessions/list") or {"items": []}

    mtx_code, _ = _systemctl("is-active", "usb-rtsp")
    admin_code, _ = _systemctl("is-active", "usb-rtsp-admin")

    items = paths.get("items", [])
    ready = sum(1 for p in items if p.get("ready"))
    readers = sum(len(p.get("readers", [])) for p in items)
    bytes_received = sum(p.get("bytesReceived", 0) or 0 for p in items)

    return JSONResponse({
        "services": {
            "mediamtx": "active" if mtx_code == 0 else "inactive",
            "admin": "active" if admin_code == 0 else "inactive",
        },
        "paths": {
            "total": len(items),
            "ready": ready,
            "readers": readers,
            "bytes_received": bytes_received,
            "bytes_received_h": fmt_bytes(bytes_received),
        },
        "sessions": {"total": len(sessions.get("items", []))},
    })


@app.get("/api/paths")
def api_paths() -> JSONResponse:
    data = _api_get("/v3/paths/list")
    if data is None:
        raise HTTPException(503, "mediamtx api unreachable")
    # enrich each item with human-formatted byte/duration fields for the table
    for p in data.get("items", []):
        p["bytesReceived_h"] = fmt_bytes(p.get("bytesReceived"))
        p["readers_count"] = len(p.get("readers", []))
        # mediamtx returns sourceReady etc; we surface a simple "ready"
        if "ready" not in p and "sourceReady" in p:
            p["ready"] = bool(p.get("sourceReady"))
    return JSONResponse(data)


@app.get("/api/sessions")
def api_sessions() -> JSONResponse:
    data = _api_get("/v3/rtspsessions/list")
    if data is None:
        raise HTTPException(503, "mediamtx api unreachable")
    now = datetime.now(timezone.utc)
    for s in data.get("items", []):
        s["bytesSent_h"] = fmt_bytes(s.get("bytesSent"))
        s["bytesReceived_h"] = fmt_bytes(s.get("bytesReceived"))
        # duration from `created` ISO field if present
        created = s.get("created")
        try:
            if created:
                t = datetime.fromisoformat(created.replace("Z", "+00:00"))
                s["duration_s"] = (now - t).total_seconds()
                s["duration_h"] = fmt_duration(s["duration_s"])
        except (ValueError, TypeError):
            s["duration_h"] = "—"
    return JSONResponse(data)


@app.get("/api/logs")
def api_logs(unit: str = "usb-rtsp", lines: int = 100) -> JSONResponse:
    return JSONResponse({"unit": unit, "lines": lines, "text": _journal(unit, lines)})


# ─── routes: camera config ──────────────────────────────────────────────────

def _validate_cam_name(name: str) -> str:
    if not CAM_NAME_RE.match(name):
        raise HTTPException(400, f"invalid camera name: {name!r}")
    return name


@app.post("/api/cam/{name}")
def api_save_cam(
    name: str,
    by_id: str = Form(...),
    format: str = Form(...),
    width: int = Form(...),
    height: int = Form(...),
    fps: int = Form(...),
    profile: str = Form("balanced"),
) -> JSONResponse:
    _validate_cam_name(name)
    if format not in ALLOWED_FORMATS:
        raise HTTPException(400, f"format must be one of {sorted(ALLOWED_FORMATS)}")
    if not (16 <= width <= 7680 and 16 <= height <= 4320 and 1 <= fps <= 240):
        raise HTTPException(400, "width/height/fps out of sane range")
    profiles = load_profiles()
    if profile not in profiles:
        raise HTTPException(400, f"profile must be one of {list(profiles.keys())}")

    doc = load_cameras()
    cams = doc.setdefault("cameras", [])
    new_entry = {
        "name": name, "by_id": by_id, "format": format,
        "width": width, "height": height, "fps": fps, "profile": profile,
    }
    for i, c in enumerate(cams):
        if c["name"] == name:
            cams[i] = new_entry
            break
    else:
        cams.append(new_entry)
    save_cameras(doc)

    ok, msg = render_config()
    if not ok:
        raise HTTPException(500, f"render failed: {msg}")
    method = reload_mediamtx()
    return JSONResponse({"saved": True, "reload": method})


@app.delete("/api/cam/{name}")
def api_delete_cam(name: str) -> JSONResponse:
    _validate_cam_name(name)
    doc = load_cameras()
    before = len(doc.get("cameras", []))
    doc["cameras"] = [c for c in doc.get("cameras", []) if c["name"] != name]
    if len(doc["cameras"]) == before:
        raise HTTPException(404, "no such camera")
    save_cameras(doc)
    ok, msg = render_config()
    if not ok:
        raise HTTPException(500, f"render failed: {msg}")
    return JSONResponse({"deleted": True, "reload": reload_mediamtx()})


@app.post("/api/cam/{name}/restart")
def api_path_restart(name: str) -> JSONResponse:
    _validate_cam_name(name)
    code, body = _api_post(f"/v3/paths/kick/{name}")
    return JSONResponse({"kicked": code in (200, 204), "code": code})


@app.get("/api/cam/{name}/snap")
def api_snap(name: str):
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


# ─── routes: service recovery ───────────────────────────────────────────────

@app.post("/api/rescan")
def api_rescan() -> JSONResponse:
    """Re-detect cameras; add any new ones to cameras.yml with sensible defaults."""
    detected = detect_cameras()
    doc = load_cameras()
    existing_ids = {c["by_id"] for c in doc.get("cameras", [])}
    next_idx = len(doc.get("cameras", []))
    added = []
    for c in detected.get("cameras", []):
        if c["by_id"] in existing_ids:
            continue
        d = c.get("default")
        if not d:
            continue
        doc.setdefault("cameras", []).append({
            "name": f"cam{next_idx}",
            "by_id": c["by_id"],
            "format": d["format"],
            "width": d["width"],
            "height": d["height"],
            "fps": d["fps"],
            "profile": "balanced",
        })
        added.append(f"cam{next_idx}")
        next_idx += 1
    if added:
        save_cameras(doc)
        ok, msg = render_config()
        if not ok:
            raise HTTPException(500, f"render failed: {msg}")
        method = reload_mediamtx()
    else:
        method = "noop"
    return JSONResponse({"added": added, "reload": method})


@app.post("/api/restart")
def api_restart() -> JSONResponse:
    code, msg = _systemctl("restart", "usb-rtsp")
    return JSONResponse({"ok": code == 0, "code": code, "msg": msg})


@app.post("/api/restart-admin")
def api_restart_admin() -> JSONResponse:
    # this kills self; client will reconnect
    subprocess.Popen(
        ["systemctl", "--user", "restart", "usb-rtsp-admin"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return JSONResponse({"ok": True, "msg": "restart scheduled"})


@app.get("/healthz")
def healthz() -> JSONResponse:
    api = _api_get("/v3/paths/list")
    return JSONResponse({"ok": api is not None})
