"""Thin adapter over :mod:`esplib` exposing exactly what chim's MCP tools need.

This module is the seam between chim's MCP server + safety transaction and the
:mod:`esplib` byte engine. The server hands us the current on-disk plugin
**bytes** (from ``safety.transaction``'s ``txn.data``); we parse them into an
:class:`esplib.plugin.Plugin`, apply one edit, and serialize back to bytes.

Why an adapter instead of calling esplib directly from the tools
----------------------------------------------------------------
* **Load-from-bytes seam.** esplib's :class:`~esplib.plugin.Plugin` loads from a
  *path* (its ``__init__`` / ``_load`` read a file). chim's transaction owns the
  bytes (it read them under a lock and will write them back under verification),
  so we need a bytes entry point. esplib exposes the pieces publicly enough:
  :meth:`Plugin._parse_plugin` consumes a :class:`~esplib.utils.BinaryReader`,
  and :meth:`Plugin.to_bytes` serializes without touching disk. :func:`load`
  wires those together on a bare ``Plugin()`` (constructed with no path, so its
  auto-load never runs but its lock / index / fixup state is fully initialized).
* **Group-tree operations.** esplib models the record tree as
  ``Plugin.groups`` (top-level :class:`~esplib.record.GroupRecord`), each holding
  a ``records`` list of :class:`~esplib.record.Record` or nested ``GroupRecord``.
  chim's placement edits (insert a REFR into a CELL's persistent-children GRUP,
  clone a cluster, delete a CELL + its child block) are expressed against that
  tree here, so the tool layer stays a thin wire wrapper.

FormID minting
--------------
esplib's :meth:`Plugin.get_next_form_id` mints a *sentinel* (file-index 0xFF)
FormID that only gets its real local index at save time, and only for records
registered on ``_new_records`` via ``add_record``. Records we splice directly
into a nested GRUP (a CELL's type-8 children) are never seen by ``add_record``,
so their sentinel would never resolve. To match chim's original semantics --
mint ``(pluginIndex << 24) | nextObjectID`` immediately, with pluginIndex ==
the master count -- :func:`mint_form_id` computes the real index directly and
bumps ``header.next_object_id``. The resulting FormID is final; splicing it
anywhere is safe.

HEDR bookkeeping
----------------
Unlike chim's own engine, esplib recomputes ``HEDR.num_records`` from the live
group tree on every :meth:`Plugin.to_bytes`, so nothing here maintains the
record count by hand. We only touch ``next_object_id`` (when minting).
"""

from __future__ import annotations

import re
import struct
from typing import Dict, List, Optional, Sequence, Tuple

from esplib.plugin import Plugin
from esplib.record import Record, SubRecord, GroupRecord
from esplib.utils import BinaryReader, FormID
from esplib import vmad as _vmad
from esplib.vmad import (
    VmadData, VmadScript, VmadProperty, VmadObject, VmadAliasScripts,
    VmadFragmentData,
    PROP_OBJECT, PROP_STRING, PROP_INT32, PROP_FLOAT, PROP_BOOL, PROP_STRUCT,
    PROP_OBJECT_ARRAY, PROP_STRING_ARRAY, PROP_INT32_ARRAY, PROP_FLOAT_ARRAY,
    PROP_BOOL_ARRAY, PROP_STRUCT_ARRAY,
)


class OpsError(ValueError):
    """A requested edit could not be performed against the plugin tree."""


# --------------------------------------------------------------------------- #
# Constants (byte layouts + group types) -- match chim.esp.fields / records
# --------------------------------------------------------------------------- #

# Header flags (32-bit record-header flags field). Bit positions match esplib's
# RefHeaderFlags: Persistent=bit10, InitiallyDisabled=bit11, Deleted=bit5,
# Compressed=bit18.
FLAG_DELETED = 0x00000020
FLAG_PERSISTENT = 0x00000400
FLAG_INITIALLY_DISABLED = 0x00000800
FLAG_COMPRESSED = 0x00040000

# GRUP group_type codes (Bethesda's TES4 group taxonomy).
GT_TOP = 0
GT_CELL_CHILDREN = 6
GT_CELL_PERSISTENT_CHILDREN = 8
GT_CELL_TEMPORARY_CHILDREN = 9

# Placement record signatures (records that instantiate a base via NAME and can
# carry XESP / XLKR). Mirrors chim.esp.query.PLACEMENT_SIGS.
PLACEMENT_SIGS = ("REFR", "ACHR", "ACRE", "PGRE", "PHZD", "PMIS", "PARW", "PBAR",
                  "PBEA", "PCON", "PFLA")

_POSROT = struct.Struct("<6f")   # DATA: x,y,z, rx,ry,rz (float32)
_XLKR = struct.Struct("<II")     # keyword formID + ref formID
_XESP = struct.Struct("<II")     # parent formID + flags
_OBND = struct.Struct("<6h")     # x1,y1,z1,x2,y2,z2 (int16)


# --------------------------------------------------------------------------- #
# Load / serialize -- the bytes <-> Plugin seam
# --------------------------------------------------------------------------- #

def load(data: bytes) -> Plugin:
    """Parse plugin ``data`` (bytes) into an :class:`esplib.plugin.Plugin`.

    Constructs a bare ``Plugin()`` (no path -> no auto file load, but the lock,
    FormID-fixup lists and index dicts are all initialized by ``__init__``),
    then drives esplib's own parse pipeline over the bytes:
    :meth:`~esplib.plugin.Plugin._parse_plugin` (which also parses the TES4
    header) -> ``_link_records`` -> ``_build_indexes``. Round-trips
    byte-identical: ``serialize(load(data)) == data`` for an unmodified plugin.

    Deliberately does NOT bind record schemas (no ``set_game`` /
    ``auto_detect_game``): with no schema esplib emits a modified record's
    subrecords in their raw list order, so :func:`insert_subrecord`'s explicit
    field positions survive. Binding a schema would re-sort subrecords by schema
    member order and silently override deliberate placement -- so callers must
    not run ``set_game`` / ``auto_detect_game`` on a plugin from ``load``.
    """
    plug = Plugin()
    plug.groups.clear()
    plug.records.clear()
    plug._parse_plugin(BinaryReader(data))
    plug._link_records()
    plug._build_indexes()
    plug.modified = False
    return plug


