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

  // ─── foldable block (plugins + settings sections) ──────────────────────
  // A "block" is any element matching .plugin-block or .settings-block with
  // an inner .plugin-body or .settings-body. The header carries either
  // [data-act=fold-plugin] (legacy) or [data-act=fold-block] as the toggle.
  const BLOCK_SEL = '.plugin-block, .settings-block';
  const BODY_SEL  = '.plugin-body, .settings-body';
  const FOLD_SEL  = '[data-act=fold-plugin], [data-act=fold-block]';

  function setPluginFold(block, open) {
    const body = block?.querySelector(BODY_SEL);
    const btn  = block?.querySelector(FOLD_SEL);
    if (!body || !btn) return;
    body.hidden = !open;
    btn.classList.toggle("open", open);
    btn.textContent = open ? "Hide ▴" : "Details ▾";
  }

  $$(FOLD_SEL).forEach(btn => {
    btn.addEventListener("click", () => {
      const block = btn.closest(BLOCK_SEL);
      const body = block?.querySelector(BODY_SEL);
      if (!body) return;
      setPluginFold(block, body.hidden);
    });
  });

  // Open the targeted plugin block when /settings is loaded with a
  // hash like #plugin-relay (e.g. follow the dashboard "no relays" hint).
  // Plugin cards are nested inside the Plugins settings-block now, so we
  // also have to unfold that parent first.
  function openPluginByHash() {
    const m = location.hash.match(/^#plugin-([a-z0-9_-]+)$/i);
    if (!m) return;
    const block = document.getElementById(`plugin-${m[1]}`);
    if (block) {
      const parent = block.closest('.settings-block');
      if (parent && parent !== block) setPluginFold(parent, true);
      setPluginFold(block, true);
      block.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }
  openPluginByHash();
  window.addEventListener("hashchange", openPluginByHash);

  // ─── stream credential rotation ────────────────────────────────────────
  // The password is never displayed to humans:
  //   - dashboard URL pills are server-side rendered with creds embedded
  //     for external players to copy/paste
  //   - the iframe preview goes through /preview/<cam>/ proxy and doesn't
  //     need the password client-side
  // The settings card just shows the username, a manual Rotate button,
  // and the auto-rotate timer toggle.

  function setSettingsStatus(name, text, kind) {
    const el = document.querySelector(`[data-settings-status="${name}"]`);
    if (!el) return;
    el.textContent = text;
    el.classList.remove("ok", "warn", "err");
    if (kind) el.classList.add(kind);
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
        setSettingsStatus("auth", "stream auth off", "warn");
        return;
      }
      status.classList.add("ok");
      status.textContent = "stream auth enabled";
      $("#stream-user").textContent = j.user;
      creds.hidden = false;
      setSettingsStatus("auth", `${j.user} · stream auth on`, "ok");
    } catch {
      setSettingsStatus("auth", "fetch failed", "err");
    }
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

  // ─── WebRTC public access ─────────────────────────────────────────────

  function fmtAgo(iso) {
    if (!iso) return "never";
    const t = Date.parse(iso);
    if (isNaN(t)) return "—";
    const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (s < 60)   return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s/60)}m${s%60 ? ` ${s%60}s` : ""} ago`;
    return `${Math.floor(s/3600)}h${Math.floor((s%3600)/60)}m ago`;
  }

  function setVal(id, v) {
    const el = document.getElementById(id);
    if (el) el.value = v == null ? "" : v;
  }
  function setText(id, v) {
    const el = document.getElementById(id);
    if (el) el.textContent = v;
  }
  function setChecked(id, v) {
    const el = document.getElementById(id);
    if (el) el.checked = !!v;
  }
  function getVal(id, fallback = "") {
    const el = document.getElementById(id);
    return el ? el.value : fallback;
  }
  function getChecked(id) {
    const el = document.getElementById(id);
    return el ? !!el.checked : false;
  }

  async function loadWebrtcState() {
    if (!document.getElementById("webrtc-current-ip")) return;  // not on this page
    let j;
    try {
      const r = await fetch("/api/webrtc/state");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      j = await r.json();
    } catch (err) {
      setText("webrtc-current-ip", "—");
      setText("webrtc-current-source", `state fetch failed: ${err.message}`);
      return;
    }
    setText("webrtc-current-ip", j.public_ip || "(none yet)");
    setText("webrtc-current-source",
      j.source ? `via ${String(j.source).toUpperCase()}` : (j.last_error || ""));
    setSettingsStatus(
      "webrtc",
      j.public_ip ? `${j.public_ip} · ${j.source || "—"}` : "no public IP",
      j.public_ip ? "ok" : "warn",
    );
    const sessions = j.active_webrtc_sessions || 0;
    setText("webrtc-last-detected",
      `last detected ${fmtAgo(j.last_detected_at)} · ${sessions} active session${sessions === 1 ? "" : "s"}`);
    setVal("webrtc-public-host", j.configured_host || "");
    setVal("webrtc-echo-url",    j.ip_echo_url    || "https://ifconfig.me");
    setVal("webrtc-refresh-min", j.refresh_minutes || 30);
    setChecked("webrtc-auto-detect", j.auto_detect !== false);
  }

  const detectBtn = document.getElementById("webrtc-detect-btn");
  if (detectBtn) {
    detectBtn.addEventListener("click", async () => {
      const result = document.getElementById("webrtc-detect-result");
      detectBtn.disabled = true;
      if (result) result.textContent = "detecting…";
      try {
        const r = await fetch("/api/webrtc/detect", { method: "POST" });
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
        const parts = [`got ${j.ip || "(none)"}`];
        if (j.changed)   parts.push("changed");
        if (j.restarted) parts.push("mediamtx restarted");
        if (j.deferred === "active_viewers") parts.push("restart deferred (viewers active)");
        if (result) result.textContent = parts.join(" · ");
      } catch (err) {
        if (result) result.textContent = `error: ${err.message}`;
      } finally {
        detectBtn.disabled = false;
        loadWebrtcState();
      }
    });
  }

  const webrtcForm = document.getElementById("webrtc-form");
  if (webrtcForm) {
    webrtcForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const status = document.getElementById("webrtc-form-status");
      if (status) { status.className = "form-status"; status.textContent = "saving…"; }
      const body = {
        public_host:     getVal("webrtc-public-host").trim(),
        ip_echo_url:     getVal("webrtc-echo-url").trim() || "https://ifconfig.me",
        refresh_minutes: parseInt(getVal("webrtc-refresh-min"), 10) || 30,
        auto_detect:     getChecked("webrtc-auto-detect"),
      };
      try {
        const r = await fetch("/api/webrtc/settings", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
        const parts = ["saved"];
        if (j.ip) parts.push(`ip=${j.ip}`);
        if (j.changed)   parts.push("changed");
        if (j.restarted) parts.push("mediamtx restarted");
        if (j.deferred === "active_viewers") parts.push("restart deferred (viewers active)");
        if (status) { status.classList.add("ok"); status.textContent = parts.join(" · "); }
        loadWebrtcState();
      } catch (err) {
        if (status) { status.classList.add("err"); status.textContent = `error: ${err.message}`; }
      }
    });
  }

  // ─── UFW (firewall) management ───────────────────────────────────────

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  async function loadUfwState() {
    if (!document.getElementById("ufw-section")) return;
    let j;
    try {
      const r = await fetch("/api/ufw/state");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      j = await r.json();
    } catch (err) {
      setText("ufw-status-hint", `state fetch failed: ${err.message}`);
      return;
    }
    const badge = document.getElementById("ufw-status-badge");
    if (badge) {
      badge.classList.remove("ok", "warn", "err");
      if (!j.sudo_ok) {
        badge.classList.add("warn"); badge.textContent = "sudo missing";
      } else if (j.active) {
        badge.classList.add("ok"); badge.textContent = "active";
      } else {
        badge.classList.add("err"); badge.textContent = "inactive";
      }
    }
    if (!j.sudo_ok) {
      setSettingsStatus("ufw", "sudo not configured", "warn");
    } else {
      const blocks = (j.blocks || []).length;
      const summary = `${j.active ? "active" : "inactive"} · ${(j.managed || []).length} managed${blocks ? ` · ${blocks} blocked` : ""}`;
      setSettingsStatus("ufw", summary, j.active ? "ok" : "err");
    }
    setText("ufw-status-hint", j.lan_cidr ? `LAN scope = ${j.lan_cidr}` : "LAN CIDR unknown");
    document.getElementById("ufw-sudo-warn").hidden = !!j.sudo_ok;

    const toggleBtn = document.getElementById("ufw-toggle-active");
    if (toggleBtn) {
      toggleBtn.hidden = !j.sudo_ok;
      toggleBtn.textContent = j.active ? "Disable UFW" : "Enable UFW";
      toggleBtn.dataset.action = j.active ? "disable" : "enable";
    }

    // Managed ports — grouped by purpose (Admin / RTSP / HLS / WebRTC),
    // each group rendered into its own subtable so you can scan + bulk-flip.
    const managedHost = document.getElementById("ufw-managed-host");
    if (managedHost) {
      const items = j.managed || [];
      const groups = [];
      const seen = new Map();
      for (const m of items) {
        const g = m.group || "Other";
        if (!seen.has(g)) {
          seen.set(g, groups.length);
          groups.push({ name: g, items: [] });
        }
        groups[seen.get(g)].items.push(m);
      }
      const dis = j.sudo_ok ? "" : "disabled";
      managedHost.innerHTML = groups.map(g => {
        const rows = g.items.map(m => {
          const scopes = ["lan", "anywhere", "off"];
          const pills = scopes.map(sc => {
            const active = sc === m.scope ? `active scope-${sc}` : "";
            return `<button type="button" class="${active}" data-port="${m.port}" data-proto="${m.proto}" data-scope="${sc}" ${dis}>${sc}</button>`;
          }).join("");
          const nums = (m.numbers || []).map(n => `#${n}`).join(", ");
          const numsCell = nums ? `<span class="hint" style="font-size:0.7rem">rules ${escapeHtml(nums)}</span>` : `<span class="hint" style="font-size:0.7rem">no rule</span>`;
          return `<tr>
            <td class="port">${m.port}/${m.proto}<br>${numsCell}</td>
            <td>${escapeHtml(m.label)}<br><span class="hint" style="font-size:0.7rem">${escapeHtml(m.comment || "")}</span></td>
            <td><div class="scope-pills">${pills}</div></td>
            <td>${m.warn_off ? `<span class="hint" style="color:var(--warn)">⚠ panel port</span>` : ""}</td>
          </tr>`;
        }).join("");
        const bulk = ["lan", "anywhere", "off"].map(sc =>
          `<button type="button" class="" data-ufw-group-bulk="${escapeHtml(g.name)}" data-scope="${sc}" ${dis}>all ${sc}</button>`
        ).join("");
        return `<div class="ufw-group" data-ufw-group="${escapeHtml(g.name)}">
          <div class="ufw-group-head">
            <span class="ufw-group-title">${escapeHtml(g.name)}</span>
            <div class="scope-pills ufw-group-bulk">${bulk}</div>
          </div>
          <table class="ufw-table"><tbody>${rows}</tbody></table>
        </div>`;
      }).join("");
    }

    // Blocklist table — DENY-from-<X> entries (no port restriction).
    const blocksBody = document.getElementById("ufw-blocks-body");
    if (blocksBody) {
      const blocks = j.blocks || [];
      blocksBody.innerHTML = blocks.length
        ? blocks.map(b => `<tr>
            <td>#${b.number}</td>
            <td class="port">${escapeHtml(b.source)}${b.v6 ? ' <span class="hint" style="font-size:0.7rem">(v6)</span>' : ''}</td>
            <td>${escapeHtml(b.comment || "")}</td>
            <td>${j.sudo_ok ? `<button type="button" class="danger" data-ufw-unblock="${escapeHtml(b.source)}">×</button>` : ""}</td>
          </tr>`).join("")
        : `<tr><td colspan="4" class="empty">no blocks</td></tr>`;
    }

    // Other rules table — each row has a delete button (sudo permitting).
    const otherBody = document.getElementById("ufw-other-body");
    if (otherBody) {
      const others = j.other || [];
      otherBody.innerHTML = others.length
        ? others.map(r => `<tr>
            <td>#${r.number}</td>
            <td class="port">${escapeHtml(r.to)}</td>
            <td>${escapeHtml(r.action)}</td>
            <td>${escapeHtml(r.from)}</td>
            <td>${escapeHtml(r.comment || "")}</td>
            <td>${j.sudo_ok ? `<button type="button" class="danger" data-ufw-delete="${r.number}" data-ufw-summary="${escapeHtml(r.to + ' from ' + r.from)}">×</button>` : ""}</td>
          </tr>`).join("")
        : `<tr><td colspan="6" class="empty">none</td></tr>`;
      setText("ufw-other-count", `(${others.length})`);
    }
  }

  async function deleteUfwRule(number, summary) {
    if (!confirm(`Delete rule #${number} (${summary})? This is non-reversible.`)) return;
    const status = document.getElementById("ufw-status");
    if (status) { status.className = "form-status"; status.textContent = `deleting #${number}…`; }
    try {
      const r = await fetch("/api/ufw/delete", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ number }),
      });
      const j = await r.json();
      if (!j.ok) throw new Error(j.error || j.output || `HTTP ${r.status}`);
      if (status) { status.classList.add("ok"); status.textContent = `deleted #${number} · ${j.output || "ok"}`; }
    } catch (err) {
      if (status) { status.classList.add("err"); status.textContent = `error: ${err.message}`; }
    } finally {
      loadUfwState();
    }
  }

  async function setUfwScope(port, proto, scope, label) {
    const status = document.getElementById("ufw-status");
    if (status) { status.className = "form-status"; status.textContent = `applying ${port}/${proto} → ${scope}…`; }
    if (label === "Admin panel" && scope === "off") {
      if (!confirm(`Closing the panel port (${port}/${proto}) will lock you out of this UI immediately. Continue?`)) {
        if (status) status.textContent = "";
        loadUfwState();
        return;
      }
    }
    if (label === "Admin panel" && scope === "anywhere") {
      if (!confirm("Exposing the panel port to the public internet means anyone can attempt to log in. Make sure you've enabled panel auth (./install.sh --enable-auth) and rotated your stream creds. Continue?")) {
        if (status) status.textContent = "";
        loadUfwState();
        return;
      }
    }
    try {
      const r = await fetch("/api/ufw/port", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ port, proto, scope }),
      });
      const j = await r.json();
      if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
      if (status) {
        status.classList.add("ok");
        status.textContent = `${port}/${proto} → ${j.scope_after}`;
      }
    } catch (err) {
      if (status) {
        status.classList.add("err");
        status.textContent = `error: ${err.message}`;
      }
    } finally {
      loadUfwState();
    }
  }

  async function unblockUfwSource(source) {
    if (!confirm(`Unblock ${source}? UFW will allow connections from this source again.`)) return;
    const status = document.getElementById("ufw-status");
    if (status) { status.className = "form-status"; status.textContent = `unblocking ${source}…`; }
    try {
      const r = await fetch("/api/ufw/unblock", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ source }),
      });
      const j = await r.json();
      if (!j.ok) throw new Error(j.error || `HTTP ${r.status}`);
      if (status) { status.classList.add("ok"); status.textContent = `unblocked ${source} · removed ${j.deleted} rule(s)`; }
    } catch (err) {
      if (status) { status.classList.add("err"); status.textContent = `error: ${err.message}`; }
    } finally {
      loadUfwState();
    }
  }

  document.getElementById("ufw-block-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const status = document.getElementById("ufw-status");
    const source = (document.getElementById("ufw-block-source")?.value || "").trim();
    const reason = (document.getElementById("ufw-block-reason")?.value || "").trim();
    if (!source) {
      if (status) { status.className = "form-status err"; status.textContent = "source is required"; }
      return;
    }
    if (status) { status.className = "form-status"; status.textContent = `blocking ${source}…`; }
    try {
      const r = await fetch("/api/ufw/block", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ source, reason }),
      });
      const j = await r.json();
      if (!r.ok || j.ok === false) throw new Error(j.detail || j.error || `HTTP ${r.status}`);
      if (status) {
        status.classList.add("ok");
        const kicked = (j.kicked || []).length;
        status.textContent = `blocked ${source}${kicked ? ` · kicked ${kicked} session(s)` : ""}`;
      }
      document.getElementById("ufw-block-source").value = "";
      document.getElementById("ufw-block-reason").value = "";
    } catch (err) {
      if (status) { status.classList.add("err"); status.textContent = `error: ${err.message}`; }
    } finally {
      loadUfwState();
    }
  });

  async function setUfwGroupScope(groupName, scope) {
    const status = document.getElementById("ufw-status");
    if (!confirm(`Set every ${groupName} port to '${scope}'? You'll see one toggle per port apply.`)) return;
    if (status) { status.className = "form-status"; status.textContent = `${groupName} → ${scope}…`; }
    // Pull the current managed list to find which (port, proto) belong to this group.
    let state;
    try {
      state = await fetch("/api/ufw/state").then(r => r.json());
    } catch (err) {
      if (status) { status.classList.add("err"); status.textContent = `state fetch failed: ${err.message}`; }
      return;
    }
    const targets = (state.managed || []).filter(m => (m.group || "Other") === groupName);
    let okCount = 0, errMsg = "";
    for (const m of targets) {
      try {
        const r = await fetch("/api/ufw/port", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ port: m.port, proto: m.proto, scope }),
        });
        const j = await r.json();
        if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
        okCount++;
      } catch (err) {
        errMsg = err.message;
        break;
      }
    }
    if (status) {
      if (okCount === targets.length) {
        status.classList.add("ok");
        status.textContent = `${groupName} → ${scope} · ${okCount}/${targets.length} applied`;
      } else {
        status.classList.add("err");
        status.textContent = `${groupName}: ${okCount}/${targets.length} applied; stopped at ${errMsg}`;
      }
    }
    loadUfwState();
  }

  document.addEventListener("click", (e) => {
    const bulkBtn = e.target.closest("button[data-ufw-group-bulk]");
    if (bulkBtn && !bulkBtn.disabled) {
      setUfwGroupScope(bulkBtn.dataset.ufwGroupBulk, bulkBtn.dataset.scope);
      return;
    }
    const scopeBtn = e.target.closest("#ufw-managed-host button[data-port]");
    if (scopeBtn && !scopeBtn.disabled) {
      const row = scopeBtn.closest("tr");
      const label = row?.querySelector("td:nth-child(2)")?.firstChild?.textContent || "";
      setUfwScope(parseInt(scopeBtn.dataset.port, 10), scopeBtn.dataset.proto, scopeBtn.dataset.scope, label.trim());
      return;
    }
    const delBtn = e.target.closest("button[data-ufw-delete]");
    if (delBtn && !delBtn.disabled) {
      deleteUfwRule(parseInt(delBtn.dataset.ufwDelete, 10), delBtn.dataset.ufwSummary || "");
      return;
    }
    const unblockBtn = e.target.closest("button[data-ufw-unblock]");
    if (unblockBtn && !unblockBtn.disabled) {
      unblockUfwSource(unblockBtn.dataset.ufwUnblock);
    }
  });

  document.getElementById("ufw-refresh")?.addEventListener("click", loadUfwState);
  document.getElementById("ufw-toggle-active")?.addEventListener("click", async (e) => {
    const action = e.currentTarget.dataset.action;
    if (action === "disable" && !confirm("Disabling UFW means every port is open until re-enabled. Continue?")) return;
    const status = document.getElementById("ufw-status");
    if (status) { status.className = "form-status"; status.textContent = `${action}…`; }
    try {
      const r = await fetch(`/api/ufw/${action}`, { method: "POST" });
      const j = await r.json();
      if (!j.ok) throw new Error(j.error || j.output || `HTTP ${r.status}`);
      if (status) { status.classList.add("ok"); status.textContent = j.output || "ok"; }
    } catch (err) {
      if (status) { status.classList.add("err"); status.textContent = `error: ${err.message}`; }
    } finally {
      loadUfwState();
    }
  });

  // Boot
  loadStreamCreds();
  loadAutoRotateState();
  loadWebrtcState();
  loadUfwState();
  setInterval(loadWebrtcState, 30000);
  setInterval(loadUfwState, 60000);
})();
