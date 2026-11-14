"""Fetch the lists, poll the company boards, merge, update the seen store, write the site data.

python -m radar.build --state state/seen.json.gz --out site/data [--poll auto|yes|no]
"""
import argparse
import inspect
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from . import ats, classify, eligibility, jdfetch, sources, store, tech

WINDOW_DAYS = 2          # the page shows 24 h or 2 days
POLL_EVERY = 170 * 60    # 'auto' polls the boards about every 3 hours
MAX_JD_FETCH = 150       # list-only jobs whose JD we fetch per run (the rest next run; results are cached)
JD_RETRY = 6 * 3600      # a JD we couldn't get is tried again after this
ATS_SOURCES = set(ats.ATS_NAMES.values())


def posted_at(job: dict, rec: dict) -> int | None:
    """When the job became public, as far as we can tell.

    Native date if the source gives one; else our first_seen, unless that came from a bootstrap run.
    The earlier of the two wins, so a job a source dates in the past is never called new.
    """
    native = job.get("posted")
    seen = None if rec.get("boot") else rec["first"]
    dates = [d for d in (native, seen) if d]
    return min(dates) if dates else None


def merge(jobs_by_source: dict[str, list[dict]]) -> dict[str, dict]:
    """One row per job: same company+title, or same apply URL, across sources."""
    merged: dict[str, dict] = {}
    by_url: dict[str, str] = {}
    for src, jobs in jobs_by_source.items():
        for j in jobs:
            k = store.job_key(j["company"], j["title"])
            u = store.norm_url(j["url"]) if "go.vansh.nyc" not in j["url"] else None
            k = by_url.get(u, k) if u else k
            if k not in merged:
                merged[k] = {**j, "sources": [src]}
                if u:
                    by_url.setdefault(u, k)
                continue
            m = merged[k]
            if src not in m["sources"]:
                m["sources"].append(src)
            m["locations"] = sorted(set(m["locations"]) | set(j["locations"]))
            m["terms"] = sorted(set(m["terms"]) | set(j["terms"]))
            if j.get("posted") and (not m.get("posted") or j["posted"] < m["posted"]):
                m["posted"] = j["posted"]
            if "go.vansh.nyc" in m["url"] and "go.vansh.nyc" not in j["url"]:
                m["url"] = j["url"]
            m["field"] = m.get("field") or j.get("field")
            if j.get("jd_terms") is not None:
                m["jd_terms"] = sorted(set(m.get("jd_terms") or []) | set(j["jd_terms"]))
            if j.get("elig"):
                m["elig"] = eligibility.combine(m.get("elig"), j["elig"])
            if j.get("sponsorship") not in (None, "Other"):
                m["sponsorship"] = j["sponsorship"]
            m["degrees"] = sorted(set(m.get("degrees") or []) | set(j.get("degrees") or []))
            if u:
                by_url.setdefault(u, k)
    return merged


def apply_jd(j: dict, c: dict) -> None:
    j["elig"], j["jd_terms"] = c.get("elig"), c.get("jd_terms")
    if c.get("country") and not classify.is_us([], [c["country"]]):
        j["non_us"] = True


