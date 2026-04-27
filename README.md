# usb-rtsp

Lightweight RTSP server for USB cameras on a Raspberry Pi (5, aarch64). Built on
[mediamtx](https://github.com/bluenviron/mediamtx). Plug a UVC webcam in, run
`./install.sh`, and `rtsp://<host>:8554/cam0` is live in VLC / ffplay / OBS.

## What you get

- One RTSP path per detected USB camera, auto-named `cam0`, `cam1`, …
- Native MJPEG passthrough where the camera supports it (≈0% CPU re-encode)
- A `snap <cam>` CLI that saves a JPEG from any running stream
- A tiny admin panel at `http://<host>:8080/` for resolution / fps / format /
  buffering profile per camera, plus live viewer count, active session table,
  and one-click service-recovery buttons (rescan, restart, log tail, snapshot)
- Two user systemd units that survive reboot via `loginctl enable-linger`

LAN-only by default, no auth — flip on mediamtx's `authInternalUsers` block in
`bin/usb-rtsp-render` when you need it.

## Install

```sh
git clone <this-repo> ~/usb-rtsp
cd ~/usb-rtsp
./install.sh
```

The installer asks for sudo once to (a) `apt install` any missing dependencies
(`ffmpeg v4l-utils python3-fastapi python3-uvicorn python3-jinja2 python3-yaml python3-multipart curl tar`),
(b) drop the mediamtx binary into `/usr/local/bin/`, and (c) enable systemd
user-linger so the services survive reboot. Everything else lives in
`~/.config/`.

The installer is idempotent — re-run after `git pull`, after editing
`etc/profiles.yml`, or any time you want to re-render `mediamtx.yml` from
`~/.config/usb-rtsp/cameras.yml`. It leaves your `cameras.yml` untouched
unless that file doesn't exist (in which case it auto-detects).

## Day-to-day

| Action | Command |
|---|---|
| Open admin panel | `xdg-open http://$(hostname).local:8080/` |
| Take a snapshot | `snap cam0`  (default → `~/.config/usb-rtsp/snapshots/<cam>-TIMESTAMP.jpg`) |
| Take a snapshot to a path | `snap cam0 /tmp/x.jpg` |
| List configured cams | `snap` |
| View live logs | `journalctl --user -u usb-rtsp -f` |
| Restart the server | `systemctl --user restart usb-rtsp` |
| Restart the panel | `systemctl --user restart usb-rtsp-admin` |
| Re-render config | `usb-rtsp-render` |
| Re-detect cameras | `usb-rtsp-detect` (prints JSON of available formats) |

## URL conventions

Pick whichever your client likes — the same H.264 source feeds all three, no
re-encode tax.

| Endpoint | URL | Latency | Best for |
|---|---|---|---|
| **RTSP** | `rtsp://<host>:8554/<cam>` | ~200 ms | `ffplay`, `mpv`, most Android RTSP apps. **VLC's RTSP client has known compat issues with mediamtx — use HLS for VLC.** |
| **HLS**  | `http://<host>:8888/<cam>/index.m3u8` | ~3 s | VLC, every web browser, smart TVs, every "any-codec" player |
| **WebRTC** | `http://<host>:8889/<cam>/` | <1 s | Chrome/Firefox/Safari (incl. mobile) — open in a browser, no app needed |
| Admin | `http://<host>:8080/` | n/a | The dashboard |
| mediamtx API | `http://127.0.0.1:9997/v3/...` | n/a | loopback only, panel-internal |

If a client is being weird with RTSP, the HLS or WebRTC URL almost always
works as a drop-in.

## Output codec (`encode` per camera)

`cameras.yml` has an `encode` field per camera, mapped to the codec that
RTSP clients actually receive. The admin panel's "Encode" dropdown writes
the same field.

| `encode` | What it does | CPU on Pi 5 (1080p30) | Compat |
|---|---|---|---|
| `h264` *(default)* | libx264 ultrafast transcode | ~70 % of one A76 core | Universal — VLC Android, every CCTV app, every player |
| `mjpeg` | RFC 2435 RTP/JPEG re-encode | ~10-15 % of one core | Desktop VLC (TCP), ffplay; flaky on Android |
| `copy` | passthrough (no encode) | ~0 % | Only safe when `format: H264` (native-H.264 webcams) |

Why H.264 by default: cheap UVC webcams produce JPEG with restart markers
and nonstandard Huffman tables that aren't RFC 2435-compliant. mediamtx
silently drops fragments → mobile players (esp. VLC Android) glitch or
disconnect after one frame. Transcoding to H.264 dodges the issue entirely
and the CPU cost on a Pi 5 stays under one core. Use `mjpeg` if your camera
is known clean *and* you only consume from desktop ffplay/VLC.

## Compression knobs

The admin panel exposes a **Quality** dropdown (primary control) plus an
**Advanced overrides ▾** expander for explicit knobs. The same fields are
hand-editable in `~/.config/usb-rtsp/cameras.yml`.

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
| `x264_preset` | enum | h264 only | Picks libx264 preset directly. Higher (slower) = tighter bitstream at the same bitrate but more CPU. |
| `gop_seconds` | int 1-10 | h264 only | Keyframe interval. Smaller = faster reconnect/seek, larger stream; bigger = more efficient compression. |
| `bframes` | 0-3 | h264 only | B-frames. >0 auto-promotes `-profile:v` to `main` and **disables WebRTC viewers** (mediamtx force-closes the session — WebRTC's H.264 spec is Constrained Baseline only). RTSP and HLS still work. Leave at 0 unless you don't need WebRTC. |
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

### VLC playback tips

If VLC stutters on WiFi, increase its network cache:

```sh
vlc --network-caching=500 rtsp://pitato.local:8554/cam0
```

Or in the GUI: Tools → Preferences → Show settings *All* → Input/Codecs →
Network caching ≥ 500 ms.

## Project layout

```
~/usb-rtsp/
├── bin/
│   ├── snap                 # snapshot CLI (bash)
│   ├── usb-rtsp-detect      # enumerate cameras → JSON (python)
│   └── usb-rtsp-render      # render mediamtx.yml from cameras.yml (python)
├── admin/
│   ├── app.py               # FastAPI single-file admin panel
│   ├── templates/index.html # one-page dashboard (Jinja)
│   └── static/{style.css, app.js}
├── etc/
│   ├── cameras.example.yml  # documented schema
│   └── profiles.yml         # quality profiles
├── systemd/
│   ├── usb-rtsp.service       # runs mediamtx
│   └── usb-rtsp-admin.service # runs uvicorn admin/app.py
├── tests/smoke.sh
├── install.sh    # idempotent
└── uninstall.sh  # add --purge to also drop ~/.config/usb-rtsp
```

Runtime files (created by `install.sh`, not in git):

```
~/.config/usb-rtsp/
├── cameras.yml      # source of truth — hand-edit or use the panel
├── mediamtx.yml     # rendered output; never hand-edit
└── snapshots/       # default snap output
```

## Adding / swapping cameras

Plug a new USB camera in and either:

- Click **Rescan cameras** in the admin panel — it adds the new device with
  sensible defaults (max-resolution MJPG@30) and reloads.
- Or run `./install.sh` again — it leaves your existing `cameras.yml` alone, so
  you'll need to edit it by hand or click rescan.

To remove a camera, edit `~/.config/usb-rtsp/cameras.yml` and remove its block,
then `usb-rtsp-render && systemctl --user restart usb-rtsp`. (A future panel
update will add a delete button.)

## Troubleshooting

**VLC fails with "Connection failed" / "Unable to open the MRL".** VLC's RTSP
client sends `SETUP` against the path URL instead of the per-track URL that
mediamtx (gortsplib) expects, so mediamtx rejects with
`invalid SETUP path` regardless of transport. **Use the HLS URL in VLC
instead** — `http://<host>:8888/<cam>/index.m3u8`. It works in every VLC
version, every smart-TV, and every browser. Latency goes up to ~3 s but
nothing else changes.

For everything else, `ffplay rtsp://<host>:8554/<cam>` is the canary —
ffplay follows RFC and works whenever the stream itself is healthy. If
ffplay works but a particular player doesn't, that player has its own
RTSP quirk; reach for HLS or WebRTC.

**Phone player drops the stream after a few seconds.** Bandwidth or decode
ceiling. Try in this order: (a) open the WebRTC URL in the phone's
browser instead of an RTSP app — `http://<host>:8889/<cam>/` — that
sidesteps the issue, (b) lower the resolution / fps in the admin panel,
(c) if you must use RTSP, force TCP transport in the player's settings
(prevents UDP packet-loss death-spiral on weak WiFi).

**mediamtx fails to start, says "device busy".** Something else is holding
`/dev/video0`. Likely candidates: another mediamtx, `motion`, an open `vlc`
session via v4l2 instead of RTSP. `lsof /dev/video0` finds the culprit.

**Admin panel says `mediamtx api unreachable`.** Check
`systemctl --user status usb-rtsp` and `journalctl --user -u usb-rtsp -n 50`.
The most common cause is a malformed `mediamtx.yml`; re-running
`usb-rtsp-render` regenerates it cleanly.

**RTSP works locally but not from another LAN host.** Confirm the listener:
`ss -lntp | grep 8554` (should show `*:8554`). Check your home router doesn't
have client isolation enabled.

**ffmpeg "Invalid argument" on YUYV format.** Some cameras lie about which
sizes they support for YUYV. Switch to MJPG in the panel — it's more reliable
and lower CPU anyway.

## Authentication (optional)

Default: panel and streams are open on the LAN. Lock them down with:

```sh
./install.sh --enable-auth
```

What that does:

1. Installs `python3-pam` if missing.
2. Drops a small PAM service file at `/etc/pam.d/usb-rtsp-admin` (one-time
   sudo) that includes `common-auth` + `common-account` — i.e. the
   panel logs you in against this Pi's user accounts.
3. Generates a 24-byte URL-safe random password for the stream user
   (default username `stream`), stored 0600 at
   `~/.config/usb-rtsp/.stream-pass`.
4. Writes `~/.config/usb-rtsp/auth.yml` enabling both layers.
5. Prints the stream credentials at the end.

After that:

| Layer | How you log in |
|---|---|
| Panel (`:8080`) | Login page asks for your Pi username + password (PAM). Session cookie lasts 7 days. |
| RTSP (`:8554`) | URL becomes `rtsp://stream:PASSWORD@host:8554/cam0` |
| HLS (`:8888`) | Browser pops basic-auth dialog; or `http://stream:PASSWORD@host:8888/cam0/index.m3u8` |
| WebRTC (`:8889`) | Same — basic-auth dialog or embedded creds |

Loopback is exempt — the local ffmpeg publisher doesn't need creds, and
`snap` keeps working as-is from the Pi itself.

To turn it back off:

```sh
./install.sh --disable-auth
```

This removes `auth.yml` (so the panel + mediamtx revert to open) but
leaves `/etc/pam.d/usb-rtsp-admin` and the stream password file in
place; re-running `--enable-auth` reuses them. Manually
`sudo rm /etc/pam.d/usb-rtsp-admin && rm ~/.config/usb-rtsp/.stream-pass`
to fully purge.

## Uninstall

```sh
./uninstall.sh           # remove services, binary, symlinks (keep config + snaps)
./uninstall.sh --purge   # also remove ~/.config/usb-rtsp/
```
