from __future__ import annotations

import json

from src import desk_summary as dsum


def _i(key, parent, category="new", assignee="Matthew Kulina", sprint=None, typ="Task"):
    return {"key": key, "summary": key, "type": typ, "parent": parent, "status": category, "category": category,
            "assignee": assignee, "priority": "Medium", "resolved": None,
            "sprints": [{"name": sprint[0], "state": sprint[1], "start": "2026-10-01", "end": "2026-10-14"}] if sprint else []}


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