def serialize(plug: Plugin) -> bytes:
    """Serialize ``plug`` back to bytes (esplib refreshes HEDR.num_records)."""
    return plug.to_bytes()


# --------------------------------------------------------------------------- #
# FormID minting + tree walking
# --------------------------------------------------------------------------- #

def _plugin_index(plug: Plugin) -> int:
    """The plugin's own load index == its master count (spec 1.7)."""
    return len(plug.header.masters)


def mint_form_id(plug: Plugin) -> int:
    """Reserve one fresh, fully-resolved local FormID.

    Returns ``(pluginIndex << 24) | nextObjectID`` and bumps
    ``header.next_object_id``. Unlike :meth:`Plugin.get_next_form_id` the index
    is the real one (not the 0xFF save-time sentinel), so the id is safe to
    splice anywhere in the tree without relying on ``add_record`` finalization.

    Full-plugin only: ``pluginIndex == master count`` is correct for a full
    ``.esp``/``.esm`` (whole-byte file index). It is WRONG for an ESL / light
    master (on-disk file index 0xFE + 12-bit ESL index + 12-bit object index) --
    do not mint into an ESL-flagged plugin with this. Raises if the 24-bit
    object-id space is exhausted rather than silently wrapping the counter.
    """
    obj = plug.header.next_object_id
    if obj >= 0x00FFFFFF:
        raise OpsError(
            f"object-id space exhausted (next_object_id=0x{obj:06X}); "
            "cannot mint a new formID")
    plug.header.next_object_id = obj + 1
    return (_plugin_index(plug) << 24) | obj


def _iter_groups(container: List):
    """Yield every :class:`GroupRecord` in ``container`` depth-first."""
    for node in container:
        if isinstance(node, GroupRecord):
            yield node
            yield from _iter_groups(node.records)


def _iter_records(plug: Plugin):
    """Yield every :class:`Record` in the plugin tree (file order)."""
    def walk(container):
        for node in container:
            if isinstance(node, GroupRecord):
                yield from walk(node.records)
            else:
                yield node
    yield from walk(plug.groups)


def find_record(plug: Plugin, form_id: int) -> Optional[Record]:
    """Return the record whose formID == ``form_id`` (via esplib's index)."""
    return plug.get_record_by_form_id(form_id & 0xFFFFFFFF)


# --------------------------------------------------------------------------- #
# Subrecord (field) byte helpers
# --------------------------------------------------------------------------- #

def _sub(sig: str, data: bytes) -> SubRecord:
    return SubRecord(sig, data)


def _hexid(form_id: int) -> str:
    """A masked 8-hex-digit FormID string (``0x0001A2B3``)."""
    return f"0x{int(form_id) & 0xFFFFFFFF:08X}"


def _coerce_hexid(value) -> int:
    """Accept an int or a hex/decimal string FormID and return an int."""
    if isinstance(value, bool):
        return int(value) & 0xFFFFFFFF
    if isinstance(value, int):
        return value & 0xFFFFFFFF
    s = str(value).strip()
    return int(s, 16 if s.lower().startswith("0x") else 10) & 0xFFFFFFFF


def record_edid(rec: Record) -> Optional[str]:
    """Decoded EDID of ``rec`` or ``None``. Transparent to compression --
    esplib parses subrecords from the decompressed payload on load."""
    return rec.editor_id


def _name_base(rec: Record) -> Optional[int]:
    """The base-object formID from a placement's NAME (or None)."""
    nm = rec.get_subrecord("NAME")
    if nm is None or nm.size < 4:
        return None
    return nm.get_uint32(0)


def _decode_xlkr(sr: SubRecord) -> Tuple[int, int]:
    kw, ref = _XLKR.unpack(sr.data[:8])
    return kw, ref


# --------------------------------------------------------------------------- #
# CELL children GRUP location
# --------------------------------------------------------------------------- #

def _find_cell_children_group(plug: Plugin, cell_form_id: int) -> Optional[GroupRecord]:
    """The type-6 cell-children GRUP whose label == ``cell_form_id``.

    esplib parses a non-type-0 group's label as an ``int`` (the CELL formID), so
    we match on the integer label directly.
    """
    cell_form_id &= 0xFFFFFFFF
    for grp in _iter_groups(plug.groups):
        if grp.group_type == GT_CELL_CHILDREN and grp.label == cell_form_id:
            return grp
    return None


def _ensure_persistent_group(cell_children: GroupRecord,
                             cell_form_id: int) -> GroupRecord:
    """Return the type-8 persistent-children GRUP inside ``cell_children``,
    creating an empty one (label = CELL formID) if absent.

    A newly created type-8 group is inserted **before** any type-9 (temporary)
    group -- the canonical persistent-then-temporary ordering -- matching chim's
    original ``_ensure_persistent_group`` placement.
    """
    for child in cell_children.records:
        if (isinstance(child, GroupRecord)
                and child.group_type == GT_CELL_PERSISTENT_CHILDREN):
            return child
    grp = GroupRecord(GT_CELL_PERSISTENT_CHILDREN, cell_form_id & 0xFFFFFFFF)
    # Insert before the first temporary (type-9) or visible-distant (type-10)
    # group if one exists; otherwise append.
    insert_at = len(cell_children.records)
    for i, child in enumerate(cell_children.records):
        if (isinstance(child, GroupRecord)
                and child.group_type >= GT_CELL_TEMPORARY_CHILDREN):
            insert_at = i
            break
    cell_children.records.insert(insert_at, grp)
    return grp


# --------------------------------------------------------------------------- #
# build_refr
# --------------------------------------------------------------------------- #

