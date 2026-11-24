"""Garmin data through each person's own Personal API key: the night, naps, heart rate and HRV, odd answers, the
number of calls and their pace, and a 429, all against a fake Garmin (garmin_fake.py) with synthetic values."""

import os
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from garmin_fake import HR_PATH, HRV_PATH, SLEEP_PATH, FakeGarmin

from embertide.app import create_app
from embertide.cloud import garmin as g
from embertide.config import Config
from embertide.engine.sleeps import all_for

H = {"X-Embertide": "1"}
TZ = ZoneInfo("America/New_York")
UTC = timezone.utc
NIGHT_DAY = "2026-11-05"                   # the night of 4 to 5 November ends on this calendar date


def client(tmp_path):
    cfg = Config()
    cfg.data_dir, cfg.tz, cfg.bed, cfg.web_dir, cfg.llm_url, cfg.auth = tmp_path, "America/New_York", "sim", tmp_path / "noweb", None, False
    cfg.public_url = "https://bed.example.org"
    return TestClient(create_app(cfg))


def ts(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=TZ).timestamp()


def ms(s: str) -> int:
    return int(ts(s) * 1000)


def at(c, when: str) -> None:
    assert c.post("/api/sim", json={"jump_to": when}, headers=H).status_code == 200


def garmin(c, fake: FakeGarmin):
    p = c.app.state.cloud.get("garmin")
    p.client = fake.client()
    p.pause_s = 0
    return p


def connect(c, person="p2", key="k-synthetic-0123456789"):
    r = c.post("/api/cloud/me/garmin/connect", json={"person": person, "key": key}, headers=H)
    assert r.status_code == 200, r.text
    return r


def look(c, person="p2"):
    return c.post("/api/cloud/me/garmin/sync", json={"person": person}, headers=H)


def store(c):
    return c.app.state.cloud.c.store


def series(c, metric: str, person="p2") -> list[tuple[float, float]]:
    return store(c).samples(f"wear:{person}:{metric}:Garmin", 0, 2e9)


def sleeps(c, person="p2") -> list[dict]:
    """The stored sleep records (engine/sleeps.py), as the provider handed them over."""
    return all_for(store(c), person)


def day_of(req: httpx.Request) -> str:
    u = urlsplit(str(req.url))
    return parse_qs(u.query).get("date", [u.path.rsplit("/", 1)[-1]])[0]


# ---------- the EX-008 night ----------
def a_night(start: object = None) -> dict:
    return {"id": 77001, "calendarDate": NIGHT_DAY,
            "sleepStartTimestampGMT": ms("2026-11-04T23:00") if start is None else start,
            "sleepEndTimestampGMT": ms("2026-11-05T06:30"),
            "sleepTimeSeconds": 340 * 60, "deepSleepSeconds": 80 * 60, "lightSleepSeconds": 200 * 60,
            "remSleepSeconds": 60 * 60, "awakeSleepSeconds": 20 * 60, "avgHeartRate": 56,
            "averageRespirationValue": 14.5, "averageSpO2Value": 95, "sleepScores": {"overall": {"value": 81}},
            "somethingNew": {"nested": [1, 2]},
            "dailyNapDTOS": [{"napStartTimestampGMT": ms("2026-11-05T13:00"), "napEndTimestampGMT": ms("2026-11-05T13:25"),
                              "napTimeSec": 1500}]}


def heart_body() -> dict:
    t0 = ms("2026-11-04T23:00")
    return {"restingHeartRate": 52,
            "heartRateValueDescriptors": [{"key": "timestamp", "index": 0}, {"key": "heartrate", "index": 1}],
            "heartRateValues": [[t0 + i * 120_000, 55 + i % 10] for i in range(220)]}


def hrv_body() -> dict:
    t0 = datetime(2026, 11, 5, 4, 0, tzinfo=UTC)                   # 23:00 New York, written as Garmin's GMT text
    return {"hrvSummary": {"calendarDate": NIGHT_DAY, "lastNightAvg": 47},
            "hrvReadings": [{"hrvValue": 40 + i % 20,
                             "readingTimeGMT": datetime.fromtimestamp(t0.timestamp() + i * 300, UTC).strftime("%Y-%m-%dT%H:%M:%S.0")}
                            for i in range(90)]}


