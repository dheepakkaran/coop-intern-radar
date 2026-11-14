"""Local review agent: Claude Code (headless, on Dheepak's laptop) re-reads the jobs and corrects the live site.

Contract with the cloud build: radar/review.py. Git is the message bus, so nothing is lost while the laptop is off.

One run (Pet bot pet/bots/radar-audit/ starts it on boot / wake and every 30 min while the laptop is on):
  1. pull the review queue (site/data/review-queue.json on Pages): every live job with its base verdict (rules +
     AI reader, before any correction), a priority and why, and jd_hash (which description the verdict read)
  2. read: jobs never read, or whose description changed (jd_hash differs), most important first -> fetch the full
     description -> Claude reads it (US, tech role, five checks, word-for-word quotes, confidence). Saved per batch.
  3. corrections = a pure function of (queue, everything read): base verdict wrong + agent confident (>= 85) ->
     correction tied to its jd_hash; not confident -> "unsure" list for Dheepak (scripts/decide.py)
  4. push agent/overrides.json from a private worktree of origin/main (never from the working checkout), only
     when it changed; the push rebuilds the site
  5. heartbeat: status.json force-pushed to the agent-status branch (when it ran, which jobs it has read)
  6. docs/agent-report.md (local): corrections + whether the site shows them, and the unsure list

python scripts/agent_audit.py run [--max 40] [--batch 8] [--dry]
python scripts/agent_audit.py report         # last run summary
python scripts/agent_audit.py gold           # corrections -> tests/gold/candidates.json (confirm, then add to dev)
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
from radar import eligibility as E, jdfetch  # noqa: E402

QUEUE = "https://dheepakkaran.github.io/coop-intern-radar/data/review-queue.json"
DIR = os.path.join(ROOT, ".cache", "agent")
REVIEWS = os.path.join(DIR, "reviews.json")      # every job the agent read (local)
LAST = os.path.join(DIR, "last.json")            # last run summary (the Pet bot shows it)
REPORT = os.path.join(ROOT, "docs", "agent-report.md")
UNSURE = os.path.join(DIR, "unsure.json")        # numbered unsure list (scripts/decide.py uses the numbers)
PUSHED = os.path.join(DIR, "pushed.json")        # when each correction was first pushed (for "NOT live")
WT = os.path.join(DIR, "wt")                     # private worktree of origin/main, used only to push
STATUS_BRANCH = "agent-status"
NO_JD_RETRY = 6 * 3600   # a job whose description could not be read is tried again after this x tries so far
NO_JD_MAX_TRIES = 4      # then it stays "blocked" (shown on the page as such) until the job leaves the window
PW_PY = os.path.expanduser("~/workspace/.tools/pw-venv/bin/python")   # Playwright venv (headless Chromium)
PW_BROWSERS = os.path.expanduser("~/workspace/.tools/ms-playwright")
BROWSER_MAX = 30         # pages read in the browser per run (each ~5 s)
BROWSER_TEXT_LIMIT = 12000
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


def fetch_queue() -> dict:
    req = urllib.request.Request(f"{QUEUE}?t={int(time.time())}", headers={"User-Agent": "coop-intern-radar-agent"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    if not isinstance(data.get("jobs"), list) or not (data.get("meta") or {}).get("generated"):
        raise ValueError("review queue has no jobs list / generated time")
    return data


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
    try:
        r = jdfetch.fetch(url)
    except Exception:  # noqa: BLE001
        return None
    return E._clean(r["text"])[:TEXT_LIMIT] if r and r.get("text") else None


def browser_texts(urls: list[str]) -> dict:
    """{url: (text | None, why)} from a real headless browser, for pages a plain fetch can't read."""
    if not urls or not os.path.exists(PW_PY):
        return {u: (None, "no browser") for u in urls}
    try:
        p = subprocess.run([PW_PY, os.path.join(ROOT, "scripts", "browser_text.py")], input=json.dumps(urls),
                           capture_output=True, text=True, timeout=60 + 15 * len(urls),
                           env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": PW_BROWSERS})
        got = json.loads(p.stdout)
    except Exception as e:  # noqa: BLE001
        return {u: (None, "browser error: " + str(e)[:60]) for u in urls}
    out = {}
    for u in urls:
        r = got.get(u) or {}
        out[u] = ((E._clean(r["text"])[:BROWSER_TEXT_LIMIT], None) if r.get("text") else (None, r.get("why") or "unknown"))
    return out


def _git(*a, cwd=ROOT, stdin=None):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, timeout=180, input=stdin)


