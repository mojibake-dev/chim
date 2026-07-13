"""Tests for chim.esp.query against the synthetic sample.esp fixture.

The fixture (see ``tests/fixtures/make_fixture.py``) contains:

    TES4
    GRUP 'STAT'  -> STAT 0x00000800  (EDID "ChimTestStatic", OBND, MODL)
    GRUP 'CELL'  -> ... -> CELL 0x00000801 (EDID "ChimTestCell")
                          -> REFR 0x00000802 (NAME->STAT, DATA) [persistent]

That covers find_by_formid / find_by_base / find_by_cell / find_by_edid /
base_map. The fixture has no XESP/XLKR fields, so transitive_closure is
exercised against a small synthetic plugin built from the same on-disk
primitives and round-tripped through parse_plugin.
"""

from __future__ import annotations

import os
import re
import struct

import pytest

from chim.esp.records import parse_plugin, walk_clean, Record
from chim.esp import query

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample.esp")

# formIDs baked into the fixture.
FID_STAT = 0x00000800
FID_CELL = 0x00000801
FID_REFR = 0x00000802


@pytest.fixture(scope="module")
def plugin():
    with open(FIXTURE, "rb") as fh:
        return parse_plugin(fh.read())


# --------------------------------------------------------------------------- #
# find_by_formid
# --------------------------------------------------------------------------- #

def test_find_by_formid_hits(plugin):
    stat = query.find_by_formid(plugin, FID_STAT)
    assert stat is not None and stat.sig == "STAT"
    assert stat.form_id == FID_STAT

    cell = query.find_by_formid(plugin, FID_CELL)
    assert cell is not None and cell.sig == "CELL"

    refr = query.find_by_formid(plugin, FID_REFR)
    assert refr is not None and refr.sig == "REFR"


def test_find_by_formid_finds_tes4(plugin):
    tes4 = query.find_by_formid(plugin, 0x00000000)
    assert tes4 is not None and tes4.sig == "TES4"


def test_find_by_formid_miss(plugin):
    assert query.find_by_formid(plugin, 0x00FFFFFF) is None


# --------------------------------------------------------------------------- #
# find_by_base
# --------------------------------------------------------------------------- #

def test_find_by_base_returns_placement(plugin):
    refs = query.find_by_base(plugin, FID_STAT)
    assert [r.sig for r in refs] == ["REFR"]
    assert refs[0].form_id == FID_REFR


def test_find_by_base_no_match(plugin):
    assert query.find_by_base(plugin, FID_CELL) == []
    assert query.find_by_base(plugin, 0x00DEAD00) == []


# --------------------------------------------------------------------------- #
# find_by_cell
# --------------------------------------------------------------------------- #

def test_find_by_cell_by_edid(plugin):
    cell = query.find_by_cell(plugin, "ChimTestCell")
    assert cell is not None and cell.form_id == FID_CELL


def test_find_by_cell_by_formid(plugin):
    cell = query.find_by_cell(plugin, FID_CELL)
    assert cell is not None and cell.sig == "CELL"
    assert query.record_edid(cell) == "ChimTestCell"


def test_find_by_cell_miss(plugin):
    assert query.find_by_cell(plugin, "NoSuchCell") is None
    assert query.find_by_cell(plugin, 0x00ABCDEF) is None


def test_find_by_cell_edid_is_exact_not_substring(plugin):
    # "ChimTest" is a prefix of the real EDID but not equal -> no match.
    assert query.find_by_cell(plugin, "ChimTest") is None


# --------------------------------------------------------------------------- #
# find_by_edid
# --------------------------------------------------------------------------- #

def test_find_by_edid_substring(plugin):
    hits = {r.sig for r in query.find_by_edid(plugin, "ChimTest")}
    assert hits == {"STAT", "CELL"}


def test_find_by_edid_anchored(plugin):
    hits = query.find_by_edid(plugin, r"^ChimTestStatic$")
    assert [r.form_id for r in hits] == [FID_STAT]


def test_find_by_edid_accepts_compiled_pattern(plugin):
    rx = re.compile(r"Cell$")
    hits = query.find_by_edid(plugin, rx)
    assert [r.sig for r in hits] == ["CELL"]


