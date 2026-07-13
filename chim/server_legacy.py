"""MCP server exposing ``chim``'s headless ESP/ESM editing over the wire.

This wraps the pure-Python plugin model (:mod:`chim.esp`) as a set of MCP tools
so an agent can query and mutate a live Skyrim Special Edition plugin on the
remote Windows host.

Design
------
* **One FastMCP app, eight tools.** Read-only tools (``esp_query``,
  ``esp_get_record``) parse the plugin and answer questions without ever
  writing. Mutating tools (``esp_set_flag``, ``esp_move_ref``,
  ``esp_clone_cluster``, ``esp_insert_ref``, ``esp_delete_records``) run their
  whole edit inside :func:`chim.esp.safety.transaction` -- lock-check, backup,
  edit-bytes, write-back, verify walk-clean, restore-on-dirty. ``papyrus_compile``
  drives the Creation Kit compiler on the host; it changes no plugin bytes so it
  needs no transaction.
* **Host selection from the environment.** ``CHIM_SSH_HOST`` (e.g.
  ``user@host``) selects a :class:`~chim.esp.safety.RemoteHost`;
  ``CHIM_DATA_DIR`` overrides the Skyrim SE ``Data`` directory that plugin names
  are resolved against. With no ``CHIM_SSH_HOST`` the server falls back to a
  :class:`~chim.esp.safety.LocalHost` rooted at ``CHIM_DATA_DIR`` (or the current
  working directory) -- the same double the test suite uses, so the server is
  runnable on a dev box against a local copy of a plugin.

Every tool takes a ``plugin`` argument that is a bare file *name*
(``MyMod.esp``); it is joined to the configured data dir on the host. Absolute
paths are passed through untouched.

Run with ``python -m chim.server`` (stdio transport) or via the ``chim-mcp``
console script.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Sequence

from mcp.server.fastmcp import FastMCP

from .esp import fields
from .esp.records import (
    FLAG_DELETED,
    FLAG_INITIALLY_DISABLED,
    FLAG_PERSISTENT,
    parse_plugin,
    serialize,
)
from .esp import edit, query, safety, subrecords
from .esp import papyrus


# --------------------------------------------------------------------------- #
# Host / path configuration from the environment
# --------------------------------------------------------------------------- #

#: SSH target for the remote Windows host, e.g. ``user@host``.
ENV_SSH_HOST = "CHIM_SSH_HOST"
#: Skyrim SE ``Data`` directory plugin names are resolved against.
ENV_DATA_DIR = "CHIM_DATA_DIR"


def _make_host() -> safety.Host:
    """Return the configured :class:`~chim.esp.safety.Host`.

    * ``CHIM_SSH_HOST`` set -> a :class:`RemoteHost` over the system ssh client
      (chim runs elsewhere and reaches the remote host over SSH).
    * unset, on Windows -> a :class:`LocalWindowsHost`: direct local file I/O
      **plus a real lock-check** (chim runs *on* the remote host -- the intended
      production deployment).
    * unset, elsewhere -> a :class:`LocalHost` dev/test double (always unlocked).
    """
    target = os.environ.get(ENV_SSH_HOST)
    if target:
        return safety.RemoteHost(target)
    if os.name == "nt":
        return safety.LocalWindowsHost()
    return safety.LocalHost()


def _data_dir(host: safety.Host) -> str:
    """The directory plugin names resolve against for ``host``.

    Prefers ``CHIM_DATA_DIR``. Otherwise: the stock Steam SE ``Data`` path on a
    Windows/remote host, or the current working directory for a local host.
    """
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return override
    if host.windows:
        return safety.DEFAULT_DATA_DIR
    return os.getcwd()


def _resolve(host: safety.Host, plugin: str) -> str:
    """Resolve a plugin *name* to a full path on ``host``.

    A value that already looks absolute (drive-letter, UNC, or POSIX ``/``) is
    returned untouched; a bare name is joined to the configured data dir using
    the host's path flavour.
    """
    looks_absolute = (
        plugin.startswith("/")
        or plugin.startswith("\\\\")
        or (len(plugin) >= 2 and plugin[1] == ":")
    )
    if looks_absolute:
        return plugin
    directory = _data_dir(host)
    if host.windows:
        return directory.rstrip("\\/") + "\\" + plugin
    return os.path.join(directory, plugin)


def _load(host: safety.Host, plugin: str):
    """Read + parse ``plugin`` from ``host`` (read-only path, no transaction)."""
    path = _resolve(host, plugin)
    return parse_plugin(host.read_file(path))


# --------------------------------------------------------------------------- #
# Serialization helpers: turn model objects into JSON-friendly dicts
# --------------------------------------------------------------------------- #

def _hexid(form_id: int) -> str:
    return f"0x{form_id & 0xFFFFFFFF:08X}"


def _record_summary(rec) -> Dict[str, Any]:
    """A compact, JSON-safe summary of a record (no raw payload bytes)."""
    return {
        "sig": rec.sig,
        "form_id": rec.form_id,
        "form_id_hex": _hexid(rec.form_id),
        "flags": rec.flags,
        "flags_hex": _hexid(rec.flags),
        "data_size": rec.data_size,
        "is_compressed": rec.is_compressed,
        "is_deleted": rec.is_deleted,
        "is_persistent": rec.is_persistent,
        "edid": query.record_edid(rec),
    }


def _record_detail(rec) -> Dict[str, Any]:
    """Full record view: summary + decoded field list."""
    out = _record_summary(rec)
    field_views: List[Dict[str, Any]] = []
    for f in query.record_fields(rec):
        view: Dict[str, Any] = {"sig": f.sig, "size": f.size}
        # Decode the handful of fields the query layer knows about.
        try:
            if f.signature == b"EDID":
                view["value"] = fields.decode_edid(f.payload)
            elif f.signature == b"NAME":
                view["value"] = _hexid(fields.decode_name(f.payload))
            elif f.signature == b"MODL":
                view["value"] = fields.decode_modl(f.payload)
            elif f.signature == b"OBND":
                view["value"] = list(fields.decode_obnd(f.payload))
            elif f.signature == b"DATA" and f.size == 24:
                view["value"] = fields.decode_data_posrot(f.payload).as_tuple()
            elif f.signature == b"XLKR":
                lr = fields.decode_xlkr(f.payload)
                view["value"] = {
                    "keyword": _hexid(lr.keyword_form_id),
                    "ref": _hexid(lr.ref_form_id),
                }
            elif f.signature == b"XESP":
                ep = fields.decode_xesp(f.payload)
                view["value"] = {
                    "parent": _hexid(ep.parent_form_id),
                    "flags": ep.flags,
                }
            elif f.signature == b"XSCL":
                view["value"] = fields.decode_xscl(f.payload)
        except Exception:  # pragma: no cover - decode is best-effort
            pass
        field_views.append(view)
    out["fields"] = field_views
    return out


def _coerce_form_id(value: Any) -> int:
    """Accept an int or a hex/decimal string formID and return an int."""
    if isinstance(value, int):
        return value & 0xFFFFFFFF
    s = str(value).strip()
    return int(s, 16 if s.lower().startswith("0x") else 10) & 0xFFFFFFFF


#: Symbolic flag names accepted by ``esp_set_flag`` (plus raw ints / hex).
FLAG_ALIASES = {
    "deleted": FLAG_DELETED,
    "persistent": FLAG_PERSISTENT,
    "initially_disabled": FLAG_INITIALLY_DISABLED,
    "disabled": FLAG_INITIALLY_DISABLED,
}


def _coerce_flag(value: Any) -> int:
    if isinstance(value, int):
        return value & 0xFFFFFFFF
    s = str(value).strip().lower()
    if s in FLAG_ALIASES:
        return FLAG_ALIASES[s]
    return int(s, 16 if s.startswith("0x") else 10) & 0xFFFFFFFF


def _unhex(value: str) -> bytes:
    """Decode a hex payload string (``"00201a00"`` / ``"0x00201A00"`` / spaced)."""
    s = str(value).strip().replace(" ", "").replace("_", "")
    if s.lower().startswith("0x"):
        s = s[2:]
    try:
        return bytes.fromhex(s)
    except ValueError as exc:
        raise ValueError(f"invalid hex payload {value!r}: {exc}")


def _sig_bytes(sig: str) -> bytes:
    """Coerce a 4-char subrecord signature string to bytes (``"XLKR"``)."""
    try:
        b = str(sig).encode("ascii")
    except UnicodeEncodeError:
        raise ValueError(f"subrecord signature must be ASCII, got {sig!r}")
    if len(b) != 4:
        raise ValueError(f"subrecord signature must be 4 ASCII chars, got {sig!r}")
    return b


def _field_view(flds, only: Optional[bytes] = None) -> List[Dict[str, Any]]:
    """JSON view of a field list with per-signature occurrence indices.

    ``index`` is the occurrence number of that signature (0, 1, 2, ...) computed
    over the WHOLE record, so it is the exact value to pass back to
    ``esp_set_subrecord`` / ``esp_patch_subrecord`` / ``esp_delete_subrecord``.
    """
    counters: Dict[bytes, int] = {}
    out: List[Dict[str, Any]] = []
    for f in flds:
        idx = counters.get(f.signature, 0)
        counters[f.signature] = idx + 1
        if only is not None and f.signature != only:
            continue
        out.append({
            "sig": f.sig,
            "index": idx,
            "size": f.size,
            "payload_hex": f.payload.hex(),
        })
    return out


# --------------------------------------------------------------------------- #
# FastMCP app + tool registration
# --------------------------------------------------------------------------- #

mcp = FastMCP("chim")


@mcp.tool()
def esp_query(
    plugin: str,
    by: str,
    value: str,
    sigs: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Read-only search over a plugin (never writes; no transaction).

    ``by`` selects the lookup:

    * ``"formid"`` -- the single record whose formID == ``value`` (int/hex).
    * ``"edid"``   -- every record whose EDID matches the regex ``value``.
    * ``"base"``   -- every placement instantiating base object ``value``.
    * ``"cell"``   -- the CELL identified by ``value`` (EDID string or formID).
    * ``"basemap"``-- ``formid -> (edid, sig, mesh, obnd)`` for records whose
      signature is in ``sigs`` (required for this mode).

    Returns ``{"count": N, "records": [...]}`` of record summaries (or, for
    ``basemap``, ``{"bases": {...}}``).
    """
    host = _make_host()
    plug = _load(host, plugin)
    mode = by.lower()

    if mode == "formid":
        rec = query.find_by_formid(plug, _coerce_form_id(value))
        recs = [rec] if rec is not None else []
    elif mode == "edid":
        recs = query.find_by_edid(plug, value)
    elif mode == "base":
        recs = query.find_by_base(plug, _coerce_form_id(value))
    elif mode == "cell":
        # int-looking value -> formID; otherwise treat as an EDID string.
        try:
            key: Any = _coerce_form_id(value)
        except ValueError:
            key = value
        rec = query.find_by_cell(plug, key)
        recs = [rec] if rec is not None else []
    elif mode == "basemap":
        if not sigs:
            raise ValueError("esp_query by='basemap' requires 'sigs'")
        bmap = query.base_map(plug, tuple(sigs))
        return {
            "bases": {
                _hexid(fid): {
                    "edid": edid,
                    "sig": sig,
                    "mesh": mesh,
                    "obnd": list(obnd) if obnd is not None else None,
                }
                for fid, (edid, sig, mesh, obnd) in bmap.items()
            }
        }
    elif mode == "cellrefs":
        try:
            cell_key: Any = _coerce_form_id(value)
        except ValueError:
            cell_key = value
        cell = query.find_by_cell(plug, cell_key)
        if cell is None:
            return {"cell": None, "count": 0, "refs": []}
        out_refs: List[Dict[str, Any]] = []
        for r in query.cell_refs(plug, cell.form_id):
            fl = query.record_fields(r)
            name = fields.find_field(fl, b"NAME")
            base = _hexid(fields.decode_name(name.payload)) if name else None
            xlkr = []
            for f in fl:
                if f.signature == b"XLKR":
                    lr = fields.decode_xlkr(f.payload)
                    xlkr.append({"keyword": _hexid(lr.keyword_form_id),
                                 "ref": _hexid(lr.ref_form_id)})
            out_refs.append({
                "form_id": _hexid(r.form_id),
                "sig": r.sig,
                "base": base,
                "flags_hex": _hexid(r.flags),
                "edid": query.record_edid(r),
                "persistent": bool(r.flags & FLAG_PERSISTENT),
                "initially_disabled": bool(r.flags & FLAG_INITIALLY_DISABLED),
                "has_xesp": fields.find_field(fl, b"XESP") is not None,
                "xlkr": xlkr,
            })
        return {"cell": _hexid(cell.form_id), "count": len(out_refs),
                "refs": out_refs}
    else:
        raise ValueError(
            f"unknown query mode {by!r}; expected one of "
            "formid|edid|base|cell|basemap|cellrefs"
        )

    return {"count": len(recs), "records": [_record_summary(r) for r in recs]}


