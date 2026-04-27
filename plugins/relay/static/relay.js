(function () {
  const $ = (s, c = document) => c.querySelector(s);
  const $$ = (s, c = document) => Array.from(c.querySelectorAll(s));

  async function refreshRelayCards() {
    let data;
    try { data = await fetch("/api/paths").then(r => r.json()); }
    catch { return; }
    const items = data.items || [];
    for (const card of $$('.card[data-relay]')) {
      const name = card.dataset.relay;
      const item = items.find(p => p.name === name);
      const dot = $("[data-ready]", card);
      const readers = $("[data-readers]", card);
      const bytes = $("[data-bytes]", card);
      if (!item) {
        dot.classList.remove("ok", "err");
        readers.textContent = "(no path)";
        bytes.textContent = "—";
        continue;
      }
      const ready = item.ready === true || item.sourceReady === true;
      dot.classList.toggle("ok", ready);
      dot.classList.toggle("err", !ready);
      readers.textContent = `${item.readers_count || 0} viewer${item.readers_count === 1 ? "" : "s"}`;
      bytes.textContent = item.bytesReceived_h || "—";
    }
  }

  function wireUp() {
    // Delete buttons
    $$('.card[data-relay] [data-act=delete-relay]').forEach(btn => {
      btn.addEventListener("click", async () => {
        const card = btn.closest("[data-relay]");
        const name = card.dataset.relay;
        if (!confirm(`Remove relay '${name}'?`)) return;
        const orig = btn.textContent;
        btn.disabled = true;
        btn.textContent = "…";
        try {
          const r = await fetch(`/api/relay/sources/${encodeURIComponent(name)}`, { method: "DELETE" });
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          location.reload();
        } catch (err) {
          btn.disabled = false;
          btn.textContent = orig;
          alert(`delete failed: ${err.message}`);
        }
      });
    });

    // Copy buttons (reuse window.copyText from core)
    $$('.relays .copy[data-copy]').forEach(btn => {
      btn.addEventListener("click", async () => {
        const orig = btn.textContent;
        const ok = await window.copyText(btn.dataset.copy);
        btn.classList.toggle("ok", ok);
        btn.textContent = ok ? "copied" : "select+ctrl-c";
        setTimeout(() => { btn.textContent = orig; btn.classList.remove("ok"); }, 1500);
      });
    });

    // Add form
    const form = $("[data-relay-add]");
    if (form) {
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const status = $("[data-relay-status]", form);
        status.className = "form-status";
        status.textContent = "adding…";
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
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    wireUp();
    refreshRelayCards();
    setInterval(refreshRelayCards, 3000);
  });
})();
