"""Relay plugin entry point."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from fastapi.staticfiles import StaticFiles

from .api import make_router
from .render import load_sources, render_paths  # re-export for renderer

__all__ = ["register", "render_paths"]

PLUGIN_DIR = Path(__file__).resolve().parent


def _live_paths_state() -> dict:
    """Snapshot of mediamtx /v3/paths/list keyed by path name. Best-effort:
    returns {} if mediamtx is unreachable."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:9997/v3/paths/list", timeout=2) as r:
            data = json.loads(r.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
        return {}
    out = {}
    for p in data.get("items", []):
        out[p.get("name")] = p
    return out


def list_inputs(ctx) -> list[dict]:
    sources = load_sources(ctx.plugin.config_dir)
    return [{
        "name": s.get("name", ""),
        "enabled": s.get("enabled") is not False,
        "label": s.get("url", ""),
    } for s in sources if s.get("name")]


def section_context(ctx, request) -> dict:
    """Per-render data the section template uses. Adds live state from
    mediamtx so each card can show ready / bytesReceived / source error."""
    sources = load_sources(ctx.plugin.config_dir)
    live = _live_paths_state()
    enriched = []
    for s in sources:
        item = dict(s)
        path_state = live.get(s.get("name"))
        if path_state:
            tracks = path_state.get("tracks") or []
            item["_live"] = {
                "ready": bool(path_state.get("ready") or path_state.get("sourceReady")),
                "bytes_received": path_state.get("bytesReceived", 0),
                "tracks": ", ".join(tracks),
                "readers": len(path_state.get("readers") or []),
            }
        else:
            item["_live"] = None
        enriched.append(item)
    return {"relay_sources": enriched}


def register(app, ctx) -> None:
    app.include_router(make_router(ctx))
    static_dir = PLUGIN_DIR / "static"
    if static_dir.is_dir():
        app.mount("/static/relay", StaticFiles(directory=str(static_dir)), name="static-relay")
