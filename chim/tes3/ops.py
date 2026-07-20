"""Generic-primitive operations over a TES3 :class:`~chim.tes3.container.Plugin`.

The TES3 analogue of :mod:`chim.esp.ops`: a thin layer of *general* primitives
(find, subrecord CRUD, record authoring, masters) that the MCP tools wrap and
domain logic composes agent-side. Everything below the subrecord boundary stays
byte-opaque -- the only fields this module *interprets* are record editor-ids
(for lookup) and the HEDR record count (recomputed on count-changing mutation).

Two things that deliberately differ from :mod:`chim.esp.ops` because TES3 is not
TES4/TES5:

* **Addressing is by string editor-id, not FormID.** There is no FormID minting,
  no master-index high byte, no OBJID. :func:`find_record` takes ``(type, id)``.
* **``HEDR.numRecords`` is recomputed ONLY on count-changing mutation, never on
  serialize.** Real Bethesda masters ship stale counts; recomputing on every
  serialize would break byte-exactness of an untouched file (the inverse of
  esplib's HEDR behaviour). :func:`serialize` touches nothing derived beyond a
  record's ``dataSize``.
"""

from __future__ import annotations

import re
import struct
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from . import container
from .container import Plugin, Record, Subrecord, parse_plugin


class OpsError(ValueError):
    """A requested edit could not be performed against the TES3 plugin."""


# --------------------------------------------------------------------------- #
# Load / serialize / verify
# --------------------------------------------------------------------------- #

def load(data: bytes) -> Plugin:
    return parse_plugin(data)


def serialize(plug: Plugin) -> bytes:
    return container.serialize(plug)


def walk_clean(data: bytes) -> bool:
    """True iff ``data`` parses as TES3 and re-serialises to itself (the transaction
    verify predicate -- the TES3 analogue of the ESP ``walk_clean``)."""
    try:
        plug = parse_plugin(data)
    except container.ParseError:
        return False
    return plug.to_bytes() == data


# --------------------------------------------------------------------------- #
# Addressing (string editor-ids, no FormID)
# --------------------------------------------------------------------------- #

#: The subrecord that carries a record's editor-id, per record type. ``NAME`` is
#: the overwhelming default; a handful of types differ. ``SCPT``'s id is the
#: first 32 bytes of its ``SCHD`` block. Grounded in UESP / OpenMW.
ID_SUBREC: Dict[str, str] = {
    "SKIL": "INDX", "MGEF": "INDX", "SCPT": "SCHD", "INFO": "INAM", "LAND": "INTV",
    # everything else -> NAME
}
_DEFAULT_ID = "NAME"


def record_id(rec: Record) -> Optional[str]:
    """The editor-id STRING of ``rec`` (cp1252, NUL-trimmed) or None.

    Uses :data:`ID_SUBREC` (default ``NAME``); ``SCPT`` ids live in the fixed
    first 32 bytes of ``SCHD``.
    """
    sig = ID_SUBREC.get(rec.type, _DEFAULT_ID)
    sr = rec.get(sig)
    if sr is None:
        return None
    raw = sr.data[:32] if rec.type == "SCPT" else sr.data
    return raw.split(b"\x00", 1)[0].decode("cp1252", "replace")


def iter_records(plug: Plugin, type: Optional[str] = None) -> Iterator[Record]:
    for rec in plug.records:
        if type is None or rec.type == type:
            yield rec


def find_record(plug: Plugin, type: str, id: str) -> Optional[Record]:
    """The record of ``type`` whose editor-id == ``id`` (case-insensitive -- TES3
    ids are). None if absent."""
    want = id.lower()
    for rec in iter_records(plug, type):
        rid = record_id(rec)
        if rid is not None and rid.lower() == want:
            return rec
    return None


def find_by_id(plug: Plugin, id: str) -> List[Record]:
    """Every record (any type) whose editor-id == ``id`` (case-insensitive)."""
    want = id.lower()
    out: List[Record] = []
    for rec in plug.records:
        rid = record_id(rec)
        if rid is not None and rid.lower() == want:
            out.append(rec)
    return out


def find_by_id_regex(plug: Plugin, pattern: str) -> List[Record]:
    """Every record whose editor-id matches the regex ``pattern`` (search)."""
    rx = re.compile(pattern)
    out: List[Record] = []
    for rec in plug.records:
        rid = record_id(rec)
        if rid is not None and rx.search(rid) is not None:
            out.append(rec)
    return out


def record_view(rec: Record) -> dict:
    """JSON-able summary of a record (no payload decode)."""
    return {
        "type": rec.type,
        "id": record_id(rec),
        "flags": rec.flags,
        "is_deleted": rec.is_deleted(),
        "data_size": rec.data_size(),
        "subrecords": [s.name for s in rec.subrecords],
    }


# --------------------------------------------------------------------------- #
# HEDR record count (recompute ONLY on count-changing mutation)
# --------------------------------------------------------------------------- #

