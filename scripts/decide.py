"""Your own call on a job: final over the rules, the AI reader and the review agent (see radar/review.py).

python scripts/decide.py list                        # the agent's unsure jobs (with ids) + your decisions
python scripts/decide.py show 3fa2c "CPT is fine"    # show that job in My matches
python scripts/decide.py hide 9b01e "hardware role"  # hide it from My matches
python scripts/decide.py show https://...            # or give the job's URL
python scripts/decide.py undo 3fa2c                  # remove your decision (the agent / rules decide again)

Ids come from the job itself (not its place in the list), so they never point at a different job.

Writes agent/decisions.json on origin/main (from the agent's private worktree) and pushes: the site rebuilds in a
few minutes. Decisions older than 14 days are dropped (jobs leave the page after 2 days).
"""
import json
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from radar import store  # noqa: E402
from scripts import agent_audit as A  # noqa: E402

KEEP = 14 * 86400


def _current() -> dict:
    """decisions.json as it is on origin/main right now."""
    A._git("fetch", "-q", "origin", "main")
    p = A._git("show", "origin/main:agent/decisions.json")
    try:
        return json.loads(p.stdout) if p.returncode == 0 else {}
    except ValueError:
        return {}


def _target(arg: str, dec: dict) -> dict:
    if arg.startswith("http"):
        u = store.norm_url(arg)
        for k, rv in A._load(A.REVIEWS, {}).items():   # the job's key (company + title) from what the agent read
            if rv.get("url") and store.norm_url(rv["url"]) == u:
                return {"key": k, "company": rv.get("company", ""), "title": rv.get("title", "")}
        return {"key": u, "company": "", "title": ""}  # the build also finds decisions by URL
    reviews = A._load(A.REVIEWS, {})
    for k in list(reviews) + list(dec):
        if A.short_id(k) == arg:
            v = reviews.get(k) or dec.get(k) or {}
            return {"key": k, "company": v.get("company", ""), "title": v.get("title", "")}
    sys.exit(f"no job with id {arg} (run: python scripts/decide.py list)")


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("list", "show", "hide", "undo"):
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "list":
        unsure = A._load(A.UNSURE, [])
        print(f"Unsure ({len(unsure)}):" if unsure else "Unsure: none")
        for u in unsure:
            print(f"  {u['id']}  {u['company']} · {u['title']}\n      base: {u['was']} | agent: {u['now']} "
                  f"({u['confidence']}) {u['note']}\n      {u['url']}")
        dec = _current()
        print(f"\nYour decisions ({len(dec)}):" if dec else "\nYour decisions: none")
        for k, v in dec.items():
            print(f"  {A.short_id(k)}  {'SHOW' if v['show'] else 'HIDE'}  {v['company']} · {v['title']}  {v.get('note', '')}")
        return
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    dec = _current()
    t = _target(sys.argv[2], dec)
    note = " ".join(sys.argv[3:]).strip()
    now = int(time.time())
    if cmd == "undo":
        if dec.pop(t["key"], None) is None:
            sys.exit("no decision of yours on that job")
    else:
        dec[t["key"]] = {"show": cmd == "show", "note": note, "t": now, "company": t["company"], "title": t["title"]}
    dec = {k: v for k, v in dec.items() if now - v.get("t", now) < KEEP}
    what = {"show": "show", "hide": "hide", "undo": "undo decision on"}[cmd]
    res = A.push_file("agent/decisions.json", json.dumps(dec, indent=1, sort_keys=True) + "\n",
                      f"Decision: {what} {t['company']} · {t['title']}")
    print(f"{what} {t['company']} · {t['title']}: {res}")


if __name__ == "__main__":
    main()
