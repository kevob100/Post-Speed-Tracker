"""Speed and coverage by who was on the news desk (data/<sport>/staffing/YYYY-MM-DD.yaml).

A schedule file is human-owned (the pipeline never writes it) and lists, for each hour of
one day, who held each desk role:

  date: '2026-10-04'
  timezone: America/New_York
  roles: {tweets: Tweets (L1-2), distributor: Distributor (L3-5), writers: Writers}
  slots:
  - {start: '13:00', tweets: [{name: Adam, until: '13:50'}, Cullum], ...}

A role lists names: plain names share the whole hour; {name, until} holds the role until
that time and hands it to the names after it.

Every story is credited to whoever held each role when the news broke (its earliest post
on either feed, in the schedule's timezone), so a story Underdog posted first counts
against the desk that was on when Underdog posted it. A shared hour credits every person
on it. Hours the schedule does not cover are left out.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .store import parse_dt


def load_schedules(data_dir: Path) -> list[dict]:
    """Every schedule file under data_dir/staffing, oldest first."""
    folder = data_dir / "staffing"
    if not folder.exists():
        return []
    out = []
    for path in sorted(folder.glob("*.yaml")):
        sched = yaml.safe_load(path.read_text()) or {}
        if sched.get("slots"):
            sched["date"] = str(sched.get("date") or path.stem)
            out.append(sched)
    return out


def on_duty(entries: list, hhmm: str) -> list[str]:
    """Names holding a role at local time hhmm within one slot's entry list."""
    names: list[str] = []
    handed_off = False
    for e in entries or []:
        if isinstance(e, dict):
            if hhmm < str(e.get("until") or "99:99"):
                return [str(e["name"])]
            handed_off = True
            names = []          # whoever follows a hand-off takes the rest of the hour
        else:
            names.append(str(e))
    return names if names or not handed_off else []


def _slot_for(sched: dict, local: datetime) -> dict | None:
    for s in sched["slots"]:
        if int(str(s["start"])[:2]) == local.hour:
            return s
    return None


def credit(stories: list[dict], schedules: list[dict], story_time) -> list[tuple[dict, dict, dict]]:
    """(story, schedule, {role: [names]}) for each story that falls inside a scheduled hour."""
    by_date = {s["date"]: s for s in schedules}
    out = []
    for st in stories:
        t = story_time(st)
        for sched in schedules:
            tz = ZoneInfo(sched.get("timezone") or "America/New_York")
            local = t.astimezone(tz)
            if local.date().isoformat() != sched["date"]:
                continue
            slot = _slot_for(by_date[sched["date"]], local)
            if slot is None:
                break
            hhmm = local.strftime("%H:%M")
            roles = {r: on_duty(slot.get(r), hhmm) for r in (sched.get("roles") or {})}
            out.append((st, sched, roles))
            break
    return out


def _summary_plus(summary, sts: list[dict]) -> dict:
    """summary() plus how many of the Underdog-only gaps are borderline posts (highlight
    clips, in-game notes) so a reader can discount them."""
    out = summary(sts)
    out["underdog_only_borderline"] = sum(
        1 for s in sts if s.get("status") == "underdog_only" and not s.get("same_event_duplicate")
        and not s.get("roundup") and s.get("gap_kind") not in ("update", "not_fantasy", "not_covered")
        and s.get("review_status") not in ("rejected", "merged")
        and (s.get("underdog") or {}).get("borderline"))
    return out


