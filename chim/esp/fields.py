"""Subrecord (field) layer.

A record's *plaintext* payload (``Record.data`` for uncompressed records, or
``decompress_record(record)`` for compressed ones) is a flat sequence of
subrecords::

    signature   4 ASCII
    dataSize    uint16 LE
    payload     dataSize bytes

The XXXX size-override: because ``dataSize`` is only 16 bits, a field larger
than 0xFFFF is preceded by a special ``XXXX`` subrecord whose 4-byte payload is
the real (uint32) size of the *following* field. That following field then
stores ``0`` in its own 16-bit size slot and its payload actually runs for the
XXXX-declared length. :func:`iterate_fields` transparently resolves this and
yields :class:`Field` objects carrying the *true* payload; :func:`pack_fields`
re-emits the XXXX prefix whenever a payload exceeds 0xFFFF, round-tripping.

Everything here operates on the plaintext field stream. Compression is handled
in :mod:`chim.esp.compression`; container structure in
:mod:`chim.esp.records`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple

XXXX = b"XXXX"
_MAX_U16 = 0xFFFF


# --------------------------------------------------------------------------- #
# Raw field container
# --------------------------------------------------------------------------- #

@dataclass
class Field:
    """One subrecord with its true (XXXX-resolved) payload.

    ``used_xxxx`` records whether this field was, on disk, preceded by an XXXX
    override. It is advisory; :func:`pack_fields` re-derives the need for XXXX
    purely from ``len(payload) > 0xFFFF`` so edits stay correct.
    """

    signature: bytes
    payload: bytes
    used_xxxx: bool = False

    @property
    def sig(self) -> str:
        return self.signature.decode("ascii", "replace")

    @property
    def size(self) -> int:
        return len(self.payload)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"Field({self.sig!r}, size={self.size})"


# --------------------------------------------------------------------------- #
# Iteration / (de)serialization of the raw field stream
# --------------------------------------------------------------------------- #

def iterate_fields(payload: bytes) -> Iterator[Field]:
    """Yield :class:`Field` objects from a plaintext record payload.

    XXXX overrides are consumed and applied to the following field; the XXXX
    subrecord is not yielded on its own.
    """
    off = 0
    n = len(payload)
    pending_size: Optional[int] = None
    while off + 6 <= n:
        sig = payload[off : off + 4]
        raw_size = struct.unpack_from("<H", payload, off + 4)[0]
        off += 6
        if sig == XXXX:
            if raw_size != 4:
                raise ValueError(f"XXXX field with size {raw_size} != 4")
            pending_size = struct.unpack_from("<I", payload, off)[0]
            off += 4
            continue
        size = pending_size if pending_size is not None else raw_size
        end = off + size
        if end > n:
            raise ValueError(
                f"field {sig!r}: payload size {size} runs past end of record"
            )
        yield Field(signature=sig, payload=payload[off:end],
                    used_xxxx=pending_size is not None)
        pending_size = None
        off = end
    if pending_size is not None:
        # A trailing XXXX size-override with no field after it: malformed. Report
        # it rather than silently dropping the override (which would truncate the
        # record on the next pack_fields, and the container walk-clean wouldn't
        # notice because it never re-parses the field stream).
        raise ValueError("dangling XXXX override with no following field")
    if off != n:
        raise ValueError(f"trailing {n - off} bytes in field stream")


def parse_fields(payload: bytes) -> List[Field]:
    """Eager list form of :func:`iterate_fields`."""
    return list(iterate_fields(payload))


def pack_field(field: Field) -> bytes:
    """Serialize a single field, emitting an XXXX prefix if needed."""
    size = len(field.payload)
    if size > _MAX_U16:
        return (
            XXXX + struct.pack("<H", 4) + struct.pack("<I", size)
            + field.signature + struct.pack("<H", 0) + field.payload
        )
    return field.signature + struct.pack("<H", size) + field.payload


def pack_fields(fields: List[Field]) -> bytes:
    """Serialize a list of fields back into a plaintext record payload.

    Round-trips :func:`parse_fields` for any well-formed input. XXXX prefixes
    are re-derived from payload length, so growing a field past 0xFFFF (or
    shrinking one back) produces a valid stream automatically.
    """
    return b"".join(pack_field(f) for f in fields)


def find_field(fields: List[Field], signature: bytes) -> Optional[Field]:
    """Return the first field with ``signature`` or ``None``."""
    for f in fields:
        if f.signature == signature:
            return f
    return None


# --------------------------------------------------------------------------- #
# Typed decoders / encoders
# --------------------------------------------------------------------------- #
#
# Each decode_* takes a raw payload (bytes) and returns a Python value; each
# encode_* takes the value and returns a raw payload. They intentionally do NOT
# touch the surrounding Field object so callers can mix decoded and raw fields
# freely.

# ---- EDID : null-terminated ASCII editor id ------------------------------- #

def decode_edid(payload: bytes) -> str:
    return payload.split(b"\x00", 1)[0].decode("ascii", "replace")


def encode_edid(editor_id: str) -> bytes:
    return editor_id.encode("ascii") + b"\x00"


# ---- OBND : 6 x int16 (x1,y1,z1,x2,y2,z2) --------------------------------- #

_OBND = struct.Struct("<6h")


def decode_obnd(payload: bytes) -> Tuple[int, int, int, int, int, int]:
    return _OBND.unpack(payload)


def encode_obnd(bounds: Tuple[int, int, int, int, int, int]) -> bytes:
    return _OBND.pack(*bounds)


# ---- MODL : zstring mesh path relative to meshes\ ------------------------- #

def decode_modl(payload: bytes) -> str:
    return payload.split(b"\x00", 1)[0].decode("ascii", "replace")


def encode_modl(mesh_path: str) -> bytes:
    return mesh_path.encode("ascii") + b"\x00"


# ---- FULL : name. May be a zstring or a lstring (localized formID). ------- #
# Without the plugin's localization flag we cannot know which; expose both.

def decode_full_string(payload: bytes) -> str:
    return payload.split(b"\x00", 1)[0].decode("utf-8", "replace")


def encode_full_string(name: str) -> bytes:
    return name.encode("utf-8") + b"\x00"


def decode_full_lstring(payload: bytes) -> int:
    """Localized FULL: a uint32 string-table id."""
    return struct.unpack("<I", payload)[0]


def encode_full_lstring(string_id: int) -> bytes:
    return struct.pack("<I", string_id)


# ---- DATA (REFR/ACHR placement) : 6 x float32 pos + rot ------------------- #

_POSROT = struct.Struct("<6f")


@dataclass
class PosRot:
    x: float
    y: float
    z: float
    rx: float
    ry: float
    rz: float

    def as_tuple(self) -> Tuple[float, float, float, float, float, float]:
        return (self.x, self.y, self.z, self.rx, self.ry, self.rz)


def decode_data_posrot(payload: bytes) -> PosRot:
    return PosRot(*_POSROT.unpack(payload))


def encode_data_posrot(posrot: PosRot) -> bytes:
    return _POSROT.pack(*posrot.as_tuple())


# ---- NAME : uint32 base formID (what a REFR is an instance of) ------------ #

def decode_name(payload: bytes) -> int:
    return struct.unpack("<I", payload)[0]


def encode_name(form_id: int) -> bytes:
    return struct.pack("<I", form_id)


# ---- XLKR : linked reference (keyword formID + ref formID) ---------------- #

@dataclass
class LinkedRef:
    keyword_form_id: int
    ref_form_id: int


def decode_xlkr(payload: bytes) -> LinkedRef:
    kw, ref = struct.unpack("<II", payload)
    return LinkedRef(kw, ref)


def encode_xlkr(linked: LinkedRef) -> bytes:
    return struct.pack("<II", linked.keyword_form_id, linked.ref_form_id)


# ---- XESP : enable parent (parent formID + flags) ------------------------- #

@dataclass
class EnableParent:
    parent_form_id: int
    flags: int


def decode_xesp(payload: bytes) -> EnableParent:
    parent, flags = struct.unpack("<II", payload)
    return EnableParent(parent, flags)


def encode_xesp(parent: EnableParent) -> bytes:
    return struct.pack("<II", parent.parent_form_id, parent.flags)


# ---- XSCL : float32 scale ------------------------------------------------- #

def decode_xscl(payload: bytes) -> float:
    return struct.unpack("<f", payload)[0]


def encode_xscl(scale: float) -> bytes:
    return struct.pack("<f", scale)


# ---- HEDR (inside TES4) : version, numRecords, nextObjectID --------------- #

_HEDR = struct.Struct("<fiI")


@dataclass
class Hedr:
    version: float
    num_records: int
    next_object_id: int


def decode_hedr(payload: bytes) -> Hedr:
    ver, num, nxt = _HEDR.unpack_from(payload, 0)
    return Hedr(ver, num, nxt)


def encode_hedr(hedr: Hedr) -> bytes:
    return _HEDR.pack(hedr.version, hedr.num_records, hedr.next_object_id)


# --------------------------------------------------------------------------- #
# VMAD : script metadata (objFormat 2). We decode enough to list & edit
# script names and property scalars; unknown structure is preserved as raw
# tails so anything we can decode we can re-encode identically.
# --------------------------------------------------------------------------- #

@dataclass
class VmadProperty:
    name: str
    prop_type: int
    status: int
    value: object          # decoded scalar, or raw bytes for unhandled types
    raw_value: bytes       # the exact on-disk value bytes (for round-trip)


@dataclass
class VmadScript:
    name: str
    flags: int
    properties: List[VmadProperty]


@dataclass
class Vmad:
    version: int
    obj_format: int
    scripts: List[VmadScript]
    trailer: bytes = b""   # fragment/alias data after the script list, kept raw


def _read_wstring(buf: bytes, off: int) -> Tuple[str, int]:
    length = struct.unpack_from("<H", buf, off)[0]
    off += 2
    s = buf[off : off + length].decode("utf-8", "replace")
    return s, off + length


def _write_wstring(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<H", len(b)) + b


def _read_vmad_value(buf: bytes, off: int, ptype: int,
                     obj_format: int) -> Tuple[object, bytes, int]:
    """Return (decoded_value, raw_value_bytes, new_offset)."""
    start = off
    if ptype == 1:  # Object
        # objFormat 2: uint16 unused=0, uint16 aliasID, uint32 formID
        _unused, alias_id, form_id = struct.unpack_from("<HHI", buf, off)
        off += 8
        return {"alias_id": alias_id, "form_id": form_id}, buf[start:off], off
    if ptype == 2:  # wstring
        s, off = _read_wstring(buf, off)
        return s, buf[start:off], off
    if ptype == 3:  # int32
        v = struct.unpack_from("<i", buf, off)[0]
        off += 4
        return v, buf[start:off], off
    if ptype == 4:  # float32
        v = struct.unpack_from("<f", buf, off)[0]
        off += 4
        return v, buf[start:off], off
    if ptype == 5:  # bool (uint8)
        v = bool(buf[off])
        off += 1
        return v, buf[start:off], off
    if ptype == 11:  # array
        count = struct.unpack_from("<I", buf, off)[0]
        off += 4
        elems = []
        # arrays hold elements of a single element type == ptype - 10.
        elem_type = ptype - 10
        for _ in range(count):
            val, _raw, off = _read_vmad_value(buf, off, elem_type, obj_format)
            elems.append(val)
        return elems, buf[start:off], off
    raise ValueError(f"unsupported VMAD property type {ptype}")


def decode_vmad(payload: bytes) -> Vmad:
    """Decode a VMAD field (objFormat 2). Script fragments / alias sections
    beyond the plain script list are preserved verbatim in ``trailer``."""
    off = 0
    version, obj_format, script_count = struct.unpack_from("<hhH", payload, 0)
    off = 6
    scripts: List[VmadScript] = []
    for _ in range(script_count):
        name, off = _read_wstring(payload, off)
        flags = payload[off]
        off += 1
        prop_count = struct.unpack_from("<H", payload, off)[0]
        off += 2
        props: List[VmadProperty] = []
        for _ in range(prop_count):
            pname, off = _read_wstring(payload, off)
            ptype = payload[off]
            status = payload[off + 1]
            off += 2
            value, raw_value, off = _read_vmad_value(
                payload, off, ptype, obj_format
            )
            props.append(VmadProperty(pname, ptype, status, value, raw_value))
        scripts.append(VmadScript(name, flags, props))
    return Vmad(version, obj_format, scripts, trailer=payload[off:])


def script_names(payload: bytes) -> List[str]:
    """Convenience: just the attached script names, in order."""
    return [s.name for s in decode_vmad(payload).scripts]


def encode_vmad(vmad: Vmad) -> bytes:
    out = bytearray()
    out += struct.pack("<hhH", vmad.version, vmad.obj_format,
                       len(vmad.scripts))
    for script in vmad.scripts:
        out += _write_wstring(script.name)
        out += struct.pack("<B", script.flags)
        out += struct.pack("<H", len(script.properties))
        for prop in script.properties:
            out += _write_wstring(prop.name)
            out += struct.pack("<BB", prop.prop_type, prop.status)
            # Re-emit the exact on-disk value bytes we captured on decode.
            out += prop.raw_value
    out += vmad.trailer
    return bytes(out)
