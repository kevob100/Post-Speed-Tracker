"""NFL game windows: how RotoWire compares while games are on.

Kickoffs come from ESPN's public scoreboard and are cached in data/<sport>/games.json, so
one-off slots (international, Saturday, holiday games) get windows automatically. A
game's window opens before kickoff (injury reports land ~90 minutes before) and closes
after it (sports.<sport>.game_windows in config.yaml):

  start = kickoff - before_hours, rounded DOWN to the hour
  end   = kickoff + after_hours (primetime_after_hours for a kickoff at or after
          primetime_from), rounded UP to the hour

Overlapping or touching windows merge, so a Sunday reads as one block (11am-8pm) plus the
night game (6pm-1am) folds into it when they meet.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import DEFAULT_TZ


ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
# (seasontype, weeks): 2 = regular season, 3 = postseason.
SEASON_PARTS = ((2, range(1, 19)), (3, range(1, 6)))


def fetch_games(year: int, session=None, timeout: int = 20) -> list[dict]:
    """Every regular-season and postseason game ESPN lists for a season."""
    import requests

    http = session or requests
    games: dict[str, dict] = {}
    for seasontype, weeks in SEASON_PARTS:
        for week in weeks:
            r = http.get(ESPN, params={"dates": year, "seasontype": seasontype, "week": week},
                         timeout=timeout)
            r.raise_for_status()
            for e in r.json().get("events") or []:
                games[str(e["id"])] = {"id": str(e["id"]), "kickoff": e["date"],
                                       "name": e.get("shortName") or e.get("name") or "",
                                       "seasontype": seasontype, "week": week}
    return sorted(games.values(), key=lambda g: (g["kickoff"], g["id"]))


def refresh_games(data_dir: Path, year: int, session=None) -> int:
    """Re-fetch the season's games into games.json. On any error keep the old file."""
    path = data_dir / "games.json"
    try:
        games = fetch_games(year, session=session)
    except Exception as exc:                       # network hiccup: keep what we have
        print(f"[games]     fetch failed ({exc}); keeping cached games.json")
        return -1
    if games:
        path.write_text(json.dumps(games, indent=1))
    return len(games)


def load_games(data_dir: Path) -> list[dict]:
    path = data_dir / "games.json"
    return json.loads(path.read_text()) if path.exists() else []


def windows(games: list[dict], cfg: dict) -> list[dict]:
    """Merged game windows: [{start, end (UTC datetimes), games: [{name, kickoff}]}]."""
    tz = ZoneInfo(cfg.get("timezone") or DEFAULT_TZ)
    before = timedelta(hours=float(cfg.get("before_hours", 2)))
    after = timedelta(hours=float(cfg.get("after_hours", 3)))
    late_after = timedelta(hours=float(cfg.get("primetime_after_hours", 4)))
    prime = str(cfg.get("primetime_from") or "19:00")

    spans = []
    for g in games:
        kick = datetime.fromisoformat(g["kickoff"].replace("Z", "+00:00"))   # "2026-10-04T17:00Z"
        local = kick.astimezone(tz)
        start = (local - before).replace(minute=0, second=0, microsecond=0)
        end = local + (late_after if local.strftime("%H:%M") >= prime else after)
        if end.minute or end.second or end.microsecond:
            end = end.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        spans.append((start, end, {"name": g["name"], "kickoff": kick.isoformat()}))
    spans.sort(key=lambda x: x[0])

    merged: list[dict] = []
    for start, end, game in spans:
        if merged and start <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], end)
            merged[-1]["games"].append(game)
        else:
            merged.append({"start": start, "end": end, "games": [game]})
    return merged


def rollup(stories: list[dict], games: list[dict], cfg: dict, story_time, summary,
           since: datetime | None = None, until: datetime | None = None) -> dict:
    """Per window, inside vs outside game windows, and by clock hour inside them."""
    tz = ZoneInfo(cfg.get("timezone") or DEFAULT_TZ)
    wins = [w for w in windows(games, cfg)
            if (since is None or w["end"] > since) and (until is None or w["start"] < until)]
    timed = [(story_time(s), s) for s in stories]
    if since is not None:
        timed = [(t, s) for t, s in timed if t >= since]

    inside_ids: set[str] = set()
    rows = []
    for w in wins:
        sts = [s for t, s in timed if w["start"] <= t < w["end"]]
        inside_ids.update(s["story_id"] for s in sts)
        row = {"start": w["start"].isoformat(), "end": w["end"].isoformat(),
               "date": w["start"].astimezone(tz).date().isoformat(),
               "label": f"{w['start'].astimezone(tz):%a %b} {w['start'].astimezone(tz).day}",
               "hours": round((w["end"] - w["start"]).total_seconds() / 3600, 1),
               "games": w["games"], "story_ids": [s["story_id"] for s in sts]}
        row.update(summary(sts))
        rows.append(row)

    hours = []
    by_hour: dict[int, list[dict]] = {}
    for t, s in timed:
        if s["story_id"] in inside_ids:
            by_hour.setdefault(t.astimezone(tz).hour, []).append(s)
    for h in sorted(by_hour):
        row = {"hour": h}
        row.update(summary(by_hour[h]))
        hours.append(row)

    inside = [s for _, s in timed if s["story_id"] in inside_ids]
    outside = [s for _, s in timed if s["story_id"] not in inside_ids]
    return {"windows": rows, "hours": hours, "inside": summary(inside), "outside": summary(outside),
            "inside_hours": round(sum(r["hours"] for r in rows), 1)}
