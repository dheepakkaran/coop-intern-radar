"""Second reader: an LLM on Groq reads each JD excerpt and answers the same five checks as the rules.

Trust model (the LLM is never trusted blindly):
  * every non-neutral answer must come with the exact sentence from the excerpt; the code checks the sentence is
    really there (whitespace/case-insensitive) and drops the answer otherwise ("unverified");
  * held-out test set (130 JDs, 2026-11-14): gpt-oss-120b 95.4% verdict accuracy, 0 / 35 not-eligible jobs shown,
    6 eligible jobs hidden, 1 made-up quote; qwen3.8-27b 93.8%, 0 shown, 8 hidden, 9 made-up quotes; rules 82.3%, 16 shown.

Token management (free tier: ~8,000 tokens/min, ~1,000 requests/day, small output-tokens/min on some models):
  * input = the excerpt only (eligibility sentences), trimmed to MAX_EXCERPT chars, most important sentences first;
  * several excerpts per request (BATCH), so the system prompt is paid once per batch;
  * reasoning kept low/off and max_tokens set from the batch size (output limits count the reserved max_tokens);
  * results cached by a hash of the excerpt (same boilerplate across a company's jobs = one call);
  * excerpts with no eligibility words at all are answered "all neutral" without a call;
  * the rate-limit headers are read after every call; when the minute budget runs low we wait for the reset,
    and when the run budget is spent the rest waits for the next run;
  * token usage is counted and reported in the site meta.
No key (GROQ_API_KEY) -> the reader is off and the rules decide alone.
"""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request

from .eligibility import LABELS, NEUTRAL

API = "https://api.groq.com/openai/v1/chat/completions"
MODEL = os.environ.get("RADAR_LLM_MODEL") or "openai/gpt-oss-120b"  # empty env var (unset GitHub var) -> default
LLM_VERSION = 2       # bump when the prompt, model settings or excerpt trimming change: cached answers are redone
BATCH = int(os.environ.get("RADAR_LLM_BATCH") or 5)        # excerpts per request
EFFORT = os.environ.get("RADAR_LLM_EFFORT") or "low"        # gpt-oss reasoning effort: low / medium / high
MAX_EXCERPT = 1200    # chars per excerpt sent to the model
OUT_PER_ITEM = 140    # output tokens reserved per excerpt (answers + quotes; gpt-oss adds a little reasoning)
OUT_BASE = 220

SYSTEM = """You check if ONE applicant may apply to US internships/co-ops, from job-description excerpts.
Applicant: master's student (MS Electrical & Computer Engineering), F-1 visa, needs CPT to work, graduates May 2028.
Not a US citizen, no green card, no clearance.

For each excerpt answer 5 questions (exact values):
citizenship: "required" = must be US citizen / green card / "U.S. person" (ITAR/EAR) / have or get a security clearance / hold another country's citizenship. A company that only MAY decline non-U.S. persons is "not_required".
cpt: "rejected" = F-1/CPT/OPT/student-visa holders not eligible or "do not meet" requirements, or PERMANENT work authorization required. "welcome" = says CPT/F-1 students can apply. Else "silent".
sponsorship: "no_sponsorship" = won't sponsor / must not need sponsorship now or in future / authorized for any employer. "sponsors" = will sponsor. Else "silent".
degree: "ms_ok" = master's or graduate students can apply (BS/MS, "BS or higher", graduate internship, all levels). "ug_only" = only undergraduates (pursuing a Bachelor's, rising junior/senior, undergraduate program). "phd_only" = only PhD students. "ug_preferred" = undergrad only preferred. Else "unclear". A requirement line beats a softer mention (intro text, pay table).
grad: "fits" = stated graduation window includes May 2028 (±3 months). "misses" = stated window excludes it. Else "not_stated". Ignore dates not about the applicant's graduation.

Only answer from the excerpt. For each non-default answer (default: not_required, silent, silent, unclear, not_stated) copy the supporting sentence EXACTLY into q.
Reply JSON only: {"r":[{"id":<id>,"citizenship":..,"cpt":..,"sponsorship":..,"degree":..,"grad":..,"q":{"<question>":"<exact sentence>"}}]}"""

