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

from . import develop, focus, games, practice, source, staffing
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


def _is_gap(s: dict, status: str) -> bool:
    """A true coverage gap: one-sided, not a follow-up, not an unmatched roundup post, and
    not an update gap (the other account covered the story, just not this step)."""
    return (s.get("status") == status and not _is_dup(s) and not s.get("roundup")
            and s.get("gap_kind") != "update")


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
    # same-event duplicates that lost the 1-to-1 match are tallied separately, and so are
    # unmatched roundup posts (one digest naming many players is not many exclusives).
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
        "rotowire_only": sum(1 for s in active if _is_gap(s, "rotowire_only")),
        "underdog_only": sum(1 for s in active if _is_gap(s, "underdog_only")),
        "roundup_only": sum(1 for s in active if s.get("roundup") and not _is_dup(s)),
        # Steps of a story both covered that only one account posted.
        "rotowire_update_only": sum(1 for s in active if s.get("status") == "rotowire_only"
                                    and s.get("gap_kind") == "update" and not _is_dup(s)),
        "underdog_update_only": sum(1 for s in active if s.get("status") == "underdog_only"
                                    and s.get("gap_kind") == "update" and not _is_dup(s)),
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


def _hour_rollup(stories: list[dict], tweets: list[dict], weeks_cfg: dict,
                 rotowire_handle: str) -> list[dict]:
    """Per hour of the day (season timezone), Week 1 on: news volume and head-to-heads.

    One row per hour 0-23: rotowire_posts / underdog_posts count news posts (hype and other
    non-news excluded) made that hour; rotowire_all / underdog_all count every collected post
    with nothing excluded; rotowire_links / underdog_links count the "More Details" link
    replies within that total; matched / rotowire_first / median_lead_seconds cover
    matched stories, bucketed by the hour of the story's FIRST post.
    """
    of = season_week_of(weeks_cfg)
    tz = ZoneInfo(weeks_cfg.get("timezone") or "America/New_York")
    rows = [{"hour": h, "rotowire_posts": 0, "underdog_posts": 0, "rotowire_all": 0,
             "underdog_all": 0, "rotowire_links": 0, "underdog_links": 0, "_d": []} for h in range(24)]
    for t in tweets:
        dt = parse_dt(t["created_at"])
        if of(dt)[0] == 0:
            continue
        side = "rotowire" if t.get("account") == rotowire_handle else "underdog"
        row = rows[dt.astimezone(tz).hour]
        row[f"{side}_all"] += 1
        if t.get("excluded_reason") == "link_reply":
            row[f"{side}_links"] += 1     # "More Details" link to the full write-up
        if t.get("is_news"):
            row[f"{side}_posts"] += 1
    for s in stories:
        if (not _is_active(s) or s.get("status") != "matched"
                or s.get("time_delta_seconds") is None):
            continue
        first = _story_time(s)
        if of(first)[0] == 0:
            continue
        rows[first.astimezone(tz).hour]["_d"].append(s["time_delta_seconds"])
    for r in rows:
        d = r.pop("_d")
        r["matched"] = len(d)
        r["rotowire_first"] = sum(1 for x in d if x > 0)
        r["median_lead_seconds"] = round(statistics.median(d), 1) if d else None
    return rows


