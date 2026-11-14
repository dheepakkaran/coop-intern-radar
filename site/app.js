(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
  };

  const marks = store.get("radar.marks", {}); // url -> "applied" | "skip" | "save"
  const st = Object.assign(
    { win: 1, kind: "all", fields: [], places: [], term: "", q: "", hideDone: true },
    store.get("radar.filters", {})
  );
  if (![1, 2].includes(st.win)) st.win = 1;
  st.fields = (st.fields || []).filter((f) => ["AI/ML", "Data", "Software"].includes(f));
  let data = { meta: null, jobs: [] };
  const open = new Set();

  const ago = (t) => {
    const s = Date.now() / 1000 - t;
    if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const save = () => store.set("radar.filters", st);

  // ---- the checks, grouped ----------------------------------------------------------------------------
  // state: ok (clear), warn (review), unk (not stated). A failed check never reaches the page: the job is hidden.
  const GROUPS = [
    ["Work authorization", [
      ["citizenship", "Citizenship", { not_required: ["ok", "Not required"] }],
      ["cpt", "CPT / F-1", { welcome: ["ok", "CPT welcome"], silent: ["ok", "Not rejected"] }],
      ["sponsorship", "Sponsorship", { sponsors: ["ok", "Sponsors"], silent: ["ok", "Not mentioned"],
        no_sponsorship: ["warn", "No sponsorship: often fine for CPT"] }],
    ]],
    ["Education", [
      ["degree", "Degree", { ms_ok: ["ok", "Master's students OK"], unclear: ["unk", "Not stated"],
        ug_preferred: ["warn", "Bachelor's preferred"] }],
      ["grad", "Graduation", { fits: ["ok", "Window includes May 2028"], not_stated: ["unk", "Not stated"] }],
    ]],
  ];

  function status(e) {
    if (!e || !e.labels) return ["unk", "Unverified"];
    if (e.auth === "check" || e.degree === "check" || (e.disagree || []).length) return ["warn", "Review"];
    return ["ok", "All checks passed"];
  }

  function checkState(e, key, map) {
    const v = e.labels[key];
    const [s, word] = map[v] || ["warn", v];
    const dis = (e.disagree || []).includes(key);
    return { s: dis ? "warn" : s, word: dis ? `${word} (readers disagree)` : word, quote: (e.quotes || {})[key] || "" };
  }

  function checksStrip(e) {
    if (!e || !e.labels) {
      return `<div class="checks"><div class="cg"><span class="ck"><i class="dot unk"></i>No job description could be read: check the posting yourself</span></div></div>`;
    }
    return `<div class="checks">${GROUPS.map(([name, items]) => `<div class="cg"><span class="cg-name">${name}</span>${
      items.map(([k, label, map]) => {
        const c = checkState(e, k, map);
        return `<span class="ck" title="${esc(`${label}: ${c.word}${c.quote ? " — " + c.quote : ""}`)}"><i class="dot ${c.s}"></i>${label}</span>`;
      }).join("")}</div>`).join("")}</div>`;
  }

  function detail(j) {
    const e = j.elig;
    const secs = [];
    if (e && e.labels) {
      for (const [name, items] of GROUPS) {
        secs.push(`<div class="dsec"><h4>${name}</h4>${items.map(([k, label, map]) => {
          const c = checkState(e, k, map);
          return `<div class="drow"><b><i class="dot ${c.s}"></i>${label}</b><span><span class="v">${esc(c.word)}</span>${
            c.quote ? `<span class="why"><q>${esc(c.quote)}</q></span>` : ""}</span></div>`;
        }).join("")}</div>`);
      }
    }
    const role = [
      ["Field", j.field + (j.why === "JD" ? ` (from the description: ${j.jd_terms.join(", ")})` : "")],
      ["Type", j.kind + (j.terms.length ? ` · ${j.terms.join(", ")}` : "")],
      ["Location", (j.locations.join(" · ") || "Not listed") + (j.more_locations ? ` +${j.more_locations} more` : "")],
    ];
    secs.push(`<div class="dsec"><h4>Role</h4>${role.map(([k, v]) => `<div class="drow"><b>${k}</b><span class="v">${esc(v)}</span></div>`).join("")}</div>`);
    const reader = !e || !e.labels ? "no description" : e.ai === "read" ? "rules + AI reader (quotes verified)" : "rules";
    secs.push(`<p class="dfoot">Read by ${reader} · found via ${esc(j.sources.join(", "))} · first seen ${ago(j.posted)}</p>`);
    return `<div class="detail">${secs.join("")}</div>`;
  }

  // ---- filters ----------------------------------------------------------------------------------------
  function visible(j, now) {
    if (j.posted < now - st.win * 86400) return false;
    if (st.kind !== "all" && j.kind !== st.kind) return false;
    if (st.fields.length && !st.fields.includes(j.field)) return false;
    if (st.places.length && !st.places.some((p) => j.places.includes(p))) return false;
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
    const [s, label] = status(j.elig);
    const extra = Math.max(0, j.locations.length - 2) + (j.more_locations || 0);
    const loc = (j.locations.slice(0, 2).join(" · ") || "Location not listed") + (extra ? ` +${extra}` : "");
    const pills = [`<span class="pill${j.kind === "co-op" ? " coop" : ""}">${j.kind === "co-op" ? "Co-op" : "Intern"}</span>`,
      `<span class="pill">${esc(j.field)}</span>`, ...j.terms.slice(0, 2).map((t) => `<span class="pill">${esc(t)}</span>`)];
    return `<li class="job${m === "applied" || m === "skip" ? " done" : ""}${open.has(j.url) ? " open" : ""}" data-url="${esc(j.url)}">
      <div class="top"><div><span class="co">${esc(j.company)}</span><span class="age${fresh ? " fresh" : ""}">${ago(j.posted)}</span></div>
        <span class="badge ${s}">${label}</span></div>
      <a class="title" href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a>
      <div class="meta">${pills.join("")}<span>${esc(loc)}</span></div>
      ${checksStrip(j.elig)}
      <div class="bottom">
        <button class="more" data-open aria-expanded="${open.has(j.url)}">Details<svg viewBox="0 0 12 12" aria-hidden="true"><path d="m2 4 4 4 4-4"/></svg></button>
        <div class="acts">
          <button data-m="save" class="${m === "save" ? "on" : ""}">Save</button>
          <button data-m="applied" class="${m === "applied" ? "on" : ""}">Applied</button>
          <button data-m="skip" class="${m === "skip" ? "on" : ""}">Skip</button>
        </div>
      </div>
      ${detail(j)}
    </li>`;
  }

  function render() {
    const now = Date.now() / 1000;
    const list = data.jobs.filter((j) => visible(j, now));
    $("jobs").innerHTML = list.map(row).join("");
    $("empty").hidden = list.length > 0;
    const co = list.filter((j) => j.kind === "co-op").length;
    $("count").textContent = `${list.length} roles · ${co} co-op · all checks passed`;
  }

  function seg(el, key, cast) {
    el.querySelectorAll("button").forEach((b) => {
      b.classList.toggle("on", String(st[key]) === b.dataset.v);
      b.onclick = () => { st[key] = cast(b.dataset.v); save(); seg(el, key, cast); render(); };
    });
  }
  function chips(el, key) {
    el.querySelectorAll("button").forEach((b) => {
      b.classList.toggle("on", st[key].includes(b.dataset.v));
      b.onclick = () => {
        const i = st[key].indexOf(b.dataset.v);
        if (i >= 0) st[key].splice(i, 1); else st[key].push(b.dataset.v);
        save(); chips(el, key); render();
      };
    });
  }

  $("jobs").addEventListener("click", (ev) => {
    const li = ev.target.closest(".job");
    if (!li) return;
    const url = li.dataset.url;
    if (ev.target.closest("[data-open]")) {
      if (open.has(url)) open.delete(url); else open.add(url);
      li.classList.toggle("open");
      ev.target.closest("[data-open]").setAttribute("aria-expanded", li.classList.contains("open"));
      return;
    }
    const b = ev.target.closest("button[data-m]");
    if (!b) return;
    marks[url] = marks[url] === b.dataset.m ? undefined : b.dataset.m;
    if (!marks[url]) delete marks[url];
    store.set("radar.marks", marks);
    render();
  });

  function init(d) {
    data = d;
    const m = d.meta;
    const now = Date.now() / 1000;
    $("s24").textContent = d.jobs.filter((j) => j.posted >= now - 86400).length;
    $("s48").textContent = d.jobs.length;
    $("supd").textContent = ago(m.generated);
    $("supd").title = new Date(m.generated * 1000).toLocaleString();
    $("health").textContent = Object.entries(m.sources).map(([k, v]) => {
      if (!v.ok) return `${k}: failed`;
      if (v.boards !== undefined) return `${k}: ${v.boards - v.failed}/${v.boards} boards`;
      return `${k}: ${v.rows} rows`;
    }).join(" · ") + (m.polled ? ` · boards checked ${ago(m.polled)}` : "") +
      (m.ineligible_skipped !== undefined ? ` · hidden: ${m.not_tech_skipped} non-tech, ${m.ineligible_skipped} failed a check` +
        (m.review_skipped !== undefined ? `, ${m.review_skipped} need review, ${m.unverified_skipped} unverified` : "") : "");

    const fields = [...new Set(d.jobs.map((j) => j.field))].sort();
    $("fields").innerHTML = fields.map((f) => `<button data-v="${esc(f)}">${esc(f)}</button>`).join("");
    const terms = [...new Set(d.jobs.flatMap((j) => j.terms))].sort();
    $("term").innerHTML = `<option value="">Any</option>` + terms.map((t) => `<option>${esc(t)}</option>`).join("");
    $("term").value = terms.includes(st.term) ? st.term : "";

    seg($("win"), "win", Number);
    seg($("kind"), "kind", String);
    chips($("fields"), "fields");
    chips($("places"), "places");
    $("q").value = st.q;
    $("q").oninput = (e) => { st.q = e.target.value.trim(); save(); render(); };
    $("term").onchange = (e) => { st.term = e.target.value; save(); render(); };
    $("hideDone").checked = st.hideDone;
    $("hideDone").onchange = (e) => { st.hideDone = e.target.checked; save(); render(); };
    render();
  }

  fetch("data/jobs.json", { cache: "no-store" })
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(init)
    .catch((e) => { $("count").textContent = "Could not load jobs (" + e.message + "). Try again in a minute."; });
})();
