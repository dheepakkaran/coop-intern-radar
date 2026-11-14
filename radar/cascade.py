"""Token-light eligibility reader: a cascade that sends the AI only what it cannot decide locally.

  Layer 0  split the JD excerpt into sentences; keep only sentences with an eligibility word     (0 tokens)
  Layer 1  local model on each sentence: bge-small embedding (CPU, fastembed) + the rules' hits as
           features -> one logistic regression per code -> probability per code                  (0 tokens)
  Layer 2  sentences the local model is unsure about -> Groq, tagged input, letter-code output,
           many sentences per request, static system prompt (Groq caches it)                    (~10-40 tokens/sentence)
  Layer 3  sentence codes -> the five checks, with the sentence itself as the quote             (0 tokens)

Codes per sentence (several allowed):
  C citizenship/green card/U.S. person/clearance required   R F-1/CPT/OPT rejected or permanent authorization
  W CPT/F-1 welcome   S no sponsorship / any employer   V sponsors   M master's/graduate students OK
  U undergraduates only   D PhD only   B bachelor's preferred   F grad window includes May 2028   X excludes it
"""
import json
import os
import re

from . import eligibility as E

CODES = "CRWSVMUDBFX"
HERE = os.path.dirname(__file__)
MODEL_PATH = os.path.join(HERE, "model", "cascade.json")
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
CACHE_DIR = os.environ.get("RADAR_CACHE") or os.path.join(HERE, "..", ".cache", "fastembed")

# ---- layer 0 ---------------------------------------------------------------------------------------------------
SIGNAL = re.compile(
    r"citizen|clearance|itar|export|u\.s\. person|green card|permanent|sponsor|visa|\bcpt\b|\bopt\b|f-1|authori[sz]|"
    r"eligib|bachelor|master|undergrad|graduate|ph\.?d|doctor|degree|pursuing|enrolled|enrollment|junior|senior|"
    r"sophomore|freshman|graduat|class of|\bb\.?s\.?\b|\bm\.?s\.?\b|mba|student", re.I)


def sentences(excerpt: str | None) -> list[str]:
    if not excerpt:
        return []
    out, seen = [], set()
    for s in re.split(r"(?<=[.!?])\s+", excerpt):
        s = s.strip()
        if 12 < len(s) < 600 and SIGNAL.search(s) and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ---- layer 1 ---------------------------------------------------------------------------------------------------
def rule_features(s: str) -> list[float]:
    """What the hand-written rules see in one sentence (one flag per code)."""
    g, _ = E.grad_check(s)
    return [
        float(bool(E.CITIZEN.search(s))),
        float(bool(E.F1_REJECT.search(s))),
        float(bool(E.CPT_OK.search(s))),
        float(bool(E.NO_SPONSOR.search(s))),
        float(bool(E.SPONSOR_YES.search(s))),
        float(bool(E.MS_OK.search(s))),
        float(bool(E.UG_ONLY.search(s) or E.UG_MUST.search(s))),
        float(bool(E.PHD_ONLY.search(s) or E.PHD_MUST.search(s))),
        float(bool(E.UG_PREF.search(s))),
        float(g == "ok"),
        float(g == "no"),
    ]


_embedder = None


def embed(texts: list[str]):
    global _embedder
    import numpy as np
    if not texts:
        return np.zeros((0, 384), dtype="float32")
    if _embedder is None:
        from fastembed import TextEmbedding
        _embedder = TextEmbedding(EMBED_MODEL, cache_dir=CACHE_DIR)
    return np.array(list(_embedder.embed(texts)), dtype="float32")


def features(sents: list[str]):
    import numpy as np
    if not sents:
        return np.zeros((0, 384 + len(CODES)), dtype="float32")
    return np.hstack([embed(sents), np.array([rule_features(s) for s in sents], dtype="float32") * 2.0])


class LocalModel:
    """One logistic regression per code, stored as plain JSON (no sklearn needed to run it)."""

    def __init__(self, path: str = MODEL_PATH):
        import numpy as np
        d = json.load(open(path))
        self.codes = d["codes"]
        self.W = np.array(d["W"], dtype="float32")    # (codes, features)
        self.b = np.array(d["b"], dtype="float32")    # (codes,)
        self.lo = np.array(d["lo"], dtype="float32")  # per code: p < lo = no, p > hi = yes, between = ask AI
        self.hi = np.array(d["hi"], dtype="float32")
        self.meta = d.get("meta", {})

    def proba(self, X):
        import numpy as np
        return 1.0 / (1.0 + np.exp(-(X @ self.W.T + self.b)))


def load_model():
    try:
        return LocalModel()
    except (OSError, ValueError, KeyError):
        return None


# ---- layer 2 ---------------------------------------------------------------------------------------------------
PROMPT = """Label sentences from US internship job descriptions for ONE applicant: master's student (MS ECE), F-1 visa, \
needs CPT, graduates May 2028, not a US citizen, no green card, no clearance.
Codes (all that apply, else N):
C requires US citizenship, green card, U.S. person (ITAR/EAR), security clearance, or another country's citizenship
R F-1/CPT/OPT/student-visa holders not eligible or "do not meet" requirements, or PERMANENT work authorization required
W CPT/F-1 students can apply
S no visa sponsorship, no sponsorship now or in future, or authorized for any employer
V company sponsors visas
M master's/graduate students can apply (BS/MS, BS or higher, graduate internship, all levels)
U only undergraduates (pursuing a Bachelor's, rising junior/senior, undergraduate program)
D only PhD students
B bachelor's only preferred
F graduation window/date includes May 2028 (±3 months)
X graduation window/date excludes May 2028
N none
A company that only MAY decline non-U.S. persons is N. Dates not about the applicant's graduation are N.
Reply only lines <id>:<codes>, e.g. 4:SM"""


