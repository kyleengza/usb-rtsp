"""Public-IP discovery for mediamtx WebRTC NAT1To1.

mediamtx needs a routable IP to advertise as a host candidate so external
WebRTC peers can connect. Residential ISPs rotate the public IP, so we
auto-detect and cache it. Strategy, in priority order:

  1. ``cfg["public_host"]`` is an IP literal → use verbatim.
  2. ``cfg["public_host"]`` is a hostname → ``socket.gethostbyname()``.
  3. Otherwise → ``curl <ip_echo_url>`` (default ``ifconfig.me``).

Successful detection writes the IP to ``~/.cache/usb-rtsp/public-ip``
so the renderer can run without a network round-trip.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
from pathlib import Path

CACHE_FILE = Path(os.path.expanduser("~/.cache/usb-rtsp/public-ip"))


def _is_ipv4(s: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(s), ipaddress.IPv4Address)
    except ValueError:
        return False


def _resolve_hostname(host: str, timeout: float = 3.0) -> str | None:
    socket.setdefaulttimeout(timeout)
    try:
        return socket.gethostbyname(host)
    except (socket.gaierror, socket.herror, OSError):
        return None
    finally:
        socket.setdefaulttimeout(None)


def _curl_ip_echo(url: str, timeout: int = 5) -> str | None:
    try:
        r = subprocess.run(
            ["curl", "-fsS", "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 2,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    if r.returncode != 0:
        return None
    ip = (r.stdout or "").strip()
    return ip if _is_ipv4(ip) else None


def _write_cache(ip: str) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(ip + "\n")
        tmp.replace(CACHE_FILE)
    except OSError:
        pass


def read_cached() -> str | None:
    try:
        ip = CACHE_FILE.read_text().strip()
    except OSError:
        return None
    return ip if _is_ipv4(ip) else None


def detect(cfg: dict | None = None) -> tuple[str | None, str | None]:
    """Return ``(ip, source)`` where ``source`` is ``"dns"`` or ``"http"``.

    On total failure returns ``(None, None)``; the caller decides whether
    to leave the previous cache in place.
    """
    cfg = cfg or {}
    host = (cfg.get("public_host") or "").strip()

    if host:
        if _is_ipv4(host):
            _write_cache(host)
            return host, "dns"
        ip = _resolve_hostname(host)
        if ip:
            _write_cache(ip)
            return ip, "dns"
        # configured-but-unresolvable: fall through so we still have *some*
        # IP for now (next refresh tick retries the hostname).

    if cfg.get("auto_detect", True):
        echo_url = cfg.get("ip_echo_url") or "https://ifconfig.me"
        ip = _curl_ip_echo(echo_url)
        if ip:
            _write_cache(ip)
            return ip, "http"

    return None, None
