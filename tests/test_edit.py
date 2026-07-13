"""Mutation tests for :mod:`chim.esp.edit`.

Every mutation must leave the plugin walk-clean (parses TES4->EOF with zero
leftover and round-trips byte-identically). These tests exercise the three
required paths -- a flag toggle, a clone, and a delete -- and assert walk-clean
after each, plus the guardrails those paths must uphold (HEDR bookkeeping,
float32 packing, ancestor-size re-flow, XESP stripping, link remap/report).
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
    Group,
    FLAG_INITIALLY_DISABLED,
    FLAG_PERSISTENT,
    GT_CELL_PERSISTENT_CHILDREN,
)
from chim.esp import fields
from chim.esp import edit

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample.esp")

# Known formIDs baked into the fixture (see fixtures/make_fixture.py).
FID_STAT = 0x00000800
FID_CELL = 0x00000801
FID_REFR = 0x00000802
NEXT_OBJECT_ID = 0x00000803


@pytest.fixture()
def plugin():
    with open(FIXTURE, "rb") as fh:
        return parse_plugin(fh.read())


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _hedr(plugin):
    fl = fields.parse_fields(plugin.tes4.data)
    return fields.decode_hedr(fields.find_field(fl, b"HEDR").payload)


def _node_count_excluding_tes4(plugin):
    return sum(1 for _ in iterate(plugin)) - 1


def _find(plugin, form_id):
    for node in iterate(plugin):
        if isinstance(node, Record) and node.form_id == form_id:
            return node
    return None


# --------------------------------------------------------------------------- #
# set_flag / clear_flag
# --------------------------------------------------------------------------- #

def test_set_flag_walk_clean(plugin):
    before = _hedr(plugin)
    edit.set_flag(plugin, FID_REFR, FLAG_INITIALLY_DISABLED)

    out = serialize(plugin)
    assert walk_clean(out) is True

    # Flag is set, and it survives a re-parse.
    reparsed = parse_plugin(out)
    refr = _find(reparsed, FID_REFR)
    assert refr.flags & FLAG_INITIALLY_DISABLED

    # Pure header edit: HEDR untouched.
    after = _hedr(plugin)
    assert after.num_records == before.num_records
    assert after.next_object_id == before.next_object_id


def test_clear_flag_force_enable_walk_clean(plugin):
    # First disable, then clear 0x800 to force-enable -- the canonical use.
    edit.set_flag(plugin, FID_REFR, FLAG_INITIALLY_DISABLED)
    assert _find(plugin, FID_REFR).flags & FLAG_INITIALLY_DISABLED

    edit.clear_flag(plugin, FID_REFR, FLAG_INITIALLY_DISABLED)
    out = serialize(plugin)
    assert walk_clean(out) is True
    assert not (_find(parse_plugin(out), FID_REFR).flags & FLAG_INITIALLY_DISABLED)


def test_set_flag_missing_formid_raises(plugin):
    with pytest.raises(edit.EditError):
        edit.set_flag(plugin, 0x00DEAD00, FLAG_INITIALLY_DISABLED)


# --------------------------------------------------------------------------- #
# clone_cluster (+ insert_records to place the clones)
# --------------------------------------------------------------------------- #

def test_clone_cluster_walk_clean(plugin):
    before_next = _hedr(plugin).next_object_id
    before_nodes = _node_count_excluding_tes4(plugin)

    result = edit.clone_cluster(
        plugin, FID_REFR, count=2, translate=(10.0, -5.0, 0.0)
    )

    # Two fresh formIDs, allocated from nextObjectID (plugin index 0x00).
    assert len(result.records) == 2
    assert [r.form_id for r in result.records] == [
        NEXT_OBJECT_ID, NEXT_OBJECT_ID + 1
    ]
    # nextObjectID bumped by exactly the count.
    assert _hedr(plugin).next_object_id == before_next + 2

    # Clones alone don't change the tree yet; walk-clean holds regardless.
    assert walk_clean(serialize(plugin)) is True

    # Positions were translated as float32, rotation preserved.
    seed_data = fields.find_field(
        fields.parse_fields(_find(plugin, FID_REFR).data), b"DATA"
    ).payload
    seed_pr = fields.decode_data_posrot(seed_data)
    for clone in result.records:
        pr = fields.decode_data_posrot(
            fields.find_field(fields.parse_fields(clone.data), b"DATA").payload
        )
        assert pr.x == pytest.approx(seed_pr.x + 10.0)
        assert pr.y == pytest.approx(seed_pr.y - 5.0)
        assert pr.z == pytest.approx(seed_pr.z)
        assert pr.rx == pytest.approx(seed_pr.rx)
        assert pr.rz == pytest.approx(seed_pr.rz)
        # DATA is exactly six float32 (24 bytes), never a double.
        assert len(fields.find_field(
            fields.parse_fields(clone.data), b"DATA").payload) == 24

    # Now place them under the CELL's persistent children and re-check.
    grp = edit.insert_records(plugin, FID_CELL, result.records)
    assert grp.group_type == GT_CELL_PERSISTENT_CHILDREN
    out = serialize(plugin)
    assert walk_clean(out) is True

    # HEDR.num_records grew by the two inserted records; tree agrees.
    reparsed = parse_plugin(out)
    assert _hedr(reparsed).num_records == before_nodes + 2
    assert _node_count_excluding_tes4(reparsed) == before_nodes + 2


def test_clone_strips_xesp_and_reports_external_link():
    """A seed carrying an out-of-set XLKR and an XESP: link is reported (not
    remapped), XESP is stripped, and the whole thing stays walk-clean."""
    with open(FIXTURE, "rb") as fh:
        plugin = parse_plugin(fh.read())

    # Give the seed REFR an external XLKR (ref target 0x00AABBCC not in set) and
    # an XESP enable-parent, then clone it.
    refr = _find(plugin, FID_REFR)
    fl = fields.parse_fields(refr.data)
    fl.append(fields.Field(b"XLKR", fields.encode_xlkr(
        fields.LinkedRef(keyword_form_id=0x00000111,
                         ref_form_id=0x00AABBCC))))
    fl.append(fields.Field(b"XESP", fields.encode_xesp(
        fields.EnableParent(parent_form_id=0x00000801, flags=0x1))))
    refr.data = fields.pack_fields(fl)
    assert walk_clean(serialize(plugin)) is True

    result = edit.clone_cluster(plugin, FID_REFR, count=1, translate=(0, 0, 0))
    clone = result.records[0]
    clone_fields = fields.parse_fields(clone.data)

    # XESP stripped.
    assert fields.find_field(clone_fields, b"XESP") is None
    # XLKR kept verbatim (target outside the set) and reported.
    kept_xlkr = fields.find_field(clone_fields, b"XLKR")
    assert kept_xlkr is not None
    assert fields.decode_xlkr(kept_xlkr.payload).ref_form_id == 0x00AABBCC
    assert any(t == 0x00AABBCC for (_owner, _ctx, t) in result.external_links)
    # And the external XESP parent was reported too.
    assert any(t == 0x00000801 for (_owner, _ctx, t) in result.external_links)

    assert walk_clean(serialize(plugin)) is True


# --------------------------------------------------------------------------- #
# delete_records
# --------------------------------------------------------------------------- #

def test_delete_records_walk_clean(plugin):
    before_nodes = _node_count_excluding_tes4(plugin)
    before_num = _hedr(plugin).num_records

    removed = edit.delete_records(plugin, [FID_REFR])
    assert removed == [FID_REFR]

    out = serialize(plugin)
    assert walk_clean(out) is True

    reparsed = parse_plugin(out)
    # The record is gone.
    assert _find(reparsed, FID_REFR) is None
    # HEDR decremented by one; tree node-count agrees (empty type-8 GRUP kept).
    assert _hedr(reparsed).num_records == before_num - 1
    assert _node_count_excluding_tes4(reparsed) == before_nodes - 1


def test_delete_noncontiguous_and_missing(plugin):
    # Insert a second REFR so we can delete a non-contiguous pair, one of which
    # (0x00C0FFEE) doesn't exist and must be silently skipped.
    new = edit.build_refr(
        form_id=NEXT_OBJECT_ID, base=FID_STAT,
        pos=(1.0, 2.0, 3.0), rot=(0.0, 0.0, 0.0),
        flags=FLAG_PERSISTENT,
    )
    edit.insert_records(plugin, FID_CELL, [new])
    assert walk_clean(serialize(plugin)) is True
    before_num = _hedr(plugin).num_records

    removed = set(edit.delete_records(plugin, [FID_REFR, 0x00C0FFEE, NEXT_OBJECT_ID]))
    assert removed == {FID_REFR, NEXT_OBJECT_ID}      # missing id skipped

    out = serialize(plugin)
    assert walk_clean(out) is True
    reparsed = parse_plugin(out)
    assert _find(reparsed, FID_REFR) is None
    assert _find(reparsed, NEXT_OBJECT_ID) is None
    assert _hedr(reparsed).num_records == before_num - 2


def test_delete_nothing_is_noop(plugin):
    original = serialize(plugin)
    removed = edit.delete_records(plugin, [])
    assert removed == []
    assert serialize(plugin) == original


# --------------------------------------------------------------------------- #
# move_ref / build_refr / swap_model (supporting mutators)
# --------------------------------------------------------------------------- #

def test_move_ref_packs_float32_and_walk_clean(plugin):
    edit.move_ref(plugin, FID_REFR, pos=(1.5, 2.5, 3.5), rot=(0.1, 0.2, 0.3))
    out = serialize(plugin)
    assert walk_clean(out) is True

    data = fields.find_field(
        fields.parse_fields(_find(parse_plugin(out), FID_REFR).data), b"DATA"
    ).payload
    # Six 32-bit floats == 24 bytes; never an 8-byte double.
    assert len(data) == 24
    pr = fields.decode_data_posrot(data)
    assert struct.pack("<6f", *pr.as_tuple()) == data
    assert pr.x == pytest.approx(1.5)
    assert pr.rz == pytest.approx(0.3)


def test_build_refr_field_order_and_insert(plugin):
    refr = edit.build_refr(
        form_id=NEXT_OBJECT_ID, base=FID_STAT,
        pos=(0.0, 0.0, 0.0), rot=(0.0, 0.0, 0.0),
        flags=FLAG_PERSISTENT,
        xlkr=[(0x00000111, 0x00000222)],
        meta=[fields.Field(b"XSCL", fields.encode_xscl(2.0))],
    )
    assert refr.sig == "REFR"
    assert refr.form_id == NEXT_OBJECT_ID
    order = [f.sig for f in fields.parse_fields(refr.data)]
    assert order == ["NAME", "XLKR", "DATA", "XSCL"]
    assert len(fields.find_field(
        fields.parse_fields(refr.data), b"DATA").payload) == 24

    edit.insert_records(plugin, FID_CELL, [refr])
    assert walk_clean(serialize(plugin)) is True


def test_swap_model_copies_donor_fields(plugin):
    # Add a target STAT (EDID + a wrong MODL) into the top STAT GRUP, keeping
    # HEDR honest, then swap the fixture STAT's model onto it.
    target = Record(
        signature=b"STAT",
        header=b"STAT" + struct.pack("<IIIIHH", 0, 0, 0x00000901, 0, 0x2C, 0),
        data=fields.pack_fields([
            fields.Field(b"EDID", fields.encode_edid("TargetStat")),
            fields.Field(b"MODL", fields.encode_modl("old\\wrong.nif")),
        ]),
    )
    for node in plugin.top_level:
        if isinstance(node, Group) and node.label == b"STAT":
            node.children.append(target)
    tfl = fields.parse_fields(plugin.tes4.data)
    hf = fields.find_field(tfl, b"HEDR")
    h = fields.decode_hedr(hf.payload)
    h.num_records += 1
    hf.payload = fields.encode_hedr(h)
    plugin.tes4.data = fields.pack_fields(tfl)
    assert walk_clean(serialize(plugin)) is True

    donor_modl = fields.decode_modl(
        fields.find_field(fields.parse_fields(_find(plugin, FID_STAT).data),
                          b"MODL").payload)

    edit.swap_model(plugin, 0x00000901, FID_STAT)
    out = serialize(plugin)
    assert walk_clean(out) is True

    tgt = _find(parse_plugin(out), 0x00000901)
    tgt_fields = fields.parse_fields(tgt.data)
    # Donor's MODL now on the target...
    assert fields.decode_modl(
        fields.find_field(tgt_fields, b"MODL").payload) == donor_modl
    # ...and OBND (absent before) was copied in after EDID.
    sigs = [f.sig for f in tgt_fields]
    assert sigs[0] == "EDID"
    assert "OBND" in sigs
