"""Poll company job boards directly through their public ATS APIs.

Boards are discovered from the apply URLs in the intern lists, so we only poll companies that hire interns.
Each fetcher returns jobs in the same shape as radar.sources, plus `countries` (ISO codes / names when the ATS gives them).
Only co-op / intern titles in the US are kept.
"""
import html
import json
import re
import sys
import threading
import time
import zlib
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import classify, eligibility, tech
from .sources import UA

ATS_NAMES = {"gh": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "sr": "SmartRecruiters",
             "wd": "Workday", "oracle": "Oracle Cloud", "amazon": "Amazon", "google": "Google", "apple": "Apple"}
WORKERS = {"gh": 8, "lever": 6, "ashby": 6, "sr": 6, "wd": 10, "oracle": 6,
           "amazon": 1, "google": 1, "apple": 1}  # a few requests at a time per ATS
# Big employers with their own career sites (not an ATS the lists' URLs reveal): always watched.
FIXED_BOARDS = {
    "amazon:us": {"ats": "amazon", "token": "us", "company": "Amazon"},
    "google:us": {"ats": "google", "token": "us", "company": "Google"},
    "apple:us": {"ats": "apple", "token": "us", "company": "Apple"},
}

# ---- How often each board is polled (idea from github.com/hemnaath04/job-os: per-board next_check_after) ----
# Boards that answer ETag / 304 (Greenhouse, Lever, Ashby) are checked every run: an unchanged board costs
# one empty 304 reply. The others are checked every run while "hot" (a recent intern job), else every ~3 h.
ETAG_ATS = {"gh", "lever", "ashby"}
HOT_EVERY = 20 * 60          # a little under the 30-min run interval, so a hot board is due every run
COLD_EVERY = 170 * 60
HOT_DAYS = 14                # a board with an intern job posted in the last 14 days is hot
ETAG_MAX_AGE = 24 * 3600     # re-read a full board at least daily (and when the parser changes), 304 or not
PARSE_VERSION = 1            # bump when a fetcher's output changes: cached board results are then re-read
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


class NotModified(Exception):
    """304: the board is unchanged since our last read (conditional GET with the ETag we kept)."""


# The ETag of the board being polled in this thread: poll() sets it, the board's main request uses it.
_CTX = threading.local()


def get_json(url: str, timeout: int = 25, tries: int = 3, body: dict | None = None, conditional: bool = False):
    for i in range(tries):
        headers = {"User-Agent": UA, "Accept": "application/json"}
        if conditional and getattr(_CTX, "etag", None):
            headers["If-None-Match"] = _CTX.etag
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if conditional:
                    _CTX.new_etag = r.headers.get("ETag")
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 304 and conditional:
                raise NotModified(url) from None
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


def _job(ats, company, title, url, locations, countries, posted, extra_intern=False, jd=None, detail=None):
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
        "jd_terms": tech.jd_terms(jd), "elig": eligibility.check(jd) if jd is not None else None,
        "excerpt": eligibility.excerpt(jd),
        "_detail": detail,
    }


# ---- Job descriptions for titles that don't say "tech" ---------------------------------------------------
JD_DAYS = 3          # only jobs recent enough to show up need their JD
JD_MAX_PER_BOARD = 25    # new JD fetches per board per poll; the rest come next poll (JDs are cached)
KNOWN: dict = {}         # url -> {"elig", "jd_terms"} from earlier polls, set by poll()
FAILED: set = set()      # board ids that failed in the last poll() (their old jobs should be kept)


def enrich(jobs: list, now: int) -> list:
    """For recent jobs whose JD we don't have yet, fetch it (tech terms + eligibility need it).

    Each job's `_detail` is a function returning (jd_text, exact_posted_or_None, country_or_None).
    A job whose detail says a non-US country is dropped.
    """
    n = 0
    for i, j in enumerate(jobs):
        if not j:
            continue
        fn = j.pop("_detail", None)
        cached = KNOWN.get(j["url"])
        if cached and cached.get("elig") is not None and j.get("elig") is None:
            j["elig"], j["jd_terms"], j["excerpt"] = cached.get("elig"), cached.get("jd_terms"), cached.get("excerpt")
            continue
        if (fn is None or j.get("elig") is not None or tech.TITLE_NOT.search(j["title"]) or n >= JD_MAX_PER_BOARD
                or (j.get("posted") and j["posted"] < now - JD_DAYS * 86400)):
            continue
        n += 1
        try:
            text, exact, country = fn()
        except Exception:
            continue
        if country and not classify.is_us([], [country]):
            jobs[i] = None
            continue
        j["jd_terms"] = tech.jd_terms(text or "")
        j["elig"] = eligibility.check(text or "")
        j["excerpt"] = eligibility.excerpt(text or "")
        if exact:   # never newer than what the listing said ("Posted 30+ Days Ago" vs a later startDate)
            j["posted"] = min(exact, j["posted"]) if j.get("posted") else exact
    for j in jobs:
        if j:
            j.pop("_detail", None)
    return jobs