# ---- excerpt: trimmed, most important sentences first --------------------------------------------------------
PRIORITY = [re.compile(p, re.I) for p in (
    r"citizen|clearance|itar|export|u\.s\. person|green card|permanent",
    r"sponsor|visa|\bcpt\b|\bopt\b|f-1|authori[sz]",
    r"bachelor|master|undergrad|graduate|ph\.?d|degree|pursuing|enrolled|junior|senior|sophomore|freshman",
    r"graduat|class of|semester",
)]
SIGNAL = re.compile("|".join(p.pattern for p in PRIORITY), re.I)


def trim(excerpt: str, limit: int = MAX_EXCERPT) -> str:
    """Keep whole sentences, most important groups first, in their original order, up to `limit` chars."""
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", excerpt or "") if s.strip()]
    rank = {i: next((k for k, rx in enumerate(PRIORITY) if rx.search(s)), len(PRIORITY)) for i, s in enumerate(sents)}
    keep, used = set(), 0
    for i in sorted(range(len(sents)), key=lambda i: (rank[i], i)):
        if used + len(sents[i]) + 1 > limit:
            continue
        keep.add(i)
        used += len(sents[i]) + 1
    return " ".join(sents[i] for i in sorted(keep))


def key(excerpt: str) -> str:
    return hashlib.sha1(f"{MODEL}|{LLM_VERSION}|{EFFORT}|{trim(excerpt)}".encode()).hexdigest()[:20]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[‘’]", "'", s or "")).strip().lower()


def verify(answer: dict, excerpt: str) -> tuple[dict, dict, list]:
    """Keep only valid values; a non-neutral answer needs a quote that is really in the excerpt."""
    ex = _norm(excerpt)
    quotes = answer.get("q") or answer.get("quotes") or {}
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


# ---- calling Groq -------------------------------------------------------------------------------------------
class Budget:
    """Token/request accounting for one run, fed by Groq's rate-limit headers."""

    def __init__(self, run_tokens: int = 60000, run_requests: int = 60):
        self.run_tokens, self.run_requests = run_tokens, run_requests
        self.prompt = self.completion = self.requests = self.waited = 0
        self.remaining_tokens, self.reset_tokens = None, 0.0

    def spent(self) -> bool:
        return self.prompt + self.completion >= self.run_tokens or self.requests >= self.run_requests

    def before(self, need: int) -> None:
        """Wait for the per-minute window if the last response said we're too close to the limit."""
        if self.remaining_tokens is not None and self.remaining_tokens < need:
            wait = min(65.0, self.reset_tokens + 0.5)
            time.sleep(wait)
            self.waited += wait
            self.remaining_tokens = None

    def after(self, usage: dict, headers: dict) -> None:
        self.requests += 1
        self.prompt += int(usage.get("prompt_tokens") or 0)
        self.completion += int(usage.get("completion_tokens") or 0)
        h = {k.lower(): v for k, v in (headers or {}).items()}
        if "x-ratelimit-remaining-tokens" in h:
            self.remaining_tokens = int(float(h["x-ratelimit-remaining-tokens"]))
            self.reset_tokens = _seconds(h.get("x-ratelimit-reset-tokens", "0s"))

    def report(self) -> dict:
        return {"requests": self.requests, "prompt_tokens": self.prompt, "completion_tokens": self.completion,
                "waited_s": round(self.waited, 1)}


def _seconds(s: str) -> float:
    total = 0.0
    for num, unit in re.findall(r"([\d.]+)(ms|h|m|s)", s or ""):
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total


def _post(body: dict, api_key: str) -> tuple[dict, dict]:
    req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                                          "User-Agent": "coop-intern-radar"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r), dict(r.headers)


def _body(items: list[tuple[str, str]], model: str) -> dict:
    user = "\n\n".join(f"[id {i}]\n{trim(ex)}" for i, ex in items)
    body = {"model": model, "temperature": 0, "response_format": {"type": "json_object"},
            "max_tokens": OUT_BASE + OUT_PER_ITEM * len(items),
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}
    if "gpt-oss" in model:
        body["reasoning_effort"] = EFFORT
        if EFFORT != "low":  # reasoning tokens count as output: reserve more
            body["max_tokens"] += {"medium": 350, "high": 900}.get(EFFORT, 0) * len(items)
    elif "qwen3" in model:
        body["reasoning_effort"] = "none"
    return body


