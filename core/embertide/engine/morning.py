"""The morning after a night: what is waiting for the person (suggestions for tonight), and which morning cards they
have put away. Shared by the morning notification, the review notification and the app's morning cards."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .rhythm import night_date

CARD_RE = re.compile(r"(night|review|sugg)-(\d{4}-\d{2}-\d{2})")
KEEP_DAYS = 14


def last_night(c, now: float):
    """The night that just ended: the one that began yesterday evening (after noon), or last evening (before noon)."""
    t = datetime.fromtimestamp(now, c.clock.tz)
    return night_date(t) - timedelta(days=1) if t.hour >= 12 else night_date(t)


def waiting_suggestions(c, p, now: float) -> list[dict]:
    """Suggestions for tonight that the person hasn't put away: Autopilot's (on their cadence, one per period) and the
    tuning guide's bedtime / wake changes. Each: id, source ("suggestion" | "guide"), title, detail, proposal."""
    out: list[dict] = []
    try:
        from ..autopilot_api import _release, _suggest
        for s in _release(c, p, _suggest(c, p))["items"]:
            out.append({"id": s["id"], "source": "suggestion", "kind": s.get("kind"), "title": s.get("title") or "",
                        "detail": s.get("detail") or "", "proposal": s.get("proposal")})
    except Exception:  # noqa: BLE001  (a suggestion engine problem never stops the morning message)
        pass
    try:
        from .guide import tuning_guide
        gone = c.store.get(f"guide_dismissed:{p.id}", {}) or {}
        applied = c.store.get(f"applied_insights:{p.id}", {}) or {}
        for g in tuning_guide(c, p, now):
            prop = g.get("proposal") or {}
            if g.get("severity") != "suggest" or not prop.get("schedule") or g["id"] in applied:
                continue
            d = gone.get(g["id"])
            if d and d.get("sig") == str(prop.get("to", "")):
                continue
            out.append({"id": g["id"], "source": "guide", "kind": "guide", "title": g.get("title") or "",
                        "detail": g.get("detail") or "", "proposal": prop})
    except Exception:  # noqa: BLE001
        pass
    return out


def dismissed(c, pid: str) -> dict[str, float]:
    return c.store.get(f"morning_dismissed:{pid}", {}) or {}


def dismiss(c, pid: str, card: str, now: float) -> str:
    """Put one morning card away on every device ("night-<date>", "review-<date>", "sugg-<date>"). A put-away review
    is also hidden from the older review card. Returns the night's date; ValueError for anything else."""
    m = CARD_RE.fullmatch(card or "")
    if not m:
        raise ValueError("unknown card")
    kind, night = m.groups()
    keep = {k: v for k, v in dismissed(c, pid).items() if now - float(v) < KEEP_DAYS * 86400}
    keep[card] = now
    c.store.put(f"morning_dismissed:{pid}", keep)
    if kind == "review":
        c.store.put(f"ai_review_dismissed:{pid}:{night}", now)
    return night


REVIEW_WAIT_S = 15 * 60          # how long "Reviewing your night…" shows before giving up on a review that never came


def feed(c, p, now: float) -> dict:
    """The morning cards for Tonight: last night in brief (with the check-in), its review (ready, or still being
    written), and tonight's suggestions. Cards the person put away are left out; nothing when they were away."""
    from ..notify import summary_line
    from .away import is_away, night_verdict
    from .summary import summarize
    night = last_night(c, now)
    d = night.isoformat()
    out: dict = {"night": d, "cards": []}
    if is_away(c.store, p.id, now) or night_verdict(c, p, night)[0] == "away":
        return out
    gone = dismissed(c, p.id)
    s = summarize(c.store, p, night, c.clock.tz, now)
    if s and s.get("has_data") and s.get("in_bed_min") and not s.get("in_progress") and f"night-{d}" not in gone:
        fb = c.store.get(f"feedback:{p.id}:{d}") or {}
        out["cards"].append({"id": f"night-{d}", "kind": "night", "line": summary_line(s), "in_bed_min": s.get("in_bed_min"),
                             "settle_min": s.get("time_to_settle_min"), "times_up": s.get("times_up") or 0, "feel": fb.get("temp")})
    pol = p.settings.autopilot
    if pol.ai_review and f"review-{d}" not in gone and not c.store.get(f"ai_review_dismissed:{p.id}:{d}"):
        rv = c.store.get(f"ai_review:{p.id}:{d}") or {}
        started = c.store.get(f"ai_review_started:{p.id}:{d}")
        if rv.get("answer"):
            out["cards"].append({"id": f"review-{d}", "kind": "review", "state": "ready", "answer": rv["answer"],
                                 "model": rv.get("model"), "proposal": rv.get("proposal")})
        elif started and now - float(started) < REVIEW_WAIT_S:
            out["cards"].append({"id": f"review-{d}", "kind": "review", "state": "working"})
    sugg = waiting_suggestions(c, p, now)
    if sugg and f"sugg-{d}" not in gone:
        out["cards"].append({"id": f"sugg-{d}", "kind": "sugg", "items": sugg})
    return out


