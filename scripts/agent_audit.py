"""Local review agent: Claude Code (headless, on Dheepak's laptop) re-checks every live job and fixes the live site.

One run (the Pet hub starts it every hour while the laptop is on, see pet/bots/radar-audit/):
  1. read the live page data (what the site shows right now)
  2. verify: corrections pushed earlier, are they live now? (reflected / not reflected yet / NOT reflected)
  3. review: jobs the agent has not read yet -> fetch the full description -> Claude reads it (all five checks
     plus US + tech role, with a word-for-word quote for every finding and a confidence)
  4. compare with the live verdict. Live wrong + agent confident -> agent/overrides.json, commit, push
     (the push rebuilds the site; radar/build.py treats the agent's reading as final)
  5. docs/agent-report.md (local): every mismatch, what was done, whether the live site shows it

python scripts/agent_audit.py run [--max 40] [--batch 8] [--dry]
python scripts/agent_audit.py report
"""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from radar import eligibility as E, jdfetch, store  # noqa: E402

LIVE = "https://dheepakkaran.github.io/coop-intern-radar/data/jobs.json"
DIR = os.path.join(ROOT, ".cache", "agent")
REVIEWS = os.path.join(DIR, "reviews.json")      # every job the agent read (local)
LAST = os.path.join(DIR, "last.json")            # last run summary (the Pet bot shows it)
OVERRIDES = os.path.join(ROOT, "agent", "overrides.json")   # corrections the live build applies (pushed)
REPORT = os.path.join(ROOT, "docs", "agent-report.md")
MIN_CONF = 85          # below this the agent's answer is listed for Dheepak, never pushed
TEXT_LIMIT = 9000      # characters of description per job
MODEL = os.environ.get("RADAR_AGENT_MODEL", "opus")
BLOCKING = {"citizenship": {"required"}, "cpt": {"rejected"}, "degree": {"ug_only", "phd_only", "ug_preferred"},
            "grad": {"misses"}, "sponsorship": set()}

