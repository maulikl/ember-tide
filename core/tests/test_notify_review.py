"""The morning AI review as a notification: once a night, only with its switch (and the review on), trimmed to a sentence
or two, never for a review without a model; and the Tonight card's endpoints."""

import asyncio

from embertide.notify import Notifier, review_excerpt
from test_autopilot_suite import make
from test_notify import Fake, providers
from test_scenarios import Night

H = {"X-Embertide": "1"}
LONG = ("You settled in quickly and the bed followed your plan well. " * 1 + "Autopilot made no changes overnight. "
        "A cooler start might help on warm nights.")


async def morning(tmp_path):
    n = Night(tmp_path, "2026-11-05T22:00")
    n.c.notifier = Notifier(n.c, None, client=Fake().client())
    n.c.notifier.save_providers(providers())
    n.present("left", True)
    await n.until("2026-11-06T07:00", dt=120)
    n.present("left", False)
    await n.until("2026-11-06T08:30", dt=120)
    n.c.notifier._snap = n.c.snapshot()
    n.p1.settings.autopilot.ai_review = True
    return n


def reviews(n):
    return [e for e in n.events("notification") if e.get("about") == "review"]


def out(answer=LONG, model="test-model"):
    return {"answer": answer, "proposal": None, "model": model, "ts": 0, "date": "2026-11-05"}


async def test_sent_once_with_trimmed_body_and_link(tmp_path):
    n = await morning(tmp_path)
    m = n.c.notifier.review_message(n.p1, "2026-11-05", out())
    assert m and m.kind == "review" and m.title == "Your night, reviewed" and m.link == "/sleep#review" and m.person == n.p1.id
    assert [(a.label, a.act, a.ref) for a in m.actions] == [("Read review", "open", "/sleep#review"), ("Not now", "card_later", "review-2026-11-05")]
    assert m.body == "You settled in quickly and the bed followed your plan well. Autopilot made no changes overnight."
    assert n.c.notifier.review_message(n.p1, "2026-11-05", out()) is None               # same night: not twice
    assert n.c.notifier.review_message(n.p1, "2026-11-06", out())                        # another night is fine


async def test_send_review_goes_out_once(tmp_path):
    n = await morning(tmp_path)
    assert n.c.notifier.send_review(n.p1, "2026-11-05", out()) is True
    assert not n.c.notifier.send_review(n.p1, "2026-11-05", out())
    await asyncio.sleep(0)
    assert len(reviews(n)) == 1


async def test_respects_switches_and_empty_or_modelless(tmp_path):
    n = await morning(tmp_path)
    nt = n.c.notifier
    n.p1.settings.notify.review = False
    assert nt.review_message(n.p1, "2026-11-05", out()) is None
    n.p1.settings.notify.review = True
    n.p1.settings.autopilot.ai_review = False
    assert nt.review_message(n.p1, "2026-11-05", out()) is None
    n.p1.settings.autopilot.ai_review = True
    assert nt.review_message(n.p1, "2026-11-05", out("  ")) is None
    assert nt.review_message(n.p1, "2026-11-05", out(model=None)) is None               # the built-in summary: no ping
    assert nt.review_message(n.p1, "2026-11-05", None) is None
    assert nt.review_message(n.p1, "2026-11-05", out())                                  # none of the above used up the night


async def test_not_for_someone_away(tmp_path):
    from embertide.engine import away
    n = await morning(tmp_path)
    away.start(n.c, n.p1.id, n.clock.now_ts())
    assert n.c.notifier.review_message(n.p1, "2026-11-05", out()) is None


async def test_held_in_quiet_hours_is_dropped_not_told_late(tmp_path):
    n = await morning(tmp_path)
    nt = n.c.notifier
    assert nt._wants_held({"kind": "review"}, n.p1.id) is False


def test_excerpt_trimming():
    assert review_excerpt("One. Two. Three.") == "One. Two."
    assert review_excerpt("Short only") == "Short only"
    long2 = "A" * 100 + ". " + "B" * 100 + "."
    assert review_excerpt(long2) == "A" * 100 + "."                                      # the second sentence doesn't fit
    one = ("word " * 80).strip() + "."
    t = review_excerpt(one)
    assert len(t) <= 180 and t.endswith("…")


def test_tonight_card_endpoints_and_dismiss(tmp_path):
    with make(tmp_path) as c:
        c.post("/api/sim", json={"jump_to": "2026-11-06T09:00"}, headers=H)
        st = c.app.state.oura.c.store
        assert c.get("/api/people/p1/autopilot/ai-review/last").json() == {}
        st.put("ai_review:p1:2026-11-05", {"answer": "Fine night.", "proposal": None, "model": "m", "ts": 1, "date": "2026-11-05"})
        r = c.get("/api/people/p1/autopilot/ai-review/last").json()
        assert r["answer"] == "Fine night." and r["dismissed"] is False
        assert c.post("/api/people/p1/autopilot/ai-review/dismiss", json={"date": "2026-11-05"}, headers=H).json()["ok"]
        assert c.get("/api/people/p1/autopilot/ai-review/last").json()["dismissed"] is True
        c.post("/api/sim", json={"jump_to": "2026-11-06T15:00"}, headers=H)                # afternoon: still last night's
        assert c.get("/api/people/p1/autopilot/ai-review/last").json()["answer"] == "Fine night."
        c.post("/api/sim", json={"jump_to": "2026-11-07T09:00"}, headers=H)                # a day later: that review is old
        assert c.get("/api/people/p1/autopilot/ai-review/last").json() == {}


