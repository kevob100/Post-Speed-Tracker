"""Why editors changed template drafts (NFL).

Each template-written draft that went out edited gets one reason, judged once by the model
from the draft and the posted text, and cached by article id:

  template     right template and case, but its wording is off or it leaves out a detail the
               source gave (fix: change the template)
  wrong_match  the source called for a different template or case, or none at all
               (fix: the matching rules)
  style        same facts and meaning, reworded to taste (fix: train the desk / style guide)
  other        anything else, e.g. a typo or a fact the source didn't have

Only the draft-vs-posted examples carry the text, and News Admin returns at most 20 a day,
so a busy day is a sample: `coverage` says how many of the day's edited template posts were
judged.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from .config import env, load_config
from .store import load_jsonl, parse_dt, write_jsonl

CACHE = "template_edit_reasons.jsonl"
REASONS = ("template", "wrong_match", "style", "other")
LABELS = {"template": "Template wording", "wrong_match": "Wrong template", "style": "Style only", "other": "Other"}

SYSTEM = (
    "You review NFL news posts for RotoWire. A template wrote a draft post from a source tweet, "
    "and an editor changed it before posting. You get the template name and case, the draft and "
    "what was posted. Decide WHY the editor changed it, picking exactly one reason:\n\n"
    "template: the template and case were the right ones, but the template's wording is wrong or "
    "awkward, or it leaves out a detail that belongs in the post, including the player's full name such as a missing Jr. (e.g. the editor wrote 'is being "
    "evaluated for a concussion and is questionable to return' where the draft had '(concussion) "
    "is questionable to return', or added the quarter he left in). The fix is to change the "
    "template.\n"
    "wrong_match: the news called for a different template or case, or no template (e.g. the "
    "draft says he returned but the post says he was cleared to return; the draft says "
    "questionable but he was ruled out; the draft's status or injury is wrong). The fix is the "
    "rules that pick the template.\n"
    "style: the facts and meaning are the same and nothing was missing; the editor only reworded "
    "it to taste (word order, 'for Sunday' vs 'Sunday', '(ankle)' vs 'due to an ankle injury', "
    "'headed' vs 'has gone'). The fix is training the desk to send good drafts as they are.\n"
    "other: anything else (a typo fix, a correction the template could not have known).\n\n"
    "If the editor both added a missing detail and reworded, pick template. If a fact changed, "
    "pick wrong_match. Never use double quotes inside why; use single quotes. Respond with JSON ONLY, no prose, no fences: "
    '{"reason": "template|wrong_match|style|other", "why": "<under 15 words: what the editor changed>"}'
)


def _prompt(e: dict) -> str:
    return (f"Template: {e.get('template')} / case: {e.get('template_case')} "
            f"(check: {e.get('template_check') or 'n/a'})\n\n"
            f"Draft:\n{e.get('draft')}\n\nPosted:\n{e.get('posted')}")


def _parse(raw: str) -> dict:
    text = (raw or "").strip().strip("`")
    if text.lower().startswith("json"):
        text = text[4:]
    try:
        d = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except Exception:  # usually unescaped quotes inside "why": read the two fields directly
        m = re.search(r'"reason"\s*:\s*"(\w+)"', text)
        if not m:
            return {"reason": "other", "why": "could not classify", "parse_error": True}
        w = re.search(r'"why"\s*:\s*"(.*)"\s*}', text, re.DOTALL)
        d = {"reason": m.group(1), "why": w.group(1) if w else ""}
    r = d.get("reason") if d.get("reason") in REASONS else "other"
    return {"reason": r, "why": str(d.get("why") or "")[:160]}


def _edited_template(e: dict) -> bool:
    return e.get("template") not in (None, "freeform") and (e.get("distance") or 0) > 0


def classify(data_dir: Path, client=None, limit: int = 150) -> str:
    """Judge every cached example not judged yet. Needs ANTHROPIC_API_KEY."""
    path = data_dir / CACHE
    done = {r["article_id"]: r for r in load_jsonl(path) if not r.get("parse_error")}
    todo = [e for e in load_jsonl(data_dir / "x_post_examples.jsonl")
            if _edited_template(e) and e["article_id"] not in done][:limit]
    if not todo:
        return f"template edits: nothing new ({len(done)} judged)"
    if client is None:
        from anthropic import Anthropic
        client = Anthropic(api_key=env("ANTHROPIC_API_KEY"), max_retries=6)
    model = load_config()["llm"]["model"]
    for e in todo:
        resp = client.messages.create(model=model, max_tokens=200, system=SYSTEM,
                                      messages=[{"role": "user", "content": _prompt(e)}])
        v = _parse(resp.content[0].text)
        done[e["article_id"]] = {"article_id": e["article_id"], "posted_at": e["posted_at"],
                                 "template": e.get("template"), "template_case": e.get("template_case"),
                                 "manual": e.get("manual"), "draft": e.get("draft"), "posted": e.get("posted"),
                                 "distance": e.get("distance"), **v}
    write_jsonl(path, sorted(done.values(), key=lambda r: r["posted_at"]))
    return f"template edits: judged {len(todo)} new ({len(done)} total)"


def _edited_total(data_dir: Path, a: datetime, b: datetime) -> int:
    """Edited template posts News Admin counted in [a, b), by UTC post day."""
    rows = [r for r in load_jsonl(data_dir / "x_post_edits.jsonl")
            if r.get("template") not in (None, "freeform") and a.date().isoformat() <= r["day"] < b.date().isoformat()]
    return sum((r.get("scored") or 0) - (r.get("unchanged") or 0) for r in rows)


def window(data_dir: Path, a: datetime, b: datetime) -> dict:
    """Counts by reason for posts in [a, b), plus how many edited template posts there were."""
    rows = [r for r in load_jsonl(data_dir / CACHE) if a <= parse_dt(r["posted_at"]) < b]
    counts = {k: sum(r["reason"] == k for r in rows) for k in REASONS}
    return {"judged": len(rows), "edited": max(len(rows), _edited_total(data_dir, a, b)), **counts}


def build(data_dir: Path, now: datetime, windows=(7, 30)) -> dict | None:
    rows = load_jsonl(data_dir / CACHE)
    if not rows:
        return None
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    out = {"labels": LABELS}
    for w in windows:
        out[f"d{w}"] = window(data_dir, end - timedelta(days=w), end)
        out[f"p{w}"] = window(data_dir, end - timedelta(days=2 * w), end - timedelta(days=w))  # the window before, for the trend
    # Last four 7-day blocks, oldest first: the reason mix week by week.
    out["weeks"] = [{"start": (end - timedelta(days=7 * (i + 1))).date().isoformat(),
                     **window(data_dir, end - timedelta(days=7 * (i + 1)), end - timedelta(days=7 * i))}
                    for i in reversed(range(4))]
    recent = [r for r in rows if parse_dt(r["posted_at"]) >= end - timedelta(days=max(windows))]
    out["posts"] = [{k: r.get(k) for k in ("posted_at", "template", "template_case", "manual", "draft", "posted", "reason", "why")}
                    for r in sorted(recent, key=lambda r: r["posted_at"], reverse=True)]
    return out


def recap_line(data_dir: Path, a: datetime, b: datetime) -> str | None:
    w = window(data_dir, a, b)
    if not w["judged"]:
        return None
    parts = [f"{w[k]} {LABELS[k].lower()}" for k in REASONS if w[k]]
    sample = f" (judged {w['judged']} of {w['edited']})" if w["judged"] < w["edited"] else ""
    return f"Why template drafts were edited: {' · '.join(parts)}{sample}"
