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
    tbody.innerHTML = '<tr class="empty"><td colspan="6">no active sessions</td></tr>';
    return;
  }
  tbody.innerHTML = items.map(s => `
    <tr>
      <td>${escapeHtml(s.path || "—")}</td>
      <td class="peer">${escapeHtml(s.remoteAddr || "—")}</td>
      <td>${escapeHtml(s.state || "—")}</td>
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

  // form submit → POST /api/cam/{name}
  $("form[data-cam-form]", card).addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const status = $("[data-form-status]", form);
    status.className = "form-status";
    status.textContent = "saving…";

    const fd = new FormData(form);
    const [w, h] = fd.get("resolution").split("x");
    const body = {
      by_id: fd.get("by_id"),
      format: fd.get("format"),
      width: parseInt(w, 10),
      height: parseInt(h, 10),
      fps: parseInt(fd.get("fps"), 10),
      encode: fd.get("encode") || "h264",
      profile: fd.get("profile"),
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
      status.textContent = `saved · reload: ${j.reload}`;
      setTimeout(() => { status.textContent = ""; status.className = "form-status"; }, 3000);
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

  // live preview toggle (lazy-loads WebRTC iframe so it doesn't auto-play
  // every camera's stream the moment the page opens)
  $("[data-act=preview]", card)?.addEventListener("click", () => {
    const btn = $("[data-act=preview]", card);
    const wrap = $(".preview", card);
    const iframe = $("[data-preview-frame]", card);
    const isOpen = !wrap.hidden;
    if (isOpen) {
      wrap.hidden = true;
      iframe.removeAttribute("src");
      btn.classList.remove("open");
      btn.textContent = "Live preview ▾";
    } else {
      const cam = card.dataset.cam;
      iframe.src = `http://${location.hostname}:8889/${cam}/`;
      wrap.hidden = false;
      btn.classList.add("open");
      btn.textContent = "Hide preview ▴";
    }
  });

  // copy URL buttons
  $$(".copy", card).forEach(btn => {
    btn.addEventListener("click", async () => {
      const url = btn.dataset.copy;
      try {
        await navigator.clipboard.writeText(url);
        const orig = btn.textContent;
        btn.classList.add("ok");
        btn.textContent = "copied";
        setTimeout(() => { btn.textContent = orig; btn.classList.remove("ok"); }, 1200);
      } catch {
        btn.textContent = "ctrl-c it";
      }
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

function wireUpRecovery() {
  $$("[data-act=rescan], [data-act=restart], [data-act=restart-admin]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const act = btn.dataset.act;
      if (act === "restart-admin" && !confirm("Restart admin? You'll briefly lose this page.")) return;
      const orig = btn.textContent;
      btn.disabled = true;
      btn.textContent = "…";
      try {
        const path = act === "rescan" ? "/api/rescan"
                   : act === "restart" ? "/api/restart"
                   : "/api/restart-admin";
        const r = await fetch(path, { method: "POST" });
        const j = await r.json();
        btn.textContent = act === "rescan"
          ? (j.added?.length ? `added ${j.added.length}` : "no new")
          : (j.ok ? "done" : "failed");
      } catch (err) {
        btn.textContent = "error";
      }
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2500);
    });
  });

  $("[data-act=refresh-logs]")?.addEventListener("click", async () => {
    const lines = parseInt($("#log-lines").value, 10) || 100;
    const r = await fetch(`/api/logs?unit=usb-rtsp&lines=${lines}`);
    const j = await r.json();
    $("#logs").textContent = j.text || "(empty)";
  });
}

// ─── boot ───────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  $$(".card[data-cam]").forEach(wireUpCard);
  wireUpRecovery();
  refreshStatus();
  refreshPaths();
  refreshSessions();
  setInterval(() => {
    refreshStatus();
    refreshPaths();
    refreshSessions();
  }, POLL_MS);
});
