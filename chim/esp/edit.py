"""High-level, walk-clean mutations on parsed :class:`~chim.esp.records.Plugin`
trees.

Everything in this module is built on *only* the three foundation modules:
:mod:`chim.esp.records`, :mod:`chim.esp.fields`, and
:mod:`chim.esp.compression`. No new on-disk parsing is introduced.

Why you don't see manual ``groupSize`` arithmetic
--------------------------------------------------
The container model re-derives sizes from live payload on serialize:

* ``Record.byte_size``  == ``24 + len(record.data)``  (dataSize re-synced by
  :meth:`Record.to_bytes`).
* ``Group.group_size``  == ``24 + sum(child.byte_size ...)`` (groupSize
  re-synced by :meth:`Group.to_bytes`).

So the guardrail "add the byte delta to this GRUP's groupSize **and to every
ancestor GRUP's groupSize**" is satisfied *structurally*: because a group's size
is a pure function of its (transitive) children, mutating a deeply-nested
``Record.data`` or a group's ``children`` list automatically re-flows the delta
up through **every** ancestor when the tree is serialized. Hand-patching the
stored size fields would instead *fight* that re-derivation and produce a dirty
file. To keep the invariant honest rather than incidental, every mutator here:

1. Locates the target via an explicit ancestor *path* (root -> ... -> node), so
   the set of GRUPs whose size changes is known and auditable.
2. Decompresses compressed records before touching subrecords and re-stores
   them uncompressed (clears ``0x40000``; dataSize follows ``len(data)``).
3. Keeps ``HEDR.num_records`` / ``HEDR.next_object_id`` in sync by hand -- those
   live in the TES4 payload and are the *only* sizes the model will not derive.
4. Asserts ``walk_clean(serialize(plugin))`` before returning, so a caller can
   never receive a tree that would round-trip dirty.

All floats are packed as 32-bit ``<f`` (via the ``DATA`` posrot codec); an
8-byte double is never emitted. Size arithmetic is always parenthesized.

Public API
----------
``set_flag`` / ``clear_flag``       toggle a record header flag (clear 0x800 =
                                    force-enable a reference).
``move_ref``                        overwrite a placement's DATA pos/rot.
``build_refr``                      construct a fresh REFR :class:`Record`.
``insert_records``                  splice records into a CELL's persistent
                                    (type-8) children GRUP.
``clone_cluster``                   deep-copy a set of records under fresh
                                    formIDs, remap intra-set links, translate
                                    positions, strip enable-parents.
``clone_record``                    deep-copy one record (QUST/ACTI/base) under a
                                    fresh formID into its own top-level GRUP.
``delete_records``                  remove a non-contiguous set of records.
``swap_model``                      copy OBND/MODL/MODT from a donor base onto a
                                    target base.
"""

from __future__ import annotations

import copy
import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .records import (
    Group,
    Plugin,
    Record,
    serialize,
    walk_clean,
    FLAG_COMPRESSED,
    FLAG_INITIALLY_DISABLED,
    GT_CELL_CHILDREN,
    GT_CELL_PERSISTENT_CHILDREN,
    GROUP_HEADER_SIZE,
    RECORD_HEADER_SIZE,
    TES4_SIGNATURE,
)
from . import fields
from .compression import decompress_record, store_uncompressed


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class EditError(ValueError):
    """A mutation could not be performed as requested."""


# --------------------------------------------------------------------------- #
# Internal helpers: ancestor paths, HEDR, plaintext field access
# --------------------------------------------------------------------------- #

# A "path" is the ordered chain of GRUP ancestors from the outermost top-level
# group down to (but not including) the target node, paired with the container
# list the node lives in and its index inside that list. Because every
# ``Group`` in ``ancestors`` transitively contains the target, editing the
# target re-derives the size of *every* group in this list on serialize -- this
# is the concrete manifestation of guardrail #1.
@dataclass
class _Location:
    ancestors: List[Group]          # outermost -> innermost GRUP chain
    container: List                 # the list the node is stored in
    index: int                      # node's position within ``container``

    @property
    def node(self):
        return self.container[self.index]