def ex008(night=None):
    """The fake answers: the sleep list, and heart rate and HRV for the night's date (204 on other days)."""
    def sleep(req):
        return httpx.Response(200, json=[night or a_night()])

    def hr(req):
        return httpx.Response(200, json=heart_body()) if day_of(req) == NIGHT_DAY else httpx.Response(204)

    def hv(req):
        return httpx.Response(200, json=hrv_body()) if day_of(req) == NIGHT_DAY else httpx.Response(204)
    return {SLEEP_PATH: sleep, HR_PATH: hr, HRV_PATH: hv}


def test_a_night_a_nap_and_the_series(tmp_path):
    fake = FakeGarmin(ex008())
    with client(tmp_path) as c:
        garmin(c, fake)
        at(c, "2026-11-05T12:00")
        connect(c)
        r = look(c)                                                 # waits for the first look, then looks again
        assert r.status_code == 200, r.text
        got = {x["kind"]: x for x in sleeps(c)}
        assert sorted(got) == ["nap", "night"]
        n, nap = got["night"], got["nap"]
        assert n["id"] == "garmin:77001" and n["source"] == "garmin" and n["device"] == "Garmin"
        assert (n["start"], n["end"]) == (ts("2026-11-04T23:00"), ts("2026-11-05T06:30"))
        assert (n["asleep_min"], n["deep_min"], n["light_min"], n["rem_min"], n["awake_min"]) == (340, 80, 200, 60, 20)
        assert n["hr_avg"] == 56 and n["breath"] == 14.5 and n["hrv_avg"] == 47 and n["segments"] == []
        assert nap["id"] == f"garmin:{int(ts('2026-11-05T13:00'))}" and nap["asleep_min"] == 25 and nap["source"] == "garmin"
        assert len(series(c, "hr")) == 220 and len(series(c, "hrv")) == 90
        assert series(c, "resting_hr") == [(ts("2026-11-05T06:30"), 52.0)]
        assert series(c, "sleep_score") == [(ts("2026-11-05T06:30"), 81.0)]
        assert series(c, "night_breath") == [(ts("2026-11-05T06:30"), 14.5)]
        assert series(c, "spo2") == [(ts("2026-11-05T06:30"), 95.0)]
        assert store(c).sample_sources("wear:p2:hr:") == ["wear:p2:hr:Garmin"]
        # no stage timeline for Garmin (D12): stage minutes live on the period only
        assert "garmin" not in store(c).get_stages("p2", ts("2026-11-04T22:00"), ts("2026-11-05T14:00"))
        # the same days again store nothing new
        again = look(c).json()
        assert again["ok"] and all(v == 0 for v in again["stored"].values()), again
        assert len(series(c, "hr")) == 220 and len(sleeps(c)) == 2


FORMS = [ms("2026-11-04T23:00"), int(ts("2026-11-04T23:00")), str(ms("2026-11-04T23:00")),
         "2026-11-05T04:00:00.0", "2026-11-05T04:00:00Z", "2026-11-04T23:00:00-05:00"]


@pytest.fixture
def new_york_process():
    """The server process itself runs in New York, so reading GMT text as local time would show."""
    if not hasattr(time, "tzset"):
        pytest.skip("POSIX only")
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


@pytest.mark.parametrize("start", FORMS, ids=["ms", "s", "digits", "gmt-text", "z", "offset"])
def test_time_forms(tmp_path, new_york_process, start):
    assert g.gts(start) == ts("2026-11-04T23:00")
    fake = FakeGarmin(ex008(a_night(start)))
    with client(tmp_path) as c:
        garmin(c, fake)
        at(c, "2026-11-05T12:00")
        connect(c)
        assert look(c).status_code == 200
        n = next(x for x in sleeps(c) if x["kind"] == "night")
        assert n["start"] == ts("2026-11-04T23:00")
        # an HRV reading time in GMT text is UTC: 04:00 GMT is 23:00 in New York, not 04:00 New York
        assert series(c, "hrv")[0][0] == ts("2026-11-04T23:00")


