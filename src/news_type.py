"""Keyword-based news type for a matched story (NFL wording).

Used only for the dashboard's by-type rollup, so it is deliberately cheap: rules run
over both posts' text and the first rule that matches wins, so order matters (an
in-game injury that mentions practice is still in-game). Tuned on the 2026 NFL posts;
treat the buckets as close, not exact.
"""
from __future__ import annotations

import re

NEWS_TYPES = [
    ("in_game", "In-game",
     r"return(ed)? to|won'?t return|questionable to return|status alert|left (sunday|monday|"
     r"thursday|wednesday|saturday|the game|.{0,30}game)|carted|locker room|medical tent|x-rays|"
     r"blue tent|enters game|exited|exiting"),
    ("injury_report", "Practice / injury report",
     r"injury report|not listed|practice report|full participant|non-participant|\bDNP\b|"
     r"(limited|fully|full|did not|didn'?t|doesn'?t|won'?t) (in )?practic"),
    ("transaction", "Transaction",
     r"injured reserve|\bIR\b|sign(s|ed|ing)\b|releas|waive|trade|activated|claim|practice squad|"
     r"contract|extension|suspend|restructur"),
    ("diagnosis", "Diagnosis / timeline",
     r"\bMRI\b|surgery|procedure|tests?\b|sprain|strain|\btear\b|torn|diagnos|expected to miss|"
     r"out (for )?(several|multiple|\d+|a few) weeks|week-to-week|day-to-day"),
    ("game_status", "Game status",
     r"ruled out for|\bdoubtful\b|questionable for|listed as questionable|\binactive\b|\bactive\b|"
     r"will play|won'?t play|game-time decision|pre-?game decision|expected to play|good to go|"
     r"on track to play|will start|starting|not expected to play|pitch count|snap count"),
    ("practice_obs", "Practice observation",
     r"practice|\blimited\b|did not participate|seen|spotted|working out|pregame workout"),
]
OTHER = ("comments", "Coach / reporter comments")

LABELS = dict([(k, label) for k, label, _ in NEWS_TYPES] + [OTHER])
_COMPILED = [(k, re.compile(p, re.I)) for k, _, p in NEWS_TYPES]


def news_type(story: dict) -> str:
    text = "\n".join((story.get(side) or {}).get("text", "") for side in ("rotowire", "underdog"))
    for key, pat in _COMPILED:
        if pat.search(text):
            return key
    return OTHER[0]