def build_refr(form_id: int, base: int,
               pos: Sequence[float], rot: Sequence[float],
               flags: int = FLAG_PERSISTENT,
               xlkr: Optional[Sequence[Tuple[int, int]]] = None) -> Record:
    """Construct a standalone REFR :class:`Record` (not yet attached).

    Field order emitted: NAME, [XLKR ...], DATA -- a valid REFR layout (base,
    links, placement). ``pos``/``rot`` are packed as six float32 (never a
    double). ``flags`` defaults to 0x400 (Persistent) so the record belongs in a
    CELL's type-8 persistent-children GRUP.
    """
    if len(pos) != 3 or len(rot) != 3:
        raise OpsError("pos and rot must each have 3 components")
    rec = Record("REFR", FormID(form_id & 0xFFFFFFFF), int(flags))
    rec.version = 0x2C
    rec.subrecords.append(_sub("NAME", struct.pack("<I", base & 0xFFFFFFFF)))
    for kw, ref in (xlkr or []):
        rec.subrecords.append(
            _sub("XLKR", _XLKR.pack(kw & 0xFFFFFFFF, ref & 0xFFFFFFFF)))
    rec.subrecords.append(_sub("DATA", _POSROT.pack(
        float(pos[0]), float(pos[1]), float(pos[2]),
        float(rot[0]), float(rot[1]), float(rot[2]))))
    return rec


def insert_ref(plug: Plugin, cell_form_id: int, base: int,
               pos: Sequence[float], rot: Sequence[float],
               xlkr: Optional[Sequence[Tuple[int, int]]] = None,
               flags: int = FLAG_PERSISTENT) -> int:
    """Build a fresh REFR and splice it into a CELL's persistent (type-8) GRUP.

    Mints a real local formID (:func:`mint_form_id`), builds the REFR
    (:func:`build_refr`), locates the CELL's type-6 children GRUP, ensures a
    type-8 persistent-children GRUP exists inside it (creating one before any
    type-9 group if absent), and appends the REFR there. Returns the new formID.
    """
    cell_children = _find_cell_children_group(plug, cell_form_id)
    if cell_children is None:
        raise OpsError(
            f"no cell-children GRUP for CELL 0x{cell_form_id & 0xFFFFFFFF:08X}; "
            "the CELL must already exist with a children group")
    new_id = mint_form_id(plug)
    refr = build_refr(new_id, base, pos, rot, flags=flags, xlkr=xlkr)
    refr.plugin = plug
    persistent = _ensure_persistent_group(cell_children, cell_form_id)
    persistent.records.append(refr)
    # Keep the flat records list current, then let esplib rebuild ALL lookup
    # indexes (form-id, editor-id, signature) coherently in one pass.
    plug.records.append(refr)
    plug._build_indexes()
    plug.modified = True
    return new_id


# --------------------------------------------------------------------------- #
# clone_cluster
# --------------------------------------------------------------------------- #

class CloneResult:
    """Outcome of :func:`clone_cluster`."""

    def __init__(self, records: List[Record], id_map: Dict[int, int],
                 external_links: List[Tuple[int, int, int]]):
        self.records = records
        self.id_map = id_map
        self.external_links = external_links


def _translate_data(rec: Record, dx: float, dy: float, dz: float) -> None:
    """Add (dx,dy,dz) to a placement's DATA position (rotation untouched)."""
    data = rec.get_subrecord("DATA")
    if data is None or data.size < 24:
        return
    x, y, z, rx, ry, rz = _POSROT.unpack(data.data[:24])
    data.data = _POSROT.pack(x + dx, y + dy, z + dz, rx, ry, rz)


def _remap_and_strip(rec: Record, id_map: Dict[int, int],
                     external: List[Tuple[int, int, int]]) -> None:
    """Rewrite XLKR ref targets inside ``id_map``; strip XESP enable-parents.

    * XLKR.ref in id_map -> remapped. Otherwise reported in ``external`` and
      left verbatim. NAME (base) is intentionally NOT remapped.
    * XESP is removed entirely (a cloned cluster is independent). An XESP parent
      outside the set is reported before removal.
    """
    owner = rec.form_id.value
    kept: List[SubRecord] = []
    for sr in rec.subrecords:
        if sr.signature == "XLKR" and sr.size >= 8:
            kw, ref = _decode_xlkr(sr)
            if ref in id_map:
                sr.data = _XLKR.pack(kw, id_map[ref])
            elif ref != 0:
                external.append((owner, kw, ref))
            kept.append(sr)
        elif sr.signature == "XESP" and sr.size >= 8:
            parent, xflags = _XESP.unpack(sr.data[:8])
            if parent not in id_map and parent != 0:
                external.append((owner, xflags, parent))
            # strip: do not keep
        else:
            kept.append(sr)
    rec.subrecords = kept
    rec.modified = True


def clone_cluster(plug: Plugin, seed_form_id: int, count: int,
                  translate: Sequence[float],
                  into_cell: Optional[int] = None) -> CloneResult:
    """Clone record ``seed_form_id`` ``count`` times under fresh formIDs.

    For each clone: deep-copy the seed (esplib :meth:`Record.copy`, plaintext --
    a compressed seed clones uncompressed), assign a fresh formID, remap intra-
    set XLKR links, strip XESP, and translate DATA position by ``translate``
    (dx,dy,dz). Rotation is left intact. If ``into_cell`` (a CELL formID) is
    given, each clone is spliced into that CELL's type-8 persistent-children
    GRUP; otherwise the clones are created but left unattached.

    Returns a :class:`CloneResult` (records, old->new id_map, external_links).
    """
    if count < 1:
        raise OpsError("count must be >= 1")
    if len(translate) != 3:
        raise OpsError("translate must have 3 components (dx, dy, dz)")
    seed = find_record(plug, seed_form_id)
    if seed is None:
        raise OpsError(f"no seed record with formID 0x{seed_form_id & 0xFFFFFFFF:08X}")

    dx, dy, dz = (float(translate[0]), float(translate[1]), float(translate[2]))

    cell_children = None
    persistent = None
    if into_cell is not None:
        cell_children = _find_cell_children_group(plug, into_cell)
        if cell_children is None:
            raise OpsError(
                f"no cell-children GRUP for CELL 0x{into_cell & 0xFFFFFFFF:08X}")

    clones: List[Record] = []
    external: List[Tuple[int, int, int]] = []
    id_map: Dict[int, int] = {}

    for _ in range(count):
        new_id = mint_form_id(plug)
        clone = seed.copy()                 # plaintext deep copy
        clone.flags.Compressed = False      # store uncompressed
        clone.form_id = FormID(new_id)
        clone.plugin = plug
        # Per-clone remap: the seed's own formID collapses to this clone.
        per_clone = {seed_form_id & 0xFFFFFFFF: new_id}
        _remap_and_strip(clone, per_clone, external)
        _translate_data(clone, dx, dy, dz)
        id_map[seed_form_id & 0xFFFFFFFF] = new_id
        clones.append(clone)

    if into_cell is not None:
        persistent = _ensure_persistent_group(cell_children, into_cell)
        for clone in clones:
            persistent.records.append(clone)
            plug.records.append(clone)
        plug._build_indexes()

    plug.modified = True
    return CloneResult(clones, id_map, external)


