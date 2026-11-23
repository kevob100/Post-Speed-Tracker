from __future__ import annotations

from datetime import datetime, timezone

from src import practice
from src.store import write_jsonl

RW, UD = "RotoWireNFL", "UnderdogNFL"


def post(tid, acct, ts, text, key="chris godwin"):
    return {"id": tid, "account": acct, "created_at": ts, "is_news": True, "text": text,
            "players": [{"name": "Chris Godwin", "player_key": key}], "player_key": key}


class Fake:
    def __init__(self, labels): self.labels = labels; self.calls = 0
    def label(self, text):
        self.calls += 1
        return self.labels[text]


def test_godwin_day_shows_underdog_skipped_pre_practice(tmp_path):
    # Thu Oct 1: RW pre at 11:39 ET, both post around 16:19 ET.
    posts = [post("rw_pre", RW, "2026-10-01T15:39:00.000Z", "Chris Godwin (ankle) is participating in Thursday's practice."),
             post("ud_post", UD, "2026-10-01T20:19:00.000Z", "Chris Godwin (ankle) fully practices Thursday."),
             post("rw_post", RW, "2026-10-01T20:19:30.000Z", "Chris Godwin was a full participant on Thursday's report.")]
    write_jsonl(tmp_path / "tweets.jsonl", posts)
    fake = Fake({posts[0]["text"]: "pre", posts[1]["text"]: "post", posts[2]["text"]: "post"})
    phases = practice.label_phases(tmp_path, labeler=fake)
    assert phases == {"rw_pre": "pre", "ud_post": "post", "rw_post": "post"}
    practice.label_phases(tmp_path, labeler=fake)              # cached: no new calls
    assert fake.calls == 3
    r = practice.rollup(tmp_path, RW, phases, datetime(2026, 10, 5, tzinfo=timezone.utc))
    day = r["recent"][0]
    assert "rotowire" in day["pre"] and "underdog" not in day["pre"]
    pre, postp = r["phases"]["pre"], r["phases"]["post"]
    assert (pre["rotowire_only"], pre["underdog_only"]) == (1, 0)
    assert postp["both"] == 1 and postp["ties"] == 1


def test_guess_and_parse():
    assert practice.guess_phase("Zay Flowers officially limited in practice Thursday.") == "post"
    assert practice.guess_phase("Puka Nacua was not spotted during the portion of practice open to the media.") == "pre"
    assert practice.parse_phase('{"phase": "pre"}') == "pre" and practice.parse_phase("junk") == "other"


def test_weekly_pre_counts_only_rotowire_by_monday_week():
    from src.practice import weekly_pre
    rw = {"rotowire": {"created_at": "2026-11-20T15:00:00Z"}}
    both = {"rotowire": {"created_at": "2026-11-20T15:00:00Z"}, "underdog": {"created_at": "2026-11-20T15:00:30Z"}}
    rows = [{"date": "2026-11-20", "pre": rw, "post": {}},          # Fri, week of 11/16
            {"date": "2026-11-22", "pre": both, "post": {}},        # Sun, same week
            {"date": "2026-11-23", "pre": rw, "post": {}},          # Mon, next week
            {"date": "2026-11-23", "pre": {}, "post": rw}]          # post only: not a pre day
    w = weekly_pre(rows)
    assert [x["week"] for x in w] == ["2026-11-16", "2026-11-23"]
    assert (w[0]["days"], w[0]["rotowire_only"], w[0]["both"], w[0]["ties"]) == (2, 1, 1, 1)
    assert (w[1]["days"], w[1]["rotowire_only"]) == (1, 1)


def test_practice_line_in_weekly_post():
    from datetime import datetime
    from src.news_recap import practice_line
    wk = [{"week": "2026-11-09", "days": 40, "rotowire_only": 27, "underdog_only": 5,
           "rotowire_posted_rate": 0.875, "underdog_posted_rate": 0.325},
          {"week": "2026-11-16", "days": 46, "rotowire_only": 28, "underdog_only": 0,
           "rotowire_posted_rate": 1.0, "underdog_posted_rate": 0.3913}]
    line = practice_line(wk, datetime(2026, 11, 16))
    assert line == ("Pre-practice notes only we posted: *28* (was 27) · only Underdog: 0 · "
                    "we covered 100% of 46 player practice days, Underdog 39%")
    assert practice_line(wk, datetime(2026, 11, 23)) is None
    assert practice_line(None, datetime(2026, 11, 16)) is None
