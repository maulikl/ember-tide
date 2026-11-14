"""Daily release manifest check and update settings API."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from . import __version__
from .config import Config
from .store import Store

log = logging.getLogger("embertide.updates")

SETTINGS_KEY = "update_settings"
LATEST_KEY = "update_latest"
MAX_BODY_BYTES = 64 * 1024
INITIAL_DELAY_S = 120
CHECK_INTERVAL_S = 24 * 3600
CHECK_JITTER_S = 30 * 60
MANUAL_COOLDOWN_S = 60
VALID_FLAVORS = ("full", "minimal", "wear")

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_monotonic = time.monotonic


class UpdateSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def _ver_tuple(v: str | None) -> tuple[int, int, int] | None:
    if not isinstance(v, str) or not VERSION_RE.fullmatch(v):
        return None
    a, b, c = v.split(".")
    return (int(a), int(b), int(c))


def newer(a: str | None, b: str | None) -> bool:
    """Return True when X.Y.Z version `a` is strictly newer than `b`."""
    ta, tb = _ver_tuple(a), _ver_tuple(b)
    return ta is not None and tb is not None and ta > tb


def is_enabled(store: Store) -> bool:
    """Whether the owner's update-check switch is on (defaults to True)."""
    saved = store.get(SETTINGS_KEY)
    if not isinstance(saved, dict):
        return True
    return bool(saved.get("enabled", True))


def _is_github_url(val: object) -> bool:
    return (
        isinstance(val, str)
        and len(val) <= 500
        and val.lower().startswith("https://github.com/")
        and not any(ch.isspace() or ord(ch) < 32 for ch in val)
    )


def _is_sha256(val: object) -> bool:
    return isinstance(val, str) and bool(SHA256_RE.fullmatch(val))


def _is_pos_int(val: object) -> bool:
    return isinstance(val, int) and not isinstance(val, bool) and val > 0


def validate_manifest(data: object) -> dict:
    """Validate a downloaded release manifest strictly and return normalised fields."""
    if not isinstance(data, dict):
        raise ValueError("The update manifest didn't look right.")
    version = data.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ValueError("The update manifest didn't look right.")
    version_code = data.get("version_code")
    if not _is_pos_int(version_code):
        raise ValueError("The update manifest didn't look right.")
    notes_url = data.get("notes_url")
    if not _is_github_url(notes_url):
        raise ValueError("The update manifest didn't look right.")
    cert = data.get("cert_sha256")
    if cert is not None and not _is_sha256(cert):
        raise ValueError("The update manifest didn't look right.")

    raw_android = data.get("android")
    android: dict[str, dict] = {}
    if raw_android is not None:
        if not isinstance(raw_android, dict):
            raise ValueError("The update manifest didn't look right.")
        for flavor, entry in raw_android.items():
            if flavor not in VALID_FLAVORS or entry is None:
                continue
            if not isinstance(entry, dict):
                raise ValueError("The update manifest didn't look right.")
            url = entry.get("url")
            sha = entry.get("sha256")
            size = entry.get("size")
            if not (_is_github_url(url) and _is_sha256(sha) and _is_pos_int(size)):
                raise ValueError("The update manifest didn't look right.")
            android[flavor] = {"url": url, "sha256": sha.lower(), "size": size}

    return {
        "version": version,
        "version_code": version_code,
        "notes_url": notes_url,
        "cert_sha256": cert.lower() if cert is not None else None,
        "android": android,
    }


def _explain_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "Update check timed out."
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Update check failed (HTTP {exc.response.status_code})."
    if isinstance(exc, httpx.RequestError):
        return "Couldn't reach GitHub to check for updates."
    if isinstance(exc, ValueError):
        return str(exc)
    return "Couldn't check for updates right now."


async def _fetch_manifest(url: str) -> dict:
    headers = {"User-Agent": f"EmberTide/{__version__}"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            cl = resp.headers.get("content-length")
            if cl is not None:
                try:
                    length = int(cl)
                except ValueError:
                    raise ValueError("The update manifest didn't look right.") from None
                if length > MAX_BODY_BYTES:
                    raise ValueError("The update manifest was too large.")
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_BODY_BYTES:
                    raise ValueError("The update manifest was too large.")
    try:
        data = json.loads(buf.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("The update manifest didn't look right.") from None
    return validate_manifest(data)


async def check_once(cfg: Config, store: Store) -> dict | None:
    """Fetch and store the latest release manifest when update checks are enabled."""
    if cfg.demo or not cfg.update_check or not is_enabled(store):
        return None
    now = time.time()
    prev = store.get(LATEST_KEY)
    prev = prev if isinstance(prev, dict) else {}
    try:
        parsed = await _fetch_manifest(cfg.update_url)
    except Exception as exc:  # noqa: BLE001
        record = {
            "checked_at": now,
            "version": prev.get("version"),
            "version_code": prev.get("version_code"),
            "notes_url": prev.get("notes_url"),
            "cert_sha256": prev.get("cert_sha256"),
            "android": prev.get("android") if isinstance(prev.get("android"), dict) else {},
            "error": _explain_error(exc),
        }
        store.put(LATEST_KEY, record)
        return record

    record = {
        "checked_at": now,
        "version": parsed["version"],
        "version_code": parsed["version_code"],
        "notes_url": parsed["notes_url"],
        "cert_sha256": parsed["cert_sha256"],
        "android": parsed["android"],
        "error": None,
    }
    store.put(LATEST_KEY, record)
    return record


async def update_loop(cfg: Config, store: Store) -> None:
    """Background loop: check 120 s after startup, then roughly once a day."""
    if cfg.demo:
        return
    try:
        await asyncio.sleep(INITIAL_DELAY_S)
        while True:
            try:
                await check_once(cfg, store)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("update check failed")
            await asyncio.sleep(CHECK_INTERVAL_S + random.uniform(0, CHECK_JITTER_S))
    except asyncio.CancelledError:
        return


def build_view(cfg: Config, store: Store) -> dict:
    """Build the GET /api/update response payload."""
    if cfg.demo:
        return {
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
    saved = store.get(LATEST_KEY)
    saved = saved if isinstance(saved, dict) else {}
    latest = saved.get("version")
    env_enabled = bool(cfg.update_check)
    return {
        "current": __version__,
        "enabled": env_enabled and is_enabled(store),
        "env_enabled": env_enabled,
        "latest": latest,
        "version_code": saved.get("version_code"),
        "available": newer(latest, __version__),
        "notes_url": saved.get("notes_url"),
        "checked_at": saved.get("checked_at"),
        "error": saved.get("error"),
        "android": saved.get("android") if isinstance(saved.get("android"), dict) else {},
    }


def build_update_router(state) -> APIRouter:
    r = APIRouter(prefix="/api/update")
    last_check: float | None = None

    @r.get("")
    async def get_update():
        return build_view(state.cfg, state.controller.store)

    @r.put("/settings")
    async def put_settings(b: UpdateSettingsBody):
        state.controller.store.put(SETTINGS_KEY, {"enabled": b.enabled})
        return build_view(state.cfg, state.controller.store)

    @r.post("/check")
    async def post_check():
        nonlocal last_check
        store = state.controller.store
        if state.cfg.demo or not state.cfg.update_check or not is_enabled(store):
            return build_view(state.cfg, store)
        now = _monotonic()
        if last_check is not None and now - last_check < MANUAL_COOLDOWN_S:
            raise HTTPException(429, "Checked a moment ago — wait a minute before checking again.")
        last_check = now
        await check_once(state.cfg, store)
        return build_view(state.cfg, store)

    return r