# --------------------------------------------------------------------------- #
# clone_record
# --------------------------------------------------------------------------- #

def clone_record(plug: Plugin, seed_form_id: int) -> Tuple[Record, int]:
    """Clone one record under a fresh formID, into the seed's own container.

    Deep-copies **any** record (QUST, ACTI, base object, ...), mints a fresh
    formID, and inserts the clone immediately after the seed in the same
    ``records`` list -- so a top-level record's clone lands in the same top-level
    GRUP. Does NOT remap links or translate: use it to duplicate a record, then
    rewrite the clone's subrecords with the subrecord ops. Returns
    ``(clone, new_form_id)``.
    """
    # Locate the seed AND its containing records list so we can insert after it.
    location = _find_record_location(plug, seed_form_id)
    if location is None:
        raise OpsError(f"no seed record with formID 0x{seed_form_id & 0xFFFFFFFF:08X}")
    container, index, seed = location

    new_id = mint_form_id(plug)
    clone = seed.copy()
    clone.flags.Compressed = False
    clone.form_id = FormID(new_id)
    clone.plugin = plug

    container.insert(index + 1, clone)
    plug.records.append(clone)
    plug._build_indexes()
    plug.modified = True
    return clone, new_id


def _find_record_location(plug: Plugin, form_id: int):
    """Return ``(container_list, index, record)`` for ``form_id`` or None."""
    form_id &= 0xFFFFFFFF

    def walk(container):
        for i, node in enumerate(container):
            if isinstance(node, GroupRecord):
                hit = walk(node.records)
                if hit is not None:
                    return hit
            elif node.form_id.value == form_id:
                return (container, i, node)
        return None

    for grp in plug.groups:
        hit = walk(grp.records)
        if hit is not None:
            return hit
    return None


# --------------------------------------------------------------------------- #
# delete_records / delete_cells / move_ref / set_flag
# --------------------------------------------------------------------------- #

def delete_records(plug: Plugin, form_ids: Sequence[int]) -> List[int]:
    """Remove every record whose formID is in ``form_ids`` (any container).

    Empty GRUPs left behind are kept. Returns formIDs actually removed. HEDR
    num_records is recomputed by esplib on serialize.
    """
    wanted = set(int(f) & 0xFFFFFFFF for f in form_ids)
    if not wanted:
        return []
    removed: List[int] = []

    def prune(container):
        i = 0
        while i < len(container):
            node = container[i]
            if isinstance(node, Record) and node.form_id.value in wanted:
                removed.append(node.form_id.value)
                del container[i]
                continue
            if isinstance(node, GroupRecord):
                prune(node.records)
            i += 1

    for grp in plug.groups:
        prune(grp.records)

    if removed:
        _drop_from_indexes(plug, removed)
        plug.modified = True
    return removed


def delete_cells(plug: Plugin, cell_form_ids: Sequence[int]) -> List[Tuple[int, int]]:
    """Delete each CELL record AND its trailing type-6 children GRUP.

    Reverts a whole CELL override so the game falls back to the master's cell.
    Returns ``[(cell_formid, records_removed)]`` for the cells found (records
    counted = the CELL plus every record inside its children GRUP). Empty
    container GRUPs left behind are kept.
    """
    wanted = set(int(f) & 0xFFFFFFFF for f in cell_form_ids)
    if not wanted:
        return []
    removed: List[Tuple[int, int]] = []
    dropped_ids: List[int] = []

    def count_records(node) -> int:
        if isinstance(node, Record):
            return 1
        return sum(count_records(c) for c in node.records)

    def collect_ids(node, out: List[int]) -> None:
        if isinstance(node, Record):
            out.append(node.form_id.value)
        else:
            for c in node.records:
                collect_ids(c, out)

    def prune(container):
        i = 0
        while i < len(container):
            node = container[i]
            if (isinstance(node, Record) and node.signature == "CELL"
                    and node.form_id.value in wanted):
                n = 1
                end = i + 1
                collect_ids(node, dropped_ids)
                if (end < len(container)
                        and isinstance(container[end], GroupRecord)
                        and container[end].group_type == GT_CELL_CHILDREN):
                    n += count_records(container[end])
                    collect_ids(container[end], dropped_ids)
                    end += 1
                del container[i:end]
                removed.append((node.form_id.value, n))
                continue
            if isinstance(node, GroupRecord):
                prune(node.records)
            i += 1

    for grp in plug.groups:
        prune(grp.records)

    if dropped_ids:
        _drop_from_indexes(plug, dropped_ids)
        plug.modified = True
    return removed