def push_file(rel: str, content: str, msg: str, dry: bool = False) -> str:
    """Commit one file onto the newest origin/main and push it, from a private worktree, retrying on a race.
    The working checkout (where Dheepak and Claude edit code) is never touched or pushed."""
    if dry:
        return "dry run: not pushed"
    err = ""
    for _ in range(3):
        f = _git("fetch", "-q", "origin", "main")
        if f.returncode != 0:
            return "fetch failed: " + f.stderr[-200:]
        if not os.path.isdir(os.path.join(WT, ".git")) and not os.path.isfile(os.path.join(WT, ".git")):
            _git("worktree", "prune")
            w = _git("worktree", "add", "-f", "--detach", WT, "origin/main")
            if w.returncode != 0:
                return "worktree failed: " + w.stderr[-200:]
        _git("checkout", "-q", "-f", "--detach", "origin/main", cwd=WT)
        _git("clean", "-fdq", cwd=WT)
        path = os.path.join(WT, rel)
        try:
            with open(path) as fh:
                if fh.read() == content:
                    return "no change"
        except OSError:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)
        _git("add", rel, cwd=WT)
        c = _git("commit", "-q", "-m", msg, cwd=WT)
        if c.returncode != 0:
            return "commit failed: " + (c.stderr or c.stdout)[-200:]
        p = _git("push", "-q", "origin", "HEAD:main", cwd=WT)
        if p.returncode == 0:
            return "pushed"
        err = p.stderr[-200:]   # someone pushed meanwhile: start again from the new origin/main
    return "push failed: " + err


def push_status(status: dict, dry: bool = False) -> str:
    """Heartbeat: one commit holding status.json, force-pushed to its own branch (no history, no rebuild)."""
    if dry:
        return "dry run"
    blob = _git("hash-object", "-w", "--stdin", stdin=json.dumps(status, sort_keys=True))
    tree = _git("mktree", stdin=f"100644 blob {blob.stdout.strip()}\tstatus.json\n")
    commit = _git("commit-tree", tree.stdout.strip(), "-m", f"agent status {time.strftime('%Y-%m-%dT%H:%M')}")
    if commit.returncode != 0:
        return "status commit failed: " + commit.stderr[-200:]
    p = _git("push", "-q", "-f", "origin", f"{commit.stdout.strip()}:refs/heads/{STATUS_BRANCH}")
    return "ok" if p.returncode == 0 else "status push failed: " + p.stderr[-200:]


def needs_read(q: dict, rv: dict | None, now: int) -> bool:
    if not rv:
        return True
    if not rv.get("jd"):
        tries = rv.get("tries", 1)
        return tries < NO_JD_MAX_TRIES and now - rv.get("t", 0) >= NO_JD_RETRY * tries
    return rv.get("jd_hash") != q["jd_hash"]


def corrections(queue_jobs: list[dict], reviews: dict) -> tuple[dict, list[dict]]:
    """Pure: (overrides, unsure) from the queue and everything the agent has read.
    Recomputed from scratch every run, so a crash, a restart or a changed description can never leave a stale
    or missing correction behind."""
    out, unsure = {}, []
    for q in queue_jobs:
        rv = reviews.get(q["key"])
        if not rv or not rv.get("jd") or "answer" not in rv or rv.get("jd_hash") != q["jd_hash"]:
            continue
        if rv["agent_match"] == q["base_match"]:
            continue
        a = rv["answer"]
        item = {"company": q["company"], "title": q["title"], "was": q["base_why_not"] or "match",
                "now": "match" if rv["agent_match"] else rv["agent_reason"], "confidence": a["confidence"]}
        if a["confidence"] < MIN_CONF:
            unsure.append({**item, "key": q["key"], "url": q["url"], "note": rv.get("note") or ""})
            continue
        out[q["key"]] = {**item, "jd_hash": q["jd_hash"], "us": a["us"], "tech": a["tech"],
                         "labels": {x: a[x] for x in E.LABELS}, "quotes": a["quotes"], "read_at": rv["t"]}
    return out, unsure


def live_state(overrides: dict, queue_jobs: list[dict], pushed: dict, generated: int) -> dict:
    """Is each correction on the site? reflected / waiting for the next build / NOT live."""
    by = {q["key"]: q for q in queue_jobs}
    out = {}
    for k, ov in overrides.items():
        q = by.get(k) or {}
        if q.get("by") == "agent" and q.get("match") == (ov["now"] == "match"):
            out[k] = "reflected"
        elif pushed.get(k, generated) < generated - 120:
            out[k] = "NOT live"
        else:
            out[k] = "waiting for the next build"
    return out