@mcp.tool()
def esp_get_record(plugin: str, form_id: str) -> Dict[str, Any]:
    """Read-only: full detail of one record (summary + decoded fields).

    No transaction -- this only reads. Raises if ``form_id`` is not present.
    """
    host = _make_host()
    plug = _load(host, plugin)
    fid = _coerce_form_id(form_id)
    rec = query.find_by_formid(plug, fid)
    if rec is None:
        raise ValueError(f"no record with formID {_hexid(fid)} in {plugin!r}")
    return _record_detail(rec)


@mcp.tool()
def esp_set_flag(
    plugin: str,
    form_id: str,
    flag: str,
    clear: bool = False,
) -> Dict[str, Any]:
    """Set (or, with ``clear=True``, clear) a header flag on a record.

    ``flag`` may be a symbolic name (``deleted``, ``persistent``,
    ``initially_disabled``) or a raw int/hex. Clearing ``initially_disabled``
    (0x800) force-enables a reference. Runs inside a safety transaction:
    lock-check -> backup -> edit -> verify walk-clean -> restore-on-dirty.
    """
    host = _make_host()
    path = _resolve(host, plugin)
    fid = _coerce_form_id(form_id)
    flag_bits = _coerce_flag(flag)

    with safety.transaction(host, path) as txn:
        plug = parse_plugin(txn.data)
        if clear:
            edit.clear_flag(plug, fid, flag_bits)
        else:
            edit.set_flag(plug, fid, flag_bits)
        txn.data = serialize(plug)

    return {
        "plugin": path,
        "form_id": _hexid(fid),
        "flag": _hexid(flag_bits),
        "cleared": clear,
        "backup": txn.backup_path,
    }


