from __future__ import annotations

from datetime import date

from src import aggregate as agg
from src import sprout
from src.store import load_jsonl, load_state


class Resp:
    def __init__(self, body):
        self.status_code, self._body, self.text = 200, body, ""

    def json(self):
        return self._body


class Sess:
    """Answers every request with one row per day of the requested range."""
    def __init__(self):
        self.ranges = []

    def post(self, url, headers=None, timeout=None, json=None):
        rng = json["filters"][1].split("(")[1].rstrip(")")
        a, b = (date.fromisoformat(x) for x in rng.split("..."))
        self.ranges.append((a, b))
        days = [date.fromordinal(o) for o in range(a.toordinal(), b.toordinal() + 1)]
        return Resp({"data": [{"dimensions": {"reporting_period.by(day)": d.isoformat()},
                               "metrics": {"lifetime_snapshot.followers_count": 1000 + d.toordinal() % 100,
                                           "impressions": 50, "post_link_clicks": 3, "net_follower_growth": 1}}
                              for d in days], "paging": {"current_page": 1, "total_pages": 1}})


def test_sync_fills_once_then_refreshes_recent_days(tmp_path, monkeypatch):
    monkeypatch.setenv("SPROUT_SOCIAL_TOKEN", "t")
    monkeypatch.setenv("SPROUT_CUSTOMER_ID", "1")
    s = Sess()
    sprout.sync(tmp_path, 9, "2025-01-01", refresh_days=7, session=s, today=date(2026, 3, 1))
    # Over a year: chunked into requests of at most 365 days, ending yesterday.
    assert s.ranges[0] == (date(2025, 1, 1), date(2025, 12, 31))
    assert s.ranges[-1][1] == date(2026, 2, 28)
    assert len(load_jsonl(tmp_path / "sprout_daily.jsonl")) == 424
    assert load_state(tmp_path)["sprout_filled_from"] == "2025-01-01"
    s.ranges.clear()
    sprout.sync(tmp_path, 9, "2025-01-01", refresh_days=7, session=s, today=date(2026, 3, 3))
    assert s.ranges == [(date(2026, 2, 24), date(2026, 3, 2))]
    assert len(load_jsonl(tmp_path / "sprout_daily.jsonl")) == 426


def test_sprout_followers_extend_history_and_weekly_totals_skip_follower_level():
    rows = [{"date": f"2026-04-{d:02d}", "followers": 900 + d, "net_follows": 1, "impressions": 10,
             "link_clicks": 2} for d in range(6, 13)]                      # Mon Apr 6 .. Sun Apr 12
    followers = [{"date": "2026-04-10", "account": "rotowire", "followers_count": 950},
                 {"date": "2026-04-10", "account": "underdog", "followers_count": 5000}]
    a = agg._audience([], [], followers, "RotoWireNFL", None, x_daily=sprout.daily_metrics(rows), x_export={},
                      sprout_followers=sprout.follower_counts(rows), account_sources=["Sprout Social"])
    fr = a["followers"]["rotowire"]
    # Sprout counts before the pipeline's first count, then the pipeline's own count.
    assert [p["date"] for p in fr if p.get("source") == "sprout"] == ["2026-04-06", "2026-04-07", "2026-04-08", "2026-04-09"]
    assert fr[[p["date"] for p in fr].index("2026-04-10")]["followers"] == 950
    wk = a["rotowire_account_impressions"][0]
    assert wk["days"] == 7 and wk["impressions"] == 70 and wk["link_clicks"] == 14 and "followers" not in wk
    assert a["account_sources"] == ["Sprout Social"]


def test_export_walk_back_wins_and_sprout_only_fills_earlier_dates():
    sp = [{"date": f"2026-04-0{d}", "followers": 1, "source": "sprout"} for d in range(1, 9)]
    export = {"2026-04-08": {"new_follows": 5, "unfollows": 0}, "2026-04-07": {"new_follows": 5, "unfollows": 0}}
    followers = [{"date": "2026-04-08", "account": "rotowire", "followers_count": 100}]
    fr = agg._audience([], [], followers, "RotoWireNFL", None, x_daily=export, sprout_followers=sp,
                       x_export=export)["followers"]["rotowire"]
    assert [(p["date"], p.get("source")) for p in fr] == [
        ("2026-04-01", "sprout"), ("2026-04-02", "sprout"), ("2026-04-03", "sprout"), ("2026-04-04", "sprout"),
        ("2026-04-05", "sprout"), ("2026-04-06", "x_analytics"), ("2026-04-07", "x_analytics"), ("2026-04-08", None)]
