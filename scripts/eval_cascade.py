"""Score the token-light cascade (radar/cascade.py) on a gold set, with token counts.

python scripts/eval_cascade.py --set dev --model openai/gpt-oss-20b --effort low
Prints verdict accuracy, false_show / false_hide, how many sentences were decided locally vs by the AI,
and AI tokens per JD. AI answers are cached in .cache/cascade/ so reruns are free.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from radar import cascade as C, eligibility as E, llm  # noqa: E402
from scripts.eval_gold import verdict  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="dev", choices=["dev", "test"])
    ap.add_argument("--model", default=llm.MODEL)
    ap.add_argument("--effort", default="low")
    ap.add_argument("--batch", type=int, default=30)
    ap.add_argument("--no-local", action="store_true", help="send every sentence to the AI (no layer 1)")
    a = ap.parse_args()
    items = json.load(open(os.path.join(ROOT, "tests", "gold", f"{a.set}.json")))
    model = C.load_model()
    if a.no_local or model is None:
        class AskAll:  # thresholds that make every sentence 'unsure'
            codes = C.CODES
            import numpy as np
            lo = np.zeros(len(C.CODES)) - 1
            hi = np.zeros(len(C.CODES)) + 2

            def proba(self, X):
                import numpy as np
                return np.full((X.shape[0], len(C.CODES)), 0.5)
        model = AskAll()

    cache_path = os.path.join(ROOT, ".cache", "cascade", f"ask_{a.set}_{a.model.replace('/', '_')}_{a.effort}.json")
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    budget = llm.Budget(run_tokens=10**9, run_requests=10**6)

    def ask(pairs, b):
        todo = [(i, s) for i, s in pairs if s not in cache]
        for j in range(0, len(todo), a.batch):
            chunk = todo[j:j + a.batch]
            got = C.ask_codes(chunk, b, model=a.model, effort=a.effort)
            for i, s in chunk:
                if i in got:
                    cache[s] = got[i]
            json.dump(cache, open(cache_path, "w"), indent=0)
            if getattr(b, "daily_limit_hit", False):
                break
        return {i: cache[s] for i, s in pairs if s in cache}

    res = C.read({str(it["id"]): it["excerpt"] for it in items}, model, budget, ask=ask, batch=10**6)
    stats = res.pop("_stats")
    n = fs = fh = ok = pend = 0
    wrong = []
    for it in items:
        r = res[str(it["id"])]
        if r.get("pending"):
            pend += 1
            continue
        n += 1
        v, g = verdict(r["labels"]), it["verdict"]
        ok += v == g
        fs += g == "not_eligible" and v != "not_eligible"
        fh += g != "not_eligible" and v == "not_eligible"
        if v != g:
            wrong.append((it, r["labels"]))
    rep = budget.report()
    tok = rep["prompt_tokens"] + rep["completion_tokens"]
    print(f"{a.set}: {len(items)} JDs, scored {n}, pending {pend} | model {a.model} effort {a.effort}")
    print(f"verdict_acc {ok / max(1, n):.3f}  false_show {fs}  false_hide {fh}")
    print(f"sentences {stats['sentences']}: local {stats['local']}, AI {stats['asked']} "
          f"| AI tokens this run {rep} -> {tok / max(1, len(items)):.0f} tokens/JD (new calls only)")
    for it, lab in wrong[:15]:
        diff = {k: f"{lab[k]} (gold {it['gold'][k]})" for k in E.LABELS if lab[k] != it["gold"][k]}
        print(f"  #{it['id']} {it['company'][:20]} | gold {it['verdict']} | {diff}")


if __name__ == "__main__":
    main()