def _find_record(plugin: Plugin, form_id: int) -> Optional[_Location]:
    """Depth-first search for the (first) record whose formID matches.

    Returns a :class:`_Location` carrying the full GRUP ancestor chain so the
    caller knows exactly which groups' sizes a size-changing edit will re-flow
    through.
    """
    def walk(container: List, ancestors: List[Group]) -> Optional[_Location]:
        for i, node in enumerate(container):
            if isinstance(node, Record):
                if node.form_id == form_id:
                    return _Location(list(ancestors), container, i)
            else:  # Group
                hit = walk(node.children, ancestors + [node])
                if hit is not None:
                    return hit
        return None

    return walk(plugin.top_level, [])


def _plaintext_fields(record: Record) -> List[fields.Field]:
    """Return the parsed *plaintext* field list for ``record``.

    Guardrail #4: a compressed record is decompressed first (the raw blob is
    never parsed as fields).
    """
    plain = decompress_record(record)
    return fields.parse_fields(plain)


def _store_fields(record: Record, field_list: List[fields.Field]) -> None:
    """Re-pack ``field_list`` into ``record.data`` uncompressed.

    Guardrail #4: if the record was compressed we clear ``0x40000`` and store
    the plaintext directly. dataSize is re-derived by :meth:`Record.to_bytes`.
    Guardrail #5: field order is preserved because we mutated the list in place;
    unknown subrecords ride along verbatim as raw :class:`fields.Field` values.
    """
    if record.is_compressed:
        # Drop the compressed representation; we're rewriting plaintext anyway.
        record.flags = record.flags & ~FLAG_COMPRESSED
    record.data = fields.pack_fields(field_list)


def _tes4(plugin: Plugin) -> Record:
    tes4 = plugin.tes4
    if tes4 is None or tes4.signature != TES4_SIGNATURE:
        raise EditError("plugin has no TES4 header record")
    return tes4


def _read_hedr(plugin: Plugin) -> Tuple[List[fields.Field], fields.Field, fields.Hedr]:
    tes4 = _tes4(plugin)
    # TES4 is not expected to be compressed, but honor the flag if set.
    fl = _plaintext_fields(tes4)
    hedr_field = fields.find_field(fl, b"HEDR")
    if hedr_field is None:
        raise EditError("TES4 has no HEDR subrecord")
    return fl, hedr_field, fields.decode_hedr(hedr_field.payload)


def _write_hedr(plugin: Plugin, fl: List[fields.Field],
                hedr_field: fields.Field, hedr: fields.Hedr) -> None:
    hedr_field.payload = fields.encode_hedr(hedr)
    _store_fields(_tes4(plugin), fl)


def _plugin_index(plugin: Plugin) -> int:
    """The plugin's own load index == its number of masters (spec 1.7).

    Counts ``MAST`` subrecords in TES4; a master-less plugin is index 0x00.
    """
    tes4 = _tes4(plugin)
    fl = _plaintext_fields(tes4)
    return sum(1 for f in fl if f.signature == b"MAST")


def _alloc_form_ids(plugin: Plugin, count: int) -> Tuple[List[int], List[fields.Field],
                                                         fields.Field, fields.Hedr]:
    """Reserve ``count`` fresh formIDs from HEDR.nextObjectID.

    Guardrail #2: each new formID is ``(pluginIndex << 24) | nextObjectID`` with
    ``nextObjectID`` bumped once per allocation. Returns the ids plus the
    (not-yet-written) HEDR handles so the caller can also apply a num_records
    delta in the same write.
    """
    fl, hedr_field, hedr = _read_hedr(plugin)
    idx = _plugin_index(plugin)
    ids: List[int] = []
    for _ in range(count):
        obj = hedr.next_object_id & 0x00FFFFFF
        ids.append((idx << 24) | obj)
        hedr.next_object_id = obj + 1
    return ids, fl, hedr_field, hedr


