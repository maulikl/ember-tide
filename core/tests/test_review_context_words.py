"""Tests for night review context formatting (units, times, ordering, phase names, notes, evidence)."""

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import embertide.ai as ai
import embertide.autopilot_api as ap_api
from embertide.autopilot_api import (
    REVIEW_SYSTEM,
    _c_words,
    _clip_note,
    _has_evidence,
    _phase_name,
    _review,
    _review_events,
    _review_hm,
    _signal_words,
)
from embertide.clock import FixedClock
from embertide.config import Config
from embertide.drivers.sim import SimBed
from embertide.engine.controller import Controller
from embertide.models import PersonSettings
from embertide.seed import default_household
from embertide.store import Store

TZ = ZoneInfo("America/New_York")


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=TZ).timestamp()


def test_c_words_four_shapes_f_and_c():
    # signed difference
    assert _c_words("+0.6 °C", "F") == "+1°F"
    assert _c_words("-0.6 °C", "F") == "-1°F"
    assert _c_words("+0.6 °C", "C") == "+0.6°C"
    assert _c_words("-0.6 °C", "C") == "-0.6°C"

    # absolute temperature
    assert _c_words("35.4 °C", "F") == "96°F"
    assert _c_words("35.4 °C", "C") == "35.5°C"

    # absolute temperature plus signed difference against usual room
    assert _c_words("21.0 °C (+1.8)", "F") == "70°F (+3°F vs usual)"
    assert _c_words("21.0 °C (-1.8)", "F") == "70°F (-3°F vs usual)"
    assert _c_words("21.0 °C (+1.8)", "C") == "21°C (+1.8°C vs usual)"
    assert _c_words("21.0 °C (-1.8)", "C") == "21°C (-1.8°C vs usual)"

    # unsigned ± limit
    assert _c_words("±1.7 °C", "F") == "±3°F"
    assert _c_words("±1.7 °C", "C") == "±1.7°C"
    assert _c_words("would go past the ±1.7 °C limit for tonight", "F") == "would go past the ±3°F limit for tonight"
    assert _c_words("would go past the ±1.7 °C limit for tonight", "C") == "would go past the ±1.7°C limit for tonight"

    # combined reason string and standalone parentheses untouched
    assert _c_words("skin_temp +0.6 °C, room 21.0 °C (+1.8)", "F") == "skin_temp +1°F, room 70°F (+3°F vs usual)"
    assert _c_words("note (+1.8) and +6 bpm", "F") == "note (+1.8) and +6 bpm"


def test_signal_words_units_f_has_no_celsius():
    store = Store(":memory:")
    p = default_household().people[0]
    rows = []
    for i, val in enumerate((0.5, 0.5, 0.7)):
        rows.append((1000.0 + i * 60, f"ap:{p.id}:skin_delta", val))
    for i, val in enumerate((1.0, 1.0, 1.0)):
        rows.append((1000.0 + i * 60, f"ap:{p.id}:room_vs_usual", val))
    for i, val in enumerate((0.5, 1.0, 1.5)):
        rows.append((1000.0 + i * 60, f"ap:{p.id}:score", val))
    for i, val in enumerate((0.2, 0.4, 0.3)):
        rows.append((1000.0 + i * 60, f"ap:{p.id}:restless", val))
    for i, val in enumerate((4.0, 6.0, 5.0)):
        rows.append((1000.0 + i * 60, f"ap:{p.id}:hr_rise", val))
    store.samples_many(rows)
    c = SimpleNamespace(store=store)

    out_f = _signal_words(c, p, 900.0, 1500.0, "F")
    assert "°C" not in json.dumps(out_f)
    assert out_f["skin temperature change"] == {"avg": "+1°F", "peak": "+1°F"}
    assert out_f["room vs its usual"] == {"avg": "+2°F", "peak": "+2°F"}
    score_key = "Autopilot discomfort score (+ means warm, - means cold; it acts at about ±1)"
    assert out_f[score_key] == {"avg": 1.0, "peak": 1.5}

    out_c = _signal_words(c, p, 900.0, 1500.0, "C")
    assert out_c["skin temperature change"] == {"avg": "+0.6°C", "peak": "+0.7°C"}
    assert out_c["room vs its usual"] == {"avg": "+1.0°C", "peak": "+1.0°C"}

    empty = _signal_words(c, p, 0.0, 100.0, "F")
    assert "°C" not in json.dumps(empty)


