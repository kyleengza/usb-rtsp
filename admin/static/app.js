// usb-rtsp admin panel — core JS.
// Polls /api/status, /api/sessions, /api/host. Wires up the auth bar,
// service-recovery row, log viewer, snapshot list. Plugin JS lives in
// /static/<plugin>/<plugin>.js — those files handle their own per-card
// behaviour.

const POLL_MS = 3000;

const $ = (sel, ctx = document) => ctx.querySelector(sel);
const $$ = (sel, ctx = document) => Array.from(ctx.querySelectorAll(sel));


// ─── helpers exposed to plugin JS ──────────────────────────────────────────

// QR-code modal — vendored qrcode-generator (window.qrcode) renders an SVG
// for the URL the user already sees on screen. Plugin JS calls
// window.showQrModal(url, label) from per-row buttons; the modal is
// lazy-built once and reused.
window.showQrModal = function showQrModal(url, label) {
  if (typeof window.qrcode !== "function") {
    alert("QR library not loaded — try a hard refresh (Ctrl+Shift+R).");
    return;
  }
  let modal = document.getElementById("qr-modal");
  if (!modal) {
    modal = document.createElement("div");
    modal.id = "qr-modal";
    modal.hidden = true;
    modal.innerHTML = `
      <div class="qr-modal-backdrop" data-qr-close></div>
      <div class="qr-modal-card" role="dialog" aria-modal="true" aria-labelledby="qr-modal-label">
        <div class="qr-modal-head">
          <span class="qr-modal-label" id="qr-modal-label"></span>
          <button type="button" class="qr-modal-close" data-qr-close aria-label="close">×</button>
        </div>
        <div class="qr-modal-svg"></div>
        <div class="qr-modal-url"></div>
        <div class="qr-modal-hint">scan with your phone's camera or RTSP/HLS app</div>
      </div>
    `;
    document.body.appendChild(modal);
    const close = () => { modal.hidden = true; };
    modal.querySelectorAll("[data-qr-close]").forEach(el => el.addEventListener("click", close));
    document.addEventListener("keydown", e => {
      if (!modal.hidden && e.key === "Escape") close();
    });
  }
  // QR cell-size scales inversely with payload — mediamtx URLs with
  // creds can be ~80 chars, default cellSize=4 keeps total ~250px.
  let qr;
  try {
    qr = window.qrcode(0, "M");
    qr.addData(url);
    qr.make();
  } catch (err) {
    alert("QR encode failed: " + (err && err.message ? err.message : err));
    return;
  }
  modal.querySelector(".qr-modal-svg").innerHTML = qr.createSvgTag({ cellSize: 5, margin: 2, scalable: true });
  modal.querySelector(".qr-modal-label").textContent = label || "URL";
  modal.querySelector(".qr-modal-url").textContent = url;
  modal.hidden = false;
};


// ─── URL host mode (LAN / Public / DNS) ────────────────────────────────────
// Each URL row carries data-url-pre + data-url-suf; the host fills the gap.
// Switching mode rebuilds the visible <code>, the copy button's data-copy,
// and the QR button's data-qr-url in place.

const HOST_MODE_KEY = "usb-rtsp-host-mode";

function applyHostMode(mode) {
  const hosts = window.__USB_RTSP_HOSTS__ || {};
  const host = hosts[mode];
  if (!host) return false;
  document.querySelectorAll(".url-row[data-url-pre]").forEach(li => {
    const pre = li.dataset.urlPre || "";
    const suf = li.dataset.urlSuf || "";
    const url = pre + host + suf;
    const codeEl = li.querySelector("[data-url-display]") || li.querySelector("code");
    if (codeEl) codeEl.textContent = url;
    const copyBtn = li.querySelector(".copy[data-copy]");
    if (copyBtn) copyBtn.dataset.copy = url;
    const qrBtn = li.querySelector(".qr-btn[data-qr-url]");
    if (qrBtn) qrBtn.dataset.qrUrl = url;
  });
  document.querySelectorAll("#host-toggle button[data-host-mode]").forEach(b => {
    b.classList.toggle("active", b.dataset.hostMode === mode);
  });
  try { localStorage.setItem(HOST_MODE_KEY, mode); } catch {}
  return true;
}

