"""Morrowind TES3 BSA archive reading (uncompressed).

A TES3 ``.bsa`` is a flat, **uncompressed** asset archive (unlike Skyrim's
LZ4/zlib BSAs). The layout is dead simple:

* header (12 B): ``u32 version(0x100)``, ``u32 hashOffset``, ``u32 fileCount``
* ``fileCount`` × ``{u32 size, u32 offset}``  (offset relative to the data block)
* ``fileCount`` × ``u32 nameOffset``          (offset into the name table)
* the name table (NUL-terminated names)
* the hash table (``fileCount`` × 8 B), at ``12 + hashOffset``
* the file data, at ``12 + hashOffset + 8*fileCount + offset``

The reader indexes by **name** (the hash table is never needed to read), so it
tolerates a zeroed hash table. Constructed from a path it reads only the index
region up front and ``seek``s the file per extract (so a 242 MB mod BSA never
lands wholesale in memory); constructed from bytes it slices in memory.

Grounded in the UESP *Morrowind Mod:BSA File Format*; validated against real
mod BSAs. This backs :mod:`chim.modinstall`'s surgical ("only what we use")
asset extraction.
"""

from __future__ import annotations

import fnmatch
import os
import struct
from typing import Dict, List, Optional, Sequence, Tuple

BSA_VERSION = 0x100


class BsaError(Exception):
    """The bytes/file do not parse as a TES3 BSA."""


class Bsa:
    """A parsed (read-only) Morrowind BSA. Give it a ``path`` or raw ``data``."""

    def __init__(self, path: Optional[str] = None, data: Optional[bytes] = None):
        if (path is None) == (data is None):
            raise BsaError("give exactly one of path= or data=")
        self.path = path
        self._data = data
        #: lower(name) -> (real_name, data_offset, size)
        self._files: Dict[str, Tuple[str, int, int]] = {}
        self._order: List[str] = []
        self._parse()

    # -- parse ------------------------------------------------------------ #

    def _index_bytes(self) -> Tuple[bytes, int]:
        """Return (index_region_bytes, data_start). The index region covers the
        header + file records + name offsets + name table."""
        if self._data is not None:
            d = self._data
            ver, hash_off, count = struct.unpack_from("<III", d, 0)
            if ver != BSA_VERSION:
                raise BsaError(f"bad BSA version {ver:#x}")
            return d[: 12 + hash_off], 12 + hash_off + 8 * count
        with open(self.path, "rb") as f:
            head = f.read(12)
            if len(head) < 12:
                raise BsaError("truncated header")
            ver, hash_off, count = struct.unpack("<III", head)
            if ver != BSA_VERSION:
                raise BsaError(f"bad BSA version {ver:#x}")
            return head + f.read(hash_off), 12 + hash_off + 8 * count

    def _parse(self) -> None:
        idx, data_start = self._index_bytes()
        self._data_start = data_start
        _, hash_off, count = struct.unpack_from("<III", idx, 0)
        self.count = count
        recs = [struct.unpack_from("<II", idx, 12 + 8 * i) for i in range(count)]
        noffs = [struct.unpack_from("<I", idx, 12 + 8 * count + 4 * i)[0] for i in range(count)]
        name0 = 12 + 8 * count + 4 * count
        for (size, off), no in zip(recs, noffs):
            s = name0 + no
            e = idx.index(b"\x00", s)
            name = idx[s:e].decode("cp1252", "replace").replace("/", "\\")
            self._files[name.lower()] = (name, data_start + off, size)
            self._order.append(name)

    # -- read ------------------------------------------------------------- #

    def names(self) -> List[str]:
        return list(self._order)

    def __contains__(self, name: str) -> bool:
        return name.lower().replace("/", "\\") in self._files

    def __len__(self) -> int:
        return self.count

    def _read_range(self, off: int, size: int) -> bytes:
        if self._data is not None:
            return self._data[off : off + size]
        with open(self.path, "rb") as f:
            f.seek(off)
            return f.read(size)

    def read(self, name: str) -> bytes:
        """Raw bytes of the archived file ``name`` (case-insensitive)."""
        key = name.lower().replace("/", "\\")
        if key not in self._files:
            raise KeyError(name)
        _, off, size = self._files[key]
        return self._read_range(off, size)

    def extract(self, name: str, dest: str) -> int:
        """Write ``name`` to ``dest`` (clearing a read-only leftover first)."""
        data = self.read(name)
        d = os.path.dirname(dest)
        if d:
            os.makedirs(d, exist_ok=True)
        if os.path.exists(dest):
            os.chmod(dest, 0o666)
            os.remove(dest)
        with open(dest, "wb") as f:
            f.write(data)
        return len(data)

    def find(self, patterns: Sequence[str]) -> List[str]:
        """Archived names matching any case-insensitive glob (e.g. ``meshes\\em_kids\\*``)."""
        pats = [p.lower().replace("/", "\\") for p in patterns]
        return [n for n in self._order
                if any(fnmatch.fnmatch(n.lower(), p) for p in pats)]

    def extract_matching(self, dest_root: str, *, patterns: Optional[Sequence[str]] = None,
                         names: Optional[Sequence[str]] = None) -> List[Tuple[str, int]]:
        """Extract files matching ``patterns`` (globs) and/or the explicit ``names``
        set into ``dest_root``, preserving each file's internal path. Returns the
        list of ``(name, size)`` extracted. This is the surgical
        "only-the-assets-we-use" path."""
        want = None
        if names is not None:
            want = {n.lower().replace("/", "\\") for n in names}
        pats = [p.lower().replace("/", "\\") for p in patterns] if patterns else None
        out: List[Tuple[str, int]] = []
        for name in self._order:
            low = name.lower()
            if want is not None and low not in want:
                continue
            if pats is not None and not any(fnmatch.fnmatch(low, p) for p in pats):
                continue
            dest = os.path.join(dest_root, name.replace("\\", os.sep))
            size = self.extract(name, dest)
            out.append((name, size))
        return out
