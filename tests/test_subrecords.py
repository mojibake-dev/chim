"""Tests for :mod:`chim.esp.subrecords` -- record-level subrecord CRUD.

Every mutation must (a) do exactly what was asked at the field level and (b)
leave any containing plugin walk-clean. We cover the ordinary path on the real
sample.esp fixture, then the edges that make a general subrecord editor correct:
repeated-signature occurrence indexing, byte-range patch bounds, compression
(decompress + flag-clear), XXXX >0xFFFF overflow round-trips, insert positioning,
and the concrete grotto-shaped operations (FLST LNAM append, MGEF DATA flag-byte
patch, MODL replace).
"""

from __future__ import annotations

import os
import struct

import pytest

from chim.esp.records import (
    parse_plugin,
    serialize,
    walk_clean,
    iterate,
    Record,
    FLAG_COMPRESSED,
)
from chim.esp import fields, compression, subrecords
from chim.esp.fields import Field
from chim.esp.subrecords import SubrecordError

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample.esp")
FID_STAT = 0x00000800
FID_CELL = 0x00000801
FID_REFR = 0x00000802


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _make_record(sig: bytes, form_id: int, flds, flags: int = 0) -> Record:
    """Build a standalone Record from a field list (uncompressed)."""
    data = fields.pack_fields(flds)
    header = sig + struct.pack("<IIIIHH", len(data), flags, form_id, 0, 0x2C, 0)
    return Record(signature=sig, header=header, data=data)


def _make_compressed(sig: bytes, form_id: int, flds, flags: int = 0) -> Record:
    plain = fields.pack_fields(flds)
    blob = compression.compress_payload(plain)
    header = sig + struct.pack(
        "<IIIIHH", len(blob), flags | FLAG_COMPRESSED, form_id, 0, 0x2C, 0
    )
    return Record(signature=sig, header=header, data=blob)


def _find(plugin, form_id: int) -> Record:
    for node in iterate(plugin):
        if isinstance(node, Record) and node.form_id == form_id:
            return node
    raise AssertionError(f"no record 0x{form_id:08X}")


def _sigs(record) -> list:
    return [f.sig for f in subrecords.read_fields(record)]


@pytest.fixture()
def plugin():
    with open(FIXTURE, "rb") as fh:
        return parse_plugin(fh.read())


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #

def test_read_fields_matches_fixture(plugin):
    stat = _find(plugin, FID_STAT)
    assert _sigs(stat) == ["EDID", "OBND", "MODL"]


def test_get_subrecords_filter(plugin):
    stat = _find(plugin, FID_STAT)
    modl = subrecords.get_subrecords(stat, b"MODL")
    assert len(modl) == 1 and modl[0].sig == "MODL"
    assert subrecords.get_subrecords(stat, b"ZZZZ") == []


def test_read_write_roundtrip_is_noop_bytes(plugin):
    """Reading then writing the same fields must not perturb the record."""
    stat = _find(plugin, FID_STAT)
    before = stat.data
    subrecords.write_fields(stat, subrecords.read_fields(stat))
    assert stat.data == before


# --------------------------------------------------------------------------- #
# set (whole-payload replace), through the real plugin -> walk-clean
# --------------------------------------------------------------------------- #

def test_set_modl_replaces_and_walks_clean(plugin):
    stat = _find(plugin, FID_STAT)
    new = fields.encode_modl(r"Clutter\DisplayCases\DisplayCaseLGAngled01.nif")
    subrecords.set_subrecord(stat, b"MODL", new)
    out = serialize(plugin)
    assert walk_clean(out)
    reparsed = _find(parse_plugin(out), FID_STAT)
    assert fields.find_field(
        subrecords.read_fields(reparsed), b"MODL"
    ).payload == new


def test_set_can_grow_and_shrink_field(plugin):
    stat = _find(plugin, FID_STAT)
    subrecords.set_subrecord(stat, b"EDID", b"A_MUCH_LONGER_EDITOR_ID\x00")
    assert walk_clean(serialize(plugin))
    subrecords.set_subrecord(stat, b"EDID", b"x\x00")
    assert walk_clean(serialize(plugin))
    assert fields.decode_edid(
        subrecords.get_subrecords(stat, b"EDID")[0].payload
    ) == "x"


def test_set_missing_signature_raises(plugin):
    stat = _find(plugin, FID_STAT)
    with pytest.raises(SubrecordError):
        subrecords.set_subrecord(stat, b"VMAD", b"\x00\x00")


# --------------------------------------------------------------------------- #
# patch (surgical byte-range) -- the MGEF flag-byte case
# --------------------------------------------------------------------------- #

