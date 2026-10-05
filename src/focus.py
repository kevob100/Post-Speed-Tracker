"""Focus areas: which kinds of news cost RotoWire the most against Underdog.

Recomputed every pipeline run over a trailing window (default 30 days). Each story gets a
focus group: insider reports (Rapoport, Schefter, Glazer...) and in-game status alerts
are pulled out first because they behave differently, then the news_type buckets. Per
group:

  - time behind: minutes RotoWire trailed on matched stories (volume x lateness)
  - missed: real stories only Underdog posted (borderline clips, roundups, follow-ups
    and skipped steps left out)
  - Underdog's median views: how much fantasy players care about that kind of news

Groups are ranked by priority = share of all time behind + share of all misses, so a
group that is both slow and often missed rises to the top. Each group carries worked
examples: the clear races RotoWire lost by the most (1-30 min behind, both tweets) and
recent stories it missed (Underdog's tweet).
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta

from .news_type import LABELS, news_type

INSIDERS = re.compile(
    r"^(Rap|Rapoport|Schefter|Glazer|Pelissero|Fowler|Garafolo|Graziano|Russini|Breer|"
    r"Silver|Schultz|Anderson|Wilson|Jones)\s*:|per @(AdamSchefter|RapSheet|JayGlazer|"
    r"TomPelissero|JFowlerESPN|MikeGarafolo|DanGrazianoESPN|DiannaRussini|AlbertBreer|MikeSilver)|"
    r"via @(AdamSchefter|RapSheet|JayGlazer|TomPelissero|JFowlerESPN|MikeGarafolo)",
    re.I)
IN_GAME_ALERT = re.compile(r"^(Status alert|Lineup alert|Update)\s*:", re.I)

GROUPS = {"insider": "Insider reports", "in_game_alert": "In-game status alerts"}


def group_of(story: dict) -> str:
    ud = (story.get("underdog") or {}).get("text", "")
    rw = (story.get("rotowire") or {}).get("text", "")
    if INSIDERS.search(ud) or INSIDERS.search(rw):
        return "insider"
    if IN_GAME_ALERT.search(ud):
        return "in_game_alert"
    return news_type(story)


def label_of(key: str) -> str:
    return GROUPS.get(key) or LABELS.get(key) or key


def _side(side: dict | None) -> dict | None:
    if not side:
        return None
    return {k: side.get(k) for k in ("tweet_id", "created_at", "text", "impression_count")}


def _distinct(sts: list[dict], n: int) -> list[dict]:
    """First n stories with distinct Underdog tweets (one post naming two players is one example)."""
    seen, out = set(), []
    for s in sts:
        tid = (s.get("underdog") or {}).get("tweet_id")
        if tid in seen:
            continue
        seen.add(tid)
        out.append(s)
        if len(out) == n:
            break
    return out


def rollup(stories: list[dict], story_time, now: datetime, mature_views, tweets_by_id: dict,
           days: int = 30, tie_s: int = 60, min_stories: int = 10,
           n_slow: int = 3, n_missed: int = 3, example_max_s: int = 1800) -> dict:
    since = now - timedelta(days=days)
    sts = [s for s in stories if story_time(s) >= since
           and s.get("review_status") not in ("rejected", "merged")
           and not s.get("same_event_duplicate") and not s.get("roundup")]

    views = lambda side: mature_views(tweets_by_id.get((side or {}).get("tweet_id")))  # noqa: E731
    groups: dict[str, dict] = {}
    for s in sts:
        g = groups.setdefault(group_of(s), {"matched": [], "missed": [], "rw_only": 0, "steps": 0,
                                            "ud_views": []})
        status = s.get("status")
        if status == "matched" and s.get("time_delta_seconds") is not None:
            g["matched"].append(s)
            v = views(s.get("underdog"))
            if v:
                g["ud_views"].append(v)
        elif status == "underdog_only" and not (s.get("underdog") or {}).get("borderline"):
            if s.get("gap_kind") == "update":
                g["steps"] += 1
            elif s.get("gap_kind") in ("not_fantasy", "not_covered"):
                continue            # outside RotoWire's beat: not a miss
            else:
                g["missed"].append(s)
                v = views(s.get("underdog"))
                if v:
                    g["ud_views"].append(v)
        elif status == "rotowire_only" and s.get("gap_kind") not in ("update", "not_fantasy"):
            g["rw_only"] += 1

    behind = {k: sum(-s["time_delta_seconds"] for s in g["matched"] if s["time_delta_seconds"] < 0) / 60
              for k, g in groups.items()}
    total_behind = sum(behind.values()) or 1
    total_missed = sum(len(g["missed"]) for g in groups.values()) or 1

    rows = []
    for k, g in groups.items():
        d = [s["time_delta_seconds"] for s in g["matched"]]
        n = len(d) + len(g["missed"]) + g["rw_only"]
        if n < min_stories:
            continue
        # Examples come from clear races (RotoWire 1-30 min behind): the extreme hours-apart
        # matches are often loosely related reports and make poor examples.
        slow = _distinct(sorted((s for s in g["matched"] if -example_max_s <= s["time_delta_seconds"] < -tie_s),
                                key=lambda s: s["time_delta_seconds"]), n_slow)
        missed = _distinct(sorted(g["missed"], key=story_time, reverse=True), n_missed)
        rows.append({
            "key": k, "label": label_of(k), "stories": n, "matched": len(d),
            "rotowire_first": sum(1 for x in d if x > tie_s),
            "ties": sum(1 for x in d if abs(x) <= tie_s),
            "underdog_first": sum(1 for x in d if x < -tie_s),
            "median_lead_seconds": round(statistics.median(d), 1) if d else None,
            "minutes_behind": round(behind[k]),
            "share_of_time_behind": round(behind[k] / total_behind, 4),
            "missed": len(g["missed"]), "missed_rate": round(len(g["missed"]) / n, 4),
            "share_of_misses": round(len(g["missed"]) / total_missed, 4),
            "steps_skipped": g["steps"], "rotowire_only": g["rw_only"],
            "underdog_median_views": round(statistics.median(g["ud_views"])) if g["ud_views"] else None,
            "priority": round(behind[k] / total_behind + len(g["missed"]) / total_missed, 4),
            "slowest": [{"story_id": s["story_id"], "player": s.get("player"),
                         "delta_seconds": s["time_delta_seconds"],
                         "rotowire": _side(s.get("rotowire")), "underdog": _side(s.get("underdog"))}
                        for s in slow],
            "recent_misses": [{"story_id": s["story_id"], "player": s.get("player"),
                               "underdog": _side(s.get("underdog"))} for s in missed],
        })
    rows.sort(key=lambda r: -r["priority"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return {"days": days, "since": since.date().isoformat(), "tie_seconds": tie_s, "groups": rows}
