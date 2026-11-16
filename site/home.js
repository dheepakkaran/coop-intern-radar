(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const NS = "http://www.w3.org/2000/svg";
  const C = 300, R = 270;                       // centre and radius of the disc (viewBox 600)
  const BANDS = { match: [42, 118], tech: [134, 196], other: [212, 258] };   // ring radii per kind
  const EDGES = [126, 204];                     // dashed lines between the rings
  const PERIOD = 6000;                          // one sweep, ms
  const NOT_TECH = "not an AI/ML, Data or Software role";
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const safeUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? u : null);
  const ago = (t) => {
    const s = Date.now() / 1000 - t;
    if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  };
  const el = (tag, attrs, parent) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    if (parent) parent.appendChild(n);
    return n;
  };
  const pt = (r, deg) => [C + r * Math.sin(deg * Math.PI / 180), C - r * Math.cos(deg * Math.PI / 180)];
  const hash = (s) => { let h = 2166136261; for (const ch of s) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619); } return (h >>> 0) / 4294967295; };

  // ---- the grid: disc, rings, hours (clockwise from "now" at the top) ---------------------------------
  function grid() {
    const g = $("grid");
    el("circle", { class: "disc", cx: C, cy: C, r: R }, g);
    for (const r of [R * 0.25, R * 0.5, R * 0.75]) el("circle", { class: "ring", cx: C, cy: C, r }, g);
    for (const r of EDGES) el("circle", { class: "ring band", cx: C, cy: C, r }, g);
    for (let h = 0; h < 24; h++) {
      const a = h * 15;
      const [x1, y1] = pt(h % 3 ? R - 7 : 0, a), [x2, y2] = pt(R, a);
      el("line", { class: h % 3 ? "tick" : "spoke", x1, y1, x2, y2 }, g);
      if (h % 3 === 0) {
        const [x, y] = pt(R + 18, a);
        const t = el("text", { class: "hour" + (h ? "" : " now"), x, y }, g);
        t.textContent = h ? `${h}h` : "now";
      }
    }
    el("circle", { class: "core", cx: C, cy: C, r: 3.5 }, g);
  }

  // ---- blips --------------------------------------------------------------------------------------------
  const kind = (j) => (j.match !== false ? "match" : j.why_not === NOT_TECH ? "other" : "tech");
  const verified = (j) => j.by === "agent" || j.checked === true;
  let blips = [];
  let shown = null;                             // the blip whose tooltip is open
  const touch = window.matchMedia("(hover: none)").matches;

  function place(jobs, now) {
    const g = $("blips");
    g.textContent = "";
    // matches last, so they sit on top
    const order = { other: 0, tech: 1, match: 2 };
    blips = jobs.slice().sort((a, b) => order[kind(a)] - order[kind(b)]).map((j) => {
      const k = kind(j);
      const a = Math.min(359.5, Math.max(0, (now - j.posted) / 3600 / 24 * 360));
      const [r0, r1] = BANDS[k];
      const r = r0 + (r1 - r0) * hash(j.key || j.url);
      const [x, y] = pt(r, a);
      const b = el("g", { class: `blip ${k}`, tabindex: "0", role: "link",
        "aria-label": `${j.company}, ${j.title}, ${ago(j.posted)}${k === "match" ? ", a match" : ""}` }, g);
      const halo = k === "match" ? el("circle", { class: "halo", cx: x, cy: y, r: 11, opacity: 0 }, b) : null;
      if (k === "match" && verified(j)) el("circle", { class: "ring-cc", cx: x, cy: y, r: 8 }, b);
      const dot = el("circle", { class: "dot2", cx: x, cy: y, r: k === "match" ? 5 : k === "tech" ? 3.6 : 3 }, b);
      const item = { j, k, a, x, y, b, dot, halo };
      b.addEventListener("mouseenter", () => tip(item));
      b.addEventListener("focus", () => tip(item));
      b.addEventListener("mouseleave", () => { if (!touch) tip(null); });
      b.addEventListener("blur", () => tip(null));
      const go = () => { const u = safeUrl(j.url); if (u) window.open(u, "_blank", "noopener"); };
      // on a phone the first tap shows what it is, the second opens it
      b.addEventListener("click", () => { if (touch && shown !== item) { tip(item); return; } go(); });
      b.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
      return item;
    });
  }

  function tip(it) {
    const t = $("tip");
    shown = it;
    if (!it) { t.hidden = true; return; }
    const box = $("scopeBox").getBoundingClientRect();
    const s = box.width / 600;
    const what = it.k === "match" ? "Match" + (verified(it.j) ? " · verified by Claude Code" : "") : it.j.why_not || "";
    t.innerHTML = `<b>${esc(it.j.company)}</b>${esc(it.j.title)}<br><span>${ago(it.j.posted)} · ${esc(what)}</span>`;
    t.style.left = `${it.x * s}px`;
    t.style.top = `${it.y * s}px`;
    t.hidden = false;
  }

  // ---- the sweep: blips flare as it passes and fade over the next turn -----------------------------------
  function frame(ts) {
    const s = (ts % PERIOD) / PERIOD * 360;
    $("sweep").style.transform = `rotate(${s}deg)`;
    for (const it of blips) {
      const d = (s - it.a + 360) % 360;              // degrees since the sweep passed this blip
      const glow = Math.max(0, 1 - d / 320);
      it.dot.setAttribute("opacity", (it.k === "match" ? 0.55 : 0.3) + (it.k === "match" ? 0.45 : 0.55) * glow);
      if (it.halo) it.halo.setAttribute("opacity", (0.28 * glow * glow).toFixed(3));
    }
    requestAnimationFrame(frame);
  }

  // ---- the brief ------------------------------------------------------------------------------------------
  function brief(d, day, now) {
    const matches = day.filter((j) => j.match !== false);
    const tech = day.filter((j) => j.why_not !== NOT_TECH);
    const ver = matches.filter(verified);
    $("n24").textContent = matches.length;
    $("n24").nextElementSibling.textContent = matches.length === 1 ? "new match" : "new matches";
    $("steps").innerHTML = [
      ["New US co-ops & internships", day.length, ""],
      ["AI/ML, Data or Software roles", tech.length, ""],
      ["Pass all four checks", matches.length, "hl"],
      ["Verified by Claude Code", `${ver.length}${matches.length ? ` of ${matches.length}` : ""}`, "cc"],
    ].map(([t, n, c]) => `<li class="${c}"><span>${t}</span><b>${n}</b></li>`).join("");
    const latest = matches.slice().sort((a, b) => b.posted - a.posted).slice(0, 5);
    $("latest").innerHTML = latest.length ? latest.map((j) => {
      const u = safeUrl(j.url);
      return `<li><a ${u ? `href="${esc(u)}" target="_blank" rel="noopener noreferrer"` : ""}><span class="c">${esc(j.company)}${
        verified(j) ? " · Claude Code ✓" : ""}</span><span class="t">${esc(j.title)}</span><span class="a">${ago(j.posted)}</span></a></li>`;
    }).join("") : `<li class="none">No new matches in the last 24 hours yet. <a href="jobs.html">See the last 2 days</a></li>`;

    const a = d.meta.agent || {};
    $("supd").innerHTML = `Updated ${ago(d.meta.generated)}` + (a.last_run ? `<span class="cc-ago"> · Claude Code ${ago(a.last_run)}</span>` : "");
    $("supd").parentElement.classList.toggle("stale", a.hours_since == null || a.hours_since > 6 || a.ok === false);
  }

  grid();
  fetch("data/jobs.json", { cache: "no-store" })
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then((d) => {
      const now = Date.now() / 1000;
      const day = d.jobs.filter((j) => !j.reposted && j.posted >= now - 86400);   // fresh only: reposts are not new
      brief(d, day, now);
      place(day, now);
      if (reduce) blips.forEach((it) => { it.dot.setAttribute("opacity", 1); if (it.halo) it.halo.setAttribute("opacity", 0.15); });
      else requestAnimationFrame(frame);
    })
    .catch((e) => { $("n24").textContent = "–"; $("supd").textContent = "Could not load jobs (" + e.message + ")"; });
})();
