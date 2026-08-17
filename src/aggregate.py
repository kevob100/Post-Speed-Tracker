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
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR, DOCS_DATA_DIR
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


def build_aggregates(
    data_dir: Path = DATA_DIR,
    docs_data_dir: Path = DOCS_DATA_DIR,
    stories_path: Path | None = None,
    reviews_path: Path | None = None,
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
