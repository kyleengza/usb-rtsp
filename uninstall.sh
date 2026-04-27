#!/usr/bin/env bash
# usb-rtsp uninstaller — removes services, binary, and CLI symlinks.
# Leaves ~/.config/usb-rtsp/ intact (your cameras.yml + snapshots) unless --purge.

set -euo pipefail

PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1

USER_SYSTEMD_DIR="$HOME/.config/systemd/user"
BIN_DIR="$HOME/.local/bin"
CONFIG_DIR="${USB_RTSP_CONFIG_DIR:-$HOME/.config/usb-rtsp}"
MEDIAMTX_BIN="/usr/local/bin/mediamtx"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }

bold "stopping services…"
systemctl --user disable --now usb-rtsp.service usb-rtsp-admin.service 2>/dev/null || true

bold "removing unit files…"
rm -f "$USER_SYSTEMD_DIR/usb-rtsp.service" "$USER_SYSTEMD_DIR/usb-rtsp-admin.service"
systemctl --user daemon-reload || true

bold "removing CLI symlinks…"
for cli in snap usb-rtsp-detect usb-rtsp-render; do
  rm -f "$BIN_DIR/$cli"
done

if [[ -x "$MEDIAMTX_BIN" ]]; then
  bold "removing $MEDIAMTX_BIN (sudo)…"
  sudo rm -f "$MEDIAMTX_BIN"
fi

if (( PURGE )); then
  bold "purging $CONFIG_DIR…"
  rm -rf "$CONFIG_DIR"
fi

green "done."
[[ $PURGE -eq 0 ]] && echo "(your config + snapshots remain at $CONFIG_DIR — pass --purge to remove)"