def move_ref(plug: Plugin, form_id: int,
             pos: Sequence[float], rot: Sequence[float]) -> Record:
    """Overwrite a placement's DATA position/rotation (six float32).

    If the record has no DATA field, one is appended. Returns the record.
    """
    if len(pos) != 3 or len(rot) != 3:
        raise OpsError("pos and rot must each have 3 components")
    rec = find_record(plug, form_id)
    if rec is None:
        raise OpsError(f"no record with formID 0x{form_id & 0xFFFFFFFF:08X}")
    if rec.is_compressed:
        rec.flags.Compressed = False
    encoded = _POSROT.pack(
        float(pos[0]), float(pos[1]), float(pos[2]),
        float(rot[0]), float(rot[1]), float(rot[2]))
    data = rec.get_subrecord("DATA")
    if data is None:
        rec.add_subrecord("DATA", encoded)
    else:
        data.data = encoded
    rec.modified = True
    return rec


def set_flag(plug: Plugin, form_id: int, flag: int, clear: bool = False) -> Record:
    """Set (or clear) header ``flag`` bits on the record ``form_id``.

    Clearing 0x800 (InitiallyDisabled) force-enables a reference. Returns the
    record. A pure header edit -- no size / count change.
    """
    rec = find_record(plug, form_id)
    if rec is None:
        raise OpsError(f"no record with formID 0x{form_id & 0xFFFFFFFF:08X}")
    cur = int(rec.flags)
    new = (cur & ~flag) if clear else (cur | flag)
    # Re-decode through the record's own flag schema so bit semantics stick.
    from esplib.record import _make_flags
    rec.flags = _make_flags(rec.signature, new)
    rec.modified = True
    return rec


def _drop_from_indexes(plug: Plugin, form_ids: Sequence[int]) -> None:
    """Drop ``form_ids`` from the flat records list, then rebuild ALL lookup
    indexes (form-id, editor-id, signature) coherently via esplib's own
    ``_build_indexes`` so no stale entry survives in any index."""
    ids = set(int(f) & 0xFFFFFFFF for f in form_ids)
    plug.records = [r for r in plug.records if r.form_id.value not in ids]
    plug._build_indexes()


# --------------------------------------------------------------------------- #
# Subrecord (field) CRUD -- get / set / patch / insert / delete
# --------------------------------------------------------------------------- #

def _decompress_for_edit(rec: Record) -> None:
    """Ensure a record edits its plaintext subrecords stored uncompressed.

    esplib already parses ``rec.subrecords`` from the decompressed payload on
    load; we just clear the Compressed flag so ``to_bytes`` re-emits plaintext
    (matching chim's "store uncompressed, CK re-compresses" convention).
    """
    if rec.is_compressed:
        rec.flags.Compressed = False
        rec.modified = True


def _nth_subrecord(rec: Record, signature: str, index: int) -> Tuple[int, SubRecord]:
    """Absolute (list-position, SubRecord) of the ``index``-th ``signature``."""
    seen = 0
    for i, sr in enumerate(rec.subrecords):
        if sr.signature == signature:
            if seen == index:
                return i, sr
            seen += 1
    raise OpsError(
        f"no occurrence #{index} of subrecord {signature!r} (found {seen})")


def field_views(rec: Record, only: Optional[str] = None) -> List[dict]:
    """JSON-ready view of a record's subrecords with per-signature indices.

    ``index`` is the occurrence number of that signature over the WHOLE record,
    so it is the exact value the mutating subrecord ops expect back.
    """
    counters: Dict[str, int] = {}
    out: List[dict] = []
    for sr in rec.subrecords:
        idx = counters.get(sr.signature, 0)
        counters[sr.signature] = idx + 1
        if only is not None and sr.signature != only:
            continue
        out.append({
            "sig": sr.signature,
            "index": idx,
            "size": sr.size,
            "payload_hex": sr.data.hex(),
        })
    return out


def get_subrecords(rec: Record) -> List[SubRecord]:
    """All subrecords of ``rec`` (plaintext -- esplib decompressed on load)."""
    return list(rec.subrecords)


def set_subrecord(rec: Record, signature: str, payload: bytes,
                  index: int = 0) -> SubRecord:
    """Replace the ``index``-th ``signature`` subrecord's whole payload."""
    _decompress_for_edit(rec)
    _, sr = _nth_subrecord(rec, signature, index)
    sr.data = bytes(payload)
    rec.modified = True
    return sr


def patch_subrecord(rec: Record, signature: str, offset: int,
                    new_bytes: bytes, index: int = 0) -> SubRecord:
    """Overwrite ``new_bytes`` into the field at ``offset`` (length unchanged)."""
    _decompress_for_edit(rec)
    new_bytes = bytes(new_bytes)
    _, sr = _nth_subrecord(rec, signature, index)
    payload = bytearray(sr.data)
    if offset < 0 or offset + len(new_bytes) > len(payload):
        raise OpsError(
            f"patch range [{offset}, {offset + len(new_bytes)}) out of bounds "
            f"for {signature!r} payload of length {len(payload)}")
    payload[offset:offset + len(new_bytes)] = new_bytes
    sr.data = bytes(payload)
    rec.modified = True
    return sr


def insert_subrecord(rec: Record, signature: str, payload: bytes,
                     after: Optional[str] = None,
                     before: Optional[str] = None,
                     at_index: Optional[int] = None) -> int:
    """Insert a new ``signature`` subrecord. Returns the absolute index used.

    Position (at most one selector): ``after`` (after LAST occurrence),
    ``before`` (before FIRST occurrence), ``at_index`` (absolute), or none to
    append.
    """
    selectors = [x for x in (after, before, at_index) if x is not None]
    if len(selectors) > 1:
        raise OpsError("give at most one of after / before / at_index")
    _decompress_for_edit(rec)
    subs = rec.subrecords
    if at_index is not None:
        pos = int(at_index)
        if pos < 0 or pos > len(subs):
            raise OpsError(f"at_index {pos} out of range 0..{len(subs)}")
    elif after is not None:
        last = None
        for i, sr in enumerate(subs):
            if sr.signature == after:
                last = i
        if last is None:
            raise OpsError(f"no field {after!r} to insert after")
        pos = last + 1
    elif before is not None:
        pos = None
        for i, sr in enumerate(subs):
            if sr.signature == before:
                pos = i
                break
        if pos is None:
            raise OpsError(f"no field {before!r} to insert before")
    else:
        pos = len(subs)
    rec.insert_subrecord(pos, signature, bytes(payload))
    return pos


