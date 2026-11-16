"""Fetch the lists, poll the company boards, merge, update the seen store, write the site data.

python -m radar.build --state state/seen.json.gz --out site/data [--poll auto|yes|no]
"""
import argparse
import inspect
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from . import ats, classify, eligibility, jdfetch, llm, review, sources, store, tech

WINDOW_DAYS = 2          # the page shows 24 h or 2 days
REPOST_GAP = 7 * 86400   # first posted at least this long before it showed up again = a repost (own tab), not new
# Boards are polled every run: each board has its own schedule (ats.next_check), unchanged boards answer 304.
STRICT = True           # show only jobs that pass every check (no unverified, no "needs review")
MAX_JD_FETCH = 150       # list-only jobs whose JD we fetch per run (the rest next run; results are cached)
JD_RETRY = 6 * 3600      # a JD we couldn't get is tried again after this
RECHECK = 12 * 3600      # a list-only job's posting is read again after this, to see if it closed
MAX_AI_PER_RUN = int(os.environ.get("RADAR_AI_REQUESTS", "20"))  # AI requests per run (each = up to llm.BATCH JDs)
ATS_SOURCES = set(ats.ATS_NAMES.values())
# Corrections from the local review agent and Dheepak's own decisions: see radar/review.py for the contract.
OVERRIDES = review.OVERRIDES
DECISIONS = review.DECISIONS
NOT_TECH = "not an AI/ML, Data or Software role"


def load_overrides(path: str | None = None) -> dict:
    return review.load(path or OVERRIDES)


def _elig_base(j: dict, c: dict | None, ai_on: bool) -> dict:
    """The verdict from the rules, the list hint and the AI reader: everything except the agent and Dheepak."""
    lst = eligibility.from_list(j.get("sponsorship"), j.get("degrees")) if "Simplify" in j["sources"] else None
    rules = j.get("elig")
    extra = {k: rules[k] for k in ("residency", "tags") if rules and k in rules}
    if rules and rules.get("labels") and c:
        lab, quo, dis = llm.merge(rules, c)
        rules = {**eligibility.from_labels(lab, quo, source="ai+rules"), **extra, "disagree": dis, "ai": "read"}
    elif rules and rules.get("labels"):
        # re-derive the verdict from the five answers with today's policy (cached verdicts may be older)
        rules = {**rules, **eligibility.from_labels(rules["labels"], rules.get("quotes") or {}),
                 "ai": "pending" if ai_on else "off"}
    if rules and (rules.get("residency") or {}).get("block"):
        # US-residency length is arithmetic on a quoted sentence: final whatever the AI reader saw in its excerpt
        rules = eligibility.with_residency(rules)
        rules["disagree"] = [k for k in rules.get("disagree") or [] if k != "citizenship"]
    return eligibility.with_list(rules, lst)


def _reason(e: dict) -> str | None:
    """None = a match; else why not."""
    if eligibility.hidden(e):
        return "fails: " + ", ".join(k for k in ("citizenship", "cpt", "degree", "grad")
                                     if (e.get("labels") or {}).get(k) in ("required", "rejected", "ug_only",
                                                                            "phd_only", "misses")) or "fails a check"
    # Dheepak (2026-11-14): "My matches" lists only jobs that pass every check. No job description read ->
    # not a match; anything that needs a second look (bachelor's preferred, readers disagree) -> not a match.
    # "Not stated" (the JD sets no degree / graduation rule) is not a restriction, so it passes.
    if STRICT and not e.get("labels"):
        return "unverified: no job description"
    if STRICT and eligibility.needs_check(e):
        return "needs review: " + ", ".join(
            [k for k in ("auth", "degree") if e.get(k) == "check"] + (e.get("disagree") or []))
    return None


LIST_SOURCES = {"Simplify", "vanshb03"}
DAY_SLACK = 32 * 3600    # a day-only date (start of the day, US Pacific) covers up to this long after it


def posted_at(job: dict, rec: dict) -> int | None:
    """When the job became public, as far as we can tell.

    Native date if the source gives one; else our first_seen, unless that came from a bootstrap run.
    The earlier of the two wins, so a job a source dates in the past is never called new.
    One exception: a day-only date ("November 14") is the start of that day. If we first saw the job inside
    that day, our sighting is the tighter estimate (boards are polled every ~30 min), so it wins.
    """
    return dated(job, rec)[0]


