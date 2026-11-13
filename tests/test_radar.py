import json
import os

from radar import ats, build, classify, sources, store

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
    meta = build.run(state, out, fetch=_fake_fetch(now, [old, fresh], []), poll="no", now=now)
    assert meta["bootstrap"] is True
    assert meta["count_24h"] == 1  # only the natively fresh one

    # Next run: a vansh-only job without a usable date appears -> first_seen makes it new.
    vjob = {"company_name": "Gamma", "title": "SWE Intern", "date_posted": None, "url": "https://go.vansh.nyc/abc",
            "locations": ["Remote"], "season": "Fall", "active": True, "is_visible": True}
    meta2 = build.run(state, out, fetch=_fake_fetch(now, [old, fresh], [vjob]),
                      resolve=lambda u: "https://jobs.ashbyhq.com/gamma/1", poll="no", now=now + 600)
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
              resolve=lambda u: u, poll="no", now=now)
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
    meta = build.run(str(tmp_path / "s.gz"), str(tmp_path / "o"), fetch=fetch, poll="no", now=2_000_000_000)
    assert meta["sources"]["Simplify"]["ok"] and not meta["sources"]["vanshb03"]["ok"]


def test_us_only():
    us = ["Boston, MA", "NYC", "Remote in USA", "Melbourne, FL", "Carlsbad, Ca", "Albuquerque, New Mexico", "California"]
    non = ["Toronto, ON, Canada", "London, UK", "Remote in Canada", "Waterloo, ON", "Bengaluru, India", "Melbourne, Australia"]
    assert all(classify.loc_country(l) == "US" for l in us), [l for l in us if classify.loc_country(l) != "US"]
    assert all(classify.loc_country(l) == "X" for l in non), [l for l in non if classify.loc_country(l) != "X"]
    assert classify.is_us(["Remote"])                      # can't tell -> keep
    assert classify.is_us(["Toronto, ON", "Austin, TX"])   # any US place -> keep
    assert not classify.is_us(["Remote", "London, UK"])
    assert not classify.is_us(["Remote"], ["CA"]) and classify.is_us(["Remote"], ["US"])


def test_terms_from_title():
    assert classify.terms_from_title("SWE Intern - Summer 2027") == ["Summer 2027"]
    assert classify.terms_from_title("Co-op, Fall '26") == ["Fall 2026"]
    assert classify.terms_from_title("Data Intern") == []


def test_discover_boards():
    b = ats.discover([
        ("Stripe", "https://boards.greenhouse.io/stripe/jobs/123?gh_jid=123"),
        ("Acme", "https://job-boards.greenhouse.io/acme/jobs/9"),
        ("Plaid", "https://jobs.lever.co/plaid/abc-123/apply"),
        ("Ramp", "https://jobs.ashbyhq.com/ramp/uuid"),
        ("Visa", "https://jobs.smartrecruiters.com/Visa/7444?trid=x"),
        ("EU", "https://jobs.eu.lever.co/euco/1"),
        ("Other", "https://careers.example.com/1"),
    ])
    assert set(b) == {"gh:stripe", "gh:acme", "lever:plaid", "ashby:ramp", "sr:visa"}
    assert b["sr:visa"]["token"] == "Visa" and b["gh:stripe"]["company"] == "Stripe"


def test_ats_parsers_keep_us_interns_only(monkeypatch):
    responses = {
        "boards-api.greenhouse.io": {"jobs": [
            {"title": "Software Engineering Intern, Summer 2027", "absolute_url": "https://x.com/j/1",
             "location": {"name": "New York, NY"}, "first_published": "2026-11-13T10:00:00-05:00"},
            {"title": "Senior Engineer", "absolute_url": "https://x.com/j/2", "location": {"name": "New York, NY"},
             "first_published": "2026-11-13T10:00:00-05:00"},
            {"title": "Data Intern", "absolute_url": "https://x.com/j/3", "location": {"name": "Toronto, ON"},
             "first_published": "2026-11-13T10:00:00-05:00"},
            {"title": "International Sales Lead", "absolute_url": "https://x.com/j/4", "location": {"name": "Austin, TX"}},
        ]},
        "api.lever.co": [
            {"text": "Hardware Co-op", "hostedUrl": "https://jobs.lever.co/a/1", "createdAt": 1794500000000,
             "country": "US", "categories": {"commitment": "Full-time", "allLocations": ["Remote"]}},
            {"text": "Research Associate", "hostedUrl": "https://jobs.lever.co/a/2", "createdAt": 1794500000000,
             "country": "US", "categories": {"commitment": "Intern", "location": "Seattle, WA"}},
            {"text": "ML Intern", "hostedUrl": "https://jobs.lever.co/a/3", "createdAt": 1794500000000,
             "country": "GB", "categories": {"commitment": "Intern", "location": "Remote"}},
        ],
        "api.ashbyhq.com": {"jobs": [
            {"title": "AI Intern", "jobUrl": "https://jobs.ashbyhq.com/r/1", "location": "Remote (US)",
             "employmentType": "Intern", "publishedAt": "2026-11-13T17:12:35.753+00:00", "isListed": True,
             "address": {"postalAddress": {"addressCountry": "USA"}}},
            {"title": "Hidden Intern", "jobUrl": "https://jobs.ashbyhq.com/r/2", "isListed": False},
        ]},
        "api.smartrecruiters.com": {"content": [
            {"id": "7", "name": "Finance Intern", "releasedDate": "2026-11-13T12:00:00.000Z",
             "location": {"city": "Chicago", "region": "IL", "country": "us"}},
        ]},
    }
    monkeypatch.setattr(ats, "get_json", lambda url, **k: next(v for h, v in responses.items() if h in url))
    gh = ats.fetch_greenhouse("x", "X")
    assert [j["title"] for j in gh if j] == ["Software Engineering Intern, Summer 2027"]
    assert [j for j in gh if j][0]["terms"] == ["Summer 2027"]
    lv = [j["title"] for j in ats.fetch_lever("a", "A") if j]
    assert lv == ["Hardware Co-op", "Research Associate"]          # commitment=Intern counts; GB dropped
    assert [j["title"] for j in ats.fetch_ashby("r", "R") if j] == ["AI Intern"]
    sr = [j for j in ats.fetch_smartrecruiters("s", "S") if j]
    assert len(sr) == 1 and sr[0]["url"] == "https://jobs.smartrecruiters.com/s/7"


