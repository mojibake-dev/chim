"""Tests for the OpenMW/Morrowind mod installer (:mod:`chim.modinstall`).

Filesystem-based: build a synthetic extracted mod tree (a "Data Files" folder with
a plugin, a BSA, and loose meshes/textures), then exercise plan / filter /
install / uninstall / cfg-registration / archive-install / surgical-BSA-extract.
"""

import os
import struct
import zipfile

import pytest

from chim import modinstall as M
from chim.tes3.bsa import BSA_VERSION


def _make_mod_tree(root: str) -> str:
    df = os.path.join(root, "Data Files")
    os.makedirs(os.path.join(df, "Meshes", "sub"))
    os.makedirs(os.path.join(df, "Textures"))
    open(os.path.join(df, "MyMod.esp"), "wb").write(b"TES3fake")
    open(os.path.join(df, "MyMod.bsa"), "wb").write(b"\x00\x01\x00\x00")
    open(os.path.join(df, "Meshes", "sub", "x.nif"), "wb").write(b"nif")
    open(os.path.join(df, "Textures", "y.tga"), "wb").write(b"tga")
    return df


def _build_bsa(files: dict) -> bytes:
    names = list(files); count = len(names)
    nblobs = [n.encode("cp1252") + b"\x00" for n in names]
    noffs, acc = [], 0
    for nb in nblobs:
        noffs.append(acc); acc += len(nb)
    nt = b"".join(nblobs)
    recs, data = [], bytearray()
    for n in names:
        recs.append((len(files[n]), len(data))); data += files[n]
    hoff = 8 * count + 4 * count + len(nt)
    out = bytearray(struct.pack("<III", BSA_VERSION, hoff, count))
    for s, o in recs:
        out += struct.pack("<II", s, o)
    for no in noffs:
        out += struct.pack("<I", no)
    out += nt + b"\x00" * (8 * count) + bytes(data)
    return bytes(out)


def test_find_data_root_named(tmp_path):
    df = _make_mod_tree(str(tmp_path / "mod"))
    assert M.find_data_root(str(tmp_path / "mod")) == df


def test_find_data_root_loose(tmp_path):
    r = str(tmp_path / "loose"); os.makedirs(os.path.join(r, "Meshes"))
    open(os.path.join(r, "Mod.esp"), "wb").write(b"x")
    open(os.path.join(r, "Meshes", "a.nif"), "wb").write(b"n")
    assert M.find_data_root(r) == r


def test_plan_and_filters(tmp_path):
    df = _make_mod_tree(str(tmp_path / "mod"))
    plan = M.plan_install(df)
    assert plan.plugins == ["MyMod.esp"] and plan.archives == ["MyMod.bsa"]
    assert plan.summary()["file_count"] == 4
    only_meshes = M.plan_install(df, include=["Meshes/*"])
    assert [i.rel.replace("\\", "/") for i in only_meshes.items] == ["Meshes/sub/x.nif"]
    no_bsa = M.plan_install(df, exclude=["*.bsa"])
    assert "MyMod.bsa" not in no_bsa.archives


def test_install_uninstall_roundtrip(tmp_path):
    df = _make_mod_tree(str(tmp_path / "mod"))
    data = str(tmp_path / "game"); os.makedirs(data)
    cfg = str(tmp_path / "openmw.cfg")
    open(cfg, "w").write("content=Morrowind.esm\nfallback-archive=Morrowind.bsa\n")
    md = str(tmp_path / "manifests")
    man = M.install(M.plan_install(df), data, cfg, "MyMod", md)
    assert os.path.exists(os.path.join(data, "MyMod.esp"))
    assert os.path.exists(os.path.join(data, "Meshes", "sub", "x.nif"))
    ctxt = open(cfg).read()
    assert "content=MyMod.esp" in ctxt and "fallback-archive=MyMod.bsa" in ctxt
    assert man["new_file_count"] == 4
    assert os.path.exists(os.path.join(md, "MyMod.json"))
    # idempotent re-register
    M.install(M.plan_install(df), data, cfg, "MyMod", md)
    assert open(cfg).read().count("content=MyMod.esp") == 1
    # reverse it
    res = M.uninstall(M.load_manifest(md, "MyMod"), cfg)
    assert res["files_removed"] == 4 and res["cfg_lines_removed"] == 2
    assert not os.path.exists(os.path.join(data, "MyMod.esp"))
    assert "content=MyMod.esp" not in open(cfg).read()
    assert "content=Morrowind.esm" in open(cfg).read()      # vanilla untouched