function initialHostMode() {
  const hosts = window.__USB_RTSP_HOSTS__ || {};
  let saved = null;
  try { saved = localStorage.getItem(HOST_MODE_KEY); } catch {}
  if (saved && hosts[saved]) return saved;
  const cur = hosts.current;
  for (const m of ["lan", "public", "dns"]) {
    if (hosts[m] && hosts[m] === cur) return m;
  }
  for (const m of ["lan", "public", "dns"]) {
    if (hosts[m]) return m;
  }
  return null;
}

function wireHostToggle() {
  const buttons = document.querySelectorAll("#host-toggle button[data-host-mode]");
  if (!buttons.length) return;
  buttons.forEach(b => b.addEventListener("click", () => applyHostMode(b.dataset.hostMode)));
  const m = initialHostMode();
  if (m) applyHostMode(m);
}


window.copyText = async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); return true; } catch {}
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  ta.style.pointerEvents = "none";
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand("copy"); } catch {}
  document.body.removeChild(ta);
  return ok;
};

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function setBadge(id, kind, text) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove("ok", "warn", "err");
  el.classList.add(kind);
  el.textContent = text;
}


// ─── status header ─────────────────────────────────────────────────────────

async function refreshStatus() {
  let s;
  try { s = await fetch("/api/status").then(r => r.json()); }
  catch { setBadge("svc-mediamtx", "err", "mediamtx ?"); return; }

  setBadge("svc-mediamtx", s.services.mediamtx === "active" ? "ok" : "err",
           `mediamtx ${s.services.mediamtx}`);
  setBadge("svc-admin", s.services.admin === "active" ? "ok" : "warn",
           `admin ${s.services.admin}`);
  const agg = $("#agg");
  if (agg) {
    agg.textContent =
      `paths: ${s.paths.ready}/${s.paths.total} ready · ${s.paths.readers} viewers · ↑ ${s.paths.bytes_received_h}`;
  }
}


// ─── auth bar ──────────────────────────────────────────────────────────────

async function refreshAuthBar() {
  try {
    const r = await fetch("/api/auth/state");
    const j = await r.json();
    const bar = $("#auth-bar");
    if (!bar) return;
    if (j.panel_enabled && j.authenticated) {
      bar.hidden = false;
      $("#auth-user").textContent = `${j.user}`;
    } else {
      bar.hidden = true;
    }
  } catch {}
}


// ─── active streams table (RTSP / WebRTC / HLS merged) ─────────────────────

// Pull just the IP out of "1.2.3.4:55678" / "[::1]:80" / "(HTTP poll)".
// Returns "" if not a real IP — used to gate the per-row block button.
function _peerIp(remoteAddr) {
  if (!remoteAddr || remoteAddr.startsWith("(")) return "";
  if (remoteAddr.startsWith("[")) {
    const close = remoteAddr.indexOf("]");
    return close > 0 ? remoteAddr.slice(1, close) : "";
  }
  const colon = remoteAddr.lastIndexOf(":");
  return colon > 0 ? remoteAddr.slice(0, colon) : remoteAddr;
}

// True if the IP is something we'd let the user block. Mirrors the
// server-side is_blockable check well enough to hide the button on
// loopback / LAN / own-IP — the server still re-validates.
function _isBlockableIp(ip) {
  if (!ip) return false;
  if (ip.startsWith("127.") || ip === "::1") return false;
  const hosts = window.__USB_RTSP_HOSTS__ || {};
  if (hosts.lan && ip === hosts.lan) return false;       // self
  if (hosts.lan) {
    // Same /24 as the LAN — don't offer block button.
    const parts = ip.split(".");
    const lanParts = hosts.lan.split(".");
    if (parts.length === 4 && lanParts.length === 4
        && parts[0] === lanParts[0] && parts[1] === lanParts[1] && parts[2] === lanParts[2]) {
      return false;
    }
  }
  return true;
}