def dated(job: dict, rec: dict) -> tuple[int | None, str | None]:
    """(posted, basis). basis = how we know: "board" (the company's job board gave the exact time),
    "day" (the board gave only the day), "list" (when a community list added it: an upper bound),
    "seen" (when the radar first saw it)."""
    native = job.get("posted")
    seen = None if rec.get("boot") else rec["first"]
    if native and job.get("posted_day") and seen and native <= seen <= native + DAY_SLACK:
        return seen, "seen"
    if native and (not seen or native <= seen):
        return native, job.get("posted_src") or ("day" if job.get("posted_day") else "board")
    if seen:
        return seen, "seen"
    return None, None


def merge(jobs_by_source: dict[str, list[dict]]) -> dict[str, dict]:
    """One row per job: same company+title, or same apply URL, across sources."""
    merged: dict[str, dict] = {}
    by_url: dict[str, str] = {}
    for src, jobs in jobs_by_source.items():
        for j in jobs:
            k = store.job_key(j["company"], j["title"])
            u = store.norm_url(j["url"]) if "go.vansh.nyc" not in j["url"] else None
            k = by_url.get(u, k) if u else k
            if j.get("posted") and not j.get("posted_src"):
                j["posted_src"] = "list" if src in LIST_SOURCES else ("day" if j.get("posted_day") else "board")
            dated_by_board = src not in sources.SOURCES and bool(j.get("posted"))
            if k not in merged:
                merged[k] = {**j, "sources": [src], "_board_dated": dated_by_board}
                if u:
                    by_url.setdefault(u, k)
                continue
            m = merged[k]
            m["_board_dated"] = m.get("_board_dated") or dated_by_board
            if src not in m["sources"]:
                m["sources"].append(src)
            m["locations"] = sorted(set(m["locations"]) | set(j["locations"]))
            m["terms"] = sorted(set(m["terms"]) | set(j["terms"]))
            if j.get("posted") and (not m.get("posted") or j["posted"] < m["posted"]):
                m["posted"], m["posted_src"], m["posted_day"] = j["posted"], j.get("posted_src"), j.get("posted_day")
            if "go.vansh.nyc" in m["url"] and "go.vansh.nyc" not in j["url"]:
                m["url"] = j["url"]
            m["field"] = m.get("field") or j.get("field")
            if j.get("jd_terms") is not None:
                m["jd_terms"] = sorted(set(m.get("jd_terms") or []) | set(j["jd_terms"]))
            if j.get("elig"):
                had = m.get("elig")
                m["elig"] = eligibility.combine(had, j["elig"])
                # combine() keeps only the verdict; the AI reader needs the five labels (+ quotes) to read a row.
                # Keep them from the stricter reading (Pacific Fusion HPC/DevOps, 2026-11-16: lost -> never read).
                if not m["elig"].get("labels"):
                    src = max((e for e in (had, j["elig"]) if e and e.get("labels")),
                              key=lambda e: eligibility.RANK.get(e.get("auth"), -1), default=None)
                    if src:
                        m["elig"] = {**m["elig"], "labels": src["labels"], "quotes": src.get("quotes") or {},
                                     "source": src.get("source", "rules")}
            if j.get("excerpt") and not m.get("excerpt"):
                # A list row (no JD) merged with its board row: keep the board's JD excerpt, or the AI reader and
                # Claude Code's jd_hash lose it and an ITAR block it found is dropped (Pacific Fusion, 2026-11-16).
                m["excerpt"] = j["excerpt"]
            if j.get("sponsorship") not in (None, "Other"):
                m["sponsorship"] = j["sponsorship"]
            m["degrees"] = sorted(set(m.get("degrees") or []) | set(j.get("degrees") or []))
            if u:
                by_url.setdefault(u, k)
    return merged


def apply_jd(j: dict, c: dict) -> None:
    j["elig"], j["jd_terms"], j["excerpt"] = c.get("elig"), c.get("jd_terms"), c.get("excerpt")
    if c.get("country") and not classify.is_us([], [c["country"]]):
        j["non_us"] = True


