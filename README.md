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

sudo apt install ffmpeg v4l-utils python3-fastapi python3-uvicorn \
                 python3-jinja2 python3-yaml python3-multipart curl tar

./install.sh
```

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

- **RTSP:**  `rtsp://<host>:8554/<cam>`
- **Admin:** `http://<host>:8080/`
- **mediamtx control API:** `http://127.0.0.1:9997/v3/...` (loopback only — used
  by the admin panel; not exposed to the LAN)

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

## Uninstall

```sh
./uninstall.sh           # remove services, binary, symlinks (keep config + snaps)
./uninstall.sh --purge   # also remove ~/.config/usb-rtsp/
```
