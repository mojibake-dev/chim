"""Tests for :mod:`chim.loadorder` -- master-valid load-order sorting + mlox integration.

The pure functions (topo sort, mlox-output parsing, content I/O) are covered directly;
``plugin_masters`` and the full pipeline use a minimal hand-built TES3 plugin (a ``TES3``
header record carrying ``MAST`` subrecords) so no real Morrowind files are needed.
"""

import os
import struct

from chim import loadorder as LO


def _plugin(path: str, masters=()):
    """Write a minimal valid TES3 plugin (header record + optional MAST/DATA pairs)."""
    hedr = struct.pack("<fI", 1.3, 0) + b"\x00" * 32 + b"\x00" * 256 + struct.pack("<I", 0)  # 300 bytes
    subs = b"HEDR" + struct.pack("<I", len(hedr)) + hedr
    for m in masters:
        mb = m.encode("cp1252") + b"\x00"
        subs += b"MAST" + struct.pack("<I", len(mb)) + mb
        subs += b"DATA" + struct.pack("<I", 8) + struct.pack("<Q", 0)
    open(path, "wb").write(b"TES3" + struct.pack("<III", len(subs), 0, 0) + subs)


def test_topo_sort_orders_after_masters():
    plugins = ["C.esp", "A.esm", "B.esp"]
    masters = {"C.esp": ["A.esm"], "B.esp": ["C.esp"], "A.esm": []}
    out = LO.topo_sort(plugins, masters)
    assert out.index("A.esm") < out.index("C.esp") < out.index("B.esp")


def test_topo_sort_priority_breaks_ties():
    plugins = ["X.esp", "Y.esp", "Z.esp"]
    masters = {p: [] for p in plugins}
    out = LO.topo_sort(plugins, masters, priority={"z.esp": 0, "y.esp": 1, "x.esp": 2})
    assert out == ["Z.esp", "Y.esp", "X.esp"]


def test_topo_sort_fixes_master_before_dependent():
    # even when the priority wants the dependent first, the master must precede it
    plugins = ["patch.esp", "base.esm"]
    masters = {"patch.esp": ["base.esm"], "base.esm": []}
    out = LO.topo_sort(plugins, masters, priority={"patch.esp": 0, "base.esm": 1})
    assert out == ["base.esm", "patch.esp"]


def test_topo_sort_master_cycle_appended():
    plugins = ["A.esp", "B.esp"]                       # mutual masters (degenerate) -> both still emitted
    masters = {"A.esp": ["B.esp"], "B.esp": ["A.esp"]}
    assert sorted(LO.topo_sort(plugins, masters)) == ["A.esp", "B.esp"]


def test_parse_mlox_output():
    stdout = "\n".join([
        "noise line",
        "[CONFLICT]",
        " > 'A.esp'",
        " > 'B.esp'",
        " |  pick one",
        "--------------[New Load Order]--------------",
        "_001_ Morrowind.esm",
        "*002* Tribunal.esm",
        "*!003*! Weird.esp",
        "-----------[END PROPOSED LOAD ORDER]-----------",
        "trailing",
    ])
    order, warn = LO.parse_mlox_output(stdout, ["Morrowind.esm", "Tribunal.esm", "Weird.esp", "A.esp", "B.esp"])
    assert order == ["Morrowind.esm", "Tribunal.esm", "Weird.esp"]
    assert len(warn) == 1 and "CONFLICT" in warn[0] and "pick one" in warn[0]


def test_read_write_content(tmp_path):
    cfg = str(tmp_path / "openmw.cfg")
    open(cfg, "w").write("data=/x\ncontent=A.esp\ncontent=B.esp\nfallback-archive=M.bsa\n")
    assert LO.read_content(cfg) == ["A.esp", "B.esp"]
    LO.write_content(cfg, ["B.esp", "A.esp", "NotPresent.esp"], backup=False)
    assert LO.read_content(cfg) == ["B.esp", "A.esp"]              # reordered; unknown ignored
    txt = open(cfg).read()
    assert "data=/x" in txt and "fallback-archive=M.bsa" in txt    # non-content lines preserved


