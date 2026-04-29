# usb-rtsp

Lightweight RTSP / HLS / WebRTC server for USB cameras on a Raspberry Pi (5,
aarch64). Built on [mediamtx](https://github.com/bluenviron/mediamtx). Plug a
UVC webcam in, run `./install.sh`, and `rtsp://<host>:8554/cam0` is live in
ffplay / mpv / OBS — plus a one-page admin panel at `http://<host>:8080/`
that handles cameras, relays, firewall, public-IP tracking, plugins, and
auth without leaving the browser.

## What you get

- One RTSP / HLS / WebRTC path per detected USB camera (`cam0`, `cam1`, …)
  fed by a single H.264 encode (no re-encode tax across protocols).
- A dashboard with foldable cards per area: live cameras (with embedded
  WebRTC preview), relays (when the relay plugin is enabled), per-row QR
  codes for every stream URL, a host-mode toggle (LAN / Public / DNS) that
  rebuilds every URL on the page, active-streams table with per-session
  `kick` and per-IP `block` actions, host telemetry, and conditional cards
  for Hailo HAT, UPS HAT, and the auto-detected WebRTC public IP.
- A `/settings` page split into foldable cards: Plugins, Authentication
  (PAM panel login + rotating stream password), WebRTC public access
  (auto IP detection / DDNS), Firewall (UFW) with per-port scope toggles
  + IP/CIDR blocklist, and Service recovery (start/stop/restart per unit,
  log tail, snapshot manager).
- A `snap <cam>` CLI that saves a JPEG from any running stream.
- Two user systemd units that survive reboot via `loginctl enable-linger`.

LAN-only by default and unauthenticated; opt-in flags below take it
public-facing safely.

## Install

```sh
git clone <this-repo> ~/usb-rtsp
cd ~/usb-rtsp
./install.sh                       # base install
./install.sh --enable-auth         # optional: PAM panel login + stream password
./install.sh --enable-ufw-mgmt     # optional: NOPASSWD sudo for /usr/sbin/ufw
                                   #           (lets the panel manage UFW rules)
```

The installer asks for sudo to (a) `apt install` any missing dependencies
(`ffmpeg v4l-utils python3-fastapi python3-uvicorn python3-jinja2 python3-yaml
python3-multipart curl tar`), (b) drop the mediamtx binary into
`/usr/local/bin/`, and (c) enable systemd user-linger so the services
survive reboot. Everything else lives under `~/.config/usb-rtsp/` and
`~/.local/share/usb-rtsp/`.

It's idempotent — re-run after `git pull`, after editing `etc/profiles.yml`,
or any time you want to re-render `mediamtx.yml`. It leaves your camera /
relay configs untouched unless those files don't exist (in which case it
auto-detects USB cameras and seeds defaults).