def test_find_by_edid_no_match(plugin):
    assert query.find_by_edid(plugin, "ZZZ_not_present") == []


# --------------------------------------------------------------------------- #
# base_map
# --------------------------------------------------------------------------- #

def test_base_map_stat(plugin):
    bm = query.base_map(plugin, ("STAT",))
    assert set(bm) == {FID_STAT}
    edid, sig, mesh, obnd = bm[FID_STAT]
    assert edid == "ChimTestStatic"
    assert sig == "STAT"
    assert mesh == "chim\\teststatic.nif"
    assert obnd == (-16, -16, 0, 16, 16, 32)


def test_base_map_multiple_sigs(plugin):
    bm = query.base_map(plugin, ("STAT", "CELL"))
    assert set(bm) == {FID_STAT, FID_CELL}
    # CELL in this fixture has an EDID but no MODL / OBND.
    cedid, csig, cmesh, cobnd = bm[FID_CELL]
    assert (cedid, csig, cmesh, cobnd) == ("ChimTestCell", "CELL", None, None)


def test_base_map_empty_when_sig_absent(plugin):
    assert query.base_map(plugin, ("WEAP",)) == {}


# --------------------------------------------------------------------------- #
# transitive_closure -- built on a synthetic XESP/XLKR plugin
# --------------------------------------------------------------------------- #

_TS_VC = 0
_IVERSION = 0x2C
_UNK16 = 0


def _sub(sig: bytes, payload: bytes) -> bytes:
    return sig + struct.pack("<H", len(payload)) + payload


def _rec(sig: bytes, form_id: int, data: bytes, flags: int = 0) -> bytes:
    header = sig + struct.pack(
        "<IIIIHH", len(data), flags, form_id, _TS_VC, _IVERSION, _UNK16
    )
    return header + data


def _grp(label: bytes, gtype: int, children: bytes) -> bytes:
    header = b"GRUP" + struct.pack(
        "<I4siIH", 24 + len(children), label, gtype, _TS_VC, _UNK16
    ) + struct.pack("<H", 0)
    return header + children


def _xesp(parent_fid: int, flags: int = 0) -> bytes:
    return struct.pack("<II", parent_fid, flags)


def _xlkr(keyword_fid: int, ref_fid: int) -> bytes:
    return struct.pack("<II", keyword_fid, ref_fid)


# Cluster we build under one CELL:
#   STAT  0x0800  (base, MODL)
#   KYWD  0x0900  (a keyword an XLKR references -- NOT a placement, not followed)
#   REFR  0x0810  parent of the group (no links)          <- seed target
#   REFR  0x0811  XESP -> 0x0810           (child of 0810)
#   REFR  0x0812  XLKR keyword=0x0900 ref=0x0810  (links to 0810)
#   REFR  0x0820  isolated, NAME->STAT but no XESP/XLKR    (NOT in cluster)
#   REFR  0x0813  XLKR ref=0x0DEADBEE (dangling, ignored)  + XESP->0x0811
FID_BASE = 0x00000800
FID_KYWD = 0x00000900
FID_R_PARENT = 0x00000810
FID_R_CHILD = 0x00000811
FID_R_LINKER = 0x00000812
FID_R_ISOLATED = 0x00000820
FID_R_CHAIN = 0x00000813
FID_CELL2 = 0x00000700


