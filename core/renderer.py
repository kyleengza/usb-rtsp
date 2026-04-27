"""Central mediamtx.yml renderer.

Loads the global config (transport profiles, quality presets, auth, RTSP/
HLS/WebRTC server settings), then asks every enabled plugin for its slice
of paths and merges them in. Replaces the per-camera/per-plugin
responsibilities scattered in the old bin/usb-rtsp-render.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import auth as auth_lib
from . import compression
from .helpers import CONFIG_DIR, REPO_DIR
from .loader import enabled_plugins, render_all_paths, _make_ctx

PROFILES_FILE = REPO_DIR / "etc" / "profiles.yml"
QUALITY_PRESETS_FILE = REPO_DIR / "etc" / "quality-presets.yml"
DEFAULT_OUT = CONFIG_DIR / "mediamtx.yml"


@dataclass
class RenderCtx:
    """Render-time context handed to each plugin's render_paths()."""
    plugin: object               # core.loader.Plugin
    config_dir: Path
    profiles: dict               # etc/profiles.yml contents
    quality_presets: dict        # etc/quality-presets.yml contents — relay/usb both use this
    stream_user: str | None      # if stream auth on, username; else None
    stream_pass: str | None


def _load_yaml(p: Path) -> dict:
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}


def _stream_creds() -> tuple[str | None, str | None]:
    creds = auth_lib.stream_credentials()
    if not creds:
        return None, None
    return creds


def _auth_block() -> dict:
    """authInternalUsers if stream auth is on, else just authMethod."""
    user, password = _stream_creds()
    if not (user and password):
        return {"authMethod": "internal"}
    return {
        "authMethod": "internal",
        "authInternalUsers": [
            {
                "user": "any",
                "pass": "",
                "ips": ["127.0.0.1/32", "::1/128"],
                "permissions": [
                    {"action": "publish"}, {"action": "read"},
                    {"action": "playback"}, {"action": "api"},
                    {"action": "metrics"}, {"action": "pprof"},
                ],
            },
            {
                "user": user,
                "pass": password,
                "ips": [],
                "permissions": [{"action": "read"}, {"action": "playback"}],
            },
        ],
    }


def _global_buffers(profiles: dict, plugin_paths: dict, plugins: list) -> tuple[int, int]:
    """Pick the most generous buffer/queue from the active transport profiles
    referenced by any plugin's source list. We don't know which profile each
    plugin uses without a deeper hook, so default to the medium ('balanced')
    profile for now and let plugins override per-path if needed.
    """
    active = profiles.get("balanced") or {}
    return active.get("read_buffer_count", 2048), active.get("write_queue_size", 512)


def build_config() -> dict:
    profiles = _load_yaml(PROFILES_FILE)
    qpresets = compression.load_quality_presets()
    user, password = _stream_creds()

    def ctx_factory(plugin):
        return RenderCtx(
            plugin=plugin,
            config_dir=plugin.config_dir,
            profiles=profiles,
            quality_presets=qpresets,
            stream_user=user,
            stream_pass=password,
        )

    paths = render_all_paths(ctx_factory)
    plugins = enabled_plugins()

    read_buf, write_q = _global_buffers(profiles, paths, plugins)

    cfg = {
        "logLevel": "info",
        "logDestinations": ["stdout"],
        "readTimeout": "10s",
        "writeTimeout": "10s",
        "readBufferCount": read_buf,
        "writeQueueSize": write_q,
        "udpMaxPayloadSize": 1472,
        **_auth_block(),
        "api": True,
        "apiAddress": "127.0.0.1:9997",
        "rtsp": True,
        "rtspAddress": ":8554",
        "rtspTransports": ["tcp", "udp"],
        "hls": True,
        "hlsAddress": ":8888",
        "hlsAlwaysRemux": False,
        "hlsVariant": "mpegts",
        "hlsSegmentCount": 5,
        "hlsSegmentDuration": "1s",
        "hlsAllowOrigin": "*",
        "webrtc": True,
        "webrtcAddress": ":8889",
        "webrtcAllowOrigin": "*",
        "webrtcLocalUDPAddress": ":8189",
        "rtmp": False,
        "srt": False,
        "paths": paths or {},
    }
    return cfg


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--print", action="store_true",
                   help="print rendered config instead of writing --out")
    args = p.parse_args()

    cfg = build_config()
    rendered = yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False)
    if args.print:
        sys.stdout.write(rendered)
    else:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered)
        print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
