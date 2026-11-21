"""Morning check-ins count toward suggestions: "Too cold" on 3 of the last 7 mornings suggests warming the coolest
stage (and "Too warm" cooling the warmest), worded as the person's mornings, not their taps."""

from embertide.autopilot_api import _suggest
from embertide.notify import save_feel
from test_scenarios import Night, ts


def mornings(n, answers: dict[str, str]):
    for night, temp in answers.items():
        save_feel(n.c, "p1", temp, ts(f"{night}T23:59"), night=night)


def tonight(n):
    from datetime import datetime
    from embertide.engine.rhythm import night_date, pick_rhythm
    return pick_rhythm(n.p1.rhythms, night_date(datetime.fromtimestamp(n.clock.now_ts(), n.clock.tz)))


def test_three_cold_mornings_suggest_warming_the_coolest_stage(tmp_path):
    n = Night(tmp_path, "2026-11-06T09:00")
    mornings(n, {"2026-11-03": "cold", "2026-11-04": "cold"})
    assert not [s for s in _suggest(n.c, n.p1) if s["kind"] == "pattern"], "two mornings aren't a pattern yet"
    mornings(n, {"2026-11-05": "cold"})
    got = [s for s in _suggest(n.c, n.p1) if s["kind"] == "pattern"]
    coolest = min(tonight(n).phases, key=lambda q: q.temp_c)
    assert len(got) == 1 and got[0]["phase"] == coolest.id and got[0]["direction"] == "warmer"
    assert got[0]["nights"] == ["2026-11-03", "2026-11-04", "2026-11-05"]
    assert got[0]["proposal"]["to"] > got[0]["proposal"]["from"] == coolest.temp_c
    words = (got[0]["title"] + " " + got[0]["detail"]).lower()
    assert "morning" in words or "check-in" in words
    assert "taps" not in words and "autopilot" not in words, "nothing but the mornings said so"


def test_warm_mornings_go_to_the_warmest_stage_and_a_changed_answer_counts_once(tmp_path):
    n = Night(tmp_path, "2026-11-06T09:00")
    mornings(n, {"2026-11-03": "warm", "2026-11-04": "warm", "2026-11-05": "warm"})
    warmest = max(tonight(n).phases, key=lambda q: q.temp_c)
    got = [s for s in _suggest(n.c, n.p1) if s["kind"] == "pattern"]
    assert [(s["phase"], s["direction"]) for s in got] == [(warmest.id, "cooler")]
    mornings(n, {"2026-11-05": "right"})                                  # they changed their mind about one night
    assert not [s for s in _suggest(n.c, n.p1) if s["kind"] == "pattern"]


def test_old_nights_answered_late_dont_count(tmp_path):
    n = Night(tmp_path, "2026-11-20T09:00")
    for night in ("2026-11-01", "2026-11-02", "2026-11-03"):                # filled in from History today
        save_feel(n.c, "p1", "cold", n.clock.now_ts(), night=night)
    assert not [s for s in _suggest(n.c, n.p1) if s["kind"] == "pattern"]


async def test_only_steps_they_were_in_bed_for(tmp_path):
    # wind-down is often the coolest step, but it runs before bed: a cold morning isn't about it
    n = Night(tmp_path, "2026-11-02T21:00")
    for rh in n.p1.rhythms:
        next(q for q in rh.phases if q.id == "wind_down").temp_c = 15.0
    n.c.reload_household(n.c.household)
    for day in ("02", "03", "04"):
        await n.until(f"2026-11-{day}T23:00", dt=120)
        n.present("left", True)
        nxt = f"2026-11-{int(day) + 1:02d}"
        await n.until(f"{nxt}T07:00", dt=120)
        n.present("left", False)
        await n.until(f"{nxt}T09:00", dt=120)
        save_feel(n.c, "p1", "cold", n.clock.now_ts(), night=f"2026-11-{day}")
    got = [s for s in _suggest(n.c, n.p1) if s["kind"] == "pattern"]
    assert [(s["phase"], s["direction"]) for s in got] == [("deep", "warmer")]
