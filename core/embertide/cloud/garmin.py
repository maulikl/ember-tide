"""Garmin, through each person's own Personal API key.

The person makes a key at Garmin Connect and pastes it here. There are no owner credentials and no sign-in page: the
key is exchanged for a token (valid about 24 hours), the token is sent with each data call, and a new token is asked
for with the key when less than an hour of the old one is left. The key and the token are stored only sealed
(`sealed.py`, bound to the row `garmin:<person>`); the token's end time is kept in clear so deciding whether to renew
needs no decryption.

Every request goes through `_send()`: one fixed host, no redirects, a 20 s timeout, and one request at a time for the
whole provider with a pause between them. A 429 stops the sync and backs off; while backing off, a manual look and a
new key are refused without calling Garmin. Neither the key nor the token is ever put in a URL, a log line, an
exception text or a status.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from .base import UA, CloudConnector, CloudData, CloudError, retry_after
from .oura import morning
from .sealed import Sealer, SealError

log = logging.getLogger(__name__)

HOST = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
SLEEP_PATH = "/sleep-service/sleep/dailySleepSummariesByDate"
HR_PATH = "/wellness-service/wellness/dailyHeartRate"
HRV_PATH = "/hrv-service/hrv/"                       # + the calendar date
RENEW_BEFORE_S = 3600
KEY_MAX = 1024
TOKEN_MAX = 8192
DEFAULT_LIFE_S = 86400.0
MAX_LIFE_S = 366 * 86400.0
TIMEOUT_S = 20.0
EXPIRY_AHEAD_DAYS = 366
EXPIRY_WARN_DAYS = 7
_VISIBLE = re.compile(r"^[\x21-\x7e]+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

BUSY = "Garmin is busy; trying again later."
BAD_KEY = "That doesn't look like a Garmin key. Copy all of it from Garmin Connect, with no spaces, and paste it here."
BAD_DATE = "Choose the date the key runs out, from today to a year ahead."
NO_TOKEN = "Garmin didn't send a token."
UNREACHABLE = "Couldn't reach Garmin."
UNSAFE = "This server couldn't keep the key safe, so it wasn't saved. Check that its data folder can be written."
SKIP = object()                         # _get(soft=True): Garmin refused this kind of data; the caller skips it


def visible(v: object, most: int) -> bool:
    """1 to `most` visible ASCII characters: no space, no line break, nothing that could break a header."""
    return isinstance(v, str) and 0 < len(v) <= most and bool(_VISIBLE.match(v))


def claims(token: str) -> dict:
    """The JWT's claims, unchecked (the token only goes back to Garmin), or {} when they can't be read."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        out = json.loads(raw)
    except Exception:  # noqa: BLE001  (anything unreadable means "no claims")
        return {}
    return out if isinstance(out, dict) else {}


