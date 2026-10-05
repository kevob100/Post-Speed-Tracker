"""RotoWire follower history from X Analytics exports (data/<sport>/x_analytics/*.csv).

X only reports an account's current follower count, so followers.jsonl starts the day
collection did. The account owner's X Analytics export (analytics.x.com, account overview,
CSV) has one row per day with new follows and unfollows; walking those back from the
earliest real count gives the daily total before it. Rows the export and followers.jsonl
both cover keep the real count.

Columns are matched loosely ("Date", "New follows", "Unfollows"; case and spacing don't
matter). Several files may overlap; a later file wins for a date.
"""
from __future__ import annotations

import csv
import re
from datetime import date, datetime, timedelta
from pathlib import Path


def _key(h: str) -> str:
    return re.sub(r"[^a-z]", "", (h or "").lower())


def _date(v: str) -> date | None:
    v = (v or "").strip()
    for fmt in ("%Y-%m-%d", "%a, %b %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _int(v) -> int:
    try:
        return int(float(str(v).replace(",", "").strip() or 0))
    except ValueError:
        return 0


def load_daily(folder: Path) -> dict[str, dict]:
    """{YYYY-MM-DD: {new_follows, unfollows, impressions}} from every CSV in folder."""
    out: dict[str, dict] = {}
    for path in sorted(folder.glob("*.csv")):
        with path.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                r = {_key(k): v for k, v in row.items() if k}
                d = _date(r.get("date", ""))
                if not d:
                    continue
                out[d.isoformat()] = {
                    "new_follows": _int(r.get("newfollows") or r.get("follows") or r.get("newfollowers")),
                    "unfollows": _int(r.get("unfollows")),
                    "impressions": _int(r.get("impressions")) if r.get("impressions") not in (None, "") else None,
                }
    return out


def backfill(series: list[dict], daily: dict[str, dict]) -> list[dict]:
    """Extend a real follower series ([{date, followers}], oldest first) back in time with
    the export's daily net follows. A day's count is the next day's count minus that next
    day's net gain. Added points carry source: x_analytics."""
    if not series or not daily:
        return series
    real = {p["date"] for p in series}
    d = date.fromisoformat(series[0]["date"])
    count = series[0]["followers"]
    out = []
    # The count on day d-1 is the count on day d minus day d's net follows.
    while d.isoformat() in daily:
        net = daily[d.isoformat()]
        count -= net["new_follows"] - net["unfollows"]
        d -= timedelta(days=1)
        out.append({"date": d.isoformat(), "followers": count, "source": "x_analytics"})
    out.reverse()
    return [p for p in out if p["date"] not in real] + series
