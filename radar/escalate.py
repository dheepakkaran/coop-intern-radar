"""Rules first, AI only where the rules are unsure — per check, per sentence (token-light second reader).

For each blocking check (citizenship, CPT, degree, graduation) the rules give a label and a confidence:
  * no sentence in the JD even mentions the topic           -> confident (nothing to read; rules were 100% on dev)
  * a strong pattern fired and that label was >= 95% right on dev -> confident
  * otherwise (topic words present but no pattern, or a weaker label) -> unsure
Only unsure checks go to the AI, and only the sentences that mention that topic. Many JDs share one request,
the system prompt is identical every time (Groq caches it), the answer is a letter per check plus the number of
the supporting sentence, so the quote is our own sentence and cannot be made up.
"""
import json
import os
import re

from . import eligibility as E

TOPIC = {
    "citizenship": re.compile(r"citizen|clearance|\bitar\b|export[- ]control|u\.s\. person|us person|green card|"
                              r"permanent resident|lawful permanent", re.I),
    "cpt": re.compile(r"\bcpt\b|\bopt\b|f-1|student visa|work authori|authori[sz]ed to work|permanent(ly)?\b|"
                      r"curricular practical", re.I),
    "degree": re.compile(r"bachelor|master|undergrad|graduate (student|level|program|degree)|ph\.?d|doctor|"
                         r"\bjunior|\bsenior|sophomore|freshman|\bmba\b|\bb\.?s\.?\b|\bm\.?s\.?\b|pursuing|enrolled", re.I),
    "grad": re.compile(r"graduat\w* (between|by|in|no|after|before|date)|class of|expected graduation", re.I),
}
# labels the rules may decide alone (>= 95% correct on the dev set, 2026-11-14)
CONFIDENT = {
    "citizenship": {"not_required"},       # only when no topic sentence (see below)
    "cpt": {"rejected", "welcome", "silent"},
    "degree": {"ug_only", "phd_only", "ms_ok"},
    "grad": {"fits"},
}
LETTER = {"citizenship": "C", "cpt": "P", "degree": "D", "grad": "G"}
ANSWERS = {
    "citizenship": {"Y": "required", "N": "not_required"},
    "cpt": {"R": "rejected", "W": "welcome", "N": "silent"},
    "degree": {"M": "ms_ok", "U": "ug_only", "P": "phd_only", "B": "ug_preferred", "N": "unclear"},
    "grad": {"F": "fits", "X": "misses", "N": "not_stated"},
}
BLOCK = {"Y", "R", "U", "P", "X"}

PROMPT = """Applicant: MS ECE student on F-1 needing CPT, graduates May 2028; not a US citizen, no green card, no clearance.
For each item answer ONLY the asked checks, using only that item's numbered sentences.
C citizenship: Y = US citizenship, green card, U.S. person (ITAR/EAR) or a security clearance is REQUIRED; N = not required (a company that only MAY decline non-U.S. persons is N).
P CPT: R = F-1/CPT/OPT holders not eligible or "do not meet" requirements, or PERMANENT work authorization required; W = CPT/F-1 students can apply; N = neither.
D degree: M = master's/graduate students can apply; U = undergraduates only; P = PhD only; B = bachelor's only preferred; N = not stated. A requirement line beats a softer mention.
G graduation: F = stated window includes May 2028 (±3 months); X = stated window excludes it; N = not stated.
For Y, R, U, P, X add @n = the sentence number that says it.
Reply one line per item, nothing else, e.g.:  7 C=N D=U@2"""


ABBR = re.compile(r"\b(U\.S|U\.S\.C|e\.g|i\.e|etc|Inc|No|vs|Ph\.D|B\.S|M\.S|B\.A|M\.A|Jr|Sr|Dr|Mr|Ms|St)\.", re.I)


def sentences(excerpt: str) -> list[str]:
    """Split into sentences without breaking "U.S. person", "8 U.S.C.", "e.g.", "Ph.D." apart."""
    protected = ABBR.sub(lambda m: m.group(0).replace(".", "\u2024"), excerpt or "")
    out = []
    for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9•\-(])", protected):
        s = s.replace("\u2024", ".").strip()
        if 12 < len(s) < 600:
            out.append(s)
    return out


