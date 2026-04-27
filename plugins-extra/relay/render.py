"""Relay plugin render hook.

For each entry in <config_dir>/sources.yml emit a mediamtx path. Two
modes:

  default:  source: <upstream_url> + sourceOnDemand
            (mediamtx pulls and re-broadcasts; zero CPU re-encode)

  encode:   source: publisher + runOnDemand: ffmpeg ... -f rtsp 127.0.0.1/<name>
            (downsize / re-encode upstream — only while a reader is connected,
             matching the non-encode sourceOnDemand semantics so an idle relay
             doesn't peg the CPU or OOM the host. Restart=False + ffmpeg
             -stimeout cap the respawn churn when upstream is unreachable.)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from core.compression import h264_bitrate_kbps, load_quality_presets


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


def _ffmpeg_transcode_cmd(src: dict, qprof: dict) -> str:
    """Build the ffmpeg downsize/re-encode pipeline for an `encode:` source."""
    upstream = _embed_creds(src["url"], src.get("user"), src.get("pass"))
    transport = src.get("transport") or "tcp"
    enc = src.get("encode") or {}
    w = int(enc.get("width") or 0)
    h = int(enc.get("height") or 0)
    fps = int(enc.get("fps") or 0)

    x264_preset = enc.get("x264_preset") or qprof.get("preset", "ultrafast")
    bframes = int(enc.get("bframes") if enc.get("bframes") is not None else qprof.get("bframes", 0))
    gop_seconds = int(enc.get("gop_seconds") or qprof.get("gop_seconds", 2))
    bitrate_k = int(
        enc.get("bitrate_kbps")
        or h264_bitrate_kbps(w or 1280, h or 720, qprof.get("bitrate_factor", 1.0))
    )

    h264_profile = "main" if bframes > 0 else "baseline"
    fps_for_gop = fps or 30
    gop_frames = max(1, gop_seconds * fps_for_gop)

    # Default to 1280x720 when caller didn't pin a resolution. Without a
    # cap, ffmpeg encodes at upstream resolution — a 1080p60 (or 4K) feed
    # spikes Pi 5 CPU/power hard enough to brown the PSU on startup. The
    # scale filter preserves aspect ratio; force_original_aspect_ratio
    # =decrease + ensures-even-dimensions handle non-16:9 sources.
    if not (w or h):
        w, h = 1280, 720
    vf_parts = ["format=yuv420p", "scale=in_range=full:out_range=tv"]
    if w and h:
        vf_parts.insert(0, f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2")
    if fps:
        vf_parts.append(f"fps={fps}")
    vf = ",".join(vf_parts)

    # -timeout caps how long ffmpeg will block on the upstream socket
    # before erroring out (microseconds; was -stimeout pre-ffmpeg-5).
    # Without it, an unreachable upstream wedges ffmpeg for ~30s while
    # a reader (and runOnDemand) holds the path open, which on a Pi 5
    # can pile up enough respawn churn to brown out / OOM the host.
    common_in = (
        f"-hide_banner -loglevel warning "
        f"-rtsp_transport {transport} "
        f"-timeout 5000000 "
        f"-i {upstream}"
    )
    # -threads 2 caps libx264 to half the Pi 5's cores. Without it,
    # x264 spawns one thread per core (4) and the all-core spike on
    # ffmpeg startup can sag a marginal PSU enough to hard-reboot the
    # host before any logs flush. Two threads is plenty for 720p
    # ultrafast and leaves headroom for mediamtx + the panel.
    codec = (
        f"-an -vf {vf} "
        f"-c:v libx264 -preset {x264_preset} -tune zerolatency "
        f"-threads 2 "
        f"-profile:v {h264_profile} -level 3.1 "
        f"-pix_fmt yuv420p -color_range tv "
        f"-b:v {bitrate_k}k -maxrate {bitrate_k}k -bufsize {bitrate_k}k "
        f"-g {gop_frames} -keyint_min {fps_for_gop} -bf {bframes}"
    )
    common_out = (
        f"-f rtsp -rtsp_transport tcp "
        f"rtsp://127.0.0.1:$RTSP_PORT/$MTX_PATH"
    )
    return f"ffmpeg {common_in} {codec} {common_out}"


def render_paths(ctx) -> dict[str, Any]:
    out: dict[str, Any] = {}
    qpresets = getattr(ctx, "quality_presets", None) or load_quality_presets()
    for src in load_sources(ctx.config_dir):
        name = (src.get("name") or "").strip()
        url = (src.get("url") or "").strip()
        if not name or not url:
            continue
        # disabled sources stay in sources.yml so the user can re-enable
        # them from the panel; we just don't emit a mediamtx path.
        if src.get("enabled") is False:
            continue

        encode = src.get("encode")
        if encode:
            qprof = qpresets.get(encode.get("preset") or "medium", {})
            # runOnDemandRestart=False: when ffmpeg dies (e.g. upstream
            # offline), don't immediately respawn while a reader is still
            # attached. The reader's player has to retry the path, which
            # gives a much slower (and self-limiting) re-trigger loop —
            # the brown-out we saw came from mediamtx tight-respawning
            # ffmpeg every few seconds against a dead upstream.
            out[name] = {
                "source": "publisher",
                "runOnDemand": _ffmpeg_transcode_cmd(src, qprof),
                "runOnDemandRestart": False,
                "runOnDemandStartTimeout": "15s",
                "runOnDemandCloseAfter": "10s",
            }
        else:
            path_cfg: dict[str, Any] = {
                "source": _embed_creds(url, src.get("user"), src.get("pass")),
                "sourceOnDemand": True,
                "sourceOnDemandStartTimeout": "10s",
                "sourceOnDemandCloseAfter": "10s",
            }
            if src.get("transport"):
                path_cfg["rtspTransport"] = src["transport"]
            out[name] = path_cfg
    return out
