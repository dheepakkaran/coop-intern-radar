# Co-op Intern Radar

**Live page: https://dheepakkaran.github.io/coop-intern-radar/**

One page that shows every co-op and intern job **first seen in the last 24 hours** (3 and 7 days too),
refreshed every 30 minutes by GitHub Actions and served by GitHub Pages. $0 to run.

## How it works

```
every 30 min (GitHub Actions)
  SimplifyJobs + vanshb03 listings.json ──► merge same job across lists ──► seen store (data branch)
                                                                         └─► site/data/jobs.json ──► GitHub Pages
```

- **Sources (phase 1):** the public `listings.json` feeds of
  [SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships) and
  [vanshb03/Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships).
  vanshb03 shortlinks are resolved to the real apply page once and cached.
- **"New"** = the earlier of the list's posted date and the first time this radar saw the job.
  The first run marks everything it sees as already known, so old jobs never show up as new.
- **Co-op** = the title says co-op, or the term is Spring, Fall or Winter (off-season).
- **Seen store** = `seen.json.gz` on the `data` branch, kept as a single force-pushed commit so the repo does not grow.
- **Page:** filters for window, co-op/intern, field, Boston/MA, remote, term, search; Save / Applied / Skip marks
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
1. ✅ Lists → merge → page (this)
2. Poll company job boards directly (Greenhouse, Lever, Ashby, SmartRecruiters) for jobs the lists miss
3. Workday and Oracle Cloud boards
4. Alerts
5. Bigger company list

## Credits
Job data comes from the SimplifyJobs and vanshb03 community lists; all credit for the listings goes to them.
This project only links to the original postings.

MIT licence (code only).