def _trend(stories: list[dict], weeks_cfg: dict | None) -> dict:
    """Median margin over time: is RotoWire closing the gap?

    For each day / week / month: matched story count, how many RotoWire was first on, and
    the median time_delta_seconds (positive = RotoWire earlier). Days and months use the
    season timezone (US Eastern by default). Weeks follow the season calendar when there is
    one (NFL Tue-Mon with its rollover hour, numbered Week 1+, earlier weeks labelled by
    start date) and ISO weeks otherwise. The day series also carries a trailing 7-day median
    (over the stories themselves, not an average of daily medians) to show the trend
    through day-to-day noise.
    """
    tz = ZoneInfo((weeks_cfg or {}).get("timezone") or "America/New_York")
    matched = sorted(
        ((_story_time(s), s["time_delta_seconds"]) for s in stories
         if _is_active(s) and s.get("status") == "matched" and s.get("time_delta_seconds") is not None),
        key=lambda x: x[0])

    # Matched stories with a source tweet (src/source.py): (time, RW secs, UD secs).
    sourced = sorted(
        ((_story_time(s), s["rotowire_secs_from_source"], s["underdog_secs_from_source"])
         for s in stories if _is_active(s) and s.get("status") == "matched"
         and s.get("underdog_secs_from_source") is not None),
        key=lambda x: x[0])

    if weeks_cfg:
        hh, mm = (int(x) for x in str(weeks_cfg.get("boundary") or "00:00").split(":"))
        w1 = datetime.fromisoformat(str(weeks_cfg["start"])).replace(hour=hh, minute=mm, tzinfo=tz)

        def week_of(dt):
            n = (dt.astimezone(tz) - w1) // timedelta(days=7) + 1
            start = (w1 + timedelta(days=7 * (n - 1))).date()
            return start.isoformat(), (f"Week {n}" if n >= 1 else f"Wk of {start:%b} {start.day}")
    else:
        def week_of(dt):
            d = dt.astimezone(tz).date()
            start = d - timedelta(days=d.weekday())
            return start.isoformat(), f"Wk of {start:%b} {start.day}"

    def series(key_of, trailing: int | None = None) -> list[dict]:
        """Buckets in order. With `trailing`, each also carries rolling_median_seconds: the
        median over every story in it and the trailing-1 buckets before it (pooled stories,
        not a median of medians), so one thin or odd bucket does not swing the line."""
        buckets: dict[str, dict] = {}
        # Every period from the first story to the last, so a period with no matched story
        # keeps its place on the axis (median None) instead of silently disappearing.
        if matched:
            day = matched[0][0].astimezone(tz).replace(hour=12, minute=0, second=0, microsecond=0)
            while day <= matched[-1][0] + timedelta(days=1):
                key, label = key_of(day)
                buckets.setdefault(key, {"period": key, "label": label, "_d": []})
                day += timedelta(days=1)
        for dt, d in matched:
            key, label = key_of(dt)
            b = buckets.setdefault(key, {"period": key, "label": label, "_d": []})
            b["_d"].append(d)
        out, pooled = [], []
        for key in sorted(buckets):
            b = buckets[key]
            d = b.pop("_d")
            pooled.append(d)
            b.update(matched=len(d), rotowire_first=sum(1 for x in d if x > 0),
                     median_lead_seconds=round(statistics.median(d), 1) if d else None)
            if trailing:
                window = [x for group in pooled[-trailing:] for x in group]
                b.update(rolling_median_seconds=round(statistics.median(window), 1) if window else None,
                         rolling_matched=len(window))
            out.append(b)
        return out

    def day_of(dt):
        d = dt.astimezone(tz).date()
        return d.isoformat(), f"{d:%b} {d.day}"

    days = series(day_of)
    for row in days:
        end = datetime.fromisoformat(row["period"]).replace(tzinfo=tz) + timedelta(days=1)
        window = [d for dt, d in matched if end - timedelta(days=7) <= dt < end]
        row["rolling7_median_seconds"] = round(statistics.median(window), 1) if window else None
        row["rolling7_matched"] = len(window)
        window30 = [d for dt, d in matched if end - timedelta(days=30) <= dt < end]
        row["rolling30_median_seconds"] = round(statistics.median(window30), 1) if window30 else None
        row["rolling30_matched"] = len(window30)
        # Time to post from the original source tweet, per side (stories with a source).
        start = end - timedelta(days=1)
        day_src = [(r, u) for dt, r, u in sourced if start <= dt < end]
        row["sourced"] = len(day_src)
        row["rw_from_source"] = _med([r for r, _ in day_src])
        row["ud_from_source"] = _med([u for _, u in day_src])
        for n, label in ((7, "rolling7"), (30, "rolling30")):
            src = [(r, u) for dt, r, u in sourced if end - timedelta(days=n) <= dt < end]
            row[f"{label}_sourced"] = len(src)
            row[f"{label}_rw_from_source"] = _med([r for r, _ in src])
            row[f"{label}_ud_from_source"] = _med([u for _, u in src])
        # Same field the week and month series use, so the chart reads one key.
        row["rolling_median_seconds"] = row["rolling7_median_seconds"]
        row["rolling_matched"] = len(window)

    def month_of(dt):
        local = dt.astimezone(tz)
        return local.strftime("%Y-%m"), local.strftime("%b %Y")

    # Trailing windows: 4 weeks, 3 months (days use a calendar 7 days, above).
    return {"day": days, "week": series(week_of, trailing=4),
            "month": series(month_of, trailing=3)}


def _med(xs: list) -> float | None:
    return round(statistics.median(xs), 1) if xs else None


HEADLINE_DAYS = 30
TIE_SECONDS = 60


def _real_gap(s: dict, status: str) -> bool:
    """A real miss: one-sided, not a follow-up/roundup/skipped step, and not a borderline
    post (a highlight clip or in-game note the classifier only let in as borderline)."""
    side = s.get("underdog") if status == "underdog_only" else s.get("rotowire")
    return _is_gap(s, status) and not (side or {}).get("borderline")


