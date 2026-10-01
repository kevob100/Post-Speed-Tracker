"""Phase 4 aggregate builder.

Reads data/stories.jsonl (Stage 3 output) and data/reviews.jsonl (human-owned),
applies reviews, computes the headline metrics and weekly/monthly rollups, and
writes the precomputed JSON the static dashboard reads:

  docs/data/stories.json     -- final stories with review status applied
  docs/data/aggregates.json  -- summary KPIs + weekly + monthly rollups

Everything here is pure/deterministic and safe to run with no stories yet (it
emits valid empty structures). It NEVER writes reviews.jsonl.

Metric rules (PRD section 9):
  - matched           = two-sided stories not rejected by review
  - rotowire_first_rate = matched where rotowire_first / matched
  - lead time         = time_delta_seconds over matched (median is headline, mean too)
  - coverage gaps     = rotowire_only / underdog_only counts+lists, never in timing

Run: python -m src.aggregate
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import DATA_DIR, DOCS_DATA_DIR
from .news_type import LABELS as NEWS_TYPE_LABELS
from .news_type import news_type
from .store import load_jsonl, now_iso, parse_dt


def _latest_reviews(reviews: list[dict]) -> dict[str, dict]:
    """story_id -> most recent review (reviews.jsonl is append-only)."""
    out: dict[str, dict] = {}
    for r in reviews:
        sid = r.get("story_id")
        if not sid:
            continue
        prev = out.get(sid)
        if prev is None or (r.get("reviewed_at") or "") >= (prev.get("reviewed_at") or ""):
            out[sid] = r
    return out


def apply_reviews(stories: list[dict], reviews: list[dict]) -> list[dict]:
    """Annotate each story with review_status; merged-away stories are marked.

    review_status: pending | confirmed | rejected | merged
    """
    latest = _latest_reviews(reviews)
    out: list[dict] = []
    for s in stories:
        s = dict(s)
        rv = latest.get(s.get("story_id"))
        decision = (rv or {}).get("decision")
        if decision == "reject":
            s["review_status"] = "rejected"
        elif decision == "confirm":
            s["review_status"] = "confirmed"
        elif decision == "merge":
            s["review_status"] = "merged"
            s["merge_into"] = rv.get("merge_into")
        else:
            s["review_status"] = "pending"
        out.append(s)
    return out


def _story_time(story: dict) -> datetime:
    """Canonical time for rollup bucketing: the earliest side present."""
    times = [
        side["created_at"]
        for side in (story.get("rotowire"), story.get("underdog"))
        if side and side.get("created_at")
    ]
    return min(parse_dt(t) for t in times)


def _is_active(story: dict) -> bool:
    """Counts toward metrics? Rejected and merged-away stories do not."""
    return story.get("review_status") not in ("rejected", "merged")


def _is_dup(s: dict) -> bool:
    return bool(s.get("same_event_duplicate"))


def _summary(stories: list[dict]) -> dict:
    active = [s for s in stories if _is_active(s)]
    matched = [s for s in active if s.get("status") == "matched"]
    deltas = [s["time_delta_seconds"] for s in matched if s.get("time_delta_seconds") is not None]
    rw_first = sum(1 for s in matched if s.get("rotowire_first"))

    # Split matched deltas by who posted first (delta > 0 => RotoWire first). "Lead" is the
    # average head start when RotoWire wins; "Trail" is the average lag when it loses.
    leads = [d for d in deltas if d > 0]
    trails = [-d for d in deltas if d < 0]

    # Coverage gaps count TRUE gaps only (a post with no counterpart on the other feed);
    # same-event duplicates that lost the 1-to-1 match are tallied separately.
    return {
        "matched": len(matched),
        "rotowire_first": rw_first,
        "rotowire_first_rate": round(rw_first / len(matched), 4) if matched else None,
        "median_lead_seconds": round(statistics.median(deltas), 1) if deltas else None,
        "mean_lead_seconds": round(statistics.mean(deltas), 1) if deltas else None,
        "avg_lead_seconds": round(statistics.mean(leads), 1) if leads else None,
        "avg_trail_seconds": round(statistics.mean(trails), 1) if trails else None,
        # Medians of the same splits: a handful of hour-long wins drag the means around.
        "median_win_lead_seconds": round(statistics.median(leads), 1) if leads else None,
        "median_win_trail_seconds": round(statistics.median(trails), 1) if trails else None,
        "rotowire_only": sum(1 for s in active if s.get("status") == "rotowire_only" and not _is_dup(s)),
        "underdog_only": sum(1 for s in active if s.get("status") == "underdog_only" and not _is_dup(s)),
        "rotowire_duplicate": sum(1 for s in active if s.get("status") == "rotowire_only" and _is_dup(s)),
        "underdog_duplicate": sum(1 for s in active if s.get("status") == "underdog_only" and _is_dup(s)),
    }


def _rollup(stories: list[dict], period_of) -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for s in stories:
        if not _is_active(s):
            continue
        buckets.setdefault(period_of(_story_time(s)), []).append(s)

    rows = []
    for period in sorted(buckets):
        group = buckets[period]
        row = {"period": period}
        row.update(_summary(group))
        rows.append(row)
    return rows


def _iso_week(dt: datetime) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def _month(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def season_week_of(weeks_cfg: dict):
    """Return dt -> (sort_key, label, start, end) for a sport's season-week calendar.

    weeks_cfg (sports.<sport>.season_weeks in config.yaml):
      start:      local date Week 1 begins, e.g. '2026-09-08' (a Tuesday for the NFL)
      boundary:   local time each week rolls over, e.g. '06:00' so late Monday-night news
                  stays with that week's games (default '00:00')
      timezone:   default 'America/New_York'
      preseason_label: bucket for anything before Week 1 (default 'Preseason')
    Weeks are 7 days. start/end are local dates (end inclusive) for display.
    """
    tz = ZoneInfo(weeks_cfg.get("timezone") or "America/New_York")
    hh, mm = (int(x) for x in str(weeks_cfg.get("boundary") or "00:00").split(":"))
    start = datetime.fromisoformat(str(weeks_cfg["start"])).replace(hour=hh, minute=mm, tzinfo=tz)
    pre = weeks_cfg.get("preseason_label") or "Preseason"

    def of(dt: datetime):
        local = dt.astimezone(tz)
        if local < start:
            return (0, pre, None, (start - timedelta(days=1)).date().isoformat())
        n = (local - start) // timedelta(days=7) + 1
        wk_start = start + timedelta(days=7 * (n - 1))
        return (n, f"Week {n}", wk_start.date().isoformat(),
                (wk_start + timedelta(days=6)).date().isoformat())
    return of


def _season_rollup(stories: list[dict], weeks_cfg: dict) -> list[dict]:
    of = season_week_of(weeks_cfg)
    buckets: dict[tuple, list[dict]] = {}
    for s in stories:
        if _is_active(s):
            buckets.setdefault(of(_story_time(s)), []).append(s)
    rows = []
    for key in sorted(buckets):
        n, label, start, end = key
        row = {"period": label, "week": n, "start": start, "end": end}
        row.update(_summary(buckets[key]))
        row["followups"] = row["rotowire_duplicate"] + row["underdog_duplicate"]
        rows.append(row)
    return rows


def _type_cell(group: list[dict]) -> dict:
    deltas = [s["time_delta_seconds"] for s in group]
    return {
        "matched": len(group),
        "rotowire_first_rate": round(sum(1 for d in deltas if d > 0) / len(group), 4),
        "median_lead_seconds": round(statistics.median(deltas), 1),
    }


def _news_type_rollup(stories: list[dict], weeks_cfg: dict) -> dict:
    """Matched stories by news type x season week (Week 1 on; preseason is left out).

    Returns {"weeks": ["Week 1", ...], "types": [{key, label, season, weeks: {label: cell}}]}
    with types ordered by season volume. A cell is {matched, rotowire_first_rate,
    median_lead_seconds}; a type with no stories in a week has no entry for it.
    """
    of = season_week_of(weeks_cfg)
    by_type: dict[str, dict[tuple, list[dict]]] = {}
    week_keys: set[tuple] = set()
    for s in stories:
        if not _is_active(s) or s.get("status") != "matched" or s.get("time_delta_seconds") is None:
            continue
        n, label, _, _ = of(_story_time(s))
        if n == 0:
            continue
        week_keys.add((n, label))
        by_type.setdefault(news_type(s), {}).setdefault((n, label), []).append(s)

    types = []
    for key, weeks in by_type.items():
        season = [s for group in weeks.values() for s in group]
        types.append({
            "key": key,
            "label": NEWS_TYPE_LABELS[key],
            "season": _type_cell(season),
            "weeks": {label: _type_cell(g) for (_, label), g in sorted(weeks.items())},
        })
    types.sort(key=lambda t: -t["season"]["matched"])
    return {"weeks": [label for _, label in sorted(week_keys)], "types": types}


def _hype_rollup(tweets: list[dict], weeks_cfg: dict, rotowire_handle: str) -> dict:
    """Posts tagged hype (not a report) per season week and account, Week 1 on.

    {"weeks": {"Week 1": {"rotowire": n, "underdog": n}, ...}, "kinds": {"underdog": {kind: n}}}
    """
    of = season_week_of(weeks_cfg)
    weeks: dict[tuple, dict] = {}
    kinds: dict[str, dict[str, int]] = {"rotowire": {}, "underdog": {}}
    for t in tweets:
        if t.get("excluded_reason") != "hype":
            continue
        n, label, _, _ = of(parse_dt(t["created_at"]))
        if n == 0:
            continue
        side = "rotowire" if t.get("account") == rotowire_handle else "underdog"
        row = weeks.setdefault((n, label), {"rotowire": 0, "underdog": 0})
        row[side] += 1
        kind = t.get("hype_kind") or "other"
        kinds[side][kind] = kinds[side].get(kind, 0) + 1
    return {"weeks": {label: row for (_, label), row in sorted(weeks.items())}, "kinds": kinds}


def build_aggregates(
    data_dir: Path = DATA_DIR,
    docs_data_dir: Path = DOCS_DATA_DIR,
    stories_path: Path | None = None,
    reviews_path: Path | None = None,
    season_weeks: dict | None = None,
    rotowire_handle: str | None = None,
) -> dict:
    stories_path = stories_path or (data_dir / "stories.jsonl")
    reviews_path = reviews_path or (data_dir / "reviews.jsonl")

    stories = apply_reviews(load_jsonl(stories_path), load_jsonl(reviews_path))

    aggregates = {
        "generated_at": now_iso(),
        "summary": _summary(stories),
        "weekly": _rollup(stories, _iso_week),
        "monthly": _rollup(stories, _month),
    }
    if season_weeks:
        aggregates["season_weeks"] = _season_rollup(stories, season_weeks)
        aggregates["news_types"] = _news_type_rollup(stories, season_weeks)
        # Tag each story so the dashboard can list examples per week / news type.
        of = season_week_of(season_weeks)
        for st in stories:
            st["season_week"] = of(_story_time(st))[1]
            if st.get("status") == "matched":
                st["news_type"] = news_type(st)
        if rotowire_handle:
            aggregates["hype"] = _hype_rollup(load_jsonl(data_dir / "tweets.jsonl"),
                                              season_weeks, rotowire_handle)

    docs_data_dir.mkdir(parents=True, exist_ok=True)
    _write_json(docs_data_dir / "stories.json", stories)
    _write_json(docs_data_dir / "aggregates.json", aggregates)
    return aggregates


def _write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2))
    tmp.replace(path)


if __name__ == "__main__":
    agg = build_aggregates()
    s = agg["summary"]
    print(
        f"Aggregates built -> docs/data/. matched={s['matched']} "
        f"rw_first_rate={s['rotowire_first_rate']} "
        f"median_lead={s['median_lead_seconds']}s "
        f"gaps(rw_only={s['rotowire_only']}, ud_only={s['underdog_only']})"
    )
