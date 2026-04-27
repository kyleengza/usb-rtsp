// Settings page — plugin toggles + service recovery.
// Reuses helpers (refreshSvcRow, refreshLogs, refreshSnapshots, copyText)
// from /static-core/app.js — those are exposed at module scope there.

(function () {
  const $ = (s, c = document) => c.querySelector(s);
  const $$ = (s, c = document) => Array.from(c.querySelectorAll(s));

  // ─── plugin toggles ─────────────────────────────────────────────────────

  $$('input[data-plugin-toggle]').forEach(box => {
    box.addEventListener("change", async () => {
      const name = box.dataset.pluginToggle;
      const enable = box.checked;
      const status = $("#plugins-status");
      status.className = "form-status";
      status.textContent = `${enable ? "enabling" : "disabling"} ${name}…`;
      box.disabled = true;
      try {
        const path = `/api/plugins/${encodeURIComponent(name)}/${enable ? "enable" : "disable"}`;
        const r = await fetch(path, { method: "POST" });
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
        status.classList.add("ok");
        status.textContent = `${name} ${enable ? "enabled" : "disabled"} · admin restarting · reloading in 5 s`;
        setTimeout(() => location.reload(), 5000);
      } catch (err) {
        status.classList.add("err");
        status.textContent = `error: ${err.message}`;
        box.checked = !enable;
        box.disabled = false;
      }
    });
  });

  // ─── plugin block fold/unfold ──────────────────────────────────────────
  $$('[data-act=fold-plugin]').forEach(btn => {
    btn.addEventListener("click", () => {
      const block = btn.closest('.plugin-block');
      const body = block?.querySelector('.plugin-body');
      if (!body) return;
      const isOpen = !body.hidden;
      body.hidden = isOpen;
      btn.classList.toggle("open", !isOpen);
      btn.textContent = isOpen ? "Details ▾" : "Hide ▴";
    });
  });

  // ─── stream credential rotation ────────────────────────────────────────
  let realPass = null;        // populated when /api/auth/stream-credentials returns
  let revealed = false;

  function maskedPass(s) { return "●".repeat(Math.max(8, Math.min(28, s?.length || 24))); }

  function setPassDisplay() {
    const el = $("#stream-pass");
    if (!el) return;
    el.textContent = revealed && realPass ? realPass : maskedPass(realPass);
  }

  async function loadStreamCreds() {
    try {
      const r = await fetch("/api/auth/stream-credentials");
      const j = await r.json();
      const status = $("#auth-stream-status");
      const creds = $("#auth-creds");
      if (!j.enabled) {
        status.classList.add("warn");
        status.textContent = "stream auth disabled";
        return;
      }
      status.classList.add("ok");
      status.textContent = "stream auth enabled";
      $("#stream-user").textContent = j.user;
      realPass = j.password;
      setPassDisplay();
      creds.hidden = false;
    } catch {}
  }

  $("[data-act=reveal-pass]")?.addEventListener("click", (e) => {
    revealed = !revealed;
    setPassDisplay();
    e.currentTarget.textContent = revealed ? "Hide" : "Show";
    if (revealed) {
      // auto-mask after 15 s
      setTimeout(() => {
        if (revealed) {
          revealed = false;
          setPassDisplay();
          const btn = $("[data-act=reveal-pass]");
          if (btn) btn.textContent = "Show";
        }
      }, 15000);
    }
  });

  $("#copy-pass")?.addEventListener("click", async (e) => {
    if (!realPass) return;
    const btn = e.currentTarget;
    const orig = btn.textContent;
    const ok = await window.copyText(realPass);
    btn.classList.toggle("ok", ok);
    btn.textContent = ok ? "copied" : "select+ctrl-c";
    setTimeout(() => { btn.textContent = orig; btn.classList.remove("ok"); }, 1500);
  });

  $("[data-act=rotate-pass]")?.addEventListener("click", async (e) => {
    const status = $("#auth-status");
    status.className = "form-status";
    status.textContent = "";
    if (!confirm("Rotate the stream password? Every active RTSP/HLS/WebRTC client will be disconnected and need the new password.")) return;
    const btn = e.currentTarget;
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = "rotating…";
    try {
      const r = await fetch("/api/auth/stream-rotate", { method: "POST" });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
      realPass = j.password;
      revealed = true;
      setPassDisplay();
      const reveal = $("[data-act=reveal-pass]");
      if (reveal) reveal.textContent = "Hide";
      status.classList.add("ok");
      status.textContent = `rotated · admin restarting · reloading in 6 s — copy the new password now`;
      setTimeout(() => location.reload(), 6000);
    } catch (err) {
      status.classList.add("err");
      status.textContent = `rotate failed: ${err.message}`;
    } finally {
      btn.disabled = false;
      btn.textContent = orig;
    }
  });

  // Boot
  loadStreamCreds();
})();
