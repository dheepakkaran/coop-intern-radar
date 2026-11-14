"""Can a master's student on CPT apply? Read the job description and decide.

Three checks, each 'ok' / 'check' / 'no' (+ 'unknown' when there is no JD):
  auth   : US citizenship / clearance / ITAR "U.S. person" / "F-1, CPT, OPT not eligible" -> no
           "authorized for any employer" / "no sponsorship now or in future"              -> check
           CPT / OPT / F-1 explicitly welcome                                              -> ok (tag CPT ok)
  degree : undergrad-only ("enrolled in an undergraduate program", "rising junior/senior", Bachelor's only),
           PhD-only                                                                        -> no
           Master's / graduate students / BS-MS / all levels                               -> ok
           "undergraduate preferred"                                                       -> check
  grad   : a graduation window that leaves out the owner's graduation month               -> no
           a window that includes it                                                       -> ok

Rules were written from ~1,300 eligibility sentences in real intern JDs (notes: docs/eligibility.md, local).
A job is hidden when any check says 'no'. 'check' jobs are shown with a tag (the page can hide them).
"""
import re

GRAD = (2028, 5)  # the site owner's expected graduation (year, month): MS, May 2028
RULES_VERSION = 9  # bump when the rules here or in tech.py change: cached JD verdicts are then redone

TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    t = TAG.sub(" ", text or "")
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("’", "'").replace("‘", "'")
    return WS.sub(" ", t)


def _find(rx, text, n=90):
    m = rx.search(text)
    if not m:
        return None
    a, b = max(0, m.start() - 20), min(len(text), m.end() + 40)
    return text[a:b].strip()[:n]


# ---- work authorization ----------------------------------------------------------------------------------
CITIZEN = re.compile(
    r"(must|required to|need to|should|you must) (be|have) (a )?u\.?s\.? citizen|"
    r"u\.?s\.? citizenship (is |will be )?(required|is a requirement|needed)|requires? (u\.?s\.? )?citizenship|"
    r"only (open to )?u\.?s\.? citizens|united states citizenship (is )?required|"
    r"citizenship requirement|"
    r"(must|required to) be a u\.?s\.? person|"
    r"u\.?s\.? citizen,? (or )?(national,? )?(a )?lawful permanent resident|"
    r"u\.?s\.? citizens? or (lawful )?permanent residents?|green card holders? (only|required)|"
    r"(active|current|interim|existing) (dod |government |security )?(secret|top secret|ts/sci|ts|public trust)[^.]{0,30}clearance|"
    r"(secret|top secret|ts/sci) (security )?clearance (is )?(required|needed)|"
    r"(ability|able|eligib\w*|willing\w*) to (obtain|get|hold|acquire)[^.]{0,70}clearance|"
    r"security clearance\s*[:\-]\s*(ability|must|required|active|secret|top secret|ts)|"
    r"(security|government|secret) clearance (is )?required|requires? (a|an) (active )?(security|secret) clearance|"
    r"itar[^.]{0,120}u\.?s\.? (person|citizen)|export control[^.]{0,80}u\.?s\.? person|"
    r"obtain and maintain (an? )?(\w+ ){0,3}clearance|"
    r"must (either )?be (\(\w\) )?an? (\(\w+\) )?u\.?s\.? (person|citizen)|u\.?s\.? person status|\bperson status required|"
    r"(^|[.:;]\s*)citizenship (is )?required|proof of u\.?s\.? citizenship|restricted to u\.?s\.? citizens|"
    r"(available|open) (only )?to citizens of|\b(eea|eu|european economic area|european union) citizens?(hip)?\b", re.I)
F1_REJECT = re.compile(
    r"(f-?1|\bcpt\b|\bopt\b|j-?1|student visa)[^.]{0,200}(not eligible|ineligible|will not be considered|cannot be considered|"
    r"not able to|unable to|do(es)? not (accept|support|qualify|meet)|don'?t meet|not meet|not accepted|not be accepted|"
    r"excluded|not considered|disqualif)|"
    r"permanent(ly)? (authori[sz]ed|work authori[sz]ation|right to work)|"
    r"(not eligible|ineligible|cannot|will not|do not|does not|unable|no )[^.]{0,220}(f-?1|\bcpt\b|\bopt\b|student visa|"
    r"curricular practical training|optional practical training)|"
    r"(work|employment|authori[sz]ed to work)[^.]{0,40}(permanently|on a permanent basis)|"
    r"sponsorship \(for example,? [^)]*(f-?1|opt|cpt)", re.I)
