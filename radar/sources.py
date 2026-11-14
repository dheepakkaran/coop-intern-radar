"""Feeds that publish intern listings as JSON on GitHub.

Every source returns a list of `Job` dicts with the same fields:
  source, company, title, url, locations, terms, posted (unix seconds or None), field, sponsorship
"""
import json
import urllib.request

UA = "coop-intern-radar (+https://github.com/dheepakkaran/coop-intern-radar)"

SIMPLIFY_URL = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"
VANSH_URL = "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json"


def fetch_json(url: str, timeout: int = 60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _live(x: dict) -> bool:
    return bool(x.get("active", True)) and bool(x.get("is_visible", True))


def parse_simplify(rows: list[dict]) -> list[dict]:
    out = []
    for x in rows:
        if not _live(x) or not x.get("url"):
            continue
        out.append({
            "source": "Simplify",
            "company": x.get("company_name", "").strip(),
            "title": x.get("title", "").strip(),
            "url": x["url"],
            "locations": x.get("locations") or [],
            "terms": [t for t in (x.get("terms") or []) if t and t != "N/A"],
            "posted": x.get("date_posted"),
            "field": x.get("category"),
            "sponsorship": x.get("sponsorship"),
            "degrees": x.get("degrees") or [],
        })
    return out


def parse_vansh(rows: list[dict]) -> list[dict]:
    out = []
    for x in rows:
        if not _live(x) or not x.get("url"):
            continue
        season = x.get("season")
        terms = [s.strip() for s in season.split("/")] if season and season != "null" else []
        out.append({
            "source": "vanshb03",
            "company": x.get("company_name", "").strip(),
            "title": x.get("title", "").strip(),
            "url": x["url"],  # go.vansh.nyc shortlink, resolved later for new rows only
            "locations": x.get("locations") or [],
            "terms": terms,
            "posted": x.get("date_posted"),
            "field": None,
            "sponsorship": x.get("sponsorship"),
        })
    return out


SOURCES = {
    "Simplify": (SIMPLIFY_URL, parse_simplify),
    "vanshb03": (VANSH_URL, parse_vansh),
}


def resolve_shortlink(url: str, timeout: int = 15) -> str:
    """Follow a go.vansh.nyc redirect to the real apply URL; return the input on failure."""
    if "go.vansh.nyc" not in url:
        return url
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.geturl()
    except Exception:
        return url
