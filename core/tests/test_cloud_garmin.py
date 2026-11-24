"""Garmin through each person's own Personal API key: connecting, the key and its token, access and safety, all
against a fake Garmin (garmin_fake.py) with synthetic values."""

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from garmin_fake import HOST, HR_PATH, HRV_PATH, KEY, SLEEP_PATH, TOKEN_PATH, FakeGarmin

from embertide.app import create_app
from embertide.cloud import NOT_AVAILABLE
from embertide.cloud.base import CloudError
from embertide.cloud.sealed import KEY_FILE
from embertide.config import Config

H = {"X-Embertide": "1"}
TZ = ZoneInfo("America/New_York")
PW = "correct horse battery"
GONE = "Your Garmin key has run out or was deleted. Make a new one at Garmin Connect and paste it here."


def client(tmp_path, auth=False):
    cfg = Config()
    cfg.data_dir, cfg.tz, cfg.bed, cfg.web_dir, cfg.llm_url, cfg.auth = tmp_path, "America/New_York", "sim", tmp_path / "noweb", None, auth
    cfg.public_url = "https://bed.example.org"
    return TestClient(create_app(cfg))


def at(c, when: str) -> None:
    assert c.post("/api/sim", json={"jump_to": when}, headers=H).status_code == 200


def garmin(c, fake: FakeGarmin):
    p = c.app.state.cloud.get("garmin")
    p.client = fake.client()
    p.pause_s = 0
    return p


def connect(c, key=KEY, person="p2", expires=None, cl=None):
    body = {"person": person, "key": key}
    if expires is not None:
        body["expires"] = expires
    return (cl or c).post("/api/cloud/me/garmin/connect", json=body, headers=H)


def look(c, person="p2", cl=None):
    """A manual look. Right after connecting it also waits for the background first look (same lock)."""
    return (cl or c).post("/api/cloud/me/garmin/sync", json={"person": person}, headers=H)


def status(c, person="p2"):
    return c.get(f"/api/cloud/me/garmin?person={person}").json()


def kv_rows(c) -> list[tuple[str, str]]:
    return list(c.app.state.cloud.c.store._db.execute("SELECT key, value FROM kv"))


def test_garmin_is_listed_available(tmp_path):
    with client(tmp_path) as c:
        raw = c.get("/api/cloud/providers").json()["providers"]
        g = next(x for x in raw if x["id"] == "garmin")
        assert g["available"] and g["configured"] and g["connect"] == "key" and g["name"] == "Garmin"
        assert g["connected"] == {"p1": False, "p2": False} and g["gives"] and urlsplit(g["docs_url"]).hostname == "developer.garmin.com"
        assert "why" not in g and "scopes" not in g and "redirect_uri" not in g
        assert [x["id"] for x in raw if x["available"]] == ["oura", "withings", "whoop", "polar", "garmin"]
        assert [x["id"] for x in NOT_AVAILABLE] == ["fitbit", "samsung", "apple"]
        oura = next(x for x in raw if x["id"] == "oura")
        assert oura["connect"] == "signin" and oura["redirect_uri"] == "https://bed.example.org/api/oura/callback" and "scopes" in oura
        # nothing for the owner: no client id to read or save, no sign-in callback
        a = c.get("/api/integrations/cloud/garmin").json()
        assert a == {"id": "garmin", "configured": True, "connect": "key", "note": a["note"]} and "Settings" in a["note"]
        assert c.get("/api/cloud/garmin/callback?code=x&state=y").status_code == 404


