"""Poll company job boards directly through their public ATS APIs.

Boards are discovered from the apply URLs in the intern lists, so we only poll companies that hire interns.
Each fetcher returns jobs in the same shape as radar.sources, plus `countries` (ISO codes / names when the ATS gives them).
Only co-op / intern titles in the US are kept.
"""
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import classify
from .sources import UA

ATS_NAMES = {"gh": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "sr": "SmartRecruiters",
             "wd": "Workday", "oracle": "Oracle Cloud"}
WORKERS = {"gh": 8, "lever": 6, "ashby": 6, "sr": 6, "wd": 10, "oracle": 6}  # a few requests at a time per ATS
# Workday and Oracle boards live on each company's own host, so more parallel boards still means few requests per host.

BOARD_PATTERNS = [
    ("gh", re.compile(r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_(?:app|board)\?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("gh", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"(?<!eu\.)jobs\.lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)", re.I)),
    ("sr", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    # token = "host|tenant|site"
    ("wd", re.compile(r"//(([\w-]+)\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)", re.I)),
    ("wd", re.compile(r"//(wd\d+\.myworkdaysite\.com)/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([\w-]+)/([\w-]+)", re.I)),
    # token = "host|siteNumber"
    ("oracle", re.compile(r"//([\w.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[\w-]+/sites/([\w-]+)", re.I)),
]
NOT_A_BOARD = {"embed", "jobs", "v1", "api", "job_app", "job_board", "oneclick-ui"}


def discover(rows: list[tuple[str, str]]) -> dict[str, dict]:
    """[(company, url)] -> {"gh:stripe": {"ats": "gh", "token": "stripe", "company": "Stripe"}}"""
    boards = {}
    for company, url in rows:
        for ats, rx in BOARD_PATTERNS:
            m = rx.search(url or "")
            if not m:
                continue
            if ats == "wd":
                host, tenant, site = m.groups()
                if site.lower() in ("job", "wday", "details", "login"):
                    break
                token = f"{host.lower()}|{tenant.lower()}|{site}"
            elif ats == "oracle":
                token = f"{m.group(1).lower()}|{m.group(2)}"
            else:
                token = urllib.parse.unquote(m.group(1)).strip()
            if token.lower() in NOT_A_BOARD:
                break
            bid = f"{ats}:{token.lower()}"
            boards.setdefault(bid, {"ats": ats, "token": token, "company": company})
            break
    return boards


class Gone(Exception):
    """The board does not exist (404): count it and stop polling it after a few misses."""


def get_json(url: str, timeout: int = 25, tries: int = 3, body: dict | None = None):
    for i in range(tries):
        headers = {"User-Agent": UA, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise Gone(url) from None
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise


def iso(s) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _job(ats, company, title, url, locations, countries, posted, extra_intern=False):
    title = (title or "").strip()
    if not title or not (classify.is_early_career(title, from_intern_feed=False) or extra_intern):
        return None
    if classify.FOREIGN_TITLE.search(title):
        return None
    locations = [l for l in locations if l]
    if not classify.is_us(locations, countries):
        return None
    return {
        "source": ATS_NAMES[ats], "company": company, "title": title, "url": url,
        "locations": locations, "countries": countries, "terms": classify.terms_from_title(title),
        "posted": posted, "field": None, "sponsorship": None,
    }


def fetch_greenhouse(token, company):
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(token)}/jobs")
    out = []
    for j in d.get("jobs", []):
        loc = (j.get("location") or {}).get("name") or ""
        out.append(_job("gh", j.get("company_name") or company, j.get("title"), j.get("absolute_url"),
                        [p.strip() for p in re.split(r";|\|", loc)], None,
                        iso(j.get("first_published")) or iso(j.get("updated_at"))))
    return out


def fetch_lever(token, company):
    d = get_json(f"https://api.lever.co/v0/postings/{urllib.parse.quote(token)}?mode=json")
    out = []
    for j in d if isinstance(d, list) else []:
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location")]
        intern = bool(re.search(r"intern|co-?op", cat.get("commitment") or "", re.I))
        created = j.get("createdAt")
        out.append(_job("lever", company, j.get("text"), j.get("hostedUrl"), locs,
                        [j["country"]] if j.get("country") else None,
                        int(created / 1000) if created else None, intern))
    return out


def fetch_ashby(token, company):
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(token)}")
    out = []
    for j in d.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or []]
        countries = [((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry")]
        countries += [((s.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
                      for s in j.get("secondaryLocations") or []]
        intern = (j.get("employmentType") or "").lower() == "intern"
        out.append(_job("ashby", company, j.get("title"), j.get("jobUrl"), locs,
                        [c for c in countries if c] or None, iso(j.get("publishedAt")), intern))
    return out


def fetch_smartrecruiters(token, company):
    out, seen = [], set()
    for q in ("intern", "co-op"):
        d = get_json(f"https://api.smartrecruiters.com/v1/companies/{urllib.parse.quote(token)}/postings"
                     f"?limit=100&q={urllib.parse.quote(q)}")
        for j in d.get("content", []):
            if j.get("id") in seen:
                continue
            seen.add(j.get("id"))
            loc = j.get("location") or {}
            place = ", ".join(x for x in (loc.get("city"), (loc.get("region") or "")) if x)
            if loc.get("remote"):
                place = (place + " (Remote)").strip()
            out.append(_job("sr", (j.get("company") or {}).get("name") or company, j.get("name"), f"https://jobs.smartrecruiters.com/{token}/{j.get('id')}",
                            [place], [loc.get("country")] if loc.get("country") else None, iso(j.get("releasedDate"))))
    return out


# ---- Workday --------------------------------------------------------------
# POST {host}/wday/cxs/{tenant}/{site}/jobs  {"appliedFacets":{}, "limit":20, "offset":N, "searchText":""}
# - limit is at most 20, and the total is capped at 2,000, so we narrow first:
#   1. read the board's facets and pick values like "Intern", "Co-op", "Intern / Co-Op / Student"
#      (prefer the Job Type facet `workerSubType`); every job with that value counts as intern/co-op;
#   2. no such facet -> text search "intern" and "co-op", then keep intern/co-op titles only.
# - postedOn is text ("Posted Today", "Posted 3 Days Ago", "Posted 30+ Days Ago") or missing.
WD_INTERN_VALUE = re.compile(r"\bintern|co-?op|\bcoop\b|trainee|apprentice", re.I)
WD_US_VALUE = re.compile(r"^\s*(united states( of america)?|usa|us|u\.s\.a?\.?)\s*$", re.I)
WD_NOT = re.compile(r"graduate|new grad|full[- ]time|regular|employee|faculty|staff", re.I)
WD_PAGE, WD_MAX_PAGES = 20, 15


def workday_posted(text: str | None, now: int) -> int | None:
    if not text:
        return None
    t = text.lower()
    # Workday only gives whole days: "Today" = now (later polls date it by first_seen anyway),
    # "Yesterday" / "N Days Ago" = middle of that day, so a 1-day-old job doesn't show in the 24 h view.
    if "today" in t:
        return now
    if "yesterday" in t:
        return now - 36 * 3600
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return now - int((int(m.group(1)) + (1 if "+" in t else 0.5)) * 86400)
    return None


def _wd_facet_groups(facets: list[dict]):
    """Yield every (facetParameter, descriptor, values) incl. the ones nested under locationMainGroup."""
    for f in facets or []:
        vals = f.get("values") or []
        if vals and "facetParameter" in vals[0]:
            for sub in vals:
                yield sub.get("facetParameter"), sub.get("descriptor") or "", sub.get("values") or []
        else:
            yield f.get("facetParameter"), f.get("descriptor") or "", vals


def _wd_us_facet(facets: list[dict]) -> tuple[bool, tuple[str, str] | None]:
    """(board has a country facet, (facetParameter, id of the US value) or None)."""
    has_country, best = False, None
    for param, desc, vals in _wd_facet_groups(facets):
        countryish = "country" in (param or "").lower() or "country" in desc.lower()
        has_country = has_country or countryish
        us = [v for v in vals if "id" in v and WD_US_VALUE.match(v.get("descriptor") or "")]
        if us and (countryish or best is None):
            best = (param, us[0]["id"])
            if countryish:
                break
    return has_country or best is not None, best


def _wd_intern_facet(facets: list[dict]) -> tuple[str, list[str], bool] | None:
    """Find (facetParameter, [value ids], trusted). trusted = Job Type facet: titles need no check."""
    best = None
    for f in facets or []:
        vals = f.get("values") or []
        if not vals or "id" not in vals[0]:
            continue
        ids = [v["id"] for v in vals if WD_INTERN_VALUE.search(v.get("descriptor") or "")
               and not WD_NOT.search(v.get("descriptor") or "")]
        if not ids:
            continue
        trusted = f.get("facetParameter") == "workerSubType"
        cand = (f["facetParameter"], ids, trusted)
        if trusted:
            return cand
        best = best or cand
    return best


def fetch_workday(token, company, now=None):
    now = now or int(time.time())
    host, tenant, site = token.split("|")
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    base = f"https://{host}/{site}" if "myworkdayjobs" in host else f"https://{host}/recruiting/{tenant}/{site}"
    first = get_json(api, body={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    has_country, us = _wd_us_facet(first.get("facets"))
    if has_country and not us:
        return []  # the board lists countries and the US isn't one of them
    us_facet = {us[0]: [us[1]]} if us else {}
    facet = _wd_intern_facet(first.get("facets"))
    searches = ([({facet[0]: facet[1], **us_facet}, "", facet[2])] if facet
                else [(dict(us_facet), "intern", False), (dict(us_facet), "co-op", False)])
    out, seen = [], set()
    for applied, text, trusted in searches:
        for page in range(WD_MAX_PAGES):
            d = get_json(api, body={"appliedFacets": applied, "limit": WD_PAGE, "offset": page * WD_PAGE, "searchText": text})
            posts = d.get("jobPostings") or []
            for p in posts:
                path = p.get("externalPath")
                if not path or path in seen:
                    continue
                seen.add(path)
                locs = [p.get("locationsText") or ""]
                if re.match(r"^\d+ locations?$", locs[0].strip(), re.I):
                    locs = []
                if not us and not any(classify.loc_country(l) == "US" for l in locs):
                    continue  # no US facet to lean on: the place itself must say US
                out.append(_job("wd", company, p.get("title"), base + path, locs, ["US"] if us else None,
                                workday_posted(p.get("postedOn"), now), extra_intern=trusted))
            if len(posts) < WD_PAGE or (page + 1) * WD_PAGE >= (d.get("total") or 0):
                break
    return out


# ---- Oracle Cloud HCM -----------------------------------------------------
# GET {host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=...&finder=findReqs;
#     siteNumber=..,keyword=..,limit=25,offset=N,sortBy=POSTING_DATES_DESC   (paging lives inside `finder`)
ORACLE_PAGE, ORACLE_MAX_PAGES, ORACLE_DAYS = 25, 8, 10


def fetch_oracle(token, company, now=None):
    now = now or int(time.time())
    host, site = token.split("|")
    out, seen = [], set()
    for kw in ("intern", "co-op"):
        for page in range(ORACLE_MAX_PAGES):
            finder = (f"findReqs;siteNumber={site},keyword={kw},limit={ORACLE_PAGE},"
                      f"offset={page * ORACLE_PAGE},sortBy=POSTING_DATES_DESC")
            url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
                   f"&expand=requisitionList.secondaryLocations&finder={urllib.parse.quote(finder, safe=';,=')}")
            items = get_json(url).get("items") or [{}]
            reqs = items[0].get("requisitionList") or []
            oldest = now
            for r in reqs:
                posted = iso(r.get("PostedDate"))
                oldest = min(oldest, posted or now)
                if r.get("Id") in seen:
                    continue
                seen.add(r.get("Id"))
                locs = [r.get("PrimaryLocation")] + [x.get("Name") for x in r.get("secondaryLocations") or []]
                countries = [r.get("PrimaryLocationCountry")] + [x.get("CountryCode") for x in r.get("secondaryLocations") or []]
                out.append(_job("oracle", company, r.get("Title"),
                                f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r.get('Id')}",
                                locs, [c for c in countries if c] or None, posted))
            if len(reqs) < ORACLE_PAGE or oldest < now - ORACLE_DAYS * 86400:
                break  # newest first: once we're past the window, stop paging
    return out


FETCHERS = {"gh": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby, "sr": fetch_smartrecruiters,
            "wd": fetch_workday, "oracle": fetch_oracle}


def poll(boards: dict[str, dict], max_fails: int = 3) -> tuple[list[dict], dict]:
    """Poll every live board. Updates each board's `fails` counter in place. Returns (jobs, health)."""
    jobs, health = [], {}
    for ats, fetch in FETCHERS.items():
        todo = [(bid, b) for bid, b in boards.items() if b["ats"] == ats and b.get("fails", 0) < max_fails]
        h = {"ok": True, "boards": len(todo), "failed": 0, "rows": 0}
        t0 = time.time()

        def one(item):
            bid, b = item
            try:
                return bid, [j for j in fetch(b["token"], b["company"]) if j], None
            except Gone:
                return bid, [], "gone"
            except Exception as e:  # network trouble: try again next poll, don't count as gone
                return bid, [], f"{type(e).__name__}"

        with ThreadPoolExecutor(max_workers=WORKERS[ats]) as pool:
            for bid, got, err in pool.map(one, todo):
                if err == "gone":
                    boards[bid]["fails"] = boards[bid].get("fails", 0) + 1
                if err:
                    h["failed"] += 1
                else:
                    boards[bid]["fails"] = 0
                jobs.extend(got)
                h["rows"] += len(got)
        h["seconds"] = round(time.time() - t0, 1)
        h["ok"] = h["boards"] == 0 or h["failed"] < h["boards"]
        health[ATS_NAMES[ats]] = h
        print(f"[ats] {ATS_NAMES[ats]}: {h}", file=sys.stderr)
    return jobs, health