def test_review_hm_respects_clock_style():
    t = _ts("2026-11-05T23:05")
    assert _review_hm(t, PersonSettings(units="F", clock="auto"), TZ) == "11:05 PM"
    assert _review_hm(t, PersonSettings(units="C", clock="auto"), TZ) == "23:05"
    assert _review_hm(t, PersonSettings(units="C", clock="12"), TZ) == "11:05 PM"
    assert _review_hm(t, PersonSettings(units="F", clock="24"), TZ) == "23:05"


def test_phase_name_lookup_and_fallback():
    phases = {"deep": "Deep", "fall_asleep": "Fall asleep"}
    assert _phase_name("deep", phases) == "Deep"
    assert _phase_name("unknown_phase", phases) == "unknown_phase"
    assert _phase_name(None, phases) is None


def test_clip_note():
    assert _clip_note(None) is None
    assert _clip_note("") == ""
    assert _clip_note("a" * 300) == "a" * 300
    clipped = _clip_note("a" * 305)
    assert clipped == "a" * 299 + "…"
    assert len(clipped) == 300


def test_review_events_ordering_caps_phase_names_units_and_notes():
    p = default_household().people[0]
    rh = p.rhythms[0]
    base = _ts("2026-11-05T23:00")
    hm = lambda t: _review_hm(t, PersonSettings(units="F", clock="12"), TZ)

    # build newest-first to verify oldest-first sorting and caps (12, 12, 8)
    evs = []
    for i in range(13, -1, -1):
        evs.append({
            "ts": base + i * 600,
            "kind": "autopilot",
            "phase": "deep" if i == 0 else "custom_id",
            "status": "blocked" if i == 0 else "suggested",
            "direction": "cooler",
            "reasons": [{"signal": "skin_temp", "value": "+0.6 °C"}, {"signal": "room", "value": "21.0 °C (+1.8)"}],
            "blocked_by": ["would go past the ±1.7 °C limit for tonight"] if i == 0 else [],
        })
        evs.append({
            "ts": base + i * 600 + 30,
            "kind": "feel",
            "phase": "fall_asleep" if i == 0 else "other_phase",
            "feel": "too_hot",
            "note": ("z" * 350) if i == 0 else "warm",
        })
    for i in range(9, -1, -1):
        evs.append({"ts": base + i * 600 + 45, "kind": "hot_flash" if i % 2 == 0 else "cold_flash"})

    noticed, taps, flashes, tap_evs = _review_events(evs, rh, "F", hm)
    assert len(noticed) == 12
    assert len(taps) == 12
    assert len(tap_evs) == 12
    assert len(flashes) == 8

    # oldest first (23:00 -> 11:00 PM) with phase names and rewritten units
    assert noticed[0].startswith("11:00 PM Deep: blocked cooler (skin_temp +1°F, room 70°F (+3°F vs usual))")
    assert noticed[0].endswith("held back: would go past the ±3°F limit for tonight")
    assert noticed[1].startswith("11:10 PM custom_id:")
    assert noticed[-1].startswith("12:50 AM custom_id:")

    assert taps[0].startswith("11:00 PM Fall asleep: too_hot ")
    assert ('"' + "z" * 299 + '…"') in taps[0]
    assert taps[1].startswith("11:10 PM other_phase: too_hot")
    assert flashes[0].startswith("11:00 PM hot_flash")
    assert flashes[-1].startswith("12:10 AM cold_flash")


def test_has_evidence_rules():
    assert not _has_evidence([], [], None, [], [])
    assert not _has_evidence([], [], {"temp": "right", "note": None}, [], [])
    assert not _has_evidence([], [], {"temp": "right", "note": ""}, [], [])
    assert not _has_evidence([], [{"feel": "just_right"}], None, [], [])

    assert _has_evidence([], [], {"temp": "right", "note": "woke up warm"}, [], [])
    assert _has_evidence([], [], {"temp": "cold", "note": None}, [], [])
    assert _has_evidence([], [], {"temp": "warm", "note": None}, [], [])
    assert _has_evidence([], [{"feel": "just_right"}, {"feel": "too_hot"}], None, [], [])
    assert _has_evidence([], [{"feel": "too_cold"}], None, [], [])
    assert _has_evidence(["11:00 PM Deep: suggested cooler ()"], [], None, [], [])
    assert _has_evidence([], [], None, ["11:00 PM hot_flash"], [])
    assert _has_evidence([], [], None, [], ["11:00 PM Deep: lowered"])


def test_review_system_prompt_updated():
    assert "the person's rhythm (°C)" not in REVIEW_SYSTEM
    assert "times are 24-hour" not in REVIEW_SYSTEM
    assert "keep them as written" in REVIEW_SYSTEM
    assert "Numbers named *_c are Celsius" in REVIEW_SYSTEM


