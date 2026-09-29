"""Phase 3 Stage 2 helper: Anthropic match-adjudication client.

Wraps the Anthropic SDK to ask, for a candidate pair, whether both posts describe
the SAME news event. The model must return strict JSON:

    {"same_story": true, "confidence": 0.0-1.0, "canonical_label": "short description"}

`parse_verdict` is pure and defensive (strips ``` fences, falls back to the first
{...} block, never raises) so it can be unit-tested without any network or key. The
Anthropic client itself is created lazily, so importing this module never requires
ANTHROPIC_API_KEY — only calling `.verdict()` does.
"""
from __future__ import annotations

import json
import re

from .config import env, load_config

SYSTEM_PROMPT = (
    "You decide whether two short social-media posts describe the SAME real-world "
    "sports news event about the SAME player (e.g. both report the same injury, roster "
    "move, or status change). Different events about the same player (an AM scratch "
    "vs a PM injury designation) are NOT the same story. "
    "IMPORTANT — name matching: the two posts may name the player differently. Treat "
    "names as the same player when they plausibly refer to the same individual despite "
    "accents/diacritics (Eury Perez = Eury Pérez), spelling variants or typos, "
    "abbreviations or initials, suffixes (Jr./Sr./II), or common nicknames. Do NOT "
    "require an exact string match on the name. "
    "Respond with JSON ONLY — no prose, no markdown fences. Schema: "
    '{"same_story": <bool>, "confidence": <0.0-1.0>, "canonical_label": "<short story description>"}'
)


def _bad_verdict() -> dict:
    return {"same_story": False, "confidence": 0.0, "canonical_label": None, "parse_error": True}


def parse_verdict(raw: str | None) -> dict:
    """Defensively parse the model's JSON verdict. Never raises."""
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
        return _bad_verdict()
    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "same_story": bool(data.get("same_story")),
        "confidence": max(0.0, min(1.0, confidence)),
        "canonical_label": data.get("canonical_label"),
    }


def _user_prompt(player: str | None, rw_text: str, ud_text: str) -> str:
    return (
        f"Player under consideration: {player or 'unknown'}\n\n"
        f"Post A (RotoWire):\n{rw_text}\n\n"
        f"Post B (Underdog):\n{ud_text}\n\n"
        "Do these describe the same news event for that player?"
    )


class Adjudicator:
    """Thin Anthropic wrapper. Inject `client` in tests to avoid network/key."""

    def __init__(self, client=None, model: str | None = None, max_tokens: int = 300):
        self._client = client
        self.model = model or load_config()["llm"]["model"]
        self.max_tokens = max_tokens

    def client(self):
        if self._client is None:
            from anthropic import Anthropic

            self._client = Anthropic(api_key=env("ANTHROPIC_API_KEY"))
        return self._client

    def verdict(self, player: str | None, rw_text: str, ud_text: str) -> dict:
        resp = self.client().messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _user_prompt(player, rw_text, ud_text)}],
        )
        return parse_verdict(resp.content[0].text)


# --------------------------------------------------------------------------- #
# Development grouping (match_mode: developments)
#
# Instead of judging posts two at a time, the model sees every post about ONE player
# in a news session (both accounts, time-ordered) and splits them into distinct
# developments. Each development is scored once: the first post on each side is that
# side's time, and every later post on it is a follow-up.
# --------------------------------------------------------------------------- #

GROUP_SYSTEM_PROMPT = (
    "You group short social-media posts about ONE {sport} player into distinct news "
    "developments. The posts come from two news accounts, RotoWire (RW) and Underdog (UD), "
    "and are listed in time order (US Eastern).\n\n"
    "A development is ONE underlying event or status: e.g. he got hurt / left the game; he "
    "is questionable to return; he is ruled out; he was limited in Wednesday's practice; he "
    "had a procedure and will miss time; he was placed on IR; he signed a contract.\n\n"
    "SAME development: posts that report the same underlying event or status, even when "
    "worded differently, when one adds quotes or colour, or when one is less specific "
    "(\"headed to the medical tent\" and \"left the game with an apparent leg injury\" both "
    "report that he got hurt).\n"
    "DIFFERENT development: a later post that reports new information that changes the "
    "situation (questionable to return -> ruled out; limited -> full practice; expected to "
    "miss time -> placed on IR), or an observation and its later explanation (\"not seen at "
    "practice\" vs \"underwent a knee procedure\").\n"
    "Each account usually posts once per development and posts again only when something "
    "new happens, so a second post from the SAME account (including a post marked "
    "[quotes ...], which quotes the account's earlier post) normally starts a new "
    "development unless it only repeats or adds colour to its earlier post.\n\n"
    "Every post id must appear in exactly one development. "
    "Respond with JSON ONLY - no prose, no markdown fences. Schema: "
    '{"developments": [{"label": "<short description>", "post_ids": ["<id>", ...]}]}'
)


def parse_groups(raw: str | None, post_ids: list[str]) -> list[dict]:
    """Defensively parse the model's grouping. Never raises.

    Guarantees every id in `post_ids` lands in exactly one group: unknown ids are dropped,
    an id claimed twice stays with its first group, and any id the model left out becomes
    its own single-post group. An unparseable reply yields one group per post.
    """
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

    valid = set(post_ids)
    seen: set[str] = set()
    groups: list[dict] = []
    devs = data.get("developments") if isinstance(data, dict) else None
    for d in devs if isinstance(devs, list) else []:
        if not isinstance(d, dict):
            continue
        ids = [str(i) for i in (d.get("post_ids") or []) if str(i) in valid and str(i) not in seen]
        if not ids:
            continue
        seen.update(ids)
        groups.append({"label": d.get("label"), "post_ids": ids})
    for pid in post_ids:
        if pid not in seen:
            groups.append({"label": None, "post_ids": [pid]})
    return groups


def _group_user_prompt(player: str | None, posts: list[dict]) -> str:
    lines = [f"Player: {player or 'unknown'}", "", "Posts:"]
    for p in posts:
        quote = f" [quotes {p['quotes']}]" if p.get("quotes") else ""
        text = " ".join((p.get("text") or "").split())
        lines.append(f"- id={p['id']} {p['side']} {p['time']}{quote}: {text}")
    lines += ["", "Group these posts into developments."]
    return "\n".join(lines)


class Grouper:
    """Thin Anthropic wrapper for development grouping. Inject `client` in tests."""

    def __init__(self, client=None, model: str | None = None, max_tokens: int = 1500,
                 sport_label: str = "NFL"):
        self._client = client
        self.model = model or load_config()["llm"]["model"]
        self.max_tokens = max_tokens
        self.system_prompt = GROUP_SYSTEM_PROMPT.replace("{sport}", sport_label)

    def client(self):
        if self._client is None:
            from anthropic import Anthropic

            self._client = Anthropic(api_key=env("ANTHROPIC_API_KEY"))
        return self._client

    def group(self, player: str | None, posts: list[dict]) -> list[dict]:
        """`posts`: [{id, side: RW|UD, time, text, quotes?}] in time order."""
        resp = self.client().messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self.system_prompt,
            messages=[{"role": "user", "content": _group_user_prompt(player, posts)}],
        )
        return parse_groups(resp.content[0].text, [p["id"] for p in posts])
