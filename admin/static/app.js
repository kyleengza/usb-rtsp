// usb-rtsp admin panel — vanilla JS, no framework.
// Polls /api/status, /api/paths, /api/sessions every 3s; wires up form/button actions.

const POLL_MS = 3000;

const $ = (sel, ctx = document) => ctx.querySelector(sel);
const $$ = (sel, ctx = document) => Array.from(ctx.querySelectorAll(sel));

// ─── status header ──────────────────────────────────────────────────────────

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

function setBadge(id, kind, text) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove("ok", "warn", "err");
  el.classList.add(kind);
  el.textContent = text;
}

// ─── per-camera live state ──────────────────────────────────────────────────

async function refreshPaths() {
  let data;
  try { data = await fetch("/api/paths").then(r => r.json()); }
  catch { return; }

  const items = data.items || [];
  for (const card of $$(".card[data-cam]")) {
    const name = card.dataset.cam;
    const item = items.find(p => p.name === name);
    const dot = $("[data-ready]", card);
    const readersEl = $("[data-readers]", card);
    const bytesEl = $("[data-bytes]", card);
    const upEl = $("[data-uptime]", card);

    if (!item) {
      dot.classList.remove("ok", "err");
      readersEl.textContent = "(no path)";
      bytesEl.textContent = "—";
      upEl.textContent = "—";
      continue;
    }
    const ready = item.ready === true || item.sourceReady === true;
    dot.classList.toggle("ok", ready);
    dot.classList.toggle("err", !ready);
    readersEl.textContent = `${item.readers_count || 0} viewer${item.readers_count === 1 ? "" : "s"}`;
    bytesEl.textContent = item.bytesReceived_h || "—";
    if (item.readyTime) {
      const dur = (Date.now() - new Date(item.readyTime).getTime()) / 1000;
      upEl.textContent = formatDuration(dur);
    } else {
      upEl.textContent = "—";
    }
  }
}

async function refreshSessions() {
  let data;
  try { data = await fetch("/api/sessions").then(r => r.json()); }
  catch { return; }
  const tbody = $("#sessions-tbody");
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

function formatDuration(s) {
  s = Math.max(0, Math.floor(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function waitForPathReady(name, timeoutMs = 12000) {
  // Poll /api/paths every 400ms until the named path is ready (or timeout).
  // Used after a save+restart to delay reconnecting the iframe until the
  // publisher is actually producing again.
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await fetch("/api/paths");
      if (r.ok) {
        const d = await r.json();
        const item = (d.items || []).find(p => p.name === name);
        if (item && (item.ready === true || item.sourceReady === true)) return true;
      }
    } catch {}
    await new Promise(res => setTimeout(res, 400));
  }
  return false;
}

async function copyText(text) {
  // Prefer modern API where allowed (HTTPS or localhost).
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); return true; } catch {}
  }
  // Fallback for http://lan-ip: hidden textarea + execCommand.
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
}

// ─── per-camera form: cascading resolution/fps dropdowns ────────────────────

