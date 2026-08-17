from __future__ import annotations

import json

from src import aggregate as agg
from src.store import write_jsonl


def _matched(sid, rw_min, ud_min, day=29, **over):
    rw = f"2026-05-{day:02d}T18:{rw_min:02d}:00.000Z"
    ud = f"2026-05-{day:02d}T18:{ud_min:02d}:00.000Z"
    delta = (ud_min - rw_min) * 60
    s = {
        "story_id": sid,
        "status": "matched",
        "player": "Christian Yelich",
        "rotowire": {"tweet_id": f"rw{sid}", "created_at": rw, "impression_count": 100},
        "underdog": {"tweet_id": f"ud{sid}", "created_at": ud, "impression_count": 50},
        "time_delta_seconds": delta,
        "rotowire_first": delta > 0,
    }
    s.update(over)
    return s


def _one_sided(sid, status, minute, day=29):
    ts = f"2026-05-{day:02d}T18:{minute:02d}:00.000Z"
    side = {"tweet_id": f"x{sid}", "created_at": ts, "impression_count": 10}
    return {
        "story_id": sid,
        "status": status,
        "player": "Someone",
        "rotowire": side if status == "rotowire_only" else None,
        "underdog": side if status == "underdog_only" else None,
        "time_delta_seconds": None,
        "rotowire_first": None,
    }


def test_same_event_duplicates_counted_separately_from_true_gaps():
    # A same-event duplicate (lost the 1-to-1 match) must NOT inflate the true
    # coverage-gap counts; it's tallied under *_duplicate instead.
    stories = agg.apply_reviews([
        _matched("a", 0, 8),
        _one_sided("g", "underdog_only", 5),                          # true gap
        _one_sided("d", "underdog_only", 6, ) | {"same_event_duplicate": True,
                                                 "duplicate_of": "a"},
    ], [])
    s = agg._summary(stories)
    assert s["underdog_only"] == 1       # only the true gap
    assert s["underdog_duplicate"] == 1  # the duplicate, separately
    assert s["rotowire_duplicate"] == 0


def test_summary_win_rate_and_leads():
    stories = agg.apply_reviews(
        [_matched("a", 0, 8), _matched("b", 0, 2), _matched("c", 10, 4)], []
    )
    s = agg._summary(stories)
    assert s["matched"] == 3
    assert s["rotowire_first"] == 2          # a(+480), b(+120) ahead; c(-360) behind
    assert s["rotowire_first_rate"] == round(2 / 3, 4)
    assert s["median_lead_seconds"] == 120.0  # median of [480,120,-360]
    assert s["mean_lead_seconds"] == 80.0     # (480+120-360)/3
    # Lead/Trail split by who posted first: leads=[480,120], trails=[360].
    assert s["avg_lead_seconds"] == 300.0     # (480+120)/2
    assert s["avg_trail_seconds"] == 360.0    # 360/1


def test_coverage_gaps_excluded_from_timing():
    stories = agg.apply_reviews(
        [_matched("a", 0, 8), _one_sided("b", "rotowire_only", 5), _one_sided("c", "underdog_only", 6)],
        [],
    )
    s = agg._summary(stories)
    assert s["matched"] == 1
    assert s["rotowire_only"] == 1
    assert s["underdog_only"] == 1
    assert s["median_lead_seconds"] == 480.0  # only the matched story counts


def test_rejected_review_excluded():
    reviews = [{"story_id": "b", "decision": "reject", "reviewed_at": "2026-05-29T20:00:00Z"}]
    stories = agg.apply_reviews([_matched("a", 0, 8), _matched("b", 0, 2)], reviews)
    s = agg._summary(stories)
    assert s["matched"] == 1  # b rejected
    assert next(x for x in stories if x["story_id"] == "b")["review_status"] == "rejected"


def test_confirm_kept_and_latest_review_wins():
    reviews = [
        {"story_id": "a", "decision": "reject", "reviewed_at": "2026-05-29T20:00:00Z"},
        {"story_id": "a", "decision": "confirm", "reviewed_at": "2026-05-29T21:00:00Z"},
    ]
    stories = agg.apply_reviews([_matched("a", 0, 8)], reviews)
    assert stories[0]["review_status"] == "confirmed"
    assert agg._summary(stories)["matched"] == 1


def test_merged_excluded_from_metrics():
    reviews = [{"story_id": "b", "decision": "merge", "merge_into": "a",
                "reviewed_at": "2026-05-29T20:00:00Z"}]
    stories = agg.apply_reviews([_matched("a", 0, 8), _matched("b", 1, 3)], reviews)
    s = agg._summary(stories)
    assert s["matched"] == 1
    b = next(x for x in stories if x["story_id"] == "b")
    assert b["review_status"] == "merged"
    assert b["merge_into"] == "a"


def test_weekly_monthly_rollup_grouping():
    # Two stories in late May (ISO week 22), one in mid-April (a different month).
    april = _matched("c", 0, 4)
    april["rotowire"]["created_at"] = "2026-04-15T18:00:00.000Z"
    april["underdog"]["created_at"] = "2026-04-15T18:04:00.000Z"
    stories = agg.apply_reviews([_matched("a", 0, 8, day=29), _matched("b", 0, 2, day=28), april], [])

    monthly = agg._rollup(stories, agg._month)
    periods = {r["period"]: r["matched"] for r in monthly}
    assert periods == {"2026-04": 1, "2026-05": 2}

    weekly = agg._rollup(stories, agg._iso_week)
    wk = {r["period"]: r["matched"] for r in weekly}
    assert wk["2026-W22"] == 2  # May 28-29, 2026 fall in ISO week 22
    assert sum(wk.values()) == 3


def test_build_writes_valid_empty_when_no_stories(tmp_path):
    out = tmp_path / "docs_data"
    agg.build_aggregates(
        data_dir=tmp_path, docs_data_dir=out,
        stories_path=tmp_path / "missing_stories.jsonl",
        reviews_path=tmp_path / "missing_reviews.jsonl",
    )
    stories = json.loads((out / "stories.json").read_text())
    aggregates = json.loads((out / "aggregates.json").read_text())
    assert stories == []
    assert aggregates["summary"]["matched"] == 0
    assert aggregates["summary"]["rotowire_first_rate"] is None
    assert aggregates["weekly"] == [] and aggregates["monthly"] == []


def test_build_end_to_end_with_files(tmp_path):
    out = tmp_path / "docs_data"
    write_jsonl(tmp_path / "stories.jsonl", [_matched("a", 0, 8), _one_sided("b", "underdog_only", 5)])
    write_jsonl(tmp_path / "reviews.jsonl", [])
    res = agg.build_aggregates(data_dir=tmp_path, docs_data_dir=out)
    assert res["summary"]["matched"] == 1
    assert res["summary"]["underdog_only"] == 1
    assert len(json.loads((out / "stories.json").read_text())) == 2
