"""One-off helper: resolve each account handle to its numeric X user_id and persist
it back into config.yaml.

Collection keys timelines by user_id (not handle), so this must be run once before the
first collect when an account's user_id is missing. It is idempotent — re-running simply
re-confirms the ids. Requires the X app credentials (see .env.example).

Run: python -m src.resolve_ids
"""
from __future__ import annotations

from .config import load_config, save_config
from .xapi import XClient


def resolve_ids(force: bool = False) -> dict:
    """Resolve every sport account's handle to its numeric user_id and persist.

    Skips accounts that already have a user_id (unless force=True) to avoid re-hitting
    the API across all sports. Idempotent.
    """
    cfg = load_config()
    client = XClient()
    resolved: dict[str, str] = {}
    for sport, meta in cfg["sports"].items():
        for role, acct in meta["accounts"].items():
            handle = acct["handle"]
            if acct.get("user_id") and not force:
                continue
            uid = client.resolve_username(handle)
            acct["user_id"] = uid
            resolved[handle] = uid
    save_config(cfg)
    return resolved


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Resolve account handles to user_ids for all sports.")
    ap.add_argument("--force", action="store_true", help="Re-resolve even accounts that already have a user_id.")
    args = ap.parse_args()
    result = resolve_ids(force=args.force)
    if not result:
        print("All account user_ids already resolved (use --force to re-resolve).")
    for handle, uid in result.items():
        print(f"@{handle} -> {uid}")
