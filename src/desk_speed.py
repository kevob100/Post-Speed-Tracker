"""Desk Speed: where RotoWire's time goes between the source tweet and our post.

The head-to-head says how far behind Underdog we are; it can't say why. RotoWire's own
News Admin times every tweet through the desk (newsadmin.rotowire.com/monitor/desk-speed),
and its read-only MCP server exposes that per tweet. This module pulls it and splits our
time into the three steps the desk can work on:

  1. sent -> in Monitor   (arrival: ingestion)
  2. in Monitor -> opened (AI draft + an editor finding and opening it)
  3. opened -> on X       (editing and posting; tweet edit rate rides along here)

Joining: a RotoWire post in the tracker is the same post as a Desk Speed row when the
row's posted_to_x_at equals the post's created_at (within 2s, and only one row there).
Posts with no row were added by hand (News Admin's Add Line, which the timeline leaves
out) or, rarely, posted outside News Admin. Fewer of those is a goal in itself: each one
is an editor writing a post from scratch, so the share is tracked week by week.

Underdog's time is measured from the same source tweet our Monitor ingested, on the
stories both accounts posted, so both bars start on one clock.

Panel opens are tracked from 2026-10-13. Before that steps 2 and 3 are known only
together, so they are measured on the rows that have an open stamp, and the panel falls
back to one combined step until there are enough of them.

Needs ROTOWIRE_MCP_TOKEN (read-only desk analytics scope). Without it the sync is
skipped and the last stored rows are used. Timeline rows are kept in
data/<sport>/desk_timeline.jsonl keyed by tweet_id, so history outlives the server's
30-day window.

Run: python -m src.desk_speed --sport nfl
"""
from __future__ import annotations

import json
import os
import statistics
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .store import load_jsonl, now_iso, parse_dt, write_jsonl

MCP_URL = "https://newsadmin.rotowire.com/mcp"
WINDOW_DAYS = 30          # what the panel summarises
FETCH_DAYS = 30           # server max per call
MIN_SPLIT_N = 10          # opens needed before steps 2 and 3 are shown apart
JOIN_TOLERANCE_S = 2
# Editor names stay out of the stored rows (the repo is public).
PRIVATE_FIELDS = {"first_opened_by", "x_posted_by", "sent_by"}
TZ = ZoneInfo("America/New_York")
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# Days the desk reports on first: game day and injury-report day.
KEY_DAYS = {"Sun": "game day", "Wed": "injury reports"}


def has_credentials() -> bool:
    return bool(os.environ.get("ROTOWIRE_MCP_TOKEN"))


def _call(tool: str, args: dict) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}}).encode()
    req = urllib.request.Request(MCP_URL, data=body, headers={
        "Authorization": "Bearer " + os.environ["ROTOWIRE_MCP_TOKEN"],
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    with urllib.request.urlopen(req, timeout=120) as r:
        text = r.read().decode()
    text = text[text.find("{"):]  # streamable HTTP may wrap the JSON in an SSE frame
    msg = json.loads(text)
    if "error" in msg:
        raise RuntimeError(f"{tool}: {msg['error']}")
    res = msg["result"]
    if res.get("isError"):
        raise RuntimeError(f"{tool}: {res['content'][0].get('text')}")
    return json.loads(res["content"][0]["text"])


def fetch_timeline(league: str, frm: str, to: str) -> list[dict]:
    rows, cursor = [], None
    while True:
        args = {"from": frm, "to": to, "leagues": [league]}
        if cursor:
            args["cursor"] = cursor
        page = _call("desk_speed_timeline", args)
        rows += page.get("rows", [])
        cursor = page.get("next_cursor")
        if not cursor:
            return rows


def sync(data_dir: Path, league: str) -> str:
    """Pull the last FETCH_DAYS of timeline rows and merge them into the stored file."""
    path = data_dir / "desk_timeline.jsonl"
    stored = {r["tweet_id"]: r for r in load_jsonl(path)}
    today = datetime.now(timezone.utc).date()
    frm = (today - timedelta(days=FETCH_DAYS - 1)).isoformat()
    fresh = fetch_timeline(league, frm, today.isoformat())
    for r in fresh:
        # The repo and its Pages site are public: keep timings, drop who did what.
        stored[r["tweet_id"]] = {k: v for k, v in r.items() if k not in PRIVATE_FIELDS}  # later pulls win
    rows = sorted(stored.values(), key=lambda r: r.get("record_created_at") or "")
    write_jsonl(path, rows)
    return f"{len(fresh)} rows pulled, {len(rows)} stored"


# ---------------------------------------------------------------- summarising

def _med(xs: list) -> int | None:
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs)) if xs else None


def _stat(xs: list) -> dict:
    xs = [x for x in xs if x is not None]
    return {"median": _med(xs), "n": len(xs)}