def test_gts_refuses_what_isnt_a_time():
    assert g.gts("2026-11-05T08:10:00.0") == datetime(2026, 11, 5, 8, 10, tzinfo=UTC).timestamp()
    for v in (None, True, False, float("nan"), float("inf"), "", "soon", "2026-13-40T00:00", [], {}, 12, -5, 1e30, "9" * 30):
        assert g.gts(v) is None, v


def test_parsers_never_raise():
    odd = [None, True, 0, 1.5, float("nan"), "", "x", [], {}, [None], [[]], {"a": None}, [{"x": [1, {}]}], "[]",
           {"heartRateValues": "x", "heartRateValueDescriptors": [{"key": "timestamp", "index": 99}, None, {"key": 3}]},
           {"heartRateValues": [[None, None], [1, 2, 3], "x", {"timestamp": "x"}]},
           {"hrvReadings": [None, {"readingTimeGMT": [], "hrvValue": {}}], "hrvSummary": []},
           {"dailySleepDTO": None}, {"dailySleepDTO": {"sleepStartTimestampGMT": {}}},
           {"sleepStartTimestampGMT": 2e12, "sleepEndTimestampGMT": 1e12}, {"sleepScores": {"overall": 5}},
           {"id": True, "sleepStartTimestampGMT": 1793851200, "sleepEndTimestampGMT": 1793851200}]
    for v in odd:
        g.sleep_rows(v), g.night(v), g.nap(v), g.night_numbers(v), g.heart(v), g.hrv(v), g.gts(v)
    assert g.heart({"heartRateValues": None}) == ([], None)
    assert g.night({"sleepStartTimestampGMT": 1793851200, "sleepEndTimestampGMT": 1793851000}) is None
    assert g.night({"sleepStartTimestampGMT": 1793851200, "sleepEndTimestampGMT": 1793851800,
                    "deepSleepSeconds": -60, "sleepTimeSeconds": "600"})["deep_min"] is None
    assert g.sleep_rows({"dailySleepDTO": {"id": 1}}) == [{"id": 1}] and g.sleep_rows({}) == [] and g.sleep_rows("x") == []
    # object rows read by name; descriptors pick the columns, whatever their order
    assert g.heart({"heartRateValues": [{"timestamp": 1793851200000, "heartrate": 60}]})[0] == [(1793851200.0, 60.0)]
    flipped = {"heartRateValueDescriptors": [{"key": "heartrate", "index": 0}, {"key": "timestamp", "index": 1}],
               "heartRateValues": [[61, 1793851200000], [300, 1793851260000], [10, 1793851320000]], "restingHeartRate": 160}
    assert g.heart(flipped) == ([(1793851200.0, 61.0)], None)
    assert g.night_numbers({"sleepScores": {"overall": {"value": 101}}, "averageRespirationValue": 3, "averageSpO2Value": 49}) == {}


def test_heart_object_row_keys():
    t0 = 1793851200000
    rows = [
        {"timestamp": t0, "heartrate": 60},
        {"timestamp": t0 + 60_000, "heartRate": 61},
        {"timestamp": t0 + 120_000, "value": 62},
        {"timestamp": t0 + 180_000, "heartrate": None, "heartRate": 63, "value": 64},
    ]
    assert g.heart({"heartRateValues": rows}) == (
        [(1793851200.0, 60.0), (1793851260.0, 61.0), (1793851320.0, 62.0), (1793851380.0, 63.0)],
        None,
    )


def test_longer_sleep_wins_for_same_calendar_date(tmp_path):
    shorter = {"id": 77002, "calendarDate": NIGHT_DAY,
               "sleepStartTimestampGMT": ms("2026-11-05T13:00"), "sleepEndTimestampGMT": ms("2026-11-05T14:00"),
               "sleepTimeSeconds": 55 * 60}
    data = ex008()
    data[SLEEP_PATH] = lambda req: httpx.Response(200, json=[a_night(), shorter])
    fake = FakeGarmin(data)
    with client(tmp_path) as c:
        garmin(c, fake)
        at(c, "2026-11-05T15:00")
        connect(c)
        assert look(c).status_code == 200
        by_id = {x["id"]: x for x in sleeps(c)}
        assert {"garmin:77001", "garmin:77002"} <= set(by_id)
        assert by_id["garmin:77001"]["hrv_avg"] == 47
        assert by_id["garmin:77002"]["hrv_avg"] is None
        assert series(c, "resting_hr") == [(ts("2026-11-05T06:30"), 52.0)]


