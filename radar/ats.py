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

ATS_NAMES = {"gh": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "sr": "SmartRecruiters"}
WORKERS = {"gh": 8, "lever": 6, "ashby": 6, "sr": 6}  # polite: a few requests at a time per ATS

BOARD_PATTERNS = [
    ("gh", re.compile(r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_(?:app|board)\?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("gh", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"(?<!eu\.)jobs\.lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)", re.I)),
    ("sr", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
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
            token = urllib.parse.unquote(m.group(1)).strip()
            if token.lower() in NOT_A_BOARD:
                break
            bid = f"{ats}:{token.lower()}"
            boards.setdefault(bid, {"ats": ats, "token": token, "company": company})
            break
    return boards


class Gone(Exception):
    """The board does not exist (404): count it and stop polling it after a few misses."""


def get_json(url: str, timeout: int = 25, tries: int = 3):
    for i in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
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


FETCHERS = {"gh": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby, "sr": fetch_smartrecruiters}


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