CPT_OK = re.compile(
    r"(including|incl\.|such as|welcome|accept\w*|support\w*|eligible|consider\w*)[^.]{0,60}(\bcpt\b|curricular practical training|f-?1 students?)|"
    r"(\bcpt\b|curricular practical training)[^.]{0,60}(welcome|accepted|eligible|supported|is fine|ok)|"
    r"(international students?|student visa holders?) (are )?(welcome|encouraged|eligible)", re.I)
NO_SPONSOR = re.compile(
    r"without (the )?(need for |needing |requiring )?[^.]{0,40}sponsorship(?! for an? export)|"
    r"(will not|won't|cannot|can't|unable to|do not|does not|not able to) (provide|offer|support)?[^.]{0,60}sponsor(?!ed benefit)|"
    r"sponsorship\s*[:\-]\s*(not available|none|no\b|n/a)|"
    r"not (be )?eligible for (visa |immigration |work authorization |employment )?sponsorship|"
    r"(no|not offer(ing)?) (visa |immigration )?sponsorship|"
    r"(now or in the future|currently or in the future)[^.]{0,40}sponsor|sponsor[^.]{0,100}\bin the future\b|"
    r"authori[sz]ed to work (in the (u\.?s\.?|united states) )?for any employer", re.I)
SPONSOR_YES = re.compile(r"(we|will|do|does|can) (sponsor|offer (visa )?sponsorship|provide (visa )?sponsorship)(?! benefits)", re.I)

# ---- degree ------------------------------------------------------------------------------------------------
MS_OK = re.compile(
    r"\bgrad students?\b|\bmaster'?s\b|\bm\.?s\.? (student|degree|candidate|program|in)\b|\bmsc\b|\bmeng\b|\bm\.s\.|"
    r"(?<!under)\bgraduate[- ](level |degree |school )?(student|degree|program|candidate)s?|graduate[- ]level|"
    r"\bms\b(?=\s*(,|/|\)|or\b|and\b|degree|in\b|program|student|candidate))|\b(bs|b\.s\.)\s*(,|/|or)\s*(ms|m\.s\.)|"
    r"(bachelor'?s?|b\.?s\.?|undergraduate)( degree)?,? or (higher|above)|at least (a|an) (bachelor|undergraduate)|"
    r"(undergraduate|bachelor'?s?)( degree)?,? (or|and/or|/) (graduate|master)|\bbs/ms\b|b\.?s\.?/m\.?s\.?|"
    r"all (education|degree) levels|undergrad(uate)? (or|and) grad|advanced degree|any degree level", re.I)
UG_ONLY = re.compile(
    r"(enrolled|enrollment) in (a|an)( accredited)? (4-year |four-year )?(undergraduate|bachelor'?s?) (degree )?program|"
    r"(pursuing|working toward[s]?|completing|enrolled in) (a|an|your) (undergraduate|bachelor'?s?|b\.?s\.?|ba/bs|4-year) (degree|program)|"
    r"(pursuing|working toward[s]?) (a |an )?(bachelor'?s?|b\.?s\.?|b\.?a\.?)(?![\w.]*\s*(or|/|and)\b)(?!/)|"
    r"\brising (sophomore|junior|senior)s?|\b(sophomore|junior|senior)s? (or|and) (junior|senior)s?\b|"
    r"(entering|completed) (your )?(sophomore|junior|senior) year|junior (or|and) senior (standing|year)|"
    r"undergraduate students? only|only (open to )?undergraduate|"
    r"in pursuit of an? (bachelor|undergraduate)|(current )?enrollment in an? (four-year |4-year )?(undergraduate|bachelor)|"
    r"pursuing an? undergraduate'?s? degree|\ban undergraduate student\b|undergraduate students (currently )?enrolled|"
    r"\b(freshman|sophomore)s?( or (sophomore|junior))?( year)?\b|\bjuniors?,? (or |and )?seniors?\b|"
    r"enrolled in an? bachelor of (science|arts|engineering)|"
    r"\bundergraduate students? (pursuing|majoring|enrolled|studying|in)\b|bachelor'?s degree (in process|in progress)", re.I)
