"""Your own call on a job: final over the rules, the AI reader and the review agent (see radar/review.py).

python scripts/decide.py list                    # the agent's unsure jobs (numbered) + your decisions
python scripts/decide.py show 2 "CPT is fine"    # show job #2 of the unsure list in My matches
python scripts/decide.py hide 3 "hardware role"  # hide it from My matches
python scripts/decide.py show https://...        # or give the job's URL
python scripts/decide.py undo 2                  # remove your decision (the agent / rules decide again)

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


def _target(arg: str) -> dict:
    if arg.isdigit():
        for u in A._load(A.UNSURE, []):
            if u["n"] == int(arg):
                return {"key": u["key"], "company": u["company"], "title": u["title"]}
        sys.exit(f"no #{arg} in the unsure list (run: python scripts/decide.py list)")
    if arg.startswith("http"):
        key = store.norm_url(arg)
        rv = A._load(A.REVIEWS, {}).get(key) or {}
        return {"key": key, "company": rv.get("company", ""), "title": rv.get("title", "")}
    sys.exit("give a number from the unsure list or the job's URL")


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("list", "show", "hide", "undo"):
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "list":
        unsure = A._load(A.UNSURE, [])
        print(f"Unsure ({len(unsure)}):" if unsure else "Unsure: none")
        for u in unsure:
            print(f"  {u['n']:>2}. {u['company']} · {u['title']}\n      base: {u['was']} | agent: {u['now']} "
                  f"({u['confidence']}) {u['note']}\n      {u['url']}")
        dec = _current()
        print(f"\nYour decisions ({len(dec)}):" if dec else "\nYour decisions: none")
        for v in dec.values():
            print(f"  {'SHOW' if v['show'] else 'HIDE'}  {v['company']} · {v['title']}  {v.get('note', '')}")
        return
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    t = _target(sys.argv[2])
    note = " ".join(sys.argv[3:]).strip()
    dec = _current()
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