def _assert_clean(plugin: Plugin) -> None:
    """Guardrail: end every mutation walk-clean."""
    out = serialize(plugin)
    if not walk_clean(out):
        raise EditError("mutation left the plugin not walk-clean")


# --------------------------------------------------------------------------- #
# 1. Flag mutation (no size change; HEDR untouched)
# --------------------------------------------------------------------------- #

def set_flag(plugin: Plugin, form_id: int, flag: int) -> Record:
    """Set ``flag`` in the header of the record ``form_id``.

    Pure header edit: dataSize/groupSize are unchanged, HEDR is unchanged.
    Returns the affected record.
    """
    loc = _find_record(plugin, form_id)
    if loc is None:
        raise EditError(f"no record with formID 0x{form_id:08X}")
    rec: Record = loc.node
    rec.flags = rec.flags | flag
    _assert_clean(plugin)
    return rec


def clear_flag(plugin: Plugin, form_id: int, flag: int) -> Record:
    """Clear ``flag`` in the header of the record ``form_id``.

    Clearing ``0x800`` (:data:`FLAG_INITIALLY_DISABLED`) force-enables a
    reference that started disabled -- the canonical use.
    Returns the affected record.
    """
    loc = _find_record(plugin, form_id)
    if loc is None:
        raise EditError(f"no record with formID 0x{form_id:08X}")
    rec: Record = loc.node
    rec.flags = rec.flags & ~flag
    _assert_clean(plugin)
    return rec


# --------------------------------------------------------------------------- #
# 2. Move a placement (DATA pos/rot rewrite; float32 only)
# --------------------------------------------------------------------------- #

def move_ref(plugin: Plugin, form_id: int,
             pos: Sequence[float], rot: Sequence[float]) -> Record:
    """Overwrite the ``DATA`` pos/rot of placement ``form_id``.

    ``pos`` and ``rot`` are each 3-tuples of floats. Guardrail #3: values are
    packed via the ``DATA`` codec as six 32-bit floats (``<6f``) -- never an
    8-byte double.

    Changing DATA to the *same* 24-byte length is size-neutral; if the record
    previously had no DATA field we append one (a real size change, which
    re-flows through every ancestor GRUP automatically on serialize).
    """
    if len(pos) != 3 or len(rot) != 3:
        raise EditError("pos and rot must each have 3 components")
    loc = _find_record(plugin, form_id)
    if loc is None:
        raise EditError(f"no record with formID 0x{form_id:08X}")
    rec: Record = loc.node

    field_list = _plaintext_fields(rec)
    posrot = fields.PosRot(
        float(pos[0]), float(pos[1]), float(pos[2]),
        float(rot[0]), float(rot[1]), float(rot[2]),
    )
    encoded = fields.encode_data_posrot(posrot)   # struct.pack('<6f', ...)
    data_field = fields.find_field(field_list, b"DATA")
    if data_field is None:
        field_list.append(fields.Field(b"DATA", encoded))
    else:
        data_field.payload = encoded
    _store_fields(rec, field_list)
    _assert_clean(plugin)
    return rec


# --------------------------------------------------------------------------- #
# 3. Build a fresh REFR record (not yet attached to any tree)
# --------------------------------------------------------------------------- #

def _new_record_header(signature: bytes, data_size: int, flags: int,
                       form_id: int) -> bytes:
    """A 24-byte record header. dataSize is advisory here -- it is re-synced
    from ``data`` on serialize -- but we set it correctly for tidiness."""
    from .records import _REC_HDR_TAIL  # local import: internal struct
    return signature + _REC_HDR_TAIL.pack(
        data_size,      # dataSize (re-derived on to_bytes anyway)
        flags,          # flags
        form_id,        # formID
        0x00000000,     # timestamp + version control
        0x002C,         # internal version
        0x0000,         # unknown
    )


