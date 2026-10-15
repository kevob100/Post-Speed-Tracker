"""Weekly Slack post for KOB: Underdog's sources that aren't on our NFL Monitor list.

Posts once a week (Monday ET, first run of the day) to the channel behind SLACK_WEBHOOK_URL
(a Slack incoming webhook). Lists the accounts Underdog credited in the last 7 days that
aren't on the list and haven't been dismissed, what was added to the list that week, and
links to the tracker and the "not to add" sheet. Records the week it posted so reruns
don't post twice. Skipped without the webhook.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .credits import classify
from .store import load_jsonl, parse_dt

STATE_FILE = "slack_report_state.json"
TRACKER = "https://kevob100.github.io/Post-Speed-Tracker/#nfl"


def message(data_dir: Path, credits: dict, names: dict, underdog: str, sheet_url: str | None,
            now: datetime) -> str:
    start = now - timedelta(days=7)
    rows = {r["handle"].lower(): r for r in credits.get("accounts", [])}
    week: dict[str, int] = {}
    for t in load_jsonl(data_dir / "tweets.jsonl"):
        if t.get("account", "").lower() != underdog.lower() or not t.get("is_news") or parse_dt(t["created_at"]) < start:
            continue
        h = classify(t, names).get("handle")
        if h:
            week[h.lower()] = week.get(h.lower(), 0) + 1
    todo = sorted(((rows[k]["handle"], n) for k, n in week.items()
                   if k in rows and rows[k]["on_list"] is False and not rows[k].get("dismissed")),
                  key=lambda x: (-x[1], x[0].lower()))
    added = [a for a in credits.get("added", []) if a["date"] >= start.date().isoformat()]
    lines = ["*Underdog sources not on our NFL list, last 7 days*"]
    lines += [f"• <https://x.com/{h}|@{h}>: credited {n}x" for h, n in todo[:15]] or ["• None this week"]
    if len(todo) > 15:
        lines.append(f"…and {len(todo) - 15} more on the tracker")
    if added:
        lines.append("\n*Added to the list this week:* " + ", ".join(f"@{a['handle']}" for a in added))
    tail = f"<{TRACKER}|Full table on the tracker>"
    if sheet_url:
        tail += f" · Don't want one? Add it to <{sheet_url}|this sheet> and it won't show again."
    lines.append("\n" + tail)
    return "\n".join(lines)


def maybe_post(data_dir: Path, credits: dict, names: dict, underdog: str, sheet_url: str | None,
               now: datetime | None = None, force: bool = False) -> str:
    hook = os.environ.get("SLACK_WEBHOOK_URL")
    if not hook:
        return "no SLACK_WEBHOOK_URL, skipped"
    now = now or datetime.now(timezone.utc)
    et = now.astimezone(ZoneInfo("America/New_York"))
    week = f"{et.isocalendar().year}-W{et.isocalendar().week:02d}"
    state_p = data_dir / STATE_FILE
    state = json.loads(state_p.read_text()) if state_p.exists() else {}
    if not force and (et.weekday() != 0 or state.get("last_week") == week):
        return "not due"
    body = json.dumps({"text": message(data_dir, credits, names, underdog, sheet_url, now)}).encode()
    req = urllib.request.Request(hook, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()
    state_p.write_text(json.dumps({"last_week": week, "posted_at": now.isoformat()}, indent=2))
    return f"posted for {week}"
