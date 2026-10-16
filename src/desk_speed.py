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
Add Line posts come back as rows flagged manual (RD-8163); posts with no row at all went
out outside News Admin. Fewer Add Line posts is a goal in itself: each one is an editor
writing a post from scratch, so the share is tracked week by week.

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
import re
import statistics
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import desk_summary
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


EDIT_DAYS = 90          # how far back the editing trend is kept
EXAMPLE_DAYS = 3        # days re-pulled for draft-vs-posted examples each run (20 per day max)
EXAMPLES_KEPT = 60


def sync_edits(data_dir: Path, league: str) -> str:
    """X post edits: per day/version rows (accumulated past the 30-day window) and recent
    draft-vs-posted examples, which are what the templates get rewritten from."""
    today = datetime.now(timezone.utc).date()
    rows_path, ex_path = data_dir / "x_post_edits.jsonl", data_dir / "x_post_examples.jsonl"
    key = lambda r: f"{r['day']}|{r['league']}|{r['generate_prompt_version']}|{r.get('manual')}"  # noqa: E731
    stored = {key(r): r for r in load_jsonl(rows_path)}
    frm = (today - timedelta(days=FETCH_DAYS - 1)).isoformat()
    fresh = _call("x_post_edits", {"from": frm, "to": today.isoformat(), "leagues": [league]})["rows"]
    for r in fresh:
        stored[key(r)] = r
    write_jsonl(rows_path, sorted(stored.values(), key=key))

    ex = {e["article_id"]: e for e in load_jsonl(ex_path)}
    first = not ex
    for i in range(FETCH_DAYS if first else EXAMPLE_DAYS):
        day = (today - timedelta(days=i)).isoformat()
        for e in _call("x_post_edits", {"from": day, "to": day, "leagues": [league], "examples": True}).get("examples", []):
            ex[e["article_id"]] = e
    kept = sorted(ex.values(), key=lambda e: e["posted_at"], reverse=True)
    write_jsonl(ex_path, kept[:EXAMPLES_KEPT * 4])
    return f"{len(fresh)} edit rows, {len(kept)} examples"


# The NFL template sentence shapes (News Admin app/Services/Nfl/*Template.php), matched on
# the post's first line. Used to tell "a template wrote this draft" from "an editor typed the
# template's wording by hand", the gap the templates are meant to close.
TEMPLATE_FORMS = [
    ("practice_report", re.compile(r"\b(?:was (?:officially )?listed as an? |(?:officially )?remained an? |was (?:upgraded|downgraded) to an? |was an? )"
                                   r"(?:full |limited |non-)?participant (?:on|in) \w+'s (?:estimated )?(?:practice|injury) report", re.I)),
    ("practice_paraphrase", re.compile(r"^[^,(\n]+ \([^)\n]+\), per [^,\n]+, .+\.$")),
    ("final_report", re.compile(r"(?:practiced fully|was limited at|did not participate in) .+ (?:ruled out for|is listed as (?:questionable|doubtful) for"
                                r"|does not have an injury designation for)", re.I)),
    ("game_day", re.compile(r"\b(?:is inactive for|is listed as active for|(?:is|downgraded to) (?:questionable|doubtful) to return to|ruled out for the rest of"
                            r"|returned to \w+'s game|carted off during|helped off the field during|walked gingerly off the field during|exited during"
                            r"|will undergo (?:an MRI|X-rays|further testing))\b", re.I)),
    ("transaction", re.compile(r"\b(?:is scheduled to visit with the|worked out for the|is signing a contract with the|officially signed a contract with the"
                               r"|have agreed to an? \S+-year|is signing with the .+ practice squad|was elevated from the practice squad"
                               r"|is being (?:released|waived|traded|placed on injured reserve)|was (?:released|waived) by the|was claimed off waivers"
                               r"|was traded from the|was placed on injured reserve|was activated from injured reserve|was designated to return to practice"
                               r"|(?:Active|Reserve)/PUP list)\b", re.I)),
]