def build_refr(form_id: int, base: int,
               pos: Sequence[float], rot: Sequence[float],
               flags: int = 0x400,
               xlkr: Optional[Sequence[Tuple[int, int]]] = None,
               meta: Optional[Sequence[fields.Field]] = None) -> Record:
    """Construct a standalone persistent ``REFR`` record.

    Parameters
    ----------
    form_id : int
        The new reference's formID. (Callers that want this allocated from
        HEDR should use :func:`clone_cluster` / :func:`insert_records`, which
        handle nextObjectID; ``build_refr`` is the raw constructor.)
    base : int
        The base object's formID -> ``NAME``.
    pos, rot : 3-tuples of float
        -> ``DATA`` as six 32-bit floats (guardrail #3).
    flags : int
        Header flags; defaults to ``0x400`` (Persistent) so the record belongs
        in a type-8 persistent-children GRUP.
    xlkr : sequence of (keyword_formID, ref_formID)
        Zero or more ``XLKR`` linked references.
    meta : sequence of extra :class:`fields.Field`
        Additional subrecords appended verbatim after the standard ones
        (order preserved, guardrail #5).

    Field order emitted: NAME, [XLKR ...], DATA, [meta ...] -- a valid REFR
    layout (base, links, placement, extras).
    """
    if len(pos) != 3 or len(rot) != 3:
        raise EditError("pos and rot must each have 3 components")

    field_list: List[fields.Field] = [
        fields.Field(b"NAME", fields.encode_name(base)),
    ]
    for kw, ref in (xlkr or []):
        field_list.append(
            fields.Field(b"XLKR", fields.encode_xlkr(
                fields.LinkedRef(keyword_form_id=kw, ref_form_id=ref)))
        )
    posrot = fields.PosRot(
        float(pos[0]), float(pos[1]), float(pos[2]),
        float(rot[0]), float(rot[1]), float(rot[2]),
    )
    field_list.append(fields.Field(b"DATA", fields.encode_data_posrot(posrot)))
    for extra in (meta or []):
        # copy so callers can't alias our internal payloads
        field_list.append(fields.Field(extra.signature, extra.payload,
                                       extra.used_xxxx))

    data = fields.pack_fields(field_list)
    header = _new_record_header(b"REFR", len(data), flags, form_id)
    return Record(signature=b"REFR", header=header, data=data)


# --------------------------------------------------------------------------- #
# 4. Insert records into a CELL's persistent (type-8) children GRUP
# --------------------------------------------------------------------------- #

def _find_cell_children_group(plugin: Plugin, cell_form_id: int) -> Optional[Group]:
    """Return the type-6 cell-children GRUP whose label == ``cell_form_id``."""
    label = struct.pack("<I", cell_form_id & 0xFFFFFFFF)
    for node in _iter_groups(plugin.top_level):
        if node.group_type == GT_CELL_CHILDREN and node.label == label:
            return node
    return None


def _iter_groups(container: List):
    for node in container:
        if isinstance(node, Group):
            yield node
            yield from _iter_groups(node.children)


def _ensure_persistent_group(cell_children: Group,
                             cell_form_id: int) -> Tuple[Group, bool]:
    """Return ``(group, created)`` for the type-8 persistent-children GRUP inside
    ``cell_children``, creating an empty one (label = CELL formID) if absent.

    Adding a brand-new GRUP is a byte-length change; because the new group is
    appended to ``cell_children.children`` its 24-byte header + body re-flow
    through every ancestor's groupSize on serialize (guardrail #1). It also adds
    one node to the tree -> HEDR.num_records must be bumped by the caller.
    """
    for child in cell_children.children:
        if (isinstance(child, Group)
                and child.group_type == GT_CELL_PERSISTENT_CHILDREN):
            return child, False
    label = struct.pack("<I", cell_form_id & 0xFFFFFFFF)
    from .records import _GRP_HDR_TAIL  # internal struct
    header = (b"GRUP"
              + _GRP_HDR_TAIL.pack(GROUP_HEADER_SIZE, label,
                                   GT_CELL_PERSISTENT_CHILDREN, 0x00000000,
                                   0x0000)
              + struct.pack("<H", 0))
    grp = Group(header=header, children=[])
    cell_children.children.append(grp)
    return grp, True


