"""usb-rtsp admin panel — single-file FastAPI app.

Bound to 0.0.0.0:8080 by the systemd unit. Talks to mediamtx control
API on 127.0.0.1:9997 and to systemd via `systemctl --user`.

Plugin-aware: at startup, walks plugins/<name>/manifest.yml, imports
every enabled plugin, calls its register(app, ctx) hook, mounts its
templates and static dir. The dashboard loops over those plugins and
renders each one's section.html partial.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PrefixLoader, select_autoescape
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from core import auth as auth_lib
from core import loader as plugin_loader
from core import public_ip
from core import ufw as ufw_lib
from core.helpers import (
    ALLOWED_UNITS,
    REPO_DIR,
    SNAP_DIR,
    api_get,
    api_post,
    duration_h,
    fmt_bytes,
    fmt_duration,
    is_valid_name,
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


def _render_mediamtx_yml() -> tuple[bool, str]:
    """Run `python3 -m core.renderer` to rewrite ~/.config/usb-rtsp/mediamtx.yml.
    Returns (ok, message). Used by config-changing endpoints + the
    public-IP refresher."""
    p = subprocess.run(
        ["python3", "-m", "core.renderer"],
        cwd=str(REPO_DIR),
        capture_output=True, text=True, timeout=15,
    )
    if p.returncode != 0:
        return False, (p.stdout + p.stderr).strip()
    return True, ""


# ─── public-IP tracking for WebRTC NAT1To1 ─────────────────────────────────
# Detected at admin startup + refreshed on a timer. When the IP changes we
# re-render mediamtx.yml and restart mediamtx (skipped if there's an active
# WebRTC viewer, to avoid kicking them — the next tick will catch up).

WEBRTC_STATE: dict = {
    "public_ip": None,
    "source": None,                # "dns" | "http" | None
    "last_detected_at": None,      # ISO timestamp
    "last_error": None,
}


def _webrtc_active_session_count() -> int:
    """How many active WebRTC sessions are connected (via mediamtx API)."""
    try:
        d = api_get("/v3/webrtcsessions/list") or {}
    except Exception:
        return 0
    return len(d.get("items") or [])


def _refresh_public_ip(force_restart: bool = False) -> dict:
    """Detect public IP; on change, re-render and (when safe) restart mediamtx."""
    cfg = (auth_lib.load_config().get("webrtc") or {})
    prev = public_ip.read_cached()
    ip, source = public_ip.detect(cfg)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if ip:
        WEBRTC_STATE["public_ip"] = ip
        WEBRTC_STATE["source"] = source
        WEBRTC_STATE["last_detected_at"] = now_iso
        WEBRTC_STATE["last_error"] = None
    else:
        WEBRTC_STATE["last_error"] = "all detection methods failed"
        return {"changed": False, "ip": prev, "source": WEBRTC_STATE["source"], "restarted": False}

    changed = (ip != prev)
    restarted = False
    if changed or force_restart:
        ok, err = _render_mediamtx_yml()
        if not ok:
            WEBRTC_STATE["last_error"] = f"render failed: {err}"
            return {"changed": changed, "ip": ip, "source": source, "restarted": False, "error": err}
        # Skip the restart if anyone's actively watching via WebRTC.
        # The new IP is already in the rendered YAML; next non-active
        # window or any other config-change restart will pick it up.
        if not force_restart and _webrtc_active_session_count() > 0:
            return {"changed": changed, "ip": ip, "source": source, "restarted": False, "deferred": "active_viewers"}
        code, _ = systemctl("restart", "usb-rtsp")
        restarted = (code == 0)
    return {"changed": changed, "ip": ip, "source": source, "restarted": restarted}


async def _public_ip_refresher() -> None:
    """Background task: re-detect every refresh_minutes minutes."""
    while True:
        try:
            cfg = (auth_lib.load_config().get("webrtc") or {})
            mins = max(1, int(cfg.get("refresh_minutes") or 30))
        except Exception:
            mins = 30
        await asyncio.sleep(mins * 60)
        try:
            await asyncio.to_thread(_refresh_public_ip)
        except Exception as e:
            WEBRTC_STATE["last_error"] = f"refresh: {e}"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # On startup: prime the cache + state. Don't block startup on render
    # failure — admin should still come up so the user can fix things.
    try:
        await asyncio.to_thread(_refresh_public_ip)
    except Exception as e:
        WEBRTC_STATE["last_error"] = f"startup: {e}"
    task = asyncio.create_task(_public_ip_refresher(), name="public-ip-refresher")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task


app = FastAPI(title="usb-rtsp admin", lifespan=lifespan)
templates = _make_templates()
# /static-core/* serves admin/static/. Each plugin's register() mounts its
# own /static/<name>/ — keeping these on different prefixes avoids the
# Starlette Mount-prefix-matching trap where the more-general /static
# would catch /static/<plugin>/<file> requests and 404 inside admin/static.
app.mount("/static-core", StaticFiles(directory=str(REPO_DIR / "admin" / "static")), name="static-core")


# ─── auth middleware ────────────────────────────────────────────────────────

PUBLIC_PATHS = {"/login", "/logout", "/healthz", "/api/auth/state"}
# /preview/* requires panel auth (the cookie check below); we add it to a
# separate list because the proxy is reached via the iframe's own load
# (same-origin to the panel), so it benefits from the same auth gate.
PUBLIC_PREFIXES = ("/static/", "/static-core/")


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Always attempt to derive `request.state.user` from the cookie when
        # panel auth is on — public endpoints like /api/auth/state need it
        # to report "who am I" even though they don't gate on it.
        if auth_lib.panel_enabled():
            cookie = request.cookies.get(auth_lib.COOKIE_NAME)
            request.state.user = auth_lib.verify_cookie(cookie)
        else:
            request.state.user = None

        if not auth_lib.panel_enabled():
            return await call_next(request)

        path = request.url.path
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)

        if request.state.user:
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

def _primary_lan_ip() -> str:
    """Best-effort: the IP this Pi would use to reach the internet."""
    try:
        r = subprocess.run(
            ["ip", "-4", "-o", "route", "get", "1.1.1.1"],
            capture_output=True, text=True, timeout=2,
        )
        toks = r.stdout.split()
        if "src" in toks:
            return toks[toks.index("src") + 1]
    except (OSError, ValueError, IndexError):
        pass
    return ""


def _looks_like_hostname(s: str) -> bool:
    """Cheap: anything with a letter is a hostname; pure dotted-quad is an IP."""
    if not s:
        return False
    if any(c.isalpha() for c in s):
        return True
    return False


def _dashboard_host_options() -> dict:
    """Hosts the URL toggle on the dashboard can switch between."""
    cfg = (auth_lib.load_config().get("webrtc") or {})
    configured = (cfg.get("public_host") or "").strip()
    return {
        "lan":    _primary_lan_ip() or "",
        "public": (WEBRTC_STATE.get("public_ip") or public_ip.read_cached() or ""),
        "dns":    configured if _looks_like_hostname(configured) else "",
    }


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
        "host_options": _dashboard_host_options(),
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
            "bundled": p.bundled,
            "dir": str(p.dir),
        })
    return JSONResponse({"items": items})


class PluginInstall(BaseModel):
    source: str = Field(min_length=1)


@app.post("/api/plugins/install")
async def api_plugin_install(body: PluginInstall) -> JSONResponse:
    """Install a plugin from a git URL or a local path."""
    spec = body.source.strip()
    try:
        if spec.startswith(("http://", "https://", "git@", "ssh://")):
            p = plugin_loader.install_plugin_from_git(spec)
        else:
            p = plugin_loader.install_plugin_from_path(Path(spec))
    except (FileExistsError, FileNotFoundError, ValueError) as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    # Schedule admin restart out-of-band so the new plugin actually loads.
    subprocess.Popen(
        ["systemctl", "--user", "restart", "usb-rtsp-admin"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return JSONResponse({
        "installed": p.name if p else None,
        "dir": str(p.dir) if p else None,
        "admin_restart": True,
    })


@app.post("/api/plugins/uninstall/{name}")
def api_plugin_uninstall(name: str) -> JSONResponse:
    try:
        plugin_loader.uninstall_plugin(name)
    except PermissionError as e:
        raise HTTPException(422, str(e))
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(404, str(e))

    # Re-render mediamtx (in case the removed plugin contributed paths)
    # and restart usb-rtsp + admin so the plugin is gone from runtime.
    subprocess.run(
        ["python3", "-m", "core.renderer"],
        cwd=str(REPO_DIR), capture_output=True, text=True, timeout=15,
    )
    systemctl("restart", "usb-rtsp")
    subprocess.Popen(
        ["systemctl", "--user", "restart", "usb-rtsp-admin"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return JSONResponse({"uninstalled": name, "admin_restart": True})


@app.post("/api/plugins/refresh")
def api_plugin_refresh() -> JSONResponse:
    """Re-walk plugin paths + schedule admin restart so any newly-dropped
    plugins or removed ones reflect in the running process."""
    plugins = plugin_loader.refresh()
    subprocess.Popen(
        ["systemctl", "--user", "restart", "usb-rtsp-admin"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return JSONResponse({
        "discovered": [p.name for p in plugins],
        "admin_restart": True,
    })


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
            "bundled": p.bundled,
            "dir": str(p.dir),
            "settings_template": p.settings_template,
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

# Linux ioctl request number for "set i2c slave address"; see <linux/i2c-dev.h>.
_I2C_SLAVE = 0x0703


def _hwmon_read(name: str, file: str) -> int | None:
    """Read /sys/class/hwmon/hwmonN/<file> from the entry whose `name`
    attribute equals `name` (e.g. 'pwmfan'). Returns None if no such
    hwmon, or the file doesn't exist / isn't an int."""
    for h in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        try:
            if (h / "name").read_text().strip() == name:
                return int((h / file).read_text().strip())
        except (OSError, ValueError):
            continue
    return None


def _fan_info() -> dict | None:
    """Pi 5 active cooler / pwmfan: tach RPM + duty cycle. None when no
    pwmfan hwmon (passively-cooled board)."""
    rpm = _hwmon_read("pwmfan", "fan1_input")
    if rpm is None:
        return None
    pwm_raw = _hwmon_read("pwmfan", "pwm1") or 0
    return {"rpm": rpm, "pwm_pct": round(pwm_raw * 100 / 255)}


# pi-bringup (https://github.com/kyleengza/pi-bringup) is the source of
# truth for HAT/UPS bring-up on this hardware. When its helpers exist
# we defer to them — better curves, configurable thresholds, single
# authoritative state — and fall back to inline logic on bare installs.
_PI_BRINGUP_BATTERY = Path("/home/potato/pi-bringup/scripts/pi-bringup-battery.sh")
_PI_BRINGUP_THROTTLE = Path("/home/potato/pi-bringup/scripts/pifetch-throttle.sh")
_PI_BRINGUP_HAILO_CACHE = Path("/var/cache/pi-bringup/hailo.txt")
_UPS_WATCHDOG_CONF = Path("/etc/default/ups-watchdog")


def _pibringup_battery(arg: str) -> str | None:
    """Shell out to pi-bringup-battery.sh; return stripped stdout or None."""
    if not _PI_BRINGUP_BATTERY.is_file():
        return None
    try:
        p = subprocess.run(
            [str(_PI_BRINGUP_BATTERY), arg],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    out = (p.stdout or "").strip()
    return out or None


def _ups_watchdog_conf() -> dict:
    """Parse /etc/default/ups-watchdog into a dict (KEY=value, sh-style)."""
    out: dict = {}
    if not _UPS_WATCHDOG_CONF.is_file():
        return out
    try:
        for line in _UPS_WATCHDOG_CONF.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


_UPS_MODEL_LABELS = {
    "waveshare-e": "Waveshare UPS HAT (E)",
    "max1704x":    "MAX1704x fuel gauge",
    "bq25895":     "BQ25895 charger",
    "ip5312":      "IP5312 (PiSugar)",
}


_HAILO_FW_CACHE: dict | None = None


def _hailo_fw_identify() -> dict:
    """One-shot read of HailoRT FW info. Prefers the pi-bringup cache
    (`/var/cache/pi-bringup/hailo.txt`, format "Hailo-8 / FW 4.23.0"),
    falls back to a direct `hailortcli fw-control identify` for bare
    installs. Cached for the process lifetime — FW info is static."""
    global _HAILO_FW_CACHE
    if _HAILO_FW_CACHE is not None:
        return _HAILO_FW_CACHE
    out: dict = {}

    # Preferred path: pi-bringup's cached identify line.
    if _PI_BRINGUP_HAILO_CACHE.is_file():
        try:
            line = _PI_BRINGUP_HAILO_CACHE.read_text().strip()
            # "Hailo-8 / FW 4.23.0"
            if "/" in line:
                model, _, fw = line.partition("/")
                out["Board Name"] = model.strip()
                out["Firmware Version"] = fw.strip().removeprefix("FW").strip()
        except OSError:
            pass

    # Fallback: live hailortcli call.
    if not out:
        try:
            p = subprocess.run(
                ["hailortcli", "fw-control", "identify"],
                capture_output=True, text=True, timeout=3,
            )
            for line in p.stdout.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    # FW returns fixed-width strings padded with NULs (Board
                    # Name is 32 bytes); strip those + surrounding whitespace.
                    out[k.strip()] = v.strip().strip("\x00").strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            pass

    _HAILO_FW_CACHE = out
    return out


def _pcie_link_for(bdf: str) -> str | None:
    """Return e.g. 'Gen3 x1 (8.0 GT/s)' for a PCI device's current link."""
    d = Path(f"/sys/bus/pci/devices/{bdf}")
    if not d.exists():
        return None
    try:
        spd = (d / "current_link_speed").read_text().strip()
        wid = (d / "current_link_width").read_text().strip()
    except OSError:
        return None
    gen_map = {"2.5": "Gen1", "5.0": "Gen2", "8.0": "Gen3", "16.0": "Gen4", "32.0": "Gen5"}
    gen = next((v for k, v in gen_map.items() if spd.startswith(k)), "?")
    return f"{gen} x{wid} ({spd})"


