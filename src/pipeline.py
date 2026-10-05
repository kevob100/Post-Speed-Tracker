"""End-to-end automated pipeline runner (Phase 6), multi-sport.

Runs the daily production sequence for every ACTIVE sport in config, in order, printing
a one-line summary per stage:

    collect -> classify (LLM) -> match (candidates) -> resolve -> build aggregates

Each sport is fully isolated in its own data partition (data/<sport>/ and
docs/data/<sport>/), so the exact same RotoWire-vs-Underdog head-to-head runs per sport.
After all sports run, a docs/data/sports.json index is written so the static dashboard
can offer a sport switcher.

This is the single entrypoint the GitHub Actions workflow calls. It NEVER touches
reviews.jsonl (human-owned). The LLM classifier and the adjudication + story resolution
run only when ANTHROPIC_API_KEY is present; without the key the classify step reuses
cached classifications and matching uses the exact-name stand-in, so the aggregate
builder still emits valid output.

Run: python -m src.pipeline [--sport mlb]
"""
from __future__ import annotations

import json
from datetime import datetime
import os

from . import aggregate, classify, collect, develop, games, match
from .config import (
    DOCS_DATA_DIR,
    load_config,
    sport_accounts,
    sport_data_dir,
    sport_docs_dir,
    sports,
)
from .store import load_jsonl, now_iso


def run_sport(sport: str, cfg: dict | None = None) -> dict:
    """Run the full pipeline for a single sport. Returns its aggregates."""
    cfg = cfg or load_config()
    accounts = sport_accounts(cfg, sport)
    meta = cfg["sports"][sport]
    # "developments" scores each news development once (see src/develop.py); the default
    # "pairwise" mode keeps the original closest-pair matcher.
    developments = meta.get("match_mode") == "developments"
    data_dir = sport_data_dir(sport)
    docs_dir = sport_docs_dir(sport)
    data_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== {sport} ===")
    coll = collect.collect(data_dir=data_dir, sport=sport, accounts=accounts)
    print(f"[collect]   {coll}")

    has_key = bool(os.getenv("ANTHROPIC_API_KEY"))
    if has_key:
        classified = classify.classify_file(data_dir=data_dir, sport=sport,
                                            multi_player=developments)
        print(f"[classify]  total={classified['total']} news={classified['news']} "
              f"excluded={classified['excluded']}")
    else:
        # No key: reuse whatever classifications are already cached on the posts.
        classified = classify.classify_file(data_dir=data_dir, sport=sport, llm=False)
        print(f"[classify]  (no key, cached only) total={classified['total']} "
              f"news={classified['news']}")

    if developments:
        if has_key:
            stories = develop.resolve_developments(data_dir=data_dir, sport=sport, accounts=accounts)
            method = "developments (llm)"
        else:
            news = [r for r in load_jsonl(data_dir / "tweets.jsonl") if r.get("is_news")]
            stories = develop.resolve_developments(
                data_dir=data_dir, sport=sport, accounts=accounts,
                grouper=develop.EventClassGrouper({r["id"]: r for r in news}),
                method="event_class", cache=False,
            )
            method = "developments (event_class stand-in, no key)"
    elif has_key:
        candidates = match.generate_candidates(data_dir=data_dir, sport=sport, accounts=accounts)
        print(f"[match]     candidates={len(candidates)}")
        stories = match.resolve_stories(data_dir=data_dir, sport=sport, accounts=accounts)
        method = "llm"
    else:
        candidates = match.generate_candidates(data_dir=data_dir, sport=sport, accounts=accounts)
        print(f"[match]     candidates={len(candidates)}")
        # Stand-in until the key arrives: exact-player auto-accept, never cached.
        stories = match.resolve_stories(
            data_dir=data_dir, sport=sport, accounts=accounts,
            adjudicator=match.ExactNameAdjudicator(),
            method="exact_name", cache=False,
        )
        method = "exact_name (stand-in, no key)"
    matched = sum(1 for s in stories if s["status"] == "matched")
    print(f"[resolve]   stories={len(stories)} matched={matched} method={method}")

    gw = meta.get("game_windows")
    if gw and gw.get("source") == "espn":
        n = games.refresh_games(data_dir, int(gw.get("season") or datetime.now().year))
        print(f"[games]     {n} games")

    agg = aggregate.build_aggregates(data_dir=data_dir, docs_data_dir=docs_dir,
                                     season_weeks=meta.get("season_weeks"),
                                     rotowire_handle=accounts["rotowire"]["handle"],
                                     milestones=cfg.get("milestones"),
                                     game_windows=gw,
                                     analysis_start=meta.get("analysis_start"),
                                     eras=meta.get("eras"))
    s = agg["summary"]
    print(
        f"[aggregate] matched={s['matched']} rw_first_rate={s['rotowire_first_rate']} "
        f"median_lead={s['median_lead_seconds']}s gaps(rw={s['rotowire_only']}, ud={s['underdog_only']})"
    )
    return agg


def _generated_at(sport: str, aggs: dict[str, dict]) -> str | None:
    """generated_at from this run's aggregates, falling back to the on-disk file so a
    single-sport run does not drop the other sports' timestamps from the index."""
    if sport in aggs:
        return aggs[sport].get("generated_at")
    path = sport_docs_dir(sport) / "aggregates.json"
    if path.exists():
        try:
            return json.loads(path.read_text()).get("generated_at")
        except Exception:
            return None
    return None


def _write_sports_index(cfg: dict, aggs: dict[str, dict]) -> None:
    """Write docs/data/sports.json so the dashboard can build its sport switcher.

    Always lists ALL active sports (not just those run this invocation) so a single-sport
    run doesn't drop the others. Each entry carries the sport's label, its two account
    handles (so tweet links point at the right accounts), and its aggregate generated_at.
    """
    DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    entries = []
    for key in sports(cfg):
        meta = cfg["sports"][key]
        accounts = meta["accounts"]
        entries.append({
            "key": key,
            "label": meta.get("label", key.upper()),
            "handles": {
                "rotowire": accounts["rotowire"]["handle"],
                "underdog": accounts["underdog"]["handle"],
            },
            "generated_at": _generated_at(key, aggs),
        })
    index = {"generated_at": now_iso(), "sports": entries}
    path = DOCS_DATA_DIR / "sports.json"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2))
    tmp.replace(path)


def run(sport: str | None = None) -> dict[str, dict]:
    """Run the pipeline for one sport or all active sports. Returns {sport: aggregates}."""
    cfg = load_config()
    sport_keys = [sport] if sport else sports(cfg)
    aggs: dict[str, dict] = {}
    for key in sport_keys:
        aggs[key] = run_sport(key, cfg)
    _write_sports_index(cfg, aggs)
    return aggs


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Run the daily pipeline for one or all sports.")
    ap.add_argument("--sport", default=None,
                    help="Run a single sport key (default: all active sports).")
    args = ap.parse_args()
    run(sport=args.sport)
