"""NFL player positions, so news about players RotoWire doesn't cover isn't counted as a miss.

RotoWire is a fantasy account: it skips offensive linemen, defenders and specialists.
Positions and fantasy rank come from Sleeper's public player list (no key), cached in
data/<sport>/positions.json as {player_key: {pos: [...], rank: n}} and refreshed once a
week. A name two players share keeps every position and the better rank, so it reads as
fantasy (and as relevant) if either player is.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .store import normalize_name

SLEEPER = "https://api.sleeper.app/v1/players/nfl"
FANTASY = frozenset({"QB", "RB", "WR", "TE", "K", "FB"})


def refresh(data_dir: Path, max_age_days: int = 7, session=None) -> int | None:
    """Re-download positions when the cache is missing or older than max_age_days.
    Returns the player count written, or None when the cache was fresh enough."""
    path = data_dir / "positions.json"
    if path.exists():
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if age < timedelta(days=max_age_days):
            return None
    import requests

    resp = (session or requests).get(SLEEPER, timeout=120)
    resp.raise_for_status()
    out: dict[str, dict] = {}
    for p in resp.json().values():
        name, pos = p.get("full_name"), p.get("position")
        if not (name and pos):
            continue
        e = out.setdefault(normalize_name(name), {"pos": set(), "rank": None})
        e["pos"].add(pos)
        r = p.get("search_rank")
        if isinstance(r, int) and r < 9_000_000 and (e["rank"] is None or r < e["rank"]):
            e["rank"] = r
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({k: {"pos": sorted(v["pos"]), "rank": v["rank"]} for k, v in sorted(out.items())},
                              separators=(",", ":")))
    tmp.replace(path)
    return len(out)


def load(data_dir: Path) -> dict[str, dict]:
    path = data_dir / "positions.json"
    return json.loads(path.read_text()) if path.exists() else {}


def non_fantasy(player_keys: list[str], positions: dict[str, dict]) -> bool:
    """True only when every named player is known and none plays a fantasy position."""
    keys = [k for k in player_keys if k]
    return bool(keys) and all(k in positions and not FANTASY & set(positions[k]["pos"]) for k in keys)


def ranked_below(player_key: str, positions: dict[str, dict], cutoff: int) -> bool:
    """True when the player is known and outside Sleeper's top `cutoff` (or unranked)."""
    e = positions.get(player_key)
    return e is not None and (e.get("rank") is None or e["rank"] > cutoff)
