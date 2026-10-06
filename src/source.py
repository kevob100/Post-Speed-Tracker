"""Source times: when the original report went out, so each side's speed is absolute.

The margin (RotoWire minus Underdog) can't say which side moved: a wider gap is Underdog
getting faster or RotoWire getting slower. For a matched story whose posts credit a
reporter or team account ("via @AdamSchefter"), the source tweet is a fixed start that
neither side controls, so each side gets a time to post: source tweet -> its own post.

How the source is found, per matched story (first that works):
  1. quoted: Underdog quote-tweeted the source. One batched lookup gives its time.
  2. search: full-archive search over the cited handles for a tweet naming the player in
     the `window_hours` before the earlier of the two posts. The latest such tweet wins
     (the one closest to the posts, so a reporter's earlier, unrelated note on the same
     player isn't taken as the source).
A source must be no later than the earlier post. Results, including "none found", are
cached in data/<sport>/sources.jsonl keyed by the story's post ids, so each story costs
X API reads once. `max_searches_per_run` bounds a backfill; the rest carry to the next run.

Run: python -m src.source --sport nfl
"""
from __future__ import annotations

import re
import time
from datetime import timedelta
from pathlib import Path

from .store import load_jsonl, now_iso, parse_dt, write_jsonl

HANDLE_RE = re.compile(r"@(\w{1,15})")
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
# Full-archive search allows about one request every 3 seconds, so searches are bounded by
# count and by wall time, and progress is saved as it goes: a run that is cut off keeps what it
# found and the next run carries on from there.
DEFAULTS = {"window_hours": 6, "max_searches_per_run": 300, "max_minutes": 20, "save_every": 25}


def cited_handles(text: str, own: set[str]) -> list[str]:
    """Handles credited in a post, minus the RotoWire / Underdog accounts themselves."""
    out = []
    for h in HANDLE_RE.findall(text or ""):
        low = h.lower()
        if low in own or low.startswith(("rotowire", "underdog")) or low in (x.lower() for x in out):
            continue
        out.append(h)
    return out


def last_name(player: str | None) -> str | None:
    """The search keyword for a player: last name, skipping Jr./III-style suffixes."""
    parts = [p for p in re.split(r"\s+", (player or "").strip()) if p]
    while len(parts) > 1 and parts[-1].lower().strip(".") in SUFFIXES:
        parts.pop()
    if not parts:
        return None
    name = re.sub(r"[^\w'\-]", "", parts[-1])
    return name if len(name) >= 3 else None


def _key(story: dict) -> str:
    return f"{story['rotowire']['tweet_id']}_{story['underdog']['tweet_id']}"