def delete_subrecord(rec: Record, signature: str, index: int = 0) -> SubRecord:
    """Remove the ``index``-th ``signature`` subrecord. Returns the removed one."""
    _decompress_for_edit(rec)
    pos, sr = _nth_subrecord(rec, signature, index)
    del rec.subrecords[pos]
    rec.modified = True
    return sr


# --------------------------------------------------------------------------- #
# VMAD (Papyrus script data) -- faithful decode / rebuild
# --------------------------------------------------------------------------- #
#
# A general read/author primitive over esplib's :mod:`esplib.vmad`. The record's
# signature drives fragment + QUST-alias parsing, so every codec entry point
# takes the record so ``VmadData.from_record`` / ``to_bytes`` see the right sig.
#
# Faithfulness: scripts + Object/scalar properties + STRUCT / *_ARRAY values +
# QUST alias-scripts all round-trip byte-identical. Fragment bodies are
# preserved *verbatim* as their esplib dataclass fields (including PERK's hidden
# ``_perk_extra`` and SCEN phase fragments), so a read->write of a record whose
# VMAD you did not touch reproduces the original bytes. What the JSON dict does
# NOT attempt is to let an agent *hand-author* an arbitrary QUST/PERK/SCEN
# fragment body field-by-field from scratch -- fragment_data is captured as a
# best-effort field dump for inspection + preservation, not a full authoring
# surface. Scripts, properties (incl. Object links) and alias-scripts ARE fully
# authorable from the dict.

#: Property type code -> stable human name (for the JSON ``type_name`` field).
_PROP_TYPE_NAMES = {
    PROP_OBJECT: "object",
    PROP_STRING: "string",
    PROP_INT32: "int32",
    PROP_FLOAT: "float",
    PROP_BOOL: "bool",
    PROP_STRUCT: "struct",
    PROP_OBJECT_ARRAY: "object_array",
    PROP_STRING_ARRAY: "string_array",
    PROP_INT32_ARRAY: "int32_array",
    PROP_FLOAT_ARRAY: "float_array",
    PROP_BOOL_ARRAY: "bool_array",
    PROP_STRUCT_ARRAY: "struct_array",
}


def _vmad_obj_to_dict(obj: VmadObject) -> dict:
    """Encode a :class:`VmadObject` value as JSON (formID as a hex string)."""
    return {
        "form_id": _hexid(obj.form_id),
        "alias": obj.alias,
        "unused": obj.unused,
    }


def _vmad_obj_from_dict(d: dict) -> VmadObject:
    return VmadObject(
        form_id=_coerce_hexid(d.get("form_id", 0)),
        alias=int(d.get("alias", -1)),
        unused=int(d.get("unused", 0)),
    )


def _vmad_prop_value_to_json(prop: VmadProperty):
    """Encode a property's value for JSON per its declared ``prop.type``.

    * OBJECT -> ``{form_id, alias, unused}``
    * STRUCT -> nested list of member-property dicts
    * OBJECT_ARRAY -> list of object dicts; STRUCT_ARRAY -> list of member lists
    * other scalars / arrays -> verbatim (str/int/float/bool or list thereof)
    """
    t = prop.type
    v = prop.value
    if t == PROP_OBJECT:
        return _vmad_obj_to_dict(v)
    if t == PROP_OBJECT_ARRAY:
        return [_vmad_obj_to_dict(o) for o in (v or [])]
    if t == PROP_STRUCT:
        return [_vmad_prop_to_dict(m) for m in (v or [])]
    if t == PROP_STRUCT_ARRAY:
        return [[_vmad_prop_to_dict(m) for m in members] for members in (v or [])]
    return v


def _vmad_prop_to_dict(prop: VmadProperty) -> dict:
    return {
        "name": prop.name,
        "type": prop.type,
        "type_name": _PROP_TYPE_NAMES.get(prop.type, f"type_{prop.type}"),
        "flags": prop.flags,
        "value": _vmad_prop_value_to_json(prop),
    }


def _vmad_prop_value_from_json(prop_type: int, value):
    if prop_type == PROP_OBJECT:
        return _vmad_obj_from_dict(value)
    if prop_type == PROP_OBJECT_ARRAY:
        return [_vmad_obj_from_dict(o) for o in (value or [])]
    if prop_type == PROP_STRUCT:
        return [_vmad_prop_from_dict(m) for m in (value or [])]
    if prop_type == PROP_STRUCT_ARRAY:
        return [[_vmad_prop_from_dict(m) for m in members] for members in (value or [])]
    return value


def _vmad_prop_from_dict(d: dict) -> VmadProperty:
    ptype = int(d["type"])
    return VmadProperty(
        name=d.get("name", ""),
        type=ptype,
        flags=int(d.get("flags", 1)),
        value=_vmad_prop_value_from_json(ptype, d.get("value")),
    )


def _vmad_script_to_dict(script: VmadScript) -> dict:
    return {
        "name": script.name,
        "flags": script.flags,
        "properties": [_vmad_prop_to_dict(p) for p in script.properties],
    }


def _vmad_script_from_dict(d: dict) -> VmadScript:
    return VmadScript(
        name=d.get("name", ""),
        flags=int(d.get("flags", 0)),
        properties=[_vmad_prop_from_dict(p) for p in d.get("properties", [])],
    )