# ---------- the morning summary with suggestions, the review joining it, and its buttons ----------

SUGG = [{"id": "ap-late-warmer-2026-11-05", "source": "suggestion", "kind": "pattern", "title": "Warmer late night?",
         "detail": "You warmed up near the end on 3 of 7 nights.", "proposal": {"rhythm_id": "r", "phase_id": "late", "field": "temp_c", "to": 28}}]


async def test_morning_with_suggestions_says_how_many_and_opens_them(tmp_path, monkeypatch):
    n = await morning(tmp_path)
    monkeypatch.setattr("embertide.engine.morning.waiting_suggestions", lambda *a, **k: list(SUGG))
    m = n.c.notifier._morning(n.p1, "2026-11-05", n.clock.now_ts())
    assert m and "1 suggestion" in m.title and "suggestions" not in m.title
    assert "Warmer late night?" in m.body and "check in" not in m.body.lower()
    assert m.link == "/sleep#review"
    assert [(a.label, a.act, a.ref) for a in m.actions] == [("See suggestions", "open", "/sleep#review"), ("Not now", "card_later", "sugg-2026-11-05")]
    monkeypatch.setattr("embertide.engine.morning.waiting_suggestions", lambda *a, **k: SUGG * 2)
    assert "2 suggestions" in n.c.notifier._morning(n.p1, "2026-11-05", n.clock.now_ts()).title


async def test_morning_without_suggestions_always_has_the_check_in(tmp_path, monkeypatch):
    n = await morning(tmp_path)
    monkeypatch.setattr("embertide.engine.morning.waiting_suggestions", lambda *a, **k: [])
    n.c.autopilot.learning = lambda p, now: {"done": True}
    m = n.c.notifier._morning(n.p1, "2026-11-05", n.clock.now_ts())
    assert [a.act for a in m.actions] == ["feel_cold", "feel_right", "feel_warm"] and m.link is None


async def test_review_joins_an_unanswered_morning_summary_quietly(tmp_path, monkeypatch):
    n = await morning(tmp_path)
    nt = n.c.notifier
    monkeypatch.setattr("embertide.engine.morning.waiting_suggestions", lambda *a, **k: [])
    first = nt._morning(n.p1, "2026-11-05", n.clock.now_ts())
    m = nt.review_message(n.p1, "2026-11-05", out(), now=n.clock.now_ts() + 120)
    assert m.kind == "morning" and m.update and m.title == first.title and m.link == "/sleep#review"
    assert "Autopilot made no changes overnight." in m.body and [a.act for a in m.actions] == ["feel_cold", "feel_right", "feel_warm"]
    assert nt._allowed(m, n.clock.now_ts()) is None                       # outside the day's one-morning limit


async def test_review_is_its_own_notification_once_the_morning_was_answered_or_is_old(tmp_path, monkeypatch):
    from embertide.notify import save_feel
    n = await morning(tmp_path)
    nt = n.c.notifier
    monkeypatch.setattr("embertide.engine.morning.waiting_suggestions", lambda *a, **k: [])
    nt._morning(n.p1, "2026-11-05", n.clock.now_ts())
    save_feel(n.c, n.p1.id, "right", n.clock.now_ts(), night="2026-11-05")
    m = nt.review_message(n.p1, "2026-11-05", out(), now=n.clock.now_ts() + 60)
    assert m.kind == "review" and not m.update
    nt._morning(n.p1, "2026-11-06", n.clock.now_ts())
    late = nt.review_message(n.p1, "2026-11-06", out(), now=n.clock.now_ts() + 45 * 60)
    assert late.kind == "review"


async def test_not_now_puts_the_card_away_on_every_device(tmp_path):
    from embertide.engine import morning as mo
    from embertide.notify import Action
    n = await morning(tmp_path)
    nt = n.c.notifier
    done = nt.use_action(nt.new_action(n.p1.id, Action("Not now", "card_later", ref="review-2026-11-05")))
    assert done["done"].startswith("Okay")
    assert "review-2026-11-05" in mo.dismissed(n.c, n.p1.id)
    assert n.store.get(f"ai_review_dismissed:{n.p1.id}:2026-11-05")          # the older review card hides too
    nt.use_action(nt.new_action(n.p1.id, Action("Not now", "card_later", ref="nonsense")))   # ignored, no error
    assert set(mo.dismissed(n.c, n.p1.id)) == {"review-2026-11-05"}


