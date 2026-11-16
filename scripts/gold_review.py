"""Weekly 10 minutes: confirm Claude Code's corrections as hand labels, so the rules and the AI reader learn from them.

python scripts/agent_audit.py gold      # 1. refresh tests/gold/candidates.json (eligibility disagreements only)
python scripts/gold_review.py           # 2. go through them, one job at a time

For each job you see the posting's eligibility sentences, what the site said (base) and what Claude Code says:
  a          accept Claude Code's five answers as the gold labels
  e          edit first: type changes like  degree=ug_only grad=not_stated  (then it is added)
  s          skip (not sure / not worth it): it is not shown again
  q          stop; the rest stay for next time
Accepted jobs go into tests/gold/dev.json (never test.json: that one stays held out). Your answers are kept in
.cache/agent/gold_seen.json so a job is never asked twice. Then run: python scripts/eval_gold.py
"""
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from radar import eligibility as E, store  # noqa: E402

GOLD = os.path.join(ROOT, "tests", "gold")
SEEN = os.path.join(ROOT, ".cache", "agent", "gold_seen.json")
NEUTRAL = set(E.NEUTRAL.values())


def _load(p, default):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def verdict(lab: dict) -> str:
    e = E.from_labels(lab, {})
    return "not_eligible" if E.hidden(e) else "check" if E.needs_check(e) else "eligible"


def parse_edits(line: str, lab: dict) -> dict | None:
    """'degree=ug_only grad=not_stated' -> new labels, or None if a key or value is not allowed."""
    out = dict(lab)
    for part in line.split():
        k, _, v = part.partition("=")
        if k not in E.LABELS or v not in E.LABELS[k]:
            print(f"  ? {part}: use one of " + "; ".join(f"{x}={'|'.join(E.LABELS[x])}" for x in E.LABELS))
            return None
        out[k] = v
    return out


def main(inp=input) -> None:
    cands = _load(os.path.join(GOLD, "candidates.json"), [])
    dev = _load(os.path.join(GOLD, "dev.json"), [])
    test = _load(os.path.join(GOLD, "test.json"), [])
    seen = _load(SEEN, {})
    have = {store.job_key(x["company"], x["title"]) for x in dev + test}
    todo = [c for c in cands if c.get("excerpt") and store.job_key(c["company"], c["title"]) not in have
            and store.job_key(c["company"], c["title"]) not in seen]
    print(f"{len(todo)} to review ({len(cands)} candidates; the rest are already in the gold sets or were seen)\n")
    added = 0
    for n, c in enumerate(todo, 1):
        k = store.job_key(c["company"], c["title"])
        lab = dict(c["gold"])
        print(f"[{n}/{len(todo)}] {c['company']} · {c['title']}\n  {c.get('url') or ''}")
        print(f"  site said: {c['base']}   |   Claude Code ({c['confidence']}): "
              + (", ".join(f"{x}={v}" for x, v in lab.items() if v not in NEUTRAL) or "all neutral")
              + f"  -> {verdict(lab)}")
        for x, q in (c.get("quotes") or {}).items():
            print(f"    {x}: \"{q[:220]}\"")
        print("  posting: " + c["excerpt"][:900].replace("\n", " ") + ("…" if len(c["excerpt"]) > 900 else ""))
        while True:
            ans = inp("  [a]ccept / [e]dit / [s]kip / [q]uit > ").strip().lower()
            if ans in ("a", "s", "q"):
                break
            if ans == "e":
                new = parse_edits(inp("  changes (key=value ...) > "), lab)
                if new is not None:
                    lab, ans = new, "a"
                    break
        if ans == "q":
            break
        if ans == "a":
            dev.append({"id": max((x["id"] for x in dev), default=0) + 1, "company": c["company"], "title": c["title"],
                        "excerpt": c["excerpt"], "gold": lab, "verdict": verdict(lab),
                        "quotes": {x: q for x, q in (c.get("quotes") or {}).items() if lab.get(x) not in NEUTRAL},
                        "note": "confirmed by Dheepak from a Claude Code correction (gold_review.py)"})
            added += 1
        seen[k] = ans
        with open(os.path.join(GOLD, "dev.json"), "w") as f:    # saved after every answer: quitting loses nothing
            json.dump(dev, f, indent=1, ensure_ascii=False)
        os.makedirs(os.path.dirname(SEEN), exist_ok=True)
        with open(SEEN, "w") as f:
            json.dump(seen, f, indent=1)
        print()
    print(f"added {added} to tests/gold/dev.json ({len(dev)} now). Next: python scripts/eval_gold.py")


if __name__ == "__main__":
    main()
