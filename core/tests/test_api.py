from fastapi.testclient import TestClient

from embertide.app import create_app
from embertide.config import Config


def client(tmp_path):
    cfg = Config()
    cfg.data_dir, cfg.tz, cfg.bed, cfg.web_dir, cfg.llm_url, cfg.auth = tmp_path, "America/New_York", "sim", tmp_path / "noweb", None, False
    return TestClient(create_app(cfg))


def test_every_write_endpoint_accepts_json(tmp_path):
    with client(tmp_path) as c:
        assert c.get("/healthz").json()["ok"]
        assert c.post("/api/sim", json={"jump_to": "2026-11-05T22:40"}).status_code == 200
        ok = lambda r: r.status_code < 300 or print(r.status_code, r.text)  # noqa: E731
        assert ok(c.post("/api/people/p1/adjust", json={"delta_c": -0.5}))
        assert ok(c.post("/api/people/p1/adjust/clear"))
        assert ok(c.post("/api/people/p1/flash", json={"kind": "hot"}))
        assert ok(c.post("/api/people/p1/flash/extend"))
        assert ok(c.post("/api/people/p1/flash/stop"))
        assert ok(c.post("/api/people/p1/tonight", json={"action": "pause", "minutes": 30}))
        assert ok(c.post("/api/people/p1/context", json={"home": True}))
        assert ok(c.put("/api/people/p1/profile", json={"name": "Robin"}))
        assert c.get("/api/household").json()["people"][0]["name"] == "Robin"
        s = c.get("/api/household").json()["people"][0]["settings"]
        s["simple_default"] = True
        assert ok(c.put("/api/people/p1/settings", json=s))
        assert ok(c.post("/api/people/p1/feedback", json={"date": "2026-11-05", "temp": "warm"}))
        rh = c.get("/api/people/p1/rhythms").json()[0]
        rh["phases"][2]["temp_c"] -= 0.5
        assert ok(c.put(f"/api/people/p1/rhythms/{rh['id']}", json=rh))
        assert ok(c.post("/api/console", json={"command": "IB DIMMER 20"}))
        assert c.post("/api/console", json={"command": "OTA http://x"}).status_code in (200, 400)
        src = c.post("/api/sources", json={"name": "Test radar", "driver": "sim", "role": "presence", "zone": "left", "params": {"kind": "presence"}}).json()
        assert ok(c.put("/api/calibration", json={"weights": {"bed": 0.6, "presence": 0.2, "weight": 0.4}, "weight_threshold_kg": 25}))
        assert ok(c.delete(f"/api/sources/{src['id']}"))
        assert ok(c.post("/api/sources/test-ha", json={"url": "http://127.0.0.1:9", "token": "x", "entity_id": "sensor.x"}))
        ins = c.get("/api/people/p1/insights").json()
        assert isinstance(ins, list)
        assert ok(c.post("/api/people/p1/insights/apply", json={"proposal": {"rhythm_id": rh["id"], "phase_id": "deep", "field": "temp_c", "to": 17.0}}))
        assert ok(c.post("/api/ai/ask", json={"person_id": "p1", "question": "How did I sleep?"}))
        assert c.get("/api/explore?person_id=p1").json()["series"]
        assert isinstance(c.get("/api/presence/agreement?person_id=p1").json(), list)
        assert c.get("/api/people/p1/night").status_code == 200
        assert c.get("/api/export?fmt=csv&hours=24").status_code == 200


def test_stage_import_health_connect_codes(tmp_path):
    with client(tmp_path) as c:
        c.post("/api/sim", json={"jump_to": "2026-11-06T07:30"})
        from datetime import datetime
        from zoneinfo import ZoneInfo
        base = datetime(2026, 11, 6, 0, 30, tzinfo=ZoneInfo("America/New_York")).timestamp()
        segs = [{"start": base, "end": base + 1800, "stage": "4"}, {"start": base + 1800, "end": base + 3600, "stage": 5},
                {"start": base + 3600, "end": base + 4200, "stage": "REM"}, {"start": base + 4200, "end": base + 4300, "stage": "nonsense"}]
        r = c.post("/api/people/p1/stages", json={"source": "health-connect", "label": "Pixel Watch · Health Connect", "segments": segs})
        assert r.status_code == 200 and r.json()["stored"] == 3
        st = c.get("/api/people/p1/stages?date=2026-11-05").json()
        hc = next(x for x in st if x["id"] == "health-connect")
        assert [g["stage"] for g in hc["segments"]] == ["light", "deep", "rem"]


