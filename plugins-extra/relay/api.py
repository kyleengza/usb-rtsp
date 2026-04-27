"""Relay plugin REST endpoints (mounted at /api/relay)."""
from __future__ import annotations

import json
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

from .render import _embed_creds, load_sources, sources_yml_path


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


class RelayEncode(BaseModel):
    preset: str | None = "medium"
    bitrate_kbps: int | None = Field(default=None, ge=100, le=20000)
    width: int | None = Field(default=None, ge=16, le=7680)
    height: int | None = Field(default=None, ge=16, le=4320)
    fps: int | None = Field(default=None, ge=1, le=240)
    x264_preset: str | None = None
    bframes: int | None = Field(default=None, ge=0, le=3)
    gop_seconds: int | None = Field(default=None, ge=1, le=10)


class RelaySource(BaseModel):
    name: str = Field(min_length=1, max_length=16)
    url: str = Field(min_length=4)
    user: str | None = None
    pass_: str | None = Field(default=None, alias="pass")
    transport: str | None = None
    encode: RelayEncode | None = None


class RelaySourcePatch(BaseModel):
    """Same shape as RelaySource but every field optional."""
    url: str | None = Field(default=None, min_length=4)
    user: str | None = None
    pass_: str | None = Field(default=None, alias="pass")
    transport: str | None = None
    encode: RelayEncode | None = None


class RelayProbe(BaseModel):
    url: str = Field(min_length=4)
    user: str | None = None
    pass_: str | None = Field(default=None, alias="pass")
    transport: str | None = None


def make_router(ctx) -> APIRouter:
    cfg_dir = ctx.plugin.config_dir
    cfg_dir.mkdir(parents=True, exist_ok=True)
    router = APIRouter(prefix="/api/relay", tags=["relay"])

    @router.get("/sources")
    def list_sources() -> JSONResponse:
        return JSONResponse({"sources": load_sources(cfg_dir)})

    # NOTE: probe must come before any /sources/{name} matchers below so it
    # doesn't get hijacked as if {name}=="probe".
    @router.post("/sources/probe")
    def probe_source(body: RelayProbe) -> JSONResponse:
        url = _embed_creds(body.url, body.user, body.pass_)
        cmd = [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_entries", "stream=codec_name,profile,width,height,r_frame_rate",
            "-select_streams", "v:0",
        ]
        if body.transport:
            cmd += ["-rtsp_transport", body.transport]
        cmd += ["-i", url]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        except subprocess.TimeoutExpired:
            return JSONResponse({"ok": False, "error": "ffprobe timed out (>8s)"})
        if p.returncode != 0:
            err = (p.stderr or p.stdout).strip().splitlines()
            return JSONResponse({"ok": False, "error": err[-1] if err else f"ffprobe exited {p.returncode}"})
        try:
            data = json.loads(p.stdout)
            stream = (data.get("streams") or [{}])[0]
        except (json.JSONDecodeError, IndexError):
            return JSONResponse({"ok": False, "error": "could not parse ffprobe output"})
        rate = stream.get("r_frame_rate", "0/1")
        try:
            num, den = rate.split("/")
            fps = round(int(num) / int(den), 1) if int(den) else 0
        except (ValueError, ZeroDivisionError):
            fps = 0
        return JSONResponse({
            "ok": True,
            "codec": stream.get("codec_name"),
            "profile": stream.get("profile"),
            "width": stream.get("width"),
            "height": stream.get("height"),
            "fps": fps,
        })

    @router.post("/sources")
    def add_source(body: RelaySource) -> JSONResponse:
        if not is_valid_name(body.name):
            raise HTTPException(400, f"invalid name: {body.name!r}")
        if not body.url.startswith(("rtsp://", "rtmp://", "http://", "https://")):
            raise HTTPException(400, "url must start with rtsp:// rtmp:// http:// or https://")
        if body.transport is not None and body.transport not in ("tcp", "udp", "udp_multicast"):
            raise HTTPException(400, "transport must be tcp | udp | udp_multicast")
        sources = load_sources(cfg_dir)
        for s in sources:
            if s.get("name") == body.name:
                raise HTTPException(409, f"a source named {body.name!r} already exists")
        entry: dict[str, Any] = {"name": body.name, "url": body.url}
        if body.user: entry["user"] = body.user
        if body.pass_: entry["pass"] = body.pass_
        if body.transport: entry["transport"] = body.transport
        if body.encode is not None:
            entry["encode"] = {k: v for k, v in body.encode.model_dump().items() if v is not None}
        sources.append(entry)
        _save_sources(cfg_dir, sources)
        ok, msg = _render_config()
        if not ok:
            raise HTTPException(500, f"render failed: {msg}")
        return JSONResponse({"added": body.name, "reload": _reload_mediamtx()})

    @router.patch("/sources/{name}")
    def edit_source(name: str, body: RelaySourcePatch) -> JSONResponse:
        if not is_valid_name(name):
            raise HTTPException(400, f"invalid name: {name!r}")
        if body.transport is not None and body.transport not in ("tcp", "udp", "udp_multicast"):
            raise HTTPException(400, "transport must be tcp | udp | udp_multicast")
        if body.url is not None and not body.url.startswith(("rtsp://", "rtmp://", "http://", "https://")):
            raise HTTPException(400, "url must start with rtsp:// rtmp:// http:// or https://")
        sources = load_sources(cfg_dir)
        target = next((s for s in sources if s.get("name") == name), None)
        if target is None:
            raise HTTPException(404, "no such source")
        # PATCH semantics: distinguish "field omitted" (skip) from "explicit
        # null" (clear). Pydantic v2 exposes the set of fields the client
        # actually supplied via model_fields_set.
        sent = body.model_fields_set
        for k, v in (("url", body.url), ("user", body.user), ("transport", body.transport)):
            if v is not None:
                target[k] = v
        if body.pass_ is not None:
            target["pass"] = body.pass_
        if "encode" in sent:
            if body.encode is None:
                target.pop("encode", None)
            else:
                target["encode"] = {k: v for k, v in body.encode.model_dump().items() if v is not None}
        _save_sources(cfg_dir, sources)
        ok, msg = _render_config()
        if not ok:
            raise HTTPException(500, f"render failed: {msg}")
        return JSONResponse({"updated": name, "reload": _reload_mediamtx()})

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