async function refreshSessions() {
  let data;
  try { data = await fetch("/api/sessions").then(r => r.json()); }
  catch { return; }
  const tbody = $("#sessions-tbody");
  if (!tbody) return;
  const items = data.items || [];
  if (!items.length) {
    tbody.innerHTML = '<tr class="empty"><td colspan="9">no active viewers</td></tr>';
    return;
  }
  tbody.innerHTML = items.map(s => {
    const ip = _peerIp(s.remoteAddr);
    const kickable = (s.kind === "rtspsessions" || s.kind === "webrtcsessions") && !!s.id;
    const kickBtn = kickable
      ? `<button type="button" class="kick-session-btn" data-kick-kind="${escapeHtml(s.kind)}" data-kick-id="${escapeHtml(s.id)}" title="terminate this session in mediamtx (firewall untouched)">kick</button>`
      : "";
    const blockBtn = _isBlockableIp(ip)
      ? `<button type="button" class="danger block-ip-btn" data-block-ip="${escapeHtml(ip)}" title="UFW deny new connections from ${escapeHtml(ip)} + kick this session">block</button>`
      : "";
    const actions = [kickBtn, blockBtn].filter(Boolean).join(" ");
    return `
    <tr>
      <td><span class="proto proto-${escapeHtml((s.protocol || '').toLowerCase())}">${escapeHtml(s.protocol || "—")}</span></td>
      <td>${escapeHtml(s.path || "—")}</td>
      <td class="peer">${escapeHtml(s.remoteAddr || "—")}</td>
      <td>${escapeHtml(s.state || "—")}</td>
      <td>${escapeHtml(s.transport || "—")}</td>
      <td class="bytes">${escapeHtml(s.bytesSent_h || "—")}</td>
      <td class="bytes">${escapeHtml(s.bytesReceived_h || "—")}</td>
      <td class="dur">${escapeHtml(s.duration_h || "—")}</td>
      <td>${actions}</td>
    </tr>
  `;
  }).join("");
}

async function kickSession(kind, id) {
  try {
    const r = await fetch("/api/sessions/kick", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ kind, id }),
    });
    const j = await r.json();
    if (!r.ok || !j.ok) throw new Error(j.detail || `HTTP ${r.status} (mediamtx ${j.status})`);
    refreshSessions();
  } catch (err) {
    alert(`kick failed: ${err.message}`);
  }
}

async function blockIp(ip, source = "dashboard") {
  if (!confirm(`Block ${ip}? UFW will deny new connections from this IP and any active sessions will be kicked.`)) return;
  try {
    const r = await fetch("/api/ufw/block", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ source: ip, reason: source }),
    });
    const j = await r.json();
    if (!r.ok || j.ok === false) throw new Error(j.detail || j.error || `HTTP ${r.status}`);
    refreshSessions();
  } catch (err) {
    alert(`block ${ip} failed: ${err.message}`);
  }
}

document.addEventListener("click", (e) => {
  const blk = e.target.closest(".block-ip-btn[data-block-ip]");
  if (blk) { blockIp(blk.dataset.blockIp); return; }
  const kk = e.target.closest(".kick-session-btn[data-kick-id]");
  if (kk) { kickSession(kk.dataset.kickKind, kk.dataset.kickId); }
});


// ─── service recovery (per-unit row + log viewer + snapshots) ──────────────

let logTailTimer = null;

async function refreshSvcRow(row) {
  const unit = row.dataset.svcRow;
  try {
    const r = await fetch(`/api/svc/${unit}`);
    if (!r.ok) return;
    const s = await r.json();
    const state = $("[data-svc-state]", row);
    const uptime = $("[data-svc-uptime]", row);
    const pid = $("[data-svc-pid]", row);
    state.textContent = `${s.active_state} / ${s.sub_state}`;
    state.classList.remove("ok", "warn", "err");
    state.classList.add(s.active ? "ok" : (s.active_state === "activating" ? "warn" : "err"));
    uptime.textContent = s.uptime_h && s.uptime_h !== "—" ? `up ${s.uptime_h}` : "—";
    pid.textContent = s.main_pid && s.main_pid !== "0" ? `pid ${s.main_pid}` : "pid —";
  } catch {}
}

async function refreshAllSvcRows() {
  $$(".svc-row").forEach(refreshSvcRow);
  // Compose a one-line summary for the foldable Service-recovery card header.
  setTimeout(() => {
    const states = $$(".svc-row [data-svc-state]").map(el => el.textContent);
    const target = document.querySelector('[data-settings-status="recovery"]');
    if (!target) return;
    if (!states.length) { target.textContent = "—"; return; }
    const allOk = states.every(t => t.includes("active"));
    target.textContent = allOk ? `all ${states.length} active` : states.join(" · ");
    target.classList.toggle("ok", allOk);
    target.classList.toggle("err", !allOk);
  }, 250);
}