def _recompute_num_records(plug: Plugin) -> None:
    """Rewrite ``HEDR.numRecords`` = number of CONTENT records (excludes the TES3
    header). Patches only the trailing u32 so author[32]/desc[256] padding is
    preserved byte-for-byte. Called ONLY by count-changing ops."""
    hedr = plug.header.get("HEDR")
    if hedr is None or hedr.size() < container.HEDR_SIZE:
        raise OpsError("TES3 header is missing a full HEDR subrecord")
    buf = bytearray(hedr.data)
    struct.pack_into("<I", buf, container.HEDR_NUMRECORDS_OFFSET, len(plug.records))
    hedr.data = bytes(buf)


def num_records_stored(plug: Plugin) -> Optional[int]:
    """The ``numRecords`` value currently stored in HEDR (may disagree with the
    actual count on real Bethesda masters)."""
    hedr = plug.header.get("HEDR")
    if hedr is None or hedr.size() < container.HEDR_SIZE:
        return None
    return struct.unpack_from("<I", hedr.data, container.HEDR_NUMRECORDS_OFFSET)[0]


# --------------------------------------------------------------------------- #
# Record authoring (build / add / delete) -- no typed decoders
# --------------------------------------------------------------------------- #

def build_record(type: str, subrecords: Sequence[Tuple[str, bytes]],
                 flags: int = 0, header1: int = 0) -> Record:
    """Build an unattached :class:`Record` from ``(subsig, payload)`` pairs.

    Payloads are composed **agent-side** (e.g. the RADT race block); the engine
    does no field validation beyond 4-char tags. This is what lets RACE/BODY and
    any other record be authored without per-record decoders.
    """
    if len(type) != 4:
        raise OpsError(f"record type {type!r} must be exactly 4 chars")
    subs: List[Subrecord] = []
    for sig, payload in subrecords:
        if len(sig) != 4:
            raise OpsError(f"subrecord tag {sig!r} must be exactly 4 chars")
        subs.append(Subrecord(sig, bytes(payload)))
    return Record(type, header1, flags, subs)


def add_record(plug: Plugin, rec: Record, before: Optional[Record] = None) -> Record:
    """Append ``rec`` to the flat records list (or insert before an existing
    record), then recompute ``HEDR.numRecords``."""
    if before is None:
        plug.records.append(rec)
    else:
        plug.records.insert(plug.records.index(before), rec)
    _recompute_num_records(plug)
    return rec


def delete_record(plug: Plugin, rec: Record) -> None:
    plug.records.remove(rec)
    _recompute_num_records(plug)


def delete_records(plug: Plugin, type: str, ids: Sequence[str]) -> List[str]:
    """Hard-remove records of ``type`` whose editor-id is in ``ids``. Returns the
    ids removed. (Hard remove shrinks numRecords; see :func:`mark_deleted` for the
    soft-delete convention.)"""
    want = {i.lower() for i in ids}
    removed: List[str] = []
    kept: List[Record] = []
    for rec in plug.records:
        rid = record_id(rec)
        if rec.type == type and rid is not None and rid.lower() in want:
            removed.append(rid)
        else:
            kept.append(rec)
    if removed:
        plug.records = kept
        _recompute_num_records(plug)
    return removed


def mark_deleted(plug: Plugin, rec: Record) -> None:
    """Morrowind soft-delete: set the header ``DELETED`` flag AND ensure a ``DELE``
    subrecord (u32 0). Does NOT remove the record or change ``numRecords`` -- the
    record stays physically present (this is the convention the CS/OpenMW read)."""
    rec.flags |= container.FLAG_DELETED
    if rec.get("DELE") is None:
        rec.subrecords.append(Subrecord("DELE", struct.pack("<I", 0)))


# --------------------------------------------------------------------------- #
# Generic subrecord CRUD (opaque bytes)
# --------------------------------------------------------------------------- #

def get_subrecords(rec: Record) -> List[Subrecord]:
    return list(rec.subrecords)


def field_views(rec: Record, only: Optional[str] = None) -> List[dict]:
    """``{sig, index, size, payload_hex}`` per subrecord; ``index`` is the
    whole-record occurrence number for that sig (feeds back into the mutating ops)."""
    counts: Dict[str, int] = {}
    out: List[dict] = []
    for sr in rec.subrecords:
        idx = counts.get(sr.name, 0)
        counts[sr.name] = idx + 1
        if only is not None and sr.name != only:
            continue
        out.append({"sig": sr.name, "index": idx, "size": sr.size(),
                    "payload_hex": sr.data.hex()})
    return out


def _nth(rec: Record, sig: str, index: int) -> Tuple[int, Subrecord]:
    """List position + subrecord of the ``index``-th ``sig`` occurrence, or raise."""
    seen = 0
    for pos, sr in enumerate(rec.subrecords):
        if sr.name == sig:
            if seen == index:
                return pos, sr
            seen += 1
    raise OpsError(f"record {rec.type!r} has no {sig!r} subrecord at index {index}")


