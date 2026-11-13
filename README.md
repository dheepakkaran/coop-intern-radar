# Co-op Intern Radar

**Live page: https://dheepakkaran.github.io/coop-intern-radar/**

One page that shows every **US tech** (AI/ML, Data, Software) co-op and intern job **first seen in the last 24 hours**
(or 2 days), refreshed by GitHub Actions and served by GitHub Pages. $0 to run.

## How it works

```
every 30 min   SimplifyJobs + vanshb03 listings.json ─┐
every ~3 h     company job boards (public ATS APIs) ───┼─► US only ─► merge same job ─► seen store (data branch)
               Greenhouse · Lever · Ashby · Workday ·  │                                └─► site/data/jobs.json ─► Pages
               SmartRecruiters · Oracle Cloud          ┘
```

- **Sources (phase 1):** the public `listings.json` feeds of
  [SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships) and
  [vanshb03/Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships).
  vanshb03 shortlinks are resolved to the real apply page once and cached.
- **Company boards (phases 2–3):** every Greenhouse, Lever, Ashby, SmartRecruiters, Workday and Oracle Cloud board
  those lists ever linked to (about 2,400 companies that hire interns) is polled through its public job-board API, about every 3 hours,
  a few requests at a time. Only co-op / intern titles are kept. This catches jobs before the lists add them,
  and co-ops the lists skip. A board that returns 404 three times in a row is dropped.
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
- **"New"** = the earlier of the list's posted date and the first time this radar saw the job.
  The first run marks everything it sees as already known, so old jobs never show up as new.
- **Co-op** = the title says co-op, or the term is Spring, Fall or Winter (off-season).
- **Seen store** = `seen.json.gz` on the `data` branch, kept as a single force-pushed commit so the repo does not grow.
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
