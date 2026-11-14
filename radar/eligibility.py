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
RULES_VERSION = 5  # bump when the rules here or in tech.py change: cached JD verdicts are then redone

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
    r"(ability|able|eligib\w*) to (obtain|get)( and maintain)? (an? )?(dod |government |u\.?s\.? )?(security |secret |top secret )?clearance|"
    r"(security|government|secret) clearance (is )?required|requires? (a|an) (active )?(security|secret) clearance|"
    r"itar[^.]{0,120}u\.?s\.? (person|citizen)|export control[^.]{0,80}u\.?s\.? person", re.I)
F1_REJECT = re.compile(
    r"(f-?1|\bcpt\b|\bopt\b|j-?1|student visa)[^.]{0,160}(not eligible|ineligible|will not be considered|cannot be considered|"
    r"not able to|unable to|do(es)? not (accept|support|qualify|meet)|don'?t meet|not meet|not accepted|not be accepted|"
    r"excluded|not considered|disqualif)|"
    r"permanent(ly)? (authori[sz]ed|work authori[sz]ation|right to work)|"
    r"(not eligible|ineligible|cannot|will not|do not|unable)[^.]{0,80}(f-?1|cpt|\bopt\b|student visa)|"
    r"sponsorship \(for example,? [^)]*(f-?1|opt|cpt)", re.I)
CPT_OK = re.compile(
    r"(including|incl\.|such as|welcome|accept\w*|support\w*|eligible|consider\w*)[^.]{0,60}(\bcpt\b|curricular practical training|f-?1 students?)|"
    r"(\bcpt\b|curricular practical training)[^.]{0,60}(welcome|accepted|eligible|supported|is fine|ok)|"
    r"(international students?|student visa holders?) (are )?(welcome|encouraged|eligible)", re.I)
NO_SPONSOR = re.compile(
    r"without (the )?(need for |requiring )?(current or future |future |any )?(employer |visa |immigration |work visa )?sponsorship(?! for an? export)|"
    r"(will not|won't|cannot|can't|unable to|do not|does not|not able to) (provide |offer )?(visa |immigration |employment |work )?sponsor|"
    r"not (be )?eligible for (visa |immigration |work authorization |employment )?sponsorship|"
    r"(no|not offer(ing)?) (visa |immigration )?sponsorship|"
    r"(now or in the future|currently or in the future)[^.]{0,40}sponsor|sponsor[^.]{0,100}\bin the future\b|"
    r"authori[sz]ed to work (in the (u\.?s\.?|united states) )?for any employer", re.I)
SPONSOR_YES = re.compile(r"(we|will|do|does|can) (sponsor|offer (visa )?sponsorship|provide (visa )?sponsorship)(?! benefits)", re.I)

# ---- degree ------------------------------------------------------------------------------------------------
MS_OK = re.compile(
    r"\bmaster'?s\b|\bm\.?s\.? (student|degree|candidate|program|in)\b|\bmsc\b|\bmeng\b|\bm\.s\.|"
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
    r"undergraduate students? only|only (open to )?undergraduate", re.I)
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


def check(jd: str | None) -> dict:
    """Eligibility of one job from its JD. Returns {'auth','degree','grad','notes'}."""
    if jd is None:
        return {"auth": "unknown", "degree": "unknown", "grad": None, "notes": []}
    t = _clean(jd)
    notes = []

    # auth
    if (p := _find(CITIZEN, t)):
        auth = "no"; notes.append(p)
    elif (p := _find(F1_REJECT, t)):
        auth = "no"; notes.append(p)
    elif (p := _find(CPT_OK, t)):
        auth = "cpt"; notes.append(p)
    elif (p := _find(NO_SPONSOR, t)) and not SPONSOR_YES.search(t):
        auth = "check"; notes.append(p)
    else:
        auth = "ok"

    # degree
    ms = MS_OK.search(t)
    if ms:
        degree = "ok"
    elif (p := _find(UG_ONLY, t)):
        degree = "no"; notes.append(p)
    elif (p := _find(PHD_ONLY, t)):
        degree = "no"; notes.append(p)
    elif (p := _find(UG_PREF, t)):
        degree = "check"; notes.append(p)
    else:
        degree = "unknown"

    grad, p = grad_check(t)
    if p and grad == "no":
        notes.append(p)
    return {"auth": auth, "degree": degree, "grad": grad, "notes": notes[:3]}


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
    return bool(e) and "check" in (e.get("auth"), e.get("degree"))