def insert_records(plugin: Plugin, cell_form_id: int,
                   records: Sequence[Record]) -> Group:
    """Splice ``records`` into the persistent-children (type-8) GRUP of the CELL
    ``cell_form_id``.

    Guardrail #1: appending to the type-8 group's ``children`` grows that GRUP;
    the delta re-flows through the type-6 cell-children GRUP and every ancestor
    block/sub-block/top GRUP automatically on serialize -- the whole path from
    the top ``CELL`` GRUP down is re-derived.
    Guardrail #2: HEDR.num_records is bumped by the number of nodes added
    (records inserted, plus one if a type-8 GRUP had to be created).

    Returns the type-8 group the records now live in.
    """
    records = list(records)
    if not records:
        raise EditError("no records to insert")
    for r in records:
        if not isinstance(r, Record):
            raise EditError("insert_records only accepts Record instances")

    cell_children = _find_cell_children_group(plugin, cell_form_id)
    if cell_children is None:
        raise EditError(
            f"no cell-children GRUP for CELL 0x{cell_form_id:08X}; "
            "the CELL must already exist with a children group"
        )
    persistent, created = _ensure_persistent_group(cell_children, cell_form_id)

    persistent.children.extend(records)

    # HEDR bookkeeping: added len(records) records (+1 if we created the GRUP).
    added_nodes = len(records) + (1 if created else 0)
    fl, hedr_field, hedr = _read_hedr(plugin)
    hedr.num_records = hedr.num_records + added_nodes
    _write_hedr(plugin, fl, hedr_field, hedr)

    _assert_clean(plugin)
    return persistent


# --------------------------------------------------------------------------- #
# 5. Clone a cluster of records under fresh formIDs
# --------------------------------------------------------------------------- #

@dataclass
class CloneResult:
    """Outcome of :func:`clone_cluster`."""
    records: List[Record]                 # the freshly-cloned records
    id_map: Dict[int, int]                # old formID -> new formID
    external_links: List[Tuple[int, int, int]]
    # ^ (owner_new_formID, keyword_or_flags_context, unresolved_target_formID)
    #   for XLKR/XESP targets that pointed OUTSIDE the cloned set. Reported, not
    #   rewritten (guardrail #7).


def _remap_field_targets(record: Record, id_map: Dict[int, int],
                         external: List[Tuple[int, int, int]]) -> None:
    """Rewrite XLKR ref targets that fall inside ``id_map``; strip XESP.

    * XLKR.ref_form_id in id_map -> remapped. Otherwise left verbatim and
      reported in ``external`` (guardrail #7).
    * XESP (enable parent) is removed entirely -- a cloned cluster is
      independent, so it must not inherit the seed's enable-parent (this is the
      "strip XESP" step in the closure). If the XESP parent was outside the set
      it is still reported before removal.
    * NAME (base object) is intentionally NOT remapped: bases are shared library
      objects, not part of a placement cluster.
    """
    field_list = _plaintext_fields(record)
    new_owner = record.form_id
    kept: List[fields.Field] = []
    for f in field_list:
        if f.signature == b"XLKR":
            link = fields.decode_xlkr(f.payload)
            if link.ref_form_id in id_map:
                link.ref_form_id = id_map[link.ref_form_id]
                f = fields.Field(b"XLKR", fields.encode_xlkr(link), f.used_xxxx)
            elif link.ref_form_id != 0:
                external.append((new_owner, link.keyword_form_id,
                                 link.ref_form_id))
            kept.append(f)
        elif f.signature == b"XESP":
            parent = fields.decode_xesp(f.payload)
            if parent.parent_form_id not in id_map and parent.parent_form_id != 0:
                external.append((new_owner, parent.flags,
                                 parent.parent_form_id))
            # strip: do not append.
        else:
            kept.append(f)  # order + unknowns preserved (guardrail #5)
    _store_fields(record, kept)


