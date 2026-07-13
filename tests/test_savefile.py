"""Tests for chim's save-file (.ess) engine: container + Papyrus heap + cleaning.

Two layers:

* **Synthetic** — a hand-built minimal (uncompressed) save wrapping a *complete*
  Papyrus heap (string table, one real + one stub script, three instances, empty
  Reference/Array/ActiveScript maps, two instance data blocks, an opaque tail).
  Portable/CI-safe; exercises whole-block round-trip and the remove-undefined /
  remove-unattached cleaning.
* **Real save** — gated on ``.scratch/save.ess`` (a real 1.6.1170 save, not
  committed). Asserts byte-exact container + heap round-trip, that a known
  removed-mod orphan (``mff_refalias``) is surfaced, and that cleaning removes it
  while the file still re-parses.
"""

import os
import struct

import pytest

from chim.save import ess, analysis
from chim.save.heap import PapyrusHeap

REAL_SAVE = os.path.join(os.path.dirname(__file__), os.pardir, ".scratch", "save.ess")


def _wstr(s: bytes) -> bytes:
    return struct.pack("<H", len(s)) + s


def _build_heap() -> bytes:
    """A complete type-1001 block: 1 defined + 1 stub script; 3 instances
    (attached-defined, attached-undefined, unattached); 2 data blocks; empty
    Reference/Array/ActiveScript maps; an opaque tail (non-instance EID)."""
    strings = [b"", b"Quest", b"goodscript", b"deadmod_script"]
    w = bytearray()
    w += struct.pack("<H", 7)                          # header
    w += struct.pack("<I", len(strings))               # string table
    for s in strings:
        w += _wstr(s)
    scripts = [(2, 1, []), (3, 0, [])]                 # goodscript/Quest, deadmod/"" (stub)
    w += struct.pack("<I", len(scripts))
    for ni, ti, mem in scripts:
        w += struct.pack("<IIi", ni, ti, len(mem))
    insts = [                                           # (eid, name, u2, unk, refid, ub)
        (1, 2, 0, 0, 0x10, 0),                          # goodscript, attached
        (2, 3, 0, 0, 0x20, 0),                          # deadmod_script, attached -> undefined
        (3, 2, 0, 0, 0x00, 0),                          # goodscript, refid 0 -> unattached
    ]
    w += struct.pack("<I", len(insts))
    for eid, ni, u2, unk, refid, ub in insts:
        w += struct.pack("<QIHH", eid, ni, u2, unk)
        w += bytes(((refid >> 16) & 0xFF, (refid >> 8) & 0xFF, refid & 0xFF), )
        w += bytes((ub,))
    # mid: empty ReferenceMap, ArrayMap, papyrusRuntime EID, ActiveScriptMap, gap
    w += struct.pack("<I", 0)                           # nref
    w += struct.pack("<I", 0)                           # narr
    w += struct.pack("<Q", 0)                           # papyrusRuntime
    w += struct.pack("<I", 0)                           # nact
    w += struct.pack("<I", 0)                           # gap
    # instance data blocks (eid 1 and 2 have data; eid 3 does not)
    def datablock(eid):
        return struct.pack("<Q", eid) + bytes((0,)) + struct.pack("<Iii", 0, 0, 0)
    w += datablock(1)
    w += datablock(2)
    # tail begins at the first non-instance EID (mimics the Reference data section)
    w += struct.pack("<Q", 0xDEADBEEF) + b"\x01\x02\x03\x04"
    return bytes(w)


def _build_save() -> bytes:
    """A minimal uncompressed .ess wrapping ``_build_heap()`` as GlobalData 1001."""
    heap = _build_heap()
    gd = struct.pack("<II", ess.PAPYRUS_TYPE, len(heap)) + heap

    body = bytearray()
    body.append(78)                                     # formVersion
    pi = struct.pack("<B", 0) + struct.pack("<H", 0)    # 0 full, 0 light plugins
    body += struct.pack("<I", len(pi)) + pi
    body += struct.pack("<6I", 0, 0, 0, 0, 0, 0)        # FLT offsets (rebuilt on serialize)
    body += struct.pack("<4I", 0, 0, 0, 0)              # t1/t2/t3/changeForm counts
    body += struct.pack("<15I", *([0] * 15))            # 15 unused
    body += gd                                          # table3: t3count+1 = 1 block
    body += struct.pack("<I", 0)                        # formIDArray count
    body += struct.pack("<I", 0)                        # visitedWorldspace count

    fields = struct.pack("<I", 1)
    fields += _wstr(b"tester")
    fields += struct.pack("<I", 5)
    fields += _wstr(b"Test Cell") + _wstr(b"1.0.0") + _wstr(b"NordRace")
    fields += struct.pack("<H", 0)
    fields += struct.pack("<ff", 0.0, 0.0)
    fields += struct.pack("<Q", 0)
    fields += struct.pack("<II", 0, 0)                  # screenshot w, h (0 -> none)
    header_size = 4 + len(fields) + 2

    out = bytearray(b"TESV_SAVEGAME")
    out += struct.pack("<I", header_size)
    out += struct.pack("<I", 12)
    out += fields
    out += struct.pack("<H", ess.COMP_NONE)
    out += bytes(body)
    return bytes(out)