@mcp.tool()
def esp_move_ref(
    plugin: str,
    form_id: str,
    pos: Sequence[float],
    rot: Sequence[float],
) -> Dict[str, Any]:
    """Overwrite a placement's DATA position/rotation (float32).

    ``pos`` and ``rot`` are each 3 floats ``[x, y, z]``. Runs inside a safety
    transaction (lock -> backup -> edit -> verify -> restore-on-dirty).
    """
    host = _make_host()
    path = _resolve(host, plugin)
    fid = _coerce_form_id(form_id)
    pos_t = [float(v) for v in pos]
    rot_t = [float(v) for v in rot]

    with safety.transaction(host, path) as txn:
        plug = parse_plugin(txn.data)
        edit.move_ref(plug, fid, pos_t, rot_t)
        txn.data = serialize(plug)

    return {
        "plugin": path,
        "form_id": _hexid(fid),
        "pos": pos_t,
        "rot": rot_t,
        "backup": txn.backup_path,
    }


@mcp.tool()
def esp_clone_cluster(
    plugin: str,
    seed_form_id: str,
    count: int,
    translate: Sequence[float],
    into_cell: Optional[str] = None,
) -> Dict[str, Any]:
    """Clone a record ``count`` times under fresh formIDs, offset by ``translate``.

    Allocates new formIDs from ``HEDR.nextObjectID``, deep-copies the seed,
    remaps intra-set XLKR links, strips XESP enable-parents, and translates each
    clone's DATA position by ``translate`` (dx, dy, dz). If ``into_cell`` (a
    CELL formID) is given the clones are also spliced into that CELL's
    persistent-children GRUP and ``HEDR.num_records`` is bumped; otherwise the
    clones are created but left unattached.

    Runs inside a safety transaction (lock -> backup -> edit -> verify ->
    restore-on-dirty).
    """
    host = _make_host()
    path = _resolve(host, plugin)
    seed = _coerce_form_id(seed_form_id)
    trans = [float(v) for v in translate]

    with safety.transaction(host, path) as txn:
        plug = parse_plugin(txn.data)
        result = edit.clone_cluster(plug, seed, int(count), trans)
        cell_id: Optional[int] = None
        if into_cell is not None:
            cell_id = _coerce_form_id(into_cell)
            edit.insert_records(plug, cell_id, result.records)
        txn.data = serialize(plug)

    return {
        "plugin": path,
        "seed": _hexid(seed),
        "count": int(count),
        "new_form_ids": [_hexid(r.form_id) for r in result.records],
        "id_map": {_hexid(k): _hexid(v) for k, v in result.id_map.items()},
        "external_links": [
            {"owner": _hexid(o), "context": c, "target": _hexid(t)}
            for (o, c, t) in result.external_links
        ],
        "inserted_into_cell": _hexid(cell_id) if cell_id is not None else None,
        "backup": txn.backup_path,
    }