UNDO_DAYS = 7
FEEL_WORDS = {"cold": "too cold", "right": "just right", "warm": "too warm"}


def applied_night(c, a: dict) -> str | None:
    """The night an applied suggestion came from: the one that had just ended when it was applied. (The record's own
    "night" is the newest finished night it had seen, which can lag on a lie-in morning.)"""
    return last_night(c, float(a["ts"])).isoformat() if a.get("ts") else a.get("night")


def change_text(p, a: dict) -> str | None:
    """An applied change in the person's units and clock: "Weeknights · Late night: 81°F → 85°F", "Weeknights:
    bedtime 11:00 PM → 12:00 AM"."""
    from datetime import time as _time
    from ..units import clock_style, fmt_clock, fmt_temp
    prop = a.get("proposal") or {}
    rh = next((r for r in p.rhythms if r.id == prop.get("rhythm_id")), None)
    old = a.get("before") if a.get("before") is not None else prop.get("from")
    if rh and prop.get("schedule") in ("bedtime", "wake"):
        def t(v):
            try:
                hh, mm = str(v).split(":")
                return fmt_clock(_time(int(hh), int(mm)), clock_style(p.settings))
            except (ValueError, TypeError):
                return str(v)
        return f"{rh.name}: {prop['schedule']} {t(old) + ' → ' if old else ''}{t(prop.get('to'))}"
    ph = next((x for x in (rh.phases if rh else []) if x.id == prop.get("phase_id")), None)
    if rh and ph and prop.get("field") == "temp_c":
        u = p.settings.units
        return f"{rh.name} · {ph.name}: {fmt_temp(float(old), u) + ' → ' if old is not None else ''}{fmt_temp(float(prop['to']), u)}"
    return a.get("change")


def current_value(p, prop: dict):
    """What a proposal would change, as it is now (kept with an applied suggestion so it can be undone)."""
    for rh in p.rhythms:
        if rh.id != (prop or {}).get("rhythm_id"):
            continue
        if prop.get("schedule") in ("bedtime", "wake"):
            return getattr(rh.schedule, prop["schedule"])
        for ph in rh.phases:
            if ph.id == prop.get("phase_id") and prop.get("field"):
                return getattr(ph, prop["field"], None)
    return None


def basis(s: dict, fb: dict) -> list[str]:
    """What a night's review is based on, in plain words, from what was actually recorded that night."""
    out = ["The bed: when you were on your side and its temperature, through the night"]
    n = s.get("adjustments") or 0
    if n:
        out.append(f"Your own changes: {n} on the dial or in the app")
    fl = (s.get("hot_flashes") or 0) + (s.get("cold_flashes") or 0)
    if fl:
        out.append(f"{fl} hot or cold flash{'es' if fl != 1 else ''}")
    worn = []
    if s.get("hr_avg"):
        worn.append("heart rate")
    if s.get("stages") or s.get("settled_min"):
        worn.append("sleep stages")
    if worn:
        out.append("Your watch or ring: " + " and ".join(worn))
    if fb.get("temp"):
        out.append(f"Your check-in: {FEEL_WORDS.get(fb['temp'], fb['temp'])}")
    return out


def review_page(c, p, night, now: float) -> dict:
    """Sleep → Review for one night: its check-in, its review and what that is based on, tonight's suggestions (for
    the night that just ended only) and what was applied from it (undoable for a week)."""
    from .summary import summarize
    d = night.isoformat()
    fb = c.store.get(f"feedback:{p.id}:{d}") or {}
    s = summarize(c.store, p, night, c.clock.tz, now) or {}
    rv = c.store.get(f"ai_review:{p.id}:{d}") or None
    started = c.store.get(f"ai_review_started:{p.id}:{d}")
    last = night == last_night(c, now)
    applied = []
    for iid, a in (c.store.get(f"applied_insights:{p.id}", {}) or {}).items():
        if applied_night(c, a) == d:
            applied.append({"id": iid, "change": change_text(p, a), "ts": a.get("ts"),
                            "can_undo": a.get("before") is not None and now - float(a.get("ts") or 0) < UNDO_DAYS * 86400})
    return {"date": d, "last": last, "has_data": bool(s.get("has_data")), "feel": fb.get("temp"),
            "review": rv if rv and rv.get("answer") else None,
            "reviewing": bool(not rv and started and now - float(started) < REVIEW_WAIT_S),
            "review_on": bool(p.settings.autopilot.ai_review),
            "basis": basis(s, fb) if s.get("has_data") else [],
            "suggestions": waiting_suggestions(c, p, now) if last else [],
            "applied": sorted(applied, key=lambda x: -(x["ts"] or 0))}
