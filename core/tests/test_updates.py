"""Release manifest check, validation, settings, auth, demo, and rate-limit tests. No network."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from embertide import __version__, updates
from embertide.app import create_app
from embertide.config import Config

H = {"X-Embertide": "1"}
PW = "correct horse battery"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
CERT_SHA = "0123456789abcdef" * 4
TEST_URL = "https://github.com/test/ember-tide/releases/latest/download/latest.json"

_open: list[TestClient] = []


@pytest.fixture(autouse=True)
def _close_apps():
    yield
    while _open:
        _open.pop().__exit__(None, None, None)


def make(tmp_path, *, update_check: bool = True, demo: bool = False, auth: bool = True) -> TestClient:
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.tz = "America/New_York"
    cfg.bed = "sim"
    cfg.web_dir = tmp_path / "noweb"
    cfg.auth = auth
    cfg.demo = demo
    cfg.update_check = update_check
    cfg.update_url = TEST_URL
    c = TestClient(create_app(cfg))
    c.__enter__()
    _open.append(c)
    if auth and not demo:
        assert c.post("/api/auth/setup", json={"username": "alex", "password": PW, "person": "p1"}).status_code == 200
    return c


def mock_http(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(updates.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def valid_manifest(version: str = "99.0.0", version_code: int = 990000) -> dict:
    return {
        "version": version,
        "version_code": version_code,
        "notes_url": f"https://github.com/test/ember-tide/releases/tag/v{version}",
        "cert_sha256": CERT_SHA.upper(),
        "extra_top_key": "ignored",
        "android": {
            "full": {
                "url": f"https://github.com/test/ember-tide/releases/download/v{version}/ember-tide-{version}-android-google-push.apk",
                "sha256": SHA_A,
                "size": 1000,
                "extra_flavor_key": 123,
            },
            "minimal": {
                "url": f"https://github.com/test/ember-tide/releases/download/v{version}/ember-tide-{version}-android-no-google.apk",
                "sha256": SHA_B,
                "size": 900,
            },
            "wear": {
                "url": f"https://github.com/test/ember-tide/releases/download/v{version}/ember-tide-{version}-wear-os.apk",
                "sha256": SHA_C,
                "size": 500,
            },
            "desktop": {
                "url": "https://github.com/test/ember-tide/releases/download/v99.0.0/other.zip",
                "sha256": SHA_A,
                "size": 42,
            },
        },
    }


def test_newer():
    assert updates.newer("0.1.13", "0.1.12") is True
    assert updates.newer("0.2.0", "0.1.99") is True
    assert updates.newer("1.0.0", "0.99.99") is True
    assert updates.newer("0.1.12", "0.1.12") is False
    assert updates.newer("0.1.11", "0.1.12") is False
    assert updates.newer(None, "0.1.12") is False
    assert updates.newer("0.1.13\n", "0.1.12") is False
    assert updates.newer("1.2", "0.1.12") is False


def test_valid_manifest_available_and_same_version(tmp_path, monkeypatch):
    c = make(tmp_path)
    seen: list[tuple[str, str]] = []
    current_payload = [valid_manifest("99.0.0", 990000)]

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((str(req.url), req.headers.get("user-agent", "")))
        return httpx.Response(200, json=current_payload[0])

    mock_http(monkeypatch, handler)

    r = c.post("/api/update/check", headers=H)
    assert r.status_code == 200
    g = r.json()
    assert seen == [(TEST_URL, f"EmberTide/{__version__}")]
    assert g["current"] == __version__
    assert g["enabled"] is True and g["env_enabled"] is True
    assert g["latest"] == "99.0.0"
    assert g["version_code"] == 990000
    assert g["available"] is True
    assert g["error"] is None
    assert isinstance(g["checked_at"], float)
    assert set(g["android"]) == {"full", "minimal", "wear"}
    assert "extra_flavor_key" not in g["android"]["full"]

    # Same version as running server -> available is False
    current_payload[0] = valid_manifest(__version__, 112)
    st = c.app.state.et_state
    c.portal.call(updates.check_once, st.cfg, st.controller.store)
    g2 = c.get("/api/update").json()
    assert g2["latest"] == __version__
    assert g2["available"] is False


@pytest.mark.parametrize("bad_resp", [
    lambda: httpx.Response(200, json={**valid_manifest(), "notes_url": "https://evil.invalid/notes"}),
    lambda: httpx.Response(200, json={**valid_manifest(), "android": {"minimal": {"url": "https://evil.invalid/a.apk", "sha256": SHA_B, "size": 10}}}),
    lambda: httpx.Response(200, json={**valid_manifest(), "android": {"minimal": {"url": "http://github.com/test/a.apk", "sha256": SHA_B, "size": 10}}}),
    lambda: httpx.Response(200, json={**valid_manifest(), "android": {"minimal": {"url": "https://github.com/test/a.apk", "sha256": "deadbeef", "size": 10}}}),
    lambda: httpx.Response(200, json={**valid_manifest(), "cert_sha256": "not-64-hex"}),
    lambda: httpx.Response(200, json={**valid_manifest(), "version": "99.0"}),
    lambda: httpx.Response(200, json={**valid_manifest(), "version_code": 0}),
    lambda: httpx.Response(200, json={**valid_manifest(), "version_code": True}),
    lambda: httpx.Response(200, json={**valid_manifest(), "android": {"minimal": {"url": "https://github.com/test/a.apk", "sha256": SHA_B, "size": 0}}}),
    lambda: httpx.Response(200, content=b"{" + b"x" * (65 * 1024)),
    lambda: httpx.Response(200, text="not json"),
    lambda: httpx.Response(503, text="unavailable"),
])
def test_invalid_manifest_rejected_and_previous_kept(tmp_path, monkeypatch, bad_resp):
    c = make(tmp_path)
    st = c.app.state.et_state
    mode = ["good"]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=valid_manifest("99.0.0", 990000)) if mode[0] == "good" else bad_resp()

    mock_http(monkeypatch, handler)
    assert c.post("/api/update/check", headers=H).json()["latest"] == "99.0.0"

    mode[0] = "bad"
    c.portal.call(updates.check_once, st.cfg, st.controller.store)
    g = c.get("/api/update").json()
    assert g["latest"] == "99.0.0"
    assert g["version_code"] == 990000
    assert g["available"] is True
    assert g["notes_url"] == "https://github.com/test/ember-tide/releases/tag/v99.0.0"
    assert set(g["android"]) == {"full", "minimal", "wear"}
    assert isinstance(g["error"], str) and len(g["error"]) > 0
    assert isinstance(g["checked_at"], float)


def test_env_off_and_store_off_make_no_network_call(tmp_path, monkeypatch):
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, json=valid_manifest())

    mock_http(monkeypatch, handler)

    # ET_UPDATE_CHECK=off -> no network call
    c_off = make(tmp_path / "env_off", update_check=False)
    g = c_off.get("/api/update").json()
    assert g["env_enabled"] is False and g["enabled"] is False
    assert c_off.post("/api/update/check", headers=H).status_code == 200
    st_off = c_off.app.state.et_state
    assert c_off.portal.call(updates.check_once, st_off.cfg, st_off.controller.store) is None
    assert calls == []

    # Store switch off -> no network call
    c_on = make(tmp_path / "store_off", update_check=True)
    assert c_on.put("/api/update/settings", json={"enabled": True, "extra": 1}, headers=H).status_code == 422
    g2 = c_on.put("/api/update/settings", json={"enabled": False}, headers=H).json()
    assert g2["env_enabled"] is True and g2["enabled"] is False
    assert c_on.post("/api/update/check", headers=H).status_code == 200
    st_on = c_on.app.state.et_state
    assert c_on.portal.call(updates.check_once, st_on.cfg, st_on.controller.store) is None
    assert calls == []


def test_demo_never_checks_and_blocks_write_routes(tmp_path, monkeypatch):
    class _FailAsyncClient:
        def __init__(self, *a, **kw):
            raise AssertionError("outbound httpx.AsyncClient is forbidden in demo mode")

    monkeypatch.setattr(httpx, "AsyncClient", _FailAsyncClient)
    c = make(tmp_path, demo=True, auth=False)

    g = c.get("/api/update")
    assert g.status_code == 200
    assert g.json() == {
        "current": __version__,
        "enabled": False,
        "env_enabled": False,
        "latest": None,
        "version_code": None,
        "available": False,
        "notes_url": None,
        "checked_at": None,
        "error": None,
        "android": {},
    }

    r_put = c.put("/api/update/settings", json={"enabled": True}, headers=H)
    assert r_put.status_code == 403 and r_put.json() == {"detail": "Not available in the demo."}
    r_post = c.post("/api/update/check", headers=H)
    assert r_post.status_code == 403 and r_post.json() == {"detail": "Not available in the demo."}

    st = c.app.state.et_state
    assert c.portal.call(updates.check_once, st.cfg, st.controller.store) is None


def test_owner_only_and_sleeper_can_read(tmp_path, monkeypatch):
    c = make(tmp_path)
    mock_http(monkeypatch, lambda req: httpx.Response(200, json=valid_manifest()))

    code = c.post("/api/auth/invites", json={"person": "p2", "role": "sleeper"}, headers=H).json()["code"]
    sleeper = TestClient(c.app)
    assert sleeper.post("/api/auth/accept", json={"code": code, "username": "sam", "password": PW}).status_code == 200

    anon = TestClient(c.app)
    assert anon.get("/api/update").status_code == 401

    assert sleeper.get("/api/update").status_code == 200
    assert sleeper.put("/api/update/settings", json={"enabled": False}, headers=H).status_code == 403
    assert sleeper.post("/api/update/check", headers=H).status_code == 403

    assert c.put("/api/update/settings", json={"enabled": True}, headers=H).status_code == 200
    assert c.post("/api/update/check", headers=H).status_code == 200


def test_rate_limit_60s(tmp_path, monkeypatch):
    c = make(tmp_path)
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=valid_manifest())

    mock_http(monkeypatch, handler)
    now = [1000.0]
    monkeypatch.setattr(updates, "_monotonic", lambda: now[0])

    assert c.post("/api/update/check", headers=H).status_code == 200
    assert calls == 1

    r2 = c.post("/api/update/check", headers=H)
    assert r2.status_code == 429
    assert isinstance(r2.json().get("detail"), str) and r2.json()["detail"]
    assert calls == 1

    now[0] += 61.0
    assert c.post("/api/update/check", headers=H).status_code == 200
    assert calls == 2
