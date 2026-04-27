"""Relay plugin entry point."""
from __future__ import annotations

from pathlib import Path

from fastapi.staticfiles import StaticFiles

from .api import make_router
from .render import load_sources, render_paths  # re-export for renderer

__all__ = ["register", "render_paths"]

PLUGIN_DIR = Path(__file__).resolve().parent


def list_inputs(ctx) -> list[dict]:
    sources = load_sources(ctx.plugin.config_dir)
    return [{
        "name": s.get("name", ""),
        "enabled": s.get("enabled") is not False,
        "label": s.get("url", ""),
    } for s in sources if s.get("name")]


def section_context(ctx, request) -> dict:
    """Per-render data the section template uses."""
    return {
        "relay_sources": load_sources(ctx.plugin.config_dir),
    }


def register(app, ctx) -> None:
    app.include_router(make_router(ctx))
    static_dir = PLUGIN_DIR / "static"
    if static_dir.is_dir():
        app.mount("/static/relay", StaticFiles(directory=str(static_dir)), name="static-relay")