def test_connect_with_a_key(tmp_path):
    fake = FakeGarmin()
    with client(tmp_path) as c:
        garmin(c, fake)
        r = connect(c)
        assert r.status_code == 200, r.text
        assert r.json()["connected"] and r.json()["connect"] == "key" and "url" not in r.json()
        ex = fake.exchanges()
        assert len(ex) == 1 and ex[0].method == "POST" and ex[0].headers["authorization"] == f"ApiKey {KEY}"
        assert KEY not in r.text and fake.issued[0] not in r.text
        assert look(c).status_code == 200
        sleeps = [q for q in fake.data_requests() if urlsplit(str(q.url)).path == SLEEP_PATH]
        assert len(sleeps) == 2                                   # the background first look, then this one
        assert sleeps[0].headers["authorization"] == f"Bearer {fake.issued[0]}"
        # the token answer is text/plain: "Accept: application/json" there gets a 406 from Garmin; data calls want JSON
        assert ex[0].headers["accept"] == "*/*" and sleeps[0].headers["accept"] == "application/json"
        assert len(fake.exchanges()) == 1                         # the token was reused
        assert status(c)["last_sync"] and not status(c, "p1")["connected"]


@pytest.mark.parametrize("key", ["", " ", "abc def", "abc\ndef", "tab\there", "k" * 2000, "kéy-synthetic", "k" * 5000, None])
def test_malformed_key_never_reaches_garmin(tmp_path, key):
    fake = FakeGarmin()
    with client(tmp_path) as c:
        garmin(c, fake)
        r = c.post("/api/cloud/me/garmin/connect", json={"person": "p2", **({} if key is None else {"key": key})}, headers=H)
        assert r.status_code == 422
        if key is not None and len(key) <= 4096:
            assert "Garmin key" in r.json()["detail"]
            assert not key.strip() or key.strip() not in r.text
        assert fake.calls == [] and not status(c)["connected"]


def test_refused_key_stores_nothing(tmp_path):
    fake = FakeGarmin()
    fake.refused.add(KEY)
    with client(tmp_path) as c:
        garmin(c, fake)
        r = connect(c)
        assert r.status_code == 400 and "didn't accept that key" in r.json()["detail"] and KEY not in r.text
        assert len(fake.exchanges()) == 1
        s = status(c)
        assert not s["connected"] and not s["needs_reconnect"] and not s["last_error"]
        assert c.app.state.cloud.c.store.get("garmin_token:p2") is None


def test_replacing_a_key(tmp_path):
    fake = FakeGarmin()
    other = "k-synthetic-replacement-42"
    with client(tmp_path) as c:
        p = garmin(c, fake)
        at(c, "2026-11-20T12:00")
        assert connect(c, expires="2026-11-25").status_code == 200
        look(c)
        assert status(c)["key_expires_soon"]
        before = p.token("p2")
        # a refused replacement leaves the working key as it was
        fake.refused.add("k-synthetic-refused")
        assert connect(c, key="k-synthetic-refused").status_code == 400
        assert p.token("p2") == before and status(c)["key_expires"] == "2026-11-25"
        assert look(c).status_code == 200
        # an accepted one takes over and clears the warning
        assert connect(c, key=other).status_code == 200
        s = status(c)
        assert s["connected"] and s["key_expires"] is None and not s["key_expires_soon"] and not s["needs_reconnect"]
        fake.revoke_tokens()
        assert look(c).status_code == 200
        assert fake.exchanges()[-1].headers["authorization"] == f"ApiKey {other}"


def test_only_your_own_side(tmp_path):
    fake = FakeGarmin()
    with client(tmp_path, auth=True) as c:
        garmin(c, fake)
        c.post("/api/auth/setup", json={"username": "alex", "password": PW, "person": "p1"})
        code = c.post("/api/auth/invites", json={"person": "p2", "role": "sleeper"}, headers=H).json()["code"]
        sam = TestClient(c.app)
        sam.post("/api/auth/accept", json={"code": code, "username": "sam", "password": PW})
        r = connect(c, person="p1", cl=sam)                      # names the other side; connects sam's own
        assert r.status_code == 200, r.text
        look(c, person="p2", cl=sam)
        p = c.app.state.cloud.get("garmin")
        assert p.connected("p2") and not p.connected("p1")
        vcode = c.post("/api/auth/invites", json={"person": None, "role": "viewer"}, headers=H).json()["code"]
        viewer = TestClient(c.app)
        viewer.post("/api/auth/accept", json={"code": vcode, "username": "guest", "password": PW})
        n = len(fake.calls)
        assert connect(c, person="p1", cl=viewer).status_code == 403
        assert len(fake.calls) == n
        assert c.put("/api/integrations/cloud/garmin", json={"client_id": "cid_test", "client_secret": "sec_test"}, headers=H).status_code == 409
        g = c.get("/api/integrations/cloud/garmin")
        assert g.status_code == 200 and "client_id" not in g.json() and "secret_saved" not in g.json()
        assert sam.put("/api/integrations/cloud/garmin", json={"client_id": "cid_test"}, headers=H).status_code == 403


