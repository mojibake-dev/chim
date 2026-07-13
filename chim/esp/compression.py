"""Compressed record data (the 0x40000 flag).

When a record has ``FLAG_COMPRESSED`` (0x40000) set, its on-disk ``data`` is::

    uint32 decompressedSize      # length of the plaintext payload
    <zlib stream>                # 2-byte zlib header + raw DEFLATE

The plaintext is the same field stream you'd see in an uncompressed record of
the same signature. These helpers convert between the two representations and
keep the flag / header in sync via :class:`chim.esp.records.Record`.
"""

from __future__ import annotations

import struct
import zlib

from .records import Record, FLAG_COMPRESSED


def is_compressed(record: Record) -> bool:
    """True iff the record's compressed flag is set."""
    return bool(record.flags & FLAG_COMPRESSED)


def decompress_record(record: Record) -> bytes:
    """Return the *plaintext* field payload of ``record``.

    If the record is not compressed, ``record.data`` is returned unchanged.
    For compressed records the leading uint32 is stripped and the zlib stream
    is inflated; the result is validated against the stored decompressed size.
    """
    if not is_compressed(record):
        return record.data

    data = record.data
    if len(data) < 4:
        raise ValueError("compressed record data too short for size prefix")
    decompressed_size = struct.unpack_from("<I", data, 0)[0]
    plain = zlib.decompress(data[4:])
    if len(plain) != decompressed_size:
        raise ValueError(
            f"decompressed length {len(plain)} != stored size "
            f"{decompressed_size}"
        )
    return plain


def compress_payload(plain: bytes, level: int = 9) -> bytes:
    """Produce the on-disk compressed ``data`` blob for a plaintext payload.

    Returns ``uint32 decompressedSize + zlib(plain)``. Note that the exact
    bytes depend on zlib's compressor, so this does not necessarily reproduce
    an arbitrary original blob; use it only when you intend to (re)write the
    record's stored form.
    """
    return struct.pack("<I", len(plain)) + zlib.compress(plain, level)


def store_uncompressed(record: Record) -> Record:
    """Clear the compressed flag in-place, replacing ``data`` with plaintext.

    After this the record holds its field stream directly (no size prefix, no
    zlib) and ``FLAG_COMPRESSED`` is unset. Returns the same ``record`` for
    chaining. A no-op for already-uncompressed records.
    """
    if not is_compressed(record):
        return record
    plain = decompress_record(record)
    record.data = plain
    record.flags = record.flags & ~FLAG_COMPRESSED
    return record


def store_compressed(record: Record, level: int = 9) -> Record:
    """Compress a currently-uncompressed record in-place.

    Sets ``FLAG_COMPRESSED`` and replaces ``data`` with the
    ``size + zlib`` blob. Returns the same ``record``. A no-op for
    already-compressed records.
    """
    if is_compressed(record):
        return record
    blob = compress_payload(record.data, level)
    record.data = blob
    record.flags = record.flags | FLAG_COMPRESSED
    return record
