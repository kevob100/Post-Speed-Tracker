from __future__ import annotations

from datetime import datetime, timezone

from src import aggregate as agg
from src import focus


def st(sid, status, delta=None, ud="", rw="", ts="2026-10-01T16:00:00.000Z", **kw):
    side = lambda t: {"tweet_id": sid + t[:2], "created_at": ts, "text": t}  # noqa: E731
    return {"story_id": sid, "status": status, "time_delta_seconds": delta,
            "rotowire": side(rw) if status != "underdog_only" else None,
            "underdog": side(ud) if status != "rotowire_only" else None, **kw}


def test_groups_insiders_and_ranks_by_time_behind_plus_misses():
    stories = [st(f"i{n}", "matched", -600, ud="Rap: Joe Burrow expected to play.", rw="Burrow will play.")
               for n in range(10)]
    stories += [st(f"p{n}", "matched", -60, ud="Bryce Young fully practices Wednesday.",
                   rw="Bryce Young was a full participant on the practice report.") for n in range(10)]
    stories += [st("m1", "underdog_only", ud="Glazer: Mixon to visit Seahawks."),
                st("clip", "underdog_only", ud="Keon Coleman tips it to himself!", gap_kind="missed")]
    stories[-1]["underdog"]["borderline"] = "in_game_note"
    out = focus.rollup(stories, agg._story_time, datetime(2026, 10, 5, tzinfo=timezone.utc),
                       lambda t: None, {})
    top = out["groups"][0]
    assert top["key"] == "insider" and top["rank"] == 1 and top["missed"] == 1
    assert len(top["slowest"]) == 3 and top["slowest"][0]["delta_seconds"] == -600
    assert all(g["key"] != "comments" or g["missed"] == 0 for g in out["groups"])   # clip ignored


def test_group_of():
    assert focus.group_of({"underdog": {"text": "Schefter: Puka trending wrong way."}}) == "insider"
    assert focus.group_of({"underdog": {"text": "Status alert: X headed to locker room Sunday."}}) == "in_game_alert"