def fetch_greenhouse(token, company):
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(token)}/jobs?content=true",
                 conditional=True)
    out = []
    for j in d.get("jobs", []):
        loc = (j.get("location") or {}).get("name") or ""
        out.append(_job("gh", j.get("company_name") or company, j.get("title"), j.get("absolute_url"),
                        [p.strip() for p in re.split(r";|\|", loc)], None,
                        iso(j.get("first_published")) or iso(j.get("updated_at")),
                        jd=html.unescape(j.get("content") or "")))
    return out


def fetch_lever(token, company):
    d = get_json(f"https://api.lever.co/v0/postings/{urllib.parse.quote(token)}?mode=json", conditional=True)
    out = []
    for j in d if isinstance(d, list) else []:
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location")]
        intern = bool(re.search(r"intern|co-?op", cat.get("commitment") or "", re.I))
        created = j.get("createdAt")
        jd = " ".join([j.get("descriptionPlain") or "", j.get("additionalPlain") or ""] +
                      [x.get("content") or "" for x in j.get("lists") or []])
        out.append(_job("lever", company, j.get("text"), j.get("hostedUrl"), locs,
                        [j["country"]] if j.get("country") else None,
                        int(created / 1000) if created else None, intern, jd=jd))
    return out


def fetch_ashby(token, company):
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(token)}", conditional=True)
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
                        [c for c in countries if c] or None, iso(j.get("publishedAt")), intern,
                        jd=j.get("descriptionPlain") or j.get("descriptionHtml") or ""))
    return out


def _sr_detail(token, pid):
    def fn():
        d = get_json(f"https://api.smartrecruiters.com/v1/companies/{urllib.parse.quote(token)}/postings/{pid}")
        secs = (d.get("jobAd") or {}).get("sections") or {}
        return " ".join((v or {}).get("text") or "" for k, v in secs.items() if k != "companyDescription"), None, None
    return fn


def fetch_smartrecruiters(token, company, now=None):
    now = now or int(time.time())
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
                            [place], [loc.get("country")] if loc.get("country") else None, iso(j.get("releasedDate")),
                            detail=_sr_detail(token, j.get("id"))))
    return enrich(out, now)


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


def _wd_detail(api_base, path):
    def fn():
        info = get_json(api_base + path).get("jobPostingInfo") or {}
        return info.get("jobDescription") or "", iso(info.get("startDate")), (info.get("country") or {}).get("descriptor")
    return fn


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
                                workday_posted(p.get("postedOn"), now), extra_intern=trusted,
                                detail=_wd_detail(api.rsplit("/jobs", 1)[0], path)))
            if len(posts) < WD_PAGE or (page + 1) * WD_PAGE >= (d.get("total") or 0):
                break
    return enrich(out, now)


# ---- Oracle Cloud HCM -----------------------------------------------------
# GET {host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=...&finder=findReqs;
#     siteNumber=..,keyword=..,limit=25,offset=N,sortBy=POSTING_DATES_DESC   (paging lives inside `finder`)
ORACLE_PAGE, ORACLE_MAX_PAGES, ORACLE_DAYS = 25, 8, 10


def _oracle_detail(host, site, rid):
    def fn():
        finder = urllib.parse.quote(f'ById;Id="{rid}",siteNumber={site}', safe=";,=")
        it = (get_json(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                       f"?onlyData=true&expand=all&finder={finder}").get("items") or [{}])[0]
        text = " ".join(it.get(k) or "" for k in ("ExternalDescriptionStr", "ExternalQualificationsStr",
                                                   "ExternalResponsibilitiesStr", "ShortDescriptionStr"))
        return text, iso(it.get("ExternalPostedStartDate")), None
    return fn


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
                                locs, [c for c in countries if c] or None, posted,
                                detail=_oracle_detail(host, site, r.get("Id"))))
            if len(reqs) < ORACLE_PAGE or oldest < now - ORACLE_DAYS * 86400:
                break  # newest first: once we're past the window, stop paging
    return enrich(out, now)