def test_explorer_labels_are_plain_words(tmp_path):
    """Chart labels are read by people, not programmers: no engine words, vendor names or raw codes jargon."""
    with client(tmp_path) as c:
        c.post("/api/sim", json={"jump_to": "2026-11-05T23:30"})
        series = {s["id"]: s for s in c.get("/api/explore?person_id=p1").json()["series"]}
        assert series["occupied"]["label"] == "In bed: final answer"
        assert series["bed_occ"]["label"] == "In bed: bed's own sensor"
        assert series["status"]["label"] == "Bed sensor reading (unfiltered)"
        assert series["target"]["label"] == "Planned temperature"
        for s in series.values():
            text = f"{s['label']} {s['source']} {s['device']}".lower()
            for word in ("fusion", "sleepace", "raw", "setpoint", "tec)", "combined", "engine", "echo"):
                assert word not in text, (s["id"], word)


def test_explorer_side_follows_bed_orientation(tmp_path):
    """A bed that names its sides from the foot: the chart's source line says the side as you lie."""
    with client(tmp_path) as c:
        c.post("/api/sim", json={"jump_to": "2026-11-05T23:30"})
        src = lambda: {s["id"]: s for s in c.get("/api/explore?person_id=p1").json()["series"]}["measured"]["source"]
        assert "left side" in src()
        assert c.put("/api/bed-orientation", json={"from_foot": True}, headers={"X-Embertide": "1"}).status_code == 200
        assert "right side" in src()


def test_every_night_insights_and_put_rhythm_clamp_to_household_limits(tmp_path):
    with client(tmp_path) as c:
        c.post("/api/sim", json={"jump_to": "2026-11-05T22:40"})
        r = c.post("/api/people/p1/adjust", json={"temp_c": 44.0, "until": "every_night"})
        assert r.status_code == 200
        rhs = c.get("/api/people/p1/rhythms").json()
        wn = next(x for x in rhs if x["id"] == "weeknights")
        active_ph = r.json()["people"][0]["night"]["phase_id"]
        assert next(ph["temp_c"] for ph in wn["phases"] if ph["id"] == active_ph) == 35.0

        r2 = c.post("/api/people/p1/adjust", json={"temp_c": 10.0, "until": "every_night"})
        assert r2.status_code == 200
        rhs2 = c.get("/api/people/p1/rhythms").json()
        wn2 = next(x for x in rhs2 if x["id"] == "weeknights")
        assert next(ph["temp_c"] for ph in wn2["phases"] if ph["id"] == active_ph) == 12.0

        r3 = c.post("/api/people/p1/insights/apply", json={
            "proposal": {"rhythm_id": "weeknights", "phase_id": "deep", "field": "temp_c", "to": 42.0}
        })
        assert r3.status_code == 200
        rhs3 = c.get("/api/people/p1/rhythms").json()
        wn3 = next(x for x in rhs3 if x["id"] == "weeknights")
        assert next(ph["temp_c"] for ph in wn3["phases"] if ph["id"] == "deep") == 35.0

        wn3["phases"][0]["temp_c"] = 41.0
        r4 = c.put("/api/people/p1/rhythms/weeknights", json=wn3)
        assert r4.status_code == 200
        rhs4 = c.get("/api/people/p1/rhythms").json()
        wn4 = next(x for x in rhs4 if x["id"] == "weeknights")
        assert wn4["phases"][0]["temp_c"] == 35.0


def test_startup_clamps_out_of_range_saved_household(tmp_path, caplog):
    from embertide.seed import default_household
    from embertide.store import Store

    cfg = Config()
    cfg.data_dir, cfg.tz, cfg.bed, cfg.web_dir, cfg.llm_url, cfg.auth = tmp_path, "America/New_York", "sim", tmp_path / "noweb", None, False
    saved = default_household().model_dump()
    saved["people"][0]["rhythms"][0]["phases"][0]["temp_c"] = 50.0
    saved["people"][0]["rhythms"][0]["phases"][1]["temp_c"] = 5.0
    st = Store(cfg.db_path)
    st.put("household", saved)
    st._db.close()

    with caplog.at_level("WARNING", logger="embertide"):
        with TestClient(create_app(cfg)) as c:
            ph = c.get("/api/people/p1/rhythms").json()[0]["phases"]
            assert ph[0]["temp_c"] == 45.0
            assert ph[1]["temp_c"] == 10.0
    assert any("clamped saved phase" in rec.message for rec in caplog.records)