def clone_cluster(plugin: Plugin, seed_form_id: int, count: int,
                  translate: Sequence[float]) -> CloneResult:
    """Clone the record ``seed_form_id`` ``count`` times.

    The operation is a *closure* over one seed record:

      1. Allocate ``count`` fresh formIDs from ``HEDR.nextObjectID`` (guardrail
         #2), building ``old -> new`` for the seed. (The seed set is the single
         seed; each clone is an independent copy under its own new formID.)
      2. Deep-copy the seed, assign the fresh formID.
      3. Remap XLKR targets that point inside the cloned set; strip XESP
         enable-parents. Links pointing outside are reported (guardrail #7).
      4. Translate each clone's DATA position by ``translate`` (dx,dy,dz),
         re-packing as 32-bit floats (guardrail #3). Rotation is left intact.

    The clones are returned but *not* inserted into the tree -- pair with
    :func:`insert_records` to place them (which also handles the num_records
    delta). Here we only bump ``nextObjectID`` for the ids we minted.

    ``translate`` is a 3-tuple (dx, dy, dz).
    """
    if count < 1:
        raise EditError("count must be >= 1")
    if len(translate) != 3:
        raise EditError("translate must have 3 components (dx, dy, dz)")

    loc = _find_record(plugin, seed_form_id)
    if loc is None:
        raise EditError(f"no seed record with formID 0x{seed_form_id:08X}")
    seed: Record = loc.node
    # Work against plaintext so a compressed seed clones as uncompressed.
    seed_plain_data = decompress_record(seed)

    new_ids, fl, hedr_field, hedr = _alloc_form_ids(plugin, count)
    # Bump nextObjectID now (the ids are minted); num_records stays put until
    # the clones are actually inserted into the tree by insert_records.
    _write_hedr(plugin, fl, hedr_field, hedr)

    # The set of formIDs that are "inside" the clone for link-remap purposes.
    # A single seed maps to each new id; when remapping an individual clone we
    # only need seed->that-clone (self references collapse to the clone).
    dx, dy, dz = (float(translate[0]), float(translate[1]),
                  float(translate[2]))

    clones: List[Record] = []
    external: List[Tuple[int, int, int]] = []
    id_map: Dict[int, int] = {}

    for new_id in new_ids:
        clone = Record(
            signature=seed.signature,
            header=seed.header,               # replaced below via setters
            data=seed_plain_data,             # plaintext copy
        )
        # Ensure the clone is a genuine deep copy (immutable bytes make the
        # above safe, but be explicit for clarity / future mutability).
        clone = copy.deepcopy(clone)
        # It is now plaintext: make sure the compressed flag is off.
        if clone.is_compressed:
            clone.flags = clone.flags & ~FLAG_COMPRESSED
        clone.form_id = new_id

        # Per-clone remap set: the seed's own formID collapses to this clone,
        # so intra-record self-links resolve to the new id.
        per_clone_map = {seed_form_id: new_id}
        _remap_field_targets(clone, per_clone_map, external)
        id_map[seed_form_id] = new_id  # last-writer; report is per-record anyway

        # Translate DATA position (rotation untouched); pack as float32.
        field_list = _plaintext_fields(clone)
        data_field = fields.find_field(field_list, b"DATA")
        if data_field is not None:
            pr = fields.decode_data_posrot(data_field.payload)
            pr.x = pr.x + dx
            pr.y = pr.y + dy
            pr.z = pr.z + dz
            data_field.payload = fields.encode_data_posrot(pr)
            _store_fields(clone, field_list)

        clones.append(clone)

    _assert_clean(plugin)  # nextObjectID bump kept TES4 payload valid
    return CloneResult(records=clones, id_map=id_map, external_links=external)


