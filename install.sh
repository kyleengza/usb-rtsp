#!/usr/bin/env bash
# usb-rtsp installer — idempotent. Re-run after `git pull` or config changes.
#
# - verifies aarch64 + apt deps
# - downloads + sha256-verifies mediamtx binary into /usr/local/bin (one-time sudo)
# - seeds ~/.config/usb-rtsp/cameras.yml from auto-detect (if absent)
# - renders ~/.config/usb-rtsp/mediamtx.yml from cameras.yml
# - installs user systemd units + enables linger
# - enables + starts both services

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${USB_RTSP_CONFIG_DIR:-$HOME/.config/usb-rtsp}"
USER_SYSTEMD_DIR="$HOME/.config/systemd/user"
BIN_DIR="$HOME/.local/bin"

MEDIAMTX_VERSION="${MEDIAMTX_VERSION:-v1.18.0}"
MEDIAMTX_URL="https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_arm64.tar.gz"
MEDIAMTX_SUMS_URL="https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/checksums.sha256"
MEDIAMTX_BIN="/usr/local/bin/mediamtx"

APT_DEPS=(ffmpeg v4l-utils python3-fastapi python3-uvicorn python3-jinja2 python3-yaml curl tar)

# CLI flags
ENABLE_AUTH=0
DISABLE_AUTH=0
ENABLE_UFW_MGMT=0
DISABLE_UFW_MGMT=0
LIST_PLUGINS=0
declare -a ENABLE_PLUGINS=()
declare -a DISABLE_PLUGINS=()
declare -a ADD_PLUGINS=()
declare -a REMOVE_PLUGINS=()
NEXT_IS=""
for arg in "$@"; do
  if [[ "$NEXT_IS" == "enable-plugin" ]]; then ENABLE_PLUGINS+=("$arg"); NEXT_IS=""; continue; fi
  if [[ "$NEXT_IS" == "disable-plugin" ]]; then DISABLE_PLUGINS+=("$arg"); NEXT_IS=""; continue; fi
  if [[ "$NEXT_IS" == "add-plugin" ]]; then ADD_PLUGINS+=("$arg"); NEXT_IS=""; continue; fi
  if [[ "$NEXT_IS" == "remove-plugin" ]]; then REMOVE_PLUGINS+=("$arg"); NEXT_IS=""; continue; fi
  case "$arg" in
    --enable-auth)        ENABLE_AUTH=1 ;;
    --disable-auth)       DISABLE_AUTH=1 ;;
    --enable-ufw-mgmt)    ENABLE_UFW_MGMT=1 ;;
    --disable-ufw-mgmt)   DISABLE_UFW_MGMT=1 ;;
    --list-plugins)       LIST_PLUGINS=1 ;;
    --enable-plugin)      NEXT_IS="enable-plugin" ;;
    --disable-plugin)     NEXT_IS="disable-plugin" ;;
    --enable-plugin=*)    ENABLE_PLUGINS+=("${arg#*=}") ;;
    --disable-plugin=*)   DISABLE_PLUGINS+=("${arg#*=}") ;;
    --add-plugin)         NEXT_IS="add-plugin" ;;
    --remove-plugin)      NEXT_IS="remove-plugin" ;;
    --add-plugin=*)       ADD_PLUGINS+=("${arg#*=}") ;;
    --remove-plugin=*)    REMOVE_PLUGINS+=("${arg#*=}") ;;
  esac
