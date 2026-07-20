"""Tests for chim's Morrowind TES3 plugin engine (:mod:`chim.tes3`).

Two layers (mirrors ``test_savefile.py``):

* **Synthetic** -- a hand-built minimal TES3 plugin (a ``TES3`` header record with
  HEDR + one master, plus a ``GMST`` and a Rotfern-shaped ``RACE`` record).
  Portable/CI-safe; exercises byte-exact round-trip, the subrecord walk, and the
  ``Record.get``/``all`` accessors.
* **Real plugin** -- gated on ``.scratch/Morrowind.esm`` (a real Morrowind master,
  not committed). Asserts whole-file byte-exact round-trip -- the phase-0 gate.
"""

import os
import struct

import pytest

from chim.tes3 import container, ops
from chim.tes3.container import Plugin, Record, Subrecord, parse_plugin, serialize

REAL_ESM = os.path.join(os.path.dirname(__file__), os.pardir, ".scratch", "Morrowind.esm")


# --------------------------------------------------------------------------- #
# Synthetic builders
# --------------------------------------------------------------------------- #

def _sub(name: str, data: bytes) -> Subrecord:
    return Subrecord(name, data)


def _rec(typ: str, subs, flags: int = 0, header1: int = 0) -> Record:
    return Record(typ, header1, flags, list(subs))


def _hedr(num_records: int, author: bytes = b"chim", desc: bytes = b"synthetic") -> bytes:
    """A fixed 300-byte HEDR: version f32, fileType u32, author[32], desc[256], numRecords u32."""
    blob = (struct.pack("<f", 1.3)
            + struct.pack("<I", 0)                 # fileType 0 = esp
            + author.ljust(32, b"\x00")
            + desc.ljust(256, b"\x00")
            + struct.pack("<I", num_records))
    assert len(blob) == container.HEDR_SIZE
    return blob


def _build_plugin() -> bytes:
    header = _rec("TES3", [
        _sub("HEDR", _hedr(2)),
        _sub("MAST", b"Morrowind.esm\x00"),
        _sub("DATA", struct.pack("<Q", 79837557)),
    ])
    gmst = _rec("GMST", [
        _sub("NAME", b"fCombatDistanceWarningThreshold\x00"),
        _sub("FLTV", struct.pack("<f", 128.0)),
    ])
    # Rotfern-shaped RACE: NAME -> FNAM -> RADT(140) -> NPCS(32) -> DESC, in order.
    race = _rec("RACE", [
        _sub("NAME", b"Rotfern\x00"),
        _sub("FNAM", b"Rotfern\x00"),
        _sub("RADT", bytes(140)),
        _sub("NPCS", b"ancestor guardian".ljust(32, b"\x00")),
        _sub("DESC", b"A rotfern-blooded Dunmer child.\x00"),
    ])
    return header.to_bytes() + gmst.to_bytes() + race.to_bytes()


# --------------------------------------------------------------------------- #
# Synthetic tests
# --------------------------------------------------------------------------- #

def test_record_roundtrips_byte_exact():
    raw = _build_plugin()
    plug = parse_plugin(raw)
    assert plug.to_bytes() == raw
    assert plug.roundtrips()
    assert serialize(plug) == raw


def test_structure():
    plug = parse_plugin(_build_plugin())
    assert plug.header.type == "TES3"
    assert [r.type for r in plug.records] == ["GMST", "RACE"]
    assert list(r.type for r in plug.all_records()) == ["TES3", "GMST", "RACE"]


def test_subrecord_walk_and_datasize():
    plug = parse_plugin(_build_plugin())
    race = plug.records[1]
    assert [s.name for s in race.subrecords] == ["NAME", "FNAM", "RADT", "NPCS", "DESC"]
    # declared dataSize on the wire == recomputed from the parsed subrecords
    assert race.data_size() == sum(container.SUBREC_HEADER_SIZE + len(s.data)
                                   for s in race.subrecords)
    assert race.get("RADT").size() == 140
    assert race.get("NAME").data == b"Rotfern\x00"


def test_record_get_and_all():
    header = parse_plugin(_build_plugin()).header
    assert header.get("HEDR") is not None
    assert header.get("MISSING") is None
    assert len(header.all("MAST")) == 1
    # numRecords in HEDR is the count of CONTENT records (excludes the TES3 header)
    hedr = header.get("HEDR").data
    assert struct.unpack_from("<I", hedr, container.HEDR_NUMRECORDS_OFFSET)[0] == 2


