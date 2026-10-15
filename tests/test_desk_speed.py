from __future__ import annotations

from src import desk_speed as ds


def _story(sid, rw, ud=None):
    s = {"story_id": sid, "status": "matched" if ud else "rotowire_only",
         "rotowire": {"tweet_id": f"rw{sid}", "created_at": rw}}
    if ud:
        s["underdog"] = {"tweet_id": f"ud{sid}", "created_at": ud}
    return s


def _row(tid, src, posted, **over):
    r = {"tweet_id": tid, "tweet_posted_at": src, "posted_to_x_at": posted,
         "arrival_seconds": 30, "ingest_to_draft_seconds": 5, "draft_to_open_seconds": 60,
         "open_to_x_seconds": 25, "tweet_to_x_seconds": 120, "tweet_edited": "yes"}
    r.update(over)
    return r


def test_join_matches_on_post_time_within_tolerance():
    stories = [_story(1, "2026-10-12T18:02:00Z", "2026-10-12T18:01:00Z"),
               _story(2, "2026-10-12T19:00:00Z")]
    rows = [_row("a", "2026-10-12T18:00:00Z", "2026-10-12T18:02:01Z")]
    pairs = ds._join(stories, rows)
    assert [s["story_id"] for s, _ in pairs] == [1]


def test_race_uses_per_story_gap_and_flags_earlier_sourcing():
    stories = [
        _story(1, "2026-10-12T18:03:00Z", "2026-10-12T18:01:00Z"),  # we're 120s behind
        _story(2, "2026-10-12T19:01:00Z", "2026-10-12T19:02:00Z"),  # we're 60s ahead
        _story(3, "2026-10-12T20:05:00Z", "2026-10-12T19:58:00Z"),  # UD before our source
    ]
    rows = [_row("a", "2026-10-12T18:00:00Z", "2026-10-12T18:03:00Z"),
            _row("b", "2026-10-12T19:00:00Z", "2026-10-12T19:01:00Z"),
            _row("c", "2026-10-12T20:00:00Z", "2026-10-12T20:05:00Z")]
    race = ds._race(ds._join(stories, rows))
    assert race["n"] == 3
    assert race["gaps"] == [-60, 120, 420]
    assert race["median_gap"] == 120
    assert race["underdog_first_pct"] == round(200 / 3, 1)
    assert race["underdog_before_source_pct"] == round(100 / 3, 1)


def test_steps_fall_back_to_combined_without_enough_opens():
    rows = [_row(str(i), "2026-10-12T18:00:00Z", "2026-10-12T18:02:00Z") for i in range(3)]
    st = ds._steps(rows)
    assert st["split"] is False
    assert st["sent_to_monitor"]["median"] == 30
    assert st["monitor_to_x"]["median"] == 90
    assert st["edited"] == {"pct": 100.0, "n": 3}


def test_paths_split_monitor_add_line_and_other():
    stories = [
        _story(1, "2026-10-12T18:03:00Z", "2026-10-12T18:01:00Z"),  # from Monitor
        _story(2, "2026-10-12T19:01:00Z", "2026-10-12T19:02:00Z"),  # Add Line
        _story(3, "2026-10-12T20:00:00Z"),                          # outside News Admin
    ]
    rows = [_row("a", "2026-10-12T18:00:00Z", "2026-10-12T18:03:00Z"),
            _row("manual:x", "2026-10-12T19:00:50Z", "2026-10-12T19:01:00Z", manual=True,
                 source_tweet_posted_at="2026-10-12T18:58:00Z", add_line_to_x_seconds=40,
                 arrival_seconds=0)]
    p = ds._paths(stories, rows)
    assert p["monitor"]["posts"] == 1 and p["manual"]["posts"] == 1 and p["other"]["posts"] == 1
    assert p["manual_pct"] == round(100 / 3, 1)
    assert p["manual"]["race"]["gaps"] == [-60]
    assert p["manual"]["from_source"]["median"] == 180  # from the credited source tweet, not the record
    assert p["manual"]["add_line_to_x"] == {"median": 40, "n": 1}
    assert p["monitor"]["steps"]["sent_to_monitor"]["n"] == 1  # Add Line rows stay out of the steps


def test_before_source_split_uses_earlier_monitor_tweet():
    stories = [_story(1, "2026-10-12T18:05:00Z", "2026-10-12T17:59:00Z"),
               _story(2, "2026-10-12T19:05:00Z", "2026-10-12T18:59:00Z")]
    rows = [_row("a", "2026-10-12T18:00:00Z", "2026-10-12T18:05:00Z",
                 earlier_monitor_tweet={"tweet_id": "e", "account": "X", "posted_at": "2026-10-12T17:57:00Z",
                                        "ingested_at": "2026-10-12T17:58:00Z"}),
            _row("b", "2026-10-12T19:00:00Z", "2026-10-12T19:05:00Z")]
    r = ds._race(ds._join(stories, rows))
    assert r["before_source"] == {"n": 2, "monitor_had_earlier": 1, "monitor_never_had": 1}


def test_one_per_post_collapses_roundups():
    a = _story(1, "2026-10-12T18:03:00Z", "2026-10-12T18:05:00Z")
    b = _story(2, "2026-10-12T18:03:00Z", "2026-10-12T18:01:00Z")
    b["rotowire"]["tweet_id"] = a["rotowire"]["tweet_id"]
    c = _story(3, "2026-10-12T19:00:00Z")
    out = ds._one_per_post([a, b, c])
    assert [s["story_id"] for s in out] == [2, 3]  # earliest matched Underdog post kept


def test_post_steps_keys_each_post_by_our_tweet_and_splits_monitor_from_add_line():
    from src.desk_speed import _post_steps
    stories = [{"rotowire": {"tweet_id": "a", "created_at": "2026-10-14T15:00:00Z"}},
               {"rotowire": {"tweet_id": "b", "created_at": "2026-10-14T16:00:00Z"}}]
    rows = [{"posted_to_x_at": "2026-10-14T15:00:01Z", "arrival_seconds": 30, "ingest_to_draft_seconds": 5,
             "draft_to_open_seconds": 40, "open_to_x_seconds": 50, "tweet_to_x_seconds": 125, "tweet_edited": "yes"},
            {"posted_to_x_at": "2026-10-14T16:00:00Z", "manual": True, "arrival_seconds": 0, "tweet_to_x_seconds": 300}]
    out = _post_steps(stories, rows)
    assert out["a"] == {"hand": False, "monitor": 30, "open": 45, "post": 50, "total": 125, "edited": "yes"}
    assert out["b"] == {"hand": True, "monitor": None, "open": None, "post": None, "total": 300, "edited": None}