async function refreshLogs(announceTail = false) {
  const unit = $("#log-unit").value;
  const lines = parseInt($("#log-lines").value, 10) || 100;
  const status = $("#log-status");
  try {
    const r = await fetch(`/api/logs?unit=${encodeURIComponent(unit)}&lines=${lines}`);
    const j = await r.json();
    const pre = $("#logs");
    pre.textContent = j.text || "(no log entries — service running yet?)";
    pre.scrollTop = pre.scrollHeight;
    if (announceTail) {
      status.classList.add("tail");
      status.textContent = `tailing ${unit} every 2 s · ${new Date().toLocaleTimeString()}`;
    }
  } catch {
    if (status) status.textContent = "log fetch failed";
  }
}

async function refreshSnapshots() {
  try {
    const r = await fetch("/api/snapshots");
    const j = await r.json();
    $("[data-snapshots-summary]").textContent = `(${j.count} files · ${j.total_h})`;
    const ul = $("#snap-list");
    if (!ul) return;
    if (!j.files.length) {
      ul.innerHTML = '<li class="empty">no snapshots</li>';
      return;
    }
    ul.innerHTML = j.files.map(f => {
      const dt = new Date(f.mtime * 1000).toLocaleString();
      return `<li><span class="snap-size">${escapeHtml(f.size_h)}</span><span>${escapeHtml(dt)}</span><span>${escapeHtml(f.name)}</span></li>`;
    }).join("");
  } catch {}
}

function wireUpRecovery() {
  // Per-service start / stop / restart
  $$(".svc-row [data-svc-act]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const row = btn.closest(".svc-row");
      const unit = row.dataset.svcRow;
      const act = btn.dataset.svcAct;
      if (unit === "usb-rtsp-admin" && (act === "stop" || act === "restart")) {
        const verb = act === "stop" ? "Stop" : "Restart";
        if (!confirm(`${verb} the admin service? This page will stop responding.`)) return;
      }
      const orig = btn.textContent;
      btn.disabled = true;
      btn.textContent = "…";
      try {
        const r = await fetch(`/api/svc/${unit}/${act}`, { method: "POST" });
        const j = await r.json();
        btn.textContent = j.ok || j.scheduled ? "done" : "failed";
      } catch { btn.textContent = "error"; }
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; refreshSvcRow(row); }, 2000);
    });
  });

  // Logs
  $("[data-act=refresh-logs]")?.addEventListener("click", () => refreshLogs(false));
  $("#log-unit")?.addEventListener("change", () => refreshLogs(false));
  $("#log-tail")?.addEventListener("change", (e) => {
    if (e.target.checked) {
      logTailTimer = setInterval(() => refreshLogs(true), 2000);
      refreshLogs(true);
    } else {
      if (logTailTimer) clearInterval(logTailTimer);
      logTailTimer = null;
      const status = $("#log-status");
      if (status) { status.classList.remove("tail"); status.textContent = ""; }
    }
  });

  // Snapshots
  $("[data-act=snap-refresh]")?.addEventListener("click", refreshSnapshots);
  $("[data-act=snap-cleanup]")?.addEventListener("click", async (e) => {
    const days = parseInt($("#snap-days").value, 10);
    if (isNaN(days) || days < 0) return;
    if (!confirm(`Delete snapshots older than ${days} day(s)?`)) return;
    const btn = e.currentTarget;
    const orig = btn.textContent;
    btn.disabled = true; btn.textContent = "…";
    try {
      const r = await fetch(`/api/snapshots/cleanup?older_than_days=${days}`, { method: "POST" });
      const j = await r.json();
      btn.textContent = `freed ${j.freed_h} (${j.deleted})`;
    } catch { btn.textContent = "error"; }
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; refreshSnapshots(); }, 2500);
  });
  refreshSnapshots();
  refreshAllSvcRows();
  setInterval(refreshAllSvcRows, 5000);
}


// ─── host info tiles ───────────────────────────────────────────────────────

