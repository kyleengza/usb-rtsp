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
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from admin import auth as auth_lib

# ─── paths & constants ──────────────────────────────────────────────────────

REPO_DIR = Path(os.environ.get("USB_RTSP_REPO", Path(__file__).resolve().parent.parent))
CONFIG_DIR = Path(os.environ.get("USB_RTSP_CONFIG_DIR", Path.home() / ".config" / "usb-rtsp"))
CAMERAS_YML = CONFIG_DIR / "cameras.yml"
SNAP_DIR = CONFIG_DIR / "snapshots"
DETECT_BIN = REPO_DIR / "bin" / "usb-rtsp-detect"
RENDER_BIN = REPO_DIR / "bin" / "usb-rtsp-render"
SNAP_BIN = REPO_DIR / "bin" / "snap"
PROFILES_YML = REPO_DIR / "etc" / "profiles.yml"
QUALITY_PRESETS_YML = REPO_DIR / "etc" / "quality-presets.yml"

MEDIAMTX_API = "http://127.0.0.1:9997"
ALLOWED_UNITS = {"usb-rtsp", "usb-rtsp-admin"}
CAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,15}$")
ALLOWED_FORMATS = {"MJPG", "YUYV", "H264"}
ALLOWED_ENCODES = {"h264", "mjpeg", "copy"}
ALLOWED_X264_PRESETS = {"ultrafast", "superfast", "veryfast", "faster", "fast"}
ALLOWED_BFRAMES = {0, 1, 2, 3}

app = FastAPI(title="usb-rtsp admin")
templates = Jinja2Templates(directory=str(REPO_DIR / "admin" / "templates"))
app.mount("/static", StaticFiles(directory=str(REPO_DIR / "admin" / "static")), name="static")


# ─── auth middleware ────────────────────────────────────────────────────────

