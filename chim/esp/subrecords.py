"""Record-level subrecord (field) CRUD -- get / set / patch / insert / delete.

:mod:`chim.esp.fields` parses and packs the *plaintext* field stream (the
``XXXX`` size-override for >0xFFFF fields is handled transparently there). This
module ties that layer to a live :class:`~chim.esp.records.Record`: it reads the
plaintext (decompressing a compressed record first), applies one operation to a
subrecord selected by 4-byte signature and occurrence index, re-packs, and
writes the result back **stored uncompressed** (``FLAG_COMPRESSED`` cleared).

Why store uncompressed: the game reads uncompressed records fine and the CK
re-compresses on its next save -- this is the exact convention proven on the
live Tel Solanum plugin for editing compressed cells (see the grotto notes).

What this guarantees, and what it does not:

* No size bookkeeping. A record's ``dataSize`` is a property of ``len(data)`` and
  every container GRUP re-derives its size in
  :func:`~chim.esp.records.serialize`, so changing a record's field stream
  re-flows structurally. Subrecord edits never change the record *count*, so
  ``HEDR.num_records`` is untouched.
* These edit RAW payload bytes. They keep the *container* well-formed (which the
  surrounding :func:`~chim.esp.safety.transaction` re-verifies with a walk-clean)
  but do NOT validate that the new bytes are semantically correct for the record
  type -- that judgement is the caller's.
"""

from __future__ import annotations

from typing import List, Optional

from .records import Record, FLAG_COMPRESSED
from .compression import decompress_record
from . import fields as _fields
from .fields import Field


class SubrecordError(ValueError):
    """Invalid subrecord operation (missing signature, bad index or range)."""


# --------------------------------------------------------------------------- #
# read / write the field list of a (possibly compressed) record
# --------------------------------------------------------------------------- #

def read_fields(record: Record) -> List[Field]:
    """Parse ``record``'s plaintext field stream (decompressing if needed)."""
    return _fields.parse_fields(decompress_record(record))


def write_fields(record: Record, flds: List[Field]) -> None:
    """Pack ``flds`` into ``record.data``, stored UNCOMPRESSED.

    Clears ``FLAG_COMPRESSED`` when it was set (the payload is now plaintext).
    """
    record.data = _fields.pack_fields(flds)
    if record.flags & FLAG_COMPRESSED:
        record.flags = record.flags & ~FLAG_COMPRESSED


# --------------------------------------------------------------------------- #
# occurrence indexing
# --------------------------------------------------------------------------- #

def _nth_index(flds: List[Field], signature: bytes, index: int) -> int:
    """Absolute position in ``flds`` of the ``index``-th field with ``signature``.

    Repeated signatures (``XLKR``, ``LNAM``, ``EFID``/``EFIT``, ``CTDA`` ...) are
    numbered 0, 1, 2, ... in file order. Raises :class:`SubrecordError` if there
    is no such occurrence.
    """
    seen = 0
    for i, f in enumerate(flds):
        if f.signature == signature:
            if seen == index:
                return i
            seen += 1
    raise SubrecordError(
        f"no occurrence #{index} of subrecord {signature!r} (found {seen})"
    )


def count_subrecords(record: Record, signature: bytes) -> int:
    """How many fields with ``signature`` the record currently holds."""
    return sum(1 for f in read_fields(record) if f.signature == signature)


# --------------------------------------------------------------------------- #
# operations
# --------------------------------------------------------------------------- #

def get_subrecords(record: Record,
                   signature: Optional[bytes] = None) -> List[Field]:
    """Return the record's fields, optionally filtered to ``signature``."""
    flds = read_fields(record)
    if signature is None:
        return flds
    return [f for f in flds if f.signature == signature]


def set_subrecord(record: Record, signature: bytes, payload: bytes,
                  index: int = 0) -> Field:
    """Replace the ``index``-th ``signature`` field's whole payload.

    The field may grow or shrink freely (``pack_fields`` re-derives ``XXXX`` when
    a payload crosses 0xFFFF). Raises if that occurrence is absent.
    """
    flds = read_fields(record)
    pos = _nth_index(flds, signature, index)
    flds[pos] = Field(signature=signature, payload=bytes(payload))
    write_fields(record, flds)
    return flds[pos]


def patch_subrecord(record: Record, signature: bytes, offset: int,
                    new_bytes: bytes, index: int = 0) -> Field:
    """Overwrite ``new_bytes`` into the ``index``-th ``signature`` field at
    ``offset`` (in place -- the field length is unchanged).

    This is the surgical byte-range editor (e.g. flip an ``MGEF DATA`` flag byte,
    set a magic-skill byte, retarget a formID inside a fixed struct). Raises if
    the range would run past the payload; use :func:`set_subrecord` to change a
    field's length.
    """
    new_bytes = bytes(new_bytes)
    flds = read_fields(record)
    pos = _nth_index(flds, signature, index)
    payload = bytearray(flds[pos].payload)
    if offset < 0 or offset + len(new_bytes) > len(payload):
        raise SubrecordError(
            f"patch range [{offset}, {offset + len(new_bytes)}) out of bounds "
            f"for {signature!r} payload of length {len(payload)}"
        )
    payload[offset:offset + len(new_bytes)] = new_bytes
    flds[pos] = Field(signature=signature, payload=bytes(payload))
    write_fields(record, flds)
    return flds[pos]


def insert_subrecord(record: Record, signature: bytes, payload: bytes,
                     after: Optional[bytes] = None,
                     before: Optional[bytes] = None,
                     at_index: Optional[int] = None) -> int:
    """Insert a new ``signature`` field carrying ``payload``.

    Position (at most one selector may be given):

    * ``at_index`` -> absolute position in the field list (0..len);
    * ``after``    -> immediately after the LAST field whose sig == ``after``
      (e.g. append an ``LNAM`` after the last ``LNAM`` in a ``FLST``);
    * ``before``   -> immediately before the FIRST field whose sig == ``before``;
    * none given   -> appended at the end.

    Returns the absolute index the new field landed at.
    """
    selectors = [x for x in (after, before, at_index) if x is not None]
    if len(selectors) > 1:
        raise SubrecordError("give at most one of after / before / at_index")
    flds = read_fields(record)
    new = Field(signature=signature, payload=bytes(payload))
    if at_index is not None:
        pos = int(at_index)
        if pos < 0 or pos > len(flds):
            raise SubrecordError(f"at_index {pos} out of range 0..{len(flds)}")
    elif after is not None:
        last = None
        for i, f in enumerate(flds):
            if f.signature == after:
                last = i
        if last is None:
            raise SubrecordError(f"no field {after!r} to insert after")
        pos = last + 1
    elif before is not None:
        pos = None
        for i, f in enumerate(flds):
            if f.signature == before:
                pos = i
                break
        if pos is None:
            raise SubrecordError(f"no field {before!r} to insert before")
    else:
        pos = len(flds)
    flds.insert(pos, new)
    write_fields(record, flds)
    return pos


def delete_subrecord(record: Record, signature: bytes, index: int = 0) -> Field:
    """Remove the ``index``-th ``signature`` field. Returns the removed Field."""
    flds = read_fields(record)
    pos = _nth_index(flds, signature, index)
    removed = flds.pop(pos)
    write_fields(record, flds)
    return removed
