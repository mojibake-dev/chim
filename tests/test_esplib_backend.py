"""End-to-end tests for the esplib-backed chim engine (``chim.esp.ops``).

These exercise the adapter that replaced chim's own byte engine. They use only
synthetic fixtures (no game files): the suite's ``sample.esp`` -- a minimal but
real Skyrim SE plugin whose CELL carries a type-6 children GRUP with a nested
type-8 persistent-children GRUP holding one REFR -- plus esplib's own
``make_*`` binary helpers for a couple of standalone shapes.

Coverage:
  * byte-identical round-trip through ``ops.load`` / ``ops.serialize``
  * ``insert_ref`` into a CELL's persistent-children GRUP (+ XLKR)
  * ``clone_cluster`` (fresh formIDs, translate, XLKR remap, XESP strip)
  * ``clone_record`` (top-level dup into the seed's own GRUP)
  * ``delete_records`` and ``delete_cells``
  * ``move_ref`` and ``set_flag`` (set + clear force-enable)
  * subrecord set / patch / insert / delete
  * the mutating MCP tools driven through a real ``safety.transaction`` on a
    ``LocalHost`` temp copy (verifying the load->edit->serialize->walk-clean
    seam the whole server depends on).
"""

from __future__ import annotations

import os
import shutil
import struct

import pytest

from chim.esp import ops, safety
from chim.esp.records import walk_clean
from chim import server


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

# formIDs baked into the sample.esp fixture (see fixtures/make_fixture.py).
FID_STAT = 0x00000800
FID_CELL = 0x00000801
FID_REFR = 0x00000802
NEXT_OBJECT_ID = 0x00000803


@pytest.fixture()
def plug(raw):
    """A freshly-parsed esplib Plugin from the sample.esp bytes."""
    return ops.load(raw)


@pytest.fixture()
def live_plugin(tmp_path, fixture_path):
    """A LocalHost + a writable temp copy of sample.esp for transaction tests.

    Returns ``(host, path)``. LocalHost reports no lock-holding processes, so the
    safety transaction runs its full backup / write / walk-clean-verify path.
    """
    dst = os.path.join(str(tmp_path), "TestMod.esp")
    shutil.copyfile(fixture_path, dst)
    return safety.LocalHost(), dst


# --------------------------------------------------------------------------- #
# Round-trip seam
# --------------------------------------------------------------------------- #

def test_roundtrip_byte_identical(raw):
    """ops.load then ops.serialize is byte-for-byte identical (the seam)."""
    plug = ops.load(raw)
    assert ops.serialize(plug) == raw


def test_roundtrip_double(raw):
    """A second parse of the serialized output is still identical."""
    once = ops.serialize(ops.load(raw))
    twice = ops.serialize(ops.load(once))
    assert once == raw
    assert twice == raw


def test_serialized_output_walk_clean(raw):
    """The serialized bytes pass chim.safety's walk_clean gate."""
    assert walk_clean(ops.serialize(ops.load(raw)))


# --------------------------------------------------------------------------- #
# insert_ref
# --------------------------------------------------------------------------- #

def test_insert_ref_into_persistent_group(plug):
    new_id = ops.insert_ref(
        plug, FID_CELL, FID_STAT, [1.0, 2.0, 3.0], [0.0, 0.0, 0.0],
        xlkr=[(0x111, FID_REFR)],
    )
    assert new_id == (NEXT_OBJECT_ID)  # first mint from HEDR.nextObjectID

    out = ops.serialize(plug)
    assert walk_clean(out)
    re = ops.load(out)

    ref = ops.find_record(re, new_id)
    assert ref is not None
    assert ref.signature == "REFR"
    assert int(ref.flags) & ops.FLAG_PERSISTENT
    # base via NAME
    assert struct.unpack("<I", ref.get_subrecord("NAME").data[:4])[0] == FID_STAT
    # XLKR present
    kw, target = struct.unpack("<II", ref.get_subrecord("XLKR").data[:8])
    assert (kw, target) == (0x111, FID_REFR)
    # DATA pos
    assert struct.unpack("<6f", ref.get_subrecord("DATA").data)[:3] == (1.0, 2.0, 3.0)
    # It lives inside the cell's ref set now (persistent group).
    assert new_id in {r.form_id.value for r in ops.cell_refs(re, FID_CELL)}