def run(state_path: str, out_dir: str, fetch=sources.fetch_json, resolve=sources.resolve_shortlink,
        poll: str = "auto", poll_fn=ats.poll, now: int | None = None, jd_fetch=None) -> dict:
    jd_fetch = jd_fetch or jdfetch.fetch
    now = now or int(time.time())
    state = store.load(state_path)
    state.setdefault("boards", {})
    boot = not state["jobs"]

    # 1. The community lists.
    raw, by_source, health = {}, {}, {}
    for name, (url, parse) in sources.SOURCES.items():
        try:
            raw[name] = fetch(url)
            jobs = [j for j in parse(raw[name]) if classify.is_us(j["locations"])]
            by_source[name] = jobs
            health[name] = {"ok": True, "rows": len(jobs)}
        except Exception as e:  # one broken source must not stop the others
            health[name] = {"ok": False, "rows": 0, "error": f"{type(e).__name__}: {str(e)[:200]}"}
            print(f"[warn] {name}: {e}", file=sys.stderr)

    # 1b. Resolve shortlinks of recent rows first (cached; each link fetched once), so the merge can match by URL.
    cut = now - WINDOW_DAYS * 86400
    recent = [j for jobs in by_source.values() for j in jobs
              if "go.vansh.nyc" in j["url"] and (j.get("posted") or now) >= cut]
    todo = sorted({j["url"] for j in recent if j["url"] not in state["links"]})
    with ThreadPoolExecutor(max_workers=16) as pool:
        for short, real in zip(todo, pool.map(resolve, todo)):
            if real != short:
                state["links"][short] = real
    for j in recent:
        j["url"] = state["links"].get(j["url"], j["url"])

    # 2. Learn company boards from every apply URL the lists have ever shown (active or not).
    pairs = [(x.get("company_name", ""), x.get("url", "")) for x in raw.get("Simplify", [])]
    pairs += [(x.get("company_name", ""), state["links"].get(x.get("url", ""), "")) for x in raw.get("vanshb03", [])]
    for bid, b in ats.discover(pairs).items():
        state["boards"].setdefault(bid, b)

    # 3. Poll the boards (every ~3 h); between polls reuse the last result.
    last = state.get("ats") or {}
    # Which board types have been polled before? A type's first poll only learns what exists (undated jobs
    # it finds are not "new"), the same way the very first run treats the lists.
    polled_before = set(state.get("ats_polled") or (last.get("health") or {}).keys())
    new_types: set[str] = set()
    do_poll = poll == "yes" or (poll == "auto" and now - last.get("at", 0) >= POLL_EVERY)
    if do_poll and state["boards"]:
        state["jd_cache"] = jd_cache = {u: v for u, v in state.get("jd_cache", {}).items()
                                        if v.get("v") == eligibility.RULES_VERSION or v.get("miss")}
        takes_known = "known" in inspect.signature(poll_fn).parameters  # test stubs take only the boards
        got, ats_health = poll_fn(state["boards"], known=jd_cache) if takes_known else poll_fn(state["boards"])
        # A board that failed this time (outage, 429...) keeps the jobs it had last poll.
        failed = ats.FAILED if poll_fn is ats.poll else set()
        got = got + [j for j in (last.get("jobs") or []) if j.get("board") in failed]
        for j in got:
            if j.get("elig") is not None and j["url"] not in jd_cache:
                jd_cache[j["url"]] = {"t": now, "v": eligibility.RULES_VERSION, "elig": j["elig"], "jd_terms": j.get("jd_terms")}

        # Keep only jobs that can still be "new": undated ones (dated by first_seen) or dated inside the window.
        keep = [j for j in got if not j.get("posted") or j["posted"] >= now - WINDOW_DAYS * 86400]
        last = {"at": now, "jobs": [{k: v for k, v in j.items() if k != "countries" and not k.startswith("_")} for j in keep], "health": ats_health}
        state["ats"] = last
        new_types = set(ats_health) - polled_before
        state["ats_polled"] = sorted(polled_before | set(ats_health))
    for name, h in (last.get("health") or {}).items():
        health[name] = {**h, "polled": last["at"]}
    for j in last.get("jobs", []):
        by_source.setdefault(j["source"], []).append(j)

    # 4. Merge, date, keep the last WINDOW_DAYS.
    merged = merge(by_source)
    in_window = []
    for k, j in merged.items():
        from_new_type = bool(new_types) and set(j["sources"]) <= new_types
        rec = store.mark_seen(state, k, now, boot or from_new_type)
        t = posted_at(j, rec)
        if t and t >= cut:
            in_window.append((j, t))

    # 5. Jobs we only know from a list have no JD yet: fetch it from the apply page (cached per URL),
    #    so every job, whatever its source, goes through the same tech + eligibility checks.
    jd_cache = state.setdefault("jd_cache", {})
    todo = []
    for j, _ in in_window:
        if j.get("elig") is not None or tech.TITLE_NOT.search(j["title"]):
            continue
        c = jd_cache.get(j["url"])
        if c and c.get("v") != eligibility.RULES_VERSION and not c.get("miss"):
            c = None  # verdict from older rules: fetch the JD again
        if c and c.get("elig") is not None:
            apply_jd(j, c)
        elif not (c and c.get("miss") and now - c["t"] < JD_RETRY):
            todo.append(j)
    todo = todo[:MAX_JD_FETCH]
    jd_stats = {"fetched": 0, "missed": 0, "cached": sum(1 for v in jd_cache.values() if v.get("elig") is not None)}
    with ThreadPoolExecutor(max_workers=8) as pool:
        def safe(x):
            try:
                return jd_fetch(x["url"])
            except Exception:
                return None
        for j, r in zip(todo, pool.map(safe, todo)):
            if r and r.get("text"):
                c = {"t": now, "v": eligibility.RULES_VERSION, "elig": eligibility.check(r["text"]),
                     "jd_terms": tech.jd_terms(r["text"]), "country": r.get("country"), "via": r.get("via")}
                jd_stats["fetched"] += 1
                apply_jd(j, c)
            else:
                c = {"t": now, "miss": True}
                jd_stats["missed"] += 1
            jd_cache[j["url"]] = c
    state["jd_cache"] = {u: v for u, v in jd_cache.items() if v["t"] >= now - 10 * 86400}

    # 6. The same checks for every job: US, tech role, eligibility.
    picked = []
    skipped_not_tech = skipped_inelig = skipped_non_us = 0
    for j, t in in_window:
        if j.get("non_us"):
            skipped_non_us += 1
            continue
        tc = tech.classify(j["title"], j.get("field"), j.get("jd_terms"))
        if not tc:
            skipped_not_tech += 1
            continue
        lst = eligibility.from_list(j.get("sponsorship"), j.get("degrees")) if "Simplify" in j["sources"] else None
        e = eligibility.with_list(j.get("elig"), lst)
        if eligibility.hidden(e):
            skipped_inelig += 1
            continue
        picked.append((j, t, tc, e))

    rows = []
    for j, t, (fld, why), e in picked:
        rows.append({
            "company": j["company"],
            "title": j["title"],
            "url": j["url"],
            "locations": j["locations"][:6],
            "more_locations": max(0, len(j["locations"]) - 6),
            "terms": j["terms"],
            "kind": classify.kind(j["title"], j["terms"]),
            "field": fld,
            "why": why,
            "jd_terms": sorted(j.get("jd_terms") or [], key=lambda x: x not in tech.JD_CORE)[:6] if why == "JD" else [],
            "places": classify.places(j["locations"]),
            "sponsorship": j.get("sponsorship") if j.get("sponsorship") not in (None, "Other") else None,
            "sources": j["sources"],
            "posted": t,
            "elig": {"auth": e["auth"], "degree": e["degree"], "grad": e.get("grad"), "notes": e.get("notes") or []},
        })

    rows.sort(key=lambda r: r["posted"], reverse=True)
    store.prune(state, now)
    if len(state["links"]) > 8000:
        state["links"] = dict(list(state["links"].items())[-8000:])

    live = [b for b in state["boards"].values() if b.get("fails", 0) < 3]
    meta = {
        "generated": now,
        "window_days": WINDOW_DAYS,
        "count_24h": sum(1 for r in rows if r["posted"] >= now - 86400),
        "count": len(rows),
        "sources": health,
        "boards": {ats.ATS_NAMES[a]: sum(1 for b in live if b["ats"] == a) for a in ats.ATS_NAMES},
        "polled": last.get("at"),
        "not_tech_skipped": skipped_not_tech,
        "ineligible_skipped": skipped_inelig,
        "non_us_skipped": skipped_non_us,
        "jd": jd_stats,
        "check_count": sum(1 for r in rows if eligibility.needs_check(r["elig"])),
        "bootstrap": boot,
    }
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "jobs.json"), "w") as f:
        json.dump({"meta": meta, "jobs": rows}, f, separators=(",", ":"))
    store.save(state, state_path)
    return meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state/seen.json.gz")
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--poll", choices=["auto", "yes", "no"], default="auto")
    a = ap.parse_args()
    meta = run(a.state, a.out, poll=a.poll)
    print(json.dumps(meta, indent=1))
    if not any(s["ok"] for s in meta["sources"].values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