def test_set_away_ends_active_flash(tmp_path):
    with client(tmp_path) as c:
        c.post("/api/sim", json={"jump_to": "2026-11-05T22:40"})
        r1 = c.post("/api/people/p1/flash", json={"kind": "hot", "minutes": 30})
        assert r1.status_code == 200 and r1.json()["people"][0]["night"]["flash"] is not None
        r2 = c.post("/api/people/p1/away", json={"away": True})
        assert r2.status_code == 200 and r2.json()["people"][0]["night"]["flash"] is None


def test_dismiss_non_guide_insight_sig_and_expiry(tmp_path, monkeypatch):
    import embertide.studio_api as S

    cards = [
        {
            "id": "fb_warm",
            "severity": "suggest",
            "title": "Feels warm lately",
            "detail": "Try lowering Deep by 1 degree.",
            "evidence": ["3 warm check-ins"],
            "proposal": {"rhythm_id": "weeknights", "phase_id": "deep", "field": "temp_c", "from": 19.0, "to": 18.0},
        },
        {
            "id": "need_data",
            "severity": "info",
            "title": "Just getting started",
            "detail": "A few more nights will sharpen patterns.",
            "evidence": [],
            "proposal": None,
        },
    ]
    monkeypatch.setattr(S, "insights", lambda p, nights, tz, now_ts: [dict(x) for x in cards])
    monkeypatch.setattr(S, "tuning_guide", lambda ctl, p, now_ts: [])

    with client(tmp_path) as c:
        by_id = lambda: {i["id"]: i for i in c.get("/api/people/p1/insights").json()}
        init = by_id()
        assert not init["fb_warm"].get("dismissed") and not init["need_data"].get("dismissed")

        # Dismiss both non-guide cards (proposal uses proposal.to, info uses title)
        assert c.post("/api/people/p1/insights/dismiss", json={"id": "fb_warm", "sig": "18.0"}).status_code == 200
        assert c.post("/api/people/p1/insights/dismiss", json={"id": "need_data", "sig": "Just getting started"}).status_code == 200
        hidden = by_id()
        assert hidden["fb_warm"].get("dismissed") is True
        assert hidden["need_data"].get("dismissed") is True

        # Changing proposal target or info title brings the card back
        cards[0]["proposal"] = {**cards[0]["proposal"], "to": 17.5}
        cards[1]["title"] = "Still getting started"
        changed = by_id()
        assert not changed["fb_warm"].get("dismissed")
        assert not changed["need_data"].get("dismissed")

        # Dismiss with the new signatures, then jump forward 14 days
        assert c.post("/api/people/p1/insights/dismiss", json={"id": "fb_warm", "sig": "17.5"}).status_code == 200
        assert c.post("/api/people/p1/insights/dismiss", json={"id": "need_data", "sig": "Still getting started"}).status_code == 200
        assert by_id()["need_data"].get("dismissed") is True

        clk = c.app.state.et_state.controller.clock
        clk.jump_to(clk.now_ts() + 14 * 86400 + 60)
        after = by_id()
        assert after["fb_warm"].get("dismissed") is True    # proposal card stays dismissed
        assert not after["need_data"].get("dismissed")      # info card expires after 14 days


def test_dismiss_rejects_bad_insight_id(tmp_path):
    with client(tmp_path) as c:
        for bad in ("", "a" * 61, "bad id", "bad/id", "bad:id", "bad.id", "ok_id\n"):
            r = c.post("/api/people/p1/insights/dismiss", json={"id": bad, "sig": "x"})
            assert r.status_code == 422, f"expected 422 for {bad!r}"
        for ok_id in ("a", "A_1-b", "x" * 60):
            assert c.post("/api/people/p1/insights/dismiss", json={"id": ok_id, "sig": "x"}).status_code == 200


def test_show_ask_setting_default_and_patch(tmp_path):
    with client(tmp_path) as c:
        hh = c.get("/api/household").json()
        assert hh["people"][0]["settings"]["show_ask"] is True
        units_before = hh["people"][0]["settings"]["units"]

        r1 = c.patch("/api/people/p1/settings", json={"show_ask": False})
        assert r1.status_code == 200
        assert r1.json()["people"][0]["settings"]["show_ask"] is False
        assert r1.json()["people"][0]["settings"]["units"] == units_before
        assert c.get("/api/household").json()["people"][0]["settings"]["show_ask"] is False

        r2 = c.patch("/api/people/p1/settings", json={"show_ask": True})
        assert r2.status_code == 200
        assert r2.json()["people"][0]["settings"]["show_ask"] is True
        assert c.get("/api/household").json()["people"][0]["settings"]["show_ask"] is True