@mcp.tool()
def esp_clone_record(plugin: str, seed_form_id: str) -> Dict[str, Any]:
    """Clone one whole record under a fresh formID, into its own top-level GRUP.

    Deep-copies the record ``seed_form_id`` (a QUST, ACTI, base object, ...),
    mints a new formID from ``HEDR.nextObjectID``, and inserts the clone
    immediately after the seed in the seed's own container -- so a top-level
    record's clone lands in the same top-level GRUP. Bumps ``HEDR.num_records``.
    Unlike ``esp_clone_cluster`` it does NOT remap links or move anything: use it
    to duplicate a record, then rewrite the clone's subrecords (EDID / VMAD / ...)
    with the subrecord tools. This is how you author a new quest, activator, or
    base object headlessly. Runs inside a safety transaction (lock -> backup ->
    edit -> verify walk-clean -> restore-on-dirty).
    """
    host = _make_host()
    path = _resolve(host, plugin)
    seed = _coerce_form_id(seed_form_id)

    with safety.transaction(host, path) as txn:
        plug = parse_plugin(txn.data)
        clone, new_id = edit.clone_record(plug, seed)
        txn.data = serialize(plug)

    return {
        "plugin": path,
        "seed": _hexid(seed),
        "new_form_id": _hexid(new_id),
        "sig": clone.sig,
        "backup": txn.backup_path,
    }


