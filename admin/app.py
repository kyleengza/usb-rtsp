"""usb-rtsp admin panel — single-file FastAPI app.

Bound to 0.0.0.0:8080 by the systemd unit. Talks to mediamtx control
API on 127.0.0.1:9997 and to systemd via `systemctl --user`.

Plugin-aware: at startup, walks plugins/<name>/manifest.yml, imports
every enabled plugin, calls its register(app, ctx) hook, mounts its
templates and static dir. The dashboard loops over those plugins and
renders each one's section.html partial.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PrefixLoader, select_autoescape
from starlette.middleware.base import BaseHTTPMiddleware

from core import auth as auth_lib
from core import loader as plugin_loader
from core.helpers import (
    ALLOWED_UNITS,
    CONFIG_DIR,
    REPO_DIR,
    SNAP_DIR,
    api_get,
    duration_h,
    fmt_bytes,
    fmt_duration,
    journal,
    service_meta,
    systemctl,
)


# ─── Jinja env: admin templates + per-plugin templates under <name>/ ───────

def _make_templates() -> Jinja2Templates:
    plugin_loaders: dict[str, FileSystemLoader] = {}
    for p in plugin_loader.discover_plugins():
        td = p.dir / "templates"
        if td.is_dir():
            plugin_loaders[p.name] = FileSystemLoader(str(td))
    env = Environment(
        loader=ChoiceLoader([
            FileSystemLoader(str(REPO_DIR / "admin" / "templates")),
            PrefixLoader(plugin_loaders),
        ]),
        autoescape=select_autoescape(["html", "xml"]),
    )
    return Jinja2Templates(env=env)


app = FastAPI(title="usb-rtsp admin")
templates = _make_templates()
# /static-core/* serves admin/static/. Each plugin's register() mounts its
# own /static/<name>/ — keeping these on different prefixes avoids the
# Starlette Mount-prefix-matching trap where the more-general /static
# would catch /static/<plugin>/<file> requests and 404 inside admin/static.
app.mount("/static-core", StaticFiles(directory=str(REPO_DIR / "admin" / "static")), name="static-core")


# ─── auth middleware ────────────────────────────────────────────────────────

PUBLIC_PATHS = {"/login", "/logout", "/healthz", "/api/auth/state"}
PUBLIC_PREFIXES = ("/static/", "/static-core/")


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not auth_lib.panel_enabled():
            request.state.user = None
            return await call_next(request)

        path = request.url.path
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            request.state.user = None
            return await call_next(request)

        cookie = request.cookies.get(auth_lib.COOKIE_NAME)
        user = auth_lib.verify_cookie(cookie)
        if user:
            request.state.user = user
            return await call_next(request)

        if path.startswith("/api/"):
            return JSONResponse({"detail": "auth required"}, status_code=401)
        nxt = request.url.path
        if request.url.query:
            nxt += "?" + request.url.query
        return RedirectResponse(f"/login?next={nxt}", status_code=303)


app.add_middleware(AuthMiddleware)


# ─── plugin loader: import + register every enabled plugin ─────────────────

ACTIVE_PLUGINS = plugin_loader.register_all(app, templates)


def _plugin_module(name: str):
    for p in ACTIVE_PLUGINS:
        if p.name == name and p.module is not None:
            return p.module
    return None


# ─── dashboard ──────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    creds = auth_lib.stream_credentials()
    stream_user, stream_pass = (creds or (None, None))
    host = request.headers.get("host", "").split(":")[0] or "pitato.local"

    # base context shared with every plugin section
    base_ctx = {
        "request": request,
        "plugins": ACTIVE_PLUGINS,
        "host": host,
        "stream_user": stream_user,
        "stream_pass": stream_pass,
        "stream_auth": bool(creds),
    }

    # let each plugin contribute its own slice of the template ctx
    merged = dict(base_ctx)
    for p in ACTIVE_PLUGINS:
        mod = _plugin_module(p.name)
        if not mod:
            continue
        sec_ctx_fn = getattr(mod, "section_context", None)
        if not callable(sec_ctx_fn):
            continue
        try:
            piece = sec_ctx_fn(plugin_loader._make_ctx(p, templates), request) or {}
        except Exception as e:
            print(f"[dashboard] {p.name}.section_context raised: {e}")
            piece = {}
        merged.update(piece)

    return templates.TemplateResponse("index.html", merged)


# ─── status / paths / sessions ─────────────────────────────────────────────

@app.get("/api/status")
def api_status() -> JSONResponse:
    paths = api_get("/v3/paths/list") or {"items": []}
    sessions = api_get("/v3/rtspsessions/list") or {"items": []}
    mtx_code, _ = systemctl("is-active", "usb-rtsp")
    admin_code, _ = systemctl("is-active", "usb-rtsp-admin")

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
    data = api_get("/v3/paths/list")
    if data is None:
        raise HTTPException(503, "mediamtx api unreachable")
    for p in data.get("items", []):
        p["bytesReceived_h"] = fmt_bytes(p.get("bytesReceived"))
        p["readers_count"] = len(p.get("readers", []))
        if "ready" not in p and "sourceReady" in p:
            p["ready"] = bool(p.get("sourceReady"))
    return JSONResponse(data)


@app.get("/api/sessions")
def api_sessions() -> JSONResponse:
    """Merged view of every active stream consumer across RTSP, WebRTC, HLS.
    The producer side (loopback ffmpeg) is filtered out."""
    now = datetime.now(timezone.utc)
    items: list[dict] = []

    rtsp = api_get("/v3/rtspsessions/list") or {"items": []}
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
            "duration_h": duration_h(s.get("created"), now),
        })

    webrtc = api_get("/v3/webrtcsessions/list") or {"items": []}
    for s in webrtc.get("items", []):
        items.append({
            "protocol": "WebRTC",
            "path": s.get("path") or "—",
            "remoteAddr": s.get("remoteAddr") or "—",
            "state": s.get("state") or "—",
            "transport": "UDP/ICE",
            "bytesSent_h": fmt_bytes(s.get("bytesSent")),
            "bytesReceived_h": fmt_bytes(s.get("bytesReceived")),
            "duration_h": duration_h(s.get("created"), now),
        })

    hls = api_get("/v3/hlsmuxers/list") or {"items": []}
    for m in hls.get("items", []):
        items.append({
            "protocol": "HLS",
            "path": m.get("path") or "—",
            "remoteAddr": "(HTTP poll)",
            "state": "muxer active",
            "transport": "TCP/HTTP",
            "bytesSent_h": fmt_bytes(m.get("bytesSent")),
            "bytesReceived_h": "—",
            "duration_h": duration_h(m.get("created"), now),
        })

    return JSONResponse({"itemCount": len(items), "items": items})


@app.get("/api/logs")
def api_logs(unit: str = "usb-rtsp", lines: int = 100) -> JSONResponse:
    if unit not in ALLOWED_UNITS:
        raise HTTPException(400, f"unit not allowed: {unit}")
    return JSONResponse({"unit": unit, "lines": lines, "text": journal(unit, lines)})


# ─── service recovery ───────────────────────────────────────────────────────

@app.post("/api/restart")
def api_restart() -> JSONResponse:
    code, msg = systemctl("restart", "usb-rtsp")
    return JSONResponse({"ok": code == 0, "code": code, "msg": msg})


@app.post("/api/restart-admin")
def api_restart_admin() -> JSONResponse:
    subprocess.Popen(
        ["systemctl", "--user", "restart", "usb-rtsp-admin"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return JSONResponse({"ok": True, "msg": "restart scheduled"})


@app.post("/api/svc/{unit}/{action}")
def api_svc(unit: str, action: str) -> JSONResponse:
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
    code, msg = systemctl(action, unit)
    return JSONResponse({"ok": code == 0, "code": code, "msg": msg})


@app.get("/api/svc/{unit}")
def api_svc_status(unit: str) -> JSONResponse:
    if unit not in ALLOWED_UNITS:
        raise HTTPException(400, f"unit not allowed: {unit}")
    meta = service_meta(unit)
    uptime_s: float | None = None
    pid = meta.get("MainPID", "0")
    if pid and pid != "0":
        try:
            p = subprocess.run(
                ["ps", "-o", "etimes=", "-p", pid],
                capture_output=True, text=True, timeout=2,
            )
            if p.stdout.strip():
                uptime_s = float(p.stdout.strip())
        except (ValueError, OSError):
            pass
    return JSONResponse({
        "unit": unit,
        "active": meta.get("ActiveState") == "active",
        "active_state": meta.get("ActiveState", "unknown"),
        "sub_state": meta.get("SubState", "unknown"),
        "active_enter": meta.get("ActiveEnterTimestamp", ""),
        "uptime_s": uptime_s,
        "uptime_h": fmt_duration(uptime_s) if uptime_s is not None else "—",
        "main_pid": pid,
    })


# ─── plugin admin ───────────────────────────────────────────────────────────

@app.get("/api/plugins")
def api_plugins() -> JSONResponse:
    """Return every discovered plugin + its enabled state."""
    enabled = plugin_loader.read_enabled_set()
    items = []
    for p in plugin_loader.discover_plugins():
        items.append({
            "name": p.name,
            "description": p.description,
            "version": p.version,
            "default_enabled": p.default_enabled,
            "enabled": p.name in enabled,
        })
    return JSONResponse({"items": items})


def _set_plugin_enabled(name: str, enable: bool) -> dict:
    """Mutate plugins-enabled.yml + re-render mediamtx + restart usb-rtsp.
    The admin process needs its own restart for routes/templates to load —
    we schedule that out-of-band so this response makes it back to the
    client first."""
    known = {p.name for p in plugin_loader.discover_plugins()}
    if name not in known:
        raise HTTPException(404, f"unknown plugin: {name}")
    enabled = plugin_loader.read_enabled_set()
    if enable:
        enabled.add(name)
    else:
        enabled.discard(name)
    plugin_loader.write_enabled_set(enabled)

    # Re-render mediamtx config + restart so paths reflect the new set.
    render_p = subprocess.run(
        ["python3", "-m", "core.renderer"],
        cwd=str(REPO_DIR),
        capture_output=True, text=True, timeout=15,
    )
    if render_p.returncode != 0:
        raise HTTPException(500, f"render failed: {(render_p.stdout + render_p.stderr).strip()}")
    code, _ = systemctl("restart", "usb-rtsp")

    # The admin process needs to restart to (un)load the plugin's APIRouter
    # + templates + static. Schedule it via Popen so this response returns
    # before systemd kills us.
    subprocess.Popen(
        ["systemctl", "--user", "restart", "usb-rtsp-admin"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return {
        "name": name,
        "enabled": enable,
        "reload": "restart" if code == 0 else "failed",
        "admin_restart": True,
    }


@app.post("/api/plugins/{name}/enable")
def api_plugin_enable(name: str) -> JSONResponse:
    return JSONResponse(_set_plugin_enabled(name, True))


@app.post("/api/plugins/{name}/disable")
def api_plugin_disable(name: str) -> JSONResponse:
    return JSONResponse(_set_plugin_enabled(name, False))


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    enabled = plugin_loader.read_enabled_set()
    plugins = []
    for p in plugin_loader.discover_plugins():
        meta = {
            "name": p.name,
            "description": p.description,
            "version": p.version,
            "default_enabled": p.default_enabled,
            "enabled": p.name in enabled,
            "inputs": [],
        }
        if p.name in enabled:
            mod = _plugin_module(p.name)
            list_inputs_fn = getattr(mod, "list_inputs", None) if mod else None
            if callable(list_inputs_fn):
                try:
                    meta["inputs"] = list(list_inputs_fn(plugin_loader._make_ctx(p, templates)))
                except Exception as e:
                    print(f"[settings] {p.name}.list_inputs raised: {e}")
        plugins.append(meta)
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "plugins_meta": plugins,
    })


# ─── host info ──────────────────────────────────────────────────────────────

@app.get("/api/host")
def api_host() -> JSONResponse:
    info: dict = {}
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
    try:
        u = subprocess.run(["uname", "-srm"], capture_output=True, text=True, timeout=2)
        info["kernel"] = u.stdout.strip()
    except OSError:
        info["kernel"] = "—"

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

    try:
        meminfo: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                meminfo[k.strip()] = int(rest.strip().split()[0]) * 1024
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

    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        info["uptime_s"] = up
        info["uptime_h"] = fmt_duration(up)
    except (OSError, ValueError):
        info["uptime_s"] = 0
        info["uptime_h"] = "—"

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

    info["mediamtx_version"] = "—"
    try:
        v = subprocess.run(
            ["/usr/local/bin/mediamtx", "--version"],
            capture_output=True, text=True, timeout=2,
        )
        info["mediamtx_version"] = (v.stdout + v.stderr).strip().splitlines()[0]
    except (OSError, IndexError):
        pass

    info["lan_ip"] = "—"
    try:
        r = subprocess.run(
            ["ip", "-4", "-o", "route", "get", "1.1.1.1"],
            capture_output=True, text=True, timeout=2,
        )
        toks = r.stdout.split()
        if "src" in toks:
            info["lan_ip"] = toks[toks.index("src") + 1]
    except (OSError, ValueError, IndexError):
        pass

    return JSONResponse(info)


# ─── snapshots ──────────────────────────────────────────────────────────────

@app.get("/api/snapshots")
def api_snapshots() -> JSONResponse:
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


# ─── healthz ────────────────────────────────────────────────────────────────

@app.get("/healthz")
def healthz() -> JSONResponse:
    api = api_get("/v3/paths/list")
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
    import urllib.parse
    raw = (await request.body()).decode("utf-8", "replace")
    data = urllib.parse.parse_qs(raw, keep_blank_values=True)
    username = (data.get("username", [""])[0] or "").strip()
    password = data.get("password", [""])[0] or ""
    next_url = data.get("next", ["/"])[0] or "/"
    cookie_days = auth_lib.load_config()["panel"].get("cookie_max_age_days", 7)
    if not auth_lib.pam_authenticate(username, password):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "invalid username or password",
            "next_url": next_url,
            "cookie_days": cookie_days,
        }, status_code=401)
    cookie_value, max_age = auth_lib.make_cookie(username)
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