def test_read_axes(tmp_path):
    cfg = str(tmp_path / "openmw.cfg")
    open(cfg, "w").write(
        'data="/home/user/Data Files"\n'
        "data=/plain/path\n"
        "content=A.esp\n"
        "content=B.esp\n"
        "fallback-archive=Morrowind.bsa\n"
        "fallback-archive=Tribunal.bsa\n"
        "groundcover=Grass.esp\n"
    )
    assert LO.read_data_dirs(cfg) == ["/home/user/Data Files", "/plain/path"]  # quotes stripped
    assert LO.read_archives(cfg) == ["Morrowind.bsa", "Tribunal.bsa"]
    assert LO.read_groundcover(cfg) == ["Grass.esp"]
    assert LO.read_content(cfg) == ["A.esp", "B.esp"]


def test_cfg_escape_roundtrip():
    for p in ["/plain/path", "/has spaces/x", '/weird&"chars/y', "C:\\Games\\Data Files"]:
        assert LO._unescape_cfg(LO._escape_cfg(p)) == p
    assert LO._escape_cfg("/plain/path") == "/plain/path"          # no quotes when unneeded
    assert LO._escape_cfg("/has spaces/x").startswith('"')          # quoted when spaced


def test_write_all_axes(tmp_path):
    cfg = str(tmp_path / "openmw.cfg")
    open(cfg, "w").write(
        "# my config\n"
        "data=/old/dir\n"
        "content=Old.esp\n"
        "fallback-archive=Old.bsa\n"
        "some-setting=keepme\n"
    )
    LO.write_all_axes(
        cfg,
        data=["/canon/Data Files"],
        content=["Morrowind.esm", "Canon.esp"],
        archives=["Morrowind.bsa"],
        groundcover=["Grass.esp"],  # absent originally -> appended
        backup=False,
    )
    assert LO.read_data_dirs(cfg) == ["/canon/Data Files"]
    assert LO.read_content(cfg) == ["Morrowind.esm", "Canon.esp"]
    assert LO.read_archives(cfg) == ["Morrowind.bsa"]
    assert LO.read_groundcover(cfg) == ["Grass.esp"]
    txt = open(cfg).read()
    assert "# my config" in txt and "some-setting=keepme" in txt  # unrelated lines preserved
    assert txt.count("content=") == 2 and txt.count("data=") == 1  # old block replaced, not appended


def test_write_all_axes_leaves_unspecified_axis(tmp_path):
    cfg = str(tmp_path / "openmw.cfg")
    open(cfg, "w").write("data=/keep\ncontent=Keep.esp\n")
    LO.write_all_axes(cfg, content=["New.esp"], backup=False)  # only content provided
    assert LO.read_data_dirs(cfg) == ["/keep"]                 # data untouched
    assert LO.read_content(cfg) == ["New.esp"]


def test_plugin_masters(tmp_path):
    _plugin(str(tmp_path / "Child.esp"), masters=["Parent.esm", "Other.esm"])
    assert LO.plugin_masters(str(tmp_path), "Child.esp") == ["Parent.esm", "Other.esm"]
    _plugin(str(tmp_path / "Loner.esp"))
    assert LO.plugin_masters(str(tmp_path), "Loner.esp") == []
    open(str(tmp_path / "x.omwscripts"), "w").write("LUA = y.lua")
    assert LO.plugin_masters(str(tmp_path), "x.omwscripts") == []  # not a TES3 plugin
    assert LO.plugin_masters(str(tmp_path), "missing.esp") == []


def test_sort_load_order_pipeline_no_mlox(tmp_path):
    d = str(tmp_path)
    _plugin(os.path.join(d, "Base.esm"))
    _plugin(os.path.join(d, "Patch.esp"), masters=["Base.esm"])
    _plugin(os.path.join(d, "Other.esp"))
    cfg = os.path.join(d, "openmw.cfg")
    open(cfg, "w").write("content=Patch.esp\ncontent=Other.esp\ncontent=Base.esm\n")
    # priority wants Patch before Base, but the master constraint must win
    r = LO.sort_load_order(cfg, d, use_mlox=False,
                           priority={"other.esp": 0, "patch.esp": 1, "base.esm": 2})
    assert r["applied"] and not r["master_violations"] and not r["mlox_used"]
    order = LO.read_content(cfg)
    assert order.index("Base.esm") < order.index("Patch.esp")
    assert order[0] == "Other.esp"                                 # priority honoured where masters allow


def test_sort_load_order_preview(tmp_path):
    d = str(tmp_path)
    _plugin(os.path.join(d, "A.esm"))
    cfg = os.path.join(d, "openmw.cfg")
    open(cfg, "w").write("content=A.esm\n")
    r = LO.sort_load_order(cfg, d, use_mlox=False, apply=False)
    assert not r["applied"] and r["order"] == ["A.esm"]
