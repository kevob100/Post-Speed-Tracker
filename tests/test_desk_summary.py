from __future__ import annotations

import json

from src import desk_summary as dsum


def _dates(state):
    # Sprint dates around today, since the summary trusts dates over Jira's state.
    from datetime import datetime, timedelta, timezone
    t = datetime.now(timezone.utc)
    a, b = {"active": (-3, 10), "future": (11, 24), "closed": (-30, -17)}[state]
    return (t + timedelta(days=a)).isoformat(), (t + timedelta(days=b)).isoformat()


def _i(key, parent, category="new", assignee="Matthew Kulina", sprint=None, typ="Task"):
    return {"key": key, "summary": key, "type": typ, "parent": parent, "status": category, "category": category,
            "assignee": assignee, "priority": "Medium", "resolved": None,
            "sprints": [{"name": sprint[0], "state": sprint[1], "start": _dates(sprint[1])[0], "end": _dates(sprint[1])[1]}] if sprint else []}


def test_initiative_trusts_sprint_dates_over_jira_state(tmp_path):
    from datetime import datetime, timedelta, timezone
    t = datetime.now(timezone.utc)
    late = {"name": "S21", "state": "future", "start": (t - timedelta(days=1)).isoformat(), "end": (t + timedelta(days=12)).isoformat()}
    over = {"name": "S20", "state": "active", "start": (t - timedelta(days=16)).isoformat(), "end": (t - timedelta(days=2)).isoformat()}
    issues = [_i("E1", None, typ="Epic"), {**_i("T1", "E1"), "sprints": [over, late]}]
    (tmp_path / dsum.INITIATIVE_FILE).write_text(json.dumps({"root": "R", "issues": issues}))
    out = dsum.initiative(tmp_path, {"prioritize_assignee": "Matthew Kulina"})
    assert [(s["name"], s["state"]) for s in out["sprints"]] == [("S21", "active")]


def test_initiative_prioritized_needs_assignee_and_open_sprint(tmp_path):
    issues = [_i("E1", "ROOT", typ="Epic"),
              _i("T1", "E1", sprint=("S20", "active")),                    # prioritized
              _i("T2", "E1", sprint=("S21", "future")),                    # prioritized, next sprint
              _i("T3", "E1", sprint=("S19", "closed")),                    # backlog: sprint closed
              _i("T4", "E1", assignee="Andrew Parr", sprint=("S20", "active")),  # backlog: someone else
              _i("T5", "E1"),                                              # backlog: no sprint
              _i("T6", "E1", category="done", sprint=("S20", "active"))]   # done
    (tmp_path / dsum.INITIATIVE_FILE).write_text(json.dumps({"root": "ROOT", "issues": issues}))
    out = dsum.initiative(tmp_path, {"prioritize_assignee": "Matthew Kulina"})
    e = out["epics"][0]
    assert [t["key"] for t in e["prioritized"]] == ["T1", "T2"]
    assert [t["key"] for t in e["backlog"]] == ["T3", "T4", "T5"]
    assert [t["key"] for t in e["done"]] == ["T6"]
    assert [s["name"] for s in out["sprints"]] == ["S20", "S21"]


def test_turbo_windows_use_sunday_zero():
    turbo = {"timezone": "America/New_York", "windows": [{"day": 0, "start": "11:00", "end": "18:00"}]}
    assert dsum.in_windows("2026-10-11T16:00:00Z", turbo)       # Sun 12pm ET
    assert not dsum.in_windows("2026-10-12T16:00:00Z", turbo)   # Mon 12pm ET


def test_margin_rows_carry_each_days_own_median_and_count():
    from datetime import datetime, timezone

    def s(day, rw, ud):  # minute offsets on 2026-10-<day>
        return {"status": "matched", "rotowire": {"created_at": f"2026-10-{day}T12:{rw:02d}:00Z"},
                "underdog": {"created_at": f"2026-10-{day}T12:{ud:02d}:00Z"}}
    stories = [s(13, 5, 0), s(13, 3, 0), s(13, 1, 0), s(14, 0, 2)]
    rows = {r[0]: r for r in dsum.margin(stories, datetime(2026, 10, 15, 20, tzinfo=timezone.utc))["rolling"]}
    assert rows["2026-10-13"][3:] == [180, 3]   # median of 300, 180, 60 seconds behind
    assert rows["2026-10-14"][3:] == [-120, 1]  # RotoWire first by 2 minutes
    assert rows["2026-10-15"][3:] == [None, 0]


def test_initiative_carries_unfinished_work_from_an_ended_unclosed_sprint(tmp_path):
    from datetime import datetime, timedelta, timezone
    t = datetime.now(timezone.utc)
    cur = {"name": "S21", "state": "future", "start": (t - timedelta(days=1)).isoformat(), "end": (t + timedelta(days=12)).isoformat()}
    over = {"name": "S20", "state": "active", "start": (t - timedelta(days=16)).isoformat(), "end": (t - timedelta(days=2)).isoformat()}
    issues = [_i("E1", None, typ="Epic"), {**_i("T1", "E1", category="indeterminate"), "sprints": [over]},
              {**_i("T2", "E1"), "sprints": [cur]}]
    (tmp_path / dsum.INITIATIVE_FILE).write_text(json.dumps({"root": "R", "issues": issues}))
    out = dsum.initiative(tmp_path, {"prioritize_assignee": "Matthew Kulina"})
    assert sorted((t["key"], t["sprint"]) for t in out["epics"][0]["prioritized"]) == [("T1", "S21"), ("T2", "S21")]