def template_form(text: str | None) -> str | None:
    """Which template's sentence shape the post's first line has, if any."""
    line = (text or "").split("\n")[0].strip()
    return next((name for name, rx in TEMPLATE_FORMS if rx.search(line)), None)


def _template_daily(data_dir: Path) -> list[dict]:
    """Per post day, over exact (stored) drafts only: how many drafts a template wrote vs how
    many posts went out in a template's wording. Examples are capped at 20 a day and trimmed,
    so each run folds its days into data/<sport>/edit_templates_daily.json and history stays."""
    path = data_dir / "edit_templates_daily.json"
    kept = json.loads(path.read_text()) if path.exists() else {}
    days: dict[str, list] = {}
    for e in load_jsonl(data_dir / "x_post_examples.jsonl"):
        if e.get("baseline_source") == "stored" and e.get("draft") and e.get("posted"):
            days.setdefault(e["posted_at"][:10], []).append(e)
    for d, g in days.items():
        kept[d] = {"sampled": len(g),
                   "drafted_by_template": sum(template_form(e["draft"]) is not None for e in g),
                   "posted_in_template_form": sum(template_form(e["posted"]) is not None for e in g),
                   "untouched": sum(e["draft"].strip() == e["posted"].strip() for e in g)}
    path.write_text(json.dumps(dict(sorted(kept.items())), indent=1) + "\n")
    return [{"day": d, **v} for d, v in sorted(kept.items())]


def _edits(data_dir: Path, timeline: list[dict], now: datetime) -> dict | None:
    """Daily % of posts edited and how much they change, opened-to-posted time, the same
    by template version, and the latest draft-vs-posted examples."""
    rows = load_jsonl(data_dir / "x_post_edits.jsonl")
    if not rows:
        return None
    start = (now - timedelta(days=EDIT_DAYS)).date().isoformat()
    rows = [r for r in rows if r["day"] >= start]

    def roll(group):
        sc = sum(r["scored"] for r in group)
        un = sum(r["unchanged"] for r in group)
        dist = sum((r["mean_distance"] or 0) * r["scored"] for r in group)
        stored = sum((r.get("baseline") or {}).get("stored", 0) for r in group)
        return {"posts": sum(r["posts"] for r in group), "scored": sc,
                "edited_pct": round(100 * (sc - un) / sc, 1) if sc else None,
                "change_pct": round(100 * dist / sc, 1) if sc else None,
                "stored_pct": round(100 * stored / sc, 1) if sc else None}

    by_day: dict[str, list] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r)
    open_x: dict[str, list] = {}
    for t in timeline:
        if t.get("open_to_x_seconds") is not None and t.get("posted_to_x_at") and not _is_manual(t):
            open_x.setdefault(t["posted_to_x_at"][:10], []).append(t["open_to_x_seconds"])
    daily = [{"day": d, **roll(g), "open_to_x": _stat(open_x.get(d, []))} for d, g in sorted(by_day.items())]

    by_ver: dict[str, list] = {}
    for r in rows:
        if r["generate_prompt_version"]:
            by_ver.setdefault(r["generate_prompt_version"], []).append(r)
    versions = sorted(({"version": v, "first_day": min(r["day"] for r in g), "last_day": max(r["day"] for r in g), **roll(g)}
                       for v, g in by_ver.items()), key=lambda x: x["first_day"])

    ex = sorted(load_jsonl(data_dir / "x_post_examples.jsonl"), key=lambda e: e["posted_at"], reverse=True)
    examples = [{k: e.get(k) for k in ("posted_at", "generate_prompt_version", "manual", "baseline_source", "distance", "draft", "posted")}
                for e in ex if e.get("draft") and e.get("posted")][:EXAMPLES_KEPT]
    last30 = [r for r in rows if r["day"] > (now - timedelta(days=30)).date().isoformat()]
    # Monitor drafts vs Add Line drafts (News Admin splits them since RD-8163, backfilled).
    by_path = {"monitor": roll([r for r in last30 if not r.get("manual")]),
               "manual": roll([r for r in last30 if r.get("manual")])}
    return {"daily": daily, "versions": versions, "examples": examples, "last30": roll(last30), "by_path": by_path,
            "stored_since": "2026-10-14", "templates": _template_daily(data_dir)}


