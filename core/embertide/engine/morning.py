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