def rollup(stories: list[dict], schedules: list[dict], story_time, summary,
           tweets: list[dict] | None = None, rotowire_handle: str | None = None) -> dict:
    """Per day: an hour-by-hour line and a per-role, per-person table, plus all days pooled.

    summary is aggregate._summary (same metric rules as the rest of the dashboard). With
    tweets, every row also carries rotowire_posts / underdog_posts: the news posts each
    account made while that person (or hour) was on, so a low match count can be read
    against how much news there was. Link replies and other non-news posts are left out.
    """
    base = summary
    summary = lambda sts: _summary_plus(base, sts)  # noqa: E731
    credited = credit(stories, schedules, story_time)
    news = [t for t in (tweets or []) if t.get("is_news") or t.get("excluded_reason") == "link_reply"]
    posted = credit(news, schedules, lambda t: parse_dt(t["created_at"]))

    def volume(posts: list[dict]) -> dict:
        rw = [t for t in posts if t.get("account") == rotowire_handle]
        ud = [t for t in posts if t.get("account") != rotowire_handle]
        link = lambda ts: sum(1 for t in ts if t.get("excluded_reason") == "link_reply")  # noqa: E731
        return {"rotowire_posts": len(rw) - link(rw), "underdog_posts": len(ud) - link(ud),
                "rotowire_links": link(rw)}
    days = []
    pooled: dict[str, dict[str, list[dict]]] = {}
    hours_by_person: dict[str, dict[str, float]] = {}
    for sched in schedules:
        tz = ZoneInfo(sched.get("timezone") or "America/New_York")
        roles = sched.get("roles") or {}
        mine = [(st, r) for st, sc, r in credited if sc is sched]
        my_posts = [(t, r) for t, sc, r in posted if sc is sched]
        slots = []
        for slot in sched["slots"]:
            h = int(str(slot["start"])[:2])
            in_slot = [st for st, _ in mine if story_time(st).astimezone(tz).hour == h]
            row = {"start": str(slot["start"]),
                   "roles": {r: [e["name"] if isinstance(e, dict) else e for e in slot.get(r) or []]
                             for r in roles},
                   "handoffs": {r: [f"{e['name']} until {e['until']}" for e in slot.get(r) or []
                                    if isinstance(e, dict)] for r in roles},
                   "story_ids": [st["story_id"] for st in in_slot]}
            row.update(summary(in_slot))
            if tweets is not None:
                row.update(volume([t for t, _ in my_posts
                                   if parse_dt(t["created_at"]).astimezone(tz).hour == h]))
            slots.append(row)
            for r in roles:
                for e in slot.get(r) or []:
                    name = e["name"] if isinstance(e, dict) else str(e)
                    # Hours on duty: a hand-off splits the hour at its time.
                    share = 1.0
                    if isinstance(e, dict):
                        mm = int(str(e["until"])[3:5])
                        share = mm / 60
                    elif any(isinstance(x, dict) for x in slot.get(r) or []):
                        until = max(int(str(x["until"])[3:5]) for x in slot.get(r) if isinstance(x, dict))
                        share = (60 - until) / 60
                    hours_by_person.setdefault(f"{sched['date']}|{r}|{name}", {"h": 0.0})["h"] += share
        people = {}
        for r in roles:
            per: dict[str, list[dict]] = {}
            for st, rr in mine:
                for name in rr.get(r, []):
                    per.setdefault(name, []).append(st)
                    pooled.setdefault(r, {}).setdefault(name, []).append(st)
            names = {k.split("|")[2] for k in hours_by_person if k.startswith(f"{sched['date']}|{r}|")}
            rows = []
            for name in sorted(names | set(per)):
                row = {"name": name,
                       "hours": round(hours_by_person.get(f"{sched['date']}|{r}|{name}", {"h": 0})["h"], 2)}
                row.update(summary(per.get(name, [])))
                if tweets is not None:
                    row.update(volume([t for t, rr in my_posts if name in rr.get(r, [])]))
                rows.append(row)
            people[r] = rows
        total = summary([st for st, _ in mine])
        if tweets is not None:
            total.update(volume([t for t, _ in my_posts]))
        days.append({"date": sched["date"], "timezone": sched.get("timezone") or "America/New_York",
                     "roles": roles, "slots": slots, "people": people, "total": total})

    roles_all: dict[str, str] = {}
    for sched in schedules:
        roles_all.update(sched.get("roles") or {})
    all_people = {}
    for r in roles_all:
        rows = []
        for name, sts in sorted((pooled.get(r) or {}).items()):
            hrs = sum(v["h"] for k, v in hours_by_person.items()
                      if k.split("|")[1] == r and k.split("|")[2] == name)
            row = {"name": name, "hours": round(hrs, 2)}
            row.update(summary(sts))
            if tweets is not None:
                row.update(volume([t for t, _, rr in posted if name in rr.get(r, [])]))
            rows.append(row)
        all_people[r] = rows
    return {"roles": roles_all, "days": days, "all": {"people": all_people,
                                                      "total": summary([st for st, _, _ in credited])}}
