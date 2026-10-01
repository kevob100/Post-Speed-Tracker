"""Phase 2 (LLM edition): per-post classification, replacing the roster-based tagger.

For each post in data/tweets.jsonl the Anthropic model reads the text and returns:

  - is_news:         whether the post is an in-scope MLB news item (an injury, roster
                     move, status change, or role change) about a specific player —
                     NOT a lineup card, promo, or game recap.
  - player:          the canonical player name (the model normalizes spelling/accents/
                     nicknames; we store its best full-name form).
  - team:            the player's team if stated/known, else null.
  - event_class:     one of the PRD classes (injury_il, injury_dtd, scratch, return,
                     transaction_callup, transaction_option, transaction_dfa,
                     transaction_trade, transaction_sign_release, role_change, other).
  - excluded_reason: when is_news is false, a short reason (no_player, lineup_card,
                     promo, recap, not_news).

The enrichment is written back into tweets.jsonl in place (raw fields preserved) and we
also store a normalized `player_key` (via store.normalize_name) for downstream matching.

Verdicts are cached in data/classifications.jsonl (keyed by tweet id) so each post is
classified only once. `parse_classification` is pure/defensive (strips ``` fences, falls
back to the first {...} block, never raises) so it is unit-testable without a key or
network. The Anthropic client is created lazily, so importing this module never requires
ANTHROPIC_API_KEY — only running an uncached classification does.

Run: python -m src.classify
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import DATA_DIR, env, load_config
from .store import append_jsonl, load_jsonl, normalize_name, now_iso, write_jsonl

# Default cross-sport taxonomy (roster sports: MLB, NFL, NBA, WNBA).
DEFAULT_EVENT_CLASSES = (
    "injury",
    "status_change",
    "roster_move",
    "role_change",
    "other",
)

# Golf is an individual sport with no roster: the only in-scope events are a player
# withdrawing from a tournament and changes to a tournament's field.
GOLF_EVENT_CLASSES = ("withdrawal", "field_change", "other")

# Per-sport taxonomy overrides; sports absent here use DEFAULT_EVENT_CLASSES.
SPORT_EVENT_CLASSES = {
    "golf": GOLF_EVENT_CLASSES,
}

# Backward-compatible alias (the generic taxonomy).
EVENT_CLASSES = DEFAULT_EVENT_CLASSES

EXCLUDED_REASONS = ("no_player", "lineup_card", "promo", "recap", "not_news", "hype")

# Why a post was tagged hype (sports with match_mode: developments).
HYPE_KINDS = ("soundbite", "rumor", "in_game_note", "non_fantasy", "other")

# Legacy (baseball-specific) classes -> generic cross-sport taxonomy. Applied when
# reading cached MLB classifications so the whole dashboard is uniform without paying
# to re-classify existing posts.
LEGACY_EVENT_MAP = {
    "injury_il": "injury",
    "injury_dtd": "injury",
    "scratch": "status_change",
    "return": "status_change",
    "transaction_callup": "roster_move",
    "transaction_option": "roster_move",
    "transaction_dfa": "roster_move",
    "transaction_trade": "roster_move",
    "transaction_sign_release": "roster_move",
    "role_change": "role_change",
}


def event_classes_for(sport: str | None) -> tuple[str, ...]:
    return SPORT_EVENT_CLASSES.get(sport or "", DEFAULT_EVENT_CLASSES)


def _canonical_event(event: str | None, valid: tuple[str, ...] = DEFAULT_EVENT_CLASSES) -> str:
    """Coerce an event_class to the sport's taxonomy; remap legacy values; default 'other'."""
    if event in valid:
        return event
    mapped = LEGACY_EVENT_MAP.get(event)
    if mapped and mapped in valid:
        return mapped
    return "other"


DEFAULT_PROMPT = (
    "You classify a single social-media post from a {sport} news account. Decide whether "
    "it is an in-scope NEWS item about a SPECIFIC player: an injury or injury-status "
    "update, a status change (scratch/inactive, return/activation), a roster move "
    "(call-up/assignment, option/demotion, waiver/release, trade, signing), or a role "
    "change (starter/depth-chart/usage). The following are NOT news: starting-lineup "
    "cards, promos/betting/advertisements, and game recaps or stat lines (box-score "
    "performance). "
    "Identify the player by their canonical full name; normalize accents/diacritics, "
    "spelling variants, abbreviations, suffixes (Jr./Sr./II), and nicknames to the real "
    "name. If no single specific player is the subject, set player to null. "
    "Pick exactly one event_class from this list: "
    "injury, status_change, roster_move, role_change, other. "
    "When is_news is false, give excluded_reason as one of: no_player, lineup_card, promo, "
    "recap, not_news. "
    "Respond with JSON ONLY — no prose, no markdown fences. Schema: "
    '{"is_news": <bool>, "player": "<full name or null>", "team": "<team or null>", '
    '"event_class": "<class>", "excluded_reason": "<reason or null>"}'
)

