// usb-rtsp admin panel — core JS.
// Polls /api/status, /api/sessions, /api/host. Wires up the auth bar,
// service-recovery row, log viewer, snapshot list. Plugin JS lives in
// /static/<plugin>/<plugin>.js — those files handle their own per-card
// behaviour.

const POLL_MS = 3000;

const $ = (sel, ctx = document) => ctx.querySelector(sel);
const $$ = (sel, ctx = document) => Array.from(ctx.querySelectorAll(sel));


// ─── helpers exposed to plugin JS ──────────────────────────────────────────

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
  $("#agg").textContent =
    `paths: ${s.paths.ready}/${s.paths.total} ready · ${s.paths.readers} viewers · ↑ ${s.paths.bytes_received_h}`;
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

async function refreshSessions() {
  let data;
  try { data = await fetch("/api/sessions").then(r => r.json()); }
  catch { return; }
  const tbody = $("#sessions-tbody");
  if (!tbody) return;
  const items = data.items || [];
  if (!items.length) {
    tbody.innerHTML = '<tr class="empty"><td colspan="8">no active viewers</td></tr>';
    return;
  }
  tbody.innerHTML = items.map(s => `
    <tr>
      <td><span class="proto proto-${escapeHtml((s.protocol || '').toLowerCase())}">${escapeHtml(s.protocol || "—")}</span></td>
      <td>${escapeHtml(s.path || "—")}</td>
      <td class="peer">${escapeHtml(s.remoteAddr || "—")}</td>
      <td>${escapeHtml(s.state || "—")}</td>
      <td>${escapeHtml(s.transport || "—")}</td>
      <td class="bytes">${escapeHtml(s.bytesSent_h || "—")}</td>
      <td class="bytes">${escapeHtml(s.bytesReceived_h || "—")}</td>
      <td class="dur">${escapeHtml(s.duration_h || "—")}</td>
    </tr>
  `).join("");
}


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
  set("[data-host-cpu]", `${h.cpu_count || 0} cores`);
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
  if (h.disk_config) {
    set("[data-host-disk-config]", `${h.disk_config.used_h} / ${h.disk_config.total_h} (${h.disk_config.used_pct}%)`);
    setBar("[data-host-disk-config-bar]", h.disk_config.used_pct || 0);
  }
  set("[data-host-temp]", h.cpu_temp_c != null ? `${h.cpu_temp_c} °C` : "—");
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


document.addEventListener("DOMContentLoaded", () => {
  wireUpRecovery();
  wireInputToggles();
  refreshStatus();
  refreshSessions();
  refreshHost();
  refreshAuthBar();
  setInterval(() => {
    refreshStatus();
    refreshSessions();
  }, POLL_MS);
  setInterval(refreshHost, 10000);
});
