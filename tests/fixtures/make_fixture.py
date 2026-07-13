#!/usr/bin/env python3
"""Generate a minimal-but-valid synthetic Skyrim SE plugin: sample.esp.

Structure produced (all little-endian, formIDs use plugin index 0x00 for a
pretend master-count of 0 so self-refs would use index 0x00... but this file
has no masters, so its own index is 0 and we keep everything under 0x00):

    TES4  (HEDR: version, numRecords, nextObjectID; + CNAM author, empty MAST)
    GRUP top 'STAT'
        STAT  (EDID + OBND + MODL)
    GRUP top 'CELL'
        GRUP interior-block (type 2)
            GRUP interior-subblock (type 3)
                CELL  (EDID)
                GRUP cell-children (type 6, label = CELL formID)
                    GRUP cell-persistent-children (type 8, label = CELL formID)
                        REFR  (NAME + DATA pos/rot)

The file walks clean: parse -> serialize is byte identical and every GRUP's
size consumes exactly its children.

Run directly to (re)write ``sample.esp`` next to this script.
"""

from __future__ import annotations

import os
import struct

# --- header sizes ---------------------------------------------------------- #
REC_HDR = 24
GRP_HDR = 24

# --- flags ----------------------------------------------------------------- #
FLAG_PERSISTENT = 0x00000400

# --- group types ----------------------------------------------------------- #
GT_TOP = 0
GT_INTERIOR_BLOCK = 2
GT_INTERIOR_SUBBLOCK = 3
GT_CELL_CHILDREN = 6
GT_CELL_PERSISTENT_CHILDREN = 8

# fixed header meta bytes shared by everything (timestamp+vc, iversion, unk).
_TS_VC = 0x00000000
_IVERSION = 0x002C
_UNK16 = 0x0000

# formIDs for our objects (plugin index 0x00).
FID_STAT = 0x00000800
FID_CELL = 0x00000801
FID_REFR = 0x00000802
NEXT_OBJECT_ID = 0x00000803


def subrecord(sig: bytes, payload: bytes) -> bytes:
    """A field: sig(4) + uint16 size + payload. (No field here exceeds 0xFFFF
    so we never need XXXX in the fixture.)"""
    assert len(sig) == 4
    assert len(payload) <= 0xFFFF
    return sig + struct.pack("<H", len(payload)) + payload


def zstring(s: str) -> bytes:
    return s.encode("ascii") + b"\x00"


def record(sig: bytes, form_id: int, data: bytes, flags: int = 0) -> bytes:
    """A record: 24-byte header + data. dataSize is derived from ``data``."""
    assert len(sig) == 4
    header = sig + struct.pack(
        "<IIIIHH",
        len(data),      # dataSize
        flags,          # flags
        form_id,        # formID
        _TS_VC,         # timestamp + version control
        _IVERSION,      # internal version
        _UNK16,         # unknown
    )
    return header + data


def group(label: bytes, group_type: int, children: bytes) -> bytes:
    """A GRUP: 24-byte header + children. groupSize INCLUDES the header."""
    assert len(label) == 4
    group_size = GRP_HDR + len(children)
    header = b"GRUP" + struct.pack(
        "<I4siIH",
        group_size,     # groupSize (includes 24-byte header)
        label,          # label
        group_type,     # groupType (int32)
        _TS_VC,         # timestamp + version control
        _UNK16,         # unknown (16 bits...)
    ) + struct.pack("<H", 0)  # ...+ trailing 2 unknown bytes = 24 total
    return header + children


def build_tes4() -> bytes:
    # HEDR: float32 version, int32 numRecords, uint32 nextObjectID.
    # numRecords = count of records + groups EXCLUDING TES4. We fill it in
    # after the fact by counting; for a fixed fixture we hardcode the count.
    #   top GRUP 'STAT'              (1)
    #     STAT                       (2)
    #   top GRUP 'CELL'              (3)
    #     GRUP interior-block        (4)
    #       GRUP interior-subblock   (5)
    #         CELL                   (6)
    #         GRUP cell-children     (7)
    #           GRUP persistent      (8)
    #             REFR               (9)
    num_records = 9
    hedr = struct.pack("<fiI", 1.71, num_records, NEXT_OBJECT_ID)
    data = (
        subrecord(b"HEDR", hedr)
        + subrecord(b"CNAM", zstring("chim"))
        + subrecord(b"INTV", struct.pack("<I", 1))
    )
    return record(b"TES4", 0x00000000, data)


def build_stat_group() -> bytes:
    obnd = struct.pack("<6h", -16, -16, 0, 16, 16, 32)
    stat_data = (
        subrecord(b"EDID", zstring("ChimTestStatic"))
        + subrecord(b"OBND", obnd)
        + subrecord(b"MODL", zstring("chim\\teststatic.nif"))
    )
    stat = record(b"STAT", FID_STAT, stat_data)
    return group(b"STAT", GT_TOP, stat)


def build_cell_group() -> bytes:
    # Innermost: the persistent REFR placing the STAT.
    refr_data = (
        subrecord(b"NAME", struct.pack("<I", FID_STAT))
        + subrecord(
            b"DATA",
            struct.pack("<6f", 100.0, 200.0, 300.0, 0.0, 0.0, 1.5708),
        )
    )
    refr = record(b"REFR", FID_REFR, refr_data, flags=FLAG_PERSISTENT)

    # type 8 persistent-children GRUP, label = parent CELL formID.
    persistent = group(
        struct.pack("<I", FID_CELL), GT_CELL_PERSISTENT_CHILDREN, refr
    )

    # type 6 cell-children GRUP wraps the persistent group, label = CELL formID.
    cell_children = group(
        struct.pack("<I", FID_CELL), GT_CELL_CHILDREN, persistent
    )

    # The CELL record itself (EDID only, minimal).
    cell = record(b"CELL", FID_CELL, subrecord(b"EDID", zstring("ChimTestCell")))

    # interior sub-block (type 3), label = block number (0). Contains the CELL
    # record immediately followed by its children GRUP.
    subblock = group(
        struct.pack("<i", 0), GT_INTERIOR_SUBBLOCK, cell + cell_children
    )

    # interior block (type 2), label = block number (0).
    block = group(struct.pack("<i", 0), GT_INTERIOR_BLOCK, subblock)

    # top CELL group.
    return group(b"CELL", GT_TOP, block)


def build_plugin() -> bytes:
    return build_tes4() + build_stat_group() + build_cell_group()


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "sample.esp")
    data = build_plugin()
    with open(out, "wb") as fh:
        fh.write(data)
    print(f"wrote {out} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