async function refreshHost() {
  let h;
  try { h = await fetch("/api/host").then(r => r.json()); } catch { return; }

  const set = (sel, val) => { const el = $(sel); if (el) el.textContent = val; };
  const setBar = (sel, pct) => {
    const el = $(sel); if (!el) return;
    el.style.width = `${Math.max(0, Math.min(100, pct))}%`;
    el.classList.remove("warn", "err");
    if (pct >= 90) el.classList.add("err");
    else if (pct >= 75) el.classList.add("warn");
  };

  set("[data-host-model]", h.model || "—");
  set("[data-host-host]", h.hostname || "—");
  set("[data-host-kernel]", h.kernel || "—");
  set("[data-host-uptime]", `up ${h.uptime_h || "—"}`);
  set("[data-host-ip]", h.lan_ip || "—");
  set("[data-host-mediamtx]", h.mediamtx_version || "—");
  set("[data-host-cpu]", h.cpu_pct != null
    ? `${h.cpu_pct}% busy · ${h.cpu_count || 0} cores`
    : `${h.cpu_count || 0} cores`);
  if (h.loadavg) {
    set("[data-host-load]", `${h.loadavg[0].toFixed(2)} / ${h.loadavg[1].toFixed(2)} / ${h.loadavg[2].toFixed(2)}`);
  }
  if (h.mem && h.mem.total_h) {
    set("[data-host-mem]", `${h.mem.used_h} / ${h.mem.total_h} (${h.mem.used_pct}%)`);
    setBar("[data-host-mem-bar]", h.mem.used_pct || 0);
  }
  if (h.disk_root) {
    set("[data-host-disk-root]", `${h.disk_root.used_h} / ${h.disk_root.total_h} (${h.disk_root.used_pct}%)`);
    setBar("[data-host-disk-root-bar]", h.disk_root.used_pct || 0);
  }
  set("[data-host-temp]", h.cpu_temp_c != null ? `${h.cpu_temp_c} °C` : "—");
  set("[data-host-fan]", h.fan
    ? `fan ${h.fan.rpm} rpm · ${h.fan.pwm_pct}% pwm`
    : "thermal zone 0");

  const fmtBps = (bps) => {
    if (bps == null) return "—";
    if (bps < 1024)         return `${bps} B/s`;
    if (bps < 1024 * 1024)  return `${(bps / 1024).toFixed(1)} KB/s`;
    return `${(bps / 1024 / 1024).toFixed(2)} MB/s`;
  };
  const fmtAgo = (s) => {
    if (s == null) return "—";
    if (s < 60)    return `${s}s`;
    if (s < 3600)  return `${Math.floor(s / 60)}m${s % 60 ? ` ${s % 60}s` : ""}`;
    return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`;
  };
  if (h.lan) {
    set("[data-host-net]", `↓ ${fmtBps(h.lan.rx_bps)} · ↑ ${fmtBps(h.lan.tx_bps)}`);
    set("[data-host-net-iface]", h.lan.iface || "—");
  } else {
    set("[data-host-net]", "—");
    set("[data-host-net-iface]", "no default route");
  }

  const showCard = (name, on) => {
    const el = document.querySelector(`[data-hardware-card="${name}"]`);
    if (el) el.hidden = !on;
  };

  if (h.throttle) {
    const t = h.throttle;
    const tile = document.querySelector('[data-host-tile="throttle"]');
    let value = "OK";
    let sub   = t.raw || "—";
    if (t.now) {
      value = `⚠ ${t.text}`;
      sub   = "happening now";
    } else if (t.latched && t.fresh) {
      value = `latched: ${t.text}`;
      sub   = `last event ${fmtAgo(t.age_s)} ago`;
    } else if (t.latched) {
      // Bits are still set in the register (only a power-cycle clears
      // them) but no events for a while — treat as old news.
      value = "OK";
      sub   = `latched ${t.raw} · last event ${fmtAgo(t.age_s)} ago`;
    }
    set("[data-host-throttle]", value);
    set("[data-host-throttle-raw]", sub);
    if (tile) {
      tile.classList.toggle("warn", !!t.latched && !!t.fresh && !t.now);
      tile.classList.toggle("err",  !!t.now);
    }
  }

  if (h.hailo) {
    showCard("hailo", true);
    set("[data-hailo-model]",  h.hailo.model || "—");
    set("[data-hailo-arch]",   h.hailo.arch || "—");
    set("[data-hailo-fw]",     h.hailo.fw_version || "—");
    set("[data-hailo-dev]",    h.hailo.dev ? "/dev/hailo0 present" : "/dev/hailo0 missing");
    set("[data-hailo-driver]", h.hailo.driver || "—");
    set("[data-hailo-svc]",
      h.hailo.hailort_active === true  ? "hailort active"
      : h.hailo.hailort_active === false ? "hailort inactive"
      : "hailort —");
    set("[data-hailo-pcie]",   h.hailo.pcie_link || "—");
  } else {
    showCard("hailo", false);
  }

  if (h.ups) {
    showCard("ups", true);
    set("[data-ups-title]", h.ups.model ? `UPS HAT — ${h.ups.model}` : "UPS HAT");
    const v = h.ups.battery_v != null ? `${h.ups.battery_v.toFixed(2)} V` : "— V";
    const p = h.ups.battery_pct != null ? `${h.ups.battery_pct}%` : "—";
    set("[data-ups-volt]", `${v} (${p})`);
    setBar("[data-ups-bar]", h.ups.battery_pct || 0);
    const sourceLabels = {
      ac:          "AC power",
      battery:     "On battery",
      battery_low: "On battery — low",
      unreachable: "⚠ HAT i2c unreachable",
      unknown:     "—",
    };
    set("[data-ups-source]", sourceLabels[h.ups.source] || "—");
    // Degraded sub-line when we can't read voltage at all.
    if (h.ups.source === "unreachable") {
      set("[data-ups-low]", "no i2c — cell state unknown");
    } else {
      set("[data-ups-low]", h.ups.low_v != null ? `cutoff ${h.ups.low_v.toFixed(2)} V` : "");
    }
    set("[data-ups-watchdog]",
      h.ups.watchdog_active === true  ? "active"
      : h.ups.watchdog_active === false ? "inactive"
      : "—");

    // Tile colouring: red on unreachable / battery_low, yellow on
    // battery, no colour on AC.
    const upsCard = document.querySelector('[data-hardware-card="ups"]');
    if (upsCard) {
      upsCard.querySelectorAll('.host-tile').forEach(t => {
        t.classList.remove("warn", "err");
      });
      const sourceTile = upsCard.querySelectorAll('.host-tile')[1];
      if (sourceTile) {
        if (h.ups.source === "unreachable" || h.ups.source === "battery_low") {
          sourceTile.classList.add("err");
        } else if (h.ups.source === "battery") {
          sourceTile.classList.add("warn");
        }
      }
    }
  } else {
    showCard("ups", false);
  }
}


// ─── WebRTC public-access tile ────────────────────────────────────────────

async function refreshWebrtcPublic() {
  let j;
  try { j = await fetch("/api/webrtc/state").then(r => r.json()); } catch { return; }
  const set = (sel, val) => { const el = document.querySelector(sel); if (el) el.textContent = val; };
  const showCard = (name, on) => {
    const el = document.querySelector(`[data-hardware-card="${name}"]`);
    if (el) el.hidden = !on;
  };
  // Hide the card on bare LAN-only setups where neither auto-detect nor a
  // configured host produced a public IP.
  if (!j.public_ip && !j.configured_host) {
    showCard("webrtc-public", false);
    return;
  }
  showCard("webrtc-public", true);
  set("[data-webrtc-public-ip]", j.public_ip || "(none yet)");
  const srcLabel = j.source ? `via ${j.source.toUpperCase()}` : (j.last_error ? `error: ${j.last_error}` : "—");
  const cfg = j.configured_host ? ` · cfg ${j.configured_host}` : "";
  set("[data-webrtc-public-source]", srcLabel + cfg);
  if (j.last_detected_at) {
    const s = Math.max(0, Math.floor((Date.now() - new Date(j.last_detected_at).getTime()) / 1000));
    const ago = s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s/60)}m` : `${Math.floor(s/3600)}h`;
    set("[data-webrtc-public-detected]", `${ago} ago`);
  } else {
    set("[data-webrtc-public-detected]", "never");
  }
}