done

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
warn()  { printf '\033[33m%s\033[0m\n' "$*"; }
die()   { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# ── 1. arch check ───────────────────────────────────────────────────────────
# --list-plugins is a read-only side action — print + exit
if (( LIST_PLUGINS )); then
  python3 - <<PYEOF
import sys
sys.path.insert(0, "$REPO_DIR")
from core.loader import discover_plugins, read_enabled_set
enabled = read_enabled_set()
for p in discover_plugins():
    state = "enabled" if p.name in enabled else "disabled"
    star = "*" if p.default_enabled else " "
    src = "bundled" if p.bundled else "user"
    print(f"  {star} {p.name:12s}  {state:8s}  {src:7s}  v{p.version}  {p.description}")
print()
print("(* = default_enabled when no plugins-enabled.yml exists)")
PYEOF
  exit 0
fi

# --add-plugin / --remove-plugin are also stand-alone actions
if (( ${#ADD_PLUGINS[@]} || ${#REMOVE_PLUGINS[@]} )); then
  python3 - <<PYEOF
import sys
sys.path.insert(0, "$REPO_DIR")
from pathlib import Path
from core.loader import install_plugin_from_git, install_plugin_from_path, uninstall_plugin

for spec in """${ADD_PLUGINS[*]}""".split():
    if not spec: continue
    try:
        if spec.startswith(("http://", "https://", "git@", "ssh://")):
            p = install_plugin_from_git(spec)
        else:
            p = install_plugin_from_path(Path(spec))
        print(f"installed plugin: {p.name} ({p.dir})")
    except Exception as e:
        print(f"add-plugin {spec!r} failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

for name in """${REMOVE_PLUGINS[*]}""".split():
    if not name: continue
    try:
        uninstall_plugin(name)
        print(f"uninstalled plugin: {name}")
    except Exception as e:
        print(f"remove-plugin {name!r} failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
PYEOF
fi

# Make sure the user-plugins dir exists — the panel's "Add plugin" and
# `--add-plugin` both clone/copy into it. The optional relay + inference
# plugins live in their own repos (kyleengza/usb-rtsp-plugin-relay,
# kyleengza/usb-rtsp-plugin-inference); install them via the panel or
# `./install.sh --add-plugin <git-url>`.
mkdir -p "$HOME/.local/share/usb-rtsp/plugins"

arch="$(uname -m)"
[[ "$arch" == "aarch64" ]] || die "this installer targets aarch64; got $arch (extend if needed)"

# ── 2. apt deps ─────────────────────────────────────────────────────────────
bold "checking apt deps…"
missing=()
for pkg in "${APT_DEPS[@]}"; do
  dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if (( ENABLE_AUTH )); then
  APT_DEPS+=(python3-pam)
fi
if (( ${#missing[@]} )); then
  bold "installing missing packages: ${missing[*]} (sudo required)…"
  sudo apt-get update -qq
  sudo apt-get install -y --no-install-recommends "${missing[@]}"
  for pkg in "${missing[@]}"; do
    dpkg -s "$pkg" >/dev/null 2>&1 || die "$pkg still missing after apt-get install"
  done
fi
# refresh missing[] in case --enable-auth added new ones above
if (( ENABLE_AUTH )) && ! dpkg -s python3-pam >/dev/null 2>&1; then
  bold "installing python3-pam for --enable-auth (sudo required)…"
  sudo apt-get install -y --no-install-recommends python3-pam
fi
green "✓ apt deps present"

# ── 3. mediamtx binary ──────────────────────────────────────────────────────
need_install=1
if [[ -x "$MEDIAMTX_BIN" ]]; then
  installed_ver="$("$MEDIAMTX_BIN" --version 2>/dev/null | head -1 || true)"
  if [[ "$installed_ver" == *"${MEDIAMTX_VERSION#v}"* ]] || [[ "$installed_ver" == *"$MEDIAMTX_VERSION"* ]]; then
    green "✓ mediamtx ${MEDIAMTX_VERSION} already installed"
    need_install=0
  else
    warn "mediamtx installed but version mismatch ($installed_ver) — will reinstall"
  fi
fi

if (( need_install )); then
  bold "downloading mediamtx ${MEDIAMTX_VERSION}…"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  ( cd "$tmp"
    curl -fsSL "$MEDIAMTX_URL" -o mediamtx.tar.gz
    curl -fsSL "$MEDIAMTX_SUMS_URL" -o checksums.sha256
    expected="$(grep "mediamtx_${MEDIAMTX_VERSION}_linux_arm64.tar.gz" checksums.sha256 | awk '{print $1}')"
    [[ -n "$expected" ]] || die "couldn't find arm64 hash in checksums.sha256"
    actual="$(sha256sum mediamtx.tar.gz | awk '{print $1}')"
    [[ "$expected" == "$actual" ]] || die "sha256 mismatch: expected $expected, got $actual"
    tar -xzf mediamtx.tar.gz
    [[ -x ./mediamtx ]] || die "extracted archive missing mediamtx binary"
    bold "installing $MEDIAMTX_BIN (sudo required once)…"
    sudo install -m 0755 ./mediamtx "$MEDIAMTX_BIN"
  )
  green "✓ mediamtx ${MEDIAMTX_VERSION} installed at $MEDIAMTX_BIN"
fi

# ── 4. config dir + cameras.yml seed + mediamtx.yml render ──────────────────
bold "preparing $CONFIG_DIR…"
mkdir -p "$CONFIG_DIR" "$CONFIG_DIR/snapshots"

# ── 4a. plugin enable/disable list ──────────────────────────────────────────
PLUGINS_FILE="$CONFIG_DIR/plugins-enabled.yml"

# Bootstrap default-enabled set if the file doesn't exist yet.
if [[ ! -f "$PLUGINS_FILE" ]]; then
  python3 - <<PYEOF
import sys, yaml
sys.path.insert(0, "$REPO_DIR")
from core.loader import discover_plugins, write_enabled_set
defaults = {p.name for p in discover_plugins() if p.default_enabled}
write_enabled_set(defaults)
print(f"seeded plugins-enabled.yml with: {sorted(defaults) or '(none)'}")
PYEOF
fi

# Apply --enable-plugin / --disable-plugin flags
if (( ${#ENABLE_PLUGINS[@]} || ${#DISABLE_PLUGINS[@]} )); then
  python3 - <<PYEOF
import sys, yaml
sys.path.insert(0, "$REPO_DIR")
from core.loader import discover_plugins, read_enabled_set, write_enabled_set
known = {p.name for p in discover_plugins()}
enabled = read_enabled_set()
for name in """${ENABLE_PLUGINS[*]}""".split():
    if not name: continue
    if name not in known:
        print(f"warning: --enable-plugin {name!r}: not found in plugins/", file=sys.stderr)
        continue
    enabled.add(name)
for name in """${DISABLE_PLUGINS[*]}""".split():
    if not name: continue
    enabled.discard(name)
write_enabled_set(enabled)
print(f"plugins enabled: {sorted(enabled) or '(none)'}")
PYEOF
fi

# ── 4b. cameras.yml migration: ~/.config/usb-rtsp/cameras.yml → usb/cameras.yml
if [[ -f "$CONFIG_DIR/cameras.yml" && ! -f "$CONFIG_DIR/usb/cameras.yml" ]]; then
  mkdir -p "$CONFIG_DIR/usb"
  mv "$CONFIG_DIR/cameras.yml" "$CONFIG_DIR/usb/cameras.yml"
  green "✓ migrated cameras.yml → usb/cameras.yml"
fi

# ── 4c. seed usb/cameras.yml from auto-detect if absent ────────────────────
USB_CAMERAS="$CONFIG_DIR/usb/cameras.yml"
if [[ ! -f "$USB_CAMERAS" ]] && python3 -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from core.loader import read_enabled_set
sys.exit(0 if 'usb' in read_enabled_set() else 1)
"; then
  bold "seeding $USB_CAMERAS from auto-detect…"
  mkdir -p "$CONFIG_DIR/usb"
  python3 - <<PYEOF
import sys, json, subprocess, yaml
sys.path.insert(0, "$REPO_DIR")
from plugins.usb import detect, render
detected = {"cameras": detect.discover()}
cams = []
for i, c in enumerate(detected.get("cameras", [])):
    d = c.get("default")
    if not d: continue
    cams.append({
        "name": f"cam{i}",
        "by_id": c["by_id"],
        "format": d["format"],
        "width": d["width"],
        "height": d["height"],
        "fps": d["fps"],
        "encode": render.default_encode(d["format"]),
        "profile": "balanced",
        "quality": "medium",
    })
open("$USB_CAMERAS", "w").write(yaml.safe_dump({"cameras": cams}, sort_keys=False))
print(f"seeded {len(cams)} cam(s)")
PYEOF
  green "✓ seeded $USB_CAMERAS"
elif [[ -f "$USB_CAMERAS" ]]; then
  green "✓ $USB_CAMERAS exists (preserved)"
fi

# ── auth: opt-in / opt-out ──────────────────────────────────────────────────
AUTH_YML="$CONFIG_DIR/auth.yml"
PAM_SERVICE_FILE="/etc/pam.d/usb-rtsp-admin"
STREAM_PASS_FILE="$CONFIG_DIR/.stream-pass"

if (( ENABLE_AUTH )); then
  bold "configuring auth (panel via PAM, streams via mediamtx)…"
  if [[ ! -f "$PAM_SERVICE_FILE" ]]; then
    bold "installing PAM service file at $PAM_SERVICE_FILE (sudo required)…"
    sudo install -m 0644 /dev/stdin "$PAM_SERVICE_FILE" <<'PAMEOF'
# usb-rtsp admin panel — local user authentication
# Reuses the system's common auth/account stack.
@include common-auth
@include common-account
PAMEOF
  fi
  # generate stream password if missing (24 url-safe random bytes)
  if [[ ! -s "$STREAM_PASS_FILE" ]]; then
    python3 -c 'import secrets; print(secrets.token_urlsafe(24))' \
      > "$STREAM_PASS_FILE"
    chmod 0600 "$STREAM_PASS_FILE"
  fi
  STREAM_PASS="$(cat "$STREAM_PASS_FILE")"
  cat > "$AUTH_YML" <<YAMLEOF
panel:
  enabled: true
  pam_service: usb-rtsp-admin
  cookie_max_age_days: 7
streams:
  enabled: true
  user: stream
YAMLEOF
  chmod 0600 "$AUTH_YML"
  green "✓ auth enabled (panel + streams)"
elif (( DISABLE_AUTH )); then
  bold "disabling auth…"
  rm -f "$AUTH_YML"
  green "✓ auth disabled"
fi

# ── ufw management: opt-in / opt-out ───────────────────────────────────────
# When opted in, the panel can change UFW rules without typing a sudo password.
# Limited to /usr/sbin/ufw — the user can't escalate to anything else.
UFW_SUDOERS_FILE="/etc/sudoers.d/usb-rtsp-ufw"
UFW_BIN="/usr/sbin/ufw"

if (( ENABLE_UFW_MGMT )); then
  if [[ ! -x "$UFW_BIN" ]]; then
    warn "ufw not found at $UFW_BIN — install it first ('sudo apt-get install ufw')"
  else
    bold "granting passwordless sudo for ufw to $USER (sudo required)…"
    SUDOERS_LINE="$USER ALL=(ALL) NOPASSWD: $UFW_BIN"
    # validate syntax via visudo -c before installing
    TMPF="$(mktemp)"
    printf '%s\n' "$SUDOERS_LINE" > "$TMPF"
    chmod 0440 "$TMPF"
    if sudo visudo -cf "$TMPF" >/dev/null; then
      sudo install -m 0440 -o root -g root "$TMPF" "$UFW_SUDOERS_FILE"
      green "✓ ufw mgmt enabled — wrote $UFW_SUDOERS_FILE"
    else
      die "generated sudoers line failed visudo -c — refusing to install"
    fi
    rm -f "$TMPF"
  fi
elif (( DISABLE_UFW_MGMT )); then
  bold "removing $UFW_SUDOERS_FILE (sudo required)…"
  sudo rm -f "$UFW_SUDOERS_FILE"
  green "✓ ufw mgmt disabled"
fi

bold "rendering mediamtx.yml…"
"$REPO_DIR/bin/usb-rtsp-render" >/dev/null
green "✓ $CONFIG_DIR/mediamtx.yml"

# ── 5. CLI symlinks ─────────────────────────────────────────────────────────
mkdir -p "$BIN_DIR"
for cli in snap usb-rtsp-detect usb-rtsp-render; do
  src="$REPO_DIR/bin/$cli"
  dst="$BIN_DIR/$cli"
  if [[ -L "$dst" || -e "$dst" ]]; then
    rm -f "$dst"
  fi
  ln -s "$src" "$dst"
done
green "✓ CLIs symlinked into $BIN_DIR"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on \$PATH — add it to your shell rc to use 'snap' from anywhere" ;;
esac

# ── 6. systemd user units ───────────────────────────────────────────────────
bold "installing user systemd units…"
mkdir -p "$USER_SYSTEMD_DIR"
install -m 0644 "$REPO_DIR/systemd/usb-rtsp.service" "$USER_SYSTEMD_DIR/usb-rtsp.service"
sed "s|@@REPO_DIR@@|$REPO_DIR|g" "$REPO_DIR/systemd/usb-rtsp-admin.service" \
  > "$USER_SYSTEMD_DIR/usb-rtsp-admin.service"
chmod 0644 "$USER_SYSTEMD_DIR/usb-rtsp-admin.service"

systemctl --user daemon-reload

# enable linger so units survive logout/reboot
if ! loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
  warn "enabling lingering for $USER (sudo required once)…"
  sudo loginctl enable-linger "$USER"
fi

systemctl --user enable --now usb-rtsp.service usb-rtsp-admin.service
green "✓ services enabled and started"

# ── 7. summary ──────────────────────────────────────────────────────────────
sleep 1
echo
bold "─── usb-rtsp ready ───"
host="$(hostname).local"
ip="$(ip -4 -o addr show wlan0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 || true)"
[[ -z "$ip" ]] && ip="$(ip -4 -o addr show eth0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 || true)"

if [[ -f "$AUTH_YML" ]] && grep -q "enabled: true" "$AUTH_YML"; then
  STREAM_USER="$(python3 -c "import yaml; print(yaml.safe_load(open('$AUTH_YML'))['streams']['user'])" 2>/dev/null || echo stream)"
  STREAM_PASS="$(cat "$STREAM_PASS_FILE" 2>/dev/null || echo '')"
  AUTH_PREFIX="${STREAM_USER}:${STREAM_PASS}@"
else
  AUTH_PREFIX=""
fi
python3 -c "
import yaml, os
src = '$CONFIG_DIR/usb/cameras.yml'
if not os.path.exists(src):
    src = '$CONFIG_DIR/cameras.yml'  # legacy path, just in case
if os.path.exists(src):
    d = yaml.safe_load(open(src)) or {}
    for c in d.get('cameras', []):
        print(f\"  rtsp://{c['name']}: rtsp://${AUTH_PREFIX}$host:8554/{c['name']}  ({c['format']} {c['width']}x{c['height']}@{c['fps']})\")
else:
    print('  (no cameras configured yet — plug a USB cam in and re-run install)')
"
echo
echo "  admin:    http://$host:8080/    (or http://$ip:8080/)"
echo "  snap:     snap <cam>            (saves to $CONFIG_DIR/snapshots/)"
echo "  status:   systemctl --user status usb-rtsp"
echo "  logs:     journalctl --user-unit=usb-rtsp.service -f"

if [[ -f "$AUTH_YML" ]] && grep -q "enabled: true" "$AUTH_YML"; then
  echo
  bold "─── auth enabled ───"
  echo "  panel:    log in with your Pi user account (PAM)"
  echo "  streams:  username = ${STREAM_USER:-stream}"
  echo "            password = $(cat "$STREAM_PASS_FILE")"
  echo "            (also saved at $STREAM_PASS_FILE — keep it safe)"
  echo "  disable:  ./install.sh --disable-auth"
fi

echo
green "done."