def test_odd_answers_do_not_stop_a_sync(tmp_path):
    def sleep(req):
        return httpx.Response(200, json=[None, "junk", {"sleepStartTimestampGMT": True}, {"id": None, "sleepScores": None},
                                         {"sleepStartTimestampGMT": "soon", "dailyNapDTOS": "x"}, a_night()])

    def hr(req):
        d = day_of(req)
        if d == NIGHT_DAY:
            t = ms("2026-11-05T01:00")
            return httpx.Response(200, json={"heartRateValues": [[t, 60], [t + 60_000, None], "junk", [t + 120_000],
                                                                 {"timestamp": t + 180_000, "heartrate": 61}],
                                             "heartRateValueDescriptors": None, "restingHeartRate": "52", "extra": 1})
        if d == "2026-11-04":
            return httpx.Response(404)
        if d == "2026-11-03":
            return httpx.Response(200, content=b"")
        return httpx.Response(200, json={"heartRateValues": None})

    def hv(req):
        if day_of(req) == "2026-11-04":
            return httpx.Response(200, content=b"<html>not json</html>")
        return httpx.Response(204)

    fake = FakeGarmin({SLEEP_PATH: sleep, HR_PATH: hr, HRV_PATH: hv})
    with client(tmp_path) as c:
        garmin(c, fake)
        at(c, "2026-11-05T12:00")
        connect(c)
        r = look(c)
        assert r.status_code == 200, r.text
        assert sorted(x["kind"] for x in sleeps(c)) == ["nap", "night"]
        assert [v for _, v in series(c, "hr")] == [60.0, 61.0]
        assert series(c, "hrv") == [] and series(c, "resting_hr") == []
        assert c.get("/api/cloud/me/garmin?person=p2").json()["last_error"] is None

    # the sleep list arrives as {}: nothing to store, and still no error
    fake = FakeGarmin({SLEEP_PATH: lambda req: httpx.Response(200, json={})})
    with client(tmp_path / "b") as c:
        garmin(c, fake)
        at(c, "2026-11-05T12:00")
        connect(c)
        assert look(c).status_code == 200 and sleeps(c) == []


def test_resting_heart_rate_without_a_night(tmp_path):
    """D14: a past date without a night is stamped 07:00 local; today without a night isn't stored yet."""
    def hr(req):
        return httpx.Response(200, json={"restingHeartRate": 50 if day_of(req) == "2026-11-04" else 49})
    fake = FakeGarmin({HR_PATH: hr})
    with client(tmp_path) as c:
        garmin(c, fake)
        at(c, "2026-11-05T12:00")
        connect(c)
        assert look(c).status_code == 200
        got = dict(series(c, "resting_hr"))
        assert got[ts("2026-11-04T07:00")] == 50 and ts("2026-11-05T07:00") not in got


def test_polled_without_owner_credentials(tmp_path):
    fake = FakeGarmin(ex008())
    with client(tmp_path) as c:
        p = garmin(c, fake)
        at(c, "2026-11-05T12:00")
        connect(c)
        assert look(c).status_code == 200
        assert not store(c).get("garmin_app")                      # no client id was ever saved
        at(c, "2026-11-05T23:30")                                  # p2's night: the poll leaves them alone
        n = len(fake.calls)
        c.portal.call(p.tick)
        assert len(fake.calls) == n
        at(c, "2026-11-06T08:00")                                  # after waking, the poll looks
        c.portal.call(p.tick)
        assert len(fake.data_requests()) > 0 and len(fake.calls) > n
        assert abs(c.get("/api/cloud/me/garmin?person=p2").json()["last_sync"] - ts("2026-11-06T08:00")) < 5