def run(max_jobs: int, batch: int, dry: bool) -> dict:
    os.makedirs(DIR, exist_ok=True)
    lock = open(os.path.join(DIR, "lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return {"status": "ok", "say": "review agent already running"}
    now = int(time.time())
    try:
        qd = fetch_queue()
    except Exception as e:  # no queue = no decisions this run (never recompute corrections from nothing)
        res = {"status": "error", "say": f"review queue unavailable: {str(e)[:120]}", "t": now}
        _save(LAST, res)
        return res
    queue, generated = qd["jobs"], qd["meta"]["generated"]
    reviews = _load(REVIEWS, {})
    # records read before jd_hash existed: adopt today's hash once instead of re-reading 400 jobs
    for q in queue:
        rv = reviews.get(q["key"])
        if rv and "jd_hash" not in rv:
            rv["jd_hash"] = q["jd_hash"]

    # 1. read: never read, or the description changed; most important first (the queue is already ordered)
    todo = [q for q in queue if needs_read(q, reviews.get(q["key"]), now)][:max_jobs]
    with ThreadPoolExecutor(max_workers=8) as pool:
        texts = list(pool.map(lambda q: fetch_text(q["url"]), todo))
    # pages a plain fetch can't read (JavaScript-only career sites): a real headless browser on this laptop
    missed = [q["url"] for q, text in zip(todo, texts) if not text][:BROWSER_MAX]
    via_browser = browser_texts(missed)
    ready, no_jd = [], 0
    for q, text in zip(todo, texts):
        why = None
        if not text:
            text, why = via_browser.get(q["url"], (None, "not tried (browser limit per run)"))
        if text:
            ready.append((q, text))
        elif why and why.startswith("not tried"):
            continue   # over this run's browser limit: not a real try, next run picks it up
        else:
            no_jd += 1
            old = reviews.get(q["key"]) or {}
            reviews[q["key"]] = {"t": now, "company": q["company"], "title": q["title"], "url": q["url"], "jd": False,
                                 "jd_hash": q["jd_hash"], "why": why,
                                 "tries": (old.get("tries", 1) + 1) if old and not old.get("jd") else 1}
    stats = {"read": 0, "no_jd": no_jd, "browser": sum(1 for t, _ in via_browser.values() if t), "tokens": 0,
             "errors": []}
    for i in range(0, len(ready), batch):
        part = ready[i:i + batch]
        items = [{"id": str(n), "company": q["company"], "title": q["title"], "locations": q["locations"],
                  "text": text} for n, (q, text) in enumerate(part)]
        try:
            answers, tok = ask_claude(items)
        except Exception as e:  # a failed call: these jobs are tried again next run
            stats["errors"].append(str(e)[:200])
            continue
        stats["tokens"] += tok
        by_id = {str(a.get("id")): a for a in answers}
        for n, (q, text) in enumerate(part):
            a = by_id.get(str(n))
            if not a:
                continue
            a = check_answer(a, text)
            if a["tech"] == "Borderline":  # undecided role family: keep the site's own tech decision
                a["tech"] = q["field"] if q["field"] in ("AI/ML", "Data", "Software") else None
                a["note"] = ("borderline role family; " + (a.get("note") or "")).strip("; ")
            m, reason = agent_verdict(a)
            reviews[q["key"]] = {
                "t": now, "company": q["company"], "title": q["title"], "url": q["url"], "jd": True,
                "jd_hash": q["jd_hash"], "excerpt": E.excerpt(text), "base_match": q["base_match"],
                "base_reason": q["base_why_not"], "agent_match": m, "agent_reason": reason,
                "answer": {x: a[x] for x in ("us", "tech", *E.LABELS, "quotes", "confidence")},
                "note": a.get("note") or "", "unverified": a.get("unverified") or []}
            stats["read"] += 1
        _save(REVIEWS, reviews)   # every batch: a crash loses at most one batch of reading
    live_keys = {q["key"] for q in queue}
    reviews = {k: v for k, v in reviews.items() if k in live_keys or now - v.get("t", 0) < 7 * 86400}
    _save(REVIEWS, reviews)

    # 2. corrections from everything read (pure), then push only if the file changed
    overrides, unsure = corrections(queue, reviews)
    pushed = _load(PUSHED, {})
    prev = _load(os.path.join(WT, "agent", "overrides.json"), None) if os.path.isdir(WT) else None
    content = json.dumps(overrides, indent=1, sort_keys=True) + "\n"
    stats["push"] = push_file("agent/overrides.json", content,
                              f"Review agent: {len(overrides)} correction(s) on the live jobs", dry)
    if stats["push"] == "pushed":
        for k in overrides:
            pushed.setdefault(k, now)
    pushed = {k: v for k, v in pushed.items() if k in overrides}
    _save(PUSHED, pushed)
    live = live_state(overrides, queue, pushed, generated)
    for n, u in enumerate(unsure, 1):
        u["n"] = n
    _save(UNSURE, unsure)

    stats.update(corrections=len(overrides), unsure=len(unsure),
                 not_live=sum(1 for v in live.values() if v == "NOT live"),
                 left=sum(1 for q in queue if needs_read(q, reviews.get(q["key"]), now)),
                 changed=prev is not None and prev != overrides)
    say = (f"read {stats['read']} jobs, {len(overrides)} corrections live-bound, {len(unsure)} unsure"
           f"{', ' + str(stats['not_live']) + ' NOT live' if stats['not_live'] else ''}, {stats['left']} left")
    status = {"t": now, "say": say, "stats": {k: v for k, v in stats.items() if k != "errors"},
              "reviewed": {k: v["jd_hash"] for k, v in reviews.items() if v.get("jd") and k in live_keys},
              # jobs the agent tried but could not read (the page says so instead of "not read yet")
              "tried": {k: {"t": v["t"], "why": v.get("why") or "could not read the posting", "tries": v.get("tries", 1)}
                        for k, v in reviews.items() if not v.get("jd") and k in live_keys}}
    stats["heartbeat"] = push_status(status, dry)
    write_report(overrides, unsure, live, queue, reviews, stats, now)
    res = {"status": "error" if stats["errors"] and not stats["read"] else "ok", "say": say, "t": now,
           "stats": stats}
    _save(LAST, res)
    return res


def write_report(overrides, unsure, live, queue, reviews, stats, now):
    read = sum(1 for q in queue if (reviews.get(q["key"]) or {}).get("jd")
               and reviews[q["key"]].get("jd_hash") == q["jd_hash"])
    lines = ["# Review agent report", "",
             f"Updated {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}. Queue {len(queue)} jobs, read by the "
             f"agent {read}, still to read {stats['left']}. Last run: {json.dumps(stats)}", "",
             "## Corrections (the agent overrides the base verdict)", "",
             "| Job | Base said | Agent says | Conf | On the site |", "|---|---|---|---|---|"]
    for k, v in sorted(overrides.items(), key=lambda kv: -kv[1]["read_at"]):
        lines.append(f"| {v['company']} · {v['title']} | {v['was']} | {v['now']} | {v['confidence']} | {live[k]} |")
    lines += ["", "## Unsure: your call (`python scripts/decide.py show|hide <n> \"note\"`)", "",
              "| n | Job | Base | Agent | Conf | Note |", "|---|---|---|---|---|---|"]
    for u in unsure:
        lines.append(f"| {u['n']} | [{u['company']} · {u['title']}]({u['url']}) | {u['was']} | {u['now']} | "
                     f"{u['confidence']} | {u['note']} |")
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w") as f:
        f.write("\n".join(lines) + "\n")


def gold_candidates() -> str:
    """Agent disagreements -> tests/gold/candidates.json, in the gold-set format. Confirm by hand, then move the
    right ones into tests/gold/dev.json: the rules learn from their mistakes and fewer jobs need the agent."""
    reviews = _load(REVIEWS, {})
    out = []
    for k, v in reviews.items():
        if not v.get("jd") or "answer" not in v or v["agent_match"] == v.get("base_match", v.get("live_match")):
            continue
        a = v["answer"]
        out.append({"company": v["company"], "title": v["title"], "url": v.get("url"), "excerpt": v.get("excerpt"),
                    "gold": {x: a[x] for x in E.LABELS}, "quotes": a["quotes"], "confidence": a["confidence"],
                    "base": v.get("base_reason", v.get("live_reason")) or "match",
                    "note": "from the review agent " + time.strftime("%Y-%m-%d", time.localtime(v["t"]))
                            + "; confirm the labels before adding to dev.json"})
    path = os.path.join(ROOT, "tests", "gold", "candidates.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1)
    return f"{len(out)} candidates -> {os.path.relpath(path, ROOT)} ({sum(1 for o in out if not o['excerpt'])} without excerpt)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "report", "gold"])
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    if a.cmd == "report":
        print(json.dumps(_load(LAST, {}), indent=1))
        return
    if a.cmd == "gold":
        print(gold_candidates())
        return
    print(json.dumps(run(a.max, a.batch, a.dry)))


if __name__ == "__main__":
    main()