@mcp.tool()
def esp_insert_ref(
    plugin: str,
    cell_form_id: str,
    base: str,
    pos: Sequence[float],
    rot: Sequence[float],
    flags: int = FLAG_PERSISTENT,
    xlkr: Optional[Sequence[Sequence[str]]] = None,
) -> Dict[str, Any]:
    """Build a fresh REFR and splice it into a CELL's persistent-children GRUP.

    A new formID is allocated from ``HEDR.nextObjectID``; the REFR names ``base``
    (a base-object formID) via NAME, is placed at ``pos``/``rot`` (float32), and
    carries any ``xlkr`` linked references (each a ``[keyword_formid,
    ref_formid]`` pair). ``HEDR.num_records`` is bumped by
    :func:`~chim.esp.edit.insert_records`.

    Runs inside a safety transaction (lock -> backup -> edit -> verify ->
    restore-on-dirty).
    """
    host = _make_host()
    path = _resolve(host, plugin)
    cell = _coerce_form_id(cell_form_id)
    base_id = _coerce_form_id(base)
    pos_t = [float(v) for v in pos]
    rot_t = [float(v) for v in rot]
    xlkr_pairs = [
        (_coerce_form_id(kw), _coerce_form_id(ref)) for kw, ref in (xlkr or [])
    ]

    with safety.transaction(host, path) as txn:
        plug = parse_plugin(txn.data)
        # Allocate one fresh formID via a 1-record clone-free path: reuse the
        # edit layer's allocator by cloning nothing -- instead, mint an id the
        # same way insert flow expects by pulling from HEDR through build.
        new_ids, fl, hedr_field, hedr = edit._alloc_form_ids(plug, 1)
        edit._write_hedr(plug, fl, hedr_field, hedr)
        refr = edit.build_refr(
            new_ids[0], base_id, pos_t, rot_t,
            flags=int(flags), xlkr=xlkr_pairs,
        )
        edit.insert_records(plug, cell, [refr])
        txn.data = serialize(plug)

    return {
        "plugin": path,
        "cell": _hexid(cell),
        "base": _hexid(base_id),
        "new_form_id": _hexid(new_ids[0]),
        "pos": pos_t,
        "rot": rot_t,
        "backup": txn.backup_path,
    }


@mcp.tool()
def esp_delete_records(plugin: str, form_ids: Sequence[str]) -> Dict[str, Any]:
    """Delete a (possibly non-contiguous) set of records by formID.

    Records vanish from their container GRUPs (sizes re-flow on serialize) and
    ``HEDR.num_records`` is decremented once per removed record. Runs inside a
    safety transaction (lock -> backup -> edit -> verify -> restore-on-dirty).
    Returns the formIDs actually removed.
    """
    host = _make_host()
    path = _resolve(host, plugin)
    ids = [_coerce_form_id(v) for v in form_ids]

    with safety.transaction(host, path) as txn:
        plug = parse_plugin(txn.data)
        removed = edit.delete_records(plug, ids)
        txn.data = serialize(plug)

    return {
        "plugin": path,
        "requested": [_hexid(i) for i in ids],
        "removed": [_hexid(i) for i in removed],
        "backup": txn.backup_path,
    }


@mcp.tool()
def esp_delete_cells(
    plugin: str,
    cell_form_ids: Sequence[str],
) -> Dict[str, Any]:
    """Revert whole CELL overrides to vanilla by dropping them from the plugin.

    For each cell formID, deletes the CELL record **and** its child block (its
    terrain, navmesh, and refs), so the game falls back to the master's version
    of that cell -- the clean way to remove a mod's dirty/leftover cell edits.
    Empty container GRUPs are left in place. Runs inside a safety transaction
    (lock -> backup -> edit -> verify walk-clean -> restore-on-dirty).
    """
    host = _make_host()
    path = _resolve(host, plugin)
    cells = [_coerce_form_id(c) for c in cell_form_ids]
    with safety.transaction(host, path) as txn:
        plug = parse_plugin(txn.data)
        removed = edit.delete_cells(plug, cells)
        txn.data = serialize(plug)
    return {
        "plugin": path,
        "reverted": [{"cell": _hexid(c), "records_removed": n} for c, n in removed],
        "count": len(removed),
        "backup": txn.backup_path,
    }


# --------------------------------------------------------------------------- #
# Subrecord (field) CRUD -- the general subrecord editor
# --------------------------------------------------------------------------- #

