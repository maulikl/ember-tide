"""Live "Is that you in bed, or <pet>?" question when occupancy and phone/watch disagree."""

from embertide.engine import petq, phone_context
from embertide.engine.pets import PetGuard
from embertide.engine.presence import SideState
from embertide.models import Pet
from embertide.notify import Notifier
from embertide.seed import default_household
from test_notify import Fake, providers
from test_pets import CAT, DOG, PetNight
from test_watch_identity import BAND, WatchNight, elsewhere, matching


def _live_dog():
    return {**DOG, "name": "Rufus"}


def _live_cat():
    return {**CAT, "name": "Biscuit"}


# (a) petq: ask_live, view, answer yes / me

def test_ask_live_creates_question_views_and_answers_yes(tmp_path):
    n = PetNight(tmp_path, "2026-11-05T23:55", pets=[_live_dog(), _live_cat()])
    now = n.clock.now_ts()
    # pre-existing open question for Alex gets marked skipped when a live question is asked
    old_ep = {"since": now - 2400, "last_on": now - 400, "ticks": 60, "pet_ticks": 60, "in_bed_ticks": 0, "readings": []}
    old = petq.consider(n.c, "left", old_ep, now - 400)
    assert old is not None and old["status"] == "open"

    dog, cat = n.hh.pets
    ep = {"since": now - 240, "pet_id": cat.id,
          "readings": [[round(now - 90), 61.0, 16.0], [round(now - 60), 62.0, 15.0], [round(now - 30), 60.0, 16.0]]}
    q = petq.ask_live(n.c, "left", ep, now, "steps", steps=52)
    assert q is not None
    assert q["kind"] == "live" and q["status"] == "open" and q["steps"] == 52
    assert q["pets"] == [cat.id, dog.id]                   # guessed pet comes first, up to 3 fitting pets
    assert q["spans"][0]["evidence"] == "conflict"
    assert next(x for x in petq.questions(n.store) if x["id"] == old["id"])["status"] == "skipped"
    grp = petq._group(now, n.clock.tz)
    assert n.store.get(f"who_asked:{n.p1.id}") == grp
    pq_evs = [e for e in n.events("pet_question") if e.get("question") == q["id"]]
    assert pq_evs and pq_evs[0]["about"] == "live" and pq_evs[0]["why"] == "steps" and pq_evs[0]["steps"] == 52

    v = petq.view(q, n.hh, n.clock.tz)
    assert v["title"] == "Is that you in bed, or one of your pets?"
    assert "phone counted 52 steps since then" in v["detail"]
    labels = [c["label"] for c in v["choices"]]
    assert labels == ["It's Biscuit", "It's Rufus", "It's me", "Not sure"]

    watch_v = petq.view({**q, "why": "watch"}, n.hh, n.clock.tz)
    assert "watch says it isn't you" in watch_v["detail"]

    answered = petq.answer(n.c, q["id"], n.p1.id, "yes", dog.id, now + 15)
    assert answered["status"] == "answered" and answered["answered_pet"] == dog.id
    assert dog.id in petq.pet_sigs(n.store)
    assert n.c._pets.state["left"]["confirmed"] == "Rufus"
    wa_evs = n.events("who_answer")
    assert wa_evs and wa_evs[0]["pet"] == dog.id and wa_evs[0]["undid_start"] is False


async def test_ask_live_answer_me_widens_and_suppresses_reasking(tmp_path):
    n = PetNight(tmp_path, "2026-11-05T23:54", pets=[_live_dog()])
    n.bed.scripted = False
    n.bed.pets["left"] = 61
    await n.until("2026-11-05T23:55")
    phone_context.update(n.store, n.p1.id, {"steps_total": 100.0, "steps_at": n.clock.now_ts()}, n.clock.now_ts(), sharing=["steps"])
    await n.until("2026-11-05T23:59")
    phone_context.update(n.store, n.p1.id, {"steps_total": 150.0, "steps_at": n.clock.now_ts()}, n.clock.now_ts(), sharing=["steps"])
    await n.step(1)

    q = petq.open_for(n.store, n.p1.id, n.clock.now_ts())
    assert q is not None and q["kind"] == "live"
    assert q["spans"][0]["readings"]
    petq.answer(n.c, q["id"], n.p1.id, "me", None, n.clock.now_ts())
    assert petq.learned_range(n.store, n.p1.id)

    # Even if who_asked is cleared, the same episode is not asked again after "It's me"
    n.store.put(f"who_asked:{n.p1.id}", None)
    await n.until("2026-11-06T00:04")
    assert petq.open_for(n.store, n.p1.id, n.clock.now_ts()) is None


# (b) PetGuard: confirmed flag

