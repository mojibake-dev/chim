"""Papyrus VM heap (``GlobalData`` type 1001) — parse, round-trip, and clean.

Phase 2 parses the heap deep enough to **delete** script instances safely:

    u16 header
    StringTable            (u32 count + WString[])
    u32 scriptCount + Script[]   (name TString, type TString, i32 nMembers, MemberDesc[])
    ScriptInstanceMap      (u32 count + ScriptInstance[20B])   <-- instance HEADERS
    ── mid (kept raw) ──    ReferenceMap, ArrayMap, papyrusRuntime EID, ActiveScriptMap, 4B gap
    ScriptInstance DATA    (self-terminating: [EID u64][flag,state,unk1,(unk2 if flag&4),
                            varCount, Variable[]] per instance, until an EID leaves the
                            instance set — that EID starts the Reference data)
    ── tail (kept raw) ──   Reference/Array/ActiveScript data, messages, stacks, unbinds…

Only two regions are modelled for mutation: the instance **headers** and the
instance **data blocks** (keyed by EID; a strict subset of the headers — some
instances carry no data block). Everything else is copied verbatim, so a parse →
serialize is **byte-identical** (the mutation-safety gate). Removing an instance
drops its header and, if present, its data block together; dangling references
elsewhere resolve to ``None`` on load (the VM tolerates this, as ReSaver relies on).

TString refs are u32 (Skyrim SE / STR32); element EIDs are u64 (measured), except
ActiveScript/SuspendedStack which are u32. Variable ``Type`` codes: scalars 0–7,
arrays 11–17. Layouts grounded in ReSaver / FallrimTools (Apache-2.0) + validated
byte-exact against real 1.6.1170 saves.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .ess import Reader, w_wstr

NATIVE_SCRIPTS = {
    "activemagiceffect", "alias", "debug", "form", "game", "input", "math",
    "modevent", "skse", "stringutil", "ui", "utility", "commonarrayfunctions",
    "scriptobject", "inputenablelayer",
}


class HeapParseError(Exception):
    pass


@dataclass
class Script:
    name_idx: int
    type_idx: int
    members: List[Tuple[int, int]] = field(default_factory=list)


@dataclass
class ScriptInstance:
    """A 20-byte instance header: EID u64, name TString u32, u16, u16, RefID(3), u8."""

    eid: int
    name_idx: int
    unknown2bits: int
    unknown: int
    refid: int
    unknown_byte: int

    def is_unattached(self) -> bool:
        return self.refid == 0

    def to_bytes(self) -> bytes:
        return (
            struct.pack("<QIHH", self.eid, self.name_idx, self.unknown2bits, self.unknown)
            + bytes(((self.refid >> 16) & 0xFF, (self.refid >> 8) & 0xFF, self.refid & 0xFF))
            + bytes((self.unknown_byte,))
        )


def _skip_variable(r: Reader) -> None:
    """Advance past one Papyrus Variable (type byte + type-dependent payload)."""
    t = r.u8()
    if t == 0:                      # NULL
        r.u32()
    elif t in (1, 7, 11, 17):       # REF / STRUCT / REF_ARRAY / STRUCT_ARRAY
        r.u32(); r.u64()            #   TString + EID
    elif t == 2:                    # STRING
        r.u32()
    elif t in (3, 4, 5):            # INT / FLOAT / BOOL
        r.u32()
    elif t == 6:                    # VARIANT
        _skip_variable(r)
    elif t in (12, 13, 14, 15, 16):  # STRING/INT/FLOAT/BOOL/VARIANT _ARRAY
        r.u64()                     #   EID
    else:
        raise HeapParseError(f"unknown Variable type {t} @ {r.o}")


class PapyrusHeap:
    def __init__(self, block: bytes):
        self.block = block
        self.header: int = 0
        self.strings: List[bytes] = []
        self.script_count: int = 0
        self.scripts: List[Script] = []
        self.instances: List[ScriptInstance] = []
        self.mid: bytes = b""                         # ReferenceMap..ActiveScriptMap+gap (raw)
        self.inst_data: List[List] = []               # [ [eid, data_bytes], ... ] (order preserved)
        self.tail: bytes = b""                        # Reference data onward (raw)
        self._parse(block)

    # ---- decode ----

    def _parse(self, block: bytes) -> None:
        r = Reader(block)
        self.header = r.u16()

        n_str = r.u32()
        self.strings = [r.wstr() for _ in range(n_str)]

        self.script_count = r.u32()
        for _ in range(self.script_count):
            name_idx = r.u32(); type_idx = r.u32(); n_mem = r.i32()
            members = [(r.u32(), r.u32()) for _ in range(n_mem)]
            self.scripts.append(Script(name_idx, type_idx, members))

        n_inst = r.u32()
        for _ in range(n_inst):
            eid = r.u64(); name_idx = r.u32(); u2 = r.u16(); unk = r.u16()
            refid = (r.u8() << 16) | (r.u8() << 8) | r.u8(); ub = r.u8()
            self.instances.append(ScriptInstance(eid, name_idx, u2, unk, refid, ub))

        # mid: header maps we navigate but never edit (kept raw for byte-exact round-trip)
        mid_start = r.o
        nref = r.u32(); r.o += nref * 12               # Reference header = EID u64 + name TString
        narr = r.u32()
        for _ in range(narr):                          # ArrayInfo = EID + type + [refType] + length
            r.u64(); t = r.u8()
            if t == 1:
                r.u32()
            r.u32()
        r.u64()                                        # papyrusRuntime EID
        nact = r.u32(); r.o += nact * 5                # ActiveScript header = EID32 + type
        r.u32()                                        # 4-byte gap before instance data
        self.mid = block[mid_start:r.o]

        # instance data blocks: self-terminating at the first non-instance EID
        eidset = {i.eid for i in self.instances}
        while r.o + 8 <= len(block):
            eid = struct.unpack_from("<Q", block, r.o)[0]
            if eid not in eidset:
                break
            start = r.o
            r.u64()                                    # EID
            flag = r.u8(); r.u32(); r.i32()            # flag, state TString, unk1
            if flag & 4:
                r.i32()                                # unk2
            vc = r.i32()
            for _ in range(vc):
                _skip_variable(r)
            self.inst_data.append([eid, block[start + 8:r.o]])
        self.tail = block[r.o:]

    # ---- serialize ----

    def to_bytes(self) -> bytes:
        w = bytearray()
        w += struct.pack("<H", self.header)
        w += struct.pack("<I", len(self.strings))
        for s in self.strings:
            w += w_wstr(s)
        w += struct.pack("<I", len(self.scripts))
        for sc in self.scripts:
            w += struct.pack("<IIi", sc.name_idx, sc.type_idx, len(sc.members))
            for nm, ty in sc.members:
                w += struct.pack("<II", nm, ty)
        w += struct.pack("<I", len(self.instances))
        for it in self.instances:
            w += it.to_bytes()
        w += self.mid
        for eid, data in self.inst_data:
            w += struct.pack("<Q", eid)
            w += data
        w += self.tail
        return bytes(w)

    def roundtrips(self) -> bool:
        return self.to_bytes() == self.block

    # ---- mutation ----

    def remove_instances(self, eids) -> Dict[str, int]:
        """Remove instances (by EID): drop each header and, if present, its data
        block. Returns counts. Dangling references left for the VM to null out."""
        eids = set(eids)
        before_h = len(self.instances)
        before_d = len(self.inst_data)
        self.instances = [i for i in self.instances if i.eid not in eids]
        self.inst_data = [d for d in self.inst_data if d[0] not in eids]
        return {
            "instances_removed": before_h - len(self.instances),
            "data_blocks_removed": before_d - len(self.inst_data),
        }

    def remove_unattached(self) -> Dict[str, int]:
        return self.remove_instances({i.eid for i in self.instances if i.is_unattached()})

    def remove_undefined(self) -> Dict[str, int]:
        undef = self.undefined_script_names()
        eids = {i.eid for i in self.instances if self.is_instance_undefined(i, undef)}
        res = self.remove_instances(eids)
        before_s = len(self.scripts)
        self.scripts = [sc for sc in self.scripts if not self.script_is_undefined(sc)]
        self.script_count = len(self.scripts)
        res["stub_defs_removed"] = before_s - len(self.scripts)
        return res

    # ---- helpers ----

    def instance_script_name(self, inst: ScriptInstance) -> str:
        return self.string(inst.name_idx)

    def string(self, idx: int) -> str:
        if 0 <= idx < len(self.strings):
            return self.strings[idx].decode("utf-8", "replace")
        return f"<bad string idx {idx}>"

    def _by_name(self) -> dict:
        bn = getattr(self, "_bn", None)
        if bn is None:
            bn = {}
            for sc in self.scripts:
                bn.setdefault(self.string(sc.name_idx).lower(), sc)
            self._bn = bn
        return bn

    def script_is_undefined(self, sc: Script) -> bool:
        return (
            self.string(sc.type_idx) == ""
            and self.string(sc.name_idx).lower() not in NATIVE_SCRIPTS
        )

    def undefined_script_names(self) -> set:
        return {
            self.string(sc.name_idx).lower()
            for sc in self.scripts
            if self.script_is_undefined(sc)
        }

    def is_instance_undefined(self, inst: ScriptInstance, undefined: set = None) -> bool:
        if undefined is None:
            undefined = self.undefined_script_names()
        name = self.instance_script_name(inst).lower()
        if name in undefined:
            return True
        return name not in self._by_name() and name not in NATIVE_SCRIPTS