PUBLIC_PATHS = {"/login", "/logout", "/healthz", "/api/auth/state"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not auth_lib.panel_enabled():
            request.state.user = None
            return await call_next(request)

        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            request.state.user = None
            return await call_next(request)

        cookie = request.cookies.get(auth_lib.COOKIE_NAME)
        user = auth_lib.verify_cookie(cookie)
        if user:
            request.state.user = user
            return await call_next(request)

        # not authenticated
        if path.startswith("/api/"):
            return JSONResponse({"detail": "auth required"}, status_code=401)
        nxt = request.url.path
        if request.url.query:
            nxt += "?" + request.url.query
        return RedirectResponse(f"/login?next={nxt}", status_code=303)


app.add_middleware(AuthMiddleware)


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


ALLOWED_SVC_ACTIONS = {"is-active", "restart", "stop", "start", "status", "show"}


def _systemctl(action: str, unit: str) -> tuple[int, str]:
    if unit not in ALLOWED_UNITS:
        raise HTTPException(400, f"unit not allowed: {unit}")
    if action not in ALLOWED_SVC_ACTIONS:
        raise HTTPException(400, f"action not allowed: {action}")
    cmd = ["systemctl", "--user", action, unit]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return p.returncode, (p.stdout + p.stderr).strip()


def _service_meta(unit: str) -> dict:
    """Return active state + ActiveEnterTimestamp + sub-state for a user unit."""
    p = subprocess.run(
        ["systemctl", "--user", "show", unit,
         "--property=ActiveState,SubState,ActiveEnterTimestamp,MainPID"],
        capture_output=True, text=True, timeout=5,
    )
    out = {}
    for line in p.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def _journal(unit: str, lines: int) -> str:
    if unit not in ALLOWED_UNITS:
        raise HTTPException(400, f"unit not allowed: {unit}")
    n = max(1, min(int(lines), 500))
    # Use --user-unit (queries system journal by user-unit tag) instead of
    # --user -u (queries the per-user journal). Debian doesn't enable
    # per-user journal storage by default, so --user -u returns
    # 'No journal files were found' even when the unit is logging fine.
    p = subprocess.run(
        ["journalctl", f"--user-unit={unit}.service", "-n", str(n), "--no-pager"],
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


def load_quality_presets() -> dict:
    if not QUALITY_PRESETS_YML.exists():
        return {}
    return yaml.safe_load(QUALITY_PRESETS_YML.read_text()) or {}


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
        formats = det.get("formats", [])
        # find sizes valid for the camera's currently configured format,
        # so the template can server-side render the resolution dropdown
        # with every option (rather than just the current one) and the
        # fps dropdown with everything supported at the current resolution.
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

    # also surface unconfigured detected cameras (so the user can add them)
    configured_ids = {c["by_id"] for c in cams_doc.get("cameras", [])}
    new_cams = [c for c in detected.get("cameras", []) if c["by_id"] not in configured_ids]

    qpresets = load_quality_presets()
    creds = auth_lib.stream_credentials()
    stream_user, stream_pass = (creds or (None, None))
    return templates.TemplateResponse("index.html", {
        "request": request,
        "cards": cards,
        "new_cams": new_cams,
        "profiles": list(profiles.keys()),
        "qualities": list(qpresets.keys()) or ["low", "medium", "high"],
        "x264_presets": ["ultrafast", "superfast", "veryfast", "faster", "fast"],
        "host": request.headers.get("host", "").split(":")[0] or "pitato.local",
        "stream_user": stream_user,
        "stream_pass": stream_pass,
        "stream_auth": bool(creds),
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


def _duration_h(created: str | None, now: datetime) -> str:
    try:
        if not created:
            return "—"
        t = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return fmt_duration((now - t).total_seconds())
    except (ValueError, TypeError):
        return "—"


@app.get("/api/sessions")
def api_sessions() -> JSONResponse:
    """Merged view of every active stream consumer across RTSP, WebRTC, HLS.

    The producer side (our ffmpeg pushing into mediamtx on 127.0.0.1) is
    intentionally filtered out — users only care about who's *watching*.
    """
    now = datetime.now(timezone.utc)
    items: list[dict] = []

    # RTSP — readers only (skip our own publishers on loopback)
    rtsp = _api_get("/v3/rtspsessions/list") or {"items": []}
    for s in rtsp.get("items", []):
        if s.get("state") == "publish":
            continue
        if (s.get("remoteAddr") or "").startswith("127."):
            continue
        items.append({
            "protocol": "RTSP",
            "path": s.get("path") or "—",
            "remoteAddr": s.get("remoteAddr") or "—",
            "state": s.get("state") or "—",
            "transport": s.get("transport") or "—",
            "bytesSent_h": fmt_bytes(s.get("bytesSent")),
            "bytesReceived_h": fmt_bytes(s.get("bytesReceived")),
            "duration_h": _duration_h(s.get("created"), now),
        })

    # WebRTC — every session is a reader
    webrtc = _api_get("/v3/webrtcsessions/list") or {"items": []}
    for s in webrtc.get("items", []):
        items.append({
            "protocol": "WebRTC",
            "path": s.get("path") or "—",
            "remoteAddr": s.get("remoteAddr") or "—",
            "state": s.get("state") or "—",
            "transport": "UDP/ICE",
            "bytesSent_h": fmt_bytes(s.get("bytesSent")),
            "bytesReceived_h": fmt_bytes(s.get("bytesReceived")),
            "duration_h": _duration_h(s.get("created"), now),
        })

    # HLS — mediamtx tracks per-path muxers (not per-viewer; HLS is HTTP polling).
    # We surface one row per active muxer with cumulative bytes sent.
    hls = _api_get("/v3/hlsmuxers/list") or {"items": []}
    for m in hls.get("items", []):
        items.append({
            "protocol": "HLS",
            "path": m.get("path") or "—",
            "remoteAddr": "(HTTP poll)",
            "state": "muxer active",
            "transport": "TCP/HTTP",
            "bytesSent_h": fmt_bytes(m.get("bytesSent")),
            "bytesReceived_h": "—",
            "duration_h": _duration_h(m.get("created"), now),
        })

    return JSONResponse({"itemCount": len(items), "items": items})


@app.get("/api/logs")
def api_logs(unit: str = "usb-rtsp", lines: int = 100) -> JSONResponse:
    return JSONResponse({"unit": unit, "lines": lines, "text": _journal(unit, lines)})


# ─── routes: camera config ──────────────────────────────────────────────────

def _validate_cam_name(name: str) -> str:
    if not CAM_NAME_RE.match(name):
        raise HTTPException(400, f"invalid camera name: {name!r}")
    return name


class CamSettings(BaseModel):
    by_id: str
    format: str
    width: int = Field(ge=16, le=7680)
    height: int = Field(ge=16, le=4320)
    fps: int = Field(ge=1, le=240)
    encode: str = "h264"
    profile: str = "balanced"
    quality: str = "medium"
    bitrate_kbps: int | None = Field(default=None, ge=100, le=20000)
    x264_preset: str | None = None
    gop_seconds: int | None = Field(default=None, ge=1, le=10)
    bframes: int | None = Field(default=None, ge=0, le=3)
    mjpeg_qv: int | None = Field(default=None, ge=1, le=31)


@app.post("/api/cam/{name}")
def api_save_cam(name: str, body: CamSettings) -> JSONResponse:
    _validate_cam_name(name)
    if body.format not in ALLOWED_FORMATS:
        raise HTTPException(400, f"format must be one of {sorted(ALLOWED_FORMATS)}")
    if body.encode not in ALLOWED_ENCODES:
        raise HTTPException(400, f"encode must be one of {sorted(ALLOWED_ENCODES)}")
    profiles = load_profiles()
    if body.profile not in profiles:
        raise HTTPException(400, f"profile must be one of {list(profiles.keys())}")
    qpresets = load_quality_presets()
    if qpresets and body.quality not in qpresets:
        raise HTTPException(400, f"quality must be one of {list(qpresets.keys())}")
    if body.x264_preset is not None and body.x264_preset not in ALLOWED_X264_PRESETS:
        raise HTTPException(400, f"x264_preset must be one of {sorted(ALLOWED_X264_PRESETS)}")
    if body.bframes is not None and body.bframes not in ALLOWED_BFRAMES:
        raise HTTPException(400, f"bframes must be one of {sorted(ALLOWED_BFRAMES)}")

    doc = load_cameras()
    cams = doc.setdefault("cameras", [])
    new_entry = {
        "name": name, "by_id": body.by_id, "format": body.format,
        "width": body.width, "height": body.height, "fps": body.fps,
        "encode": body.encode, "profile": body.profile,
        "quality": body.quality,
    }
    # Only persist advanced overrides if non-null — keeps cameras.yml clean.
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


@app.post("/api/svc/{unit}/{action}")
def api_svc(unit: str, action: str) -> JSONResponse:
    """Generic per-unit service control (start | stop | restart).

    For 'usb-rtsp-admin restart' we fork+exit so the response makes it back
    before the unit gets killed. For everything else we run synchronously
    so the user sees the real exit code.
    """
    if unit not in ALLOWED_UNITS:
        raise HTTPException(400, f"unit not allowed: {unit}")
    if action not in {"start", "stop", "restart"}:
        raise HTTPException(400, f"action not allowed: {action}")
    if unit == "usb-rtsp-admin" and action == "restart":
        subprocess.Popen(
            ["systemctl", "--user", "restart", unit],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return JSONResponse({"ok": True, "scheduled": True})
    code, msg = _systemctl(action, unit)
    return JSONResponse({"ok": code == 0, "code": code, "msg": msg})


@app.get("/api/svc/{unit}")
def api_svc_status(unit: str) -> JSONResponse:
    if unit not in ALLOWED_UNITS:
        raise HTTPException(400, f"unit not allowed: {unit}")
    meta = _service_meta(unit)
    since_str = meta.get("ActiveEnterTimestamp", "")
    uptime_s: float | None = None
    try:
        # systemd timestamp format: "Mon 2026-04-27 12:00:00 SAST"
        if since_str and since_str != "n/a":
            from email.utils import parsedate_to_datetime
            # parsedate is too flexible — fall back to subprocess `date` if needed.
            # Try a simpler approach: just compute from MainPID etime.
            pid = meta.get("MainPID", "0")
            if pid and pid != "0":
                p = subprocess.run(
                    ["ps", "-o", "etimes=", "-p", pid],
                    capture_output=True, text=True, timeout=2,
                )
                if p.stdout.strip():
                    uptime_s = float(p.stdout.strip())
    except (ValueError, ImportError, OSError):
        pass
    return JSONResponse({
        "unit": unit,
        "active": meta.get("ActiveState") == "active",
        "active_state": meta.get("ActiveState", "unknown"),
        "sub_state": meta.get("SubState", "unknown"),
        "active_enter": since_str,
        "uptime_s": uptime_s,
        "uptime_h": fmt_duration(uptime_s) if uptime_s is not None else "—",
        "main_pid": meta.get("MainPID", "0"),
    })


@app.get("/api/host")
def api_host() -> JSONResponse:
    """Lightweight host info for the dashboard. /proc + a couple of cheap shellouts."""
    info: dict = {}

    # hostname + Pi-ish model
    try:
        info["hostname"] = subprocess.run(
            ["hostname"], capture_output=True, text=True, timeout=2,
        ).stdout.strip()
    except OSError:
        info["hostname"] = "—"
    try:
        with open("/proc/device-tree/model") as f:
            info["model"] = f.read().rstrip("\x00").strip()
    except OSError:
        info["model"] = "—"

    # kernel + arch
    try:
        u = subprocess.run(["uname", "-srm"], capture_output=True, text=True, timeout=2)
        info["kernel"] = u.stdout.strip()
    except OSError:
        info["kernel"] = "—"

    # CPU count + load avg
    try:
        with open("/proc/cpuinfo") as f:
            info["cpu_count"] = sum(1 for line in f if line.startswith("processor"))
    except OSError:
        info["cpu_count"] = 0
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            info["loadavg"] = [float(parts[0]), float(parts[1]), float(parts[2])]
    except (OSError, ValueError, IndexError):
        info["loadavg"] = [0.0, 0.0, 0.0]

    # memory
    try:
        meminfo: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                meminfo[k.strip()] = int(rest.strip().split()[0]) * 1024  # kB → bytes
        total = meminfo.get("MemTotal", 0)
        avail = meminfo.get("MemAvailable", 0)
        used = max(0, total - avail)
        info["mem"] = {
            "total": total, "total_h": fmt_bytes(total),
            "used": used, "used_h": fmt_bytes(used),
            "avail": avail, "avail_h": fmt_bytes(avail),
            "used_pct": round(100 * used / total, 1) if total else 0,
        }
    except (OSError, ValueError):
        info["mem"] = {}

    # uptime
    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        info["uptime_s"] = up
        info["uptime_h"] = fmt_duration(up)
    except (OSError, ValueError):
        info["uptime_s"] = 0
        info["uptime_h"] = "—"

    # disk: / and the config dir's filesystem
    def _df(path: Path) -> dict:
        try:
            stat = os.statvfs(path)
            total = stat.f_blocks * stat.f_frsize
            free = stat.f_bavail * stat.f_frsize
            used = total - free
            return {
                "path": str(path),
                "total": total, "total_h": fmt_bytes(total),
                "used": used, "used_h": fmt_bytes(used),
                "free": free, "free_h": fmt_bytes(free),
                "used_pct": round(100 * used / total, 1) if total else 0,
            }
        except OSError:
            return {"path": str(path), "total_h": "—", "used_h": "—", "free_h": "—", "used_pct": 0}

    info["disk_root"] = _df(Path("/"))
    info["disk_config"] = _df(CONFIG_DIR if CONFIG_DIR.exists() else Path.home())

    # CPU temp (Linux thermal zone — Pi 5 reports here too)
    info["cpu_temp_c"] = None
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        try:
            tname = (zone / "type").read_text().strip()
            if "cpu" in tname.lower() or "soc" in tname.lower() or zone.name == "thermal_zone0":
                raw = int((zone / "temp").read_text().strip())
                info["cpu_temp_c"] = round(raw / 1000.0, 1)
                break
        except (OSError, ValueError):
            continue

    # mediamtx version (cheap shellout, ~10ms)
    info["mediamtx_version"] = "—"
    try:
        v = subprocess.run(
            ["/usr/local/bin/mediamtx", "--version"],
            capture_output=True, text=True, timeout=2,
        )
        info["mediamtx_version"] = (v.stdout + v.stderr).strip().splitlines()[0]
    except (OSError, IndexError):
        pass

    # local LAN IP (best-effort; whatever default route uses)
    info["lan_ip"] = "—"
    try:
        r = subprocess.run(
            ["ip", "-4", "-o", "route", "get", "1.1.1.1"],
            capture_output=True, text=True, timeout=2,
        )
        # output: "1.1.1.1 via 192.168.x.1 dev wlan0 src 192.168.x.x ..."
        for tok in r.stdout.split():
            if tok == "src":
                idx = r.stdout.split().index("src") + 1
                info["lan_ip"] = r.stdout.split()[idx]
                break
    except (OSError, ValueError, IndexError):
        pass

    return JSONResponse(info)


@app.get("/api/snapshots")
def api_snapshots() -> JSONResponse:
    """List snapshot files + total size (snapshot dir hygiene)."""
    if not SNAP_DIR.exists():
        return JSONResponse({"count": 0, "total_bytes": 0, "total_h": "0 B", "files": []})
    files = sorted(SNAP_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    items = [{
        "name": f.name,
        "size": f.stat().st_size,
        "size_h": fmt_bytes(f.stat().st_size),
        "mtime": int(f.stat().st_mtime),
    } for f in files[:100]]
    total = sum(f.stat().st_size for f in files)
    return JSONResponse({
        "count": len(files),
        "total_bytes": total,
        "total_h": fmt_bytes(total),
        "files": items,
    })


@app.post("/api/snapshots/cleanup")
def api_snapshots_cleanup(older_than_days: int = 7) -> JSONResponse:
    """Delete snapshots older than N days. Default: 7."""
    if not SNAP_DIR.exists():
        return JSONResponse({"deleted": 0, "freed_bytes": 0})
    cutoff = datetime.now().timestamp() - max(0, int(older_than_days)) * 86400
    deleted = 0
    freed = 0
    for f in SNAP_DIR.glob("*.jpg"):
        st = f.stat()
        if st.st_mtime < cutoff:
            freed += st.st_size
            f.unlink()
            deleted += 1
    return JSONResponse({"deleted": deleted, "freed_bytes": freed, "freed_h": fmt_bytes(freed)})


@app.get("/healthz")
def healthz() -> JSONResponse:
    api = _api_get("/v3/paths/list")
    return JSONResponse({"ok": api is not None})


# ─── auth routes ────────────────────────────────────────────────────────────

@app.get("/api/auth/state")
def api_auth_state(request: Request) -> JSONResponse:
    user = getattr(request.state, "user", None)
    return JSONResponse({
        "panel_enabled": auth_lib.panel_enabled(),
        "authenticated": bool(user),
        "user": user,
    })


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", error: str = "") -> HTMLResponse:
    if not auth_lib.panel_enabled():
        return RedirectResponse("/", status_code=303)
    cfg = auth_lib.load_config()
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": error,
        "next_url": next or "/",
        "cookie_days": cfg["panel"].get("cookie_max_age_days", 7),
    })


@app.post("/login")
async def login_submit(request: Request) -> Response:
    if not auth_lib.panel_enabled():
        return RedirectResponse("/", status_code=303)
    # Parse form manually — FastAPI's Form(...) annotation triggers a hard
    # python-multipart import check that fails on Debian (python3-multipart
    # is the wrong upstream lib). request.form() works fine for urlencoded.
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    next_url = form.get("next") or "/"
    cookie_days = auth_lib.load_config()["panel"].get("cookie_max_age_days", 7)
    if not auth_lib.pam_authenticate(username, password):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "invalid username or password",
            "next_url": next_url,
            "cookie_days": cookie_days,
        }, status_code=401)
    cookie_value, max_age = auth_lib.make_cookie(username)
    # only allow same-origin redirect targets to avoid open-redirect via ?next=
    target = next_url if next_url.startswith("/") else "/"
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(
        auth_lib.COOKIE_NAME, cookie_value,
        max_age=max_age, httponly=True, samesite="lax", path="/",
    )
    return resp


@app.post("/logout")
def logout() -> Response:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth_lib.COOKIE_NAME, path="/")
    return resp
