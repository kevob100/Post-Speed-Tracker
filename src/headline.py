"""The Summary tab's numbers: the race with Underdog, and the chain that moves it.

The backbone (Nick, 2026-11-13): the headline is two numbers, the median gap behind
Underdog and the share of stories we post first. Below them, the chain that closes the
gap: templates write more posts -> template posts go out unedited -> step 3 (opened to
posted) gets faster -> we post sooner -> the gap shrinks. Then the three steps, so step 1
(arrival, mostly fixed by turbo) and step 2 (in Monitor to opened, the next operational
target) stay in view, plus the share added by hand (goal: none).

Every number is built for the last 7 and the last 30 days, each against the window of
the same length before it, so the page can switch between the two.

Template data comes from News Admin's x_post_edits rows (template field since RD-8168,
10/28). Step 3 and the gap by template are a sample: only the draft-vs-posted examples
(up to 20 a day) carry both a template and a post time to join on, until the desk
timeline rows carry the template themselves.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from pathlib import Path

from . import desk_summary as ds
from .store import load_jsonl, parse_dt

WINDOWS = (7, 30)
ROLL_DAYS = 90         # how far back the race trend goes
MIN_ROLL_N = 20        # stories needed before a rolling race point is drawn
JOIN_S = 2
DEFAULT_GOALS = {"template_share": 90, "template_unedited": 75, "automated": 67}


def _med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs)) if xs else None


def _pct(a, b):
    return round(100 * a / b, 1) if b else None


def _race_points(stories: list[dict]) -> list[tuple[datetime, float]]:
    m = [s for s in stories if s.get("status") == "matched" and s.get("rotowire") and s.get("underdog")
         and s.get("review_status") not in ("rejected", "merged")]
    return sorted((ds._at(s), ds._gap(s)) for s in m)


def _race(g: list[float]) -> dict:
    n, T = len(g), ds.TIE_SECONDS
    return {"n": n, "median": _med(g),
            "rw_first_pct": _pct(sum(x < -T for x in g), n),
            "tie_pct": _pct(sum(abs(x) <= T for x in g), n),
            "ud_first_pct": _pct(sum(x > T for x in g), n)}


def race(stories: list[dict], now: datetime) -> dict:
    pts = _race_points(stories)
    win = lambda a, b: [x for t, x in pts if a <= t < b]  # noqa: E731
    out = {f"d{d}": {"cur": _race(win(now - timedelta(days=d), now)),
                     "prior": _race(win(now - timedelta(days=2 * d), now - timedelta(days=d)))}
           for d in WINDOWS}
    # Daily rolling points, days midnight to midnight Central like the rest of the page:
    # [day, gap 7d, gap 30d, first% 7d, first% 30d].
    rows, today = [], now.astimezone(ds.TZ).date()
    d = today - timedelta(days=ROLL_DAYS)
    while d <= today:
        end = datetime(d.year, d.month, d.day, tzinfo=ds.TZ) + timedelta(days=1)
        pt = []
        for w in WINDOWS:
            r = _race(win(end - timedelta(days=w), end))
            pt.append(r if r["n"] >= MIN_ROLL_N else None)
        rows.append([d.isoformat(), *[r and r["median"] for r in pt], *[r and r["rw_first_pct"] for r in pt]])
        d += timedelta(days=1)
    out["rolling"] = rows
    return out


def _tpl_sum(rows: list[dict]) -> dict:
    t = [r for r in rows if r["template"] != "freeform"]
    f = [r for r in rows if r["template"] == "freeform"]
    S = lambda rs, k: sum(r.get(k) or 0 for r in rs)  # noqa: E731
    tp, fp = S(t, "posts"), S(f, "posts")
    tu, fu = _pct(S(t, "unchanged"), S(t, "scored")), _pct(S(f, "unchanged"), S(f, "scored"))
    share = _pct(tp, tp + fp)
    return {"template_posts": tp, "freeform_posts": fp, "share": share,
            "template_unedited": tu, "freeform_unedited": fu,
            # template wrote it AND it went out unedited, as a share of every post
            "automated": _pct(S(t, "unchanged"), tp + fp) if tp + fp else None}


def templates(data_dir: Path, now: datetime) -> dict | None:
    rows = [r for r in load_jsonl(data_dir / "x_post_edits.jsonl") if r.get("template")]
    if not rows:
        return None
    # Whole UTC post days only: the nightly run lands just after midnight, so today holds
    # an hour of posts at most. Windows end at today, exclusive.
    today = now.date()
    in_days = lambda a, b: [r for r in rows if a.isoformat() <= r["day"] < b.isoformat()]  # noqa: E731
    out = {"since": min(r["day"] for r in rows)}
    for w in WINDOWS:
        end = today
        out[f"d{w}"] = {"cur": _tpl_sum(in_days(end - timedelta(days=w), end)),
                        "prior": _tpl_sum(in_days(end - timedelta(days=2 * w), end - timedelta(days=w)))}
    # Per day since the template field exists: [day, template posts, freeform posts,
    # share 7d, template unedited 7d, freeform unedited 7d, same three over 30d].
    series, d = [], parse_dt(out["since"] + "T00:00:00Z").date()
    while d <= today:
        day = [r for r in rows if r["day"] == d.isoformat()]
        if day or series:
            s = _tpl_sum(day)
            pt = [d.isoformat(), s["template_posts"], s["freeform_posts"]]
            for w in WINDOWS:
                r = _tpl_sum(in_days(d - timedelta(days=w - 1), d + timedelta(days=1)))
                pt += [r["share"], r["template_unedited"], r["freeform_unedited"]]
            series.append(pt)
        d += timedelta(days=1)
    # Today is partial until the nightly run; leave it off the trend.
    out["daily"] = [p for p in series if p[0] < today.isoformat()]
    return out


def steps(rows: list[dict], manual_pct, now: datetime) -> dict:
    from .desk_speed import _steps  # local: desk_speed imports this module

    def win(a, b):
        r = [x for x in rows if a <= parse_dt(x["posted_to_x_at"]) < b]
        st = _steps(r)
        mon = [x for x in r if not x.get("manual")]
        return {"s1": st["sent_to_monitor"]["median"], "s2": st["monitor_to_open"]["median"],
                "s3": st["open_to_x"]["median"], "total": _med([x.get("tweet_to_x_seconds") for x in mon]),
                "n": st["open_to_x"]["n"], "hand_pct": manual_pct(a, b)["value"]}
    return {f"d{w}": {"cur": win(now - timedelta(days=w), now),
                      "prior": win(now - timedelta(days=2 * w), now - timedelta(days=w))} for w in WINDOWS}


def by_template(data_dir: Path, rows: list[dict], stories: list[dict], now: datetime) -> dict:
    """Step 3 and the gap behind Underdog, template vs freeform, on the sampled posts."""
    ex = [e for e in load_jsonl(data_dir / "x_post_examples.jsonl") if e.get("template") and e.get("posted_at")]
    tl = {int(parse_dt(r["posted_to_x_at"]).timestamp()): r for r in rows}
    sm = {int(parse_dt(s["rotowire"]["created_at"]).timestamp()): s
          for s in stories if s.get("status") == "matched" and s.get("rotowire") and s.get("underdog")
          and s.get("review_status") not in ("rejected", "merged")}
    near = lambda m, t: next((m[t + d] for d in sorted(range(-JOIN_S, JOIN_S + 1), key=abs) if t + d in m), None)  # noqa: E731
    out = {}
    for w in WINDOWS:
        g = {"template": {"s3": [], "gap": []}, "freeform": {"s3": [], "gap": []}}
        for e in ex:
            t = parse_dt(e["posted_at"])
            if not now - timedelta(days=w) <= t < now:
                continue
            k = "freeform" if e["template"] == "freeform" else "template"
            r, s = near(tl, int(t.timestamp())), near(sm, int(t.timestamp()))
            if r and not r.get("manual") and r.get("open_to_x_seconds") is not None:
                g[k]["s3"].append(r["open_to_x_seconds"])
            if s:
                g[k]["gap"].append(ds._gap(s))
        out[f"d{w}"] = {k: {"s3": _med(v["s3"]), "s3_n": len(v["s3"]), "gap": _med(v["gap"]), "gap_n": len(v["gap"])}
                        for k, v in g.items()}
    return out


def build(cfg: dict, data_dir: Path, rows: list[dict], stories: list[dict], manual_pct, now: datetime) -> dict:
    return {
        "windows": list(WINDOWS),
        "goals": {**DEFAULT_GOALS, **(cfg.get("goals") or {})},
        "race": race(stories, now),
        "templates": templates(data_dir, now),
        "steps": steps(rows, manual_pct, now),
        "by_template": by_template(data_dir, rows, stories, now),
    }
