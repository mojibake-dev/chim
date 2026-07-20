"""MCP server exposing ``chim``'s headless ESP/ESM editing over the wire.

This wraps the :mod:`esplib` plugin engine (via the thin :mod:`chim.esp.ops`
adapter) as a set of MCP tools so an agent can query and mutate a live Skyrim
Special Edition plugin on the remote Windows host.

Design
------
* **One FastMCP app.** Read-only tools (``esp_query``, ``esp_get_record``,
  ``esp_get_subrecords``, ``esp_get_vmad``) parse the plugin and answer
  questions without ever writing. Mutating tools run their whole edit inside
  :func:`chim.esp.safety.transaction` -- lock-check, backup, edit-bytes,
  write-back, verify walk-clean, restore-on-dirty. ``papyrus_compile`` drives the
  Creation Kit compiler on the host; it changes no plugin bytes so it needs no
  transaction.
* **esplib is the byte engine.** ``safety.transaction`` yields the current file
  bytes as ``txn.data``; every mutating tool does
  ``plug = ops.load(txn.data)`` -> edit -> ``txn.data = ops.serialize(plug)``.
  esplib round-trips byte-identical and recomputes ``HEDR.num_records`` on
  serialize, so no manual header bookkeeping is needed here.
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
from typing import Any, Dict, List, Optional, Sequence

from mcp.server.fastmcp import FastMCP

from .esp import ops, papyrus, safety
from .save import analysis as save_analysis
from .tes3 import ops as tes3_ops, analysis as tes3_analysis
from . import modinstall


# --------------------------------------------------------------------------- #
# Host / path configuration from the environment
# --------------------------------------------------------------------------- #

#: SSH target for the remote Windows host, e.g. ``user@host``.
ENV_SSH_HOST = "CHIM_SSH_HOST"
#: Skyrim SE ``Data`` directory plugin names are resolved against.
ENV_DATA_DIR = "CHIM_DATA_DIR"
#: Skyrim SE ``Saves`` directory ``.ess`` names are resolved against.
ENV_SAVE_DIR = "CHIM_SAVE_DIR"
#: Morrowind ``Data Files`` directory TES3 plugin names resolve against (OpenMW).
ENV_MW_DATA_DIR = "CHIM_MW_DATA_DIR"
#: openmw.cfg / Skyrim plugins.txt paths, mod-manifest dir, mod-archive source.
ENV_OPENMW_CFG = "CHIM_OPENMW_CFG"
ENV_PLUGINS_TXT = "CHIM_PLUGINS_TXT"
ENV_MOD_MANIFESTS = "CHIM_MOD_MANIFESTS"
ENV_DOWNLOADS = "CHIM_DOWNLOADS"


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
    return ops.load(host.read_file(path))


def _mw_data_dir(host: safety.Host) -> str:
    """Directory TES3 (Morrowind/OpenMW) plugin names resolve against.

    Prefers ``CHIM_MW_DATA_DIR``; otherwise the stock Steam Morrowind
    ``Data Files`` on a Windows/remote host, or the working directory locally.
    """
    override = os.environ.get(ENV_MW_DATA_DIR)
    if override:
        return override
    if host.windows:
        return safety.OPENMW_DEFAULT_DATA_DIR
    return os.getcwd()


def _resolve_tes3(host: safety.Host, plugin: str) -> str:
    """Resolve a TES3 plugin *name* to a full path on ``host`` (abs paths pass through)."""
    looks_absolute = (
        plugin.startswith("/")
        or plugin.startswith("\\\\")
        or (len(plugin) >= 2 and plugin[1] == ":")
    )
    if looks_absolute:
        return plugin
    directory = _mw_data_dir(host)
    if host.windows:
        return directory.rstrip("\\/") + "\\" + plugin
    return os.path.join(directory, plugin)


def _save_dir(host: safety.Host) -> str:
    """The directory ``.ess`` save names resolve against for ``host``.

    Prefers ``CHIM_SAVE_DIR``. Otherwise the stock per-user SE saves path on a
    Windows/remote host (``…\\Documents\\My Games\\Skyrim Special Edition\\Saves``
    under the current profile), or the working directory locally.
    """
    override = os.environ.get(ENV_SAVE_DIR)
    if override:
        return override
    if host.windows:
        prof = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        return prof + r"\Documents\My Games\Skyrim Special Edition\Saves"
    return os.getcwd()


def _resolve_save(host: safety.Host, save: str) -> str:
    """Resolve an ``.ess`` *name* to a full path on ``host`` (abs paths pass through)."""
    looks_absolute = (
        save.startswith("/")
        or save.startswith("\\\\")
        or (len(save) >= 2 and save[1] == ":")
    )
    if looks_absolute:
        return save
    directory = _save_dir(host)
    if host.windows:
        return directory.rstrip("\\/") + "\\" + save
    return os.path.join(directory, save)


def _read_save(host: safety.Host, save: str) -> bytes:
    """Read raw ``.ess`` bytes from ``host`` (read-only; saves are 5–50 MB)."""
    return host.read_file(_resolve_save(host, save))


def _default_clean_out(path: str) -> str:
    """Derive a sibling ``<name>_CHIMCLEAN.ess`` path (never clobbers the original)."""
    d, b = os.path.split(path)
    stem, ext = os.path.splitext(b)
    sep = "\\" if ("\\" in path or (len(path) >= 2 and path[1] == ":")) else "/"
    return (d + sep if d else "") + stem + "_CHIMCLEAN" + (ext or ".ess")


def _cosave_path(ess_path: str) -> str:
    """The paired SKSE co-save path (``.skse``) for an ``.ess`` file."""
    return os.path.splitext(ess_path)[0] + ".skse"


# --------------------------------------------------------------------------- #
# Serialization helpers: turn esplib objects into JSON-friendly dicts
# --------------------------------------------------------------------------- #

def _hexid(form_id: int) -> str:
    return f"0x{form_id & 0xFFFFFFFF:08X}"


def _record_summary(rec) -> Dict[str, Any]:
    """A compact, JSON-safe summary of a record (no raw payload bytes)."""
    flags = int(rec.flags)
    return {
        "sig": rec.signature,
        "form_id": rec.form_id.value,
        "form_id_hex": _hexid(rec.form_id.value),
        "flags": flags,
        "flags_hex": _hexid(flags),
        "data_size": sum(sr.size for sr in rec.subrecords),
        "is_compressed": bool(rec.is_compressed),
        "is_deleted": bool(flags & ops.FLAG_DELETED),
        "is_persistent": bool(flags & ops.FLAG_PERSISTENT),
        "edid": rec.editor_id,
    }


def _record_detail(rec) -> Dict[str, Any]:
    """Full record view: summary + decoded field list."""
    import struct

    out = _record_summary(rec)
    field_views: List[Dict[str, Any]] = []
    for sr in rec.subrecords:
        view: Dict[str, Any] = {"sig": sr.signature, "size": sr.size}
        payload = sr.data
        try:
            if sr.signature == "EDID":
                view["value"] = payload.split(b"\x00", 1)[0].decode("ascii", "replace")
            elif sr.signature == "NAME" and sr.size >= 4:
                view["value"] = _hexid(struct.unpack("<I", payload[:4])[0])
            elif sr.signature == "MODL":
                view["value"] = payload.split(b"\x00", 1)[0].decode("ascii", "replace")
            elif sr.signature == "OBND" and sr.size >= 12:
                view["value"] = list(struct.unpack("<6h", payload[:12]))
            elif sr.signature == "DATA" and sr.size == 24:
                view["value"] = list(struct.unpack("<6f", payload))
            elif sr.signature == "XLKR" and sr.size >= 8:
                kw, ref = struct.unpack("<II", payload[:8])
                view["value"] = {"keyword": _hexid(kw), "ref": _hexid(ref)}
            elif sr.signature == "XESP" and sr.size >= 8:
                parent, xflags = struct.unpack("<II", payload[:8])
                view["value"] = {"parent": _hexid(parent), "flags": xflags}
            elif sr.signature == "XSCL" and sr.size >= 4:
                view["value"] = struct.unpack("<f", payload[:4])[0]
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
    "deleted": ops.FLAG_DELETED,
    "persistent": ops.FLAG_PERSISTENT,
    "initially_disabled": ops.FLAG_INITIALLY_DISABLED,
    "disabled": ops.FLAG_INITIALLY_DISABLED,
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


def _sig_str(sig: str) -> str:
    """Validate + return a 4-char subrecord signature string (``"XLKR"``)."""
    s = str(sig)
    try:
        s.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError(f"subrecord signature must be ASCII, got {sig!r}")
    if len(s) != 4:
        raise ValueError(f"subrecord signature must be 4 ASCII chars, got {sig!r}")
    return s


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
    * ``"cellrefs"``-- every placement inside a CELL, with base/flags/xlkr.
    * ``"basemap"``-- ``formid -> (edid, sig, mesh, obnd)`` for records whose
      signature is in ``sigs`` (required for this mode).

    Returns ``{"count": N, "records": [...]}`` of record summaries (or, for
    ``basemap``, ``{"bases": {...}}``; for ``cellrefs``, ``{"cell", "count",
    "refs"}``).
    """
    host = _make_host()
    plug = _load(host, plugin)
    mode = by.lower()

    if mode == "formid":
        rec = ops.find_record(plug, _coerce_form_id(value))
        recs = [rec] if rec is not None else []
    elif mode == "edid":
        recs = ops.find_by_edid(plug, value)
    elif mode == "base":
        recs = ops.find_by_base(plug, _coerce_form_id(value))
    elif mode == "cell":
        try:
            key: Any = _coerce_form_id(value)
        except ValueError:
            key = value
        rec = ops.find_by_cell(plug, key)
        recs = [rec] if rec is not None else []
    elif mode == "basemap":
        if not sigs:
            raise ValueError("esp_query by='basemap' requires 'sigs'")
        bmap = ops.base_map(plug, tuple(sigs))
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
        cell = ops.find_by_cell(plug, cell_key)
        if cell is None:
            return {"cell": None, "count": 0, "refs": []}
        out_refs: List[Dict[str, Any]] = []
        for r in ops.cell_refs(plug, cell.form_id.value):
            v = ops.cell_ref_view(r)
            out_refs.append({
                "form_id": _hexid(v["form_id"]),
                "sig": v["sig"],
                "base": _hexid(v["base"]) if v["base"] is not None else None,
                "flags_hex": _hexid(v["flags"]),
                "edid": v["edid"],
                "persistent": v["persistent"],
                "initially_disabled": v["initially_disabled"],
                "has_xesp": v["has_xesp"],
                "xlkr": [{"keyword": _hexid(kw), "ref": _hexid(ref)}
                         for kw, ref in v["xlkr"]],
            })
        return {"cell": _hexid(cell.form_id.value), "count": len(out_refs),
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
    rec = ops.find_record(plug, fid)
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
        plug = ops.load(txn.data)
        rec = ops.find_record(plug, fid)
        if rec is None:
            raise ValueError(f"no record with formID {_hexid(fid)} in {plugin!r}")
        ops.set_flag(plug, fid, flag_bits, clear=clear)
        txn.data = ops.serialize(plug)

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
        plug = ops.load(txn.data)
        ops.move_ref(plug, fid, pos_t, rot_t)
        txn.data = ops.serialize(plug)

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
    persistent-children GRUP; otherwise the clones are created but left
    unattached.

    Runs inside a safety transaction (lock -> backup -> edit -> verify ->
    restore-on-dirty).
    """
    host = _make_host()
    path = _resolve(host, plugin)
    seed = _coerce_form_id(seed_form_id)
    trans = [float(v) for v in translate]

    with safety.transaction(host, path) as txn:
        plug = ops.load(txn.data)
        cell_id: Optional[int] = None
        if into_cell is not None:
            cell_id = _coerce_form_id(into_cell)
        result = ops.clone_cluster(plug, seed, int(count), trans,
                                   into_cell=cell_id)
        txn.data = ops.serialize(plug)

    return {
        "plugin": path,
        "seed": _hexid(seed),
        "count": int(count),
        "new_form_ids": [_hexid(r.form_id.value) for r in result.records],
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
    record's clone lands in the same top-level GRUP. Unlike ``esp_clone_cluster``
    it does NOT remap links or move anything: use it to duplicate a record, then
    rewrite the clone's subrecords (EDID / VMAD / ...) with the subrecord tools.
    This is how you author a new quest, activator, or base object headlessly.
    Runs inside a safety transaction (lock -> backup -> edit -> verify
    walk-clean -> restore-on-dirty).
    """
    host = _make_host()
    path = _resolve(host, plugin)
    seed = _coerce_form_id(seed_form_id)

    with safety.transaction(host, path) as txn:
        plug = ops.load(txn.data)
        clone, new_id = ops.clone_record(plug, seed)
        txn.data = ops.serialize(plug)

    return {
        "plugin": path,
        "seed": _hexid(seed),
        "new_form_id": _hexid(new_id),
        "sig": clone.signature,
        "backup": txn.backup_path,
    }


@mcp.tool()
def esp_insert_ref(
    plugin: str,
    cell_form_id: str,
    base: str,
    pos: Sequence[float],
    rot: Sequence[float],
    flags: int = ops.FLAG_PERSISTENT,
    xlkr: Optional[Sequence[Sequence[str]]] = None,
) -> Dict[str, Any]:
    """Build a fresh REFR and splice it into a CELL's persistent-children GRUP.

    A new formID is allocated from ``HEDR.nextObjectID``; the REFR names ``base``
    (a base-object formID) via NAME, is placed at ``pos``/``rot`` (float32), and
    carries any ``xlkr`` linked references (each a ``[keyword_formid,
    ref_formid]`` pair).

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
        plug = ops.load(txn.data)
        new_id = ops.insert_ref(
            plug, cell, base_id, pos_t, rot_t,
            xlkr=xlkr_pairs, flags=int(flags),
        )
        txn.data = ops.serialize(plug)

    return {
        "plugin": path,
        "cell": _hexid(cell),
        "base": _hexid(base_id),
        "new_form_id": _hexid(new_id),
        "pos": pos_t,
        "rot": rot_t,
        "backup": txn.backup_path,
    }


@mcp.tool()
def esp_delete_records(plugin: str, form_ids: Sequence[str]) -> Dict[str, Any]:
    """Delete a (possibly non-contiguous) set of records by formID.

    Records vanish from their container GRUPs (sizes re-flow on serialize) and
    ``HEDR.num_records`` is recomputed. Runs inside a safety transaction (lock ->
    backup -> edit -> verify -> restore-on-dirty). Returns the formIDs actually
    removed.
    """
    host = _make_host()
    path = _resolve(host, plugin)
    ids = [_coerce_form_id(v) for v in form_ids]

    with safety.transaction(host, path) as txn:
        plug = ops.load(txn.data)
        removed = ops.delete_records(plug, ids)
        txn.data = ops.serialize(plug)

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
        plug = ops.load(txn.data)
        removed = ops.delete_cells(plug, cells)
        txn.data = ops.serialize(plug)
    return {
        "plugin": path,
        "reverted": [{"cell": _hexid(c), "records_removed": n} for c, n in removed],
        "count": len(removed),
        "backup": txn.backup_path,
    }


# --------------------------------------------------------------------------- #
# Subrecord (field) CRUD -- the general subrecord editor
# --------------------------------------------------------------------------- #

def _field_view(rec, only: Optional[str] = None) -> List[Dict[str, Any]]:
    """JSON view of a record's fields with per-signature occurrence indices."""
    return ops.field_views(rec, only=only)


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
    rec = ops.find_record(plug, fid)
    if rec is None:
        raise ValueError(f"no record with formID {_hexid(fid)} in {plugin!r}")
    only = _sig_str(signature) if signature is not None else None
    view = _field_view(rec, only=only)
    return {
        "plugin": _resolve(host, plugin),
        "form_id": _hexid(fid),
        "sig": rec.signature,
        "is_compressed": bool(rec.is_compressed),
        "field_count": len(rec.subrecords),
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
    sig = _sig_str(signature)
    payload = _unhex(payload_hex)

    with safety.transaction(host, path) as txn:
        plug = ops.load(txn.data)
        rec = ops.find_record(plug, fid)
        if rec is None:
            raise ValueError(f"no record with formID {_hexid(fid)} in {plugin!r}")
        ops.set_subrecord(rec, sig, payload, int(index))
        txn.data = ops.serialize(plug)

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
    sig = _sig_str(signature)
    new = _unhex(bytes_hex)

    with safety.transaction(host, path) as txn:
        plug = ops.load(txn.data)
        rec = ops.find_record(plug, fid)
        if rec is None:
            raise ValueError(f"no record with formID {_hexid(fid)} in {plugin!r}")
        ops.patch_subrecord(rec, sig, int(offset), new, int(index))
        txn.data = ops.serialize(plug)

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
    sig = _sig_str(signature)
    payload = _unhex(payload_hex)

    with safety.transaction(host, path) as txn:
        plug = ops.load(txn.data)
        rec = ops.find_record(plug, fid)
        if rec is None:
            raise ValueError(f"no record with formID {_hexid(fid)} in {plugin!r}")
        pos = ops.insert_subrecord(
            rec, sig, payload,
            after=_sig_str(after) if after is not None else None,
            before=_sig_str(before) if before is not None else None,
            at_index=int(at_index) if at_index is not None else None,
        )
        txn.data = ops.serialize(plug)

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
    sig = _sig_str(signature)

    with safety.transaction(host, path) as txn:
        plug = ops.load(txn.data)
        rec = ops.find_record(plug, fid)
        if rec is None:
            raise ValueError(f"no record with formID {_hexid(fid)} in {plugin!r}")
        removed = ops.delete_subrecord(rec, sig, int(index))
        txn.data = ops.serialize(plug)

    return {
        "plugin": path,
        "form_id": _hexid(fid),
        "signature": signature,
        "index": int(index),
        "removed_size": removed.size,
        "backup": txn.backup_path,
    }


# --------------------------------------------------------------------------- #
# VMAD (Papyrus script data) tools
# --------------------------------------------------------------------------- #

@mcp.tool()
def esp_get_vmad(plugin: str, form_id: str) -> Dict[str, Any]:
    """Read-only: decode a record's VMAD (Papyrus script data) to JSON.

    Returns ``{plugin, form_id, sig, vmad}`` where ``vmad`` is a faithful,
    JSON-able decode of the record's VMAD subrecord (or ``None`` if it has none).
    The record's signature drives fragment + QUST-alias parsing. Object property
    and alias-object values are ``{form_id: "0x...", alias, unused}``; STRUCT
    values are nested property lists; ``*_ARRAY`` are lists; scalars are
    verbatim; formIDs are hex strings. The returned ``vmad`` is the exact shape
    ``esp_set_vmad`` accepts, so a read -> edit -> write round-trips. No
    transaction (this only reads).
    """
    host = _make_host()
    plug = _load(host, plugin)
    fid = _coerce_form_id(form_id)
    rec = ops.find_record(plug, fid)
    if rec is None:
        raise ValueError(f"no record with formID {_hexid(fid)} in {plugin!r}")
    return {
        "plugin": _resolve(host, plugin),
        "form_id": _hexid(fid),
        "sig": rec.signature,
        "vmad": ops.vmad_to_dict(rec),
    }


@mcp.tool()
def esp_set_vmad(
    plugin: str,
    form_id: str,
    vmad_json: Dict[str, Any],
) -> Dict[str, Any]:
    """Author/overwrite a record's VMAD from a JSON dict.

    ``vmad_json`` is the same shape ``esp_get_vmad`` returns (``{version,
    obj_format, scripts, fragment_data, alias_scripts}``), so a read -> edit ->
    write round-trips. The VMAD subrecord is created if the record has none.
    Object property/alias-object formIDs are hex strings. When ``alias_scripts``
    are present a fragment block is synthesized if absent (esplib only emits the
    trailing alias block after a non-null ``fragment_data``). Compressed records
    transparently recompress on serialize. Runs inside a safety transaction
    (lock -> backup -> edit -> verify walk-clean -> restore-on-dirty).
    """
    host = _make_host()
    path = _resolve(host, plugin)
    fid = _coerce_form_id(form_id)
    if not isinstance(vmad_json, dict):
        raise ValueError("vmad_json must be an object (the shape esp_get_vmad returns)")

    with safety.transaction(host, path) as txn:
        plug = ops.load(txn.data)
        rec = ops.find_record(plug, fid)
        if rec is None:
            raise ValueError(f"no record with formID {_hexid(fid)} in {plugin!r}")
        ops.vmad_from_dict(rec, vmad_json)
        txn.data = ops.serialize(plug)

    return {
        "plugin": path,
        "form_id": _hexid(fid),
        "sig": rec.signature,
        "backup": txn.backup_path,
    }


# --------------------------------------------------------------------------- #
# Master-list tools
# --------------------------------------------------------------------------- #

@mcp.tool()
def esp_add_master(plugin: str, master_name: str) -> Dict[str, Any]:
    """Add a master dependency to the plugin header (idempotent).

    Appends ``master_name`` to the header master list AND emits the MAST/DATA
    subrecords (esplib's ``add_master`` alone does not reach the serialized bytes
    on a loaded plugin -- see ``ops.add_master``). Adding a master does NOT
    renumber any existing formIDs. Already-present masters (case-insensitive) are
    a no-op. Runs inside a safety transaction (lock -> backup -> edit -> verify
    walk-clean -> restore-on-dirty). Returns the resulting ``masters`` list.
    """
    host = _make_host()
    path = _resolve(host, plugin)

    with safety.transaction(host, path) as txn:
        plug = ops.load(txn.data)
        masters = ops.add_master(plug, str(master_name))
        txn.data = ops.serialize(plug)

    return {
        "plugin": path,
        "master": str(master_name),
        "masters": masters,
        "backup": txn.backup_path,
    }


@mcp.tool()
def esp_rename_master(
    plugin: str,
    old_name: str,
    new_name: str,
) -> Dict[str, Any]:
    """Rename an existing master in the plugin header (touches no formIDs).

    Rewrites both the header master-list entry and its MAST subrecord. Raises if
    ``old_name`` is not a current master (case-insensitive). A rename never
    changes any formID's plugin index. Runs inside a safety transaction (lock ->
    backup -> edit -> verify walk-clean -> restore-on-dirty). Returns the
    resulting ``masters`` list.
    """
    host = _make_host()
    path = _resolve(host, plugin)

    with safety.transaction(host, path) as txn:
        plug = ops.load(txn.data)
        masters = ops.rename_master(plug, str(old_name), str(new_name))
        txn.data = ops.serialize(plug)

    return {
        "plugin": path,
        "old_name": str(old_name),
        "new_name": str(new_name),
        "masters": masters,
        "backup": txn.backup_path,
    }


# --------------------------------------------------------------------------- #
# Papyrus compilation
# --------------------------------------------------------------------------- #
#
# NOTE: ``esp_wire_weapon_racks`` was retired here in favour of the general
# ``esp_insert_ref`` + ``esp_insert_subrecord`` primitives, which compose the
# same result agent-side (mint a co-located activator REFR, cross-link the pair
# with two XLKRs). The domain-specific tool + its rack-base mapping are gone.


@mcp.tool()
def papyrus_compile(
    script_name: str,
    import_paths: Optional[Sequence[str]] = None,
    output_dir: Optional[str] = None,
    flags_file: str = papyrus.DEFAULT_FLAGS_FILE,
) -> Dict[str, Any]:
    """Compile a Papyrus script with the Creation Kit compiler.

    Two execution modes, chosen by the environment:

    * ``CHIM_SSH_HOST`` set -> compile on the remote host over SSH
      (``papyrus.compile_script``).
    * unset, on a :class:`LocalWindowsHost` (chim running *on* the remote host)
      -> compile in-process via ``cmd /c`` a local batch
      (``papyrus.compile_script_local``).

    Either way the command line, import search path (SKSE-first) and flags file
    are identical. Defaults to the SKSE-first import path and the SE flags file.
    Not a plugin mutation, so no transaction. Returns the compiler's exit status
    and output. Raises if neither ``CHIM_SSH_HOST`` nor a local Windows host is
    available (there is no CK compiler off Windows).
    """
    imports = list(import_paths) if import_paths is not None else None
    out_dir = output_dir or papyrus.DEFAULT_OUTPUT_DIR

    host_target = os.environ.get(ENV_SSH_HOST)
    if host_target:
        result = papyrus.compile_script(
            host_target,
            script_name,
            import_paths=imports,
            output_dir=out_dir,
            flags_file=flags_file,
        )
    else:
        host = _make_host()
        if not host.windows:
            raise ValueError(
                "papyrus_compile requires CHIM_SSH_HOST (remote CK compiler) or "
                "a local Windows host (LocalWindowsHost); no Papyrus compiler is "
                "available on this host"
            )
        result = papyrus.compile_script_local(
            script_name,
            import_paths=imports,
            output_dir=out_dir,
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
# Save-file (.ess) inspection — READ-ONLY (no transaction)
# --------------------------------------------------------------------------- #
#
# A save is not a plugin: these tools use chim's second byte engine
# (:mod:`chim.save`) to parse the ``.ess`` container + the Papyrus VM heap
# (GlobalData type 1001) and report script-instance hygiene. Phase 1 is
# read-only — no mutation, no transaction. ``save`` is an ``.ess`` file name
# resolved against the SE ``Saves`` dir (``CHIM_SAVE_DIR`` / the per-user
# default), or an absolute path.


@mcp.tool()
def save_info(save: str) -> Dict[str, Any]:
    """Read-only summary of a Skyrim SE ``.ess`` save.

    Parses the container (header, LZ4/zlib/none body, plugin lists, global-data
    tables) and reports the header fields, full/light plugin counts, change-form
    count, the set of global-data types present, whether the Papyrus VM heap
    (type 1001) is present and its size, and ``body_walk_clean`` (the parsed body
    re-serialises byte-identical — the save-side integrity check). No mutation.
    """
    host = _make_host()
    return save_analysis.save_info(_read_save(host, save))


@mcp.tool()
def save_count_orphans(save: str) -> Dict[str, Any]:
    """Read-only Papyrus-heap orphan tally for a ``.ess`` save.

    Decodes the type-1001 heap front and counts: total script definitions and
    instances, **undefined** definitions/instances (a script re-saved as an
    empty-type stub because its source was removed — the game's "Could not find
    type X in the type table" orphans), and **unattached** instances (RefID == 0,
    bound to nothing). ``heap_walk_clean`` reports that the decoded heap front
    re-serialises byte-identical. No mutation.
    """
    host = _make_host()
    return save_analysis.count_orphans(_read_save(host, save))


@mcp.tool()
def save_list_undefined(save: str, limit: int = 200) -> Dict[str, Any]:
    """Read-only: undefined + unattached script instances grouped by class.

    Lists which removed-mod script classes left junk in the save and how many
    instances each has (``undefined``), plus unattached instances by class
    (``unattached``) — the diagnostic to run *before* trusting any cleaner. This
    is what surfaces e.g. ``mff_refalias`` orphans from an uninstalled follower
    framework. ``limit`` caps rows per list. No mutation.
    """
    host = _make_host()
    return save_analysis.list_undefined(_read_save(host, save), limit=int(limit))


@mcp.tool()
def save_clean_orphans(
    save: str,
    mode: str = "undefined",
    dry_run: bool = True,
    out: Optional[str] = None,
) -> Dict[str, Any]:
    """Remove orphaned Papyrus script instances from a save (writes a NEW file).

    The one **mutating** save tool. ``mode="undefined"`` removes removed-mod stub
    instances + their stub script definitions (targets e.g. ``mff_refalias``);
    ``mode="unattached"`` removes every RefID==0 instance (broader, community-safe
    housekeeping). The whole file is re-serialised (heap edited, FLT + sizes
    recomputed, recompressed).

    Safety: **``dry_run=True`` by default** — reports what would be removed and
    writes nothing. A real run (``dry_run=False``) refuses if the game/CK is
    running (lock-check), **never overwrites the original** — it writes a sibling
    ``<name>_CHIMCLEAN.ess`` (override with ``out``) and copies the paired
    ``.skse`` co-save to match — then re-reads and re-parses the written file to
    verify orphans are gone. This is recovery/housekeeping tooling: like ReSaver,
    a cleaned save can still, in principle, be damaged — keep the original.
    """
    host = _make_host()
    path = _resolve_save(host, save)
    raw = host.read_file(path)
    cleaned, report = save_analysis.clean(raw, mode)
    out_path = _resolve_save(host, out) if out else _default_clean_out(path)
    report["dry_run"] = bool(dry_run)
    report["out"] = out_path

    if dry_run:
        return report

    if out_path == path:
        raise ValueError("refusing to overwrite the original save; pass a different 'out'")
    locked = safety.lock_check(host)
    if locked:
        raise ValueError(
            f"cannot write while these processes run: {locked}. Close the game/CK first."
        )

    host.write_file(out_path, cleaned)
    cosave_copied = False
    src_cosave = _cosave_path(path)
    try:
        cosave = host.read_file(src_cosave)
        host.write_file(_cosave_path(out_path), cosave)
        cosave_copied = True
    except Exception:  # no co-save present, or unreadable — non-fatal
        pass

    verify = save_analysis.count_orphans(host.read_file(out_path))
    report.update({
        "wrote": out_path,
        "cosave_copied": cosave_copied,
        "verify_body_walk_clean": verify.get("heap_walk_clean"),
        "verify_undefined_instances": verify.get("undefined_instances"),
        "verify_unattached_instances": verify.get("unattached_instances"),
    })
    return report


# --------------------------------------------------------------------------- #
# Morrowind TES3 plugin editing — chim's third byte engine (:mod:`chim.tes3`)
# --------------------------------------------------------------------------- #
#
# TES3 is a FLAT record list addressed by STRING editor-id (not a FormID), with
# no GRUP tree and no compression. Read tools parse + report; mutating tools wrap
# the same ``safety.transaction`` as the esp tools but inject the TES3 verify
# predicate (``tes3_ops.walk_clean``) and the OpenMW lock-process list. ``plugin``
# resolves against ``CHIM_MW_DATA_DIR`` (the Morrowind ``Data Files`` dir) or an
# absolute path. Records are named by ``(type, id)`` — e.g. ``("RACE", "Rotfern")``.


def _tes3_txn(host: safety.Host, path: str):
    """A ``safety.transaction`` configured for TES3/OpenMW (verify + lock set)."""
    return safety.transaction(
        host, path,
        verify_fn=tes3_ops.walk_clean,
        lock_processes=safety.OPENMW_LOCK_PROCESSES,
    )


def _find_tes3(plug, type: str, id: str):
    rec = tes3_ops.find_record(plug, _sig_str(type), id)
    if rec is None:
        raise ValueError(f"no {type!r} record with id {id!r}")
    return rec


@mcp.tool()
def tes3_info(plugin: str) -> Dict[str, Any]:
    """Read-only summary of a Morrowind TES3 plugin (``.esp``/``.esm``/``.omwaddon``).

    Parses the flat record list and reports version, file type, author,
    description, the master list, record count (stored HEDR value vs actual),
    a record-type histogram, and ``walk_clean`` (the parsed plugin re-serialises
    byte-identical — the TES3 integrity check). No mutation.
    """
    host = _make_host()
    return tes3_analysis.tes3_info(host.read_file(_resolve_tes3(host, plugin)))


@mcp.tool()
def tes3_query(plugin: str, by: str, value: str = "", limit: int = 200) -> Dict[str, Any]:
    """Read-only search over a TES3 plugin.

    ``by`` selects the mode: ``type`` (every record of a 4-char type, e.g.
    ``RACE``), ``id`` (records whose editor-id equals ``value``, case-insensitive),
    ``id_regex`` (editor-id matches the regex ``value``), or ``master`` (the
    MAST/DATA master list). Returns compact record summaries; ``limit`` caps rows
    (with a ``truncated`` flag). No mutation.
    """
    host = _make_host()
    path = _resolve_tes3(host, plugin)
    plug = tes3_ops.load(host.read_file(path))
    mode = by.lower()
    if mode == "master":
        return {"plugin": path, "masters": [{"name": n, "size": s}
                                            for n, s in tes3_ops.masters(plug)]}
    if mode == "type":
        recs = list(tes3_ops.iter_records(plug, _sig_str(value)))
    elif mode == "id":
        recs = tes3_ops.find_by_id(plug, value)
    elif mode == "id_regex":
        recs = tes3_ops.find_by_id_regex(plug, value)
    else:
        raise ValueError(f"unknown query mode {by!r} (use type|id|id_regex|master)")
    total = len(recs)
    return {
        "plugin": path, "by": mode, "value": value, "count": total,
        "records": [tes3_ops.record_view(r) for r in recs[:int(limit)]],
        "truncated": total > int(limit),
    }


@mcp.tool()
def tes3_get_record(plugin: str, type: str, id: str) -> Dict[str, Any]:
    """Read-only: a TES3 record's summary + its subrecord field views (opaque hex).

    Identify the record by its 4-char ``type`` and editor-id ``id``
    (case-insensitive). Each field view is ``{sig, index, size, payload_hex}`` —
    the hex feeds straight back into the mutating tools. No mutation.
    """
    host = _make_host()
    path = _resolve_tes3(host, plugin)
    plug = tes3_ops.load(host.read_file(path))
    rec = _find_tes3(plug, type, id)
    out = tes3_ops.record_view(rec)
    out["fields"] = tes3_ops.field_views(rec)
    out["plugin"] = path
    return out


@mcp.tool()
def tes3_get_subrecords(plugin: str, type: str, id: str,
                        signature: Optional[str] = None) -> Dict[str, Any]:
    """Read-only: the subrecords of a TES3 record (opaque hex), optionally filtered
    to a single 4-char ``signature``. No mutation."""
    host = _make_host()
    path = _resolve_tes3(host, plugin)
    plug = tes3_ops.load(host.read_file(path))
    rec = _find_tes3(plug, type, id)
    only = _sig_str(signature) if signature else None
    return {"plugin": path, "type": rec.type, "id": tes3_ops.record_id(rec),
            "fields": tes3_ops.field_views(rec, only)}


@mcp.tool()
def tes3_set_subrecord(plugin: str, type: str, id: str, signature: str,
                       payload_hex: str, index: int = 0) -> Dict[str, Any]:
    """Replace the ``index``-th ``signature`` subrecord's whole payload in a TES3
    record. ``payload_hex`` is the new field bytes as hex (may grow or shrink).
    Runs inside a safety transaction (lock -> backup -> edit -> verify -> restore)."""
    host = _make_host()
    path = _resolve_tes3(host, plugin)
    sig = _sig_str(signature)
    payload = _unhex(payload_hex)
    with _tes3_txn(host, path) as txn:
        plug = tes3_ops.load(txn.data)
        rec = _find_tes3(plug, type, id)
        tes3_ops.set_subrecord(rec, sig, payload, int(index))
        txn.data = tes3_ops.serialize(plug)
    return {"plugin": path, "type": type, "id": id, "signature": signature,
            "index": int(index), "new_size": len(payload), "backup": txn.backup_path}


@mcp.tool()
def tes3_patch_subrecord(plugin: str, type: str, id: str, signature: str,
                         offset: int, bytes_hex: str, index: int = 0) -> Dict[str, Any]:
    """Surgically overwrite bytes at ``offset`` inside a subrecord (same length).

    The precision editor for fixed-width TES3 fields (the RADT race block,
    author[32], a 32-byte NPCS ability id) — surrounding NUL padding is untouched.
    ``bytes_hex`` must fit within the existing payload. Safety transaction wrapped."""
    host = _make_host()
    path = _resolve_tes3(host, plugin)
    sig = _sig_str(signature)
    new = _unhex(bytes_hex)
    with _tes3_txn(host, path) as txn:
        plug = tes3_ops.load(txn.data)
        rec = _find_tes3(plug, type, id)
        tes3_ops.patch_subrecord(rec, sig, int(offset), new, int(index))
        txn.data = tes3_ops.serialize(plug)
    return {"plugin": path, "type": type, "id": id, "signature": signature,
            "index": int(index), "offset": int(offset), "wrote": len(new),
            "backup": txn.backup_path}


@mcp.tool()
def tes3_insert_subrecord(plugin: str, type: str, id: str, signature: str,
                          payload_hex: str, after: Optional[str] = None,
                          before: Optional[str] = None,
                          at_index: Optional[int] = None) -> Dict[str, Any]:
    """Insert a NEW subrecord into a TES3 record. Position: ``at_index`` >
    ``after`` (right after the last such tag) > ``before`` (right before the first)
    > append. Subrecord order is significant in TES3; this never re-sorts. Safety
    transaction wrapped."""
    host = _make_host()
    path = _resolve_tes3(host, plugin)
    sig = _sig_str(signature)
    payload = _unhex(payload_hex)
    with _tes3_txn(host, path) as txn:
        plug = tes3_ops.load(txn.data)
        rec = _find_tes3(plug, type, id)
        pos = tes3_ops.insert_subrecord(rec, sig, payload,
                                        after=_sig_str(after) if after else None,
                                        before=_sig_str(before) if before else None,
                                        at_index=None if at_index is None else int(at_index))
        txn.data = tes3_ops.serialize(plug)
    return {"plugin": path, "type": type, "id": id, "signature": signature,
            "position": pos, "backup": txn.backup_path}


@mcp.tool()
def tes3_delete_subrecord(plugin: str, type: str, id: str, signature: str,
                          index: int = 0) -> Dict[str, Any]:
    """Remove the ``index``-th ``signature`` subrecord from a TES3 record. Safety
    transaction wrapped."""
    host = _make_host()
    path = _resolve_tes3(host, plugin)
    sig = _sig_str(signature)
    with _tes3_txn(host, path) as txn:
        plug = tes3_ops.load(txn.data)
        rec = _find_tes3(plug, type, id)
        tes3_ops.delete_subrecord(rec, sig, int(index))
        txn.data = tes3_ops.serialize(plug)
    return {"plugin": path, "type": type, "id": id, "signature": signature,
            "index": int(index), "backup": txn.backup_path}


@mcp.tool()
def tes3_add_record(plugin: str, type: str, subrecords: List[List[str]],
                    flags: int = 0) -> Dict[str, Any]:
    """Author a NEW TES3 record and append it (bumping HEDR.numRecords by one).

    ``subrecords`` is a list of ``[signature, payload_hex]`` pairs in on-wire order
    (order is significant — e.g. a RACE is NAME, FNAM, RADT, NPCS…, DESC). Payloads
    are composed agent-side; the engine stays byte-opaque, so any record type can
    be authored without a typed decoder. This is the surface used to author the
    Rotfern RACE + its BODY parts. Safety transaction wrapped."""
    host = _make_host()
    path = _resolve_tes3(host, plugin)
    rtype = _sig_str(type)
    pairs = []
    for item in subrecords:
        if len(item) != 2:
            raise ValueError(f"each subrecord must be [signature, payload_hex], got {item!r}")
        pairs.append((_sig_str(item[0]), _unhex(item[1])))
    with _tes3_txn(host, path) as txn:
        plug = tes3_ops.load(txn.data)
        rec = tes3_ops.build_record(rtype, pairs, flags=int(flags))
        tes3_ops.add_record(plug, rec)
        new_id = tes3_ops.record_id(rec)
        txn.data = tes3_ops.serialize(plug)
    return {"plugin": path, "type": rtype, "id": new_id,
            "subrecords": [p[0] for p in pairs], "backup": txn.backup_path}


@mcp.tool()
def tes3_delete_records(plugin: str, type: str, ids: List[str]) -> Dict[str, Any]:
    """Hard-remove records of ``type`` whose editor-id is in ``ids`` (shrinks
    HEDR.numRecords). For a save-safe soft delete instead, use ``tes3_mark_deleted``.
    Safety transaction wrapped."""
    host = _make_host()
    path = _resolve_tes3(host, plugin)
    rtype = _sig_str(type)
    with _tes3_txn(host, path) as txn:
        plug = tes3_ops.load(txn.data)
        removed = tes3_ops.delete_records(plug, rtype, list(ids))
        txn.data = tes3_ops.serialize(plug)
    return {"plugin": path, "type": rtype, "removed": removed, "backup": txn.backup_path}


@mcp.tool()
def tes3_mark_deleted(plugin: str, type: str, id: str) -> Dict[str, Any]:
    """Morrowind soft-delete: set the record's DELETED flag AND add a ``DELE``
    subrecord, leaving the record physically present (the convention the CS/OpenMW
    read). Does not change HEDR.numRecords. Safety transaction wrapped."""
    host = _make_host()
    path = _resolve_tes3(host, plugin)
    with _tes3_txn(host, path) as txn:
        plug = tes3_ops.load(txn.data)
        rec = _find_tes3(plug, type, id)
        tes3_ops.mark_deleted(plug, rec)
        txn.data = tes3_ops.serialize(plug)
    return {"plugin": path, "type": type, "id": id, "backup": txn.backup_path}


@mcp.tool()
def tes3_add_master(plugin: str, name: str, size: int = 0) -> Dict[str, Any]:
    """Append a master (``MAST``+``DATA`` pair) to a TES3 plugin header
    (idempotent, case-insensitive). Unlike TES4, adds NO FormID fixups — TES3 refs
    are string ids. Safety transaction wrapped."""
    host = _make_host()
    path = _resolve_tes3(host, plugin)
    with _tes3_txn(host, path) as txn:
        plug = tes3_ops.load(txn.data)
        masters = tes3_ops.add_master(plug, name, int(size))
        txn.data = tes3_ops.serialize(plug)
    return {"plugin": path, "masters": [{"name": n, "size": s} for n, s in masters],
            "backup": txn.backup_path}


@mcp.tool()
def tes3_rename_master(plugin: str, old: str, new: str) -> Dict[str, Any]:
    """Rename a master (``MAST`` filename) ``old`` -> ``new`` in a TES3 header
    (case-insensitive match). Safety transaction wrapped."""
    host = _make_host()
    path = _resolve_tes3(host, plugin)
    with _tes3_txn(host, path) as txn:
        plug = tes3_ops.load(txn.data)
        masters = tes3_ops.rename_master(plug, old, new)
        txn.data = tes3_ops.serialize(plug)
    return {"plugin": path, "masters": [{"name": n, "size": s} for n, s in masters],
            "backup": txn.backup_path}


# --------------------------------------------------------------------------- #
# Mod installation — OpenMW / Morrowind (:mod:`chim.modinstall`)
# --------------------------------------------------------------------------- #
#
# Generalises the "extract archive -> drop assets in Data Files -> register in
# openmw.cfg" workflow into one reversible operation. These touch the local
# filesystem + openmw.cfg, so they require chim running ON the game machine
# (LocalWindowsHost -- the production deployment), not a RemoteHost. Archives
# resolve against CHIM_DOWNLOADS (the user's Downloads by default); installs are
# **dry-run by default**, lock-check OpenMW/OpenMW-CS, back up openmw.cfg, and
# write a manifest so an install can be cleanly reversed.


def _openmw_cfg(host: safety.Host) -> str:
    o = os.environ.get(ENV_OPENMW_CFG)
    if o:
        return o
    if host.windows:
        prof = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        return prof + r"\Documents\My Games\OpenMW\openmw.cfg"
    return os.path.join(os.getcwd(), "openmw.cfg")


def _mod_manifest_dir(host: safety.Host) -> str:
    o = os.environ.get(ENV_MOD_MANIFESTS)
    if o:
        return o
    base = os.path.dirname(_openmw_cfg(host))
    return (base.rstrip("\\/") + "\\chim_mods") if host.windows else os.path.join(base, "chim_mods")


def _downloads_dir(host: safety.Host) -> str:
    o = os.environ.get(ENV_DOWNLOADS)
    if o:
        return o
    if host.windows:
        prof = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        return prof + r"\Downloads"
    return os.getcwd()


def _resolve_in(host: safety.Host, directory: str, name: str) -> str:
    if name.startswith("/") or name.startswith("\\\\") or (len(name) >= 2 and name[1] == ":"):
        return name
    return (directory.rstrip("\\/") + "\\" + name) if host.windows else os.path.join(directory, name)


def _skyrim_plugins_txt(host: safety.Host) -> str:
    o = os.environ.get(ENV_PLUGINS_TXT)
    if o:
        return o
    if host.windows:
        local = os.environ.get("LOCALAPPDATA") or (os.path.expanduser("~") + r"\AppData\Local")
        return local.rstrip("\\/") + r"\Skyrim Special Edition\plugins.txt"
    return os.path.join(os.getcwd(), "plugins.txt")


def _game_profile(host: safety.Host, game: str) -> Dict[str, Any]:
    """Per-game data dir + registration file + lock-process set for mod installs."""
    g = (game or "openmw").lower()
    if g == "skyrim":
        return {"game": "skyrim", "data": _data_dir(host),
                "cfg": _skyrim_plugins_txt(host), "lock": safety.LOCK_PROCESSES}
    if g == "openmw":
        return {"game": "openmw", "data": _mw_data_dir(host),
                "cfg": _openmw_cfg(host), "lock": safety.OPENMW_LOCK_PROCESSES}
    raise ValueError(f"unknown game {game!r} (use 'openmw' or 'skyrim')")


def _require_local(host: safety.Host) -> None:
    if isinstance(host, safety.RemoteHost):
        raise ValueError("mod install needs chim running ON the game machine "
                         "(LocalWindowsHost); it is not supported over a RemoteHost")


def _mod_lock_or_raise(host: safety.Host) -> None:
    locked = safety.lock_check(host, safety.OPENMW_LOCK_PROCESSES)
    if locked:
        raise ValueError(f"cannot modify the install while these run: {locked}. "
                         "Close OpenMW / OpenMW-CS first.")


@mcp.tool()
def mod_install(archive: str, game: str = "openmw", include: Optional[Sequence[str]] = None,
                exclude: Optional[Sequence[str]] = None, name: Optional[str] = None,
                dry_run: bool = True) -> Dict[str, Any]:
    """Install a mod **archive** (.7z/.zip/.rar) into the game — one installer for
    both **OpenMW/Morrowind** (``game="openmw"``, default) and **Skyrim SE**
    (``game="skyrim"``).

    Extracts it, finds the data root (``Data Files`` / ``Data``), copies assets
    into the game's data dir, and **activates** any plugins: OpenMW registers
    ``content=`` (+ ``fallback-archive=`` for BSAs) in ``openmw.cfg``; Skyrim adds
    ``*Plugin.esp`` to ``plugins.txt`` (Skyrim auto-loads a BSA named after an
    active plugin). Writes a reversible manifest. ``include``/``exclude`` are globs
    over the data-relative path (e.g. ``include=["Meshes/*","*.esp"]``) to take
    **only the assets you want**.

    ``archive`` resolves against your Downloads folder (or an absolute path).
    **``dry_run=True`` by default** reports the plan without changing anything. A
    real run refuses if the game/editor is running and backs up the config first.
    Reverse it later with ``mod_uninstall``.
    """
    host = _make_host()
    _require_local(host)
    prof = _game_profile(host, game)
    apath = _resolve_in(host, _downloads_dir(host), archive)
    md = _mod_manifest_dir(host)
    backup = None
    if not dry_run:
        locked = safety.lock_check(host, prof["lock"])
        if locked:
            raise ValueError(f"cannot install while these run: {locked}. Close the game/editor first.")
        if os.path.exists(prof["cfg"]):
            backup = safety.backup(host, prof["cfg"])
    rep = modinstall.install_archive(
        apath, prof["data"], prof["cfg"], md, name=name, game=prof["game"],
        include=list(include) if include else None,
        exclude=list(exclude) if exclude else None, dry_run=dry_run)
    return {"archive": apath, "game": prof["game"], "name": rep["name"],
            "data_dir": prof["data"], "cfg": prof["cfg"], "plugins": rep["plugins"],
            "archives": rep["archives"], "new_file_count": rep["new_file_count"],
            "cfg_added": rep["cfg_added"], "dry_run": dry_run, "cfg_backup": backup}


@mcp.tool()
def mod_extract_bsa(bsa: str, patterns: Optional[Sequence[str]] = None,
                    names: Optional[Sequence[str]] = None, for_meshes: Optional[str] = None,
                    name: Optional[str] = None, dry_run: bool = True) -> Dict[str, Any]:
    """Surgically extract a **subset** of a Morrowind mod's ``.bsa`` as loose files
    into ``Data Files`` — the "only the assets we use" path.

    Three ways to select what to pull:

    * ``patterns`` — case-insensitive globs over archived paths (e.g.
      ``["meshes\\\\em_kids\\\\*", "textures\\\\*corean*"]``).
    * ``names`` — an explicit archived-path list.
    * ``for_meshes`` — **the smart mode**: a glob of installed NIFs relative to the
      Morrowind ``Data Files`` (e.g. ``"Meshes/Em_kids/*.nif"`` or ``"Meshes/**/*.nif"``).
      chim scans those meshes for every texture they reference and pulls, from the
      BSA, just the ones **not already present** — matched by **stem** so a mesh's
      ``.tga`` reference grabs the archive's ``.dds`` (OpenMW substitutes the
      extension), with a leading ``Textures\\`` normalised. Vanilla textures are
      skipped. This is "pull the textures these meshes need," done correctly.

    ``bsa`` resolves against the Morrowind ``Data Files`` (or an absolute path).
    **``dry_run=True`` by default** reports the match count. Writes a manifest for
    ``mod_uninstall``.
    """
    host = _make_host()
    _require_local(host)
    bpath = _resolve_in(host, _mw_data_dir(host), bsa)
    data, md = _mw_data_dir(host), _mod_manifest_dir(host)
    nm = name or (os.path.splitext(os.path.basename(bpath))[0] + "_assets")
    if not dry_run:
        _mod_lock_or_raise(host)
    if for_meshes:
        import glob as _glob
        pat = for_meshes if (for_meshes.startswith(("/", "\\")) or (len(for_meshes) >= 2 and for_meshes[1] == ":")) \
            else os.path.join(data, for_meshes.replace("/", os.sep))
        mesh_paths = [p for p in _glob.glob(pat, recursive=True) if p.lower().endswith(".nif")]
        vanilla = [os.path.join(data, b) for b in ("Morrowind.bsa", "Tribunal.bsa", "Bloodmoon.bsa")
                   if os.path.exists(os.path.join(data, b))]
        rep = modinstall.extract_bsa_for_meshes(bpath, data, nm, md, mesh_paths,
                                                vanilla_bsas=vanilla, dry_run=dry_run)
        return {"bsa": bpath, "name": nm, "data_dir": data, "meshes_scanned": len(mesh_paths),
                "texture_refs": rep.get("refs_scanned"), "matched": len(rep["files"]),
                "new_file_count": rep["new_file_count"], "dry_run": dry_run}
    rep = modinstall.extract_bsa_assets(
        bpath, data, nm, md,
        patterns=list(patterns) if patterns else None,
        names=list(names) if names else None, dry_run=dry_run)
    return {"bsa": bpath, "name": nm, "data_dir": data, "matched": len(rep["files"]),
            "new_file_count": rep["new_file_count"], "dry_run": dry_run}


@mcp.tool()
def mod_uninstall(name: str) -> Dict[str, Any]:
    """Reverse a chim mod install by its manifest ``name``: delete the files it
    added (never a pre-existing one) and drop the config lines it added. Uses the
    manifest's game (openmw/skyrim) to lock-check the right processes and back up
    the right config (``openmw.cfg`` / ``plugins.txt``) first, then removes the
    manifest."""
    host = _make_host()
    _require_local(host)
    md = _mod_manifest_dir(host)
    manifest = modinstall.load_manifest(md, name)
    game = (manifest.get("game") or "openmw").lower()
    cfg = manifest.get("cfg")
    lock = safety.LOCK_PROCESSES if game == "skyrim" else safety.OPENMW_LOCK_PROCESSES
    locked = safety.lock_check(host, lock)
    if locked:
        raise ValueError(f"cannot uninstall while these run: {locked}. Close the game/editor first.")
    backup = safety.backup(host, cfg) if cfg and os.path.exists(cfg) else None
    res = modinstall.uninstall(manifest, cfg)
    mp = os.path.join(md, name + ".json")
    if os.path.exists(mp):
        os.remove(mp)
    return {"name": name, "game": game, "cfg": cfg, "cfg_backup": backup, **res}


@mcp.tool()
def mod_list() -> Dict[str, Any]:
    """List the mods chim has installed (from its manifests), with plugin / BSA /
    file counts. No mutation."""
    host = _make_host()
    md = _mod_manifest_dir(host)
    out: List[Dict[str, Any]] = []
    for n in modinstall.list_manifests(md):
        try:
            m = modinstall.load_manifest(md, n)
            out.append({"name": n, "plugins": m.get("plugins", []),
                        "archives": m.get("archives", []), "files": len(m.get("files", []))})
        except Exception:  # pragma: no cover - skip a corrupt manifest
            continue
    return {"manifest_dir": md, "installed": out}


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