def test_petguard_confirmed_flag_cleared_on_empty_and_scale_person_wins():
    guard = PetGuard()
    hh = default_household()
    hh.pets = [Pet(**_live_dog())]
    side_on = SideState(occupied=True, hr=61, br=16)
    side_off = SideState(occupied=False)

    base = guard.assess("left", 1000.0, hh, side_on, [], [], [65.0])
    assert not base["suspected"]

    guard.state["left"]["confirmed"] = "Rufus"
    res = guard.assess("left", 1030.0, hh, side_on, [], [], [65.0])
    assert res["suspected"] is True and res["pet"] == "Rufus"
    assert res["evidence"] == "answer" and res["why"] == "you said it's Rufus"

    # A person on the scale still wins over the confirmed flag
    scale_res = guard.assess("left", 1060.0, hh, side_on, [64.0], [], [65.0])
    assert scale_res["scale_person"] is True and scale_res["suspected"] is False

    # Emptying the side clears the confirmed flag so the next occupancy starts clean
    # (a blip of the strip while the dog shifts doesn't: it must stay empty CONFIRMED_EMPTY_S)
    blip = guard.assess("left", 1090.0, hh, side_off, [], [], [65.0])
    assert blip["suspected"] is False and guard.state["left"].get("confirmed") == "Rufus"
    back = guard.assess("left", 1100.0, hh, side_on, [], [], [65.0])
    assert back["suspected"] is True and back["evidence"] == "answer"
    guard.assess("left", 1110.0, hh, side_off, [], [], [65.0])
    empty_res = guard.assess("left", 1110.0 + 121, hh, side_off, [], [], [65.0])
    assert empty_res["suspected"] is False and "confirmed" not in guard.state["left"]
    re_occ = guard.assess("left", 1260.0, hh, side_on, [], [], [65.0])
    assert re_occ["suspected"] is False


# (c) Controller + Notifier: conflict triggers live question/push, watch "me" blocks it, "yes" undoes start

async def test_phone_unplugged_not_by_dial_without_steps_asks_nothing(tmp_path):
    n = PetNight(tmp_path, "2026-11-05T23:54", pets=[_live_dog()])
    n.bed.scripted = False
    n.bed.pets["left"] = 61
    phone_context.update(n.store, n.p1.id, {"wifi_home": True, "charging": False, "dials": []},
                         n.clock.now_ts(), sharing=["wifi", "charging", "dial"])
    await n.until("2026-11-06T00:10")
    assert petq.open_for(n.store, n.p1.id, n.clock.now_ts()) is None


async def test_steps_after_occupied_asks_notifies_and_yes_undoes_autostart(tmp_path):
    n = PetNight(tmp_path, "2026-11-05T23:54", pets=[_live_dog()])
    fake = Fake()
    n.c.notifier = Notifier(n.c, "https://bed.example", client=fake.client())
    n.c.notifier.save_providers(providers())
    n.bed.scripted = False
    n.bed.pets["left"] = 61
    await n.until("2026-11-05T23:56")
    phone_context.update(n.store, n.p1.id, {"steps_total": 500.0, "steps_at": n.clock.now_ts()},
                         n.clock.now_ts(), sharing=["steps"])
    await n.until("2026-11-06T00:02")
    phone_context.update(n.store, n.p1.id, {"steps_total": 550.0, "steps_at": n.clock.now_ts()},
                         n.clock.now_ts(), sharing=["steps"])
    await n.step(1)
    for t in list(n.c.notifier._tasks):
        await t

    q = petq.open_for(n.store, n.p1.id, n.clock.now_ts())
    assert q is not None and q["kind"] == "live" and q["why"] == "steps" and q["steps"] == 50
    v = petq.view(q, n.hh, n.clock.tz)
    assert "50 steps" in v["detail"]
    to_alex = [c for c in fake.calls if c["json"].get("topic") == "alex-bed"]
    assert len(to_alex) == 1
    body = to_alex[0]["json"]
    assert body["priority"] == 4
    assert [a["label"] for a in body["actions"]] == ["It's Rufus", "It's me"]

    assert n.e(n.p1).s.started is True
    petq.answer(n.c, q["id"], n.p1.id, "yes", n.hh.pets[0].id, n.clock.now_ts())
    assert n.e(n.p1).s.started is False and n.e(n.p1).s.in_bed is None
    wa = n.events("who_answer")
    assert wa and wa[-1]["undid_start"] is True

    # Next tick still sees the dog as suspected pet and doesn't restart the night
    await n.step(2)
    assert n.c.fused["left"]["pet"]["suspected"] is True
    assert n.c.fused["left"]["pet"]["evidence"] == "answer"
    assert n.e(n.p1).s.started is False


