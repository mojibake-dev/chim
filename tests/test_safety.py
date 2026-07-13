"""Tests for chim.esp.safety using LocalHost on a temp copy of the fixture.

All of these exercise the real code paths (lock-check -> backup -> edit -> write
-> walk-clean verify / rollback) against the local filesystem, so they run
without a Windows host or paramiko.
"""

from __future__ import annotations

import glob
import os
import shutil

import pytest

from chim import parse_plugin, serialize, walk_clean
from chim.esp import fields
from chim.esp.records import iterate, Record
from chim.esp.safety import (
    LocalHost,
    LockError,
    TransactionError,
    LOCK_PROCESSES,
    backup,
    lock_check,
    transaction,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample.esp")


@pytest.fixture
def plugin_path(tmp_path) -> str:
    """A writable temp copy of the fixture plugin."""
    dst = tmp_path / "MyMod.esp"
    shutil.copyfile(FIXTURE, dst)
    return str(dst)


@pytest.fixture
def host() -> LocalHost:
    return LocalHost()


# --------------------------------------------------------------------------- #
# Host basics
# --------------------------------------------------------------------------- #

def test_localhost_read_write_roundtrip(host, tmp_path):
    p = str(tmp_path / "blob.bin")
    payload = bytes(range(256)) * 4
    host.write_file(p, payload)
    assert host.read_file(p) == payload


def test_localhost_read_matches_fixture(host, plugin_path):
    with open(FIXTURE, "rb") as fh:
        assert host.read_file(plugin_path) == fh.read()


def test_localhost_exists(host, plugin_path, tmp_path):
    assert host.exists(plugin_path) is True
    assert host.exists(str(tmp_path / "nope.esp")) is False


# --------------------------------------------------------------------------- #
# lock_check
# --------------------------------------------------------------------------- #

def test_lock_check_clear_by_default(host):
    assert lock_check(host) == []


@pytest.mark.parametrize("proc", LOCK_PROCESSES)
def test_lock_check_detects_each_process(proc):
    host = LocalHost(fake_processes=[proc])
    assert lock_check(host) == [proc]


def test_lock_check_ignores_unrelated_process():
    host = LocalHost(fake_processes=["notepad", "explorer"])
    assert lock_check(host) == []


def test_lock_check_reports_multiple():
    host = LocalHost(fake_processes=["CreationKit", "SkyrimSE"])
    assert set(lock_check(host)) == {"CreationKit", "SkyrimSE"}


# --------------------------------------------------------------------------- #
# backup
# --------------------------------------------------------------------------- #

def test_backup_creates_timestamped_copy(host, plugin_path):
    bak = backup(host, plugin_path, timestamp="20260707-000000")
    assert bak.endswith(".20260707-000000.bak")
    assert os.path.exists(bak)
    with open(plugin_path, "rb") as a, open(bak, "rb") as b:
        assert a.read() == b.read()


def test_backup_missing_file_raises(host, tmp_path):
    with pytest.raises(FileNotFoundError):
        backup(host, str(tmp_path / "ghost.esp"))


def test_backup_beside_original(host, plugin_path):
    bak = backup(host, plugin_path, timestamp="20260707-010203")
    assert os.path.dirname(bak) == os.path.dirname(plugin_path)


# --------------------------------------------------------------------------- #
# transaction: happy path
# --------------------------------------------------------------------------- #

def _edit_stat_edid(data: bytes, new_id: str) -> bytes:
    plugin = parse_plugin(data)
    for node in iterate(plugin):
        if isinstance(node, Record) and node.sig == "STAT":
            fl = fields.parse_fields(node.data)
            fields.find_field(fl, b"EDID").payload = fields.encode_edid(new_id)
            node.data = fields.pack_fields(fl)
            break
    return serialize(plugin)


def test_transaction_commits_valid_edit(host, plugin_path):
    with transaction(host, plugin_path) as txn:
        assert txn.data == host.read_file(plugin_path)
        txn.data = _edit_stat_edid(txn.data, "EditedViaTransaction")

    on_disk = host.read_file(plugin_path)
    assert walk_clean(on_disk)
    plugin = parse_plugin(on_disk)
    for node in iterate(plugin):
        if isinstance(node, Record) and node.sig == "STAT":
            fl = fields.parse_fields(node.data)
            assert (
                fields.decode_edid(fields.find_field(fl, b"EDID").payload)
                == "EditedViaTransaction"
            )
            break


def test_transaction_no_edit_leaves_file_intact(host, plugin_path):
    before = host.read_file(plugin_path)
    with transaction(host, plugin_path):
        pass  # write back unchanged bytes
    assert host.read_file(plugin_path) == before


def test_transaction_makes_backup(host, plugin_path):
    original = host.read_file(plugin_path)
    with transaction(host, plugin_path, timestamp="20260707-120000") as txn:
        txn.data = _edit_stat_edid(txn.data, "Whatever")
        bak_path = txn.backup_path
    # Backup holds the *pre-edit* bytes.
    assert host.read_file(bak_path) == original


# --------------------------------------------------------------------------- #
# transaction: lock refusal
# --------------------------------------------------------------------------- #

def test_transaction_refuses_when_locked(plugin_path):
    host = LocalHost(fake_processes=["CreationKit"])
    original = host.read_file(plugin_path)
    with pytest.raises(LockError) as exc:
        with transaction(host, plugin_path) as txn:
            txn.data = b"should never be written"
    assert "CreationKit" in str(exc.value)
    # File untouched and no backup made.
    assert host.read_file(plugin_path) == original
    assert glob.glob(plugin_path + ".*.bak") == []


# --------------------------------------------------------------------------- #
# transaction: dirty result -> rollback
# --------------------------------------------------------------------------- #

def test_transaction_rolls_back_dirty_write(host, plugin_path):
    original = host.read_file(plugin_path)
    with pytest.raises(TransactionError) as exc:
        with transaction(host, plugin_path, timestamp="20260707-130000") as txn:
            # Not a walk-clean plugin: doesn't start with TES4 / trailing junk.
            txn.data = b"GARBAGE not an esp at all"
    # Original restored, transaction reports success of the rollback.
    assert exc.value.restored is True
    assert host.read_file(plugin_path) == original
    # Backup is preserved for manual recovery.
    assert os.path.exists(exc.value.backup_path)
    assert host.read_file(exc.value.backup_path) == original


def test_transaction_rolls_back_trailing_garbage(host, plugin_path):
    original = host.read_file(plugin_path)
    with pytest.raises(TransactionError):
        with transaction(host, plugin_path) as txn:
            txn.data = txn.data + b"\x00\x01\x02"  # valid prefix, dirty tail
    assert host.read_file(plugin_path) == original


def test_transaction_body_exception_skips_write(host, plugin_path):
    original = host.read_file(plugin_path)

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with transaction(host, plugin_path, timestamp="20260707-140000") as txn:
            txn.data = b"partially edited garbage"
            raise Boom("edit aborted midway")
    # Body raised before commit: file unchanged, backup still exists.
    assert host.read_file(plugin_path) == original


def test_transaction_verify_false_skips_walk_clean(host, plugin_path):
    # With verify disabled, even a dirty write is committed (caller's problem).
    with transaction(host, plugin_path, verify=False) as txn:
        txn.data = b"deliberately broken"
    assert host.read_file(plugin_path) == b"deliberately broken"
