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

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
warn()  { printf '\033[33m%s\033[0m\n' "$*"; }
die()   { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# ── 1. arch check ───────────────────────────────────────────────────────────
arch="$(uname -m)"
[[ "$arch" == "aarch64" ]] || die "this installer targets aarch64; got $arch (extend if needed)"

# ── 2. apt deps ─────────────────────────────────────────────────────────────
bold "checking apt deps…"
missing=()
for pkg in "${APT_DEPS[@]}"; do
  dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if (( ${#missing[@]} )); then
  bold "installing missing packages: ${missing[*]} (sudo required)…"
  sudo apt-get update -qq
  sudo apt-get install -y --no-install-recommends "${missing[@]}"
  for pkg in "${missing[@]}"; do
    dpkg -s "$pkg" >/dev/null 2>&1 || die "$pkg still missing after apt-get install"
  done
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

if [[ ! -f "$CONFIG_DIR/cameras.yml" ]]; then
  bold "seeding cameras.yml from auto-detect…"
  "$REPO_DIR/bin/usb-rtsp-render" --seed-if-missing >/dev/null
  green "✓ cameras.yml seeded ($(yq -r '.cameras | length' "$CONFIG_DIR/cameras.yml" 2>/dev/null || python3 -c "import yaml,sys; print(len(yaml.safe_load(open(sys.argv[1])).get('cameras', [])))" "$CONFIG_DIR/cameras.yml") cam(s))"
else
  green "✓ cameras.yml exists (preserved)"
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

python3 -c "
import yaml
d = yaml.safe_load(open('$CONFIG_DIR/cameras.yml')) or {}
for c in d.get('cameras', []):
    print(f\"  rtsp://{c['name']}: rtsp://$host:8554/{c['name']}  ({c['format']} {c['width']}x{c['height']}@{c['fps']})\")
"
echo
echo "  admin:    http://$host:8080/    (or http://$ip:8080/)"
echo "  snap:     snap <cam>            (saves to $CONFIG_DIR/snapshots/)"
echo "  status:   systemctl --user status usb-rtsp"
echo "  logs:     journalctl --user -u usb-rtsp -f"
echo
green "done."
