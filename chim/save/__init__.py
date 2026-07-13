"""Skyrim SE save-file (``.ess``) reading — chim's second byte engine.

A save is **not** a plugin. Where :mod:`chim.esp` models records/GRUPs/subrecords
of an ``.esp``/``.esm``, this package models the ``.ess`` *save* grammar: an
uncompressed header prefix followed by an LZ4- (or zlib-) compressed body of
global-data tables, change forms, and — the part that matters for save
hygiene — the Papyrus VM heap (``GlobalData`` type ``1001``), where orphaned and
undefined script instances accumulate after mods are removed.

Phase 1 is **read-only**: parse the container (:mod:`chim.save.ess`), decode the
Papyrus heap deep enough to enumerate script instances
(:mod:`chim.save.heap`), and report (:mod:`chim.save.analysis`). No mutation.

Format grounded in ReSaver / FallrimTools (Apache-2.0) source and validated
byte-exact against real 1.6.1170 saves.
"""

from .ess import SaveFile, GlobalData, parse_save, PAPYRUS_TYPE
from .heap import PapyrusHeap, ScriptInstance, Script
from .analysis import save_info, list_undefined, count_orphans

__all__ = [
    "SaveFile",
    "GlobalData",
    "parse_save",
    "PAPYRUS_TYPE",
    "PapyrusHeap",
    "ScriptInstance",
    "Script",
    "save_info",
    "list_undefined",
    "count_orphans",
]
