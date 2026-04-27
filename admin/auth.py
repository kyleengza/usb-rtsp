"""Authentication helpers for the usb-rtsp admin panel.

Two concerns:
  1. Login: PAM-based, against the host's local user accounts. Reuses the
     same credentials as SSH / console login on this Pi. Handled by a small
     /etc/pam.d/usb-rtsp-admin service file (installed by install.sh) that
     just `@include`s common-auth + common-account.
  2. Sessions: signed cookie carrying `user.expiry.hmac`. No server-side
     state — easier to deploy, fine for a LAN tool.
"""
from __future__ import annotations

import hmac
import os
import secrets
import time
from hashlib import sha256
from pathlib import Path

import yaml

CONFIG_DIR = Path(os.environ.get("USB_RTSP_CONFIG_DIR", Path.home() / ".config" / "usb-rtsp"))
AUTH_YML = CONFIG_DIR / "auth.yml"
COOKIE_SECRET_FILE = CONFIG_DIR / ".cookie-secret"
STREAM_PASS_FILE = CONFIG_DIR / ".stream-pass"
COOKIE_NAME = "usb-rtsp-auth"


# ─── config ─────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "panel": {
        "enabled": False,
        "pam_service": "usb-rtsp-admin",
        "cookie_max_age_days": 7,
    },
    "streams": {
        "enabled": False,
        "user": "stream",
    },
}


def load_config() -> dict:
    if not AUTH_YML.exists():
        return DEFAULT_CONFIG
    try:
        loaded = yaml.safe_load(AUTH_YML.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return DEFAULT_CONFIG
    # merge over defaults so missing keys don't crash callers
    cfg = {k: {**v, **(loaded.get(k) or {})} for k, v in DEFAULT_CONFIG.items()}
    return cfg


def panel_enabled() -> bool:
    return bool(load_config().get("panel", {}).get("enabled"))


def streams_enabled() -> bool:
    return bool(load_config().get("streams", {}).get("enabled"))


def stream_credentials() -> tuple[str, str] | None:
    """Returns (user, password) when stream auth is on, else None."""
    cfg = load_config()
    if not cfg.get("streams", {}).get("enabled"):
        return None
    user = cfg["streams"].get("user") or "stream"
    if not STREAM_PASS_FILE.exists():
        return None
    pw = STREAM_PASS_FILE.read_text().strip()
    if not pw:
        return None
    return user, pw


# ─── cookie signing ─────────────────────────────────────────────────────────

def _secret() -> bytes:
    """Lazy-load (or create) a 32-byte random cookie secret."""
    if COOKIE_SECRET_FILE.exists():
        return COOKIE_SECRET_FILE.read_bytes()
    COOKIE_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    raw = secrets.token_bytes(32)
    # write 0600
    fd = os.open(COOKIE_SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)
    return raw


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), sha256).hexdigest()


def make_cookie(user: str) -> tuple[str, int]:
    """Return (cookie_value, max_age_seconds) for a freshly-issued session."""
    days = int(load_config().get("panel", {}).get("cookie_max_age_days", 7))
    max_age = max(60, days * 86400)
    expiry = int(time.time()) + max_age
    payload = f"{user}|{expiry}"
    return f"{payload}|{_sign(payload)}", max_age


def verify_cookie(value: str | None) -> str | None:
    """Validate a cookie value. Returns the user on success, else None."""
    if not value:
        return None
    try:
        user, expiry_str, sig = value.split("|", 2)
    except ValueError:
        return None
    payload = f"{user}|{expiry_str}"
    expected = _sign(payload)
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        if int(expiry_str) < time.time():
            return None
    except ValueError:
        return None
    if not user:
        return None
    return user


# ─── PAM ────────────────────────────────────────────────────────────────────

def pam_authenticate(username: str, password: str) -> bool:
    """Authenticate via PAM. Returns True iff the credentials are valid for
    a *real* local user. Disallows root and any user with UID < 1000 to make
    accidental privilege escalation harder.
    """
    if not username or not password:
        return False
    # cheap pre-filter — PAM wouldn't accept root anyway with our service file
    # but defence in depth costs nothing.
    if username == "root":
        return False
    try:
        import pwd
        u = pwd.getpwnam(username)
        if u.pw_uid < 1000:
            return False
    except KeyError:
        return False
    try:
        import pam  # type: ignore
    except ImportError:
        return False
    p = pam.pam()
    service = load_config().get("panel", {}).get("pam_service", "usb-rtsp-admin")
    return bool(p.authenticate(username, password, service=service))
