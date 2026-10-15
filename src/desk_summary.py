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
TZ = ZoneInfo("America/Chicago")  # the page shows Central time


def _med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs)) if xs else None


def _gap(s: dict) -> float:
    return (parse_dt(s["rotowire"]["created_at"]) - parse_dt(s["underdog"]["created_at"])).total_seconds()


def _at(s: dict) -> datetime:
    return min(parse_dt(s["rotowire"]["created_at"]), parse_dt(s["underdog"]["created_at"]))


def margin(stories: list[dict], now: datetime) -> dict:
    """Typical margin (median per-story gap; positive = Underdog ahead) for the last 30 days
    and the 30 before, and for each of the last 90 days: rolling 7- and 30-day medians, then
    that day's own median and story count."""
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
    today = now.astimezone(TZ).date()
    d = today - timedelta(days=90)
    while d <= today:  # days run midnight to midnight Central, like every time on the page
        end = datetime(d.year, d.month, d.day, tzinfo=TZ) + timedelta(days=1)
        r7 = [x for t, x in pts if end - timedelta(days=7) <= t < end]
        r30 = [x for t, x in pts if end - timedelta(days=30) <= t < end]
        r1 = [x for t, x in pts if end - timedelta(days=1) <= t < end]
        rolling.append([d.isoformat(), _med(r7) if len(r7) >= 30 else None, _med(r30) if len(r30) >= 30 else None,
                        _med(r1) if r1 else None, len(r1)])
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
    ini = _initiative_issues(data_dir)
    for i in ini.get("issues", []):  # the nightly initiative pull is the fresher source
        stored[i["key"]] = {**stored.get(i["key"], {}), "summary": i["summary"], "status": i["status"], "category": i["category"]}
    fetched = ini.get("fetched_at") or fetched
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
        "initiative": initiative(data_dir, cfg.get("initiative") or {}),
    }


# ---------------------------------------------------------------- initiative breakdown

INITIATIVE_FILE = "jira_initiative.json"
FIELDS = "summary,status,issuetype,parent,assignee,customfield_10010,priority,resolutiondate"


def _jira_search(jql: str) -> list[dict]:
    email, token = os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"]
    auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
    out, page = [], None
    while True:
        q = {"jql": jql, "fields": FIELDS, "maxResults": 100}
        if page:
            q["nextPageToken"] = page
        req = urllib.request.Request(f"{JIRA}/rest/api/3/search/jql?{urllib.parse.urlencode(q)}",
                                     headers={"Authorization": auth, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.loads(r.read())
        out += body.get("issues", [])
        page = body.get("nextPageToken")
        if not page or body.get("isLast"):
            return out


def _slim(i: dict) -> dict:
    f = i["fields"]
    return {"key": i["key"], "summary": f["summary"], "type": f["issuetype"]["name"],
            "parent": (f.get("parent") or {}).get("key"), "status": f["status"]["name"],
            "category": f["status"]["statusCategory"]["key"],
            "assignee": (f.get("assignee") or {}).get("displayName"),
            "priority": (f.get("priority") or {}).get("name"), "resolved": f.get("resolutiondate"),
            "sprints": [{"id": sp.get("id"), "name": sp.get("name"), "state": sp.get("state"),
                         "start": sp.get("startDate"), "end": sp.get("endDate")} for sp in f.get("customfield_10010") or []]}


def sync_initiative(data_dir: Path, root: str) -> str:
    """Every epic under the initiative and every ticket under those epics, read-only."""
    if not (os.environ.get("JIRA_EMAIL") and os.environ.get("JIRA_API_TOKEN")):
        return "no JIRA_EMAIL / JIRA_API_TOKEN, kept stored initiative"
    epics = [_slim(i) for i in _jira_search(f"parent = {root} ORDER BY key ASC")]
    kids = [_slim(i) for i in _jira_search(f"parent in ({','.join(e['key'] for e in epics)}) ORDER BY key ASC")] if epics else []
    (data_dir / INITIATIVE_FILE).write_text(json.dumps(
        {"fetched_at": now_iso(), "root": root, "issues": epics + kids}, ensure_ascii=False, indent=1))
    return f"{len(epics)} epics, {len(kids)} tickets"


def _initiative_issues(data_dir: Path) -> dict:
    p = data_dir / INITIATIVE_FILE
    return json.loads(p.read_text()) if p.exists() else {}


def initiative(data_dir: Path, cfg: dict) -> dict | None:
    """Epics under the initiative with their tickets split three ways. Prioritized means
    assigned to the dev who builds this work and in an open sprint (active or next): that
    is the commitment for that sprint. Everything else not done is backlog."""
    ini = _initiative_issues(data_dir)
    if not ini.get("issues"):
        return None
    who = (cfg or {}).get("prioritize_assignee")
    issues = ini["issues"]
    epics = [i for i in issues if i["type"] == "Epic"]
    open_sprints: dict = {}
    rows = []
    for e in epics:
        kids = [i for i in issues if i["parent"] == e["key"]]
        pri, back, done = [], [], []
        for k in kids:
            sp = [x for x in k["sprints"] if x["state"] in ("active", "future")]
            t = {"key": k["key"], "summary": k["summary"], "type": k["type"], "status": k["status"],
                 "category": k["category"], "assignee": k["assignee"], "priority": k["priority"],
                 "resolved": k["resolved"], "url": f"{JIRA}/browse/{k['key']}"}
            if k["category"] == "done":
                done.append(t)
            elif sp and (not who or k["assignee"] == who):
                cur = sorted(sp, key=lambda x: (x["state"] != "active", x.get("start") or ""))[0]
                t["sprint"] = cur["name"]
                t["sprint_state"] = cur["state"]
                open_sprints[cur["name"]] = {"name": cur["name"], "state": cur["state"], "start": cur.get("start"), "end": cur.get("end")}
                pri.append(t)
            else:
                back.append(t)
        done.sort(key=lambda t: t["resolved"] or "", reverse=True)
        rows.append({"key": e["key"], "summary": e["summary"], "status": e["status"], "category": e["category"],
                     "url": f"{JIRA}/browse/{e['key']}", "prioritized": pri, "backlog": back, "done": done})
    rows.sort(key=lambda r: (r["category"] == "done", -len(r["prioritized"]), -len(r["backlog"]), r["key"]))
    return {"root": ini.get("root"), "label": (cfg or {}).get("label"), "fetched_at": ini.get("fetched_at"),
            "url": f"{JIRA}/browse/{ini.get('root')}", "assignee": who,
            "sprints": sorted(open_sprints.values(), key=lambda x: (x["state"] != "active", x.get("start") or "")),
            "epics": rows}
