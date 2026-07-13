"""Authoring brand-new BASE records and their top-level GRUPs.

:mod:`chim.esp.edit` mutates existing records and mints placements (``REFR``).
This module fills the remaining gap: create a fresh base object (a ``STAT`` today)
and slot it into the plugin's top-level group structure, creating that group if
the plugin does not yet contain one for the signature.

All the same invariants hold: the container model re-derives every GRUP size on
serialize, so appending a record (or a whole new top GRUP) re-flows structurally;
only ``HEDR.num_records`` is bumped by hand, and every mutation ends walk-clean.
"""

from __future__ import annotations

import struct
from typing import Optional, Sequence

from .records import (
    Group,
    Plugin,
    Record,
    GT_TOP,
    GROUP_HEADER_SIZE,
    _GRP_HDR_TAIL,
)
from . import fields
from .edit import (
    EditError,
    _new_record_header,
    _read_hedr,
    _write_hedr,
    _assert_clean,
)


def build_static(form_id: int, edid: str, mesh: str,
                 obnd: Sequence[int] = (0, 0, 0, 0, 0, 0),
                 flags: int = 0) -> Record:
    """Construct a standalone ``STAT`` base record (``EDID`` + ``OBND`` + ``MODL``).

    Parameters
    ----------
    form_id : int
        The new static's formID (allocate via :func:`chim.esp.edit._alloc_form_ids`).
    edid : str
        Editor ID -> ``EDID`` (ASCII, null-terminated).
    mesh : str
        NIF path relative to ``Data/Meshes/`` -> ``MODL``, e.g.
        ``r"Clutter\\DisplayCases\\DisplayCaseLGAngled01.nif"``.
    obnd : 6 ints
        Object bounds ``x1,y1,z1,x2,y2,z2`` -> ``OBND`` (``<6h``). Bounds only
        affect LOD / activation distance, never the rendered mesh.
    flags : int
        Header flags (usually ``0`` for a plain static).

    The record is not attached to any tree; pass it to :func:`insert_base_record`.
    """
    if len(obnd) != 6:
        raise EditError("obnd must have 6 components (x1,y1,z1,x2,y2,z2)")
    fl = [
        fields.Field(b"EDID", edid.encode("ascii") + b"\x00"),
        fields.Field(b"OBND", struct.pack("<6h", *(int(v) for v in obnd))),
        fields.Field(b"MODL", mesh.encode("ascii") + b"\x00"),
    ]
    data = fields.pack_fields(fl)
    header = _new_record_header(b"STAT", len(data), flags, form_id)
    return Record(signature=b"STAT", header=header, data=data)


def insert_base_record(plugin: Plugin, record: Record) -> Group:
    """Insert base ``record`` into its top-level (type-0) GRUP, creating that
    GRUP right after the ``TES4`` header if the plugin has none for the signature.

    Guardrail #2: ``HEDR.num_records`` is bumped by 1 (the record) plus 1 more if
    a new top GRUP had to be created. Returns the top GRUP the record now lives in.
    """
    if not isinstance(record, Record):
        raise EditError("insert_base_record only accepts a Record")
    sig = record.signature
    top: Optional[Group] = None
    for node in plugin.top_level:
        if (isinstance(node, Group) and node.group_type == GT_TOP
                and node.label == sig):
            top = node
            break
    created = False
    if top is None:
        header = (b"GRUP"
                  + _GRP_HDR_TAIL.pack(GROUP_HEADER_SIZE, sig, GT_TOP,
                                       0x00000000, 0x0000)
                  + struct.pack("<H", 0))
        top = Group(header=header, children=[])
        plugin.top_level.insert(1, top)  # right after TES4
        created = True
    top.children.append(record)

    fl, hedr_field, hedr = _read_hedr(plugin)
    hedr.num_records = hedr.num_records + 1 + (1 if created else 0)
    _write_hedr(plugin, fl, hedr_field, hedr)
    _assert_clean(plugin)
    return top
