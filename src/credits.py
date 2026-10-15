"""Who Underdog credits, and whether our Monitor watches them.

Underdog names its source on about half its NFL posts, in three styles: "via @handle",
a reporter-name prefix ("Schefter: ..."), or a quote-tweet. A coach or GM prefix
("McVay: ...") is a press-conference quote, not an account. Every Underdog news post is
classified here, and each credited account is checked against the X list our Monitor
ingests (fetched from the X API each run; KOB owns the list).

An account Underdog keeps crediting that isn't on our list is a report we can't see in
time, so the table puts those first: it is the add-these list for the list owner.

Without X credentials the list can't be read; the panel then falls back to "seen in the
Monitor" (the account produced a tweet the desk actioned in the last 30 days), which
proves membership but can't prove absence.

Run: python -m src.credits --sport nfl
"""
from __future__ import annotations

import json
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .store import load_jsonl, now_iso, parse_dt

HANDLE_CREDIT_RE = re.compile(r"(?:via|per|from|h/t)\s+@(\w{1,15})", re.I)
NAME_PREFIX_RE = re.compile(r"^([A-Z][A-Za-z'.\- ]{1,25}):\s")
NOT_A_SOURCE = {"Status alert", "Update", "Breaking", "Report", "Injury update"}
WINDOW_DAYS = 60
LIST_FILE = "monitor_list.json"


def classify(post: dict, names: dict[str, str]) -> dict:
    """How an Underdog post credits its source: kind plus handle when there is one."""
    text = post.get("text") or ""
    handles = HANDLE_CREDIT_RE.findall(text)
    if handles:
        return {"kind": "handle", "handle": handles[0]}
    m = NAME_PREFIX_RE.match(text)
    if m and m.group(1) in names:
        return {"kind": "reporter", "handle": names[m.group(1)], "name": m.group(1)}
    if any(r.get("type") == "quoted" for r in post.get("referenced_tweets") or []):
        return {"kind": "quote", "handle": None}
    if m and m.group(1) not in NOT_A_SOURCE:
        return {"kind": "presser", "handle": None, "name": m.group(1)}
    return {"kind": "none", "handle": None}


def credited_handle(text: str, names: dict[str, str]) -> str | None:
    """The account a post's reporter-name prefix stands for, if it maps to one."""
    m = NAME_PREFIX_RE.match(text or "")
    return names.get(m.group(1)) if m else None


def fetch_list(client, list_id: str, data_dir: Path) -> int:
    """Store the Monitor list's members (handles) for this run."""
    handles, token = [], None
    while True:
        params = {"max_results": 100}
        if token:
            params["pagination_token"] = token
        page = client._get(f"/lists/{list_id}/members", params=params)
        handles += [u["username"] for u in page.get("data", [])]
        token = (page.get("meta") or {}).get("next_token")
        if not token:
            break
    (data_dir / LIST_FILE).write_text(json.dumps(
        {"list_id": list_id, "fetched_at": now_iso(), "handles": sorted(handles, key=str.lower)}, indent=2))
    return len(handles)


def _med(xs):
    return round(statistics.median(xs)) if xs else None


def build(data_dir: Path, docs_dir: Path, stories: list[dict], names: dict[str, str],
          underdog_handle: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=WINDOW_DAYS)
    ud = underdog_handle.lower()
    posts = [t for t in load_jsonl(data_dir / "tweets.jsonl")
             if t.get("account", "").lower() == ud and t.get("is_news") and parse_dt(t["created_at"]) >= start]

    lst = {}
    if (data_dir / LIST_FILE).exists():
        lst = json.loads((data_dir / LIST_FILE).read_text())
    on_list = {h.lower() for h in lst.get("handles", [])} or None
    seen = {r["tweet_url"].split("/")[3].lower() for r in load_jsonl(data_dir / "desk_timeline.jsonl")
            if r.get("tweet_url")}

    by_post = {s["underdog"]["tweet_id"]: s for s in stories
               if s.get("status") == "matched" and s.get("underdog") and s.get("rotowire")}
    kinds: dict[str, int] = {}
    accounts: dict[str, dict] = {}
    for p in posts:
        c = classify(p, names)
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
        if not c["handle"]:
            continue
        key = c["handle"].lower()
        a = accounts.setdefault(key, {"handle": c["handle"], "credited": 0, "last": None, "gaps": []})
        a["credited"] += 1
        a["last"] = max(a["last"] or "", p["created_at"])
        s = by_post.get(p["id"])
        if s:
            a["gaps"].append((parse_dt(s["rotowire"]["created_at"]) - parse_dt(s["underdog"]["created_at"])).total_seconds())

    rows = []
    for key, a in accounts.items():
        g = a.pop("gaps")
        rows.append({**a,
                     "on_list": (key in on_list) if on_list is not None else None,
                     "seen_in_monitor": key in seen,
                     "shared": len(g),
                     "we_first_pct": round(100 * sum(x < 0 for x in g) / len(g), 1) if g else None,
                     "median_gap": _med(g)})
    missing = lambda r: r["on_list"] is False or (r["on_list"] is None and not r["seen_in_monitor"])  # noqa: E731
    rows.sort(key=lambda r: (not missing(r), -r["credited"], r["handle"].lower()))
    total = len(posts)
    out = {
        "generated_at": now_iso(),
        "window_days": WINDOW_DAYS,
        "posts": total,
        "kinds": {k: {"n": v, "pct": round(100 * v / total, 1)} for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])},
        "list": {"id": lst.get("list_id"), "fetched_at": lst.get("fetched_at"), "members": len(lst.get("handles", []))}
                if lst else None,
        "missing": sum(missing(r) for r in rows),
        "accounts": rows,
    }
    path = docs_dir / "credits.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    tmp.replace(path)
    return out


if __name__ == "__main__":
    import argparse

    from .config import load_config, sport_data_dir, sport_docs_dir

    ap = argparse.ArgumentParser(description="Rebuild credits.json (who Underdog credits).")
    ap.add_argument("--sport", default="nfl")
    a = ap.parse_args()
    meta = load_config()["sports"][a.sport]
    cc = meta["underdog_credits"]
    stories = json.loads((sport_docs_dir(a.sport) / "stories.json").read_text())
    res = build(sport_data_dir(a.sport), sport_docs_dir(a.sport), stories, cc.get("names") or {},
                meta["accounts"]["underdog"]["handle"])
    print(json.dumps({k: v for k, v in res.items() if k != "accounts"}, indent=2))
    for r in res["accounts"][:15]:
        print(r)
