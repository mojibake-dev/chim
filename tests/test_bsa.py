"""Tests for the Morrowind TES3 BSA reader (:mod:`chim.tes3.bsa`).

Synthetic: hand-build a minimal valid uncompressed BSA (zeroed hash table — the
reader indexes by name and never needs it) and assert list / read / find /
extract behave, both from bytes and from a real file on disk.
"""

import os
import struct

import pytest

from chim.tes3.bsa import Bsa, BsaError, BSA_VERSION


def _build_bsa(files: dict) -> bytes:
    """Build a minimal TES3 BSA from ``{internal_name: data}``."""
    names = list(files)
    count = len(names)
    name_blobs = [n.replace("/", "\\").encode("cp1252") + b"\x00" for n in names]
    # name offsets into the name table
    noffs = []
    acc = 0
    for nb in name_blobs:
        noffs.append(acc)
        acc += len(nb)
    name_table = b"".join(name_blobs)
    # data block: concatenate file contents; record (size, offset)
    recs = []
    data = bytearray()
    for n in names:
        recs.append((len(files[n]), len(data)))
        data += files[n]
    hash_off = 8 * count + 4 * count + len(name_table)   # records + name offsets + name table
    out = bytearray()
    out += struct.pack("<III", BSA_VERSION, hash_off, count)
    for size, off in recs:
        out += struct.pack("<II", size, off)
    for no in noffs:
        out += struct.pack("<I", no)
    out += name_table
    out += b"\x00" * (8 * count)          # hash table (zeroed; reader ignores it)
    out += bytes(data)
    return bytes(out)


FILES = {
    r"meshes\em_kids\1em_child_de_3.nif": b"NIFDATA-de3" * 4,
    r"meshes\em_kids\1em_child_we2.nif": b"NIFDATA-we2" * 3,
    r"textures\_war_corean_hr_01.tga": b"TGA-corean",
    r"icons\em_kids\1em_a_duck.tga": b"icon",
}


def test_parse_from_bytes_and_list():
    b = Bsa(data=_build_bsa(FILES))
    assert len(b) == 4
    assert set(n.lower() for n in b.names()) == set(FILES)
    assert r"meshes\em_kids\1em_child_de_3.nif" in b
    assert "MESHES/EM_KIDS/1EM_CHILD_WE2.NIF".replace("/", "\\").lower() in {n.lower() for n in b.names()}


def test_read_byte_exact():
    b = Bsa(data=_build_bsa(FILES))
    for name, payload in FILES.items():
        assert b.read(name) == payload
    assert b.read(r"MESHES\EM_KIDS\1EM_CHILD_DE_3.NIF") == FILES[r"meshes\em_kids\1em_child_de_3.nif"]
    with pytest.raises(KeyError):
        b.read("nope.nif")


def test_from_file_seeks(tmp_path):
    p = tmp_path / "test.bsa"
    p.write_bytes(_build_bsa(FILES))
    b = Bsa(path=str(p))                              # index-only up front, seek per read
    assert len(b) == 4
    assert b.read(r"textures\_war_corean_hr_01.tga") == b"TGA-corean"


def test_find_globs():
    b = Bsa(data=_build_bsa(FILES))
    assert set(b.find([r"meshes\em_kids\*"])) == {
        r"meshes\em_kids\1em_child_de_3.nif", r"meshes\em_kids\1em_child_we2.nif"}
    assert b.find([r"textures\*corean*"]) == [r"textures\_war_corean_hr_01.tga"]


def test_extract_matching_preserves_paths(tmp_path):
    b = Bsa(data=_build_bsa(FILES))
    got = b.extract_matching(str(tmp_path), patterns=[r"meshes\*", r"textures\*"])
    assert len(got) == 3                              # 2 meshes + 1 texture, not the icon
    assert (tmp_path / "meshes" / "em_kids" / "1em_child_de_3.nif").read_bytes() \
        == FILES[r"meshes\em_kids\1em_child_de_3.nif"]
    assert (tmp_path / "textures" / "_war_corean_hr_01.tga").read_bytes() == b"TGA-corean"
    assert not (tmp_path / "icons").exists()


def test_extract_matching_by_names(tmp_path):
    b = Bsa(data=_build_bsa(FILES))
    got = b.extract_matching(str(tmp_path), names=[r"meshes\em_kids\1em_child_we2.nif"])
    assert [n.lower() for n, _ in got] == [r"meshes\em_kids\1em_child_we2.nif"]


def test_rejects_bad_version():
    bad = struct.pack("<III", 0xDEAD, 0, 0)
    with pytest.raises(BsaError):
        Bsa(data=bad)
