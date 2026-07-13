"""Tests for :mod:`chim.esp.records` -- the ESP container data model.

The load-bearing guarantee of this module is *byte-exactness*: any plugin it
can parse must serialize back identically, and :func:`walk_clean` must be able
to tell an intact plugin apart from a corrupted one. These tests pin that down
against the synthetic ``sample.esp`` fixture and a handful of hand-built cases:

    * parse -> serialize is byte-identical on the fixture,
    * ``walk_clean`` is True on the fixture,
    * flipping a single byte makes ``walk_clean`` False,

plus the header/size re-derivation, iteration order, and ParseError paths that
back those guarantees up.
"""

from __future__ import annotations

import struct

import pytest

from chim.esp.records import (
    Group,
    ParseError,
    Plugin,
    Record,
    RECORD_HEADER_SIZE,
    GROUP_HEADER_SIZE,
    FLAG_COMPRESSED,
    FLAG_PERSISTENT,
    GT_TOP,
    GT_CELL_PERSISTENT_CHILDREN,
    iterate,
    parse_plugin,
    serialize,
    walk,
    walk_clean,
)


# --------------------------------------------------------------------------- #
# Core guarantees on the fixture
# --------------------------------------------------------------------------- #

def test_fixture_starts_with_tes4(raw: bytes) -> None:
    assert raw[:4] == b"TES4"
    assert len(raw) > RECORD_HEADER_SIZE


def test_parse_serialize_byte_identical(raw: bytes) -> None:
    """parse -> serialize reproduces the input byte-for-byte."""
    plugin = parse_plugin(raw)
    assert serialize(plugin) == raw
    # Plugin.to_bytes() and serialize() agree.
    assert plugin.to_bytes() == raw


def test_walk_clean_true(raw: bytes) -> None:
    assert walk_clean(raw) is True


def test_walk_clean_false_on_flipped_size_byte(raw: bytes) -> None:
    """Flipping a byte in a record's dataSize field breaks the container.

    ``walk_clean`` is a *structural* guarantee: it verifies the byte stream
    parses TES4->EOF with zero leftover and round-trips identically. Corrupting
    the TES4 dataSize (uint32 at offset 4) makes the declared payload run past
    EOF, so parsing fails and ``walk_clean`` reports False.
    """
    mutated = bytearray(raw)
    mutated[7] ^= 0xFF  # high byte of TES4.dataSize -> huge, past EOF
    assert walk_clean(bytes(mutated)) is False


def test_walk_clean_false_on_flipped_group_size(raw: bytes) -> None:
    """Corrupting a GRUP's groupSize breaks child consumption -> not clean."""
    # The first top GRUP begins right after the TES4 record.
    plugin = parse_plugin(raw)
    grp_off = plugin.tes4.byte_size  # offset of the first 'GRUP'
    assert raw[grp_off : grp_off + 4] == b"GRUP"
    mutated = bytearray(raw)
    mutated[grp_off + 4] ^= 0x01  # nudge groupSize so it no longer fits children
    assert walk_clean(bytes(mutated)) is False


def _size_field_offsets(buf: bytes) -> list[int]:
    """Byte offsets covered by every node's 4-byte size field (dataSize for
    records, groupSize for GRUPs)."""
    offsets: list[int] = []

    def rec(off: int, end: int) -> None:
        while off < end:
            sig = buf[off : off + 4]
            size = struct.unpack_from("<I", buf, off + 4)[0]
            offsets.extend(range(off + 4, off + 8))
            if sig == b"GRUP":
                rec(off + GROUP_HEADER_SIZE, off + size)
                off += size
            else:
                off += RECORD_HEADER_SIZE + size

    rec(0, len(buf))
    return offsets


def test_flipping_any_size_field_byte_breaks_walk_clean(raw: bytes) -> None:
    """Exhaustive over the structural size fields: every one is load-bearing.

    ``walk_clean`` does not (and by design cannot) detect corruption of bytes
    that are stored and re-emitted verbatim -- flags, formIDs, and payload
    values round-trip identically. What it *must* catch is any change to a
    dataSize / groupSize, because those drive how the container is walked.
    """
    for i in _size_field_offsets(raw):
        mutated = bytearray(raw)
        mutated[i] ^= 0xFF
        assert walk_clean(bytes(mutated)) is False, (
            f"size-field flip at byte {i} slipped through walk_clean"
        )