def _headline(stories: list[dict], now: datetime, days: int = HEADLINE_DAYS,
              tie_s: int = TIE_SECONDS) -> dict:
    """The dashboard's top line over the trailing `days`: who posted first (a match within
    tie_s either way is a tie), and what share of real stories each side covered.

    stories = matched + real RotoWire-only + real Underdog-only. Rejected/merged stories,
    follow-ups, roundups, skipped steps and borderline posts are left out.
    """
    since = now - timedelta(days=days)
    sts = [s for s in stories if _is_active(s) and _story_time(s) >= since]
    matched = [s for s in sts if s.get("status") == "matched" and s.get("time_delta_seconds") is not None]
    d = [s["time_delta_seconds"] for s in matched]
    rw_first = sum(1 for x in d if x > tie_s)
    ud_first = sum(1 for x in d if x < -tie_s)
    ud_only = sum(1 for s in sts if _real_gap(s, "underdog_only"))
    rw_only = sum(1 for s in sts if _real_gap(s, "rotowire_only"))
    total = len(matched) + ud_only + rw_only
    pct = lambda n, of: round(n / of, 4) if of else None  # noqa: E731
    return {
        "days": days, "tie_seconds": tie_s, "since": since.date().isoformat(),
        "matched": len(matched), "rotowire_first": rw_first, "ties": len(matched) - rw_first - ud_first,
        "underdog_first": ud_first,
        "rotowire_first_rate": pct(rw_first, len(matched)), "tie_rate": pct(len(matched) - rw_first - ud_first, len(matched)),
        "underdog_first_rate": pct(ud_first, len(matched)),
        "median_lead_seconds": round(statistics.median(d), 1) if d else None,
        "stories": total, "matched_rate": pct(len(matched), total),
        "rotowire_missed": ud_only, "rotowire_missed_rate": pct(ud_only, total),
        "underdog_missed": rw_only, "underdog_missed_rate": pct(rw_only, total),
    }


MATURE_HOURS = 36   # impressions captured within this long of posting are comparable


def _mature_views(t: dict | None) -> int | None:
    """A post's impressions if they were frozen at a comparable age (12-36h after posting).
    Posts filled in by the archive backfill were measured weeks later and are left out."""
    if not t or not t.get("metrics_frozen_at"):
        return None
    age = (datetime.fromisoformat(t["metrics_frozen_at"]) - parse_dt(t["created_at"])).total_seconds()
    v = (t.get("public_metrics") or {}).get("impression_count")
    return v if age < MATURE_HOURS * 3600 else None


def _audience(stories: list[dict], tweets: list[dict], followers: list[dict], rw_handle: str,
              weeks_cfg: dict | None, tie_s: int = TIE_SECONDS) -> dict:
    """Followers, views per news post by week, and views on head-to-head stories."""
    tz = ZoneInfo((weeks_cfg or {}).get("timezone") or "America/New_York")
    side = lambda t: "rotowire" if t.get("account") == rw_handle else "underdog"  # noqa: E731

    # Weekly median views per news post (Monday weeks, ET).
    weeks: dict[str, dict[str, list[int]]] = {}
    for t in tweets:
        v = _mature_views(t) if t.get("is_news") else None
        if v is None:
            continue
        d = parse_dt(t["created_at"]).astimezone(tz).date()
        wk = (d - timedelta(days=d.weekday())).isoformat()
        weeks.setdefault(wk, {"rotowire": [], "underdog": []})[side(t)].append(v)
    views = []
    for wk in sorted(weeks):
        row = {"week": wk}
        for k, vs in weeks[wk].items():
            row[f"{k}_posts"] = len(vs)
            row[f"{k}_median_views"] = round(statistics.median(vs)) if vs else None
            row[f"{k}_total_views"] = sum(vs)
        views.append(row)

    # Head to head: the same story on both feeds, by who posted first.
    by_id = {t["id"]: t for t in tweets}
    buckets: dict[str, list[tuple[int, int]]] = {"rotowire_first": [], "tie": [], "underdog_first": [],
                                                  "underdog_first_10m": []}
    for s in stories:
        if not _is_active(s) or s.get("status") != "matched" or s.get("time_delta_seconds") is None:
            continue
        r = _mature_views(by_id.get(s["rotowire"]["tweet_id"]))
        u = _mature_views(by_id.get(s["underdog"]["tweet_id"]))
        if r is None or not u:
            continue
        d = s["time_delta_seconds"]
        key = "rotowire_first" if d > tie_s else "underdog_first" if d < -tie_s else "tie"
        buckets[key].append((r, u))
        if d < -600:
            buckets["underdog_first_10m"].append((r, u))
    h2h = {k: {"stories": len(v),
               "rotowire_median_views": round(statistics.median(x for x, _ in v)) if v else None,
               "underdog_median_views": round(statistics.median(y for _, y in v)) if v else None,
               "rotowire_share": round(statistics.median(x / y for x, y in v), 4) if v else None}
           for k, v in buckets.items()}

    # Followers: daily snapshots per account (history starts when collection did).
    series: dict[str, list[dict]] = {}
    for f in followers:
        series.setdefault(f["account"], []).append({"date": f["date"], "followers": f["followers_count"]})
    growth = {}
    for acct, pts in series.items():
        pts.sort(key=lambda p: p["date"])
        first, last = pts[0], pts[-1]
        days = (datetime.fromisoformat(last["date"]) - datetime.fromisoformat(first["date"])).days
        gained = last["followers"] - first["followers"]
        growth[acct] = {"first_date": first["date"], "last_date": last["date"], "days": days,
                        "followers": last["followers"], "gained": gained,
                        "gained_rate": round(gained / first["followers"], 5) if first["followers"] else None,
                        "per_day": round(gained / days, 1) if days else None}
    return {"followers": series, "growth": growth, "views_by_week": views, "head_to_head": h2h,
            "mature_hours": MATURE_HOURS}