def test_nights_week_bars_agree_with_night_summary_and_explore_moments(tmp_path):
    from datetime import datetime, timedelta

    with client(tmp_path) as c:
        ctl = c.app.state.et_state.controller
        tz = ctl.clock.tz
        c.post("/api/sim", json={"jump_to": "2026-11-06T14:00"})
        p1 = ctl.household.people[0]
        base = datetime(2026, 11, 5, 23, 20, tzinfo=tz)
        rows = []
        # Bed sees 23:20..06:30 with a 40-min out-of-bed gap at 01:00..01:40; watch sees 23:30..06:20.
        for m in range(7 * 60 + 11):
            t = (base + timedelta(minutes=m)).timestamp()
            occ = 0 if 100 <= m < 140 else 1
            rows.append((t, p1.zone, 1, 19.0, 19.0, occ, 60 if occ else None, 14 if occ else None, 0 if occ else None, 19.0, "deep", occ, p1.id))
        ctl.store.telemetry(rows)
        t_start = (base + timedelta(minutes=1)).timestamp()
        ctl.store.event(t_start, "rhythm_started", person=p1.id)
        t_w0 = (base + timedelta(minutes=10)).timestamp()
        t_w_asleep = (base + timedelta(minutes=25)).timestamp()
        t_w_woke = (base + timedelta(minutes=415)).timestamp()
        t_w_end = (base + timedelta(minutes=420)).timestamp()
        ctl.store.put_stages(
            p1.id, "watch", "Watch",
            [
                {"start": t_w0, "end": t_w_asleep, "stage": "awake"},
                {"start": t_w_asleep, "end": t_w_woke, "stage": "light"},
                {"start": t_w_woke, "end": t_w_end, "stage": "awake"},
            ],
            t_w0, t_w_end,
        )

        night = c.get("/api/people/p1/night?date=2026-11-05").json()
        nights = c.get("/api/people/p1/nights?count=7").json()
        bar = next(x for x in nights if x["date"] == "2026-11-05")
        assert bar["in_bed_min"] == night["in_bed_min"]
        assert bar["in_bed_min"] != night["bed_times"]["in_bed_min"]
        assert bar["in_bed_at"] == night["in_bed_at"]
        assert bar["out_at"] == night["out_at"]
        assert bar["settled_min"] == night["settled_min"]
        assert any("Watch: up" in line and "Bed: up" in line for line in night["compare"]["lines"])
        assert any("got-up time" in u.lower() for u in night["compare"]["using"])

        explore = c.get("/api/explore?person_id=p1&date=2026-11-05").json()
        assert [m["kind"] for m in explore["moments"]] == [m["kind"] for m in night["moments"]]
        assert [m["ts"] for m in explore["moments"]] == sorted(m["ts"] for m in explore["moments"])
        assert any(m["kind"] == "out_of_bed" and m["minutes"] >= 39 for m in explore["moments"])


def test_snapshot_schedule_reports_wake_source(tmp_path):
    import time as _t

    def source_after(c, want, tries=40):
        """The new wake applies on the controller's next tick: wait for it (a few seconds at most)."""
        got = None
        for _ in range(tries):
            got = c.get("/api/snapshot").json()["people"][0]["schedule"]["wake_source"]
            if got == want:
                return got
            _t.sleep(0.15)
        return got

    with client(tmp_path) as c:
        ctl = c.app.state.et_state.controller
        c.post("/api/sim", json={"jump_to": "2026-11-05T21:00"})
        snap0 = c.get("/api/snapshot").json()
        p1_sched = snap0["people"][0]["schedule"]
        assert p1_sched["wake_source"] is None

        alarm_ts = p1_sched["wake"] + 30 * 60
        ctl.engines["p1"].follow_alarm(ctl.clock.now_ts(), alarm_ts, reported=True)
        assert source_after(c, "alarm") == "alarm"

        cal_ts = p1_sched["wake"] - 45 * 60
        c.post("/api/people/p1/context", json={"wake": cal_ts, "wake_source": "calendar"})
        assert source_after(c, "calendar") == "calendar"

        hand_ts = p1_sched["wake"] - 20 * 60
        c.post("/api/people/p1/context", json={"wake": hand_ts})
        assert source_after(c, "hand") == "hand"
