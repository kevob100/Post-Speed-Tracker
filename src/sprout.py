"""RotoWire account history from Sprout Social (data/<sport>/sprout_daily.jsonl).

Sprout keeps one row per day for every X profile connected to the Gambling.com Group
account: the real follower count that day plus account-wide impressions, engagements and
clicks. That gives every sport the follower history and account totals that NFL otherwise
gets only from a hand-exported X Analytics CSV.

Each run re-reads the last `refresh_days` (Sprout revises recent days) and anything missing
back to `since`. Sprout answers at most one year per request, so longer ranges are chunked.
Rows are stored on the date Sprout reports them; today is skipped because it is partial.

Run: python -m src.sprout [--sport nfl]
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .config import DEFAULT_TZ
from .store import load_jsonl, load_state, save_state, write_jsonl

BASE = "https://api.sproutsocial.com/v1"

# Sprout metric -> stored field.
METRICS = {
    "lifetime_snapshot.followers_count": "followers",
    "net_follower_growth": "net_follows",
    "impressions": "impressions",
    "engagements": "engagements",
    "likes": "likes",
    "comments_count": "replies",
    "shares_count": "reposts",
    "post_link_clicks": "link_clicks",
    "post_content_clicks": "content_clicks",
    "post_media_views": "media_views",
    "video_views": "video_views",
    "posts_sent_count": "posts",
}


def has_credentials() -> bool:
    return bool(os.getenv("SPROUT_SOCIAL_TOKEN") and os.getenv("SPROUT_CUSTOMER_ID"))


def fetch(profile_id: int | str, start: date, end: date, session: requests.Session | None = None) -> list[dict]:
    """Daily rows for one profile, start..end inclusive, oldest first."""
    session = session or requests.Session()
    headers = {"Authorization": f"Bearer {os.environ['SPROUT_SOCIAL_TOKEN']}"}
    url = f"{BASE}/{os.environ['SPROUT_CUSTOMER_ID']}/analytics/profiles"
    out: list[dict] = []
    a = start
    while a <= end:
        b = min(end, a + timedelta(days=364))
        page = 1
        while True:
            resp = session.post(url, headers=headers, timeout=60, json={
                "filters": [f"customer_profile_id.eq({profile_id})", f"reporting_period.in({a}...{b})"],
                "metrics": list(METRICS), "page": page})
            if resp.status_code != 200:
                raise RuntimeError(f"Sprout {resp.status_code}: {resp.text[:300]}")
            body = resp.json()
            for row in body.get("data", []):
                m = row.get("metrics") or {}
                out.append({"date": row["dimensions"]["reporting_period.by(day)"],
                            **{field: m.get(metric) for metric, field in METRICS.items()}})
            if page >= (body.get("paging") or {}).get("total_pages", 1):
                break
            page += 1
        a = b + timedelta(days=1)
    return sorted(out, key=lambda r: r["date"])


def sync(data_dir: Path, profile_id: int | str, since: str, refresh_days: int = 7,
         session: requests.Session | None = None, today: date | None = None) -> str:
    """Bring data/<sport>/sprout_daily.jsonl up to yesterday (US Central)."""
    path = data_dir / "sprout_daily.jsonl"
    rows = {r["date"]: r for r in load_jsonl(path)}
    today = today or datetime.now(ZoneInfo(DEFAULT_TZ)).date()
    end = today - timedelta(days=1)
    first = date.fromisoformat(str(since))
    # Fill from `since` once (state remembers it, so a profile Sprout only started tracking
    # later is not re-read every run), and again only if `since` moves earlier.
    state = load_state(data_dir)
    filled = state.get("sprout_filled_from")
    start = first if not filled or filled > first.isoformat() else max(first, end - timedelta(days=refresh_days - 1))
    if start > end:
        return "up to date"
    fresh = fetch(profile_id, start, end, session=session)
    for r in fresh:
        # Days before Sprout started tracking the profile come back with every metric empty.
        if any(r.get(f) for f in METRICS.values()):
            rows[r["date"]] = r
    write_jsonl(path, [rows[d] for d in sorted(rows)])
    state["sprout_filled_from"] = first.isoformat()
    save_state(state, data_dir)
    return f"{len(fresh)} days read ({start} to {end}), {len(rows)} stored"


def daily_metrics(rows: list[dict]) -> dict[str, dict]:
    """Rows in x_analytics.load_daily's shape ({date: {impressions, ...}}) for the weekly
    account totals. Follower level is left out (weekly sums would add it up); net follows
    become new_follows so the shape matches the export's."""
    out = {}
    for r in rows:
        if r.get("impressions") is None:
            continue
        out[r["date"]] = {"new_follows": r.get("net_follows") or 0, "unfollows": 0,
                          **{k: r[k] for k in ("impressions", "engagements", "likes", "replies", "reposts",
                                               "link_clicks", "content_clicks", "media_views", "video_views", "posts")
                             if r.get(k) is not None}}
    return out


def follower_counts(rows: list[dict]) -> list[dict]:
    """[{date, followers, source: sprout}] for every day with a real count, oldest first."""
    return [{"date": r["date"], "followers": r["followers"], "source": "sprout"}
            for r in rows if r.get("followers")]


if __name__ == "__main__":
    import argparse

    from .config import load_config, sport_data_dir, sports

    parser = argparse.ArgumentParser(description="Pull RotoWire account history from Sprout Social.")
    parser.add_argument("--sport", default=None, help="Sport key (default: every active sport).")
    args = parser.parse_args()
    cfg = load_config()
    sp = cfg.get("sprout") or {}
    for key in [args.sport] if args.sport else sports(cfg):
        pid = cfg["sports"][key]["accounts"]["rotowire"].get("sprout_profile_id")
        if pid:
            print(key, sync(sport_data_dir(key), pid, sp.get("since", "2025-10-01"), sp.get("refresh_days", 7)))
