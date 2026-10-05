from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import aggregate as agg
from src import develop
from src.anthropic_client import parse_groups
from src.store import load_jsonl, normalize_name, write_jsonl

RW, UD = "RotoWireNFL", "UnderdogNFL"
ACCOUNTS = {"rotowire": {"handle": RW, "user_id": "1"}, "underdog": {"handle": UD, "user_id": "2"}}
CFG = {"sports": {"nfl": {"label": "NFL", "accounts": ACCOUNTS}},
       "matching": {"time_window_minutes": 90}}


@pytest.fixture(autouse=True)
def _patch_config(monkeypatch):
    monkeypatch.setattr(develop, "load_config", lambda: CFG)


def _ts(minute: float) -> str:
    dt = datetime(2026, 9, 10, 0, 30, tzinfo=timezone.utc) + timedelta(minutes=minute)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _post(tid, account, minute, players=("Sam Darnold",), event="injury", text="", quotes=None):
    ps = [{"name": p, "team": None, "player_key": normalize_name(p)} for p in players]
    return {"id": tid, "account": account, "created_at": _ts(minute), "is_news": True,
            "event_class": event, "text": text or tid, "public_metrics": {"impression_count": 10},
            "player": players[0], "team": None, "player_key": normalize_name(players[0]),
            "players": ps,
            "referenced_tweets": [{"type": "quoted", "id": quotes}] if quotes else []}


class FixedGrouper:
    """Returns pre-set developments (lists of post ids) and records each call."""

    def __init__(self, *groups):
        self.groups = [list(g) for g in groups]
        self.calls: list[list[dict]] = []

    def group(self, player, posts):
        self.calls.append(posts)
        return parse_groups(
            '{"developments": [' + ",".join(
                '{"label": "d%d", "post_ids": [%s]}' % (i, ",".join(f'"{p}"' for p in g))
                for i, g in enumerate(self.groups)) + "]}",
            [p["id"] for p in posts])


def _run(tmp_path, posts, grouper, cache=True):
    write_jsonl(tmp_path / "tweets.jsonl", posts)
    stories = develop.resolve_developments(data_dir=tmp_path, sport="nfl", accounts=ACCOUNTS,
                                           grouper=grouper, cache=cache)
    return {s["story_id"]: s for s in stories}


def test_darnold_first_report_wins_not_closest_pair(tmp_path):
    # Real Week 1 sequence. The closest-pair matcher scored "RW first by 81s" by pairing
    # RW's 8:41 post with UD's 8:42 X-rays post; UD actually broke it at 8:35.
    posts = [
        _post("ud_tent", UD, 5),        # 8:35 headed to medical tent
        _post("ud_lockr", UD, 8),       # 8:38 headed to locker room
        _post("rw_left", RW, 11),       # 8:41 left the game
        _post("ud_xray", UD, 12),       # 8:42 receiving X-rays
        _post("ud_q", UD, 13),          # 8:43 questionable to return
        _post("rw_q", RW, 15),          # 8:45 questionable to return
        _post("ud_out", UD, 63),        # 9:33 won't return
        _post("rw_out", RW, 63),        # 9:33 ruled out
    ]
    g = FixedGrouper(["ud_tent", "ud_lockr", "rw_left", "ud_xray"], ["ud_q", "rw_q"], ["ud_out", "rw_out"])
    s = _run(tmp_path, posts, g)

    matched = {k: v for k, v in s.items() if v["status"] == "matched"}
    assert set(matched) == {"st_rw_left_ud_tent", "st_rw_q_ud_q", "st_rw_out_ud_out"}
    assert matched["st_rw_left_ud_tent"]["time_delta_seconds"] == -360   # UD by 6 min
    assert matched["st_rw_q_ud_q"]["time_delta_seconds"] == -120          # UD by 2 min
    assert matched["st_rw_out_ud_out"]["time_delta_seconds"] == 0         # tie

    # Later posts on the first development are follow-ups pointing at it, not gaps.
    for fid in ("ud_lockr", "ud_xray"):
        f = s[f"st_{fid}"]
        assert f["same_event_duplicate"] is True
        assert f["duplicate_of"] == "st_rw_left_ud_tent"
    assert len(g.calls) == 1                       # one model call for the whole session


