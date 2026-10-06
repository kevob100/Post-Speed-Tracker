"""Config + path helpers shared across modules."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
DATA_DIR = ROOT / "data"
DOCS_DATA_DIR = ROOT / "docs" / "data"
# The desk works in US Central, so every day, week, hour and displayed time is Central.
DEFAULT_TZ = "America/Chicago"

# override=True so values in .env win over empty/stale vars inherited from the
# shell profile (e.g. an exported ANTHROPIC_API_KEY=''). On CI there is no .env
# file, so injected GitHub Actions secrets are left untouched.
load_dotenv(ROOT / ".env", override=True)


def load_config() -> dict:
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f)


def save_config(cfg: dict) -> None:
    with CONFIG_PATH.open("w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, default_flow_style=False)


def env(name: str, required: bool = True) -> str | None:
    val = os.getenv(name)
    if required and not val:
        raise RuntimeError(f"Missing required environment variable: {name} (add it to .env)")
    return val


# --------------------------------------------------------------------------- #
# Sport helpers
#
# The tracker runs the same RotoWire-vs-Underdog head-to-head for every sport.
# A "sport" is a config entry under `sports:` plus its own data partition
# (data/<sport>/ and docs/data/<sport>/); the rest of the pipeline is unchanged.
# --------------------------------------------------------------------------- #

def sports(cfg: dict, active_only: bool = True) -> list[str]:
    """Sport keys in config order (mlb, nfl, ...). By default only active ones."""
    out = []
    for key, meta in (cfg.get("sports") or {}).items():
        if active_only and not meta.get("active", True):
            continue
        out.append(key)
    return out


def sport_meta(cfg: dict, sport: str) -> dict:
    meta = (cfg.get("sports") or {}).get(sport)
    if meta is None:
        raise KeyError(f"Unknown sport {sport!r} (known: {list((cfg.get('sports') or {}))})")
    return meta


def sport_accounts(cfg: dict, sport: str) -> dict:
    """The {rotowire, underdog} account pair for a sport."""
    return sport_meta(cfg, sport)["accounts"]


def sport_data_dir(sport: str) -> Path:
    return DATA_DIR / sport


def sport_docs_dir(sport: str) -> Path:
    return DOCS_DATA_DIR / sport
