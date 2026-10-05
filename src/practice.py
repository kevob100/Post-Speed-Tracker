"""Pre-practice vs post-practice updates (NFL).

A practice day produces two kinds of update and RotoWire tries to post both, since both
matter to fantasy players:

  pre   before or during the session, from observation or announcement: will / won't
        practice, is practicing, seen / not seen, worked on the side, in uniform, what
        the media saw in the open portion
  post  the session's result, usually from the official practice / injury report:
        full / limited / did not participate, "officially", "listed as"

The development grouper treats both as the same story (so a RotoWire pre-practice post
hours before Underdog's report is a RotoWire win). This module tracks them separately,
per player per practice day, so a day where Underdog skipped the pre-practice update
shows up as exactly that.

Each practice post's phase is labelled once by the model (cached in
data/<sport>/practice_phase.jsonl). Without an API key, uncached posts fall back to a
keyword guess that is never cached.
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .store import append_jsonl, load_jsonl, now_iso, parse_dt

ET = ZoneInfo("America/New_York")
PHASES = ("pre", "post")
PRACTICE_RE = re.compile(r"practic|walkthrough|participant|\bDNP\b|injury report|individual drills|"
                         r"open to the media|media-viewing|on the side|in uniform|suited up", re.I)
PROMPT_VERSION = 1

PHASE_PROMPT = (
    "You label one NFL news post about a player and a practice. Answer with JSON only: "
    '{"phase": "pre" | "post" | "other"}.\n'
    "pre: reported before or during that day's practice, from observation or announcement: "
    "he will or won't practice today, is practicing, was seen or not seen, worked on the side "
    "or with a trainer, was in uniform or a non-contact jersey, took part in or missed the "
    "portion open to the media, a coach saying before practice whether he will take part.\n"
    "post: the result of that day's session, usually from the official practice or injury "
    "report: full, limited or did not participate, DNP, 'officially', 'listed as', "
    "'fully practices', 'limited in practice', or a plain statement that he did not practice "
    "that day when it reads as the day's final status.\n"
    "other: not about one specific day's practice (game designations like questionable or "
    "out, 'could practice this week', injury diagnoses, roster moves)."
)


def is_practice_post(post: dict) -> bool:
    return bool(post.get("is_news")) and bool(PRACTICE_RE.search(post.get("text") or ""))


_POST_HINT = re.compile(r"officially|participant|listed as|injury report|practice report|\bDNP\b|"
                        r"fully practices|limited in practice|practiced (?:in full|fully|on a limited)", re.I)
_PRE_HINT = re.compile(r"will (?:not )?practice|won't practice|is practicing|seen|spotted|on the side|"
                       r"in uniform|suited up|open to the media|media-viewing|individual drills|"
                       r"warming up|present|will take part|not practicing|is participating|"
                       r"portion of|participating in (?:\w+day's )?practice", re.I)


def guess_phase(text: str) -> str:
    """Keyword stand-in when no model is available. Post wins ties: report language is
    the stronger signal."""
    if _POST_HINT.search(text or ""):
        return "post"
    if _PRE_HINT.search(text or ""):
        return "pre"
    return "other"


def parse_phase(raw: str | None) -> str:
    m = re.search(r'"phase"\s*:\s*"(pre|post|other)"', raw or "")
    return m.group(1) if m else "other"


class PhaseLabeler:
    """Model-backed labeler (same client setup as anthropic_client). Inject `client` in
    tests; the real client is created lazily, so importing needs no key."""

    def __init__(self, client=None, model: str | None = None):
        from .config import load_config

        self._client = client
        self.model = model or load_config()["llm"]["model"]

    def client(self):
        if self._client is None:
            from anthropic import Anthropic

            from .config import env

            self._client = Anthropic(api_key=env("ANTHROPIC_API_KEY"), max_retries=6)
        return self._client

    def label(self, text: str) -> str:
        resp = self.client().messages.create(model=self.model, max_tokens=30, system=PHASE_PROMPT,
                                             messages=[{"role": "user", "content": text}])
        return parse_phase(resp.content[0].text)


def label_phases(data_dir: Path, labeler=None, llm: bool = True) -> dict[str, str]:
    """tweet id -> phase for every practice post; model verdicts are cached."""
    path = data_dir / "practice_phase.jsonl"
    cached = {r["id"]: r["phase"] for r in load_jsonl(path)
              if (r.get("prompt_version") or 0) >= PROMPT_VERSION}
    out: dict[str, str] = {}
    for post in load_jsonl(data_dir / "tweets.jsonl"):
        if not is_practice_post(post):
            continue
        tid = post["id"]
        if tid in cached:
            out[tid] = cached[tid]
        elif llm:
            labeler = labeler or PhaseLabeler()
            try:
                phase = labeler.label(post.get("text") or "")
            except Exception as exc:                 # transient API error: guess, don't cache
                print(f"[practice]  label failed for {tid} ({exc}); guessing")
                out[tid] = guess_phase(post.get("text") or "")
                continue
            append_jsonl(path, {"id": tid, "phase": phase, "prompt_version": PROMPT_VERSION,
                                "labeled_at": now_iso()})
            out[tid] = phase
        else:
            out[tid] = guess_phase(post.get("text") or "")
    return out


def practice_days(posts: list[dict], phases: dict[str, str], rw_handle: str,
                  key_of=lambda p: [x["player_key"] for x in (p.get("players") or []) if x.get("player_key")]
                  ) -> list[dict]:
    """One row per (player, ET date): each side's first pre and first post update."""
    days: dict[tuple, dict] = {}
    for p in posts:
        phase = phases.get(p["id"])
        if phase not in PHASES:
            continue
        side = "rotowire" if p["account"] == rw_handle else "underdog"
        t = parse_dt(p["created_at"])
        date = t.astimezone(ET).date().isoformat()
        for key in key_of(p) or [p.get("player_key")]:
            if not key:
                continue
            row = days.setdefault((key, date), {"player_key": key, "date": date,
                                                "player": None, "pre": {}, "post": {}})
            name = next((x.get("name") for x in p.get("players") or [] if x.get("player_key") == key), None)
            row["player"] = row["player"] or name or p.get("player")
            prev = row[phase].get(side)
            if prev is None or t < parse_dt(prev["created_at"]):
                row[phase][side] = {"tweet_id": p["id"], "created_at": p["created_at"],
                                    "text": p.get("text", "")}
    return sorted(days.values(), key=lambda r: (r["date"], r["player_key"]))