# ---- Big employers' own career sites ---------------------------------------------------------------------
BIGTECH_DAYS = 10    # page back until jobs are this old (older dates still matter: see build.py old_dates)
HTML_UA = "Mozilla/5.0 (X11; Linux x86_64) coop-intern-radar (+https://github.com/dheepakkaran/coop-intern-radar)"


def get_text(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": HTML_UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise Gone(url) from None
        raise


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def day_start_pt(text: str | None) -> int | None:
    """"November 14, 2026" (a whole day, US Pacific) -> the earliest moment of that day, 08:00 UTC.
    The earliest moment is the safe choice: a day-only date never makes a job look newer than it is
    (build.posted_at swaps in our own first sighting when it falls inside that day)."""
    if not text:
        return None
    try:
        d = datetime.strptime(re.sub(r"\s+", " ", text.strip()), "%B %d, %Y")
    except ValueError:
        return None
    return int(d.replace(tzinfo=timezone.utc).timestamp()) + 8 * 3600


def fetch_amazon(token, company, now=None):
    """amazon.jobs search.json: public JSON behind the search page, newest first, day-only dates."""
    now = now or int(time.time())
    out = []
    for page in range(5):
        d = get_json(f"https://www.amazon.jobs/en/search.json?base_query=intern&country=USA&sort=recent"
                     f"&result_limit=100&offset={page * 100}")
        rows = d.get("jobs") or []
        oldest = now
        for j in rows:
            posted = day_start_pt(j.get("posted_date"))
            oldest = min(oldest, posted or now)
            if j.get("country_code") not in (None, "USA"):
                continue
            jd = _strip_html(" ".join(j.get(k) or "" for k in ("description", "basic_qualifications",
                                                               "preferred_qualifications")))
            row = _job("amazon", j.get("company_name") or company, j.get("title"),
                       "https://www.amazon.jobs" + (j.get("job_path") or ""),
                       [j.get("normalized_location") or j.get("location") or ""], ["US"], posted,
                       extra_intern="internship" in (j.get("primary_search_label") or ""), jd=jd or None)
            if row:
                row["posted_day"] = True
            out.append(row)
        if len(rows) < 100 or oldest < now - BIGTECH_DAYS * 86400:
            break
    return out


GOOGLE_DATA = re.compile(r"AF_initDataCallback\(\{key: 'ds:1', hash: '\d+', data:(.*?), sideChannel: \{\}\}\);</script>", re.S)


def fetch_google(token, company, now=None):
    """Google Careers search page, first page only (robots.txt disallows `page=`). The jobs are embedded as
    JSON. Google re-dates jobs when it edits them and sorts by that edit date, so we use the creation date
    (field 12): the conservative one."""
    s = get_text("https://www.google.com/about/careers/applications/jobs/results"
                 "?q=intern&location=United%20States&sort_by=date")
    m = GOOGLE_DATA.search(s)
    if not m:
        raise ValueError("google: no ds:1 data (page layout changed?)")
    out = []
    for j in (json.loads(m.group(1)) or [[]])[0] or []:
        try:
            jid, title, places = j[0], j[1], j[9] or []
            created = (j[12] or [None])[0]
        except (IndexError, TypeError):
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
        jd = _strip_html(" ".join((x[1] if isinstance(x, list) and len(x) > 1 else "") or ""
                                  for x in (j[3], j[4], j[10], j[15] if len(j) > 15 else None,
                                            j[19] if len(j) > 19 else None)))
        out.append(_job("google", company, title,
                        f"https://www.google.com/about/careers/applications/jobs/results/{jid}-{slug}",
                        [p[0] for p in places if p], [p[5] for p in places if p and len(p) > 5] or None,
                        int(created) if created else None, jd=jd or None))
    return out


APPLE_DATA = re.compile(r'__staticRouterHydrationData = JSON\.parse\((".*?")\);', re.S)


def fetch_apple(token, company, now=None):
    """jobs.apple.com Students team (internships) in the US, newest first; data embedded in the page."""
    out = []
    for page in (1, 2, 3):
        s = get_text("https://jobs.apple.com/en-us/search?sort=newest&location=united-states-USA"
                     f"&team=internships-STDNT-INTRN&page={page}")
        m = APPLE_DATA.search(s)
        if not m:
            raise ValueError("apple: no hydration data (page layout changed?)")
        sr = (json.loads(json.loads(m.group(1))).get("loaderData") or {}).get("search") or {}
        rows = sr.get("searchResults") or []
        for j in rows:
            locs = j.get("locations") or []
            countries = ["US" if "USA" in (l.get("countryID") or "") else (l.get("countryName") or "") for l in locs]
            places = [", ".join(x for x in (l.get("city"), l.get("stateProvince")) if x) or (l.get("name") or "")
                      for l in locs]
            out.append(_job("apple", company, j.get("postingTitle"),
                            f"https://jobs.apple.com/en-us/details/{j.get('id')}/{j.get('transformedPostingTitle') or ''}",
                            places or ["United States"], [c for c in countries if c] or ["US"],
                            iso(j.get("postDateInGMT")), extra_intern=True, jd=j.get("jobSummary") or None))
        if len(rows) < 20 or page * 20 >= (sr.get("totalRecords") or 0):
            break
    return out


FETCHERS = {"gh": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby, "sr": fetch_smartrecruiters,
            "wd": fetch_workday, "oracle": fetch_oracle, "amazon": fetch_amazon, "google": fetch_google,
            "apple": fetch_apple}


def _spread(bid: str, every: int) -> int:
    """A stable offset in [0, every) per board, so first polls of many new boards spread over the interval."""
    return zlib.crc32(bid.encode()) % max(every, 1)


def _parse_key() -> str:
    return f"{PARSE_VERSION}|{eligibility.RULES_VERSION}"


def _slim(jobs: list[dict], since: int) -> dict:
    """What a board's result is cached as: jobs that can still be new in full, older ones as url -> date."""
    keep, old = [], {}
    for j in jobs:
        if j.get("posted") and j["posted"] < since:
            old[j["url"]] = j["posted"]
        else:
            keep.append({k: v for k, v in j.items() if k != "countries" and not k.startswith("_")})
    return {"jobs": keep, "old": old}


def _expand(cache: dict | None, bid: str) -> list[dict]:
    """Cached board result -> job rows: the full ones, plus slim {url, posted} rows for the older dates."""
    if not cache:
        return []
    rows = [{**j, "board": bid} for j in cache.get("jobs") or []]
    rows += [{"url": u, "posted": p, "board": bid, "_old": True} for u, p in (cache.get("old") or {}).items()]
    return rows


def is_hot(b: dict, now: int) -> bool:
    c = b.get("cache") or {}
    if c.get("jobs"):
        return True
    return any(p >= now - HOT_DAYS * 86400 for p in (c.get("old") or {}).values())


def next_check(bid: str, b: dict, now: int) -> int:
    if b["ats"] in ETAG_ATS or b["ats"] in ("amazon", "google", "apple") or is_hot(b, now):
        return now + HOT_EVERY
    return now + COLD_EVERY


def retry_soon(err: str) -> bool:
    """Rate limits, server trouble and network errors pass: try next run. Other 4xx (403, 410, 422: a board
    that refuses or moved) will not fix itself in 30 min: wait the cold interval."""
    if err == "gone":   # 404: 3 misses drop a board, so space them out (~9 h, as when boards were polled every 3 h)
        return False
    m = re.match(r"HTTP (\d+)", err or "")
    return not m or int(m.group(1)) in (408, 425, 429) or int(m.group(1)) >= 500


DEAD_RETRY = 7 * 86400   # a board that 404'd max_fails times is tried once a week (it may come back)


def poll(boards: dict[str, dict], max_fails: int = 3, known: dict | None = None, now: int | None = None,
         force: bool = False, window_start: int | None = None) -> tuple[list[dict], dict]:
    """Poll the boards that are due (all of them with force=True); every other live board answers from its
    cached result. Updates each board's `fails`, `next`, `etag` and `cache` in place. Returns (jobs, health).

    Rows for jobs too old to be new carry only url + posted (+ `_old`): build.py keeps them as old dates.
    `known` = JD results from earlier polls (url -> elig/jd_terms), so each JD is fetched once."""
    global KNOWN, FAILED
    KNOWN = known or {}
    FAILED = set()
    now = now or int(time.time())
    since = window_start or now - 2 * 86400
    pkey = _parse_key()
    jobs, health = [], {}
    for ats, fetch in FETCHERS.items():
        mine = [(bid, b) for bid, b in boards.items() if b["ats"] == ats]
        live = [(bid, b) for bid, b in mine if b.get("fails", 0) < max_fails]
        retry = [(bid, b) for bid, b in mine if b.get("fails", 0) >= max_fails
                 and now - b.get("dead_at", 0) >= DEAD_RETRY]
        for bid, b in live:
            if "next" not in b:   # never scheduled: spread first polls over the interval (due now if never read)
                b["next"] = 0 if not b.get("cache") else now + _spread(bid, COLD_EVERY)
        todo = [(bid, b) for bid, b in live if force or b.get("next", 0) <= now] + retry
        h = {"ok": True, "boards": len(live), "checked": len(todo), "unchanged": 0, "failed": 0, "rows": 0,
             "errors": {}}
        t0 = time.time()

        def one(item):
            bid, b = item
            fresh_etag = (b.get("etag") and b.get("pkey") == pkey and now - b.get("etag_t", 0) < ETAG_MAX_AGE
                          and b.get("cache") is not None)
            _CTX.etag = b.get("etag") if fresh_etag else None
            _CTX.new_etag = None
            try:
                takes_now = fetch in (fetch_smartrecruiters, fetch_workday, fetch_oracle, fetch_amazon)
                got = fetch(b["token"], b["company"], now=now) if takes_now else fetch(b["token"], b["company"])
                return bid, [j for j in got if j], None, _CTX.new_etag
            except NotModified:
                return bid, None, "304", None
            except Gone:
                return bid, None, "gone", None
            except urllib.error.HTTPError as e:  # e.g. 429, 5xx, 521 (Workday data center down): retry next run
                return bid, None, f"HTTP {e.code}", None
            except Exception as e:  # network trouble: try again next run, don't count as gone
                return bid, None, f"{type(e).__name__}", None

        with ThreadPoolExecutor(max_workers=WORKERS[ats]) as pool:
            for bid, got, err, etag in pool.map(one, todo):
                b = boards[bid]
                if err == "304":
                    h["unchanged"] += 1
                    b["fails"] = 0
                    b.pop("dead_at", None)
                elif err:
                    if err == "gone":
                        b["fails"] = b.get("fails", 0) + 1
                        if b["fails"] >= max_fails:
                            b["dead_at"] = now   # stops being polled; reported; retried after DEAD_RETRY
                    FAILED.add(bid)
                    h["failed"] += 1
                    h["errors"][err] = h["errors"].get(err, 0) + 1
                else:
                    b["fails"] = 0
                    b.pop("dead_at", None)
                    b["cache"] = _slim(got, since)
                    b["pkey"] = pkey
                    if etag:
                        b["etag"], b["etag_t"] = etag, now
                    else:
                        b.pop("etag", None)
                b["next"] = (next_check(bid, b, now) if err in (None, "304")
                             else now + HOT_EVERY if retry_soon(err) else now + COLD_EVERY)
        for bid, b in live:
            rows = _expand(b.get("cache"), bid)
            # a cached result can age past the window between polls: those rows become old dates too
            for r in rows:
                if not r.get("_old") and r.get("posted") and r["posted"] < since:
                    r.update({"_old": True})
            jobs.extend(rows)
            h["rows"] += sum(1 for r in rows if not r.get("_old"))
        h["seconds"] = round(time.time() - t0, 1)
        h["ok"] = h["checked"] == 0 or h["failed"] < h["checked"]
        h["hot"] = sum(1 for _, b in live if is_hot(b, now))
        # boards that stopped answering (404 x max_fails): the page lists them, so lost coverage is never silent
        h["dead"] = [{"company": b["company"], "board": bid, "since": b.get("dead_at")}
                     for bid, b in mine if b.get("fails", 0) >= max_fails]
        health[ATS_NAMES[ats]] = h
        print(f"[ats] {ATS_NAMES[ats]}: {h}", file=sys.stderr)
    return jobs, health
