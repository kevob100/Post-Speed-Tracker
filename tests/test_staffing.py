from __future__ import annotations

from datetime import datetime, timezone

from src import aggregate as agg
from src import staffing

SCHED = {"date": "2026-10-04", "timezone": "America/New_York",
         "roles": {"tweets": "Tweets (L1-2)", "writers": "Writers"},
         "slots": [
             {"start": "12:00", "tweets": ["Adam"], "writers": ["Eric", "Paul"]},
             {"start": "13:00", "tweets": [{"name": "Adam", "until": "13:50"}, "Cullum"],
              "writers": ["Eric"]},
         ]}


def test_on_duty_handles_shared_and_handoff():
    assert staffing.on_duty(["Eric", "Paul"], "12:10") == ["Eric", "Paul"]
    hand = [{"name": "Adam", "until": "13:50"}, "Cullum"]
    assert staffing.on_duty(hand, "13:49") == ["Adam"]
    assert staffing.on_duty(hand, "13:50") == ["Cullum"]


def _st(sid, utc_hhmm, status="matched", delta=-60, **kw):
    ts = f"2026-10-04T{utc_hhmm}:00.000Z"
    side = {"created_at": ts, "tweet_id": sid}
    return {"story_id": sid, "status": status, "time_delta_seconds": delta if status == "matched" else None,
            "rotowire_first": (delta > 0) if status == "matched" else None,
            "rotowire": side if status != "underdog_only" else None,
            "underdog": side if status != "rotowire_only" else None, **kw}


def test_rollup_credits_whoever_was_on_when_the_news_broke():
    # 16:30 UTC = 12:30 ET (Adam; Eric+Paul). 17:55 UTC = 13:55 ET (Cullum after hand-off).
    stories = [_st("a", "16:30", delta=30), _st("b", "17:55"), _st("c", "17:20", status="underdog_only"),
               _st("night", "04:00")]                         # 00:00 ET: not scheduled
    out = staffing.rollup(stories, [SCHED], agg._story_time, agg._summary)
    day = out["days"][0]
    tweets = {r["name"]: r for r in day["people"]["tweets"]}
    assert tweets["Adam"]["matched"] == 1 and tweets["Adam"]["rotowire_first"] == 1
    assert tweets["Adam"]["underdog_only"] == 1               # 13:20 ET, before the hand-off
    assert tweets["Cullum"]["matched"] == 1 and tweets["Cullum"]["rotowire_first"] == 0
    assert tweets["Adam"]["hours"] == round(1 + 50 / 60, 2) and tweets["Cullum"]["hours"] == round(10 / 60, 2)
    writers = {r["name"]: r for r in day["people"]["writers"]}
    assert writers["Paul"]["matched"] == 1 and writers["Eric"]["matched"] == 2
    assert day["total"]["matched"] == 2 and [s["matched"] for s in day["slots"]] == [1, 1]