@pytest.mark.parametrize("form,life,expect", [
    ("bare", 7200, 7200),                     # the observed form: exp - iat
    ("json", 5400, 5400),
    ("jsonstr", 3 * 3600, 3 * 3600),
    ("bare", None, 86400),                    # no exp: 24 hours
    ("json-unreadable", None, 5000),          # claims can't be read: expires_in
    ("bare-unreadable", None, 86400),         # nothing to go on: 24 hours
])
def test_token_answer_forms(tmp_path, form, life, expect):
    fake = FakeGarmin()
    fake.exchange = form.split("-")[0]
    fake.life, fake.readable = life, not form.endswith("unreadable")
    fake.expires_in = 5000 if form == "json-unreadable" else None
    with client(tmp_path) as c:
        p = garmin(c, fake)
        r = connect(c)
        assert r.status_code == 200, r.text
        left = p.token("p2")["expires_at"] - c.app.state.cloud.c.clock.now_ts()
        assert abs(left - expect) < 5
        assert look(c).status_code == 200
        assert fake.data_requests()[-1].headers["authorization"] == f"Bearer {fake.issued[-1]}"


def test_an_empty_token_answer_fails_cleanly(tmp_path):
    fake = FakeGarmin()
    fake.exchange = "empty"
    with client(tmp_path) as c:
        garmin(c, fake)
        r = connect(c)
        assert r.status_code == 502 and r.json()["detail"] == "Garmin didn't send a token."
        assert not status(c)["connected"] and c.app.state.cloud.c.store.get("garmin_token:p2") is None


def test_token_is_reused_then_renewed(tmp_path):
    fake = FakeGarmin()
    with client(tmp_path) as c:
        garmin(c, fake)
        at(c, "2026-11-05T08:00")
        assert connect(c).status_code == 200
        look(c)
        assert len(fake.exchanges()) == 1
        for when in ("2026-11-05T12:00", "2026-11-05T20:00", "2026-11-06T06:30"):   # more than an hour left
            at(c, when)
            assert look(c).status_code == 200
        assert len(fake.exchanges()) == 1
        at(c, "2026-11-06T07:30")                                 # 30 minutes left
        assert look(c).status_code == 200
        assert len(fake.exchanges()) == 2
        assert fake.data_requests()[-1].headers["authorization"] == f"Bearer {fake.issued[-1]}"


def test_a_401_renews_once(tmp_path):
    fake = FakeGarmin()
    with client(tmp_path) as c:
        garmin(c, fake)
        connect(c)
        look(c)
        ex, data = len(fake.exchanges()), len(fake.data_requests())
        fake.revoke_tokens()                                       # 10+ hours left, yet Garmin answers 401
        r = look(c)
        assert r.status_code == 200, r.text
        # one renewal, and one retry on top of a routine look's seven data calls
        assert len(fake.exchanges()) == ex + 1 and len(fake.data_requests()) == data + 8
        assert not status(c)["needs_reconnect"]
        # two 401s in a row: the key is gone; at most two exchanges in that sync
        fake.always_401 = True
        p = c.app.state.cloud.get("garmin")
        tok = p.token("p2")
        tok["expires_at"] = 0                                      # the sync starts with a renewal too
        c.app.state.cloud.c.store.put("garmin_token:p2", tok)
        ex = len(fake.exchanges())
        r = look(c)
        assert r.status_code == 502 and r.json()["detail"] == GONE
        assert len(fake.exchanges()) - ex <= 2
        s = status(c)
        assert s["needs_reconnect"] and s["last_error"] == GONE


