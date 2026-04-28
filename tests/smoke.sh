#!/usr/bin/env bash
# tests/smoke.sh — end-to-end verification of a fresh usb-rtsp install.
# Assumes:
#   - install.sh has already been run successfully
#   - at least one USB camera is connected and configured

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${USB_RTSP_CONFIG_DIR:-$HOME/.config/usb-rtsp}"

pass() { printf '\033[32m✓\033[0m %s\n' "$*"; }
fail() { printf '\033[31m✗\033[0m %s\n' "$*"; exit 1; }
info() { printf '\033[36m·\033[0m %s\n' "$*"; }

# If panel auth is on, forge a valid cookie so the smoke test can hit
# protected endpoints. Otherwise leave the cookie header empty.
COOKIE_ARG=()
if [[ -f "$CONFIG_DIR/auth.yml" ]] && grep -q "enabled: true" "$CONFIG_DIR/auth.yml"; then
  TEST_COOKIE="$(USB_RTSP_REPO=$REPO_DIR python3 -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from core import auth
v, _ = auth.make_cookie('smoke-test')
print(v)
")"
  COOKIE_ARG=(--cookie "usb-rtsp-auth=$TEST_COOKIE")
  info "panel auth on — forged a smoke-test cookie"
fi

# 1. services up
info "services up"
systemctl --user is-active usb-rtsp.service       >/dev/null || fail "usb-rtsp.service not active"
systemctl --user is-active usb-rtsp-admin.service >/dev/null || fail "usb-rtsp-admin.service not active"
pass "both units active"

# 2. mediamtx control API reachable
info "mediamtx control API"
paths_json="$(curl -fsS http://127.0.0.1:9997/v3/paths/list)"
[[ -n "$paths_json" ]] || fail "control API empty response"
pass "control API responds"

# 3. at least one path configured
info "configured paths"
n="$(echo "$paths_json" | python3 -c 'import json, sys; print(len(json.load(sys.stdin).get("items", [])))')"
[[ "$n" -ge 1 ]] || fail "no paths configured"
pass "$n path(s) configured"

# 4. first path becomes ready (subscribing via ffprobe triggers the
#    on-demand encoder, so we run the probe before checking 'ready' —
#    runOnDemand paths sit at sourceReady=false until something subscribes)
first_cam="$(echo "$paths_json" | python3 -c 'import json, sys; print(json.load(sys.stdin)["items"][0]["name"])')"
info "ffprobe rtsp://127.0.0.1:8554/$first_cam (triggers on-demand spawn)"
codec="$(ffprobe -v error -rtsp_transport tcp -timeout 15000000 -i "rtsp://127.0.0.1:8554/$first_cam" -show_streams -of json 2>/dev/null | python3 -c 'import json, sys; d=json.load(sys.stdin); print(d["streams"][0]["codec_name"])' 2>/dev/null || true)"
[[ -n "$codec" ]] || fail "ffprobe failed (check journalctl --user -u usb-rtsp)"
pass "stream codec: $codec"

# 6. snap CLI works
info "snap $first_cam"
out="/tmp/usb-rtsp-smoke-$$.jpg"
"$REPO_DIR/bin/snap" "$first_cam" "$out" >/dev/null
[[ -s "$out" ]] && file "$out" | grep -q 'JPEG image data' || fail "snap output not a JPEG"
size="$(stat -c%s "$out")"
rm -f "$out"
pass "snap saved $size bytes JPEG"

# 7. admin panel healthz
info "admin /healthz"
hz="$(curl -fsS http://127.0.0.1:8080/healthz)"
echo "$hz" | grep -q '"ok":true' || fail "healthz not ok: $hz"
pass "admin healthy"

# 8. admin /api/status returns something sensible
info "admin /api/status"
status_json="$(curl -fsS "${COOKIE_ARG[@]}" http://127.0.0.1:8080/api/status)"
echo "$status_json" | python3 -c 'import json, sys; d=json.load(sys.stdin); assert d["services"]["mediamtx"]=="active"' \
  || fail "status JSON doesn't report mediamtx active"
pass "status reports mediamtx active"

# 9. admin / dashboard renders
info "admin GET /"
curl -fsS -o /dev/null -w '%{http_code}' "${COOKIE_ARG[@]}" http://127.0.0.1:8080/ | grep -q '^200$' || fail "dashboard didn't return 200"
pass "dashboard renders"

echo
printf '\033[1;32mall checks passed\033[0m\n'
