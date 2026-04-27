"""Shared helpers used across the admin core and plugin modules.

Pure utilities — no FastAPI app, no plugin awareness, no business logic.
Anything imported here must work standalone.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ─── paths used everywhere ──────────────────────────────────────────────────

CONFIG_DIR = Path(os.environ.get("USB_RTSP_CONFIG_DIR", Path.home() / ".config" / "usb-rtsp"))
SNAP_DIR = CONFIG_DIR / "snapshots"
PLUGINS_ENABLED_FILE = CONFIG_DIR / "plugins-enabled.yml"

REPO_DIR = Path(os.environ.get("USB_RTSP_REPO", Path(__file__).resolve().parent.parent))
PLUGINS_DIR = REPO_DIR / "plugins"          # bundled plugins (ship with usb-rtsp main repo)
USER_PLUGINS_DIR = Path.home() / ".local" / "share" / "usb-rtsp" / "plugins"  # user-installed

MEDIAMTX_API = "http://127.0.0.1:9997"

# unit names allowed by every shellout that touches systemctl or journalctl
ALLOWED_UNITS = {"usb-rtsp", "usb-rtsp-admin"}
ALLOWED_SVC_ACTIONS = {"is-active", "restart", "stop", "start", "status", "show"}

# camera/source name validator — also used by API path-param checks
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,15}$")


def is_valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or ""))


# ─── mediamtx control API ───────────────────────────────────────────────────

def api_get(path: str, timeout: float = 2.0) -> Any:
    try:
        with urllib.request.urlopen(f"{MEDIAMTX_API}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None


def api_post(path: str, body: dict | None = None, timeout: float = 3.0) -> tuple[int, Any]:
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


# ─── systemd ────────────────────────────────────────────────────────────────

def systemctl(action: str, unit: str) -> tuple[int, str]:
    if unit not in ALLOWED_UNITS:
        raise ValueError(f"unit not allowed: {unit}")
    if action not in ALLOWED_SVC_ACTIONS:
        raise ValueError(f"action not allowed: {action}")
    p = subprocess.run(
        ["systemctl", "--user", action, unit],
        capture_output=True, text=True, timeout=10,
    )
    return p.returncode, (p.stdout + p.stderr).strip()


def service_meta(unit: str) -> dict:
    """ActiveState / SubState / ActiveEnterTimestamp / MainPID for a user unit."""
    if unit not in ALLOWED_UNITS:
        raise ValueError(f"unit not allowed: {unit}")
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


def journal(unit: str, lines: int) -> str:
    """Read the system-journal entries for a user unit. Uses --user-unit
    (queries the system journal by user-unit tag) instead of --user -u
    (per-user journal, not enabled on stock Debian)."""
    if unit not in ALLOWED_UNITS:
        raise ValueError(f"unit not allowed: {unit}")
    n = max(1, min(int(lines), 500))
    p = subprocess.run(
        ["journalctl", f"--user-unit={unit}.service", "-n", str(n), "--no-pager"],
        capture_output=True, text=True, timeout=10,
    )
    return p.stdout + (p.stderr if p.returncode else "")


# ─── formatting ─────────────────────────────────────────────────────────────

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


def duration_h(created: str | None, now: datetime | None = None) -> str:
    """Format an RFC3339 timestamp as a human-readable duration since `now`."""
    try:
        if not created:
            return "—"
        t = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return fmt_duration(((now or datetime.now(timezone.utc)) - t).total_seconds())
    except (ValueError, TypeError):
        return "—"