def test_refused_exchange_asks_for_a_new_key(tmp_path):
    fake = FakeGarmin()
    with client(tmp_path) as c:
        p = garmin(c, fake)
        connect(c)
        look(c)
        fake.refused.add(KEY)                                      # deleted at Garmin: its tokens stop too
        fake.revoke_tokens()
        r = look(c)
        assert r.status_code == 502 and r.json()["detail"] == GONE
        s = status(c)
        assert s["connected"] and s["needs_reconnect"] and s["last_error"] == GONE
        n = len(fake.calls)
        now = c.app.state.cloud.c.clock.now_ts()
        assert not p.due("p2", now + 86400)                        # the poll leaves this person alone
        c.portal.call(p.tick)
        assert len(fake.calls) == n
        assert ("p2", "Garmin", True) in c.app.state.notifier.account_states()
        # a new key puts it right
        assert connect(c, key="k-synthetic-new-key-7").status_code == 200
        assert not status(c)["needs_reconnect"] and ("p2", "Garmin", False) in c.app.state.notifier.account_states()

        # a 503 on the exchange is a passing error: no new key needed, polling carries on
        assert connect(c, key="k-synthetic-first-side", person="p1").status_code == 200
        look(c, person="p1")
        fake.revoke_tokens()
        fake.exchange = 503
        r = look(c, person="p1")
        assert r.status_code == 502 and r.json()["detail"] == "Garmin answered 503."
        s = status(c, "p1")
        assert not s["needs_reconnect"] and s["last_error"] == "Garmin answered 503."


def test_only_garmins_host(tmp_path):
    def moved(req):
        return httpx.Response(302, headers={"location": "https://elsewhere.example.org/steal"})
    fake = FakeGarmin()
    with client(tmp_path) as c:
        garmin(c, fake)
        connect(c)
        look(c)
        c.post("/api/cloud/me/garmin/disconnect", json={"person": "p2"}, headers=H)
        connect(c)
        look(c)
        fake.data[SLEEP_PATH] = moved
        r = look(c)
        assert r.status_code == 502 and r.json()["detail"] == "Garmin answered 302."
        assert fake.foreign == []
        assert {urlsplit(str(q.url)).netloc for q in fake.calls} == {HOST}
        assert all(str(q.url).startswith(f"https://{HOST}/") for q in fake.calls)
        paths = {urlsplit(str(q.url)).path for q in fake.calls}
        assert {TOKEN_PATH, SLEEP_PATH, HR_PATH} <= paths
        assert all(x in (TOKEN_PATH, SLEEP_PATH, HR_PATH) or x.startswith(HRV_PATH) for x in paths)


def test_demo_refuses_garmin(tmp_path):
    fake = FakeGarmin()
    cfg = Config(data_dir=tmp_path, tz="America/New_York", demo=True, bed="sim", llm_url=None, auth=True)
    with TestClient(create_app(cfg)) as c:
        c.app.state.cloud.get("garmin").client = fake.client()
        for method, path, body in [("GET", "/api/cloud/providers", None), ("GET", "/api/cloud/me/garmin", None),
                                   ("POST", "/api/cloud/me/garmin/connect", {"person": "p1", "key": KEY}),
                                   ("POST", "/api/cloud/me/garmin/sync", {"person": "p1"}),
                                   ("POST", "/api/cloud/me/garmin/disconnect", {"person": "p1"}),
                                   ("GET", "/api/integrations/cloud/garmin", None),
                                   ("PUT", "/api/integrations/cloud/garmin", {"client_id": "cid_test"})]:
            r = c.request(method, path, json=body, headers=H)
            assert r.status_code == 403, (method, path, r.status_code)
        assert fake.calls == []


