"""Tests for chim.assets -- the plugin->mesh->texture closure resolver."""
import struct

from chim import assets
from chim.tes3 import container, ops, serialize


def _mk_plugin(path, modl_refs=(), icon_refs=(), ltex_refs=()):
    hedr = struct.pack("<fI", 1.3, 0) + b"\x00" * 32 + b"\x00" * 256 + struct.pack("<I", 0)
    header = container.Record("TES3", 0, 0, [container.Subrecord("HEDR", hedr)])
    plug = container.Plugin(raw=b"", header=header, records=[])
    for i, m in enumerate(modl_refs):
        ops.add_record(plug, ops.build_record("STAT", [
            ("NAME", ("stat_%d" % i).encode() + b"\x00"), ("MODL", m.encode() + b"\x00")]))
    for i, ic in enumerate(icon_refs):
        ops.add_record(plug, ops.build_record("MISC", [
            ("NAME", ("misc_%d" % i).encode() + b"\x00"), ("ICON", ic.encode() + b"\x00")]))
    for i, lt in enumerate(ltex_refs):
        ops.add_record(plug, ops.build_record("LTEX", [
            ("NAME", ("ltex_%d" % i).encode() + b"\x00"), ("DATA", lt.encode() + b"\x00")]))
    open(path, "wb").write(serialize(plug))


def test_resolve_closure(tmp_path):
    d = tmp_path / "data"
    (d / "meshes" / "x").mkdir(parents=True)
    (d / "textures" / "a").mkdir(parents=True)
    (d / "icons" / "n").mkdir(parents=True)
    # a mesh referencing textures\a\b.dds (isolated by non-printable bytes, as a real
    # NIF's length-prefixed strings are); plus loose files
    (d / "meshes" / "x" / "test.nif").write_bytes(b"NIF\x00\x08textures\\a\\b.dds\x00tail")
    (d / "textures" / "a" / "b.dds").write_bytes(b"DDS")
    (d / "textures" / "a" / "land.tga").write_bytes(b"TGA")   # ltex ref by .dds -> resolves to .tga
    (d / "icons" / "n" / "key.tga").write_bytes(b"ICON")
    (d / "orphan.dds").write_bytes(b"nobody references me")   # must NOT be in the closure
    (d / "meshes" / "orphan.nif").write_bytes(b"orphan mesh")

    p = tmp_path / "mod.esp"
    _mk_plugin(str(p), modl_refs=["x\\test.nif", "x\\gone.nif"],
               icon_refs=["n\\key.tga"], ltex_refs=["a\\land.dds"])
    r = assets.resolve_closure([str(p)], [str(d)])

    names = {f.rsplit("/", 1)[-1] for f in r["used_files"]}
    assert {"mod.esp", "test.nif", "b.dds", "key.tga", "land.tga"} <= names
    assert "orphan.dds" not in names and "orphan.nif" not in names   # orphans excluded
    assert "x\\gone.nif" in r["missing"]["meshes"]                    # gap reported
    assert r["stats"]["used_files"] == 5


def test_resolve_modlist_via_cfg(tmp_path):
    d = tmp_path / "data"
    (d / "meshes").mkdir(parents=True)
    (d / "textures" / "t").mkdir(parents=True)
    (d / "meshes" / "keep.nif").write_bytes(b"mesh\x00textures\\t\\x.dds\x00")
    (d / "textures" / "t" / "x.dds").write_bytes(b"DDS")
    (d / "orphan.dds").write_bytes(b"orphan")
    _mk_plugin(str(d / "Mod.esp"), modl_refs=["keep.nif"])
    cfg = tmp_path / "openmw.cfg"
    cfg.write_text('data="%s"\ncontent=Mod.esp\ncontent=Missing.esp\n' % d, encoding="utf-8")
    r = assets.resolve_modlist(cfg=str(cfg))
    assert r["plugins_resolved"] == 1 and r["plugins_missing"] == ["Missing.esp"]
    names = {f.rsplit("/", 1)[-1] for f in r["used_files"]}
    assert {"Mod.esp", "keep.nif", "x.dds"} <= names and "orphan.dds" not in names


def test_cli_main(tmp_path):
    d = tmp_path / "data"
    (d / "meshes").mkdir(parents=True)
    (d / "meshes" / "k.nif").write_bytes(b"mesh")
    _mk_plugin(str(d / "M.esp"), modl_refs=["k.nif"])
    cfg = tmp_path / "openmw.cfg"
    cfg.write_text('data="%s"\ncontent=M.esp\n' % d, encoding="utf-8")
    closure = tmp_path / "closure.txt"
    prog = tmp_path / "prog.txt"
    assets.main(["--cfg", str(cfg), "--out-closure", str(closure), "--progress", str(prog)])
    lines = closure.read_text().splitlines()
    assert any(l.endswith("k.nif") for l in lines) and any(l.endswith("M.esp") for l in lines)
    assert "COMPLETE" in prog.read_text()


def test_materialize_closure(tmp_path):
    d = tmp_path / "data"
    (d / "meshes").mkdir(parents=True)
    (d / "meshes" / "keep.nif").write_bytes(b"mesh")
    (d / "unused.dds").write_bytes(b"orphan")
    p = tmp_path / "m.esp"
    _mk_plugin(str(p), modl_refs=["keep.nif"])
    r = assets.resolve_closure([str(p)], [str(d)])
    dest = tmp_path / "clean"
    rep = assets.materialize_closure(r, [str(d)], str(dest))
    assert (dest / "meshes" / "keep.nif").exists()
    assert (dest / "m.esp").exists()
    assert not (dest / "unused.dds").exists()   # clean: orphan didn't come along
    assert rep["copied"] == 2
