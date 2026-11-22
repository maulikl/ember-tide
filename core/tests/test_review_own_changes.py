"""The review is told the person's own changes in words: direction, their unit, against the plan. It used to get
the raw absolute °C setting ("01:26 22.78") and could read a dial turned down as turned up."""
from types import SimpleNamespace as NS

from embertide.autopilot_api import _change_words

RH = NS(phases=[NS(id="wind_down", name="Wind-down", temp_c=23.89), NS(id="deep", name="Deep", temp_c=22.0)])


def hm(t):
    return f"t{int(t)}"


def adj(ts, phase="wind_down", source="ha", temp=None, delta=None, total_delta=None):
    return {"ts": ts, "kind": "adjust", "source": source, "phase": phase, "temp_c": temp, "delta_c": delta,
            "total_temp_c": temp, "total_delta_c": total_delta}


def test_a_dial_turned_down_twice_reads_as_one_cooler_change_in_fahrenheit():
    # last night's events: the dial set 74 °F then 73 °F in Wind-down (planned 75 °F)
    lines = _change_words([adj(100, temp=23.33), adj(101, temp=22.78)], RH, "F", hm)
    assert lines == ["t100 Wind-down: they LOWERED it from 75°F to 73°F (2°F cooler) by a dial or Home Assistant; "
                     "the rhythm had 75°F, so it ended 2°F cooler than planned"]


def test_raised_with_the_phone_in_celsius_and_separate_changes_stay_separate():
    evs = [adj(100, source="app", delta=0.5, total_delta=0.5), adj(2000, phase="deep", source="app", delta=-1.0, total_delta=-1.0)]
    lines = _change_words(evs, RH, "C", hm)
    assert lines[0].startswith("t100 Wind-down: they RAISED it from 24°C to 24.5°C (0.5°C warmer) by the phone")
    assert lines[1].startswith("t2000 Deep: they LOWERED it from 22°C to 21°C (1.0°C cooler)")
    assert "cooler than planned" in lines[1]


def test_turning_back_and_autopilot_are_handled():
    evs = [adj(100, temp=23.33), {"ts": 200, "kind": "adjust_cleared", "source": "app"},
           adj(300, source="autopilot", temp=20.0), adj(1500, temp=24.44)]
    lines = _change_words(evs, RH, "F", hm)
    assert len(lines) == 3
    assert "went back to the rhythm" in lines[1]
    assert lines[2].startswith("t1500 Wind-down: they RAISED it from 75°F to 76°F")   # from the plan again, not 74


def test_back_to_the_planned_value_says_so_without_a_gap():
    lines = _change_words([adj(100, temp=23.33), adj(110, temp=23.89)], RH, "F", hm)
    assert lines == ["t100 Wind-down: set to 75°F by a dial or Home Assistant"]