def test_nothing_readable_is_stored_or_returned(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeGarmin()
    with client(tmp_path) as c:
        garmin(c, fake)
        answers = [connect(c, expires=None).text, look(c).text]
        fake.revoke_tokens()                                       # a renewal too, so a second token exists
        answers.append(look(c).text)
        connect(c, person="p1")
        fake.refused.add("k-synthetic-refused")
        answers.append(connect(c, key="k-synthetic-refused").text)
        for path in ("/api/cloud/providers", "/api/cloud/me/garmin?person=p2", "/api/cloud/me/garmin?person=p1",
                     "/api/snapshot", "/api/export", "/api/integrations/cloud/garmin"):
            r = c.get(path)
            assert r.status_code == 200, path
            answers.append(r.text)
        rows = kv_rows(c)
        assert any(k == "garmin_token:p2" for k, _ in rows)
        secrets = [KEY, "k-synthetic-refused", *fake.issued]
        assert len(fake.issued) >= 3
        for s in secrets:
            for part in (s, s.split(".")[-1]):                     # the whole token and its signature part
                assert not any(part in v or part in k for k, v in rows), "stored readable"
                assert not any(part in a for a in answers), "answered"
                assert part not in caplog.text, "logged"
        sealed = json.loads(dict(rows)["garmin_token:p2"])
        assert set(sealed) == {"v", "sealed", "expires_at", "connected_at"} and sealed["sealed"].startswith("v1.")
        f = Path(tmp_path) / KEY_FILE
        assert f.stat().st_size == 32
        if os.name == "posix":
            assert f.stat().st_mode & 0o777 == 0o600


def test_lost_secret_asks_for_a_new_key(tmp_path):
    fake = FakeGarmin()
    with client(tmp_path) as c:
        garmin(c, fake)
        assert connect(c).status_code == 200
        look(c)
    (Path(tmp_path) / KEY_FILE).write_bytes(os.urandom(32))       # the server's secret was replaced
    n = len(fake.calls)
    with client(tmp_path) as c:                                    # a new process: the new secret is read
        garmin(c, fake)
        assert status(c)["connected"]                              # known without decrypting
        r = look(c)
        assert r.status_code == 502 and r.json()["detail"] == GONE
        s = status(c)
        assert s["needs_reconnect"] and s["last_error"] == GONE
        assert len(fake.calls) == n                                # nothing could be sent
        assert c.get("/api/snapshot").status_code == 200           # the server carries on
        assert ("p2", "Garmin", True) in c.app.state.notifier.account_states()
        assert connect(c).status_code == 200 and look(c).status_code == 200


def test_key_expiry_warning(tmp_path):
    fake = FakeGarmin()
    with client(tmp_path) as c:
        garmin(c, fake)
        at(c, "2026-11-01T12:00")
        for bad in ("2026-10-25", "2028-11-01", "30/11/2026", "2026-02-30", "soon"):
            r = connect(c, expires=bad)
            assert r.status_code == 422, bad
        assert fake.calls == []
        assert connect(c, expires="2026-11-30").status_code == 200
        look(c)
        at(c, "2026-11-22T12:00")
        s = status(c)
        assert s["key_expires"] == "2026-11-30" and not s["key_expires_soon"]
        at(c, "2026-11-23T09:00")
        assert status(c)["key_expires_soon"]
        at(c, "2026-12-02T12:00")                                  # past the date, Garmin still takes the key
        assert look(c).status_code == 200
        s = status(c)
        assert s["key_expires_soon"] and s["key_expires"] == "2026-11-30" and not s["needs_reconnect"]
        # no date, no warning
        assert connect(c, person="p1").status_code == 200
        s = status(c, "p1")
        assert s["key_expires"] is None and not s["key_expires_soon"]


def test_disconnect_removes_only_yours(tmp_path):
    fake = FakeGarmin()
    with client(tmp_path) as c:
        garmin(c, fake)
        connect(c, person="p1", expires="2027-01-01")
        connect(c, person="p2")
        look(c, person="p1")
        look(c, person="p2")
        store = c.app.state.cloud.c.store
        t = datetime(2026, 11, 5, 3, 0, tzinfo=TZ).timestamp()
        store.sample(t, "wear:p1:hr:Garmin", 55.0)                # something Garmin already sent
        n = len(fake.calls)
        assert c.post("/api/cloud/me/garmin/disconnect", json={"person": "p1"}, headers=H).json() == {"ok": True}
        assert len(fake.calls) == n                                # no call to Garmin
        assert store.get("garmin_token:p1") is None and not store.get("garmin_status:p1")
        s = status(c, "p1")
        assert not s["connected"] and s["key_expires"] is None and not s["key_expires_soon"]
        assert store.samples("wear:p1:hr:Garmin", 0, 2e9) == [(t, 55.0)]
        assert status(c, "p2")["connected"] and look(c, person="p2").status_code == 200


def test_busy_garmin_refuses_a_look_and_a_key(tmp_path):
    fake = FakeGarmin()
    with client(tmp_path) as c:
        garmin(c, fake)
        at(c, "2026-11-05T12:00")
        connect(c)
        look(c)
        fake.status_at[fake.data_calls + 1] = (429, {"retry-after": "120"})
        r = look(c)
        assert r.status_code == 429
        now = c.app.state.cloud.c.clock.now_ts()
        s = c.app.state.cloud.c.store.get("garmin_status:p2")
        assert abs(s["backoff_until"] - (now + 120)) < 5 and s["last_error"] == "Garmin is busy; trying again later."
        n = len(fake.calls)
        at(c, "2026-11-05T12:01")
        r = look(c)
        assert r.status_code == 429 and r.json()["detail"] == "Garmin is busy; trying again later."
        assert connect(c, key="k-synthetic-another-1").status_code == 429
        assert len(fake.calls) == n                                # neither reached Garmin
        at(c, "2026-11-05T12:03")
        assert look(c).status_code == 200


def test_requests_are_paced_one_at_a_time(tmp_path):
    fake = FakeGarmin()
    with client(tmp_path) as c:
        p = garmin(c, fake)
        connect(c)
        look(c)
        waits: list[float] = []

        async def nap(s):
            waits.append(s)
        p.pause_s, p._mono, p._sleep, p._last = 0.5, (lambda: 1000.0), nap, 1000.0   # a frozen clock: each call waits
        fake.revoke_tokens()
        n = len(fake.calls)
        assert look(c).status_code == 200                          # 401, renewal, retry, then six more data calls
        assert len(fake.calls) - n == 9
        assert waits == [0.5] * 9


def test_backoff_set_while_waiting_on_lock_skips_garmin(tmp_path, monkeypatch):
    fake = FakeGarmin()
    with client(tmp_path) as c:
        p = garmin(c, fake)
        at(c, "2026-11-05T12:00")
        assert connect(c).status_code == 200
        assert look(c).status_code == 200

        async def run():
            entered = asyncio.Event()
            release = asyncio.Event()
            orig_fetch = p.fetch

            async def slow_fetch(pid, start, end, now):
                entered.set()
                await release.wait()
                return await orig_fetch(pid, start, end, now)

            monkeypatch.setattr(p, "fetch", slow_fetch)
            t1 = asyncio.create_task(p.sync("p2"))
            await entered.wait()
            n = len(fake.calls)
            t2 = asyncio.create_task(p.sync("p2"))
            t3 = asyncio.create_task(p.connect_key("p2", "k-synthetic-another-1"))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            fake.status_at[fake.data_calls + 1] = (429, {"retry-after": "120"})
            release.set()
            for task in (t1, t2, t3):
                with pytest.raises(CloudError) as err:
                    await task
                assert err.value.status == 429
            assert len(fake.calls) - n == 1                         # only t1's 429 call; t2 and t3 made none

        c.portal.call(run)
