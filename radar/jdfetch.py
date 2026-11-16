"""Get the job description for a job we only know from a list (apply URL, no JD).

`fetch(url)` -> {"text": str, "posted": int|None, "country": str|None, "via": str} or None.
`posted` is the job's own date (Workday startDate / "Posted N Days Ago", JSON-LD datePosted, the page's "Posted N
Days Ago"): a list's date is only when the list added the job, so the build takes the earlier of the two.
Order: a known ATS API for that URL (Workday, Greenhouse, Lever, Ashby, Workable, BambooHR, iCIMS, UltiPro),
else the page's schema.org JSON-LD JobPosting (most career sites add it for Google Jobs), else the page's
visible text when it is long enough to be a job description.
"""
import html as htmllib
import json
import re
import urllib.error
import urllib.parse
import time
import urllib.request

from .ats import iso, workday_posted

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
MIN_WORDS = 150


def http(url: str, body: dict | None = None, timeout: int = 20) -> str:
    headers = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9, */*;q=0.5"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=timeout) as r:
        return r.read(4_000_000).decode("utf-8", "ignore")


def _strings(obj, out=None):
    """All long-ish strings inside a JSON value (descriptions live in different keys on every ATS)."""
    out = [] if out is None else out
    if isinstance(obj, dict):
        for v in obj.values():
            _strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _strings(v, out)
    elif isinstance(obj, str) and len(obj) > 40:
        out.append(obj)
    return out


def _text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", s)
    s = htmllib.unescape(re.sub(r"<[^>]+>", " ", s))
    return " ".join(s.split())


def _json_ld(page: str) -> dict | None:
    for block in re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for item in data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []:
            if isinstance(item, dict) and item.get("@type") == "JobPosting" and item.get("description"):
                loc = item.get("jobLocation")
                loc = loc[0] if isinstance(loc, list) and loc else loc
                country = (((loc or {}).get("address") or {}).get("addressCountry")) if isinstance(loc, dict) else None
                if isinstance(country, dict):
                    country = country.get("name")
                return {"text": _text(item["description"]), "posted": iso(item.get("datePosted")),
                        "country": country, "via": "json-ld"}
    return None


# JSON-LD / page dates come from page templates, and some are fake: IDEMIA's iCIMS page says datePosted = now - 2 years
# (to the second) for a live 2027 internship. An internship posted more than MAX_PAGE_AGE ago is unlikely, so such a
# date is treated as unknown (the list date then stands, as before). Structured ATS APIs (Workday...) are trusted.
MAX_PAGE_AGE = 180 * 86400


def _plausible(t: int | None) -> int | None:
    return t if t and t >= _NOW[0] - MAX_PAGE_AGE else None


POSTED_TEXT = re.compile(r"\bposted\s*:?\s*(today|yesterday|\d+\+?\s*days?\s+ago)", re.I)
_NOW = [0]   # the run's clock for relative dates ("Posted 3 Days Ago"), set by fetch()


def page_posted(text: str) -> int | None:
    m = POSTED_TEXT.search(text or "")
    return _plausible(workday_posted(m.group(1), _NOW[0])) if m else None


# ---- per-ATS adapters (url regex -> function(match) -> result) ------------------------------------------
def _workday(m, get):
    host, tenant, site, path = m.group("host"), m.group("tenant"), m.group("site"), m.group("path")
    info = json.loads(get(f"https://{host}/wday/cxs/{tenant}/{site}{path}")).get("jobPostingInfo") or {}
    dates = [d for d in (iso(info.get("startDate")), workday_posted(info.get("postedOn"), _NOW[0])) if d]
    return {"text": _text(info.get("jobDescription") or ""), "posted": min(dates) if dates else None,
            "country": (info.get("country") or {}).get("descriptor"), "via": "workday"}


def _greenhouse(m, get):
    d = json.loads(get(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}"))
    return {"text": _text(htmllib.unescape(d.get("content") or "")), "posted": iso(d.get("first_published")),
            "country": None, "via": "greenhouse"}


def _lever(m, get):
    d = json.loads(get(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}"))
    return {"text": _text(" ".join(_strings(d))), "posted": None, "country": d.get("country"), "via": "lever"}


def _ashby(m, get):
    d = json.loads(get(f"https://api.ashbyhq.com/posting-api/job-board/{m.group(1)}"))
    for j in d.get("jobs", []):
        if j.get("id") == m.group(2):
            return {"text": _text(j.get("descriptionPlain") or j.get("descriptionHtml") or ""),
                    "posted": iso(j.get("publishedAt")), "country": None, "via": "ashby"}
    return None


def _workable(m, get):
    d = json.loads(get(f"https://apply.workable.com/api/v2/accounts/{m.group(1)}/jobs/{m.group(2)}"))
    return {"text": _text(" ".join(_strings(d))), "posted": iso(d.get("published")),
            "country": ((d.get("location") or {}).get("countryCode")), "via": "workable"}


def _bamboohr(m, get):
    d = json.loads(get(f"https://{m.group(1)}.bamboohr.com/careers/{m.group(2)}/detail"))
    return {"text": _text(" ".join(_strings(d))), "posted": None, "country": None, "via": "bamboohr"}


def _icims(m, get):
    page = get(f"https://{m.group(1)}/jobs/{m.group(2)}/job?in_iframe=1")
    r = _json_ld(page)
    if r:
        r["via"], r["posted"] = "icims", _plausible(r["posted"])
        return r
    return {"text": _text(page), "posted": None, "country": None, "via": "icims"}


def _ultipro(m, get):
    page = get(m.group(0))
    d = re.search(r'"Description"\s*:\s*"((?:[^"\\]|\\.)*)"', page)
    if not d:
        return None
    # UKG gives the dates in the same JSON: ExternalPostedDate (public), else PostedDate (Milliman, 2026-11-15)
    dates = [iso(x) for k in ("ExternalPostedDate", "PostedDate")
             for x in re.findall(rf'"{k}"\s*:\s*"([^"]+)"', page)]
    dates = [x for x in dates if x]
    return {"text": _text(json.loads('"' + d.group(1) + '"')), "posted": min(dates) if dates else None,
            "country": None, "via": "ultipro"}


ADAPTERS = [
    (re.compile(r"https?://(?P<host>(?P<tenant>[\w-]+)\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[\w-]+)"
                r"(?P<path>/job/[^?#]+)"), _workday),
    (re.compile(r"https?://(?P<host>wd\d+\.myworkdaysite\.com)/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/(?P<tenant>[\w-]+)/"
                r"(?P<site>[\w-]+)(?P<path>/job/[^?#]+)"), _workday),
    (re.compile(r"(?:job-)?boards\.greenhouse\.io/([\w-]+)/jobs/(\d+)"), _greenhouse),
    (re.compile(r"(?<!eu\.)jobs\.lever\.co/([\w.-]+)/([0-9a-f-]{36})"), _lever),
    (re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})"), _ashby),
    (re.compile(r"apply\.workable\.com/([\w-]+)/j/([0-9A-F]+)"), _workable),
    (re.compile(r"https?://([\w-]+)\.bamboohr\.com/careers/(\d+)"), _bamboohr),
    (re.compile(r"https?://([\w.-]+\.icims\.com)/jobs/(\d+)"), _icims),
    (re.compile(r"https?://recruiting2?\.ultipro\.com/[^?#]+/opportunitydetail\?opportunityid=[\w-]+", re.I), _ultipro),
]


def fetch(url: str, get=None, now: int | None = None) -> dict | None:
    """Job description for an apply URL, or None if we can't get one."""
    get = get or http
    _NOW[0] = int(now or time.time())
    if not url or "go.vansh.nyc" in url:
        return None
    try:
        for rx, fn in ADAPTERS:
            m = rx.search(url)
            if m:
                r = fn(m, get)
                if r and len(r["text"].split()) >= 30:
                    return r
                break
        page = get(url)
        r = _json_ld(page)
        text = _text(page)
        if r and len(r["text"].split()) >= 30:
            dates = [d for d in (_plausible(r["posted"]), page_posted(text)) if d]
            r["posted"] = min(dates) if dates else None
            return r
        if len(text.split()) >= MIN_WORDS:
            return {"text": text, "posted": page_posted(text), "country": None, "via": "page"}
    except Exception:  # any broken page (cut-off download, odd JSON, ...) just means "no JD this time"
        return None
    return None