def _steps(rows: list[dict]) -> dict:
    """Step medians over Monitor-posted rows. Step 2 = draft + wait to open."""
    opened = [r for r in rows if r.get("draft_to_open_seconds") is not None
              and r.get("open_to_x_seconds") is not None]
    s2 = [(r.get("ingest_to_draft_seconds") or 0) + r["draft_to_open_seconds"] for r in opened]
    s23 = [r["tweet_to_x_seconds"] - (r.get("arrival_seconds") or 0) for r in rows
           if r.get("tweet_to_x_seconds") is not None]
    scored = [r for r in rows if r.get("tweet_edited") in ("yes", "no")]
    return {
        "sent_to_monitor": _stat([r.get("arrival_seconds") for r in rows]),
        "monitor_to_open": _stat(s2),
        "open_to_x": _stat([r["open_to_x_seconds"] for r in opened]),
        "monitor_to_x": _stat(s23),  # steps 2 + 3 together, measured on every row
        "split": len(opened) >= MIN_SPLIT_N,
        "edited": {"pct": round(100 * sum(r["tweet_edited"] == "yes" for r in scored) / len(scored), 1)
                   if scored else None, "n": len(scored)},
    }


def _join(stories: list[dict], rows: list[dict]) -> list[tuple[dict, dict]]:
    by_post: dict[int, list[dict]] = {}
    for r in rows:
        if r.get("posted_to_x_at"):
            by_post.setdefault(int(parse_dt(r["posted_to_x_at"]).timestamp()), []).append(r)
    pairs = []
    for s in stories:
        rw = s.get("rotowire")
        if not rw:
            continue
        t = int(parse_dt(rw["created_at"]).timestamp())
        for d in sorted(range(-JOIN_TOLERANCE_S, JOIN_TOLERANCE_S + 1), key=abs):
            hit = by_post.get(t + d)
            if hit and len(hit) == 1:
                pairs.append((s, hit[0]))
                break
    return pairs


def _race(pairs: list[tuple[dict, dict]]) -> dict:
    """The gap on stories both accounts posted, measured story by story.

    The gap is the per-story median of (our post - Underdog's post), never our median
    minus theirs: both distributions are long-tailed, so the difference of medians
    overstates the gap. Cutting S seconds from every one of our posts lowers every
    per-story gap by S, so `gaps` (sorted) answers "if we were S faster, how often would
    we be first" exactly: the share of gaps below S.

    Underdog sometimes posts before the source tweet our Monitor picked up (they had an
    earlier report). No step cut wins those; they stay in the gaps and are counted
    separately as a sourcing loss.
    """
    gaps, ud = [], []
    for s, r in pairs:
        if s.get("status") != "matched" or not s.get("underdog"):
            continue
        src = parse_dt(r["tweet_posted_at"])
        rw_t, ud_t = parse_dt(s["rotowire"]["created_at"]), parse_dt(s["underdog"]["created_at"])
        gaps.append(round((rw_t - ud_t).total_seconds()))
        ud.append((ud_t - src).total_seconds())
    n = len(gaps)
    return {
        "n": n,
        "median_gap": _med(gaps),
        "underdog_first_pct": round(100 * sum(g > 0 for g in gaps) / n, 1) if n else None,
        "underdog_before_source_pct": round(100 * sum(u < 0 for u in ud) / n, 1) if n else None,
        # Underdog's own time to post, on the stories where the source was the same start
        # for both of us (they posted after our source tweet). Context only, not the target.
        "underdog_from_source": _med([u for u in ud if u >= 0]),
        "gaps": sorted(gaps),
    }


def _path(stories: list[dict], pairs: list[tuple[dict, dict]] | None = None) -> dict:
    """Gap to Underdog for one set of our posts (all, from Monitor, or added by hand).

    Gaps are per story (our post minus Underdog's), so they work for any path. Time from
    source uses the tracker's own source lookup (rotowire_secs_from_source), which exists
    for Add Line posts too; Monitor posts additionally get the desk's step split.
    """
    matched = [s for s in stories if s.get("status") == "matched" and s.get("underdog")]
    gaps = sorted(round((parse_dt(s["rotowire"]["created_at"]) - parse_dt(s["underdog"]["created_at"])).total_seconds())
                  for s in matched)
    src = [s.get("rotowire_secs_from_source") for s in matched]
    out = {
        "posts": len(stories),
        "race": {
            "n": len(gaps),
            "median_gap": _med(gaps),
            "underdog_first_pct": round(100 * sum(g > 0 for g in gaps) / len(gaps), 1) if gaps else None,
            "gaps": gaps,
        },
        "from_source": _stat(src),
    }
    if pairs is not None:
        out["race"]["underdog_before_source_pct"] = _race(pairs)["underdog_before_source_pct"]
    return out


