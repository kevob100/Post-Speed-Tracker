from __future__ import annotations

import json

from src import credits as cr
from src.store import write_jsonl

NAMES = {"Schefter": "AdamSchefter", "Rap": "RapSheet"}


def test_classify_styles():
    assert cr.classify({"text": "X (knee) is out. Via @JayGlazer"}, NAMES) == {"kind": "handle", "handle": "JayGlazer"}
    assert cr.classify({"text": "Schefter: X traded."}, NAMES)["handle"] == "AdamSchefter"
    assert cr.classify({"text": "X out.", "referenced_tweets": [{"type": "quoted", "id": "1"}]}, NAMES)["kind"] == "quote"
    assert cr.classify({"text": "McVay: X will play."}, NAMES)["kind"] == "presser"
    assert cr.classify({"text": "Status alert: X questionable."}, NAMES)["kind"] == "none"
    assert cr.classify({"text": "X questionable."}, NAMES)["kind"] == "none"


def test_build_puts_unlisted_accounts_first(tmp_path):
    data, docs = tmp_path / "data", tmp_path / "docs"
    data.mkdir(); docs.mkdir()
    ud = lambda i, text: {"id": f"u{i}", "account": "UnderdogNFL", "is_news": True,  # noqa: E731
                          "created_at": "2026-10-10T18:00:00.000Z", "text": text}
    write_jsonl(data / "tweets.jsonl", [ud(1, "Schefter: A out."), ud(2, "B out. Via @JayGlazer"),
                                        ud(3, "Schefter: C out."), ud(4, "D out.")])
    (data / cr.LIST_FILE).write_text(json.dumps({"list_id": "1", "fetched_at": "x", "handles": ["AdamSchefter"]}))
    stories = [{"status": "matched", "underdog": {"tweet_id": "u2", "created_at": "2026-10-10T18:00:00Z"},
                "rotowire": {"tweet_id": "r2", "created_at": "2026-10-10T18:02:00Z"}}]
    out = cr.build(data, docs, stories, NAMES, "UnderdogNFL", now=cr.parse_dt("2026-10-14T00:00:00Z"))
    assert out["posts"] == 4 and out["missing"] == 1
    first, second = out["accounts"]
    assert first["handle"] == "JayGlazer" and first["on_list"] is False
    assert first["shared"] == 1 and first["median_gap"] == 120 and first["we_first_pct"] == 0.0
    assert second["handle"] == "AdamSchefter" and second["on_list"] is True and second["credited"] == 2


def test_list_history_and_dismissed(tmp_path):
    data, docs = tmp_path / "data", tmp_path / "docs"
    data.mkdir(); docs.mkdir()

    class Client:
        def __init__(self, handles): self.h = handles
        def _get(self, path, params=None): return {"data": [{"username": h} for h in self.h], "meta": {}}

    cr.fetch_list(Client(["AdamSchefter"]), "1", data)                  # first read: baseline only
    assert not (data / cr.HISTORY_FILE).exists()
    cr.fetch_list(Client(["AdamSchefter", "DanGraziano"]), "1", data)   # next night: one added
    hist = [json.loads(l) for l in (data / cr.HISTORY_FILE).read_text().splitlines()]
    assert [(h["handle"], h["action"]) for h in hist] == [("DanGraziano", "added")]

    (data / cr.DISMISSED_FILE).write_text(json.dumps({"handles": ["JayGlazer"]}))
    write_jsonl(data / "tweets.jsonl", [
        {"id": "u1", "account": "UnderdogNFL", "is_news": True, "created_at": "2026-10-10T18:00:00.000Z", "text": "A out. Via @JayGlazer"},
        {"id": "u2", "account": "UnderdogNFL", "is_news": True, "created_at": "2026-10-10T18:00:00.000Z", "text": "B out. Via @MikeSilver"}])
    out = cr.build(data, docs, [], NAMES, "UnderdogNFL", now=cr.parse_dt("2026-10-14T00:00:00Z"))
    assert out["missing"] == 1 and out["dismissed"] == 1
    assert [a["handle"] for a in out["added"]] == ["DanGraziano"]
