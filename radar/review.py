"""The contract between the cloud build and the local review agent (scripts/agent_audit.py on Dheepak's laptop).

Git is the message bus, so the laptop never has to be reachable and nothing is lost while it is off:

  cloud -> laptop   site/data/review-queue.json (GitHub Pages): every job in the window with the verdict the rules
                    and the AI reader gave ("base"), a priority, the reason it needs a look, and jd_hash.
  laptop -> cloud   agent/overrides.json on main (a push starts a build): the agent's confident corrections, each
                    tied to the jd_hash it read. A correction whose jd_hash no longer matches is stale: not applied,
                    and the job goes back to the top of the queue.
  laptop -> cloud   agent-status branch, status.json (force-pushed, no history): heartbeat + which jobs the agent
                    has read, so the page can say "agent checked" and when the agent last ran.
  Dheepak -> cloud  agent/decisions.json on main (scripts/decide.py): his own show / hide calls. Final over
                    everything, never expire while the job is live, and the agent skips those jobs.

Order of authority: Dheepak > review agent > AI reader + rules > rules.
"""
import hashlib
import json
import os
import re

from . import escalate

HERE = os.path.dirname(__file__)
OVERRIDES = os.path.join(HERE, "..", "agent", "overrides.json")
DECISIONS = os.path.join(HERE, "..", "agent", "decisions.json")
QUEUE_VERSION = 2   # 2: jobs keyed by the merge key (company + title), not the URL

# priorities: lower = read first
P_UNSURE, P_MATCH, P_FAILS, P_NOT_TECH = 1, 2, 3, 4


def load(path: str | None) -> dict:
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def jd_hash(excerpt: str | None) -> str | None:
    """Fingerprint of the description text the verdict was made from (whitespace/case-insensitive)."""
    if not excerpt:
        return None
    return hashlib.sha1(re.sub(r"\s+", " ", excerpt).strip().lower().encode()).hexdigest()[:16]


def lookup(d: dict, j: dict):
    """Entry for a job: by its merge key (since queue v2), else by its URL (older corrections / decisions)."""
    return d.get(j["_key"]) or d.get(j.get("_url_key"))


def overrides_sha(overrides: dict) -> str:
    """Fingerprint of the corrections a build applied: the agent compares it with what it pushed, so "not live"
    is only reported when this build really had the agent's latest file."""
    return hashlib.sha1(json.dumps(overrides, sort_keys=True).encode()).hexdigest()[:12]


def cc_state(by: str | None, rv, tried: dict | None, h: str | None) -> dict:
    """What Claude Code made of a job, for the page:
    corrected (its correction is live) / agree / pending (correction pushed, not built yet) / unsure (disagrees,
    not sure enough to correct) / tried (could not read the posting) / none."""
    if by == "agent":
        return {"state": "corrected"}
    if isinstance(rv, str):          # heartbeat from before verdicts were sent: read, verdict unknown
        rv = {"h": rv, "verdict": "agree"}
    if rv and rv.get("h") == h:
        return {"state": {"correction": "pending"}.get(rv.get("verdict"), rv.get("verdict") or "agree")}
    if tried:
        return {"state": "tried", "tried": tried}
    return {"state": None}


def override_applies(ov: dict | None, h: str | None) -> bool:
    """An agent correction counts only for the description it read. Old corrections without a hash still count."""
    return bool(ov) and ("jd_hash" not in ov or ov["jd_hash"] == h)


def priority(base_reason: str | None, e_base: dict | None, excerpt: str | None, stale: bool) -> tuple[int, str]:
    """(priority, why) for the review agent, from the verdict made WITHOUT the agent."""
    e_base = e_base or {}
    if stale:
        return P_UNSURE, "description changed since the agent read it"
    if base_reason is None:
        return P_MATCH, "a match: every match gets a second read"
    if base_reason.startswith(("needs review", "unverified")) or e_base.get("disagree"):
        return P_UNSURE, "rules unsure: " + base_reason
    if base_reason.startswith("fails"):
        labels = e_base.get("labels") or {}
        unsure = set(escalate.plan(excerpt, e_base)) if labels else set()
        blocking = {k for k in ("citizenship", "cpt", "degree", "grad")
                    if labels.get(k) in ("required", "rejected", "ug_only", "phd_only", "misses")}
        if blocking & unsure and e_base.get("ai") != "read":
            return P_UNSURE, "fails on a rule the rules are not sure of: " + ", ".join(sorted(blocking & unsure))
        return P_FAILS, base_reason
    return P_NOT_TECH, base_reason


def queue_rows(rows: list[dict]) -> list[dict]:
    """Rows of the published review queue: every job not decided by Dheepak, most important first."""
    keep = ("key", "url", "company", "title", "locations", "field", "posted", "jd_hash", "match", "why_not", "by")
    out = []
    for r in rows:
        if r.get("by") == "you":
            continue
        q = {k: r.get(k) for k in keep}
        q.update(base_match=r["base"]["match"], base_why_not=r["base"]["why_not"], p=r["review"]["p"],
                 why=r["review"]["why"])
        out.append(q)
    out.sort(key=lambda q: (q["p"], -(q["posted"] or 0)))
    return out


def agent_meta(status: dict, rows: list[dict], overrides: dict, applied: int, stale: int, now: int) -> dict:
    reviewed = status.get("reviewed") or {}
    return {
        "last_run": status.get("t"),
        "hours_since": round((now - status["t"]) / 3600, 1) if status.get("t") else None,
        "say": status.get("say"),
        "checked": sum(1 for r in rows if r.get("checked")),
        "total": len(rows),
        "corrections": len(overrides),
        "applied": applied,
        "stale": stale,
        "known": len(reviewed),
        "ok": status.get("ok", True),
        "errors": (status.get("errors") or [])[:3],
    }
