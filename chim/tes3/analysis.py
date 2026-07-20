"""Read-only TES3 plugin analysis -- the JSON reporting layer for the MCP tools.

Pure functions over parsed bytes (mirrors :mod:`chim.save.analysis`): parse the
container, read the HEDR header fields, and summarise. No mutation.
"""

from __future__ import annotations

import struct
from collections import Counter
from typing import Any, Dict

from .container import parse_plugin, HEDR_SIZE
from . import ops

#: HEDR fileType u32 -> label (0 esp, 1 esm; 32 is a TES3 savegame, out of scope).
_FILETYPE = {0: "esp", 1: "esm", 32: "savegame"}


def _cstr(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("cp1252", "replace")


def tes3_info(raw: bytes) -> Dict[str, Any]:
    """Container-level summary of a TES3 plugin: header/version/author, masters,
    record count (stored vs actual), a record-type histogram, and the byte-exact
    ``walk_clean`` flag."""
    plug = parse_plugin(raw)
    info: Dict[str, Any] = {"magic": plug.header.type}

    hedr = plug.header.get("HEDR")
    if hedr is not None and hedr.size() >= HEDR_SIZE:
        d = hedr.data
        ftype = struct.unpack_from("<I", d, 4)[0]
        info.update(
            version=round(struct.unpack_from("<f", d, 0)[0], 3),
            file_type=_FILETYPE.get(ftype, ftype),
            author=_cstr(d[8:40]),
            description=_cstr(d[40:296]),
            num_records_stored=struct.unpack_from("<I", d, HEDR_SIZE - 4)[0],
        )

    counts = Counter(r.type for r in plug.records)
    info.update(
        record_count=len(plug.records),
        masters=[{"name": n, "size": s} for n, s in ops.masters(plug)],
        record_types=dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        walk_clean=plug.roundtrips(),
    )
    return info
