# Co-op Intern Radar

**Live page: https://dheepakkaran.github.io/coop-intern-radar/**

One page that shows every **US tech** (AI/ML, Data, Software) co-op and intern job **first seen in the last 24 hours**
(or 2 days), refreshed by GitHub Actions and served by GitHub Pages. $0 to run.

## How it works

```
every 30 min   SimplifyJobs + vanshb03 listings.json ─┐
every 30 min   company job boards that are due ────────┼─► US only ─► merge same job ─► seen store (data branch)
(tick.yml)     Greenhouse · Lever · Ashby · Workday ·  │                                └─► site/data/jobs.json ─► Pages
               SmartRecruiters · Oracle Cloud ·        │
               Amazon · Google · Apple                 ┘
```

- **Sources (phase 1):** the public `listings.json` feeds of
  [SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships) and
  [vanshb03/Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships).
  vanshb03 shortlinks are resolved to the real apply page once and cached.
- **Company boards (phases 2–3):** every Greenhouse, Lever, Ashby, SmartRecruiters, Workday and Oracle Cloud board
  those lists ever linked to (about 2,400 companies that hire interns) is polled through its public job-board API,
  a few requests at a time. Only co-op / intern titles are kept. This catches jobs before the lists add them,
  and co-ops the lists skip. A board that returns 404 three times in a row (~9 h apart) is dropped.
  - **Each board has its own schedule (2026-11-15, ideas from [job-os](https://github.com/hemnaath04/job-os)):**
    Greenhouse, Lever and Ashby are checked every run with the ETag from the last read (`If-None-Match`); an
    unchanged board answers an empty `304` (Greenhouse 485 boards: 176 s → 10 s). The others are checked every run
    while "hot" (an intern job posted in the last 14 days), else every ~3 h; boards that refuse (403/422) wait ~3 h,
    rate limits and server errors retry next run. Every board's last result is cached, so a board that fails keeps
    its jobs. A full read happens at least daily anyway, so rule changes reach unchanged boards.
  - **Reliable 30-minute clock:** GitHub's cron skipped most "every 30 min" runs (measured: one scheduled run per
    ~6.6 h), so `tick.yml` stays alive and starts `update` every 30 min, then hands over to the next tick.
  - **Big employers' own sites:** Amazon (`amazon.jobs` search JSON), Google Careers (first page only, as its
    robots.txt allows; dated by creation, not by Google's later edit date) and Apple (Students team).
  - **Workday:** the board's own filters do the work: its *Job Type* value for interns/co-ops (e.g. "Intern (Fixed Term)")
    plus its *Country* value for the United States. Boards without those filters fall back to a text search for
    "intern" / "co-op", and then the title and place must say intern/co-op and US. "Posted N Days Ago" is turned into a date.
  - **Oracle Cloud:** keyword search sorted newest first; paging stops once results are older than 10 days.
- **US only:** a job is kept if any of its places is in the US (state code or name, US city shorthand, or the
  country the ATS reports) or if no place says where it is (e.g. just "Remote"). Canada and other countries are dropped.
- **Tech only:** a job is shown when its **title** has a tech role word (AI, ML, LLM, inference, post-training, evals,
  forward deployed / FDE, agentic, computer vision, data, analytics, BI, software, SWE, developer, full stack, cloud,
  DevOps, SRE … ~150 patterns in `radar/tech.py`, built from 12,720 real intern titles plus new LLM-era titles), or,
  when the title has none, its **job description** has at least 3 strong tech terms including a core one
  (machine learning, PyTorch, LLM, software development, data engineering …). JDs come free with Greenhouse, Lever
  and Ashby; for Workday, SmartRecruiters and Oracle the radar fetches the JD only for recent non-tech titles.
  Rows that pass only through the JD are tagged "JD" with the terms found.
- **Eligibility (master's student on CPT, graduating May 2028):** from each job description, hide jobs that need
  US citizenship / a green card / a security clearance / ITAR "U.S. person" / more years of US residency than I have
  (in the US since January 2026; e.g. "three years of continuous residency in the U.S."), that say F-1/CPT/OPT students are not
  eligible, that are undergraduate-only or PhD-only, or whose graduation window leaves out May 2028. "No sponsorship
  now or in the future" is fine: an internship is authorized through CPT, so only an explicit F-1/CPT/OPT rejection
  blocks a job. A federal facility credential on its own (PIV / HSPD-12, national agency check, public trust) is a
  "Federal credential: check" tag, not a block. The page groups the results into four checks (Location, Role, Work authorization, Education), each
  with the posting's own sentence behind it. Rules in `radar/eligibility.py`, written from ~1,300 real JD sentences
  and tested on them.
- **Every job gets the same checks, whatever its source.** Jobs that only the lists know (company sites, iCIMS,
  UltiPro, Workable, BambooHR, Eightfold, Paylocity …) get their job description from the apply page: a known ATS
  API when there is one (`radar/jdfetch.py`), else the page's schema.org JobPosting (JSON-LD), else the page text.
  Results are cached per URL (and redone when the rules change), so each JD is fetched once. Then US → tech →
  eligibility, exactly like the board jobs. Pages that block robots (e.g. Meta) stay "Work auth: no JD".
- **Only clean matches (2026-11-14):** a job is published only when every check passes. Failed checks, "no
  sponsorship" / "bachelor's preferred" / readers that disagree ("needs review"), and postings whose description
  can't be read ("unverified") are all left out; the counts are in the page footer.
- **Second opinion from a local review agent (2026-11-14):** Claude Code on my laptop re-reads every job's full
  description and corrects the page when it is confident. Git is the message bus, so the laptop never has to be
  reachable and nothing is lost while it is off (`radar/review.py` has the contract):
  - cloud → laptop: `data/review-queue.json` on the page: each job's verdict before any correction, a priority
    (unsure rules first, then every match, then failed checks, then non-tech roles) and `jd_hash`, a fingerprint of
    the description the verdict read;
  - laptop → cloud: `agent/overrides.json` (a push rebuilds the page). Each correction is tied to its `jd_hash`;
    if the company edits the posting, the correction stops applying and the job is read again;
  - heartbeat: `status.json` on the `agent-status` branch, so the page shows when the agent last ran and marks jobs
    "Agent checked" / "Corrected by agent";
  - my own calls: `python scripts/decide.py list | show <n> | hide <n> | undo <n>` writes `agent/decisions.json`,
    final over everything. Order of authority: me > review agent > AI reader + rules > rules.
  The agent derives the corrections from scratch on every run (a pure function of the queue and what it has read),
  so a crash or a reboot mid-run can never leave a stale or missing correction. `scripts/agent_audit.py gold` turns
  its corrections into gold-set candidates, so the rules learn from them.
- **"New"** = the earliest of the job's own date (its company board, or the posting page: Workday startDate /
  "Posted N Days Ago", JSON-LD datePosted), the list's posted date and the first time this radar saw the job. A list's
  date is when the *list* added the job, so it only ever caps the age; a job the board says is 30+ days old never shows as new.
  The first run marks everything it sees as already known, so old jobs never show up as new.
  A day-only date (Workday, Amazon: "November 14") counts from the start of that day, unless the radar first saw the
  job within that day: then its own sighting is the tighter estimate. Each job says how its date is known: the
  board's exact time, the posting's date, the day only, a list's date, or "first seen by the radar".
- **Speed:** the footer shows how long after a board published a job the radar first saw it (median and 90th
  percentile over the last 7 days, exact board times only). Before the 30-minute clock it was a median of ~8 h.
- **Co-op** = the title says co-op, or the term is Spring, Fall or Winter (off-season).
- **Seen store** = `seen.json.gz` on the `data` branch, kept as a single force-pushed commit so the repo does not grow.
- **Home page:** a 24-hour radar. Each blip is a role first seen in the last 24 hours: the angle is how long ago
  (clockwise from "now"), the ring is how far it got (inner = passes every check, middle = other tech roles, outer =
  other co-ops and internships); the sweep lights them up, hover or tap for the job. The job list is `jobs.html`.
- **Page:** 24 h / 2 days, co-op/intern, field (AI/ML, Data, Software), Boston/MA, remote, term, search; Save / Applied / Skip marks
  stay in your browser (localStorage). A link bar opens LinkedIn and Indeed with the 24 h filter set, plus Handshake,
  NUworks, Jobright, Simplify, Wellfound, Runway, hiring.cafe and TrueUp. These sites are never scraped.

## Run locally

```bash
python -m radar.build --state state/seen.json.gz --out site/data
python -m http.server -d site 8000      # open http://localhost:8000
python -m pytest -q
```

Python 3.12, standard library only (pytest for tests).

## Roadmap
1. ✅ Lists → merge → page
2. ✅ Company job boards: Greenhouse, Lever, Ashby, SmartRecruiters
3. ✅ Workday and Oracle Cloud boards
4. Alerts
5. Bigger company list

## Credits
Job data comes from the SimplifyJobs and vanshb03 community lists and from companies' public job boards; credit for
the lists goes to their maintainers. Board API details follow the MIT-licensed
[kalil0321/ats-scrapers](https://github.com/kalil0321/ats-scrapers).
This project only links to the original postings.

MIT licence (code only).