async def test_review_builds_readable_context(monkeypatch):
    clock = FixedClock(_ts("2026-11-06T08:00"), "America/New_York")
    store = Store(":memory:")
    hh = default_household()
    p = hh.people[0]
    p.settings.units = "F"
    p.settings.clock = "auto"
    c = Controller(hh, SimBed(clock), store, clock)
    state = SimpleNamespace(
        controller=c,
        cfg=Config(llm_url="http://localhost:11434/v1", llm_model="qwen2.5:7b", llm_provider="openai", demo=False),
    )

    night_day = "2026-11-05"
    t0 = _ts("2026-11-05T23:05")
    store.event(t0 + 600, "autopilot", p.id, "autopilot", phase="deep", status="blocked", direction="cooler",
                reasons=[{"signal": "skin_temp", "value": "+0.6 °C"}],
                blocked_by=["would go past the ±1.7 °C limit for tonight"])
    store.event(t0, "feel", p.id, "app", phase="fall_asleep", feel="too_hot", note="n" * 350)
    store.put(f"feedback:{p.id}:{night_day}", {"temp": "warm", "note": "c" * 350, "ts": _ts("2026-11-06T07:15")})
    store.samples_many([
        (t0 + 60, f"ap:{p.id}:skin_delta", 0.6),
        (t0 + 120, f"ap:{p.id}:skin_delta", 0.6),
        (t0 + 180, f"ap:{p.id}:skin_delta", 0.6),
    ])

    monkeypatch.setattr(ap_api, "summarize", lambda *_a, **_kw: {
        "in_bed_min": 420, "time_to_settle_min": 15, "times_up": 0, "hr_avg": 58, "hr_low": 52,
        "hot_flashes": 0, "cold_flashes": 0, "adjustments": 0, "tracking_c": 0.56,
        "settled_min": 390, "awake_moving_min": None, "up_minutes": 0,
        "fell_asleep_est": t0 + 900, "in_bed_at": t0, "out_at": _ts("2026-11-06T06:05"), "br_avg": 14.2,
    })
    monkeypatch.setattr(c.autopilot, "skin", lambda *_a, **_kw: [(t0, 34.0), (t0 + 600, 35.4)])

    seen = {}

    async def fake_complete(_cfg, system, user, **_kw):
        seen["system"] = system
        seen["user"] = user
        return 'Slept warm.\n```proposal\n{"rhythm_id": "weeknights", "phase_id": "deep", "field": "temp_c", "to": 17.0}\n```'

    monkeypatch.setattr(ai, "complete", fake_complete)

    out = await _review(state, p, night_day, None)
    ctx = json.loads(seen["user"].split("```json\n", 1)[1].split("\n```", 1)[0])

    assert ctx["bed_vs_plan"]["typical_gap"] == "1°F"
    assert "avg_gap_c" not in ctx["bed_vs_plan"]
    assert ctx["skin_temp"] == {"samples": 2, "min": "93°F", "max": "96°F"}
    assert "skin_temp_c" not in ctx
    assert ctx["morning_checkin"] == {"temp": "warm", "note": "c" * 299 + "…"}
    assert ctx["night"]["in_bed_from"] == "11:05 PM"
    assert ctx["night"]["fell_asleep"] == "11:20 PM"
    assert ctx["night"]["out_at"] == "6:05 AM"
    assert ctx["autopilot_noticed"][0].startswith("11:15 PM Deep:")
    assert "±3°F" in ctx["autopilot_noticed"][0]
    assert ('"' + "n" * 299 + '…"') in ctx["their_taps"][0]
    assert "°C" not in json.dumps(ctx["autopilot"]["signals_overnight"])
    assert ctx["evidence_tonight"] is True
    assert out["proposal"] is not None

    # No check-in and only a just_right tap -> morning_checkin is None, evidence_tonight is False, proposal dropped
    store2 = Store(":memory:")
    c2 = Controller(hh, SimBed(clock), store2, clock)
    state2 = SimpleNamespace(controller=c2, cfg=state.cfg)
    store2.event(t0, "feel", p.id, "app", phase="deep", feel="just_right")
    monkeypatch.setattr(c2.autopilot, "skin", lambda *_a, **_kw: [])
    out2 = await _review(state2, p, night_day, None)
    ctx2 = json.loads(seen["user"].split("```json\n", 1)[1].split("\n```", 1)[0])
    assert ctx2["morning_checkin"] is None
    assert ctx2["evidence_tonight"] is False
    assert out2["proposal"] is None
