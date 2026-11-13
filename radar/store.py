"""The seen store: when did we first see each job.

state/seen.json.gz = {"version": 1, "jobs": {key: {"first": unix, "boot": bool}}, "links": {short: real}}
A job seen during the very first run is marked boot=True, so the first run doesn't call 17k old jobs "new".
"""
import gzip
import json
import os
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

TRACKING = re.compile(r"^(utm_|ref$|refs$|source$|src$|gh_src$|lever-source|iis$|iisn$|jobboard$|codes$)", re.I)


def norm_url(url: str) -> str:
    p = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k)]
    path = p.path.rstrip("/")
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, urlencode(sorted(q)), ""))


def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def job_key(company: str, title: str) -> str:
    """Same company + same title = same job across feeds (feeds list one row per role, many locations)."""
    return f"{_norm_text(company)}|{_norm_text(title)}"


def load(path: str) -> dict:
    if not os.path.exists(path):
        return {"version": 1, "jobs": {}, "links": {}}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def save(state: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(tmp, path)


def mark_seen(state: dict, key: str, now: int, boot: bool) -> dict:
    rec = state["jobs"].get(key)
    if rec is None:
        rec = {"first": now, "boot": boot}
        state["jobs"][key] = rec
    return rec


def prune(state: dict, now: int, keep_days: int = 120) -> None:
    """Forget jobs first seen long ago so the store stays small."""
    cut = now - keep_days * 86400
    state["jobs"] = {k: v for k, v in state["jobs"].items() if v["first"] >= cut}