# --------------------------------------------------------------------------- #
# Synthetic
# --------------------------------------------------------------------------- #

def test_heap_roundtrips_byte_exact():
    block = _build_heap()
    h = PapyrusHeap(block)
    assert len(h.scripts) == 2
    assert len(h.instances) == 3
    assert len(h.inst_data) == 2          # eid 3 has no data block
    assert h.to_bytes() == block
    assert h.roundtrips()


def test_heap_remove_undefined():
    h = PapyrusHeap(_build_heap())
    assert h.undefined_script_names() == {"deadmod_script"}
    res = h.remove_undefined()
    assert res == {"instances_removed": 1, "data_blocks_removed": 1, "stub_defs_removed": 1}
    assert len(h.instances) == 2
    assert not any(h.is_instance_undefined(i) for i in h.instances)
    # cleaned heap still round-trips through a fresh parse
    assert PapyrusHeap(h.to_bytes()).roundtrips()


def test_heap_remove_unattached():
    h = PapyrusHeap(_build_heap())
    assert sum(i.is_unattached() for i in h.instances) == 1
    res = h.remove_unattached()
    assert res["instances_removed"] == 1
    assert res["data_blocks_removed"] == 0   # the unattached one had no data block
    assert not any(i.is_unattached() for i in h.instances)
    assert PapyrusHeap(h.to_bytes()).roundtrips()


def test_container_serialize_is_stable_and_clean():
    raw = _build_save()
    sf = ess.parse_save(raw)
    assert sf.header.version == 12 and sf.form_version == 78 and sf.papyrus() is not None
    out = sf.serialize()                     # rebuilds the FLT to canonical offsets
    sf2 = ess.parse_save(out)                # must re-parse cleanly
    assert sf2.serialize() == out            # serialize is a fixed point
    # analysis over the synthetic save
    c = analysis.count_orphans(raw)
    assert c["undefined_script_definitions"] == 1
    assert c["undefined_instances"] == 1
    assert c["unattached_instances"] == 1


def test_container_clean_reparses():
    raw = _build_save()
    sf = ess.parse_save(raw)
    h = PapyrusHeap(sf.papyrus().data)
    h.remove_undefined()
    sf.papyrus().heap = h
    cleaned = sf.serialize()
    sf2 = ess.parse_save(cleaned)            # cleaned FILE re-parses end to end
    assert sf2.body_roundtrips()
    c = analysis.count_orphans(cleaned)
    assert c["undefined_instances"] == 0
    assert c["script_instances"] == 2


# --------------------------------------------------------------------------- #
# Real save (ground truth) — skipped when the scratch file is absent
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not os.path.exists(REAL_SAVE), reason="no real .ess in .scratch/")
def test_real_save_full_roundtrip_and_clean():
    raw = open(REAL_SAVE, "rb").read()
    sf = ess.parse_save(raw)
    assert sf.header.version == 12
    assert sf.body_roundtrips()                       # 25 MB body, byte-exact
    assert sf.serialize() == raw                      # whole file byte-exact (incl. LZ4)
    heap = PapyrusHeap(sf.papyrus().data)
    assert heap.roundtrips()                          # 11 MB heap, byte-exact whole-block
    lu = analysis.list_undefined(raw)
    assert "mff_refalias" in {r["script"].lower() for r in lu["undefined"]}
    # clean it and confirm the orphan is gone and the file re-parses
    heap.remove_undefined()
    sf.papyrus().heap = heap
    cleaned = sf.serialize()
    assert len(cleaned) < len(raw)
    ch = PapyrusHeap(ess.parse_save(cleaned).papyrus().data)   # parse the cleaned heap ONCE
    assert ch.roundtrips()
    assert not any(ch.string(i.name_idx).lower() == "mff_refalias" for i in ch.instances)