def _quoted_id(post: dict | None) -> str | None:
    for ref in (post or {}).get("referenced_tweets") or []:
        if ref.get("type") == "quoted":
            return ref.get("id")
    return None


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def find_sources(client, data_dir: Path, own_handles: list[str], cfg: dict | None = None) -> dict:
    """Fill data/<sport>/sources.jsonl for matched stories not looked up yet."""
    cfg = {**DEFAULTS, **(cfg or {})}
    own = {h.lower() for h in own_handles}
    path = data_dir / "sources.jsonl"
    cache = {r["key"]: r for r in load_jsonl(path)}
    tweets = {t["id"]: t for t in load_jsonl(data_dir / "tweets.jsonl")}
    stories = [s for s in load_jsonl(data_dir / "stories.jsonl")
               if s.get("status") == "matched" and s.get("rotowire") and s.get("underdog")]
    todo = [s for s in stories if _key(s) not in cache]
    if not todo:
        return {"stories": len(stories), "new": 0, "found": 0, "searches": 0, "pending": 0}

    def row(s, **kw):
        return {"key": _key(s), "story_id": s["story_id"], "looked_up_at": now_iso(),
                "source_id": None, "source_handle": None, "source_at": None, "method": "none", **kw}

    # 1. Underdog quote-tweeted the source: one batched lookup for all of them.
    quoted = {}
    for s in todo:
        qid = _quoted_id(tweets.get(s["underdog"]["tweet_id"]))
        if qid:
            quoted[_key(s)] = qid
    looked = client.tweets_lookup(sorted(set(quoted.values())), fields="created_at,author_id") if quoted else {}
    authors = sorted({t["author_id"] for t in looked.values() if t.get("author_id")})
    names = client.users_by_ids(authors) if authors else {}

    new, found, searches, pending = 0, 0, 0, 0
    deadline = time.monotonic() + cfg["max_minutes"] * 60
    save = lambda: write_jsonl(path, sorted(cache.values(), key=lambda r: r["story_id"]))  # noqa: E731
    for s in todo:
        first = min(parse_dt(s["rotowire"]["created_at"]), parse_dt(s["underdog"]["created_at"]))
        q = looked.get(quoted.get(_key(s)) or "")
        if q:
            handle = names.get(q.get("author_id"), "")
            if handle.lower() not in own and not handle.lower().startswith(("rotowire", "underdog")) \
                    and parse_dt(q["created_at"]) <= first:
                cache[_key(s)] = row(s, source_id=q["id"], source_handle=handle,
                                     source_at=q["created_at"], method="quoted")
                new += 1; found += 1
                continue

        handles = cited_handles(s["underdog"]["text"], own)
        handles += [h for h in cited_handles(s["rotowire"]["text"], own)
                    if h.lower() not in (x.lower() for x in handles)]
        name = last_name(s.get("player"))
        if not handles or not name:
            cache[_key(s)] = row(s)
            new += 1
            continue
        if searches >= cfg["max_searches_per_run"] or time.monotonic() > deadline:
            pending += 1
            continue
        if searches and searches % cfg["save_every"] == 0:
            save()
        query = f"({' OR '.join(f'from:{h}' for h in handles[:8])}) \"{name}\" -is:retweet"
        start = first - timedelta(hours=cfg["window_hours"])
        searches += 1
        try:
            hits = list(client.search_all(query, _iso(start), _iso(first + timedelta(seconds=1)),
                                          page_size=10))
        except Exception as e:  # leave uncached so the next run retries
            print(f"[source] search failed for {s['story_id']}: {e}")
            continue
        hits = [h for h in hits if parse_dt(h["created_at"]) <= first]
        if hits:
            best = max(hits, key=lambda h: parse_dt(h["created_at"]))
            cache[_key(s)] = row(s, source_id=best["id"], source_author_id=best.get("author_id"),
                                 source_at=best["created_at"], method="search")
            found += 1
        else:
            cache[_key(s)] = row(s)
        new += 1

    # Search results carry author ids, not handles: name them in one batched lookup.
    missing = sorted({r["source_author_id"] for r in cache.values()
                      if r.get("source_author_id") and not r.get("source_handle")} - set(names))
    if missing:
        names.update(client.users_by_ids(missing))
    for r in cache.values():
        if r.get("source_author_id") and not r.get("source_handle"):
            r["source_handle"] = names.get(r["source_author_id"])

    save()
    return {"stories": len(stories), "new": new, "found": found, "searches": searches,
            "pending": pending}


def attach(stories: list[dict], data_dir: Path) -> int:
    """Copy cached source times onto stories as rotowire_secs_from_source /
    underdog_secs_from_source (whole seconds). Returns how many stories got one."""
    cache = {r["key"]: r for r in load_jsonl(data_dir / "sources.jsonl") if r.get("source_at")}
    if not cache:
        return 0
    n = 0
    for s in stories:
        if s.get("status") != "matched" or not (s.get("rotowire") or {}).get("tweet_id") \
                or not (s.get("underdog") or {}).get("tweet_id"):
            continue
        r = cache.get(_key(s))
        if not r:
            continue
        src = parse_dt(r["source_at"])
        s["source"] = {"tweet_id": r["source_id"], "handle": r["source_handle"],
                       "created_at": r["source_at"], "method": r["method"]}
        s["rotowire_secs_from_source"] = round((parse_dt(s["rotowire"]["created_at"]) - src).total_seconds())
        s["underdog_secs_from_source"] = round((parse_dt(s["underdog"]["created_at"]) - src).total_seconds())
        n += 1
    return n


if __name__ == "__main__":
    import argparse

    from .config import load_config, sport_accounts, sport_data_dir
    from .xapi import XClient

    ap = argparse.ArgumentParser(description="Find source tweets for matched stories.")
    ap.add_argument("--sport", default="nfl")
    args = ap.parse_args()
    conf = load_config()
    accts = sport_accounts(conf, args.sport)
    print(find_sources(XClient(), sport_data_dir(args.sport), [a["handle"] for a in accts.values()],
                       conf["sports"][args.sport].get("source_lookup")))