// ─── boot ──────────────────────────────────────────────────────────────────

// ─── per-input enable/disable (cameras, relay sources, ...) ───────────────
// Wherever a slider has data-input-toggle="<plugin>/<name>", clicking it
// flips the input via /api/<plugin>/(cam|sources)/<name>/(enable|disable).
// The endpoint names happen to follow a regular pattern that we encode here.

const INPUT_ROUTE = {
  usb:   (name, action) => `/api/usb/cam/${encodeURIComponent(name)}/${action}`,
  relay: (name, action) => `/api/relay/sources/${encodeURIComponent(name)}/${action}`,
};

function wireInputToggles() {
  $$('input[data-input-toggle]').forEach(box => {
    if (box.dataset._wired) return;
    box.dataset._wired = "1";
    box.addEventListener("change", async () => {
      const [plugin, name] = (box.dataset.inputToggle || "").split("/");
      const route = INPUT_ROUTE[plugin];
      if (!route) {
        console.warn("no route for plugin", plugin);
        return;
      }
      const enable = box.checked;
      box.disabled = true;
      try {
        const r = await fetch(route(name, enable ? "enable" : "disable"), { method: "POST" });
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
        // visual: dim/undim the card / row
        const host = box.closest("[data-cam], [data-relay], .plugin-input");
        if (host) host.classList.toggle("input-disabled", !enable);
        if (host) host.classList.toggle("disabled", !enable);
      } catch (err) {
        box.checked = !enable;
        alert(`${plugin}/${name} toggle failed: ${err.message}`);
      } finally {
        box.disabled = false;
      }
    });
  });
}