**Recommended companion installer for Raspberry Pi 5 + AI HAT+ + UPS HAT:**
[pi-bringup](https://github.com/kyleengza/pi-bringup) brings up the Hailo
driver, UPS watchdog, and journald persistence. The dashboard's
hardware-presence cards (Hailo, UPS, throttle history) populate richest
when pi-bringup is installed first; they degrade gracefully on a bare image.
Install order: pi-bringup → reboot → usb-rtsp.

## Dashboard tour

`http://<host>:8080/` (LAN by default; see the WebRTC and UFW sections to
take it public).

| Element | What it does |
|---|---|
| **URL host: `[LAN] [Public] [DNS]`** at the top | Click to switch every URL on the page (Copy buttons, QR codes, displayed text) between the LAN IP, the auto-detected public IP, and a configured DDNS hostname. Choice persists in `localStorage`. The `Public` and `DNS` pills only appear when those hosts are known. |
| **Cameras card** (left pane) | One row per camera. Each row has a Live preview iframe (WebRTC via the panel's same-origin proxy), per-format/resolution/fps/encode/quality dropdowns, plus three URL rows (WebRTC / HLS / RTSP) each with `copy`, `open here`, and a `QR` button that pops a scannable code. |
| **Relays card** (left pane, when `relay` plugin enabled) | Same shape as cameras — pulls a remote RTSP/RTMP/HTTP source and re-broadcasts it as a local path. Optional re-encode for cross-browser-compatible H.264 baseline. |
| **Active streams** (right pane) | One row per live RTSP / WebRTC / HLS session with peer IP, transport, bytes, duration, plus per-row `kick` (terminate the mediamtx session) and `block` (UFW deny + auto-kick) actions. |
| **Host card** (right pane) | Model, kernel, uptime, LAN IP, network RX/TX, CPU load, memory, disk, CPU temperature + fan RPM, throttle/under-voltage history with a "fresh" decay window so latched bits don't stay yellow forever. **Pi PSU (USB-C) tile** shows live 5V_RAIL voltage + estimated input wattage from `vcgencmd pmic_read_adc`; tints yellow at <4.95 V, red at <4.75 V (the brown-out band). |
| **Hailo AI HAT** (conditional) | Model, firmware (cached from `pifetch` / `hailortcli`), driver, `/dev/hailo0` presence, PCIe link speed/width to the RP1 bridge, and **live NNC utilisation** (% of the chip's 26 TOPS being used) when any `hailort` app is running with `HAILO_MONITOR=1` set — the `inference` plugin's worker enables that automatically. Stale-file detection (10 s) handles SIGKILLed workers cleanly. |
| **UPS HAT** (conditional) | Battery voltage and percent, source label (`AC` / `On battery` / `On battery — low` / `⚠ HAT i2c unreachable`), watchdog service status, low-voltage cutoff, **charge direction** (charging / discharging / balanced with mA from the INA219 shunt). Battery bar colour reflects health (green near full, yellow when charging-but-low or discharging-but-ok, red when low + draining). Reads from pi-bringup's helpers when present, falls back to a direct INA219 read at `0x43`. |
| **WebRTC public access** (conditional) | Current auto-detected public IP, source (`HTTP` echo or `DNS` resolve), last detection time. Click → `/settings#webrtc-section` to configure. |

Every card has a `Hide ▴ / Show ▾` button next to its title. Choice
persists per-card so a reload preserves what you collapsed.

## URL conventions

Pick whichever your client likes — the same H.264 source feeds all three,
no re-encode tax.

| Endpoint | URL | Latency | Best for |
|---|---|---|---|
| **WebRTC** | `http://<host>:8889/<cam>/` | <1 s | Chrome / Firefox / Safari (incl. mobile). The smoothest experience; just open in a browser. |
| **HLS** | `http://<host>:8888/<cam>/index.m3u8` | ~3 s | VLC, every web browser, smart TVs. |
| **RTSP** | `rtsp://<host>:8554/<cam>` | ~200 ms | `ffplay`, `mpv`, most Android RTSP apps. **VLC's RTSP client has known compat issues with mediamtx — use HLS for VLC.** |
| Admin | `http://<host>:8080/` | n/a | The dashboard. |
| mediamtx API | `http://127.0.0.1:9997/v3/...` | n/a | Loopback only, panel-internal. |

The QR button on each URL row encodes whatever the dashboard currently
shows — combine with the host-mode toggle at the top to scan the right
host (LAN at home, Public when remote).

If a particular client is being weird with RTSP, the HLS or WebRTC URL
almost always works as a drop-in.

## Output codec (`encode` per camera)

`~/.config/usb-rtsp/usb/cameras.yml` has an `encode` field per camera,
mapped to the codec that RTSP clients actually receive. The admin panel's
"Encode" dropdown writes the same field.

| `encode` | What it does | CPU on Pi 5 (1080p30) | Compat |
|---|---|---|---|
| `h264` *(default)* | libx264 ultrafast transcode | ~70 % of one A76 core | Universal — VLC Android, every CCTV app, every player |
| `mjpeg` | RFC 2435 RTP/JPEG re-encode | ~10-15 % of one core | Desktop VLC (TCP), ffplay; flaky on Android |
| `copy` | passthrough (no encode) | ~0 % | Only safe when `format: H264` (native-H.264 webcams) |

Why H.264 by default: cheap UVC webcams produce JPEG with restart markers
and nonstandard Huffman tables that aren't RFC 2435-compliant. mediamtx
silently drops fragments → mobile players (esp. VLC Android) glitch or
disconnect. Transcoding to H.264 dodges the issue and the CPU cost on a
Pi 5 stays under one core.

## Compression knobs

The "Settings ▾" tab on each camera card exposes a **Quality** dropdown
(primary control) plus an **Advanced overrides ▾** expander for explicit
per-camera knobs. Same fields are hand-editable in
`~/.config/usb-rtsp/usb/cameras.yml`.

### Quality presets (`etc/quality-presets.yml`)

| Preset | x264 preset | Bitrate (× resolution baseline) | GOP | B-frames | MJPEG q:v | Pi 5 cost @ 1080p30 |
|---|---|---|---|---|---|---|
| `low` | ultrafast | 0.5× | 2 s | 0 | 6 | ~35 % of one core |
| `medium` *(default)* | ultrafast | 1.0× | 2 s | 0 | 3 | ~75 % of one core |
| `high` | veryfast | 1.5× | 4 s | 1 | 2 | ~120 % (1.2 cores) |

Resolution baseline (the "1.0× bitrate" target):

| Resolution | kbps |
|---|---|
| 1920×1080 | 2500 |
| 1280×720 | 1500 |
| 640×480 | 800 |
| ≤ 480p | 500 |

So `cam0 @ 1080p / quality=high` → 2500 × 1.5 = **3750 kbps**.

### Advanced overrides (per camera)

| Field | Type | When set | Effect |
|---|---|---|---|
| `bitrate_kbps` | int 100-20000 | always | Replaces `factor × baseline`. |
| `x264_preset` | enum | h264 only | Picks libx264 preset directly. Slower = tighter bitstream at the same bitrate but more CPU. |
| `gop_seconds` | int 1-10 | h264 only | Keyframe interval. Smaller = faster reconnect/seek; bigger = more efficient. |
| `bframes` | 0-3 | h264 only | B-frames. >0 auto-promotes `-profile:v` to `main` and **disables WebRTC** (mediamtx force-closes the session — WebRTC's H.264 spec is Constrained Baseline only). RTSP and HLS still work. |
| `mjpeg_qv` | int 1-31 | mjpeg only | ffmpeg `-q:v` (1 = best, 31 = worst). |

Blank in the panel / absent in `cameras.yml` = use the Quality preset's value.

## Quality / smoothing profiles

Each camera entry has a `profile` key, mapped from `etc/profiles.yml`:

| Profile | Transport | Read buffer | Use when |
|---|---|---|---|
| `smooth` | TCP | 4096 packets | WiFi viewer, prefer no stutter over latency |
| `balanced` *(default)* | TCP | 2048 packets | LAN, mostly-stable WiFi |
| `low_latency` | UDP | 512 packets | Wired LAN, tolerate frame drops for snappy response |

Switch profiles in the admin panel; mediamtx restarts the affected path.

## Going public — WebRTC over the internet

mediamtx's WebRTC media flow needs the peer to know a routable address
for the Pi. Without that, an external client completes the SDP handshake
but the ICE step fails because the only candidates advertised are LAN
host addresses.

### What the panel does for you

`/settings → WebRTC public access` runs a detection chain on admin
startup and every `refresh_minutes` (default 30):

```
public_host configured?
 ├── yes, IP literal      → use verbatim                   [source=dns]
 ├── yes, hostname        → socket.gethostbyname()         [source=dns]
 │       └── failure → fall through to HTTP
 └── no                   → curl https://ifconfig.me       [source=http]
                            └── failure → keep last cached IP
```

The result is cached at `~/.cache/usb-rtsp/public-ip` and emitted into the
rendered `mediamtx.yml` as a unified host list (mediamtx ≥ 1.18 schema):

```yaml
webrtcAdditionalHosts:
- 192.168.100.14           # LAN — listed first so on-network browsers try direct UDP
- 102.213.127.232          # public — auto-detected
webrtcICEServers2:
- url: stun:stun.l.google.com:19302
- url: stun:stun.cloudflare.com:3478
```

STUN servers (configurable via `webrtc.stun_servers` in `auth.yml`) are
advertised so off-LAN browsers and clients behind symmetric NAT can
discover server-reflexive candidates and establish ICE without relying
on the host candidate alone.

When the IP changes the lifespan task re-renders + restarts mediamtx —
**only if there are no active WebRTC viewers**. If anyone's watching, the
restart is deferred to the next tick so they don't get kicked.

The Settings form lets you set:

- `Public hostname or IP` — leave empty for `curl ifconfig.me` auto-detect,
  or fill in a DDNS hostname (e.g. `myhouse.duckdns.org`) once you have one
  set up. Hostname resolution wins over the HTTP echo.
- `IP echo URL` — defaults to `https://ifconfig.me`. Swap for `ipify`,
  `icanhazip`, a self-hosted echo, etc.
- `Refresh interval (minutes)` — default 30.
- `Auto-detect` toggle — off = only the configured host (if any) is used,
  never reaches the echo URL. Useful for fully air-gapped LAN-only.

The **Detect now** button forces an immediate re-detect.

### What you also need on the network side

1. **Forward the right ports** on your perimeter device (router / fibre
   ONT). Minimum for external WebRTC:

   | Protocol | Port | What it carries |
   |---|---|---|
   | TCP | 8889 | WebRTC signalling (HTTP + WHEP SDP exchange) |
   | UDP | 8189 | WebRTC media (RTP + RTCP) |
   | TCP | 8189 | Optional ICE-over-TCP fallback for restrictive client networks |

   Don't forward `8080` (panel) or `22` (SSH) externally without thinking
   carefully — UFW keeps both LAN-only by default in the panel's managed
   list.

2. **Flip UFW to Anywhere for the WebRTC group.** `/settings → Firewall (UFW)`
   has an `all anywhere` button on each protocol group; clicking it on
   the WebRTC group recreates IPv4+IPv6 Anywhere allow rules for the
   three ports above. Or set them individually with the per-port pills.

3. **Confirm `webrtcICEHostNAT1To1IPs` in the rendered config:**

   ```sh
   grep -A1 webrtcICEHostNAT1To1 ~/.config/usb-rtsp/mediamtx.yml
   ```

After all three steps, journal lines on a successful external connection
look like:

```
[WebRTC] [session XXX] created by <external_ip>:NN
[WebRTC] [session XXX] peer connection established,
         local candidate: host/udp/<your_public_ip>/8189,
         remote candidate: ...
[WebRTC] [session XXX] is reading from path 'cam0', 1 track (H264)
```

The previous failure mode was `closed: deadline exceeded while waiting
connection` — that disappears once the public host candidate is a real
routable address.

### Caveats

- **CGNAT.** Some ISPs (especially mobile + cheaper fibre) NAT every
  customer behind a shared address — port forwards on your "public" IP
  go nowhere because that IP is shared. Confirm by comparing the IP your
  Pi reports (`curl ifconfig.me`) against the WAN IP your router shows.
  If they differ, you're on CGNAT and need either an ISP request, a
  TURN server, or a tunnel like Tailscale.
- **IPv6.** mediamtx supports IPv6 natively, but the panel's auto-detect
  only writes the first IPv4 it gets. If you have native IPv6 and want
  to advertise both, hand-edit `mediamtx.yml`'s `webrtcICEHostNAT1To1IPs`
  list to include both.
- **NAT hairpin / split-horizon DNS.** A single hostname that resolves
  differently on LAN vs. internet (e.g. via Pi-hole or dnsmasq) avoids
  the "URL works at home but not from cellular" trap, since most
  consumer routers don't loop port-forwarded traffic back to LAN clients
  cleanly.

## Host firewall (UFW) management

Built on top of UFW, scoped narrowly. The panel can change rules for the
ports usb-rtsp owns, plus maintain an IP/CIDR blocklist and view the rest
read-only. Free-form rule editing isn't exposed — one wrong CIDR via a
web form is too easy a way to lock yourself out of SSH.

### Enable

```sh
./install.sh --enable-ufw-mgmt
```

That writes `/etc/sudoers.d/usb-rtsp-ufw` granting NOPASSWD for
`/usr/sbin/ufw` to the install user (validated through `visudo -cf`
before installing). Without that file the panel renders the firewall
section read-only and asks you to run the command.

`./install.sh --disable-ufw-mgmt` removes the sudoers file.

### What the panel manages

`/settings → Firewall (UFW)` shows the ports usb-rtsp owns, grouped:

| Group | Ports |
|---|---|
| **Admin** | `8080/tcp` (panel) — `⚠ panel port`, confirmation required to take it Off |
| **RTSP** | `8554/tcp` (control), `8554/udp`, `8000/udp`, `8001/udp` (RTP transport) |
| **HLS** | `8888/tcp` |
| **WebRTC** | `8889/tcp` (signalling), `8189/udp` (media), `8189/tcp` (ICE fallback) |

Each row has a 3-state pill: **LAN** (allow from your /24), **Anywhere**
(open to the internet), **Off** (insert an explicit DENY rule — visible
in the rule list rather than relying on default-deny). Per-group bulk
buttons (`all lan` / `all anywhere` / `all off`) flip the entire protocol
family at once.

Below the managed-port tables: a fully-editable IP blocklist (insert at
position 1 so deny preempts every later allow rule) with foot-gun guards:

- Refuses to block loopback, your LAN /24, the IP making the request,
  and any CIDR wider than `/8`.
- Block from the active-streams row also kicks any matching mediamtx
  session in the same call — UFW alone only blocks new packets, the kick
  also tears down whatever's still flowing through conntrack.

The "Other rules" section at the bottom shows everything else
(SSH-from-LAN, custom rules) read-only with a `×` per row to delete by
rule number.

### Active streams: kick vs block

- **kick** — terminates that one session in mediamtx
  (`POST /v3/<kind>/kick/<id>`). Firewall untouched. Useful when you
  changed UFW scope mid-stream and the row is dangling because UDP
  conntrack is keeping the dead flow tracked.
- **block** — UFW deny rule from the peer IP + auto-kick the session.
  Firewall preempts any future connection from that IP.

## Authentication (optional)

Default: panel and streams are open on the LAN. Lock them down with:

```sh
./install.sh --enable-auth
```

What that does:

1. Installs `python3-pam` if missing.
2. Drops `/etc/pam.d/usb-rtsp-admin` (one-time sudo) that includes
   `common-auth` + `common-account` — the panel logs you in against
   this Pi's user accounts.
3. Generates a 24-byte URL-safe random password for the stream user
   (default username `stream`), stored 0600 at
   `~/.config/usb-rtsp/.stream-pass`.
4. Writes `~/.config/usb-rtsp/auth.yml` enabling both layers.
5. Prints the stream credentials at the end.

After that:

| Layer | How you log in |
|---|---|
| Panel (`:8080`) | Login page asks for your Pi username + password (PAM). Session cookie 7 days. The header shows a `🔒 username` pill + `log out` button. |
| RTSP (`:8554`) | URL becomes `rtsp://stream:PASSWORD@host:8554/cam0` |
| HLS (`:8888`) | Browser pops basic-auth dialog; or `http://stream:PASSWORD@host:8888/cam0/index.m3u8` |
| WebRTC (`:8889`) | Same — basic-auth dialog or embedded creds |

Loopback is exempt — the local ffmpeg publisher doesn't need creds, and
`snap` keeps working as-is from the Pi itself.

`/settings → Authentication` has manual rotate ("disconnects every active
client; dashboard URLs reload automatically") and an auto-rotate toggle
that wires up `usb-rtsp-rotate.timer` on a `daily` / `weekly` / `monthly`
schedule.

To turn it back off:

```sh
./install.sh --disable-auth
```

This removes `auth.yml` (panel + mediamtx revert to open) but leaves
`/etc/pam.d/usb-rtsp-admin` and the stream password file in place;
re-running `--enable-auth` reuses them.

## Plugins

usb-rtsp ships with the **usb** plugin bundled. Other plugins live as
user-installed packages under `~/.local/share/usb-rtsp/plugins/<name>/`:

```sh
./install.sh --list-plugins                         # bundled vs user
./install.sh --add-plugin <git-url-or-local-path>   # clone or copy
./install.sh --remove-plugin <name>                 # rm user dir
./install.sh --enable-plugin <name>                 # add to enabled set
./install.sh --disable-plugin <name>                # remove
```

The same actions surface in `/settings → Plugins`. The Plugins section is
itself a foldable card showing `N enabled · M disabled` in its header;
expand to see per-plugin sub-cards with the toggle switch + `Details ▾`.

Bundled plugins can never be uninstalled via the panel or API.

### Optional plugins

Two first-party plugins live in their own repos so they install à la carte:

| Plugin | Repo | What it does |
|---|---|---|
| `relay` | [`kyleengza/usb-rtsp-plugin-relay`](https://github.com/kyleengza/usb-rtsp-plugin-relay) | Pull a remote RTSP/RTMP/HTTP stream and re-broadcast as a local path. Optional re-encode (cross-browser-compatible H.264 baseline). |
| `inference` | [`kyleengza/usb-rtsp-plugin-inference`](https://github.com/kyleengza/usb-rtsp-plugin-inference) | Run object detection (Hailo or CPU) on any mediamtx path; republish annotated frames as `<source>-ai`; record event-triggered clips. One-click toggle per source on the settings page. |

Install via the panel (Settings → Plugins → **Add plugin ▾**) or CLI:

```sh
./install.sh --add-plugin git@github.com:kyleengza/usb-rtsp-plugin-relay.git
./install.sh --add-plugin git@github.com:kyleengza/usb-rtsp-plugin-inference.git
# (or the https://github.com/... form if no SSH key on the box)
./install.sh --enable-plugin relay
./install.sh --enable-plugin inference
```

After enabling, the install endpoint schedules an admin restart. The
plugin's section appears in the dashboard's left pane (with the same
QR / host-toggle / fold treatment as cameras) and its config form lives
at `/settings#plugin-<name>`.

### Developing a plugin

```sh
git clone git@github.com:kyleengza/usb-rtsp-plugin-relay.git ~/dev/relay
ln -s ~/dev/relay ~/.local/share/usb-rtsp/plugins/relay   # live edits
systemctl --user restart usb-rtsp-admin
```

Or `./install.sh --add-plugin ~/dev/relay` for a one-shot snapshot copy.

The plugin contract is one Python package with up to five entry-points:

```
<plugin-repo-root>/
├── manifest.yml              name, description, version, default_enabled, order
├── __init__.py               register(app, ctx),
│                              section_context(ctx, request) -> dict,
│                              list_inputs(ctx) -> [{name, enabled, label}]
├── render.py                 render_paths(ctx) -> {path: mediamtx-cfg}
├── api.py                    FastAPI APIRouter (mounted at /api/<name>/...)
├── templates/section.html    Jinja partial for the dashboard
├── templates/settings.html   Jinja partial for /settings (optional)
└── static/<name>.js          per-plugin JS, served at /static/<name>/
```

The loader puts the main `usb-rtsp` repo on `sys.path` before importing,
so `from core.helpers import …` resolves regardless of where the plugin
lives on disk. URL rows in `templates/section.html` should use
`data-url-pre="..."` / `data-url-suf="..."` on each `<li>` (and
`<code data-url-display>` on the `<code>`) so the dashboard's host-mode
toggle can rewrite them.

## Day-to-day

| Action | Command |
|---|---|
| Open admin panel | `xdg-open http://$(hostname).local:8080/` |
| Take a snapshot | `snap cam0` (default → `~/.config/usb-rtsp/snapshots/<cam>-TIMESTAMP.jpg`) |
| Take a snapshot to a path | `snap cam0 /tmp/x.jpg` |
| List configured cams | `snap` |
| View live mediamtx logs | `journalctl --user -u usb-rtsp -f` |
| View live admin logs | `journalctl --user -u usb-rtsp-admin -f` |
| Restart the server | `systemctl --user restart usb-rtsp` |
| Restart the panel | `systemctl --user restart usb-rtsp-admin` |
| Re-render config | `python3 -m core.renderer` (from repo dir) |
| Re-detect cameras | `usb-rtsp-detect` (prints JSON of available formats) |
| Manage UFW from CLI | `sudo ufw status numbered` (after `--enable-ufw-mgmt`) |

## Project layout

```
~/usb-rtsp/
├── bin/
│   ├── snap                    # snapshot CLI (bash)
│   ├── usb-rtsp-detect         # enumerate cameras → JSON (python)
│   └── usb-rtsp-render         # legacy entry; calls core.renderer
├── core/
│   ├── auth.py                 # PAM + cookie signing + auth.yml schema
│   ├── helpers.py              # mediamtx api_get/api_post, systemctl, fmt
│   ├── loader.py               # plugin discovery / register / sys.path injection
│   ├── public_ip.py            # NAT1To1 detection (DNS resolve → curl ifconfig.me)
│   ├── renderer.py             # writes ~/.config/usb-rtsp/mediamtx.yml
│   └── ufw.py                  # status parser + scope toggle + blocklist
├── admin/
│   ├── app.py                  # FastAPI single-file admin panel
│   ├── templates/
│   │   ├── index.html          # dashboard
│   │   ├── settings.html       # foldable settings cards
│   │   └── login.html          # PAM login (when auth enabled)
│   └── static/
│       ├── app.js              # dashboard logic (sessions, host, QR, fold, host-toggle)
│       ├── settings.js         # plugins / auth / webrtc / ufw forms
│       ├── style.css
│       ├── favicon.svg
│       └── vendor/qrcode.min.js  # vendored qrcode-generator (MIT)
├── plugins/
│   └── usb/                    # bundled USB camera plugin
├── etc/
│   ├── profiles.yml
│   └── quality-presets.yml
├── systemd/
│   ├── usb-rtsp.service        # mediamtx
│   └── usb-rtsp-admin.service  # uvicorn admin/app.py
├── tests/smoke.sh
├── install.sh    # idempotent
└── uninstall.sh  # add --purge to also drop ~/.config/usb-rtsp
```

Runtime files (created by `install.sh`, not in git):

```
~/.config/usb-rtsp/
├── usb/cameras.yml             # source of truth for the usb plugin
├── mediamtx.yml                # rendered output; never hand-edit
├── auth.yml                    # panel + streams + webrtc settings
├── .cookie-secret              # 0600, per-host, signs panel session cookies
├── .stream-pass                # 0600, the rotating stream password
└── snapshots/                  # default snap output

~/.cache/usb-rtsp/
└── public-ip                   # last successful NAT1To1 detection

~/.local/share/usb-rtsp/plugins/
└── <name>/                     # user-installed plugins
```

System-wide files (sudo):

```
/usr/local/bin/mediamtx         # the binary
/etc/pam.d/usb-rtsp-admin       # if --enable-auth
/etc/sudoers.d/usb-rtsp-ufw     # if --enable-ufw-mgmt
~/.config/systemd/user/usb-rtsp{,-admin,-rotate.timer}.service
```

## Adding / swapping cameras

Plug a new USB camera in and either:

- Click **Rescan cameras** in the admin panel — it adds the new device with
  sensible defaults (max-resolution MJPG@30) and reloads.
- Or run `./install.sh` again — it leaves your existing
  `~/.config/usb-rtsp/usb/cameras.yml` alone, so you'll need to edit it by
  hand or click rescan.

To remove a camera, edit `~/.config/usb-rtsp/usb/cameras.yml` and remove
its block, then `python3 -m core.renderer && systemctl --user restart usb-rtsp`.

## Troubleshooting

**External WebRTC: SDP exchange completes, then session closes with
`deadline exceeded while waiting connection`.** mediamtx's SDP answer
isn't advertising a candidate the peer can route to. Check
`grep webrtcAdditionalHosts ~/.config/usb-rtsp/mediamtx.yml` — if the
public IP isn't there, the auto-detect failed; click **Detect now** in
the panel or set a `Public hostname or IP` manually. If both LAN and
public are listed but ICE still fails for off-LAN clients, the panel
also advertises STUN servers (Google + Cloudflare by default) so the
browser can gather server-reflexive candidates — that path covers
client-isolated networks. If it still fails, verify your perimeter
forwards UDP 8189 (and TCP 8889) to the Pi, and that UFW has those
ports set to **Anywhere** in the panel.

**Preview iframe fails to start, repeated WHEP DELETE 404s.** Likely a
Firefox bfcache issue with the iframe's WebRTC client retry timer
surviving an iframe-src navigation. The relay/inference plugins
replace the iframe element on fold-close to defeat this; if you see
it on a custom integration, set `iframe.src = "about:blank"` *and*
remove the iframe from the DOM (don't just hide it).

**Stream froze on the phone after I changed UFW scope, but the active
session row stayed up.** UFW filters new packets at the kernel boundary;
existing UDP conntrack entries keep being tracked even after the rule
change. mediamtx doesn't realise the connection is dead until either
conntrack times the flow out (~30 s for UDP) or the peer marks the
WebRTC connection dead and emits a goodbye. Click `kick` on the row to
release the session immediately without touching the firewall.

**I locked myself out of the panel by toggling 8080/tcp to Off.** From a
LAN terminal: `sudo ufw insert 1 allow from 192.168.100.0/24 to any port 8080 proto tcp comment 'usb-rtsp admin'`
(adjust your `/24`). The panel will be reachable again, then re-toggle
through the UI.

**Pi reboots / brownouts under sustained inference load.** PSU + cable
combination undersized for `ffmpeg` + Hailo + concurrent encodes peaks.
Watch `vcgencmd get_throttled` for `0x50000` (under-voltage + throttle
since boot). Best fix is hardware: a Pi 5 official 5 V / 5 A supply
plugged directly into the Pi USB-C, charging the UPS HAT separately
via its own USB-C input — the Pi stays clean while the HAT keeps the
backup pack topped up. Pi-bringup's UPS watchdog ≥ commit `e9c40b1`
honours `RESPECT_PI_RAIL=1` so a low UPS battery won't shut down a
Pi that has its own healthy supply.

**VLC fails with "Connection failed" / "Unable to open the MRL".** VLC's
RTSP client sends `SETUP` against the path URL instead of the per-track
URL that mediamtx (gortsplib) expects. **Use the HLS URL in VLC instead**
— `http://<host>:8888/<cam>/index.m3u8`. It works in every VLC version,
every smart-TV, and every browser. Latency goes up to ~3 s but nothing
else changes.

For everything else, `ffplay rtsp://<host>:8554/<cam>` is the canary —
ffplay follows RFC and works whenever the stream is healthy.

**Phone player drops the stream after a few seconds.** Bandwidth or
decode ceiling. Try: (a) open the WebRTC URL in the phone's browser
instead of an RTSP app — `http://<host>:8889/<cam>/`, (b) lower
resolution / fps in the panel, (c) if you must use RTSP, force TCP
transport in the player to avoid UDP packet-loss death spiral on weak
WiFi.

**mediamtx fails to start, says "device busy".** Something else is
holding `/dev/video0`. Likely candidates: another mediamtx, `motion`, an
open `vlc` session via v4l2 instead of RTSP. `lsof /dev/video0` finds
the culprit.

**Admin panel says `mediamtx api unreachable`.** Check
`systemctl --user status usb-rtsp` and
`journalctl --user -u usb-rtsp -n 50`. The most common cause is a
malformed `mediamtx.yml`; re-running `python3 -m core.renderer`
regenerates it cleanly.

**RTSP works locally but not from another LAN host.** Confirm the listener:
`ss -lntp | grep 8554` (should show `*:8554`). Confirm UFW for that port
isn't set to LAN-only with a different /24 than your client. Confirm
your home router doesn't have client isolation enabled.

**ffmpeg "Invalid argument" on YUYV format.** Some cameras lie about
which sizes they support for YUYV. Switch to MJPG in the panel — more
reliable and lower CPU.

**The panel auth-bar (`🔒 user / log out`) was missing.** Hard-refresh
(Ctrl-Shift-R). The panel relies on `/api/auth/state` to populate the
header pill; older cached JS may have lacked the cookie-verification
fix. If it persists, check `journalctl --user -u usb-rtsp-admin -n 20`
for traceback.

## Uninstall

```sh
./uninstall.sh           # remove services, binary, symlinks (keep config + snaps)
./uninstall.sh --purge   # also remove ~/.config/usb-rtsp/
```

The sudoers file at `/etc/sudoers.d/usb-rtsp-ufw` (if you ran
`--enable-ufw-mgmt`) is left in place — `sudo rm /etc/sudoers.d/usb-rtsp-ufw`
to remove. Same for `/etc/pam.d/usb-rtsp-admin` and
`/usr/local/bin/mediamtx`.