# A requirement line ("must be pursuing a Bachelor's", "enrolled in a PhD program") beats a softer mention elsewhere
# (an intro that says "undergraduate and graduate programs", a pay table that lists Masters).
UG_MUST = re.compile(r"(must|should|required to) (be )?(currently )?(pursuing|enrolled in|working toward\w*) (a |an )?"
                     r"(bachelor|undergraduate|b\.?s\.?\b)", re.I)
PHD_MUST = re.compile(r"(currently )?enrolled in a ph\.?d\.? program|\bcurrent ph\.?d\.? students|pursuing a doctorate|"
                      r"ph\.?d\.? students? only|must be (a |currently )?(pursuing a |enrolled in a )?ph\.?d", re.I)


GRADWORD = re.compile(r"master|graduate student|\bms\b|m\.s\.|grad student|or higher|or above", re.I)
UG_PREF = re.compile(r"(undergraduate|bachelor'?s?)[^.]{0,40}preferred|prefer\w* (an? )?(undergraduate|bachelor)", re.I)
PHD_ONLY = re.compile(r"(currently )?(enrolled in|pursuing|candidate in|working towards) (a )?ph\.?d\.?(?! or)(?!/)(?!,)|ph\.?d\.? (students?|candidates?) only", re.I)

# ---- graduation date -----------------------------------------------------------------------------------------
MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}
SEASONS = {"spring": 5, "summer": 8, "fall": 12, "autumn": 12, "winter": 12}
DATE = re.compile(r"\b(spring|summer|fall|autumn|winter|jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|nov\w*|dec\w*)?"
                  r"[\s,/-]*((?:20)\d\d)\b", re.I)
GRAD_WORD = re.compile(r"\bgraduat\w*", re.I)


def _grad_snippets(text: str) -> list[str]:
    """The words right around each 'graduat…' (same sentence, at most ~60 chars before / 140 after).

    Not the whole 'sentence': page text without punctuation (menus, other job titles) would otherwise
    hand us years that have nothing to do with graduating."""
    out = []
    for m in GRAD_WORD.finditer(text):
        left = text[max(0, m.start() - 60):m.start()]
        right = text[m.start():m.end() + 140]
        cut = max(left.rfind("."), left.rfind(";"), left.rfind(":"))
        left = left[cut + 1:] if cut >= 0 else left
        end = re.search(r"(?<!\b[A-Z])[.;](\s|$)", right[m.end() - m.start():])
        if end:
            right = right[:m.end() - m.start() + end.start()]
        out.append((left + right).strip())
    return out


def _dates(s: str) -> list[tuple[int, int]]:
    out = []
    for word, yr in DATE.findall(s):
        w = (word or "").lower()
        month = SEASONS.get(w) or MONTHS.get(w[:3]) or 6
        out.append((int(yr), month))
    return out


def _months(d):
    return d[0] * 12 + d[1]


def grad_check(text: str) -> tuple[str | None, str | None]:
    """('ok'|'no'|None, phrase) from the words around each 'graduat…' that come with a year.

    JDs often repeat the line (once with "or later", once without), so one snippet that fits is enough;
    the job is 'no' only when every dated snippet leaves the owner's graduation out."""
    g = _months(GRAD)
    oks, nos = [], []
    for s in _grad_snippets(text):
        if re.search(r"after graduation|upon graduation|post[- ]graduation|full[- ]time", s, re.I):
            continue
        ds = _dates(s)
        if not ds:
            continue
        ms = [_months(d) for d in ds]
        low = s.lower()
        if "between" in low or re.search(r"\bfrom\b.*\b(to|through|and)\b", low):
            ok = min(ms) - 3 <= g <= max(ms) + 3
        elif re.search(r"no earlier than|or later|after|at least", low):
            ok = g >= min(ms) - 3
        elif re.search(r"\bby\b|no later than|or earlier|before", low):
            ok = g <= max(ms) + 3
        else:  # a list of dates, e.g. "December 2028 or Spring 2029"
            ok = any(abs(g - m) <= 3 for m in ms)
        (oks if ok else nos).append(s.strip()[:110])
    if oks:
        return "ok", oks[0]
    if nos:
        return "no", nos[0]
    return None, None


