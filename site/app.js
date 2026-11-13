(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
  };

  const marks = store.get("radar.marks", {}); // url -> "applied" | "skip" | "save"
  const st = Object.assign(
    { win: 1, kind: "all", fields: [], places: [], term: "", q: "", noCanada: true, hideDone: true },
    store.get("radar.filters", {})
  );
  let data = { meta: null, jobs: [] };

  const ago = (t) => {
    const s = Date.now() / 1000 - t;
    if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  };
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function save() { store.set("radar.filters", st); }

  function setSeg(el, key, cast) {
    el.querySelectorAll("button").forEach((b) => {
      b.classList.toggle("on", String(st[key]) === b.dataset.v);
      b.onclick = () => { st[key] = cast(b.dataset.v); save(); setSeg(el, key, cast); render(); };
    });
  }
  function setChips(el, key) {
    el.querySelectorAll("button").forEach((b) => {
      b.classList.toggle("on", st[key].includes(b.dataset.v));
      b.onclick = () => {
        const i = st[key].indexOf(b.dataset.v);
        if (i >= 0) st[key].splice(i, 1); else st[key].push(b.dataset.v);
        save(); setChips(el, key); render();
      };
    });
  }

  function visible(j, now) {
    if (j.posted < now - st.win * 86400) return false;
    if (st.kind !== "all" && j.kind !== st.kind) return false;
    if (st.fields.length && !st.fields.includes(j.field)) return false;
    if (st.places.length && !st.places.some((p) => j.places.includes(p))) return false;
    if (st.noCanada && j.places.includes("Canada")) return false;
    if (st.term && !j.terms.includes(st.term)) return false;
    if (st.hideDone && (marks[j.url] === "applied" || marks[j.url] === "skip")) return false;
    if (st.q) {
      const hay = `${j.company} ${j.title} ${j.locations.join(" ")}`.toLowerCase();
      if (!st.q.toLowerCase().split(/\s+/).every((w) => hay.includes(w))) return false;
    }
    return true;
  }

  function row(j) {
    const m = marks[j.url] || "";
    const fresh = Date.now() / 1000 - j.posted < 6 * 3600;
    const locs = j.locations.join(" · ") + (j.more_locations ? ` +${j.more_locations}` : "");
    const tags = [
      `<span class="tag${j.kind === "co-op" ? " coop" : ""}">${j.kind}</span>`,
      `<span class="tag">${esc(j.field)}</span>`,
      ...j.terms.map((t) => `<span class="tag">${esc(t)}</span>`),
      j.sponsorship ? `<span class="tag">${esc(j.sponsorship)}</span>` : "",
    ].join(" ");
    return `<li class="job${m === "applied" || m === "skip" ? " done" : ""}" data-url="${esc(j.url)}">
      <div>
        <div><span class="c">${esc(j.company)}</span> — <a class="t" href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a></div>
        <div class="meta"><span class="age${fresh ? " fresh" : ""}">${ago(j.posted)}</span><span>${esc(locs)}</span>${tags}<span>via ${esc(j.sources.join(" + "))}</span></div>
      </div>
      <div class="acts">
        <button data-m="save" class="${m === "save" ? "on" : ""}">Save</button>
        <button data-m="applied" class="${m === "applied" ? "on" : ""}">Applied</button>
        <button data-m="skip" class="${m === "skip" ? "on" : ""}">Skip</button>
      </div>
    </li>`;
  }

  function render() {
    const now = Date.now() / 1000;
    const list = data.jobs.filter((j) => visible(j, now));
    $("jobs").innerHTML = list.map(row).join("");
    $("empty").hidden = list.length > 0;
    const co = list.filter((j) => j.kind === "co-op").length;
    $("count").textContent = `${list.length} jobs (${co} co-op) · window ${st.win === 1 ? "24 hours" : st.win + " days"}`;
  }

  $("jobs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-m]");
    if (!b) return;
    const url = b.closest(".job").dataset.url;
    marks[url] = marks[url] === b.dataset.m ? undefined : b.dataset.m;
    if (!marks[url]) delete marks[url];
    store.set("radar.marks", marks);
    render();
  });

  function init(d) {
    data = d;
    const m = d.meta;
    const gen = new Date(m.generated * 1000);
    $("status").textContent = `${m.count_24h} new in the last 24 h · updated ${ago(m.generated)} (${gen.toLocaleString()})`;
    $("health").textContent = "Source health: " + Object.entries(m.sources)
      .map(([k, v]) => `${k} ${v.ok ? "ok, " + v.rows + " live rows" : "FAILED"}`).join(" · ");

    const fields = [...new Set(d.jobs.map((j) => j.field))].sort();
    $("fields").innerHTML = fields.map((f) => `<button data-v="${esc(f)}">${esc(f)}</button>`).join("");
    const terms = [...new Set(d.jobs.flatMap((j) => j.terms))].sort();
    $("term").innerHTML = `<option value="">Any term</option>` + terms.map((t) => `<option>${esc(t)}</option>`).join("");
    $("term").value = terms.includes(st.term) ? st.term : "";

    setSeg($("win"), "win", Number);
    setSeg($("kind"), "kind", String);
    setChips($("fields"), "fields");
    setChips($("places"), "places");
    $("q").value = st.q;
    $("q").oninput = (e) => { st.q = e.target.value.trim(); save(); render(); };
    $("term").onchange = (e) => { st.term = e.target.value; save(); render(); };
    $("noCanada").checked = st.noCanada;
    $("noCanada").onchange = (e) => { st.noCanada = e.target.checked; save(); render(); };
    $("hideDone").checked = st.hideDone;
    $("hideDone").onchange = (e) => { st.hideDone = e.target.checked; save(); render(); };
    render();
  }

  fetch("data/jobs.json", { cache: "no-store" })
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(init)
    .catch((e) => { $("status").textContent = "Could not load jobs (" + e.message + "). Try again in a minute."; });
})();
