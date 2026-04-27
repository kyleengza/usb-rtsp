"""USB plugin's render contribution.

Reads the plugin's cameras.yml + the central transport profiles + quality
presets, emits one mediamtx path per camera.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from core.compression import h264_bitrate_kbps

# v4l2 FOURCC → ffmpeg's `-input_format` string
V4L2_TO_FFMPEG_INPUT_FORMAT = {
    "MJPG": "mjpeg",
    "YUYV": "yuyv422",
    "UYVY": "uyvy422",
    "H264": "h264",
    "HEVC": "hevc",
    "NV12": "nv12",
    "YV12": "yuv420p",
    "RGB3": "rgb24",
    "BGR3": "bgr24",
}


def ffmpeg_input_format(fourcc: str) -> str:
    return V4L2_TO_FFMPEG_INPUT_FORMAT.get(fourcc.upper(), fourcc.lower())


def default_encode(fmt: str) -> str:
    if fmt == "H264":
        return "copy"
    return "h264"


def _ffmpeg_cmd(cam: dict, profile: dict, qprof: dict) -> str:
    by_id_path = f"/dev/v4l/by-id/{cam['by_id']}"
    fmt = cam["format"]
    w, h, fps = cam["width"], cam["height"], cam["fps"]
    transport = profile.get("transport", "tcp")
    encode = cam.get("encode") or default_encode(fmt)

    x264_preset = cam.get("x264_preset") or qprof.get("preset", "ultrafast")
    bitrate_k = cam.get("bitrate_kbps") or h264_bitrate_kbps(w, h, qprof.get("bitrate_factor", 1.0))
    gop_seconds = cam.get("gop_seconds") or qprof.get("gop_seconds", 2)
    bframes = cam.get("bframes") if cam.get("bframes") is not None else qprof.get("bframes", 0)
    mjpeg_qv = cam.get("mjpeg_qv") or qprof.get("mjpeg_qv", 3)

    common_in = (
        f"-hide_banner -loglevel warning "
        f"-f v4l2 -input_format {ffmpeg_input_format(fmt)} "
        f"-video_size {w}x{h} -framerate {fps} "
        f"-i {by_id_path}"
    )
    common_out = (
        f"-f rtsp -rtsp_transport {transport} "
        f"rtsp://127.0.0.1:$RTSP_PORT/$MTX_PATH"
    )

    if encode == "copy":
        codec = "-c copy"
    elif encode == "mjpeg":
        codec = f"-c:v mjpeg -q:v {int(mjpeg_qv)} -huffman default"
    elif encode == "h264":
        h264_profile = "main" if int(bframes) > 0 else "baseline"
        gop_frames = max(1, int(gop_seconds) * fps)
        codec = (
            f"-an "
            f"-vf format=yuv420p,scale=in_range=full:out_range=tv "
            f"-c:v libx264 -preset {x264_preset} -tune zerolatency "
            f"-profile:v {h264_profile} -level 3.1 "
            f"-pix_fmt yuv420p -color_range tv "
            f"-b:v {bitrate_k}k -maxrate {bitrate_k}k -bufsize {bitrate_k}k "
            f"-g {gop_frames} -keyint_min {fps} -bf {int(bframes)}"
        )
    else:
        codec = "-c copy"

    return f"ffmpeg {common_in} {codec} {common_out}"


def cameras_yml_path(config_dir: Path) -> Path:
    return config_dir / "cameras.yml"


def load_cameras(config_dir: Path) -> dict:
    p = cameras_yml_path(config_dir)
    if not p.exists():
        return {"cameras": []}
    return yaml.safe_load(p.read_text()) or {"cameras": []}


def render_paths(ctx) -> dict[str, Any]:
    """Plugin entry point — called by core/renderer.py.

    Returns {path_name: mediamtx_path_config} for every camera in
    <config_dir>/cameras.yml.
    """
    doc = load_cameras(ctx.config_dir)
    profiles = ctx.profiles or {}
    qpresets = ctx.quality_presets or {}

    out: dict[str, Any] = {}
    for cam in doc.get("cameras", []):
        prof = profiles.get(cam.get("profile") or "balanced", {})
        qprof = qpresets.get(cam.get("quality") or "medium", {})
        out[cam["name"]] = {
            "source": "publisher",
            "runOnInit": _ffmpeg_cmd(cam, prof, qprof),
            "runOnInitRestart": True,
        }
    return out
