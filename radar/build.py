"""Fetch the feeds, merge, update the seen store, write the site data.

python -m radar.build --state state/seen.json.gz --out site/data
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from . import classify, sources, store

WINDOW_DAYS = 7  # the page filters 24 h / 3 d / 7 d itself


def posted_at(job: dict, rec: dict) -> int | None:
    """When the job became public, as far as we can tell.

    Native date if the feed gives one; else our first_seen, unless that came from the bootstrap run.
    The earlier of the two wins, so a job a feed dates in the past is never called new.
    """
    native = job.get("posted")
    seen = None if rec.get("boot") else rec["first"]
    dates = [d for d in (native, seen) if d]
    return min(dates) if dates else None


def merge(jobs_by_source: dict[str, list[dict]]) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for src, jobs in jobs_by_source.items():
        for j in jobs:
            k = store.job_key(j["company"], j["title"])
            if k not in merged:
                merged[k] = {**j, "sources": [src]}
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
    return merged


def run(state_path: str, out_dir: str, fetch=sources.fetch_json, resolve=sources.resolve_shortlink, now: int | None = None) -> dict:
    now = now or int(time.time())
    state = store.load(state_path)
    boot = not state["jobs"]

    by_source, health = {}, {}
    for name, (url, parse) in sources.SOURCES.items():
        try:
            jobs = parse(fetch(url))
            by_source[name] = jobs
            health[name] = {"ok": True, "rows": len(jobs)}
        except Exception as e:  # one broken feed must not stop the others
            health[name] = {"ok": False, "rows": 0, "error": f"{type(e).__name__}: {str(e)[:200]}"}
            print(f"[warn] {name}: {e}", file=sys.stderr)

    merged = merge(by_source)
    cut = now - WINDOW_DAYS * 86400
    picked = []
    for k, j in merged.items():
        if not classify.is_early_career(j["title"], from_intern_feed=True):
            continue
        rec = store.mark_seen(state, k, now, boot)
        t = posted_at(j, rec)
        if t and t >= cut:
            picked.append((j, t))

    # Resolve new shortlinks in parallel (cached in the store, so each link is fetched once).
    todo = sorted({j["url"] for j, _ in picked if "go.vansh.nyc" in j["url"] and j["url"] not in state["links"]})
    with ThreadPoolExecutor(max_workers=16) as pool:
        for short, real in zip(todo, pool.map(resolve, todo)):
            if real != short:
                state["links"][short] = real

    rows = []
    for j, t in picked:
        url = state["links"].get(j["url"], j["url"])
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
    if len(state["links"]) > 5000:
        state["links"] = dict(list(state["links"].items())[-5000:])

    meta = {
        "generated": now,
        "window_days": WINDOW_DAYS,
        "count_24h": sum(1 for r in rows if r["posted"] >= now - 86400),
        "count": len(rows),
        "sources": health,
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
    a = ap.parse_args()
    meta = run(a.state, a.out)
    print(json.dumps(meta, indent=1))
    if not any(s["ok"] for s in meta["sources"].values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
