"""Phase 1 collector: pull posts from both accounts, dedupe, backfill, and
manage the 12h impression freeze.

Per run, for each account:
  1. Read since_id from state (null on first run -> backfill from config date).
  2. Page the user timeline, appending new posts (dedupe by tweet ID).
  3. Advance since_id to the newest ID seen.
Then a metrics pass:
  4. Re-fetch public_metrics for every non-frozen post (so both accounts are
     compared at equal maturity), then freeze any post >= freeze_hours old.
  5. For the account that owns the X app's access token (@RotoWireNFL), store its own
     posts' non-public metrics (impressions, engagements, link and profile clicks) on the
     same schedule; X only serves these for posts under 30 days old.

Run: python -m src.collect
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DATA_DIR, load_config, sport_accounts
from .store import load_jsonl, load_state, now_iso, parse_dt, save_state, write_jsonl
from .xapi import OwnerClient, XClient

OWNED_MAX_AGE = timedelta(days=29)   # X serves non-public metrics for posts under 30 days old


def _backfill_start_time(cfg: dict, sport: str | None = None) -> str:
    """First-run backfill start. A sport may override the global collection date."""
    meta = (cfg.get("sports") or {}).get(sport or "", {}) or {}
    date = meta.get("backfill_start_date") or cfg["collection"]["backfill_start_date"]
    return f"{date}T00:00:00Z"


def _new_record(tweet: dict, account_handle: str) -> dict:
    """Raw post record. Enrichment fields are filled later by the tagger (Phase 2)."""
    return {
        "id": tweet["id"],
        "account": account_handle,
        "created_at": tweet["created_at"],
        "text": tweet.get("text", ""),
        "lang": tweet.get("lang"),
        "public_metrics": tweet.get("public_metrics", {}),
        # [{type: quoted|replied_to|retweeted, id}] — marks Underdog's quote-tweet updates.
        "referenced_tweets": tweet.get("referenced_tweets") or [],
        "metrics_frozen": False,
        "metrics_frozen_at": None,
        "first_seen_at": now_iso(),
        "players": [],
        "event_class": None,
        "is_news": None,
        "excluded_reason": None,
    }


def collect(
    client: XClient | None = None,
    data_dir: Path = DATA_DIR,
    per_account_limit: int | None = None,
    sport: str = "mlb",
    accounts: dict | None = None,
    owner: OwnerClient | None = None,
) -> dict:
    cfg = load_config()
    client = client or XClient()
    state = load_state(data_dir)
    state.setdefault("accounts", {})

    accounts = accounts if accounts is not None else sport_accounts(cfg, sport)
    exclude = cfg["collection"].get("exclude")
    freeze_hours = cfg["impressions"]["freeze_hours"]
    tweets_path = data_dir / "tweets.jsonl"

    by_id = {r["id"]: r for r in load_jsonl(tweets_path)}
    fetched_this_run: set[str] = set()

    for key, account in accounts.items():
        handle = account["handle"]
        user_id = account.get("user_id")
        if not user_id:
            raise RuntimeError(f"{handle} has no user_id — run `python -m src.resolve_ids` first")

        acct_state = state["accounts"].setdefault(key, {"since_id": None, "last_run": None})
        since_id = acct_state.get("since_id")
        start_time = None if since_id else _backfill_start_time(cfg, sport)

        newest_id = since_id
        new_count = 0
        seen = 0
        page_size = per_account_limit if per_account_limit else 100
        max_pages = 1 if per_account_limit else None
        for tweet in client.user_tweets(
            user_id,
            since_id=since_id,
            start_time=start_time,
            exclude=exclude,
            page_size=page_size,
            max_pages=max_pages,
        ):
            if per_account_limit and seen >= per_account_limit:
                break
            seen += 1
            tid = tweet["id"]
            fetched_this_run.add(tid)
            if tid not in by_id:
                by_id[tid] = _new_record(tweet, handle)
                new_count += 1
            elif not by_id[tid]["metrics_frozen"]:
                by_id[tid]["public_metrics"] = tweet.get(
                    "public_metrics", by_id[tid]["public_metrics"]
                )
            if newest_id is None or int(tid) > int(newest_id):
                newest_id = tid

        acct_state["since_id"] = newest_id
        acct_state["last_run"] = now_iso()
        print(f"{handle}: +{new_count} new posts (since_id -> {newest_id})")

    archive_start = (cfg.get("collection") or {}).get("archive_start")
    if archive_start:
        _archive_backfill(client, accounts, state, by_id, fetched_this_run, f"{archive_start}T00:00:00Z")

    _refresh_and_freeze(client, by_id, fetched_this_run, freeze_hours)
    if owner:
        _owned_metrics(owner, accounts, by_id)
    _backfill_references(client, by_id, _reference_backfill_since(cfg, sport))
    _snapshot_followers(client, accounts, data_dir)

    records = sorted(by_id.values(), key=lambda r: (r["created_at"], r["id"]))
    write_jsonl(tweets_path, records)
    state["last_run"] = now_iso()
    save_state(state, data_dir)
    print(f"Total stored posts: {len(records)}")
    return state


def _archive_backfill(client, accounts: dict, state: dict, by_id: dict[str, dict],
                      fetched_this_run: set[str], start: str) -> None:
    """One-time fill of each account's posts from `start` up to its oldest stored post.

    The user-timeline endpoint only reaches an account's latest ~3,200 posts, so older
    history comes from full-archive search (retweets and replies excluded, as in normal
    collection). Each account records the start it was filled from in state, so this runs
    once per account and again only if collection.archive_start moves earlier.
    """
    for key, account in accounts.items():
        handle = account["handle"]
        acct_state = state["accounts"].setdefault(key, {"since_id": None, "last_run": None})
        done = acct_state.get("archive_start")
        if done and done <= start:
            continue
        own = [r["created_at"] for r in by_id.values() if r["account"] == handle]
        end = min(own) if own else None
        if not end or end <= start:
            acct_state["archive_start"] = start
            continue
        added = 0
        try:
            for tweet in client.search_all(f"from:{handle} -is:retweet -is:reply", start, end):
                tid = tweet["id"]
                fetched_this_run.add(tid)
                if tid not in by_id:
                    by_id[tid] = _new_record(tweet, handle)
                    added += 1
        except Exception as e:  # leave state unset so the next run retries
            print(f"{handle}: archive backfill failed ({e}); will retry next run")
            continue
        acct_state["archive_start"] = start
        print(f"{handle}: archive backfill +{added} posts ({start[:10]} to {end[:10]})")


def _snapshot_followers(client: XClient, accounts: dict, data_dir: Path) -> None:
    """Record each account's follower count once per day (US Eastern date).

    data/<sport>/followers.jsonl gets one row per account per date; a second run on the
    same date replaces that date's row, so the file stays one-per-day. The X API only
    reports the current count, so history starts the day this first ran.
    """
    from zoneinfo import ZoneInfo

    ids = {a["user_id"]: (key, a["handle"]) for key, a in accounts.items() if a.get("user_id")}
    if not ids:
        return
    try:
        metrics = client.users_metrics(list(ids))
    except Exception as e:  # never fail the pipeline over a follower count
        print(f"Follower snapshot skipped: {e}")
        return
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    path = data_dir / "followers.jsonl"
    rows = [r for r in load_jsonl(path)
            if not (r.get("date") == today and r.get("user_id") in metrics)]
    for uid, m in metrics.items():
        key, handle = ids[uid]
        rows.append({"date": today, "account": key, "handle": handle, "user_id": uid,
                     "followers_count": m.get("followers_count"),
                     "following_count": m.get("following_count"),
                     "tweet_count": m.get("tweet_count"),
                     "listed_count": m.get("listed_count"),
                     "captured_at": now_iso()})
    rows.sort(key=lambda r: (r["date"], r["account"]))
    write_jsonl(path, rows)
    print("Followers: " + ", ".join(f"{ids[u][1]} {m.get('followers_count')}" for u, m in metrics.items()))


def _owned_metrics(owner: OwnerClient, accounts: dict, by_id: dict[str, dict]) -> None:
    """Non-public metrics for the token owner's own posts, refreshed until the post's public
    metrics freeze (one last fetch at freeze time) so both are read at the same age. Posts
    already frozen when this first runs get one fetch, flagged by owned_metrics_at."""
    handles = {a["handle"] for a in accounts.values() if str(a.get("user_id")) == owner.user_id}
    if not handles:
        return
    now = datetime.now(timezone.utc)
    ids = [r["id"] for r in by_id.values()
           if r["account"] in handles and not r.get("owned_metrics_frozen")
           and now - parse_dt(r["created_at"]) < OWNED_MAX_AGE]
    if not ids:
        return
    try:
        found = owner.owned_metrics(ids)
    except Exception as e:  # never fail the pipeline over the extra metrics
        print(f"Owned metrics skipped: {e}")
        return
    for tid, m in found.items():
        r = by_id[tid]
        r["non_public_metrics"] = m["non_public_metrics"]
        r["organic_metrics"] = m["organic_metrics"]
        r["owned_metrics_at"] = now_iso()
        r["owned_metrics_frozen"] = r["metrics_frozen"]
    print(f"Owned metrics for {len(found)}/{len(ids)} {'/'.join(sorted(handles))} posts")


def _reference_backfill_since(cfg: dict, sport: str | None) -> str | None:
    meta = (cfg.get("sports") or {}).get(sport or "", {}) or {}
    date = meta.get("reference_backfill_since")
    return f"{date}T00:00:00Z" if date else None


def _backfill_references(client: XClient, by_id: dict[str, dict], since: str | None) -> None:
    """One-time fill of referenced_tweets for posts stored before collection kept it.

    Limited to posts created on/after the sport's `reference_backfill_since` date so the
    X API read cost stays bounded. Posts get an empty list once looked up (including
    deleted ones) so each post is fetched at most once.
    """
    if not since:
        return
    since_dt = parse_dt(since)
    ids = [r["id"] for r in by_id.values()
           if "referenced_tweets" not in r and parse_dt(r["created_at"]) >= since_dt]
    if not ids:
        return
    found = client.tweets_lookup(ids, fields="referenced_tweets")
    for tid in ids:
        by_id[tid]["referenced_tweets"] = (found.get(tid) or {}).get("referenced_tweets") or []
    print(f"Backfilled referenced_tweets for {len(ids)} posts ({len(found)} found)")


def _refresh_and_freeze(
    client: XClient, by_id: dict[str, dict], fetched_this_run: set[str], freeze_hours: int
) -> None:
    now = datetime.now(timezone.utc)
    cutoff = timedelta(hours=freeze_hours)

    # Refresh metrics for non-frozen posts not already fetched fresh this run.
    refresh_ids = [
        r["id"]
        for r in by_id.values()
        if not r["metrics_frozen"] and r["id"] not in fetched_this_run
    ]
    if refresh_ids:
        metrics = client.tweets_metrics(refresh_ids)
        for tid, m in metrics.items():
            if m:
                by_id[tid]["public_metrics"] = m
        print(f"Refreshed metrics for {len(metrics)}/{len(refresh_ids)} non-frozen posts")

    # Freeze posts that have reached maturity.
    frozen = 0
    for r in by_id.values():
        if r["metrics_frozen"]:
            continue
        if now - parse_dt(r["created_at"]) >= cutoff:
            r["metrics_frozen"] = True
            r["metrics_frozen_at"] = now_iso()
            frozen += 1
    if frozen:
        print(f"Froze metrics for {frozen} posts (>= {freeze_hours}h old)")


if __name__ == "__main__":
    import argparse

    from .config import sport_data_dir

    parser = argparse.ArgumentParser(description="Collect posts from both accounts.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap reads to N most-recent posts per account (for cheap test runs).",
    )
    parser.add_argument("--sport", default="mlb", help="Sport key from config (default: mlb).")
    args = parser.parse_args()
    collect(per_account_limit=args.limit, sport=args.sport, data_dir=sport_data_dir(args.sport),
            owner=OwnerClient.from_env())