def _build_cluster_plugin() -> bytes:
    hedr = struct.pack("<fiI", 1.71, 0, 0x00001000)
    tes4 = _rec(b"TES4", 0x00000000,
                _sub(b"HEDR", hedr) + _sub(b"CNAM", b"chim\x00"))

    stat = _rec(b"STAT", FID_BASE,
                _sub(b"EDID", b"ClusterBase\x00")
                + _sub(b"OBND", struct.pack("<6h", -1, -1, 0, 1, 1, 2))
                + _sub(b"MODL", b"chim\\clusterbase.nif\x00"))
    stat_grp = _grp(b"STAT", 0, stat)

    kywd = _rec(b"KYWD", FID_KYWD, _sub(b"EDID", b"ClusterKeyword\x00"))
    kywd_grp = _grp(b"KYWD", 0, kywd)

    def refr(fid, extra=b"", flags=0):
        data = (_sub(b"EDID", struct.pack("<I", fid).hex().encode() + b"\x00")
                + _sub(b"NAME", struct.pack("<I", FID_BASE))
                + _sub(b"DATA", struct.pack("<6f", 0, 0, 0, 0, 0, 0))
                + extra)
        return _rec(b"REFR", fid, data, flags=flags)

    r_parent = refr(FID_R_PARENT)
    r_child = refr(FID_R_CHILD, extra=_sub(b"XESP", _xesp(FID_R_PARENT)))
    r_linker = refr(FID_R_LINKER,
                    extra=_sub(b"XLKR", _xlkr(FID_KYWD, FID_R_PARENT)))
    r_isolated = refr(FID_R_ISOLATED)
    r_chain = refr(FID_R_CHAIN,
                   extra=_sub(b"XESP", _xesp(FID_R_CHILD))
                   + _sub(b"XLKR", _xlkr(FID_KYWD, 0x00DEADBE)))  # dangling ref

    cell = _rec(b"CELL", FID_CELL2, _sub(b"EDID", b"ClusterCell\x00"))
    refrs = r_parent + r_child + r_linker + r_isolated + r_chain
    persistent = _grp(struct.pack("<I", FID_CELL2), 8, refrs)
    cell_children = _grp(struct.pack("<I", FID_CELL2), 6, persistent)
    subblock = _grp(struct.pack("<i", 0), 3, cell + cell_children)
    block = _grp(struct.pack("<i", 0), 2, subblock)
    cell_grp = _grp(b"CELL", 0, block)

    return tes4 + stat_grp + kywd_grp + cell_grp


@pytest.fixture(scope="module")
def cluster_plugin():
    raw = _build_cluster_plugin()
    # Sanity: the synthetic plugin we hand-assembled is itself walk-clean.
    assert walk_clean(raw) is True
    return parse_plugin(raw)


def test_cluster_fixture_is_walk_clean():
    assert walk_clean(_build_cluster_plugin()) is True


def test_closure_from_parent_gathers_family(cluster_plugin):
    cluster = query.transitive_closure(cluster_plugin, FID_R_PARENT)
    # parent + XESP child + XLKR linker + child-of-child chain, all connected.
    assert set(cluster) == {
        FID_R_PARENT, FID_R_CHILD, FID_R_LINKER, FID_R_CHAIN
    }
    # Every value is the Record with that formID.
    for fid, rec in cluster.items():
        assert isinstance(rec, Record) and rec.form_id == fid


def test_closure_from_child_walks_up_and_across(cluster_plugin):
    # Seeding a leaf still gathers the whole connected component.
    cluster = query.transitive_closure(cluster_plugin, FID_R_CHAIN)
    assert set(cluster) == {
        FID_R_PARENT, FID_R_CHILD, FID_R_LINKER, FID_R_CHAIN
    }


def test_closure_excludes_isolated_reference(cluster_plugin):
    cluster = query.transitive_closure(cluster_plugin, FID_R_PARENT)
    assert FID_R_ISOLATED not in cluster


def test_closure_does_not_follow_xlkr_keyword(cluster_plugin):
    # The KYWD (0x0900) is referenced as the *keyword* of an XLKR, never as a
    # ref target, so it must not be pulled into the cluster.
    cluster = query.transitive_closure(cluster_plugin, FID_R_PARENT)
    assert FID_KYWD not in cluster


def test_closure_ignores_base_and_dangling(cluster_plugin):
    cluster = query.transitive_closure(cluster_plugin, FID_R_PARENT)
    # NAME->STAT is not an XESP/XLKR edge, so the base is not gathered.
    assert FID_BASE not in cluster
    # The dangling XLKR target (0x00DEADBE) resolves to nothing -> ignored.
    assert 0x00DEADBE not in cluster


def test_closure_seed_absent_returns_empty(cluster_plugin):
    assert query.transitive_closure(cluster_plugin, 0x00123456) == {}


def test_closure_seed_isolated_returns_just_itself(cluster_plugin):
    cluster = query.transitive_closure(cluster_plugin, FID_R_ISOLATED)
    assert set(cluster) == {FID_R_ISOLATED}