def ask_batch(items: list[tuple[str, str]], api_key: str | None = None, model: str | None = None,
              budget: Budget | None = None, post=None, tries: int = 4) -> dict:
    """[(id, excerpt), ...] -> {id: {'labels','quotes','unverified','model'}} (missing ids = not answered)."""
    api_key = api_key or os.environ.get("GROQ_API_KEY")
    model = model or MODEL
    if not api_key or not items:
        return {}
    budget = budget or Budget()
    post = post or _post
    body = _body(items, model)
    need = body["max_tokens"] + len(json.dumps(body["messages"])) // 3
    for attempt in range(tries):
        budget.before(need)
        try:
            raw, headers = post(body, api_key)
            budget.after(raw.get("usage") or {}, headers)
            text = re.sub(r"(?s)<think>.*?</think>", "", raw["choices"][0]["message"]["content"] or "").strip()
            data = json.loads(text[text.find("{"):text.rfind("}") + 1])
            rows = data.get("r") if isinstance(data, dict) else data
            out = {}
            excerpts = {str(i): trim(ex) for i, ex in items}
            for row in rows or []:
                if isinstance(row, str):  # some models return each row as a JSON string
                    try:
                        row = json.loads(row)
                    except ValueError:
                        continue
                if not isinstance(row, dict):
                    continue
                rid = str(row.get("id"))
                if rid in excerpts:
                    lab, quo, unv = verify(row, excerpts[rid])
                    out[rid] = {"labels": lab, "quotes": quo, "unverified": unv, "model": model}
            return out
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = _seconds((e.headers or {}).get("retry-after", "") + "s") if e.headers else 0
                time.sleep(min(65, max(wait, 5 * (attempt + 1))))
                budget.waited += min(65, max(wait, 5 * (attempt + 1)))
                continue
            return {}
        except (ValueError, KeyError, IndexError, TypeError, AttributeError, urllib.error.URLError, TimeoutError):
            time.sleep(2)
            continue
    return {}


def ask(excerpt: str, key: str | None = None, model: str | None = None, post=None, tries: int = 4) -> dict | None:
    """One excerpt (kept for simple callers and tests)."""
    if not excerpt:
        return None
    if post is not None:  # old single-call test hook: adapt (body, key) -> response
        res = ask_batch([("1", excerpt)], api_key=key, model=model, tries=tries,
                        post=lambda body, k: (post(body, k), {}))
    else:
        res = ask_batch([("1", excerpt)], api_key=key, model=model, tries=tries)
    return res.get("1")


def neutral_if_no_signal(excerpt: str | None) -> dict | None:
    """Excerpts without a single eligibility word need no model call: every answer is the default."""
    if excerpt is not None and not SIGNAL.search(excerpt):
        return {"labels": dict(NEUTRAL), "quotes": {}, "unverified": [], "model": "none (no eligibility words)"}
    return None


# ---- merging the two readers ----------------------------------------------------------------------------------
SEVERITY = {  # higher = worse for the applicant
    "citizenship": {"not_required": 0, "required": 3},
    "cpt": {"welcome": 0, "silent": 1, "rejected": 3},
    "sponsorship": {"sponsors": 0, "silent": 1, "no_sponsorship": 2},
    "degree": {"ms_ok": 0, "unclear": 1, "ug_preferred": 2, "ug_only": 3, "phd_only": 3},
    "grad": {"fits": 0, "not_stated": 1, "misses": 3},
}
BLOCKING = {"citizenship": "required", "cpt": "rejected", "degree": ("ug_only", "phd_only"), "grad": "misses"}


def _blocks(k: str, v: str) -> bool:
    b = BLOCKING.get(k)
    return v in b if isinstance(b, tuple) else v == b


def merge(rules: dict, ai: dict | None) -> tuple[dict, dict, list]:
    """Combine the readers when the AI has read the JD.

    The AI decides (it was far more accurate on the held-out set). A blocking answer the rules gave but the AI
    did not is NOT used to hide the job: it is kept as a disagreement, and the check shows as 'needs a look'.
    Returns (labels, quotes, disagreements)."""
    if not ai:
        return dict(rules["labels"]), dict(rules["quotes"]), []
    lab, quo, dis = {}, {}, []
    for k in LABELS:
        r, a = rules["labels"][k], ai["labels"][k]
        lab[k] = a
        if k in ai["quotes"]:
            quo[k] = ai["quotes"][k]
        if r != a:
            dis.append(k)
            if _blocks(k, r) and not _blocks(k, a):
                if k in rules["quotes"]:
                    quo[k] = rules["quotes"][k] + "  [rules read this as blocking; AI did not]"
    return lab, quo, dis
