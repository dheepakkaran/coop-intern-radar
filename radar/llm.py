"""Second reader: an LLM on Groq reads each JD excerpt and answers the same five checks as the rules.

Trust model (the LLM is never trusted blindly):
  * it must return, for every non-neutral answer, the exact sentence from the excerpt that supports it;
  * the code checks that the sentence really is in the excerpt (whitespace/case-insensitive); an answer whose
    quote is not found is dropped back to neutral and counted as "unverified";
  * the rules and the LLM are then merged in `merge()`: a verified "no" from either side hides the job,
    and a disagreement is kept on the job so the page can show it.
No key (GROQ_API_KEY) -> the reader is off and the rules decide alone.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

from .eligibility import LABELS, NEUTRAL

API = "https://api.groq.com/openai/v1/chat/completions"
MODEL = os.environ.get("RADAR_LLM_MODEL", "qwen/qwen3.8-27b")
LLM_VERSION = 1  # bump when the prompt or model changes: cached answers are redone

SYSTEM = """You check whether one applicant may apply to a US internship/co-op, using only the job-description \
excerpt you are given. The applicant: a master's student (MS, Electrical & Computer Engineering) on an F-1 student \
visa who needs CPT (Curricular Practical Training) to work; expected graduation May 2028; not a US citizen, no \
green card, no security clearance.

Answer five questions with exactly these values:
- citizenship: "required" if the excerpt requires US citizenship, a green card / permanent residency, "U.S. person" \
status (ITAR/EAR), or an active or obtainable security clearance; else "not_required".
- cpt: "rejected" if it says F-1 / CPT / OPT / student-visa holders are not eligible or do not meet requirements, \
or it requires PERMANENT work authorization; "welcome" if it explicitly says CPT / F-1 students can apply; else "silent".
- sponsorship: "no_sponsorship" if it will not sponsor, or candidates must not need sponsorship now or in the future, \
or must be authorized to work for any employer; "sponsors" if it says it will sponsor; else "silent".
- degree: "ms_ok" if master's students can apply (master's, graduate students, BS/MS, "BS or higher", all levels); \
"ug_only" if only undergraduate / bachelor's students (e.g. "pursuing a Bachelor's", "rising junior/senior", \
"enrolled in an undergraduate program") with no graduate students; "phd_only" if only PhD students; \
"ug_preferred" if a bachelor's / undergrad is only preferred; else "unclear".
- grad: "fits" if a stated graduation window or date includes May 2028 (±3 months); "misses" if a stated \
graduation window or date excludes May 2028 (e.g. "graduating Dec 2028 or later", "class of 2029", "graduated in 2026"); \
else "not_stated". Ignore dates that are not about the applicant's graduation.

For every answer that is not the neutral value (not_required / silent / unclear / not_stated), give the EXACT \
sentence copied from the excerpt that supports it. Copy it character for character; do not paraphrase.

Reply with JSON only: {"citizenship": ..., "cpt": ..., "sponsorship": ..., "degree": ..., "grad": ..., \
"quotes": {"<question>": "<exact sentence>", ...}}"""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[‘’]", "'", s or "")).strip().lower()


def verify(answer: dict, excerpt: str) -> tuple[dict, dict, list]:
    """Keep only valid values; a non-neutral answer needs a quote that is really in the excerpt."""
    ex = _norm(excerpt)
    quotes = answer.get("quotes") or {}
    lab, quo, unverified = dict(NEUTRAL), {}, []
    for k, allowed in LABELS.items():
        v = answer.get(k)
        if v not in allowed or v == NEUTRAL[k]:
            continue
        q = quotes.get(k) or ""
        nq = _norm(q).rstrip(".")
        if len(nq) >= 12 and nq in ex:
            lab[k], quo[k] = v, q.strip()
        else:
            unverified.append(k)
    return lab, quo, unverified


def ask(excerpt: str, key: str | None = None, model: str | None = None, post=None, tries: int = 4) -> dict | None:
    """One excerpt -> {'labels', 'quotes', 'unverified', 'model'} or None (no key / API trouble)."""
    key = key or os.environ.get("GROQ_API_KEY")
    if not key or not excerpt:
        return None
    body = {"model": model or MODEL, "temperature": 0, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": "Excerpt:\n" + excerpt}]}
    post = post or _post
    for i in range(tries):
        try:
            raw = post(body, key)
            text = raw["choices"][0]["message"]["content"]
            text = re.sub(r"(?s)<think>.*?</think>", "", text).strip()  # reasoning models may prepend thoughts
            answer = json.loads(text[text.find("{"):text.rfind("}") + 1])
            lab, quo, unv = verify(answer, excerpt)
            return {"labels": lab, "quotes": quo, "unverified": unv, "model": body["model"]}
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = float(e.headers.get("retry-after") or 0) if e.headers else 0
                time.sleep(min(60, max(wait, 5 * (i + 1))))
                continue
            return None
        except (ValueError, KeyError, IndexError, urllib.error.URLError, TimeoutError):
            time.sleep(2)
            continue
    return None


def _post(body: dict, key: str) -> dict:
    req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "User-Agent": "coop-intern-radar"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


# ---- merging the two readers ----------------------------------------------------------------------------------
SEVERITY = {  # higher = worse for the applicant
    "citizenship": {"not_required": 0, "required": 3},
    "cpt": {"welcome": 0, "silent": 1, "rejected": 3},
    "sponsorship": {"sponsors": 0, "silent": 1, "no_sponsorship": 2},
    "degree": {"ms_ok": 0, "unclear": 1, "ug_preferred": 2, "ug_only": 3, "phd_only": 3},
    "grad": {"fits": 0, "not_stated": 1, "misses": 3},
}


def merge(rules: dict, ai: dict | None) -> tuple[dict, dict, list]:
    """Per check, the worse verified answer wins (a 'no' from either reader hides the job).

    Returns (labels, quotes, disagreements) where disagreements = checks the two readers answered differently."""
    if not ai:
        return dict(rules["labels"]), dict(rules["quotes"]), []
    lab, quo, dis = {}, {}, []
    for k in LABELS:
        r, a = rules["labels"][k], ai["labels"][k]
        if r != a:
            dis.append(k)
        if SEVERITY[k][a] > SEVERITY[k][r]:
            lab[k] = a
            if k in ai["quotes"]:
                quo[k] = ai["quotes"][k]
        else:
            lab[k] = r
            if k in rules["quotes"]:
                quo[k] = rules["quotes"][k]
    return lab, quo, dis