# ---------------------------------------------------------------- summarising

def _med(xs: list) -> int | None:
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs)) if xs else None


def _stat(xs: list) -> dict:
    xs = [x for x in xs if x is not None]
    return {"median": _med(xs), "n": len(xs)}


def _steps(rows: list[dict]) -> dict:
    """Step medians over Monitor-posted rows. Step 2 = draft + wait to open."""
    rows = [r for r in rows if not _is_manual(r)]
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


def _is_manual(r: dict) -> bool:
    """An Add Line post (News Admin flags these since RD-8163); older rows are Monitor rows."""
    return r.get("manual") is True


def _source_at(r: dict) -> str | None:
    """When the source tweet went out. On an Add Line row tweet_posted_at is when the
    Add Line record was made, not a tweet, so the credited source tweet is used instead."""
    return r.get("source_tweet_posted_at") if _is_manual(r) else r.get("tweet_posted_at")


def _race(pairs: list[tuple[dict, dict]]) -> dict:
    """The gap on stories both accounts posted, measured story by story.

    The gap is the per-story median of (our post - Underdog's post), never our median
    minus theirs: both distributions are long-tailed, so the difference of medians
    overstates the gap. Cutting S seconds from every one of our posts lowers every
    per-story gap by S, so `gaps` (sorted) answers "if we were S faster, how often would
    we be first" exactly: the share of gaps below S.

    When Underdog posts before our source tweet, earlier_monitor_tweet (RD-8164) says
    whether the Monitor already held an earlier tweet about the player by then (the desk
    used a later one) or not (an account we don't watch, or a live press conference).
    """
    gaps, before, had = [], 0, 0
    for s, r in pairs:
        if s.get("status") != "matched" or not s.get("underdog"):
            continue
        rw_t, ud_t = parse_dt(s["rotowire"]["created_at"]), parse_dt(s["underdog"]["created_at"])
        gaps.append(round((rw_t - ud_t).total_seconds()))
        src = _source_at(r)
        if src and ud_t < parse_dt(src):
            before += 1
            e = r.get("earlier_monitor_tweet") or {}
            if e.get("ingested_at") and parse_dt(e["ingested_at"]) <= ud_t:
                had += 1
    n = len(gaps)
    return {
        "n": n,
        "median_gap": _med(gaps),
        "underdog_first_pct": round(100 * sum(g > 0 for g in gaps) / n, 1) if n else None,
        "underdog_before_source_pct": round(100 * before / n, 1) if n else None,
        # Of those: the Monitor already had an earlier tweet about the player when
        # Underdog posted, vs it never had one in time.
        "before_source": {"n": before, "monitor_had_earlier": had, "monitor_never_had": before - had},
        "gaps": sorted(gaps),
    }


def _path(stories: list[dict], pairs: list[tuple[dict, dict]] | None = None) -> dict:
    """Gap to Underdog for one set of our posts (all, from Monitor, added by hand, other).

    Time from source: News Admin's own source stamp where the row has one (Monitor rows,
    and Add Line posts that credit a source tweet), else the tracker's source lookup.
    """
    matched = [s for s in stories if s.get("status") == "matched" and s.get("underdog")]
    gaps = sorted(round((parse_dt(s["rotowire"]["created_at"]) - parse_dt(s["underdog"]["created_at"])).total_seconds())
                  for s in matched)
    row_of = {id(s): r for s, r in (pairs or [])}
    src = []
    for s in matched:
        r = row_of.get(id(s))
        at = _source_at(r) if r else None
        src.append((parse_dt(s["rotowire"]["created_at"]) - parse_dt(at)).total_seconds() if at
                   else s.get("rotowire_secs_from_source"))
    out = {
        "posts": len(stories),
        "race": {
            "n": len(gaps),
            "median_gap": _med(gaps),
            "underdog_first_pct": round(100 * sum(g > 0 for g in gaps) / len(gaps), 1) if gaps else None,
            "gaps": gaps,
        },
        "from_source": _stat([x for x in src if x is None or x >= 0]),
    }
    if pairs:
        r = _race(pairs)
        out["race"]["underdog_before_source_pct"] = r["underdog_before_source_pct"]
        out["race"]["before_source"] = r["before_source"]
    return out