def test_patch_mgef_flag_byte():
    # An MGEF-shaped record: 152-byte DATA whose byte 0 is a flags word.
    data_payload = bytearray(152)
    data_payload[0:4] = struct.pack("<I", 0x00201A00)  # the "NoDuration" junk
    data_payload[12] = 0xFF  # some other byte we must NOT disturb
    rec = _make_record(b"MGEF", 0x07001234, [
        Field(b"EDID", b"TelSolTeleport\x00"),
        Field(b"DATA", bytes(data_payload)),
    ])
    subrecords.patch_subrecord(rec, b"DATA", 0, b"\x00\x00\x00\x00")
    out = fields.find_field(subrecords.read_fields(rec), b"DATA").payload
    assert struct.unpack_from("<I", out, 0)[0] == 0  # flags cleared
    assert out[12] == 0xFF                            # neighbour untouched
    assert len(out) == 152                            # length unchanged


def test_patch_single_byte_magic_skill():
    rec = _make_record(b"MGEF", 0x07001235, [
        Field(b"DATA", bytes(160)),
    ])
    subrecords.patch_subrecord(rec, b"DATA", 12, bytes([18]))  # Alteration
    out = fields.find_field(subrecords.read_fields(rec), b"DATA").payload
    assert out[12] == 18


def test_patch_out_of_bounds_raises():
    rec = _make_record(b"STAT", 0x07001236, [Field(b"OBND", bytes(12))])
    with pytest.raises(SubrecordError):
        subrecords.patch_subrecord(rec, b"OBND", 10, b"\x00\x00\x00\x00")
    with pytest.raises(SubrecordError):
        subrecords.patch_subrecord(rec, b"OBND", -1, b"\x00")


# --------------------------------------------------------------------------- #
# repeated-signature occurrence indexing
# --------------------------------------------------------------------------- #

def _xlkr(kw: int, ref: int) -> Field:
    return Field(b"XLKR", struct.pack("<II", kw, ref))


def test_repeated_signature_set_targets_one():
    rec = _make_record(b"REFR", 0x07002000, [
        Field(b"NAME", struct.pack("<I", 0x34)),
        _xlkr(0x6D95E, 0x111),
        _xlkr(0x6D95F, 0x222),
        _xlkr(0x00000, 0x333),
    ])
    subrecords.set_subrecord(rec, b"XLKR", struct.pack("<II", 0xAAAA, 0xBBBB), index=1)
    xs = subrecords.get_subrecords(rec, b"XLKR")
    assert len(xs) == 3
    assert struct.unpack("<II", xs[0].payload) == (0x6D95E, 0x111)
    assert struct.unpack("<II", xs[1].payload) == (0xAAAA, 0xBBBB)
    assert struct.unpack("<II", xs[2].payload) == (0x00000, 0x333)


def test_repeated_signature_delete_reindexes():
    rec = _make_record(b"REFR", 0x07002001, [
        _xlkr(1, 1), _xlkr(2, 2), _xlkr(3, 3),
    ])
    subrecords.delete_subrecord(rec, b"XLKR", index=0)
    xs = subrecords.get_subrecords(rec, b"XLKR")
    assert [struct.unpack("<II", f.payload) for f in xs] == [(2, 2), (3, 3)]


def test_bad_occurrence_index_raises():
    rec = _make_record(b"REFR", 0x07002002, [_xlkr(1, 1)])
    with pytest.raises(SubrecordError):
        subrecords.set_subrecord(rec, b"XLKR", b"\x00" * 8, index=1)


# --------------------------------------------------------------------------- #
# insert positioning -- incl. the FLST LNAM-append (vampire-exemption) case
# --------------------------------------------------------------------------- #

def test_insert_append_default():
    rec = _make_record(b"STAT", 0x07003000, [Field(b"EDID", b"x\x00")])
    pos = subrecords.insert_subrecord(rec, b"MODL", b"m\x00")
    assert pos == 1 and _sigs(rec) == ["EDID", "MODL"]


def test_insert_after_before_at_index():
    def fresh():
        return _make_record(b"STAT", 0x07003001, [
            Field(b"EDID", b"e\x00"), Field(b"OBND", bytes(12)),
            Field(b"MODL", b"m\x00"),
        ])
    r = fresh(); subrecords.insert_subrecord(r, b"FULL", b"f\x00", after=b"EDID")
    assert _sigs(r) == ["EDID", "FULL", "OBND", "MODL"]
    r = fresh(); subrecords.insert_subrecord(r, b"FULL", b"f\x00", before=b"MODL")
    assert _sigs(r) == ["EDID", "OBND", "FULL", "MODL"]
    r = fresh(); subrecords.insert_subrecord(r, b"FULL", b"f\x00", at_index=0)
    assert _sigs(r) == ["FULL", "EDID", "OBND", "MODL"]