# Golf: only two in-scope events — a withdrawal or a field change.
GOLF_PROMPT = (
    "You classify a single social-media post from a {sport} news account. Decide whether "
    "it is an in-scope NEWS item about a SPECIFIC golfer, limited to exactly TWO kinds of "
    "event: a WITHDRAWAL (the golfer withdraws from or is forced out of a tournament — WD, "
    "pulls out before or during play, or is disqualified/forced out mid-event), or a FIELD "
    "CHANGE (a change to a tournament's field: the golfer commits to or is added to the "
    "field, enters as or is replaced by an alternate, gets in via Monday qualifier or "
    "sponsor exemption, or is removed from the field). "
    "The following are NOT news: leaderboards, round recaps, scores, tee times, pairings, "
    "course/weather notes, promos/betting/advertisements, and general commentary. "
    "Identify the golfer by their canonical full name; normalize accents/diacritics, "
    "spelling variants, abbreviations, suffixes (Jr./Sr./II), and nicknames to the real "
    "name. If no single specific golfer is the subject, set player to null. "
    "Pick exactly one event_class from this list: withdrawal, field_change, other. "
    "When is_news is false, give excluded_reason as one of: no_player, lineup_card, promo, "
    "recap, not_news. "
    "Respond with JSON ONLY — no prose, no markdown fences. Schema: "
    '{"is_news": <bool>, "player": "<full name or null>", "team": "<team or null>", '
    '"event_class": "<class>", "excluded_reason": "<reason or null>"}'
)

# Multi-player variant (sports with match_mode: developments). Underdog often folds several
# players into one post ("Chase (knee), Higgins (heel) fully practice") where RotoWire posts
# one tweet each; listing every player lets each be matched on its own.
MULTI_PLAYER_PROMPT = DEFAULT_PROMPT.replace(
    "If no single specific player is the subject, set player to null. ",
    "If the post reports the same news for SEVERAL specific players, list every one of "
    "them. If no specific player is the subject, return an empty list. ",
).replace(
    '''"player": "<full name or null>", "team": "<team or null>", ''',
    '''"players": [{"name": "<full name>", "team": "<team or null>"}], ''',
).replace(
    "When is_news is false, give excluded_reason as one of: no_player, lineup_card, promo, "
    "recap, not_news. ",
    "HYPE: set is_news to false with excluded_reason \"hype\" for any post that is not a news "
    "report, even when it names a player: a quote or soundbite that does not change a "
    "player's status or role (a quote that DOES carry news, e.g. a coach saying a player "
    "will start or is out, is news); a rumor, speculation, interest or trade/contract "
    "chatter short of a completed move; an in-game note made DURING A GAME that is not a "
    "status change (a player returning to the game, standing on the sideline, getting taped "
    "on the bench); a video clip or photo with no news in it; and ANY post about players who "
    "are not fantasy-relevant: offensive linemen, defensive players, punters and long "
    "snappers. "
    "These are NEWS, never hype: any injury (hurt, left the game or field, carted off, "
    "headed to the tent or locker room, questionable/doubtful to return, ruled out), even "
    "when the post is a clip; practice and pregame observations (participating, not seen at "
    "practice, working on the side, limited, wearing a non-contact jersey, in uniform, "
    "warming up); a quote that reveals health, test results, availability or role (more "
    "reps, starting, a new job such as punt returner). "
    "Only tag non_fantasy when the post states a non-fantasy position (LG, OT, C, DE, DT, "
    "LB, CB, S, P, LS, edge, pass rusher) or you are certain of it; quarterbacks, running "
    "backs, wide receivers, tight ends and kickers are fantasy-relevant, and when you are "
    "unsure of a player's position (rookies especially) it is NOT non_fantasy. "
    "For hype also give hype_kind: soundbite, rumor, in_game_note, non_fantasy, or other. "
    "When is_news is false, give excluded_reason as one of: no_player, lineup_card, promo, "
    "recap, not_news, hype. ",
).replace(
    '''"excluded_reason": "<reason or null>"}''',
    '''"excluded_reason": "<reason or null>", "hype_kind": "<kind or null>"}''',
)