def test_latin1_tags_preserve_underscore_types():
    # e.g. NPC_ / AI_W carry underscores; the tag path must round-trip any bytes.
    rec = _rec("NPC_", [_sub("AI_W", b"\x00" * 14)])
    raw = (_rec("TES3", [_sub("HEDR", _hedr(1))]).to_bytes() + rec.to_bytes())
    plug = parse_plugin(raw)
    assert plug.records[0].type == "NPC_"
    assert plug.records[0].subrecords[0].name == "AI_W"
    assert plug.to_bytes() == raw


def test_rejects_non_tes3_first_record():
    bad = _rec("GMST", [_sub("NAME", b"x\x00")]).to_bytes()
    with pytest.raises(container.ParseError):
        parse_plugin(bad)


# --------------------------------------------------------------------------- #
# Real plugin (ground truth) -- skipped when the scratch file is absent
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not os.path.exists(REAL_ESM), reason="no real Morrowind.esm in .scratch/")
def test_real_esm_roundtrip():
    raw = open(REAL_ESM, "rb").read()
    plug = parse_plugin(raw)
    assert plug.header.type == "TES3"
    assert plug.roundtrips()               # whole file byte-exact (the phase-0 gate)
    assert serialize(plug) == raw


# --------------------------------------------------------------------------- #
# Ops: addressing, authoring, subrecord CRUD, masters
# --------------------------------------------------------------------------- #

def test_find_record_by_id_case_insensitive():
    plug = parse_plugin(_build_plugin())
    assert ops.find_record(plug, "RACE", "rotfern") is not None       # ci
    assert ops.find_record(plug, "RACE", "Rotfern").type == "RACE"
    assert ops.find_record(plug, "RACE", "nope") is None
    assert ops.record_id(ops.find_record(plug, "GMST", "fCombatDistanceWarningThreshold")) \
        == "fCombatDistanceWarningThreshold"


def test_find_by_id_and_regex():
    plug = parse_plugin(_build_plugin())
    assert [r.type for r in ops.find_by_id(plug, "Rotfern")] == ["RACE"]
    assert {r.type for r in ops.find_by_id_regex(plug, r"Rotfern|fCombat")} == {"RACE", "GMST"}


def test_build_and_add_record_bumps_numrecords():
    plug = parse_plugin(_build_plugin())
    assert ops.num_records_stored(plug) == 2
    rec = ops.build_record("BODY", [("NAME", b"b_rotfern_chest\x00"),
                                     ("MODL", b"rotfern\\chest.nif\x00")])
    ops.add_record(plug, rec)
    assert len(plug.records) == 3
    assert ops.num_records_stored(plug) == 3
    out = ops.serialize(plug)
    assert ops.walk_clean(out)
    assert ops.find_record(parse_plugin(out), "BODY", "b_rotfern_chest") is not None


def test_delete_record_and_delete_records():
    plug = parse_plugin(_build_plugin())
    ops.delete_record(plug, ops.find_record(plug, "GMST", "fCombatDistanceWarningThreshold"))
    assert ops.num_records_stored(plug) == 1
    assert ops.walk_clean(ops.serialize(plug))
    plug2 = parse_plugin(_build_plugin())
    assert ops.delete_records(plug2, "RACE", ["Rotfern"]) == ["Rotfern"]
    assert ops.num_records_stored(plug2) == 1


def test_subrecord_crud_reparses_clean():
    plug = parse_plugin(_build_plugin())
    race = ops.find_record(plug, "RACE", "Rotfern")
    ops.set_subrecord(race, "DESC", b"Reworded.\x00")
    ops.insert_subrecord(race, "NPCS", b"fire shield".ljust(32, b"\x00"), after="NPCS")
    ops.delete_subrecord(race, "FNAM")
    out = ops.serialize(plug)
    assert ops.walk_clean(out)
    race2 = ops.find_record(parse_plugin(out), "RACE", "Rotfern")
    assert race2.get("FNAM") is None
    assert len(race2.all("NPCS")) == 2
    assert race2.get("DESC").data == b"Reworded.\x00"


