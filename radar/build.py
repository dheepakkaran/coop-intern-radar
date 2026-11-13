"""Fetch the lists, poll the company boards, merge, update the seen store, write the site data.

python -m radar.build --state state/seen.json.gz --out site/data [--poll auto|yes|no]
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from . import ats, classify, sources, store

WINDOW_DAYS = 7          # the page filters 24 h / 3 d / 7 d itself
POLL_EVERY = 170 * 60    # 'auto' polls the boards about every 3 hours
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
            if u:
                by_url.setdefault(u, k)
    return merged


def run(state_path: str, out_dir: str, fetch=sources.fetch_json, resolve=sources.resolve_shortlink,
        poll: str = "auto", poll_fn=ats.poll, now: int | None = None) -> dict:
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
        got, ats_health = poll_fn(state["boards"])
        # Keep only jobs that can still be "new": undated ones (dated by first_seen) or dated inside the window.
        keep = [j for j in got if not j.get("posted") or j["posted"] >= now - WINDOW_DAYS * 86400]
        last = {"at": now, "jobs": [{k: v for k, v in j.items() if k != "countries"} for j in keep], "health": ats_health}
        state["ats"] = last
        new_types = set(ats_health) - polled_before
        state["ats_polled"] = sorted(polled_before | set(ats_health))
    for name, h in (last.get("health") or {}).items():
        health[name] = {**h, "polled": last["at"]}
    for j in last.get("jobs", []):
        by_source.setdefault(j["source"], []).append(j)

    # 4. Merge, date, keep the last WINDOW_DAYS.
    merged = merge(by_source)
    picked = []
    for k, j in merged.items():
        from_new_type = bool(new_types) and set(j["sources"]) <= new_types
        rec = store.mark_seen(state, k, now, boot or from_new_type)
        t = posted_at(j, rec)
        if t and t >= cut:
            picked.append((j, t))

    rows = []
    for j, t in picked:
        url = j["url"]
        rows.append({
            "company": j["company"],
            "title": j["title"],
            "url": url,
            "locations": j["locations"][:6],
            "more_locations": max(0, len(j["locations"]) - 6),
            "terms": j["terms"],
            "kind": classify.kind(j["title"], j["terms"]),
            "field": classify.field(j["title"], j.get("field")),
            "places": classify.places(j["locations"]),
            "sponsorship": j.get("sponsorship") if j.get("sponsorship") not in (None, "Other") else None,
            "sources": j["sources"],
            "posted": t,
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