def set_subrecord(rec: Record, signature: str, payload: bytes, index: int = 0) -> Subrecord:
    """Replace the whole payload of the ``index``-th ``signature`` subrecord."""
    _, sr = _nth(rec, signature, index)
    sr.data = bytes(payload)
    return sr


def patch_subrecord(rec: Record, signature: str, offset: int,
                    new_bytes: bytes, index: int = 0) -> Subrecord:
    """Overwrite ``new_bytes`` at ``offset`` inside the subrecord (length
    unchanged). The byte-exact-friendly editor for fixed-width fields (RADT block,
    author[32]) -- surrounding NUL padding is left untouched."""
    _, sr = _nth(rec, signature, index)
    end = offset + len(new_bytes)
    if offset < 0 or end > len(sr.data):
        raise OpsError(f"patch [{offset}:{end}] out of range for {signature!r} "
                       f"(size {len(sr.data)})")
    buf = bytearray(sr.data)
    buf[offset:end] = new_bytes
    sr.data = bytes(buf)
    return sr


def insert_subrecord(rec: Record, signature: str, payload: bytes,
                     after: Optional[str] = None, before: Optional[str] = None,
                     at_index: Optional[int] = None) -> int:
    """Insert a new subrecord. Position: ``at_index`` (list index) > ``after``
    (right after the last such tag) > ``before`` (right before the first such tag)
    > append. Returns the list index it landed at. Subrecord order is significant
    in TES3 -- this never re-sorts."""
    sr = Subrecord(signature, bytes(payload))
    if at_index is not None:
        pos = max(0, min(at_index, len(rec.subrecords)))
    elif after is not None:
        pos = len(rec.subrecords)
        for i, s in enumerate(rec.subrecords):
            if s.name == after:
                pos = i + 1
    elif before is not None:
        pos = len(rec.subrecords)
        for i, s in enumerate(rec.subrecords):
            if s.name == before:
                pos = i
                break
    else:
        pos = len(rec.subrecords)
    rec.subrecords.insert(pos, sr)
    return pos


def delete_subrecord(rec: Record, signature: str, index: int = 0) -> Subrecord:
    """Remove the ``index``-th ``signature`` subrecord; returns it."""
    pos, sr = _nth(rec, signature, index)
    del rec.subrecords[pos]
    return sr


# --------------------------------------------------------------------------- #
# Masters (MAST/DATA pairs in the TES3 header record)
# --------------------------------------------------------------------------- #

def masters(plug: Plugin) -> List[Tuple[str, int]]:
    """``[(filename, size)]`` from the header's ``MAST``+``DATA`` subrecord pairs
    (walked positionally: each ``MAST`` is followed by its ``DATA``)."""
    out: List[Tuple[str, int]] = []
    subs = plug.header.subrecords
    for i, sr in enumerate(subs):
        if sr.name == "MAST":
            fn = sr.data.split(b"\x00", 1)[0].decode("cp1252", "replace")
            size = 0
            if i + 1 < len(subs) and subs[i + 1].name == "DATA":
                size = struct.unpack_from("<Q", subs[i + 1].data, 0)[0]
            out.append((fn, size))
    return out


def add_master(plug: Plugin, name: str, size: int = 0) -> List[Tuple[str, int]]:
    """Append a ``MAST``+``DATA`` pair to the header (idempotent, case-insensitive).

    Does NOT touch ``numRecords`` (masters aren't records) and -- unlike TES4 --
    does NOT renumber anything: TES3 cross-references are string ids, so there is
    no master-index high byte to fix up. Returns the resulting master list."""
    if any(fn.lower() == name.lower() for fn, _ in masters(plug)):
        return masters(plug)
    subs = plug.header.subrecords
    # insert right after the last existing MAST/DATA pair, else after HEDR.
    pos = len(subs)
    for i in range(len(subs) - 1, -1, -1):
        if subs[i].name in ("MAST", "DATA"):
            pos = i + 1
            break
    else:
        for i, s in enumerate(subs):
            if s.name == "HEDR":
                pos = i + 1
                break
    subs.insert(pos, Subrecord("MAST", name.encode("cp1252") + b"\x00"))
    subs.insert(pos + 1, Subrecord("DATA", struct.pack("<Q", int(size) & 0xFFFFFFFFFFFFFFFF)))
    return masters(plug)


def rename_master(plug: Plugin, old: str, new: str) -> List[Tuple[str, int]]:
    """Rewrite the ``MAST`` filename ``old`` -> ``new`` (case-insensitive match)."""
    for sr in plug.header.subrecords:
        if sr.name == "MAST":
            cur = sr.data.split(b"\x00", 1)[0].decode("cp1252", "replace")
            if cur.lower() == old.lower():
                sr.data = new.encode("cp1252") + b"\x00"
                return masters(plug)
    raise OpsError(f"master {old!r} not found in {[m for m, _ in masters(plug)]!r}")
