"""Insight rules that read several nights: the typical night, lately, on the right rhythm, worded honestly."""
from datetime import datetime
from zoneinfo import ZoneInfo

from embertide.engine.insights import insights
from embertide.seed import default_household

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 15, 16, 0, tzinfo=TZ).timestamp()        # a Sunday afternoon: tonight is a weeknight


def night(d, settle=None, fb=None):
    n = {"date": d, "has_data": True, "moments": [], "times_up": 0}
    if settle is not None:
        n["time_to_settle_min"] = settle
    if fb:
        n["feedback"] = {"temp": fb}
    return n


def ids(cards):
    return {c["id"]: c for c in cards}


def person():
    return default_household().people[0]


def test_settling_uses_the_typical_night_and_not_old_outliers():
    # 2026-11-15: 85/54/105 a week ago, then 10/10/7: the average said "45 minutes", the last three nights say no
    ns = [night("2026-11-14", 10), night("2026-11-13", 10), night("2026-11-12", 7),
          night("2026-11-11", 85), night("2026-11-10", 54), night("2026-11-09", 105)]
    assert "slow_settle" not in ids(insights(person(), ns, TZ, NOW))


def test_settling_still_shows_when_it_is_slow_lately_on_that_rhythm():
    # weeknights (Sun-Thu) slow; the card is for Weeknights and says the typical time
    ns = [night("2026-11-12", 40), night("2026-11-11", 35), night("2026-11-10", 50), night("2026-11-09", 45)]
    c = ids(insights(person(), ns, TZ, NOW))["slow_settle"]
    assert c["title"] == "Weeknights: Settling usually takes about 42 minutes" and c["proposal"]["rhythm_id"] == "weeknights"


def test_a_weekend_pattern_goes_to_the_weekend_rhythm():
    ns = [night("2026-11-14", fb="cold"), night("2026-11-13", fb="cold"), night("2026-11-07", fb="cold")]
    c = ids(insights(person(), ns, TZ, NOW))["fb_cold"]
    assert c["proposal"]["rhythm_id"] == "weekends" and c["detail"].startswith("Weekends:")


def test_two_mornings_are_not_several():
    ns = [night("2026-11-11", fb="cold"), night("2026-11-09", fb="cold")]
    c = ids(insights(person(), ns, TZ, NOW))["fb_cold"]
    assert c["title"] == "You've said “too cold” on your last 2 mornings"
    ns.append(night("2026-11-10", fb="right"))
    assert ids(insights(person(), ns, TZ, NOW))["fb_cold"]["title"] == "You've said “too cold” on 2 of your last 3 mornings"