def _hailo_info() -> dict | None:
    """Hailo HAT presence + driver/service liveness + FW info + PCIe link.
    None when no Hailo PCIe device is bound (board absent / driver not
    loaded). Live power isn't surfaced because `hailortcli measure-power`
    returns UNSUPPORTED_DEVICE on this Hailo-8 revision."""
    if not Path("/sys/class/hailo_chardev/hailo0").exists():
        return None
    fw = _hailo_fw_identify()
    info = {
        "model": fw.get("Board Name", "Hailo-8").strip(),
        "fw_version": fw.get("Firmware Version", "—"),
        "arch": fw.get("Device Architecture", "HAILO8"),
        "dev": Path("/dev/hailo0").exists(),
        "driver": "hailo",
        "pcie_link": _pcie_link_for("0001:01:00.0"),
    }
    try:
        p = subprocess.run(
            ["systemctl", "is-active", "hailort.service"],
            capture_output=True, text=True, timeout=2,
        )
        info["hailort_active"] = p.stdout.strip() == "active"
    except (subprocess.SubprocessError, FileNotFoundError):
        info["hailort_active"] = None
    return info


def _i2c_read_word_be(bus: int, addr: int, reg: int) -> int | None:
    """Read a 16-bit big-endian register from an SMBus device. Pure
    stdlib (no smbus2 dep). Returns None if the bus/device/register
    doesn't respond."""
    try:
        fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
    except OSError:
        return None
    try:
        fcntl.ioctl(fd, _I2C_SLAVE, addr)
        os.write(fd, bytes([reg]))
        data = os.read(fd, 2)
        if len(data) != 2:
            return None
        return (data[0] << 8) | data[1]
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _ups_info() -> dict | None:
    """UPS HAT state. Prefers pi-bringup's pi-bringup-battery.sh +
    /etc/default/ups-watchdog (authoritative thresholds, full Li-ion
    discharge curve, MODEL label). Falls back to a direct INA219 read
    on bare installs. Returns None when no battery rail is detectable."""
    conf = _ups_watchdog_conf()

    # Preferred: pi-bringup-battery.sh (matches the watchdog daemon's view)
    v_str = _pibringup_battery("--voltage")
    if v_str:
        try:
            volts = float(v_str.rstrip("V"))
        except ValueError:
            volts = None
        pct_str = _pibringup_battery("--percent")
        try:
            pct = int(pct_str) if pct_str else None
        except ValueError:
            pct = None
        state_str = _pibringup_battery("--state") or "unknown"
        # pi-bringup returns "ac" / "bat" / "unknown"; normalise to our
        # 3-value vocabulary so the JS can show a low-battery tint.
        low_v_str = conf.get("LOW_VOLTAGE")
        try:
            low_v = float(low_v_str) if low_v_str else 3.4
        except ValueError:
            low_v = 3.4
        if state_str == "ac":
            source = "ac"
        elif state_str == "bat" and volts is not None and volts < low_v:
            source = "battery_low"
        elif state_str == "bat":
            source = "battery"
        else:
            source = "unknown"
        return {
            "battery_v": volts,
            "battery_pct": pct,
            "source": source,
            "model": _UPS_MODEL_LABELS.get(conf.get("MODEL", ""), conf.get("MODEL", "UPS")),
            "low_v": low_v,
            "watchdog_active": _ups_watchdog_active(),
        }

    # Fallback: direct INA219 read (no pi-bringup helper available).
    raw = _i2c_read_word_be(1, 0x43, 0x02)
    if raw is None or raw == 0:
        # If the watchdog config exists, we know a HAT is supposed to
        # be there — surface a degraded card rather than hiding it.
        # Hiding is exactly wrong when the HAT's gone offline mid-run
        # (Waveshare UPS HAT (E) MCUs occasionally wedge after deep
        # discharge cycles and stop responding on i2c until cold start).
        if conf:
            return {
                "battery_v": None,
                "battery_pct": None,
                "source": "unreachable",
                "model": _UPS_MODEL_LABELS.get(conf.get("MODEL", ""), conf.get("MODEL", "UPS")),
                "low_v": float(conf.get("LOW_VOLTAGE", 3.4)) if conf.get("LOW_VOLTAGE") else None,
                "watchdog_active": _ups_watchdog_active(),
            }
        return None
    volts = (raw >> 3) * 0.004
    pct = max(0, min(100, round((volts - 3.00) / (4.20 - 3.00) * 100)))
    if volts >= 4.15:
        source = "ac"
    elif volts >= 3.50:
        source = "battery"
    else:
        source = "battery_low"
    return {
        "battery_v": round(volts, 2),
        "battery_pct": pct,
        "source": source,
        "model": "UPS (INA219 0x43)",
    }


