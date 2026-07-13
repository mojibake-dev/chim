"""Round-trip and walk-clean guarantees for the ESP container model."""

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
    Group,
    Plugin,
    FLAG_PERSISTENT,
    GT_TOP,
    GT_INTERIOR_BLOCK,
    GT_INTERIOR_SUBBLOCK,
    GT_CELL_CHILDREN,
    GT_CELL_PERSISTENT_CHILDREN,
)
from chim.esp import fields, compression

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample.esp")


@pytest.fixture(scope="module")
def raw() -> bytes:
    with open(FIXTURE, "rb") as fh:
        return fh.read()


def test_fixture_exists_and_starts_with_tes4(raw: bytes) -> None:
    assert len(raw) > 24
    assert raw[:4] == b"TES4"


def test_roundtrip_byte_identical(raw: bytes) -> None:
    plugin = parse_plugin(raw)
    assert serialize(plugin) == raw


def test_walk_clean_true(raw: bytes) -> None:
    assert walk_clean(raw) is True


def test_walk_clean_false_on_trailing_garbage(raw: bytes) -> None:
    assert walk_clean(raw + b"\x00\x01\x02") is False


def test_structure(raw: bytes) -> None:
    plugin = parse_plugin(raw)
    assert isinstance(plugin.tes4, Record)
    assert plugin.tes4.sig == "TES4"

    groups = plugin.groups
    assert [g.label for g in groups] == [b"STAT", b"CELL"]

    stat_grp = groups[0]
    assert stat_grp.group_type == GT_TOP
    assert len(stat_grp.children) == 1
    assert stat_grp.children[0].sig == "STAT"

    cell_grp = groups[1]
    block = cell_grp.children[0]
    assert isinstance(block, Group) and block.group_type == GT_INTERIOR_BLOCK
    subblock = block.children[0]
    assert subblock.group_type == GT_INTERIOR_SUBBLOCK
    cell, children = subblock.children
    assert cell.sig == "CELL"
    assert children.group_type == GT_CELL_CHILDREN
    persistent = children.children[0]
    assert persistent.group_type == GT_CELL_PERSISTENT_CHILDREN
    refr = persistent.children[0]
    assert refr.sig == "REFR"
    assert refr.is_persistent


def test_hedr_num_records_matches_tree(raw: bytes) -> None:
    plugin = parse_plugin(raw)
    hedr_field = fields.find_field(
        fields.parse_fields(plugin.tes4.data), b"HEDR"
    )
    hedr = fields.decode_hedr(hedr_field.payload)
    # numRecords excludes TES4 itself.
    node_count = sum(1 for _ in iterate(plugin)) - 1
    assert hedr.num_records == node_count


def test_group_size_consumes_children(raw: bytes) -> None:
    plugin = parse_plugin(raw)
    for node in iterate(plugin):
        if isinstance(node, Group):
            expected = 24 + sum(c.byte_size for c in node.children)
            assert node.group_size == expected


def test_form_id_decomposition(raw: bytes) -> None:
    plugin = parse_plugin(raw)
    refr = None
    for node in iterate(plugin):
        if isinstance(node, Record) and node.sig == "REFR":
            refr = node
    assert refr is not None
    assert refr.plugin_index == 0x00
    assert refr.object_index == refr.form_id & 0x00FFFFFF


def test_edit_reserializes_clean(raw: bytes) -> None:
    """Grow an EDID and confirm the whole file still walks clean."""
    plugin = parse_plugin(raw)
    for node in iterate(plugin):
        if isinstance(node, Record) and node.sig == "STAT":
            fl = fields.parse_fields(node.data)
            edid = fields.find_field(fl, b"EDID")
            edid.payload = fields.encode_edid("AMuchLongerEditorIDThanBefore")
            node.data = fields.pack_fields(fl)
            break
    out = serialize(plugin)
    assert walk_clean(out) is True
    # And the edited value survives a re-parse.
    plugin2 = parse_plugin(out)
    for node in iterate(plugin2):
        if isinstance(node, Record) and node.sig == "STAT":
            fl = fields.parse_fields(node.data)
            assert fields.decode_edid(
                fields.find_field(fl, b"EDID").payload
            ) == "AMuchLongerEditorIDThanBefore"


def test_xxxx_override_roundtrip() -> None:
    big = b"Z" * 70000
    original = fields.pack_fields(
        [
            fields.Field(b"EDID", b"x\x00"),
            fields.Field(b"HUGE", big),
            fields.Field(b"NAME", struct.pack("<I", 3)),
        ]
    )
    parsed = fields.parse_fields(original)
    assert parsed[1].payload == big
    assert parsed[1].used_xxxx is True
    assert fields.pack_fields(parsed) == original
    assert b"XXXX" in original


def test_compression_roundtrip() -> None:
    plain = b"EDID" + struct.pack("<H", 3) + b"hi\x00"
    header = b"STAT" + struct.pack("<IIIIHH", len(plain), 0, 0x800, 0, 0x2C, 0)
    rec = Record(signature=b"STAT", header=header, data=plain)
    compression.store_compressed(rec)
    assert rec.is_compressed
    assert compression.decompress_record(rec) == plain
    compression.store_uncompressed(rec)
    assert not rec.is_compressed
    assert rec.data == plain


def test_vmad_roundtrip() -> None:
    def wstr(s: str) -> bytes:
        b = s.encode()
        return struct.pack("<H", len(b)) + b

    blob = struct.pack("<hhH", 5, 2, 1)
    blob += wstr("MyScript") + struct.pack("<B", 0) + struct.pack("<H", 2)
    blob += wstr("LinkedRef") + struct.pack("<BB", 1, 1)
    blob += struct.pack("<HHI", 0, 0xFFFF, 0x00000800)
    blob += wstr("Count") + struct.pack("<BB", 3, 1) + struct.pack("<i", 42)

    assert fields.script_names(blob) == ["MyScript"]
    decoded = fields.decode_vmad(blob)
    assert fields.encode_vmad(decoded) == blob