// ─── dashboard section folding ─────────────────────────────────────────────
// Each top-level <section> on the dashboard gets a fold button injected
// next to its <h2>; the rest of the section's content is wrapped in a
// fold-body div that toggles `hidden`. Runtime DOM mutation, so plugin
// section templates don't have to know about this.

function makeSectionFoldable(section, opts = {}) {
  if (section.dataset.foldable === "1") return;
  const h2 = section.querySelector(":scope > h2");
  if (!h2) return;
  section.dataset.foldable = "1";

  const body = document.createElement("div");
  body.className = "fold-body";
  let next = h2.nextSibling;
  while (next) {
    const tmp = next.nextSibling;
    body.appendChild(next);
    next = tmp;
  }
  section.appendChild(body);

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "card-tab fold-btn";

  const persistKey = opts.persistKey ? `fold:${opts.persistKey}` : "";
  let open = opts.defaultOpen !== false;
  if (persistKey) {
    try {
      const saved = localStorage.getItem(persistKey);
      if (saved === "open")   open = true;
      if (saved === "closed") open = false;
    } catch {}
  }

  function apply() {
    body.hidden = !open;
    btn.classList.toggle("open", open);
    btn.textContent = open ? "Hide ▴" : "Show ▾";
  }
  apply();

  btn.addEventListener("click", () => {
    open = !open;
    if (persistKey) {
      try { localStorage.setItem(persistKey, open ? "open" : "closed"); } catch {}
    }
    apply();
  });

  h2.appendChild(btn);
}

function wireDashboardFolds() {
  document.querySelectorAll('.plugin-stack > section, .host-stack > section').forEach(section => {
    const key = section.id || section.classList[0] || section.tagName.toLowerCase();
    makeSectionFoldable(section, { defaultOpen: true, persistKey: `dash-${key}` });
  });
}

// ─── dashboard section reorder (drag handle in <h2>) ──────────────────────
// Sibling of wireDashboardFolds — injects a ⋮⋮ grip into each h2 and lets
// the user drag sections within their stack. Order persists per-stack to
// localStorage. Cross-stack drops are ignored.

function _sectionKey(sec) {
  return sec.id || sec.classList[0] || sec.tagName.toLowerCase();
}

function applyStackOrder(stack, savedKeys) {
  const byKey = new Map();
  Array.from(stack.children).forEach(sec => byKey.set(_sectionKey(sec), sec));
  savedKeys.forEach(k => {
    const sec = byKey.get(k);
    if (sec) stack.appendChild(sec);
  });
}

function persistStackOrder(stack, storageKey) {
  const keys = Array.from(stack.children).map(_sectionKey);
  try { localStorage.setItem(storageKey, JSON.stringify(keys)); } catch {}
}

function _stackChildSection(el, stack) {
  while (el && el !== stack) {
    if (el.parentElement === stack && el.tagName === "SECTION") return el;
    el = el.parentElement;
  }
  return null;
}