async def test_steps_only_before_occupancy_asks_nothing(tmp_path):
    n = PetNight(tmp_path, "2026-11-05T23:50", pets=[_live_dog()])
    n.bed.scripted = False
    phone_context.update(n.store, n.p1.id, {"steps_total": 100.0, "steps_at": n.clock.now_ts()},
                         n.clock.now_ts(), sharing=["steps"])
    await n.until("2026-11-05T23:53")
    phone_context.update(n.store, n.p1.id, {"steps_total": 300.0, "steps_at": n.clock.now_ts()},
                         n.clock.now_ts(), sharing=["steps"])
    await n.until("2026-11-05T23:54")
    n.bed.pets["left"] = 61
    await n.until("2026-11-05T23:56")
    phone_context.update(n.store, n.p1.id, {"steps_total": 305.0, "steps_at": n.clock.now_ts()},
                         n.clock.now_ts(), sharing=["steps"])
    await n.until("2026-11-06T00:05")
    phone_context.update(n.store, n.p1.id, {"steps_total": 310.0, "steps_at": n.clock.now_ts()},
                         n.clock.now_ts(), sharing=["steps"])
    await n.step(2)
    assert petq.open_for(n.store, n.p1.id, n.clock.now_ts()) is None


async def test_watch_on_charger_and_no_steps_asks_nothing(tmp_path):
    n = WatchNight(tmp_path, "2026-11-05T23:54", watch_fn=None, pets=[_live_dog()])
    n.bed.scripted = False
    n.bed.pets["right"] = 61
    await n.until("2026-11-06T00:20")
    assert petq.open_for(n.store, n.p2.id, n.clock.now_ts()) is None


async def test_watch_not_me_asks_after_hold(tmp_path):
    n = WatchNight(tmp_path, "2026-11-05T23:45", watch_fn=elsewhere, pets=[_live_dog()])
    n.p2.settings.autopilot.trust_more = "bed"
    n.bed.scripted = False
    await n.step(12)
    n.bed.pets["right"] = 88
    for _ in range(40):
        await n.step(1)
        if (n.c._eps.get("right") or {}).get("not_me"):
            break
    assert (n.c._eps.get("right") or {}).get("not_me") is True
    assert petq.open_for(n.store, n.p2.id, n.clock.now_ts()) is None       # held during CONFLICT_HOLD_S
    await n.step(4)                                                         # 120 s < 180 s
    assert petq.open_for(n.store, n.p2.id, n.clock.now_ts()) is None
    await n.step(3)                                                         # >= 180 s
    q = petq.open_for(n.store, n.p2.id, n.clock.now_ts())
    assert q is not None and q["kind"] == "live" and q["why"] == "watch"
    assert "watch says it isn't you" in petq.view(q, n.hh, n.clock.tz)["detail"]


async def test_watch_me_gives_no_live_question_even_when_phone_counts_steps(tmp_path):
    # the sleeper is in bed with their watch agreeing ("it's me"): steps from the phone (someone else carrying it, a
    # step counter glitch) never raise the question
    n = WatchNight(tmp_path, "2026-11-05T22:00", watch_fn=matching, pets=[_live_dog()], band=BAND)
    n.bed.scripted = False
    await n.step(12)
    n.present("right", True)
    await n.until("2026-11-05T22:40")
    assert n.fused()["watch"]["held"] == "me"
    phone_context.update(n.store, n.p2.id, {"steps_total": 100.0, "steps_at": n.clock.now_ts()},
                         n.clock.now_ts(), sharing=["steps"])
    await n.until("2026-11-05T22:50")
    phone_context.update(n.store, n.p2.id, {"steps_total": 160.0, "steps_at": n.clock.now_ts()},
                         n.clock.now_ts(), sharing=["steps"])
    await n.step(2)
    assert petq.open_for(n.store, n.p2.id, n.clock.now_ts()) is None

async def test_notify_at_0010_sends_live_and_holds_ordinary_pet_question(tmp_path):
    n = PetNight(tmp_path, "2026-11-06T00:10", pets=[_live_dog()])
    fake = Fake()
    n.c.notifier = Notifier(n.c, "https://bed.example", client=fake.client())
    n.c.notifier.save_providers(providers())
    now = n.clock.now_ts()

    # Ordinary pet question at 00:10 must wait until 8 AM
    ord_ep = {"since": now - 2000, "last_on": now - 60, "ticks": 60, "pet_ticks": 60, "in_bed_ticks": 0, "readings": []}
    q_ord = petq.consider(n.c, "left", ord_ep, now)
    assert q_ord is not None and q_ord["kind"] == "pet"
    out_ord = n.c.notifier.observe(now, n.c.snapshot())
    assert not any(m.kind in ("pet_question", "pet_live") for m in out_ord)
    assert not [c for c in fake.calls if c["json"].get("topic") == "alex-bed"]

    # Live question at 00:10 goes out right away with priority 4
    q_live = petq.ask_live(n.c, "left", {"since": now - 200, "readings": []}, now, "steps", steps=45)
    assert q_live is not None
    out_live = n.c.notifier.observe(now, n.c.snapshot())
    for t in list(n.c.notifier._tasks):
        await t
    live_msgs = [m for m in out_live if m.kind == "pet_live"]
    assert len(live_msgs) == 1 and live_msgs[0].priority == 4
    to_alex = [c for c in fake.calls if c["json"].get("topic") == "alex-bed"]
    assert len(to_alex) == 1