@mcp.tool()
def esp_get_subrecords(
    plugin: str,
    form_id: str,
    signature: Optional[str] = None,
) -> Dict[str, Any]:
    """Read-only: list a record's subrecords (fields) with raw payload hex.

    Decompresses the record first if needed. Each field carries its 4-char
    ``sig``, its occurrence ``index`` for that signature (0, 1, 2, ...), ``size``,
    and full ``payload_hex``. Pass ``signature`` (e.g. ``"XLKR"``) to filter --
    the ``index`` values are still the true whole-record occurrence numbers, so
    they can be fed straight back to the mutating subrecord tools.

    No transaction (this only reads). Use this to plan a surgical edit: read the
    field, compute the new bytes, then ``esp_patch_subrecord`` / ``_set`` / etc.
    """
    host = _make_host()
    plug = _load(host, plugin)
    fid = _coerce_form_id(form_id)
    rec = query.find_by_formid(plug, fid)
    if rec is None:
        raise ValueError(f"no record with formID {_hexid(fid)} in {plugin!r}")
    flds = subrecords.read_fields(rec)
    only = _sig_bytes(signature) if signature is not None else None
    view = _field_view(flds, only=only)
    return {
        "plugin": _resolve(host, plugin),
        "form_id": _hexid(fid),
        "sig": rec.sig,
        "is_compressed": rec.is_compressed,
        "field_count": len(flds),
        "count": len(view),
        "fields": view,
    }


@mcp.tool()
def esp_set_subrecord(
    plugin: str,
    form_id: str,
    signature: str,
    payload_hex: str,
    index: int = 0,
) -> Dict[str, Any]:
    """Replace the ``index``-th ``signature`` subrecord's whole payload.

    ``payload_hex`` is the new field bytes as hex. The field may grow or shrink
    (XXXX is re-derived automatically). The record is stored uncompressed if it
    was compressed. Runs inside a safety transaction (lock -> backup -> edit ->
    verify walk-clean -> restore-on-dirty).
    """
    host = _make_host()
    path = _resolve(host, plugin)
    fid = _coerce_form_id(form_id)
    sig = _sig_bytes(signature)
    payload = _unhex(payload_hex)

    with safety.transaction(host, path) as txn:
        plug = parse_plugin(txn.data)
        rec = query.find_by_formid(plug, fid)
        if rec is None:
            raise ValueError(f"no record with formID {_hexid(fid)} in {plugin!r}")
        subrecords.set_subrecord(rec, sig, payload, int(index))
        txn.data = serialize(plug)

    return {
        "plugin": path,
        "form_id": _hexid(fid),
        "signature": signature,
        "index": int(index),
        "new_size": len(payload),
        "backup": txn.backup_path,
    }


@mcp.tool()
def esp_patch_subrecord(
    plugin: str,
    form_id: str,
    signature: str,
    offset: int,
    bytes_hex: str,
    index: int = 0,
) -> Dict[str, Any]:
    """Surgically overwrite bytes inside a subrecord at ``offset`` (same length).

    The precision editor: flip an ``MGEF DATA`` flag byte, set a magic-skill
    byte, retarget a formID inside a fixed struct -- without disturbing the rest
    of the field. ``bytes_hex`` must fit within the existing payload (use
    ``esp_set_subrecord`` to change a field's length). Runs inside a safety
    transaction (lock -> backup -> edit -> verify -> restore-on-dirty).
    """
    host = _make_host()
    path = _resolve(host, plugin)
    fid = _coerce_form_id(form_id)
    sig = _sig_bytes(signature)
    new = _unhex(bytes_hex)

    with safety.transaction(host, path) as txn:
        plug = parse_plugin(txn.data)
        rec = query.find_by_formid(plug, fid)
        if rec is None:
            raise ValueError(f"no record with formID {_hexid(fid)} in {plugin!r}")
        subrecords.patch_subrecord(rec, sig, int(offset), new, int(index))
        txn.data = serialize(plug)

    return {
        "plugin": path,
        "form_id": _hexid(fid),
        "signature": signature,
        "index": int(index),
        "offset": int(offset),
        "wrote": len(new),
        "backup": txn.backup_path,
    }


@mcp.tool()
def esp_insert_subrecord(
    plugin: str,
    form_id: str,
    signature: str,
    payload_hex: str,
    after: Optional[str] = None,
    before: Optional[str] = None,
    at_index: Optional[int] = None,
) -> Dict[str, Any]:
    """Insert a new ``signature`` subrecord carrying ``payload_hex``.

    Position (give at most one): ``after`` a signature (after its LAST
    occurrence -- e.g. append ``LNAM`` to a ``FLST``), ``before`` a signature
    (before its FIRST occurrence), ``at_index`` (absolute), or none to append.
    Runs inside a safety transaction (lock -> backup -> edit -> verify ->
    restore-on-dirty).
    """
    host = _make_host()
    path = _resolve(host, plugin)
    fid = _coerce_form_id(form_id)
    sig = _sig_bytes(signature)
    payload = _unhex(payload_hex)

    with safety.transaction(host, path) as txn:
        plug = parse_plugin(txn.data)
        rec = query.find_by_formid(plug, fid)
        if rec is None:
            raise ValueError(f"no record with formID {_hexid(fid)} in {plugin!r}")
        pos = subrecords.insert_subrecord(
            rec, sig, payload,
            after=_sig_bytes(after) if after is not None else None,
            before=_sig_bytes(before) if before is not None else None,
            at_index=int(at_index) if at_index is not None else None,
        )
        txn.data = serialize(plug)

    return {
        "plugin": path,
        "form_id": _hexid(fid),
        "signature": signature,
        "inserted_at": pos,
        "new_size": len(payload),
        "backup": txn.backup_path,
    }


