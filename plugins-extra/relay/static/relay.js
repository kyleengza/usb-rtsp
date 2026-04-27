(function () {
  const $ = (s, c = document) => c.querySelector(s);
  const $$ = (s, c = document) => Array.from(c.querySelectorAll(s));

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
      const w  = num("encode_width");         if (w)  enc.width = w;
      const h  = num("encode_height");        if (h)  enc.height = h;
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

  // Per-card edit (URL only for now via prompt; full form is the next step)
  $$('.card[data-relay] [data-act=edit-relay]').forEach(btn => {
    btn.addEventListener("click", async () => {
      const card = btn.closest("[data-relay]");
      const name = card.dataset.relay;
      let current = null;
      try {
        const r = await fetch("/api/relay/sources");
        const j = await r.json();
        current = (j.sources || []).find(s => s.name === name);
      } catch { current = {}; }
      if (!current) current = {};
      const newUrl = prompt(`Edit upstream URL for '${name}':`, current.url || "");
      if (newUrl === null || newUrl.trim() === current.url) return;
      try {
        const r = await fetch(`/api/relay/sources/${encodeURIComponent(name)}`, {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ url: newUrl.trim() }),
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
        location.reload();
      } catch (err) {
        alert(`edit failed: ${err.message}`);
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
