"""The leadership summary on top of Desk Speed: is the margin moving, and is the work behind it paying off.

Three things, all from data the pipeline already holds:

- margin: the typical per-story gap to Underdog over the last 30 days against the 30 days
  before, plus rolling 7- and 30-day medians for the chart.
- turbo: News Admin scans NFL sources every 10s instead of 30s in fixed ET windows (its
  config/scraper.php -> nfl_scan_schedule, mirrored in config.yaml). Step 1 and the margin
  are split by whether a tweet landed inside a window, since turbo went live.
- changes: each shipped change in config.yaml, with the metric(s) it targets measured over
  the 14 days before it went live and since. Fewer than 5 days since is "too soon".

And the roadmap: the tickets behind each step, with live Jira status when JIRA_EMAIL and
JIRA_API_TOKEN are set, else the last statuses stored in data/<sport>/jira_tickets.json.
"""
from __future__ import annotations

import base64
import json
import os
import statistics
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .store import load_jsonl, now_iso, parse_dt

JIRA = "https://gdcgroup.atlassian.net"
BEFORE_DAYS = 14
MIN_AFTER_DAYS = 5
TIE_SECONDS = 60


def _med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs)) if xs else None


def _gap(s: dict) -> float:
    return (parse_dt(s["rotowire"]["created_at"]) - parse_dt(s["underdog"]["created_at"])).total_seconds()


def _at(s: dict) -> datetime:
    return min(parse_dt(s["rotowire"]["created_at"]), parse_dt(s["underdog"]["created_at"]))


def margin(stories: list[dict], now: datetime) -> dict:
    """Typical margin (median per-story gap; positive = Underdog ahead) for the last 30 days
    and the 30 before, and daily rolling 7- and 30-day medians for the last 90 days."""
    m = [s for s in stories if s.get("status") == "matched" and s.get("rotowire") and s.get("underdog")]
    pts = sorted((_at(s), _gap(s)) for s in m)

    def window(a, b):
        g = [x for t, x in pts if a <= t < b]
        n = len(g)
        return {"n": n, "median": _med(g),
                "rw_first_pct": round(100 * sum(x < -TIE_SECONDS for x in g) / n, 1) if n else None,
                "tie_pct": round(100 * sum(abs(x) <= TIE_SECONDS for x in g) / n, 1) if n else None,
                "ud_first_pct": round(100 * sum(x > TIE_SECONDS for x in g) / n, 1) if n else None}

    rolling = []
    d = (now - timedelta(days=90)).date()
    while d <= now.date():
        end = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1)
        r7 = [x for t, x in pts if end - timedelta(days=7) <= t < end]
        r30 = [x for t, x in pts if end - timedelta(days=30) <= t < end]
        rolling.append([d.isoformat(), _med(r7) if len(r7) >= 15 else None, _med(r30) if len(r30) >= 30 else None])
        d += timedelta(days=1)
    return {"last30": window(now - timedelta(days=30), now),
            "prior30": window(now - timedelta(days=60), now - timedelta(days=30)),
            "rolling": rolling}


def in_windows(t: str, turbo: dict) -> bool:
    tz = ZoneInfo(turbo.get("timezone", "America/New_York"))
    d = parse_dt(t).astimezone(tz)
    hm = d.strftime("%H:%M")
    # config days follow Carbon / JS: 0 = Sunday
    dow = (d.weekday() + 1) % 7
    return any(int(w["day"]) == dow and w["start"] <= hm < w["end"] for w in turbo.get("windows", []))


def turbo_split(rows: list[dict], stories: list[dict], turbo: dict) -> dict | None:
    if not turbo or not turbo.get("since"):
        return None
    since = turbo["since"]
    mon = [r for r in rows if not r.get("manual") and r.get("arrival_seconds") is not None
           and r.get("tweet_posted_at", "") >= since]
    m = [s for s in stories if s.get("status") == "matched" and s.get("rotowire") and s.get("underdog")
         and s["underdog"]["created_at"] >= since]
    side = lambda on: {  # noqa: E731
        "arrival": {"median": _med([r["arrival_seconds"] for r in mon if in_windows(r["tweet_posted_at"], turbo) == on]),
                    "n": sum(1 for r in mon if in_windows(r["tweet_posted_at"], turbo) == on)},
        "margin": {"median": _med([_gap(s) for s in m if in_windows(s["underdog"]["created_at"], turbo) == on]),
                   "n": sum(1 for s in m if in_windows(s["underdog"]["created_at"], turbo) == on)},
    }
    return {"since": since, "seconds": turbo.get("seconds"), "normal_seconds": turbo.get("normal_seconds"),
            "windows": turbo.get("windows"), "timezone": turbo.get("timezone"), "on": side(True), "off": side(False)}


