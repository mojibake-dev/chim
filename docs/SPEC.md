# chim — ESP/ESM Format & Interface Specification

This document was the **authoritative contract** for the foundation phase of
`chim` — the on-disk Bethesda plugin format plus the interface of the original
hand-rolled byte engine (`chim/esp/records.py`, `chim/esp/fields.py`,
`chim/esp/compression.py`).

> **Engine rewritten onto esplib, 2026-07-11.** chim's production byte engine is
> no longer the hand-rolled model described in *Part 2* below — it is
> [esplib](https://github.com/BadDogSkyrim/esplib) (v0.2.0, BadDogSkyrim,
> MPL-2.0), reached through the `chim/esp/ops.py` adapter. **Part 1 (the on-disk
> format) is still fully valid** — the file format did not change, and esplib
> parses/serializes exactly the bytes described here (byte-identical round-trip).
> **Part 2 (the interfaces) describes the *legacy* engine**, which survives on
> disk as a fallback (still importable, and its `walk_clean` is used by the
> safety transaction's independent verify — see the [README](../README.md)); new
> code uses `chim.esp.ops` over esplib instead.
>
> For the **current, esplib-grounded deep format reference** — the same byte
> layouts plus how esplib actually reads/writes them, FormID/ESL structure,
> compression, VMAD in depth, and the persistent-cell rule — read
> [`docs/modding/esp-format-reference.md`](modding/esp-format-reference.md). For
> the toolkit and the 19 MCP tools, see
> [`docs/modding/chim-toolkit.md`](modding/chim-toolkit.md).

All integers are **little-endian** unless stated otherwise.

---

## Part 1 — On-disk format

### 1.1 File layout

A plugin file is:

```
TES4 record            (the header record; always first)
top-level GRUP ...      (zero or more, in file order)
```

Parsing runs from byte 0 to EOF. "**Walk-clean**" means the file re-parses from
the leading `TES4` to EOF with **zero leftover bytes** and every GRUP's declared
size consumes **exactly** its children — no more, no less.

### 1.2 Record

A record is a 24-byte header followed by `dataSize` bytes of payload.

| Offset | Size | Field                         |
|-------:|-----:|-------------------------------|
| 0      | 4    | signature (4 ASCII, e.g. `STAT`, `REFR`, `TES4`, `CELL`) |
| 4      | 4    | `dataSize` (uint32) — bytes of data **after** the 24-byte header |
| 8      | 4    | `flags` (uint32)              |
| 12     | 4    | `formID` (uint32)             |
| 16     | 4    | timestamp + version control (uint32) |
| 20     | 2    | internal version (uint16, typically `0x002C`) |
| 22     | 2    | unknown (uint16)              |
| 24     | …    | `dataSize` bytes of payload   |

**Record flags of interest:**

| Value      | Name                | Meaning |
|-----------:|---------------------|---------|
| `0x00000020` | Deleted           | record marked deleted |
| `0x00000400` | Persistent        | placement (REFR/ACHR/ACRE) is persistent |
| `0x00000800` | InitiallyDisabled | reference starts disabled |
| `0x00040000` | Compressed        | payload is zlib-compressed (see §1.4) |

### 1.3 Group (GRUP)

A GRUP is a 24-byte header followed by nested records and/or sub-GRUPs.

| Offset | Size | Field                                   |
|-------:|-----:|-----------------------------------------|
| 0      | 4    | `GRUP`                                  |
| 4      | 4    | `groupSize` (uint32) — **includes** the 24-byte header |
| 8      | 4    | `label` (4 bytes; meaning depends on `groupType`) |
| 12     | 4    | `groupType` (int32)                     |
| 16     | 4    | timestamp + version control (uint32)    |
| 20     | 2    | unknown (uint16)                        |
| 22     | 2    | unknown (uint16)                        |
| 24     | …    | `groupSize - 24` bytes of nested children |

**Group types:**

| Type | Meaning                        | Label interpretation |
|-----:|--------------------------------|----------------------|
| 0    | Top                            | record signature (4 ASCII) |
| 1    | World children                 | parent WRLD formID |
| 2    | Interior cell block            | block number (int32) |
| 3    | Interior cell sub-block        | sub-block number (int32) |
| 4    | Exterior cell block            | grid Y,X (int16,int16) |
| 5    | Exterior cell sub-block        | grid Y,X (int16,int16) |
| 6    | Cell children                  | parent CELL formID |
| 7    | Topic children                 | parent DIAL formID |
| 8    | Cell **persistent** children   | parent CELL formID |
| 9    | Cell **temporary** children    | parent CELL formID |
| 10   | Cell visible-distant children  | parent CELL formID |

GRUPs nest arbitrarily; `parse_plugin` recurses.

### 1.4 Compressed record data (flag `0x40000`)

When `flags & 0x40000` the record's payload is:

```
uint32 decompressedSize       # length of the plaintext field stream
<zlib stream>                 # 2-byte zlib header + raw DEFLATE
```

Read: `zlib.decompress(data[4:])`, validate against `decompressedSize`.
Write: `struct.pack("<I", len(plain)) + zlib.compress(plain)`.

`chim` stores the **compressed blob** verbatim in `Record.data` so parsing is
lossless; use `chim.esp.compression` to get/set plaintext.

### 1.5 Subrecords (fields)

A record's **plaintext** payload is a flat sequence of subrecords:

```
signature   4 ASCII
dataSize    uint16 LE
payload     dataSize bytes
```

**XXXX size-override.** Because `dataSize` is 16 bits, any field larger than
`0xFFFF` is preceded by a special subrecord:

```
'XXXX'  size=4  payload=uint32 realSize
```

The **next** subrecord then stores `0` in its own 16-bit size slot, and its
payload actually runs for `realSize` bytes. `chim.esp.fields` consumes `XXXX`
transparently on read and re-emits it on write whenever a payload exceeds
`0xFFFF`.

### 1.6 Common subrecords

| Sig  | Payload |
|------|---------|
| `EDID` | null-terminated ASCII editor id |
| `OBND` | 6×int16: `x1,y1,z1,x2,y2,z2` |
| `MODL` | zstring mesh path, relative to `meshes\` |
| `MODT` | binary model texture hashes (opaque) |
| `FULL` | zstring name, **or** uint32 localized string id if the plugin is localized |
| `DATA` | for REFR/ACHR: 6×float32 `posX,posY,posZ,rotX,rotY,rotZ` |
| `NAME` | uint32 base formID (what a placement instantiates) |
| `XLKR` | uint32 keyword formID + uint32 ref formID (linked reference) |
| `XESP` | uint32 parent formID + uint32 flags (enable parent) |
| `XSCL` | float32 scale |
| `XRGD` | ragdoll bytes (opaque) |
| `HEDR` | (inside TES4) float32 version, int32 numRecords, uint32 nextObjectID |

`HEDR.numRecords` counts **records + groups excluding TES4**.

### 1.7 FormIDs

```
pluginIndex  = formID >> 24
objectIndex  = formID & 0x00FFFFFF
```

In-file self-references use the **plugin's own load index**, which equals its
number of masters. A master-less plugin's own index is `0x00`.

### 1.8 VMAD (script metadata), objFormat 2

```
int16  version
int16  objFormat            # == 2 here
uint16 scriptCount
  per script:
    uint16 nameLen + name (ASCII)
    uint8  flags
    uint16 propCount
      per property:
        uint16 nameLen + name (ASCII)
        uint8  type
        uint8  status
        value  (see below)
```

Property value encodings:

| type | meaning | value bytes (objFormat 2) |
|-----:|---------|---------------------------|
| 1  | Object | uint16 unused(=0) + uint16 aliasID(0xFFFF if none) + uint32 formID |
| 2  | wstring | uint16 len + UTF-8 bytes |
| 3  | int32   | int32 |
| 4  | float32 | float32 |
| 5  | bool    | uint8 |
| 11 | array   | uint32 count + `count` elements of type `type-10` |

Data past the script list (fragments / alias info) is preserved verbatim.

---

## Part 2 — Interfaces (the legacy hand-rolled engine)

> **This part describes the retired, hand-rolled engine.** Since the 2026-07-11
> esplib rewrite, production code parses/edits/serializes through
> `chim.esp.ops` (over esplib), whose model is `esplib.plugin.Plugin` /
> `esplib.record.Record` / `SubRecord` / `GroupRecord` — *not* the classes below.
> The `chim.esp.records` / `chim.esp.compression` / `chim.esp.fields` API here is
> **kept on disk and still importable as a fallback**; `records.walk_clean` in
> particular is still called by `chim.esp.safety` as an independent post-write
> verify. For the current engine and its call surface see
> [`docs/modding/chim-toolkit.md`](modding/chim-toolkit.md).

Everything below is importable from `chim.esp.records`,
`chim.esp.compression`, and `chim.esp.fields`. The most-used names are also
re-exported from `chim` and `chim.esp`.

### 2.1 `chim.esp.records`

**Constants**

```
RECORD_HEADER_SIZE = 24
GROUP_HEADER_SIZE  = 24
FLAG_DELETED  = 0x20   FLAG_PERSISTENT = 0x400
FLAG_INITIALLY_DISABLED = 0x800   FLAG_COMPRESSED = 0x40000
GT_TOP=0  GT_WORLD_CHILDREN=1  GT_INTERIOR_BLOCK=2  GT_INTERIOR_SUBBLOCK=3
GT_EXTERIOR_BLOCK=4  GT_EXTERIOR_SUBBLOCK=5  GT_CELL_CHILDREN=6
GT_TOPIC_CHILDREN=7  GT_CELL_PERSISTENT_CHILDREN=8
GT_CELL_TEMPORARY_CHILDREN=9  GT_CELL_VISIBLE_DISTANT_CHILDREN=10
```

**`class Record`** — dataclass. A single record.

Fields: `signature: bytes`, `header: bytes` (verbatim 24-byte header),
`data: bytes` (payload exactly as on disk; compressed blob if compressed).

Read-only properties: `data_size` (== `len(data)`), `timestamp_vc`,
`internal_version`, `unknown`, `is_compressed`, `is_deleted`, `is_persistent`,
`plugin_index`, `object_index`, `sig` (str), `byte_size` (24 + len(data)).

Read/write properties: `flags: int`, `form_id: int` (setters re-pack the
header, keeping all other header bytes intact).

Methods: `to_bytes() -> bytes` (re-syncs `dataSize` from `data`).

> **Editing contract:** to change a record's payload, assign `record.data`.
> `dataSize` is always re-derived on serialize — never hand-patch the header
> size. To change flags/formID, use the properties.

**`class Group`** — dataclass. A GRUP container.

Fields: `header: bytes` (verbatim 24-byte header), `children: list[Record|Group]`.

Read-only properties: `signature` (`b"GRUP"`), `sig` (`"GRUP"`),
`group_size` (24 + total children size; re-derived), `group_type: int`,
`timestamp_vc`, `label_as_signature` (label if top group else None),
`label_as_uint32`, `byte_size`.

Read/write properties: `label: bytes` (must be 4 bytes).

Methods: `to_bytes() -> bytes` (re-syncs `groupSize` from serialized children).

> **Editing contract:** append/remove/reorder `children` freely; `groupSize`
> is recomputed on serialize, so structural edits stay walk-clean automatically.

**`class Plugin`** — dataclass. Whole-file container.

Field: `top_level: list[Record|Group]` (element 0 is TES4).
Properties: `tes4 -> Record|None`, `groups -> list[Group]`.
Methods: `to_bytes()`, iterable (`for node in plugin`).

**Functions**

```
parse_plugin(data: bytes) -> Plugin
    Parse a whole plugin. Raises ParseError if it doesn't start with TES4
    or doesn't consume cleanly to EOF.

serialize(tree: Plugin | Record | Group) -> bytes
    Byte-identical for any unmodified parsed tree.

iterate(tree) -> Iterator[Record | Group]      # alias: walk
    Depth-first pre-order; groups yielded before their children.

walk_clean(data: bytes) -> bool
    True iff data parses to EOF with zero leftover AND round-trips
    byte-identically.
```

`class ParseError(ValueError)` — raised on malformed structure.

### 2.2 `chim.esp.compression`

```
is_compressed(record: Record) -> bool
decompress_record(record: Record) -> bytes
    Plaintext field stream. No-op passthrough for uncompressed records.
compress_payload(plain: bytes, level=9) -> bytes
    Returns  uint32 decompressedSize + zlib(plain).
store_uncompressed(record: Record) -> Record
    In-place: clears 0x40000, replaces data with plaintext. Returns record.
store_compressed(record: Record, level=9) -> Record
    In-place: sets 0x40000, replaces data with size+zlib blob. Returns record.
```

> Note: `compress_payload` output depends on zlib and will not necessarily
> reproduce a third-party tool's exact bytes. Round-tripping an *unmodified*
> compressed record uses the stored blob verbatim (no re-compression), so
> `parse_plugin`/`serialize` stays byte-exact even for compressed records.

### 2.3 `chim.esp.fields`

**`class Field`** — dataclass: `signature: bytes`, `payload: bytes` (true,
XXXX-resolved), `used_xxxx: bool`. Props: `sig` (str), `size`.

**Raw stream**

```
iterate_fields(payload: bytes) -> Iterator[Field]     # XXXX consumed silently
parse_fields(payload: bytes) -> list[Field]
pack_field(field: Field) -> bytes                     # emits XXXX if >0xFFFF
pack_fields(fields: list[Field]) -> bytes             # round-trips parse_fields
find_field(fields: list[Field], signature: bytes) -> Field | None
```

**Typed codecs** (each `decode_*(payload)->value`, `encode_*(value)->payload`):

```
EDID   decode_edid / encode_edid            -> str
OBND   decode_obnd / encode_obnd            -> (x1,y1,z1,x2,y2,z2) ints
MODL   decode_modl / encode_modl            -> str
FULL   decode_full_string / encode_full_string   -> str        (zstring form)
       decode_full_lstring / encode_full_lstring -> int        (localized id)
DATA   decode_data_posrot / encode_data_posrot   -> PosRot(x,y,z,rx,ry,rz)
NAME   decode_name / encode_name            -> int (formID)
XLKR   decode_xlkr / encode_xlkr            -> LinkedRef(keyword_form_id, ref_form_id)
XESP   decode_xesp / encode_xesp            -> EnableParent(parent_form_id, flags)
XSCL   decode_xscl / encode_xscl            -> float
HEDR   decode_hedr / encode_hedr            -> Hedr(version, num_records, next_object_id)
```

Dataclasses: `PosRot`, `LinkedRef`, `EnableParent`, `Hedr`.

**VMAD**

```
decode_vmad(payload: bytes) -> Vmad
encode_vmad(vmad: Vmad) -> bytes            # byte-exact for anything decode_vmad reads
script_names(payload: bytes) -> list[str]

class Vmad:          version:int  obj_format:int  scripts:list[VmadScript]  trailer:bytes
class VmadScript:    name:str  flags:int  properties:list[VmadProperty]
class VmadProperty:  name:str  prop_type:int  status:int  value  raw_value:bytes
```

`VmadProperty.raw_value` holds the exact on-disk value bytes so `encode_vmad`
reproduces the input byte-for-byte; `value` is the decoded convenience form.

---

## Part 3 — Editing recipe (for the next phase)

```python
from chim import parse_plugin, serialize, walk_clean, iterate
from chim.esp.records import Record, Group
from chim.esp import fields

plugin = parse_plugin(open("in.esp", "rb").read())

for node in iterate(plugin):
    if isinstance(node, Record) and node.sig == "STAT":
        fl = fields.parse_fields(node.data)          # decompress first if node.is_compressed
        edid = fields.find_field(fl, b"EDID")
        edid.payload = fields.encode_edid("NewEditorID")
        node.data = fields.pack_fields(fl)           # re-store; store_compressed() if needed

out = serialize(plugin)
assert walk_clean(out)                                # invariant to hold after every edit
open("out.esp", "wb").write(out)
```

**Invariants the next phase must preserve**

1. After every edit, `walk_clean(serialize(plugin))` is `True`.
2. Never hand-write `dataSize`/`groupSize`; assign `data`/`children` and let
   `to_bytes()` re-derive sizes.
3. For compressed records, edit the plaintext (`decompress_record`) then
   `store_compressed`/`store_uncompressed`; don't mutate the raw blob.
4. Keep `HEDR.num_records` in sync (records + groups excluding TES4) whenever
   you add/remove nodes.