def _ups_watchdog_active() -> bool | None:
    """is-active for ups-watchdog.service (system unit, not user)."""
    try:
        p = subprocess.run(
            ["systemctl", "is-active", "ups-watchdog.service"],
            capture_output=True, text=True, timeout=2,
        )
        return p.stdout.strip() == "active"
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


# Latched throttle bits never clear without a SoC power-cycle, but we
# don't want the dashboard tile screaming yellow forever for an event
# that happened during boot. Track the value across polls and only
# colour the tile while a new event is "fresh" — defaults to 10 min.
_THROTTLE_STATE: dict = {
    "last_value": None,        # last observed register value
    "last_event_ts": 0.0,      # monotonic ts of last "now" bit set OR new latched bit
    "first_seen_ts": 0.0,      # first time we observed any latched bits
}
_THROTTLE_FRESH_S = 600        # how long after the last event we keep the tile yellow


def _throttle_info() -> dict | None:
    """Decoded vcgencmd get_throttled bitfield. Uses pi-bringup's
    pifetch-throttle.sh for the human-readable text when available;
    decodes inline otherwise. Returns None if vcgencmd is missing.

    Adds `fresh: bool` — true while a throttle event is currently
    happening OR happened within the last `_THROTTLE_FRESH_S` seconds.
    The dashboard uses this to decide colour, so latched bits from
    boot stop yelling once they're old news."""
    try:
        p = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    raw = p.stdout.strip().removeprefix("throttled=")
    try:
        val = int(raw, 16)
    except ValueError:
        return None

    text = "no events"
    if _PI_BRINGUP_THROTTLE.is_file():
        try:
            tp = subprocess.run(
                [str(_PI_BRINGUP_THROTTLE)], capture_output=True, text=True, timeout=2,
            )
            text = (tp.stdout or "").strip() or text
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
    elif val:
        now_names, latched_names = [], []
        for bit, name in [(0x1, "under-voltage"), (0x2, "freq-capped"),
                          (0x4, "throttled"),    (0x8, "soft-throttled")]:
            if val & bit: now_names.append(name)
            if val & (bit << 16): latched_names.append(name)
        parts = []
        if now_names:     parts.append(",".join(now_names)     + " (now)")
        if latched_names: parts.append(",".join(latched_names) + " (latched)")
        text = "; ".join(parts) or "no events"

    now_bits = val & 0x0F
    latched_bits = val & 0xF0000
    state = _THROTTLE_STATE
    now_t = time.monotonic()
    last_val = state["last_value"]

    if last_val is None:
        # First observation. If there are already latched bits set, we
        # don't know how old they are — but conservatively assume the
        # event was just before we started watching, so the tile shows
        # yellow until the decay window passes. Avoids hiding a real
        # ongoing problem by silently starting "stale".
        if latched_bits or now_bits:
            state["last_event_ts"] = now_t
            state["first_seen_ts"] = now_t
    else:
        # A new "now" bit appearing, or any "now" bit currently set,
        # counts as a live event — refresh the timer.
        if now_bits:
            state["last_event_ts"] = now_t
        # A latched bit becoming set (wasn't set last time) means an
        # event happened between polls — refresh the timer.
        new_latched = latched_bits & ~(last_val & 0xF0000)
        if new_latched:
            state["last_event_ts"] = now_t

    state["last_value"] = val

    age_s = (now_t - state["last_event_ts"]) if state["last_event_ts"] > 0 else None
    fresh = bool(now_bits) or (age_s is not None and age_s < _THROTTLE_FRESH_S)

    return {
        "raw": raw,
        "value": val,
        "text": text,
        "now": bool(now_bits),
        "latched": bool(latched_bits),
        "fresh": fresh,
        "age_s": int(age_s) if age_s is not None else None,
        "fresh_window_s": _THROTTLE_FRESH_S,
    }


