"""The helper that encrypts a stored cloud secret (`embertide.cloud.sealed`)."""

import os
import stat
import time

import pytest

from embertide.cloud.sealed import KEY_FILE, SealError, Sealer

VALUE = {"key": "k-synthetic-0123456789", "token": "t-synthetic"}


def test_round_trip(tmp_path):
    s = Sealer(tmp_path)
    blob = s.seal(VALUE, "garmin:p1")
    assert blob.startswith("v1.")
    assert "synthetic" not in blob
    assert s.open(blob, "garmin:p1") == VALUE
    assert s.seal(VALUE, "garmin:p1") != blob          # a fresh nonce each time
    assert Sealer(tmp_path).open(blob, "garmin:p1") == VALUE  # a restart reads the same key file


def test_in_memory_key():
    s = Sealer(None)
    assert s.open(s.seal(VALUE, "garmin:p1"), "garmin:p1") == VALUE
    assert Sealer(None).open(s.seal(VALUE, "garmin:p1"), "garmin:p1") is None


def test_another_row_does_not_open(tmp_path):
    s = Sealer(tmp_path)
    assert s.open(s.seal(VALUE, "garmin:p1"), "garmin:p2") is None


def test_a_changed_character_does_not_open(tmp_path):
    s = Sealer(tmp_path)
    blob = s.seal(VALUE, "garmin:p1")
    i = len(blob) // 2
    changed = blob[:i] + ("A" if blob[i] != "A" else "B") + blob[i + 1:]
    assert s.open(changed, "garmin:p1") is None


@pytest.mark.parametrize("blob", [None, 42, "", "v1.", "v2.abc", "abc", "v1.not base64!", "v1.QUJD", "v1.***"])
def test_junk_does_not_open(tmp_path, blob):
    assert Sealer(tmp_path).open(blob, "garmin:p1") is None


def test_a_second_key_file_does_not_open(tmp_path):
    blob = Sealer(tmp_path).seal(VALUE, "garmin:p1")
    other = tmp_path / "other"
    other.mkdir()
    assert Sealer(other).open(blob, "garmin:p1") is None


def test_key_file_is_32_bytes_and_private(tmp_path):
    Sealer(tmp_path).seal(VALUE, "garmin:p1")
    f = tmp_path / KEY_FILE
    assert len(f.read_bytes()) == 32
    if os.name == "posix":
        assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_short_key_file_refuses_and_is_left_alone(tmp_path):
    f = tmp_path / KEY_FILE
    f.write_bytes(b"too short")
    s = Sealer(tmp_path)
    with pytest.raises(SealError) as e:
        s.seal(VALUE, "garmin:p1")
    assert "synthetic" not in str(e.value)
    assert f.read_bytes() == b"too short"
    assert s.open("v1." + "A" * 60, "garmin:p1") is None


def test_unwritable_folder_refuses(tmp_path):
    missing = tmp_path / "missing"           # a folder that does not exist cannot be written either
    s = Sealer(missing)
    with pytest.raises(SealError):
        s.seal(VALUE, "garmin:p1")
    assert s.open("v1." + "A" * 60, "garmin:p1") is None
    assert not missing.exists()


def test_write_failure_removes_broken_file_and_next_seal_works(tmp_path, monkeypatch):
    class BadWriter:
        def __init__(self, fd):
            self._fd = fd

        def __enter__(self):
            return self

        def __exit__(self, *args):
            os.close(self._fd)

        def write(self, data):
            raise OSError("disk full")

    monkeypatch.setattr(os, "fdopen", lambda fd, mode: BadWriter(fd))
    s = Sealer(tmp_path)
    with pytest.raises(SealError):
        s.seal(VALUE, "garmin:p1")
    assert not (tmp_path / KEY_FILE).exists()
    monkeypatch.undo()
    blob = s.seal(VALUE, "garmin:p1")
    assert s.open(blob, "garmin:p1") == VALUE


def test_file_exists_waits_for_concurrent_writer(tmp_path, monkeypatch):
    f = tmp_path / KEY_FILE
    expected = os.urandom(32)
    sleeps = 0

    def fake_open(path, flags, mode=0o777):
        f.write_bytes(b"")
        raise FileExistsError

    def fake_sleep(secs):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            f.write_bytes(expected)

    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(time, "sleep", fake_sleep)
    blob = Sealer(tmp_path).seal(VALUE, "garmin:p1")
    assert sleeps == 2
    assert Sealer(tmp_path).open(blob, "garmin:p1") == VALUE
