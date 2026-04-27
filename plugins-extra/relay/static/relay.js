(function () {
  const $ = (s, c = document) => c.querySelector(s);
  const $$ = (s, c = document) => Array.from(c.querySelectorAll(s));

  // Split a "WxH" resolution token (e.g. "1280x720") into integers, or
  // [null, null] for the empty / "upstream native" option.
  function splitRes(v) {
    const s = (v || "").toString().trim();
    if (!s) return [null, null];
    const m = s.match(/^(\d+)x(\d+)$/i);
    return m ? [parseInt(m[1], 10), parseInt(m[2], 10)] : [null, null];
  }

  function buildBody(form) {
    const fd = new FormData(form);
    const body = {
      name: (fd.get("name") || "").toString().trim(),
      url: (fd.get("url") || "").toString().trim(),
    };
    const u = (fd.get("user") || "").toString().trim();
    const p = (fd.get("pass") || "").toString();
    const t = (fd.get("transport") || "").toString().trim();
    if (u) body.user = u;
    if (p) body.pass = p;
    if (t) body.transport = t;
    if (fd.get("encode_enabled")) {
      const enc = { preset: fd.get("encode_preset") || "medium" };
      const num = (k) => {
        const v = (fd.get(k) || "").toString().trim();
        return v === "" ? null : parseInt(v, 10);
      };
      const bk = num("encode_bitrate_kbps"); if (bk) enc.bitrate_kbps = bk;
      const [w, h] = splitRes(fd.get("encode_resolution"));
      if (w && h) { enc.width = w; enc.height = h; }
      const f  = num("encode_fps");           if (f)  enc.fps = f;
      body.encode = enc;
    }
    return body;
  }

  // Add form
  const addForm = $("[data-relay-add]");
  if (addForm) {
    addForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const status = $("[data-relay-status]", addForm);
      status.className = "form-status";
      status.textContent = "adding…";
      const body = buildBody(addForm);
      try {
        const r = await fetch("/api/relay/sources", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
        status.classList.add("ok");
        status.textContent = `added · ${j.reload} · refreshing`;
        setTimeout(() => location.reload(), 1500);
      } catch (err) {
        status.classList.add("err");
        status.textContent = `error: ${err.message}`;
      }
    });

    $("[data-act=probe-source]", addForm)?.addEventListener("click", async () => {
      const status = $("[data-relay-status]", addForm);
      status.className = "form-status";
      status.textContent = "probing upstream…";
      const body = buildBody(addForm);
      const probe = { url: body.url };
      if (body.user) probe.user = body.user;
      if (body.pass) probe.pass = body.pass;
      if (body.transport) probe.transport = body.transport;
      try {
        const r = await fetch("/api/relay/sources/probe", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(probe),
        });
        const j = await r.json();
        if (j.ok) {
          status.classList.add("ok");
          status.textContent = `OK · ${j.codec} ${j.profile || ""} · ${j.width}x${j.height} @ ${j.fps} fps`;
        } else {
          status.classList.add("err");
          status.textContent = `probe failed: ${j.error || "unknown error"}`;
        }
      } catch (err) {
        status.classList.add("err");
        status.textContent = `probe error: ${err.message}`;
      }
    });
  }

  // Per-card delete
  $$('.card[data-relay] [data-act=delete-relay]').forEach(btn => {
    btn.addEventListener("click", async () => {
      const card = btn.closest("[data-relay]");
      const name = card.dataset.relay;
      if (!confirm(`Remove relay '${name}'?`)) return;
      btn.disabled = true;
      try {
        const r = await fetch(`/api/relay/sources/${encodeURIComponent(name)}`, { method: "DELETE" });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        location.reload();
      } catch (err) {
        btn.disabled = false;
        alert(`delete failed: ${err.message}`);
      }
    });
  });

  // Per-card Settings tab — toggles the inline form (mirrors the cameras
  // section's Settings tab UX so both plugins feel consistent).
  $$('.card[data-relay] [data-act=settings]').forEach(btn => {
    btn.addEventListener("click", () => {
      const card = btn.closest("[data-relay]");
      const wrap = $(".settings-wrap", card);
      if (!wrap) return;
      const isOpen = !wrap.hidden;
      wrap.hidden = isOpen;
      btn.classList.toggle("open", !isOpen);
      btn.textContent = isOpen ? "Settings ▾" : "Hide settings ▴";
    });
  });

  // Per-card form: PATCH /api/relay/sources/{name}.
  // The PATCH endpoint treats omitted fields as "keep current" (it skips
  // anything that's None on the model), so we omit empty optional fields
  // rather than send "" — matches the existing API contract. encode is
  // special: send null to clear, send the object to (re)set.
  function buildPatchBody(form) {
    const fd = new FormData(form);
    const body = { url: (fd.get("url") || "").toString().trim() };
    const u = (fd.get("user") || "").toString().trim();
    const p = (fd.get("pass") || "").toString();
    const t = (fd.get("transport") || "").toString().trim();
    if (u) body.user = u;
    if (p) body.pass = p;
    if (t) body.transport = t;
    if (fd.get("encode_enabled")) {
      const enc = { preset: fd.get("encode_preset") || "medium" };
      const num = (k) => {
        const v = (fd.get(k) || "").toString().trim();
        return v === "" ? null : parseInt(v, 10);
      };
      const bk = num("encode_bitrate_kbps"); if (bk) enc.bitrate_kbps = bk;
      const [w, h] = splitRes(fd.get("encode_resolution"));
      if (w && h) { enc.width = w; enc.height = h; }
      const f  = num("encode_fps");           if (f)  enc.fps = f;
      body.encode = enc;
    } else {
      body.encode = null;
    }
    return body;
  }

  $$('.card[data-relay] [data-relay-form]').forEach(form => {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const card = form.closest("[data-relay]");
      const name = card.dataset.relay;
      const status = $("[data-form-status]", form);
      status.className = "form-status";
      status.textContent = "saving…";
      try {
        const r = await fetch(`/api/relay/sources/${encodeURIComponent(name)}`, {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(buildPatchBody(form)),
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
        status.classList.add("ok");
        status.textContent = `saved · ${j.reload} · refreshing`;
        setTimeout(() => location.reload(), 1500);
      } catch (err) {
        status.classList.add("err");
        status.textContent = `error: ${err.message}`;
      }
    });

    // Probe using the form's current values (lets the user test a URL
    // change before saving). The .live-area Probe button keeps using
    // saved values — both are useful.
    $("[data-act=probe-form]", form)?.addEventListener("click", async () => {
      const status = $("[data-form-status]", form);
      status.className = "form-status";
      status.textContent = "probing upstream…";
      const fd = new FormData(form);
      const probe = { url: (fd.get("url") || "").toString().trim() };
      const u = (fd.get("user") || "").toString().trim();
      const p = (fd.get("pass") || "").toString();
      const t = (fd.get("transport") || "").toString().trim();
      if (u) probe.user = u;
      if (p) probe.pass = p;
      if (t) probe.transport = t;
      try {
        const r = await fetch("/api/relay/sources/probe", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(probe),
        });
        const j = await r.json();
        if (j.ok) {
          status.classList.add("ok");
          status.textContent = `OK · ${j.codec} ${j.profile || ""} · ${j.width}x${j.height} @ ${j.fps} fps`;
        } else {
          status.classList.add("err");
          status.textContent = `probe failed: ${j.error || "unknown error"}`;
        }
      } catch (err) {
        status.classList.add("err");
        status.textContent = `probe error: ${err.message}`;
      }
    });
  });

  // Per-card live preview — same /preview/<name>/ proxy the USB plugin uses.
  // Works for any mediamtx path regardless of plugin (just keyed on path name).
  function rebuildPreviewIframe(card, name) {
    const wrap = $(".preview", card);
    if (!wrap) return null;
    const old = $("[data-preview-frame]", wrap);
    const fresh = document.createElement("iframe");
    fresh.setAttribute("data-preview-frame", "");
    fresh.setAttribute("loading", "lazy");
    fresh.setAttribute("allow", "autoplay");
    fresh.setAttribute("allowfullscreen", "");
    fresh.src = `/preview/${encodeURIComponent(name)}/?t=${Date.now()}`;
    if (old) old.replaceWith(fresh); else wrap.appendChild(fresh);
    return fresh;
  }

  $$('.card[data-relay] [data-act=preview]').forEach(btn => {
    btn.addEventListener("click", () => {
      const card = btn.closest("[data-relay]");
      const name = card.dataset.relay;
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
        rebuildPreviewIframe(card, name);
      }
    });
  });

  $$('.card[data-relay] [data-act=preview-reconnect]').forEach(btn => {
    btn.addEventListener("click", () => {
      const card = btn.closest("[data-relay]");
      rebuildPreviewIframe(card, card.dataset.relay);
    });
  });

  // Per-card probe — tests upstream on demand without needing a subscriber
  $$('.card[data-relay] [data-act=probe-relay]').forEach(btn => {
    btn.addEventListener("click", async () => {
      const card = btn.closest("[data-relay]");
      const name = card.dataset.relay;
      const result = $('[data-probe-result]', card);
      result.className = "form-status";
      result.textContent = "probing upstream…";
      btn.disabled = true;
      try {
        const r = await fetch("/api/relay/sources");
        const j = await r.json();
        const src = (j.sources || []).find(s => s.name === name);
        if (!src) throw new Error("source not found in config");
        const probe = { url: src.url };
        if (src.user) probe.user = src.user;
        if (src.pass) probe.pass = src.pass;
        if (src.transport) probe.transport = src.transport;
        const pr = await fetch("/api/relay/sources/probe", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(probe),
        });
        const pj = await pr.json();
        if (pj.ok) {
          result.classList.add("ok");
          let txt = `OK · ${pj.codec} ${pj.profile || ""} · ${pj.width}x${pj.height} @ ${pj.fps} fps`;
          // WebRTC compatibility hint — mediamtx serves H.264 of any profile,
          // but browsers (esp. Safari) often only decode Constrained Baseline.
          const codec = (pj.codec || "").toLowerCase();
          const profile = (pj.profile || "").toLowerCase();
          const webrtcRisky = (codec === "hevc" || codec === "h265" || codec === "mjpeg" ||
                               (codec === "h264" && !profile.includes("baseline")));
          if (webrtcRisky && !src.encode) {
            txt += " · WebRTC may fail — Edit › Re-encode";
            result.classList.add("warn");
          }
          result.textContent = txt;
        } else {
          result.classList.add("err");
          result.textContent = `failed: ${pj.error || "unknown error"}`;
        }
      } catch (err) {
        result.classList.add("err");
        result.textContent = `error: ${err.message}`;
      } finally {
        btn.disabled = false;
      }
    });
  });

  // Copy buttons
  $$('.relays .copy[data-copy]').forEach(btn => {
    btn.addEventListener("click", async () => {
      const orig = btn.textContent;
      const ok = await window.copyText(btn.dataset.copy);
      btn.classList.toggle("ok", ok);
      btn.textContent = ok ? "copied" : "select+ctrl-c";
      setTimeout(() => { btn.textContent = orig; btn.classList.remove("ok"); }, 1500);
    });
  });
})();
