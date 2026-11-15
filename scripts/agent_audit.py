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
from radar import eligibility as E, jdfetch, review, store  # noqa: E402

QUEUE = "https://dheepakkaran.github.io/coop-intern-radar/data/review-queue.json"
DIR = os.path.join(ROOT, ".cache", "agent")
REVIEWS = os.path.join(DIR, "reviews.json")      # every job the agent read (local)
LAST = os.path.join(DIR, "last.json")            # last run summary (the Pet bot shows it)
REPORT = os.path.join(ROOT, "docs", "agent-report.md")
NOTES_MARK = "<!-- notes below this line are written by hand and kept across agent runs -->"
UNSURE = os.path.join(DIR, "unsure.json")        # unsure list with stable ids (scripts/decide.py uses the ids)
WT = os.path.join(DIR, "wt")                     # private worktree of origin/main, used only to push
WT_LOCK = os.path.join(DIR, "wt.lock")           # agent and decide.py never use the worktree at the same time
AGENT_VERSION = 2   # bump when SYSTEM / the verdict policy changes: every job is read again, most important first
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
Graduates May 2028. """ + E.residency_fact() + """ These are internships / co-ops: the university authorizes the work through CPT, so the employer
never has to sponsor a visa. "No sponsorship now or in future" is therefore FINE and must not lower your confidence.
The only work-authorization question is whether the employer accepts a CPT/OPT (F-1) student at all: a citizenship /
green card / clearance / U.S. person requirement, a US-residency length he does not have, or an explicit
"F-1 / CPT / OPT not eligible", blocks the job.

For each job answer:
- us: true if the job is in the United States (remote-US counts), false if it is only outside the US.
- tech: "AI/ML", "Data" or "Software" if the role's actual work is AI/ML (incl. AI in any industry: biotech AI,
  chip AI, inference, evals, agents), data (data science / engineering / analytics with real data work) or software
  engineering; "Borderline" for cybersecurity / SOC / GRC, firmware / FPGA / embedded / hardware design, and GIS
  roles (Dheepak has not decided on these yet); null for anything else (marketing, finance, mechanical, sales,
  operations, HR, ...).