def _eras(stories: list[dict], eras: list[dict], rotowire_handle: str | None,
          tweets: list[dict]) -> list[dict]:
    """One summary row per era ({label, from?, until?}, US Eastern dates, until exclusive),
    with weeks covered and news posts per week so a quiet era reads as quiet."""
    tz = ZoneInfo("America/New_York")
    day = lambda d: datetime.fromisoformat(str(d)).replace(tzinfo=tz) if d else None  # noqa: E731
    out = []
    for era in eras:
        lo, hi = day(era.get("from")), day(era.get("until"))
        inside = lambda t: (lo is None or t >= lo) and (hi is None or t < hi)  # noqa: E731
        sts = [s for s in stories if inside(_story_time(s))]
        posts = [t for t in tweets if t.get("is_news") and inside(parse_dt(t["created_at"]))]
        times = [_story_time(s) for s in sts]
        first = min(times) if times else None
        last = max(times) if times else None
        weeks = max(1.0, ((last - first).total_seconds() / 86400 + 1) / 7) if times else 0
        row = {"label": era["label"], "from": str(era.get("from") or "") or None,
               "until": str(era.get("until") or "") or None,
               "first": first.astimezone(tz).date().isoformat() if first else None,
               "last": last.astimezone(tz).date().isoformat() if last else None,
               "weeks": round(weeks, 1)}
        row.update(_summary(sts))
        rw = sum(1 for t in posts if t.get("account") == rotowire_handle)
        row["rotowire_posts_per_week"] = round(rw / weeks, 1) if weeks else None
        row["underdog_posts_per_week"] = round((len(posts) - rw) / weeks, 1) if weeks else None
        row["matched_per_week"] = round(row["matched"] / weeks, 1) if weeks else None
        src = [s for s in sts if s.get("status") == "matched" and s.get("underdog_secs_from_source") is not None]
        row["sourced"] = len(src)
        row["rotowire_from_source_seconds"] = _med([s["rotowire_secs_from_source"] for s in src])
        row["underdog_from_source_seconds"] = _med([s["underdog_secs_from_source"] for s in src])
        out.append(row)
    return out