def _is_reply(story: dict, tweets: dict[str, dict]) -> bool:
    t = tweets.get(story["rotowire"].get("tweet_id")) or {}
    return any(r.get("type") == "replied_to" for r in t.get("referenced_tweets") or [])


def _split(stories: list[dict], rows: list[dict]):
    """Our posts by how they were made: from a Monitor tweet, Add Line, or neither
    (posted outside News Admin)."""
    pairs = _join(stories, rows)
    mon = [(s, r) for s, r in pairs if not _is_manual(r)]
    man = [(s, r) for s, r in pairs if _is_manual(r)]
    joined = {id(s) for s, _ in pairs}
    other = [s for s in stories if id(s) not in joined]
    return mon, man, other


def _paths(stories: list[dict], rows: list[dict]) -> dict:
    mon, man, other = _split(stories, rows)
    m = _path([s for s, _ in mon], mon)
    m["steps"] = _steps([r for r in rows if not _is_manual(r)])
    h = _path([s for s, _ in man], man)
    hand = [r for r in rows if _is_manual(r)]
    h["add_line_to_x"] = _stat([r.get("add_line_to_x_seconds") for r in hand])
    total = len(stories)
    return {
        "all": _path(stories, mon + man),
        "monitor": m,
        "manual": h,
        "other": _path(other),
        "manual_pct": round(100 * len(man) / total, 1) if total else None,
        "other_pct": round(100 * len(other) / total, 1) if total else None,
    }


def _one_per_post(stories: list[dict]) -> list[dict]:
    """One entry per post of ours. A roundup post is split into a story per player, which
    would count the same post (and its race) several times; keep its earliest matched one."""
    best: dict[str, dict] = {}
    for s in stories:
        k = s["rotowire"].get("tweet_id")
        cur = best.get(k)
        matched = s.get("status") == "matched" and s.get("underdog")
        if cur is None or (matched and not (cur.get("status") == "matched" and cur.get("underdog"))) or (
                matched and parse_dt(s["underdog"]["created_at"]) < parse_dt(cur["underdog"]["created_at"])):
            best[k] = s
    return list(best.values())


def _week(t: str) -> str:
    d = parse_dt(t).astimezone(TZ).date()
    return (d - timedelta(days=d.weekday())).isoformat()  # Monday of the week


def _post_steps(stories: list[dict], rows: list[dict]) -> dict[str, dict]:
    """Each of our posts' own desk times, keyed by our tweet id, for the one-day story view.
    Monitor posts carry the three steps; Add Line posts only the total from source."""
    out = {}
    for s, r in _join(stories, rows):
        hand, opened = _is_manual(r), r.get("draft_to_open_seconds")
        out[s["rotowire"]["tweet_id"]] = {
            "hand": hand,
            "monitor": None if hand else r.get("arrival_seconds"),
            "open": None if hand or opened is None else (r.get("ingest_to_draft_seconds") or 0) + opened,
            "post": None if hand else r.get("open_to_x_seconds"),
            "total": r.get("tweet_to_x_seconds"),
            "edited": r.get("tweet_edited"),
        }
    return out


