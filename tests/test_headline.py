import json
from datetime import datetime, timezone

from src import headline


def _edit_row(day, template, posts, unchanged):
    return {"day": day, "template": template, "posts": posts, "scored": posts, "unchanged": unchanged}


def test_templates_windows_and_series(tmp_path):
    rows = [_edit_row("2026-11-10", "freeform", 30, 3), _edit_row("2026-11-10", "game_day", 10, 4),
            _edit_row("2026-11-11", "freeform", 20, 2), _edit_row("2026-11-11", "practice_report", 20, 10),
            _edit_row("2026-11-12", "freeform", 5, 0)]  # today: partial, left out
    (tmp_path / "x_post_edits.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    out = headline.templates(tmp_path, datetime(2026, 11, 12, 5, tzinfo=timezone.utc))
    cur = out["d7"]["cur"]
    assert (cur["template_posts"], cur["freeform_posts"]) == (30, 50)
    assert cur["share"] == 37.5
    assert cur["template_unedited"] == round(100 * 14 / 30, 1)
    assert cur["automated"] == round(100 * 14 / 80, 1)
    assert out["d7"]["prior"]["share"] is None  # nothing before 11/10
    assert [p[0] for p in out["daily"]] == ["2026-11-10", "2026-11-11"]
    assert out["daily"][-1][3] == 37.5  # 7-day share on 11/11


def test_race_counts_ties_as_neither_first():
    r = headline._race([-120, -30, 0, 45, 200])
    assert r["n"] == 5 and r["median"] == 0
    assert r["rw_first_pct"] == 20.0 and r["tie_pct"] == 60.0 and r["ud_first_pct"] == 20.0