- citizenship: "required" if US citizenship, permanent residency / green card, "U.S. person" status, an active or
  obtainable security clearance, or N years of (continuous) US residency that he does not have (e.g. "three years of
  continuous residency in the U.S." for a government credential) is REQUIRED; quote that sentence. "not_required"
  otherwise. A PIV badge, public trust, national agency check or background check alone is "not_required". A company that only says it MAY decline or
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


def claude_env() -> dict:
    """Dheepak (2026-11-14): Claude Code only, never an Anthropic API key. Without these variables `claude -p` uses
    the Claude Code login (subscription); with one set it would bill that key instead."""
    return {k: v for k, v in os.environ.items()
            if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")}


def ask_claude(items: list[dict]) -> list[dict]:
    """items: [{id, company, title, locations, text}] -> the agent's answers (unchecked)."""
    blocks = [f"### id {it['id']}\nCompany: {it['company']}\nTitle: {it['title']}\nLocations: "
              f"{'; '.join(it['locations'][:5]) or 'not listed'}\nDescription:\n{it['text']}" for it in items]
    prompt = "Check these postings.\n\n" + "\n\n".join(blocks)
    cmd = ["claude", "-p", "--model", MODEL, "--tools", "", "--setting-sources", "", "--no-session-persistence",
           "--output-format", "json", "--system-prompt", SYSTEM]
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=900, cwd=DIR, env=claude_env())
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
    try:   # a missing / null / "90" confidence must never crash a run or poison reviews.json
        a["confidence"] = max(0, min(100, int(float(a.get("confidence")))))
    except (TypeError, ValueError):
        a["confidence"] = 0
    q = a.get("quotes")
    a["quotes"] = {k: v for k, v in q.items() if isinstance(v, str)} if isinstance(q, dict) else {}
    for k in E.LABELS:
        if a.get(k) not in E.LABELS[k]:
            a[k] = E.NEUTRAL[k]
            a["confidence"] = min(a["confidence"], 50)
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
            a["confidence"] = min(a["confidence"], MIN_CONF - 1)
    a["tech"] = a.get("tech") if a.get("tech") in ("AI/ML", "Data", "Software", "Borderline") else None
    a["us"] = a.get("us") is not False
    return a


def fetch_text(url: str) -> str | None:
    try:
        r = jdfetch.fetch(url)
    except Exception:  # noqa: BLE001
        return None
    return E._clean(r["text"])[:TEXT_LIMIT] if r and r.get("text") else None


NOT_TRIED = "not tried"   # prefix of a "why" that does not count as a try (browser missing, crashed, out of time)


def browser_texts(urls: list[str], budget_s: int = 40) -> dict:
    """{url: (text | None, why)} from a real headless browser, for pages a plain fetch can't read.
    The browser prints one line per page as it goes, so a timeout or crash keeps every page already read; pages it
    never reached come back as "not tried" and do not count against the job."""
    if not urls or not os.path.exists(PW_PY):
        return {u: (None, NOT_TRIED + ": no browser") for u in urls}
    out_text = ""
    try:
        p = subprocess.run([PW_PY, os.path.join(ROOT, "scripts", "browser_text.py")], input=json.dumps(urls),
                           capture_output=True, text=True, timeout=30 + budget_s * len(urls),
                           env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": PW_BROWSERS})
        out_text = p.stdout
    except subprocess.TimeoutExpired as e:
        out_text = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
    except Exception:  # noqa: BLE001
        pass
    got = {}
    for line in out_text.splitlines():
        try:
            r = json.loads(line)
            got[r["url"]] = r
        except (ValueError, KeyError, TypeError):
            continue
    out = {}
    for u in urls:
        r = got.get(u)
        if r is None:
            out[u] = (None, NOT_TRIED + ": browser did not reach it")
        elif r.get("text"):
            out[u] = (E._clean(r["text"])[:BROWSER_TEXT_LIMIT], None)
        else:
            out[u] = (None, r.get("why") or "unknown")
    return out


def _git(*a, cwd=None, stdin=None):
    # cwd is resolved at call time (not as a default argument), so tests that point ROOT elsewhere never touch the
    # real repository
    return subprocess.run(["git", *a], cwd=cwd or ROOT, capture_output=True, text=True, timeout=180, input=stdin)


def push_file(rel: str, content: str, msg: str, dry: bool = False) -> str:
    """Commit one file onto the newest origin/main and push it, from a private worktree, retrying on a race.
    The working checkout (where Dheepak and Claude edit code) is never touched or pushed."""
    if dry:
        return "dry run: not pushed"
    os.makedirs(DIR, exist_ok=True)
    with open(WT_LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)   # wait for the other writer (agent or decide.py)
        return _push_file(rel, content, msg)


def _push_file(rel: str, content: str, msg: str) -> str:
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
    if rv.get("jd_hash") != q["jd_hash"]:
        return True          # the description changed (or the build found one): read again, tries start over
    if not rv.get("jd"):
        tries = rv.get("tries", 1)
        return tries < NO_JD_MAX_TRIES and now - rv.get("t", 0) >= NO_JD_RETRY * tries
    return rv.get("v", 1) != AGENT_VERSION


def _conf(a: dict) -> int:
    try:
        return int(a.get("confidence") or 0)
    except (TypeError, ValueError):
        return 0


def short_id(key: str) -> str:
    """Stable id of a job for decide.py: the same job keeps it however the unsure list is reordered."""
    import hashlib
    return hashlib.sha1(key.encode()).hexdigest()[:5]


def verdict(q: dict, rv: dict | None) -> str | None:
    """agree / correction / unsure for a job the agent read against the current description, else None."""
    if not rv or not rv.get("jd") or "answer" not in rv or rv.get("jd_hash") != q["jd_hash"]:
        return None
    if rv["agent_match"] == q["base_match"]:
        return "agree"
    return "correction" if _conf(rv["answer"]) >= MIN_CONF else "unsure"


overrides_sha = review.overrides_sha   # the build fingerprints the file the same way


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
        conf = _conf(a)
        item = {"company": q["company"], "title": q["title"], "was": q["base_why_not"] or "match",
                "now": "match" if rv["agent_match"] else rv["agent_reason"], "confidence": conf}
        if conf < MIN_CONF:
            unsure.append({**item, "id": short_id(q["key"]), "key": q["key"], "url": q["url"],
                           "note": rv.get("note") or ""})
            continue
        out[q["key"]] = {**item, "jd_hash": q["jd_hash"], "us": a["us"], "tech": a["tech"],
                         "labels": {x: a[x] for x in E.LABELS}, "quotes": a["quotes"], "read_at": rv["t"]}
    return out, unsure


def live_state(overrides: dict, queue_jobs: list[dict], built_sha: str | None) -> dict:
    """Is each correction on the site? reflected / waiting for the next build / NOT live.
    "NOT live" only when the build that made this queue applied exactly these corrections and still doesn't show
    one (a build that started before the push is just "waiting")."""
    by = {q["key"]: q for q in queue_jobs}
    same_file = built_sha == overrides_sha(overrides)
    out = {}
    for k, ov in overrides.items():
        q = by.get(k) or {}
        if q.get("by") == "agent" and q.get("match") == (ov["now"] == "match"):
            out[k] = "reflected"
        elif same_file:
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
    queue = qd["jobs"]
    reviews = _load(REVIEWS, {})
    for q in queue:
        # queue v2 keys jobs by company + title; move records kept under the job's URL (queue v1)
        u = store.norm_url(q["url"])
        if q["key"] not in reviews and u in reviews:
            reviews[q["key"]] = reviews.pop(u)
        rv = reviews.get(q["key"])
        # records read before jd_hash existed: adopt today's hash once instead of re-reading 400 jobs
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
            text, why = via_browser.get(q["url"], (None, NOT_TRIED + ": browser limit per run"))
        if text:
            ready.append((q, text))
        elif why and why.startswith(NOT_TRIED):
            continue   # over this run's browser limit: not a real try, next run picks it up
        else:
            no_jd += 1
            old = reviews.get(q["key"]) or {}
            again = old and not old.get("jd") and old.get("jd_hash") == q["jd_hash"]
            reviews[q["key"]] = {"t": now, "company": q["company"], "title": q["title"], "url": q["url"], "jd": False,
                                 "jd_hash": q["jd_hash"], "why": why, "v": AGENT_VERSION,
                                 "tries": old.get("tries", 1) + 1 if again else 1}
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
            if not isinstance(a, dict):
                continue
            try:   # one malformed answer must not stop the run: that job is read again next time
                a = check_answer(a, text)
                if a["tech"] == "Borderline":  # undecided role family: keep the site's own tech decision
                    a["tech"] = q["field"] if q["field"] in ("AI/ML", "Data", "Software") else None
                    a["note"] = ("borderline role family; " + str(a.get("note") or "")).strip("; ")
                m, reason = agent_verdict(a)
            except Exception as e:  # noqa: BLE001
                stats["errors"].append(f"bad answer for {q['company']}: {str(e)[:80]}")
                continue
            reviews[q["key"]] = {
                "t": now, "v": AGENT_VERSION, "company": q["company"], "title": q["title"], "url": q["url"], "jd": True,
                "jd_hash": q["jd_hash"], "excerpt": E.excerpt(text), "base_match": q["base_match"],
                "base_reason": q["base_why_not"], "agent_match": m, "agent_reason": reason,
                "answer": {x: a[x] for x in ("us", "tech", *E.LABELS, "quotes", "confidence")},
                "note": str(a.get("note") or ""), "unverified": a.get("unverified") or []}
            stats["read"] += 1
        _save(REVIEWS, reviews)   # every batch: a crash loses at most one batch of reading
    live_keys = {q["key"] for q in queue}
    reviews = {k: v for k, v in reviews.items() if k in live_keys or now - v.get("t", 0) < 7 * 86400}
    _save(REVIEWS, reviews)

    # 2. corrections from everything read (pure), then push only if the file changed
    overrides, unsure = corrections(queue, reviews)
    content = json.dumps(overrides, indent=1, sort_keys=True) + "\n"
    stats["push"] = push_file("agent/overrides.json", content,
                              f"Review agent: {len(overrides)} correction(s) on the live jobs", dry)
    if stats["push"] not in ("pushed", "no change", "dry run: not pushed"):
        stats["errors"].append("push: " + stats["push"])
    live = live_state(overrides, queue, qd["meta"].get("overrides_sha"))
    _save(UNSURE, unsure)

    stats.update(corrections=len(overrides), unsure=len(unsure),
                 not_live=sum(1 for v in live.values() if v == "NOT live"),
                 left=sum(1 for q in queue if needs_read(q, reviews.get(q["key"]), now)))
    say = (f"read {stats['read']} jobs, {len(overrides)} corrections live-bound, {len(unsure)} unsure"
           f"{', ' + str(stats['not_live']) + ' NOT live' if stats['not_live'] else ''}, {stats['left']} left")
    by_key = {q["key"]: q for q in queue}
    status = {"t": now, "say": say, "stats": {k: v for k, v in stats.items() if k != "errors"},
              "ok": not stats["errors"], "errors": stats["errors"][:5],
              # what the agent concluded for each job it read (the page shows "agrees" only for real agreement)
              "reviewed": {k: {"h": v["jd_hash"], "verdict": verdict(by_key[k], v)}
                           for k, v in reviews.items() if v.get("jd") and k in by_key and verdict(by_key[k], v)},
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
    lines += ["", "## Unsure: your call (`python scripts/decide.py show|hide <id> \"note\"`)", "",
              "| id | Job | Base | Agent | Conf | Note |", "|---|---|---|---|---|---|"]
    for u in unsure:
        lines.append(f"| {u['id']} | [{u['company']} · {u['title']}]({u['url']}) | {u['was']} | {u['now']} | "
                     f"{u['confidence']} | {u['note']} |")
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    try:   # hand-written notes (root-cause write-ups ...) live under the mark and survive every run
        with open(REPORT) as f:
            old = f.read()
        notes = old[old.index(NOTES_MARK):] if NOTES_MARK in old else NOTES_MARK + "\n"
    except OSError:
        notes = NOTES_MARK + "\n"
    with open(REPORT, "w") as f:
        f.write("\n".join(lines) + "\n\n" + notes)


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
