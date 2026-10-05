"""JSONL + state storage helpers shared across pipeline stages."""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA_DIR


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Short first names -> one canonical form, so "Pat Surtain" and "Patrick Surtain" share a
# key. Applied to the first token only, and only when a surname follows. Keep entries
# unambiguous: a short form that commonly stands for two different names (Chris, Alex,
# Nick, Sam) is left out rather than risk merging two players. Tom/Tommy is out too:
# MLB has both a Thomas White and a Tommy White.
NICKNAMES = {
    "pat": "patrick", "mike": "michael", "mikey": "michael", "matt": "matthew",
    "dan": "daniel", "danny": "daniel", "dave": "david",
    "jim": "james", "jimmy": "james", "jake": "jacob", "joe": "joseph", "joey": "joseph",
    "bob": "robert", "bobby": "robert", "rob": "robert", "robbie": "robert",
    "bill": "william", "billy": "william", "will": "william", "willie": "william",
    "ben": "benjamin", "benny": "benjamin", "josh": "joshua", "zach": "zachary",
    "zack": "zachary", "ken": "kenneth", "kenny": "kenneth", "tony": "anthony",
    "jon": "jonathan", "johnny": "john", "steve": "steven", "stevie": "steven",
    "greg": "gregory", "jeff": "jeffrey", "ed": "edward", "eddie": "edward",
    "andy": "andrew", "rick": "richard", "ricky": "richard",
    "tim": "timothy", "timmy": "timothy", "nate": "nathan",
    "cam": "cameron", "gabe": "gabriel",
}


def normalize_name(s: str | None) -> str:
    """Canonical player key: strip accents/diacritics, lowercase, drop punctuation
    and suffixes (jr/sr/ii/iii/iv), collapse whitespace. Dependency-free so it can be
    used to key stories/candidates by player without the roster.

    Periods and apostrophes are removed outright and spaced initials are joined, so
    "D.J. Moore", "D. J. Moore" and "DJ Moore" all key as "dj moore". A short first name
    maps to its long form via NICKNAMES ("Pat Surtain" -> "patrick surtain").

    e.g. "Eury Pérez" -> "eury perez"; "Bobby Witt Jr." -> "robert witt".
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[.'’`]", "", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    toks = [t for t in s.split() if t not in {"jr", "sr", "ii", "iii", "iv"}]
    # Join leading single letters left by spaced initials: "d j moore" -> "dj moore".
    lead = 0
    while lead < len(toks) - 1 and len(toks[lead]) == 1:
        lead += 1
    joined = (["".join(toks[:lead])] if lead > 1 else toks[:lead]) + toks[lead:]
    if len(joined) >= 2:
        joined[0] = NICKNAMES.get(joined[0], joined[0])
    return " ".join(joined).strip()


def parse_dt(s: str) -> datetime:
    """Parse X/ISO-8601 timestamps robustly on Python 3.9 (handles trailing Z and ms)."""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized datetime: {s!r}")


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def write_jsonl(path: Path, records: list[dict]) -> None:
    """Atomic write: serialize to a temp file then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def append_jsonl(path: Path, record: dict) -> None:
    """Append a single record as one line (used for human-owned reviews.jsonl)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _state_path(data_dir: Path) -> Path:
    return data_dir / "state.json"


def load_state(data_dir: Path = DATA_DIR) -> dict:
    p = _state_path(data_dir)
    if not p.exists():
        return {"accounts": {}, "last_run": None, "dictionary_version": None}
    return json.loads(p.read_text())


def save_state(state: dict, data_dir: Path = DATA_DIR) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    _state_path(data_dir).write_text(json.dumps(state, indent=2))