def clone_record(plugin: Plugin, seed_form_id: int) -> Tuple[Record, int]:
    """Clone one record under a fresh formID, attached to its *own* GRUP.

    Where :func:`clone_cluster` mints REFR placements and leaves them for
    :func:`insert_records` to splice into a CELL, this deep-copies **any** record
    (QUST, ACTI, a base object, ...) and inserts the clone into the same container
    the seed lives in -- immediately after the seed -- so a top-level record's
    clone lands in the same top-level GRUP as the seed. This is the record-level
    analogue of :func:`insert_records`: it exists because inserting a whole new
    top-level record was the one create-op the tool layer couldn't express.

    * Allocates one fresh formID from ``HEDR.nextObjectID`` (guardrail #2).
    * Deep-copies the seed as plaintext (a compressed seed clones uncompressed;
      dataSize + the compressed flag are re-synced on serialize).
    * Bumps ``HEDR.num_records`` by 1 (one node added to the tree).
    * Does **not** remap links or translate position -- a clone of a
      non-placement record has no cluster semantics; the caller rewrites the
      clone's subrecords (EDID / VMAD / ...) afterward via the subrecord tools.

    Guardrail #1: the added node re-flows the size of the seed's container GRUP
    and every ancestor on serialize. Returns ``(clone, new_form_id)``.
    """
    loc = _find_record(plugin, seed_form_id)
    if loc is None:
        raise EditError(f"no seed record with formID 0x{seed_form_id:08X}")
    seed: Record = loc.node
    seed_plain_data = decompress_record(seed)   # clone a compressed seed as plain

    new_ids, fl, hedr_field, hedr = _alloc_form_ids(plugin, 1)
    new_id = new_ids[0]

    clone = copy.deepcopy(Record(
        signature=seed.signature,
        header=seed.header,
        data=seed_plain_data,
    ))
    if clone.is_compressed:
        clone.flags = clone.flags & ~FLAG_COMPRESSED
    clone.form_id = new_id

    # Insert right after the seed in its own container (the top-level GRUP for a
    # top-level record). The new node re-flows every ancestor GRUP size.
    loc.container.insert(loc.index + 1, clone)

    # nextObjectID was bumped by _alloc_form_ids; the tree gained one record.
    hedr.num_records = hedr.num_records + 1
    _write_hedr(plugin, fl, hedr_field, hedr)

    _assert_clean(plugin)
    return clone, new_id


# --------------------------------------------------------------------------- #
# 6. Delete a non-contiguous set of records
# --------------------------------------------------------------------------- #

def delete_records(plugin: Plugin, form_ids: Sequence[int]) -> List[int]:
    """Remove every record whose formID is in ``form_ids``.

    The ids may be non-contiguous and scattered anywhere in the tree. For each
    removed record its bytes vanish from its container list, so its parent GRUP
    (and every ancestor GRUP) shrinks by exactly that record's ``byte_size`` on
    serialize (guardrail #1). ``HEDR.num_records`` is decremented once per
    removed record (guardrail #2).

    Empty GRUPs left behind are *kept* (a zero-child GRUP is still walk-clean);
    we don't prune them because doing so would change semantics silently.

    Returns the list of formIDs actually removed (a subset of ``form_ids``).
    """
    wanted = set(int(f) & 0xFFFFFFFF for f in form_ids)
    if not wanted:
        return []
    removed: List[int] = []

    def prune(container: List) -> None:
        i = 0
        while i < len(container):
            node = container[i]
            if isinstance(node, Record) and node.form_id in wanted:
                removed.append(node.form_id)
                del container[i]
                continue  # do not advance; next node slid into index i
            if isinstance(node, Group):
                prune(node.children)
            i += 1

    prune(plugin.top_level)

    if removed:
        fl, hedr_field, hedr = _read_hedr(plugin)
        hedr.num_records = hedr.num_records - len(removed)
        _write_hedr(plugin, fl, hedr_field, hedr)

    _assert_clean(plugin)
    return removed


