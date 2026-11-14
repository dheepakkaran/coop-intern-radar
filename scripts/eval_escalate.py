"""Score rules + per-check AI escalation (radar/escalate.py) on a gold set, with token counts.

python scripts/eval_escalate.py --set dev --effort low
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from radar import eligibility as E, escalate as X, llm  # noqa: E402
from scripts.eval_gold import verdict  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="dev", choices=["dev", "test"])
    ap.add_argument("--effort", default="low")
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--model", default=llm.MODEL)
    a = ap.parse_args()
    items = json.load(open(os.path.join(ROOT, "tests", "gold", f"{a.set}.json")))
    cache_path = os.path.join(ROOT, ".cache", "cascade", f"esc_{a.set}_{a.model.replace('/', '_')}_{a.effort}.json")
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    budget = llm.Budget(run_tokens=10**9, run_requests=10**6)

    plans = {}
    for it in items:
        lab, quo = E.labels(it["excerpt"])
        plans[str(it["id"])] = ({"labels": lab, "quotes": quo}, X.plan(it["excerpt"], {"labels": lab, "quotes": quo}))
    todo = [(i, p) for i, (_, p) in plans.items() if p and i not in cache]
    for j in range(0, len(todo), a.batch):
        got = X.ask(todo[j:j + a.batch], budget, model=a.model, effort=a.effort)
        for i, ans in got.items():
            cache[i] = {k: list(v) for k, v in ans.items()}
        json.dump(cache, open(cache_path, "w"), indent=0)
        if budget.daily_limit_hit:
            print("daily limit hit; rerun later"); break
    # second pass: items the model skipped or answered badly, in small batches
    retry = [(i, p) for i, (_, p) in plans.items() if p and i not in cache]
    for j in range(0, len(retry), 3):
        if budget.daily_limit_hit:
            break
        got = X.ask(retry[j:j + 3], budget, model=a.model, effort=a.effort)
        for i, ans in got.items():
            cache[i] = {k: list(v) for k, v in ans.items()}
        json.dump(cache, open(cache_path, "w"), indent=0)

    n = ok = fs = fh = esc = checks_asked = missing = 0
    wrong = []
    for it in items:
        i = str(it["id"])
        rules, p = plans[i]
        if p:
            esc += 1
            checks_asked += len(p)
            if i not in cache:
                missing += 1
                continue
            lab, _ = X.apply(rules, {k: tuple(v) for k, v in cache[i].items()})
        else:
            lab = rules["labels"]
        n += 1
        v, g = verdict(lab), verdict(it["gold"])  # gold verdict under today's policy (no sponsorship passes)
        ok += v == g
        fs += g == "not_eligible" and v != "not_eligible"
        fh += g != "not_eligible" and v == "not_eligible"
        if v != g:
            wrong.append((it, lab))
    rep = budget.report()
    tok = rep["prompt_tokens"] + rep["completion_tokens"]
    print(f"{a.set}: {len(items)} JDs, scored {n}, unanswered {missing} | {a.model} effort {a.effort} batch {a.batch}")
    print(f"verdict_acc {ok / max(1, n):.3f}  false_show {fs}  false_hide {fh}")
    print(f"escalated JDs {esc} ({esc / len(items):.0%}), checks asked {checks_asked} | new-call tokens {rep} "
          f"-> {tok / max(1, len(items)):.0f} tokens/JD over all JDs ({tok / max(1, esc):.0f} per escalated JD)")
    for it, lab in wrong[:20]:
        diff = {k: f"{lab[k]} (gold {it['gold'][k]})" for k in E.LABELS if lab[k] != it["gold"][k]}
        print(f"  #{it['id']} {it['company'][:20]} | gold {it['verdict']} | {diff}")


if __name__ == "__main__":
    main()