def test_quote_tweet_update_is_passed_to_the_model(tmp_path):
    posts = [_post("ud1", UD, 0), _post("rw1", RW, 2), _post("ud2", UD, 30, quotes="ud1"),
             _post("rw2", RW, 31)]
    g = FixedGrouper(["ud1", "rw1"], ["ud2", "rw2"])
    s = _run(tmp_path, posts, g)
    sent = {p["id"]: p for p in g.calls[0]}
    assert sent["ud2"]["quotes"] == "ud1" and sent["rw2"]["quotes"] is None
    assert sent["ud1"]["side"] == "UD" and sent["rw1"]["side"] == "RW"
    assert {k for k, v in s.items() if v["status"] == "matched"} == {"st_rw1_ud1", "st_rw2_ud2"}


def test_multi_player_post_is_scored_for_each_player(tmp_path):
    # UD folds two players into one post; RW posts one tweet each. Both should match.
    posts = [
        _post("ud_both", UD, 0, players=("Ja'Marr Chase", "Tee Higgins"), event="status_change"),
        _post("rw_chase", RW, 3, players=("Ja'Marr Chase",), event="status_change"),
        _post("rw_higgins", RW, 4, players=("Tee Higgins",), event="status_change"),
    ]

    class OneDev:
        def group(self, player, posts):
            return [{"label": player, "post_ids": [p["id"] for p in posts]}]

    s = _run(tmp_path, posts, OneDev())
    matched = sorted(v["story_id"] for v in s.values() if v["status"] == "matched")
    assert matched == ["st_rw_chase_ud_both_jamarr-chase", "st_rw_higgins_ud_both_tee-higgins"]


def test_single_feed_session_needs_no_model_and_keeps_every_post(tmp_path):
    # Previously unmatched posts were collapsed per (account, player, event) across the
    # WHOLE season, so later posts vanished. Now a later session keeps its own gap.
    posts = [_post("rw_mon", RW, 0), _post("rw_mon2", RW, 20), _post("rw_wed", RW, 60 * 48)]

    class Boom:
        def group(self, *a):
            raise AssertionError("model must not be called for a one-account session")

    s = _run(tmp_path, posts, Boom())
    assert s["st_rw_mon"]["status"] == "rotowire_only" and not s["st_rw_mon"]["same_event_duplicate"]
    assert s["st_rw_mon2"]["duplicate_of"] == "st_rw_mon"
    assert s["st_rw_wed"]["status"] == "rotowire_only" and not s["st_rw_wed"]["same_event_duplicate"]


def test_development_outside_window_splits_into_two_gaps(tmp_path):
    # Chained session (each gap <= 90m) but first RW and first UD are 150m apart.
    posts = [_post("rw1", RW, 0), _post("rw2", RW, 80), _post("ud1", UD, 150)]
    s = _run(tmp_path, posts, FixedGrouper(["rw1", "rw2", "ud1"]))
    assert s["st_rw1"]["status"] == "rotowire_only" and s["st_ud1"]["status"] == "underdog_only"
    assert s["st_rw2"]["duplicate_of"] == "st_rw1"
    assert not any(v["status"] == "matched" for v in s.values())


def test_grouping_is_cached_until_a_new_post_joins(tmp_path):
    posts = [_post("ud1", UD, 0), _post("rw1", RW, 2)]
    g = FixedGrouper(["ud1", "rw1"])
    _run(tmp_path, posts, g)
    _run(tmp_path, posts, g)
    assert len(g.calls) == 1
    assert len(load_jsonl(tmp_path / "developments.jsonl")) == 1

    _run(tmp_path, posts + [_post("ud2", UD, 20)], g)      # session changed -> regroup
    assert len(g.calls) == 2


