"""Development-based matching (sport config `match_mode: developments`).

The pairwise matcher in match.py pairs the two posts CLOSEST in time. That breaks when a
story has several posts: Underdog quote-tweets its first post with each update while
RotoWire posts separate tweets, so the closest pair is often an update on one side and
the first report on the other, and the side that actually broke the news loses credit.

This module scores each news DEVELOPMENT once instead:

  1. Mentions: every news post becomes one mention per player it names (a post naming
     two players is scored for both).
  2. Sessions: per player, mentions are chained into a session while consecutive posts
     are within matching.time_window_minutes of each other.
  3. Grouping: a session with posts from BOTH accounts is sent to the model, which splits
     it into distinct developments ("got hurt", "questionable to return", "ruled out").
     A one-account session needs no model: its posts are grouped by event_class.
     Groupings are cached in data/<sport>/developments.jsonl, keyed by the exact set of
     posts in the session, so a session is re-grouped only when a new post joins it.
  4. Stories: per development, the earliest post on each side is that side's report.
     Both sides within the time window -> one `matched` story timed on those two posts.
     Otherwise each side's report is a one-sided coverage gap. Every later post on the
     development is a follow-up, written as a one-sided story with
     same_event_duplicate=true and duplicate_of pointing at the development's story, so
     the dashboard's existing duplicates tab lists them.

Output is data/<sport>/stories.jsonl with the same schema the pairwise matcher writes.
time_delta_seconds = underdog.created_at - rotowire.created_at (positive => RotoWire first).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from zoneinfo import ZoneInfo

from .anthropic_client import GROUP_PROMPT_VERSION
from .config import DATA_DIR, load_config, sport_accounts
from .store import append_jsonl, load_jsonl, now_iso, parse_dt, write_jsonl

ET = ZoneInfo("America/New_York")

# A coverage gap is "late" (not "missed") when the other account posts about the same
# player within this long after the lone post.
LATE_WINDOW_S = 24 * 3600


def is_roundup(players: list[dict]) -> bool:
    """A digest post listing many players ("Fantasy Football News Updates", "everyone who
    left a game hurt"): 4+ players, or 3 from 3 different teams. A single-team list such
    as "Jets ruled out: Hall, Mitchell, Taylor" stays ordinary news. A roundup still
    matches the other feed's report on each player; when it does not, it is tagged
    gap_kind "roundup" instead of counting as a coverage gap.
    """
    teams = {p.get("team") for p in players if p.get("team")}
    return len(players) >= 4 or (len(players) == 3 and len(teams) >= 3)


def mentions(news: list[dict]) -> list[dict]:
    """One (post, player) mention per player a news post names."""
    out: list[dict] = []
    for post in news:
        players = [p for p in (post.get("players") or []) if p.get("player_key")]
        if not players and post.get("player_key"):
            players = [{"name": post.get("player"), "team": post.get("team"),
                        "player_key": post["player_key"]}]
        multi = len(players) > 1
        roundup = is_roundup(players)
        for p in players:
            out.append({"post": post, "player": p.get("name"), "team": p.get("team"),
                        "player_key": p["player_key"], "multi_player": multi,
                        "roundup": roundup})
    return out


def sessions(ms: list[dict], window_s: int) -> list[list[dict]]:
    """Chain each player's mentions while consecutive posts are <= window_s apart."""
    by_player: dict[str, list[dict]] = {}
    for m in ms:
        by_player.setdefault(m["player_key"], []).append(m)
    out: list[list[dict]] = []
    for key in sorted(by_player):
        items = sorted(by_player[key], key=lambda m: (m["post"]["created_at"], m["post"]["id"]))
        current = [items[0]]
        for m in items[1:]:
            gap = (parse_dt(m["post"]["created_at"])
                   - parse_dt(current[-1]["post"]["created_at"])).total_seconds()
            if gap <= window_s:
                current.append(m)
            else:
                out.append(current)
                current = [m]
        out.append(current)
    return out


def session_key(player_key: str, post_ids: list[str]) -> str:
    raw = player_key + "|" + ",".join(sorted(post_ids))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _quoted_id(post: dict) -> str | None:
    for ref in post.get("referenced_tweets") or []:
        if ref.get("type") == "quoted":
            return ref.get("id")
    return None


def _prompt_posts(session: list[dict], rw_handle: str) -> list[dict]:
    out = []
    for m in session:
        p = m["post"]
        out.append({
            "id": p["id"],
            "side": "RW" if p["account"] == rw_handle else "UD",
            "time": parse_dt(p["created_at"]).astimezone(ET).strftime("%a %H:%M"),
            "text": p.get("text", ""),
            "quotes": _quoted_id(p),
        })
    return out


class EventClassGrouper:
    """Stand-in when ANTHROPIC_API_KEY is unavailable: one development per event_class.

    Its groupings are never cached (cache=False at the call site) so the real model still
    groups every session once the key is present.
    """

    def __init__(self, by_id: dict[str, dict]):
        self.by_id = by_id

    def group(self, player: str | None, posts: list[dict]) -> list[dict]:
        groups: dict[str, list[str]] = {}
        for p in posts:
            groups.setdefault(self.by_id[p["id"]].get("event_class") or "other", []).append(p["id"])
        return [{"label": f"{player or ''} {ec}".strip(), "post_ids": ids}
                for ec, ids in groups.items()]


def _side(post: dict) -> dict:
    return {
        "tweet_id": post["id"],
        "created_at": post["created_at"],
        "impression_count": (post.get("public_metrics") or {}).get("impression_count"),
        "text": post.get("text", ""),
        "borderline": post.get("borderline"),   # hype kind counted as news, if any
    }


def _earliest(ms: list[dict]) -> dict:
    return min(ms, key=lambda m: (m["post"]["created_at"], m["post"]["id"]))


def resolve_developments(
    data_dir: Path = DATA_DIR,
    sport: str = "nfl",
    accounts: dict | None = None,
    grouper=None,
    cache: bool = True,
    method: str = "llm",
) -> list[dict]:
    """Build stories.jsonl by grouping each player's posts into developments."""
    cfg = load_config()
    window_s = cfg["matching"]["time_window_minutes"] * 60
    accounts = accounts if accounts is not None else sport_accounts(cfg, sport)
    rw_handle = accounts["rotowire"]["handle"]

    news = [r for r in load_jsonl(data_dir / "tweets.jsonl") if r.get("is_news")]
    by_id = {r["id"]: r for r in news}

    cache_path = data_dir / "developments.jsonl"
    cached = {g["session_key"]: g for g in load_jsonl(cache_path)} if cache else {}

    stories: list[dict] = []
    for session in sessions(mentions(news), window_s):
        pkey = session[0]["player_key"]
        player = next((m["player"] for m in session if m["player"]), None)
        team = next((m["team"] for m in session if m["team"]), None)
        mention_by_id = {m["post"]["id"]: m for m in session}
        post_ids = list(mention_by_id)
        two_sided = len({m["post"]["account"] for m in session}) == 2

        if not two_sided:
            groups = EventClassGrouper(by_id).group(player, _prompt_posts(session, rw_handle))
            source = "single_feed"
        else:
            skey = session_key(pkey, post_ids)
            hit = cached.get(skey)
            if hit is not None and (hit.get("prompt_version") or 1) < GROUP_PROMPT_VERSION \
                    and method == "llm":
                hit = None              # grouped under older instructions: redo once
            if hit is not None:
                groups = hit["developments"]
            else:
                if grouper is None:
                    from .anthropic_client import Grouper

                    label = (cfg.get("sports", {}).get(sport, {}) or {}).get("label") or sport.upper()
                    grouper = Grouper(sport_label=label)
                groups = grouper.group(player, _prompt_posts(session, rw_handle))
                if cache:
                    rec = {"session_key": skey, "player_key": pkey, "post_ids": sorted(post_ids),
                           "developments": groups, "prompt_version": GROUP_PROMPT_VERSION,
                           "grouped_at": now_iso()}
                    append_jsonl(cache_path, rec)
                    cached[skey] = rec
            source = method

        for g in groups:
            ms = [mention_by_id[i] for i in g["post_ids"] if i in mention_by_id]
            if ms:
                # One account's step inside a session both accounts posted in: an update gap.
                update_gap = two_sided and len({m["post"]["account"] for m in ms}) == 1
                for st in _development_stories(
                        ms, g.get("label"), player, pkey, team, rw_handle, window_s, source):
                    st["session_two_sided"] = update_gap
                    stories.append(st)

    _mark_gaps(stories, news, rw_handle)
    stories.sort(key=lambda s: s["story_id"])
    write_jsonl(data_dir / "stories.jsonl", stories)
    return stories


def _mark_gaps(stories: list[dict], news: list[dict], rw_handle: str) -> None:
    """Tag each coverage gap (one-sided, not a follow-up) as missed or late.

    gap_kind "late": the other account posted about the same player within LATE_WINDOW_S
    after the lone post (other_side_later names that post and the delay). It may be a
    different development, which is why the post is shown rather than claimed as a match.
    gap_kind "missed": the other account said nothing about the player in that window.
    gap_kind "roundup": the lone post is a roundup (see is_roundup), so it is not a gap.
    gap_kind "update": both accounts covered the player's story in this session, but only
    one posted this step of it (e.g. "headed to the locker room"). other_side_near names
    the other account's closest post in the session. Not counted as a missed story.
    """
    by_player: dict[tuple, list[dict]] = {}
    for m in mentions(news):
        side = "rw" if m["post"]["account"] == rw_handle else "ud"
        by_player.setdefault((m["player_key"], side), []).append(m["post"])
    for posts in by_player.values():
        posts.sort(key=lambda p: p["created_at"])

    for st in stories:
        if st["status"] == "matched" or st.get("same_event_duplicate"):
            continue
        lone = st["rotowire"] or st["underdog"]
        if st.get("roundup"):
            st["gap_kind"] = "roundup"
            st["other_side_later"] = None
            continue
        other = "ud" if st["rotowire"] else "rw"
        t0 = parse_dt(lone["created_at"])
        if st.get("session_two_sided"):
            near = min(by_player.get((st["player_key"], other), []),
                       key=lambda p: abs((parse_dt(p["created_at"]) - t0).total_seconds()),
                       default=None)
            st["gap_kind"] = "update"
            st["other_side_later"] = None
            st["other_side_near"] = near and {
                "tweet_id": near["id"], "created_at": near["created_at"],
                "text": near.get("text", ""),
                "delay_seconds": int((parse_dt(near["created_at"]) - t0).total_seconds())}
            continue
        later = None
        for p in by_player.get((st["player_key"], other), []):
            delay = (parse_dt(p["created_at"]) - t0).total_seconds()
            if 0 < delay <= LATE_WINDOW_S:
                later = {"tweet_id": p["id"], "created_at": p["created_at"],
                         "text": p.get("text", ""), "delay_seconds": int(delay)}
                break
        st["gap_kind"] = "late" if later else "missed"
        st["other_side_later"] = later


def _development_stories(ms, label, player, pkey, team, rw_handle, window_s, source) -> list[dict]:
    rw = [m for m in ms if m["post"]["account"] == rw_handle]
    ud = [m for m in ms if m["post"]["account"] != rw_handle]
    multi = any(m["multi_player"] for m in ms)
    suffix = "_" + pkey.replace(" ", "-") if multi else ""
    base = {"canonical_label": label, "player": player, "player_key": pkey, "team": team,
            "development_size": len(ms), "computed_at": now_iso()}

    def one_sided(m, *, dup_of=None):
        is_rw = m["post"]["account"] == rw_handle
        return {**base,
                "story_id": f"st_{m['post']['id']}{suffix}",
                "event_class": m["post"].get("event_class"),
                "status": "rotowire_only" if is_rw else "underdog_only",
                "same_event_duplicate": dup_of is not None,
                "duplicate_of": dup_of,
                "rotowire": _side(m["post"]) if is_rw else None,
                "underdog": None if is_rw else _side(m["post"]),
                "time_delta_seconds": None, "rotowire_first": None,
                "match_confidence": None, "match_method": "none",
                "roundup": bool(m.get("roundup"))}

    out: list[dict] = []
    first_rw = _earliest(rw) if rw else None
    first_ud = _earliest(ud) if ud else None
    heads: dict[str, str] = {}  # account -> story_id its follow-ups point at

    delta = None
    if first_rw and first_ud:
        delta = int((parse_dt(first_ud["post"]["created_at"])
                     - parse_dt(first_rw["post"]["created_at"])).total_seconds())
    if delta is not None and abs(delta) <= window_s:
        sid = f"st_{first_rw['post']['id']}_{first_ud['post']['id']}{suffix}"
        out.append({**base,
                    "story_id": sid,
                    "event_class": first_rw["post"].get("event_class"),
                    "status": "matched",
                    "same_event_duplicate": False, "duplicate_of": None,
                    "rotowire": _side(first_rw["post"]),
                    "underdog": _side(first_ud["post"]),
                    "time_delta_seconds": delta,
                    "rotowire_first": delta > 0,
                    "match_confidence": None,
                    "match_method": source})
        heads = {"rw": sid, "ud": sid}
    else:
        for key, first in (("rw", first_rw), ("ud", first_ud)):
            if first:
                story = one_sided(first)
                out.append(story)
                heads[key] = story["story_id"]

    for key, side_ms, first in (("rw", rw, first_rw), ("ud", ud, first_ud)):
        for m in side_ms:
            if m is not first:
                out.append(one_sided(m, dup_of=heads[key]))
    return out
