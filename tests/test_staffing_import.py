from __future__ import annotations

from datetime import date, datetime

import yaml

from src import staffing_import as si

NAMES = si.Names(["Cullum", "Nick R.", "Nick B.", "Eric C.", "Kevin", "Paul", "Adam", "Sam"],
                 {"Kevin": "KOB", "Eric": "Eric C."})


def test_slot_start():
    assert [si.slot_start(x) for x in ("8-9 AM", "11-Noon", "12-1 PM", "1-2 PM", "11-Mid", "Writers")] == \
        ["08:00", "11:00", "12:00", "13:00", "23:00", None]


def test_names_fix_typos_aliases_and_missing_slashes():
    assert NAMES.split("NIck B.") == ["Nick B."]
    assert NAMES.split("Eric C") == ["Eric C."]
    assert NAMES.split("Kevin") == ["KOB"] and NAMES.split("Eric") == ["Eric C."]
    assert NAMES.split("CullumNick R.") == ["Cullum", "Nick R."]
    assert NAMES.split("PaulCullum") == ["Paul", "Cullum"]
    assert NAMES.split("Samuel") == ["Samuel"]          # not "Sam" + "uel"
    assert NAMES.split("-") == []


def test_parse_cell_handoff_only_on_first_name():
    assert si.parse_cell("Adam (1:50)/Cullum", "13:00", NAMES) == [{"name": "Adam", "until": "13:50"}, "Cullum"]
    assert si.parse_cell("Cullum/Paul (10:30)", "10:00", NAMES) == ["Cullum", "Paul"]
    assert si.parse_cell("Sasha", "18:00", NAMES) == ["Sasha"]


def test_tab_dates_with_and_without_slash():
    today = date(2026, 10, 5)
    assert si._date_for_tab("10/4", today) == date(2026, 10, 4)
    assert si._date_for_tab("104", today) == date(2026, 10, 4)
    assert si._date_for_tab("1011", today) == date(2026, 10, 11)
    assert si._date_for_tab("111", today) == date(2026, 11, 1)
    assert si._date_for_tab("Base Schedule", today) is None


class WS:
    def __init__(self, title, rows):
        self.title, self.rows = title, rows

    def iter_rows(self, values_only=True):
        return iter(self.rows)


class WB:
    def __init__(self, sheets):
        self.worksheets = sheets
        self.sheetnames = [s.title for s in sheets]


def test_breaking_news_reads_nfl_columns_by_day():
    wb = WB([WS("October 26", [
        (datetime(2026, 10, 1, 10), None, None, None),
        ("Thursday", "NFL", "NFL News", "MLB"),
        ("8-9 AM", "Kevin", None, "Ryan B."),
        ("6-7 PM", "Mensio", "Paul/Nick B.", "Nick B."),
        (None, None, None, None),
    ]), WS("Copy of November 26", [(datetime(2026, 11, 1, 10), None)])])
    out = si.breaking_news(wb, NAMES, date(2026, 4, 1))
    assert list(out) == ["2026-10-01"]
    assert out["2026-10-01"]["slots"] == [{"start": "08:00", "nfl": ["KOB"]},
                                          {"start": "18:00", "nfl": ["Mensio"], "nfl_news": ["Paul", "Nick B."]}]


def test_write_keeps_hand_written_files(tmp_path):
    (tmp_path / "2026-10-01.yaml").write_text("date: '2026-10-01'\nslots: [{start: '08:00'}]\n")
    sched = {"source": "x_watcher", "roles": {"tweets": "Tweets"}, "slots": [{"start": "08:00", "tweets": ["KOB"]}]}
    res = si.write({"2026-10-01": dict(sched), "2026-10-02": dict(sched)}, tmp_path, "America/New_York")
    assert res == {"days": 2, "written": 1, "hand_written_kept": 1}
    assert yaml.safe_load((tmp_path / "2026-10-02.yaml").read_text())["source"] == "x_watcher"
