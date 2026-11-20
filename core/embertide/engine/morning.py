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