def run(state_path: str, out_dir: str, fetch=sources.fetch_json, resolve=sources.resolve_shortlink,
        poll: str = "auto", poll_fn=ats.poll, now: int | None = None, jd_fetch=None, audit: str | None = None,
        ai=None, overrides: dict | None = None, decisions: dict | None = None, agent_status: dict | None = None) -> dict:
    overrides = load_overrides() if overrides is None else overrides
    decisions = review.load(DECISIONS) if decisions is None else decisions
    agent_status = agent_status or {}
    jd_fetch = jd_fetch or jdfetch.fetch
    ai = ai or llm.ask
    now = now or int(time.time())
    state = store.load(state_path)
    state.setdefault("boards", {})
    boot = not state["jobs"]

    # 1. The community lists.
    raw, by_source, health = {}, {}, {}
    for name, (url, parse) in sources.SOURCES.items():
        try:
            raw[name] = fetch(url)
            jobs = [j for j in parse(raw[name]) if classify.is_us(j["locations"])]
            by_source[name] = jobs
            health[name] = {"ok": True, "rows": len(jobs)}
        except Exception as e:  # one broken source must not stop the others
            health[name] = {"ok": False, "rows": 0, "error": f"{type(e).__name__}: {str(e)[:200]}"}
            print(f"[warn] {name}: {e}", file=sys.stderr)

    # 1b. Resolve shortlinks of recent rows first (cached; each link fetched once), so the merge can match by URL.
    cut = now - WINDOW_DAYS * 86400
    recent = [j for jobs in by_source.values() for j in jobs
              if "go.vansh.nyc" in j["url"] and (j.get("posted") or now) >= cut]
    todo = sorted({j["url"] for j in recent if j["url"] not in state["links"]})
    with ThreadPoolExecutor(max_workers=16) as pool:
        for short, real in zip(todo, pool.map(resolve, todo)):
            if real != short:
                state["links"][short] = real
    for j in recent:
        j["url"] = state["links"].get(j["url"], j["url"])

    # 2. Learn company boards from every apply URL the lists have ever shown (active or not).
    pairs = [(x.get("company_name", ""), x.get("url", "")) for x in raw.get("Simplify", [])]
    pairs += [(x.get("company_name", ""), state["links"].get(x.get("url", ""), "")) for x in raw.get("vanshb03", [])]
    for bid, b in ats.discover(pairs).items():
        state["boards"].setdefault(bid, b)
    for bid, b in ats.FIXED_BOARDS.items():
        state["boards"].setdefault(bid, dict(b))

    # 3. Poll the boards that are due (each board has its own schedule); the rest answer from their cache.
    last = state.get("ats") or {}
    # Which board types have been polled before? A type's first poll only learns what exists (undated jobs
    # it finds are not "new"), the same way the very first run treats the lists.
    polled_before = set(state.get("ats_polled") or (last.get("health") or {}).keys())
    new_types: set[str] = set()
    do_poll = poll in ("yes", "auto")
    if do_poll and state["boards"]:
        state["jd_cache"] = jd_cache = {u: v for u, v in state.get("jd_cache", {}).items()
                                        if v.get("v") == eligibility.RULES_VERSION or v.get("miss")}
        params = inspect.signature(poll_fn).parameters  # test stubs take only the boards
        kw = {k: v for k, v in (("known", jd_cache), ("now", now), ("force", poll == "yes"),
                                ("window_start", now - WINDOW_DAYS * 86400)) if k in params}
        # A board that fails this time (outage, 429...) answers from its cached result inside ats.poll.
        got, ats_health = poll_fn(state["boards"], **kw)
        old_rows = [j for j in got if j.get("_old")]
        got = [j for j in got if not j.get("_old")]
        for j in got:
            if j.get("elig") is not None and j["url"] not in jd_cache:
                jd_cache[j["url"]] = {"t": now, "v": eligibility.RULES_VERSION, "elig": j["elig"],
                                      "jd_terms": j.get("jd_terms"), "excerpt": j.get("excerpt")}

        # Keep only jobs that can still be "new": undated ones (dated by first_seen) or dated inside the window.
        keep = [j for j in got if not j.get("posted") or j["posted"] >= now - WINDOW_DAYS * 86400]
        # The older ones still matter: a list may add the same job today (its date_posted = when the LIST added it).
        # Their own dates are kept by URL so the merge can date that list row correctly (KBR R2130067, 2026-11-15).
        # Sticky for 14 days after a board last showed the job: one empty or failed poll must not forget them.
        old_dates = {u: v for u, v in (last.get("old_dates") or {}).items()
                     if isinstance(v, list) and v[1] >= now - 14 * 86400}
        old_dates.update({store.norm_url(j["url"]): [j["posted"], now] for j in got + old_rows
                          if j.get("posted") and j["posted"] < now - WINDOW_DAYS * 86400})
        last = {"at": now, "jobs": [{k: v for k, v in j.items() if k != "countries" and not k.startswith("_")} for j in keep],
                "health": ats_health, "old_dates": old_dates}
        state["ats"] = last
        new_types = set(ats_health) - polled_before
        state["ats_polled"] = sorted(polled_before | set(ats_health))
    for name, h in (last.get("health") or {}).items():
        health[name] = {**h, "polled": last["at"]}
    for j in last.get("jobs", []):
        by_source.setdefault(j["source"], []).append(j)

    # 4. Merge, date, keep the last WINDOW_DAYS.
    merged = merge(by_source)
    old_dates = {u: (v[0] if isinstance(v, list) else v) for u, v in (last.get("old_dates") or {}).items()}
    agent_seen = agent_status.get("reviewed") or {}
    jd_cache = state.setdefault("jd_cache", {})
    in_window = []
    for k, j in merged.items():
        j["_key"] = k   # the merge key (company + title): survives URL changes between runs
        from_new_type = bool(new_types) and set(j["sources"]) <= new_types
        rec = store.mark_seen(state, k, now, boot or from_new_type)
        # A list's date is when the list added the job: only an upper bound. The job's own date (its board, or
        # the posting page read for its JD) wins when it is earlier.
        before, _ = dated(j, rec)   # what the list / our sighting alone would say
        seen_by_agent = agent_seen.get(k)   # the date Claude Code's browser saw on the posting (JS-only sites)
        own = [(d, src) for d, src in ((old_dates.get(store.norm_url(j["url"])), "board"),
                                       ((jd_cache.get(j["url"]) or {}).get("posted"), "page"),
                                       (seen_by_agent.get("posted") if isinstance(seen_by_agent, dict) else None, "page"))
               if d]
        j["_date_ok"] = bool(j.get("_board_dated") or own)
        if own and (not j.get("posted") or min(own)[0] < j["posted"]):
            j["posted"], j["posted_src"] = min(own)
            j["posted_day"] = False
        new_rec = rec["first"] == now and not rec.get("boot")
        t, basis = dated(j, rec)
        j["_basis"] = basis
        if new_rec and j.get("posted") and j.get("posted_src") == "board" and 0 <= now - j["posted"] <= 3 * 86400:
            # how long after the board published it did the radar first see it (exact board times only)
            rec["lag"] = now - j["posted"]
        if new_rec:
            rec["via"] = sorted(j["sources"])
        if t and t >= cut:
            in_window.append((j, t))
        elif t and before and before >= cut and before - t >= REPOST_GAP:
            # Reposted: a list (or our first sighting) shows it now, but the job itself is older. Not new: it goes
            # to its own "Reposted" tab, dated by when it showed up again, with its real date (Milliman, 2026-11-15).
            j["_reposted"] = t
            in_window.append((j, before))

    # 5. Jobs we only know from a list have no JD yet: fetch it from the apply page (cached per URL),
    #    so every job, whatever its source, goes through the same tech + eligibility checks.
    todo, recheck = [], []
    for j, _ in in_window:
        if j.get("elig") is not None or tech.TITLE_NOT.search(j["title"]):
            continue
        c = jd_cache.get(j["url"])
        if c and c.get("v") != eligibility.RULES_VERSION and not c.get("miss") and not c.get("closed"):
            c = None  # verdict from older rules: fetch the JD again
        if c and c.get("via") in ("ultipro", "lever") and not c.get("pv"):
            c = None  # read before the UltiPro / Lever readers took the posted date (2026-11-15)
        if c and c.get("closed") and now - c["t"] < JD_RETRY:
            j["_closed"] = True
        elif c and c.get("elig") is not None:
            apply_jd(j, c)
            if now - c["t"] >= RECHECK:
                recheck.append(j)
        elif not (c and (c.get("miss") or c.get("closed")) and now - c["t"] < JD_RETRY):
            todo.append(j)
    todo = (todo + recheck)[:MAX_JD_FETCH]   # new ones first; then open ones read 12+ h ago, in case they closed
    jd_stats = {"fetched": 0, "missed": 0, "cached": sum(1 for v in jd_cache.values() if v.get("elig") is not None)}
    with ThreadPoolExecutor(max_workers=8) as pool:
        def safe(x):
            try:
                return jd_fetch(x["url"])
            except Exception:
                return None
        for j, r in zip(todo, pool.map(safe, todo)):
            if r and r.get("closed"):
                # the posting page says it takes no more applications (a list can keep it for days)
                c = {"t": now, "closed": True, "posted": r.get("posted"), "pv": 1}
                j["_closed"] = True
                jd_stats["closed"] = jd_stats.get("closed", 0) + 1
            elif r and r.get("text"):
                c = {"t": now, "v": eligibility.RULES_VERSION, "elig": eligibility.check(r["text"]),
                     "jd_terms": tech.jd_terms(r["text"]), "excerpt": eligibility.excerpt(r["text"]),
                     "country": r.get("country"), "via": r.get("via"), "posted": r.get("posted"), "pv": 1}
                jd_stats["fetched"] += 1
                apply_jd(j, c)
            elif j in recheck:
                continue          # couldn't read it again this time: keep what we had
            else:
                c = {"t": now, "miss": True}
                jd_stats["missed"] += 1
            jd_cache[j["url"]] = c
    state["jd_cache"] = {u: v for u, v in jd_cache.items() if v["t"] >= now - 10 * 86400}
    # The posting itself may say it is older than the list's date: re-date, and drop what left the window.
    for i, (j, t) in enumerate(in_window):
        p = (jd_cache.get(j["url"]) or {}).get("posted")
        if p:
            j["_date_ok"] = True
        if p and p < (j.get("_reposted") or t):
            j["_basis"] = "page"
            if j.get("_reposted") or (p < cut and t - p >= REPOST_GAP):
                j["_reposted"] = p          # a repost: keeps the date it showed up again, carries its real date
            else:
                j["posted"] = p
                in_window[i] = (j, p)
    in_window = [(j, t) for j, t in in_window if t >= cut]   # late finds that are already older than the window

    # 6. The same checks for every job: US, tech role, eligibility (rules, then the AI reader as a second reader).
    #    6a: US + tech, and the rules' reading.
    cands, audit_rows, others = [], [], []
    skipped_not_tech = skipped_inelig = skipped_non_us = skipped_unverified = skipped_review = skipped_closed = 0
    ov_stale = 0
    for j, t in in_window:
        d = {"company": j["company"], "title": j["title"], "url": j["url"], "locations": j["locations"][:3],
             "sources": j["sources"], "jd_terms": (j.get("jd_terms") or [])[:8]}
        audit_rows.append(d)
        if not j["url"].lower().startswith(("https://", "http://")):
            d["result"] = "hidden: not a web link"   # never publish javascript:/data: links from outside feeds
            continue
        j["_url_key"] = store.norm_url(j["url"])     # older corrections / decisions were keyed by URL
        j["_h"] = review.jd_hash(j.get("excerpt"))
        ov = review.lookup(overrides, j)
        if ov and not review.override_applies(ov, j["_h"]):
            j["_stale"], ov = True, None   # the agent read an older description: re-read, don't apply
            ov_stale += 1
        if ov:
            j["_agent"] = ov
        if j.get("_closed"):
            skipped_closed += 1
            d["result"] = "hidden: posting closed"
            continue
        if j.get("non_us"):
            skipped_non_us += 1
            d["result"] = "hidden: not US (JD country)"
            continue
        j["_tc_base"] = tc = tech.classify(j["title"], j.get("field"), j.get("jd_terms"))
        if ov and "tech" in ov:
            tc = (ov["tech"], "agent") if ov["tech"] else None
        if ov and ov.get("us") is False:
            # hidden by the agent, but kept in the review queue so its correction stays tied to the job
            skipped_non_us += 1
            d["result"] = "hidden: not US (review agent)"
            j["_row_hidden"] = True
            others.append((j, t))
            continue
        if not tc:
            skipped_not_tech += 1
            d["result"] = "all roles only: not tech"
            others.append((j, t))
            continue
        cands.append((j, t, tc, d))

    #    6b: the AI reader, token-managed: one answer per distinct excerpt (cached by its hash), no call for
    #        excerpts without eligibility words, batches of llm.BATCH, newest / co-op / Boston first, and a
    #        per-run token + request budget (the rest waits for the next run).
    ai_cache = state.setdefault("ai_cache", {})
    # The AI reader runs only when switched on (RADAR_AI=on) and a key is set. Off by default: the token-light
    # cascade is not validated yet and the free daily quota is small (see docs/eligibility.md).
    ai_on = ai is not llm.ask or (os.environ.get("RADAR_AI") == "on" and bool(os.environ.get("GROQ_API_KEY")))
    budget = llm.Budget(run_tokens=int(os.environ.get("RADAR_AI_RUN_TOKENS", "60000")), run_requests=MAX_AI_PER_RUN)
    ai_stats = {"answered": 0, "from_cache": 0, "no_signal": 0, "unverified": 0, "pending": 0}
    todo = {}
    for j, t, tc, d in cands:
        ex = j.get("excerpt")
        if not (j.get("elig") or {}).get("labels") or ex is None:
            continue
        h = llm.key(ex)
        j["_ai_key"] = h
        if h in ai_cache:
            ai_stats["from_cache"] += 1
        elif (ns := llm.neutral_if_no_signal(ex)):
            ai_cache[h] = {"t": now, **ns}
            ai_stats["no_signal"] += 1
        elif h not in todo:
            boston = "Boston/MA" in classify.places(j["locations"])
            coop = classify.kind(j["title"], j["terms"]) == "co-op"
            todo[h] = ((-t, not coop, not boston), ex)
    queue = sorted(todo.items(), key=lambda kv: kv[1][0])
    if ai_on:
        for i in range(0, len(queue), llm.BATCH):
            if budget.spent():
                break
            batch = [(h, ex) for h, (_, ex) in queue[i:i + llm.BATCH]]
            if ai is llm.ask:
                got = llm.ask_batch(batch, budget=budget)
            else:  # test reader: one excerpt at a time
                got = {h: r for h, ex in batch if (r := ai(ex))}
            for h, res in got.items():
                ai_cache[h] = {"t": now, **res}
                ai_stats["answered"] += 1
                ai_stats["unverified"] += len(res.get("unverified") or [])
    ai_stats["pending"] = sum(1 for h in todo if h not in ai_cache)
    state["ai_cache"] = {h: v for h, v in ai_cache.items() if v["t"] >= now - 14 * 86400}

    #    6c: final decision per job: the base verdict (rules + list + AI reader), then the agent's correction.
    picked = []
    for j, t, tc, d in cands:
        c = ai_cache.get(j.get("_ai_key")) if j.get("_ai_key") else None
        j["_ai_read"] = bool(c)
        e_base = _elig_base(j, c, ai_on)
        j["_base"] = (e_base, _reason(e_base) if j["_tc_base"] else NOT_TECH)
        ov = j.get("_agent")
        if ov and ov.get("labels"):
            # the review agent read the whole description: its reading is final (no list hint on top)
            e = {**eligibility.from_labels(ov["labels"], ov.get("quotes") or {}, source="agent"),
                 "disagree": [], "ai": "agent"}
        else:
            e = e_base
        d["elig"], d["field"] = e, tc
        reason = _reason(e)
        d["result"] = "shown" if not reason else "all roles only: " + reason
        picked.append((j, t, tc, e, reason))
    if audit:  # every decision with its reason, for checking the filters by hand (not published)
        with open(audit, "w") as f:
            json.dump(audit_rows, f, indent=1)

    rows = []
    for j, t in others:  # non-tech roles: "All roles" view only, rules' reading for information, no AI
        e = j.get("elig") or eligibility.check(None)
        if e.get("labels"):
            e = {**e, **eligibility.from_labels(e["labels"], e.get("quotes") or {})}
        f = classify.field(j["title"], None)
        f = {"AI/ML/Data": "Other", "Software": "Other"}.get(f, f)  # tech fields belong to matched roles only
        if j.get("_tc_base"):   # a tech role the agent moved out: its base verdict is the eligibility one
            e_base = _elig_base(j, ai_cache.get(j.get("_ai_key")) if j.get("_ai_key") else None, ai_on)
            j["_base"] = (e_base, _reason(e_base))
        else:
            j["_base"] = (e, NOT_TECH)
        reason = "not US (review agent)" if j.get("_row_hidden") else NOT_TECH
        picked.append((j, t, (f, "title"), e, reason))
    reviewed = agent_status.get("reviewed") or {}
    tried = agent_status.get("tried") or {}
    for j, t, (fld, why), e, reason in picked:
        e_base, base_reason = j["_base"]
        by = "agent" if j.get("_agent") else None
        dec = review.lookup(decisions, j)
        if dec and isinstance(dec.get("show"), bool):   # Dheepak's call is final, even over an agent's "not US"
            by = "you"
            reason = None if dec["show"] else "hidden by you" + (f": {dec['note']}" if dec.get("note") else "")
            j["_row_hidden"] = False
        cc = review.cc_state(by, review.lookup(reviewed, j), review.lookup(tried, j), j["_h"])
        if reason is not None and reason.startswith("fails"):
            skipped_inelig += 1
        elif reason is not None and reason.startswith("unverified"):
            skipped_unverified += 1
        elif reason is not None and reason.startswith("needs review"):
            skipped_review += 1
        p, pwhy = review.priority(base_reason, e_base, j.get("excerpt"), bool(j.get("_stale")))
        rows.append({
            "key": j["_key"],
            "jd_hash": j["_h"],
            "base": {"match": base_reason is None, "why_not": base_reason},
            "by": by,
            "checked": cc["state"] in ("agree", "corrected"),
            "cc": cc,     # what Claude Code made of it: agree / corrected / pending / unsure / tried / None
            "cc_tried": cc.get("tried"),
            "review": {"p": p, "why": pwhy},
            # who has read this job: the rules, the AI reader (Groq), the review agent (Claude Code, see "checked"/"by")
            "readers": {"rules": bool((j.get("elig") or {}).get("labels")), "ai": bool(j.get("_ai_read"))},
            "hide_row": bool(j.get("_row_hidden")),
            "match": reason is None,
            "why_not": reason,
            "company": j["company"],
            "title": j["title"],
            "url": j["url"],
            "locations": j["locations"][:6],
            "more_locations": max(0, len(j["locations"]) - 6),
            "terms": j["terms"],
            "kind": classify.kind(j["title"], j["terms"]),
            "field": fld,
            "why": why,
            "jd_terms": sorted(j.get("jd_terms") or [], key=lambda x: x not in tech.JD_CORE)[:6] if why == "JD" else [],
            "places": classify.places(j["locations"]),
            "sponsorship": j.get("sponsorship") if j.get("sponsorship") not in (None, "Other") else None,
            "sources": j["sources"],
            "posted": t,
            "reposted": j.get("_reposted"),   # its real (older) date when it is a repost, else None
            # a company board or the posting itself gave a date; False = only a list's date / our first sighting
            "date_confirmed": bool(j.get("_date_ok")),
            "posted_basis": j.get("_basis"),
            "elig": {"auth": e["auth"], "degree": e["degree"], "grad": e.get("grad"), "notes": e.get("notes") or [],
                     "labels": e.get("labels"), "quotes": e.get("quotes") or {}, "disagree": e.get("disagree") or [],
                     "ai": e.get("ai") or ("no JD" if e["auth"] == "unknown" else "off"),
                     "residency": e.get("residency") or (j.get("elig") or {}).get("residency"),
                     "tags": e.get("tags") or (j.get("elig") or {}).get("tags") or []},
        })

    rows.sort(key=lambda r: r["posted"], reverse=True)
    queue = review.queue_rows(rows)
    applied = sum(1 for r in rows if r["by"] == "agent")
    rows = [{k: v for k, v in r.items() if k not in ("review", "hide_row")} for r in rows if not r["hide_row"]]
    state["ai_cache"] = {u: v for u, v in ai_cache.items() if v["t"] >= now - 10 * 86400}
    store.prune(state, now)
    if len(state["links"]) > 8000:
        state["links"] = dict(list(state["links"].items())[-8000:])

    live = [b for b in state["boards"].values() if b.get("fails", 0) < 3]
    meta = {
        "generated": now,
        "window_days": WINDOW_DAYS,
        # fresh = first posted inside the window; reposted jobs are counted on their own
        "count_24h": sum(1 for r in rows if r["match"] and not r["reposted"] and r["posted"] >= now - 86400),
        "count": sum(1 for r in rows if r["match"] and not r["reposted"]),
        "count_all_24h": sum(1 for r in rows if not r["reposted"] and r["posted"] >= now - 86400),
        "count_all": sum(1 for r in rows if not r["reposted"]),
        "count_reposted": sum(1 for r in rows if r["reposted"]),
        "sources": health,
        "boards": {ats.ATS_NAMES[a]: sum(1 for b in live if b["ats"] == a) for a in ats.ATS_NAMES},
        "polled": last.get("at"),
        "speed": speed(state, now),
        "not_tech_skipped": skipped_not_tech,
        "ineligible_skipped": skipped_inelig,
        "unverified_skipped": skipped_unverified,
        "review_skipped": skipped_review,
        "non_us_skipped": skipped_non_us,
        "closed_skipped": skipped_closed,
        "jd": jd_stats,
        "ai": {**ai_stats, **budget.report(), "model": llm.MODEL, "on": ai_on},
        "check_count": sum(1 for r in rows if r["match"] and eligibility.needs_check(r["elig"])),
        "agent": review.agent_meta(agent_status, rows, overrides, applied, ov_stale, now),
        "decided_by_you": sum(1 for r in rows if r["by"] == "you"),
        "bootstrap": boot,
    }
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "jobs.json"), "w") as f:
        json.dump({"meta": meta, "jobs": rows}, f, separators=(",", ":"))
    with open(os.path.join(out_dir, "review-queue.json"), "w") as f:
        json.dump({"meta": {"generated": now, "version": review.QUEUE_VERSION, "count": len(queue),
                            "rules_version": eligibility.RULES_VERSION,
                            "overrides_sha": review.overrides_sha(overrides)}, "jobs": queue}, f, separators=(",", ":"))
    store.save(state, state_path)
    return meta


