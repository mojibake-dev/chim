"""Skyrim SE ``.ess`` save container: parse / round-trip / re-serialize.

The file is an **uncompressed prefix** (13-byte magic ``TESV_SAVEGAME``,
``headerSize`` u32, the header struct, a u16 compression type, then the raw
screenshot) followed by a **compressed body**. The body is walked *sequentially*
into its sections; each is kept as a raw byte slice except the Papyrus VM heap
(``GlobalData`` type 1001), which :mod:`chim.save.heap` decodes.

The engine keeps the whole thing round-trippable: for an unmodified save,
``serialize_body()`` reproduces the decompressed body **byte-for-byte** (the
save-side analogue of the ESP ``walk_clean`` invariant). The compressed stream is
*not* byte-identical after a re-encode — the game only decompresses on load, so a
spec-compliant LZ4 block that inflates to the correct bytes is what matters.

Byte layout grounded in ReSaver / FallrimTools (Apache-2.0) source; validated
against real 1.6.1170 saves. All integers little-endian except the 3-byte RefID
(big-endian packed).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Optional

MAGIC = b"TESV_SAVEGAME"

# Compression type (u16 right before the screenshot; Skyrim SE only).
COMP_NONE = 0
COMP_ZLIB = 1
COMP_LZ4 = 2

#: GlobalData block type that holds the Papyrus VM heap.
PAPYRUS_TYPE = 1001

#: Screenshot bytes-per-pixel: Skyrim SE / FO4 = RGBA (4); Skyrim LE = RGB (3).
SSE_BYPP = 4


class ParseError(Exception):
    """The bytes do not parse as a Skyrim SE save (or a section misaligned)."""


class Reader:
    """Little-endian cursor over a ``bytes`` buffer."""

    __slots__ = ("d", "o")

    def __init__(self, data: bytes, off: int = 0):
        self.d = data
        self.o = off

    def eof(self) -> bool:
        return self.o >= len(self.d)

    def remaining(self) -> int:
        return len(self.d) - self.o

    def read(self, n: int) -> bytes:
        if self.o + n > len(self.d):
            raise ParseError(f"read {n} at {self.o} exceeds buffer {len(self.d)}")
        v = self.d[self.o:self.o + n]
        self.o += n
        return v

    def u8(self) -> int:
        v = self.d[self.o]
        self.o += 1
        return v

    def u16(self) -> int:
        v = struct.unpack_from("<H", self.d, self.o)[0]
        self.o += 2
        return v

    def u32(self) -> int:
        v = struct.unpack_from("<I", self.d, self.o)[0]
        self.o += 4
        return v

    def i32(self) -> int:
        v = struct.unpack_from("<i", self.d, self.o)[0]
        self.o += 4
        return v

    def u64(self) -> int:
        v = struct.unpack_from("<Q", self.d, self.o)[0]
        self.o += 8
        return v

    def f32(self) -> float:
        v = struct.unpack_from("<f", self.d, self.o)[0]
        self.o += 4
        return v

    def wstr(self) -> bytes:
        """A ``wstring``: u16 length prefix + that many raw bytes (no NUL)."""
        n = self.u16()
        return self.read(n)


def w_wstr(raw: bytes) -> bytes:
    return struct.pack("<H", len(raw)) + raw


def _decompress(comp: int, block: bytes, uncompressed_len: int) -> bytes:
    if comp == COMP_LZ4:
        import lz4.block
        return lz4.block.decompress(block, uncompressed_size=uncompressed_len)
    if comp == COMP_ZLIB:
        import zlib
        return zlib.decompress(block)
    return block


def _compress(comp: int, body: bytes) -> bytes:
    if comp == COMP_LZ4:
        import lz4.block
        # store_size=False → a bare, spec-standard raw LZ4 block (the game/ReSaver
        # format), not python-lz4's size-prefixed default.
        return lz4.block.compress(body, store_size=False)
    if comp == COMP_ZLIB:
        import zlib
        return zlib.compress(body, 9)
    return body


@dataclass
class GlobalData:
    """One ``[type u32][length u32][data]`` block from a global-data table.

    ``data`` is kept as raw bytes. For the Papyrus block (type 1001) a decoded
    :class:`~chim.save.heap.PapyrusHeap` may be attached as ``heap``; when
    present it is the source of truth on serialize.
    """

    type: int
    data: bytes
    heap: object = None  # Optional[PapyrusHeap], set lazily by callers

    def payload(self) -> bytes:
        if self.heap is not None:
            return self.heap.to_bytes()
        return self.data

    def to_bytes(self) -> bytes:
        p = self.payload()
        return struct.pack("<II", self.type, len(p)) + p


@dataclass
class SaveHeader:
    header_size: int
    version: int
    save_number: int
    player_name: bytes
    player_level: int
    player_location: bytes
    game_date: bytes
    player_race: bytes
    player_sex: int
    player_cur_xp: float
    player_lvl_xp: float
    filetime: int
    shot_width: int
    shot_height: int
    compression: int


@dataclass
class SaveFile:
    """A parsed Skyrim SE save. Sections kept raw except the Papyrus heap."""

    raw: bytes
    header: SaveHeader
    prefix: bytes            # magic..screenshot (verbatim, incl. screenshot)
    uncompressed_len: int
    compressed_len: int
    body: bytes              # the decompressed body, verbatim

    form_version: int = 0
    plugin_info_size: int = 0
    full_plugins: List[bytes] = field(default_factory=list)
    light_plugins: List[bytes] = field(default_factory=list)
    flt_raw: bytes = b""
    table1_count: int = 0
    table2_count: int = 0
    table3_count: int = 0     # as stored (Skyrim: actual table-3 blocks = +1)
    change_form_count: int = 0
    table1: List[GlobalData] = field(default_factory=list)
    table2: List[GlobalData] = field(default_factory=list)
    table3: List[GlobalData] = field(default_factory=list)
    change_forms_raw: bytes = b""
    form_ids: bytes = b""     # raw: u32 count + count*u32
    worldspaces: bytes = b""  # raw: u32 count + count*u32
    trailing: bytes = b""

    # ---- convenience ----

    @property
    def has_cosave_flag(self) -> bool:  # informational only; cosave is a sibling file
        return False

    def global_data(self, type_: int) -> Optional[GlobalData]:
        for tbl in (self.table1, self.table2, self.table3):
            for g in tbl:
                if g.type == type_:
                    return g
        return None

    def papyrus(self) -> Optional[GlobalData]:
        return self.global_data(PAPYRUS_TYPE)

    def global_data_types(self) -> List[int]:
        out = []
        for tbl in (self.table1, self.table2, self.table3):
            out.extend(g.type for g in tbl)
        return out

    # ---- serialize ----

    def _rebuild_flt(self) -> None:
        """Recompute the 6 FileLocationTable offsets from current section sizes.

        Offsets are absolute *as-if-uncompressed* file offsets = body position +
        ``len(prefix)`` (uniformly). Counts + the 15 unused ints are preserved.
        Idempotent on an unmodified save (reproduces the stored offsets exactly).
        """
        base = len(self.prefix)
        p = 1                                   # formVersion
        p += 4 + self.plugin_info_size          # plugin info
        p += 100                                # FLT
        t1 = p
        for g in self.table1:
            p += len(g.to_bytes())
        t2 = p
        for g in self.table2:
            p += len(g.to_bytes())
        cf = p
        p += len(self.change_forms_raw)
        t3 = p
        for g in self.table3:
            p += len(g.to_bytes())
        fid = p
        p += len(self.form_ids)
        p += len(self.worldspaces)
        ut3 = p
        offsets = struct.pack(
            "<6I", base + fid, base + ut3, base + t1, base + t2, base + cf, base + t3
        )
        self.flt_raw = offsets + self.flt_raw[24:]

    def serialize_body(self) -> bytes:
        self._rebuild_flt()
        w = bytearray()
        w.append(self.form_version)
        # plugin info
        pi = bytearray()
        pi.append(len(self.full_plugins) & 0xFF)
        for p in self.full_plugins:
            pi += w_wstr(p)
        # light block present iff it was on parse (plugin_info_size accounts for it)
        if self.plugin_info_size > len(pi):
            pi += struct.pack("<H", len(self.light_plugins))
            for p in self.light_plugins:
                pi += w_wstr(p)
        w += struct.pack("<I", len(pi))
        w += pi
        w += self.flt_raw
        for g in self.table1:
            w += g.to_bytes()
        for g in self.table2:
            w += g.to_bytes()
        w += self.change_forms_raw
        for g in self.table3:
            w += g.to_bytes()
        w += self.form_ids
        w += self.worldspaces
        w += self.trailing
        return bytes(w)

    def serialize(self) -> bytes:
        """Re-emit the whole file (prefix verbatim + recompressed body)."""
        body = self.serialize_body()
        comp = self.header.compression
        if comp == COMP_NONE:
            return self.prefix + body
        blob = _compress(comp, body)
        return self.prefix + struct.pack("<II", len(body), len(blob)) + blob

    def body_roundtrips(self) -> bool:
        """True iff the parsed body re-serializes byte-identical (walk-clean)."""
        return self.serialize_body() == self.body


def parse_save(raw: bytes) -> SaveFile:
    """Parse Skyrim SE ``.ess`` bytes into a :class:`SaveFile` (read-only)."""
    r = Reader(raw)
    if r.read(13) != MAGIC:
        raise ParseError("not a TESV_SAVEGAME save (bad magic)")
    header_size = r.u32()
    version = r.u32()
    if version < 12:
        raise ParseError(f"save version {version} is Skyrim LE; only SE (>=12) supported")
    save_number = r.u32()
    player_name = r.wstr()
    player_level = r.u32()
    player_location = r.wstr()
    game_date = r.wstr()
    player_race = r.wstr()
    player_sex = r.u16()
    cur_xp = r.f32()
    lvl_xp = r.f32()
    filetime = r.u64()
    shot_w = r.u32()
    shot_h = r.u32()
    compression = r.u16()
    # Cross-check: screenshot begins at 17 + headerSize (magic 13 + size 4).
    if r.o != 17 + header_size:
        raise ParseError(
            f"header ended at {r.o}, expected {17 + header_size} (headerSize={header_size})"
        )
    r.read(SSE_BYPP * shot_w * shot_h)  # skip screenshot
    prefix = raw[:r.o]

    hdr = SaveHeader(
        header_size, version, save_number, player_name, player_level,
        player_location, game_date, player_race, player_sex, cur_xp, lvl_xp,
        filetime, shot_w, shot_h, compression,
    )

    if compression == COMP_NONE:
        uncompressed_len = compressed_len = 0
        body = raw[r.o:]
    else:
        uncompressed_len = r.u32()
        compressed_len = r.u32()
        block = r.read(compressed_len)
        body = _decompress(compression, block, uncompressed_len)
        if len(body) != uncompressed_len:
            raise ParseError(
                f"decompressed {len(body)} != declared {uncompressed_len}"
            )

    sf = SaveFile(
        raw=raw, header=hdr, prefix=prefix,
        uncompressed_len=uncompressed_len, compressed_len=compressed_len, body=body,
    )
    _parse_body(sf, body)
    return sf


def _read_global_table(b: Reader, count: int) -> List[GlobalData]:
    out = []
    for _ in range(count):
        typ = b.u32()
        length = b.u32()
        out.append(GlobalData(typ, b.read(length)))
    return out


def _skip_change_forms(b: Reader, count: int) -> bytes:
    """Walk ``count`` change forms and return the raw region spanning them."""
    start = b.o
    for _ in range(count):
        b.read(3)          # RefID
        b.u32()            # changeFlags
        type_field = b.u8()
        b.u8()             # version
        w = type_field >> 6
        if w == 0:
            length1 = b.u8(); b.u8()
        elif w == 1:
            length1 = b.u16(); b.u16()
        elif w == 2:
            length1 = b.u32(); b.u32()
        else:
            raise ParseError(f"change form length-width {w} unsupported")
        b.read(length1)    # (length2>0 → data is zlib-compressed; opaque to us)
    return b.d[start:b.o]


def _parse_body(sf: SaveFile, body: bytes) -> None:
    b = Reader(body)
    sf.form_version = b.u8()

    pi_size = b.u32()
    pi_start = b.o
    n_full = b.u8()
    sf.full_plugins = [b.wstr() for _ in range(n_full)]
    if b.o - pi_start < pi_size:
        n_light = b.u16()
        sf.light_plugins = [b.wstr() for _ in range(n_light)]
    if b.o - pi_start != pi_size:
        raise ParseError(
            f"plugin info consumed {b.o - pi_start} != declared {pi_size}"
        )
    sf.plugin_info_size = pi_size

    # File location table: 6 offsets + 3 table counts + changeFormCount + 15 unused
    flt_start = b.o
    _offsets = [b.u32() for _ in range(6)]
    sf.table1_count = b.u32()
    sf.table2_count = b.u32()
    sf.table3_count = b.u32()
    sf.change_form_count = b.u32()
    for _ in range(15):
        b.u32()
    sf.flt_raw = body[flt_start:b.o]

    sf.table1 = _read_global_table(b, sf.table1_count)
    sf.table2 = _read_global_table(b, sf.table2_count)
    sf.change_forms_raw = _skip_change_forms(b, sf.change_form_count)
    # Skyrim stores TABLE3COUNT as (actual - 1): the type-1001 Papyrus block is
    # counted implicitly. So the real number of table-3 blocks is +1.
    sf.table3 = _read_global_table(b, sf.table3_count + 1)

    fid_start = b.o
    n_fid = b.u32()
    b.read(4 * n_fid)
    sf.form_ids = body[fid_start:b.o]

    ws_start = b.o
    n_ws = b.u32()
    b.read(4 * n_ws)
    sf.worldspaces = body[ws_start:b.o]

    sf.trailing = body[b.o:]