SYSTEM = """You check internship / co-op job postings for one applicant. Read each posting in full and answer exactly.

Applicant: MS in Electrical & Computer Engineering student (Northeastern, Boston). F-1 student visa: can work through
CPT/OPT, is NOT a US citizen, has no green card, cannot get a security clearance, is not a "U.S. person" under ITAR/EAR.
Graduates May 2028. These are internships / co-ops: the university authorizes the work through CPT, so the employer
never has to sponsor a visa. "No sponsorship now or in future" is therefore FINE and must not lower your confidence.
The only work-authorization question is whether the employer accepts a CPT/OPT (F-1) student at all: a citizenship /
green card / clearance / U.S. person requirement, or an explicit "F-1 / CPT / OPT not eligible", blocks the job.

For each job answer:
- us: true if the job is in the United States (remote-US counts), false if it is only outside the US.
- tech: "AI/ML", "Data" or "Software" if the role's actual work is AI/ML (incl. AI in any industry: biotech AI,
  chip AI, inference, evals, agents), data (data science / engineering / analytics with real data work) or software
  engineering; "Borderline" for cybersecurity / SOC / GRC, firmware / FPGA / embedded / hardware design, and GIS
  roles (Dheepak has not decided on these yet); null for anything else (marketing, finance, mechanical, sales,
  operations, HR, ...).
- citizenship: "required" if US citizenship, permanent residency / green card, "U.S. person" status, or an active or
  obtainable security clearance is REQUIRED; "not_required" otherwise. A company that only says it MAY decline or
  that export rules MAY apply, without requiring it, is "not_required".
- cpt: "rejected" if F-1 / CPT / OPT students cannot apply, or "do not meet" the authorization requirement, or
  permanent (unrestricted, no-time-limit) work authorization is required; "welcome" if CPT/OPT/F-1 students are
  explicitly welcome; "silent" otherwise.
- sponsorship: "no_sponsorship" if they will not sponsor a visa now or in future; "sponsors" if they will; "silent".
- degree: "ms_ok" if master's / graduate students can apply (incl. "BS or higher", "currently enrolled in a degree
  program" with no level limit); "ug_only" if only undergraduates / bachelor's students; "phd_only" if only PhD;
  "ug_preferred" if bachelor's students are preferred but master's not excluded; "unclear" if not stated.
- grad: "fits" if a stated graduation window includes May 2028 (Feb-Aug 2028 counts); "misses" if a stated window
  excludes it; "not_stated" if none.
- quotes: for every answer that is not the neutral one (not_required, silent, unclear, not_stated are neutral), copy
  the ONE sentence from the posting that shows it, character for character. Never paraphrase.
- confidence: 0-100, how sure you are that ALL your answers are right.
- note: under 15 words, only if something is borderline.

Return JSON only: {"jobs":[{"id":"...","us":true,"tech":"Software","citizenship":"not_required","cpt":"silent",
"sponsorship":"silent","degree":"ms_ok","grad":"not_stated","quotes":{"degree":"..."},"confidence":95,"note":""}]}"""


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _norm(s: str) -> str:
    s = (s or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


def live_data() -> dict:
    req = urllib.request.Request(f"{LIVE}?t={int(time.time())}", headers={"User-Agent": "coop-intern-radar-agent"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def agent_verdict(a: dict) -> tuple[bool, str | None]:
    """(match, reason) from the agent's answers, with the same policy the site uses."""
    if not a["us"]:
        return False, "not US"
    if not a["tech"]:
        return False, "not an AI/ML, Data or Software role"
    lab = {k: a[k] for k in E.LABELS}
    e = E.from_labels(lab, a.get("quotes") or {})
    if E.hidden(e):
        return False, "fails: " + ", ".join(k for k in BLOCKING if lab[k] in BLOCKING[k])
    if E.needs_check(e):
        return False, "needs review: " + ", ".join(k for k in ("auth", "degree") if e.get(k) == "check")
    return True, None


def ask_claude(items: list[dict]) -> list[dict]:
    """items: [{id, company, title, locations, text}] -> the agent's answers (unchecked)."""
    blocks = [f"### id {it['id']}\nCompany: {it['company']}\nTitle: {it['title']}\nLocations: "
              f"{'; '.join(it['locations'][:5]) or 'not listed'}\nDescription:\n{it['text']}" for it in items]
    prompt = "Check these postings.\n\n" + "\n\n".join(blocks)
    cmd = ["claude", "-p", "--model", MODEL, "--tools", "", "--setting-sources", "", "--no-session-persistence",
           "--output-format", "json", "--system-prompt", SYSTEM]
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=900, cwd=DIR)
    if p.returncode != 0:
        raise RuntimeError(f"claude exited {p.returncode}: {(p.stderr or p.stdout)[-300:]}")
    out = json.loads(p.stdout)
    text = out.get("result") or ""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError(f"no JSON in answer: {text[:200]}")
    usage = out.get("usage") or {}
    tok = sum(usage.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                                        "output_tokens"))
    return json.loads(m.group(0)).get("jobs") or [], tok


def check_answer(a: dict, text: str) -> dict:
    """Validate labels + verify every quote word for word; a finding without a real quote lowers confidence."""
    a = dict(a)
    a["quotes"] = dict(a.get("quotes") or {})
    for k in E.LABELS:
        if a.get(k) not in E.LABELS[k]:
            a[k] = E.NEUTRAL[k]
            a["confidence"] = min(a.get("confidence", 0), 50)
    t = _norm(text)
    bad = []
    for k in list(a["quotes"]):
        q = a["quotes"][k]
        if not q or _norm(q) not in t:
            bad.append(k)
            del a["quotes"][k]
    for k in E.LABELS:
        if a[k] != E.NEUTRAL[k] and k not in a["quotes"] and a[k] in BLOCKING[k] | {"welcome", "ms_ok", "fits"}:
            bad.append(k)
    if bad:
        a["unverified"] = sorted(set(bad))
        # an unproven blocking finding can't be pushed; an unproven positive one is fine to keep but less sure
        if any(a[k] in BLOCKING[k] for k in bad):
            a["confidence"] = min(a.get("confidence", 0), MIN_CONF - 1)
    a["tech"] = a.get("tech") if a.get("tech") in ("AI/ML", "Data", "Software", "Borderline") else None
    a["us"] = a.get("us") is not False
    return a


def fetch_text(url: str) -> str | None:
    r = jdfetch.fetch(url)
    return E._clean(r["text"])[:TEXT_LIMIT] if r and r.get("text") else None


def git_push(n_new: int, dry: bool) -> str:
    if dry:
        return "dry run: not pushed"
    def g(*a):
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, timeout=120)
    g("add", "agent/overrides.json")
    if g("diff", "--cached", "--quiet").returncode == 0:
        return "nothing to push"
    c = g("commit", "-m", f"Review agent: {n_new} correction(s) to the live checks", "--", "agent/overrides.json")
    if c.returncode != 0:
        return "commit failed: " + c.stderr[-200:]
    p = g("pull", "--rebase", "--autostash", "origin", "main")
    p = g("push", "origin", "HEAD:main")
    return "pushed" if p.returncode == 0 else "push failed: " + p.stderr[-200:]