def _num(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def lifetime(token: str, expires_in: object = None) -> float:
    """Seconds the token is good for: exp - iat, else exp - now, else expires_in, else 24 hours.

    A lifetime, not an end time: the end time is the home clock's now plus this, because the simulator's clock may
    run fast or jump while Garmin's exp is real epoch time."""
    c = claims(token)
    exp, iat = _num(c.get("exp")), _num(c.get("iat"))
    for life in ((exp - iat) if exp is not None and iat is not None else None,
                 (exp - time.time()) if exp is not None else None,
                 _num(expires_in) if not isinstance(expires_in, str) else _num(_float(expires_in))):
        if life is not None and life > 0:
            return min(life, MAX_LIFE_S)
    return DEFAULT_LIFE_S


def _float(v: str) -> float | None:
    try:
        return float(v)
    except ValueError:
        return None


class Garmin(CloudConnector):
    id = "garmin"
    name = "Garmin"
    device = "Garmin"
    connect_kind = "key"
    refreshes = False
    first_days = 14
    data_version = 1
    pause_s = 0.5
    gives = "sleep and nap times with the time in each stage, heart rate and HRV through the night, resting heart rate"
    docs_url = "https://developer.garmin.com/gc-developer-program/personal/personal-api/"

    def __init__(self, controller, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(controller, client)
        self._gate = asyncio.Lock()
        self._last: float | None = None
        self._forced: dict[str, int] = {}       # forced exchanges after a 401, per person, reset at each sync
        self._sleep = asyncio.sleep
        self._mono = time.monotonic
        self._sealer: Sealer | None = None

    # ---------- what's different about a key provider ----------
    @property
    def configured(self) -> bool:
        return True                             # nothing for the owner to set up

    @property
    def reconnect_text(self) -> str:
        return "Your Garmin key has run out or was deleted. Make a new one at Garmin Connect and paste it here."

    @property
    def sealer(self) -> Sealer:
        if self._sealer is None:
            path = getattr(self.store, "path", ":memory:")
            self._sealer = Sealer(None if path == ":memory:" else Path(path).parent)
        return self._sealer

    def bound(self, pid: str) -> str:
        return f"garmin:{pid}"

    def _today(self) -> date:
        return datetime.fromtimestamp(self.c.clock.now_ts(), self.c.clock.tz).date()

    # ---------- checks ----------
    @staticmethod
    def check_key(key: object) -> str:
        if not visible(key, KEY_MAX):
            raise CloudError(BAD_KEY, 422)
        return key  # type: ignore[return-value]

    def check_expires(self, text: object) -> str | None:
        if text is None or text == "":
            return None
        if not isinstance(text, str) or not _DATE.match(text):
            raise CloudError(BAD_DATE, 422)
        try:
            d = date.fromisoformat(text)
        except ValueError:
            raise CloudError(BAD_DATE, 422) from None
        today = self._today()
        if not today <= d <= today + timedelta(days=EXPIRY_AHEAD_DAYS):
            raise CloudError(BAD_DATE, 422)
        return d.isoformat()

    def _backing_off(self, pid: str) -> bool:
        st = self.store.get(self.k("status", pid), {}) or {}
        return self.c.clock.now_ts() < (st.get("backoff_until") or 0)

    # ---------- the one door to Garmin ----------
    async def _send(self, method: str, path: str, auth: str, params: dict | None = None,
                    accept: str = "application/json") -> httpx.Response:
        """One request to the fixed host. The gate is held for this request only (pause, send, read), never across a
        token exchange: `_get()` and `fresh_token()` each come through here separately."""
        async with self._gate:
            if self._last is not None and self.pause_s > 0:
                wait = self.pause_s - (self._mono() - self._last)
                if wait > 0:
                    await self._sleep(wait)
            try:
                async with self._http() as c:
                    r = await c.request(method, HOST + path, params=params, follow_redirects=False, timeout=TIMEOUT_S,
                                        headers={**UA, "Accept": accept, "Authorization": auth})
                    await r.aread()
            finally:
                self._last = self._mono()
        return r

    async def _exchange(self, key: str) -> tuple[str, float]:
        """The key for a token: (token, end time on the home clock)."""
        # the token comes back as text/plain: Garmin answers 406 to "Accept: application/json" here (checked live 2026-11-23)
        r = await self._send("POST", TOKEN_PATH, f"ApiKey {key}", accept="*/*")
        if r.status_code == 429:
            raise CloudError(BUSY, 429, retry_after(r))
        if r.status_code in (400, 401, 403):
            raise CloudError(self.reconnect_text, 401)
        if r.status_code >= 300:
            raise CloudError(f"Garmin answered {r.status_code}.", r.status_code)
        text = r.text.strip()
        token, expires_in = text, None
        try:
            body = json.loads(text) if text else None
        except ValueError:
            body = None                          # a bare token (what Garmin sends today) isn't JSON
        if isinstance(body, dict):
            token, expires_in = body.get("access_token"), body.get("expires_in")
        elif isinstance(body, str):
            token = body
        elif body is not None:
            token = None
        if not visible(token, TOKEN_MAX):
            raise CloudError(NO_TOKEN)
        return token, self.c.clock.now_ts() + lifetime(token, expires_in)  # type: ignore[arg-type]

    def _save(self, pid: str, key: str, token: str, expires_at: float, connected_at: float) -> None:
        sealed = self.sealer.seal({"key": key, "token": token}, self.bound(pid))   # raises SealError
        self.store.put(self.k("token", pid), {"v": 1, "sealed": sealed, "expires_at": expires_at, "connected_at": connected_at})

    # ---------- per person ----------
    def connected(self, pid: str) -> bool:
        return bool((self.token(pid) or {}).get("sealed"))

    async def connect_key(self, pid: str, key: object, expires: object = None) -> None:
        """Check, exchange once, then seal and save: nothing is written unless both worked. Replaces a working key only
        once the new one has been accepted."""
        key = self.check_key(key)
        until = self.check_expires(expires)
        if self._backing_off(pid):
            raise CloudError(BUSY, 429)
        async with self._locks.setdefault(pid, asyncio.Lock()):
            if self._backing_off(pid):
                raise CloudError(BUSY, 429)                    # another look hit 429 while this one waited on the lock
            try:
                token, expires_at = await self._exchange(key)
            except CloudError as e:
                if e.status == 429:
                    now = self.c.clock.now_ts()
                    self._status(pid, last_error=BUSY, backoff_until=now + (e.retry_after or 900))
                raise
            except httpx.HTTPError:
                raise CloudError(UNREACHABLE) from None
            try:
                self._save(pid, key, token, expires_at, time.time())
            except SealError:
                raise CloudError(UNSAFE) from None
            self._status(pid, needs_reconnect=False, last_error=None, key_expires=until)

    async def fresh_token(self, pid: str, force: bool = False) -> str:
        t = self.token(pid) or {}
        if not t.get("sealed"):
            raise CloudError("Garmin isn't connected.", 401)
        val = self.sealer.open(t.get("sealed"), self.bound(pid))
        if not val or not visible(val.get("key"), KEY_MAX):
            raise CloudError(self.reconnect_text, 401)         # the server's secret was lost or replaced
        tok = val.get("token")
        left = (_num(t.get("expires_at")) or 0) - self.c.clock.now_ts()
        if not force and visible(tok, TOKEN_MAX) and left > RENEW_BEFORE_S:
            return tok
        tok, expires_at = await self._exchange(val["key"])
        try:
            self._save(pid, val["key"], tok, expires_at, t.get("connected_at") or time.time())
        except SealError:
            raise CloudError(UNSAFE) from None
        return tok

    async def _get(self, pid: str, path: str, params: dict | None = None, soft: bool = False):
        """One data call: the parsed JSON body, None for no data (204, 404, empty or unreadable), or SKIP when `soft`
        and Garmin refused this kind of data (400/403). A 401 renews the token once per sync and retries once."""
        tok = await self.fresh_token(pid)
        r = await self._send("GET", path, f"Bearer {tok}", params)
        if r.status_code == 401:
            if self._forced.get(pid, 0) >= 1:
                raise CloudError(self.reconnect_text, 401)
            self._forced[pid] = self._forced.get(pid, 0) + 1
            tok = await self.fresh_token(pid, force=True)
            r = await self._send("GET", path, f"Bearer {tok}", params)
        if r.status_code == 429:
            raise CloudError(BUSY, 429, retry_after(r))
        if r.status_code == 401:
            raise CloudError(self.reconnect_text, 401)
        if r.status_code in (204, 404):
            return None
        if soft and r.status_code in (400, 403):
            log.info("garmin: a data call answered %s; skipped for this sync", r.status_code)
            return SKIP
        if r.status_code >= 300:
            raise CloudError(f"Garmin answered {r.status_code}.", r.status_code)
        if not r.content.strip():
            return None
        try:
            return r.json()
        except ValueError:
            return None

    async def fetch(self, pid: str, start: date, end: date, now: float) -> CloudData:
        """D11: one sleep call for the window, then one heart-rate and one HRV call per day from the window's start to
        today (the day after today that `_sync()` adds is not asked for). A 429 or a dead key raises at once, so no
        further call is made. A refused heart-rate or HRV call (400/403) skips that kind for the rest of this sync
        (D13). Each answer is parsed on its own: one that can't be read is skipped and logged by exception type only."""
        tz = self.c.clock.tz
        today = datetime.fromtimestamp(now, tz).date()
        last = min(end, today)
        out = CloudData()
        nights: dict[str, dict] = {}                       # calendar date -> that date's night period
        body = await self._get(pid, SLEEP_PATH, {"startDate": start.isoformat(), "endDate": last.isoformat()})
        for s in sleep_rows(body):
            try:
                n = night(s)
                if n is not None:
                    out.periods.append(n)
                    day = _night_date(s, n["end"], tz)
                    prev = nights.get(day)
                    if prev is None or n["end"] - n["start"] > prev["end"] - prev["start"]:
                        nights[day] = n
                    for metric, v in night_numbers(s).items():
                        out.add(metric, n["end"], v)       # the night's own numbers, stamped at its end
                naps = s.get("dailyNapDTOS")
                for x in naps if isinstance(naps, list) else []:     # read even when the night isn't usable
                    p = nap(x)
                    if p is not None:
                        out.periods.append(p)
            except Exception as e:  # noqa: BLE001  (one unreadable sleep is skipped, the rest is kept)
                log.info("garmin: a sleep was skipped (%s)", type(e).__name__)
        refused: set[str] = set()
        d = start
        while d <= last:
            day = d.isoformat()
            for kind in ("heart rate", "hrv"):
                if kind in refused:
                    continue
                if kind == "hrv":
                    body = await self._get(pid, HRV_PATH + day, soft=True)
                else:
                    body = await self._get(pid, HR_PATH, {"date": day}, soft=True)
                if body is SKIP:
                    refused.add(kind)
                    continue
                try:
                    if kind == "hrv":
                        pts, avg = hrv(body)
                        for t, v in pts:
                            out.add("hrv", t, v)
                        if avg is not None and day in nights:
                            nights[day]["hrv_avg"] = avg
                    else:
                        pts, rest = heart(body)
                        for t, v in pts:
                            out.add("hr", t, v)
                        t = rest_stamp(day, nights, today, tz)
                        if rest is not None and t is not None:
                            out.add("resting_hr", t, rest)
                except Exception as e:  # noqa: BLE001
                    log.info("garmin: a day's %s was skipped (%s)", kind, type(e).__name__)
            d += timedelta(days=1)
        return out

    async def sync(self, pid: str, days: int | None = None) -> dict:
        if self._backing_off(pid):
            raise CloudError(BUSY, 429)                        # a manual look during a back-off: no call to Garmin
        async with self._locks.setdefault(pid, asyncio.Lock()):
            if self._backing_off(pid):
                raise CloudError(BUSY, 429)                    # another look hit 429 while this one waited on the lock
            self._forced[pid] = 0
            try:
                return await self._sync(pid, days)
            finally:
                self._forced.pop(pid, None)

    async def disconnect(self, pid: str) -> None:
        async with self._locks.setdefault(pid, asyncio.Lock()):   # waits for a running look, so it can't write back
            await super().disconnect(pid)                          # no revoke address: no request to Garmin

    def status(self, pid: str) -> dict:
        out = super().status(pid)
        st = self.store.get(self.k("status", pid), {}) or {}
        until = st.get("key_expires") if self.connected(pid) else None
        soon = False
        if isinstance(until, str):
            try:
                soon = (date.fromisoformat(until) - self._today()).days <= EXPIRY_WARN_DAYS
            except ValueError:
                until = None
        out.update(connect="key", key_expires=until or None, key_expires_soon=soon)
        return out


# ---------- parsers: plain values in, never an exception out ----------
T_MIN, T_MAX = 1e8, 1e11                    # seconds: 1973 to 5138; anything else isn't a time
_DIGITS = re.compile(r"^\d{1,17}$")


def gts(v: object) -> float | None:
    """Epoch seconds from epoch milliseconds (above 1e11), epoch seconds (above 1e8), digits as text, or ISO text.
    Text without an offset is UTC (Garmin's GMT fields carry none), never the server's local time."""
    if isinstance(v, str):
        v = v.strip()
        if _DIGITS.match(v):
            return gts(int(v))
        if not 0 < len(v) <= 64:
            return None
        try:
            d = datetime.fromisoformat(v[:-1] + "+00:00" if v[-1:] in ("Z", "z") else v)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            t = d.timestamp()
        except (ValueError, OverflowError, OSError):
            return None
        return t if T_MIN < t < T_MAX else None
    t = _num(v)
    if t is None:
        return None
    if t > T_MAX:
        t /= 1000.0
    return t if T_MIN < t < T_MAX else None


def _in(v: object, lo: float, hi: float) -> float | None:
    n = _num(v)
    return n if n is not None and lo <= n <= hi else None


def _mins(v: object) -> int | None:
    n = _num(v)
    return round(n / 60) if n is not None and 0 <= n <= 86400 * 2 else None


def _obj(v: object) -> dict:
    return v if isinstance(v, dict) else {}


def sleep_rows(body: object) -> list[dict]:
    """The sleeps in an answer: a list of objects, one object, or objects wrapping one under `dailySleepDTO`."""
    rows = body if isinstance(body, list) else [body] if isinstance(body, dict) and body else []
    out = []
    for r in rows:
        if isinstance(r, dict) and isinstance(r.get("dailySleepDTO"), dict):
            r = r["dailySleepDTO"]
        if isinstance(r, dict) and r:
            out.append(r)
    return out


def _sid(v: object) -> str | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float) and math.isfinite(v) and v == int(v):
        return str(int(v))
    return v if visible(v, 64) else None


def _period(kind: str, pid: str, t0: float, t1: float) -> dict:
    return {"id": f"garmin:{pid}", "source": "garmin", "device": "Garmin", "kind": kind, "start": t0, "end": t1,
            "asleep_min": None, "deep_min": None, "rem_min": None, "light_min": None, "awake_min": None,
            "efficiency": None, "latency_min": None, "hr_avg": None, "hr_low": None, "hrv_avg": None, "breath": None,
            "segments": []}


def night(s: object) -> dict | None:
    """A night's sleep (engine/sleeps.py shape) with the minutes in each stage, or None without a usable start and
    end. Garmin gives no stage timeline, so `segments` stays empty (D12)."""
    s = _obj(s)
    t0, t1 = gts(s.get("sleepStartTimestampGMT")), gts(s.get("sleepEndTimestampGMT"))
    if t0 is None or t1 is None or t1 <= t0:
        return None
    x = _period("night", _sid(s.get("id")) or str(int(t0)), t0, t1)
    x.update(asleep_min=_mins(s.get("sleepTimeSeconds")), deep_min=_mins(s.get("deepSleepSeconds")),
             light_min=_mins(s.get("lightSleepSeconds")), rem_min=_mins(s.get("remSleepSeconds")),
             awake_min=_mins(s.get("awakeSleepSeconds")), hr_avg=_in(s.get("avgHeartRate"), 20, 250),
             breath=_in(s.get("averageRespirationValue"), 4, 40))
    return x


def nap(n: object) -> dict | None:
    n = _obj(n)
    t0, t1 = gts(n.get("napStartTimestampGMT")), gts(n.get("napEndTimestampGMT"))
    if t0 is None or t1 is None or t1 <= t0:
        return None
    x = _period("nap", str(int(t0)), t0, t1)
    x["asleep_min"] = _mins(n.get("napTimeSec"))
    return x


def night_numbers(s: object) -> dict[str, float]:
    """The sleep score, breathing rate and blood oxygen, each only inside a plausible range."""
    s = _obj(s)
    out = {"sleep_score": _in(_obj(_obj(s.get("sleepScores")).get("overall")).get("value"), 0, 100),
           "night_breath": _in(s.get("averageRespirationValue"), 4, 40),
           "spo2": _in(s.get("averageSpO2Value"), 50, 100)}
    return {k: v for k, v in out.items() if v is not None}


def _night_date(s: dict, end: float, tz) -> str:
    d = s.get("calendarDate")
    if isinstance(d, str) and _DATE.match(d):
        return d
    return datetime.fromtimestamp(end, tz).date().isoformat()


def rest_stamp(day: str, nights: dict[str, dict], today: date, tz) -> float | None:
    """D14: at that date's night's end; for a past date without one, 07:00 local; for today without one, not yet."""
    if day in nights:
        return nights[day]["end"]
    if day >= today.isoformat():
        return None
    return morning(day, tz)


def _column(desc: object, word: str, default: int) -> tuple[int, str | None]:
    for d in desc if isinstance(desc, list) else []:
        d = _obj(d)
        k, i = d.get("key"), d.get("index")
        if isinstance(k, str) and word in k.lower() and isinstance(i, int) and not isinstance(i, bool) and 0 <= i < 64:
            return i, k
    return default, None


def heart(body: object) -> tuple[list[tuple[float, float]], float | None]:
    """A day's heart rate: ([(t, bpm)], resting heart rate or None). Columns come from `heartRateValueDescriptors`,
    else timestamp then heartrate/heartRate/value; rows may be lists or objects; values outside 20 to 250 are dropped."""
    b = _obj(body)
    ti, tk = _column(b.get("heartRateValueDescriptors"), "time", 0)
    vi, vk = _column(b.get("heartRateValueDescriptors"), "heart", 1)
    pts = []
    rows = b.get("heartRateValues")
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, list):
            t = gts(r[ti]) if ti < len(r) else None
            v = _in(r[vi], 20, 250) if vi < len(r) else None
        elif isinstance(r, dict):
            t = gts(r.get(tk or "timestamp"))
            raw = r.get(vk) if vk else next((r[k] for k in ("heartrate", "heartRate", "value") if r.get(k) is not None), None)
            v = _in(raw, 20, 250)
        else:
            continue
        if t is not None and v is not None:
            pts.append((t, v))
    return pts, _in(b.get("restingHeartRate"), 20, 150)


def hrv(body: object) -> tuple[list[tuple[float, float]], float | None]:
    """A day's HRV: ([(t, ms)], last night's average or None); readings outside 1 to 400 are dropped."""
    b = _obj(body)
    pts = []
    rows = b.get("hrvReadings")
    for r in rows if isinstance(rows, list) else []:
        r = _obj(r)
        t, v = gts(r.get("readingTimeGMT")), _in(r.get("hrvValue"), 1, 400)
        if t is not None and v is not None:
            pts.append((t, v))
    return pts, _in(_obj(b.get("hrvSummary")).get("lastNightAvg"), 1, 400)