@mcp.tool()
def esp_delete_subrecord(
    plugin: str,
    form_id: str,
    signature: str,
    index: int = 0,
) -> Dict[str, Any]:
    """Delete the ``index``-th ``signature`` subrecord from a record.

    Runs inside a safety transaction (lock -> backup -> edit -> verify ->
    restore-on-dirty).
    """
    host = _make_host()
    path = _resolve(host, plugin)
    fid = _coerce_form_id(form_id)
    sig = _sig_bytes(signature)

    with safety.transaction(host, path) as txn:
        plug = parse_plugin(txn.data)
        rec = query.find_by_formid(plug, fid)
        if rec is None:
            raise ValueError(f"no record with formID {_hexid(fid)} in {plugin!r}")
        removed = subrecords.delete_subrecord(rec, sig, int(index))
        txn.data = serialize(plug)

    return {
        "plugin": path,
        "form_id": _hexid(fid),
        "signature": signature,
        "index": int(index),
        "removed_size": removed.size,
        "backup": txn.backup_path,
    }


# --------------------------------------------------------------------------- #
# Domain helper: wire orphaned weapon racks
# --------------------------------------------------------------------------- #

#: vanilla WeaponRack*PlayerHouse rack-base -> activator-base pairs.
_WEAPON_RACK_ACTIVATOR_BASE = {
    0x000DB2A3: 0x000DB2A2,   # WeaponRackPlaque
    0x000E49C8: 0x000E49C9,   # WeaponRackCOALeft
    0x000E49CA: 0x000E49CB,   # WeaponRackCOAMid
    0x000E49CC: 0x000E49CD,   # WeaponRackCOARight
    0x000E946E: 0x000E946D,   # WeaponRackDisplay      (large angled case)
    0x000D505B: 0x000D5081,   # WeaponRackDaggerDisplay (small angled case)
}
_WRACK_ACT_TO_RACK_KW = 0x0006D95E   # activator -> rack (WRackTrigger)
_WRACK_RACK_TO_ACT_KW = 0x0006D95F   # rack -> activator (WRackActivator)


def _wire_racks_in_tree(plug, cell_form_id: int,
                        rack_form_ids: Sequence[int]) -> List[Dict[str, Any]]:
    """Mint + cross-link an activator for each orphaned rack (operates on a
    parsed :class:`Plugin`; the caller owns the transaction). Returns the
    ``[{rack, activator, activator_base}]`` wired. Racks already carrying a WRack
    link are skipped."""
    jobs = []
    for rf in rack_form_ids:
        rec = query.find_by_formid(plug, rf)
        if rec is None:
            raise ValueError(f"rack {_hexid(rf)} not found")
        fl = query.record_fields(rec)
        if any(f.signature == b"XLKR"
               and fields.decode_xlkr(f.payload).keyword_form_id
               in (_WRACK_ACT_TO_RACK_KW, _WRACK_RACK_TO_ACT_KW) for f in fl):
            continue  # already wired
        name = fields.find_field(fl, b"NAME")
        base = fields.decode_name(name.payload) if name is not None else None
        act_base = _WEAPON_RACK_ACTIVATOR_BASE.get(base)
        if act_base is None:
            raise ValueError(f"rack {_hexid(rf)} base {_hexid(base or 0)} is not "
                             "a known weapon-rack rack base")
        data = fields.find_field(fl, b"DATA")
        pr = list(fields.decode_data_posrot(data.payload).as_tuple())
        jobs.append((rec, rf, act_base, pr))

    wired: List[Dict[str, Any]] = []
    if not jobs:
        return wired
    new_ids, fl2, hedr_field, hedr = edit._alloc_form_ids(plug, len(jobs))
    edit._write_hedr(plug, fl2, hedr_field, hedr)
    activators = []
    for (rec, rf, act_base, pr), aid in zip(jobs, new_ids):
        activators.append(edit.build_refr(
            aid, act_base, pr[:3], pr[3:],
            xlkr=[(_WRACK_ACT_TO_RACK_KW, rf)],
        ))
        subrecords.insert_subrecord(
            rec, b"XLKR",
            fields.encode_xlkr(fields.LinkedRef(_WRACK_RACK_TO_ACT_KW, aid)),
            after=b"NAME",
        )
        wired.append({"rack": _hexid(rf), "activator": _hexid(aid),
                      "activator_base": _hexid(act_base)})
    edit.insert_records(plug, cell_form_id, activators)
    return wired


