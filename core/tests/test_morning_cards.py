"""Tonight's morning cards: last night with the check-in, the review (ready, or still being written), tonight's
suggestions; put away on every device; nothing for an away night."""

from embertide.engine import morning as mo
from embertide.notify import save_feel
from test_notify_review import SUGG, morning
from test_autopilot_suite import make

H = {"X-Embertide": "1"}


def kinds(f):
    return [c["kind"] for c in f["cards"]]


async def test_night_card_with_check_in(tmp_path, monkeypatch):
    n = await morning(tmp_path)
    monkeypatch.setattr(mo, "waiting_suggestions", lambda *a, **k: [])
    n.p1.settings.autopilot.ai_review = False
    f = mo.feed(n.c, n.p1, n.clock.now_ts())
    assert f["night"] == "2026-11-05" and kinds(f) == ["night"]
    card = f["cards"][0]
    assert card["id"] == "night-2026-11-05" and card["in_bed_min"] > 0 and "in bed" in card["line"] and card["feel"] is None
    save_feel(n.c, n.p1.id, "warm", n.clock.now_ts(), night="2026-11-05")
    assert mo.feed(n.c, n.p1, n.clock.now_ts())["cards"][0]["feel"] == "warm"


async def test_review_shows_working_then_ready_and_gives_up(tmp_path, monkeypatch):
    n = await morning(tmp_path)
    monkeypatch.setattr(mo, "waiting_suggestions", lambda *a, **k: [])
    now = n.clock.now_ts()
    n.store.put(f"ai_review_started:{n.p1.id}:2026-11-05", now - 60)
    assert [c.get("state") for c in mo.feed(n.c, n.p1, now)["cards"] if c["kind"] == "review"] == ["working"]
    assert "review" not in kinds(mo.feed(n.c, n.p1, now + mo.REVIEW_WAIT_S))          # never came: no endless spinner
    n.store.put(f"ai_review:{n.p1.id}:2026-11-05", {"answer": "A steady night.", "model": "m", "proposal": None})
    rv = [c for c in mo.feed(n.c, n.p1, now)["cards"] if c["kind"] == "review"][0]
    assert rv["state"] == "ready" and rv["answer"] == "A steady night."
    n.p1.settings.autopilot.ai_review = False
    assert "review" not in kinds(mo.feed(n.c, n.p1, now))


async def test_suggestions_card_and_put_away(tmp_path, monkeypatch):
    n = await morning(tmp_path)
    monkeypatch.setattr(mo, "waiting_suggestions", lambda *a, **k: list(SUGG))
    now = n.clock.now_ts()
    f = mo.feed(n.c, n.p1, now)
    assert kinds(f)[-1] == "sugg" and f["cards"][-1]["items"][0]["title"] == "Warmer late night?"
    for cid in ("night-2026-11-05", "sugg-2026-11-05"):
        mo.dismiss(n.c, n.p1.id, cid, now)
    assert kinds(mo.feed(n.c, n.p1, now)) in ([], ["review"])


async def test_nothing_for_an_away_night(tmp_path, monkeypatch):
    n = await morning(tmp_path)
    monkeypatch.setattr("embertide.engine.away.night_verdict", lambda *a, **k: ("away", ""))
    assert mo.feed(n.c, n.p1, n.clock.now_ts())["cards"] == []


def test_morning_endpoint(tmp_path):
    with make(tmp_path) as tc:
        r = tc.get("/api/people/p1/morning", headers=H)
        assert r.status_code == 200 and set(r.json()) == {"night", "cards"}