# CPU and LAN deltas need a previous sample. Because /api/host is the only
# caller, module-level state is fine — we don't need cross-process locking.
_CPU_SAMPLE: dict = {"total": 0, "idle": 0}
_NET_SAMPLE: dict = {"t": 0.0, "rx": 0, "tx": 0, "iface": ""}


def _cpu_pct() -> int | None:
    """CPU utilization since the previous /api/host call. Returns None
    on the first call (no baseline yet) — the JS shows '—' until we
    have a delta."""
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
    except OSError:
        return None
    if not parts or parts[0] != "cpu":
        return None
    try:
        nums = [int(x) for x in parts[1:]]
    except ValueError:
        return None
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)   # idle + iowait
    total = sum(nums)
    last_total, last_idle = _CPU_SAMPLE["total"], _CPU_SAMPLE["idle"]
    pct: int | None = None
    if last_total > 0:
        dtot, didle = total - last_total, idle - last_idle
        if dtot > 0:
            pct = max(0, min(100, round((dtot - didle) * 100 / dtot)))
    _CPU_SAMPLE["total"], _CPU_SAMPLE["idle"] = total, idle
    return pct


def _default_route_iface() -> str | None:
    """Linux default-route interface from /proc/net/route (gateway 0.0.0.0)."""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if len(p) >= 8 and p[1] == "00000000":
                    return p[0]
    except OSError:
        pass
    return None