def _phase_summary(rows: list[dict], phase: str, tie_s: int) -> dict:
    have = [r[phase] for r in rows if r[phase]]
    both = [h for h in have if "rotowire" in h and "underdog" in h]
    d = [int((parse_dt(h["underdog"]["created_at"]) - parse_dt(h["rotowire"]["created_at"])).total_seconds())
         for h in both]
    n = len(have)
    pct = lambda a, b: round(a / b, 4) if b else None  # noqa: E731
    rw = sum(1 for h in have if "rotowire" in h)
    ud = sum(1 for h in have if "underdog" in h)
    return {"days": n, "rotowire_posted": rw, "underdog_posted": ud,
            "rotowire_posted_rate": pct(rw, n), "underdog_posted_rate": pct(ud, n),
            "both": len(both), "rotowire_only": rw - len(both), "underdog_only": ud - len(both),
            "rotowire_first": sum(1 for x in d if x > tie_s), "ties": sum(1 for x in d if abs(x) <= tie_s),
            "underdog_first": sum(1 for x in d if x < -tie_s),
            "median_lead_seconds": round(statistics.median(d), 1) if d else None}


def rollup(data_dir: Path, rw_handle: str, phases: dict[str, str], now: datetime,
           days: int = 30, tie_s: int = 60, list_days: int = 14, alias=None) -> dict:
    """Pre vs post practice coverage and speed: last `days`, plus a recent day list."""
    posts = [p for p in load_jsonl(data_dir / "tweets.jsonl") if p["id"] in phases]
    if alias:
        key_of = lambda p: [alias.get(x["player_key"], x["player_key"])  # noqa: E731
                            for x in (p.get("players") or []) if x.get("player_key")]
        rows = practice_days(posts, phases, rw_handle, key_of)
    else:
        rows = practice_days(posts, phases, rw_handle)
    since = (now - timedelta(days=days)).astimezone(ET).date().isoformat()
    recent = [r for r in rows if r["date"] >= since]
    list_since = (now - timedelta(days=list_days)).astimezone(ET).date().isoformat()
    return {"days": days, "since": since, "tie_seconds": tie_s,
            "phases": {ph: _phase_summary(recent, ph, tie_s) for ph in PHASES},
            "recent": [r for r in rows if r["date"] >= list_since]}