def _vmad_fragment_data_to_dict(fd: Optional[VmadFragmentData]) -> Optional[dict]:
    """Verbatim field dump of a :class:`VmadFragmentData` (or ``None``).

    Dumps the dataclass' own fields plus each fragment's fields (including the
    hidden PERK ``_perk_extra`` attr) so a read->write reproduces the bytes.
    FormIDs never appear in fragment bodies, so all values stay verbatim.
    """
    if fd is None:
        return None

    def _frag(frag) -> dict:
        out = {k: v for k, v in vars(frag).items()}
        extra = getattr(frag, "_perk_extra", None)
        if extra is not None:
            out["_perk_extra"] = list(extra)
        return out

    def _phase(pf) -> dict:
        return {k: v for k, v in vars(pf).items()}

    return {
        "extra_bind_version": fd.extra_bind_version,
        "filename": fd.filename,
        "fragment_count": fd.fragment_count,
        "flags": fd.flags,
        "fragments": [_frag(f) for f in fd.fragments],
        "phase_fragments": [_phase(p) for p in fd.phase_fragments],
    }


def _vmad_fragment_data_from_dict(d: Optional[dict]) -> Optional[VmadFragmentData]:
    """Rebuild a :class:`VmadFragmentData` from its verbatim field dump.

    Reconstructs the concrete fragment dataclass per-record-signature is NOT
    needed for byte-identity of scripts + alias-scripts (a null/empty fragment
    block); we rebuild whatever fragment dicts are present using duck-typed
    :class:`~esplib.vmad.VmadFragment` / ``VmadQuestFragment`` / phase fragments
    so a captured fragment body round-trips. See module note on faithfulness.
    """
    if d is None:
        return None
    fd = VmadFragmentData(
        extra_bind_version=int(d.get("extra_bind_version", 2)),
        filename=d.get("filename", ""),
        fragment_count=int(d.get("fragment_count", 0)),
        flags=int(d.get("flags", 0)),
    )
    from esplib.vmad import (VmadFragment, VmadQuestFragment,
                             VmadScenePhaseFragment)
    for fdict in d.get("fragments", []):
        extra = fdict.get("_perk_extra")
        # A QUST fragment carries quest_stage; otherwise a basic/PERK fragment.
        if "quest_stage" in fdict:
            frag = VmadQuestFragment()
        else:
            frag = VmadFragment()
        for k, v in fdict.items():
            if k == "_perk_extra":
                continue
            setattr(frag, k, v)
        if extra is not None:
            frag._perk_extra = tuple(extra)
        fd.fragments.append(frag)
    for pdict in d.get("phase_fragments", []):
        pf = VmadScenePhaseFragment()
        for k, v in pdict.items():
            setattr(pf, k, v)
        fd.phase_fragments.append(pf)
    return fd


def _vmad_alias_to_dict(alias: VmadAliasScripts) -> dict:
    return {
        "alias_obj": _vmad_obj_to_dict(alias.alias_obj),
        "version": alias.version,
        "obj_format": alias.obj_format,
        "scripts": [_vmad_script_to_dict(s) for s in alias.scripts],
    }


def _vmad_alias_from_dict(d: dict) -> VmadAliasScripts:
    return VmadAliasScripts(
        alias_obj=_vmad_obj_from_dict(d.get("alias_obj", {})),
        version=int(d.get("version", 5)),
        obj_format=int(d.get("obj_format", 2)),
        scripts=[_vmad_script_from_dict(s) for s in d.get("scripts", [])],
    )


def vmad_to_dict(rec: Record) -> Optional[dict]:
    """Faithful, JSON-able decode of a record's VMAD subrecord (or ``None``).

    Returns ``None`` if the record has no VMAD. The record's signature drives
    fragment + QUST-alias parsing (via :meth:`VmadData.from_record`). Object
    property/alias-object values become ``{form_id: "0x...", alias, unused}``;
    STRUCT values become a nested property list; ``*_ARRAY`` become lists;
    scalars are verbatim. FormIDs are hex strings.

    Round-trips: ``vmad_from_dict(rec, vmad_to_dict(rec))`` then serialize
    reproduces the original VMAD bytes for scripts + Object/scalar properties +
    QUST alias-scripts (fragment bodies preserved verbatim).
    """
    v = VmadData.from_record(rec)
    if v is None:
        return None
    return {
        "version": v.version,
        "obj_format": v.obj_format,
        "scripts": [_vmad_script_to_dict(s) for s in v.scripts],
        "fragment_data": _vmad_fragment_data_to_dict(v.fragment_data),
        "alias_scripts": [_vmad_alias_to_dict(a) for a in v.alias_scripts],
    }


def vmad_from_dict(rec: Record, d: dict) -> None:
    """Rebuild a :class:`VmadData` from ``d`` and write it onto ``rec``'s VMAD.

    Creates the VMAD subrecord if absent, otherwise overwrites its payload.
    Setting ``sr.data`` flips ``sr.modified`` so a compressed record transparently
    recompresses on serialize -- no manual decompress needed.

    CRITICAL: if ``alias_scripts`` is present, a non-``None`` ``fragment_data``
    MUST precede it on the wire (esplib only emits the alias block when
    ``fragment_data`` is set), so when the dict's ``fragment_data`` is null but
    alias scripts exist we synthesize an empty
    ``VmadFragmentData(extra_bind_version=2, filename='', fragment_count=0)``.
    """
    v = VmadData(
        version=int(d.get("version", 5)),
        obj_format=int(d.get("obj_format", 2)),
    )
    v.scripts = [_vmad_script_from_dict(s) for s in d.get("scripts", [])]
    v.fragment_data = _vmad_fragment_data_from_dict(d.get("fragment_data"))
    v.alias_scripts = [_vmad_alias_from_dict(a) for a in d.get("alias_scripts", [])]

    # esplib serializes fragment_data (and, for QUST, the trailing alias block)
    # only when fragment_data is not None. If the caller supplied alias scripts
    # without a fragment block, synthesize an empty one so the aliases survive.
    if v.alias_scripts and v.fragment_data is None:
        v.fragment_data = VmadFragmentData(
            extra_bind_version=2, filename="", fragment_count=0)

    payload = v.to_bytes(rec.signature)
    sr = rec.get_subrecord("VMAD")
    if sr is None:
        rec.add_subrecord("VMAD", payload)
    else:
        sr.data = payload
    rec.modified = True