# ---- the standard checks ---------------------------------------------------------------------------------
# Every reader (these rules, the AI reader, the hand-labeled gold set) answers the same five questions with the
# same values, so they can be compared and combined:
LABELS = {
    "citizenship": ("required", "not_required"),               # C3 citizenship / green card / clearance / ITAR
    "cpt": ("rejected", "welcome", "silent"),                  # C4 F-1 / CPT / OPT students
    "sponsorship": ("no_sponsorship", "sponsors", "silent"),   # C5 visa sponsorship
    "degree": ("ms_ok", "ug_only", "phd_only", "ug_preferred", "unclear"),  # C6 degree level
    "grad": ("fits", "misses", "not_stated"),                  # C7 graduation date vs GRAD
}
NEUTRAL = {"citizenship": "not_required", "cpt": "silent", "sponsorship": "silent", "degree": "unclear",
           "grad": "not_stated"}


def _quote(rx, text: str, limit: int = 220) -> str | None:
    """The sentence around the first match (the evidence shown on the page)."""
    m = rx.search(text)
    if not m:
        return None
    a = max(text.rfind(". ", 0, m.start()), text.rfind("! ", 0, m.start()), text.rfind("? ", 0, m.start()))
    a = a + 2 if a >= 0 else 0
    b = text.find(". ", m.end())
    b = b + 1 if b >= 0 else len(text)
    q = text[a:b].strip()
    if len(q) > limit:
        q = text[max(0, m.start() - 60):m.end() + 120].strip()
    return q


def labels(jd: str) -> tuple[dict, dict]:
    """The rules' answers to the five checks, with the JD sentence behind each non-neutral answer."""
    t = _clean(jd)
    lab, quo = dict(NEUTRAL), {}

    if (q := _quote(CITIZEN, t)):
        lab["citizenship"], quo["citizenship"] = "required", q
    if (q := _quote(F1_REJECT, t)):
        lab["cpt"], quo["cpt"] = "rejected", q
    elif (q := _quote(CPT_OK, t)):
        lab["cpt"], quo["cpt"] = "welcome", q
    if (q := _quote(NO_SPONSOR, t)) and not SPONSOR_YES.search(t):
        lab["sponsorship"], quo["sponsorship"] = "no_sponsorship", q
    elif (q := _quote(SPONSOR_YES, t)):
        lab["sponsorship"], quo["sponsorship"] = "sponsors", q

    ug_must, phd_must = _quote(UG_MUST, t), _quote(PHD_MUST, t)
    if ug_must and not GRADWORD.search(ug_must):
        lab["degree"], quo["degree"] = "ug_only", ug_must
    elif phd_must and not GRADWORD.search(phd_must):
        lab["degree"], quo["degree"] = "phd_only", phd_must
    elif (q := _quote(MS_OK, t)):
        lab["degree"], quo["degree"] = "ms_ok", q
    elif (q := _quote(UG_ONLY, t)):
        lab["degree"], quo["degree"] = "ug_only", q
    elif (q := _quote(PHD_ONLY, t)):
        lab["degree"], quo["degree"] = "phd_only", q
    elif (q := _quote(UG_PREF, t)):
        lab["degree"], quo["degree"] = "ug_preferred", q

    g, q = grad_check(t)
    if g == "ok":
        lab["grad"], quo["grad"] = "fits", q
    elif g == "no":
        lab["grad"], quo["grad"] = "misses", q
    return lab, quo


def from_labels(lab: dict, quo: dict, source: str = "rules") -> dict:
    """Turn the five answers into the verdict the page and the filter use."""
    # Dheepak (2026-11-14): only citizenship-type rules and an explicit CPT/OPT rejection block him. "No sponsorship"
    # is about future visas (H-1B); for an internship, work authorization through CPT/OPT is enough -> passes.
    if lab["citizenship"] == "required" or lab["cpt"] == "rejected":
        auth = "no"
    elif lab["cpt"] == "welcome":
        auth = "cpt"
    else:
        auth = "ok"
    degree = {"ms_ok": "ok", "ug_only": "no", "phd_only": "no", "ug_preferred": "check"}.get(lab["degree"], "unknown")
    grad = {"fits": "ok", "misses": "no"}.get(lab["grad"])
    notes = [quo[k] for k in ("citizenship", "cpt", "degree", "grad", "sponsorship")
             if k in quo and lab[k] in ("required", "rejected", "ug_only", "phd_only", "misses", "no_sponsorship", "ug_preferred")]
    return {"auth": auth, "degree": degree, "grad": grad, "notes": notes[:3],
            "labels": lab, "quotes": quo, "source": source}