def test_preexisting_files_are_not_removed(tmp_path):
    df = _make_mod_tree(str(tmp_path / "mod"))
    data = str(tmp_path / "game"); os.makedirs(os.path.join(data, "Meshes", "sub"))
    open(os.path.join(data, "Meshes", "sub", "x.nif"), "wb").write(b"ORIGINAL")
    cfg = str(tmp_path / "cfg"); open(cfg, "w").write("")
    man = M.install(M.plan_install(df), data, cfg, "Mod", str(tmp_path / "m"))
    assert any(c["rel"].endswith("x.nif") and c["preexisting"] for c in man["files"])
    M.uninstall(man, cfg)
    assert os.path.exists(os.path.join(data, "Meshes", "sub", "x.nif"))   # kept


def test_dry_run_writes_nothing(tmp_path):
    df = _make_mod_tree(str(tmp_path / "mod"))
    data = str(tmp_path / "game"); os.makedirs(data)
    cfg = str(tmp_path / "cfg"); open(cfg, "w").write("")
    man = M.install(M.plan_install(df), data, cfg, "Mod", str(tmp_path / "m"), dry_run=True)
    assert man["new_file_count"] == 4
    assert not os.path.exists(os.path.join(data, "MyMod.esp"))
    assert open(cfg).read() == ""
    assert not os.path.exists(os.path.join(str(tmp_path / "m"), "Mod.json"))


def test_install_archive_zip(tmp_path):
    _make_mod_tree(str(tmp_path / "src"))
    zpath = str(tmp_path / "MyZipMod.zip")
    with zipfile.ZipFile(zpath, "w") as z:
        for dp, _, fs in os.walk(str(tmp_path / "src")):
            for f in fs:
                full = os.path.join(dp, f)
                z.write(full, os.path.relpath(full, str(tmp_path / "src")))
    data = str(tmp_path / "game"); os.makedirs(data)
    cfg = str(tmp_path / "cfg"); open(cfg, "w").write("")
    man = M.install_archive(zpath, data, cfg, str(tmp_path / "m"), include=["*.esp", "Meshes/*"])
    assert os.path.exists(os.path.join(data, "MyMod.esp"))
    assert os.path.exists(os.path.join(data, "Meshes", "sub", "x.nif"))
    assert not os.path.exists(os.path.join(data, "Textures", "y.tga"))   # excluded by include
    assert "content=MyMod.esp" in open(cfg).read()


def test_register_skyrim(tmp_path):
    pt = str(tmp_path / "plugins.txt")
    open(pt, "w").write("*Skyrim.esm\n*Update.esm\n")
    added = M.register_skyrim(pt, ["MyMod.esp", "MyMod2.esp"], ["MyMod.bsa"])
    assert added == ["*MyMod.esp", "*MyMod2.esp"]         # BSA not registered (Skyrim auto-loads)
    txt = open(pt).read()
    assert "*MyMod.esp" in txt and "*MyMod2.esp" in txt
    open(pt, "a").write("Inactive.esp\n")                 # present but inactive
    assert M.register_skyrim(pt, ["MyMod.esp", "Inactive.esp"], []) == []   # both already present