def test_patch_subrecord_preserves_fixed_width_padding():
    plug = parse_plugin(_build_plugin())
    hedr = plug.header.get("HEDR")
    before = hedr.data
    # overwrite author[32] (offset 8) without disturbing surrounding padding/desc
    ops.patch_subrecord(plug.header, "HEDR", 8, b"eli".ljust(32, b"\x00"))
    after = plug.header.get("HEDR").data
    assert len(after) == len(before)                      # length unchanged
    assert after[8:40] == b"eli".ljust(32, b"\x00")
    assert after[40:] == before[40:]                      # desc[256] + numRecords intact
    with pytest.raises(ops.OpsError):
        ops.patch_subrecord(plug.header, "HEDR", 296, b"\x00" * 8)   # would overrun


def test_numrecords_only_changes_on_count_change():
    plug = parse_plugin(_build_plugin())
    n = ops.num_records_stored(plug)
    ops.set_subrecord(ops.find_record(plug, "RACE", "Rotfern"), "DESC", b"x\x00")
    assert ops.num_records_stored(plug) == n              # a subrecord edit does NOT touch it
    ops.add_record(plug, ops.build_record("STAT", [("NAME", b"rock\x00")]))
    assert ops.num_records_stored(plug) == n + 1          # a record add DOES


def test_mark_deleted_sets_flag_and_dele():
    plug = parse_plugin(_build_plugin())
    n = ops.num_records_stored(plug)
    race = ops.find_record(plug, "RACE", "Rotfern")
    ops.mark_deleted(plug, race)
    assert race.is_deleted() and race.get("DELE") is not None
    assert ops.num_records_stored(plug) == n              # soft-delete keeps the record
    assert ops.walk_clean(ops.serialize(plug))


def test_masters_add_and_rename():
    plug = parse_plugin(_build_plugin())
    assert ops.masters(plug) == [("Morrowind.esm", 79837557)]
    ops.add_master(plug, "Tribunal.esm", 4565686)
    ops.add_master(plug, "morrowind.esm")                 # idempotent (case-insensitive)
    assert [m for m, _ in ops.masters(plug)] == ["Morrowind.esm", "Tribunal.esm"]
    assert ops.num_records_stored(plug) == 2              # masters aren't records
    ops.rename_master(plug, "Tribunal.esm", "Bloodmoon.esm")
    assert [m for m, _ in ops.masters(plug)] == ["Morrowind.esm", "Bloodmoon.esm"]
    assert ops.walk_clean(ops.serialize(plug))


# --------------------------------------------------------------------------- #
# Analysis + safety integration
# --------------------------------------------------------------------------- #

def test_analysis_tes3_info():
    from chim.tes3 import analysis
    info = analysis.tes3_info(_build_plugin())
    assert info["magic"] == "TES3"
    assert info["file_type"] == "esp"
    assert info["record_count"] == 2
    assert info["num_records_stored"] == 2
    assert info["walk_clean"] is True
    assert info["record_types"] == {"GMST": 1, "RACE": 1}
    assert info["masters"] == [{"name": "Morrowind.esm", "size": 79837557}]


def test_transaction_with_injected_tes3_verify_and_lock(tmp_path):
    """The refactored safety.transaction drives a TES3 edit end to end with the
    tes3 verify predicate + OpenMW lock list (defaults still serve esp/save)."""
    from chim.esp import safety
    p = tmp_path / "Mod.esp"
    p.write_bytes(_build_plugin())
    host = safety.LocalHost()

    # a good edit commits and re-parses clean
    with safety.transaction(host, str(p), verify_fn=ops.walk_clean,
                            lock_processes=safety.OPENMW_LOCK_PROCESSES) as txn:
        plug = ops.load(txn.data)
        ops.set_subrecord(ops.find_record(plug, "RACE", "Rotfern"), "DESC", b"reworked\x00")
        txn.data = ops.serialize(plug)
    assert ops.find_record(ops.load(p.read_bytes()), "RACE", "Rotfern").get("DESC").data \
        == b"reworked\x00"

    # a garbage write fails the tes3 verify and rolls back
    with pytest.raises(safety.TransactionError):
        with safety.transaction(host, str(p), verify_fn=ops.walk_clean,
                                lock_processes=safety.OPENMW_LOCK_PROCESSES) as txn:
            txn.data = b"NOT-A-TES3-FILE"
    assert ops.walk_clean(p.read_bytes())          # restored from backup

    # a faked openmw-cs lock refuses to touch anything
    locked = safety.LocalHost(fake_processes=["openmw-cs"])
    with pytest.raises(safety.LockError):
        with safety.transaction(locked, str(p), verify_fn=ops.walk_clean,
                                lock_processes=safety.OPENMW_LOCK_PROCESSES):
            pass