# Bump when MULTI_PLAYER_PROMPT changes. v1 re-ran cached no_player posts (multi-player
# lists); v2 added the hype label and re-ran cached news posts once; v3 narrowed hype
# (practice observations, injuries and unknown positions are news) and re-runs hype posts.
MULTI_PLAYER_VERSION = 3

# Per-sport prompt overrides; sports absent here use DEFAULT_PROMPT.
SPORT_PROMPTS = {
    "golf": GOLF_PROMPT,
}

# Backward-compatible alias.
SYSTEM_PROMPT = DEFAULT_PROMPT


def prompt_for(sport: str | None, multi_player: bool = False) -> str:
    if multi_player and sport not in SPORT_PROMPTS:
        return MULTI_PLAYER_PROMPT
    return SPORT_PROMPTS.get(sport or "", DEFAULT_PROMPT)


def _bad_classification() -> dict:
    return {
        "is_news": False,
        "player": None,
        "team": None,
        "event_class": "other",
        "excluded_reason": "not_news",
        "parse_error": True,
    }


def parse_classification(
    raw: str | None, event_classes: tuple[str, ...] = DEFAULT_EVENT_CLASSES
) -> dict:
    """Defensively parse the model's JSON classification. Never raises.

    `event_classes` is the sport's valid taxonomy; an out-of-vocab class collapses to
    'other' (and legacy MLB classes remap into the generic taxonomy)."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        text = re.sub(r"^json\s*", "", text, flags=re.IGNORECASE).strip()
    data = None
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    if not isinstance(data, dict):
        return _bad_classification()

    event = _canonical_event(data.get("event_class"), event_classes)
    players = _parse_players(data)
    player = players[0]["name"] if players else None
    is_news = bool(data.get("is_news")) and player is not None and event != "other"
    reason = data.get("excluded_reason")
    if is_news:
        reason = None
    elif reason not in EXCLUDED_REASONS:
        reason = "no_player" if not player else "not_news"
    out = {
        "is_news": is_news,
        "player": player,
        "team": (players[0]["team"] if players else None) or data.get("team") or None,
        "event_class": event,
        "excluded_reason": reason,
    }
    if len(players) > 1:
        out["players"] = players
    if reason == "hype":
        kind = data.get("hype_kind")
        out["hype_kind"] = kind if kind in HYPE_KINDS else "other"
    return out


def _parse_players(data: dict) -> list[dict]:
    """[{name, team}] from either the multi-player `players` list or a single `player`."""
    raw = data.get("players")
    if isinstance(raw, list):
        out = []
        for p in raw:
            if isinstance(p, dict) and p.get("name"):
                out.append({"name": p["name"], "team": p.get("team") or None})
            elif isinstance(p, str) and p.strip():
                out.append({"name": p.strip(), "team": None})
        return out
    if data.get("player"):
        return [{"name": data["player"], "team": data.get("team") or None}]
    return []


def _user_prompt(text: str) -> str:
    return f"Post:\n{text}\n\nClassify this post."


class Classifier:
    """Thin Anthropic wrapper. Inject `client` in tests to avoid network/key."""

    def __init__(self, client=None, model: str | None = None, max_tokens: int = 300,
                 sport: str = "mlb", sport_label: str = "MLB", multi_player: bool = False):
        self._client = client
        self.model = model or load_config()["llm"]["model"]
        self.max_tokens = max_tokens
        self.event_classes = event_classes_for(sport)
        # replace (not str.format): the prompt contains literal JSON braces.
        self.system_prompt = prompt_for(sport, multi_player).replace("{sport}", sport_label)

    def client(self):
        if self._client is None:
            from anthropic import Anthropic

            self._client = Anthropic(api_key=env("ANTHROPIC_API_KEY"))
        return self._client

    def classify(self, text: str) -> dict:
        resp = self.client().messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self.system_prompt,
            messages=[{"role": "user", "content": _user_prompt(text)}],
        )
        return parse_classification(resp.content[0].text, self.event_classes)


def _apply(record: dict, c: dict, event_classes: tuple[str, ...] = DEFAULT_EVENT_CLASSES) -> dict:
    """Write a classification onto a tweet record (raw fields preserved)."""
    player = c.get("player")
    record["is_news"] = bool(c.get("is_news"))
    # Coerce any cached/legacy event_class into the sport's taxonomy on read.
    record["event_class"] = _canonical_event(c.get("event_class"), event_classes)
    record["excluded_reason"] = c.get("excluded_reason")
    record["hype_kind"] = c.get("hype_kind")
    record["player"] = player
    record["team"] = c.get("team")
    record["player_key"] = normalize_name(player)
    # players[] lists every player the post names (one for most posts). The pairwise
    # matcher reads only player/player_key; the development matcher scores each entry.
    players = c.get("players") or ([{"name": player, "team": c.get("team")}] if player else [])
    record["players"] = [{"name": p["name"], "team": p.get("team"),
                          "player_key": normalize_name(p["name"])} for p in players]
    return record


def _needs_rerun(c: dict) -> bool:
    """Cached verdict predates the current multi-player prompt and could change under it."""
    v = c.get("multi_player_version") or 0
    if c.get("excluded_reason") == "no_player" and v < 1:
        return True                     # v1: may name several players
    if c.get("excluded_reason") == "hype" and v < 3:
        return True                     # v3: hype narrowed, may be news after all
    return bool(c.get("is_news")) and v < 2   # v2: may now be hype


def _sport_label(sport: str) -> str:
    """Human-readable label for the classifier prompt; robust if config lacks the sport."""
    try:
        return (load_config().get("sports", {}).get(sport, {}) or {}).get("label") or sport.upper()
    except Exception:
        return sport.upper()


def classify_file(
    data_dir: Path = DATA_DIR,
    classifier: Classifier | None = None,
    cache: bool = True,
    llm: bool = True,
    sport: str = "mlb",
    multi_player: bool = False,
) -> dict:
    """Classify every post in tweets.jsonl in place. Returns a small summary.

    Cached verdicts in data/classifications.jsonl (keyed by tweet id) are reused so each
    post is classified only once; new verdicts are appended. The real classifier is
    created lazily on first uncached post, so an all-cached run needs no key.

    With ``multi_player=True`` posts may name several players and non-reports are tagged
    ``hype``. Cached verdicts that the newer prompt could change (``no_player`` posts from
    before v1, news posts from before v2) are classified again once; the new verdict is
    appended, so the cache's last line per id wins.

    With ``llm=False`` (no key available) the LLM is never called: cached posts are
    applied as-is and any uncached post falls back to a safe non-news default so the rest
    of the pipeline keeps working until the key arrives.
    """
    path = data_dir / "tweets.jsonl"
    records = load_jsonl(path)
    valid = event_classes_for(sport)

    cls_path = data_dir / "classifications.jsonl"
    cached = {r["id"]: r for r in load_jsonl(cls_path)} if cache else {}

    for r in records:
        tid = r["id"]
        c = cached.get(tid)
        if c is not None and llm and multi_player and _needs_rerun(c):
            c = None
        if c is None:
            if not llm:
                _apply(r, _bad_classification(), valid)
                continue
            if classifier is None:
                classifier = Classifier(sport=sport, sport_label=_sport_label(sport),
                                        multi_player=multi_player)
            verdict = classifier.classify(r.get("text", ""))
            c = {"id": tid, **verdict, "classified_at": now_iso()}
            if multi_player:
                c["multi_player_version"] = MULTI_PLAYER_VERSION
            if cache:
                append_jsonl(cls_path, c)
                cached[tid] = c
        _apply(r, c, valid)

    records.sort(key=lambda r: (r["created_at"], r["id"]))
    write_jsonl(path, records)

    kept = sum(1 for r in records if r["is_news"])
    reasons: dict[str, int] = {}
    for r in records:
        if r.get("excluded_reason"):
            reasons[r["excluded_reason"]] = reasons.get(r["excluded_reason"], 0) + 1
    return {"total": len(records), "news": kept, "excluded": reasons}


if __name__ == "__main__":
    import argparse

    from .config import sport_data_dir

    ap = argparse.ArgumentParser(description="Classify posts for one sport.")
    ap.add_argument("--sport", default="mlb", help="Sport key from config (default: mlb).")
    args = ap.parse_args()
    summary = classify_file(data_dir=sport_data_dir(args.sport), sport=args.sport)
    print(f"Classified {summary['total']} posts: {summary['news']} news, "
          f"excluded {summary['excluded']}")