def plan(excerpt: str | None, rules: dict) -> dict:
    """{check: [sentences]} for the checks the rules are unsure about (empty = rules decide alone)."""
    if not excerpt or not rules or not rules.get("labels"):
        return {}
    sents = sentences(excerpt)
    out = {}
    for k, rx in TOPIC.items():
        topic = [s for s in sents if rx.search(s)]
        lab = rules["labels"][k]
        if not topic:
            continue                                   # nothing about it in the JD: neutral is safe
        if k == "citizenship" and lab == "required":
            # strong citizenship patterns are right ~92%: confirm with the AI (one sentence is enough)
            q = rules["quotes"].get(k)
            out[k] = [q] if q else topic[:3]
        elif lab in CONFIDENT[k] and lab != E.NEUTRAL[k]:
            continue                                   # a confident non-neutral label
        elif k == "cpt" and lab == "silent":
            if not re.search(r"\bcpt\b|\bopt\b|f-1|student visa|permanent", " ".join(topic), re.I):
                continue                               # only "authorized to work" lines: not a CPT rule
            out[k] = topic[:3]
        else:
            out[k] = topic[:3]                         # topic words but no / weak pattern: ask
    return out


def ask(items: list[tuple[str, dict]], budget, api_key: str | None = None, model: str | None = None,
        effort: str = "low", post=None) -> dict:
    """[(id, {check: [sentences]})] -> {id: {check: (label, sentence_or_None)}}. Missing ids = not answered."""
    from . import llm
    api_key = api_key or os.environ.get("GROQ_API_KEY")
    if not api_key or not items:
        return {}
    blocks, numbered = [], {}
    for iid, checks in items:
        sents, seen = [], {}
        for k in checks:
            for s in checks[k]:
                if s not in seen:
                    seen[s] = len(sents) + 1
                    sents.append(s)
        numbered[str(iid)] = sents
        ask_for = " ".join(LETTER[k] for k in checks)
        blocks.append(f"{iid} ask {ask_for}\n" + "\n".join(f"{n}. {s}" for n, s in enumerate(sents, 1)))
    body = {"model": model or llm.MODEL, "temperature": 0,
            "max_tokens": 300 + 24 * len(items) + (900 if effort == "medium" else 500),
            "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": "\n\n".join(blocks)}]}
    if "gpt-oss" in body["model"]:
        body["reasoning_effort"] = effort
    post = post or llm._post
    import time
    for attempt in range(3):
        budget.before(body["max_tokens"] + sum(len(b) for b in blocks) // 3 + 300)
        try:
            raw, headers = post(body, api_key)
            budget.after(raw.get("usage") or {}, headers)
            return parse(raw["choices"][0]["message"]["content"] or "", items, numbered)
        except llm.urllib.error.HTTPError as e:
            msg = ""
            try:
                msg = (e.read() or b"").decode("utf-8", "ignore")
            except Exception:
                pass
            if e.code == 429 and "per day" in msg:
                budget.daily_limit_hit = True
                return {}
            time.sleep(min(30, 6 * (attempt + 1)))
        except Exception:
            time.sleep(2)
    return {}


def parse(text: str, items, numbered) -> dict:
    asked = {str(i): set(c) for i, c in items}
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*\[?(\w+)\]?\s+(.*)", line)
        if not m or m.group(1) not in asked:
            continue
        iid, rest = m.group(1), m.group(2)
        got = {}
        for k in asked[iid]:
            mm = re.search(LETTER[k] + r"\s*=\s*([A-Z])(?:\s*@\s*(\d+))?", rest)
            if not mm or mm.group(1) not in ANSWERS[k]:
                continue
            letter, n = mm.group(1), mm.group(2)
            sent = None
            if n and 1 <= int(n) <= len(numbered[iid]):
                sent = numbered[iid][int(n) - 1]
            if letter in BLOCK and not sent:
                continue                                # a blocking answer must point at a sentence
            got[k] = (ANSWERS[k][letter], sent)
        if got and set(got) == asked[iid]:
            out[iid] = got
    return out


def apply(rules: dict, answers: dict | None) -> tuple[dict, dict]:
    """Rules' labels with the AI's answers for the checks it was asked about."""
    lab, quo = dict(rules["labels"]), dict(rules.get("quotes") or {})
    for k, (v, s) in (answers or {}).items():
        lab[k] = v
        if s:
            quo[k] = s
        elif v == E.NEUTRAL[k]:
            quo.pop(k, None)
    return lab, quo