def test_insert_after_last_occurrence():
    rec = _make_record(b"FLST", 0x07003002, [
        Field(b"EDID", b"SunDamageExceptionWorldSpaces\x00"),
        Field(b"LNAM", struct.pack("<I", 0x0001A26F)),
        Field(b"LNAM", struct.pack("<I", 0x0002EDF1)),
    ])
    grotto = struct.pack("<I", 0x07028011)
    pos = subrecords.insert_subrecord(rec, b"LNAM", grotto, after=b"LNAM")
    lnams = subrecords.get_subrecords(rec, b"LNAM")
    assert len(lnams) == 3
    assert lnams[-1].payload == grotto            # appended after the last LNAM
    assert pos == 3


def test_insert_bad_selectors_and_targets():
    rec = _make_record(b"STAT", 0x07003003, [Field(b"EDID", b"e\x00")])
    with pytest.raises(SubrecordError):
        subrecords.insert_subrecord(rec, b"MODL", b"m\x00", after=b"EDID", at_index=0)
    with pytest.raises(SubrecordError):
        subrecords.insert_subrecord(rec, b"MODL", b"m\x00", after=b"ZZZZ")
    with pytest.raises(SubrecordError):
        subrecords.insert_subrecord(rec, b"MODL", b"m\x00", at_index=99)


# --------------------------------------------------------------------------- #
# compression: editing a compressed record decompresses + clears the flag
# --------------------------------------------------------------------------- #

def test_compressed_edit_decompresses_and_clears_flag():
    rec = _make_compressed(b"CELL", 0x07004000, [
        Field(b"EDID", b"TelSolGrottoCell\x00"),
        Field(b"DATA", bytes([0x00, 0x00])),
    ])
    assert rec.is_compressed
    # append an XCLR region membership (grotto region formID)
    subrecords.insert_subrecord(
        rec, b"XCLR", struct.pack("<I", 0x070468A9), after=b"DATA"
    )
    assert not rec.is_compressed                       # flag cleared
    got = subrecords.get_subrecords(rec, b"XCLR")
    assert len(got) == 1 and struct.unpack("<I", got[0].payload)[0] == 0x070468A9
    # the (now uncompressed) record is a valid field stream
    assert _sigs(rec) == ["EDID", "DATA", "XCLR"]


# --------------------------------------------------------------------------- #
# XXXX overflow: fields >0xFFFF round-trip through set/patch
# --------------------------------------------------------------------------- #

def test_dangling_xxxx_is_rejected_not_truncated():
    """A trailing XXXX override with no following field must RAISE, not silently
    drop the override (which would truncate the record undetected)."""
    blob = (b"EDID" + struct.pack("<H", 2) + b"ab"
            + b"XXXX" + struct.pack("<H", 4) + struct.pack("<I", 500))
    with pytest.raises(ValueError):
        fields.parse_fields(blob)


def test_xxxx_overflow_set_and_patch_roundtrip():
    big = bytes(70000)                                  # > 0xFFFF -> needs XXXX
    rec = _make_record(b"QUST", 0x07005000, [
        Field(b"EDID", b"TelSolQuest\x00"),
        Field(b"VMAD", big),
    ])
    # it parsed back as one 70000-byte field despite XXXX framing
    assert subrecords.get_subrecords(rec, b"VMAD")[0].size == 70000
    # grow it further and confirm it still round-trips
    subrecords.set_subrecord(rec, b"VMAD", bytes(80000))
    assert subrecords.get_subrecords(rec, b"VMAD")[0].size == 80000
    # a surgical patch inside the oversized field
    subrecords.patch_subrecord(rec, b"VMAD", 40000, b"\xDE\xAD\xBE\xEF")
    out = subrecords.get_subrecords(rec, b"VMAD")[0].payload
    assert out[40000:40004] == b"\xDE\xAD\xBE\xEF" and len(out) == 80000
    # and the record's data is itself a clean, re-parseable field stream
    assert fields.parse_fields(rec.data)[1].size == 80000


# --------------------------------------------------------------------------- #
# integration: a sequence of ops keeps the fixture plugin walk-clean
# --------------------------------------------------------------------------- #

def test_sequence_keeps_plugin_walk_clean(plugin):
    stat = _find(plugin, FID_STAT)
    refr = _find(plugin, FID_REFR)
    subrecords.set_subrecord(stat, b"MODL", b"NewMesh.nif\x00")
    assert walk_clean(serialize(plugin))
    subrecords.insert_subrecord(stat, b"FULL", b"Display Case\x00", after=b"EDID")
    assert walk_clean(serialize(plugin))
    subrecords.patch_subrecord(refr, b"DATA", 0, struct.pack("<f", 3550.0))
    assert walk_clean(serialize(plugin))
    subrecords.delete_subrecord(stat, b"FULL")
    assert walk_clean(serialize(plugin))
    # final state is what we expect
    final = _find(parse_plugin(serialize(plugin)), FID_STAT)
    assert _sigs(final) == ["EDID", "OBND", "MODL"]
