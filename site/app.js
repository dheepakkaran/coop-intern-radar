(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
  };

  const marks = store.get("radar.marks", {}); // url -> "applied" | "skip" | "save"
  const st = Object.assign(
    { view: "match", win: 1, kind: "all", fields: [], places: [], term: "", q: "", hideDone: true, cc: false },
    store.get("radar.filters", {})
  );
  if (![1, 2].includes(st.win)) st.win = 1;
  if (!["match", "all", "repost"].includes(st.view)) st.view = "match";
  st.fields = st.fields || [];
  st.places = st.places || [];
  let data = { meta: null, jobs: [] };
  const open = new Set();
  const NOT_TECH = "not an AI/ML, Data or Software role";
  const TECH = ["AI/ML", "Data", "Software"];

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const save = () => store.set("radar.filters", st);
  // only real web links become clickable (outside feeds could carry javascript: or data: URLs)
  const safeUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? u : "#");
  // Save / Applied / Skip follow the job (its key survives URL changes); older marks were kept by URL
  const mk = (j) => j.key || j.url;
  const markOf = (j) => {
    if (j.key && marks[j.url] && !marks[j.key]) { marks[j.key] = marks[j.url]; delete marks[j.url]; store.set("radar.marks", marks); }
    return marks[mk(j)] || "";
  };
  const ago = (t) => {
    const s = Date.now() / 1000 - t;
    if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  };
  const ICON = {
    check: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="m2.5 6.5 2.2 2.2 4.8-5.2"/></svg>',
    dash: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 6h6"/></svg>',
    edit: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2.5 9.5h2l5-5-2-2-5 5z"/></svg>',
    warn: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 3v3.5M6 8.8v.2"/></svg>',
    chev: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="m2 4 4 4 4-4"/></svg>',
  };

  // ---- the four checks ------------------------------------------------------------------------------
  // each sub-check -> {s: ok | warn | bad | none, word, quote}; a category takes its worst sub-check
  const FAILS = { required: "Required: you can't apply", rejected: "CPT / OPT not accepted, or permanent authorization",
                  ug_only: "Undergraduates only", phd_only: "PhD students only", misses: "Window excludes May 2028" };
  const LABELS = {
    citizenship: ["Citizenship", { not_required: ["ok", "Not required"] }],
    cpt: ["CPT / F-1", { welcome: ["ok", "CPT welcome"], silent: ["ok", "Not rejected"] }],
    sponsorship: ["Sponsorship", { sponsors: ["ok", "Sponsors visas"], silent: ["ok", "Not mentioned"],
      no_sponsorship: ["ok", "No sponsorship: fine, CPT is the authorization"] }],
    degree: ["Degree", { ms_ok: ["ok", "Master's students OK"], unclear: ["ok", "Not stated (no limit)"], ug_preferred: ["warn", "Bachelor's preferred"] }],
    grad: ["Graduation", { fits: ["ok", "Includes May 2028"], not_stated: ["ok", "Not stated (no limit)"] }],
  };
  const RANK = { bad: 3, warn: 2, none: 1, ok: 0 };
  const worst = (xs) => xs.reduce((a, x) => (RANK[x.s] > RANK[a] ? x.s : a), "ok");

  function sub(e, k) {
    const [label, map] = LABELS[k];
    if (!e || !e.labels) return { label, s: "none", word: "No description read", quote: "" };
    const v = e.labels[k];
    const quote = (e.quotes || {})[k] || "";
    const res = e.residency;   // "US residency 3 yrs: you have <1" says more than "Required"
    if (k === "citizenship" && v === "required" && res && res.block) return { label: "Citizenship / residency", s: "bad", word: res.word, quote: res.quote };
    if (FAILS[v]) return { label, s: "bad", word: FAILS[v], quote };
    const [s, word] = map[v] || ["warn", v];
    const dis = (e.disagree || []).includes(k);
    return { label, s: dis ? "warn" : s, word: dis ? `${word} (readers disagree)` : word, quote };
  }

  function categories(j) {
    const e = j.elig;
    const tech = TECH.includes(j.field) && j.why_not !== NOT_TECH;
    const how = j.why === "agent" ? "decided by Claude Code" : j.why === "JD" ? `from the description: ${(j.jd_terms || []).join(", ")}` : "from the title";
    const remote = (j.places || []).includes("Remote");
    const loc = { label: "Country", s: "ok", word: "United States" + (remote ? " · remote" : ""), quote: "" };
    const where = { label: "Places", s: "ok", word: (j.locations.join(" · ") || "Not listed") + (j.more_locations ? ` +${j.more_locations} more` : ""), quote: "" };
    const role = { label: "Field", s: tech ? "ok" : "bad", word: tech ? `${j.field} (${how})` : `${j.field}: not an AI/ML, Data or Software role`, quote: "" };
    const kind = { label: "Type", s: "ok", word: (j.kind === "co-op" ? "Co-op" : "Internship") + (j.terms.length ? ` · ${j.terms.join(", ")}` : ""), quote: "" };
    // tags = worth a look, never a block on their own (federal credential, residency without a number, summer-only)
    const tag = (t) => ({ label: t.tag.split(":")[0], s: "warn", word: t.tag, quote: t.quote || "" });
    const tags = (e && e.tags) || [];
    const auth = ["citizenship", "cpt", "sponsorship"].map((k) => sub(e, k)).concat(tags.filter((t) => !/^Term/.test(t.tag)).map(tag));
    const edu = ["degree", "grad"].map((k) => sub(e, k));
    if (e && e.labels && e.auth === "check" && !auth.some((x) => x.s === "bad")) auth[1].s = auth[1].s === "ok" ? "warn" : auth[1].s;
    const short = (xs, okWord) => {
      const w = worst(xs);
      if (w === "ok") return okWord;
      if (w === "none") return "Not read";
      return (xs.find((x) => x.s === w) || {}).word || "";
    };
    return [
      { name: "Location", s: "ok", short: remote ? "US · remote" : "US", rows: [loc, where] },
      { name: "Role", s: role.s, short: tech ? j.field : "Not tech", rows: [role, kind].concat(tags.filter((t) => /^Term/.test(t.tag)).map(tag)) },
      { name: "Work authorization", s: worst(auth), short: short(auth, "Clear"), rows: auth },
      { name: "Education", s: worst(edu), short: short(edu, "MS OK"), rows: edu },
    ];
  }

  // ---- who verified it: Checks -> Agent -> Claude Code (+ you) ----------------------------------------
  function trail(j) {
    const e = j.elig || {};
    const r = j.readers || { rules: !!e.labels, ai: e.ai === "read" };
    const steps = [
      { key: "checks", on: r.rules, text: "Checks", tip: r.rules ? "Rules read the description and quoted the sentence behind each check" : "No description was available for the rules" },
      { key: "agent", on: r.ai, text: "Agent", tip: r.ai ? "AI reader (gpt-oss-120b) read the eligibility sentences; quotes verified word for word" : "The AI reader has not read this one yet" },
      { key: "cc", cls: "cc", on: j.by === "agent" || j.checked, text: j.by === "agent" ? "Claude Code corrected" : "Claude Code",
        tip: j.by === "agent" ? "Claude Code read the full posting and corrected this verdict" : j.checked ? "Claude Code read the full posting and agrees" : "Claude Code has not read this one yet" },
    ];
    const ccs = (j.cc || {}).state;
    if (ccs === "pending") Object.assign(steps[2], { warn: true, text: "Claude Code: correction pending",
      tip: "Claude Code read the full posting, disagrees and has pushed a correction; it shows after the next build" });
    if (ccs === "doubt") Object.assign(steps[2], { warn: true, text: "Claude Code: not sure",
      tip: "Claude Code read the full posting and thinks it is a match, but is not sure. It is on your check list (decide.py list): open the posting and decide." });
    if (ccs === "unsure") Object.assign(steps[2], { warn: true, text: "Claude Code: unsure",
      tip: "Claude Code read the full posting and reads it differently, but is not sure enough to change it. Open the posting and decide." });
    const t = j.cc_tried;
    if (t && !steps[2].on && !steps[2].warn) {
      const blocked = /blocked/.test(t.why);
      Object.assign(steps[2], { tried: true, text: blocked ? "Claude Code: site blocked" : "Claude Code: couldn't read",
        tip: `Claude Code tried ${ago(t.t)} (${t.tries} ${t.tries === 1 ? "try" : "tries"}, plain fetch and a real browser): ${
          blocked ? "the company site blocks automated reading" : t.why}. Open the posting and check it yourself.` });
    }
    const tags = steps.map((x) => `<span class="vt ${x.cls || ""}${x.on ? " on" : ""}${x.tried || x.warn ? " tried" : ""}" title="${esc(x.tip)}">${
      x.on ? (x.text.endsWith("corrected") ? ICON.edit : ICON.check) : x.tried || x.warn ? ICON.warn : ICON.dash}${x.text}</span>`);
    if (j.by === "you") tags.push(`<span class="vt you" title="Your decision overrides every reader">${ICON.check}You decided</span>`);
    return { html: `<div class="trail" aria-label="Verification">${tags.join('<i class="vsep"></i>')}</div>`, steps };
  }

  function status(j) {
    if (j.by === "you") return j.match === false ? ["no", j.why_not || "Hidden by you"] : ["ok", "Shown by you"];
    if (j.match === false) return ["no", "Not a match · " + (j.why_not || "")];
    return ["ok", "Eligible"];
  }

  function detail(j, cats, steps) {
    const secs = cats.map((c) => `<div class="dsec"><h4><i class="dot ${c.s}"></i>${c.name}</h4>${c.rows.map((x) =>
      `<div class="drow"><b><i class="dot ${x.s}"></i>${esc(x.label)}</b><span><span class="v">${esc(x.word)}</span>${
        x.quote ? `<q>${esc(x.quote)}</q>` : ""}</span></div>`).join("")}</div>`).join("");
    const readers = steps.map((x) => `<div class="reader"><b><i class="dot ${x.on ? "ok" : x.tried || x.warn ? "warn" : "none"}"></i>${esc(x.text)}</b>${esc(x.tip)}</div>`).join("");
    return `<div class="detail"><div class="dgrid">${secs}</div><div class="readers">${readers}</div>
      <p class="dfoot">Found via ${esc(j.sources.join(", "))} · ${esc(postedText(j))}</p></div>`;
  }

  const dateText = (t) => new Date(t * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });

  // How the date was known (build.dated): be honest about how sure it is.
  function postedText(j) {
    if (j.reposted) return `first posted ${dateText(j.reposted)} (${ago(j.reposted)}); shown again ${ago(j.posted)}: a repost, not a new job`;
    switch (j.posted_basis) {
      case "board": return `posted ${ago(j.posted)} (time from the company's job board)`;
      case "page": return `posted ${ago(j.posted)} (date on the job posting)`;
      case "day": return `posted ${ago(j.posted)} (the board gives only the day)`;
      case "list": return `added to a list ${ago(j.posted)} (the job itself may be a little older)`;
      case "seen": return `first seen by the radar ${ago(j.posted)}`;
      default: return `first seen ${ago(j.posted)}`;
    }
  }

  function row(j) {
    const m = markOf(j);
    const fresh = !j.reposted && Date.now() / 1000 - j.posted < 6 * 3600;
    const [s, label] = status(j);
    const cats = categories(j);
    const tr = trail(j);
    const extra = Math.max(0, j.locations.length - 2) + (j.more_locations || 0);
    const loc = (j.locations.slice(0, 2).join(" · ") || "Location not listed") + (extra ? ` +${extra}` : "");
    const meta = [loc, j.field, ...j.terms.slice(0, 2)].map((x) => `<span>${esc(x)}</span>`).join("");
    const isOpen = open.has(j.url);
    return `<li class="job${m === "applied" || m === "skip" ? " done" : ""}${isOpen ? " open" : ""}" data-url="${esc(j.url)}" data-key="${esc(mk(j))}">
      <div class="jh"><div>
          <div class="co"><b>${esc(j.company)}</b><span class="age${fresh ? " fresh" : ""}">${ago(j.posted)}</span>${
            j.date_confirmed === false ? `<span class="kind nodate" title="No company board or posting gave a date: this is when a list added it or the radar first saw it. The job may be older.">Date not confirmed</span>` : ""}${
            j.reposted ? `<span class="kind repost" title="Shown again ${ago(j.posted)}; the job itself was first posted ${dateText(j.reposted)}">Reposted · first posted ${dateText(j.reposted)}</span>` : ""}${
            j.kind === "co-op" ? '<span class="kind coop">Co-op</span>' : '<span class="kind">Intern</span>'}</div>
          <a class="title" href="${esc(safeUrl(j.url))}" target="_blank" rel="noopener noreferrer">${esc(j.title)}</a>
          <div class="jmeta">${meta}</div>
        </div>
        <span class="status ${s}" title="${esc(label)}">${esc(label)}</span></div>
      <div class="cats">${cats.map((c) => `<div class="cat" title="${esc(c.name + ": " + c.short)}"><i class="dot ${c.s}"></i><b>${c.name}</b><em>${esc(c.short)}</em></div>`).join("")}</div>
      <div class="jf">${tr.html}
        <div class="acts">
          <button data-m="save" class="${m === "save" ? "on" : ""}">Save</button>
          <button data-m="applied" class="${m === "applied" ? "on" : ""}">Applied</button>
          <button data-m="skip" class="${m === "skip" ? "on" : ""}">Skip</button>
          <i class="sep"></i>
          <button class="more" data-open aria-expanded="${isOpen}">Details${ICON.chev}</button>
        </div>
      </div>
      ${detail(j, cats, tr.steps)}
    </li>`;
  }

  // ---- filters ----------------------------------------------------------------------------------------
  const ccDone = (j) => j.by === "agent" || j.checked === true;
  function visible(j, now) {
    // fresh jobs only in My matches / All; a job that was posted earlier and only showed up again lives in "Reposted"
    if ((st.view === "repost") !== !!j.reposted) return false;
    if (st.view !== "all" && j.match === false) return false;
    if (j.posted < now - st.win * 86400) return false;
    if (st.kind !== "all" && j.kind !== st.kind) return false;
    if (st.fields.length && !st.fields.includes(j.field)) return false;
    if (st.places.length && !st.places.some((p) => (j.places || []).includes(p))) return false;
    if (st.term && !j.terms.includes(st.term)) return false;
    if (st.cc && !ccDone(j)) return false;
    if (st.hideDone && ["applied", "skip"].includes(markOf(j))) return false;
    if (st.q) {
      const hay = `${j.company} ${j.title} ${j.locations.join(" ")}`.toLowerCase();
      if (!st.q.toLowerCase().split(/\s+/).every((w) => hay.includes(w))) return false;
    }
    return true;
  }

  let fieldsFor = null;
  function buildFields() {  // field chips for the jobs of the current view only
    if (fieldsFor === st.view) return;
    fieldsFor = st.view;
    const pool = data.jobs.filter((j) => (st.view === "repost") === !!j.reposted && (st.view === "all" || j.match !== false));
    const fields = [...new Set(pool.map((j) => j.field))]
      .sort((x, y) => (TECH.indexOf(x) + 1 || 9) - (TECH.indexOf(y) + 1 || 9) || x.localeCompare(y));
    st.fields = st.fields.filter((f) => fields.includes(f));
    $("fields").innerHTML = fields.map((f) => `<button data-v="${esc(f)}">${esc(f)}</button>`).join("");
    chips($("fields"), "fields");
  }

  function render() {
    buildFields();
    const now = Date.now() / 1000;
    const list = data.jobs.filter((j) => visible(j, now));
    $("jobs").innerHTML = list.map(row).join("");
    $("empty").hidden = list.length > 0;
    const co = list.filter((j) => j.kind === "co-op").length;
    const cc = list.filter(ccDone).length;
    $("count").textContent = `${list.length} roles · ${co} co-op · ${cc} verified by Claude Code`;
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
  function toggle(id, key) {
    $(id).checked = !!st[key];
    $(id).onchange = (e) => { st[key] = e.target.checked; save(); render(); };
  }

  $("jobs").addEventListener("click", (ev) => {
    const li = ev.target.closest(".job");
    if (!li) return;
    const url = li.dataset.url;
    const key = li.dataset.key;
    const more = ev.target.closest("[data-open]");
    if (more) {
      if (open.has(url)) open.delete(url); else open.add(url);
      li.classList.toggle("open");
      more.setAttribute("aria-expanded", li.classList.contains("open"));
      return;
    }
    const b = ev.target.closest("button[data-m]");
    if (!b) return;
    marks[key] = marks[key] === b.dataset.m ? undefined : b.dataset.m;
    if (!marks[key]) delete marks[key];
    store.set("radar.marks", marks);
    render();
  });

  function init(d) {
    data = d;
    const m = d.meta;
    const now = Date.now() / 1000;
    const freshJobs = d.jobs.filter((j) => !j.reposted);
    const matches = freshJobs.filter((j) => j.match !== false);
    const verified = matches.filter(ccDone).length;
    $("k24").textContent = matches.filter((j) => j.posted >= now - 86400).length;
    $("k48").textContent = matches.length;
    $("kcc").innerHTML = matches.length ? `${verified}<small>of ${matches.length}</small>` : "–";
    $("kall").textContent = freshJobs.length;
    $("vmatch").textContent = matches.length;
    $("vall").textContent = freshJobs.length;
    $("vrepost").textContent = d.jobs.filter((j) => j.reposted && j.match !== false).length;

    const tech = freshJobs.filter((j) => j.why_not !== NOT_TECH).length;
    $("funnel").innerHTML = [[freshJobs.length, "US co-ops & internships"], [tech, "tech roles"],
      [matches.length, "pass all four checks"], [verified, "verified by Claude Code"]]
      .map(([n, t]) => `<li><b>${n}</b>${t}</li>`).join("");

    const a = m.agent || {};
    const stale = a.hours_since == null || a.hours_since > 6 || a.ok === false;
    $("supd").innerHTML = `Updated ${ago(m.generated)}` + (a.last_run ? `<span class="cc-ago"> · Claude Code ${ago(a.last_run)}</span>` : "");
    $("supd").parentElement.classList.toggle("stale", stale);
    $("supd").parentElement.title = a.ok === false ? "The last Claude Code review run had errors (see Sources & status)"
      : stale ? "The Claude Code review agent runs when my laptop is on; it has not run for a while"
      : new Date(m.generated * 1000).toLocaleString();
    $("health").textContent = Object.entries(m.sources).map(([k, v]) => {
      if (!v.ok) return `${k}: failed`;
      if (v.boards !== undefined) return `${k}: ${v.boards - v.failed}/${v.boards} boards`;
      return `${k}: ${v.rows} rows`;
    }).join(" · ") + (m.polled ? ` · boards checked ${ago(m.polled)}` : "") +
      (m.speed && m.speed.n ? ` · speed: new board jobs seen a median ${m.speed.median_min} min after posting (90% within ${m.speed.p90_min} min, ${m.speed.n} jobs, last ${m.speed.days} days)` : "") +
      (m.ineligible_skipped !== undefined ? ` · not matches: ${m.not_tech_skipped} non-tech, ${m.ineligible_skipped} failed a check, ${m.review_skipped} need review, ${m.unverified_skipped} unverified` : "") +
      (a.last_run ? ` · review agent: ran ${ago(a.last_run)}, agrees on ${a.checked}/${a.total}, ${a.applied} corrections live` +
        (a.ok === false ? ` · last agent run had errors: ${(a.errors || []).join("; ")}` : "") : " · review agent: not seen yet");

    // boards that stopped answering (404 three times): lost coverage must never be silent
    const dead = Object.entries(m.sources).flatMap(([k, v]) => (v.dead || []).map((x) => `${x.company} (${k})`));
    $("dead").hidden = !dead.length;
    $("dead").innerHTML = dead.length ? `<b>${dead.length} company boards stopped answering</b> (404 three times; moved or closed, tried again weekly): ${esc(dead.join(", "))}` : "";

    const terms = [...new Set(d.jobs.flatMap((j) => j.terms))].sort();
    $("term").innerHTML = `<option value="">Any term</option>` + terms.map((t) => `<option>${esc(t)}</option>`).join("");
    if (!terms.includes(st.term)) { st.term = ""; save(); }   // a saved term no job has today would empty the list
    $("term").value = st.term;

    seg($("view"), "view", String);
    seg($("win"), "win", Number);
    seg($("kind"), "kind", String);
    chips($("places"), "places");
    toggle("ccOnly", "cc");
    toggle("hideDone", "hideDone");
    $("q").value = st.q;
    $("q").oninput = (e) => { st.q = e.target.value.trim(); save(); render(); };
    $("term").onchange = (e) => { st.term = e.target.value; save(); render(); };
    render();
  }

  fetch("data/jobs.json", { cache: "no-store" })
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(init)
    .catch((e) => { $("count").textContent = "Could not load jobs (" + e.message + "). Try again in a minute."; });
})();
