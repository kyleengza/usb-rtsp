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

  // ─── plugin install / uninstall / refresh ─────────────────────────────
  async function reloadAfterAdminRestart(statusEl, msg) {
    statusEl.classList.add("ok");
    statusEl.textContent = `${msg} · admin restarting · reloading in 5 s`;
    setTimeout(() => location.reload(), 5000);
  }

  $('[data-act=install-plugin]')?.addEventListener("click", async () => {
    const status = $("#plugins-status");
    status.className = "form-status";
    const source = ($("#add-plugin-source").value || "").trim();
    if (!source) { status.classList.add("err"); status.textContent = "git URL or path required"; return; }
    status.textContent = "installing…";
    try {
      const r = await fetch("/api/plugins/install", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ source }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
      reloadAfterAdminRestart(status, `installed ${j.installed} from ${j.dir}`);
    } catch (err) {
      status.classList.add("err");
      status.textContent = `install failed: ${err.message}`;
    }
  });

  $('[data-act=refresh-plugins]')?.addEventListener("click", async () => {
    const status = $("#plugins-status");
    status.className = "form-status";
    status.textContent = "refreshing…";
    try {
      const r = await fetch("/api/plugins/refresh", { method: "POST" });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
      reloadAfterAdminRestart(status, `discovered: ${j.discovered.join(", ")}`);
    } catch (err) {
      status.classList.add("err");
      status.textContent = `refresh failed: ${err.message}`;
    }
  });

  $$('[data-act=uninstall-plugin]').forEach(btn => {
    btn.addEventListener("click", async () => {
      const name = btn.dataset.pluginName;
      if (!confirm(`Uninstall plugin '${name}'? Removes its dir under ~/.local/share/usb-rtsp/plugins/.`)) return;
      const status = $("#plugins-status");
      status.className = "form-status";
      status.textContent = `uninstalling ${name}…`;
      btn.disabled = true;
      try {
        const r = await fetch(`/api/plugins/uninstall/${encodeURIComponent(name)}`, { method: "POST" });
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
        reloadAfterAdminRestart(status, `uninstalled ${name}`);
      } catch (err) {
        status.classList.add("err");
        status.textContent = `uninstall failed: ${err.message}`;
        btn.disabled = false;
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
  // The password is never displayed to humans:
  //   - dashboard URL pills are server-side rendered with creds embedded
  //     for external players to copy/paste
  //   - the iframe preview goes through /preview/<cam>/ proxy and doesn't
  //     need the password client-side
  // The settings card just shows the username, a manual Rotate button,
  // and the auto-rotate timer toggle.

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
      creds.hidden = false;
    } catch {}
  }

  async function loadAutoRotateState() {
    try {
      const r = await fetch("/api/auth/auto-rotate");
      const j = await r.json();
      $("#auto-rotate-toggle").checked = !!j.enabled;
      const sched = $("#auto-rotate-schedule");
      if (j.schedule) sched.value = j.schedule;
    } catch {}
  }

  async function saveAutoRotate() {
    const status = $("#auth-status");
    status.className = "form-status";
    const enabled = $("#auto-rotate-toggle").checked;
    const schedule = $("#auto-rotate-schedule").value;
    status.textContent = enabled ? `enabling ${schedule} auto-rotate…` : "disabling auto-rotate…";
    try {
      const r = await fetch("/api/auth/auto-rotate", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ enabled, schedule }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
      status.classList.add("ok");
      status.textContent = enabled
        ? `auto-rotate ${schedule} (next fire on systemd's calendar)`
        : "auto-rotate disabled";
      setTimeout(() => { status.textContent = ""; status.className = "form-status"; }, 4000);
    } catch (err) {
      status.classList.add("err");
      status.textContent = `error: ${err.message}`;
    }
  }

  $("#auto-rotate-toggle")?.addEventListener("change", saveAutoRotate);
  $("#auto-rotate-schedule")?.addEventListener("change", () => {
    if ($("#auto-rotate-toggle").checked) saveAutoRotate();
  });

  // Poll until at least one mediamtx path is back ready (or timeout).
  // Used after rotate so we don't reload the page while mediamtx is
  // still bringing the runOnInit ffmpeg up — otherwise the freshly-
  // rendered iframe srcs hit a half-ready server and the WebRTC
  // negotiation silently fails.
  async function waitForAnyPathReady(timeoutMs = 20000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        const r = await fetch("/api/paths");
        if (r.ok) {
          const d = await r.json();
          if ((d.items || []).some(p => p.ready === true || p.sourceReady === true)) {
            return true;
          }
        }
      } catch {}
      await new Promise(res => setTimeout(res, 500));
    }
    return false;
  }

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
      status.classList.add("ok");
      status.textContent = "rotated · waiting for mediamtx + ffmpeg to restart…";
      const ready = await waitForAnyPathReady(20000);
      // mediamtx reports path ready as soon as the publisher reconnects,
      // but its WebRTC stack needs another beat to accept WHEP offers.
      await new Promise(res => setTimeout(res, 800));
      status.textContent = ready
        ? "rotated · stream ready · reloading"
        : "rotated · stream slow to come back — reloading anyway";
      setTimeout(() => location.reload(), 600);
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
  loadAutoRotateState();
})();
