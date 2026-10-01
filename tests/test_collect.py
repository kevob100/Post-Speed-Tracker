from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import collect as collect_mod
from src.store import load_jsonl, load_state


def _ts(hours_ago: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _tweet(tid: str, hours_ago: float, impressions: int) -> dict:
    return {
        "id": tid,
        "created_at": _ts(hours_ago),
        "text": f"post {tid}",
        "lang": "en",
        "public_metrics": {"impression_count": impressions, "like_count": 0},
    }


class FakeClient:
    def __init__(self, tweets_by_user: dict[str, list[dict]], metrics: dict[str, dict] | None = None):
        self.tweets_by_user = tweets_by_user
        self.metrics = metrics or {}
        self.metrics_calls: list[list[str]] = []

    def user_tweets(
        self, user_id, since_id=None, start_time=None, exclude=None, max_pages=None, page_size=100
    ):
        for t in self.tweets_by_user.get(user_id, []):
            if since_id and int(t["id"]) <= int(since_id):
                continue
            yield t

    def tweets_metrics(self, ids):
        self.metrics_calls.append(list(ids))
        return {i: self.metrics[i] for i in ids if i in self.metrics}


CFG = {
    "sports": {
        "mlb": {
            "label": "MLB",
            "active": True,
            "accounts": {
                "rotowire": {"handle": "RotoWireMLB", "user_id": "111"},
                "underdog": {"handle": "UnderdogMLB", "user_id": "222"},
            },
        },
    },
    "collection": {"backfill_start_date": "2026-05-29", "exclude": ["retweets", "replies"]},
    "impressions": {"freeze_hours": 12},
}


@pytest.fixture(autouse=True)
def _patch_config(monkeypatch):
    monkeypatch.setattr(collect_mod, "load_config", lambda: CFG)


def test_dedupe_and_since_id(tmp_path, monkeypatch):
    client = FakeClient({"111": [_tweet("1005", 1, 100), _tweet("1003", 2, 50)], "222": []})
    collect_mod.collect(client=client, data_dir=tmp_path)

    rows = load_jsonl(tmp_path / "tweets.jsonl")
    assert {r["id"] for r in rows} == {"1005", "1003"}
    assert all(r["account"] == "RotoWireMLB" for r in rows)

    state = load_state(tmp_path)
    assert state["accounts"]["rotowire"]["since_id"] == "1005"

    # Second run: only a newer post should be added; no duplicates.
    client2 = FakeClient({"111": [_tweet("1009", 0.5, 200), _tweet("1005", 1, 100)], "222": []})
    collect_mod.collect(client=client2, data_dir=tmp_path)
    rows = load_jsonl(tmp_path / "tweets.jsonl")
    assert {r["id"] for r in rows} == {"1003", "1005", "1009"}
    assert load_state(tmp_path)["accounts"]["rotowire"]["since_id"] == "1009"


def test_freeze_after_cutoff(tmp_path):
    # 13h-old post should freeze; 1h-old post should stay open.
    client = FakeClient({"111": [_tweet("1010", 13, 500), _tweet("1011", 1, 10)], "222": []})
    collect_mod.collect(client=client, data_dir=tmp_path)

    rows = {r["id"]: r for r in load_jsonl(tmp_path / "tweets.jsonl")}
    assert rows["1010"]["metrics_frozen"] is True
    assert rows["1010"]["metrics_frozen_at"] is not None
    assert rows["1011"]["metrics_frozen"] is False


def test_non_frozen_metrics_refresh(tmp_path):
    # First run stores a fresh (1h-old) post.
    client = FakeClient({"111": [_tweet("1020", 1, 10)], "222": []})
    collect_mod.collect(client=client, data_dir=tmp_path)

    # Second run: no new posts, but the stored post's metrics moved. It is not
    # frozen and was not re-fetched via timeline, so it must be refreshed.
    client2 = FakeClient({"111": [], "222": []}, metrics={"1020": {"impression_count": 999}})
    collect_mod.collect(client=client2, data_dir=tmp_path)

    rows = {r["id"]: r for r in load_jsonl(tmp_path / "tweets.jsonl")}
    assert rows["1020"]["public_metrics"]["impression_count"] == 999
    assert client2.metrics_calls == [["1020"]]


def test_frozen_metrics_not_refreshed(tmp_path):
    client = FakeClient({"111": [_tweet("1030", 13, 500)], "222": []})
    collect_mod.collect(client=client, data_dir=tmp_path)  # freezes 1030

    client2 = FakeClient({"111": [], "222": []}, metrics={"1030": {"impression_count": 999}})
    collect_mod.collect(client=client2, data_dir=tmp_path)
    rows = {r["id"]: r for r in load_jsonl(tmp_path / "tweets.jsonl")}
    assert rows["1030"]["public_metrics"]["impression_count"] == 500  # unchanged
    assert client2.metrics_calls == []  # nothing to refresh


def test_backfill_references_once_and_only_since_date():
    from src.collect import _backfill_references

    class C:
        asked: list = []

        def tweets_lookup(self, ids, fields):
            C.asked.append(list(ids))
            return {"new": {"id": "new", "referenced_tweets": [{"type": "quoted", "id": "q"}]}}

    by_id = {
        "old": {"id": "old", "created_at": "2026-08-01T00:00:00.000Z"},
        "new": {"id": "new", "created_at": "2026-09-09T00:00:00.000Z"},
        "gone": {"id": "gone", "created_at": "2026-09-09T00:00:00.000Z"},
    }
    _backfill_references(C(), by_id, "2026-09-01T00:00:00Z")
    _backfill_references(C(), by_id, "2026-09-01T00:00:00Z")
    assert C.asked == [["new", "gone"]]
    assert by_id["new"]["referenced_tweets"][0]["type"] == "quoted"
    assert by_id["gone"]["referenced_tweets"] == [] and "referenced_tweets" not in by_id["old"]



def test_follower_snapshot_one_row_per_account_per_day(tmp_path):
    from src.collect import _snapshot_followers
    from src.store import load_jsonl

    class C:
        n = 1000

        def users_metrics(self, ids):
            C.n += 5
            return {i: {"followers_count": C.n, "following_count": 1, "tweet_count": 2,
                        "listed_count": 3} for i in ids}

    accounts = {"rotowire": {"handle": "RotoWireNFL", "user_id": "1"},
                "underdog": {"handle": "UnderdogNFL", "user_id": "2"}}
    _snapshot_followers(C(), accounts, tmp_path)
    _snapshot_followers(C(), accounts, tmp_path)          # same day: replaces, no duplicates
    rows = load_jsonl(tmp_path / "followers.jsonl")
    assert sorted(r["account"] for r in rows) == ["rotowire", "underdog"]
    assert all(r["followers_count"] == 1010 for r in rows)

    class Boom:
        def users_metrics(self, ids):
            raise RuntimeError("rate limited")
    _snapshot_followers(Boom(), accounts, tmp_path)       # must not raise
    assert len(load_jsonl(tmp_path / "followers.jsonl")) == 2



def test_archive_backfill_fills_before_oldest_post_once():
    from src.collect import _archive_backfill

    class C:
        calls: list = []

        def search_all(self, query, start, end):
            C.calls.append((query, start, end))
            yield {"id": "old1", "created_at": "2026-04-10T12:00:00.000Z", "text": "a",
                   "public_metrics": {"impression_count": 5}}
            yield {"id": "keep", "created_at": "2026-05-01T12:00:00.000Z", "text": "dup"}

    by_id = {"keep": {"id": "keep", "account": "RotoWireNFL", "created_at": "2026-07-06T00:00:00.000Z"}}
    state = {"accounts": {}}
    accounts = {"rotowire": {"handle": "RotoWireNFL", "user_id": "1"}}
    seen: set = set()
    _archive_backfill(C(), accounts, state, by_id, seen, "2026-04-01T00:00:00Z")
    assert C.calls == [("from:RotoWireNFL -is:retweet -is:reply", "2026-04-01T00:00:00Z",
                        "2026-07-06T00:00:00.000Z")]
    assert by_id["old1"]["account"] == "RotoWireNFL" and by_id["keep"].get("text") != "dup"
    assert state["accounts"]["rotowire"]["archive_start"] == "2026-04-01T00:00:00Z"
    _archive_backfill(C(), accounts, state, by_id, seen, "2026-04-01T00:00:00Z")
    assert len(C.calls) == 1                                  # done once
    _archive_backfill(C(), accounts, state, by_id, seen, "2026-03-01T00:00:00Z")
    assert len(C.calls) == 2                                  # earlier start re-runs


def test_archive_backfill_failure_retries_next_run():
    from src.collect import _archive_backfill

    class Boom:
        def search_all(self, *a):
            raise RuntimeError("403 not enrolled")
            yield

    state = {"accounts": {}}
    by_id = {"x": {"id": "x", "account": "UnderdogNFL", "created_at": "2026-07-07T00:00:00.000Z"}}
    _archive_backfill(Boom(), {"underdog": {"handle": "UnderdogNFL", "user_id": "2"}},
                      state, by_id, set(), "2026-04-01T00:00:00Z")
    assert "archive_start" not in state["accounts"]["underdog"]
