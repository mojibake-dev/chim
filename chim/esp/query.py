"""Read-only queries over a parsed :class:`~chim.esp.records.Plugin` tree.

This module builds *only* on the public model in :mod:`chim.esp.records` and the
field layer in :mod:`chim.esp.fields` (plus :mod:`chim.esp.compression` for the
one thing those layers delegate: getting a record's plaintext field stream when
the record is compressed). It adds no new on-disk knowledge -- every lookup is
expressed in terms of :func:`iterate`, :class:`Record`/:class:`Group`, and the
typed field codecs (``decode_edid``, ``decode_name``, ``decode_obnd``,
``decode_modl``, ``decode_xlkr``, ``decode_xesp``).

The queries here never mutate the tree; they only read it.

Vocabulary
----------
* **base object** -- a template record (STAT, FURN, DOOR, NPC_, ...) that a
  placement instantiates. Placements name their base via a ``NAME`` field.
* **placement / reference** -- a REFR / ACHR / ACRE record dropped into a CELL,
  carrying position (``DATA``), an optional enable-parent (``XESP``) and zero or
  more linked references (``XLKR``).

All formIDs are plain ``int`` (full 32-bit, i.e. ``pluginIndex<<24 | object``),
matching :attr:`Record.form_id`.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Pattern, Tuple, Union

from .records import Plugin, Record, Group, iterate, GT_CELL_CHILDREN
from .compression import decompress_record
from . import fields

__all__ = [
    "PLACEMENT_SIGS",
    "record_payload",
    "record_edid",
    "iter_records",
    "find_by_formid",
    "find_by_base",
    "find_by_cell",
    "find_by_edid",
    "cell_children_group",
    "cell_refs",
    "base_map",
    "transitive_closure",
]

# Placement record signatures -- the records that instantiate a base object via
# a NAME field and that carry XESP / XLKR.
PLACEMENT_SIGS = ("REFR", "ACHR", "ACRE", "PGRE", "PHZD", "PMIS", "PARW", "PBAR",
                  "PBEA", "PCON", "PFLA")


# --------------------------------------------------------------------------- #
# Low-level helpers (all in terms of records/fields/compression)
# --------------------------------------------------------------------------- #

def record_payload(record: Record) -> bytes:
    """Plaintext field stream for ``record`` (transparently decompressed).

    A thin adapter over :func:`chim.esp.compression.decompress_record` so the
    rest of this module can always call :func:`chim.esp.fields.parse_fields`
    without worrying about the compressed flag.
    """
    return decompress_record(record)


def record_fields(record: Record) -> List[fields.Field]:
    """Parsed field list for ``record`` (decompressing first if needed)."""
    return fields.parse_fields(record_payload(record))


def record_edid(record: Record) -> Optional[str]:
    """Decoded ``EDID`` of ``record`` or ``None`` if it has no EDID."""
    fl = record_fields(record)
    f = fields.find_field(fl, b"EDID")
    return fields.decode_edid(f.payload) if f is not None else None


def iter_records(plugin: Plugin):
    """Yield every :class:`Record` in ``plugin`` (skips GRUP nodes)."""
    for node in iterate(plugin):
        if isinstance(node, Record):
            yield node


# --------------------------------------------------------------------------- #
# find_by_formid
# --------------------------------------------------------------------------- #

def find_by_formid(plugin: Plugin, formid: int) -> Optional[Record]:
    """Return the first record whose ``form_id`` equals ``formid``.

    formIDs are unique within a plugin, so "first" is also "the" one. Returns
    ``None`` if nothing matches.
    """
    for rec in iter_records(plugin):
        if rec.form_id == formid:
            return rec
    return None


# --------------------------------------------------------------------------- #
# find_by_base
# --------------------------------------------------------------------------- #

def find_by_base(plugin: Plugin, base_formid: int) -> List[Record]:
    """All placement records that instantiate base object ``base_formid``.

    A placement points at its base through a ``NAME`` subrecord
    (:func:`chim.esp.fields.decode_name`). Records without a NAME (or whose NAME
    points elsewhere) are skipped. Result is in file order.
    """
    out: List[Record] = []
    for rec in iter_records(plugin):
        if rec.sig not in PLACEMENT_SIGS:
            continue
        name = fields.find_field(record_fields(rec), b"NAME")
        if name is None:
            continue
        if fields.decode_name(name.payload) == base_formid:
            out.append(rec)
    return out


# --------------------------------------------------------------------------- #
# find_by_cell
# --------------------------------------------------------------------------- #

def find_by_cell(plugin: Plugin, edid_or_formid: Union[str, int]) -> Optional[Record]:
    """Return the CELL record identified by an EDID (str) or formID (int).

    * ``int`` -> match ``CELL.form_id``.
    * ``str`` -> match ``CELL``'s decoded EDID exactly.

    Returns ``None`` if no CELL matches.
    """
    want_formid = isinstance(edid_or_formid, int) and not isinstance(edid_or_formid, bool)
    for rec in iter_records(plugin):
        if rec.sig != "CELL":
            continue
        if want_formid:
            if rec.form_id == edid_or_formid:
                return rec
        else:
            if record_edid(rec) == edid_or_formid:
                return rec
    return None


# --------------------------------------------------------------------------- #
# cell_refs -- enumerate the placements inside a CELL
# --------------------------------------------------------------------------- #

def cell_children_group(plugin: Plugin, cell_formid: int) -> Optional[Group]:
    """The group_type-6 CELL-children GRUP that follows CELL ``cell_formid``.

    In both interiors and worldspaces a CELL record is immediately followed, as
    its next sibling in the same container, by a type-6 GRUP holding that cell's
    persistent (t8), temporary (t9) and visible-distant (t10) sub-groups. Returns
    that GRUP, or ``None`` if the cell (or its children group) is not present.
    """
    def walk(nodes: List) -> Optional[Group]:
        for i, node in enumerate(nodes):
            if (isinstance(node, Record) and node.sig == "CELL"
                    and node.form_id == cell_formid):
                nxt = nodes[i + 1] if i + 1 < len(nodes) else None
                if isinstance(nxt, Group) and nxt.group_type == GT_CELL_CHILDREN:
                    return nxt
                return None
            if isinstance(node, Group):
                found = walk(node.children)
                if found is not None:
                    return found
        return None
    return walk(plugin.top_level)


def cell_refs(plugin: Plugin, cell_formid: int) -> List[Record]:
    """Every placement record inside CELL ``cell_formid`` (persistent + temporary).

    Walks the cell's type-6 children GRUP and returns all placement records
    (:data:`PLACEMENT_SIGS`) in its nested sub-groups, in file order. Returns
    ``[]`` if the cell has no children group. Persistence of each ref is on its
    own header flag (``FLAG_PERSISTENT``), so callers rarely need the sub-group.
    """
    grp = cell_children_group(plugin, cell_formid)
    if grp is None:
        return []
    return [n for n in iterate(grp)
            if isinstance(n, Record) and n.sig in PLACEMENT_SIGS]


# --------------------------------------------------------------------------- #
# find_by_edid
# --------------------------------------------------------------------------- #

def find_by_edid(plugin: Plugin, pattern: Union[str, Pattern[str]]) -> List[Record]:
    """All records whose EDID matches ``pattern`` (regex ``search``).

    ``pattern`` may be a compiled regex or a string (compiled with
    :func:`re.compile`; use anchors like ``^...$`` for an exact match). Records
    with no EDID are skipped. Result is in file order.
    """
    rx: Pattern[str] = pattern if isinstance(pattern, re.Pattern) else re.compile(pattern)
    out: List[Record] = []
    for rec in iter_records(plugin):
        edid = record_edid(rec)
        if edid is not None and rx.search(edid) is not None:
            out.append(rec)
    return out


# --------------------------------------------------------------------------- #
# base_map
# --------------------------------------------------------------------------- #

# (edid, sig, mesh, obnd)
BaseInfo = Tuple[Optional[str], str, Optional[str],
                 Optional[Tuple[int, int, int, int, int, int]]]


def base_map(plugin: Plugin, sigs) -> Dict[int, BaseInfo]:
    """Map ``formid -> (edid, sig, mesh, obnd)`` for records of the given sigs.

    ``sigs`` is any iterable of 4-char signature strings (e.g.
    ``("STAT", "FURN")``). For each matching record:

    * ``edid`` -- decoded ``EDID`` or ``None``.
    * ``sig``  -- the record's signature string.
    * ``mesh`` -- decoded ``MODL`` mesh path (relative to ``meshes\\``) or
      ``None`` if the record has no MODL.
    * ``obnd`` -- decoded ``OBND`` 6-tuple ``(x1,y1,z1,x2,y2,z2)`` or ``None``.

    Later records with a duplicate formID overwrite earlier ones (formIDs are
    unique within a well-formed plugin, so this only matters for malformed
    input).
    """
    wanted = frozenset(sigs)
    out: Dict[int, BaseInfo] = {}
    for rec in iter_records(plugin):
        if rec.sig not in wanted:
            continue
        fl = record_fields(rec)

        edid_f = fields.find_field(fl, b"EDID")
        edid = fields.decode_edid(edid_f.payload) if edid_f is not None else None

        modl_f = fields.find_field(fl, b"MODL")
        mesh = fields.decode_modl(modl_f.payload) if modl_f is not None else None

        obnd_f = fields.find_field(fl, b"OBND")
        obnd = fields.decode_obnd(obnd_f.payload) if obnd_f is not None else None

        out[rec.form_id] = (edid, rec.sig, mesh, obnd)
    return out


# --------------------------------------------------------------------------- #
# transitive_closure
# --------------------------------------------------------------------------- #

def _linked_formids(rec: Record) -> Tuple[List[int], List[int]]:
    """Return (xesp_parents, xlkr_targets) for ``rec``.

    * ``xesp_parents`` -- the enable-parent formID from each ``XESP`` field
      (usually zero or one).
    * ``xlkr_targets`` -- the ``ref_form_id`` from each ``XLKR`` field (the
      thing this record links *to*; the keyword formID is not a placement so it
      is not followed).
    """
    xesp_parents: List[int] = []
    xlkr_targets: List[int] = []
    for f in record_fields(rec):
        if f.signature == b"XESP":
            xesp_parents.append(fields.decode_xesp(f.payload).parent_form_id)
        elif f.signature == b"XLKR":
            xlkr_targets.append(fields.decode_xlkr(f.payload).ref_form_id)
    return xesp_parents, xlkr_targets


def transitive_closure(plugin: Plugin, seed_formid: int) -> Dict[int, Record]:
    """Gather the connected cluster of records reachable from ``seed_formid``.

    Starting at ``seed_formid`` we follow two relations until nothing new is
    added, yielding a *connected component*:

    * **XESP (children):** if record *A* has an ``XESP`` enable-parent pointing
      at *B*, then *A* and *B* are in the same cluster. Followed in **both**
      directions -- from a parent we pull in every child that names it, and from
      a child we pull in its parent -- so seeding either end grabs the family.
    * **XLKR (targets):** if record *A* has an ``XLKR`` linked reference whose
      ``ref_form_id`` is *B*, then *A* and *B* are in the same cluster. Also
      followed in both directions.

    Only formIDs that resolve to a record present in ``plugin`` are added; dangling
    references (into masters, or simply missing) are ignored. The seed itself is
    included when it resolves to a record.

    Returns ``{formid: Record}`` for the whole cluster (empty if the seed does
    not resolve to any record in the plugin).
    """
    # Index every record by formID once, and precompute the forward edges
    # (parent + target) for each so reverse edges are cheap to build.
    by_id: Dict[int, Record] = {}
    forward: Dict[int, List[int]] = {}
    reverse: Dict[int, List[int]] = {}
    for rec in iter_records(plugin):
        fid = rec.form_id
        by_id[fid] = rec  # last write wins on (malformed) dup formIDs
    for fid, rec in by_id.items():
        parents, targets = _linked_formids(rec)
        outgoing = parents + targets
        forward[fid] = outgoing
        for dst in outgoing:
            reverse.setdefault(dst, []).append(fid)

    if seed_formid not in by_id:
        return {}

    cluster: Dict[int, Record] = {}
    stack: List[int] = [seed_formid]
    while stack:
        fid = stack.pop()
        if fid in cluster:
            continue
        rec = by_id.get(fid)
        if rec is None:
            # A referenced formID with no record in this plugin (e.g. a base in
            # a master). Do not add and do not traverse further from it.
            continue
        cluster[fid] = rec
        # Forward: things this record points at.
        for nxt in forward.get(fid, ()):  # noqa: SIM118
            if nxt not in cluster:
                stack.append(nxt)
        # Reverse: things that point at this record.
        for nxt in reverse.get(fid, ()):  # noqa: SIM118
            if nxt not in cluster:
                stack.append(nxt)
    return cluster