def test_parse_groups_repairs_bad_model_output():
    ids = ["a", "b", "c", "d"]
    groups = parse_groups('```json\n{"developments": [{"label": "x", "post_ids": ["a", "b", "zz"]},'
                          '{"label": "y", "post_ids": ["b", "c"]}]}\n```', ids)
    assert [g["post_ids"] for g in groups] == [["a", "b"], ["c"], ["d"]]
    assert [g["post_ids"] for g in parse_groups("not json", ids)] == [["a"], ["b"], ["c"], ["d"]]


# ------------------------------- NFL weeks -------------------------------- #

WEEKS = {"start": "2026-09-08", "boundary": "06:00", "timezone": "America/New_York"}


def _utc(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_season_week_boundaries():
    of = agg.season_week_of(WEEKS)
    assert of(_utc("2026-09-08T09:59:00"))[1] == "Preseason"      # Tue 5:59am ET
    assert of(_utc("2026-09-08T10:00:00"))[:4] == (1, "Week 1", "2026-09-08", "2026-09-14")
    # Monday-night news at 1am ET Tuesday still belongs to Week 1 ...
    assert of(_utc("2026-09-15T05:00:00"))[1] == "Week 1"
    # ... and Week 2 starts at 6am ET Tuesday.
    assert of(_utc("2026-09-15T10:00:00"))[:4] == (2, "Week 2", "2026-09-15", "2026-09-21")


def test_season_weeks_rollup(tmp_path):
    def m(sid, ts, delta):
        side = {"tweet_id": sid, "created_at": ts, "impression_count": 1}
        return {"story_id": sid, "status": "matched", "rotowire": side, "underdog": side,
                "time_delta_seconds": delta, "rotowire_first": delta > 0}

    stories = [m("a", "2026-09-01T12:00:00.000Z", 10),
               m("b", "2026-09-09T12:00:00.000Z", 30), m("c", "2026-09-10T12:00:00.000Z", -60),
               m("d", "2026-09-16T12:00:00.000Z", 5)]
    write_jsonl(tmp_path / "stories.jsonl", stories)
    out = agg.build_aggregates(data_dir=tmp_path, docs_data_dir=tmp_path / "docs", season_weeks=WEEKS)
    rows = {r["period"]: r for r in out["season_weeks"]}
    assert list(rows) == ["Preseason", "Week 1", "Week 2"]
    assert rows["Week 1"]["matched"] == 2 and rows["Week 1"]["rotowire_first"] == 1
    assert rows["Week 1"]["start"] == "2026-09-08" and rows["Week 1"]["end"] == "2026-09-14"
    assert "season_weeks" not in agg.build_aggregates(data_dir=tmp_path, docs_data_dir=tmp_path / "d2")


def test_news_type_rollup(tmp_path):
    def m(sid, ts, delta, rw_text, ud_text="x"):
        return {"story_id": sid, "status": "matched",
                "rotowire": {"tweet_id": sid, "created_at": ts, "text": rw_text},
                "underdog": {"tweet_id": sid, "created_at": ts, "text": ud_text},
                "time_delta_seconds": delta, "rotowire_first": delta > 0}

    stories = [
        m("pre", "2026-09-01T12:00:00.000Z", 10, "He was placed on injured reserve."),
        m("a", "2026-09-09T12:00:00.000Z", 30, "Limited in practice Wednesday."),
        m("b", "2026-09-10T12:00:00.000Z", -90, "Did not practice Thursday, per the injury report."),
        m("c", "2026-09-16T12:00:00.000Z", -60, "Ruled out for the remainder of Sunday's game, won't return."),
        {"story_id": "gap", "status": "rotowire_only", "rotowire": {"tweet_id": "g",
         "created_at": "2026-09-16T12:00:00.000Z", "text": "Signed a contract."}, "underdog": None},
    ]
    write_jsonl(tmp_path / "stories.jsonl", stories)
    nt = agg.build_aggregates(data_dir=tmp_path, docs_data_dir=tmp_path / "docs",
                              season_weeks=WEEKS)["news_types"]
    assert nt["weeks"] == ["Week 1", "Week 2"]                 # preseason left out
    types = {t["key"]: t for t in nt["types"]}
    assert set(types) == {"injury_report", "in_game"}          # gaps and preseason ignored
    ir = types["injury_report"]
    assert ir["label"] == "Practice / injury report"
    assert ir["season"] == {"matched": 2, "rotowire_first_rate": 0.5, "median_lead_seconds": -30.0}
    assert list(ir["weeks"]) == ["Week 1"]
    assert types["in_game"]["weeks"]["Week 2"]["matched"] == 1
    assert nt["types"][0]["key"] == "injury_report"            # ordered by volume


def test_gaps_marked_missed_or_late(tmp_path):
    posts = [
        _post("ud_a", UD, 0, players=("Mike Evans",)),                  # RW posts on him 3h later
        _post("rw_a", RW, 180, players=("Mike Evans",)),
        _post("ud_b", UD, 0, players=("Dallas Goedert",)),              # RW never does
    ]

    class Boom:
        def group(self, *a):
            raise AssertionError("one-account sessions need no model")

    s = _run(tmp_path, posts, Boom())
    assert s["st_ud_a"]["gap_kind"] == "late"
    assert s["st_ud_a"]["other_side_later"]["tweet_id"] == "rw_a"
    assert s["st_ud_a"]["other_side_later"]["delay_seconds"] == 180 * 60
    assert s["st_rw_a"]["gap_kind"] == "missed"        # UD's post was before, not after
    assert s["st_ud_b"]["gap_kind"] == "missed" and s["st_ud_b"]["other_side_later"] is None


def test_hype_rollup_and_story_tags(tmp_path):
    write_jsonl(tmp_path / "stories.jsonl", [
        {"story_id": "a", "status": "matched",
         "rotowire": {"tweet_id": "1", "created_at": "2026-09-09T12:00:00.000Z", "text": "Ruled out for Week 1."},
         "underdog": {"tweet_id": "2", "created_at": "2026-09-09T12:01:00.000Z", "text": "Out Week 1."},
         "time_delta_seconds": 60, "rotowire_first": True}])
    write_jsonl(tmp_path / "tweets.jsonl", [
        {"id": "h1", "account": UD, "created_at": "2026-09-09T12:00:00.000Z", "excluded_reason": "hype", "hype_kind": "soundbite"},
        {"id": "h2", "account": UD, "created_at": "2026-09-16T12:00:00.000Z", "excluded_reason": "hype", "hype_kind": "rumor"},
        {"id": "h3", "account": RW, "created_at": "2026-09-16T12:00:00.000Z", "excluded_reason": "hype", "hype_kind": "rumor"},
        {"id": "h0", "account": UD, "created_at": "2026-09-01T12:00:00.000Z", "excluded_reason": "hype"},  # preseason
        {"id": "n", "account": UD, "created_at": "2026-09-16T12:00:00.000Z", "excluded_reason": "promo"},
    ])
    out = agg.build_aggregates(data_dir=tmp_path, docs_data_dir=tmp_path / "docs",
                               season_weeks=WEEKS, rotowire_handle=RW)
    assert out["hype"]["weeks"] == {"Week 1": {"rotowire": 0, "underdog": 1},
                                    "Week 2": {"rotowire": 1, "underdog": 1}}
    assert out["hype"]["kinds"]["underdog"] == {"soundbite": 1, "rumor": 1}
    import json as _json
    st = _json.loads((tmp_path / "docs" / "stories.json").read_text())[0]
    assert st["season_week"] == "Week 1" and st["news_type"] == "game_status"


def test_old_prompt_version_groupings_are_redone(tmp_path):
    posts = [_post("ud1", UD, 0), _post("rw1", RW, 2)]
    g = FixedGrouper(["ud1", "rw1"])
    _run(tmp_path, posts, g)
    recs = load_jsonl(tmp_path / "developments.jsonl")
    assert recs[0]["prompt_version"] == develop.GROUP_PROMPT_VERSION
    recs[0]["prompt_version"] = develop.GROUP_PROMPT_VERSION - 1     # pretend it is stale
    write_jsonl(tmp_path / "developments.jsonl", recs)
    _run(tmp_path, posts, g)
    _run(tmp_path, posts, g)
    assert len(g.calls) == 2          # redone once, then cached again (last line wins)


def test_hour_rollup(tmp_path):
    def m(sid, rw_ts, ud_ts, delta):
        return {"story_id": sid, "status": "matched",
                "rotowire": {"tweet_id": sid + "r", "created_at": rw_ts, "text": "Out Week 1."},
                "underdog": {"tweet_id": sid + "u", "created_at": ud_ts, "text": "Out Week 1."},
                "time_delta_seconds": delta, "rotowire_first": delta > 0}
    write_jsonl(tmp_path / "stories.jsonl", [
        # 6:05 AM ET (10:05 UTC): RW first. Bucketed by the earlier (RW) post.
        m("a", "2026-09-13T10:05:00.000Z", "2026-09-13T11:10:00.000Z", 3900),
        # 7:30 AM ET: UD first by 7 min, bucketed by UD's 7:30 post.
        m("b", "2026-09-14T11:37:00.000Z", "2026-09-14T11:30:00.000Z", -420),
        m("pre", "2026-09-01T11:30:00.000Z", "2026-09-01T11:31:00.000Z", 60),   # preseason
    ])
    write_jsonl(tmp_path / "tweets.jsonl", [
        {"id": "1", "account": RW, "created_at": "2026-09-13T10:05:00.000Z", "is_news": True},
        {"id": "2", "account": UD, "created_at": "2026-09-14T11:30:00.000Z", "is_news": True},
        {"id": "3", "account": UD, "created_at": "2026-09-14T11:45:00.000Z", "is_news": False,
         "excluded_reason": "hype"},
        {"id": "4", "account": UD, "created_at": "2026-09-01T11:30:00.000Z", "is_news": True},
    ])
    hours = agg.build_aggregates(data_dir=tmp_path, docs_data_dir=tmp_path / "docs",
                                 season_weeks=WEEKS, rotowire_handle=RW)["hours"]
    assert len(hours) == 24
    assert hours[6] == {"hour": 6, "rotowire_posts": 1, "underdog_posts": 0, "matched": 1,
                        "rotowire_all": 1, "underdog_all": 0, "rotowire_links": 0, "underdog_links": 0,
                        "rotowire_first": 1, "median_lead_seconds": 3900.0}
    # The hype post is left out of news posts but counted in all posts.
    assert hours[7] == {"hour": 7, "rotowire_posts": 0, "underdog_posts": 1, "matched": 1,
                        "rotowire_all": 0, "underdog_all": 2, "rotowire_links": 0, "underdog_links": 0,
                        "rotowire_first": 0, "median_lead_seconds": -420.0}
    assert sum(h["matched"] for h in hours) == 2


def test_name_variants_share_a_key():
    assert normalize_name("D.J. Moore") == normalize_name("DJ Moore") == normalize_name("D. J. Moore")
    assert normalize_name("Pat Surtain II") == normalize_name("Patrick Surtain") == "patrick surtain"
    assert normalize_name("Ja'Marr Chase") == normalize_name("Ja’Marr Chase") == normalize_name("JaMarr Chase")
    assert normalize_name("Thomas White") != normalize_name("Tommy White")   # two MLB players


def test_punctuation_variants_match_across_feeds(tmp_path):
    posts = [_post("ud_moore", UD, 0, players=("D.J. Moore",)),
             _post("rw_moore", RW, 12, players=("DJ Moore",))]
    s = _run(tmp_path, posts, FixedGrouper(["ud_moore", "rw_moore"]))
    assert s["st_rw_moore_ud_moore"]["status"] == "matched"
    assert s["st_rw_moore_ud_moore"]["time_delta_seconds"] == -12 * 60


def test_roundup_matches_when_it_can_and_is_not_a_gap_when_it_cannot(tmp_path):
    names = ("Terry McLaurin", "Keenan Allen", "DeVonta Smith", "Justin Jefferson")
    posts = [_post("rw_round", RW, 10, players=names, event="status_change"),
             _post("ud_smith", UD, 0, players=("DeVonta Smith",), event="status_change")]

    class OneDev:
        def group(self, player, posts):
            return [{"label": player, "post_ids": [p["id"] for p in posts]}]

    s = _run(tmp_path, posts, OneDev())
    assert s["st_rw_round_ud_smith_devonta-smith"]["status"] == "matched"
    lone = s["st_rw_round_keenan-allen"]
    assert lone["status"] == "rotowire_only" and lone["roundup"] and lone["gap_kind"] == "roundup"
    summary = agg._summary(list(s.values()))
    assert summary["rotowire_only"] == 0 and summary["roundup_only"] == 3


def test_single_team_list_is_not_a_roundup():
    jets = [{"name": n, "team": "New York Jets"} for n in ("Breece Hall", "Mason Taylor", "Adonai Mitchell")]
    assert not develop.is_roundup(jets)
    assert develop.is_roundup(jets + [{"name": "Garrett Wilson", "team": "New York Jets"}])


def test_step_only_one_side_posted_is_an_update_gap(tmp_path):
    # Both cover the injury; only Underdog posts "headed to locker room".
    posts = [_post("ud_hurt", UD, 0), _post("rw_hurt", RW, 3), _post("ud_locker", UD, 8)]
    s = _run(tmp_path, posts, FixedGrouper(["ud_hurt", "rw_hurt"], ["ud_locker"]))
    locker = s["st_ud_locker"]
    assert locker["status"] == "underdog_only" and locker["gap_kind"] == "update"
    assert locker["other_side_near"]["tweet_id"] == "rw_hurt"
    summary = agg._summary(list(s.values()))
    assert summary["underdog_only"] == 0 and summary["underdog_update_only"] == 1


def test_same_development_outside_window_is_not_an_update_gap(tmp_path):
    posts = [_post("rw1", RW, 0), _post("rw2", RW, 80), _post("ud1", UD, 150)]
    s = _run(tmp_path, posts, FixedGrouper(["rw1", "rw2", "ud1"]))
    assert s["st_ud1"]["gap_kind"] != "update" and s["st_rw1"]["gap_kind"] != "update"


def test_included_hype_kind_counts_as_news():
    from src.classify import _apply
    c = {"is_news": False, "excluded_reason": "hype", "hype_kind": "rumor",
         "players": [{"name": "Joe Mixon", "team": None}], "player": "Joe Mixon"}
    r = _apply({}, c, include_hype_kinds=frozenset({"rumor"}))
    assert r["is_news"] and r["borderline"] == "rumor" and r["excluded_reason"] is None
    r = _apply({}, {**c, "hype_kind": "soundbite"}, include_hype_kinds=frozenset({"rumor"}))
    assert not r["is_news"] and r["excluded_reason"] == "hype"


def test_trend_week_line_is_a_trailing_four_week_median():
    def m(day, delta):
        ts = f"2026-09-{day:02d}T16:00:00.000Z"
        return {"story_id": f"s{day}{delta}", "status": "matched", "time_delta_seconds": delta,
                "rotowire": {"created_at": ts}, "underdog": {"created_at": ts}}
    # Weeks 1-5 (Tue-Mon from Sep 8): one story each, week 3 an outlier.
    stories = [m(9, -100), m(16, -120), m(23, -900), m(30, -110)]
    stories.append({**m(30, -90), "story_id": "x", "rotowire": {"created_at": "2026-10-07T16:00:00.000Z"},
                    "underdog": {"created_at": "2026-10-07T16:00:00.000Z"}})
    weeks = agg._trend(stories, WEEKS)["week"]
    assert [w["median_lead_seconds"] for w in weeks] == [-100, -120, -900, -110, -90]
    # Trailing 4 weeks pooled: the -900 week moves the line far less than its own dot.
    assert [w["rolling_median_seconds"] for w in weeks] == [-100, -110, -120, -115, -115]


def test_trend_keeps_empty_weeks_on_the_axis():
    def m(ts, delta):
        return {"story_id": ts, "status": "matched", "time_delta_seconds": delta,
                "rotowire": {"created_at": ts}, "underdog": {"created_at": ts}}
    # Week 1 and week 4 have stories; weeks 2-3 have none.
    weeks = agg._trend([m("2026-09-09T16:00:00.000Z", -100), m("2026-09-30T16:00:00.000Z", -60)],
                       WEEKS)["week"]
    assert [w["label"] for w in weeks] == ["Week 1", "Week 2", "Week 3", "Week 4"]
    assert [w["median_lead_seconds"] for w in weeks] == [-100, None, None, -60]
    assert weeks[1]["matched"] == 0 and weeks[2]["rolling_median_seconds"] == -100


def test_spelling_variant_used_by_one_account_folds_into_common_spelling(tmp_path):
    posts = [_post("rw1", RW, 0, players=("Jonathon Brooks",)),
             _post("ud0", UD, -600, players=("Jonathon Brooks",)),
             _post("ud1", UD, 2, players=("Jonathan Brooks",))]
    s = _run(tmp_path, posts, FixedGrouper(["rw1", "ud1"], ["ud0"]))
    assert s["st_rw1_ud1"]["status"] == "matched"
    # Two real players, each posted by both accounts, never merge.
    news = [_post(f"{a}{n}", acct, 0, players=(n,)) for n in ("Jalen Williams", "Jaylin Williams")
            for a, acct in (("r", RW), ("u", UD))]
    assert develop.spelling_aliases(news, RW) == {}


def _wide(monkeypatch):
    cfg = {**CFG, "matching": {"time_window_minutes": 90, "match_window_minutes": 360}}
    monkeypatch.setattr(develop, "load_config", lambda: cfg)


def test_late_first_reply_joins_the_session_and_matches_within_six_hours(tmp_path, monkeypatch):
    _wide(monkeypatch)
    posts = [_post("ud1", UD, 0), _post("rw1", RW, 150)]          # RW 2.5h late
    s = _run(tmp_path, posts, FixedGrouper(["ud1", "rw1"]))
    assert s["st_rw1_ud1"]["status"] == "matched"
    assert s["st_rw1_ud1"]["time_delta_seconds"] == -150 * 60


def test_bridge_only_for_the_other_accounts_first_post(tmp_path, monkeypatch):
    _wide(monkeypatch)
    # UD posts twice 3h apart with no RW post: two separate sessions, no model needed.
    posts = [_post("ud1", UD, 0), _post("ud2", UD, 180)]

    class Boom:
        def group(self, *a):
            raise AssertionError("one-account sessions need no model")

    s = _run(tmp_path, posts, Boom())
    assert not s["st_ud2"]["same_event_duplicate"]


def test_beyond_six_hours_is_not_a_match(tmp_path, monkeypatch):
    _wide(monkeypatch)
    posts = [_post("ud1", UD, 0), _post("rw1", RW, 400)]
    s = _run(tmp_path, posts, FixedGrouper(["ud1", "rw1"]))
    assert not any(v["status"] == "matched" for v in s.values())


def test_roundup_only_matches_within_ninety_minutes(tmp_path, monkeypatch):
    _wide(monkeypatch)
    names = ("Puka Nacua", "Nico Collins", "Brock Bowers", "Chris Olave")
    posts = [_post("rw_round", RW, 0, players=names), _post("ud_olave", UD, 300, players=("Chris Olave",)),
             _post("ud_puka", UD, 30, players=("Puka Nacua",))]

    class OneDev:
        def group(self, player, posts):
            return [{"label": player, "post_ids": [p["id"] for p in posts]}]

    s = _run(tmp_path, posts, OneDev())
    assert s["st_rw_round_ud_puka_puka-nacua"]["status"] == "matched"          # 30m: ok
    assert not any(v["status"] == "matched" and v["player_key"] == "chris olave" for v in s.values())


def test_more_details_reply_is_a_link_reply_not_news():
    from src.classify import _apply
    c = {"is_news": False, "excluded_reason": "no_player", "players": []}
    rec = {"text": "@AdamSchefter More Details 👉 https://t.co/x", "referenced_tweets": [{"type": "replied_to", "id": "1"}]}
    assert _apply(rec, c)["excluded_reason"] == "link_reply"
    plain = {"text": "More Details on the Chase injury soon", "referenced_tweets": []}
    assert _apply(plain, c)["excluded_reason"] == "no_player"