def _lan_info() -> dict | None:
    """Throughput on the default-route interface. None if no default
    route. rx_bps/tx_bps are None on the first call (no delta yet)."""
    iface = _default_route_iface()
    if not iface:
        return None
    rx = tx = 0
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                if line.lstrip().startswith(iface + ":"):
                    p = line.split()
                    rx = int(p[1])
                    tx = int(p[9])
                    break
    except (OSError, ValueError):
        return None
    now = time.monotonic()
    last = _NET_SAMPLE
    rx_bps = tx_bps = None
    if last["iface"] == iface and last["t"] > 0:
        dt = now - last["t"]
        if dt > 0.1:
            rx_bps = max(0, int((rx - last["rx"]) / dt))
            tx_bps = max(0, int((tx - last["tx"]) / dt))
    _NET_SAMPLE.update(t=now, rx=rx, tx=tx, iface=iface)
    return {"iface": iface, "rx_bps": rx_bps, "tx_bps": tx_bps}


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

    info["fan"] = _fan_info()
    info["hailo"] = _hailo_info()
    info["ups"] = _ups_info()
    info["cpu_pct"] = _cpu_pct()
    info["lan"] = _lan_info()
    info["throttle"] = _throttle_info()

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


# ─── /preview/<cam>/* — same-origin proxy to mediamtx WebRTC ────────────────
# mediamtx requires HTTP Basic on its :8889 endpoints from non-loopback IPs.
# Browsers strip url-embedded creds from iframe src (cross-origin) and won't
# let JS set Authorization headers on iframe loads. Solution: iframe loads
# /preview/cam0/ from the panel (same-origin, panel cookie auth), the panel
# forwards to mediamtx with Basic. Same flow handles the WHEP POST/PATCH/
# DELETE the player JS issues — relative URLs resolve back to /preview/cam0/whep.

