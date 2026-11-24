"""A small helper that encrypts a stored secret, for the cloud accounts that hold one.

`Sealer.seal(value, bound_to)` turns a dict into text that can sit in the key/value table; `Sealer.open()` turns it
back. AES-256-GCM with a fresh 12-byte nonce per value; `bound_to` (the row's name, e.g. `garmin:<person>`) is the
associated data, so a sealed value copied into another row does not open there.

The 32-byte key lives in `<data folder>/cloud-secret.key`, made on first use with mode 0600 (the `web_push.py`
pattern) and never overwritten. It is read once and kept for the life of the process, so a key file replaced while
the server runs takes effect only after a restart. With no folder (an in-memory store) the key is held in memory only.

This protects a copy of the database, an export and any read of the key/value table. Someone with the whole data
folder has both halves. Nothing here logs or raises with the value in it.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_FILE = "cloud-secret.key"
KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
PREFIX = "v1."
_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")


class SealError(Exception):
    """A value could not be sealed: the key file is unusable or cannot be made. Never carries the value."""


class Sealer:
    def __init__(self, folder: Path | None) -> None:
        self._folder = Path(folder) if folder is not None else None
        self._key: bytes | None = None
        self._lock = threading.Lock()

    def _load_key(self) -> bytes:
        """The key, read or made on first use and cached; raises SealError if it cannot be had."""
        if self._key is not None:
            return self._key
        with self._lock:
            if self._key is not None:
                return self._key
            if self._folder is None:
                self._key = os.urandom(KEY_BYTES)
                return self._key
            f = self._folder / KEY_FILE
            try:
                key = self._read(f)
            except FileNotFoundError:
                key = self._make(f)
            self._key = key
            return key

    @staticmethod
    def _read(f: Path) -> bytes:
        try:
            key = f.read_bytes()
        except FileNotFoundError:
            raise
        except OSError:
            raise SealError("the secret key file cannot be read") from None
        if len(key) != KEY_BYTES:
            raise SealError("the secret key file is not a valid key")
        return key

    def _make(self, f: Path) -> bytes:
        key = os.urandom(KEY_BYTES)
        try:
            fd = os.open(str(f), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:                 # made by someone else in the meantime: wait briefly if still being written
            for attempt in range(8):
                try:
                    existing = f.read_bytes()
                except OSError:
                    raise SealError("the secret key file cannot be read") from None
                if len(existing) == KEY_BYTES:
                    return existing
                if len(existing) > KEY_BYTES:
                    break
                if attempt < 7:
                    time.sleep(0.05)
            raise SealError("the secret key file is not a valid key")
        except OSError:
            raise SealError("the secret key file cannot be made") from None
        try:
            out = os.fdopen(fd, "wb")
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                f.unlink()
            except OSError:
                pass
            raise SealError("the secret key file cannot be written") from None
        try:
            with out:
                out.write(key)
        except OSError:
            try:
                f.unlink()
            except OSError:
                pass
            raise SealError("the secret key file cannot be written") from None
        return key

    def seal(self, value: dict, bound_to: str) -> str:
        """`value` encrypted for the row `bound_to`: "v1." + base64url(nonce + ciphertext). Raises SealError."""
        key = self._load_key()
        try:
            plain = json.dumps(value, separators=(",", ":")).encode()
        except (TypeError, ValueError):
            raise SealError("the value cannot be stored") from None
        nonce = os.urandom(NONCE_BYTES)
        sealed = AESGCM(key).encrypt(nonce, plain, bound_to.encode())
        return PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode().rstrip("=")

    def open(self, blob: object, bound_to: str) -> dict | None:
        """The dict sealed for `bound_to`, or None for anything that cannot be opened (wrong key, changed text,
        another row, a missing prefix, text that is not base64, an unusable key file)."""
        if not isinstance(blob, str) or not blob.startswith(PREFIX):
            return None
        body = blob[len(PREFIX):]
        if not _B64URL.match(body):
            return None
        try:
            raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        except ValueError:
            return None
        if len(raw) < NONCE_BYTES + TAG_BYTES:
            return None
        try:
            key = self._load_key()
        except SealError:
            return None
        try:
            plain = AESGCM(key).decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], bound_to.encode())
            value = json.loads(plain)
        except Exception:                       # InvalidTag, bad UTF-8 or JSON: all mean "cannot be opened"
            return None
        return value if isinstance(value, dict) else None
