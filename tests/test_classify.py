from __future__ import annotations

from src import classify
from src.classify import parse_classification
from src.store import load_jsonl, write_jsonl


# --------------------------- parse_classification -------------------------- #

def test_parse_plain_json_news():
    c = parse_classification(
        '{"is_news": true, "player": "Christian Yelich", "team": "Brewers", '
        '"event_class": "injury", "excluded_reason": null}'
    )
    assert c["is_news"] is True
    assert c["player"] == "Christian Yelich"
    assert c["event_class"] == "injury"
    assert c["excluded_reason"] is None


def test_parse_remaps_legacy_event_classes():
    # Cached MLB verdicts use the old baseball-specific classes; they must remap to the
    # generic cross-sport taxonomy on read.
    assert parse_classification(
        '{"is_news": true, "player": "X", "event_class": "injury_il"}'
    )["event_class"] == "injury"
    assert parse_classification(
        '{"is_news": true, "player": "X", "event_class": "transaction_trade"}'
    )["event_class"] == "roster_move"
    assert parse_classification(
        '{"is_news": true, "player": "X", "event_class": "return"}'
    )["event_class"] == "status_change"


def test_parse_strips_code_fences():
    raw = ("```json\n{\"is_news\": false, \"player\": null, \"team\": null, "
           "\"event_class\": \"other\", \"excluded_reason\": \"promo\"}\n```")
    c = parse_classification(raw)
    assert c["is_news"] is False and c["excluded_reason"] == "promo"


def test_parse_recovers_embedded_object():
    c = parse_classification(
        'Sure: {"is_news": true, "player": "Eury Perez", "team": "Marlins", '
        '"event_class": "status_change", "excluded_reason": null} done'
    )
    assert c["is_news"] is True and c["event_class"] == "status_change"


def test_parse_garbage_is_safe():
    c = parse_classification("model rambled")
    assert c["is_news"] is False and c["parse_error"] is True


def test_parse_unknown_event_class_falls_back_to_other_and_not_news():
    c = parse_classification(
        '{"is_news": true, "player": "X", "event_class": "made_up", "excluded_reason": null}'
    )
    # An out-of-vocab class collapses to "other", which cannot be news.
    assert c["event_class"] == "other" and c["is_news"] is False


def test_classifier_builds_valid_prompt_per_sport():
    # The prompt contains literal JSON braces, so interpolation must not use str.format.
    # A real Classifier is constructed here (client injected) to exercise __init__.
    for sport, label in [("mlb", "MLB"), ("golf", "Golf"), ("nfl", "NFL")]:
        c = classify.Classifier(client=object(), sport=sport, sport_label=label)
        assert "{sport}" not in c.system_prompt      # placeholder filled
        assert label in c.system_prompt
        assert '{"is_news"' in c.system_prompt        # JSON schema intact
    golf = classify.Classifier(client=object(), sport="golf", sport_label="Golf")
    assert golf.event_classes == ("withdrawal", "field_change", "other")


def test_parse_golf_taxonomy():
    from src.classify import GOLF_EVENT_CLASSES

    wd = parse_classification(
        '{"is_news": true, "player": "Scottie Scheffler", "event_class": "withdrawal"}',
        GOLF_EVENT_CLASSES,
    )
    assert wd["is_news"] is True and wd["event_class"] == "withdrawal"

    fc = parse_classification(
        '{"is_news": true, "player": "Nick Dunlap", "event_class": "field_change"}',
        GOLF_EVENT_CLASSES,
    )
    assert fc["is_news"] is True and fc["event_class"] == "field_change"

    # A roster-sport class is out of vocab for golf -> collapses to "other" (not news).
    ros = parse_classification(
        '{"is_news": true, "player": "X", "event_class": "roster_move"}',
        GOLF_EVENT_CLASSES,
    )
    assert ros["event_class"] == "other" and ros["is_news"] is False


def test_parse_news_requires_a_player():
    c = parse_classification(
        '{"is_news": true, "player": null, "event_class": "injury", "excluded_reason": null}'
    )
    assert c["is_news"] is False and c["excluded_reason"] == "no_player"


# ------------------------------ classify_file ------------------------------ #

class FakeClassifier:
    """Returns scripted verdicts keyed by tweet id; counts calls (to prove caching)."""

    def __init__(self, by_id):
        self.by_id = by_id
        self.calls = 0
        self._next = None

    def classify(self, text):
        self.calls += 1
        # The classifier sees only text, so we map text -> verdict here.
        return dict(self._lookup(text))

    def _lookup(self, text):
        return self.by_id[text]


def _post(tid, text):
    return {"id": tid, "account": "RotoWireMLB", "created_at": f"2026-05-29T18:0{tid}:00.000Z",
            "text": text, "public_metrics": {"impression_count": 1}}


