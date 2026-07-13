"""Record / Group data model for Bethesda ESP/ESM plugins.

The parse -> serialize path is guaranteed to be *byte-identical* for any input
this module can parse. Nothing about the on-disk bytes is interpreted beyond
what is required to walk the container structure: record/group headers and the
lengths of their payloads. All field-level interpretation lives in
:mod:`chim.esp.fields`; all compression handling lives in
:mod:`chim.esp.compression`.

Layout recap (little-endian throughout; see docs/SPEC.md for the full spec):

    RECORD = 24-byte header + dataSize bytes of data
        0 : signature   4 ASCII
        4 : dataSize     uint32   (bytes AFTER the 24-byte header)
        8 : flags        uint32
        12: formID       uint32
        16: timestamp+versionControl  uint32
        20: internalVersion  uint16
        22: unknown          uint16

    GRUP = 24-byte header + (groupSize - 24) bytes of nested children
        0 : 'GRUP'
        4 : groupSize   uint32   (INCLUDES the 24-byte header)
        8 : label       4 bytes  (meaning depends on groupType)
        12: groupType   int32
        16: timestamp+versionControl  uint32
        20: unknown          uint16   (+ 2 bytes, kept as raw 'tail')

To stay byte-exact we retain the *whole* 24-byte header verbatim on both
Record and Group (``header`` attribute), and re-derive the size fields from
the live payload only when serializing. Individual header components are also
surfaced as convenience properties.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Union

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

RECORD_HEADER_SIZE = 24
GROUP_HEADER_SIZE = 24

GRUP_SIGNATURE = b"GRUP"
TES4_SIGNATURE = b"TES4"

# Record flags of interest.
FLAG_DELETED = 0x00000020
FLAG_PERSISTENT = 0x00000400  # for REFR/ACHR/ACRE placements
FLAG_INITIALLY_DISABLED = 0x00000800
FLAG_COMPRESSED = 0x00040000

# Group type codes.
GT_TOP = 0
GT_WORLD_CHILDREN = 1
GT_INTERIOR_BLOCK = 2
GT_INTERIOR_SUBBLOCK = 3
GT_EXTERIOR_BLOCK = 4
GT_EXTERIOR_SUBBLOCK = 5
GT_CELL_CHILDREN = 6
GT_TOPIC_CHILDREN = 7
GT_CELL_PERSISTENT_CHILDREN = 8
GT_CELL_TEMPORARY_CHILDREN = 9
GT_CELL_VISIBLE_DISTANT_CHILDREN = 10  # aka QUEST_CHILDREN historically

# struct formats for the two fixed-layout portions of the headers.
# Record header minus the leading 4-byte signature.
_REC_HDR_TAIL = struct.Struct("<IIIIHH")   # dataSize, flags, formID, ts+vc, iver, unk
# Group header minus the leading 4-byte 'GRUP'.
_GRP_HDR_TAIL = struct.Struct("<I4siIH")   # groupSize, label, groupType, ts+vc, unk(+2 raw)

Node = Union["Record", "Group"]


# --------------------------------------------------------------------------- #
# Record
# --------------------------------------------------------------------------- #

@dataclass
class Record:
    """A single ESP record (TES4, STAT, REFR, CELL, ...).

    Attributes
    ----------
    signature : bytes
        4-byte ASCII type code, e.g. ``b"STAT"``.
    header : bytes
        The full verbatim 24-byte on-disk header. Kept so that unknown /
        reserved header bytes round-trip untouched. The ``data_size`` field
        inside is re-synced from ``data`` on serialize.
    data : bytes
        The record payload exactly as stored on disk. For compressed records
        (``FLAG_COMPRESSED``) this is the *compressed* blob
        (``uint32 decompressedSize`` + zlib stream); use
        :func:`chim.esp.compression.decompress_record` to get plaintext.
    """

    signature: bytes
    header: bytes
    data: bytes

    # -- size / flag accessors ------------------------------------------- #

    @property
    def data_size(self) -> int:
        """Payload length in bytes (authoritative == ``len(self.data)``)."""
        return len(self.data)

    @property
    def flags(self) -> int:
        return _REC_HDR_TAIL.unpack_from(self.header, 4)[1]

    @flags.setter
    def flags(self, value: int) -> None:
        self.header = self._repack_header(flags=value)

    @property
    def form_id(self) -> int:
        return _REC_HDR_TAIL.unpack_from(self.header, 4)[2]

    @form_id.setter
    def form_id(self, value: int) -> None:
        self.header = self._repack_header(form_id=value)

    @property
    def timestamp_vc(self) -> int:
        return _REC_HDR_TAIL.unpack_from(self.header, 4)[3]

    @property
    def internal_version(self) -> int:
        return _REC_HDR_TAIL.unpack_from(self.header, 4)[4]

    @property
    def unknown(self) -> int:
        return _REC_HDR_TAIL.unpack_from(self.header, 4)[5]

    @property
    def is_compressed(self) -> bool:
        return bool(self.flags & FLAG_COMPRESSED)

    @property
    def is_deleted(self) -> bool:
        return bool(self.flags & FLAG_DELETED)

    @property
    def is_persistent(self) -> bool:
        return bool(self.flags & FLAG_PERSISTENT)

    @property
    def plugin_index(self) -> int:
        return self.form_id >> 24

    @property
    def object_index(self) -> int:
        return self.form_id & 0x00FFFFFF

    # -- header repacking ------------------------------------------------- #

    def _repack_header(
        self,
        *,
        data_size: Optional[int] = None,
        flags: Optional[int] = None,
        form_id: Optional[int] = None,
    ) -> bytes:
        cur_size, cur_flags, cur_fid, ts_vc, iver, unk = _REC_HDR_TAIL.unpack_from(
            self.header, 4
        )
        return self.signature + _REC_HDR_TAIL.pack(
            cur_size if data_size is None else data_size,
            cur_flags if flags is None else flags,
            cur_fid if form_id is None else form_id,
            ts_vc,
            iver,
            unk,
        )

    def _sync_header(self) -> bytes:
        """Return a 24-byte header whose dataSize matches ``self.data``."""
        return self._repack_header(data_size=len(self.data))

    # -- serialization ---------------------------------------------------- #

    def to_bytes(self) -> bytes:
        return self._sync_header() + self.data

    @property
    def byte_size(self) -> int:
        """Total on-disk footprint including the 24-byte header."""
        return RECORD_HEADER_SIZE + len(self.data)

    # -- convenience ------------------------------------------------------ #

    @property
    def sig(self) -> str:
        """Signature as an ``str`` for readable comparisons/branching."""
        return self.signature.decode("ascii", "replace")

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"Record({self.sig!r} formID=0x{self.form_id:08X} "
            f"flags=0x{self.flags:08X} dataSize={self.data_size})"
        )


# --------------------------------------------------------------------------- #
# Group
# --------------------------------------------------------------------------- #

@dataclass
class Group:
    """A GRUP container node.

    Attributes
    ----------
    header : bytes
        The full verbatim 24-byte on-disk header (starting with ``b"GRUP"``).
        The ``group_size`` field is re-synced from the serialized children on
        serialize.
    children : list[Record | Group]
        Nested records and sub-GRUPs, in file order.
    """

    header: bytes
    children: List[Node] = field(default_factory=list)

    # -- accessors -------------------------------------------------------- #

    @property
    def signature(self) -> bytes:
        return GRUP_SIGNATURE

    @property
    def sig(self) -> str:
        return "GRUP"

    @property
    def group_size(self) -> int:
        """On-disk group size INCLUDING the 24-byte header (authoritative
        value re-derived from children; matches the on-disk field for parsed,
        unmodified trees)."""
        return GROUP_HEADER_SIZE + sum(c.byte_size for c in self.children)

    @property
    def label(self) -> bytes:
        return _GRP_HDR_TAIL.unpack_from(self.header, 4)[1]

    @label.setter
    def label(self, value: bytes) -> None:
        if len(value) != 4:
            raise ValueError("group label must be exactly 4 bytes")
        self.header = self._repack_header(label=value)

    @property
    def group_type(self) -> int:
        return _GRP_HDR_TAIL.unpack_from(self.header, 4)[2]

    @property
    def timestamp_vc(self) -> int:
        return _GRP_HDR_TAIL.unpack_from(self.header, 4)[3]

    @property
    def label_as_signature(self) -> Optional[bytes]:
        """For top groups (type 0) the label is the contained record sig."""
        return self.label if self.group_type == GT_TOP else None

    @property
    def label_as_uint32(self) -> int:
        """Interpret the 4-byte label as a little-endian uint32 (block index,
        cell/worldspace formID, etc.)."""
        return struct.unpack_from("<I", self.header, 8)[0]

    # -- header repacking ------------------------------------------------- #

    def _repack_header(
        self,
        *,
        group_size: Optional[int] = None,
        label: Optional[bytes] = None,
    ) -> bytes:
        cur_size, cur_label, gtype, ts_vc, unk = _GRP_HDR_TAIL.unpack_from(
            self.header, 4
        )
        # Bytes 22..24 are a trailing "unknown" pair not covered by the struct
        # fields above; preserve them verbatim.
        tail = self.header[22:24]
        return (
            GRUP_SIGNATURE
            + _GRP_HDR_TAIL.pack(
                cur_size if group_size is None else group_size,
                cur_label if label is None else label,
                gtype,
                ts_vc,
                unk,
            )
            + tail
        )

    def _sync_header(self) -> bytes:
        return self._repack_header(group_size=self.group_size)

    # -- serialization ---------------------------------------------------- #

    def to_bytes(self) -> bytes:
        body = b"".join(c.to_bytes() for c in self.children)
        return self._sync_header() + body

    @property
    def byte_size(self) -> int:
        return GROUP_HEADER_SIZE + sum(c.byte_size for c in self.children)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"Group(type={self.group_type} "
            f"label={self.label!r} children={len(self.children)})"
        )


# --------------------------------------------------------------------------- #
# Plugin (top-level container)
# --------------------------------------------------------------------------- #

@dataclass
class Plugin:
    """A whole plugin file: the TES4 header record followed by top GRUPs.

    ``top_level`` is the ordered list of everything after (and including) TES4;
    element 0 is always the TES4 :class:`Record`.
    """

    top_level: List[Node] = field(default_factory=list)

    @property
    def tes4(self) -> Optional[Record]:
        if self.top_level and isinstance(self.top_level[0], Record):
            return self.top_level[0]
        return None

    @property
    def groups(self) -> List[Group]:
        return [n for n in self.top_level if isinstance(n, Group)]

    def to_bytes(self) -> bytes:
        return b"".join(n.to_bytes() for n in self.top_level)

    def __iter__(self) -> Iterator[Node]:
        return iter(self.top_level)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

class ParseError(ValueError):
    """Raised when the byte stream does not match the expected structure."""


def _parse_node(buf: bytes, off: int) -> tuple[Node, int]:
    """Parse a single Record or Group starting at ``off``.

    Returns the node and the offset immediately after it.
    """
    if off + RECORD_HEADER_SIZE > len(buf):
        raise ParseError(
            f"truncated header at offset {off}: need {RECORD_HEADER_SIZE} bytes, "
            f"have {len(buf) - off}"
        )
    sig = buf[off : off + 4]

    if sig == GRUP_SIGNATURE:
        header = buf[off : off + GROUP_HEADER_SIZE]
        group_size = struct.unpack_from("<I", buf, off + 4)[0]
        if group_size < GROUP_HEADER_SIZE:
            raise ParseError(
                f"GRUP at {off}: groupSize {group_size} < header size"
            )
        end = off + group_size
        if end > len(buf):
            raise ParseError(
                f"GRUP at {off}: groupSize {group_size} runs past EOF"
            )
        grp = Group(header=header, children=[])
        child_off = off + GROUP_HEADER_SIZE
        while child_off < end:
            child, child_off = _parse_node(buf, child_off)
            grp.children.append(child)
        if child_off != end:
            raise ParseError(
                f"GRUP at {off}: children consumed to {child_off}, "
                f"expected {end}"
            )
        return grp, end

    # Regular record.
    data_size = struct.unpack_from("<I", buf, off + 4)[0]
    header = buf[off : off + RECORD_HEADER_SIZE]
    data_start = off + RECORD_HEADER_SIZE
    data_end = data_start + data_size
    if data_end > len(buf):
        raise ParseError(
            f"record {sig!r} at {off}: dataSize {data_size} runs past EOF"
        )
    rec = Record(signature=sig, header=header, data=buf[data_start:data_end])
    return rec, data_end


def parse_plugin(data: bytes) -> Plugin:
    """Parse an entire plugin byte string into a :class:`Plugin` tree.

    The first record must be ``TES4``. Parsing continues until EOF; any
    trailing bytes that do not form a complete node raise :class:`ParseError`.
    """
    if len(data) < RECORD_HEADER_SIZE or data[0:4] != TES4_SIGNATURE:
        raise ParseError("plugin does not start with a TES4 record")

    plugin = Plugin(top_level=[])
    off = 0
    n = len(data)
    while off < n:
        node, off = _parse_node(data, off)
        plugin.top_level.append(node)
    if off != n:  # pragma: no cover - _parse_node already guards EOF
        raise ParseError(f"trailing bytes: consumed {off}, file is {n}")
    return plugin


# --------------------------------------------------------------------------- #
# Serialization / walking
# --------------------------------------------------------------------------- #

def serialize(tree: Union[Plugin, Node]) -> bytes:
    """Serialize a Plugin / Record / Group back to bytes.

    For any tree produced by :func:`parse_plugin` and left unmodified this is
    byte-identical to the original input.
    """
    return tree.to_bytes()


def iterate(tree: Union[Plugin, Node]) -> Iterator[Node]:
    """Depth-first pre-order iteration over every Record and Group.

    Groups are yielded before their children. A bare Record yields itself.
    """
    if isinstance(tree, Plugin):
        roots: List[Node] = list(tree.top_level)
    else:
        roots = [tree]
    stack: List[Node] = list(reversed(roots))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, Group):
            stack.extend(reversed(node.children))


# ``walk`` is an alias kept for the spec's "walk/iterate" wording.
walk = iterate


def walk_clean(data: bytes) -> bool:
    """Return ``True`` iff ``data`` re-parses cleanly from TES4 to EOF.

    "Clean" means: it parses without error, consumes every byte with zero
    leftover, every GRUP's groupSize consumes exactly its children, and the
    round-trip serialization reproduces the input byte-for-byte.
    """
    try:
        plugin = parse_plugin(data)
    except ParseError:
        return False
    return plugin.to_bytes() == data
