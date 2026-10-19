"""Monday Slack posts for #news-project-core, sent apart from the daily recap.

    python -m src.monday_posts weekly   # news speed weekly (news_recap), ~9:30am ET
    python -m src.monday_posts kob      # Underdog sources not on our list (slack_report), ~11am ET

Run by monday-posts.yml from the data the overnight run committed. Each waits for that run:
if it hasn't posted today's daily yet, the data isn't in, so it skips and the workflow's
second slot an hour later tries again. Both record what they sent, so a retry never doubles.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

from . import news_recap, slack_report
from .config import load_config, sport_data_dir, sport_docs_dir

SPORT = "nfl"


def main(kind: str) -> str:
    cfg = load_config()
    meta = cfg["sports"][SPORT]
    data_dir, docs_dir = sport_data_dir(SPORT), sport_docs_dir(SPORT)
    yesterday = (datetime.now(timezone.utc).astimezone(news_recap.ET) - timedelta(days=1)).date().isoformat()
    state_p = data_dir / news_recap.STATE_FILE
    state = json.loads(state_p.read_text()) if state_p.exists() else {}
    if state.get("daily") != yesterday:
        return "overnight run hasn't finished yet, skipped"
    if kind == "weekly":
        epic = (meta.get("desk_speed") or {}).get("roadmap_auto_epic")
        return news_recap.maybe_post(data_dir, docs_dir, epic, daily=False)
    if kind == "kob":
        cc = meta.get("underdog_credits") or {}
        out = json.loads((docs_dir / "credits.json").read_text())
        sheet = f"https://docs.google.com/spreadsheets/d/{cc['dismiss_sheet']}/edit" if cc.get("dismiss_sheet") else None
        return slack_report.maybe_post(data_dir, out, cc.get("names") or {}, meta["accounts"]["underdog"]["handle"], sheet)
    raise SystemExit(f"unknown post {kind!r}: weekly or kob")


if __name__ == "__main__":
    print(f"[{sys.argv[1] if len(sys.argv) > 1 else '?'}] {main(sys.argv[1] if len(sys.argv) > 1 else '')}")
