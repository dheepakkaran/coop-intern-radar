"""Score the eligibility readers against the hand-labeled gold set (tests/gold/gold.json).

python scripts/eval_gold.py              # rules on the dev set (no API calls)
python scripts/eval_gold.py --set test   # the held-out test set (never used to write the rules)
python scripts/eval_gold.py --ai         # also run the AI reader (needs GROQ_API_KEY); answers cached in
                                          # tests/gold/ai_cache.json so reruns don't call the API again

Verdicts: not_eligible (job hidden) / check / eligible. The two numbers that matter:
  false_show = gold says not eligible, the reader would show it   (wastes your time; the Sanofi case)
  false_hide = gold says you can apply, the reader would hide it  (you miss a job)
"""
import argparse
import collections
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from radar import eligibility as E, llm  # noqa: E402

HERE = os.path.join(os.path.dirname(__file__), "..", "tests", "gold")


def verdict(lab: dict) -> str:
    e = E.from_labels(lab, {})
    if E.hidden(e):
        return "not_eligible"
    return "check" if E.needs_check(e) else "eligible"


def score(name: str, items: list, get_labels) -> dict:
    per = collections.Counter()
    fs, fh, wrong = [], [], []
    for it in items:
        lab = get_labels(it)
        if lab is None:
            continue
        per["n"] += 1
        for k in E.LABELS:
            per[k] += lab[k] == it["gold"][k]
        v, g = verdict(lab), verdict(it["gold"])  # gold verdict under today's policy (no sponsorship passes)
        per["verdict"] += v == g
        if g == "not_eligible" and v != "not_eligible":
            fs.append(it)
        if g != "not_eligible" and v == "not_eligible":
            fh.append(it)
        if v != g:
            wrong.append((it, lab))
    n = per["n"] or 1
    r = {"reader": name, "n": per["n"], "verdict_acc": round(per["verdict"] / n, 3),
         "false_show": len(fs), "false_hide": len(fh),
         **{f"{k}_acc": round(per[k] / n, 3) for k in E.LABELS},
         "false_show_ids": [i["id"] for i in fs], "false_hide_ids": [i["id"] for i in fh]}
    r["_wrong"] = wrong
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="dev", choices=["dev", "test"])
    ap.add_argument("--ai", action="store_true")
    ap.add_argument("--show", type=int, default=12, help="print this many wrong items per reader")
    a = ap.parse_args()
    items = json.load(open(os.path.join(HERE, f"{a.set}.json")))
    rules = {it["id"]: E.labels(it["excerpt"]) for it in items}
    results = [score("rules", items, lambda it: rules[it["id"]][0])]

    if a.ai:
        path = os.path.join(HERE, f"ai_cache_{a.set}_{llm.MODEL.replace('/', '_')}_v{llm.LLM_VERSION}"
                                  f"_b{llm.BATCH}_{llm.EFFORT}.json")
        cache = json.load(open(path)) if os.path.exists(path) else {}
        budget = llm.Budget(run_tokens=10**9, run_requests=10**6)
        todo = [(str(it["id"]), it["excerpt"]) for it in items if str(it["id"]) not in cache]
        for i in range(0, len(todo), llm.BATCH):
            got = llm.ask_batch(todo[i:i + llm.BATCH], budget=budget)
            cache.update(got)
            json.dump(cache, open(path, "w"), indent=1)
        print(f"AI tokens this run: {budget.report()}  (excerpts sent: {len(todo)})")
        ai = lambda it: (cache.get(str(it["id"])) or {}).get("labels")
        results.append(score(f"ai ({llm.MODEL})", items, ai))

        def merged(it):
            c = cache.get(str(it["id"]))
            if not c:
                return None
            lab, quo = rules[it["id"]]
            return llm.merge({"labels": lab, "quotes": quo}, c)[0]
        results.append(score("rules + ai (merged)", items, merged))
        unv = sum(len(v.get("unverified") or []) for v in cache.values())
        print(f"AI answers dropped because the quote was not in the JD: {unv}")

    print(f"\nGold set: {len(items)} JDs  (not_eligible {sum(i['verdict']=='not_eligible' for i in items)}, "
          f"check {sum(i['verdict']=='check' for i in items)}, eligible {sum(i['verdict']=='eligible' for i in items)})\n")
    cols = ["reader", "n", "verdict_acc", "false_show", "false_hide"] + [f"{k}_acc" for k in E.LABELS]
    print(" | ".join(cols))
    for r in results:
        print(" | ".join(str(r[c]) for c in cols))
    for r in results:
        print(f"\n## {r['reader']}: wrong verdicts (first {a.show})")
        for it, lab in r["_wrong"][:a.show]:
            diff = {k: f"{lab[k]} (gold {it['gold'][k]})" for k in E.LABELS if lab[k] != it["gold"][k]}
            print(f"  #{it['id']} {it['company'][:22]} | {it['title'][:40]} | gold {it['verdict']} | {diff}")
    json.dump([{k: v for k, v in r.items() if not k.startswith("_")} for r in results],
              open(os.path.join(HERE, f"last_eval_{a.set}.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