def speed(state: dict, now: int, days: int = 7) -> dict:
    """How fast the radar sees new jobs, over jobs it first saw in the last `days`:
    lag = first seen - the board's exact publish time; found_by = which sources had each new job first."""
    lags, by = [], {}
    for rec in state["jobs"].values():
        if rec.get("boot") or rec["first"] < now - days * 86400:
            continue
        if rec.get("lag") is not None:
            lags.append(rec["lag"])
        for s in rec.get("via") or []:
            by[s] = by.get(s, 0) + 1
    lags.sort()
    pct = lambda q: lags[min(len(lags) - 1, int(q * len(lags)))] if lags else None
    return {"days": days, "n": len(lags), "median_min": round(pct(0.5) / 60) if lags else None,
            "p90_min": round(pct(0.9) / 60) if lags else None, "found_by": by}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state/seen.json.gz")
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--poll", choices=["auto", "yes", "no"], default="auto")
    ap.add_argument("--audit", help="write every job's decision + reason to this JSON file")
    ap.add_argument("--agent-status", help="status.json from the agent-status branch (heartbeat + jobs read)")
    a = ap.parse_args()
    meta = run(a.state, a.out, poll=a.poll, audit=a.audit, agent_status=review.load(a.agent_status))
    print(json.dumps(meta, indent=1))
    if not any(s["ok"] for s in meta["sources"].values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
