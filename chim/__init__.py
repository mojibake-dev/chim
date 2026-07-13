"""chim -- headless binary editing of Skyrim Special Edition plugin files.

The :mod:`chim.esp` subpackage contains the on-disk format model. The public
entry points most callers want are re-exported here for convenience::

    from chim import parse_plugin, serialize, walk_clean, Record, Group

See ``docs/SPEC.md`` for the authoritative format documentation and the exact
class / function interfaces that later phases build against.
"""

from __future__ import annotations

from .esp.records import (
    Record,
    Group,
    Plugin,
    parse_plugin,
    serialize,
    walk_clean,
    iterate,
    walk,
)

__all__ = [
    "Record",
    "Group",
    "Plugin",
    "parse_plugin",
    "serialize",
    "walk_clean",
    "iterate",
    "walk",
]

__version__ = "0.1.0"