def test_insert_ref_missing_cell_raises(plug):
    with pytest.raises(ops.OpsError):
        ops.insert_ref(plug, 0x00DEAD01, FID_STAT, [0, 0, 0], [0, 0, 0])


# --------------------------------------------------------------------------- #
# clone_cluster
# --------------------------------------------------------------------------- #

def test_clone_cluster_translate_and_attach(plug):
    res = ops.clone_cluster(plug, FID_REFR, 2, [10.0, 0.0, 0.0],
                            into_cell=FID_CELL)
    assert len(res.records) == 2
    ids = [r.form_id.value for r in res.records]
    assert ids == [NEXT_OBJECT_ID, NEXT_OBJECT_ID + 1]
    assert res.external_links == []  # the seed REFR has no XLKR/XESP

    out = ops.serialize(plug)
    assert walk_clean(out)
    re = ops.load(out)

    # Seed's DATA.x was 100.0; each clone is +10.0.
    for cid in ids:
        clone = ops.find_record(re, cid)
        assert clone is not None
        assert struct.unpack("<6f", clone.get_subrecord("DATA").data)[0] == 110.0
    # All three refs (seed + 2 clones) now in the cell.
    assert len(ops.cell_refs(re, FID_CELL)) == 3


def test_clone_cluster_unattached(plug):
    res = ops.clone_cluster(plug, FID_REFR, 1, [0, 0, 0])
    assert len(res.records) == 1
    out = ops.serialize(plug)
    re = ops.load(out)
    # Not spliced anywhere -> not among the cell's refs, and not found by index.
    assert res.records[0].form_id.value not in {
        r.form_id.value for r in ops.cell_refs(re, FID_CELL)}


def test_clone_cluster_remaps_xlkr_and_strips_xesp(plug):
    """A seed carrying an intra-set XLKR self-link + an XESP is remapped/stripped."""
    seed = ops.find_record(plug, FID_REFR)
    # self-referential XLKR (ref == seed) + an XESP enable-parent.
    ops.insert_subrecord(seed, "XLKR", struct.pack("<II", 0x222, FID_REFR),
                         after="NAME")
    ops.insert_subrecord(seed, "XESP", struct.pack("<II", FID_CELL, 1),
                         after="NAME")

    res = ops.clone_cluster(plug, FID_REFR, 1, [0, 0, 0])
    clone = res.records[0]
    # XESP stripped.
    assert clone.get_subrecord("XESP") is None
    # XLKR self-link remapped to the clone's own new formID.
    kw, ref = struct.unpack("<II", clone.get_subrecord("XLKR").data[:8])
    assert kw == 0x222
    assert ref == clone.form_id.value
    # The XESP parent pointed outside the clone set -> reported as external.
    assert any(t == FID_CELL for (_o, _c, t) in res.external_links)


# --------------------------------------------------------------------------- #
# clone_record
# --------------------------------------------------------------------------- #

def test_clone_record_top_level(plug):
    clone, new_id = ops.clone_record(plug, FID_STAT)
    assert clone.signature == "STAT"
    assert new_id == NEXT_OBJECT_ID

    out = ops.serialize(plug)
    assert walk_clean(out)
    re = ops.load(out)
    dup = ops.find_record(re, new_id)
    assert dup is not None
    assert dup.signature == "STAT"
    # Same EDID (clone_record does not rename); two STATs now.
    stats = list(re.get_records_by_signature("STAT"))
    assert len(stats) == 2


# --------------------------------------------------------------------------- #
# delete_records / delete_cells
# --------------------------------------------------------------------------- #

def test_delete_records(plug):
    removed = ops.delete_records(plug, [FID_REFR])
    assert removed == [FID_REFR]
    out = ops.serialize(plug)
    assert walk_clean(out)
    re = ops.load(out)
    assert ops.find_record(re, FID_REFR) is None