def test_walk_clean_absorbs_verbatim_byte_flips(raw: bytes) -> None:
    """Flipping a byte stored verbatim (a flag bit, a payload value) keeps the
    file structurally clean: it still parses and round-trips identically.

    This documents the boundary of the guarantee -- ``walk_clean`` is about
    container integrity, not semantic validity. Byte 8 is the low byte of the
    TES4 record's flags field, which is preserved verbatim through the header.
    """
    mutated = bytearray(raw)
    mutated[8] ^= 0x01  # a flag bit in TES4.flags, stored verbatim
    assert walk_clean(bytes(mutated)) is True


# --------------------------------------------------------------------------- #
# Record model
# --------------------------------------------------------------------------- #

def _make_record(sig: bytes, form_id: int, data: bytes, flags: int = 0) -> Record:
    header = sig + struct.pack(
        "<IIIIHH", len(data), flags, form_id, 0, 0x2C, 0
    )
    return Record(signature=sig, header=header, data=data)


def test_record_header_resync_on_serialize() -> None:
    """to_bytes() re-derives dataSize from the live payload, not the header."""
    rec = _make_record(b"STAT", 0x00000800, b"hello")
    # Corrupt the header's dataSize to something wrong; serialize must fix it.
    bad = bytearray(rec.header)
    struct.pack_into("<I", bad, 4, 9999)
    rec.header = bytes(bad)

    out = rec.to_bytes()
    assert struct.unpack_from("<I", out, 4)[0] == len(rec.data) == 5
    assert out[RECORD_HEADER_SIZE:] == b"hello"
    assert rec.byte_size == RECORD_HEADER_SIZE + 5


def test_record_flag_and_formid_setters_roundtrip() -> None:
    rec = _make_record(b"REFR", 0x00000802, b"xyz")
    assert rec.is_compressed is False

    rec.flags = rec.flags | FLAG_COMPRESSED | FLAG_PERSISTENT
    assert rec.is_compressed is True
    assert rec.is_persistent is True
    # Setter rewrote the header in place; the data is untouched.
    assert rec.data == b"xyz"

    rec.form_id = 0x01ABCDEF
    assert rec.form_id == 0x01ABCDEF
    assert rec.plugin_index == 0x01
    assert rec.object_index == 0x00ABCDEF
    # Round-trips through a parse (TES4 record followed by our edited REFR).
    plugin = parse_plugin(_tes4() + rec.to_bytes())
    reparsed = [
        n for n in iterate(plugin) if isinstance(n, Record) and n.sig == "REFR"
    ][0]
    assert reparsed.form_id == 0x01ABCDEF
    assert reparsed.is_compressed and reparsed.is_persistent


def test_record_sig_and_signature_agree() -> None:
    rec = _make_record(b"WEAP", 0x00000001, b"")
    assert rec.signature == b"WEAP"
    assert rec.sig == "WEAP"


# --------------------------------------------------------------------------- #
# Group model
# --------------------------------------------------------------------------- #

def _make_group(label: bytes, gtype: int, children) -> Group:
    body = b"".join(c.to_bytes() for c in children)
    header = (
        b"GRUP"
        + struct.pack("<I4siIH", GROUP_HEADER_SIZE + len(body), label, gtype, 0, 0)
        + struct.pack("<H", 0)
    )
    return Group(header=header, children=list(children))


def test_group_size_derived_from_children() -> None:
    child = _make_record(b"REFR", 0x00000802, b"payload", flags=FLAG_PERSISTENT)
    grp = _make_group(struct.pack("<I", 0x801), GT_CELL_PERSISTENT_CHILDREN, [child])
    assert grp.group_size == GROUP_HEADER_SIZE + child.byte_size
    assert grp.byte_size == grp.group_size
    assert grp.sig == "GRUP"
    assert grp.signature == b"GRUP"
    assert grp.group_type == GT_CELL_PERSISTENT_CHILDREN
    assert grp.label == struct.pack("<I", 0x801)
    assert grp.label_as_uint32 == 0x801


def test_group_size_reflows_when_child_grows() -> None:
    child = _make_record(b"REFR", 0x1, b"ab")
    grp = _make_group(struct.pack("<i", 0), GT_TOP, [child])
    before = grp.group_size
    child.data = child.data + b"much longer payload now"
    after = grp.group_size
    assert after == before + len(b"much longer payload now")
    # Serialized header carries the reflowed size.
    assert struct.unpack_from("<I", grp.to_bytes(), 4)[0] == after