function wireDashboardReorder() {
  document.querySelectorAll(".plugin-stack, .host-stack").forEach(stack => {
    const stackKey = stack.classList.contains("plugin-stack")
      ? "plugin-stack" : "host-stack";
    const storageKey = `dash-order:${stackKey}`;

    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) applyStackOrder(stack, JSON.parse(raw));
    } catch {}

    stack.querySelectorAll(":scope > section").forEach(section => {
      const h2 = section.querySelector(":scope > h2");
      if (!h2 || section.dataset.reorderable === "1") return;
      section.dataset.reorderable = "1";

      const grip = document.createElement("button");
      grip.type = "button";
      grip.className = "card-tab section-grip";
      grip.draggable = true;
      grip.title = "drag to reorder";
      grip.textContent = "⋮";
      h2.insertBefore(grip, h2.firstChild);

      grip.addEventListener("dragstart", e => {
        section.classList.add("dragging");
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", _sectionKey(section));
      });
      grip.addEventListener("dragend", () => {
        section.classList.remove("dragging");
        persistStackOrder(stack, storageKey);
      });
    });

    stack.addEventListener("dragover", e => {
      const dragging = stack.querySelector(".dragging");
      if (!dragging) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      const over = _stackChildSection(e.target, stack);
      if (!over || over === dragging) return;
      const rect = over.getBoundingClientRect();
      const after = (e.clientY - rect.top) > rect.height / 2;
      const next = after ? over.nextSibling : over;
      if (dragging.nextSibling !== next && dragging !== next) {
        stack.insertBefore(dragging, next);
      }
    });
    stack.addEventListener("drop", e => {
      if (stack.querySelector(".dragging")) e.preventDefault();
    });
  });
}

// Per-card reorder inside .cameras / .relays sections. After fold wiring,
// cards live inside .fold-body. Card key comes from data-cam / data-relay.

function _cardKey(card) {
  return card.dataset.cam || card.dataset.relay || card.dataset.name || "";
}

function _containerChild(el, container, tag) {
  while (el && el !== container) {
    if (el.parentElement === container && el.tagName === tag) return el;
    el = el.parentElement;
  }
  return null;
}

function wireCardReorder() {
  document.querySelectorAll("section.cameras, section.relays").forEach(section => {
    const container = section.querySelector(":scope > .fold-body") || section;
    const stackKey = section.classList.contains("cameras") ? "cameras" : "relays";
    const storageKey = `dash-order:${stackKey}-cards`;

    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) {
        const saved = JSON.parse(raw);
        const byKey = new Map();
        container.querySelectorAll(":scope > article.card").forEach(c => {
          byKey.set(_cardKey(c), c);
        });
        saved.forEach(k => {
          const c = byKey.get(k);
          if (c) container.appendChild(c);
        });
      }
    } catch {}

    container.querySelectorAll(":scope > article.card").forEach(card => {
      const header = card.querySelector(":scope > header");
      if (!header || card.dataset.reorderable === "1") return;
      if (!_cardKey(card)) return;
      card.dataset.reorderable = "1";

      const grip = document.createElement("button");
      grip.type = "button";
      grip.className = "card-tab card-grip";
      grip.draggable = true;
      grip.title = "drag to reorder";
      grip.textContent = ":";
      header.insertBefore(grip, header.firstChild);

      grip.addEventListener("dragstart", e => {
        card.classList.add("dragging");
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", _cardKey(card));
      });
      grip.addEventListener("dragend", () => {
        card.classList.remove("dragging");
        const keys = Array.from(container.querySelectorAll(":scope > article.card")).map(_cardKey);
        try { localStorage.setItem(storageKey, JSON.stringify(keys)); } catch {}
      });
    });

    container.addEventListener("dragover", e => {
      const dragging = container.querySelector(":scope > article.card.dragging");
      if (!dragging) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      const over = _containerChild(e.target, container, "ARTICLE");
      if (!over || over === dragging) return;
      const rect = over.getBoundingClientRect();
      const after = (e.clientY - rect.top) > rect.height / 2;
      const next = after ? over.nextSibling : over;
      if (dragging.nextSibling !== next && dragging !== next) {
        container.insertBefore(dragging, next);
      }
    });
    container.addEventListener("drop", e => {
      if (container.querySelector(":scope > article.card.dragging")) e.preventDefault();
    });
  });
}


document.addEventListener("DOMContentLoaded", () => {
  wireDashboardFolds();
  wireDashboardReorder();
  wireCardReorder();
  wireUpRecovery();
  wireInputToggles();
  wireHostToggle();
  refreshStatus();
  refreshSessions();
  refreshHost();
  refreshAuthBar();
  refreshWebrtcPublic();
  setInterval(() => {
    refreshStatus();
    refreshSessions();
  }, POLL_MS);
  setInterval(refreshHost, 10000);
  setInterval(refreshWebrtcPublic, 30000);
});