def _metric(name: str, a: datetime, b: datetime, rows: list[dict], stories: list[dict],
            edit_rows: list[dict], turbo: dict, manual_pct) -> dict:
    """One metric over [a, b): its value and how many items it rests on."""
    ina = lambda t: t and a <= parse_dt(t) < b  # noqa: E731
    if name == "arrival_turbo":
        xs = [r["arrival_seconds"] for r in rows if not r.get("manual") and r.get("arrival_seconds") is not None
              and ina(r.get("tweet_posted_at")) and in_windows(r["tweet_posted_at"], turbo)]
        return {"value": _med(xs), "n": len(xs), "unit": "s"}
    if name == "arrival":
        xs = [r["arrival_seconds"] for r in rows if not r.get("manual") and r.get("arrival_seconds") is not None
              and ina(r.get("tweet_posted_at"))]
        return {"value": _med(xs), "n": len(xs), "unit": "s"}
    if name == "open_to_x":
        xs = [r["open_to_x_seconds"] for r in rows if not r.get("manual") and r.get("open_to_x_seconds") is not None
              and ina(r.get("posted_to_x_at"))]
        return {"value": _med(xs), "n": len(xs), "unit": "s"}
    if name in ("edited_pct", "change_pct"):
        g = [r for r in edit_rows if a.date().isoformat() <= r["day"] < b.date().isoformat()]
        sc = sum(r["scored"] for r in g)
        if not sc:
            return {"value": None, "n": 0, "unit": "%"}
        if name == "edited_pct":
            v = 100 * (sc - sum(r["unchanged"] for r in g)) / sc
        else:
            v = 100 * sum((r["mean_distance"] or 0) * r["scored"] for r in g) / sc
        return {"value": round(v, 1), "n": sc, "unit": "%"}
    if name == "margin":
        xs = [_gap(s) for s in stories if s.get("status") == "matched" and s.get("rotowire") and s.get("underdog")
              and a <= _at(s) < b]
        return {"value": _med(xs), "n": len(xs), "unit": "s"}
    if name == "manual_pct":
        return manual_pct(a, b)
    raise ValueError(f"unknown metric {name}")


def changes(cfg_changes: list[dict], now: datetime, **data) -> list[dict]:
    out = []
    for c in cfg_changes or []:
        live = datetime.fromisoformat(c["live"]).replace(tzinfo=timezone.utc)
        days_since = (now - live).days
        metrics = []
        for m in c["metrics"]:
            before = _metric(m["metric"], live - timedelta(days=BEFORE_DAYS), live, **data)
            after = _metric(m["metric"], live, now, **data) if days_since >= MIN_AFTER_DAYS else None
            metrics.append({"metric": m["metric"], "label": m["label"], "main": bool(m.get("main")),
                            "unit": before["unit"], "before": before, "after": after})
        out.append({"key": c["key"], "label": c["label"], "live": c["live"], "days_since": days_since,
                    "too_soon": days_since < MIN_AFTER_DAYS, "metrics": metrics})
    return out


# ---------------------------------------------------------------- tickets

def sync_tickets(data_dir: Path, keys: list[str]) -> str:
    """Refresh ticket statuses from Jira (REST, read-only) when credentials are set."""
    email, token = os.environ.get("JIRA_EMAIL"), os.environ.get("JIRA_API_TOKEN")
    if not (email and token and keys):
        return "no JIRA_EMAIL / JIRA_API_TOKEN, kept stored statuses"
    q = urllib.parse.urlencode({"jql": f"key in ({','.join(keys)})", "fields": "summary,status,resolutiondate",
                                "maxResults": 100})
    req = urllib.request.Request(f"{JIRA}/rest/api/3/search/jql?{q}", headers={
        "Authorization": "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode(),
        "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        issues = json.loads(r.read())["issues"]
    stored = {t["key"]: t for t in _stored(data_dir)}
    for i in issues:
        f = i["fields"]
        stored[i["key"]] = {"key": i["key"], "summary": f["summary"], "status": f["status"]["name"],
                            "category": f["status"]["statusCategory"]["key"], "resolved": f.get("resolutiondate")}
    (data_dir / "jira_tickets.json").write_text(json.dumps(
        {"fetched_at": now_iso(), "tickets": sorted(stored.values(), key=lambda t: t["key"])}, indent=2))
    return f"{len(issues)} ticket statuses"


def _stored(data_dir: Path) -> list[dict]:
    p = data_dir / "jira_tickets.json"
    return json.loads(p.read_text()).get("tickets", []) if p.exists() else []


def roadmap(data_dir: Path, cfg_roadmap: list[dict]) -> dict:
    stored = {t["key"]: t for t in _stored(data_dir)}
    fetched = json.loads((data_dir / "jira_tickets.json").read_text()).get("fetched_at") \
        if (data_dir / "jira_tickets.json").exists() else None
    steps = []
    for st in cfg_roadmap or []:
        items = []
        for t in st.get("tickets", []):
            s = stored.get(t["key"], {})
            items.append({"key": t["key"], "title": t.get("title") or s.get("summary"), "note": t.get("note"),
                          "replaces": t.get("replaces"),
                          "status": s.get("status"), "category": s.get("category"), "url": f"{JIRA}/browse/{t['key']}"})
        for n in st.get("no_ticket", []):
            items.append({"key": None, "title": n, "status": "No ticket", "category": "none"})
        steps.append({"step": st["step"], "label": st["label"], "items": items})
    return {"fetched_at": fetched, "steps": steps}


def build(cfg: dict, data_dir: Path, rows: list[dict], stories: list[dict], manual_pct, now: datetime) -> dict:
    turbo = cfg.get("turbo") or {}
    edit_rows = load_jsonl(data_dir / "x_post_edits.jsonl")
    m = [s for s in stories if s.get("rotowire")]
    return {
        "margin": margin(m, now),
        "turbo": turbo_split(rows, m, turbo),
        "changes": changes(cfg.get("changes"), now, rows=rows, stories=m, edit_rows=edit_rows,
                           turbo=turbo, manual_pct=manual_pct),
        "roadmap": roadmap(data_dir, cfg.get("roadmap")),
    }