def test_poll_merges_with_lists_and_first_poll_is_quiet(tmp_path):
    now = 2_000_000_000
    listed = _row("Acme", "ML Intern", now - 3600, url="https://boards.greenhouse.io/acme/jobs/1")
    board_jobs = [
        # same job as the list (same URL) -> one row with two sources, earlier date wins
        {"source": "Greenhouse", "company": "Acme", "title": "Machine Learning Intern",
         "url": "https://boards.greenhouse.io/acme/jobs/1", "locations": ["Boston, MA"], "terms": [],
         "posted": now - 7200, "field": None, "sponsorship": None},
        # a co-op only the board has, with no date
        {"source": "Greenhouse", "company": "Acme", "title": "Robotics Co-op",
         "url": "https://boards.greenhouse.io/acme/jobs/2", "locations": ["Boston, MA"], "terms": [],
         "posted": None, "field": None, "sponsorship": None},
    ]
    calls = []

    def fake_poll(boards):
        calls.append(sorted(boards))
        return list(board_jobs), {"Greenhouse": {"ok": True, "boards": 1, "failed": 0, "rows": 2}}

    st, out = str(tmp_path / "s.gz"), str(tmp_path / "o")
    build.run(st, out, fetch=_fake_fetch(now, [listed], []), poll="no", now=now)          # lists only
    build.run(st, out, fetch=_fake_fetch(now, [listed], []), poll_fn=fake_poll, now=now + 60)  # first poll
    assert calls == [["gh:acme"]]
    data = json.load(open(tmp_path / "o" / "jobs.json"))
    acme = [j for j in data["jobs"] if j["url"].endswith("/1")]
    assert len(acme) == 1 and set(acme[0]["sources"]) == {"Simplify", "Greenhouse"} and acme[0]["posted"] == now - 7200
    assert not any(j["title"] == "Robotics Co-op" for j in data["jobs"])  # undated + first poll -> not new

    # A later poll finds a new undated board job -> new, dated by first_seen.
    board_jobs.append({"source": "Greenhouse", "company": "Acme", "title": "Firmware Co-op",
                       "url": "https://boards.greenhouse.io/acme/jobs/3", "locations": ["Boston, MA"], "terms": [],
                       "posted": None, "field": None, "sponsorship": None})
    meta = build.run(st, out, fetch=_fake_fetch(now, [listed], []), poll_fn=fake_poll, poll="yes", now=now + 7200)
    data = json.load(open(tmp_path / "o" / "jobs.json"))
    fw = [j for j in data["jobs"] if j["title"] == "Firmware Co-op"][0]
    assert fw["posted"] == now + 7200 and fw["kind"] == "co-op"
    assert meta["boards"]["Greenhouse"] == 1 and meta["sources"]["Greenhouse"]["ok"]

    # Between polls ('auto' before 3 h) the cached board jobs stay on the page.
    build.run(st, out, fetch=_fake_fetch(now, [listed], []), poll_fn=fake_poll, now=now + 7200 + 1800)
    assert len(calls) == 2
    data = json.load(open(tmp_path / "o" / "jobs.json"))
    assert any(j["title"] == "Firmware Co-op" for j in data["jobs"])


def test_lists_drop_non_us(tmp_path):
    now = 2_000_000_000
    ca = _row("Maple", "Data Intern", now - 60, url="https://x.ca/1")
    ca["locations"] = ["Toronto, ON, Canada"]
    build.run(str(tmp_path / "s"), str(tmp_path / "o"), fetch=_fake_fetch(now, [ca], []), poll="no", now=now)
    assert json.load(open(tmp_path / "o" / "jobs.json"))["jobs"] == []


def test_vansh_shortlink_merges_with_board_by_url(tmp_path):
    now = 2_000_000_000
    v = {"company_name": "SharkNinja", "title": "Digital & Website Technology Intern", "date_posted": now - 3600,
         "url": "https://go.vansh.nyc/q1", "locations": ["Needham, MA"], "season": "Spring", "active": True, "is_visible": True}
    board = [{"source": "Greenhouse", "company": "SharkNinja", "title": "Spring 2027: Digital & Website Technology Intern",
              "url": "https://boards.greenhouse.io/sharkninja/jobs/77", "locations": ["Needham, MA, United States"],
              "terms": ["Spring 2027"], "posted": now - 7200, "field": None, "sponsorship": None}]
    build.run(str(tmp_path / "s"), str(tmp_path / "o"), fetch=_fake_fetch(now, [], [v]),
              resolve=lambda u: "https://boards.greenhouse.io/sharkninja/jobs/77?utm_source=vansh",
              poll="yes", poll_fn=lambda b: (list(board), {}), now=now)
    jobs = json.load(open(tmp_path / "o" / "jobs.json"))["jobs"]
    assert len(jobs) == 1 and set(jobs[0]["sources"]) == {"vanshb03", "Greenhouse"}
