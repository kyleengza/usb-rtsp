"""Relay plugin REST endpoints (mounted at /api/relay)."""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.helpers import REPO_DIR, is_valid_name, systemctl

from .render import load_sources, sources_yml_path


def _save_sources(config_dir: Path, sources: list) -> None:
    p = sources_yml_path(config_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        backup = p.with_suffix(f".yml.bak.{int(datetime.now().timestamp())}")
        shutil.copy2(p, backup)
    p.write_text(yaml.safe_dump({"sources": sources}, sort_keys=False))


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


class RelaySource(BaseModel):
    name: str = Field(min_length=1, max_length=16)
    url: str = Field(min_length=4)
    user: str | None = None
    pass_: str | None = Field(default=None, alias="pass")
    transport: str | None = None  # tcp | udp | udp_multicast


def make_router(ctx) -> APIRouter:
    cfg_dir = ctx.plugin.config_dir
    cfg_dir.mkdir(parents=True, exist_ok=True)
    router = APIRouter(prefix="/api/relay", tags=["relay"])

    @router.get("/sources")
    def list_sources() -> JSONResponse:
        return JSONResponse({"sources": load_sources(cfg_dir)})

    @router.post("/sources")
    def add_source(body: RelaySource) -> JSONResponse:
        if not is_valid_name(body.name):
            raise HTTPException(400, f"invalid name: {body.name!r}")
        if not body.url.startswith(("rtsp://", "rtmp://", "http://", "https://")):
            raise HTTPException(400, "url must start with rtsp:// rtmp:// http:// or https://")
        if body.transport is not None and body.transport not in ("tcp", "udp", "udp_multicast"):
            raise HTTPException(400, "transport must be tcp | udp | udp_multicast")
        sources = load_sources(cfg_dir)
        # name uniqueness
        for s in sources:
            if s.get("name") == body.name:
                raise HTTPException(409, f"a source named {body.name!r} already exists")
        entry: dict[str, Any] = {"name": body.name, "url": body.url}
        if body.user: entry["user"] = body.user
        if body.pass_: entry["pass"] = body.pass_
        if body.transport: entry["transport"] = body.transport
        sources.append(entry)
        _save_sources(cfg_dir, sources)
        ok, msg = _render_config()
        if not ok:
            raise HTTPException(500, f"render failed: {msg}")
        return JSONResponse({"added": body.name, "reload": _reload_mediamtx()})

    @router.delete("/sources/{name}")
    def del_source(name: str) -> JSONResponse:
        if not is_valid_name(name):
            raise HTTPException(400, f"invalid name: {name!r}")
        sources = load_sources(cfg_dir)
        before = len(sources)
        sources = [s for s in sources if s.get("name") != name]
        if len(sources) == before:
            raise HTTPException(404, "no such source")
        _save_sources(cfg_dir, sources)
        ok, msg = _render_config()
        if not ok:
            raise HTTPException(500, f"render failed: {msg}")
        return JSONResponse({"deleted": name, "reload": _reload_mediamtx()})

    @router.post("/sources/{name}/enable")
    def enable_source(name: str) -> JSONResponse:
        return _set_source_enabled(name, True)

    @router.post("/sources/{name}/disable")
    def disable_source(name: str) -> JSONResponse:
        return _set_source_enabled(name, False)

    def _set_source_enabled(name: str, enable: bool) -> JSONResponse:
        if not is_valid_name(name):
            raise HTTPException(400, f"invalid name: {name!r}")
        sources = load_sources(cfg_dir)
        found = False
        for s in sources:
            if s.get("name") == name:
                s["enabled"] = enable
                found = True
                break
        if not found:
            raise HTTPException(404, "no such source")
        _save_sources(cfg_dir, sources)
        ok, msg = _render_config()
        if not ok:
            raise HTTPException(500, f"render failed: {msg}")
        return JSONResponse({"name": name, "enabled": enable, "reload": _reload_mediamtx()})

    return router
