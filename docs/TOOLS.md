# chim MCP tools

This is the tool surface `chim` exposes for LLM-driven plugin editing, as
registered in [`chim/server.py`](../chim/server.py). Each tool is a thin FastMCP
wrapper over a function in [`chim/esp/ops.py`](../chim/esp/ops.py) (the esplib
adapter — see below), so the parameters here match those functions exactly.

> **Rewritten 2026-07-11.** The byte engine underneath every tool is now
> [esplib](https://github.com/BadDogSkyrim/esplib) (v0.2.0, BadDogSkyrim,
> MPL-2.0), reached through the `chim.esp.ops` adapter — *not* the original
> hand-rolled engine (which survives on disk as a fallback: `chim/esp/records.py`,
> `fields.py`, `subrecords.py`, `edit.py`, `query.py`, and `chim/server_legacy.py`
> — see the [README](../README.md)). There are now **19 tools** (an earlier
> revision of this doc said "eight").

> **Full reference.** This file is a one-line-per-tool index. For the exhaustive
> behavior, return shapes, worked examples, deployment, and every footgun, read
> [`docs/modding/chim-toolkit.md`](modding/chim-toolkit.md) — the current source
> of truth for the toolkit — and [`docs/modding/README.md`](modding/README.md) for
> the whole knowledge base.

## Conventions

- **`plugin`** — a bare plugin file *name* (`MyMod.esp`), joined to the host's
  configured data dir (`CHIM_DATA_DIR`, or the stock Steam SE `Data` path on a
  Windows/remote host). Absolute paths (drive-letter, UNC, or POSIX `/`) pass
  through untouched.
- **FormIDs** are full 32-bit ints and may be given as an int or a hex/decimal
  string (`"0x0003A3DD"`, `238557`); results return them as 8-hex-digit strings.
- **Subrecord signatures** are exactly 4 ASCII chars (`"XLKR"`). **Byte payloads**
  are hex strings (`"00201a00"`, `"0x00201A00"`, or spaced).
- **Mutating tools are transactional.** Every tool that writes runs inside
  `chim.esp.safety.transaction`: lock-check the host → timestamped `.bak` → edit
  → write back → verify walk-clean (via the legacy independent parser) → restore
  from the backup on a dirty result. Each returns `backup` = the `.bak` path.
  Read-only tools take no transaction.

---

## Read-only (no transaction)

- **`esp_query(plugin, by, value, sigs=None)`** — search a plugin. `by` selects
  the lookup: `"formid"` (the record with that FormID), `"edid"` (records whose
  EDID matches the regex `value`), `"base"` (placements instantiating base object
  `value`), `"cell"` (the CELL by EDID string or FormID), `"cellrefs"` (every
  placement in a CELL, with base/flags/xlkr), or `"basemap"` (`formid → (edid,
  sig, mesh, obnd)` for records whose signature is in `sigs`, which is required
  for this mode).
- **`esp_get_record(plugin, form_id)`** — full detail of one record: summary plus
  a decoded field list (`EDID`, `NAME`, `MODL`, `OBND`, 24-byte `DATA` pos/rot,
  `XLKR`, `XESP`, `XSCL` are decoded for convenience). Raises if absent.
- **`esp_get_subrecords(plugin, form_id, signature=None)`** — list a record's
  subrecords with raw `payload_hex` and each field's per-signature occurrence
  `index`. Optional `signature` filters (indices stay whole-record true). The tool
  you use to *plan* a surgical subrecord edit.
- **`esp_get_vmad(plugin, form_id)`** — decode a record's `VMAD` (Papyrus script
  data) to JSON (`{version, obj_format, scripts, fragment_data, alias_scripts}`,
  or `None`). The exact shape `esp_set_vmad` accepts, so read → edit → write
  round-trips.

## Save-file (`.ess`) inspection — read-only (no transaction)