def test_delete_cells_drops_cell_and_children(plug):
    removed = ops.delete_cells(plug, [FID_CELL])
    # CELL + its one persistent REFR = 2 records.
    assert removed == [(FID_CELL, 2)]
    out = ops.serialize(plug)
    assert walk_clean(out)
    re = ops.load(out)
    assert ops.find_record(re, FID_CELL) is None
    assert ops.find_record(re, FID_REFR) is None


# --------------------------------------------------------------------------- #
# move_ref / set_flag
# --------------------------------------------------------------------------- #

def test_move_ref(plug):
    ops.move_ref(plug, FID_REFR, [9.0, 8.0, 7.0], [0.1, 0.2, 0.3])
    out = ops.serialize(plug)
    assert walk_clean(out)
    re = ops.load(out)
    data = struct.unpack("<6f", ops.find_record(re, FID_REFR).get_subrecord("DATA").data)
    assert data[:3] == (9.0, 8.0, 7.0)
    assert data[3] == pytest.approx(0.1)


def test_set_and_clear_flag(plug):
    ops.set_flag(plug, FID_REFR, ops.FLAG_INITIALLY_DISABLED)
    re = ops.load(ops.serialize(plug))
    assert int(ops.find_record(re, FID_REFR).flags) & ops.FLAG_INITIALLY_DISABLED

    # Clear it (force-enable) on the already-modified plugin.
    ops.set_flag(plug, FID_REFR, ops.FLAG_INITIALLY_DISABLED, clear=True)
    re = ops.load(ops.serialize(plug))
    assert not (int(ops.find_record(re, FID_REFR).flags) & ops.FLAG_INITIALLY_DISABLED)
    # Persistent bit (0x400) was preserved throughout.
    assert int(ops.find_record(re, FID_REFR).flags) & ops.FLAG_PERSISTENT


# --------------------------------------------------------------------------- #
# subrecord CRUD
# --------------------------------------------------------------------------- #

def test_subrecord_set(plug):
    stat = ops.find_record(plug, FID_STAT)
    ops.set_subrecord(stat, "EDID", b"RenamedStatic\x00")
    re = ops.load(ops.serialize(plug))
    assert ops.find_record(re, FID_STAT).editor_id == "RenamedStatic"


def test_subrecord_patch(plug):
    stat = ops.find_record(plug, FID_STAT)
    ops.patch_subrecord(stat, "OBND", 0, struct.pack("<h", -99))
    re = ops.load(ops.serialize(plug))
    obnd = struct.unpack("<6h", ops.find_record(re, FID_STAT).get_subrecord("OBND").data)
    assert obnd[0] == -99


def test_subrecord_patch_out_of_bounds(plug):
    stat = ops.find_record(plug, FID_STAT)
    with pytest.raises(ops.OpsError):
        ops.patch_subrecord(stat, "OBND", 10, b"\x00\x00\x00\x00")


def test_subrecord_insert_after_and_delete(plug):
    stat = ops.find_record(plug, FID_STAT)
    pos = ops.insert_subrecord(stat, "XZZZ", b"\x01\x02", after="EDID")
    assert pos == 1
    removed = ops.delete_subrecord(stat, "MODL")
    assert removed.signature == "MODL"
    re = ops.load(ops.serialize(plug))
    sigs = [f["sig"] for f in ops.field_views(ops.find_record(re, FID_STAT))]
    assert sigs == ["EDID", "XZZZ", "OBND"]


def test_field_views_indices(plug):
    stat = ops.find_record(plug, FID_STAT)
    ops.insert_subrecord(stat, "XLKR", struct.pack("<II", 1, 2), after="EDID")
    ops.insert_subrecord(stat, "XLKR", struct.pack("<II", 3, 4), after="XLKR")
    views = ops.field_views(stat, only="XLKR")
    assert [v["index"] for v in views] == [0, 1]


# --------------------------------------------------------------------------- #
# Query helpers
# --------------------------------------------------------------------------- #