def test_group_label_setter_validates_length() -> None:
    grp = _make_group(struct.pack("<i", 0), GT_TOP, [])
    grp.label = b"STAT"
    assert grp.label == b"STAT"
    with pytest.raises(ValueError):
        grp.label = b"TOOLONG"


def test_group_top_label_as_signature() -> None:
    grp = _make_group(b"STAT", GT_TOP, [])
    assert grp.label_as_signature == b"STAT"
    non_top = _make_group(struct.pack("<i", 0), GT_CELL_PERSISTENT_CHILDREN, [])
    assert non_top.label_as_signature is None


# --------------------------------------------------------------------------- #
# Iteration
# --------------------------------------------------------------------------- #

def test_iterate_is_preorder_groups_before_children(raw: bytes) -> None:
    plugin = parse_plugin(raw)
    nodes = list(iterate(plugin))
    # First node is the TES4 record.
    assert isinstance(nodes[0], Record) and nodes[0].sig == "TES4"
    # walk is an alias of iterate.
    assert [type(n) for n in walk(plugin)] == [type(n) for n in nodes]
    # Every group appears before each of its children in the flat order.
    order = {id(n): i for i, n in enumerate(nodes)}
    for n in nodes:
        if isinstance(n, Group):
            for c in n.children:
                assert order[id(n)] < order[id(c)]


def test_iterate_single_record_yields_itself() -> None:
    rec = _make_record(b"STAT", 0x1, b"")
    assert list(iterate(rec)) == [rec]


def test_iterate_count_matches_fixture_structure(raw: bytes) -> None:
    plugin = parse_plugin(raw)
    recs = [n for n in iterate(plugin) if isinstance(n, Record)]
    grps = [n for n in iterate(plugin) if isinstance(n, Group)]
    sigs = sorted(r.sig for r in recs)
    assert sigs == ["CELL", "REFR", "STAT", "TES4"]
    # STAT top group, CELL top group, interior block, subblock, cell-children,
    # persistent-children = 6 groups.
    assert len(grps) == 6


# --------------------------------------------------------------------------- #
# Plugin accessors
# --------------------------------------------------------------------------- #

def test_plugin_tes4_and_groups(raw: bytes) -> None:
    plugin = parse_plugin(raw)
    assert isinstance(plugin, Plugin)
    assert plugin.tes4 is not None and plugin.tes4.sig == "TES4"
    assert [g.label for g in plugin.groups] == [b"STAT", b"CELL"]
    # __iter__ walks the top level in order.
    assert list(plugin)[0] is plugin.tes4


def test_empty_plugin_has_no_tes4() -> None:
    empty = Plugin(top_level=[])
    assert empty.tes4 is None
    assert empty.groups == []
    assert empty.to_bytes() == b""


# --------------------------------------------------------------------------- #
# ParseError paths
# --------------------------------------------------------------------------- #

def test_parse_rejects_non_tes4_start() -> None:
    with pytest.raises(ParseError):
        parse_plugin(b"XXXX" + b"\x00" * 40)


def test_parse_rejects_empty() -> None:
    with pytest.raises(ParseError):
        parse_plugin(b"")


def test_parse_rejects_record_size_past_eof() -> None:
    # TES4 header claiming a huge dataSize with no payload.
    header = b"TES4" + struct.pack("<IIIIHH", 1000, 0, 0, 0, 0x2C, 0)
    with pytest.raises(ParseError):
        parse_plugin(header)


def test_parse_rejects_group_size_below_header() -> None:
    tes4 = _tes4()
    bad_grp = b"GRUP" + struct.pack("<I4siIH", 4, b"STAT", GT_TOP, 0, 0) + struct.pack("<H", 0)
    with pytest.raises(ParseError):
        parse_plugin(tes4 + bad_grp)


def test_parse_rejects_group_size_past_eof() -> None:
    tes4 = _tes4()
    bad_grp = (
        b"GRUP"
        + struct.pack("<I4siIH", 10_000, b"STAT", GT_TOP, 0, 0)
        + struct.pack("<H", 0)
    )
    with pytest.raises(ParseError):
        parse_plugin(tes4 + bad_grp)


def test_parse_rejects_truncated_header() -> None:
    with pytest.raises(ParseError):
        parse_plugin(b"TES4" + b"\x00" * 3)  # < 24 bytes


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _tes4() -> bytes:
    """A minimal well-formed TES4 record (empty payload)."""
    return b"TES4" + struct.pack("<IIIIHH", 0, 0, 0, 0, 0x2C, 0)