def _is_reply(story: dict, tweets: dict[str, dict]) -> bool:
    t = tweets.get(story["rotowire"].get("tweet_id")) or {}
    return any(r.get("type") == "replied_to" for r in t.get("referenced_tweets") or [])


def _paths(stories: list[dict], rows: list[dict]) -> dict:
    """Split our root posts into those sent from a Monitor tweet and the rest.

    The rest are almost all manual Add Line posts in News Admin; a small share (~2%) went
    out outside News Admin entirely. The timeline has no Add Line rows, so they can only be
    told apart by elimination until News Admin flags them.
    """
    pairs = _join(stories, rows)
    joined = {id(s) for s, _ in pairs}
    monitor = [s for s in stories if id(s) in joined]
    manual = [s for s in stories if id(s) not in joined]
    m = _path(monitor, pairs)
    m["steps"] = _steps(rows)
    total = len(stories)
    return {
        "all": _path(stories),
        "monitor": m,
        "manual": _path(manual),
        "manual_pct": round(100 * len(manual) / total, 1) if total else None,
    }


def _week(t: str) -> str:
    d = parse_dt(t).astimezone(TZ).date()
    return (d - timedelta(days=d.weekday())).isoformat()  # Monday of the week


def build(data_dir: Path, docs_dir: Path, stories: list[dict], now: datetime | None = None) -> dict | None:
    all_rows = [r for r in load_jsonl(data_dir / "desk_timeline.jsonl") if r.get("posted_to_x_at")]
    if not all_rows:
        return None
    tweets = {t["id"]: t for t in load_jsonl(data_dir / "tweets.jsonl")}
    now = now or datetime.now(timezone.utc)
    first_row = min(parse_dt(r["posted_to_x_at"]) for r in all_rows)
    start = max(now - timedelta(days=WINDOW_DAYS), first_row)
    in_win = lambda t, frm=start: t and frm <= parse_dt(t) <= now
    # Our root posts only: replies under our own tweets are follow-ups, not races.
    ours = [s for s in stories if s.get("rotowire") and not _is_reply(s, tweets)]
    rows = [r for r in all_rows if in_win(r["posted_to_x_at"])]
    win = [s for s in ours if in_win(s["rotowire"]["created_at"])]
    day = lambda t: WEEKDAYS[parse_dt(t).astimezone(TZ).weekday()]

    # Share of posts added by hand, week by week, over all the timeline history we hold.
    hist = [s for s in ours if in_win(s["rotowire"]["created_at"], first_row)]
    weeks: dict[str, list] = {}
    joined = {id(s) for s, _ in _join(hist, all_rows)}
    for s in hist:
        w = weeks.setdefault(_week(s["rotowire"]["created_at"]), [0, 0])
        w[0] += 1
        w[1] += id(s) not in joined
    weekly = [{"week": k, "posts": v[0], "manual": v[1], "pct": round(100 * v[1] / v[0], 1)}
              for k, v in sorted(weeks.items())]

    out = {
        "generated_at": now_iso(),
        "window": {"from": start.date().isoformat(), "to": now.date().isoformat(),
                   "days": (now - start).days + 1},
        "opens_tracked_since": "2026-10-13",
        "paths": _paths(win, rows),
        "manual_weekly": weekly,
        "weekdays": [],
    }
    for d in ["Sun", "Wed", "Mon", "Tue", "Thu", "Fri", "Sat"]:
        out["weekdays"].append({"day": d, "note": KEY_DAYS.get(d), "key": d in KEY_DAYS,
                                "paths": _paths([s for s in win if day(s["rotowire"]["created_at"]) == d],
                                                [r for r in rows if day(r["posted_to_x_at"]) == d])})

    path = docs_dir / "desk_speed.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    tmp.replace(path)
    return out


if __name__ == "__main__":
    import argparse

    from .config import sport_data_dir, sport_docs_dir

    ap = argparse.ArgumentParser(description="Pull Desk Speed rows and rebuild desk_speed.json.")
    ap.add_argument("--sport", default="nfl")
    ap.add_argument("--no-sync", action="store_true", help="rebuild from stored rows only")
    a = ap.parse_args()
    data_dir, docs_dir = sport_data_dir(a.sport), sport_docs_dir(a.sport)
    if not a.no_sync:
        print(f"[desk]      {sync(data_dir, a.sport) if has_credentials() else 'no ROTOWIRE_MCP_TOKEN, skipped sync'}")
    stories = json.loads((docs_dir / "stories.json").read_text())
    res = build(data_dir, docs_dir, stories)
    if res:
        for k, v in res["paths"].items():
            print(k, {kk: vv for kk, vv in v.items() if kk not in ("race", "steps")} if isinstance(v, dict) else v,
                  {kk: vv for kk, vv in v["race"].items() if kk != "gaps"} if isinstance(v, dict) else "")
        print("weekly", res["manual_weekly"])
