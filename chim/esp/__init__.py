"""chim.esp -- Bethesda ESP/ESM on-disk format model.

Modules:
    records      Record / Group / Plugin data model + parse/serialize/walk.
    compression  0x40000-compressed record data (zlib) helpers.
    fields       Subrecord (field) iteration and typed decode/encode.
"""

from __future__ import annotations

from .records import (
    Record,
    Group,
    Plugin,
    parse_plugin,
    serialize,
    walk_clean,
    iterate,
    walk,
    RECORD_HEADER_SIZE,
    GROUP_HEADER_SIZE,
    FLAG_DELETED,
    FLAG_PERSISTENT,
    FLAG_INITIALLY_DISABLED,
    FLAG_COMPRESSED,
)
from .compression import decompress_record, store_uncompressed, is_compressed
from . import fields

__all__ = [
    "Record",
    "Group",
    "Plugin",
    "parse_plugin",
    "serialize",
    "walk_clean",
    "iterate",
    "walk",
    "RECORD_HEADER_SIZE",
    "GROUP_HEADER_SIZE",
    "FLAG_DELETED",
    "FLAG_PERSISTENT",
    "FLAG_INITIALLY_DISABLED",
    "FLAG_COMPRESSED",
    "decompress_record",
    "store_uncompressed",
    "is_compressed",
    "fields",
]