function wireUpCard(card) {
  const capsEl = $(".caps", card);
  if (!capsEl) return;
  let caps;
  try { caps = JSON.parse(capsEl.textContent); } catch { caps = []; }

  const fmtSel = $("select[name=format]", card);
  const resSel = $("select[name=resolution]", card);
  const fpsSel = $("select[name=fps]", card);

  const curRes = resSel.dataset.current; // e.g. "1920x1080"
  const curFps = parseInt(fpsSel.dataset.current, 10);

  function rebuildRes() {
    const fmt = fmtSel.value;
    const fmtRec = caps.find(f => f.format === fmt);
    if (!fmtRec) return;
    const opts = fmtRec.sizes.map(s => `${s.width}x${s.height}`);
    resSel.innerHTML = opts
      .map(v => `<option value="${v}" ${v === curRes ? "selected" : ""}>${v.replace("x", "×")}</option>`)
      .join("");
    if (!opts.includes(resSel.value)) resSel.selectedIndex = 0;
    rebuildFps();
  }
  function rebuildFps() {
    const fmt = fmtSel.value;
    const res = resSel.value;
    const fmtRec = caps.find(f => f.format === fmt);
    if (!fmtRec) return;
    const sz = fmtRec.sizes.find(s => `${s.width}x${s.height}` === res);
    if (!sz) return;
    fpsSel.innerHTML = sz.fps
      .map(v => `<option value="${v}" ${v === curFps ? "selected" : ""}>${v} fps</option>`)
      .join("");
  }

  fmtSel.addEventListener("change", rebuildRes);
  resSel.addEventListener("change", rebuildFps);

  if (caps.length) rebuildRes();

  // toggle the MJPEG q:v row visibility based on Encode dropdown
  const encSel = $("select[name=encode]", card);
  const mjpegRow = $(".adv-mjpeg", card);
  function syncMjpegRow() {
    if (mjpegRow) mjpegRow.classList.toggle("hidden", encSel.value !== "mjpeg");
  }
  if (encSel && mjpegRow) {
    encSel.addEventListener("change", syncMjpegRow);
    syncMjpegRow();
  }

  // form submit → POST /api/cam/{name}
  $("form[data-cam-form]", card).addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const status = $("[data-form-status]", form);
    status.className = "form-status";
    status.textContent = "saving…";

    const fd = new FormData(form);
    const [w, h] = fd.get("resolution").split("x");
    // blank advanced inputs → null (server falls back to quality preset)
    const num = (k) => {
      const v = (fd.get(k) ?? "").toString().trim();
      return v === "" ? null : parseInt(v, 10);
    };
    const str = (k) => {
      const v = (fd.get(k) ?? "").toString().trim();
      return v === "" ? null : v;
    };
    const body = {
      by_id: fd.get("by_id"),
      format: fd.get("format"),
      width: parseInt(w, 10),
      height: parseInt(h, 10),
      fps: parseInt(fd.get("fps"), 10),
      encode: fd.get("encode") || "h264",
      profile: fd.get("profile"),
      quality: fd.get("quality") || "medium",
      bitrate_kbps: num("bitrate_kbps"),
      x264_preset: str("x264_preset"),
      gop_seconds: num("gop_seconds"),
      bframes: num("bframes"),
      mjpeg_qv: num("mjpeg_qv"),
    };

    const name = card.dataset.cam;
    try {
      const r = await fetch(`/api/cam/${name}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
      status.classList.add("ok");
      status.textContent = `saved · ${j.reload === "restart" ? "restarting mediamtx…" : `reload: ${j.reload}`}`;

      // After mediamtx restart, poll the API until our path goes ready
      // (mediamtx is up + the runOnInit ffmpeg has connected). Then reload
      // the iframe — WebRTC negotiation needs a live publisher behind it.
      // Without this, the iframe reconnects too early and shows a blank
      // player until the user manually refreshes.
      const wrap = $(".preview", card);
      if (j.reload === "restart") {
        // Tear down the running iframe instantly — its peerConnection is
        // about to die anyway, and freeing it now avoids stale UI.
        if (wrap && !wrap.hidden) {
          const cur = $("[data-preview-frame]", wrap);
          if (cur) cur.src = "about:blank";
        }
        const ready = await waitForPathReady(name, 15000);
        if (ready) {
          // mediamtx reports path ready as soon as the publisher reconnects,
          // but its WebRTC stack needs another beat to accept WHEP offers.
          // Settle for 800 ms before re-creating the iframe.
          await new Promise(res => setTimeout(res, 800));
          if (wrap && !wrap.hidden) rebuildPreviewIframe(name);
          status.textContent = "saved · stream ready";
        } else {
          status.classList.add("err");
          status.textContent = "saved, but stream didn't come back in 15 s — try Reconnect, or check logs";
        }
        setTimeout(() => { status.textContent = ""; status.className = "form-status"; }, 3000);
      } else {
        setTimeout(() => { status.textContent = ""; status.className = "form-status"; }, 3000);
      }
    } catch (err) {
      status.classList.add("err");
      status.textContent = `error: ${err.message}`;
    }
  });

  // snapshot button
  $("[data-act=snap]", card)?.addEventListener("click", async () => {
    const name = card.dataset.cam;
    const img = $(".snap-preview", card);
    const status = $("[data-form-status]", card);
    status.className = "form-status";
    status.textContent = "snapping…";
    try {
      const r = await fetch(`/api/cam/${name}/snap`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const blob = await r.blob();
      img.src = URL.createObjectURL(blob);
      img.hidden = false;
      status.classList.add("ok");
      status.textContent = "snap saved to ~/.config/usb-rtsp/snapshots/";
      setTimeout(() => { status.textContent = ""; status.className = "form-status"; }, 3000);
    } catch (err) {
      status.classList.add("err");
      status.textContent = `snap failed: ${err.message}`;
    }
  });

  // Replace the preview iframe with a brand-new DOM node — much more
  // reliable than changing src (no stale peerConnection / cached JS).
  function rebuildPreviewIframe(camName) {
    const wrap = $(".preview", card);
    if (!wrap) return null;
    const old = $("[data-preview-frame]", wrap);
    const fresh = document.createElement("iframe");
    fresh.setAttribute("data-preview-frame", "");
    fresh.setAttribute("loading", "lazy");
    fresh.setAttribute("allow", "autoplay");
    fresh.setAttribute("allowfullscreen", "");
    fresh.src = `http://${location.hostname}:8889/${camName}/?t=${Date.now()}`;
    if (old) old.replaceWith(fresh); else wrap.appendChild(fresh);
    return fresh;
  }

  // live preview toggle (lazy: only loads on demand, never on page open)
  $("[data-act=preview]", card)?.addEventListener("click", () => {
    const btn = $("[data-act=preview]", card);
    const wrap = $(".preview", card);
    const iframe = $("[data-preview-frame]", card);
    const isOpen = !wrap.hidden;
    if (isOpen) {
      wrap.hidden = true;
      // tear down the iframe entirely so its WebRTC peerConnection releases
      const blank = document.createElement("iframe");
      blank.setAttribute("data-preview-frame", "");
      blank.setAttribute("loading", "lazy");
      blank.setAttribute("allow", "autoplay");
      blank.setAttribute("allowfullscreen", "");
      iframe.replaceWith(blank);
      btn.classList.remove("open");
      btn.textContent = "Live preview ▾";
    } else {
      rebuildPreviewIframe(card.dataset.cam);
      wrap.hidden = false;
      btn.classList.add("open");
      btn.textContent = "Hide preview ▴";
    }
  });

  // settings toggle (matches the live-preview pattern; collapsed by default)
  $("[data-act=settings]", card)?.addEventListener("click", () => {
    const btn = $("[data-act=settings]", card);
    const wrap = $(".settings-wrap", card);
    if (!wrap) return;
    const isOpen = !wrap.hidden;
    wrap.hidden = isOpen;
    btn.classList.toggle("open", !isOpen);
    btn.textContent = isOpen ? "Settings ▾" : "Hide settings ▴";
  });

  // manual preview reconnect (backstop for cases where the post-save
  // auto-reconnect doesn't grab the new stream)
  $("[data-act=preview-reconnect]", card)?.addEventListener("click", () => {
    rebuildPreviewIframe(card.dataset.cam);
  });

  // copy URL buttons — navigator.clipboard.writeText() is blocked in
  // insecure contexts (http://lan-ip), so we fall back to a hidden
  // textarea + document.execCommand('copy'). Works in every browser
  // we'd realistically use this panel from.
  $$(".copy", card).forEach(btn => {
    btn.addEventListener("click", async () => {
      const url = btn.dataset.copy;
      const orig = btn.textContent;
      const ok = await copyText(url);
      btn.classList.toggle("ok", ok);
      btn.textContent = ok ? "copied" : "select+ctrl-c";
      setTimeout(() => { btn.textContent = orig; btn.classList.remove("ok"); }, 1500);
    });
  });

  // kick button
  $("[data-act=kick]", card)?.addEventListener("click", async () => {
    const name = card.dataset.cam;
    const status = $("[data-form-status]", card);
    status.className = "form-status";
    status.textContent = "kicking…";
    try {
      const r = await fetch(`/api/cam/${name}/restart`, { method: "POST" });
      const j = await r.json();
      status.classList.add("ok");
      status.textContent = j.kicked ? "readers kicked" : `no-op (code ${j.code})`;
      setTimeout(() => { status.textContent = ""; status.className = "form-status"; }, 3000);
    } catch (err) {
      status.classList.add("err");
      status.textContent = `error: ${err.message}`;
    }
  });
}

// ─── global recovery buttons ────────────────────────────────────────────────

// ─── service recovery ──────────────────────────────────────────────────────

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
    $("[data-snapshots-summary]").textContent =
      `(${j.count} files · ${j.total_h})`;
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
  // legacy global rescan button
  $("[data-act=rescan]")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = "…";
    try {
      const r = await fetch("/api/rescan", { method: "POST" });
      const j = await r.json();
      btn.textContent = j.added?.length ? `added ${j.added.length}` : "no new";
    } catch {
      btn.textContent = "error";
    }
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2500);
  });

  // per-service start / stop / restart
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
      } catch {
        btn.textContent = "error";
      }
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; refreshSvcRow(row); }, 2000);
    });
  });

  // logs viewer + tail toggle
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

  // snapshots
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
    } catch {
      btn.textContent = "error";
    }
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; refreshSnapshots(); }, 2500);
  });
  refreshSnapshots();
  refreshAllSvcRows();
  setInterval(refreshAllSvcRows, 5000);
}

// ─── boot ───────────────────────────────────────────────────────────────────

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

document.addEventListener("DOMContentLoaded", () => {
  $$(".card[data-cam]").forEach(wireUpCard);
  wireUpRecovery();
  refreshStatus();
  refreshPaths();
  refreshSessions();
  refreshHost();
  refreshAuthBar();
  setInterval(() => {
    refreshStatus();
    refreshPaths();
    refreshSessions();
  }, POLL_MS);
  setInterval(refreshHost, 10000);
});
