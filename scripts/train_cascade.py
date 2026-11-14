"""Train the cascade's local model (layer 1) and pick its 'sure' thresholds.

Data: sentence labels from the dev gold set (a sentence gets a code when it holds the gold quote for that check)
plus, when present, teacher labels from .cache/cascade/teacher.json (scripts/teacher_label.py). The held-out
test set is never used here.

Thresholds: 5-fold cross-validation grouped by JD on dev; `hi` = lowest probability where predicted-yes is
>= 98% precise, `lo` = highest probability where predicted-no misses <= 1% of true codes. Between lo and hi the
sentence goes to the AI.

python scripts/train_cascade.py            -> writes radar/model/cascade.json and prints the CV report
"""
import json
import os
import re
import sys

import numpy as np
from sklearn.linear_model import LogisticRegression

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from radar import cascade as C  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
GOLD_TO_CODE = {("citizenship", "required"): "C", ("cpt", "rejected"): "R", ("cpt", "welcome"): "W",
                ("sponsorship", "no_sponsorship"): "S", ("sponsorship", "sponsors"): "V",
                ("degree", "ms_ok"): "M", ("degree", "ug_only"): "U", ("degree", "phd_only"): "D",
                ("degree", "ug_preferred"): "B", ("grad", "fits"): "F", ("grad", "misses"): "X"}


def norm(s):
    return re.sub(r"\s+", " ", s.lower()).strip(" .")


def dev_sentences():
    """[(jd_id, sentence, codes)] from the dev gold set."""
    rows = []
    for it in json.load(open(os.path.join(ROOT, "tests", "gold", "dev.json"))):
        quotes = {k: norm(v) for k, v in (it.get("quotes") or {}).items()}
        for s in C.sentences(it["excerpt"]):
            ns, codes = norm(s), ""
            for (k, v), code in GOLD_TO_CODE.items():
                q = quotes.get(k)
                if it["gold"][k] == v and q and (q in ns or ns in q):
                    codes += code
            rows.append((it["id"], s, codes))
    return rows


def fit(X, Y):
    W, b = [], []
    for j in range(Y.shape[1]):
        y = Y[:, j]
        if y.sum() < 2:
            W.append(np.zeros(X.shape[1])); b.append(-8.0)
            continue
        m = LogisticRegression(C=2.0, class_weight="balanced", max_iter=2000).fit(X, y)
        W.append(m.coef_[0]); b.append(m.intercept_[0])
    return np.array(W), np.array(b)


def proba(X, W, b):
    return 1 / (1 + np.exp(-(X @ W.T + b)))


def main():
    dev = dev_sentences()
    teacher_path = os.path.join(ROOT, ".cache", "cascade", "teacher.json")
    teacher = json.load(open(teacher_path)) if os.path.exists(teacher_path) else {}
    dev_set = {s for _, s, _ in dev}
    extra = [(s, "".join(c for c in codes if c in C.CODES)) for s, codes in teacher.items() if s not in dev_set]
    print(f"dev sentences {len(dev)} (from {len({j for j, _, _ in dev})} JDs), teacher sentences {len(extra)}")

    def Ymat(codes_list):
        return np.array([[c in codes for c in C.CODES] for codes in codes_list], dtype=int)

    Xd = C.features([s for _, s, _ in dev]); Yd = Ymat([c for _, _, c in dev])
    Xt = C.features([s for s, _ in extra]) if extra else np.zeros((0, Xd.shape[1])); Yt = Ymat([c for _, c in extra]) if extra else np.zeros((0, len(C.CODES)), int)

    # 5-fold CV grouped by JD (teacher sentences always in train)
    ids = sorted({j for j, _, _ in dev})
    fold = {j: i % 5 for i, j in enumerate(ids)}
    oof = np.zeros(Yd.shape, dtype=float)
    for f in range(5):
        tr = np.array([fold[j] != f for j, _, _ in dev]); te = ~tr
        W, b = fit(np.vstack([Xd[tr], Xt]), np.vstack([Yd[tr], Yt]))
        oof[te] = proba(Xd[te], W, b)

    # per-code thresholds: hi = lowest p where 'yes' is >= 97% precise; lo = highest p that loses <= 2% of true codes
    grid = np.linspace(0.01, 0.99, 99)
    pos = Yd == 1
    lo, hi = [], []
    for j, c in enumerate(C.CODES):
        pj, yj = oof[:, j], pos[:, j]
        if yj.sum() < 3:  # too few examples to trust the local model: always ask unless clearly nothing
            lo.append(0.05); hi.append(1.01); continue
        h = next((t for t in grid if (pj > t).sum() >= 3 and (yj & (pj > t)).sum() / (pj > t).sum() >= 0.97), 1.01)
        l = max([t for t in grid if (yj & (pj < t)).sum() <= 0.02 * yj.sum()] or [0.01])
        lo.append(float(min(l, h))); hi.append(float(h))
    lo_a, hi_a = np.array(lo), np.array(hi)
    sure = np.all((oof < lo_a) | (oof > hi_a), axis=1)
    pred = (oof > hi_a).astype(int)
    agree = np.all(pred[sure] == Yd[sure], axis=1).mean() if sure.any() else 0
    print(f"sentences decided locally: {sure.mean():.1%}   (agreement with gold on those: {agree:.1%})")
    print(f"sentences sent to the AI: {(~sure).mean():.1%}")
    for j, c in enumerate(C.CODES):
        n = int(pos[:, j].sum())
        tp = int(((oof[:, j] > hi[j]) & pos[:, j]).sum()); miss = int(((oof[:, j] < lo[j]) & pos[:, j]).sum())
        print(f"  {c}: lo {lo[j]:.2f} hi {hi[j]:.2f} | gold {n:3d} local-yes {tp:3d} lost {miss}")

    W, b = fit(np.vstack([Xd, Xt]), np.vstack([Yd, Yt]))
    os.makedirs(os.path.dirname(C.MODEL_PATH), exist_ok=True)
    json.dump({"codes": C.CODES, "W": W.round(5).tolist(), "b": b.round(5).tolist(), "lo": lo, "hi": hi,
               "meta": {"embed": C.EMBED_MODEL, "dev_sentences": len(dev), "teacher_sentences": len(extra),
                        "local_share": float(sure.mean()), "local_agreement": float(agree)}},
              open(C.MODEL_PATH, "w"))
    json.dump({"oof": oof.round(4).tolist(), "rows": [[j, s, c] for j, s, c in dev]},
              open(os.path.join(ROOT, ".cache", "cascade", "oof_dev.json"), "w"))
    print("saved", C.MODEL_PATH)


if __name__ == "__main__":
    main()