import base64
import urllib.parse


def _mediamtx_basic_auth_header() -> str | None:
    creds = auth_lib.stream_credentials()
    if not creds:
        return None
    user, pw = creds
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


_PROXY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "host", "content-length",
}


@app.api_route(
    "/preview/{cam}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
@app.api_route("/preview/{cam}", methods=["GET"])
@app.api_route("/preview/{cam}/", methods=["GET"])
async def preview_proxy(request: Request, cam: str, path: str = ""):
    if not is_valid_name(cam):
        raise HTTPException(400, f"invalid cam name: {cam!r}")

    body = await request.body() if request.method in ("POST", "PUT", "PATCH") else None
    target = f"http://127.0.0.1:8889/{cam}"
    if path:
        target += "/" + path
    elif not request.url.path.endswith("/preview/" + cam):
        # /preview/cam → ensure trailing slash for mediamtx so the player loads
        pass
    # mediamtx serves the player at /cam/ (with trailing slash) — preserve it
    if request.url.path.endswith("/") and not target.endswith("/"):
        target += "/"
    if request.url.query:
        target += "?" + request.url.query

    fwd_headers: dict[str, str] = {}
    for k, v in request.headers.items():
        if k.lower() in _PROXY_HOP_HEADERS:
            continue
        fwd_headers[k] = v
    auth_header = _mediamtx_basic_auth_header()
    if auth_header:
        fwd_headers["Authorization"] = auth_header

    req = urllib.request.Request(target, method=request.method, data=body, headers=fwd_headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp_body = r.read()
            resp_status = r.status
            resp_headers = {}
            for h in ("content-type", "location", "etag", "cache-control"):
                if h in r.headers:
                    resp_headers[h] = r.headers[h]
    except urllib.error.HTTPError as e:
        resp_body = e.read() if e.fp else b""
        resp_status = e.code
        resp_headers = {}
        ct = e.headers.get("content-type") if e.headers else None
        if ct:
            resp_headers["content-type"] = ct
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return JSONResponse({"detail": f"upstream error: {e}"}, status_code=502)

    # mediamtx may return a Location header pointing at the upstream WHEP
    # session URL, e.g. '/cam0/whep/<id>'. Rewrite to our proxy path so the
    # player's PATCH/DELETE for ICE updates also flows through us.
    loc = resp_headers.get("location")
    if loc and loc.startswith("/" + cam + "/"):
        resp_headers["location"] = "/preview" + loc

    return Response(content=resp_body, status_code=resp_status, headers=resp_headers)


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


# ─── secret rotation ────────────────────────────────────────────────────────

@app.get("/api/auth/stream-credentials")
def api_stream_credentials() -> JSONResponse:
    """Return the active stream user (and password — anyone with a panel
    cookie can already read this from the dashboard URLs)."""
    creds = auth_lib.stream_credentials()
    if not creds:
        return JSONResponse({"enabled": False})
    user, password = creds
    return JSONResponse({"enabled": True, "user": user, "password": password})


ALLOWED_ROTATE_SCHEDULES = {
    "daily":   "daily",
    "weekly":  "weekly",
    "monthly": "monthly",
}


def _rotate_unit_paths() -> tuple[Path, Path]:
    user_systemd = Path.home() / ".config" / "systemd" / "user"
    return user_systemd / "usb-rtsp-rotate.service", user_systemd / "usb-rtsp-rotate.timer"


def _install_rotate_units(schedule: str) -> None:
    """Render systemd user units from templates and reload."""
    if schedule not in ALLOWED_ROTATE_SCHEDULES:
        raise HTTPException(400, f"schedule must be one of {sorted(ALLOWED_ROTATE_SCHEDULES)}")
    on_calendar = ALLOWED_ROTATE_SCHEDULES[schedule]
    svc_dst, timer_dst = _rotate_unit_paths()
    svc_dst.parent.mkdir(parents=True, exist_ok=True)
    svc_src = REPO_DIR / "systemd" / "usb-rtsp-rotate.service"
    timer_src = REPO_DIR / "systemd" / "usb-rtsp-rotate.timer"
    svc_dst.write_text(svc_src.read_text().replace("@@REPO_DIR@@", str(REPO_DIR)))
    timer_dst.write_text(timer_src.read_text().replace("@@SCHEDULE@@", on_calendar))
    subprocess.run(["systemctl", "--user", "daemon-reload"], timeout=10)


def _set_auto_rotate_persisted(enabled: bool, schedule: str | None) -> None:
    """Persist auto_rotate state into auth.yml so the panel re-renders correctly."""
    cfg = auth_lib.load_config()
    cfg.setdefault("streams", {})
    cfg["streams"]["auto_rotate"] = {
        "enabled": bool(enabled),
        "schedule": schedule or "weekly",
    }
    auth_lib.AUTH_YML.parent.mkdir(parents=True, exist_ok=True)
    auth_lib.AUTH_YML.write_text(yaml.safe_dump(cfg, sort_keys=False))


@app.get("/api/auth/auto-rotate")
def api_auto_rotate_state() -> JSONResponse:
    cfg = auth_lib.load_config()
    ar = (cfg.get("streams") or {}).get("auto_rotate") or {}
    # check live timer state
    p = subprocess.run(
        ["systemctl", "--user", "is-active", "usb-rtsp-rotate.timer"],
        capture_output=True, text=True, timeout=5,
    )
    return JSONResponse({
        "enabled": bool(ar.get("enabled")),
        "schedule": ar.get("schedule", "weekly"),
        "timer_active": p.returncode == 0,
        "schedules": sorted(ALLOWED_ROTATE_SCHEDULES.keys()),
    })


@app.post("/api/auth/auto-rotate")
async def api_auto_rotate_set(request: Request) -> JSONResponse:
    if not auth_lib.streams_enabled():
        raise HTTPException(400, "stream auth is not enabled (./install.sh --enable-auth first)")
    body = await request.json()
    enable = bool(body.get("enabled"))
    schedule = body.get("schedule") or "weekly"
    if schedule not in ALLOWED_ROTATE_SCHEDULES:
        raise HTTPException(400, f"schedule must be one of {sorted(ALLOWED_ROTATE_SCHEDULES)}")

    if enable:
        _install_rotate_units(schedule)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", "usb-rtsp-rotate.timer"],
            capture_output=True, text=True, timeout=10,
        )
    else:
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "usb-rtsp-rotate.timer"],
            capture_output=True, text=True, timeout=10,
        )
    _set_auto_rotate_persisted(enable, schedule)
    return JSONResponse({"enabled": enable, "schedule": schedule})


