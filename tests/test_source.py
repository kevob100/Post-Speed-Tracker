from __future__ import annotations

from src import source
from src.store import load_jsonl, write_jsonl

OWN = ["RotoWireNFL", "UnderdogNFL"]


def _story(sid, rw_id, rw_at, rw_text, ud_id, ud_at, ud_text, player="Mack Hollins"):
    return {"story_id": sid, "status": "matched", "player": player,
            "rotowire": {"tweet_id": rw_id, "created_at": rw_at, "text": rw_text},
            "underdog": {"tweet_id": ud_id, "created_at": ud_at, "text": ud_text}}


class FakeClient:
    def __init__(self, lookup=None, search=None, users=None):
        self.lookup, self.search, self.users = lookup or {}, search or [], users or {}
        self.queries = []

    def tweets_lookup(self, ids, fields=""):
        return {i: self.lookup[i] for i in ids if i in self.lookup}

    def users_by_ids(self, ids):
        return {i: self.users[i] for i in ids if i in self.users}

    def search_all(self, query, start_time, end_time, page_size=10):
        self.queries.append((query, start_time, end_time))
        return [t for t in self.search if start_time <= t["created_at"][:19] + "Z" <= end_time]


def test_cited_handles_skips_own_accounts():
    assert source.cited_handles("via @DougKyed and @UnderdogNFL @rotowire", {"underdognfl"}) == ["DougKyed"]


def test_last_name_skips_suffix():
    assert source.last_name("Marvin Harrison Jr.") == "Harrison"
    assert source.last_name("D'Angelo Ponds") == "Ponds"


def test_search_takes_latest_tweet_before_first_post(tmp_path):
    write_jsonl(tmp_path / "tweets.jsonl", [{"id": "u1", "referenced_tweets": []}])
    write_jsonl(tmp_path / "stories.jsonl", [_story(
        "st1", "r1", "2026-10-05T15:42:00.000Z", "Hollins: Uncertain (via @DougKyed)",
        "u1", "2026-10-05T15:36:00.000Z", "Vrabel on Hollins, via @DougKyed.")])
    client = FakeClient(search=[
        {"id": "s0", "created_at": "2026-10-05T12:00:00.000Z", "author_id": "9"},
        {"id": "s1", "created_at": "2026-10-05T15:33:00.000Z", "author_id": "9"},
        {"id": "s2", "created_at": "2026-10-05T15:40:00.000Z", "author_id": "9"},  # after UD
    ], users={"9": "DougKyed"})
    res = source.find_sources(client, tmp_path, OWN)
    assert res["found"] == 1 and res["searches"] == 1
    assert '(from:DougKyed) "Hollins"' in client.queries[0][0]
    row = load_jsonl(tmp_path / "sources.jsonl")[0]
    assert (row["source_id"], row["source_handle"], row["method"]) == ("s1", "DougKyed", "search")

    stories = load_jsonl(tmp_path / "stories.jsonl")
    assert source.attach(stories, tmp_path) == 1
    assert stories[0]["underdog_secs_from_source"] == 180
    assert stories[0]["rotowire_secs_from_source"] == 540

    # Cached: a second run makes no searches.
    again = FakeClient()
    assert source.find_sources(again, tmp_path, OWN)["new"] == 0 and not again.queries


def test_quoted_source_needs_no_search(tmp_path):
    write_jsonl(tmp_path / "tweets.jsonl", [{"id": "u1", "referenced_tweets": [{"type": "quoted", "id": "q1"}]}])
    write_jsonl(tmp_path / "stories.jsonl", [_story(
        "st1", "r1", "2026-10-05T15:42:00.000Z", "Hollins: Uncertain",
        "u1", "2026-10-05T15:36:00.000Z", "Hollins update")])
    client = FakeClient(lookup={"q1": {"id": "q1", "created_at": "2026-10-05T15:35:00.000Z", "author_id": "9"}},
                        users={"9": "DougKyed"})
    assert source.find_sources(client, tmp_path, OWN)["found"] == 1
    assert not client.queries
    assert load_jsonl(tmp_path / "sources.jsonl")[0]["method"] == "quoted"


def test_no_citation_cached_as_none_and_cap_defers(tmp_path):
    write_jsonl(tmp_path / "tweets.jsonl", [])
    write_jsonl(tmp_path / "stories.jsonl", [
        _story("st1", "r1", "2026-10-05T15:42:00.000Z", "Hollins: Out", "u1", "2026-10-05T15:36:00.000Z", "Hollins out"),
        _story("st2", "r2", "2026-10-05T16:42:00.000Z", "per @A", "u2", "2026-10-05T16:36:00.000Z", "via @A"),
    ])
    res = source.find_sources(FakeClient(), tmp_path, OWN, {"max_searches_per_run": 0})
    assert res == {"stories": 2, "new": 1, "found": 0, "searches": 0, "pending": 1}
    assert [r["method"] for r in load_jsonl(tmp_path / "sources.jsonl")] == ["none"]