def test_install_skyrim_data_root_and_plugins_txt(tmp_path):
    root = str(tmp_path / "mod"); df = os.path.join(root, "Data")
    os.makedirs(os.path.join(df, "meshes"))
    open(os.path.join(df, "Cool.esp"), "wb").write(b"x")
    open(os.path.join(df, "meshes", "a.nif"), "wb").write(b"n")
    assert M.find_data_root(root) == df                   # recognises a "Data" root
    data = str(tmp_path / "game" / "Data"); os.makedirs(data)
    pt = str(tmp_path / "plugins.txt"); open(pt, "w").write("*Skyrim.esm\n")
    md = str(tmp_path / "m")
    man = M.install(M.plan_install(df), data, pt, "Cool", md, game="skyrim")
    assert man["game"] == "skyrim" and man["cfg_added"] == ["*Cool.esp"]
    assert os.path.exists(os.path.join(data, "Cool.esp"))
    assert "*Cool.esp" in open(pt).read()
    M.uninstall(M.load_manifest(md, "Cool"), pt)
    assert not os.path.exists(os.path.join(data, "Cool.esp"))
    assert "*Cool.esp" not in open(pt).read() and "*Skyrim.esm" in open(pt).read()


def test_mesh_texture_refs_normalises(tmp_path):
    mesh = str(tmp_path / "m.nif")
    open(mesh, "wb").write(b"\x00Em_kids\\aaa.dds\x00\x00Textures\\Em_kids\\bbb.tga\x00\x00ccc.bmp\x00")
    assert M.mesh_texture_refs(mesh) == {r"Em_kids\aaa.dds", r"Em_kids\bbb.tga", "ccc.bmp"}


def test_extract_bsa_for_meshes_stem_and_ext_sub(tmp_path):
    # archive has the texture as .dds in a subdir; mesh references it as .tga, bare
    files = {r"textures\warrhhair\foo.dds": b"DDS", r"textures\bar.dds": b"BAR"}
    bp = str(tmp_path / "mod.bsa"); open(bp, "wb").write(_build_bsa(files))
    mesh = str(tmp_path / "m.nif")
    open(mesh, "wb").write(b"\x00foo.tga\x00\x00Textures\\baz.bmp\x00")   # baz not in bsa
    data = str(tmp_path / "game"); md = str(tmp_path / "man")
    rep = M.extract_bsa_for_meshes(bp, data, "tex", md, [mesh])
    # foo.tga -> pulled warrhhair\foo.dds by STEM, placed at the mesh's ref path Textures\foo.dds
    assert open(os.path.join(data, "Textures", "foo.dds"), "rb").read() == b"DDS"
    assert rep["new_file_count"] == 1                                    # baz skipped (not in bsa)
    # now already resolvable (foo.dds present) -> nothing new
    assert M.extract_bsa_for_meshes(bp, data, "tex", md, [mesh])["new_file_count"] == 0


def test_texture_resolvable_ext_substitution(tmp_path):
    data = str(tmp_path / "game"); os.makedirs(os.path.join(data, "Textures", "sub"))
    open(os.path.join(data, "Textures", "sub", "x.dds"), "wb").write(b"d")
    assert M.texture_resolvable(data, r"sub\x.tga")          # .tga ref satisfied by .dds
    assert not M.texture_resolvable(data, r"sub\y.tga")
    assert M.texture_resolvable(data, "z.tga", vanilla_stems={"z"})


def test_extract_bsa_assets_surgical(tmp_path):
    files = {r"meshes\a.nif": b"AAA", r"meshes\b.nif": b"BBB", r"textures\t.tga": b"TTT"}
    bp = str(tmp_path / "mod.bsa"); open(bp, "wb").write(_build_bsa(files))
    data = str(tmp_path / "game")
    man = M.extract_bsa_assets(bp, data, "modbsa", str(tmp_path / "m"), patterns=[r"meshes\*"])
    assert os.path.exists(os.path.join(data, "meshes", "a.nif"))
    assert not os.path.exists(os.path.join(data, "textures", "t.tga"))
    assert man["new_file_count"] == 2