def test_classify_file_enriches_and_caches(tmp_path):
    write_jsonl(tmp_path / "tweets.jsonl", [
        _post("1", "Yelich placed on the IL"),
        _post("2", "Use code WIN for a free pick"),
    ])
    fake = FakeClassifier({
        "Yelich placed on the IL": {"is_news": True, "player": "Christian Yelich",
                                    "team": "Brewers", "event_class": "injury_il",
                                    "excluded_reason": None},
        "Use code WIN for a free pick": {"is_news": False, "player": None, "team": None,
                                         "event_class": "other", "excluded_reason": "promo"},
    })

    summary = classify.classify_file(tmp_path, classifier=fake)
    assert summary == {"total": 2, "news": 1, "excluded": {"promo": 1}}
    assert fake.calls == 2

    rows = {r["id"]: r for r in load_jsonl(tmp_path / "tweets.jsonl")}
    assert rows["1"]["is_news"] is True
    assert rows["1"]["player"] == "Christian Yelich"
    # The classifier returned a legacy class; it is remapped to the generic taxonomy.
    assert rows["1"]["event_class"] == "injury"
    assert rows["1"]["player_key"] == "christian yelich"
    assert rows["1"]["players"][0]["player_key"] == "christian yelich"
    assert rows["2"]["is_news"] is False and rows["2"]["excluded_reason"] == "promo"

    # Re-run: verdicts served from data/classifications.jsonl, no new LLM calls.
    classify.classify_file(tmp_path, classifier=fake)
    assert fake.calls == 2
    assert len(load_jsonl(tmp_path / "classifications.jsonl")) == 2


def test_classify_file_no_key_uses_safe_default(tmp_path):
    # llm=False must never call the classifier; uncached posts become non-news.
    write_jsonl(tmp_path / "tweets.jsonl", [_post("1", "anything")])
    summary = classify.classify_file(tmp_path, llm=False)
    assert summary["news"] == 0
    row = load_jsonl(tmp_path / "tweets.jsonl")[0]
    assert row["is_news"] is False and row["player"] is None
    # Nothing cached, since no verdict was produced.
    assert not (tmp_path / "classifications.jsonl").exists()


# ------------------------------ multi-player ------------------------------- #

def test_parse_multi_player_list():
    c = parse_classification(
        '{"is_news": true, "players": [{"name": "Ja\'Marr Chase", "team": "Bengals"}, '
        '{"name": "Tee Higgins", "team": "Bengals"}], "event_class": "status_change"}'
    )
    assert c["is_news"] is True and c["player"] == "Ja'Marr Chase"
    assert [p["name"] for p in c["players"]] == ["Ja'Marr Chase", "Tee Higgins"]


def test_apply_writes_every_player():
    rec = classify._apply({"id": "1"}, {"is_news": True, "player": "A B", "event_class": "injury",
                                        "players": [{"name": "A B"}, {"name": "C D"}]})
    assert [p["player_key"] for p in rec["players"]] == ["a b", "c d"]
    assert rec["player_key"] == "a b"


def test_multi_player_reclassifies_cached_no_player_once(tmp_path):
    write_jsonl(tmp_path / "tweets.jsonl", [
        {"id": "1", "created_at": "2026-09-09T00:00:00.000Z", "text": "Chase, Higgins fully practice"},
        {"id": "2", "created_at": "2026-09-09T00:01:00.000Z", "text": "A B ruled out"},
    ])
    write_jsonl(tmp_path / "classifications.jsonl", [
        {"id": "1", "is_news": False, "player": None, "event_class": "status_change",
         "excluded_reason": "no_player"},
        {"id": "2", "is_news": True, "player": "A B", "event_class": "injury", "excluded_reason": None},
    ])

    class Stub:
        calls = 0

        def classify(self, text):
            Stub.calls += 1
            return {"is_news": True, "player": "Ja'Marr Chase", "team": None,
                    "event_class": "status_change", "excluded_reason": None,
                    "players": [{"name": "Ja'Marr Chase", "team": None},
                                {"name": "Tee Higgins", "team": None}]}

    classify.classify_file(data_dir=tmp_path, classifier=Stub(), sport="nfl", multi_player=True)
    classify.classify_file(data_dir=tmp_path, classifier=Stub(), sport="nfl", multi_player=True)
    # The no_player post (may name several players) and the old news post (may now be
    # hype) are each re-run exactly once.
    assert Stub.calls == 2
    recs = {r["id"]: r for r in load_jsonl(tmp_path / "tweets.jsonl")}
    assert recs["1"]["is_news"] and len(recs["1"]["players"]) == 2


def test_parse_hype():
    c = parse_classification(
        '{"is_news": false, "players": [{"name": "Landon Dickerson", "team": "Eagles"}], '
        '"event_class": "other", "excluded_reason": "hype", "hype_kind": "soundbite"}')
    assert c["is_news"] is False and c["excluded_reason"] == "hype" and c["hype_kind"] == "soundbite"
    c = parse_classification('{"is_news": false, "player": "X", "excluded_reason": "hype", "hype_kind": "vibes"}')
    assert c["hype_kind"] == "other"
    assert "hype_kind" not in parse_classification('{"is_news": false, "player": null, "excluded_reason": "promo"}')


def test_hype_prompt_only_in_multi_player_mode():
    assert "hype" in classify.prompt_for("nfl", multi_player=True)
    assert "hype" not in classify.prompt_for("mlb")


def test_v3_reruns_hype_but_not_v2_news():
    assert classify._needs_rerun({"excluded_reason": "hype", "multi_player_version": 2})
    assert not classify._needs_rerun({"excluded_reason": "hype", "multi_player_version": 3})
    assert not classify._needs_rerun({"is_news": True, "multi_player_version": 2})
    assert classify._needs_rerun({"is_news": True, "multi_player_version": 1})
