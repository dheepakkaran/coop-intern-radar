import re
import json
import os

import pytest

from radar import ats, build, classify, jdfetch, sources, store, tech

HERE = os.path.dirname(__file__)


@pytest.fixture(autouse=True)
def lenient_by_default(monkeypatch):
    """Most tests check merging, dates and sources with list-only jobs (no JD); the strict 'every check passes'
    filter has its own test below."""
    monkeypatch.setattr(build, "STRICT", False)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never touch the internet: any JD page fetch fails unless a test passes its own fake."""
    def offline(url, body=None, timeout=20):
        raise jdfetch.urllib.error.URLError("offline in tests")
    monkeypatch.setattr(jdfetch, "http", offline)


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


def _matches(out_dir):
    """Jobs in the "My matches" view (the page also carries every other US co-op/intern role for "All roles")."""
    return [j for j in json.load(open(os.path.join(str(out_dir), "jobs.json")))["jobs"] if j.get("match", True)]


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
        {"source": "Greenhouse", "company": "Acme", "title": "Data Platform Co-op",
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
    assert not any(j["title"] == "Data Platform Co-op" for j in data["jobs"])  # undated + first poll -> not new

    # A later poll finds a new undated board job -> new, dated by first_seen.
    board_jobs.append({"source": "Greenhouse", "company": "Acme", "title": "Firmware Software Co-op",
                       "url": "https://boards.greenhouse.io/acme/jobs/3", "locations": ["Boston, MA"], "terms": [],
                       "posted": None, "field": None, "sponsorship": None})
    meta = build.run(st, out, fetch=_fake_fetch(now, [listed], []), poll_fn=fake_poll, poll="yes", now=now + 7200)
    data = json.load(open(tmp_path / "o" / "jobs.json"))
    fw = [j for j in data["jobs"] if j["title"] == "Firmware Software Co-op"][0]
    assert fw["posted"] == now + 7200 and fw["kind"] == "co-op"
    assert meta["boards"]["Greenhouse"] == 1 and meta["sources"]["Greenhouse"]["ok"]

    # Between polls ('auto' before 3 h) the cached board jobs stay on the page.
    build.run(st, out, fetch=_fake_fetch(now, [listed], []), poll_fn=fake_poll, now=now + 7200 + 1800)
    assert len(calls) == 2
    data = json.load(open(tmp_path / "o" / "jobs.json"))
    assert any(j["title"] == "Firmware Software Co-op" for j in data["jobs"])


def test_lists_drop_non_us(tmp_path):
    now = 2_000_000_000
    ca = _row("Maple", "Data Intern", now - 60, url="https://x.ca/1")
    ca["locations"] = ["Toronto, ON, Canada"]
    build.run(str(tmp_path / "s"), str(tmp_path / "o"), fetch=_fake_fetch(now, [ca], []), poll="no", now=now)
    assert json.load(open(tmp_path / "o" / "jobs.json"))["jobs"] == []


def test_vansh_shortlink_merges_with_board_by_url(tmp_path):
    now = 2_000_000_000
    v = {"company_name": "SharkNinja", "title": "Digital Data Technology Intern", "date_posted": now - 3600,
         "url": "https://go.vansh.nyc/q1", "locations": ["Needham, MA"], "season": "Spring", "active": True, "is_visible": True}
    board = [{"source": "Greenhouse", "company": "SharkNinja", "title": "Spring 2027: Digital Data Technology Intern",
              "url": "https://boards.greenhouse.io/sharkninja/jobs/77", "locations": ["Needham, MA, United States"],
              "terms": ["Spring 2027"], "posted": now - 7200, "field": None, "sponsorship": None}]
    build.run(str(tmp_path / "s"), str(tmp_path / "o"), fetch=_fake_fetch(now, [], [v]),
              resolve=lambda u: "https://boards.greenhouse.io/sharkninja/jobs/77?utm_source=vansh",
              poll="yes", poll_fn=lambda b: (list(board), {}), now=now)
    jobs = json.load(open(tmp_path / "o" / "jobs.json"))["jobs"]
    assert len(jobs) == 1 and set(jobs[0]["sources"]) == {"vanshb03", "Greenhouse"}


def test_discover_workday_and_oracle():
    b = ats.discover([
        ("NVIDIA", "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Intern_JR1"),
        ("Acme", "https://wd3.myworkdaysite.com/recruiting/acme/AcmeCareers/job/Boston/X_1"),
        ("WTW", "https://eedu.fa.em3.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1003/job/202607277"),
    ])
    assert b["wd:nvidia.wd5.myworkdayjobs.com|nvidia|nvidiaexternalcareersite"]["token"] == \
        "nvidia.wd5.myworkdayjobs.com|nvidia|NVIDIAExternalCareerSite"
    assert "wd:wd3.myworkdaysite.com|acme|acmecareers" in b
    assert b["oracle:eedu.fa.em3.oraclecloud.com|cx_1003"]["token"] == "eedu.fa.em3.oraclecloud.com|CX_1003"


def test_workday_posted_text():
    now = 2_000_000_000
    assert ats.workday_posted("Posted Today", now) == now
    assert ats.workday_posted("Posted Yesterday", now) == now - 36 * 3600
    assert ats.workday_posted("Posted 3 Days Ago", now) == now - int(3.5 * 86400)
    assert ats.workday_posted("Posted 30+ Days Ago", now) == now - 31 * 86400
    assert ats.workday_posted(None, now) is None


def test_workday_uses_job_type_facet(monkeypatch):
    now = 2_000_000_000
    calls = []
    facets = [{"facetParameter": "jobFamilyGroup", "values": [{"descriptor": "Engineering", "id": "e"}]},
              {"facetParameter": "locationMainGroup", "values": [
                  {"facetParameter": "locationHierarchy1", "descriptor": "Locations",
                   "values": [{"descriptor": "China", "id": "cn"}, {"descriptor": "United States of America", "id": "us"}]}]},
              {"facetParameter": "workerSubType", "values": [
                  {"descriptor": "Regular Employee", "id": "r"}, {"descriptor": "Intern (Fixed Term)", "id": "i"},
                  {"descriptor": "New College Graduate", "id": "g"}]}]
    posts = [{"title": "Hardware Engineer - Summer 2027", "externalPath": "/job/US-CA/HW_1",
              "locationsText": "US, CA, Santa Clara", "postedOn": "Posted Today"},
             {"title": "Software Intern", "externalPath": "/job/China/SW_2", "locationsText": "China, Shanghai",
              "postedOn": "Posted Today"},
             {"title": "ML Intern", "externalPath": "/job/x/ML_3", "locationsText": "3 Locations", "postedOn": None}]

    def fake(url, body=None, **k):
        calls.append(body)
        return {"facets": facets, "total": 3} if body["limit"] == 1 else {"jobPostings": posts, "total": 3}
    monkeypatch.setattr(ats, "get_json", fake)
    jobs = [j for j in ats.fetch_workday("nvidia.wd5.myworkdayjobs.com|nvidia|Ext", "NVIDIA", now=now) if j]
    assert calls[1]["appliedFacets"] == {"workerSubType": ["i"], "locationHierarchy1": ["us"]}
    assert calls[1]["searchText"] == ""
    # trusted facets: every returned job counts (the fake server ignores facets, real Workday filters China out)
    assert [j["title"] for j in jobs] == ["Hardware Engineer - Summer 2027", "Software Intern", "ML Intern"]
    assert jobs[0]["url"] == "https://nvidia.wd5.myworkdayjobs.com/Ext/job/US-CA/HW_1" and jobs[0]["posted"] == now


def test_workday_falls_back_to_text_search(monkeypatch):
    bodies = []

    def fake(url, body=None, **k):
        if body is None:  # job detail (JD) request for a non-tech title
            return {"jobPostingInfo": {"jobDescription": "<p>Python, SQL and machine learning models</p>",
                                       "country": {"descriptor": "United States of America"}}}
        bodies.append(body)
        if body["limit"] == 1:
            return {"facets": [{"facetParameter": "timeType", "values": [{"descriptor": "Full time", "id": "f"}]}]}
        return {"jobPostings": [{"title": "Account Specialist", "externalPath": "/job/a", "locationsText": "Chicago, Illinois"},
                                {"title": "Risk Co-op", "externalPath": "/job/b", "locationsText": "Chicago, Illinois"}],
                "total": 2}
    monkeypatch.setattr(ats, "get_json", fake)
    jobs = [j for j in ats.fetch_workday("x.wd1.myworkdayjobs.com|x|S", "X") if j]
    assert [b["searchText"] for b in bodies[1:]] == ["intern", "co-op"]
    assert [j["title"] for j in jobs] == ["Risk Co-op"]  # fuzzy text hits need an intern/co-op title
    assert jobs[0]["jd_terms"] == ["machine learning", "python", "sql"]  # non-tech title -> JD fetched


def test_workday_board_without_us_is_skipped_and_no_facet_needs_us_place(monkeypatch):
    fr = [{"facetParameter": "locationMainGroup", "values": [{"facetParameter": "locationCountry",
          "descriptor": "Location Country", "values": [{"descriptor": "France", "id": "fr"}]}]}]
    monkeypatch.setattr(ats, "get_json", lambda url, body=None, **k: {"facets": fr, "total": 0})
    assert ats.fetch_workday("t.wd3.myworkdayjobs.com|t|C", "T") == []

    def fake(url, body=None, **k):
        if body["limit"] == 1:
            return {"facets": []}
        return {"jobPostings": [{"title": "Stage Intern", "externalPath": "/a", "locationsText": "Cannes"},
                                {"title": "Data Intern", "externalPath": "/b", "locationsText": "Austin, TX"}], "total": 2}
    monkeypatch.setattr(ats, "get_json", fake)
    assert [j["title"] for j in ats.fetch_workday("t.wd3.myworkdayjobs.com|t|C", "T") if j] == ["Data Intern"]


def test_oracle_stops_paging_past_window(monkeypatch):
    now = 2_000_000_000
    from datetime import datetime, timezone
    day = lambda n: datetime.fromtimestamp(now - n * 86400, timezone.utc).strftime("%Y-%m-%d")
    urls = []

    def fake(url, **k):
        urls.append(url)
        kw = "intern" if "keyword=intern" in url else "co-op"
        page = int(re.search(r"offset=(\d+)", url).group(1)) // ats.ORACLE_PAGE
        reqs = [{"Id": f"{kw}{page}{i}", "Title": f"Data Intern {kw} {page}-{i}", "PostedDate": day(page * 6 + i // 10),
                 "PrimaryLocation": "Austin, TX, United States", "PrimaryLocationCountry": "US"} for i in range(25)]
        return {"items": [{"requisitionList": reqs}]}
    monkeypatch.setattr(ats, "get_json", fake)
    jobs = [j for j in ats.fetch_oracle("h.fa.oraclecloud.com|CX_1", "H", now=now) if j]
    assert sum("keyword=intern" in u for u in urls) == 3  # pages 0-2; page 2 is 12+ days old -> stop
    assert jobs[0]["url"] == "https://h.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/intern00"


def test_foreign_codes_and_titles():
    assert classify.loc_country("MX-BCN-MEXICALI-238 ~ Blvd Venustiano Carranza") == "X"
    assert classify.loc_country("US-CA-SANTA-CLARA") == "US"
    for t in ["Werkstudent Lean Management (m/w/d)", "STAGE - Ingénieur F/H", "Working Student (m/f/d)", "Year in Industry- Engineering"]:
        assert classify.FOREIGN_TITLE.search(t), t
    assert not classify.FOREIGN_TITLE.search("Software Engineering Intern - Summer 2027")
    assert classify.kind("2027 Spring Co op Biologics Design", []) == "co-op"


def test_new_board_type_first_poll_is_quiet(tmp_path):
    now = 2_000_000_000
    st, out = str(tmp_path / "s"), str(tmp_path / "o")
    old = [_row("A", "Old Intern", now - 60 * 86400, url="https://boards.greenhouse.io/a/jobs/0")]  # gives a board
    gh = {"source": "Greenhouse", "company": "A", "title": "Data Intern", "url": "https://g/1", "locations": ["Boston, MA"],
          "terms": [], "posted": None, "field": None, "sponsorship": None}
    wd = {**gh, "source": "Workday", "company": "B", "url": "https://w/1"}
    build.run(st, out, fetch=_fake_fetch(now, old, []), poll="no", now=now)
    build.run(st, out, fetch=_fake_fetch(now, old, []), poll="yes",
              poll_fn=lambda b: ([gh], {"Greenhouse": {"ok": True}}), now=now + 60)
    # Workday is added later: its undated jobs on its first poll are known, not new; Greenhouse's new one is new.
    gh2 = {**gh, "title": "ML Intern", "url": "https://g/2"}
    build.run(st, out, fetch=_fake_fetch(now, old, []), poll="yes",
              poll_fn=lambda b: ([gh, gh2, wd], {"Greenhouse": {"ok": True}, "Workday": {"ok": True}}), now=now + 7200)
    titles = {(j["company"], j["title"]) for j in json.load(open(tmp_path / "o" / "jobs.json"))["jobs"]}
    assert ("A", "ML Intern") in titles and ("B", "Data Intern") not in titles


def test_tech_titles():
    yes = {"Inference Engineer Intern": "AI/ML", "Forward Deployed Engineer Intern": "AI/ML", "FDE Intern": "AI/ML",
           "AI Engineer Co-op": "AI/ML", "Machine Learning Intern": "AI/ML", "LLM Post-Training Research Intern": "AI/ML",
           "Model Evals Intern": "AI/ML", "Agentic AI Developer Intern": "AI/ML", "Applied Scientist Intern": "AI/ML",
           "Computer Vision Co-op": "AI/ML", "MLOps Intern": "AI/ML",
           "Data Engineer Intern": "Data", "Business Intelligence Co-op": "Data", "Analytics Engineer Intern": "Data",
           "Software Engineer Intern": "Software", "SWE Intern": "Software", "Full Stack Developer Co-op": "Software",
           "Site Reliability Engineering Intern": "Software", "Embedded Software Co-op": "Software",
           "Computational Biology Intern": "Data"}
    for t, f in yes.items():
        assert tech.title_field(t) == f, (t, tech.title_field(t))
    assert tech.title_field("Data Center Software Engineer Intern")  # still a tech job
    for t in ["Mechanical Engineering Intern", "Finance Intern", "Data Entry Intern", "Business Development Intern",
              "Insurance Agent Intern", "Marketing Intern", "Supply Chain Co-op", "Teaching Assistant - Data Structures",
              "2027 Data Center Growth & Development Intern - NYC", "Grad Intern - Computational Chemistry"]:  # live audit
        assert tech.title_field(t) is None, t


def test_tech_jd_rule():
    marketing = tech.jd_terms("<p>Use AI tools and Excel to support campaigns. Python a plus.</p>")
    assert not tech.jd_is_tech(marketing)
    ml = tech.jd_terms("Build machine learning pipelines in Python with PyTorch and SQL.")
    ops = tech.jd_terms("Area manager: data analytics with Python, SQL, Tableau and Power BI.")
    assert not tech.jd_is_tech(ops)  # BI tools alone don't make a tech role
    assert tech.jd_is_tech(ml) and tech.jd_field(ml) == "AI/ML"
    assert not tech.jd_is_tech(tech.jd_terms("python sql aws docker"))  # no core term
    assert tech.classify("Research Intern", None, ml) == ("AI/ML", "JD")
    assert tech.classify("Forestry Intern", "AI/ML/Data", None) is None  # list category ignored
    assert tech.classify("Research Intern", None, None) is None
    assert tech.jd_terms(None) is None


def test_build_keeps_only_tech_and_two_days(tmp_path):
    now = 2_000_000_000
    rows = [_row("A", "Software Engineer Intern", now - 3600),
            _row("B", "Finance Intern", now - 3600, url="https://x/b"),
            _row("C", "Data Science Intern", now - 3 * 86400, url="https://x/c")]  # tech but 3 days old
    meta = build.run(str(tmp_path / "s"), str(tmp_path / "o"), fetch=_fake_fetch(now, rows, []), poll="no", now=now)
    jobs = _matches(tmp_path / "o")
    assert [j["company"] for j in jobs] == ["A"] and jobs[0]["why"] == "title"
    assert meta["not_tech_skipped"] == 1


# ---- eligibility (sentences are real, from intern JDs collected 2026-11-13) ---------------------------------
from radar import eligibility as elig


def _e(s):
    return elig.check(s)


def test_elig_auth():
    no = ["Due to the nature of our work US Citizenship is required.",
          "Eligible and able to obtain and maintain a government clearance",
          "To conform to export regulations, applicant must be a U.S. citizen, lawful permanent resident, protected individual as defined by 8 U.S.C.",
          "Applicants requiring future sponsorship for continued work authorization (for example, following F-1 OPT/CPT, or J-1 status) are not eligible.",
          "Applicants must be legally authorized to work in the United States for any employer without requiring current or future visa sponsorship (for example, employment-based visas such as H-1B, F-1/OPT, or similar)",
          "Ability to obtain a security clearance",
          # KBR Defense & Intel (Dheepak's strict list, 2026-11-14)
          "Security Requirements: Security Clearance: Ability to obtain a U.S. Government Secret Security Clearance.",
          # Sanofi co-op (Dheepak caught this one, 2026-11-14)
          "Students currently on CPT, OPT, or STEM OPT usually require future sponsorship for long term employment "
          "and do not meet the Internal requirements for this program unless eligible for an alternative long-term status.",
          "Must be permanently authorized to work in the U.S. and not require sponsorship of an employment visa "
          "(e.g., H-1B or green card) at the time of application or in the future."]
    for s in no:
        assert _e(s)["auth"] == "no", s
    cpt = ["Work authorization is required on the first day of employment including F-1 students using CPT, OPT or STEM OPT.",
           "F-1 students are eligible if they are using Curricular Practical Training (CPT)."]
    for s in cpt:
        assert _e(s)["auth"] == "cpt", s
    check = ["Applicants must be authorized to work for any employer in the U.S.",
             "We are unable to sponsor or take over sponsorship of an employment Visa at this time.",
             "Applicants must be currently authorized to work in the United States without the need for employer sponsorship now or in the future.",
             "This position is not eligible for sponsorship.",
             "Candidates must not require sponsorship of an employment visa at the time of application or in the future."]
    for s in check:  # recognised as "no sponsorship", but that passes: CPT/OPT is work authorization
        assert _e(s)["labels"]["sponsorship"] == "no_sponsorship" and _e(s)["auth"] == "ok", s
    assert _e("Comply with U.S. export laws without sponsorship for an export license.")["auth"] == "ok"
    assert _e("We consider applicants regardless of race, citizenship, or veteran status.")["auth"] == "ok"
    assert _e("Interns will not be eligible for company-sponsored benefits such as the 401(k).")["auth"] == "ok"


def test_elig_degree():
    ok = ["Currently pursuing a BS, MS, or PhD in Robotics, Computer Science, AI/ML",
          "Currently pursuing a Bachelor's or Master's degree in Mechanical Engineering",
          "Rising junior, senior, or graduate-level student preferred.",
          "All education levels (Bachelor's, Master's, or Ph.D.) welcome",
          "Completion of at least two years of an undergraduate or graduate degree program in a quantitative field",
          "Currently pursuing a BS or MS in Computer Engineering",
          "Currently pursuing a B.S. or higher in Computer Science or a related technical field",   # Ramp, live audit
          "Open to college sophomore, junior, senior, recent grad, or 1st or 2nd master's student"]  # Envita
    for s in ok:
        assert _e(s)["degree"] == "ok", s
    no = ["Currently pursuing a Bachelor's degree in Computer Science, Software Engineering, or a closely related field.",
          "Rising junior or senior standing.",
          "Currently enrolled in an Undergraduate degree program majoring in Engineering",
          "Currently enrolled in a Bachelor's program in Mechanical Engineering",
          "Must be enrolled in a PhD program",
          "Job Qualifications Undergraduate student pursuing a STEM major, with a 3.5 GPA or above",   # Encore, 2026-11-14
          "Job Qualifications Bachelor's degree in process Experience programming with C#"]
    for s in no:
        assert _e(s)["degree"] == "no", s
    assert _e("Proficient in MS Excel and PowerPoint.")["degree"] == "unknown"  # MS Office is not a degree
    assert _e("Bachelor's degree preferred, any major considered.")["degree"] == "check"


def test_elig_grad_window():
    assert elig.GRAD == (2028, 5)
    ok = ["Undergraduate or master's students graduating between December 2027 and June 2028",
          "Expected to graduate by Summer 2028", "graduating in Fall 2027 or later",
          "Graduating in Summer/Fall/Winter 2027 or Spring/Summer 2028."]
    for s in ok:
        assert _e(s)["grad"] == "ok", s
    no = ["Graduating class of 2029 pursuing a Masters or Doctorate degree",
          "on track to graduate in December 2028 or Spring 2029.",
          "Recent college or university graduates from Spring/Summer/Fall/Winter 2026 with a relevant degree.",
          "Expected Date of Graduation – May 2026"]
    for s in no:
        assert _e(s)["grad"] == "no", s
    assert _e("Opportunity to transition into a full-time role upon graduation in 2027.")["grad"] is None
    # real page text (menu, no punctuation): the 2027 in the job title is not a graduation date
    menu = ("Molex Product Management Internship 2027 - Aerospace Solutions - Georgetown, Texas - Koch Skip to content "
            "Home Search Jobs Life at Koch Early Careers Recruiting Events Candidate Portal Join our Talent Community "
            "Employee Login Benefits Our Culture Students and recent graduates explore roles")
    assert _e(menu)["grad"] is None
    # ServiceNow (live audit): the same line twice, once with "or later" -> fits
    sn = ("Pursuing a Masters degree in Computer Science and graduating December 2027, or later. "
          "Requirements: Master's degree in Computer Science and graduating in December 2027.")
    assert _e(sn)["grad"] == "ok"


def test_elig_list_and_combine():
    assert elig.from_list("U.S. Citizenship is Required", ["Bachelor's"])["auth"] == "no"
    assert elig.from_list("Other", ["Bachelor's", "Master's"])["degree"] == "ok"
    assert elig.from_list("Other", ["Bachelor's"])["degree"] == "check"   # list degrees are only a hint
    jd = elig.check("Currently pursuing a Bachelor's or Master's degree. F-1 students using CPT are welcome.")
    lst = elig.from_list("Other", ["Bachelor's"])
    both = elig.with_list(jd, lst)
    assert both["auth"] == "cpt" and both["degree"] == "ok"           # the JD beats the list's hint
    assert elig.with_list(elig.check(None), lst)["degree"] == "check"  # no JD -> list hint
    assert elig.with_list(jd, elig.from_list("U.S. Citizenship is Required", None))["auth"] == "no"
    assert elig.hidden(elig.combine(jd, elig.check("US Citizenship is required.")))
    assert elig.check(None)["auth"] == "unknown"


def test_build_hides_ineligible_and_tags_the_rest(tmp_path):
    now = 2_000_000_000
    def bj(title, jd, url):
        return {"source": "Greenhouse", "company": "Acme", "title": title, "url": url, "locations": ["Boston, MA"],
                "terms": [], "posted": now - 3600, "field": None, "sponsorship": None,
                "jd_terms": tech.jd_terms(jd), "elig": elig.check(jd)}
    board = [bj("Software Engineer Intern", "US Citizenship is required.", "https://g/1"),
             bj("Data Science Intern", "Currently pursuing a Bachelor's degree in Statistics.", "https://g/2"),
             bj("ML Engineer Co-op", "Pursuing a BS or MS. F-1 students using CPT are welcome.", "https://g/3"),
             bj("AI Engineer Intern", "Must be authorized to work for any employer in the U.S.", "https://g/4")]
    old = [_row("Acme", "Old Intern", now - 60 * 86400, url="https://boards.greenhouse.io/acme/jobs/0")]
    meta = build.run(str(tmp_path / "s"), str(tmp_path / "o"), fetch=_fake_fetch(now, old, []), poll="yes",
                     poll_fn=lambda b: (list(board), {"Greenhouse": {"ok": True}}), now=now)
    jobs = {j["title"]: j for j in _matches(tmp_path / "o")}
    assert set(jobs) == {"ML Engineer Co-op", "AI Engineer Intern"}
    assert jobs["ML Engineer Co-op"]["elig"]["auth"] == "cpt" and jobs["ML Engineer Co-op"]["elig"]["degree"] == "ok"
    assert jobs["AI Engineer Intern"]["elig"]["auth"] == "ok"   # "any employer / no sponsorship": CPT/OPT is enough
    assert meta["ineligible_skipped"] == 2 and meta["check_count"] == 0


def test_jd_cache_skips_second_fetch(monkeypatch):
    calls = []

    def fake(url, body=None, **k):
        if body is None:
            calls.append(url)
            return {"jobPostingInfo": {"jobDescription": "Pursuing a BS or MS. Python, SQL, machine learning, PyTorch."}}
        if body["limit"] == 1:
            return {"facets": []}
        return {"jobPostings": [{"title": "Data Intern", "externalPath": "/a", "locationsText": "Austin, TX",
                                 "postedOn": "Posted Today"}], "total": 1}
    monkeypatch.setattr(ats, "get_json", fake)
    j1 = [j for j in ats.fetch_workday("t.wd1.myworkdayjobs.com|t|S", "T") if j][0]
    assert len(calls) == 1 and j1["elig"]["degree"] == "ok"
    ats.KNOWN = {j1["url"]: {"elig": j1["elig"], "jd_terms": j1["jd_terms"]}}
    j2 = [j for j in ats.fetch_workday("t.wd1.myworkdayjobs.com|t|S", "T") if j][0]
    assert len(calls) == 1 and j2["elig"] == j1["elig"]   # served from the cache
    ats.KNOWN = {}


def test_failed_board_keeps_last_jobs(tmp_path, monkeypatch):
    now = 2_000_000_000
    old = [_row("Acme", "Old Intern", now - 60 * 86400, url="https://boards.greenhouse.io/acme/jobs/0")]
    job = {"source": "Greenhouse", "company": "Acme", "title": "Data Science Intern", "url": "https://g/1",
           "locations": ["Boston, MA"], "terms": [], "posted": now - 3600, "field": None, "sponsorship": None,
           "jd_terms": None, "elig": None}
    state = {"fail": False}

    def fake_fetch_gh(token, company):
        if state["fail"]:
            raise ats.urllib.error.HTTPError("u", 521, "down", None, None)
        return [dict(job)]
    monkeypatch.setitem(ats.FETCHERS, "gh", fake_fetch_gh)
    for k in [k for k in ats.FETCHERS if k != "gh"]:
        monkeypatch.setitem(ats.FETCHERS, k, lambda t, c: [])
    st, out = str(tmp_path / "s"), str(tmp_path / "o")
    build.run(st, out, fetch=_fake_fetch(now, old, []), poll="yes", poll_fn=ats.poll, now=now)
    state["fail"] = True
    meta = build.run(st, out, fetch=_fake_fetch(now, old, []), poll="yes", poll_fn=ats.poll, now=now + 600)
    titles = [j["title"] for j in json.load(open(tmp_path / "o" / "jobs.json"))["jobs"]]
    assert "Data Science Intern" in titles and meta["sources"]["Greenhouse"]["errors"] == {"HTTP 521": 1}


# ---- Phase 7: JD for list-only jobs -------------------------------------------------------------------------
def test_norm_url_apply_pages_and_locale():
    assert store.norm_url("https://jobs.ashbyhq.com/acme/1234/application?embed=true") == "https://jobs.ashbyhq.com/acme/1234"
    assert store.norm_url("https://acme.wd1.myworkdayjobs.com/en-US/Ext/job/X_1") == \
        store.norm_url("https://acme.wd1.myworkdayjobs.com/Ext/job/X_1")


def test_jdfetch_adapters_and_json_ld():
    pages = {
        "apply.workable.com/api/v2/accounts/acme/jobs/AB12": json.dumps(
            {"title": "x", "description": "<p>" + "Build machine learning systems in Python and PyTorch. " * 5 + "</p>",
             "location": {"countryCode": "US"}, "published": "2026-11-13T10:00:00Z"}),
        "bamboohr.com/careers/71/detail": json.dumps({"result": {"jobOpening": {"description": "Data pipelines in SQL. " * 20}}}),
        "careers.example.com/job/9": '<html><script type="application/ld+json">' + json.dumps(
            {"@type": "JobPosting", "description": "<p>" + "US citizenship is required. Software engineering in Java. " * 5 + "</p>",
             "datePosted": "2026-11-12", "jobLocation": {"address": {"addressCountry": "US"}}}) + "</script></html>",
        "plain.example.com/j/1": "<html><body>" + "We build backend services in Go and Python for our platform. " * 30 + "</body></html>",
        "tiny.example.com/j/1": "<html><body>Apply now</body></html>",
    }

    def get(url, body=None):
        return next(v for k, v in pages.items() if k in url)
    w = jdfetch.fetch("https://apply.workable.com/acme/j/AB12/apply", get=get)
    assert w["via"] == "workable" and w["country"] == "US" and "machine learning" in w["text"]
    assert jdfetch.fetch("https://acme.bamboohr.com/careers/71", get=get)["via"] == "bamboohr"
    ld = jdfetch.fetch("https://careers.example.com/job/9", get=get)
    assert ld["via"] == "json-ld" and "citizenship" in ld["text"] and ld["posted"]
    assert jdfetch.fetch("https://plain.example.com/j/1", get=get)["via"] == "page"
    assert jdfetch.fetch("https://tiny.example.com/j/1", get=get) is None
    assert jdfetch.fetch("https://go.vansh.nyc/abc", get=get) is None
    assert jdfetch.fetch("https://careers.example.com/job/9") is None  # offline -> None, no crash

    def broken(url, body=None):
        import http.client
        raise http.client.IncompleteRead(b"")
    assert jdfetch.fetch("https://lifeattiktok.com/search/1", get=broken) is None  # real crash seen on TikTok


def test_every_list_only_job_goes_through_all_checks(tmp_path):
    """End to end: jobs that only the lists know get their JD fetched and pass the same US + tech + eligibility checks."""
    now = 2_000_000_000
    def row(company, title, url, locs=("Boston, MA",)):
        r = _row(company, title, now - 3600, url=url)
        r["locations"] = list(locs)
        return r
    rows = [
        row("A", "Research Intern", "https://a.example.com/1"),            # non-tech title, ML JD      -> shown (JD)
        row("B", "Software Engineer Intern", "https://b.example.com/1"),   # citizenship in JD          -> hidden
        row("C", "Data Science Intern", "https://c.example.com/1"),        # undergrad-only JD          -> hidden
        row("D", "Machine Learning Intern", "https://d.example.com/1"),    # no JD anywhere             -> shown, unknown
        row("E", "Finance Intern", "https://e.example.com/1"),             # non-tech title, finance JD -> hidden
        row("F", "AI Engineer Intern", "https://f.example.com/1"),         # JD says country GB         -> hidden
        row("G", "Data Engineer Intern", "https://g.example.com/1", ("Toronto, ON, Canada",)),  # non-US list -> dropped
        row("H", "SWE Intern", "https://h.example.com/1"),                 # CPT welcome, MS ok         -> shown, tags
    ]
    jds = {
        "a.example": {"text": "Train deep learning models with PyTorch, Python and SQL for machine learning research."},
        "b.example": {"text": "US Citizenship is required. Python, Java, software engineering."},
        "c.example": {"text": "Currently pursuing a Bachelor's degree in Statistics. Python, SQL, data science."},
        "e.example": {"text": "Excel, financial models, accounting."},
        "f.example": {"text": "Build LLM agents in Python.", "country": "GB"},
        "h.example": {"text": "Pursuing a BS or MS in CS. F-1 students using CPT are welcome. Python, backend."},
    }
    calls = []

    def fake_jd(url):
        calls.append(url)
        for k, v in jds.items():
            if k in url:
                return {"via": "test", "posted": None, "country": None, **v}
        return None
    st, out = str(tmp_path / "s"), str(tmp_path / "o")
    meta = build.run(st, out, fetch=_fake_fetch(now, rows, []), poll="no", jd_fetch=fake_jd, now=now)
    jobs = {j["company"]: j for j in _matches(tmp_path / "o")}
    assert set(jobs) == {"A", "D", "H"}, set(jobs)
    assert jobs["A"]["why"] == "JD" and jobs["A"]["field"] == "AI/ML"
    assert jobs["D"]["elig"]["auth"] == "unknown"
    assert jobs["H"]["elig"]["auth"] == "cpt" and jobs["H"]["elig"]["degree"] == "ok"
    assert meta["ineligible_skipped"] == 2 and meta["non_us_skipped"] == 1 and meta["not_tech_skipped"] == 1
    assert meta["jd"]["fetched"] == 6 and meta["jd"]["missed"] == 1
    assert "https://g.example.com/1" not in calls  # dropped as non-US before any fetch

    # Next run (30 min later): every JD comes from the cache; the miss is not retried yet.
    calls.clear()
    build.run(st, out, fetch=_fake_fetch(now, rows, []), poll="no", jd_fetch=fake_jd, now=now + 1800)
    assert calls == []
    assert set(j["company"] for j in _matches(tmp_path / "o")) == {"A", "D", "H"}


def test_poll_jd_results_are_cached_in_state(tmp_path, monkeypatch):
    now = 2_000_000_000
    old = [_row("Acme", "Old Intern", now - 60 * 86400, url="https://boards.greenhouse.io/acme/jobs/0")]
    job = {"source": "Greenhouse", "company": "Acme", "title": "Data Science Intern", "url": "https://g/1",
           "locations": ["Boston, MA"], "terms": [], "posted": now - 3600, "field": None, "sponsorship": None,
           "jd_terms": ["python"], "elig": elig.check("Pursuing a BS or MS.")}
    st = str(tmp_path / "s")
    build.run(st, str(tmp_path / "o"), fetch=_fake_fetch(now, old, []), poll="yes",
              poll_fn=lambda b, known=None: ([dict(job)], {"Greenhouse": {"ok": True}}), now=now)
    saved = store.load(st)["jd_cache"]["https://g/1"]
    assert saved["v"] == elig.RULES_VERSION and saved["elig"]["degree"] == "ok"


# ---- AI second reader (Groq) ------------------------------------------------------------------------------------
from radar import llm


def test_llm_verify_drops_made_up_quotes():
    ex = "Must be permanently authorized to work in the U.S. Pursuing a BS or MS in Computer Science."
    ans = {"citizenship": "not_required", "cpt": "rejected", "sponsorship": "no_sponsorship", "degree": "ms_ok",
           "grad": "misses",
           "quotes": {"cpt": "Must be permanently authorized to work in the U.S.",
                      "sponsorship": "We never sponsor visas.",          # not in the excerpt -> dropped
                      "degree": "pursuing a BS or MS in computer science",  # case/space differences are fine
                      "grad": ""}}                                       # no quote -> dropped
    lab, quo, unv = llm.verify(ans, ex)
    assert lab["cpt"] == "rejected" and lab["degree"] == "ms_ok"
    assert lab["sponsorship"] == "silent" and lab["grad"] == "not_stated"
    assert sorted(unv) == ["grad", "sponsorship"]
    bad = llm.verify({"citizenship": "maybe", "quotes": {}}, ex)[0]
    assert bad["citizenship"] == "not_required"  # invalid value -> neutral


def test_llm_ask_parses_and_handles_thinking_and_errors():
    ex = "Students currently on CPT or OPT do not meet the requirements for this program."
    def post(body, key):
        assert body["temperature"] == 0 and body["response_format"] == {"type": "json_object"}
        return {"choices": [{"message": {"content": '<think>hmm</think>{"r":[{"id":1,"citizenship":"not_required","cpt":"rejected",'
                                                    '"sponsorship":"silent","degree":"unclear","grad":"not_stated",'
                                                    '"q":{"cpt":"Students currently on CPT or OPT do not meet the requirements for this program."}}]}'}}]}
    r = llm.ask(ex, key="k", post=post)
    assert r["labels"]["cpt"] == "rejected" and r["unverified"] == []
    assert llm.ask(ex, key=None) is None or os.environ.get("GROQ_API_KEY")   # no key -> off
    def broken(body, key):
        raise llm.urllib.error.HTTPError("u", 400, "bad", None, None)
    assert llm.ask(ex, key="k", post=broken) is None


def test_merge_worse_verified_answer_wins():
    rules = elig.check("Pursuing a BS or MS in CS. Python.")
    ai = {"labels": {**elig.NEUTRAL, "cpt": "rejected", "degree": "ms_ok"},
          "quotes": {"cpt": "CPT students do not meet requirements", "degree": "Pursuing a BS or MS in CS."}}
    lab, quo, dis = llm.merge(rules, ai)
    assert lab["cpt"] == "rejected" and quo["cpt"].startswith("CPT") and dis == ["cpt"]  # readers disagreed; worse one wins
    assert elig.hidden(elig.from_labels(lab, quo))
    # The AI decides once it has read the JD (far fewer errors on the held-out set): a blocking answer only the
    # rules gave is kept as a disagreement -> the job shows as "needs a look" instead of being hidden.
    rules2 = elig.check("US Citizenship is required. Pursuing a BS or MS.")
    lab2, quo2, dis2 = llm.merge(rules2, {"labels": dict(elig.NEUTRAL, degree="ms_ok"), "quotes": {}})
    assert lab2["citizenship"] == "not_required" and dis2 == ["citizenship"] and "rules read" in quo2["citizenship"]
    e2 = {**elig.from_labels(lab2, quo2), "disagree": dis2}
    assert not elig.hidden(e2) and elig.needs_check(e2)


def test_pipeline_ai_catches_what_rules_miss(tmp_path):
    """Sanofi-style wording the rules don't know: the AI's verified quote hides the job; a made-up quote does not."""
    now = 2_000_000_000
    def bj(company, title, jd, url):
        return {"source": "Greenhouse", "company": company, "title": title, "url": url, "locations": ["Boston, MA"],
                "terms": [], "posted": now - 3600, "field": None, "sponsorship": None,
                "jd_terms": tech.jd_terms(jd), "elig": elig.check(jd), "excerpt": elig.excerpt(jd)}
    odd = "Pursuing a BS or MS in CS. Those holding student work permits tied to their studies cannot be placed in this program."
    board = [bj("A", "Software Engineer Intern", odd, "https://g/a"),
             bj("B", "Data Engineer Intern", "Pursuing a BS or MS in CS. Python and SQL.", "https://g/b")]
    def fake_ai(excerpt):
        if "cannot be placed" in excerpt:
            return {"labels": dict(elig.NEUTRAL, cpt="rejected", degree="ms_ok"),
                    "quotes": {"cpt": "Those holding student work permits tied to their studies cannot be placed in this program."},
                    "unverified": [], "model": "fake"}
        # hallucination already removed by verify(): only neutral answers survive
        return {"labels": dict(elig.NEUTRAL, degree="ms_ok"), "quotes": {}, "unverified": ["citizenship"], "model": "fake"}
    old = [_row("Z", "Old Intern", now - 60 * 86400, url="https://boards.greenhouse.io/z/jobs/0")]
    st = str(tmp_path / "s")
    meta = build.run(st, str(tmp_path / "o"), fetch=_fake_fetch(now, old, []), poll="yes",
                     poll_fn=lambda b: (list(board), {"Greenhouse": {"ok": True}}), now=now, ai=fake_ai)
    jobs = {j["company"]: j for j in _matches(tmp_path / "o")}
    assert set(jobs) == {"B"} and jobs["B"]["elig"]["ai"] == "read"
    assert meta["ai"]["answered"] == 2 and meta["ai"]["unverified"] == 1
    # cached: the next run asks nobody
    calls = []
    build.run(st, str(tmp_path / "o"), fetch=_fake_fetch(now, old, []), poll="no", now=now + 1800,
              ai=lambda e: calls.append(e))
    assert calls == []


def test_gold_dev_set_no_regressions():
    """Hand-labeled dev set (170 real JD excerpts, labeled by a reader who never saw the rules).
    The rules were tuned on it, so this guards against regressions; the honest number is the test set."""
    import subprocess, sys
    items = json.load(open(os.path.join(HERE, "gold", "dev.json")))
    fs = fh = 0
    for it in items:
        lab = elig.labels(it["excerpt"])[0]
        hid = elig.hidden(elig.from_labels(lab, {}))
        fs += it["verdict"] == "not_eligible" and not hid
        fh += it["verdict"] != "not_eligible" and hid
    assert fh == 0, f"{fh} eligible jobs would be hidden"
    assert fs <= 1, f"{fs} not-eligible jobs would be shown"



# ---- token management --------------------------------------------------------------------------------------------
def test_trim_keeps_important_sentences_first(monkeypatch):
    monkeypatch.setattr(llm, "PROFILE", "v2")
    ex = ("We love teamwork and great snacks. " * 30 + "US citizenship is required. Pursuing a BS or MS in CS. "
          "Graduating between Dec 2027 and Jun 2028.")
    t = llm.trim(ex, limit=200)
    assert "US citizenship is required." in t and "Pursuing a BS or MS in CS." in t and len(t) <= 200
    assert llm.key(ex) == llm.key(ex + " ")  and llm.key(ex) != llm.key("Pursuing a BS only.")


def test_ask_batch_one_request_for_many_and_budget(monkeypatch):
    monkeypatch.setattr(llm, "PROFILE", "v2")
    calls = []
    def post(body, key):
        calls.append(body)
        assert body["max_tokens"] == llm.OUT_BASE + llm.OUT_PER_ITEM * 3 and body["reasoning_effort"] == "low"
        assert body["messages"][1]["content"].count("[id ") == 3
        rows = [{"id": "a", "citizenship": "required", "cpt": "silent", "sponsorship": "silent", "degree": "unclear",
                 "grad": "not_stated", "q": {"citizenship": "US citizenship is required."}},
                {"id": "b", "degree": "ms_ok", "q": {"degree": "Pursuing a BS or MS."}},
                {"id": "zzz", "degree": "ug_only"}]  # unknown id is ignored; "c" missing -> not answered
        return ({"choices": [{"message": {"content": json.dumps({"r": rows})}}],
                 "usage": {"prompt_tokens": 900, "completion_tokens": 120}},
                {"x-ratelimit-remaining-tokens": "7000", "x-ratelimit-reset-tokens": "4.5s"})
    b = llm.Budget(run_tokens=5000, run_requests=5)
    got = llm.ask_batch([("a", "US citizenship is required."), ("b", "Pursuing a BS or MS."), ("c", "Python.")],
                        api_key="k", model="openai/gpt-oss-120b", budget=b, post=post)
    assert len(calls) == 1 and set(got) == {"a", "b"}
    assert got["a"]["labels"]["citizenship"] == "required" and got["b"]["labels"]["degree"] == "ms_ok"
    assert b.report()["prompt_tokens"] == 900 and b.remaining_tokens == 7000 and abs(b.reset_tokens - 4.5) < 1e-9
    assert not b.spent()
    b.prompt = 5000
    assert b.spent()


def test_budget_waits_for_the_minute_window(monkeypatch):
    slept = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: slept.append(s))
    b = llm.Budget()
    b.after({}, {"x-ratelimit-remaining-tokens": "300", "x-ratelimit-reset-tokens": "12.2s"})
    b.before(need=1500)
    assert slept and 12 < slept[0] < 13.5 and b.waited > 12
    assert llm._seconds("1m2.5s") == 62.5 and llm._seconds("150ms") == 0.15


def test_pipeline_dedupes_skips_and_defers(tmp_path, monkeypatch):
    """Same excerpt on 3 jobs -> one AI answer; no eligibility words -> no call; over budget -> pending."""
    now = 2_000_000_000
    def bj(company, title, jd, url):
        return {"source": "Greenhouse", "company": company, "title": title, "url": url, "locations": ["Boston, MA"],
                "terms": [], "posted": now - 3600, "field": None, "sponsorship": None,
                "jd_terms": tech.jd_terms(jd), "elig": elig.check(jd), "excerpt": elig.excerpt(jd)}
    same = "Pursuing a BS or MS in Computer Science. Must be authorized to work in the US."
    board = [bj("A", "Software Engineer Intern", same, "https://g/1"),
             bj("A", "Data Engineer Intern", same, "https://g/2"),
             bj("A", "ML Engineer Intern", same, "https://g/3"),
             bj("B", "AI Engineer Intern", "We build robots with Python and C++ in a fun team.", "https://g/4")]
    asked = []
    def fake_ai(ex):
        asked.append(ex)
        return {"labels": dict(elig.NEUTRAL, degree="ms_ok"), "quotes": {"degree": "Pursuing a BS or MS in Computer Science."},
                "unverified": [], "model": "fake"}
    old = [_row("Z", "Old Intern", now - 60 * 86400, url="https://boards.greenhouse.io/z/jobs/0")]
    meta = build.run(str(tmp_path / "s"), str(tmp_path / "o"), fetch=_fake_fetch(now, old, []), poll="yes",
                     poll_fn=lambda b: (list(board), {"Greenhouse": {"ok": True}}), now=now, ai=fake_ai)
    assert len(asked) == 1                      # three jobs, one excerpt, one answer
    assert meta["ai"]["no_signal"] == 1         # "robots with Python and C++": no eligibility words, no call
    jobs = json.load(open(tmp_path / "o" / "jobs.json"))["jobs"]
    assert sum(j["elig"]["ai"] == "read" for j in jobs) >= 3

    # budget of 0 requests: everything new waits for the next run
    monkeypatch.setattr(build, "MAX_AI_PER_RUN", 0)
    board2 = [bj("C", "Software Engineer Intern", "Pursuing a Bachelor's or Master's degree. Visa sponsorship is not available.", "https://g/9")]
    meta2 = build.run(str(tmp_path / "s2"), str(tmp_path / "o2"), fetch=_fake_fetch(now, old, []), poll="yes",
                      poll_fn=lambda b: (list(board2), {"Greenhouse": {"ok": True}}), now=now, ai=fake_ai)
    assert meta2["ai"]["pending"] == 1
    j9 = json.load(open(tmp_path / "o2" / "jobs.json"))["jobs"][0]
    assert j9["elig"]["ai"] == "pending"


def test_strict_shows_only_jobs_that_pass_every_check(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "STRICT", True)
    now = 2_000_000_000
    def bj(company, jd, url):
        return {"source": "Greenhouse", "company": company, "title": "Software Engineer Intern", "url": url,
                "locations": ["Boston, MA"], "terms": [], "posted": now - 3600, "field": None, "sponsorship": None,
                "jd_terms": tech.jd_terms(jd) if jd else None, "elig": elig.check(jd) if jd else None,
                "excerpt": elig.excerpt(jd) if jd else None}
    board = [bj("Clean", "Pursuing a BS or MS in CS. Python.", "https://g/1"),
             bj("NoSponsor", "Pursuing a BS or MS. We are unable to sponsor visas.", "https://g/2"),
             bj("Citizen", "US citizenship is required.", "https://g/3"),
             bj("NoJD", None, "https://g/4"),
             bj("Silent", "Python, SQL and a love of building things.", "https://g/5")]  # nothing stated -> passes
    old = [_row("Z", "Old Intern", now - 60 * 86400, url="https://boards.greenhouse.io/z/jobs/0")]
    meta = build.run(str(tmp_path / "s"), str(tmp_path / "o"), fetch=_fake_fetch(now, old, []), poll="yes",
                     poll_fn=lambda b: (list(board), {"Greenhouse": {"ok": True}}), now=now)
    shown = {j["company"] for j in _matches(tmp_path / "o")}
    assert shown == {"Clean", "Silent", "NoSponsor"}   # no sponsorship is fine with CPT/OPT (Dheepak, 2026-11-14)
    assert meta["ineligible_skipped"] == 1 and meta["review_skipped"] == 0 and meta["unverified_skipped"] == 1


def test_v1_profile_is_the_measured_setup_and_stops_at_daily_limit(monkeypatch):
    monkeypatch.setattr(llm, "PROFILE", "v1")
    body = llm._body([("x", "Pursuing a BS or MS. " * 200)], "openai/gpt-oss-120b")
    assert body["messages"][0]["content"] == llm.SYSTEM_V1 and "max_tokens" not in body and "reasoning_effort" not in body
    assert body["messages"][1]["content"].endswith("Pursuing a BS or MS. ")  # full excerpt, not trimmed
    import io
    def daily(body, key):
        raise llm.urllib.error.HTTPError("u", 429, "limit", {}, io.BytesIO(b'{"error":{"message":"... tokens per day (TPD) ..."}}'))
    b = llm.Budget()
    assert llm.ask_batch([("x", "US citizenship is required.")], api_key="k", budget=b, post=daily) == {}
    assert b.daily_limit_hit and b.spent()


def test_all_roles_view_keeps_every_us_role_with_a_reason(tmp_path):
    now = 2_000_000_000
    rows = [_row("A", "Software Engineer Intern", now - 3600),
            _row("B", "Finance Intern", now - 3600, url="https://x/b")]
    build.run(str(tmp_path / "s"), str(tmp_path / "o"), fetch=_fake_fetch(now, rows, []), poll="no", now=now)
    jobs = {j["company"]: j for j in json.load(open(tmp_path / "o" / "jobs.json"))["jobs"]}
    assert set(jobs) == {"A", "B"}
    assert jobs["B"]["match"] is False and "AI/ML, Data or Software" in jobs["B"]["why_not"]


@pytest.fixture(autouse=True)
def no_agent_overrides(monkeypatch):
    monkeypatch.setattr(build, "OVERRIDES", "/nonexistent/overrides.json")


def test_review_agent_overrides_are_final(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "STRICT", True)
    now = 2_000_000_000
    def bj(company, jd, url, title="Software Engineer Intern"):
        return {"source": "Greenhouse", "company": company, "title": title, "url": url,
                "locations": ["Boston, MA"], "terms": [], "posted": now - 3600, "field": None, "sponsorship": None,
                "jd_terms": tech.jd_terms(jd), "elig": elig.check(jd), "excerpt": elig.excerpt(jd)}
    neutral = dict(elig.NEUTRAL)
    board = [bj("Missed", "Pursuing a BS or MS in CS. Python.", "https://g/1"),       # rules: match
             bj("Hidden", "US citizenship is required.", "https://g/2"),               # rules: fails
             bj("NotTech", "Pursuing a BS or MS. Python.", "https://g/3", title="Marketing Intern"),
             bj("Untouched", "Pursuing a BS or MS in CS.", "https://g/4")]
    ov = {"https://g/1": {"us": True, "tech": "Software", "labels": {**neutral, "citizenship": "required"},
                          "quotes": {"citizenship": "Must hold an active Secret clearance."}},
          "https://g/2": {"us": True, "tech": "Software", "labels": neutral, "quotes": {}},
          "https://g/3": {"us": True, "tech": "AI/ML", "labels": {**neutral, "degree": "ms_ok"}, "quotes": {}}}
    old = [_row("Z", "Old Intern", now - 60 * 86400, url="https://boards.greenhouse.io/z/jobs/0")]
    build.run(str(tmp_path / "s"), str(tmp_path / "o"), fetch=_fake_fetch(now, old, []), poll="yes",
              poll_fn=lambda b: (list(board), {"Greenhouse": {"ok": True}}), now=now, overrides=ov)
    jobs = {j["company"]: j for j in json.load(open(tmp_path / "o" / "jobs.json"))["jobs"]}
    assert jobs["Missed"]["match"] is False and "citizenship" in jobs["Missed"]["why_not"]
    assert jobs["Hidden"]["match"] is True and jobs["Hidden"]["elig"]["ai"] == "agent"
    assert jobs["NotTech"]["match"] is True and jobs["NotTech"]["field"] == "AI/ML" and jobs["NotTech"]["why"] == "agent"
    assert jobs["Untouched"]["match"] is True and jobs["Untouched"]["elig"]["ai"] != "agent"


def test_agent_audit_checks_quotes_word_for_word():
    from scripts import agent_audit as A
    text = "We hire interns.  Applicants must be U.S. citizens. Python required."
    a = {"us": True, "tech": "Software", "citizenship": "required", "cpt": "silent", "sponsorship": "silent",
         "degree": "unclear", "grad": "not_stated", "confidence": 97,
         "quotes": {"citizenship": "Applicants must be U.S. citizens."}}
    ok = A.check_answer(a, text)
    assert ok["confidence"] == 97 and A.agent_verdict(ok) == (False, "fails: citizenship")
    made_up = A.check_answer({**a, "quotes": {"citizenship": "Only US citizens may apply."}}, text)
    assert made_up["confidence"] < A.MIN_CONF and made_up["unverified"] == ["citizenship"]