def run(max_jobs: int, batch: int, dry: bool) -> dict:
    os.makedirs(DIR, exist_ok=True)
    lock = open(os.path.join(DIR, "lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return {"status": "ok", "say": "review agent already running"}
    now = int(time.time())
    reviews, overrides = _load(REVIEWS, {}), _load(OVERRIDES, {})
    live = live_data()
    rows = {store.norm_url(r["url"]): r for r in live["jobs"]}
    live_at = live["meta"].get("updated") or live["meta"].get("generated") or 0
    # 1b. a run that died mid-way (laptop off) saved its reviews but not its corrections: rebuild them
    recovered = 0
    for k, rv in reviews.items():
        if rv.get("status") == "corrected" and k not in overrides and k in rows:
            a = rv["answer"]
            overrides[k] = {"us": a["us"], "tech": a["tech"], "labels": {x: a[x] for x in E.LABELS},
                            "quotes": a["quotes"], "confidence": a["confidence"], "company": rv["company"],
                            "title": rv["title"], "was": rv.get("live_reason") or ("match" if rv.get("live_match") else "not a match"),
                            "now": "match" if rv["agent_match"] else rv["agent_reason"], "pushed_at": now,
                            "live": "waiting for the next build"}
            recovered += 1

    # 2. verify earlier corrections
    for k, ov in overrides.items():
        r = rows.get(k)
        rv = reviews.get(k) or {}
        if r is None:
            ov["live"] = "gone (out of the 2-day window)"
        elif r["match"] == rv.get("agent_match"):
            ov["live"] = "reflected"
        elif ov.get("pushed_at", now) > live_at - 60:
            ov["live"] = "waiting for the next build"
        else:
            ov["live"] = "NOT reflected"
    # 3. review new jobs: matches first (a wrong "match" costs an application), then tech roles, then the rest
    todo = [(k, r) for k, r in rows.items() if k not in reviews]
    todo.sort(key=lambda kr: (not kr[1]["match"], kr[1]["field"] not in ("AI/ML", "Data", "Software"), -kr[1]["posted"]))
    todo = todo[:max_jobs]
    with ThreadPoolExecutor(max_workers=8) as pool:
        texts = list(pool.map(lambda kr: fetch_text(kr[1]["url"]), todo))
    ready = []
    for (k, r), text in zip(todo, texts):
        if not text:
            reviews[k] = {"t": now, "company": r["company"], "title": r["title"], "jd": False,
                          "live_match": r["match"], "agent_match": r["match"]}
            continue
        ready.append((k, r, text))
    stats = {"reviewed": 0, "agree": 0, "new_corrections": recovered, "recovered": recovered, "unsure": 0, "no_jd": len(todo) - len(ready),
             "tokens": 0, "errors": []}
    for i in range(0, len(ready), batch):
        part = ready[i:i + batch]
        items = [{"id": str(n), "company": r["company"], "title": r["title"], "locations": r["locations"],
                  "text": text} for n, (k, r, text) in enumerate(part)]
        try:
            answers, tok = ask_claude(items)
        except Exception as e:  # a failed call: these jobs are tried again next run
            stats["errors"].append(str(e)[:200])
            continue
        stats["tokens"] += tok
        by_id = {str(a.get("id")): a for a in answers}
        for n, (k, r, text) in enumerate(part):
            a = by_id.get(str(n))
            if not a:
                continue
            a = check_answer(a, text)
            if a["tech"] == "Borderline":  # undecided role family: keep the live site's tech decision
                a["tech"] = r["field"] if r["field"] in ("AI/ML", "Data", "Software") else None
                a["note"] = ("borderline role family; " + (a.get("note") or "")).strip("; ")
            m, reason = agent_verdict(a)
            rec = {"t": now, "company": r["company"], "title": r["title"], "url": r["url"], "jd": True,
                   "live_match": r["match"], "live_reason": r["why_not"], "agent_match": m, "agent_reason": reason,
                   "answer": {x: a[x] for x in ("us", "tech", *E.LABELS, "quotes", "confidence")},
                   "note": a.get("note") or "", "unverified": a.get("unverified") or []}
            stats["reviewed"] += 1
            if m == r["match"]:
                stats["agree"] += 1
                rec["status"] = "agree"
            elif a.get("confidence", 0) >= MIN_CONF:
                rec["status"] = "corrected"
                overrides[k] = {"us": a["us"], "tech": a["tech"], "labels": {x: a[x] for x in E.LABELS},
                                "quotes": a["quotes"], "confidence": a["confidence"], "company": r["company"],
                                "title": r["title"], "was": "match" if r["match"] else (r["why_not"] or "not a match"),
                                "now": "match" if m else reason, "pushed_at": now, "live": "waiting for the next build"}
                stats["new_corrections"] += 1
            else:
                rec["status"] = "unsure (for Dheepak)"
                stats["unsure"] += 1
            reviews[k] = rec
        _save(OVERRIDES, overrides)  # save both each batch, so a crash never loses a correction
        _save(REVIEWS, reviews)
    # drop corrections for jobs that left the window a while ago (keeps the file small)
    overrides = {k: v for k, v in overrides.items() if k in rows or now - v.get("pushed_at", now) < 3 * 86400}
    reviews = {k: v for k, v in reviews.items() if now - v["t"] < 7 * 86400}
    _save(REVIEWS, reviews)
    _save(OVERRIDES, overrides)
    stats["push"] = git_push(stats["new_corrections"], dry) if stats["new_corrections"] or not dry else "no changes"
    stats["left"] = sum(1 for k in rows if k not in reviews)
    stats["not_reflected"] = sum(1 for v in overrides.values() if v.get("live") == "NOT reflected")
    write_report(reviews, overrides, rows, stats, now)
    say = (f"read {stats['reviewed']} jobs, {stats['new_corrections']} fixed, {stats['unsure']} unsure"
           f"{', ' + str(stats['not_reflected']) + ' NOT live' if stats['not_reflected'] else ''}, {stats['left']} left")
    res = {"status": "error" if stats["errors"] and not stats["reviewed"] else "ok", "say": say, "t": now,
           "stats": stats}
    _save(LAST, res)
    return res


def write_report(reviews, overrides, rows, stats, now):
    live_reviewed = [v for k, v in reviews.items() if k in rows and v.get("jd")]
    agree = sum(1 for v in live_reviewed if v.get("status") == "agree")
    lines = [f"# Review agent report", "",
             f"Updated {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}. Live jobs {len(rows)}, read by the "
             f"agent {len(live_reviewed)} (agree {agree}), no description {sum(1 for k, v in reviews.items() if k in rows and not v.get('jd'))}, "
             f"not read yet {stats['left']}. Last run: {stats}", "",
             "## Corrections (the agent overrode the live verdict)", "",
             "| Job | Live said | Agent says | Conf | Live now |", "|---|---|---|---|---|"]
    for k, v in sorted(overrides.items(), key=lambda kv: -kv[1].get("pushed_at", 0)):
        lines.append(f"| {v['company']} · {v['title']} | {v['was']} | {v['now']} | {v['confidence']} | {v.get('live')} |")
    lines += ["", "## Unsure (not pushed; please look)", "", "| Job | Live | Agent | Conf | Note |", "|---|---|---|---|---|"]
    for k, v in reviews.items():
        if v.get("status", "").startswith("unsure") and k in rows:
            lines.append(f"| [{v['company']} · {v['title']}]({v['url']}) | {v['live_reason'] or 'match'} | "
                         f"{v['agent_reason'] or 'match'} | {v['answer']['confidence']} | {v['note']} |")
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "report"])
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    if a.cmd == "report":
        print(json.dumps(_load(LAST, {}), indent=1))
        return
    print(json.dumps(run(a.max, a.batch, a.dry)))


if __name__ == "__main__":
    main()
