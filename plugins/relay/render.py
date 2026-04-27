"""Relay plugin render hook.

For each entry in <config_dir>/sources.yml, emit a mediamtx path config
with `source: <upstream_url>` so mediamtx pulls and re-broadcasts on the
local RTSP/HLS/WebRTC ports. Zero CPU cost on the Pi (mediamtx remuxes
in Go; HLS segmentation only when a client subscribes).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml


def sources_yml_path(config_dir: Path) -> Path:
    return config_dir / "sources.yml"


def load_sources(config_dir: Path) -> list[dict]:
    p = sources_yml_path(config_dir)
    if not p.exists():
        return []
    doc = yaml.safe_load(p.read_text()) or {}
    return doc.get("sources") or []


def _embed_creds(url: str, user: str | None, pw: str | None) -> str:
    """Insert user:pass@ into the URL between the scheme and host."""
    if not user or not pw:
        return url
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{quote(user, safe='')}:{quote(pw, safe='')}@{rest}"


def render_paths(ctx) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for src in load_sources(ctx.config_dir):
        name = (src.get("name") or "").strip()
        url = (src.get("url") or "").strip()
        if not name or not url:
            continue
        path_cfg: dict[str, Any] = {
            "source": _embed_creds(url, src.get("user"), src.get("pass")),
            "sourceOnDemand": True,         # only pull when something subscribes
            "sourceOnDemandStartTimeout": "10s",
            "sourceOnDemandCloseAfter": "10s",
        }
        if src.get("transport"):
            path_cfg["rtspTransport"] = src["transport"]
        out[name] = path_cfg
    return out
