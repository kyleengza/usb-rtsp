// USB plugin frontend — runs in addition to the core admin/static/app.js.
// Handles per-camera card wiring (cascading dropdowns, settings save,
// snapshot, kick, live preview iframe).

(function () {
  const $ = (sel, ctx = document) => ctx.querySelector(sel);
  const $$ = (sel, ctx = document) => Array.from(ctx.querySelectorAll(sel));

  const POLL_MS = 3000;

  // ─── status / paths / sessions polling helpers (USB cards listen) ──────

  async function refreshUsbCards() {
    let data;
    try { data = await fetch("/api/paths").then(r => r.json()); }
    catch { return; }
    const items = data.items || [];
    for (const card of $$('.card[data-plugin="usb"][data-cam]')) {
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
        const s = Math.max(0, Math.floor(dur));
        upEl.textContent = s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s/60)}m${String(s%60).padStart(2,"0")}s` : `${Math.floor(s/3600)}h${String(Math.floor((s%3600)/60)).padStart(2,"0")}m`;
      } else {
        upEl.textContent = "—";
      }
    }
  }

  async function waitForPathReady(name, timeoutMs = 15000) {
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

  async function freshStreamCreds() {
    // Always re-fetch instead of trusting window.__USB_RTSP_STREAM_CREDS__
    // — that constant is server-rendered at page load and goes stale after
    // a rotate, leaving the iframe pointing at the old password.
    try {
      const r = await fetch("/api/auth/stream-credentials");
      if (!r.ok) return null;
      const j = await r.json();
      if (!j.enabled) return null;
      return { user: j.user, pass: j.password };
    } catch { return null; }
  }

  async function rebuildPreviewIframe(card, camName) {
    const wrap = $(".preview", card);
    if (!wrap) return null;
    const old = $("[data-preview-frame]", wrap);
    const fresh = document.createElement("iframe");
    fresh.setAttribute("data-preview-frame", "");
    fresh.setAttribute("loading", "lazy");
    fresh.setAttribute("allow", "autoplay");
    fresh.setAttribute("allowfullscreen", "");
    // Iframe loads from the panel's own /preview/<cam>/ proxy. Same-origin
    // (panel cookie covers it), and the panel adds the HTTP Basic header
    // mediamtx needs for non-loopback requests. The bundled WebRTC player's
    // relative WHEP URL ('whep' resolved against window.location.href) ends
    // up at /preview/<cam>/whep so it flows through the same proxy.
    fresh.src = `/preview/${encodeURIComponent(camName)}/?t=${Date.now()}`;
    if (old) old.replaceWith(fresh); else wrap.appendChild(fresh);
    return fresh;
  }

  function wireUpCard(card) {
    const capsEl = $(".caps", card);
    let caps = [];
    if (capsEl) {
      try { caps = JSON.parse(capsEl.textContent); } catch {}
    }

    const fmtSel = $("select[name=format]", card);
    const resSel = $("select[name=resolution]", card);
    const fpsSel = $("select[name=fps]", card);
    const curRes = resSel?.dataset.current;
    const curFps = parseInt(fpsSel?.dataset.current, 10);

    function rebuildRes() {
      const fmt = fmtSel.value;
      const fmtRec = caps.find(f => f.format === fmt);
      if (!fmtRec) return;
      resSel.innerHTML = fmtRec.sizes.map(s => {
        const v = `${s.width}x${s.height}`;
        return `<option value="${v}" ${v === curRes ? "selected" : ""}>${v.replace("x", "×")}</option>`;
      }).join("");
      if (![...resSel.options].some(o => o.value === resSel.value)) resSel.selectedIndex = 0;
      rebuildFps();
    }
    function rebuildFps() {
      const fmtRec = caps.find(f => f.format === fmtSel.value);
      if (!fmtRec) return;
      const sz = fmtRec.sizes.find(s => `${s.width}x${s.height}` === resSel.value);
      if (!sz) return;
      fpsSel.innerHTML = sz.fps.map(v => `<option value="${v}" ${v === curFps ? "selected" : ""}>${v} fps</option>`).join("");
    }
    fmtSel?.addEventListener("change", rebuildRes);
    resSel?.addEventListener("change", rebuildFps);
    if (caps.length) rebuildRes();

    const encSel = $("select[name=encode]", card);
    const mjpegRow = $(".adv-mjpeg", card);
    function syncMjpegRow() {
      if (mjpegRow) mjpegRow.classList.toggle("hidden", encSel.value !== "mjpeg");
    }
    if (encSel && mjpegRow) {
      encSel.addEventListener("change", syncMjpegRow);
      syncMjpegRow();
    }

    // Live-preview tab
    $("[data-act=preview]", card)?.addEventListener("click", async () => {
      const btn = $("[data-act=preview]", card);
      const wrap = $(".preview", card);
      const iframe = $("[data-preview-frame]", card);
      const isOpen = !wrap.hidden;
      if (isOpen) {
        wrap.hidden = true;
        const blank = document.createElement("iframe");
        blank.setAttribute("data-preview-frame", "");
        iframe.replaceWith(blank);
        btn.classList.remove("open");
        btn.textContent = "Live preview ▾";
      } else {
        wrap.hidden = false;
        btn.classList.add("open");
        btn.textContent = "Hide preview ▴";
        await rebuildPreviewIframe(card, card.dataset.cam);
      }
    });

    // Settings tab
    $("[data-act=settings]", card)?.addEventListener("click", () => {
      const btn = $("[data-act=settings]", card);
      const wrap = $(".settings-wrap", card);
      if (!wrap) return;
      const isOpen = !wrap.hidden;
      wrap.hidden = isOpen;
      btn.classList.toggle("open", !isOpen);
      btn.textContent = isOpen ? "Settings ▾" : "Hide settings ▴";
    });

    $("[data-act=preview-reconnect]", card)?.addEventListener("click", () => {
      rebuildPreviewIframe(card, card.dataset.cam);
    });

    // Copy buttons
    $$(".copy[data-copy]", card).forEach(btn => {
      btn.addEventListener("click", async () => {
        const orig = btn.textContent;
        const ok = await window.copyText(btn.dataset.copy);
        btn.classList.toggle("ok", ok);
        btn.textContent = ok ? "copied" : "select+ctrl-c";
        setTimeout(() => { btn.textContent = orig; btn.classList.remove("ok"); }, 1500);
      });
    });

    // Snapshot
    $("[data-act=snap]", card)?.addEventListener("click", async () => {
      const name = card.dataset.cam;
      const img = $(".snap-preview", card);
      const status = $("[data-form-status]", card);
      status.className = "form-status";
      status.textContent = "snapping…";
      try {
        const r = await fetch(`/api/usb/cam/${name}/snap`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const blob = await r.blob();
        img.src = URL.createObjectURL(blob);
        img.hidden = false;
        status.classList.add("ok");
        status.textContent = "saved to ~/.config/usb-rtsp/snapshots/";
        setTimeout(() => { status.textContent = ""; status.className = "form-status"; }, 3000);
      } catch (err) {
        status.classList.add("err");
        status.textContent = `snap failed: ${err.message}`;
      }
    });

    // Kick readers
    $("[data-act=kick]", card)?.addEventListener("click", async () => {
      const name = card.dataset.cam;
      const status = $("[data-form-status]", card);
      status.className = "form-status";
      status.textContent = "kicking…";
      try {
        const r = await fetch(`/api/usb/cam/${name}/restart`, { method: "POST" });
        const j = await r.json();
        status.classList.add("ok");
        status.textContent = j.kicked ? "readers kicked" : `no-op (code ${j.code})`;
        setTimeout(() => { status.textContent = ""; status.className = "form-status"; }, 3000);
      } catch (err) {
        status.classList.add("err");
        status.textContent = `error: ${err.message}`;
      }
    });

    // Save form
    $("form[data-cam-form]", card)?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = e.target;
      const status = $("[data-form-status]", form);
      status.className = "form-status";
      status.textContent = "saving…";

      const fd = new FormData(form);
      const [w, h] = fd.get("resolution").split("x");
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
        const r = await fetch(`/api/usb/cam/${name}`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
        status.classList.add("ok");
        status.textContent = `saved · ${j.reload === "restart" ? "restarting mediamtx…" : `reload: ${j.reload}`}`;

        const wrap = $(".preview", card);
        if (j.reload === "restart") {
          if (wrap && !wrap.hidden) {
            const cur = $("[data-preview-frame]", wrap);
            if (cur) cur.src = "about:blank";
          }
          const ready = await waitForPathReady(name, 15000);
          if (ready) {
            await new Promise(res => setTimeout(res, 800));
            if (wrap && !wrap.hidden) await rebuildPreviewIframe(card, name);
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
  }

  document.addEventListener("DOMContentLoaded", () => {
    $$('.card[data-plugin="usb"][data-cam]').forEach(wireUpCard);
    refreshUsbCards();
    setInterval(refreshUsbCards, POLL_MS);
  });
})();