def build_aggregates(
    data_dir: Path = DATA_DIR,
    docs_data_dir: Path = DOCS_DATA_DIR,
    stories_path: Path | None = None,
    reviews_path: Path | None = None,
    season_weeks: dict | None = None,
    rotowire_handle: str | None = None,
    milestones: list[dict] | None = None,
    game_windows: dict | None = None,
    analysis_start: str | None = None,
    eras: list[dict] | None = None,
    practice_phases: bool = False,
) -> dict:
    """analysis_start (sports.<sport>.analysis_start, a US Eastern date): every metric counts
    only stories and posts from that day on. Earlier stories stay in stories.json for the
    feed, flagged before_start."""
    stories_path = stories_path or (data_dir / "stories.jsonl")
    reviews_path = reviews_path or (data_dir / "reviews.jsonl")

    all_stories = apply_reviews(load_jsonl(stories_path), load_jsonl(reviews_path))
    # Source tweet times (cached by src/source.py; never calls the X API here).
    source.attach(all_stories, data_dir)
    start = None
    if analysis_start:
        start = datetime.fromisoformat(str(analysis_start)).replace(tzinfo=ZoneInfo("America/New_York"))
        for st in all_stories:
            st["before_start"] = _story_time(st) < start
    stories = [st for st in all_stories if not st.get("before_start")]

    aggregates = {
        "generated_at": now_iso(),
        "analysis_start": str(analysis_start) if analysis_start else None,
        "summary": _summary(stories),
        "headline": _headline(stories, datetime.now(ZoneInfo("UTC"))),
        "weekly": _rollup(stories, _iso_week),
        "monthly": _rollup(stories, _month),
        "trend": _trend(stories, season_weeks),
        # Workflow releases to mark on the trend chart ({date: YYYY-MM-DD, label}).
        "milestones": [{"date": str(m["date"]), "label": m["label"]} for m in (milestones or [])],
        # Daily follower counts per account (collect._snapshot_followers), oldest first.
        "followers": [{k: r.get(k) for k in ("date", "account", "followers_count")}
                      for r in load_jsonl(data_dir / "followers.jsonl")],
    }
    if season_weeks:
        aggregates["season_weeks"] = _season_rollup(stories, season_weeks)
        aggregates["news_types"] = _news_type_rollup(stories, season_weeks)
        # Tag each story so the dashboard can list examples per week / news type.
        of = season_week_of(season_weeks)
        for st in all_stories:
            st["season_week"] = of(_story_time(st))[1]
            if st.get("status") == "matched":
                st["news_type"] = news_type(st)
        if rotowire_handle:
            tweets = [t for t in load_jsonl(data_dir / "tweets.jsonl")
                      if start is None or parse_dt(t["created_at"]) >= start]
            aggregates["hype"] = _hype_rollup(tweets, season_weeks, rotowire_handle)
            aggregates["hours"] = _hour_rollup(stories, tweets, season_weeks, rotowire_handle)

    # Pre- vs post-practice updates (cached model labels; never calls the model here).
    if practice_phases and rotowire_handle:
        news = [t for t in load_jsonl(data_dir / "tweets.jsonl") if t.get("is_news")]
        phases = practice.label_phases(data_dir, llm=False)
        aggregates["practice"] = practice.rollup(
            data_dir, rotowire_handle, phases, datetime.now(ZoneInfo("UTC")),
            alias=develop.spelling_aliases(news, rotowire_handle))

    # Focus areas: which kinds of news cost RotoWire the most (NFL wording rules).
    if rotowire_handle and season_weeks:
        tw_by_id = {t["id"]: t for t in load_jsonl(data_dir / "tweets.jsonl")}
        aggregates["focus"] = focus.rollup(stories, _story_time, datetime.now(ZoneInfo("UTC")),
                                           _mature_views, tw_by_id)

    # Audience: followers, views per post, views on head-to-head stories.
    if rotowire_handle:
        followers = load_jsonl(data_dir / "followers.jsonl")
        aggregates["audience"] = _audience(
            [s for s in stories if not season_weeks or _story_time(s) >= datetime.fromisoformat(
                str(season_weeks["start"])).replace(tzinfo=ZoneInfo("America/New_York")) - timedelta(days=42)],
            load_jsonl(data_dir / "tweets.jsonl"), followers, rotowire_handle, season_weeks)

    # Before / after comparison (sports.<sport>.eras).
    if eras:
        aggregates["eras"] = _eras(stories, eras, rotowire_handle,
                                   load_jsonl(data_dir / "tweets.jsonl") if rotowire_handle else [])

    # How RotoWire compares while NFL games are on (sports with game_windows configured).
    if game_windows:
        games_list = games.load_games(data_dir)
        if games_list:
            since = None
            if season_weeks:
                tz = ZoneInfo(season_weeks.get("timezone") or "America/New_York")
                since = datetime.fromisoformat(str(season_weeks["start"])).replace(tzinfo=tz)
            aggregates["game_windows"] = games.rollup(
                stories, games_list, game_windows, _story_time, _summary,
                since=since, until=datetime.now(ZoneInfo("UTC")))

    # Speed and coverage by who was on the desk, for days with a schedule file.
    schedules = staffing.load_schedules(data_dir)
    if schedules:
        aggregates["staffing"] = staffing.rollup(
            stories, schedules, _story_time, _summary,
            tweets=load_jsonl(data_dir / "tweets.jsonl"), rotowire_handle=rotowire_handle)

    docs_data_dir.mkdir(parents=True, exist_ok=True)
    _write_json(docs_data_dir / "stories.json", all_stories)
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