def test_open_buttons_become_links_on_every_channel():
    from embertide.notify import Action, Notifier
    assert Notifier._opens(Action("See", "open", ref="/sleep#review"))
    assert not Notifier._opens(Action("See", "open", ref="//evil.example"))
    assert not Notifier._opens(Action("See", "open", ref="https://evil.example"))
    assert not Notifier._opens(Action("Too cold", "feel_cold"))


def test_dismiss_card_endpoint(tmp_path):
    with make(tmp_path) as tc:
        assert tc.post("/api/people/p1/morning/dismiss", json={"id": "sugg-2026-11-05"}, headers=H).json() == {"ok": True, "night": "2026-11-05"}
        assert tc.post("/api/people/p1/morning/dismiss", json={"id": "../x"}, headers=H).status_code == 422
        assert tc.app.state.oura.c.store.get("morning_dismissed:p1").keys() == {"sugg-2026-11-05"}


def test_a_check_in_after_the_review_has_it_rewritten(tmp_path, monkeypatch):
    # 2026-11-21: the morning review went out at 11:12 calling the night "just right"; "Too cold" came at 11:14 and the
    # review never changed
    import json
    import time

    import httpx

    from test_autopilot_suite import make as make_app
    sent, replies = [], ["Comfortable night.", "You said it was too cold: warm the late phase a little.", "Third."]

    async def fake_post(self, url, json=None, headers=None, **kw):
        sent.append(json)
        return httpx.Response(200, json={"choices": [{"message": {"content": replies[min(len(sent), len(replies)) - 1]}}]},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    def review(c):
        return c.get("/api/people/p1/autopilot/ai-review", params={"date": "2026-11-05"}).json()

    def wait_for(pred):
        for _ in range(100):
            if pred():
                return True
            time.sleep(0.02)
        return False

    with make_app(tmp_path, llm="http://fake/v1") as c:
        c.post("/api/sim", json={"jump_to": "2026-11-06T09:00"}, headers=H)
        first = c.post("/api/people/p1/autopilot/ai-review", json={"date": "2026-11-05"}, headers=H).json()
        assert first["answer"] == "Comfortable night." and first["checkin"] == {"temp": None, "note": None}
        c.post("/api/people/p1/feedback", json={"date": "2026-11-05", "temp": "cold"}, headers=H)
        assert wait_for(lambda: review(c)["answer"].startswith("You said it was too cold"))
        ctx = json.loads(sent[-1]["messages"][-1]["content"].split("```json\n", 1)[1].split("\n```", 1)[0])
        assert ctx["morning_checkin"]["temp"] == "cold" and ctx["evidence_tonight"] is True
        assert review(c)["checkin"]["temp"] == "cold"
        calls = len(sent)
        c.post("/api/people/p1/feedback", json={"date": "2026-11-05", "temp": "cold", "rested": 4}, headers=H)
        time.sleep(0.2)
        assert len(sent) == calls                                         # same answer: nothing to rewrite
        # an answer to a question someone asked is theirs: a check-in doesn't replace it
        c.post("/api/people/p1/autopilot/ai-review", json={"date": "2026-11-05", "question": "Was the start too warm?"}, headers=H)
        calls = len(sent)
        c.post("/api/people/p1/feedback", json={"date": "2026-11-05", "temp": "right"}, headers=H)
        time.sleep(0.2)
        assert len(sent) == calls


async def test_review_stale_only_for_a_model_review_that_missed_the_check_in(tmp_path):
    from embertide.notify import review_stale, save_feel
    n = await morning(tmp_path)
    calls = []
    n.c.review_refresh_hook = lambda pid, d: calls.append((pid, d))
    st = n.c.store
    assert not review_stale(n.c, "p1", "2026-11-05")                      # no review yet
    st.put("ai_review:p1:2026-11-05", {**out(), "checkin": {"temp": None, "note": None}})
    assert not review_stale(n.c, "p1", "2026-11-05")                      # no check-in yet
    save_feel(n.c, "p1", "cold", n.c.clock.now_ts(), night="2026-11-05")
    assert review_stale(n.c, "p1", "2026-11-05") and calls == [("p1", "2026-11-05")]
    st.put("ai_review:p1:2026-11-05", {**out(), "checkin": {"temp": "cold", "note": None}})
    assert not review_stale(n.c, "p1", "2026-11-05")
    st.put("ai_review:p1:2026-11-05", out(model=None))                   # the built-in summary: nothing to rewrite
    assert not review_stale(n.c, "p1", "2026-11-05")
    st.put("ai_review:p1:2026-11-05", {**out(), "asked": True})
    assert not review_stale(n.c, "p1", "2026-11-05")
    st.put("ai_review:p1:2026-11-05", out())                              # written before this change: treated as stale
    assert review_stale(n.c, "p1", "2026-11-05")
