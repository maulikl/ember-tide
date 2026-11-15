from types import SimpleNamespace

from embertide.studio_api import wearable_cards

NOW = 1_800_000_000.0


def ctl(devices):
    return SimpleNamespace(autopilot=SimpleNamespace(wearable_status=lambda p, now: devices))


def dev(name, quiet, last, learning=False):
    return {"name": name, "kind": "watch", "quiet": quiet, "last": last, "learning": learning,
            "text": f"{name} hasn't sent anything since Monday" if quiet else f"{name}: in use"}


def test_a_quiet_device_with_nothing_else_sending_gets_a_card():
    cards = wearable_cards(ctl([dev("Health Sync", True, NOW - 3 * 86400)]), None, NOW)
    assert [c["id"] for c in cards] == ["wearable_quiet"]


def test_no_quiet_card_when_another_device_still_sends():
    # 2026-11-15: an old Health Sync import stopped on Monday, but the same watch now comes through Health Connect
    cards = wearable_cards(ctl([dev("Health Sync", True, NOW - 3 * 86400), dev("Samsung Health", False, NOW - 3600)]), None, NOW)
    assert cards == []