A save is **not** a plugin — these use chim's second byte engine (`chim.save`,
the `.ess` container + the Papyrus VM heap, GlobalData type 1001). Phase 1 is
read-only. `save` is an `.ess` file name resolved against the SE `Saves` dir
(`CHIM_SAVE_DIR`, else the per-user `…\Documents\My Games\Skyrim Special
Edition\Saves`), or an absolute path.

- **`save_info(save)`** — container summary: header (version, formVersion,
  compression, player/level/location), full/light plugin counts, change-form
  count, the global-data types present, whether the Papyrus heap is present and
  its size, and `body_walk_clean` (the decompressed body re-serialises
  byte-identical — the save-side integrity check).
- **`save_count_orphans(save)`** — Papyrus-heap orphan tally: script definitions
  and instances, **undefined** definitions/instances (a script re-saved as an
  empty-type *stub* because its source mod was removed — the game's "Could not
  find type X in the type table" orphans), and **unattached** instances
  (`RefID == 0`, bound to nothing). `heap_walk_clean` = the decoded heap front
  re-serialises byte-identical.
- **`save_list_undefined(save, limit=200)`** — undefined + unattached script
  instances grouped by class: which departed mods left junk behind and how many
  instances each (e.g. `mff_refalias` from an uninstalled follower framework).
  Run this *before* trusting the cleaner.
- **`save_clean_orphans(save, mode="undefined", dry_run=True, out=None)`** — the
  one **mutating** save tool. Removes orphaned Papyrus instances and re-serialises
  the whole file (heap edited, FLT + sizes recomputed, recompressed).
  `mode="undefined"` drops removed-mod stub instances + their stub script defs
  (targets `mff_refalias` et al.); `mode="unattached"` drops every `RefID==0`
  instance (broader, community-safe housekeeping). **`dry_run=True` by default** —
  reports what would be removed, writes nothing. A real run refuses if the
  game/CK is running, **never overwrites the original** (writes a sibling
  `<name>_CHIMCLEAN.ess`, override with `out`), copies the paired `.skse` cosave,
  then re-reads + re-parses to verify orphans are gone. Recovery/housekeeping
  tooling — like ReSaver, a cleaned save *can* still be damaged; keep the original.

## Record / placement mutations (transaction)

- **`esp_set_flag(plugin, form_id, flag, clear=False)`** — set (or clear) a header
  flag bit. `flag` may be a symbolic name (`deleted`, `persistent`,
  `initially_disabled`/`disabled`) or a raw int/hex. Clearing `initially_disabled`
  (0x800) force-enables a reference.
- **`esp_move_ref(plugin, form_id, pos, rot)`** — overwrite a placement's `DATA`
  position/rotation. `pos` and `rot` are each 3 floats, packed as six float32
  (never a double). Appends a `DATA` if none exists.
- **`esp_clone_record(plugin, seed_form_id)`** — clone one whole record under a
  fresh FormID, inserted right after the seed in the seed's own container. Does
  NOT remap links or move anything — duplicate, then rewrite the clone's
  subrecords. This is how you author a brand-new top-level record (quest,
  activator, base object) headlessly.
- **`esp_clone_cluster(plugin, seed_form_id, count, translate, into_cell=None)`**
  — clone a record `count` times under fresh FormIDs, offset by `translate`
  (`[dx, dy, dz]`). Remaps intra-set `XLKR` links, strips `XESP` enable-parents,
  translates each clone's `DATA` position. Splices into `into_cell`'s type-8
  persistent GRUP if given, else leaves the clones unattached. Reports
  `external_links` (targets outside the cloned set).
- **`esp_insert_ref(plugin, cell_form_id, base, pos, rot, flags=0x400,
  xlkr=None)`** — build a fresh `REFR` (mint FormID, `NAME`=base, place at
  `pos`/`rot`, optional `XLKR` pairs) and splice it into a CELL's persistent
  (type-8) children GRUP. `flags` defaults to `0x400` (Persistent). The CELL must
  already exist with a children group.
- **`esp_delete_records(plugin, form_ids)`** — delete a (possibly non-contiguous)
  set of records by FormID; empty GRUPs left behind are kept. Returns the FormIDs
  actually removed.
- **`esp_delete_cells(plugin, cell_form_ids)`** — revert whole CELL overrides to
  vanilla: for each cell, delete the CELL record AND its trailing type-6 children
  GRUP (terrain + navmesh + refs). Returns `{reverted:[{cell, records_removed}],
  count}`.

## Subrecord (field) CRUD (transaction)

All are decompress-aware (store uncompressed, clear the compressed flag) and
`XXXX` size-override aware.

- **`esp_set_subrecord(plugin, form_id, signature, payload_hex, index=0)`** —
  replace the `index`-th `signature` subrecord's whole payload (may grow/shrink).
- **`esp_patch_subrecord(plugin, form_id, signature, offset, bytes_hex,
  index=0)`** — surgically overwrite bytes at `offset` **with the same length**
  (flip a flag byte, retarget a FormID inside a fixed struct). Must fit the
  existing payload.
- **`esp_insert_subrecord(plugin, form_id, signature, payload_hex, after=None,
  before=None, at_index=None)`** — insert a new subrecord. Give at most one
  position selector: `after` (after the LAST occurrence), `before` (before the
  FIRST), `at_index` (absolute), or none to append.
- **`esp_delete_subrecord(plugin, form_id, signature, index=0)`** — delete the
  `index`-th `signature` subrecord.

## VMAD authoring (transaction)

- **`esp_set_vmad(plugin, form_id, vmad_json)`** — author/overwrite a record's
  VMAD from a JSON dict of the same shape `esp_get_vmad` returns. Creates the
  subrecord if absent; recompresses transparently. SE-correct defaults baked in
  (`version=5`, `obj_format=2`, property `flags=1`). If `alias_scripts` are
  present but `fragment_data` is null, synthesizes an empty fragment block so QUST
  aliases don't silently vanish on serialize.

## Master-list management (transaction)

- **`esp_add_master(plugin, master_name)`** — append a master to the header
  (idempotent, case-insensitive); emits the `MAST`/`DATA` subrecords. Does NOT
  renumber existing FormIDs.
- **`esp_rename_master(plugin, old_name, new_name)`** — rename an existing master
  in place (touches no FormIDs). Raises if `old_name` is not a current master.

> Both master tools only **append or rename** — never insert mid-list. A master's
> positional index is the top byte of every FormID that references it, so
> inserting mid-list would silently corrupt every reference.

## Papyrus (no transaction)

- **`papyrus_compile(script_name, import_paths=None, output_dir=None,
  flags_file="TESV_Papyrus_Flags.flg")`** — compile a Papyrus `.psc` with the
  Creation Kit's `PapyrusCompiler.exe`. Runs remotely over SSH when
  `CHIM_SSH_HOST` is set, else in-process on a `LocalWindowsHost`. Import paths
  default to both source dirs SKSE-first; the command runs from a `.bat` via
  `cmd /c` (never PowerShell). Returns `{ok, exit_status, stdout, stderr, command,
  output_pex}`.

---

## Retired: `esp_wire_weapon_racks` (2026-07-11)

`esp_wire_weapon_racks` **was retired on 2026-07-11** and is no longer on the tool
surface. It was too domain-specific — it hardcoded weapon-rack base→activator
pairs and cross-link keywords. Per the general-primitives rule (chim tools stay
GENERAL; domain workflows are composed agent-side), **compose the same result**
from `esp_insert_ref` (mint the co-located activator REFR, with its `XLKR` and the
force-enabled flag) + `esp_insert_subrecord` (the reverse `XLKR` on the rack), and
keep the base→activator mapping in the agent. The retired mapping and keywords are
recorded in [`docs/modding/chim-toolkit.md`](modding/chim-toolkit.md) for
reference.