def test_query_helpers(plug):
    assert ops.find_by_edid(plug, "^ChimTestStatic$")[0].form_id.value == FID_STAT
    assert ops.find_by_base(plug, FID_STAT)[0].form_id.value == FID_REFR
    assert ops.find_by_cell(plug, FID_CELL).form_id.value == FID_CELL
    assert ops.find_by_cell(plug, "ChimTestCell").form_id.value == FID_CELL
    bmap = ops.base_map(plug, ("STAT",))
    assert bmap[FID_STAT][0] == "ChimTestStatic"
    assert bmap[FID_STAT][1] == "STAT"
    assert bmap[FID_STAT][2] == "chim\\teststatic.nif"
    assert bmap[FID_STAT][3] == (-16, -16, 0, 16, 16, 32)


# --------------------------------------------------------------------------- #
# Full MCP tool path through safety.transaction (LocalHost temp copy)
# --------------------------------------------------------------------------- #

def _patch_host(monkeypatch, host):
    monkeypatch.setattr(server, "_make_host", lambda: host)


def test_tool_insert_ref_end_to_end(monkeypatch, live_plugin):
    host, path = live_plugin
    _patch_host(monkeypatch, host)
    result = server.esp_insert_ref(
        path, hex(FID_CELL), hex(FID_STAT),
        [5.0, 6.0, 7.0], [0.0, 0.0, 0.0],
        xlkr=[[hex(0x111), hex(FID_REFR)]],
    )
    assert result["new_form_id"] == "0x00000803"
    assert result["backup"].endswith(".bak")
    # File was written + verified walk-clean by the transaction.
    with open(path, "rb") as fh:
        data = fh.read()
    assert walk_clean(data)
    re = ops.load(data)
    assert ops.find_record(re, NEXT_OBJECT_ID) is not None


def test_tool_set_flag_end_to_end(monkeypatch, live_plugin):
    host, path = live_plugin
    _patch_host(monkeypatch, host)
    result = server.esp_set_flag(path, hex(FID_REFR), "initially_disabled")
    assert result["cleared"] is False
    with open(path, "rb") as fh:
        data = fh.read()
    assert int(ops.load(data).get_record_by_form_id(FID_REFR).flags) \
        & ops.FLAG_INITIALLY_DISABLED


def test_tool_clone_cluster_end_to_end(monkeypatch, live_plugin):
    host, path = live_plugin
    _patch_host(monkeypatch, host)
    result = server.esp_clone_cluster(
        path, hex(FID_REFR), 2, [10.0, 0.0, 0.0], into_cell=hex(FID_CELL))
    assert len(result["new_form_ids"]) == 2
    with open(path, "rb") as fh:
        data = fh.read()
    assert walk_clean(data)
    assert len(ops.cell_refs(ops.load(data), FID_CELL)) == 3


def test_tool_delete_cells_end_to_end(monkeypatch, live_plugin):
    host, path = live_plugin
    _patch_host(monkeypatch, host)
    result = server.esp_delete_cells(path, [hex(FID_CELL)])
    assert result["count"] == 1
    assert result["reverted"][0]["records_removed"] == 2
    with open(path, "rb") as fh:
        data = fh.read()
    assert walk_clean(data)
    assert ops.load(data).get_record_by_form_id(FID_CELL) is None


def test_tool_query_and_get_record(monkeypatch, live_plugin):
    host, path = live_plugin
    _patch_host(monkeypatch, host)
    q = server.esp_query(path, "formid", hex(FID_STAT))
    assert q["count"] == 1
    assert q["records"][0]["sig"] == "STAT"
    detail = server.esp_get_record(path, hex(FID_STAT))
    assert detail["edid"] == "ChimTestStatic"
    field_sigs = [f["sig"] for f in detail["fields"]]
    assert "MODL" in field_sigs

    cellrefs = server.esp_query(path, "cellrefs", hex(FID_CELL))
    assert cellrefs["count"] == 1
    assert cellrefs["refs"][0]["base"] == "0x00000800"
    assert cellrefs["refs"][0]["persistent"] is True