def delete_cells(plugin: Plugin, cell_form_ids: Sequence[int]) -> List[Tuple[int, int]]:
    """Remove whole CELL overrides so the game falls back to the master's cell.

    For each target formID, deletes the CELL record **and** its immediately-
    following type-6 children GRUP (all its LAND / NAVM / REFR / ... at once).
    Like :func:`delete_records`, ``HEDR.num_records`` is decremented once per
    removed *record* (the CELL plus every record inside its children GRUP), and
    empty container GRUPs left behind (an emptied block/sub-block) are kept.
    Returns ``[(cell_formid, records_removed)]`` for the cells found.
    """
    wanted = set(int(f) & 0xFFFFFFFF for f in cell_form_ids)
    if not wanted:
        return []
    removed: List[Tuple[int, int]] = []

    def count_records(node) -> int:
        if isinstance(node, Record):
            return 1
        return sum(count_records(c) for c in node.children)

    def prune(container: List) -> None:
        i = 0
        while i < len(container):
            node = container[i]
            if (isinstance(node, Record) and node.sig == "CELL"
                    and node.form_id in wanted):
                n = 1  # the CELL record itself
                end = i + 1
                if (end < len(container) and isinstance(container[end], Group)
                        and container[end].group_type == GT_CELL_CHILDREN):
                    n += count_records(container[end])
                    end += 1
                del container[i:end]
                removed.append((node.form_id, n))
                continue  # next node slid into index i
            if isinstance(node, Group):
                prune(node.children)
            i += 1

    prune(plugin.top_level)

    if removed:
        total = sum(n for _, n in removed)
        fl, hedr_field, hedr = _read_hedr(plugin)
        hedr.num_records = hedr.num_records - total
        _write_hedr(plugin, fl, hedr_field, hedr)

    _assert_clean(plugin)
    return removed


# --------------------------------------------------------------------------- #
# 7. Swap a base object's model (copy OBND / MODL / MODT from a donor)
# --------------------------------------------------------------------------- #

_MODEL_SIGS = (b"OBND", b"MODL", b"MODT")


def swap_model(plugin: Plugin, base_form_id: int,
               donor_base_form_id: int) -> Record:
    """Copy the donor base's ``OBND``/``MODL``/``MODT`` onto ``base_form_id``.

    Both records are located by formID. The donor's model fields are read from
    its plaintext; on the target, each of OBND/MODL/MODT is replaced in place if
    already present (preserving field order, guardrail #5) or, if absent,
    inserted immediately after ``EDID`` (a valid position for model data). Donor
    fields it doesn't have are left as-is on the target -- we only copy what the
    donor provides.

    Changing MODL/MODT lengths is a real byte-length change; it re-flows through
    every ancestor GRUP on serialize (guardrail #1). HEDR is untouched (no node
    count change).

    Returns the modified target record.
    """
    if base_form_id == donor_base_form_id:
        raise EditError("target and donor base are the same record")

    target_loc = _find_record(plugin, base_form_id)
    if target_loc is None:
        raise EditError(f"no target base with formID 0x{base_form_id:08X}")
    donor_loc = _find_record(plugin, donor_base_form_id)
    if donor_loc is None:
        raise EditError(f"no donor base with formID 0x{donor_base_form_id:08X}")

    target: Record = target_loc.node
    donor: Record = donor_loc.node

    donor_fields = _plaintext_fields(donor)
    donor_model = {
        sig: fields.find_field(donor_fields, sig) for sig in _MODEL_SIGS
    }

    target_fields = _plaintext_fields(target)

    # Index of the EDID field, to know where to insert missing model fields.
    edid_index = next(
        (i for i, f in enumerate(target_fields) if f.signature == b"EDID"),
        -1,
    )
    # Insertion cursor: right after EDID (or at front if no EDID).
    insert_at = edid_index + 1

    for sig in _MODEL_SIGS:
        donor_field = donor_model[sig]
        if donor_field is None:
            continue  # donor doesn't carry this; leave target's as-is
        new_field = fields.Field(sig, donor_field.payload, donor_field.used_xxxx)
        # Replace in place if the target already has this sig.
        existing = next(
            (i for i, f in enumerate(target_fields) if f.signature == sig),
            -1,
        )
        if existing >= 0:
            target_fields[existing] = new_field
        else:
            target_fields.insert(insert_at, new_field)
            insert_at += 1  # keep subsequent inserts in OBND/MODL/MODT order

    _store_fields(target, target_fields)
    _assert_clean(plugin)
    return target