def test_calls_per_sync_and_pacing(tmp_path):
    fake = FakeGarmin(ex008())
    with client(tmp_path) as c:
        p = garmin(c, fake)
        at(c, "2026-11-05T12:00")
        connect(c)
        assert look(c).status_code == 200
        waits: list[float] = []

        async def nap(s):
            waits.append(s)
        p.pause_s, p._mono, p._sleep, p._last = 0.5, (lambda: 1000.0), nap, 1000.0   # a frozen clock: each call waits
        # a routine look: three calendar days, one sleep call and two per day
        n = len(fake.calls)
        assert look(c).status_code == 200
        made = fake.calls[n:]
        assert len(made) == 7 <= 8 and not fake.exchanges()[1:]
        assert waits == [0.5] * len(made)
        sleep_req = made[0]
        assert urlsplit(str(sleep_req.url)).path == SLEEP_PATH
        assert parse_qs(urlsplit(str(sleep_req.url)).query) == {"startDate": ["2026-11-03"], "endDate": ["2026-11-05"]}
        assert sorted({day_of(r) for r in made[1:]}) == ["2026-11-03", "2026-11-04", "2026-11-05"]   # never tomorrow
        # the first look: fifteen calendar days
        store(c).put("garmin_status:p2", {})
        n = len(fake.calls)
        waits.clear()
        assert look(c).status_code == 200
        made = fake.calls[n:]
        assert len(made) == 31 <= 34 and waits == [0.5] * 31


def test_a_429_stops_everything(tmp_path):
    fake = FakeGarmin(ex008())
    with client(tmp_path) as c:
        p = garmin(c, fake)
        at(c, "2026-11-05T12:00")
        connect(c)
        assert look(c).status_code == 200
        fake.status_at[fake.data_calls + 3] = (429, {"retry-after": "120"})
        n = len(fake.calls)
        r = look(c)
        assert r.status_code == 429
        assert len(fake.calls) - n == 3                            # no fourth call
        now = c.app.state.cloud.c.clock.now_ts()
        s = store(c).get("garmin_status:p2")
        assert abs(s["backoff_until"] - (now + 120)) < 5 and s["last_error"] == "Garmin is busy; trying again later."
        at(c, "2026-11-05T12:01")
        n = len(fake.calls)
        r = look(c)
        assert r.status_code == 429 and r.json()["detail"] == "Garmin is busy; trying again later."
        assert len(fake.calls) == n                                # a manual look reached nobody
        assert not p.due("p2", c.app.state.cloud.c.clock.now_ts())
        at(c, "2026-11-05T12:03")
        assert look(c).status_code == 200 and len(fake.calls) > n
        assert store(c).get("garmin_status:p2")["last_error"] is None


def test_refused_hrv_keeps_the_rest(tmp_path):
    data = ex008()
    data[HRV_PATH] = lambda req: httpx.Response(403)
    fake = FakeGarmin(data)
    with client(tmp_path) as c:
        garmin(c, fake)
        at(c, "2026-11-05T12:00")
        connect(c)
        fake.calls.clear()
        r = look(c)
        assert r.status_code == 200, r.text
        assert any(x["kind"] == "night" for x in sleeps(c))
        assert len(series(c, "hr")) == 220 and series(c, "hrv") == []
        hrv_calls = [x for x in fake.calls if urlsplit(str(x.url)).path.startswith(HRV_PATH)]
        assert len(hrv_calls) == 1                                 # not asked again in that sync
        assert store(c).get("garmin_status:p2")["last_error"] is None


def test_a_refused_sleep_call_fails_the_sync(tmp_path):
    """D13 is for heart rate and HRV only: the same answer on the sleep call, or a 5xx, is a passing error."""
    fake = FakeGarmin({SLEEP_PATH: lambda req: httpx.Response(403)})
    with client(tmp_path) as c:
        garmin(c, fake)
        at(c, "2026-11-05T12:00")
        connect(c)
        r = look(c)
        assert r.status_code == 502 and r.json()["detail"] == "Garmin answered 403."
        s = c.get("/api/cloud/me/garmin?person=p2").json()
        assert s["last_error"] == "Garmin answered 403." and not s["needs_reconnect"]
        fake.data[SLEEP_PATH] = lambda req: httpx.Response(200, json=[])
        fake.data[HR_PATH] = lambda req: httpx.Response(503)
        r = look(c)
        assert r.status_code == 502 and r.json()["detail"] == "Garmin answered 503."
