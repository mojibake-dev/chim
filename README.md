<p align="center">
  <img src="assets/chim-daedric.png" width="240" alt="CHIM">
</p>

<p align="center">
  <em>&ldquo;The waking world is the amnesia of dream.&rdquo;</em><br>
  <sub>&mdash; Vivec, <em>The Thirty-Six Lessons of Vivec</em>, Sermon Eleven</sub>
</p>

# chim

Headless, byte-level editing of Skyrim Special Edition **plugin** (`.esp` / `.esm` /
`.esl`) and **save** (`.ess`) files — as a Python 3.11+ library and an
[MCP](https://modelcontextprotocol.io) server. No Creation Kit, no xEdit, no GUI.

```python
from chim.esp import ops

raw = open("Plugin.esp", "rb").read()
plugin = ops.load(raw)
assert ops.serialize(plugin) == raw      # byte-identical round-trip
```

## What it does

- **Edits plugins at the byte level** — records, GRUP containers, compressed
  payloads, subrecords, and VMAD script data — with a hard guarantee that a
  parsed-then-unmodified file serialises back **byte-for-byte identical**, and that
  every mutation stays *walk-clean* (re-parses `TES4` → EOF with zero leftover).
  The byte engine is [esplib](https://github.com/BadDogSkyrim/esplib), reached
  through the thin `chim.esp.ops` adapter.
- **Reads and cleans save files** — a second engine (`chim.save`) parses the
  `.ess` container and the Papyrus VM heap, so it can report and remove
  orphaned/undefined script instances (the removed-mod cruft a save accumulates) —
  ReSaver-style, but headless and scriptable.
- **Transactional live edits** — every write against a real install runs
  `lock → backup → verify → rollback`: it refuses if the game or Creation Kit is
  open, takes a timestamped backup, writes, re-verifies the result byte-clean, and
  restores from backup if anything's off.
- **An MCP server** — exposes all of the above as tools (query/edit plugins, author
  VMAD, manage masters, compile Papyrus, inspect/clean saves) so an LLM agent can
  drive it directly. See [`docs/TOOLS.md`](docs/TOOLS.md).

## Install

Uses [uv](https://docs.astral.sh/uv/). `esplib` isn't on PyPI, so it's declared as
a pinned git dependency — uv clones and builds it automatically (you need `git` on
the machine running uv).

```bash
uv sync                 # core: mcp + esplib
uv sync --extra remote  # + paramiko  (SSH RemoteHost / remote Papyrus compilation)
uv sync --extra dev     # + pytest
uv run chim-mcp         # run the MCP server
```

To update esplib, bump its `rev` under `[tool.uv.sources]` in `pyproject.toml` and
re-run `uv lock`.

## Quickstart

**Read / edit a plugin** (no remote host needed):

```python
from chim.esp import ops

plugin = ops.load(open("MyMod.esp", "rb").read())

for rec in ops.find_by_edid(plugin, r"^Rotfern"):        # records by EDID regex
    print(rec.signature, hex(rec.form_id.value), rec.editor_id)

ops.set_flag(plugin, 0x00034200, ops.FLAG_INITIALLY_DISABLED, clear=True)   # force-enable a ref
ops.move_ref(plugin, 0x00034200, pos=(1024.0, -512.0, 96.0), rot=(0, 0, 1.57))

open("MyMod.esp", "wb").write(ops.serialize(plugin))
```

**Inspect / clean a save**:

```python
from chim.save import analysis

raw = open("Save66.ess", "rb").read()
print(analysis.count_orphans(raw))                    # undefined + unattached script instances
cleaned, report = analysis.clean(raw, mode="undefined")   # strip removed-mod stubs
open("Save66_clean.ess", "wb").write(cleaned)
```

**Edit a live install atomically** (lock-checked, backed up, verified):

```python
from chim.esp.safety import RemoteHost, transaction, DEFAULT_DATA_DIR
from chim.esp import ops

with transaction(RemoteHost("user@host"), DEFAULT_DATA_DIR + r"\MyMod.esp") as txn:
    plugin = ops.load(txn.data)
    ops.set_flag(plugin, 0x00034200, 0x800, clear=True)
    txn.data = ops.serialize(plugin)
# committed and re-verified walk-clean, or rolled back from the backup
```

## Safety model

Editing a plugin the game or Creation Kit has open corrupts it, and a botched
write leaves an unusable file. `chim.esp.safety.transaction` wraps every live edit
in four guard rails:

1. **Lock check** — refuses (touching nothing) if the game, its launcher, the
   Creation Kit, or the CKPE loader is running.
2. **Backup** — copies the target to a timestamped `<name>.<YYYYmmdd-HHMMSS>.bak`.
3. **Edit** — hands you the current bytes; you parse, mutate, reassign.
4. **Verify or restore** — writes, re-reads, checks the result is walk-clean; on
   any failure it restores from the backup and raises. The same path runs against
   the local filesystem for tests, so it's exercised without a remote host.

The plugin format and CK toolchain are full of traps that fail *silently*, so chim
encodes the hard-won rules into its code paths and defaults — GRUP sizes and
`HEDR.num_records` re-derived from the live tree, 32-bit float packing,
decompress-before-edit, append-only master lists, and more. See
[`docs/SPEC.md`](docs/SPEC.md) and [`docs/TOOLS.md`](docs/TOOLS.md).

## Origin

chim grew out of driving a headless Skyrim SE modding setup entirely over SSH —
building a custom race, decoupling shared DLC bases, wiring quests, moving
interiors into new worldspaces — work you don't want to do by hand in the Creation
Kit or xEdit, and *can't* do through their GUIs on a machine with no display. It's
the toolkit that made that reproducible: byte-exact, reversible, scriptable.

## Layout

```
chim/
  chim/
    server.py     # MCP server (FastMCP tools over chim.esp.ops + chim.save)
    esp/          # plugin engine: ops.py (esplib adapter), safety.py, papyrus.py
    save/         # save engine: ess.py (container), heap.py (Papyrus VM heap), analysis.py
  docs/
    SPEC.md       # on-disk plugin format + interface contract
    TOOLS.md      # the MCP tool surface
  tests/
```

The repo also keeps the original hand-rolled plugin engine (`chim/esp/records.py`,
`fields.py`, `edit.py`, `query.py`, …) on disk as a fallback — the safety
transaction still uses its independent `walk_clean` to cross-check esplib's output
before committing.

## License

MIT.