# --------------------------------------------------------------------------- #
# Masters (header master-list primitives)
# --------------------------------------------------------------------------- #

def add_master(plug: Plugin, name: str, size: int = 0) -> List[str]:
    """Add master ``name`` to the plugin header (idempotent, case-insensitive).

    esplib's :meth:`Plugin.add_master` appends to ``header.masters`` /
    ``header.master_sizes`` but, on a *loaded* plugin, its
    :meth:`PluginHeader.to_record` reuses the cached ``_raw_record`` and never
    re-emits MAST/DATA from ``header.masters`` -- so the new master would not
    reach the serialized bytes. We therefore also append the MAST + DATA
    subrecords onto ``_raw_record`` directly. Adding a master does NOT renumber
    any existing formIDs. Returns the resulting master list.
    """
    existing = {m.lower() for m in plug.header.masters}
    if name.lower() in existing:
        return list(plug.header.masters)
    plug.add_master(name)
    # add_master silently ignores a self-add; guard against a no-op divergence.
    if name.lower() not in {m.lower() for m in plug.header.masters}:
        raise OpsError(f"refused to add master {name!r} (self-reference?)")
    rr = plug.header._raw_record
    rr.add_subrecord("MAST", name.encode("cp1252") + b"\x00")
    rr.add_subrecord("DATA", struct.pack("<Q", int(size) & 0xFFFFFFFFFFFFFFFF))
    plug.modified = True
    return list(plug.header.masters)


def rename_master(plug: Plugin, old: str, new: str) -> List[str]:
    """Rename master ``old`` to ``new`` in the header (touches no formIDs).

    Rewrites the matching MAST subrecord on the header's ``_raw_record`` AND the
    ``header.masters`` list entry. Raises :class:`OpsError` if ``old`` is not a
    current master (case-insensitive). Returns the resulting master list.
    """
    lowered = [m.lower() for m in plug.header.masters]
    if old.lower() not in lowered:
        raise OpsError(f"master {old!r} not found in {plug.header.masters!r}")
    idx = lowered.index(old.lower())
    plug.header.masters[idx] = new
    rr = plug.header._raw_record
    for sr in rr.subrecords:
        if sr.signature == "MAST" and sr.get_string().lower() == old.lower():
            sr.set_string(new)
            break
    plug.modified = True
    return list(plug.header.masters)


# --------------------------------------------------------------------------- #
# Query helpers (for esp_query)
# --------------------------------------------------------------------------- #

def find_by_edid(plug: Plugin, pattern: str) -> List[Record]:
    """All records whose EDID matches the regex ``pattern`` (search)."""
    rx = re.compile(pattern)
    out: List[Record] = []
    for rec in _iter_records(plug):
        edid = rec.editor_id
        if edid is not None and rx.search(edid) is not None:
            out.append(rec)
    return out


def find_by_base(plug: Plugin, base_form_id: int) -> List[Record]:
    """All placement records instantiating base object ``base_form_id``."""
    base_form_id &= 0xFFFFFFFF
    out: List[Record] = []
    for rec in _iter_records(plug):
        if rec.signature not in PLACEMENT_SIGS:
            continue
        if _name_base(rec) == base_form_id:
            out.append(rec)
    return out


def find_by_cell(plug: Plugin, key) -> Optional[Record]:
    """The CELL record identified by an int formID or a str EDID."""
    want_formid = isinstance(key, int) and not isinstance(key, bool)
    for rec in _iter_records(plug):
        if rec.signature != "CELL":
            continue
        if want_formid:
            if rec.form_id.value == (key & 0xFFFFFFFF):
                return rec
        elif rec.editor_id == key:
            return rec
    return None


def cell_refs(plug: Plugin, cell_form_id: int) -> List[Record]:
    """Every placement record inside CELL ``cell_form_id`` (persistent + temp)."""
    grp = _find_cell_children_group(plug, cell_form_id)
    if grp is None:
        return []
    out: List[Record] = []

    def walk(container):
        for node in container:
            if isinstance(node, GroupRecord):
                walk(node.records)
            elif node.signature in PLACEMENT_SIGS:
                out.append(node)

    walk(grp.records)
    return out


def cell_ref_view(rec: Record) -> dict:
    """Per-ref view for ``esp_query by='cellrefs'``: base / flags / xlkr."""
    base = _name_base(rec)
    xlkr = []
    has_xesp = False
    for sr in rec.subrecords:
        if sr.signature == "XLKR" and sr.size >= 8:
            kw, ref = _decode_xlkr(sr)
            xlkr.append((kw, ref))
        elif sr.signature == "XESP":
            has_xesp = True
    flags = int(rec.flags)
    return {
        "form_id": rec.form_id.value,
        "sig": rec.signature,
        "base": base,
        "flags": flags,
        "edid": rec.editor_id,
        "persistent": bool(flags & FLAG_PERSISTENT),
        "initially_disabled": bool(flags & FLAG_INITIALLY_DISABLED),
        "has_xesp": has_xesp,
        "xlkr": xlkr,
    }


# (edid, sig, mesh, obnd)
BaseInfo = Tuple[Optional[str], str, Optional[str],
                 Optional[Tuple[int, int, int, int, int, int]]]


def base_map(plug: Plugin, sigs) -> Dict[int, BaseInfo]:
    """Map ``formid -> (edid, sig, mesh, obnd)`` for records of the given sigs."""
    wanted = frozenset(sigs)
    out: Dict[int, BaseInfo] = {}
    for rec in _iter_records(plug):
        if rec.signature not in wanted:
            continue
        edid = rec.editor_id
        modl = rec.get_subrecord("MODL")
        mesh = (modl.data.split(b"\x00", 1)[0].decode("ascii", "replace")
                if modl is not None and modl.size else None)
        obnd_sr = rec.get_subrecord("OBND")
        obnd = (_OBND.unpack(obnd_sr.data[:12])
                if obnd_sr is not None and obnd_sr.size >= 12 else None)
        out[rec.form_id.value] = (edid, rec.signature, mesh, obnd)
    return out
