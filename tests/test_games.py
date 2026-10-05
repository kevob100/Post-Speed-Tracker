from __future__ import annotations

from datetime import datetime, timezone

from src import aggregate as agg
from src import games

CFG = {"timezone": "America/New_York", "before_hours": 2, "after_hours": 3,
       "primetime_from": "19:00", "primetime_after_hours": 4}
ET = "America/New_York"


def g(kick, name="A @ B"):
    return {"id": kick + name, "kickoff": kick, "name": name}


def _et(w):
    from zoneinfo import ZoneInfo
    z = ZoneInfo(ET)
    return w["start"].astimezone(z).strftime("%a %H:%M"), w["end"].astimezone(z).strftime("%a %H:%M")


def test_sunday_slate_merges_and_primetime_runs_to_one_am():
    # Sun Oct 4 2026: 1:00, 4:25, 8:20 pm ET kickoffs (17:00Z, 20:25Z, 00:20Z Mon).
    ws = games.windows([g("2026-10-04T17:00Z"), g("2026-10-04T20:25Z"), g("2026-10-05T00:20Z")], CFG)
    assert len(ws) == 1 and _et(ws[0]) == ("Sun 11:00", "Mon 01:00") and len(ws[0]["games"]) == 3


def test_monday_night_and_international_morning():
    mnf = games.windows([g("2026-10-06T00:15Z")], CFG)              # Mon 8:15 pm ET
    assert _et(mnf[0]) == ("Mon 18:00", "Tue 01:00")
    lon = games.windows([g("2026-10-04T13:30Z")], CFG)              # Sun 9:30 am ET
    assert _et(lon[0]) == ("Sun 07:00", "Sun 13:00")


def test_rollup_splits_inside_and_outside():
    def st(sid, ts):
        side = {"created_at": ts, "tweet_id": sid}
        return {"story_id": sid, "status": "matched", "time_delta_seconds": -60, "rotowire_first": False,
                "rotowire": side, "underdog": side}
    stories = [st("in", "2026-10-04T18:00:00.000Z"), st("out", "2026-10-03T15:00:00.000Z")]
    out = games.rollup(stories, [g("2026-10-04T17:00Z")], CFG, agg._story_time, agg._summary)
    assert out["inside"]["matched"] == 1 and out["outside"]["matched"] == 1
    assert out["windows"][0]["story_ids"] == ["in"] and out["hours"][0]["hour"] == 14
