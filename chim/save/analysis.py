"""Read-only save analysis — the JSON the MCP tools return.

Thin composition over :mod:`chim.save.ess` + :mod:`chim.save.heap`: parse the
container, decode the Papyrus heap front, and summarise. No mutation.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Optional

from typing import Tuple

from .ess import parse_save, SaveFile
from .heap import PapyrusHeap

_COMP = {0: "none", 1: "zlib", 2: "lz4"}


def _s(b: bytes) -> str:
    return b.decode("utf-8", "replace")


def _heap(sf: SaveFile) -> Optional[PapyrusHeap]:
    pap = sf.papyrus()
    return PapyrusHeap(pap.data) if pap is not None else None


def save_info(raw: bytes) -> Dict[str, Any]:
    """Container-level summary: header, plugins, global-data map, integrity."""
    sf = parse_save(raw)
    h = sf.header
    pap = sf.papyrus()
    return {
        "version": h.version,
        "form_version": sf.form_version,
        "compression": _COMP.get(h.compression, str(h.compression)),
        "player_name": _s(h.player_name),
        "player_level": h.player_level,
        "player_location": _s(h.player_location),
        "game_date": _s(h.game_date),
        "player_race": _s(h.player_race),
        "save_number": h.save_number,
        "full_plugin_count": len(sf.full_plugins),
        "light_plugin_count": len(sf.light_plugins),
        "change_form_count": sf.change_form_count,
        "global_data_types": sorted(set(sf.global_data_types())),
        "papyrus_present": pap is not None,
        "papyrus_bytes": len(pap.data) if pap else 0,
        "file_bytes": len(raw),
        "body_bytes": len(sf.body),
        "body_walk_clean": sf.body_roundtrips(),
    }


def count_orphans(raw: bytes) -> Dict[str, Any]:
    """Aggregate orphan counts from the Papyrus heap front."""
    sf = parse_save(raw)
    heap = _heap(sf)
    if heap is None:
        return {"papyrus_present": False}
    undefined = heap.undefined_script_names()
    undef_defs = sum(1 for sc in heap.scripts if heap.script_is_undefined(sc))
    undef_inst = sum(1 for i in heap.instances if heap.is_instance_undefined(i, undefined))
    unattached = sum(1 for i in heap.instances if i.is_unattached())
    return {
        "papyrus_present": True,
        "heap_walk_clean": heap.roundtrips(),
        "script_definitions": len(heap.scripts),
        "script_instances": len(heap.instances),
        "undefined_script_definitions": undef_defs,
        "undefined_instances": undef_inst,
        "unattached_instances": unattached,
    }


def clean(raw: bytes, mode: str = "undefined") -> Tuple[bytes, Dict[str, Any]]:
    """Return ``(cleaned_bytes, report)`` for a save with orphans removed.

    ``mode="undefined"`` removes removed-mod stub script instances + their stub
    definitions (targets e.g. ``mff_refalias``). ``mode="unattached"`` removes
    every RefID==0 instance (broader; the community-safe housekeeping op). Pure —
    does no I/O; the whole file re-serialises (FLT + sizes recomputed, recompressed).
    """
    sf = parse_save(raw)
    pap = sf.papyrus()
    if pap is None:
        raise ValueError("save has no Papyrus heap (GlobalData 1001)")
    heap = PapyrusHeap(pap.data)
    if mode == "unattached":
        res = heap.remove_unattached()
    elif mode == "undefined":
        res = heap.remove_undefined()
    else:
        raise ValueError(f"mode must be 'unattached' or 'undefined', got {mode!r}")
    pap.heap = heap
    cleaned = sf.serialize()
    report = {
        "mode": mode,
        "orig_bytes": len(raw),
        "cleaned_bytes": len(cleaned),
        "bytes_saved": len(raw) - len(cleaned),
        **res,
    }
    return cleaned, report


def list_undefined(raw: bytes, limit: int = 200) -> Dict[str, Any]:
    """Undefined (stubbed / removed-mod) and unattached script instances,
    grouped by script class — i.e. which departed mods left junk behind."""
    sf = parse_save(raw)
    heap = _heap(sf)
    if heap is None:
        return {"papyrus_present": False, "undefined": [], "unattached": []}
    undefined = heap.undefined_script_names()
    undef: Counter = Counter()
    unatt: Counter = Counter()
    for i in heap.instances:
        name = heap.instance_script_name(i)
        if heap.is_instance_undefined(i, undefined):
            undef[name] += 1
        if i.is_unattached():
            unatt[name] += 1
    # also surface undefined definitions with zero live instances
    for name in undefined:
        undef.setdefault(name, 0)
    return {
        "papyrus_present": True,
        "heap_walk_clean": heap.roundtrips(),
        "undefined_script_types": len(undef),
        "undefined_instances_total": sum(undef.values()),
        "unattached_instances_total": sum(unatt.values()),
        "undefined": [
            {"script": n, "instances": c} for n, c in undef.most_common(limit)
        ],
        "unattached": [
            {"script": n, "instances": c} for n, c in unatt.most_common(limit)
        ],
    }
