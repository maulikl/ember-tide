"""The simulator's sleeper readings stay human after the clock is moved back (Studio's Simulator "jump to")."""

from embertide.clock import FixedClock
from embertide.drivers.sim import SimBed


def test_heart_rate_stays_sane_when_the_clock_jumps_back():
    clock = FixedClock(1_800_000_000.0, "America/New_York")
    bed = SimBed(clock)
    bed.z["left"].occupied = True
    bed.z["left"].in_since = clock.now_ts() + 5 * 86400          # got in "later": the clock was jumped back 5 days
    f = bed._sensor("left", clock.now_ts(), {})
    assert 40 <= f.hr <= 90 and 8 <= f.br <= 20