def build(data_dir: Path, docs_dir: Path, stories: list[dict], now: datetime | None = None,
          cfg: dict | None = None) -> dict | None:
    all_rows = [r for r in load_jsonl(data_dir / "desk_timeline.jsonl") if r.get("posted_to_x_at")]
    if not all_rows:
        return None
    tweets = {t["id"]: t for t in load_jsonl(data_dir / "tweets.jsonl")}
    now = now or datetime.now(timezone.utc)
    first_row = min(parse_dt(r["posted_to_x_at"]) for r in all_rows)
    start = max(now - timedelta(days=WINDOW_DAYS), first_row)
    in_win = lambda t, frm=start: t and frm <= parse_dt(t) <= now
    # Our root posts only: replies under our own tweets are follow-ups, not races.
    ours = _one_per_post([s for s in stories if s.get("rotowire") and not _is_reply(s, tweets)])
    rows = [r for r in all_rows if in_win(r["posted_to_x_at"])]
    win = [s for s in ours if in_win(s["rotowire"]["created_at"])]
    day = lambda t: WEEKDAYS[parse_dt(t).astimezone(TZ).weekday()]

    # Share of posts added by hand, week by week, over all the timeline history we hold.
    hist = [s for s in ours if in_win(s["rotowire"]["created_at"], first_row)]
    weeks: dict[str, list] = {}
    _, man, _ = _split(hist, all_rows)
    hand = {id(s) for s, _ in man}
    for s in hist:
        w = weeks.setdefault(_week(s["rotowire"]["created_at"]), [0, 0])
        w[0] += 1
        w[1] += id(s) in hand
    weekly = [{"week": k, "posts": v[0], "manual": v[1], "pct": round(100 * v[1] / v[0], 1)}
              for k, v in sorted(weeks.items())]

    out = {
        "generated_at": now_iso(),
        "window": {"from": start.date().isoformat(), "to": now.date().isoformat(),
                   "days": (now - start).days + 1},
        "opens_tracked_since": "2026-10-13",
        "paths": _paths(win, rows),
        "manual_weekly": weekly,
        "edits": _edits(data_dir, all_rows, now),
        "weekdays": [],
        "posts": _post_steps(hist, all_rows),
    }
    out["sheets"] = (cfg or {}).get("sheets", [])
    if out["edits"] and (cfg or {}).get("edit_breakdown"):
        out["edits"]["breakdown"] = cfg["edit_breakdown"]  # hand-labeled, see config.yaml
    def manual_pct(a, b):
        sub = [s for s in ours if a <= parse_dt(s["rotowire"]["created_at"]) < b]
        _, man_ab, _ = _split(sub, all_rows)
        return {"value": round(100 * len(man_ab) / len(sub), 1) if sub else None, "n": len(sub), "unit": "%"}

    out["summary"] = desk_summary.build(cfg or {}, data_dir, all_rows, stories, manual_pct, now)
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
        if has_credentials():
            print(f"[desk]      {sync(data_dir, a.sport)}")
            print(f"[desk]      {sync_edits(data_dir, a.sport)}")
        else:
            print("[desk]      no ROTOWIRE_MCP_TOKEN, skipped sync")
    from .config import load_config
    cfg = load_config()["sports"][a.sport].get("desk_speed") or {}
    if has_credentials() and not a.no_sync:
        keys = [t["key"] for st in cfg.get("roadmap", []) for t in st.get("tickets", [])]
        print(f"[desk]      {desk_summary.sync_tickets(data_dir, keys)}")
        root = (cfg.get("initiative") or {}).get("root")
        if root:
            print(f"[desk]      {desk_summary.sync_initiative(data_dir, root)}")
    stories = json.loads((docs_dir / "stories.json").read_text())
    res = build(data_dir, docs_dir, stories, cfg=cfg)
    if res:
        for k, v in res["paths"].items():
            print(k, {kk: vv for kk, vv in v.items() if kk not in ("race", "steps")} if isinstance(v, dict) else v,
                  {kk: vv for kk, vv in v["race"].items() if kk != "gaps"} if isinstance(v, dict) else "")
        print("weekly", res["manual_weekly"])
