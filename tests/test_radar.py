import json
import os

from radar import build, classify, sources, store

HERE = os.path.dirname(__file__)


def fixture(name):
    with open(os.path.join(HERE, "fixtures", name)) as f:
        return json.load(f)


def test_kind_coop_from_title_or_term():
    assert classify.kind("Software Engineering Co-op", []) == "co-op"
    assert classify.kind("ML Intern", ["Spring 2027"]) == "co-op"
    assert classify.kind("ML Intern", ["Summer 2027"]) == "intern"
    assert classify.kind("Cooperative Education Student", []) == "co-op"
    assert classify.kind("Cooperative Engineer", []) == "intern"  # plain 'cooperative' is not a co-op


def test_field_and_places():
    assert classify.field("Robotics Intern", None) == "Hardware"
    assert classify.field("x", "Software Engineering") == "Software"
    assert classify.places(["Boston, MA", "Remote in USA"]) == ["Boston/MA", "Remote"]
    assert classify.places(["Austin, TX"]) == []
    assert classify.places(["Toronto, ON, Canada"]) == ["Canada"]
    assert classify.places(["Toronto, ON", "Austin, TX"]) == []


def test_norm_url_drops_tracking():
    a = store.norm_url("https://Jobs.Lever.co/acme/123/?utm_source=x&lever-source=Simplify")
    assert a == "https://jobs.lever.co/acme/123"


def test_parsers_on_real_samples():
    s = sources.parse_simplify(fixture("simplify_sample.json"))
    v = sources.parse_vansh(fixture("vansh_sample.json"))
    assert s and v
    for j in s + v:
        assert j["company"] and j["title"] and j["url"]
        assert isinstance(j["posted"], int)
    assert any("Spring 2027" in j["terms"] for j in s)


def _fake_fetch(now, simplify_rows, vansh_rows):
    def fetch(url):
        return simplify_rows if "SimplifyJobs" in url else vansh_rows
    return fetch


def _row(company, title, posted, url="https://boards.greenhouse.io/acme/jobs/1", terms=("Summer 2027",), **kw):
    r = {"company_name": company, "title": title, "date_posted": posted, "url": url,
         "locations": ["Boston, MA"], "terms": list(terms), "active": True, "is_visible": True}
    r.update(kw)
    return r


def test_first_run_does_not_flood_and_second_run_finds_new(tmp_path):
    now = 2_000_000_000
    state, out = str(tmp_path / "seen.json.gz"), str(tmp_path / "out")
    old = _row("Acme", "Data Intern", now - 30 * 86400)
    fresh = _row("Beta", "ML Co-op", now - 3600, url="https://jobs.lever.co/beta/9")
    meta = build.run(state, out, fetch=_fake_fetch(now, [old, fresh], []), now=now)
    assert meta["bootstrap"] is True
    assert meta["count_24h"] == 1  # only the natively fresh one

    # Next run: a vansh-only job without a usable date appears -> first_seen makes it new.
    vjob = {"company_name": "Gamma", "title": "SWE Intern", "date_posted": None, "url": "https://go.vansh.nyc/abc",
            "locations": ["Remote"], "season": "Fall", "active": True, "is_visible": True}
    meta2 = build.run(state, out, fetch=_fake_fetch(now, [old, fresh], [vjob]),
                      resolve=lambda u: "https://jobs.ashbyhq.com/gamma/1", now=now + 600)
    data = json.load(open(os.path.join(out, "jobs.json")))
    gamma = [j for j in data["jobs"] if j["company"] == "Gamma"][0]
    assert gamma["url"] == "https://jobs.ashbyhq.com/gamma/1"
    assert gamma["kind"] == "co-op" and gamma["places"] == ["Remote"]
    assert meta2["count_24h"] == 2
    assert all(j["company"] != "Acme" for j in data["jobs"])  # 30 days old is outside the 7-day window


def test_merge_dedupes_across_feeds(tmp_path):
    now = 2_000_000_000
    a = _row("Acme Inc", "Data Science Intern", now - 7200)
    b = {"company_name": "Acme Inc.", "title": "Data Science Intern", "date_posted": now - 3600,
         "url": "https://go.vansh.nyc/x", "locations": ["Remote"], "season": "Summer", "active": True, "is_visible": True}
    build.run(str(tmp_path / "s.gz"), str(tmp_path / "o"), fetch=_fake_fetch(now, [a], [b]),
              resolve=lambda u: u, now=now)
    data = json.load(open(tmp_path / "o" / "jobs.json"))
    assert len(data["jobs"]) == 1
    j = data["jobs"][0]
    assert j["sources"] == ["Simplify", "vanshb03"]
    assert j["url"].startswith("https://boards.greenhouse.io")
    assert j["posted"] == now - 7200  # earliest date wins


def test_broken_feed_is_reported_not_fatal(tmp_path):
    def fetch(url):
        if "vanshb03" in url:
            raise TimeoutError("slow")
        return [_row("Acme", "Intern", 2_000_000_000 - 60)]
    meta = build.run(str(tmp_path / "s.gz"), str(tmp_path / "o"), fetch=fetch, now=2_000_000_000)
    assert meta["sources"]["Simplify"]["ok"] and not meta["sources"]["vanshb03"]["ok"]
