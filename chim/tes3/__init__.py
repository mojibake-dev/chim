"""Morrowind TES3 plugin editing -- chim's third byte engine.

Where :mod:`chim.esp` adapts the external esplib for Skyrim TES4/TES5 plugins and
:mod:`chim.save` hand-rolls the Skyrim ``.ess`` save grammar, this package
hand-rolls the **Morrowind TES3** plugin grammar (``.esp``/``.esm`` and the
byte-identical OpenMW ``.omwaddon``): a flat record list, no GRUP tree, no
compression, records addressed by string editor-id.

:mod:`chim.tes3.container` is the byte-exact container (parse / serialize /
roundtrip, byte-opaque below the subrecord boundary). Domain typing is composed
agent-side, so the same generic-primitive philosophy as :mod:`chim.esp.ops`
applies -- ``ops`` and ``analysis`` layer on top.

Format grounded in the UESP *Morrowind Mod:TES3 File Format* reference and the
OpenMW ESM reader; validated byte-exact against real Morrowind plugins.
"""

from .container import (
    Plugin,
    Record,
    Subrecord,
    ParseError,
    parse_plugin,
    serialize,
    roundtrips,
    MAGIC,
    FLAG_DELETED,
    FLAG_PERSISTENT,
)
from .bsa import Bsa, BsaError

__all__ = [
    "Plugin",
    "Record",
    "Subrecord",
    "ParseError",
    "parse_plugin",
    "serialize",
    "roundtrips",
    "MAGIC",
    "FLAG_DELETED",
    "FLAG_PERSISTENT",
    "Bsa",
    "BsaError",
]