@app.post("/api/auth/stream-rotate")
def api_stream_rotate() -> JSONResponse:
    """Generate a fresh stream password, persist 0600, re-render mediamtx,
    restart both services. Returns the new password so the caller can
    surface it before admin restarts and the page reloads."""
    if not auth_lib.streams_enabled():
        raise HTTPException(400, "stream auth is not enabled (./install.sh --enable-auth first)")

    import secrets
    new_pass = secrets.token_urlsafe(24)
    pf = auth_lib.STREAM_PASS_FILE
    pf.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(pf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, new_pass.encode())
    finally:
        os.close(fd)

    render_p = subprocess.run(
        ["python3", "-m", "core.renderer"],
        cwd=str(REPO_DIR),
        capture_output=True, text=True, timeout=15,
    )
    if render_p.returncode != 0:
        raise HTTPException(500, f"render failed: {(render_p.stdout + render_p.stderr).strip()}")

    code, _ = systemctl("restart", "usb-rtsp")
    # Schedule admin restart out-of-band so this response makes it back
    # before systemd kills us. The panel JS will see the success status,
    # show the new password, and reload after a settle delay.
    subprocess.Popen(
        ["systemctl", "--user", "restart", "usb-rtsp-admin"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return JSONResponse({
        "rotated": True,
        "user": auth_lib.load_config()["streams"].get("user", "stream"),
        "password": new_pass,
        "mediamtx_reload": "restart" if code == 0 else "failed",
    })


# ─── WebRTC public-IP settings + state ─────────────────────────────────────

@app.get("/api/webrtc/state")
def api_webrtc_state() -> JSONResponse:
    cfg = (auth_lib.load_config().get("webrtc") or {})
    return JSONResponse({
        "public_ip": WEBRTC_STATE["public_ip"] or public_ip.read_cached(),
        "source": WEBRTC_STATE["source"],
        "last_detected_at": WEBRTC_STATE["last_detected_at"],
        "last_error": WEBRTC_STATE["last_error"],
        "configured_host": cfg.get("public_host", ""),
        "ip_echo_url": cfg.get("ip_echo_url", "https://ifconfig.me"),
        "refresh_minutes": cfg.get("refresh_minutes", 30),
        "auto_detect": cfg.get("auto_detect", True),
        "active_webrtc_sessions": _webrtc_active_session_count(),
    })


@app.post("/api/webrtc/detect")
def api_webrtc_detect() -> JSONResponse:
    result = _refresh_public_ip(force_restart=False)
    return JSONResponse(result)


# ─── UFW (host firewall) management ────────────────────────────────────────

@app.get("/api/ufw/state")
def api_ufw_state() -> JSONResponse:
    s = ufw_lib.status()
    rules = s.get("rules", [])
    managed = []
    for spec in ufw_lib.MANAGED_PORTS:
        matching = ufw_lib.matching_rules(rules, spec["port"], spec["proto"])
        managed.append({
            **spec,
            "scope":   ufw_lib.detect_scope(rules, spec["port"], spec["proto"]),
            "numbers": [r.number for r in matching],
        })
    other = []
    managed_keys = {(m["port"], m["proto"]) for m in ufw_lib.MANAGED_PORTS}
    for r in rules:
        head = r["to"].split()[0]
        if "/" in head:
            try:
                p, pr = head.split("/", 1)
                if (int(p), pr) in managed_keys:
                    continue  # belongs to a managed port; surfaced above
            except ValueError:
                pass
        other.append(r)
    return JSONResponse({
        "sudo_ok":   s.get("sudo_ok", False),
        "active":    s.get("active", False),
        "lan_cidr":  ufw_lib.lan_cidr(),
        "managed":   managed,
        "other":     other,
        "blocks":    ufw_lib.list_blocks() if s.get("sudo_ok") else [],
        "raw":       s.get("raw", ""),
        "error":     s.get("error", ""),
    })


def _kick_sessions_for_ip(ip: str) -> list[dict]:
    """Kick every active mediamtx session whose remoteAddr starts with ``ip:``.
    Returns a per-session result list; HLS muxers are skipped (no per-session
    kick exists for them — they're stateless segment fetches)."""
    results: list[dict] = []
    if not ip:
        return results
    prefix = ip + ":"
    for kind in ("rtspsessions", "webrtcsessions"):
        listing = api_get(f"/v3/{kind}/list") or {"items": []}
        for s in listing.get("items", []):
            ra = s.get("remoteAddr") or ""
            if not ra.startswith(prefix):
                continue
            sid = s.get("id")
            if not sid:
                continue
            code, _ = api_post(f"/v3/{kind}/kick/{sid}")
            results.append({"kind": kind, "id": sid, "remoteAddr": ra, "status": code})
    return results


@app.post("/api/ufw/block")
async def api_ufw_block(request: Request) -> JSONResponse:
    body = await request.json()
    source = (body.get("source") or "").strip()
    reason = (body.get("reason") or "").strip()
    if not source:
        raise HTTPException(400, "source is required")
    requester_ip = (request.client.host if request.client else "") or ""
    ok, why = ufw_lib.is_blockable(source, requester_ip=requester_ip)
    if not ok:
        raise HTTPException(403, why)
    comment_parts = ["usb-rtsp block"]
    if reason:
        comment_parts.append(reason)
    result = ufw_lib.block(source, comment=" — ".join(comment_parts))
    if not result.get("ok"):
        return JSONResponse(result, status_code=500)
    # Kick any active sessions from this IP. Best-effort — failures don't roll
    # back the block; the UFW rule is the actual security boundary.
    kicked: list[dict] = []
    src_net = ufw_lib._parse_source(source)
    if src_net is not None and src_net.num_addresses == 1:
        kicked = _kick_sessions_for_ip(str(src_net.network_address))
    return JSONResponse({**result, "kicked": kicked})


@app.post("/api/ufw/unblock")
async def api_ufw_unblock(request: Request) -> JSONResponse:
    body = await request.json()
    source = (body.get("source") or "").strip()
    if not source:
        raise HTTPException(400, "source is required")
    result = ufw_lib.unblock(source)
    if not result.get("ok"):
        return JSONResponse(result, status_code=500)
    return JSONResponse(result)


@app.post("/api/ufw/delete")
async def api_ufw_delete(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        number = int(body["number"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "expected {number: int}")
    return JSONResponse(ufw_lib.delete_rule(number))


@app.post("/api/ufw/port")
async def api_ufw_port(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        port  = int(body["port"])
        proto = str(body["proto"])
        scope = str(body["scope"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "expected {port, proto, scope}")
    spec = next((m for m in ufw_lib.MANAGED_PORTS if m["port"] == port and m["proto"] == proto), None)
    if not spec:
        raise HTTPException(403, f"{port}/{proto} is not a managed port")
    result = ufw_lib.set_port_scope(port, proto, scope, comment=spec.get("comment", ""))
    if not result.get("ok"):
        return JSONResponse(result, status_code=500)
    return JSONResponse(result)


@app.post("/api/ufw/enable")
def api_ufw_enable() -> JSONResponse:
    return JSONResponse(ufw_lib.set_ufw_enabled(True))


@app.post("/api/ufw/disable")
def api_ufw_disable() -> JSONResponse:
    return JSONResponse(ufw_lib.set_ufw_enabled(False))


@app.post("/api/webrtc/settings")
async def api_webrtc_settings(request: Request) -> JSONResponse:
    body = await request.json()
    cfg = auth_lib.load_config()
    cfg.setdefault("webrtc", {})
    if "public_host" in body:
        cfg["webrtc"]["public_host"] = (body.get("public_host") or "").strip()
    if "ip_echo_url" in body:
        cfg["webrtc"]["ip_echo_url"] = (body.get("ip_echo_url") or "https://ifconfig.me").strip()
    if "refresh_minutes" in body:
        try:
            cfg["webrtc"]["refresh_minutes"] = max(1, int(body["refresh_minutes"]))
        except (TypeError, ValueError):
            raise HTTPException(400, "refresh_minutes must be a positive integer")
    if "auto_detect" in body:
        cfg["webrtc"]["auto_detect"] = bool(body["auto_detect"])
    auth_lib.AUTH_YML.parent.mkdir(parents=True, exist_ok=True)
    auth_lib.AUTH_YML.write_text(yaml.safe_dump(cfg, sort_keys=False))
    # Trigger re-detect so the new settings take effect immediately.
    result = _refresh_public_ip(force_restart=False)
    return JSONResponse({"saved": True, **result})
