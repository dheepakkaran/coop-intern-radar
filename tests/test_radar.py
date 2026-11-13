import re
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
