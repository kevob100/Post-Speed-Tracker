from __future__ import annotations

from src import develop
from src.positions import non_fantasy

POS = {"lane johnson": {"pos": ["T"], "rank": None}, "josh allen": {"pos": ["G", "QB"], "rank": 3},
       "micah parsons": {"pos": ["LB"], "rank": None}, "colby parkinson": {"pos": ["TE"], "rank": 136},
       "trey mcbride": {"pos": ["TE"], "rank": 22}}


def test_non_fantasy_needs_every_player_known_and_off_fantasy():
    assert non_fantasy(["lane johnson"], POS)
    assert non_fantasy(["lane johnson", "micah parsons"], POS)
    assert not non_fantasy(["josh allen"], POS)              # shared name, one is a QB
    assert not non_fantasy(["lane johnson", "colby parkinson"], POS)
    assert not non_fantasy(["somebody new"], POS)            # unknown: keep counting it
    assert not non_fantasy([], POS)


def _post(pid, acct, key, t):
    return {"id": pid, "account": acct, "created_at": t, "text": key, "is_news": True,
            "player": key, "player_key": key, "team": None,
            "players": [{"name": key, "player_key": key, "team": None}]}


def _lone(pid, key, t):
    return {"story_id": pid, "status": "underdog_only", "player_key": key, "rotowire": None,
            "underdog": {"tweet_id": pid, "created_at": t, "text": key}}


def test_mark_gaps_tags_out_of_beat_misses():
    news = [_post("1", "UD", "lane johnson", "2026-10-04T15:00:00Z"),
            _post("2", "UD", "colby parkinson", "2026-10-04T15:30:00Z"),
            _post("3", "UD", "puka nacua", "2026-10-04T16:00:00Z"),
            _post("4", "RW", "puka nacua", "2026-09-01T16:00:00Z"),
            _post("5", "UD", "trey mcbride", "2026-10-04T17:00:00Z")]
    sts = [_lone("1", "lane johnson", "2026-10-04T15:00:00Z"),
           _lone("2", "colby parkinson", "2026-10-04T15:30:00Z"),
           _lone("3", "puka nacua", "2026-10-04T16:00:00Z"),
           _lone("5", "trey mcbride", "2026-10-04T17:00:00Z")]   # RW never posted, but a top TE
    develop._mark_gaps(sts, news, "RW", POS)
    assert [s["gap_kind"] for s in sts] == ["not_fantasy", "not_covered", "missed", "missed"]


def test_x_analytics_backfill_walks_net_follows_back(tmp_path):
    from src import x_analytics
    (tmp_path / "export.csv").write_text(
        "Date,Impressions,New follows,Unfollows\n"
        "\"Wed, Oct 01, 2026\",1000,50,10\n"
        "2026-09-30,900,30,5\n"
        "2026-09-29,800,20,0\n")
    daily = x_analytics.load_daily(tmp_path)
    assert daily["2026-10-01"] == {"new_follows": 50, "unfollows": 10, "impressions": 1000}
    out = x_analytics.backfill([{"date": "2026-10-01", "followers": 1000},
                                {"date": "2026-10-02", "followers": 1100}], daily)
    assert [(p["date"], p["followers"]) for p in out] == [
        ("2026-09-28", 915), ("2026-09-29", 935), ("2026-09-30", 960),
        ("2026-10-01", 1000), ("2026-10-02", 1100)]
