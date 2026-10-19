"""NFL news speed recap for #news-project-core, posted through SLACK_WEBHOOK_URL.

Daily (every day, first run after midnight ET): yesterday's share of shared stories we posted
first and the typical gap to Underdog, each next to the last 7 days. Some days move a lot, so
the single day and the rolling week sit side by side.

Weekly (Mondays, after the daily): the Mon-Sun week that just ended against the week before:
share first, typical gap, ties, how many AI drafts editors changed, the three step times, and
the Highest-priority Tweet Speed tickets in the current sprint.

"First" means more than a minute ahead; within a minute either way is a tie (desk_summary's
TIE_SECONDS). Records what it posted so reruns don't post twice. Skipped without the webhook.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import desk_speed, desk_summary as ds
from .store import load_jsonl, parse_dt

ET = ZoneInfo("America/New_York")
STATE_FILE = "news_recap_state.json"
TRACKER = "https://kevob100.github.io/Post-Speed-Tracker/#nfl"
JIRA = "https://gdcgroup.atlassian.net/browse"


def _dur(s: float | None) -> str:
    if s is None:
        return "n/a"
    s = round(abs(s))
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def _gap_text(g: float | None) -> str:
    if g is None:
        return "n/a"
    return f"Underdog {_dur(g)} ahead" if g > 0 else f"RotoWire {_dur(g)} ahead" if g < 0 else "even"


def _pct(x: float | None, dp: int = 0) -> str:
    return "n/a" if x is None else f"{x:.{dp}f}%"


def race(stories: list[dict], a: datetime, b: datetime) -> dict:
    """Shared stories whose race ended in [a, b): share we were first, ties, median gap."""
    g = [ds._gap(s) for s in stories if s.get("status") == "matched" and s.get("rotowire")
         and s.get("underdog") and a <= ds._at(s) < b]
    n = len(g)
    return {"n": n, "gap": ds._med(g),
            "first": 100 * sum(x < -ds.TIE_SECONDS for x in g) / n if n else None,
            "tie": 100 * sum(abs(x) <= ds.TIE_SECONDS for x in g) / n if n else None}


def edits(daily: list[dict], a: datetime, b: datetime) -> dict:
    g = [x for x in daily if a.date().isoformat() <= x["day"] < b.date().isoformat()]
    sc = sum(x["scored"] for x in g)
    if not sc:
        return {"n": 0, "edited": None, "changed": None}
    return {"n": sc, "edited": sum(x["scored"] * x["edited_pct"] for x in g) / sc,
            "changed": sum(x["scored"] * (x["change_pct"] or 0) for x in g) / sc}


def steps(rows: list[dict], a: datetime, b: datetime) -> dict:
    return desk_speed._steps([r for r in rows if a <= parse_dt(r["posted_to_x_at"]) < b])


def priorities(data_dir: Path, epic: str, now: datetime, limit: int = 5) -> list[dict]:
    """Highest-priority tickets under the epic in the sprint whose dates hold today, not done."""
    iso = now.isoformat()
    out = []
    for i in ds._initiative_issues(data_dir).get("issues", []):
        if i.get("parent") != epic or i["category"] == "done" or i.get("priority") != "Highest":
            continue
        if any(x.get("start") and x.get("end") and x["start"] <= iso <= x["end"] for x in i.get("sprints") or []):
            out.append(i)
    rank = {"indeterminate": 0, "new": 1}
    return sorted(out, key=lambda i: (rank.get(i["category"], 2), i["key"]))[:limit]


def daily_message(stories: list[dict], day: datetime) -> str:
    a, b = day, day + timedelta(days=1)
    d, w = race(stories, a, b), race(stories, b - timedelta(days=7), b)
    return "\n".join([
        f"*NFL News Speed - {day.strftime('%A %b')} {day.day}*",
        f"We post first: *{_pct(d['first'])}* of {d['n']} shared stories that day · "
        f"{_pct(w['first'])} of {w['n']} over the last 7 days",
        f"Typical gap: *{_gap_text(d['gap'])}* that day · {_gap_text(w['gap'])} over the last 7 days",
        f"<{TRACKER}|Full dashboard>",
    ])


def weekly_message(data_dir: Path, stories: list[dict], rows: list[dict], edit_daily: list[dict],
                   start: datetime, epic: str | None, now: datetime) -> str:
    end, prev = start + timedelta(days=7), start - timedelta(days=7)
    r, rp = race(stories, start, end), race(stories, prev, start)
    e, ep = edits(edit_daily, start, end), edits(edit_daily, prev, start)
    s, sp = steps(rows, start, end), steps(rows, prev, start)
    last = end - timedelta(days=1)

    def was(old, fmt):
        return f" (was {fmt(old)})" if old is not None else ""

    lines = [f"*NFL News Speed - Week of {start.strftime('%b')} {start.day}-"
             f"{'' if start.month == last.month else last.strftime('%b ')}{last.day}*", ""]
    move = ""
    if r["first"] is not None and rp["first"] is not None:
        move = f", {round(r['first'], 1) - round(rp['first'], 1):+.1f} pts"
    first_was = f" (was {_pct(rp['first'], 1)}{move})" if rp["first"] is not None else ""
    lines.append(f"We post first: *{_pct(r['first'], 1)}* of {r['n']} shared stories{first_was}")
    closer = ""
    if r["gap"] is not None and rp["gap"] is not None and r["gap"] != rp["gap"]:
        closer = f", {_dur(r['gap'] - rp['gap'])} {'closer' if r['gap'] < rp['gap'] else 'further behind'}"
    gap_was = f" (was {_dur(rp['gap'])}{closer})" if rp["gap"] is not None else ""
    lines.append(f"Typical gap: *{_gap_text(r['gap'])}*{gap_was}")
    lines.append(f"Ties within a minute: {_pct(r['tie'], 1)}{was(rp['tie'], lambda v: _pct(v, 1))}")
    lines.append("")
    lines.append(f"AI drafts edited before posting: *{_pct(e['edited'])}* of {e['n']}"
                 f"{was(ep['edited'], _pct)}")
    lines.append(f"Share of each draft changed: {_pct(e['changed'])}{was(ep['changed'], _pct)}")
    lines.append("")
    lines.append("Where the time goes (medians, posts from the Monitor):")
    for i, (k, label) in enumerate([("sent_to_monitor", "Tweet to in Monitor"), ("monitor_to_open", "In Monitor to opened"),
                                    ("open_to_x", "Opened to posted")], 1):
        lines.append(f"{i}. {label}: {_dur(s[k]['median'])}{was(sp[k]['median'], _dur)}")
    if epic:
        top = priorities(data_dir, epic, now)
        if top:
            lines += ["", "Top priorities this sprint:"]
            lines += [f"• <{JIRA}/{i['key']}|{i['key']}> {i['summary'].replace('Breaking News AI: ', '')} ({i['status']})"
                      for i in top]
    lines += ["", f"<{TRACKER}|Full dashboard>"]
    return "\n".join(lines)


def _send(hook: str, text: str) -> None:
    req = urllib.request.Request(hook, data=json.dumps({"text": text, "unfurl_links": False}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def maybe_post(data_dir: Path, docs_dir: Path, epic: str | None, now: datetime | None = None,
               force: bool = False) -> str:
    hook = os.environ.get("NEWS_SLACK_WEBHOOK_URL") or os.environ.get("SLACK_WEBHOOK_URL")
    if not hook:
        return "no SLACK_WEBHOOK_URL, skipped"
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    state_p = data_dir / STATE_FILE
    state = json.loads(state_p.read_text()) if state_p.exists() else {}
    stories = json.loads((docs_dir / "stories.json").read_text())
    stories = stories.get("stories", stories) if isinstance(stories, dict) else stories
    done = []
    if force or state.get("daily") != yesterday.date().isoformat():
        _send(hook, daily_message(stories, yesterday))
        state["daily"] = yesterday.date().isoformat()
        done.append("daily")
    week_start = today - timedelta(days=7)
    if today.weekday() == 0 and (force or state.get("weekly") != week_start.date().isoformat()):
        rows = [r for r in load_jsonl(data_dir / "desk_timeline.jsonl") if r.get("posted_to_x_at")]
        dsj = json.loads((docs_dir / "desk_speed.json").read_text()) if (docs_dir / "desk_speed.json").exists() else {}
        _send(hook, weekly_message(data_dir, stories, rows, (dsj.get("edits") or {}).get("daily") or [],
                                   week_start, epic, now))
        state["weekly"] = week_start.date().isoformat()
        done.append("weekly")
    state["posted_at"] = now.isoformat()
    state_p.write_text(json.dumps(state, indent=2))
    return "posted " + " + ".join(done) if done else "not due"