def check(jd: str | None) -> dict:
    """Eligibility of one job from its JD (the rules' reading)."""
    if jd is None:
        return {"auth": "unknown", "degree": "unknown", "grad": None, "notes": [], "labels": None, "quotes": {},
                "source": "none"}
    return from_labels(*labels(jd))


# ---- excerpt: the eligibility sentences of a JD (what the AI reader and the gold set read) ------------------
EXCERPT_KEY = re.compile(
    r"citizen|clearance|secret|itar|export|u\.s\. person|green card|permanent|sponsor|visa|\bcpt\b|\bopt\b|f-1|"
    r"authori[sz]|eligib|bachelor|master|undergrad|graduate|enrolled|enrollment|pursuing|ph\.?d|degree|graduat|"
    r"class of|rising|junior|senior|sophomore|freshman|return(ing)? to school|semester|student|b\.s\.|m\.s\.", re.I)


def excerpt(jd: str | None, limit: int = 3500) -> str | None:
    if jd is None:
        return None
    t = _clean(jd)
    out, seen = [], set()
    for s in re.split(r"(?<=[.!?])\s+(?=[A-Z•\-])|\s[•·]\s", t):
        s = s.strip()
        if EXCERPT_KEY.search(s) and 12 < len(s) < 600 and s not in seen:
            seen.add(s)
            out.append(s)
    return " ".join(out)[:limit]


def from_list(sponsorship: str | None, degrees: list | None) -> dict:
    """What the SimplifyJobs list says (no JD). Citizenship -> no; degrees are only a hint."""
    auth = "unknown"
    if sponsorship == "U.S. Citizenship is Required":
        auth = "no"
    elif sponsorship == "Does Not Offer Sponsorship":
        auth = "check"
    elif sponsorship == "Offers Sponsorship":
        auth = "ok"
    degree = "unknown"
    if degrees:
        d = set(degrees)
        degree = "ok" if d & {"Master's"} else "check"
    notes = [f"list: {sponsorship}"] if auth in ("no", "check") else []
    if degree == "check":
        notes.append("list degrees: " + ", ".join(degrees))
    return {"auth": auth, "degree": degree, "grad": None, "notes": notes}


RANK = {"no": 4, "check": 3, "ok": 2, "cpt": 1, "unknown": 0, None: -1}


def combine(a: dict | None, b: dict | None) -> dict | None:
    """Merge two verdicts for the same job: a JD verdict beats 'unknown'; 'no' beats everything."""
    if not a or not b:
        return a or b
    out = {}
    for k in ("auth", "degree", "grad"):
        x, y = a.get(k), b.get(k)
        if "no" in (x, y):
            out[k] = "no"
        elif x in (None, "unknown"):
            out[k] = y
        elif y in (None, "unknown"):
            out[k] = x
        else:
            out[k] = x if RANK[x] >= RANK[y] else y
    out["notes"] = (a.get("notes") or []) + [n for n in (b.get("notes") or []) if n not in (a.get("notes") or [])]
    return out


def with_list(jd: dict | None, lst: dict | None) -> dict:
    """JD verdict first; the list's verdict only fills gaps, except a list 'no' (citizenship) always counts."""
    if not jd:
        return lst or check(None)
    if not lst:
        return jd
    out = dict(jd)
    for k in ("auth", "degree", "grad"):
        if lst.get(k) == "no" or out.get(k) in (None, "unknown"):
            out[k] = lst.get(k) if lst.get(k) not in (None, "unknown") else out.get(k)
    out["notes"] = (jd.get("notes") or []) + [n for n in (lst.get("notes") or []) if out.get("auth") == "no" or "list" not in n][:1]
    return out


def hidden(e: dict | None) -> bool:
    return bool(e) and "no" in (e.get("auth"), e.get("degree"), e.get("grad"))


def needs_check(e: dict | None) -> bool:
    return bool(e) and ("check" in (e.get("auth"), e.get("degree")) or bool(e.get("disagree")))
