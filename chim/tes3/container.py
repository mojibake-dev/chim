"""Morrowind TES3 plugin (``.esp``/``.esm``/``.omwaddon``) container: parse /
round-trip / re-serialize.

A TES3 file is **not** a Skyrim plugin. Where :mod:`chim.esp` (esplib) models a
TES4/TES5 GRUP *tree* of records with numeric FormIDs and zlib-compressed
payloads, a TES3 file is a **flat** sequence of records with **no** GRUP
hierarchy, **no** compression, and records addressed by a **string editor-id**
(not a FormID). The grammar is dead simple:

* **Record** = ``type[4]`` + ``dataSize`` u32 + ``header1`` u32 (unused, ~0) +
  ``flags`` u32, then ``dataSize`` bytes of subrecords.
* **Subrecord** = ``name[4]`` + ``size`` u32 + ``size`` bytes of data. There is
  no subrecord count; walk until the record's ``dataSize`` is consumed.

The first record is always the ``TES3`` header (version / masters / record
count); the rest are content records to EOF.

This module keeps everything **byte-opaque below the subrecord boundary**: a
:class:`Subrecord`'s ``data`` is the raw payload, and the only *derived* field on
serialize is a record's ``dataSize`` (recomputed from its subrecords). For an
unmodified plugin, :meth:`Plugin.to_bytes` reproduces the input **byte-for-byte**
(:meth:`Plugin.roundtrips` -- the TES3 analogue of the ESP ``walk_clean``
invariant). Domain typing (the RADT race block, cell references, ...) is composed
*agent-side*; the engine never decodes a payload it isn't asked to.

Byte layout grounded in the UESP *Morrowind Mod:TES3 File Format* reference and
the OpenMW ESM reader; validated byte-exact against real Morrowind plugins. All
integers little-endian. 4-char structural tags are stored via latin-1 (a total
byte<->str bijection, so any tag round-trips); string *content* is Windows-1252.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterator, List, Optional

MAGIC = "TES3"

RECORD_HEADER_SIZE = 16          # type[4] + dataSize u32 + header1 u32 + flags u32
SUBREC_HEADER_SIZE = 8           # name[4] + size u32

# Record-header flag bits (u32). TES3 uses very few; these are the ones that
# matter for editing. A "deleted" record carries this bit AND a DELE subrecord.
FLAG_DELETED = 0x00000020
FLAG_PERSISTENT = 0x00000400     # "blocked"/persistent reference marker

#: HEDR is a fixed 300-byte block: version f32, fileType u32, author[32],
#: description[256], numRecords u32. numRecords lives at this offset.
HEDR_SIZE = 300
HEDR_NUMRECORDS_OFFSET = 296


class ParseError(Exception):
    """The bytes do not parse as a TES3 plugin (or a record/subrecord misaligned)."""


class Reader:
    """Little-endian cursor over a ``bytes`` buffer (subset of :class:`chim.save.ess.Reader`)."""

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

    def tag(self) -> str:
        """A 4-char structural tag (record type / subrecord name), latin-1."""
        return self.read(4).decode("latin-1")

    def u32(self) -> int:
        if self.o + 4 > len(self.d):
            raise ParseError(f"u32 at {self.o} exceeds buffer {len(self.d)}")
        v = struct.unpack_from("<I", self.d, self.o)[0]
        self.o += 4
        return v

    def u64(self) -> int:
        if self.o + 8 > len(self.d):
            raise ParseError(f"u64 at {self.o} exceeds buffer {len(self.d)}")
        v = struct.unpack_from("<Q", self.d, self.o)[0]
        self.o += 8
        return v


@dataclass
class Subrecord:
    """One ``[name u8*4][size u32][data]`` field. ``data`` is OPAQUE bytes."""

    name: str
    data: bytes

    def size(self) -> int:
        return len(self.data)

    def to_bytes(self) -> bytes:
        return self.name.encode("latin-1") + struct.pack("<I", len(self.data)) + self.data


@dataclass
class Record:
    """One TES3 record: a 4-char ``type`` + ``header1``/``flags`` + a flat list of
    subrecords.

    ``dataSize`` is **never stored** -- it is recomputed on :meth:`to_bytes` from
    the subrecords. That is exactly what makes an *unmutated* record round-trip
    byte-for-byte and a *mutated* one stay internally consistent. ``header1``
    (unused, usually 0 but not guaranteed) and ``flags`` are preserved verbatim.
    """

    type: str
    header1: int
    flags: int
    subrecords: List[Subrecord] = field(default_factory=list)

    def data_size(self) -> int:
        return sum(SUBREC_HEADER_SIZE + s.size() for s in self.subrecords)

    def to_bytes(self) -> bytes:
        body = b"".join(s.to_bytes() for s in self.subrecords)
        return (self.type.encode("latin-1")
                + struct.pack("<III", len(body), self.header1, self.flags)
                + body)

    def get(self, name: str, index: int = 0) -> Optional[Subrecord]:
        """The ``index``-th subrecord named ``name`` (whole-record occurrence), or None."""
        seen = 0
        for sr in self.subrecords:
            if sr.name == name:
                if seen == index:
                    return sr
                seen += 1
        return None

    def all(self, name: str) -> List[Subrecord]:
        return [sr for sr in self.subrecords if sr.name == name]

    def is_deleted(self) -> bool:
        return bool(self.flags & FLAG_DELETED)


@dataclass
class Plugin:
    """A parsed TES3 plugin: the ``TES3`` header record + a FLAT list of content
    records. No ``Group`` class, no tree.

    ``header`` is the leading ``TES3`` record; ``records`` are the content records
    in file order to EOF. ``raw`` is retained so :meth:`roundtrips` can assert the
    byte-exact invariant.
    """

    raw: bytes
    header: Record
    records: List[Record] = field(default_factory=list)

    def all_records(self) -> Iterator[Record]:
        yield self.header
        yield from self.records

    def to_bytes(self) -> bytes:
        return b"".join(r.to_bytes() for r in self.all_records())

    def roundtrips(self) -> bool:
        """True iff a parsed-then-unmodified plugin re-serialises byte-identically."""
        return self.to_bytes() == self.raw


def _parse_subrecords(body: bytes) -> List[Subrecord]:
    br = Reader(body)
    out: List[Subrecord] = []
    while not br.eof():
        name = br.tag()
        size = br.u32()
        out.append(Subrecord(name, br.read(size)))   # data kept RAW/opaque
    return out


def _parse_record(r: Reader) -> Record:
    typ = r.tag()
    data_size = r.u32()
    header1 = r.u32()
    flags = r.u32()
    body = r.read(data_size)
    return Record(typ, header1, flags, _parse_subrecords(body))


def parse_plugin(data: bytes) -> Plugin:
    """Parse TES3 ``data`` into a :class:`Plugin` (header record + flat records)."""
    r = Reader(data)
    header = _parse_record(r)
    if header.type != MAGIC:
        raise ParseError(f"first record is {header.type!r}, expected {MAGIC!r}")
    records: List[Record] = []
    while not r.eof():
        records.append(_parse_record(r))
    return Plugin(raw=data, header=header, records=records)


def serialize(plug: Plugin) -> bytes:
    """Serialize a :class:`Plugin` back to bytes (header record + flat records)."""
    return plug.to_bytes()


def roundtrips(plug: Plugin) -> bool:
    return plug.roundtrips()