@mcp.tool()
def esp_wire_weapon_racks(
    plugin: str,
    cell_form_id: str,
    rack_form_ids: Sequence[str],
) -> Dict[str, Any]:
    """Fix orphaned weapon RACKS by minting each a co-located, linked activator.

    A functional vanilla weapon rack is a visible RACK ref plus an invisible
    ACTIVATOR ref at the same spot, cross-linked (rack->activator via keyword
    0x6D95F, activator->rack via 0x6D95E). The CK drops the activator when a rack
    is duplicated, leaving a rack that shows but never prompts. For each rack
    formID this reads its base + position, looks up the matching vanilla activator
    base (plaque / COA L-M-R / display / dagger), mints a fresh activator REFR at
    the same spot force-enabled (flags 0x400, so it prompts in an always-loaded
    cell), links the pair both ways, and splices the activator into
    ``cell_form_id``'s persistent-children GRUP. Racks already linked are skipped.
    One safety transaction (lock -> backup -> edit -> verify -> restore-on-dirty).
    """
    host = _make_host()
    path = _resolve(host, plugin)
    cell = _coerce_form_id(cell_form_id)
    racks = [_coerce_form_id(r) for r in rack_form_ids]

    with safety.transaction(host, path) as txn:
        plug = parse_plugin(txn.data)
        wired = _wire_racks_in_tree(plug, cell, racks)
        txn.data = serialize(plug)

    return {"plugin": path, "cell": _hexid(cell), "count": len(wired),
            "wired": wired, "backup": txn.backup_path}


@mcp.tool()
def papyrus_compile(
    script_name: str,
    import_paths: Optional[Sequence[str]] = None,
    output_dir: Optional[str] = None,
    flags_file: str = papyrus.DEFAULT_FLAGS_FILE,
) -> Dict[str, Any]:
    """Compile a Papyrus script on the remote host (Creation Kit compiler).

    Requires ``CHIM_SSH_HOST`` (there is no local Papyrus compiler). Defaults to
    the SKSE-first import path and the SE flags file. Not a plugin mutation, so
    no transaction. Returns the compiler's exit status and output.
    """
    host_target = os.environ.get(ENV_SSH_HOST)
    if not host_target:
        raise ValueError(
            "papyrus_compile requires CHIM_SSH_HOST (remote CK compiler)"
        )
    result = papyrus.compile_script(
        host_target,
        script_name,
        import_paths=list(import_paths) if import_paths is not None else None,
        output_dir=output_dir or papyrus.DEFAULT_OUTPUT_DIR,
        flags_file=flags_file,
    )
    return {
        "ok": result.ok,
        "exit_status": result.exit_status,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "command": result.command,
        "output_pex": result.output_pex,
    }


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    """Console-script / ``python -m`` entry point.

    Transport is chosen by ``CHIM_TRANSPORT``:

    * unset / ``"stdio"`` -> stdio (a local MCP client spawns this process).
    * ``"http"`` / ``"streamable-http"`` / ``"sse"`` -> a network server bound to
      ``CHIM_HTTP_HOST`` (default ``127.0.0.1``) : ``CHIM_HTTP_PORT`` (default
      ``8765``). Bind to the private-network-reachable address and firewall the port to
      trusted sources -- there is no built-in auth yet.

    DNS-rebinding protection (FastMCP's ``Host`` guard) is configured explicitly
    here: FastMCP otherwise fixes it at *construction* from the default host
    (``127.0.0.1``), so a server that later binds a routable address rejects the
    real advertised Host with ``421``. ``CHIM_HTTP_ALLOWED_HOSTS`` (comma-separated
    Host values, e.g. ``host:8765`` or the port-wildcard ``host:*``)
    turns the guard on pinned to those hosts; unset leaves it off and relies on the
    private network + firewall scope as the perimeter.
    """
    transport = os.environ.get("CHIM_TRANSPORT", "stdio").lower()
    if transport in ("http", "streamable-http", "sse"):
        from mcp.server.transport_security import TransportSecuritySettings

        mcp.settings.host = os.environ.get("CHIM_HTTP_HOST", "127.0.0.1")
        mcp.settings.port = int(os.environ.get("CHIM_HTTP_PORT", "8765"))

        allowed = os.environ.get("CHIM_HTTP_ALLOWED_HOSTS", "").strip()
        if allowed:
            hosts = [h.strip() for h in allowed.split(",") if h.strip()]
            mcp.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=hosts,
                allowed_origins=[f"http://{h}" for h in hosts]
                + [f"https://{h}" for h in hosts],
            )
        else:
            mcp.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=False,
            )
        mcp.run(transport="sse" if transport == "sse" else "streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