def ask_codes(items: list[tuple[int, str]], budget, api_key: str | None = None, model: str | None = None,
              effort: str | None = None, post=None) -> dict:
    """[(id, sentence)] -> {id: codes} via Groq. Missing ids = not answered (quota, error)."""
    from . import llm
    api_key = api_key or os.environ.get("GROQ_API_KEY")
    if not api_key or not items:
        return {}
    model = model or llm.MODEL
    effort = effort or os.environ.get("RADAR_CASCADE_EFFORT") or "low"
    user = "\n".join(f"<s id={i}>{s}</s>" for i, s in items)
    body = {"model": model, "temperature": 0, "max_tokens": 160 + 8 * len(items) + (500 if effort == "medium" else 0),
            "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": user}]}
    if "gpt-oss" in model:
        body["reasoning_effort"] = effort
    elif "qwen3" in model:
        body["reasoning_effort"] = "none"
    post = post or llm._post
    for attempt in range(3):
        budget.before(body["max_tokens"] + len(user) // 3 + 300)
        try:
            raw, headers = post(body, api_key)
            budget.after(raw.get("usage") or {}, headers)
            text = raw["choices"][0]["message"]["content"] or ""
            out = {}
            for m in re.finditer(r"(\d+)\s*:\s*([A-Z]+)", text):
                out[int(m.group(1))] = "".join(c for c in CODES if c in m.group(2))
            return out
        except llm.urllib.error.HTTPError as e:
            if e.code == 429 and b"per day" in (e.read() or b""):
                budget.daily_limit_hit = True
                return {}
            import time
            time.sleep(min(30, 6 * (attempt + 1)))
        except Exception:
            import time
            time.sleep(2)
    return {}


# ---- layer 3 ---------------------------------------------------------------------------------------------------
def aggregate(sent_codes: list[tuple[str, str]]) -> tuple[dict, dict]:
    """[(sentence, codes)] -> (labels, quotes) for the five checks."""
    lab, quo = dict(E.NEUTRAL), {}

    def first(code):
        return next((s for s, c in sent_codes if code in c), None)

    if (s := first("C")):
        lab["citizenship"], quo["citizenship"] = "required", s
    if (s := first("R")):
        lab["cpt"], quo["cpt"] = "rejected", s
    elif (s := first("W")):
        lab["cpt"], quo["cpt"] = "welcome", s
    if (s := first("S")):
        lab["sponsorship"], quo["sponsorship"] = "no_sponsorship", s
    elif (s := first("V")):
        lab["sponsorship"], quo["sponsorship"] = "sponsors", s
    for code, value in (("M", "ms_ok"), ("U", "ug_only"), ("D", "phd_only"), ("B", "ug_preferred")):
        if (s := first(code)):
            lab["degree"], quo["degree"] = value, s
            break
    if (s := first("F")):
        lab["grad"], quo["grad"] = "fits", s
    elif (s := first("X")):
        lab["grad"], quo["grad"] = "misses", s
    return lab, quo


# ---- the whole cascade for many excerpts at once ---------------------------------------------------------------
def read(excerpts: dict, model: "LocalModel", budget, ask=None, batch: int = 30) -> dict:
    """{key: excerpt} -> {key: {'labels','quotes','layer','sent','asked'}}; keys whose AI answer is missing get
    'pending' (they'll be read on the next run)."""
    ask = ask or ask_codes
    per = {k: sentences(ex) for k, ex in excerpts.items()}
    flat = [(k, s) for k, ss in per.items() for s in ss]
    codes = {}
    stats = {"sentences": len(flat), "local": 0, "asked": 0}
    if flat:
        P = model.proba(features([s for _, s in flat]))
        unsure = []
        for i, (k, s) in enumerate(flat):
            p = P[i]
            if ((p < model.lo) | (p > model.hi)).all():
                codes[(k, s)] = "".join(c for c, x, h in zip(model.codes, p, model.hi) if x > h)
                stats["local"] += 1
            else:
                unsure.append((i, k, s))
        for j in range(0, len(unsure), batch):
            if getattr(budget, "daily_limit_hit", False) or budget.spent():
                break
            chunk = unsure[j:j + batch]
            got = ask([(i, s) for i, _, s in chunk], budget)
            for i, k, s in chunk:
                if i in got:
                    codes[(k, s)] = got[i]
                    stats["asked"] += 1
    out = {}
    for k, ss in per.items():
        if any((k, s) not in codes for s in ss):
            out[k] = {"pending": True}
            continue
        lab, quo = aggregate([(s, codes[(k, s)]) for s in ss])
        out[k] = {"labels": lab, "quotes": quo, "unverified": [], "model": "cascade"}
    out["_stats"] = stats
    return out
